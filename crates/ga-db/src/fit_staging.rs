use std::{
    error::Error,
    fmt,
    io::{Read, Seek},
};

use ga_fit::{
    ExportLimits, ExportProjectionInventory, ExportProjectionMessage, FitError,
    visit_activity_projection,
};
use sqlx::{Postgres, Transaction};
use tokio::{sync::mpsc, task, time::timeout};

use crate::{Database, ReadinessError, check_readiness};

const BATCH_ROWS: usize = 500;
const BATCH_BYTES: usize = 256 * 1024;
const COPY_START: &[u8] = b"PGCOPY\n\xff\r\n\0\0\0\0\0\0\0\0\0";
const COPY_END: &[u8] = b"\xff\xff";

#[derive(Debug)]
pub enum FitStageError {
    StoreUnavailable(ReadinessError),
    DeadlineExceeded,
    Decode(FitError),
    DecoderTaskFailed,
    Database,
    RowCountMismatch,
}

impl fmt::Display for FitStageError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::StoreUnavailable(_) => "FIT projection staging is unavailable",
            Self::DeadlineExceeded => "FIT projection staging exceeded its deadline",
            Self::Decode(_) => "FIT export could not be validated",
            Self::DecoderTaskFailed => "FIT decoder task did not complete",
            Self::Database => "FIT staging rows could not be written",
            Self::RowCountMismatch => "FIT staged row count did not match the decoder",
        })
    }
}

impl Error for FitStageError {}

impl From<sqlx::Error> for FitStageError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

/// Holds a PostgreSQL transaction with candidates visible only to this connection.
pub struct StagedFitProjection {
    pub(crate) transaction: Transaction<'static, Postgres>,
    inventory: ExportProjectionInventory,
}

impl StagedFitProjection {
    pub fn inventory(&self) -> ExportProjectionInventory {
        self.inventory
    }

    pub async fn discard(self) -> Result<(), FitStageError> {
        self.transaction.rollback().await?;
        Ok(())
    }
}

impl Database {
    /// Stage a verified export. No durable activity rows are written by this method.
    pub async fn stage_fit_projection(
        &self,
        source: impl Read + Seek + Send + 'static,
        limits: ExportLimits,
    ) -> Result<StagedFitProjection, FitStageError> {
        timeout(
            limits.max_duration,
            self.stage_fit_within_budget(source, limits),
        )
        .await
        .map_err(|_| FitStageError::DeadlineExceeded)?
    }

    async fn stage_fit_within_budget(
        &self,
        source: impl Read + Seek + Send + 'static,
        limits: ExportLimits,
    ) -> Result<StagedFitProjection, FitStageError> {
        let mut transaction = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *transaction)
            .await?;
        check_readiness(&mut transaction)
            .await
            .map_err(FitStageError::StoreUnavailable)?;
        let millis = limits.max_duration.as_millis().clamp(1, i32::MAX as u128);
        sqlx::query("SELECT set_config('statement_timeout', $1, true)")
            .bind(format!("{millis}ms"))
            .execute(&mut *transaction)
            .await?;
        sqlx::query(
            "CREATE TEMP TABLE fit_candidate_rows (
                kind text NOT NULL,
                sequence integer NOT NULL,
                payload jsonb NOT NULL
             ) ON COMMIT DROP",
        )
        .execute(&mut *transaction)
        .await?;

        let mut copy = transaction
            .copy_in_raw("COPY pg_temp.fit_candidate_rows FROM STDIN (FORMAT binary)")
            .await?;
        copy.send(COPY_START).await?;
        let (sender, mut rows) = mpsc::channel::<Vec<u8>>(1);
        let decoder = task::spawn_blocking(move || stream_copy(source, limits, &sender));
        while let Some(batch) = rows.recv().await {
            if copy.send(batch).await.is_err() {
                rows.close();
                let _ = copy.abort("FIT candidate staging stopped").await;
                let _ = decoder.await;
                return Err(FitStageError::Database);
            }
        }
        let outcome = decoder
            .await
            .map_err(|_| FitStageError::DecoderTaskFailed)
            .and_then(|result| result.map_err(FitStageError::Decode));
        let inventory = match outcome {
            Ok(inventory) => inventory,
            Err(error) => {
                let _ = copy.abort("FIT candidate validation failed").await;
                return Err(error);
            }
        };
        copy.send(COPY_END).await?;
        let written = copy.finish().await?;
        if written != u64::from(inventory.messages) {
            return Err(FitStageError::RowCountMismatch);
        }
        Ok(StagedFitProjection {
            transaction,
            inventory,
        })
    }
}

fn stream_copy(
    source: impl Read + Seek,
    limits: ExportLimits,
    sender: &mpsc::Sender<Vec<u8>>,
) -> Result<ExportProjectionInventory, FitError> {
    let mut batch = CopyBatch {
        sender,
        bytes: Vec::with_capacity(BATCH_BYTES),
        rows: 0,
    };
    let inventory = visit_activity_projection(
        source,
        limits,
        || sender.is_closed(),
        |row| batch.append(row),
    )?;
    batch.flush()?;
    Ok(inventory)
}

struct CopyBatch<'a> {
    sender: &'a mpsc::Sender<Vec<u8>>,
    bytes: Vec<u8>,
    rows: usize,
}

impl CopyBatch<'_> {
    fn append(&mut self, row: ExportProjectionMessage) -> Result<(), FitError> {
        let data = binary_row(row)?;
        if self.rows == BATCH_ROWS || self.bytes.len() + data.len() > BATCH_BYTES {
            self.flush()?;
        }
        if data.len() > BATCH_BYTES {
            for chunk in data.chunks(BATCH_BYTES) {
                self.sender
                    .blocking_send(chunk.to_vec())
                    .map_err(|_| FitError::Cancelled)?;
            }
        } else {
            self.bytes.extend(data);
            self.rows += 1;
        }
        Ok(())
    }

    fn flush(&mut self) -> Result<(), FitError> {
        if self.rows > 0 {
            let bytes = std::mem::replace(&mut self.bytes, Vec::with_capacity(BATCH_BYTES));
            self.rows = 0;
            self.sender
                .blocking_send(bytes)
                .map_err(|_| FitError::Cancelled)?;
        }
        Ok(())
    }
}

fn binary_row(row: ExportProjectionMessage) -> Result<Vec<u8>, FitError> {
    let kind = format!("fit_{}", row.message.name);
    let mut bytes = Vec::new();
    bytes.extend(3_i16.to_be_bytes());
    let length = i32::try_from(kind.len()).map_err(|_| FitError::InvalidRecord)?;
    bytes.extend(length.to_be_bytes());
    bytes.extend(kind.into_bytes());
    bytes.extend(4_i32.to_be_bytes());
    let sequence = i32::try_from(row.sequence).map_err(|_| FitError::InvalidRecord)?;
    bytes.extend(sequence.to_be_bytes());
    let json_length_offset = bytes.len();
    bytes.extend([0_u8; 4]);
    bytes.push(1); // PostgreSQL JSONB binary format version.
    serde_json::to_writer(&mut bytes, &row.message.payload).map_err(|_| FitError::InvalidRecord)?;
    let json_length =
        i32::try_from(bytes.len() - json_length_offset - 4).map_err(|_| FitError::InvalidRecord)?;
    bytes[json_length_offset..json_length_offset + 4].copy_from_slice(&json_length.to_be_bytes());
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use std::{fs::File, io::Cursor, num::NonZeroUsize, time::Duration};

    use ga_fit::MAX_FIT_BYTES;

    use super::*;

    const FIT: &[u8] = b"\x0e\x20\x34\x08\x0b\x00\x00\x00.FIT\xfb\xdc\x40\x00\x00\x14\x00\x01\x03\x01\x02\x00\x48\xb0\xe5";

    fn limits(seconds: u64) -> ExportLimits {
        ExportLimits {
            max_bytes: MAX_FIT_BYTES,
            max_zip_entries: NonZeroUsize::MIN,
            max_duration: Duration::from_secs(seconds),
        }
    }

    async fn ensure_fixture() -> Result<Database, Box<dyn Error>> {
        let db = Database::connect_lazy(&std::env::var("GA_TEST_DATABASE_URL")?)
            .map_err(|_| "Invalid disposable database configuration")?;
        let name: String = sqlx::query_scalar("SELECT current_database()")
            .fetch_one(&db.control)
            .await?;
        if !name.ends_with("_test") {
            return Err("Expected a disposable _test fixture".into());
        }
        Ok(db)
    }

    #[tokio::test]
    #[ignore = "uses a migrated disposable TimescaleDB fixture"]
    async fn stages_compatible_rows_privately_and_rolls_back_on_late_failure()
    -> Result<(), Box<dyn Error>> {
        let db = ensure_fixture().await?;
        let source = Cursor::new(FIT.repeat(515));
        let mut staged = db.stage_fit_projection(source, limits(10)).await?;
        assert_eq!(staged.inventory().messages, 515);
        let (rows, equivalent, last): (i64, bool, i32) = sqlx::query_as(
            "SELECT count(*), bool_and(kind = 'fit_record' AND payload->>'heart_rate' = '72'),
                 max(sequence) FROM pg_temp.fit_candidate_rows",
        )
        .fetch_one(&mut *staged.transaction)
        .await?;
        assert_eq!((rows, equivalent, last), (515, true, 514));
        let separate: bool = sqlx::query_scalar(
            "SELECT to_regclass('pg_temp.fit_candidate_rows') IS NULL
                 AND NOT EXISTS (SELECT 1 FROM activity_parts WHERE kind = 'fit_record')",
        )
        .fetch_one(&db.control)
        .await?;
        assert!(separate);
        staged.discard().await?;

        let mut bad = FIT.repeat(515);
        if let Some(last) = bad.last_mut() {
            *last ^= 1;
        }
        assert!(matches!(
            db.stage_fit_projection(Cursor::new(bad), limits(10)).await,
            Err(FitStageError::Decode(FitError::FileChecksum))
        ));
        let retry = db
            .stage_fit_projection(Cursor::new(FIT), limits(10))
            .await?;
        assert_eq!(retry.inventory().messages, 1);
        retry.discard().await?;
        Ok(())
    }

    #[tokio::test]
    #[ignore = "uses a migrated disposable DB and the generated 100 MiB activity ZIP"]
    async fn stages_the_largest_export_with_bounded_batches() -> Result<(), Box<dyn Error>> {
        let db = ensure_fixture().await?;
        let fixture = std::env::var_os("GA_TEST_FIT_ZIP_CEILING").ok_or("Missing synthetic ZIP")?;
        let mut staged = db
            .stage_fit_projection(File::open(fixture)?, limits(90))
            .await?;
        let (rows, last, largest): (i64, i32, i32) = sqlx::query_as(
            "SELECT count(*), max(sequence), max(jsonb_array_length(payload->'_fit'->'fields'))
             FROM pg_temp.fit_candidate_rows",
        )
        .fetch_one(&mut *staged.transaction)
        .await?;
        assert_eq!((rows, last, largest), (409_600, 409_599, 2));
        assert_eq!(staged.inventory().export.fit_bytes, MAX_FIT_BYTES);
        staged.discard().await?;
        Ok(())
    }
}
