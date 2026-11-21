use std::io;

use rmcp::service::RxJsonRpcMessage;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};

use super::super::{MAX_MESSAGE_BYTES, protocol::NativeServer};

// A three-byte legacy token becomes a five-byte JSON number. Apply the
// public limit to the original frame and keep the SDK's internal limit bounded.
pub(super) const MAX_NORMALIZED_BYTES: usize = MAX_MESSAGE_BYTES.div_ceil(3) * 5;

pub(super) async fn copy_frames<R: AsyncRead + Unpin, W: AsyncWrite + Unpin>(
    input: R,
    mut output: W,
) -> io::Result<()> {
    let mut reader = BufReader::new(input);
    let mut line = Vec::new();
    let mut oversized = false;
    loop {
        let data = reader.fill_buf().await?;
        if data.is_empty() {
            return output.shutdown().await;
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
            if !oversized {
                let normalized = normalize_nonfinite_numbers(&line);
                output
                    .write_all(normalized.as_deref().unwrap_or(&line))
                    .await?;
                line.clear();
            }
            oversized = false;
        }
    }
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
    serde_json::from_slice::<RxJsonRpcMessage<NativeServer>>(frame).ok()?;
    Some(normalized)
}

fn json_whitespace(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | b'\r')
}

#[cfg(test)]
mod tests {
    use tokio::io::AsyncReadExt;

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

    #[tokio::test]
    async fn enforces_the_original_line_limit_across_fragmented_input() -> io::Result<()> {
        let (mut send, input) = tokio::io::duplex(512);
        let (output, mut receive) = tokio::io::duplex(512);
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
        let mut wire = Vec::new();
        let (send, received) = tokio::join!(send_all, receive.read_to_end(&mut wire));
        send?;
        received?;
        copy.await.map_err(io::Error::other)??;
        let expected = [NORMALIZED, spaces.as_slice(), b"\n", NORMALIZED, b"\n"].concat();
        assert_eq!(wire, expected);
        Ok(())
    }
}
