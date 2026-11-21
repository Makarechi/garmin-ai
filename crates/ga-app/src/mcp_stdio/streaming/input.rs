use std::io;

use rmcp::{
    model::{CallToolRequest, ClientRequest, JsonRpcVersion2_0, RequestId},
    service::RxJsonRpcMessage,
    transport::async_rw::{JsonRpcMessageCodec, JsonRpcMessageCodecError},
};
use serde::Deserialize;
use serde_json::value::RawValue;
use tokio::{
    io::{AsyncBufReadExt, AsyncRead, BufReader},
    sync::mpsc,
};
use tokio_util::{bytes::BytesMut, codec::Decoder};

use super::super::{MAX_MESSAGE_BYTES, protocol::NativeServer};

pub(super) async fn copy_frames<R: AsyncRead + Unpin>(
    input: R,
    output: mpsc::Sender<BytesMut>,
) -> io::Result<()> {
    let mut reader = BufReader::new(input);
    let mut line = BytesMut::new();
    let mut oversized = false;
    loop {
        let data = reader.fill_buf().await?;
        if data.is_empty() {
            return Ok(());
        }
        let newline = data.iter().position(|byte| *byte == b'\n');
        let count = newline.map_or(data.len(), |index| index + 1);
        if !oversized {
            if line.len() + count <= MAX_MESSAGE_BYTES {
                line.extend_from_slice(&data[..count]);
            } else {
                line.clear();
                oversized = true;
            }
        }
        reader.consume(count);
        if newline.is_some() {
            if !oversized && output.send(line.split()).await.is_err() {
                return Ok(());
            }
            oversized = false;
        }
    }
}

pub(super) fn decode(
    mut frame: BytesMut,
) -> Result<Option<RxJsonRpcMessage<NativeServer>>, JsonRpcMessageCodecError> {
    let normalized = normalize_nonfinite_numbers(&frame);
    let input = normalized.as_deref().unwrap_or(&frame);
    let json = input.strip_prefix(b"\xef\xbb\xbf").unwrap_or(input);
    // The SDK's general JSON-RPC decoder buffers the full payload for
    // flattened/untagged variants. Read tool arguments directly into the
    // SDK's typed request so a dense bounded argument list is parsed once.
    if let Ok(envelope) = serde_json::from_slice::<RequestEnvelope<'_>>(json)
        && envelope.method == "tools/call"
        && let (Some(id), Some(params)) = (envelope.id, envelope.params)
        && params.get().starts_with('{')
    {
        let params = serde_json::from_str(params.get())?;
        let mut request = CallToolRequest::new(params);
        if let Some(meta) = request.params.meta.take() {
            request.extensions.insert(meta);
        }
        let request = ClientRequest::CallToolRequest(request);
        return Ok(Some(RxJsonRpcMessage::<NativeServer>::request(request, id)));
    }
    if let Some(normalized) = normalized {
        frame = BytesMut::from(normalized.as_slice());
    }
    JsonRpcMessageCodec::new().decode(&mut frame)
}

fn normalize_nonfinite_numbers(line: &[u8]) -> Option<Vec<u8>> {
    let json = line.strip_prefix(b"\xef\xbb\xbf").unwrap_or(line);
    if json.iter().find(|byte| !json_whitespace(**byte)) != Some(&b'{') {
        return None;
    }
    let mut normalized = Vec::new();
    let mut start = 0;
    let mut index = 0;
    let mut quoted = false;
    while let Some(&byte) = line.get(index) {
        if byte == b'"' {
            quoted = !quoted;
        } else if quoted && byte == b'\\' {
            index += 1;
        } else if !quoted {
            let token = match byte {
                b'N' => b"NaN".as_slice(),
                b'I' => b"Infinity".as_slice(),
                b'-' => b"-Infinity".as_slice(),
                _ => b"",
            };
            let end = index + token.len();
            if !token.is_empty()
                && line[index..].starts_with(token)
                && line
                    .get(end)
                    .is_none_or(|byte| json_whitespace(*byte) || matches!(byte, b',' | b']' | b'}'))
                && matches!(
                    line[..index]
                        .iter()
                        .rev()
                        .find(|byte| !json_whitespace(**byte)),
                    Some(b':' | b'[' | b',')
                )
            {
                normalized.extend_from_slice(&line[start..index]);
                normalized.extend_from_slice(if byte == b'-' { b"-1e400" } else { b"1e400" });
                start = end;
                index = end;
                continue;
            }
        }
        index += 1;
    }
    if start == 0 {
        return None;
    }
    normalized.extend_from_slice(&line[start..]);
    // Preserve malformed frame handling. Overflowing numbers already follow
    // the same application validation as the pinned Python nonfinite values.
    let frame = normalized
        .strip_prefix(b"\xef\xbb\xbf")
        .unwrap_or(&normalized);
    serde_json::from_slice::<RequestEnvelope<'_>>(frame).ok()?;
    Some(normalized)
}

#[derive(Deserialize)]
struct RequestEnvelope<'a> {
    #[serde(rename = "jsonrpc")]
    _version: JsonRpcVersion2_0,
    id: Option<RequestId>,
    method: String,
    #[serde(borrow)]
    params: Option<&'a RawValue>,
    #[serde(rename = "result", default, deserialize_with = "reject_response_field")]
    _result: (),
    #[serde(rename = "error", default, deserialize_with = "reject_response_field")]
    _error: (),
}

fn reject_response_field<'de, D: serde::Deserializer<'de>>(_: D) -> Result<(), D::Error> {
    Err(serde::de::Error::custom("Not a JSON-RPC request"))
}

fn json_whitespace(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | b'\r')
}

#[cfg(test)]
mod tests {
    use tokio::io::AsyncWriteExt;

    use super::*;

    const REQUEST: &[u8] = br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"events","arguments":{"a":[NaN,Infinity,-Infinity],"text":"a \"NaN\" and Infinity"}}}"#;
    const NORMALIZED: &[u8] = br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"events","arguments":{"a":[1e400,1e400,-1e400],"text":"a \"NaN\" and Infinity"}}}"#;

    #[test]
    fn preserves_quoted_text_and_rejects_invalid_message_shapes() {
        assert_eq!(
            normalize_nonfinite_numbers(REQUEST).as_deref(),
            Some(NORMALIZED)
        );
        for invalid in [
            b"NaN".as_slice(),
            br#"{"jsonrpc":"2.0","id":NaN,"method":"ping"}"#,
            br#"{"jsonrpc":"2.0","id":1,"method":"ping","other":NaN1}"#,
            br#"{"jsonrpc":"2.0","id":1,"method":"ping","other":NaN} extra"#,
        ] {
            assert!(normalize_nonfinite_numbers(invalid).is_none());
        }
    }

    #[test]
    fn tool_call_keeps_the_sdk_request_metadata() -> Result<(), Box<dyn std::error::Error>> {
        use rmcp::model::RequestMetaObject;

        let message = br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"events","_meta":{"progressToken":"synthetic"}}}"#;
        let sdk_message: RxJsonRpcMessage<NativeServer> = serde_json::from_slice(message)?;
        let actual = decode(BytesMut::from(message.as_slice()))?.ok_or("Missing MCP request")?;
        let (
            Some((ClientRequest::CallToolRequest(expected), expected_id)),
            Some((ClientRequest::CallToolRequest(actual), actual_id)),
        ) = (sdk_message.into_request(), actual.into_request())
        else {
            return Err("Expected a tool request".into());
        };
        assert_eq!(actual_id, expected_id);
        assert_eq!(actual.params, expected.params);
        let expected = expected.extensions.get::<RequestMetaObject>();
        assert!(expected.is_some());
        assert_eq!(actual.extensions.get::<RequestMetaObject>(), expected);
        Ok(())
    }

    #[tokio::test]
    async fn enforces_the_original_line_limit_across_fragmented_input() -> io::Result<()> {
        let (mut send, input) = tokio::io::duplex(512);
        let (output, mut receive) = mpsc::channel(1);
        let copy = tokio::spawn(copy_frames(input, output));
        let spaces = vec![b' '; MAX_MESSAGE_BYTES - REQUEST.len() - 1];
        let send_all = async {
            for fragment in REQUEST.chunks(3) {
                send.write_all(fragment).await?;
            }
            send.write_all(&spaces).await?;
            send.write_all(b"\n").await?;
            send.write_all(REQUEST).await?;
            send.write_all(&spaces).await?;
            send.write_all(b" \n").await?;
            send.write_all(REQUEST).await?;
            send.write_all(b"\n").await?;
            send.write_all(REQUEST).await?;
            send.shutdown().await
        };
        let read_all = async {
            let mut wire = Vec::new();
            while let Some(frame) = receive.recv().await {
                wire.extend_from_slice(&frame);
            }
            wire
        };
        let (send, wire) = tokio::join!(send_all, read_all);
        send?;
        copy.await.map_err(io::Error::other)??;
        let expected = [REQUEST, spaces.as_slice(), b"\n", REQUEST, b"\n"].concat();
        assert_eq!(wire, expected);
        Ok(())
    }
}
