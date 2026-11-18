use std::{collections::BTreeMap, env, error::Error, fs, str::FromStr};

use ga_app::telegram_input::TelegramBinding;
use ga_db::{
    Database,
    jobs::telegram_inbound::{TelegramInboundState, TelegramInboundTarget},
    runtime_lease::RuntimeLease,
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use uuid::Uuid;

struct Fixture {
    pool: PgPool,
    database: Database,
    runtime: RuntimeLease,
    binding: TelegramBinding<'static>,
    expected: Value,
}

impl Fixture {
    async fn open() -> Result<Self, Box<dyn Error>> {
        let url = env::var("GA_TEST_DATABASE_URL")?;
        let address = url
            .strip_prefix("postgresql+psycopg://")
            .ok_or("Expected fixture URL")?;
        let options = PgConnectOptions::from_str(&format!("postgresql://{address}"))?
            .disable_statement_logging();
        let target = options
            .get_database()
            .filter(|name| name.ends_with("_test"))
            .ok_or("Expected a disposable database")?;
        let pool = PgPool::connect_with(options.clone()).await?;
        let actual: String = sqlx::query_scalar("SELECT current_database()")
            .fetch_one(&pool)
            .await?;
        assert_eq!(target, actual);
        let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
        let runtime = database.acquire_runtime_lease().await?;
        let owner_id = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
            .fetch_one(&pool)
            .await?;
        Ok(Self {
            pool,
            database,
            runtime,
            binding: TelegramBinding {
                owner_id,
                external_owner_id: 42,
                instance_id: "primary",
            },
            expected: serde_json::from_slice(&fs::read(env::var(
                "GA_TEST_TELEGRAM_DIARY_COMMANDS",
            )?)?)?,
        })
    }

    async fn receive(&mut self, update_id: i64, text: &str) -> Result<(), Box<dyn Error>> {
        let now = self.now().await?;
        let update = json!({
            "update_id":update_id,
            "message":{"message_id":update_id,"from":{"id":42},
                "chat":{"id":42,"type":"private"},"text":text,"date":OffsetDateTime::from(now).unix_timestamp()}
        });
        self.binding
            .accept_polled_update(&mut self.runtime, &update, now, true)
            .await?;
        Ok(())
    }

    async fn apply(&self) -> Result<Value, Box<dyn Error>> {
        let lease = self
            .database
            .claim_local_telegram_history(self.target(), self.now().await?, 60)
            .await?
            .ok_or("Expected a supported diary command")?;
        let stored = self
            .database
            .apply_local_telegram_diary(&lease, "UTC")
            .await?;
        let result: Option<String> = sqlx::query_scalar(
            "SELECT intent#>>'{blocks,0,text}' FROM outbox_messages WHERE id=$1",
        )
        .bind(stored.outbox_id)
        .fetch_one(&self.pool)
        .await?;
        Ok(json!(result.ok_or("Missing diary command reply")?))
    }

    async fn now(&self) -> Result<StoredInstant, Box<dyn Error>> {
        let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&self.pool)
            .await?;
        Ok(StoredInstant::try_from(now)?)
    }

    fn target(&self) -> TelegramInboundTarget<'_> {
        TelegramInboundTarget {
            owner_id: self.binding.owner_id,
            instance_id: self.binding.instance_id,
        }
    }

    async fn events(&self) -> Result<BTreeMap<String, Value>, sqlx::Error> {
        let rows: Vec<(String, i32, bool)> =
            sqlx::query_as("SELECT payload->>'description',revision,deleted FROM events")
                .fetch_all(&self.pool)
                .await?;
        Ok(rows
            .into_iter()
            .map(|(label, revision, deleted)| (label, json!([revision, deleted])))
            .collect())
    }

    async fn saved(&self, key: &str) -> Result<bool, sqlx::Error> {
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM app_state WHERE key=$1)")
            .bind(key)
            .fetch_one(&self.pool)
            .await
    }
}

#[tokio::test]
#[ignore = "compares native cancellation and undo with a disposable pinned Python fixture"]
async fn diary_commands_keep_order_and_undo_a_group_once() -> Result<(), Box<dyn Error>> {
    let mut fixture = Fixture::open().await?;
    fixture.receive(901, "/cancel").await?;
    assert_eq!(fixture.apply().await?, fixture.expected["replies"][0]);
    assert!(fixture.saved("tracker:chat-setup:telegram:primary").await?);
    fixture.receive(903, "/cancel").await?;
    assert_eq!(fixture.apply().await?, fixture.expected["replies"][1]);
    assert!(!fixture.saved("tracker:chat-setup:telegram:primary").await?);
    assert!(fixture.saved("conversation:pending").await?);
    fixture.receive(905, "/cancel").await?;
    assert_eq!(fixture.apply().await?, fixture.expected["replies"][2]);
    assert!(!fixture.saved("conversation:pending").await?);
    assert!(fixture.saved("conversation:pending:web:local").await?);

    fixture
        .receive(906, "Synthetic earlier diary report")
        .await?;
    fixture.receive(907, "/undo").await?;
    let now = fixture.now().await?;
    assert!(
        fixture
            .database
            .claim_local_telegram_history(fixture.target(), now, 60)
            .await?
            .is_none()
    );
    let older = fixture
        .database
        .claim_telegram_inbound(fixture.target(), now, 60)
        .await?
        .ok_or("Missing prior diary report")?;
    assert!(
        older
            .envelope_json()
            .contains("Synthetic earlier diary report")
    );
    fixture
        .database
        .finish_telegram_inbound(&older, now, TelegramInboundState::Processed)
        .await?;
    assert_eq!(fixture.apply().await?, fixture.expected["replies"][3]);
    fixture.receive(907, "/undo").await?;
    assert!(
        fixture
            .database
            .claim_local_telegram_history(fixture.target(), fixture.now().await?, 60)
            .await?
            .is_none()
    );
    for (label, revision) in fixture.events().await? {
        assert_eq!(revision, fixture.expected["events"][label]);
    }
    let undo_audits: Vec<Uuid> = sqlx::query_scalar(
        "SELECT DISTINCT operation_id FROM audit_log WHERE actor='telegram:42' AND action='undo'",
    )
    .fetch_all(&fixture.pool)
    .await?;
    assert_eq!(undo_audits.len(), 1);
    let count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM audit_log WHERE actor='telegram:42' AND action='undo'",
    )
    .fetch_one(&fixture.pool)
    .await?;
    assert_eq!(count, 2);
    Ok(())
}

#[tokio::test]
#[ignore = "verifies grouped undo rollback on a disposable pinned Python fixture"]
async fn diary_commands_preserve_every_row_when_a_group_has_changed() -> Result<(), Box<dyn Error>>
{
    let mut fixture = Fixture::open().await?;
    fixture.receive(909, "/undo").await?;
    assert_eq!(fixture.apply().await?, fixture.expected["replies"][4]);
    let status: String =
        sqlx::query_scalar("SELECT status FROM inbound_messages WHERE external_event_id='909'")
            .fetch_one(&fixture.pool)
            .await?;
    assert_eq!(status, "invalid");
    let events = fixture.events().await?;
    for label in ["Synthetic changed note", "Synthetic unchanged group peer"] {
        assert_eq!(events[label], fixture.expected["events"][label]);
    }
    assert!(fixture.saved("conversation:pending").await?);
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM audit_log WHERE action='undo'")
        .fetch_one(&fixture.pool)
        .await?;
    assert_eq!(count, 0);
    Ok(())
}
