use std::{env, error::Error, fs, str::FromStr};

use ga_app::{telegram_input::TelegramBinding, telegram_webhook::TelegramWebhook};
use ga_db::Database;
use reqwest::Client;
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use tokio::net::TcpListener;
use uuid::Uuid;

const SECRET: &str = "synthetic-webhook-secret-32-characters";
const HEADER: &str = "X-Telegram-Bot-Api-Secret-Token";

#[tokio::test]
#[ignore = "compares the webhook HTTP route against pinned Python on disposable TimescaleDB"]
async fn matches_webhook_auth_replays_and_storage_backpressure() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let current: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(current, expected);
    let owner_id: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_WEBHOOK")?)?)?;
    let binding = TelegramBinding {
        owner_id,
        external_owner_id: fixture["chat"].as_i64().ok_or("Missing fixture chat")?,
        instance_id: "primary",
    };
    assert!(TelegramWebhook::new(database.clone(), binding, "short").is_none());
    let receiver =
        TelegramWebhook::new(database, binding, SECRET).ok_or("Invalid webhook configuration")?;
    let listener = TcpListener::bind(("127.0.0.1", 0)).await?;
    let endpoint = format!("http://{}/telegram/webhook", listener.local_addr()?);
    let server = tokio::spawn(async move { axum::serve(listener, receiver.router()).await });
    let tls = rustls::ClientConfig::builder_with_provider(
        rustls::crypto::ring::default_provider().into(),
    )
    .with_safe_default_protocol_versions()?
    .with_root_certificates(rustls::RootCertStore::empty())
    .with_no_client_auth();
    let client = Client::builder()
        .tls_backend_preconfigured(tls)
        .no_proxy()
        .build()?;
    let cases = fixture["cases"].as_array().ok_or("Missing HTTP cases")?;
    for case in cases {
        let name = case["name"].as_str().ok_or("Missing case name")?;
        let mut ingress = None;
        if name == "busy" {
            let mut txn = pool.begin().await?;
            sqlx::query("SELECT pg_advisory_xact_lock(72104623)")
                .execute(&mut *txn)
                .await?;
            ingress = Some(txn);
        }
        if name == "disabled" {
            sqlx::query("INSERT INTO app_state(key,value) VALUES('preferences:onboarding','{\"channel\":null,\"fallback_channels\":[]}')")
                .execute(&pool).await?;
        }
        let mut request = client.post(&endpoint);
        request = match case["auth"].as_str() {
            Some("valid") => request.header(HEADER, SECRET),
            Some("wrong") => request.header(HEADER, "fixture-wrong"),
            _ => request,
        };
        let response = if case["large"].as_bool() == Some(true) {
            request.body(vec![b'x'; 1024 * 1024 + 1]).send().await?
        } else {
            request.json(&case["payload"]).send().await?
        };
        assert_eq!(
            Some(u64::from(response.status().as_u16())),
            case["status"].as_u64(),
            "{name}"
        );
        assert_eq!(response.json::<Value>().await?, case["body"], "{name}");
        if let Some(txn) = ingress {
            txn.rollback().await?;
        }
    }
    let invalid = client
        .post(&endpoint)
        .header(HEADER, SECRET)
        .body("{")
        .send()
        .await?;
    assert_eq!(invalid.status().as_u16(), 400);
    let duplicate_auth = client
        .post(&endpoint)
        .header(HEADER, SECRET)
        .header(HEADER, SECRET)
        .json(&cases[0]["payload"])
        .send()
        .await?;
    assert_eq!(duplicate_auth.status().as_u16(), 403);
    let rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT external_event_id,kind FROM inbound_messages ORDER BY external_event_id",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(
        rows,
        [
            ("461".into(), "text".into()),
            ("463".into(), "edit".into()),
            ("464".into(), "text".into())
        ]
    );
    let offset: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM app_state WHERE key='telegram:offset')")
            .fetch_one(&pool)
            .await?;
    assert!(!offset);
    server.abort();
    println!(
        "{} paired webhook responses matched, with no poll offset changes",
        cases.len()
    );
    Ok(())
}
