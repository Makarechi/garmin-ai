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
struct Request {
    start: String,
    end: String,
    kind: Option<String>,
    limit: u16,
}

#[tokio::test]
#[ignore = "requires a disposable TimescaleDB seeded by the diary fixture"]
async fn read_diary_pages() -> Result<(), Box<dyn Error>> {
    let db = Database::connect_lazy(&env::var("GA_TEST_DATABASE_URL")?)
        .map_err(|_| io::Error::other("Expected disposable TimescaleDB fixture URL"))?;
    let cases: Vec<Request> = serde_json::from_slice(&fs::read(env::var("GA_TEST_DIARY_READS")?)?)?;
    for request in cases {
        let answer = db
            .list_owner_events(
                request.start.parse()?,
                request.end.parse()?,
                request.kind.as_deref(),
                request.limit,
            )
            .await;
        let mut output = io::stdout().lock();
        write!(output, "diary: ")?;
        match answer {
            Ok(document) => serde_json::to_writer(&mut output, &document)?,
            Err(error) => {
                serde_json::to_writer(&mut output, &json!({"error": error.to_string()}))?;
            }
        }
        writeln!(output)?;
    }
    Ok(())
}
