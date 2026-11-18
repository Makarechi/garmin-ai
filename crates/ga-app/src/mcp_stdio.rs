use ga_db::{Database, diary::DiaryReadError, health_analysis::HealthAnalysisError};
use ga_domain::legacy_json::{Utf8Layout, encode};
use rmcp::{
    ErrorData, RoleServer, ServerHandler, ServiceExt,
    model::{
        CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, Implementation,
        ListToolsResult, PaginatedRequestParams, ProtocolVersion, ServerCapabilities, ServerConfig,
        Tool, ToolAnnotations,
    },
    service::RequestContext,
    transport::{async_rw::AsyncRwTransport, stdio_pipes},
};
use serde_json::{Value, json};
use tokio::sync::Semaphore;
use uuid::Uuid;

use crate::{
    configuration,
    http::tools::{GarminReadError, ReadTool, range_with_kind, read_garmin},
};

const INSTRUCTIONS: &str = include_str!("../../../src/garmin_ai/prompts/mcp_server.txt");
const MAX_MESSAGE_BYTES: usize = 1024 * 1024;
const MAX_RESULT_BYTES: usize = 8 * 1024 * 1024;

struct Mcp {
    database: Database,
    owner: Uuid,
    read_tools: Vec<Tool>,
    active_reads: Semaphore,
}

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn run() -> Result<(), &'static str> {
    let [url, writes] = configuration::read(["GA_DATABASE_URL", "GA_MCP_ENABLE_WRITES"])?;
    if let Some(value) = writes {
        match value.to_ascii_lowercase().as_str() {
            "0" | "off" | "false" | "f" | "no" | "n" => {}
            "1" | "on" | "true" | "t" | "yes" | "y" => {
                return Err("Diary writes are unavailable in this native MCP build");
            }
            _ => return Err("GA_MCP_ENABLE_WRITES must be a boolean"),
        }
    }
    let database = Database::connect_lazy(&url.ok_or("Storage configuration is invalid")?)
        .map_err(|_| "Storage configuration is invalid")?;
    let owner = database
        .enrolled_owner_id()
        .await
        .map_err(|_| "MCP storage is unavailable")?;
    let service = Mcp {
        database,
        owner,
        read_tools: std::iter::once(ReadTool::Events)
            .chain(ReadTool::MCP_GARMIN)
            .map(read_tool)
            .collect::<Result<_, _>>()?,
        active_reads: Semaphore::new(2),
    };
    let (input, output) = stdio_pipes();
    let transport = AsyncRwTransport::<RoleServer, _, _>::new(input, output)
        .with_max_line_length(MAX_MESSAGE_BYTES);
    service
        .serve(transport)
        .await
        .map_err(|_| "MCP initialization failed")?
        .waiting()
        .await
        .map_err(|_| "MCP session stopped unexpectedly")?;
    Ok(())
}

fn read_tool(tool: ReadTool) -> Result<Tool, &'static str> {
    let mut description = tool.catalog_entry();
    let schema = description["input_schema"].take();
    let Value::Object(schema) = schema else {
        return Err("The MCP read tool schema is invalid");
    };
    let description = description["description"]
        .as_str()
        .ok_or("The MCP read tool description is invalid")?;
    Ok(
        Tool::new(tool.name(), description.to_owned(), schema).with_annotations(
            ToolAnnotations::from_raw(None, Some(true), Some(false), None, Some(false)),
        ),
    )
}

impl ServerHandler for Mcp {
    fn get_info(&self) -> ServerConfig {
        let mut capabilities = ServerCapabilities::builder().enable_tools().build();
        capabilities.experimental = Some(Default::default());
        if let Some(tools) = capabilities.tools.as_mut() {
            tools.list_changed = Some(false);
        }
        ServerConfig::new(capabilities)
            .with_protocol_version(ProtocolVersion::V_2025_11_25)
            .with_server_info(Implementation::new("garmin-ai", "0.1.0"))
            .with_instructions(INSTRUCTIONS.trim())
    }

    async fn list_tools(
        &self,
        _: Option<PaginatedRequestParams>,
        _: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult::with_all_items(self.read_tools.clone()))
    }

    async fn call_tool(
        &self,
        call: CallToolRequestParams,
        _: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let result = match call.name.as_ref() {
            "events" => self.events(call.arguments.unwrap_or_default()).await,
            "events_create" | "events_update" | "events_delete" | "entries_create" => {
                Err("PermissionError")
            }
            tool_name => match ReadTool::MCP_GARMIN
                .into_iter()
                .find(|tool| tool.name() == tool_name)
            {
                Some(tool) => self.garmin(tool, call.arguments.unwrap_or_default()).await,
                None => Err("ValueError"),
            },
        };
        let mut result = result.unwrap_or_else(|class| CallToolResult::error(vec![ContentBlock::text(format!(
            "Operation failed ({class}); reload the record or check arguments. No sensitive details logged.",
        ))]));
        result.result_type = None;
        Ok(result.into())
    }
}

impl Mcp {
    async fn garmin(
        &self,
        tool: ReadTool,
        args: serde_json::Map<String, Value>,
    ) -> Result<CallToolResult, &'static str> {
        let _permit = self
            .active_reads
            .try_acquire()
            .map_err(|_| "RuntimeError")?;
        let result = read_garmin(&self.database, tool, &args, Some(self.owner))
            .await
            .map_err(garmin_error)?;
        tool_result(result)
    }

    async fn events(
        &self,
        args: serde_json::Map<String, Value>,
    ) -> Result<CallToolResult, &'static str> {
        let (start, end, kind) = range_with_kind(&args).ok_or("ValidationError")?;
        let _permit = self
            .active_reads
            .try_acquire()
            .map_err(|_| "RuntimeError")?;
        let page = self
            .database
            .list_mcp_events(self.owner, start, end, kind)
            .await
            .map_err(diary_error)?;
        tool_result(page.value)
    }
}

fn tool_result(value: Value) -> Result<CallToolResult, &'static str> {
    let value = if value.is_object() {
        value
    } else {
        json!({"rows": value})
    };
    let compact = serde_json::to_vec(&value).map_err(|_| "ValueError")?;
    let encoded =
        encode(&compact, Utf8Layout::Ordered, MAX_RESULT_BYTES).map_err(|_| "ValueError")?;
    let text = String::from_utf8(encoded).map_err(|_| "ValueError")?;
    let mut result = CallToolResult::success(vec![ContentBlock::text(text)]);
    result.structured_content = Some(value);
    Ok(result)
}

fn garmin_error(error: GarminReadError) -> &'static str {
    match error {
        GarminReadError::Arguments => "ValidationError",
        GarminReadError::Missing => "LookupError",
        GarminReadError::Permission => "PermissionError",
        GarminReadError::Health(HealthAnalysisError::ReplayPending) => "ReplayUnavailable",
        GarminReadError::Health(
            HealthAnalysisError::Storage(_) | HealthAnalysisError::Database,
        ) => "RuntimeError",
        _ => "ValueError",
    }
}

fn diary_error(error: DiaryReadError) -> &'static str {
    match error {
        DiaryReadError::ModelConsent | DiaryReadError::Owner => "PermissionError",
        DiaryReadError::Database | DiaryReadError::Storage(_) => "RuntimeError",
        _ => "ValueError",
    }
}
