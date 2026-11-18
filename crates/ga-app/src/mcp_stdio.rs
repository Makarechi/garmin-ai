use ga_db::{
    Database,
    diary::{
        CreateSystemEvent, DeleteEvent, DiaryReadError, DiaryWriteError,
        custom_entries::CreateCustomEntry,
    },
    health_analysis::HealthAnalysisError,
};
use ga_domain::{
    event_input::{EventInput, ValidationMode},
    event_replay::{ReplayConflict, check_idempotency_key},
    event_schema::CustomEntry,
    legacy_json::{Utf8Layout, encode},
};
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
use serde_json::{Map, Value, json};
use tokio::sync::Semaphore;
use uuid::Uuid;

use crate::{
    configuration,
    http::tools::{GarminReadError, ReadTool, range_with_kind, read_garmin},
};

const INSTRUCTIONS: &str = include_str!("../../../src/garmin_ai/prompts/mcp_server.txt");
const WRITE_TOOLS: &[&str] = &[
    include_str!("../contracts/mcp-events-create.json"),
    include_str!("../contracts/mcp-events-update.json"),
    include_str!("../contracts/mcp-events-delete.json"),
    include_str!("../contracts/mcp-entries-create.json"),
];
const MAX_MESSAGE_BYTES: usize = 1024 * 1024;
const MAX_RESULT_BYTES: usize = 8 * 1024 * 1024;

struct Mcp {
    database: Database,
    owner: Uuid,
    timezone: String,
    read_tools: Vec<Tool>,
    write_tools: Vec<Tool>,
    active_reads: Semaphore,
}

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn run() -> Result<(), &'static str> {
    let [url, writes, timezone] =
        configuration::read(["GA_DATABASE_URL", "GA_MCP_ENABLE_WRITES", "GA_TIMEZONE"])?;
    let writes = match writes.as_deref().map(str::to_ascii_lowercase).as_deref() {
        None | Some("0" | "off" | "false" | "f" | "no" | "n") => false,
        Some("1" | "on" | "true" | "t" | "yes" | "y") => true,
        _ => return Err("GA_MCP_ENABLE_WRITES must be a boolean"),
    };
    let database = Database::connect_lazy(&url.ok_or("Storage configuration is invalid")?)
        .map_err(|_| "Storage configuration is invalid")?;
    let owner = database
        .enrolled_owner_id()
        .await
        .map_err(|_| "MCP storage is unavailable")?;
    let service = Mcp {
        database,
        owner,
        timezone: timezone.unwrap_or_else(|| "Europe/Bratislava".to_owned()),
        read_tools: std::iter::once(ReadTool::Events)
            .chain(ReadTool::GARMIN_READS)
            .chain(std::iter::once(ReadTool::Freshness))
            .map(read_tool)
            .collect::<Result<_, _>>()?,
        write_tools: WRITE_TOOLS
            .iter()
            .take(if writes { WRITE_TOOLS.len() } else { 0 })
            .map(|schema| serde_json::from_str(schema))
            .collect::<Result<_, _>>()
            .map_err(|_| "The MCP write descriptor is invalid")?,
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
        let mut tools = self.read_tools.clone();
        tools.extend(self.write_tools.iter().cloned());
        Ok(ListToolsResult::with_all_items(tools))
    }

    async fn call_tool(
        &self,
        call: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let args = call.arguments.unwrap_or_default();
        let is_cancelled = || context.ct.is_cancelled();
        let enable_writes = !self.write_tools.is_empty();
        let result = match call.name.as_ref() {
            "events" => self.events(args).await,
            "data_freshness" => self.garmin(ReadTool::Freshness, args).await,
            "events_create" if enable_writes => self.create(args, is_cancelled).await,
            "events_update" if enable_writes => self.update(args, is_cancelled).await,
            "events_delete" if enable_writes => self.delete(args, is_cancelled).await,
            "entries_create" if enable_writes => self.create_custom(args, is_cancelled).await,
            "events_create" | "events_update" | "events_delete" | "entries_create" => {
                Err("PermissionError")
            }
            tool_name => match ReadTool::GARMIN_READS
                .into_iter()
                .find(|tool| tool.name() == tool_name)
            {
                Some(tool) => self.garmin(tool, args).await,
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
    async fn update(
        &self,
        mut args: Map<String, Value>,
        is_cancelled: impl Fn() -> bool,
    ) -> Result<CallToolResult, &'static str> {
        let Some(Value::Object(changes)) = args.remove("changes") else {
            return Err("ValidationError");
        };
        if !(1..=30).contains(&changes.len()) {
            return Err("ValidationError");
        }
        let (event_id, expected_revision) = revision_target(&args)?;
        let updated = self
            .database
            .correct_mcp_event_unless_cancelled(
                self.owner,
                event_id,
                expected_revision,
                changes,
                is_cancelled,
            )
            .await
            .map_err(write_error)?;
        tool_result(updated.snapshot)
    }

    async fn delete(
        &self,
        args: Map<String, Value>,
        is_cancelled: impl Fn() -> bool,
    ) -> Result<CallToolResult, &'static str> {
        let (event_id, expected_revision) = revision_target(&args)?;
        let deleted = self
            .database
            .delete_event_unless_cancelled(
                DeleteEvent {
                    owner_id: self.owner,
                    authenticated_actor: "mcp",
                    event_id,
                    expected_revision,
                },
                is_cancelled,
            )
            .await
            .map_err(write_error)?;
        tool_result(deleted.snapshot)
    }

    async fn garmin(
        &self,
        tool: ReadTool,
        args: serde_json::Map<String, Value>,
    ) -> Result<CallToolResult, &'static str> {
        let _permit = self
            .active_reads
            .try_acquire()
            .map_err(|_| "RuntimeError")?;
        if tool == ReadTool::Freshness {
            if !args.is_empty() {
                return Err("ValidationError");
            }
            let micros = time::OffsetDateTime::now_utc()
                .unix_timestamp_nanos()
                .div_euclid(1000);
            let at = ga_domain::timestamp::StoredInstant::from_unix_microseconds(
                i64::try_from(micros).map_err(|_| "RuntimeError")?,
            )
            .map_err(|_| "RuntimeError")?;
            let result = self
                .database
                .model_data_freshness(self.owner, at, &self.timezone)
                .await
                .map_err(|error| garmin_error(GarminReadError::Health(error)))?
                .ok_or("PermissionError")?;
            return tool_result(result);
        }
        let result = read_garmin(
            &self.database,
            tool,
            &args,
            Some(self.owner),
            &self.timezone,
        )
        .await
        .map_err(garmin_error)?;
        tool_result(result)
    }

    async fn create(
        &self,
        args: Map<String, Value>,
        is_cancelled: impl Fn() -> bool,
    ) -> Result<CallToolResult, &'static str> {
        let (event, key) = creation_arguments(args, "event")?;
        let input = serde_json::to_vec(&event).map_err(|_| "ValidationError")?;
        let report = EventInput::from_json(&input, MAX_MESSAGE_BYTES, ValidationMode::NewReport)
            .and_then(EventInput::into_mcp_report)
            .map_err(|_| "ValidationError")?;
        let entry = self
            .database
            .create_system_event_unless_cancelled(
                CreateSystemEvent {
                    owner_id: self.owner,
                    authenticated_actor: "mcp",
                    report: &report,
                    idempotency_key: Some(&key),
                    operation_id: None,
                    clock_uncertainty_seconds: None,
                },
                is_cancelled,
            )
            .await
            .map_err(write_error)?;
        tool_result(entry.snapshot)
    }

    async fn create_custom(
        &self,
        args: Map<String, Value>,
        is_cancelled: impl Fn() -> bool,
    ) -> Result<CallToolResult, &'static str> {
        let (entry, key) = creation_arguments(args, "entry")?;
        if entry["values"].get("type").is_some() {
            return Err("ValueError");
        }
        let entry = CustomEntry::from_value(entry)
            .map_err(|_| "ValidationError")?
            .into_mcp_entry();
        let stored = self
            .database
            .create_custom_entry_unless_cancelled(
                CreateCustomEntry {
                    owner_id: self.owner,
                    actor: "mcp",
                    entry: &entry,
                    idempotency_key: Some(&key),
                    evidence_refs: &[],
                },
                is_cancelled,
            )
            .await
            .map_err(write_error)?;
        tool_result(stored.snapshot)
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

fn creation_arguments(
    mut args: Map<String, Value>,
    field: &str,
) -> Result<(Value, String), &'static str> {
    let entry = args.remove(field).ok_or("ValidationError")?;
    let Some(Value::String(key)) = args.remove("idempotency_key") else {
        return Err("ValidationError");
    };
    if !args.is_empty() || check_idempotency_key(&key).is_err() {
        return Err("ValidationError");
    }
    Ok((entry, key))
}

fn revision_target(args: &Map<String, Value>) -> Result<(Uuid, i32), &'static str> {
    if args.len() != 2 {
        return Err("ValidationError");
    }
    let event = args
        .get("event_id")
        .and_then(Value::as_str)
        .and_then(|id| Uuid::parse_str(id).ok())
        .ok_or("ValidationError")?;
    let revision = args
        .get("revision")
        .and_then(positive_revision)
        .ok_or("ValidationError")?;
    Ok((event, revision))
}

fn positive_revision(value: &Value) -> Option<i32> {
    let revision = match value {
        Value::Number(number) => number.as_f64()?,
        Value::Bool(true) => 1.0,
        Value::String(text) => {
            let text = text.trim();
            if text.contains(['e', 'E']) {
                return None;
            }
            text.replace('_', "").parse().ok()?
        }
        _ => return None,
    };
    if !revision.is_finite() || revision < 1.0 || revision.fract() != 0.0 {
        return None;
    }
    // An out-of-range revision cannot match a stored PostgreSQL integer.
    Some(if revision > f64::from(i32::MAX) {
        0
    } else {
        revision as i32
    })
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

fn write_error(error: DiaryWriteError) -> &'static str {
    match error {
        DiaryWriteError::UnknownOwner
        | DiaryWriteError::DeletionForbidden
        | DiaryWriteError::OperationForbidden
        | DiaryWriteError::TrackingDisabled => "PermissionError",
        DiaryWriteError::Input(_) => "ValidationError",
        DiaryWriteError::EventMissing => "LookupError",
        DiaryWriteError::RevisionChanged
        | DiaryWriteError::Replay(
            ReplayConflict::AuditUnavailable | ReplayConflict::InputChanged,
        )
        | DiaryWriteError::LinkedObservations
        | DiaryWriteError::SelfReference(_)
        | DiaryWriteError::SymptomBounds => "Conflict",
        DiaryWriteError::Storage(_)
        | DiaryWriteError::Cancelled
        | DiaryWriteError::Busy
        | DiaryWriteError::Database => "RuntimeError",
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
