use std::{env, error::Error, fs, io, time::Duration};

use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::oneshot,
    time::timeout,
};

use super::{TelegramTransport, TelegramTransportError};

mod garmin_notices;
mod reminders;
mod storage_notices;
mod voice;

const FIXTURE_TOKEN: &str = "100:synthetic-transport";

#[tokio::test]
#[ignore = "compares the real local request with the pinned Python SDK recording"]
async fn matches_poll_and_acknowledgement_requests() -> Result<(), Box<dyn Error>> {
    let fixture: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_TRANSPORT")?)?)?;
    let (listener, transport) = bind().await?;
    let server = async {
        let mut captured = Vec::new();
        for result in [
            json!([]),
            json!([{"update_id":107,"message":{"text":"synthetic"}}]),
            json!(true),
        ] {
            let (mut stream, _) = listener.accept().await?;
            captured.push(request(&mut stream).await?);
            respond(
                &mut stream,
                "200 OK",
                &[],
                &json!({"ok": true, "result":result}).to_string(),
            )
            .await?;
        }
        Ok::<_, Box<dyn Error>>(Value::Array(captured))
    };
    let client = async {
        assert!(transport.get_updates(None).await?.is_empty());
        let updates = transport.get_updates(Some(107)).await?;
        assert_eq!(updates.len(), 1);
        assert_eq!(updates[0].id, 107);
        assert_eq!(updates[0].body["message"]["text"], "synthetic");
        transport.acknowledge_callback("synthetic/ack").await
    };
    let (captured, response) = tokio::join!(server, client);
    response?;
    assert_eq!(captured?, fixture);
    Ok(())
}

#[tokio::test]
async fn acknowledges_a_callback_while_polling_waits() -> Result<(), Box<dyn Error>> {
    let (listener, transport) = bind().await?;
    let (started, polling) = oneshot::channel();
    let server = async {
        let (mut poll, _) = listener.accept().await?;
        assert_eq!(request(&mut poll).await?["endpoint"], "getUpdates");
        started
            .send(())
            .map_err(|_| io::Error::other("Polling did not start"))?;
        let (mut ack, _) = timeout(Duration::from_secs(2), listener.accept()).await??;
        assert_eq!(request(&mut ack).await?["endpoint"], "answerCallbackQuery");
        respond(&mut ack, "200 OK", &[], r#"{"ok":true,"result":true}"#).await?;
        respond(&mut poll, "200 OK", &[], r#"{"ok":true,"result":[]}"#).await?;
        Ok::<_, Box<dyn Error>>(())
    };
    let ack = async {
        polling.await?;
        transport.acknowledge_callback("synthetic/parallel").await?;
        Ok::<_, Box<dyn Error>>(())
    };
    let (server, poll, ack) = tokio::join!(server, transport.get_updates(None), ack);
    server?;
    assert!(poll?.is_empty());
    ack
}

#[tokio::test]
async fn bounds_the_body_and_keeps_upstream_details_private() -> Result<(), Box<dyn Error>> {
    let cases = [
        (
            "429 Too Many Requests",
            Some(("Retry-After", "20")),
            r#"{"ok":false,"description":"synthetic provider detail","parameters":{"retry_after":60}}"#,
            TelegramTransportError::RateLimited(Duration::from_secs(60)),
        ),
        (
            "200 OK",
            None,
            r#"{"ok":false,"error_code":429,"parameters":{"retry_after":13}}"#,
            TelegramTransportError::RateLimited(Duration::from_secs(13)),
        ),
        (
            "403 Forbidden",
            None,
            "synthetic private error",
            TelegramTransportError::Authorization,
        ),
        (
            "500 Internal Server Error",
            None,
            "synthetic server error",
            TelegramTransportError::Unavailable,
        ),
        (
            "200 OK",
            None,
            r#"{"ok":true,"result":[{"update_id":"bad"}]}"#,
            TelegramTransportError::InvalidResponse,
        ),
        (
            "200 OK",
            Some(("Content-Encoding", "gzip")),
            "synthetic encoded response",
            TelegramTransportError::InvalidResponse,
        ),
    ];
    for (status, header, body, expected) in cases {
        let (listener, transport) = bind().await?;
        let server = async {
            let (mut stream, _) = listener.accept().await?;
            request(&mut stream).await?;
            respond(&mut stream, status, header.as_slice(), body).await?;
            Ok::<_, Box<dyn Error>>(())
        };
        let (result, response) = tokio::join!(server, transport.get_updates(None));
        result?;
        assert_eq!(response.err(), Some(expected));
    }
    let (listener, transport) = bind().await?;
    let server = async {
        let (mut stream, _) = listener.accept().await?;
        request(&mut stream).await?;
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 8388609\r\n\r\n")
            .await?;
        Ok::<_, Box<dyn Error>>(())
    };
    let (result, response) = tokio::join!(server, transport.get_updates(None));
    result?;
    assert_eq!(
        response.err(),
        Some(TelegramTransportError::ResponseTooLarge)
    );
    Ok(())
}

#[tokio::test]
async fn avoids_redirects_retries_and_destructive_negative_offsets() -> Result<(), Box<dyn Error>> {
    let (listener, transport) = bind().await?;
    assert_eq!(
        transport.get_updates(Some(-1)).await.err(),
        Some(TelegramTransportError::InvalidInput)
    );
    assert_eq!(
        transport.acknowledge_callback("").await.err(),
        Some(TelegramTransportError::InvalidInput)
    );
    assert!(matches!(
        TelegramTransport::new("100:has/a/path"),
        Err(TelegramTransportError::Configuration)
    ));
    let location = format!("http://{}/ignored", listener.local_addr()?);
    let server = async {
        let (mut stream, _) = listener.accept().await?;
        assert_eq!(
            request(&mut stream).await?["endpoint"],
            "answerCallbackQuery"
        );
        respond(
            &mut stream,
            "307 Temporary Redirect",
            &[("Location", &location)],
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
    let (result, response) = tokio::join!(server, transport.acknowledge_callback("synthetic/ack"));
    result?;
    assert_eq!(response.err(), Some(TelegramTransportError::Rejected));
    Ok(())
}

async fn bind() -> Result<(TcpListener, TelegramTransport), Box<dyn Error>> {
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let transport = TelegramTransport::connect(
        FIXTURE_TOKEN,
        &format!("http://{}", listener.local_addr()?),
        false,
    )?;
    Ok((listener, transport))
}

async fn request(stream: &mut TcpStream) -> Result<Value, Box<dyn Error>> {
    const MAX: usize = 16 * 1024;
    let mut data = Vec::new();
    loop {
        if let Some(header_size) = data.windows(4).position(|ends| ends == b"\r\n\r\n") {
            let head = std::str::from_utf8(&data[..header_size])?;
            let start = head.lines().next().ok_or("Missing HTTP request line")?;
            let mut parts = start.split_whitespace();
            let verb = parts.next().ok_or("Missing HTTP method")?;
            let path = parts.next().ok_or("Missing HTTP path")?;
            let endpoint = path
                .strip_prefix(&format!("/bot{FIXTURE_TOKEN}/"))
                .unwrap_or(path);
            let length: usize = head
                .lines()
                .filter_map(|line| line.split_once(':'))
                .find_map(|(key, value)| {
                    key.eq_ignore_ascii_case("content-length")
                        .then_some(value.trim())
                })
                .unwrap_or("0")
                .parse()?;
            let start = header_size + 4;
            if let Some(bytes) = data.get(start..start + length) {
                let fields: Value = if bytes.is_empty() {
                    json!({})
                } else {
                    serde_json::from_slice(bytes)?
                };
                return Ok(json!({"method":verb,"endpoint":endpoint,"fields":fields}));
            }
        }
        if data.len() >= MAX {
            return Err("Fixture HTTP request is too large".into());
        }
        let mut incoming = [0; 1024];
        let len = stream.read(&mut incoming).await?;
        if len == 0 {
            return Err("Incomplete fixture HTTP request".into());
        }
        data.extend_from_slice(&incoming[..len]);
    }
}

async fn respond(
    stream: &mut TcpStream,
    status: &str,
    extra: &[(&str, &str)],
    body: &str,
) -> io::Result<()> {
    let mut headers = format!(
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n",
        body.len()
    );
    for (name, value) in extra {
        headers.push_str(&format!("{name}: {value}\r\n"));
    }
    headers.push_str("\r\n");
    stream.write_all(headers.as_bytes()).await?;
    stream.write_all(body.as_bytes()).await
}

mod poller;
mod send;
