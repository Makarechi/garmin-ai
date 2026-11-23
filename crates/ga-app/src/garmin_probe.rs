use std::{
    error::Error,
    fs::File,
    io::{self, Seek, Write},
    num::{NonZeroU16, NonZeroU64},
    path::{Path, PathBuf},
};

use ga_archive::{
    Archive, ArchiveKind, GarminTokenFile, PutLimits,
    coverage_report::CoverageReportFile,
    storage_files::{StorageAccess, StorageFilesGuard, validate_backup_layout},
};
use ga_db::{
    Database,
    garmin_owner::{GarminSetupError, LocalGarminSetup, LocalGarminSetupMode},
};
use ga_domain::{
    event_input::source_calendar_date,
    garmin_summary::gmt_instant,
    timestamp::{OffsetTimestamp, StoredInstant, source_timezone_available},
};
use ga_garmin::{
    DateRange, GarminError, TransportConfig,
    legacy::{LEGACY_CAPABILITIES, LegacyRead, LegacyReader, LegacyResource, LegacySession},
};
use serde::Deserialize;
use serde_json::{Value, json};
use subtle::ConstantTimeEq;
use time::{Date, Duration, OffsetDateTime, format_description::well_known::Iso8601};

use crate::configuration;

const JSON_LIMITS: PutLimits = PutLimits {
    max_bytes: 8 * 1024 * 1024,
    disk_reserve_bytes: 64 * 1024 * 1024,
};
const FIT_LIMITS: PutLimits = PutLimits {
    max_bytes: ga_fit::MAX_FIT_BYTES,
    ..JSON_LIMITS
};
pub(crate) const MAX_PROBE_DAYS: usize = 31;
pub(crate) const MAX_SCOPED_ACTIVITIES: u16 = 99;
const ACTIVITY_LIST_LIMIT: NonZeroU16 = NonZeroU16::new(MAX_SCOPED_ACTIVITIES + 1).unwrap();

#[derive(Debug)]
pub(crate) struct InvalidRange;
impl std::fmt::Display for InvalidRange {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Probe range must be 1–31 days")
    }
}
impl Error for InvalidRange {}

pub(crate) fn parse_date(value: &str) -> Result<Date, &'static str> {
    Date::parse(value, &Iso8601::DATE)
        .ok()
        .or_else(|| {
            let suffix = match value.as_bytes().get(4..) {
                Some([b'W', _, _]) => "1",
                Some([b'-', b'W', _, _]) => "-1",
                _ => return None,
            };
            Date::parse(&format!("{value}{suffix}"), &Iso8601::DATE).ok()
        })
        .filter(|date| (1..=9999).contains(&date.year()))
        .ok_or("Expected an ISO calendar date")
}

#[tokio::main(flavor = "current_thread")]
pub(crate) async fn run(
    start: Option<Date>,
    end: Option<Date>,
    activities_in_range: bool,
) -> Result<(), Box<dyn Error>> {
    let [url, data, tokens, locks, backups, timezone] = configuration::read([
        "GA_DATABASE_URL",
        "GA_DATA_DIR",
        "GA_TOKEN_DIR",
        "GA_LOCK_DIR",
        "GA_BACKUP_DIR",
        "GA_TIMEZONE",
    ])?;
    let timezone = timezone.as_deref().unwrap_or("Europe/Bratislava");
    let end = match end {
        Some(end) => end,
        None => source_calendar_date(
            StoredInstant::try_from(OffsetDateTime::now_utc())?,
            timezone,
        )
        .ok_or("Invalid probe time zone")?,
    };
    let start = start
        .or_else(|| end.checked_sub(Duration::days(13)))
        .filter(|day| day.year() >= 1)
        .ok_or(InvalidRange)?;
    let days = (end - start).whole_days();
    if !usize::try_from(days).is_ok_and(|days| days < MAX_PROBE_DAYS) {
        return Err(InvalidRange.into());
    }
    let activity_dates = if activities_in_range {
        if !source_timezone_available(timezone) {
            return Err("Invalid probe time zone".into());
        }
        Some(DateRange::new(start, end)?)
    } else {
        None
    };

    let data = PathBuf::from(data.unwrap_or_else(|| "data".into()));
    let tokens = PathBuf::from(tokens.unwrap_or_else(|| "tokens/garmin".into()));
    let backups = PathBuf::from(backups.unwrap_or_else(|| "backups".into()));
    let locks = configuration::lock_directory(&data, &tokens, locks.map(PathBuf::from))?;
    validate_backup_layout(&data, &tokens, &locks, &backups)?;
    let _files = StorageFilesGuard::acquire(&locks, StorageAccess::Active)?;
    let mut setup = match url.filter(|value| !value.is_empty()) {
        Some(url) => {
            let database =
                Database::connect_lazy(&url).map_err(|_| "Storage configuration is invalid")?;
            Some(
                database
                    .acquire_local_garmin_setup(LocalGarminSetupMode::Probe)
                    .await?,
            )
        }
        None => None,
    };
    let archive_path = data.join("raw");
    let retained = Archive::has_retained_entries(&archive_path)?;
    let store =
        GarminTokenFile::open_existing(&tokens)?.ok_or(GarminError::AuthenticationRequired)?;
    let transport = TransportConfig::default();
    let (session, rotated) =
        LegacySession::restore_for_local_verification(&store, transport).await?;
    let fingerprint = session.fingerprint();
    let save = || {
        if rotated {
            session
                .save_for_owner(&store, &fingerprint)
                .map_err(|_| GarminSetupError::TokenPublication)?;
        }
        Ok(())
    };
    if let Some(guard) = setup.as_mut() {
        guard.publish(&fingerprint, retained, save).await?;
    } else {
        if retained {
            verify_retained_owner(&data, &fingerprint)?;
        }
        save()?;
    }

    let mut probe = CoverageProbe {
        archive: Archive::open(&archive_path)?,
        reader: LegacyReader::from_verified_session(store, session, transport)?,
        report: Report::open(&data, start, end, fingerprint)?,
        setup,
        activity_dates,
        timezone,
        cooldown: false,
        connection_failures: 0,
    };
    probe.collect(days).await?;
    println!(
        "Coverage report saved locally to {}. Review before sharing.",
        data.join("coverage-report.json").display()
    );
    Ok(())
}

fn verify_retained_owner(data: &Path, fingerprint: &str) -> Result<(), Box<dyn Error>> {
    #[derive(Deserialize)]
    struct Owner {
        account_fingerprint: String,
    }
    let previous = CoverageReportFile::read_existing(data)
        .ok()
        .and_then(|file| serde_json::from_reader::<_, Owner>(io::BufReader::new(file)).ok())
        .filter(|owner| {
            owner.account_fingerprint.len() == 64
                && owner
                    .account_fingerprint
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
        .ok_or("Retained probe ownership requires local enrollment")?;
    if !bool::from(
        previous
            .account_fingerprint
            .as_bytes()
            .ct_eq(fingerprint.as_bytes()),
    ) {
        return Err("Retained probe belongs to another Garmin account".into());
    }
    Ok(())
}

struct Report {
    target: CoverageReportFile,
    requests: File,
    has_requests: bool,
    start: Date,
    end: Date,
    fingerprint: String,
    activity_list_archive: Option<String>,
}

impl Report {
    fn open(
        directory: &Path,
        start: Date,
        end: Date,
        fingerprint: String,
    ) -> Result<Self, Box<dyn Error>> {
        let target = CoverageReportFile::open(directory)?;
        let requests = tempfile::tempfile_in(directory)?;
        Ok(Self {
            target,
            requests,
            has_requests: false,
            start,
            end,
            fingerprint,
            activity_list_archive: None,
        })
    }

    fn append(&mut self, row: &Value) -> Result<(), Box<dyn Error>> {
        if self.has_requests {
            self.requests.write_all(b",\n")?;
        }
        serde_json::to_writer(&mut self.requests, row)?;
        self.has_requests = true;
        self.checkpoint(false)
    }

    fn checkpoint(&mut self, complete: bool) -> Result<(), Box<dyn Error>> {
        self.requests.rewind()?;
        self.target.checkpoint(|target| {
            writeln!(
                target,
                "{{\"start\":\"{}\",\"end\":\"{}\",\"requests\":[",
                self.start, self.end
            )?;
            io::copy(&mut self.requests, target)?;
            write!(
                target,
                "\n],\"complete\":{complete},\"account_fingerprint\":\"{}\"",
                self.fingerprint,
            )?;
            if let Some(key) = &self.activity_list_archive {
                write!(target, ",\"activity_list_archive\":\"{key}\"")?;
            }
            target.write_all(b"}\n")
        })?;
        Ok(())
    }
}

struct CoverageProbe<'a> {
    archive: Archive,
    reader: LegacyReader,
    report: Report,
    setup: Option<LocalGarminSetup>,
    activity_dates: Option<DateRange>,
    timezone: &'a str,
    cooldown: bool,
    connection_failures: u8,
}

impl CoverageProbe<'_> {
    async fn collect(&mut self, days: i64) -> Result<(), Box<dyn Error>> {
        self.check_storage().await?;
        self.report.checkpoint(false)?;
        for offset in 0..=days {
            let day = self.report.start + Duration::days(offset);
            let key = day.to_string();
            for entry in LEGACY_CAPABILITIES {
                if let LegacyResource::Day(endpoint) = entry.resource {
                    self.capture_json(entry.name, &key, LegacyRead::Daily(endpoint, day))
                        .await?;
                }
            }
        }
        let list = if let Some(dates) = self.activity_dates {
            LegacyRead::ActivitiesInRange {
                limit: ACTIVITY_LIST_LIMIT,
                dates,
            }
        } else {
            self.capture_json("devices", "global", LegacyRead::Devices)
                .await?;
            LegacyRead::Activities {
                start: 0,
                limit: ACTIVITY_LIST_LIMIT,
            }
        };
        if let Some((list, archive_key)) = self.capture_json("activities", "recent", list).await?
            && !list.is_null()
        {
            self.report.activity_list_archive = Some(archive_key);
            let count = if self.activity_dates.is_some() {
                usize::from(ACTIVITY_LIST_LIMIT.get())
            } else {
                5
            };
            let activities = activity_ids(&list, count)?;
            drop(list);
            for activity in activities {
                let key = activity.to_string();
                for entry in LEGACY_CAPABILITIES {
                    if let LegacyResource::Activity(endpoint) = entry.resource {
                        self.capture_json(
                            entry.name,
                            &key,
                            LegacyRead::Activity(endpoint, activity),
                        )
                        .await?;
                    }
                }
                self.capture_fit(activity, &key).await?;
            }
        }
        self.check_storage().await?;
        self.report.checkpoint(true)
    }

    async fn check_storage(&mut self) -> Result<(), GarminSetupError> {
        if let Some(setup) = &mut self.setup {
            setup.check_connection().await?;
        }
        Ok(())
    }

    async fn capture_json(
        &mut self,
        endpoint: &str,
        key: &str,
        resource: LegacyRead,
    ) -> Result<Option<(Value, String)>, Box<dyn Error>> {
        self.check_storage().await?;
        let at = OffsetTimestamp(OffsetDateTime::now_utc()).to_string();
        let result = self.reader.get_json(resource, JSON_LIMITS.max_bytes).await;
        self.check_storage().await?;
        let bytes = match result {
            Ok(bytes) => bytes,
            Err(error) => {
                self.failure(endpoint, key, &at, error)?;
                return Ok(None);
            }
        };
        self.connection_failures = 0;
        let bytes = bytes.as_deref().unwrap_or(b"null");
        let mut value: Value = serde_json::from_slice(bytes)?;
        let filtered;
        let bytes = if let LegacyRead::ActivitiesInRange { dates, limit } = resource
            && !value.is_null()
        {
            restrict_activities(&mut value, dates, self.timezone, usize::from(limit.get()))?;
            filtered = serde_json::to_vec(&value)?;
            &filtered
        } else {
            bytes
        };
        let archive_key = self.archive.put_json(bytes, JSON_LIMITS)?.to_string();
        let empty = value.is_null()
            || value.as_object().is_some_and(serde_json::Map::is_empty)
            || value.as_array().is_some_and(std::vec::Vec::is_empty);
        self.report.append(&json!({
            "endpoint": endpoint,
            "key": key,
            "fetched_at": at,
            "status": if empty { "empty" } else { "available" },
            "archive_key": archive_key,
            "shape": shape(&value, 0),
        }))?;
        Ok(Some((value, archive_key)))
    }

    async fn capture_fit(&mut self, activity: NonZeroU64, key: &str) -> Result<(), Box<dyn Error>> {
        self.check_storage().await?;
        let at = OffsetTimestamp(OffsetDateTime::now_utc()).to_string();
        let (file, limit) = self.archive.open_download_staging(FIT_LIMITS)?;
        let mut file = tokio::fs::File::from_std(file);
        let result = self
            .reader
            .download_activity(activity, limit, &mut file)
            .await;
        self.check_storage().await?;
        let size = match result {
            Ok(size) => size,
            Err(GarminError::EmptyDownload) => 0,
            Err(GarminError::DestinationWrite) => return Err(GarminError::DestinationWrite.into()),
            Err(error) => return self.failure("activity_fit", key, &at, error),
        };
        self.connection_failures = 0;
        let mut file = file.into_std().await;
        file.rewind()?;
        let archive_key = self
            .archive
            .put_reader(file, ArchiveKind::Zip, FIT_LIMITS)?;
        self.report.append(&json!({
            "endpoint": "activity_fit",
            "key": key,
            "fetched_at": at,
            "status": if size == 0 { "empty" } else { "available" },
            "archive_key": archive_key.to_string(),
            "shape": { "type": "bytes", "size": size },
        }))
    }

    fn failure(
        &mut self,
        endpoint: &str,
        key: &str,
        at: &str,
        error: GarminError,
    ) -> Result<(), Box<dyn Error>> {
        let (kind, stop) = match error {
            GarminError::AuthenticationRequired | GarminError::Unauthorized => {
                ("AuthenticationRequired", true)
            }
            GarminError::OwnerMismatch => ("AccountMismatch", true),
            GarminError::TokenStorage => ("TokenStorage", true),
            GarminError::RateLimited { .. } if self.cooldown => ("CircuitOpen", true),
            GarminError::RateLimited { .. } => {
                self.cooldown = true;
                ("GarminConnectTooManyRequestsError", false)
            }
            GarminError::Network
            | GarminError::Timeout
            | GarminError::InterruptedTransfer
            | GarminError::Upstream(_) => {
                self.connection_failures += 1;
                if self.connection_failures >= 5 {
                    ("CircuitOpen", true)
                } else {
                    ("GarminConnectConnectionError", false)
                }
            }
            GarminError::InvalidJson => ("JSONDecodeError", false),
            GarminError::ResponseTooLarge => ("ResponseTooLarge", false),
            _ => ("GarminConnectConnectionError", false),
        };
        self.report.append(&json!({
            "endpoint": endpoint, "key": key, "fetched_at": at, "status": "error", "error_type": kind,
        }))?;
        if stop { Err(error.into()) } else { Ok(()) }
    }
}

fn restrict_activities(
    value: &mut Value,
    dates: DateRange,
    timezone: &str,
    page_limit: usize,
) -> Result<(), Box<dyn Error>> {
    let activities = value
        .as_array_mut()
        .ok_or("Unexpected activity list shape")?;
    if activities.len() >= page_limit {
        return Err("Garmin activity list may be incomplete. Probe fewer days at a time.".into());
    }
    let mut selected = Vec::new();
    for activity in std::mem::take(activities) {
        let day = gmt_instant(&activity["startTimeGMT"])
            .and_then(|at| source_calendar_date(at, timezone))
            .ok_or("Garmin activity start date could not be verified")?;
        if dates.contains(day) {
            selected.push(activity);
        }
    }
    *activities = selected;
    Ok(())
}

fn activity_ids(value: &Value, count: usize) -> Result<Vec<NonZeroU64>, Box<dyn Error>> {
    value
        .as_array()
        .ok_or_else(|| "Unexpected activity list shape".into())
        .and_then(|activities| {
            activities
                .iter()
                .take(count)
                .map(|item| {
                    let id = &item["activityId"];
                    id.as_u64()
                        .and_then(NonZeroU64::new)
                        .or_else(|| id.as_str()?.parse().ok())
                        .ok_or_else(|| "Invalid activity identity".into())
                })
                .collect()
        })
}

fn shape(value: &Value, depth: usize) -> Value {
    if depth < 8 {
        match value {
            Value::Object(values) => {
                return Value::Object(
                    values
                        .iter()
                        .map(|(key, value)| (key.clone(), shape(value, depth + 1)))
                        .collect(),
                );
            }
            Value::Array(values) => {
                let mut variants = Vec::new();
                for value in values.iter().take(100) {
                    let candidate = shape(value, depth + 1);
                    if !variants.contains(&candidate) {
                        variants.push(candidate);
                    }
                }
                return json!({"type": "array", "count": values.len(), "items": variants});
            }
            _ => {}
        }
    }
    json!(match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.to_string().contains(['.', 'e', 'E']) => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scoped_activities_use_the_configured_calendar() -> Result<(), Box<dyn Error>> {
        let dates = DateRange::new(parse_date("2026-11-20")?, parse_date("2026-11-22")?)?;
        let mut activities = json!([
            {"activityId": 1, "startTimeGMT": "2026-11-19 22:59:59"},
            {"activityId": 2, "startTimeGMT": "2026-11-19 23:00:00"},
            {"activityId": 3, "startTimeGMT": "2026-11-22 22:59:59"},
            {"activityId": 4, "startTimeGMT": "2026-11-22 23:00:00"}
        ]);
        restrict_activities(&mut activities, dates, "Europe/Bratislava", 100)?;
        let ids: Vec<_> = activity_ids(&activities, 100)?
            .into_iter()
            .map(NonZeroU64::get)
            .collect();
        assert_eq!(ids, [2, 3]);
        Ok(())
    }

    #[test]
    fn scoped_activities_reject_a_full_page_and_an_unknown_date() -> Result<(), Box<dyn Error>> {
        let dates = DateRange::new(parse_date("2026-11-20")?, parse_date("2026-11-22")?)?;
        let mut full = json!([{"activityId": 1, "startTimeGMT": "2026-11-20 15:30:00"}]);
        assert!(restrict_activities(&mut full, dates, "Europe/Bratislava", 1).is_err());
        let mut undated = json!([{"activityId": 1}]);
        assert!(restrict_activities(&mut undated, dates, "Europe/Bratislava", 100).is_err());
        Ok(())
    }
}
