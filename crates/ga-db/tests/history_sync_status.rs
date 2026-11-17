use std::{env, error::Error, fs, io};

use ga_db::Database;
use serde_json::Value;

#[tokio::test]
#[ignore = "requires the synthetic pinned Python history status fixture"]
async fn matches_the_committed_queue_and_redacts_scan_anchors() -> Result<(), Box<dyn Error>> {
    let db = Database::connect_lazy(&env::var("GA_TEST_DATABASE_URL")?)
        .map_err(|_| io::Error::other("Use the disposable fixture database"))?;
    let mut expected: Value =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_HISTORY_SYNC")?)?)?;
    let mut actual = db.history_status().await?;
    for outcome in [&mut expected, &mut actual] {
        for key in ["activity_scans", "plans"] {
            outcome[key]
                .as_array_mut()
                .ok_or_else(|| io::Error::other("Expected fixture status array"))?
                .sort_by_cached_key(Value::to_string);
        }
    }
    assert_eq!(actual, expected);
    Ok(())
}
