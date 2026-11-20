use std::{
    error::Error,
    io::{self, Write},
};

use ga_db::Database;
use serde_json::json;

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn run(limit: i64) -> Result<(), Box<dyn Error>> {
    let [database] = crate::configuration::read(["GA_DATABASE_URL"])?;
    let url = database
        .filter(|url| !url.is_empty())
        .ok_or("Projection audit requires a database")?;
    let database = Database::connect_lazy(&url).map_err(|_| "Storage configuration is invalid")?;
    let mut output = io::stdout().lock();
    let totals = database
        .audit_custom_projections(limit, |page| {
            serde_json::to_writer(&mut output, page)?;
            output.write_all(b"\n")?;
            output.flush()
        })
        .await?;
    serde_json::to_writer(
        &mut output,
        &json!({"type": "summary", "totals": totals, "writes": false}),
    )?;
    output.write_all(b"\n")?;
    output.flush()?;
    Ok(())
}
