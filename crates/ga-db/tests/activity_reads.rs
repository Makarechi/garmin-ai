use std::{
    env,
    error::Error,
    fs,
    io::{self, Write},
};

use ga_db::Database;
use serde::Deserialize;
use serde_json::json;

#[derive(Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case")]
enum Request {
    List {
        start: String,
        end: String,
        kind: Option<String>,
        limit: u16,
    },
    Details {
        activity_id: String,
        include_samples: bool,
        offset: u32,
        limit: u16,
    },
}

#[tokio::test]
#[ignore = "requires a disposable TimescaleDB seeded by the activity fixture"]
async fn read_activity_pages() -> Result<(), Box<dyn Error>> {
    let db = Database::connect_lazy(&env::var("GA_TEST_DATABASE_URL")?)
        .map_err(|_| io::Error::other("Expected disposable TimescaleDB fixture URL"))?;
    let cases: Vec<Request> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_ACTIVITY_READS")?)?)?;
    for request in cases {
        let result = match request {
            Request::List {
                start,
                end,
                kind,
                limit,
            } => {
                db.list_activities(start.parse()?, end.parse()?, kind.as_deref(), limit)
                    .await
            }
            Request::Details {
                activity_id,
                include_samples,
                offset,
                limit,
            } => {
                db.read_activity_details(&activity_id, include_samples, offset, limit)
                    .await
            }
        };
        let mut output = io::stdout().lock();
        write!(output, "activity: ")?;
        match result {
            Ok(document) => serde_json::to_writer(&mut output, &document)?,
            Err(error) => {
                serde_json::to_writer(&mut output, &json!({"error": error.to_string()}))?;
            }
        }
        writeln!(output)?;
    }
    Ok(())
}
