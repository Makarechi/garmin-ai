use std::{collections::BTreeMap, path::PathBuf, sync::Mutex, time::Instant};

use futures_util::TryStreamExt;
use ga_db::history_windows::ScheduleHistory;
use ga_domain::sha256_hex;

use super::*;

#[path = "history_workload/daily.rs"]
mod daily;
#[path = "history_workload/reconciliation.rs"]
mod reconciliation;

struct HistorySource {
    root: PathBuf,
    fingerprint: String,
    endpoints: Vec<String>,
    calls: Mutex<BTreeMap<String, usize>>,
}

impl EndpointSource for HistorySource {
    fn fingerprint(&self) -> &str {
        &self.fingerprint
    }

    async fn read(
        &self,
        resource: LegacyRead,
        maximum_bytes: u64,
    ) -> Result<Option<Vec<u8>>, GarminError> {
        let (key, endpoint) = match resource {
            LegacyRead::Daily(_, date) => {
                let key = date.to_string();
                let endpoint = self
                    .endpoints
                    .iter()
                    .find(|name| LegacyRead::queued_endpoint(name, &key) == Ok(resource))
                    .ok_or(GarminError::InvalidRequest)?;
                (key, endpoint.as_str())
            }
            LegacyRead::Devices => ("global".to_owned(), "devices"),
            _ => return Err(GarminError::InvalidRequest),
        };
        self.read_file(&key, endpoint, maximum_bytes)
            .await
            .map(Some)
    }
}

impl HistorySource {
    async fn read_file(
        &self,
        key: &str,
        endpoint: &str,
        maximum_bytes: u64,
    ) -> Result<Vec<u8>, GarminError> {
        let response = tokio::fs::read(self.root.join(format!("{key}-{endpoint}.json")))
            .await
            .map_err(|_| GarminError::InvalidRequest)?;
        if response.len() as u64 > maximum_bytes {
            return Err(GarminError::ResponseTooLarge);
        }
        self.record_call(endpoint)?;
        Ok(response)
    }

    fn record_call(&self, endpoint: &str) -> Result<(), GarminError> {
        *self
            .calls
            .lock()
            .map_err(|_| GarminError::InvalidRequest)?
            .entry(endpoint.to_owned())
            .or_default() += 1;
        Ok(())
    }
}

impl ActivityExportSource for HistorySource {
    #[expect(
        clippy::unused_async_trait_impl,
        reason = "Keep the async trait method deferred until the returned future is polled"
    )]
    async fn download(
        &self,
        _activity: NonZeroU64,
        _maximum_bytes: u64,
        _destination: &mut File,
    ) -> Result<u64, GarminError> {
        Err(GarminError::InvalidRequest)
    }
}

async fn drain(
    fixture: &Fixture,
    worker: &ScheduledGarminWorker<'_, HistorySource>,
    prefix: &str,
) -> Result<usize, Box<dyn Error>> {
    let mut completed = 0;
    let mut last_publication = Instant::now();
    loop {
        let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&fixture.inspect)
            .await?;
        match Box::pin(worker.process_one(now.try_into()?)).await? {
            GarminQueueStep::Endpoint(EndpointWork::Publication(
                EndpointPublication::Published(_),
            )) => {
                completed += 1;
                last_publication = Instant::now();
            }
            GarminQueueStep::Endpoint(EndpointWork::Deferred(RecordedJobStatus::Deferred)) => {}
            GarminQueueStep::Idle => {
                let pending: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM jobs WHERE kind='garmin_endpoint'
                       AND starts_with(dedup_key,$1) AND status IN ('pending','running'))",
                )
                .bind(prefix)
                .fetch_one(&fixture.inspect)
                .await?;
                if !pending {
                    return Ok(completed);
                }
                if last_publication.elapsed() > Duration::from_secs(20) {
                    return Err("Synthetic history queue stopped making progress".into());
                }
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
            _ => return Err("History job did not publish".into()),
        }
    }
}

#[tokio::test]
#[ignore = "requires a disposable database and a shared synthetic Garmin history fixture"]
async fn processes_shared_history_workload() -> Result<(), Box<dyn Error>> {
    let days: u16 = env::var("GA_TEST_HISTORY_DAYS")?.parse()?;
    assert!(matches!(days, 7 | 30 | 365));
    let (fixture, _archive, source) = setup_source().await?;
    let worker = ScheduledGarminWorker {
        runner: fixture.worker(&source),
        instance: "source:garmin:primary",
        history_days: days,
        backups_enabled: false,
        fit_limits: ExportLimits {
            max_bytes: 65_536,
            max_zip_entries: NonZeroUsize::MIN,
            max_duration: Duration::from_secs(3),
        },
    };
    let scheduling_clock = "2026-10-25T09:00:00Z".parse::<StoredInstant>()?;
    let preload = env::var("GA_TEST_HISTORY_PRELOAD").as_deref() == Ok("1");
    let started = Instant::now();
    let mut completed = 0;
    for offset in (0..days).step_by(2) {
        let scheduled = fixture
            .database
            .schedule_history(ScheduleHistory {
                authenticated_fingerprint: source.fingerprint(),
                timezone: "UTC",
                horizon_days: days,
                now: scheduling_clock,
            })
            .await?;
        assert_eq!(scheduled.queued, usize::from((days - offset).min(2)) * 20);
        if !preload {
            completed += Box::pin(drain(&fixture, &worker, "syncwindow:")).await?;
        }
    }
    if preload {
        completed += Box::pin(drain(&fixture, &worker, "syncwindow:")).await?;
    }
    let duration = started.elapsed().as_secs_f64();
    assert_eq!(completed, usize::from(days) * 20);
    let calls = source
        .calls
        .lock()
        .map_err(|_| "Could not count history requests")?
        .clone();
    assert_eq!(calls.len(), source.endpoints.len());
    assert!(calls.values().all(|count| *count == usize::from(days)));
    let status = fixture.database.history_status().await?;
    let verified = verify_archive(&fixture).await?;
    assert_eq!(verified, completed);
    println!(
        "WORKLOAD_RESULT={}",
        json!({
            "days": days,
            "history_queue": if preload {"preloaded"} else {"drained between scheduler passes"},
            "duration_seconds": duration,
            "fetch_counts": calls,
            "historical_windows": status["windows"],
            "archive_objects_verified": verified,
        })
    );
    Ok(())
}

async fn setup_source() -> Result<(Fixture, tempfile::TempDir, HistorySource), Box<dyn Error>> {
    let root = PathBuf::from(env::var("GA_TEST_HISTORY_FIXTURE")?);
    let endpoints = std::fs::read_to_string(root.join("endpoints.txt"))?;
    let fingerprint = std::fs::read_to_string(root.join("fingerprint.txt"))?;
    assert_eq!(fingerprint.len(), 64);
    assert!(fingerprint.bytes().all(|byte| byte.is_ascii_hexdigit()));
    let source = HistorySource {
        root,
        fingerprint,
        endpoints: endpoints.lines().map(str::to_owned).collect(),
        calls: Mutex::new(BTreeMap::new()),
    };
    assert_eq!(source.endpoints.len(), 20);
    let (fixture, archive) = setup().await?;
    sqlx::query("UPDATE app_state SET value=jsonb_build_object('fingerprint',$1::text) WHERE key='account:garmin'")
        .bind(source.fingerprint())
        .execute(&fixture.inspect)
        .await?;
    sqlx::query("UPDATE people SET timezone='UTC' WHERE singleton")
        .execute(&fixture.inspect)
        .await?;
    // The Python workload selects collection for every pack before scheduling.
    sqlx::query("UPDATE module_configs SET collection_enabled=true")
        .execute(&fixture.inspect)
        .await?;

    Ok((fixture, archive, source))
}

async fn verify_archive(fixture: &Fixture) -> Result<usize, Box<dyn Error>> {
    let mut sources = sqlx::query_as::<_, (String, String)>(
        "SELECT archive_key,payload_hash FROM source_payloads",
    )
    .fetch(&fixture.inspect);
    let mut bytes = Vec::new();
    let mut verified = 0;
    while let Some((key, hash)) = sources.try_next().await? {
        bytes.clear();
        fixture
            .archive
            .open_reader(&key.parse()?)?
            .take(65_537)
            .read_to_end(&mut bytes)?;
        assert!(bytes.len() <= 65_536);
        assert_eq!(sha256_hex(&bytes), hash);
        verified += 1;
    }

    Ok(verified)
}
