use std::{env, error::Error, fs, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{
    Database,
    jobs::telegram_inbound::{TelegramInboundState, TelegramInboundTarget},
};
use ga_domain::{
    channel::InboundEnvelope, telegram_control::TelegramControl, timestamp::StoredInstant,
};
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "requires pinned Telegram control routes and disposable TimescaleDB"]
async fn controls_continue_while_the_diary_worker_is_busy() -> Result<(), Box<dyn Error>> {
    let raw = fs::read(env::var("GA_TEST_TELEGRAM_CONTROL_LANE")?)?;
    let cases: Vec<Value> = serde_json::from_slice(&raw)?;
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
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, expected);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let mut runtime = database.acquire_runtime_lease().await?;
    let binding = TelegramBinding {
        owner_id: owner,
        external_owner_id: 42,
        instance_id: "primary",
    };
    let target = TelegramInboundTarget {
        owner_id: owner,
        instance_id: "primary",
    };
    let now: StoredInstant = "2026-11-17T20:00:00Z".parse()?;
    for case in &cases {
        let update = binding
            .accept_polled_update(&mut runtime, &case["wire"], now, false)
            .await?
            .inbound
            .ok_or("Expected accepted owner update")?;
        let lane: String = sqlx::query_scalar(
            "SELECT payload->>'lane' FROM jobs WHERE dedup_key=concat('telegram:inbound:native:',$1::uuid)",
        ).bind(update.message_id).fetch_one(&pool).await?;
        assert_eq!(case["lane"], lane, "{}", case["name"]);
    }
    let diary = database
        .claim_telegram_inbound(target, now, 60)
        .await?
        .ok_or("Missing diary job")?;
    assert!(
        database
            .claim_telegram_inbound(target, now, 60)
            .await?
            .is_none()
    );
    let control = database
        .claim_telegram_control(target, now, 10)
        .await?
        .ok_or("Missing control job")?;
    let envelope = InboundEnvelope::parse(control.envelope_json().as_bytes())?;
    assert_eq!(
        TelegramControl::from_envelope(&envelope),
        Some(TelegramControl::ForgetConversation)
    );
    assert_ne!(diary.message_id(), control.message_id());
    assert!(
        database
            .claim_telegram_control(target, now, 10)
            .await?
            .is_none()
    );
    database
        .finish_telegram_inbound(&control, now, TelegramInboundState::Processed)
        .await?;
    let next = database
        .claim_telegram_control(target, now, 10)
        .await?
        .ok_or("Missing next control")?;
    let envelope = InboundEnvelope::parse(next.envelope_json().as_bytes())?;
    assert_eq!(
        TelegramControl::from_envelope(&envelope),
        Some(TelegramControl::Today)
    );
    assert!(
        database
            .claim_telegram_inbound(target, now, 60)
            .await?
            .is_none()
    );
    println!(
        "{} Python control-lane decisions matched and short work proceeded independently",
        cases.len()
    );
    Ok(())
}
