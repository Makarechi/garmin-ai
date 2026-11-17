use std::{collections::BTreeMap, env, error::Error, io, str::FromStr};

use ga_db::{
    Database,
    garmin_owner::OwnerVerificationError,
    replay::{ReplaySelection, ReplaySelectionError},
};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

#[tokio::test]
#[ignore = "uses a disposable TimescaleDB and Python replay eligibility corpus"]
async fn matches_replay_candidates_across_stable_pages() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or_else(|| io::Error::other("Expected a PostgreSQL test database"))?;
    let opts =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let name = opts
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or_else(|| io::Error::other("Use a disposable database ending in _test"))?;
    let inspect = PgPool::connect_with(opts.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(actual, name);
    let state_before: i64 = sqlx::query_scalar("SELECT count(*) FROM app_state")
        .fetch_one(&inspect)
        .await?;
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let account = "a".repeat(64);
    assert!(matches!(
        db.replay_source_page(ReplaySelection {
            authenticated_fingerprint: &"b".repeat(64),
            target_parser_version: 9,
            after: None,
        })
        .await,
        Err(ReplaySelectionError::Owner(
            OwnerVerificationError::Mismatch
        ))
    ));
    let mut expected: BTreeMap<String, Vec<(String, bool)>> =
        serde_json::from_str(&env::var("GA_TEST_EXPECTED_REPLAY")?)?;
    let upgrade = expected
        .remove("upgrade")
        .ok_or("Missing upgrade fixture")?;
    let rollback = expected
        .remove("rollback")
        .ok_or("Missing rollback fixture")?;
    let selection = |after| ReplaySelection {
        authenticated_fingerprint: &account,
        target_parser_version: 9,
        after,
    };
    let first = db.replay_source_page(selection(None)).await?;
    assert_eq!(first.sources.len(), 128);
    let next = first.next_after.ok_or("Missing replay cursor")?;
    let second = db.replay_source_page(selection(Some(next))).await?;
    assert!(second.next_after.is_none());
    assert!(!second.sources.is_empty());
    let all: Vec<_> = first
        .sources
        .into_iter()
        .chain(second.sources)
        .map(|source| (source.id.to_string(), source.canonical))
        .collect();
    assert_eq!(all.len(), upgrade.len());
    for (actual, expected) in all.into_iter().zip(upgrade) {
        assert_eq!(actual, expected);
    }
    let prior = db
        .replay_source_page(ReplaySelection {
            target_parser_version: 8,
            ..selection(None)
        })
        .await?;
    assert!(prior.next_after.is_none());
    let prior: Vec<_> = prior
        .sources
        .into_iter()
        .map(|source| (source.id.to_string(), source.canonical))
        .collect();
    assert_eq!(prior, rollback);
    let state_after: i64 = sqlx::query_scalar("SELECT count(*) FROM app_state")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(state_before, state_after);
    let queued: i64 = sqlx::query_scalar("SELECT count(*) FROM jobs WHERE kind='raw_replay'")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(queued, 0);
    Ok(())
}
