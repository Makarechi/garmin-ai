use tokio::io::AsyncReadExt;

use super::*;

struct DailySource {
    history: HistorySource,
    activity: NonZeroU64,
    activity_key: String,
    activity_endpoints: Vec<String>,
}

impl EndpointSource for DailySource {
    fn fingerprint(&self) -> &str {
        self.history.fingerprint()
    }

    async fn read(
        &self,
        resource: LegacyRead,
        maximum_bytes: u64,
    ) -> Result<Option<Vec<u8>>, GarminError> {
        let (key, endpoint) = match resource {
            LegacyRead::Activities { start: 0, limit } if limit.get() == 100 => {
                ("page:0", "activities")
            }
            LegacyRead::Activity(_, identity) if identity == self.activity => (
                self.activity_key.as_str(),
                self.activity_endpoints
                    .iter()
                    .find(|name| {
                        LegacyRead::queued_endpoint(name, &self.activity_key) == Ok(resource)
                    })
                    .ok_or(GarminError::InvalidRequest)?
                    .as_str(),
            ),
            _ => return self.history.read(resource, maximum_bytes).await,
        };
        self.history
            .read_file(key, endpoint, maximum_bytes)
            .await
            .map(Some)
    }
}

impl ActivityExportSource for DailySource {
    async fn download(
        &self,
        activity: NonZeroU64,
        maximum_bytes: u64,
        destination: &mut File,
    ) -> Result<u64, GarminError> {
        if activity != self.activity {
            return Err(GarminError::InvalidRequest);
        }
        let source = File::open(self.history.root.join("activity.fit.zip"))
            .await
            .map_err(|_| GarminError::InvalidRequest)?;
        let bytes = tokio::io::copy(
            &mut source.take(maximum_bytes.saturating_add(1)),
            destination,
        )
        .await
        .map_err(|_| GarminError::InvalidRequest)?;
        if bytes > maximum_bytes {
            return Err(GarminError::ResponseTooLarge);
        }
        self.history.record_call("activity_fit")?;
        Ok(bytes)
    }
}

#[tokio::test]
#[ignore = "requires a disposable database and an evening Garmin sync fixture"]
async fn processes_shared_evening_workload() -> Result<(), Box<dyn Error>> {
    let (fixture, _archive, history) = setup_source().await?;
    let activity_key = std::fs::read_to_string(history.root.join("activity-id.txt"))?;
    let activity_endpoints = std::fs::read_to_string(history.root.join("activity-endpoints.txt"))?;
    let source = DailySource {
        activity: activity_key.parse()?,
        activity_key,
        activity_endpoints: activity_endpoints.lines().map(str::to_owned).collect(),
        history,
    };
    assert_eq!(source.activity_endpoints.len(), 6);
    let expected = source.history.endpoints.len() + source.activity_endpoints.len() + 3;
    assert_eq!(expected, 29);
    let worker = ScheduledGarminWorker {
        runner: fixture.worker(&source),
        instance: "source:garmin:primary",
        history_days: 0,
        backups_enabled: false,
        fit_limits: ExportLimits {
            max_bytes: 65_536,
            max_zip_entries: NonZeroUsize::new(4).ok_or("invalid ZIP limit")?,
            max_duration: Duration::from_secs(15),
        },
    };
    let started = Instant::now();
    assert!(matches!(
        worker.plan("2026-10-24T18:00:00Z".parse()?).await?,
        GarminSyncSchedule::Scheduled {
            activity_pages: 1,
            periodic: 21,
            ..
        }
    ));
    let mut completed = 0;
    let mut last_success = Instant::now();
    while completed < expected {
        let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&fixture.inspect)
            .await?;
        match worker.process_one(now.try_into()?).await? {
            GarminQueueStep::Endpoint(EndpointWork::Publication(
                EndpointPublication::Published(
                    DocumentStatus::Archived | DocumentStatus::Normalized,
                ),
            ))
            | GarminQueueStep::Activities(ActivityPageWork::Publication(
                ActivityPageResult::Published(DocumentStatus::Normalized),
            ))
            | GarminQueueStep::Fit(ActivityFitWork::Publication(FitIngestOutcome::Normalized {
                ..
            })) => {
                completed += 1;
                last_success = Instant::now();
            }
            GarminQueueStep::Idle if last_success.elapsed() < Duration::from_secs(10) => {
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
            step => return Err(format!("Unexpected evening Garmin queue outcome: {step:?}").into()),
        }
    }
    let duration = started.elapsed().as_secs_f64();
    let counts: BTreeMap<String, i64> =
        sqlx::query_as("SELECT status,count(*) FROM jobs WHERE kind = ANY($1) GROUP BY status")
            .bind(["garmin_activities", "garmin_endpoint", "garmin_fit"])
            .fetch_all(&fixture.inspect)
            .await?
            .into_iter()
            .collect();
    assert_eq!(
        counts,
        BTreeMap::from([("done".to_owned(), 29), ("pending".to_owned(), 6)])
    );
    let verified = verify_archive(&fixture).await?;
    assert_eq!(verified, expected);
    let calls = source
        .history
        .calls
        .lock()
        .map_err(|_| "Cannot count evening calls")?;
    assert_eq!(calls.len(), expected);
    assert!(calls.values().all(|count| *count == 1));
    println!(
        "WORKLOAD_RESULT={}",
        json!({
            "days": 1,
            "duration_seconds": duration,
            "fetch_counts": *calls,
            "jobs_done": counts["done"],
            "delayed_followups": counts["pending"],
            "archive_objects_verified": verified,
        })
    );
    Ok(())
}
