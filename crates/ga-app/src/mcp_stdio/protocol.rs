use std::borrow::Cow;

use rmcp::{
    ErrorData, Service,
    model::{
        CancelledNotificationParam, ClientNotification, ClientRequest, ClientResult, EmptyResult,
        ErrorCode, InitializeRequestParams, ListToolsResult, ProtocolVersion, ServerConfig,
        ServerNotification, ServerRequest, ServerResult,
    },
    service::{NotificationContext, RequestContext, ServiceRole, serve_directly},
    transport::stdio_pipes,
};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use super::{Mcp, response::ToolResult, streaming::StreamingStdio};

const SUPPORTED: [ProtocolVersion; 4] = [
    ProtocolVersion::V_2024_11_05,
    ProtocolVersion::V_2025_03_26,
    ProtocolVersion::V_2025_06_18,
    ProtocolVersion::V_2025_11_25,
];

#[derive(Clone, Copy, Debug)]
pub(super) struct NativeServer;

impl ServiceRole for NativeServer {
    type Req = ServerRequest;
    type Resp = Response;
    type Not = ServerNotification;
    type PeerReq = ClientRequest;
    type PeerResp = ClientResult;
    type PeerNot = ClientNotification;
    type Info = ServerConfig;
    type PeerInfo = InitializeRequestParams;
    type InitializeError = std::io::Error;
    const IS_CLIENT: bool = false;

    fn peer_cancelled_params(notification: &Self::PeerNot) -> Option<&CancelledNotificationParam> {
        match notification {
            ClientNotification::CancelledNotification(notification) => Some(&notification.params),
            _ => None,
        }
    }
}

#[derive(Clone, Debug)]
pub(super) enum Response {
    Protocol(Box<ServerResult>),
    Tool(ToolResult),
}

impl Serialize for Response {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Protocol(result) => result.serialize(serializer),
            Self::Tool(result) => result.serialize(serializer),
        }
    }
}

impl<'de> Deserialize<'de> for Response {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        // The SDK's transfer-object contract also requires decoding our own response type.
        ServerResult::deserialize(deserializer).map(|result| Self::Protocol(Box::new(result)))
    }
}

pub(super) async fn serve(service: Mcp) -> Result<(), &'static str> {
    let (input, output) = stdio_pipes();
    let transport = StreamingStdio::new(input, output);
    serve_directly(service, transport, None)
        .waiting()
        .await
        .map_err(|_| "MCP session stopped unexpectedly")?;
    Ok(())
}

impl Service<NativeServer> for Mcp {
    fn get_info(&self) -> ServerConfig {
        Mcp::get_info(self)
    }

    fn supported_protocol_versions(&self) -> Cow<'static, [ProtocolVersion]> {
        Cow::Borrowed(&SUPPORTED)
    }

    async fn handle_request(
        &self,
        request: ClientRequest,
        context: RequestContext<NativeServer>,
    ) -> Result<Response, ErrorData> {
        match request {
            ClientRequest::InitializeRequest(request) => {
                let mut info = self.get_info();
                if SUPPORTED.contains(&request.params.protocol_version) {
                    info.protocol_version = request.params.protocol_version.clone();
                }
                let mut peer = request.params;
                peer.protocol_version = info.protocol_version.clone();
                context.peer.set_peer_info(peer);
                Ok(Response::Protocol(Box::new(
                    ServerResult::InitializeResult(info),
                )))
            }
            ClientRequest::PingRequest(_) => Ok(Response::Protocol(Box::new(
                ServerResult::EmptyResult(EmptyResult {}),
            ))),
            _ if context.peer.peer_info().is_none() => Err(ErrorData::invalid_request(
                "MCP initialization is required",
                None,
            )),
            ClientRequest::ListToolsRequest(_) => {
                let mut tools = self.read_tools.clone();
                tools.extend(self.write_tools.iter().cloned());
                Ok(Response::Protocol(Box::new(ServerResult::ListToolsResult(
                    ListToolsResult::with_all_items(tools),
                ))))
            }
            ClientRequest::CallToolRequest(request) => Ok(Response::Tool(
                self.call_tool(request.params, context).await,
            )),
            _ => Err(ErrorData::new(
                ErrorCode::METHOD_NOT_FOUND,
                "Method not found",
                None,
            )),
        }
    }

    async fn handle_notification(
        &self,
        _: ClientNotification,
        _: NotificationContext<NativeServer>,
    ) -> Result<(), ErrorData> {
        Ok(())
    }
}
