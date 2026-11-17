use std::io;

use ga_domain::model_request::ApprovedModelSchema;
use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

use super::*;

const SYNTHETIC_KEY: &str = "synthetic-model-key";

fn prepared(provider: StructuredProvider) -> Result<StructuredModelRequest, Box<dyn Error>> {
    Ok(StructuredModelRequest::prepare(
        provider,
        ApprovedModelSchema::ModelProbe,
        "fixture-model",
        512,
        "fixture instruction",
        "fixture prompt",
    )?)
}

async fn read_request(
    connection: &mut TcpStream,
) -> Result<(String, Value), Box<dyn Error + Send + Sync>> {
    let mut bytes = Vec::new();
    let header_end = loop {
        if let Some(end) = bytes.windows(4).position(|part| part == b"\r\n\r\n") {
            break end + 4;
        }
        if connection.read_buf(&mut bytes).await? == 0 {
            return Err("Request ended before headers".into());
        }
    };
    let headers = String::from_utf8(bytes[..header_end].to_vec())?;
    let length = headers
        .lines()
        .find_map(|line| {
            let (key, value) = line.split_once(':')?;
            key.eq_ignore_ascii_case("content-length")
                .then_some(value.trim())
        })
        .ok_or("Missing request length")?
        .parse::<usize>()?;
    while bytes.len() < header_end + length {
        if connection.read_buf(&mut bytes).await? == 0 {
            return Err("Request ended before body".into());
        }
    }
    Ok((
        headers,
        serde_json::from_slice(&bytes[header_end..header_end + length])?,
    ))
}

fn reply(status: u16, headers: &str, body: &[u8]) -> Vec<u8> {
    let mut bytes = format!(
        "HTTP/1.1 {status} Fixture\r\nconnection: close\r\ncontent-length: {}\r\n{headers}\r\n",
        body.len()
    )
    .into_bytes();
    bytes.extend_from_slice(body);
    bytes
}

async fn exchange(
    transport: &ModelTransport,
    request: &StructuredModelRequest,
    response: Vec<u8>,
    allow_body_interrupt: bool,
) -> Result<Result<StructuredModelOutput, ModelTransportError>, Box<dyn Error + Send + Sync>> {
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let address = listener.local_addr()?;
    let endpoint = format!("http://{address}/provider-test");
    let server = async {
        let (mut connection, _) = listener.accept().await?;
        let observed = read_request(&mut connection).await?;
        match connection.write_all(&response).await {
            Err(error)
                if allow_body_interrupt
                    && matches!(
                        error.kind(),
                        io::ErrorKind::ConnectionReset | io::ErrorKind::BrokenPipe
                    ) => {}
            result => result?,
        }
        drop(connection);
        // The redirect in the fixture points back here. The client must also avoid replay
        // after a retryable response.
        assert!(
            tokio::time::timeout(Duration::from_millis(15), listener.accept())
                .await
                .is_err()
        );
        Ok::<_, Box<dyn Error + Send + Sync>>(observed)
    };
    let (result, observed) = tokio::join!(transport.send_to(request, &endpoint), server);
    let (headers, body) = observed?;
    let headers = headers.to_ascii_lowercase();
    assert!(headers.starts_with("post /provider-test http/1.1\r\n"));
    assert!(headers.contains(&format!("authorization: bearer {SYNTHETIC_KEY}\r\n")));
    assert!(headers.contains("accept-encoding: identity\r\n"));
    assert_eq!(
        headers.contains("anthropic-version: 2026-10-21\r\n"),
        request.provider() == StructuredProvider::Anthropic
    );
    assert_eq!(&body, request.body());
    Ok(result)
}

#[tokio::test]
async fn validates_one_response_from_each_provider() -> Result<(), Box<dyn Error + Send + Sync>> {
    for (provider, response) in [
        (
            StructuredProvider::OpenAi,
            json!({
                "status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "{\"urgent\":true}"}
                ]}], "usage": {"input_tokens": 32, "output_tokens": 5}
            }),
        ),
        (
            StructuredProvider::Anthropic,
            json!({
                "stop_reason": "end_turn", "content": [
                    {"type": "text", "text": "{\"urgent\":true}"}
                ], "usage": {"input_tokens": 32, "output_tokens": 5}
            }),
        ),
    ] {
        let transport =
            ModelTransport::connect(provider, SYNTHETIC_KEY, Duration::from_secs(5), false)?;
        let request = prepared(provider).map_err(|_| "Unable to prepare fixture request")?;
        let mut exact_limit = serde_json::to_vec(&response)?;
        exact_limit.resize(MAX_RESPONSE_BYTES, b' ');
        let received =
            exchange(&transport, &request, reply(200, "", &exact_limit), false).await??;
        assert_eq!(received.value, json!({"urgent": true}));
        assert_eq!(
            received.usage.and_then(|usage| usage.input_tokens),
            Some(32)
        );
        assert_eq!(
            received.usage.and_then(|usage| usage.output_tokens),
            Some(5)
        );
    }
    Ok(())
}

#[tokio::test]
async fn classifies_status_retry_after_and_structured_failures()
-> Result<(), Box<dyn Error + Send + Sync>> {
    use ModelUnavailability::*;
    let provider = StructuredProvider::OpenAi;
    let transport =
        ModelTransport::connect(provider, SYNTHETIC_KEY, Duration::from_secs(5), false)?;
    let request = prepared(provider).map_err(|_| "Unable to prepare fixture request")?;
    let retry = |reason, seconds| ModelTransportError::Unavailable {
        reason,
        retry_after: Duration::from_secs(seconds),
    };
    let rejected = ModelTransportError::RequestRejected;
    for (status, headers, expected) in [
        (301, "location: /provider-test\r\n", rejected),
        (400, "retry-after: 3000\r\n", rejected),
        (401, "", retry(Authorization, 1800)),
        (403, "retry-after: 2000.25\r\n", retry(Authorization, 2001)),
        (404, "", retry(MissingModel, 1800)),
        (408, "", retry(Upstream, 60)),
        (422, "", rejected),
        (429, "", retry(RateLimited, 120)),
        (429, "retry-after: 180.1\r\n", retry(RateLimited, 181)),
        (429, "retry-after: 4\r\n", retry(RateLimited, 120)),
        (429, "retry-after: 1e100\r\n", retry(RateLimited, 120)),
        (500, "", retry(Upstream, 60)),
        (503, "retry-after: 240\r\n", retry(Upstream, 240)),
        (599, "", retry(Upstream, 60)),
    ] {
        let result = exchange(
            &transport,
            &request,
            reply(status, headers, b"synthetic private upstream body"),
            false,
        )
        .await?;
        assert_eq!(result.err(), Some(expected), "status {status}");
        assert!(!format!("{expected:?} {expected}").contains("synthetic"));
    }
    for (body, error) in [
        ("{\"status\":\"failed\"}", retry(Upstream, 60)),
        (
            "{\"status\":\"incomplete\"}",
            ModelTransportError::Response(ModelResponseError::Incomplete),
        ),
        (
            "not json",
            ModelTransportError::Response(ModelResponseError::InvalidEnvelope),
        ),
    ] {
        let result = exchange(&transport, &request, reply(200, "", body.as_bytes()), false).await?;
        assert_eq!(result.err(), Some(error));
    }
    Ok(())
}

#[tokio::test]
async fn bounds_download_and_classifies_interrupted_or_slow_body()
-> Result<(), Box<dyn Error + Send + Sync>> {
    let provider = StructuredProvider::OpenAi;
    let transport =
        ModelTransport::connect(provider, SYNTHETIC_KEY, Duration::from_secs(1), false)?;
    let request = prepared(provider).map_err(|_| "Unable to prepare fixture request")?;
    let too_large = ModelTransportError::Response(ModelResponseError::TooLarge);
    let upstream = ModelUnavailability::Upstream.with_retry(None);
    let declared_large = format!(
        "HTTP/1.1 200 OK\r\nconnection: close\r\ncontent-length: {}\r\n\r\n",
        MAX_RESPONSE_BYTES + 1
    )
    .into_bytes();
    let mut chunked_large = format!(
        "HTTP/1.1 200 OK\r\nconnection: close\r\ntransfer-encoding: chunked\r\n\r\n{:x}\r\n",
        MAX_RESPONSE_BYTES + 1
    )
    .into_bytes();
    chunked_large.resize(chunked_large.len() + MAX_RESPONSE_BYTES + 1, b' ');
    chunked_large.extend_from_slice(b"\r\n0\r\n\r\n");
    for response in [declared_large, chunked_large] {
        let result = exchange(&transport, &request, response, true).await?;
        assert_eq!(result.err(), Some(too_large));
    }
    let incomplete = b"HTTP/1.1 200 OK\r\nconnection: close\r\ncontent-length: 9\r\n\r\n{".to_vec();
    assert_eq!(
        exchange(&transport, &request, incomplete, false)
            .await?
            .err(),
        Some(upstream)
    );
    assert_eq!(
        exchange(
            &transport,
            &request,
            reply(200, "content-encoding: gzip\r\n", b"{\"ok\":true}"),
            false
        )
        .await?
        .err(),
        Some(ModelTransportError::Response(
            ModelResponseError::InvalidEnvelope
        )),
    );

    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let endpoint = format!("http://{}/provider-test", listener.local_addr()?);
    let server = tokio::spawn(async move {
        let (mut connection, _) = listener.accept().await?;
        read_request(&mut connection).await?;
        connection
            .write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 4\r\n\r\n{")
            .await?;
        tokio::time::sleep(Duration::from_secs(5)).await;
        Ok::<_, Box<dyn Error + Send + Sync>>(())
    });
    let error = tokio::time::timeout(
        Duration::from_secs(3),
        transport.send_to(&request, &endpoint),
    )
    .await?;
    assert_eq!(error.err(), Some(upstream));
    server.abort();
    assert!(server.await.is_err_and(|error| error.is_cancelled()));
    Ok(())
}

#[tokio::test]
async fn rejects_invalid_configuration_and_cross_provider_requests() -> Result<(), Box<dyn Error>> {
    let openai = StructuredProvider::OpenAi;
    let invalid = ModelTransportError::InvalidConfiguration;
    for (key, timeout) in [
        ("", Duration::from_secs(1)),
        ("bad\r\nheader", Duration::from_secs(1)),
        ("trailing ", Duration::from_secs(1)),
        (SYNTHETIC_KEY, Duration::from_millis(999)),
        (SYNTHETIC_KEY, Duration::from_secs(121)),
    ] {
        assert_eq!(
            ModelTransport::new(openai, key, timeout).err(),
            Some(invalid)
        );
    }
    let transport = ModelTransport::new(openai, SYNTHETIC_KEY, Duration::from_secs(120))?;
    assert!(transport.authorization.is_sensitive());
    assert!(!format!("{:?}", transport.authorization).contains(SYNTHETIC_KEY));
    let wrong = prepared(StructuredProvider::Anthropic)?;
    assert_eq!(transport.send(&wrong).await.err(), Some(invalid));
    Ok(())
}

#[test]
fn calculates_bounded_retry_after_with_the_request_clock() {
    let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000_000);
    let later = httpdate::fmt_http_date(now + Duration::from_secs(123));
    let earlier = httpdate::fmt_http_date(now - Duration::from_secs(123));
    assert_eq!(retry_delay(&later, now), Some(Duration::from_secs(123)));
    assert_eq!(retry_delay(&earlier, now), None);
    assert_eq!(retry_delay(" 1.01 ", now), Some(Duration::from_secs(2)));
    for invalid in ["0", "-1", "NaN", "inf", "1e100", "bad date", "253402300799"] {
        assert_eq!(retry_delay(invalid, now), None);
    }
}
