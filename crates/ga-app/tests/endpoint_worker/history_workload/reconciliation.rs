use super::*;

#[tokio::test]
#[ignore = "requires a disposable database and a shared synthetic reconciliation fixture"]
async fn reconciles_two_shared_periodic_windows() -> Result<(), Box<dyn Error>> {
    let days: u16 = env::var("GA_TEST_RECONCILE_DAYS")?.parse()?;
    let (first, second, interval) = match days {
        7 => ("2026-10-27T03:00:00Z", "2026-10-28T03:00:00Z", 1),
        30 => ("2026-10-26T03:00:00Z", "2026-11-02T03:00:00Z", 7),
        _ => return Err("Expected a 7- or 30-day reconciliation window".into()),
    };
    let (fixture, _archive, source) = setup_source().await?;
    let worker = ScheduledGarminWorker {
        runner: fixture.worker(&source),
        instance: "source:garmin:primary",
        history_days: 0,
        backups_enabled: false,
        fit_limits: ExportLimits {
            max_bytes: 65_536,
            max_zip_entries: NonZeroUsize::MIN,
            max_duration: Duration::from_secs(3),
        },
    };
    let expected = usize::from(days) * source.endpoints.len() + 1;
    let mut passes = Vec::new();
    for scheduled_at in [first, second] {
        let now: StoredInstant = scheduled_at.parse()?;
        let started = Instant::now();
        for _ in 0..2 {
            assert!(matches!(
                worker.plan(now).await?,
                GarminSyncSchedule::Scheduled { .. }
            ));
        }
        // This workload measures the endpoint reconciliation lanes, with the
        // independently scheduled activity scans still queued for another worker.
        sqlx::query(
            "UPDATE jobs SET run_at=clock_timestamp()+interval '1 hour'
             WHERE kind='garmin_activities' AND status='pending'",
        )
        .execute(&fixture.inspect)
        .await?;
        let completed = Box::pin(drain(&fixture, &worker, "")).await?;
        assert_eq!(completed, expected);
        passes.push(json!({ "jobs": completed, "seconds": started.elapsed().as_secs_f64() }));
    }
    let sources = verify_archive(&fixture).await?;
    assert_eq!(
        sources,
        (usize::from(days) + interval) * source.endpoints.len() + 1
    );
    let calls = source
        .calls
        .lock()
        .map_err(|_| "Could not count reconciliation requests")?
        .clone();
    assert_eq!(calls.get("devices"), Some(&2));
    assert_eq!(calls.values().sum::<usize>(), expected * 2);
    println!(
        "WORKLOAD_RESULT={}",
        json!({
            "window_days": days,
            "passes": passes,
            "fetch_counts": calls,
            "unique_source_payloads": sources,
            "same_bytes_reused": expected * 2 - sources,
            "archive_objects_verified": sources,
        })
    );
    Ok(())
}
