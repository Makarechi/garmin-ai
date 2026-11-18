use std::{env, error::Error, fs, io, str::FromStr};

use ga_db::{
    Database,
    source_pages::{PreparedSourcePoll, PublicationState, SourcePageError, SourcePoll},
};
use ga_domain::{
    source_contract::{SourceCapabilities, SourcePage},
    timestamp::StoredInstant,
};
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[tokio::test]
#[ignore = "uses disposable TimescaleDB and pinned source adapter fixture"]
async fn publishes_source_revisions_and_cursor_as_one_transaction() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected a PostgreSQL test database"))?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Use a disposable database ending in _test"))?
        .to_owned();
    let inspect = PgPool::connect_with(options).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, expected);
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&inspect)
        .await?;
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture settings")?;
    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_SOURCE_PAGES")?)?)?;
    let source = fixture["instance"].as_str().ok_or("Missing instance")?;
    let source_hash = fixture["config_hash"]
        .as_str()
        .ok_or("Missing source hash")?;
    let caps = SourceCapabilities::from_json(&fixture["capabilities"])?;
    let mut checked_context_fence = false;
    for step in fixture["steps"].as_array().ok_or("Missing steps")? {
        let now: StoredInstant = step["now"]
            .as_str()
            .ok_or("Missing polling time")?
            .parse()?;
        let expected_state = step["expected"].as_str().ok_or("Missing expected state")?;
        let poll = db.prepare_source_poll(owner, source, now).await;
        if expected_state == "waiting" {
            assert!(matches!(poll?, SourcePoll::Deferred(_)));
            continue;
        }
        let poll = ready(poll?)?;
        assert_eq!(poll.cursor(), step["cursor"].as_str());
        let page = SourcePage::from_json(
            step["wire"]
                .as_str()
                .ok_or("Missing source page")?
                .as_bytes(),
            caps.max_page_size,
        )?;
        let stale = if step["verify_stale"] == true {
            Some(ready(db.prepare_source_poll(owner, source, now).await?)?)
        } else {
            None
        };
        if !checked_context_fence
            && !page.records.is_empty()
            && matches!(expected_state, "partial" | "complete")
        {
            let prior: (i64,i64) = sqlx::query_as(
                "SELECT (SELECT count(*) FROM source_payloads),(SELECT count(*) FROM metric_observations)",
            ).fetch_one(&inspect).await?;
            let mut model = inspect.begin().await?;
            sqlx::query("SELECT pg_advisory_xact_lock_shared(72104643)")
                .execute(&mut *model)
                .await?;
            let blocked = ready(db.prepare_source_poll(owner, source, now).await?)?;
            assert!(matches!(
                db.publish_source_page(blocked, source_hash, caps, &page)
                    .await,
                Err(SourcePageError::Busy),
            ));
            assert_eq!(
                ready(db.prepare_source_poll(owner, source, now).await?)?.cursor(),
                poll.cursor(),
            );
            let unchanged: (i64,i64) = sqlx::query_as(
                "SELECT (SELECT count(*) FROM source_payloads),(SELECT count(*) FROM metric_observations)",
            ).fetch_one(&inspect).await?;
            assert_eq!(unchanged, prior);
            model.rollback().await?;
            checked_context_fence = true;
        }
        let status = db.publish_source_page(poll, source_hash, caps, &page).await;
        if expected_state == "error" {
            assert!(
                status.is_err(),
                "Failed SQL constraint must stop the whole page"
            );
            continue;
        }
        let status = status?;
        let expected_state = match expected_state {
            "complete" => PublicationState::Complete,
            "partial" => PublicationState::Partial,
            "deferred" => PublicationState::Deferred,
            _ => return Err("Unsupported fixture state".into()),
        };
        assert_eq!(status.state, expected_state);
        assert_eq!(status.records, page.records.len());
        if let Some(stale) = stale {
            let result = db
                .publish_source_page(stale, source_hash, caps, &page)
                .await?;
            assert_eq!(result.state, PublicationState::Stale);
        }
    }
    let originals: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM source_payloads WHERE source_key IN ('rollback-a','rollback-z')",
    )
    .fetch_one(&inspect)
    .await?;
    assert_eq!(originals, 0);
    assert!(checked_context_fence);
    Ok(())
}

fn ready(poll: SourcePoll) -> Result<PreparedSourcePoll, Box<dyn Error>> {
    match poll {
        SourcePoll::Ready(poll) => Ok(*poll),
        _ => Err("Expected the bounded source poll to be ready".into()),
    }
}
