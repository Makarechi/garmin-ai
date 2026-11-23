use std::{
    io::{self, BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpStream},
    path::Path,
    process::{Child, Command, Stdio},
    time::Duration,
};

struct Server {
    process: Child,
    address: SocketAddr,
}

impl Server {
    fn start() -> Result<Self, Box<dyn std::error::Error>> {
        Self::configured(&[], None)
    }

    fn configured(
        environment: &[(&str, &str)],
        directory: Option<&Path>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let mut command = Command::new(env!("CARGO_BIN_EXE_garmin-ai-rust"));
        command
            .env_clear()
            .envs(environment.iter().copied())
            .args(["serve", "--port", "0"])
            .stdout(Stdio::piped());
        if let Some(directory) = directory {
            command.current_dir(directory);
        }
        let mut process = command.spawn()?;
        let address = (|| -> Result<SocketAddr, Box<dyn std::error::Error>> {
            let stdout = process
                .stdout
                .take()
                .ok_or_else(|| io::Error::other("Missing stdout"))?;
            let mut line = String::new();
            BufReader::new(stdout).read_line(&mut line)?;
            let address = line
                .trim()
                .strip_prefix("Listening on http://")
                .ok_or_else(|| io::Error::other("Missing liveness listener address"))?;
            Ok(address.parse()?)
        })();
        match address {
            Ok(address) => Ok(Self { process, address }),
            Err(error) => {
                let _ = process.kill();
                let _ = process.wait();
                Err(error)
            }
        }
    }

    fn request(&self, method: &str, path: &str, additional_headers: &str) -> io::Result<String> {
        self.request_with_body(method, path, additional_headers, "")
    }

    fn request_with_body(
        &self,
        method: &str,
        path: &str,
        additional_headers: &str,
        body: &str,
    ) -> io::Result<String> {
        let mut stream = TcpStream::connect_timeout(&self.address, Duration::from_secs(3))?;
        stream.set_read_timeout(Some(Duration::from_secs(3)))?;
        write!(
            stream,
            "{method} {path} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n{additional_headers}\r\n{body}",
            self.address,
            body.len()
        )?;
        let mut response = String::new();
        stream.read_to_string(&mut response)?;
        Ok(response)
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.process.kill();
        let _ = self.process.wait();
    }
}

fn parts(raw: &str) -> io::Result<(&str, &str)> {
    raw.split_once("\r\n\r\n")
        .ok_or_else(|| io::Error::other("Expected an HTTP response"))
}

#[test]
fn liveness_has_the_python_response_and_cache_policy() -> Result<(), Box<dyn std::error::Error>> {
    let server = Server::start()?;
    assert!(server.address.ip().is_loopback());

    let raw = server.request("GET", "/health/live", "")?;
    let (headers, body) = parts(&raw)?;
    assert!(headers.starts_with("HTTP/1.1 200 "));
    assert!(headers.contains("content-type: application/json"));
    assert!(!headers.contains("cache-control:"));
    assert_eq!(body, r#"{"status":"alive"}"#);

    let raw = server.request("GET", "/health/live", "Authorization: Bearer synthetic\r\n")?;
    let (headers, body) = parts(&raw)?;
    assert!(headers.starts_with("HTTP/1.1 200 "));
    assert!(headers.contains("cache-control: no-store"));
    assert_eq!(body, r#"{"status":"alive"}"#);
    Ok(())
}

#[test]
fn unsupported_methods_and_unknown_paths_have_python_error_shapes()
-> Result<(), Box<dyn std::error::Error>> {
    let server = Server::start()?;
    for method in ["HEAD", "POST", "OPTIONS"] {
        let raw = server.request(method, "/health/live", "")?;
        let (headers, body) = parts(&raw)?;
        assert!(headers.starts_with("HTTP/1.1 405 "));
        assert!(headers.contains("allow: GET"));
        assert!(headers.contains("content-type: application/json"));
        if method == "HEAD" {
            assert_eq!(body, "");
            assert!(headers.contains("content-length: 31"));
        } else {
            assert_eq!(body, r#"{"detail":"Method Not Allowed"}"#);
        }
    }

    let raw = server.request("GET", "/missing", "")?;
    let (headers, body) = parts(&raw)?;
    assert!(headers.starts_with("HTTP/1.1 404 "));
    assert_eq!(body, r#"{"detail":"Not Found"}"#);
    Ok(())
}

#[test]
fn readiness_reports_unavailable_without_a_database() -> Result<(), Box<dyn std::error::Error>> {
    let server = Server::start()?;
    let raw = server.request(
        "GET",
        "/health/ready",
        "Authorization: Bearer synthetic\r\n",
    )?;
    let (headers, body) = parts(&raw)?;
    assert!(headers.starts_with("HTTP/1.1 503 "));
    assert!(headers.contains("cache-control: no-store"));
    assert_eq!(body, r#"{"detail":"Database unavailable or not migrated"}"#);
    Ok(())
}

#[test]
fn scoped_credentials_and_dotenv_priority_are_applied() -> Result<(), Box<dyn std::error::Error>> {
    let root = tempfile::tempdir()?;
    let default = "synthetic-default-token-xxyyzzxxyyzz";
    let old = "synthetic-old-legacy-key-xxyyzzxxyyzz";
    let new = "synthetic-new-legacy-key-xxyyzzxxyyzz";
    std::fs::write(
        root.path().join(".env"),
        format!("GA_API_KEY={old}\nGA_API_TOKENS='[{{\"key\":\"{default}\"}}]'\n"),
    )?;
    let server = Server::configured(&[("ga_api_key", new)], Some(root.path()))?;
    for (key, status, expected) in [
        (
            old,
            401,
            serde_json::json!({"detail":"Authentication required"}),
        ),
        (
            default,
            200,
            serde_json::json!({"read_diary":false,"write_diary":false,"manage_definitions":false}),
        ),
        (
            new,
            200,
            serde_json::json!({"read_diary":true,"write_diary":true,"manage_definitions":true}),
        ),
    ] {
        let raw = server.request(
            "GET",
            "/capabilities",
            &format!("Authorization: Bearer {key}\r\n"),
        )?;
        let (head, body) = parts(&raw)?;
        assert!(head.starts_with(&format!("HTTP/1.1 {status} ")));
        assert!(head.contains("cache-control: no-store"));
        assert_eq!(serde_json::from_str::<serde_json::Value>(body)?, expected);
    }
    let rotated = "synthetic-rotated-editor-token-xxyyzz";
    let scopes = serde_json::json!([
        {"key":rotated,"scopes":["read:diary","write:diary"]}
    ])
    .to_string();
    let server = Server::configured(&[("gA_aPi_ToKeNs", &scopes)], Some(root.path()))?;
    let raw = server.request(
        "GET",
        "/capabilities",
        &format!("Authorization: Bearer {default}\r\n"),
    )?;
    assert!(parts(&raw)?.0.starts_with("HTTP/1.1 401 "));
    let raw = server.request(
        "GET",
        "/capabilities",
        &format!("Authorization: Bearer {rotated}\r\n"),
    )?;
    let (head, body) = parts(&raw)?;
    assert!(head.starts_with("HTTP/1.1 200 "));
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(body)?,
        serde_json::json!({"read_diary":true,"write_diary":true,"manage_definitions":false})
    );
    Ok(())
}

#[test]
fn startup_rejects_bad_token_config_without_leaking_it() -> Result<(), Box<dyn std::error::Error>> {
    let key = "synthetic-private-config-xxyyzzxxyyzz";
    let bogus = [
        serde_json::json!([{"key":key},{"key":key}]),
        serde_json::json!([{"key":key,"scopes":["write:wearable","read:diary"]}]),
        serde_json::json!([{"key":key,"scopes":["write:wearable"]}]),
        serde_json::json!([{"key":key,"scopes":["does-not-exist"]}]),
        serde_json::json!([{"key":"replace-with-a-long-synthetic-placeholder"}]),
        serde_json::json!([{"key":key,"wearable_device_id":"11111111-1111-1111-1111-111111111111"}]),
        serde_json::Value::Null,
    ];
    for config in bogus {
        let output = Command::new(env!("CARGO_BIN_EXE_garmin-ai-rust"))
            .env_clear()
            .env("GA_API_TOKENS", config.to_string())
            .args(["serve", "--port", "0"])
            .output()?;
        assert!(!output.status.success());
        assert_eq!(output.stdout, [] as [u8; 0]);
        assert_eq!(
            String::from_utf8(output.stderr)?,
            "Could not run the HTTP server.\n"
        );
    }
    Ok(())
}

#[test]
#[ignore = "requires the synthetic scope matrix captured from pinned Python"]
fn scope_matrix_matches_reference_at_http_boundary() -> Result<(), Box<dyn std::error::Error>> {
    use ga_domain::access::{ReadToolInput, Scope, Scopes};
    use serde_json::{Value, from_str};

    let document: Value =
        serde_json::from_slice(&std::fs::read(std::env::var("GA_TEST_API_SCOPES")?)?)?;
    let legacy = document["legacy"]
        .as_str()
        .ok_or_else(|| io::Error::other("legacy fixture key"))?;
    let credential_scopes = document["scoped"].to_string();
    let server = Server::configured(
        &[
            ("GA_API_KEY", legacy),
            ("GA_API_TOKENS", &credential_scopes),
        ],
        None,
    )?;
    let cases = document["http"]
        .as_array()
        .ok_or_else(|| io::Error::other("HTTP cases"))?;
    for (index, case) in cases.iter().enumerate() {
        let authorization = case["authorization"]
            .as_str()
            .map_or_else(String::new, |header| format!("Authorization: {header}\r\n"));
        let method = case["method"]
            .as_str()
            .ok_or_else(|| io::Error::other("HTTP method"))?;
        let status = case["status"]
            .as_u64()
            .ok_or_else(|| io::Error::other("HTTP status"))?;
        let raw = server.request(method, "/capabilities", &authorization)?;
        let (head, body) = parts(&raw)?;
        assert!(
            head.starts_with(&format!("HTTP/1.1 {status} ")),
            "HTTP status case {index}"
        );
        for header in ["cache-control", "allow"] {
            let expected = case[header.replace('-', "_")].as_str();
            let actual = head.lines().find_map(|line| {
                let (name, value) = line.split_once(": ")?;
                name.eq_ignore_ascii_case(header).then_some(value)
            });
            assert_eq!(actual, expected, "{header} case {index}");
        }
        let expected = case["body"]
            .as_str()
            .ok_or_else(|| io::Error::other("HTTP response"))?;
        if expected.is_empty() {
            assert!(body.is_empty(), "empty body case {index}");
        } else {
            assert_eq!(
                from_str::<Value>(body)?,
                from_str::<Value>(expected)?,
                "body case {index}"
            );
        }
    }
    let tools = document["tools"]
        .as_array()
        .ok_or_else(|| io::Error::other("tool cases"))?;
    for (index, case) in tools.iter().enumerate() {
        let scopes = case["scopes"]
            .as_array()
            .ok_or_else(|| io::Error::other("scope list"))?;
        let scopes: Scopes = scopes
            .iter()
            .map(|scope| Scope::from_name(scope.as_str()?))
            .collect::<Option<Scopes>>()
            .ok_or_else(|| io::Error::other("unknown scope"))?;
        let name = case["name"]
            .as_str()
            .ok_or_else(|| io::Error::other("tool"))?;
        let input = if let Some(kind) = case["kind"].as_str() {
            ReadToolInput::Catalog(kind)
        } else if let Some(operation) = case["operation"].as_str() {
            ReadToolInput::Analysis {
                operation,
                metric: case["metric"].as_str(),
            }
        } else {
            ReadToolInput::Discovery
        };
        assert_eq!(
            Some(scopes.permits_tool(name, input)),
            case["permitted"].as_bool(),
            "tool case {index} {name}"
        );
    }
    Ok(())
}

const WEBHOOK_PATH: &str = "/telegram/webhook";
const SYNTHETIC_WEBHOOK_SECRET: &str = "synthetic-service-webhook-xxyyzz";

#[test]
fn service_mounts_webhook_with_secret_and_allowlist_checks()
-> Result<(), Box<dyn std::error::Error>> {
    let root = tempfile::tempdir()?;
    std::fs::write(
        root.path().join(".env"),
        "GA_TELEGRAM_WEBHOOK_SECRET=fixture-old-webhook-secret\nGA_TELEGRAM_USER_ID=42\n",
    )?;
    let server = Server::configured(
        &[("ga_telegram_webhook_secret", SYNTHETIC_WEBHOOK_SECRET)],
        Some(root.path()),
    )?;
    let request = |secret| format!("X-Telegram-Bot-Api-Secret-Token: {secret}\r\n");
    let assert_status = |raw: &str, status, expected| -> io::Result<()> {
        let (headers, body) = parts(raw)?;
        assert!(
            headers.starts_with(&format!("HTTP/1.1 {status} ")),
            "{headers}"
        );
        assert_eq!(body, expected);
        Ok(())
    };
    assert_status(
        &server.request("GET", WEBHOOK_PATH, "")?,
        405,
        r#"{"detail":"Method Not Allowed"}"#,
    )?;
    assert_status(
        &server.request_with_body(
            "POST",
            WEBHOOK_PATH,
            &request("fixture-old-webhook-secret"),
            "{",
        )?,
        403,
        r#"{"detail":"Invalid webhook secret"}"#,
    )?;
    assert_status(
        &server.request_with_body(
            "POST",
            WEBHOOK_PATH,
            &request(SYNTHETIC_WEBHOOK_SECRET),
            "{",
        )?,
        400,
        r#"{"detail":"Invalid Telegram update"}"#,
    )?;
    assert_status(
        &server.request_with_body(
            "POST",
            WEBHOOK_PATH,
            &request(SYNTHETIC_WEBHOOK_SECRET),
            "{}",
        )?,
        503,
        r#"{"detail":"Database unavailable or identity is not ready"}"#,
    )?;
    let disabled = Server::configured(
        &[
            ("GA_INTEGRATIONS", "[]"),
            ("GA_TELEGRAM_WEBHOOK_SECRET", SYNTHETIC_WEBHOOK_SECRET),
        ],
        Some(root.path()),
    )?;
    assert_status(
        &disabled.request_with_body(
            "POST",
            WEBHOOK_PATH,
            &request(SYNTHETIC_WEBHOOK_SECRET),
            "{",
        )?,
        503,
        r#"{"detail":"Telegram integration is disabled"}"#,
    )?;
    let no_secret = Server::start()?;
    assert_status(
        &no_secret.request_with_body(
            "POST",
            WEBHOOK_PATH,
            &request(SYNTHETIC_WEBHOOK_SECRET),
            "{}",
        )?,
        403,
        r#"{"detail":"Invalid webhook secret"}"#,
    )?;
    Ok(())
}

#[tokio::test]
#[ignore = "starts the native service against a disposable TimescaleDB database"]
async fn service_webhook_requires_current_owner_pairing() -> Result<(), Box<dyn std::error::Error>>
{
    use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
    use std::{env, str::FromStr};

    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|db| db.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, expected);

    let server = Server::configured(
        &[
            ("GA_DATABASE_URL", &url),
            ("GA_TELEGRAM_WEBHOOK_SECRET", SYNTHETIC_WEBHOOK_SECRET),
            ("GA_TELEGRAM_USER_ID", "42"),
            (
                "GA_INTEGRATIONS",
                r#"[{"id":"channel:telegram:fixture","kind":"channel","provider":"telegram"}]"#,
            ),
        ],
        None,
    )?;
    let send = |update_id: i64| {
        let headers = format!("X-Telegram-Bot-Api-Secret-Token: {SYNTHETIC_WEBHOOK_SECRET}\r\n");
        let payload = serde_json::json!({
            "update_id": update_id,
            "message": {
                "message_id": 761, "from": {"id": 42},
                "chat": {"id": 42, "type": "private"}, "date": 1_794_916_800,
                "text": "synthetic Telegram service test",
            },
        });
        server.request_with_body("POST", WEBHOOK_PATH, &headers, &payload.to_string())
    };
    let verify = |raw: &str, status, expected| -> io::Result<()> {
        let (headers, body) = parts(raw)?;
        assert!(
            headers.starts_with(&format!("HTTP/1.1 {status} ")),
            "{headers}"
        );
        assert_eq!(serde_json::from_str::<serde_json::Value>(body)?, expected);
        Ok(())
    };
    let unavailable = serde_json::json!({"detail":"Database unavailable or identity is not ready"});
    verify(&send(471)?, 503, unavailable.clone())?;
    sqlx::query("INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
                 SELECT gen_random_uuid(),id,'telegram','fixture','42','fixture' FROM people")
        .execute(&pool).await?;
    let ok = serde_json::json!({"ok":true,"accepted":true});
    verify(&send(471)?, 200, ok.clone())?;
    verify(&send(471)?, 200, ok)?;
    sqlx::query("UPDATE channel_bindings SET external_id='43' WHERE channel='telegram' AND channel_instance_id='fixture'")
        .execute(&pool).await?;
    verify(&send(472)?, 503, unavailable)?;
    let saved: Vec<String> = sqlx::query_scalar(
        "SELECT external_event_id FROM inbound_messages WHERE channel_instance_id='fixture'",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(saved, ["471"]);
    Ok(())
}
