use std::{
    error::Error,
    fs::File,
    io::{BufReader, ErrorKind, Read},
    path::PathBuf,
    sync::Arc,
};

use ga_archive::{
    Archive, ArchiveError, ArchiveKey, GarminTokenFile, PutLimits,
    backup_envelope::open_source,
    storage_files::{StorageAccess, StorageFilesGuard, validate_backup_layout},
};
use ga_db::{
    Database,
    activity_documents::{
        ActivityDocument, ActivitySummaryKind, ApplyActivityDocument, ApplyActivitySummary,
        ApplyHealthPayload, DocumentError, DocumentStatus,
    },
    fit_ingest::{FitIngestError, FitIngestOutcome, LegacyFitCapture},
    garmin_owner::{GarminSetupError, LocalGarminSetup, LocalGarminSetupMode},
    raw_capture::{CaptureError, LegacyCapture},
};
use ga_domain::{sha256_hex, timestamp::StoredInstant};
use ga_fit::MAX_FIT_BYTES;
use ga_garmin::{GarminError, TransportConfig, legacy::LegacySession};
use serde::{Deserialize, Deserializer, Serialize, de};
use subtle::ConstantTimeEq;
use time::OffsetDateTime;

use crate::{
    configuration,
    garmin_probe::{MAX_PROBE_DAYS, MAX_SCOPED_ACTIVITIES},
    replay_worker::FIT_EXPORT_LIMITS,
};

// Twenty daily endpoints, activity list, legacy device inventory and seven requests per activity.
const MAX_REQUESTS: usize = MAX_PROBE_DAYS * 20 + 2 + MAX_SCOPED_ACTIVITIES as usize * 7;
const JSON_LIMITS: PutLimits = PutLimits {
    max_bytes: 8 * 1024 * 1024,
    disk_reserve_bytes: 64 * 1024 * 1024,
};
const FIT_ARCHIVE_LIMITS: PutLimits = PutLimits {
    max_bytes: MAX_FIT_BYTES,
    ..JSON_LIMITS
};

#[derive(Deserialize)]
struct ProbeReport {
    #[serde(default, deserialize_with = "provided_fingerprint")]
    account_fingerprint: Option<String>,
    activity_list_archive: Option<String>,
    #[serde(deserialize_with = "bounded_requests")]
    requests: Vec<ProbeRequest>,
}

fn provided_fingerprint<'de, D>(parser: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    String::deserialize(parser).map(Some)
}

fn bounded_requests<'de, D>(parser: D) -> Result<Vec<ProbeRequest>, D::Error>
where
    D: Deserializer<'de>,
{
    struct Requests;

    impl<'de> de::Visitor<'de> for Requests {
        type Value = Vec<ProbeRequest>;

        fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            write!(f, "up to {MAX_REQUESTS} probe requests")
        }

        fn visit_seq<S: de::SeqAccess<'de>>(
            self,
            mut sequence: S,
        ) -> Result<Self::Value, S::Error> {
            let mut requests = Vec::new();
            while let Some(request) = sequence.next_element()? {
                if requests.len() == MAX_REQUESTS {
                    return Err(de::Error::custom(
                        "Coverage report exceeds the supported request count",
                    ));
                }
                requests.push(request);
            }
            Ok(requests)
        }
    }
    parser.deserialize_seq(Requests)
}

#[cfg(test)]
mod request_bound_tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn import_accepts_scoped_reports_up_to_the_request_limit() -> Result<(), Box<dyn Error>> {
        let row = json!({ "endpoint": "daily", "key": "2026-11-20", "status": "available" });
        let mut report = json!({ "requests": vec![row.clone(); 31 * 20 + 1 + 6 * 7] });
        let parsed: ProbeReport = serde_json::from_value(report.clone())?;
        assert_eq!(parsed.requests.len(), 663);

        report["requests"] = json!(vec![row.clone(); MAX_REQUESTS]);
        let parsed: ProbeReport = serde_json::from_value(report.clone())?;
        assert_eq!(parsed.requests.len(), MAX_REQUESTS);

        report["requests"] = json!(vec![row; MAX_REQUESTS + 1]);
        let Err(error) = serde_json::from_value::<ProbeReport>(report) else {
            panic!("oversized report was accepted");
        };
        assert!(
            error
                .to_string()
                .contains("exceeds the supported request count")
        );
        Ok(())
    }
}

#[derive(Deserialize)]
struct ProbeRequest {
    endpoint: String,
    key: String,
    status: String,
    archive_key: Option<String>,
    fetched_at: Option<String>,
}

#[derive(Serialize)]
struct ImportFailure {
    endpoint: String,
    error_type: &'static str,
}

#[derive(Default, Serialize)]
struct ImportSummary {
    imported: usize,
    errors: Vec<ImportFailure>,
}

enum ImportOutcome {
    Imported,
    Disabled,
    Failed(&'static str),
}

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn run(path: Option<PathBuf>, confirm: bool) -> Result<bool, Box<dyn Error>> {
    let [url, data, tokens, locks, backups, timezone] = configuration::read([
        "GA_DATABASE_URL",
        "GA_DATA_DIR",
        "GA_TOKEN_DIR",
        "GA_LOCK_DIR",
        "GA_BACKUP_DIR",
        "GA_TIMEZONE",
    ])?;
    let url = url
        .filter(|url| !url.is_empty())
        .ok_or("Probe import requires a database")?;
    let data = PathBuf::from(data.unwrap_or_else(|| "data".into()));
    let tokens = PathBuf::from(tokens.unwrap_or_else(|| "tokens/garmin".into()));
    let backups = PathBuf::from(backups.unwrap_or_else(|| "backups".into()));
    let locks = configuration::lock_directory(&data, &tokens, locks.map(PathBuf::from))?;
    validate_backup_layout(&data, &tokens, &locks, &backups)?;
    let _files = StorageFilesGuard::acquire(&locks, StorageAccess::Active)?;
    let database = Database::connect_lazy(&url).map_err(|_| "Storage configuration is invalid")?;
    let mut guard = database
        .acquire_local_garmin_setup(LocalGarminSetupMode::ImportProbe {
            confirm_existing_owner: confirm,
        })
        .await?;

    let path = path.unwrap_or_else(|| data.join("coverage-report.json"));
    let report: ProbeReport = serde_json::from_reader(BufReader::new(open_source(&path)?))?;
    let confirmed = if confirm {
        let tokens =
            GarminTokenFile::open_existing(&tokens)?.ok_or(GarminError::AuthenticationRequired)?;
        let (session, rotated) =
            LegacySession::restore_for_local_verification(&tokens, TransportConfig::default())
                .await?;
        Some((tokens, session, rotated))
    } else {
        None
    };
    let expected = confirmed
        .as_ref()
        .map(|(_, session, _)| session.fingerprint());
    let fingerprint = probe_owner(report.account_fingerprint.as_deref(), expected.as_deref())?;
    let raw = data.join("raw");
    let retained = Archive::has_retained_entries(&raw)?;
    guard
        .publish(fingerprint, retained, || {
            if let Some((tokens, session, true)) = &confirmed {
                session
                    .save_for_owner(tokens, fingerprint)
                    .map_err(|_| GarminSetupError::TokenPublication)?;
            }
            Ok(())
        })
        .await?;
    guard.begin_probe_import().await?;
    let archive = Arc::new(Archive::open(&raw)?);
    let timezone = timezone.as_deref().unwrap_or("Europe/Bratislava");
    let mut importer = Importer {
        database: &database,
        archive,
        fingerprint,
        timezone,
        guard,
    };
    let summary = importer.apply(&report).await?;
    println!("{}", serde_json::to_string(&summary)?);
    Ok(summary.errors.is_empty())
}

fn probe_owner<'a>(
    reported: Option<&'a str>,
    confirmed: Option<&'a str>,
) -> Result<&'a str, Box<dyn Error>> {
    if let (Some(reported), Some(confirmed)) = (reported, confirmed)
        && !bool::from(reported.as_bytes().ct_eq(confirmed.as_bytes()))
    {
        return Err("Probe provenance does not match the confirmed owner".into());
    }
    reported.or(confirmed).ok_or_else(|| {
        "Confirm the original owner before importing an older coverage report".into()
    })
}

struct Importer<'a> {
    database: &'a Database,
    archive: Arc<Archive>,
    fingerprint: &'a str,
    timezone: &'a str,
    guard: LocalGarminSetup,
}

impl Importer<'_> {
    async fn apply(&mut self, report: &ProbeReport) -> Result<ImportSummary, Box<dyn Error>> {
        if let Some(archive) = &report.activity_list_archive {
            let fetched_at = report
                .requests
                .iter()
                .find(|request| {
                    request.endpoint == "activities"
                        && request.archive_key.as_ref() == Some(archive)
                })
                .and_then(|request| request.fetched_at.as_deref());
            if matches!(
                self.apply_json("activities", "probe", archive, fetched_at)
                    .await?,
                ImportOutcome::Failed(_)
            ) {
                return Err("Probe activity list could not be normalized".into());
            }
        }

        let mut summary = ImportSummary::default();
        for request in &report.requests {
            if !matches!(request.status.as_str(), "available" | "empty") {
                continue;
            }
            let archive_key = request.archive_key.as_deref();
            let fetched_at = request.fetched_at.as_deref();
            let outcome = if request.endpoint == "activity_fit" {
                self.apply_fit(&request.key, archive_key, fetched_at)
                    .await?
            } else {
                self.apply_json(
                    &request.endpoint,
                    &request.key,
                    archive_key.ok_or("Missing probe archive key")?,
                    fetched_at,
                )
                .await?
            };
            match outcome {
                ImportOutcome::Imported => summary.imported += 1,
                ImportOutcome::Disabled => {}
                ImportOutcome::Failed(error_type) => {
                    summary.imported += 1;
                    summary.errors.push(ImportFailure {
                        endpoint: request.endpoint.clone(),
                        error_type,
                    });
                }
            }
        }
        Ok(summary)
    }

    async fn apply_json(
        &mut self,
        endpoint: &str,
        key: &str,
        archive_key: &str,
        fetched_at: Option<&str>,
    ) -> Result<ImportOutcome, Box<dyn Error>> {
        self.guard.check_connection().await?;
        let archive_key: ArchiveKey = archive_key.parse()?;
        let file =
            self.archive
                .open_verified_reader(&archive_key, JSON_LIMITS.max_bytes, || false)?;
        let fetched_at = fetch_time(&file, fetched_at)?;
        let mut body = Vec::new();
        file.take(JSON_LIMITS.max_bytes + 1)
            .read_to_end(&mut body)?;
        if body.len() as u64 > JSON_LIMITS.max_bytes
            || sha256_hex(&body) != archive_key.digest_hex()
        {
            return Err("Probe archive changed while reading it".into());
        }
        self.guard.check_connection().await?;
        let captured = match self
            .database
            .capture_legacy_json(
                LegacyCapture {
                    authenticated_fingerprint: self.fingerprint,
                    endpoint,
                    source_key: key,
                    fetched_at,
                    body,
                },
                Arc::clone(&self.archive),
                JSON_LIMITS,
            )
            .await
        {
            Ok(captured) => captured,
            Err(CaptureError::CollectionDisabled) => return Ok(ImportOutcome::Disabled),
            Err(error) => return Err(error.into()),
        };
        self.guard.check_connection().await?;
        let status = if let Some(document) = ActivityDocument::for_endpoint(endpoint) {
            self.database
                .apply_activity_document(ApplyActivityDocument {
                    account_fingerprint: self.fingerprint,
                    activity_id: key,
                    document,
                    source_id: captured.id,
                    requested_at: fetched_at,
                    timezone: self.timezone,
                })
                .await
        } else if let Some(summary) = ActivitySummaryKind::for_endpoint(endpoint) {
            self.database
                .apply_activity_summary(ApplyActivitySummary {
                    account_fingerprint: self.fingerprint,
                    source_key: key,
                    summary,
                    source_id: captured.id,
                    requested_at: fetched_at,
                    timezone: self.timezone,
                })
                .await
        } else {
            self.database
                .apply_health_payload(ApplyHealthPayload {
                    account_fingerprint: self.fingerprint,
                    endpoint,
                    calendar_key: key,
                    source_id: captured.id,
                    requested_at: fetched_at,
                    timezone: self.timezone,
                })
                .await
        };
        match status {
            Ok(DocumentStatus::Error) => Ok(ImportOutcome::Failed("ValueError")),
            Ok(_) => Ok(ImportOutcome::Imported),
            Err(DocumentError::Access(CaptureError::CollectionDisabled)) => {
                Ok(ImportOutcome::Disabled)
            }
            Err(error) => Err(error.into()),
        }
    }

    async fn apply_fit(
        &mut self,
        activity: &str,
        archive_key: Option<&str>,
        fetched_at: Option<&str>,
    ) -> Result<ImportOutcome, Box<dyn Error>> {
        self.guard.check_connection().await?;
        match self
            .database
            .authorize_archived_legacy_fit(self.fingerprint)
            .await
        {
            Ok(()) => {}
            Err(FitIngestError::Access(CaptureError::CollectionDisabled)) => {
                return Ok(ImportOutcome::Disabled);
            }
            Err(error) => return Err(error.into()),
        }
        let Some(archive_key) = archive_key else {
            return Ok(ImportOutcome::Failed("KeyError"));
        };
        let Ok(key) = archive_key.parse() else {
            return Ok(ImportOutcome::Failed("ValueError"));
        };
        let file = match self.archive.open_reader(&key) {
            Ok(file) => file,
            Err(ArchiveError::Io(error)) if error.kind() == ErrorKind::NotFound => {
                return Ok(ImportOutcome::Failed("FileNotFoundError"));
            }
            Err(ArchiveError::Io(error)) if error.kind() == ErrorKind::PermissionDenied => {
                return Ok(ImportOutcome::Failed("PermissionError"));
            }
            Err(error) => return Err(error.into()),
        };
        let Ok(fetched_at) = fetch_time(&file, fetched_at) else {
            return Ok(ImportOutcome::Failed("ValueError"));
        };
        let capture = match self
            .database
            .capture_archived_activity_export(
                LegacyFitCapture {
                    activity_id: activity,
                    authenticated_fingerprint: self.fingerprint,
                    fetched_at,
                },
                file,
                Arc::clone(&self.archive),
                FIT_ARCHIVE_LIMITS,
                key,
            )
            .await
        {
            Ok(capture) => capture,
            Err(FitIngestError::Access(CaptureError::CollectionDisabled)) => {
                return Ok(ImportOutcome::Disabled);
            }
            Err(FitIngestError::ActivityMissing) => {
                return Ok(ImportOutcome::Failed("LookupError"));
            }
            Err(
                FitIngestError::InputTooLarge
                | FitIngestError::Archive(ArchiveError::ExistingObjectDigestMismatch),
            ) => return Ok(ImportOutcome::Failed("ValueError")),
            Err(error) => return Err(error.into()),
        };
        self.guard.check_connection().await?;
        match self
            .database
            .apply_legacy_activity_export(&capture, Arc::clone(&self.archive), FIT_EXPORT_LIMITS)
            .await
        {
            Ok(FitIngestOutcome::Error { reason }) => Ok(ImportOutcome::Failed(reason)),
            Ok(_) => Ok(ImportOutcome::Imported),
            Err(FitIngestError::Access(CaptureError::CollectionDisabled)) => {
                Ok(ImportOutcome::Disabled)
            }
            Err(error) => Err(error.into()),
        }
    }
}

fn fetch_time(file: &File, value: Option<&str>) -> Result<StoredInstant, Box<dyn Error>> {
    match value.filter(|value| !value.is_empty()) {
        Some(value) => StoredInstant::from_python_isoformat(value)
            .ok_or_else(|| "Invalid probe archive timestamp".into()),
        None => Ok(StoredInstant::try_from(OffsetDateTime::from(
            file.metadata()?.modified()?,
        ))?),
    }
}
