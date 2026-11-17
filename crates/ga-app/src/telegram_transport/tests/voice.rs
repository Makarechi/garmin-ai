use super::{FIXTURE_TOKEN, bind, request, respond};
use crate::telegram_transport::{MAX_VOICE_BYTES, TelegramTransportError};

use std::{env, error::Error, fs, time::Duration};

use serde_json::{Value, json};
use tokio::{io::AsyncWriteExt, net::TcpStream, time::timeout};

const FIXTURE_ID: &str = "synthetic_telegram_voice_id";
const FILE_PATH: &str = "voice/file_7.oga";

#[tokio::test]
#[ignore = "compares getFile and its download with the pinned Python Telegram SDK"]
async fn matches_voice_download_requests() -> Result<(), Box<dyn Error>> {
    let fixture: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_VOICE_DOWNLOAD")?)?)?;
    let (listener, transport) = bind().await?;
    let server = async {
        let (mut metadata, _) = listener.accept().await?;
        let captured = request(&mut metadata).await?;
        send_metadata(&mut metadata, FILE_PATH, Some(4)).await?;
        let (mut file, _) = listener.accept().await?;
        let download = request(&mut file).await?;
        respond(&mut file, "200 OK", &[], "OggS").await?;
        Ok::<_, Box<dyn Error>>(json!([captured, download]))
    };
    let (record, bytes) = tokio::join!(
        server,
        transport.download_voice(FIXTURE_ID, Some(600), Some(4))
    );
    assert_eq!(record?, fixture);
    assert_eq!(bytes?, b"OggS");
    Ok(())
}

#[tokio::test]
async fn rejects_oversized_or_unsafe_metadata_before_downloading() -> Result<(), Box<dyn Error>> {
    let (listener, transport) = bind().await?;
    for (duration, size) in [(Some(601), None), (None, Some(MAX_VOICE_BYTES as u64 + 1))] {
        assert_eq!(
            transport
                .download_voice(FIXTURE_ID, duration, size)
                .await
                .err(),
            Some(TelegramTransportError::ResponseTooLarge)
        );
    }
    assert!(
        timeout(Duration::from_millis(50), listener.accept())
            .await
            .is_err()
    );

    for (path, bytes, expected) in [
        (
            "../voice.ogg",
            None,
            TelegramTransportError::InvalidResponse,
        ),
        (
            "voice/%2e%2e/other",
            None,
            TelegramTransportError::InvalidResponse,
        ),
        (
            "//anything.invalid/voice.ogg",
            None,
            TelegramTransportError::InvalidResponse,
        ),
        (
            "voice/file.ogg?private=query",
            None,
            TelegramTransportError::InvalidResponse,
        ),
        (
            FILE_PATH,
            Some(MAX_VOICE_BYTES + 1),
            TelegramTransportError::ResponseTooLarge,
        ),
    ] {
        let (listener, transport) = bind().await?;
        let server = async {
            let (mut metadata, _) = listener.accept().await?;
            assert_eq!(request(&mut metadata).await?["endpoint"], "getFile");
            send_metadata(&mut metadata, path, bytes).await?;
            assert!(
                timeout(Duration::from_millis(50), listener.accept())
                    .await
                    .is_err()
            );
            Ok::<_, Box<dyn Error>>(())
        };
        let (server, result) =
            tokio::join!(server, transport.download_voice(FIXTURE_ID, None, None));
        server?;
        assert_eq!(result.err(), Some(expected));
    }
    Ok(())
}

#[tokio::test]
async fn caps_chunked_audio_and_rejects_partial_or_redirected_files() -> Result<(), Box<dyn Error>>
{
    for (status, expected) in [
        ("206 Partial Content", TelegramTransportError::Rejected),
        ("307 Temporary Redirect", TelegramTransportError::Rejected),
        (
            "429 Too Many Requests",
            TelegramTransportError::RateLimited(Duration::from_secs(3)),
        ),
    ] {
        let (listener, transport) = bind().await?;
        let server = async {
            let (mut metadata, _) = listener.accept().await?;
            request(&mut metadata).await?;
            send_metadata(&mut metadata, FILE_PATH, None).await?;
            let (mut file, _) = listener.accept().await?;
            request(&mut file).await?;
            respond(
                &mut file,
                status,
                &[("Location", "/redirect-ignored"), ("Retry-After", "3")],
                "",
            )
            .await?;
            assert!(
                timeout(Duration::from_millis(50), listener.accept())
                    .await
                    .is_err()
            );
            Ok::<_, Box<dyn Error>>(())
        };
        let (server, result) =
            tokio::join!(server, transport.download_voice(FIXTURE_ID, None, None));
        server?;
        assert_eq!(result.err(), Some(expected));
    }
    for extra in [false, true] {
        let (listener, transport) = bind().await?;
        let server = async {
            let (mut metadata, _) = listener.accept().await?;
            request(&mut metadata).await?;
            send_metadata(&mut metadata, FILE_PATH, None).await?;
            let (mut file, _) = listener.accept().await?;
            assert_eq!(
                request(&mut file).await?["endpoint"],
                format!("/file/bot{FIXTURE_TOKEN}/{FILE_PATH}")
            );
            file.write_all(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n",
            )
            .await?;
            const CHUNK: &[u8; 16 * 1024] = &[5; 16 * 1024];
            for _ in 0..MAX_VOICE_BYTES / CHUNK.len() {
                file.write_all(b"4000\r\n").await?;
                file.write_all(CHUNK).await?;
                file.write_all(b"\r\n").await?;
            }
            if extra {
                file.write_all(b"1\r\nx\r\n").await?;
            }
            let final_chunk = file.write_all(b"0\r\n\r\n").await;
            if !extra {
                final_chunk?;
            }
            Ok::<_, Box<dyn Error>>(())
        };
        let (server, result) =
            tokio::join!(server, transport.download_voice(FIXTURE_ID, None, None));
        server?;
        if extra {
            assert_eq!(result.err(), Some(TelegramTransportError::ResponseTooLarge));
        } else {
            let body = result?;
            assert_eq!(body.len(), MAX_VOICE_BYTES);
            assert!(body.iter().all(|byte| *byte == 5));
        }
    }
    Ok(())
}

async fn send_metadata(
    stream: &mut TcpStream,
    path: &str,
    size: Option<usize>,
) -> std::io::Result<()> {
    respond(
        stream,
        "200 OK",
        &[],
        &json!({"ok":true,"result":{"file_path":path,"file_size":size}}).to_string(),
    )
    .await
}
