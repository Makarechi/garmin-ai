use std::{
    env,
    error::Error,
    io::{BufRead, BufReader},
    path::Path,
    process::{Command, Stdio},
    str::FromStr,
    sync::Arc,
    time::Duration,
};

use ga_archive::{Archive, PutLimits};
use ga_db::{Database, raw_capture::LegacyCapture};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use tokio::io::AsyncBufReadExt;
use uuid::Uuid;

const ACCOUNT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

#[tokio::test]
#[ignore = "starts the real native worker with a disposable TimescaleDB and synthetic archive"]
async fn plans_and_restores_the_archive_from_the_cli() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable test database")?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(target, actual);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let root = tempfile::tempdir()?;
    let data = root.path().canonicalize()?;
    let archive = Arc::new(Archive::open(&data.join("raw"))?);

    let initial = command(&url, &data).arg("--once").output()?;
    assert!(initial.status.success());
    assert!(String::from_utf8(initial.stdout)?.contains("No archive replay is due"));
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES
        ('account:garmin',jsonb_build_object('fingerprint',$1::text)),
        ('integration:garmin',jsonb_build_object('status','reauth_required'))",
    )
    .bind(ACCOUNT)
    .execute(&inspect)
    .await?;
    sqlx::query(
        "UPDATE module_configs SET collection_enabled=true WHERE pack_key IN ('training','wellbeing')",
    )
    .execute(&inspect)
    .await?;
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&inspect)
        .await?;
    let capture = database
        .capture_legacy_json(
            LegacyCapture {
                authenticated_fingerprint: ACCOUNT,
                endpoint: "daily",
                source_key: "2026-10-21",
                fetched_at: now.try_into()?,
                body: br#"{"totalSteps":40}"#.to_vec(),
            },
            archive.clone(),
            PutLimits {
                max_bytes: 10_000,
                disk_reserve_bytes: 0,
            },
        )
        .await?;

    sqlx::query("INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts) VALUES
        (gen_random_uuid(),'raw_replay','{\"target_version\":\"fault\"}','synthetic-invalid-pending','pending',$1,0),
        (gen_random_uuid(),'raw_replay','{\"target_version\":\"fault\"}','synthetic-invalid-failed','failed',$1,0)")
        .bind(now).execute(&inspect).await?;
    let mut python_runtime = inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104620)")
        .execute(&mut *python_runtime)
        .await?;
    let occupied = command(&url, &data).arg("--once").output()?;
    assert!(!occupied.status.success());
    assert_eq!(
        String::from_utf8(occupied.stderr)?,
        "Another Garmin AI worker is already running\n"
    );
    let queued: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM jobs WHERE starts_with(dedup_key,'raw-replay:'))",
    )
    .fetch_one(&inspect)
    .await?;
    assert!(!queued);
    python_runtime.rollback().await?;

    let output = command(&url, &data).arg("--once").output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8(output.stdout)?.contains("Archive replay completed"));
    assert_eq!(
        result(&inspect, capture.id).await?,
        (1, "done".into(), 9, Some(40))
    );
    assert_eq!(recorded_timezone(&inspect, capture.id).await?, "UTC");

    sqlx::query("UPDATE people SET timezone='America/Los_Angeles'")
        .execute(&inspect)
        .await?;
    sqlx::query("INSERT INTO app_state(key,value) VALUES('preferences:onboarding','{}')")
        .execute(&inspect)
        .await?;
    let preferred = database
        .capture_legacy_json(
            LegacyCapture {
                authenticated_fingerprint: ACCOUNT,
                endpoint: "daily",
                source_key: "2026-10-22",
                fetched_at: now.try_into()?,
                body: br#"{"totalSteps":50}"#.to_vec(),
            },
            archive.clone(),
            PutLimits {
                max_bytes: 10_000,
                disk_reserve_bytes: 0,
            },
        )
        .await?;
    let output = command(&url, &data).arg("--once").output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8(output.stdout)?.contains("Archive replay completed"));
    assert_eq!(
        recorded_timezone(&inspect, preferred.id).await?,
        "America/Los_Angeles"
    );
    let again = command(&url, &data).arg("--once").output()?;
    assert!(again.status.success());
    assert!(String::from_utf8(again.stdout)?.contains("No archive replay is due"));

    sqlx::query("UPDATE jobs SET status='running',lease_token=gen_random_uuid(),
        lease_until=clock_timestamp()-interval '1 second',
        payload=payload || jsonb_build_object('repair_version','invalid','repair_parser_version','invalid')
        WHERE dedup_key=concat('raw-replay:',$1::uuid,':9')")
        .bind(capture.id).execute(&inspect).await?;
    sqlx::query("UPDATE source_payloads SET parser_version=8 WHERE id=$1")
        .bind(capture.id)
        .execute(&inspect)
        .await?;
    sqlx::query("UPDATE health_days SET steps=7 WHERE day='2026-10-21'")
        .execute(&inspect)
        .await?;
    let recovered = command(&url, &data).arg("--once").output()?;
    assert!(
        recovered.status.success(),
        "{}",
        String::from_utf8_lossy(&recovered.stderr)
    );
    assert!(String::from_utf8(recovered.stdout)?.contains("Archive replay completed"));
    assert_eq!(
        result(&inspect, capture.id).await?,
        (2, "done".into(), 9, Some(40))
    );
    assert_eq!(recorded_timezone(&inspect, capture.id).await?, "UTC");
    // A completed record with malformed repair metadata cannot stop another planning pass.
    let final_pass = command(&url, &data).arg("--once").output()?;
    assert!(final_pass.status.success());
    assert!(String::from_utf8(final_pass.stdout)?.contains("No archive replay is due"));
    let unchanged: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM jobs
        WHERE starts_with(dedup_key,'synthetic-invalid-') AND attempts=0",
    )
    .fetch_one(&inspect)
    .await?;
    assert_eq!(unchanged, 2);

    let delayed = database
        .capture_legacy_json(
            LegacyCapture {
                authenticated_fingerprint: ACCOUNT,
                endpoint: "daily",
                source_key: "2026-10-23",
                fetched_at: now.try_into()?,
                body: br#"{"totalSteps":60}"#.to_vec(),
            },
            archive,
            PutLimits {
                max_bytes: 10_000,
                disk_reserve_bytes: 0,
            },
        )
        .await?;
    let mut blocked = inspect.begin().await?;
    sqlx::query("LOCK TABLE source_payloads IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *blocked)
        .await?;
    let mut worker = tokio::process::Command::from(command(&url, &data));
    let mut running = worker
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()?;
    let stderr = running.stderr.take().ok_or("Worker stderr unavailable")?;
    let mut lines = tokio::io::BufReader::new(stderr).lines();
    tokio::time::timeout(Duration::from_secs(15), async {
        let mut planning_retried = false;
        let mut claim_retried = false;
        let mut last_line = String::new();
        while let Some(line) = lines.next_line().await? {
            planning_retried |= line.starts_with("Archive replay planning will retry:");
            claim_retried |= line.starts_with("Archive replay job claim will retry:");
            if planning_retried && claim_retried {
                return Ok(());
            }
            assert!(
                line != "Replay inventory could not be read",
                "An archive query timeout stopped the worker"
            );
            last_line = line;
        }
        Err(std::io::Error::other(format!(
            "Worker exited without scheduling both archive retries. Last diagnostic: {last_line}"
        )))
    })
    .await??;
    assert!(running.try_wait()?.is_none());
    let occupied = command(&url, &data).arg("--once").output()?;
    assert!(!occupied.status.success());
    assert_eq!(
        occupied.stderr,
        b"Another Garmin AI worker is already running\n",
    );
    blocked.rollback().await?;
    tokio::time::timeout(Duration::from_secs(45), async {
        loop {
            let completed: bool =
                sqlx::query_scalar("SELECT parser_version=9 FROM source_payloads WHERE id=$1")
                    .bind(delayed.id)
                    .fetch_one(&inspect)
                    .await?;
            if completed {
                return Ok::<_, sqlx::Error>(());
            }
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
    })
    .await??;
    assert!(running.try_wait()?.is_none());

    let mut unavailable = inspect.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104622)")
        .execute(&mut *unavailable)
        .await?;
    // Idle workers no longer claim every second. Wake the replay lane while
    // storage is fenced to exercise its existing transient-error retry path.
    // Use a separate committed statement: notifications in `unavailable`
    // itself would not be delivered until that transaction releases the fence.
    sqlx::query("SELECT pg_notify('ga_work_ready','replay')")
        .execute(&inspect)
        .await?;
    tokio::time::timeout(Duration::from_secs(10), async {
        while let Some(line) = lines.next_line().await? {
            if line == "Archive replay job claim will retry: Job storage is unavailable" {
                return Ok(());
            }
            assert!(
                line != "Job storage is unavailable",
                "A temporary storage readiness failure stopped the worker"
            );
        }
        Err(std::io::Error::other(
            "Worker exited without retrying temporary storage unavailability",
        ))
    })
    .await
    .map_err(|_| "Worker did not retry a signaled replay claim while storage was fenced")??;
    assert!(running.try_wait()?.is_none());
    sqlx::query("UPDATE source_payloads SET parser_version=8 WHERE id=$1")
        .bind(delayed.id)
        .execute(&mut *unavailable)
        .await?;
    sqlx::query(
        "UPDATE jobs SET status='pending',run_at=clock_timestamp(),completed_at=NULL
         WHERE dedup_key=concat('raw-replay:',$1::uuid,':9')",
    )
    .bind(delayed.id)
    .execute(&mut *unavailable)
    .await?;
    unavailable.commit().await?;
    tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            let (attempts, status, parser, _) = result(&inspect, delayed.id).await?;
            if (attempts, status.as_str(), parser) == (2, "done", 9) {
                return Ok::<_, sqlx::Error>(());
            }
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
    })
    .await??;
    assert!(running.try_wait()?.is_none());
    running.kill().await?;

    #[cfg(unix)]
    {
        let mut running = command(&url, &data).stdout(Stdio::piped()).spawn()?;
        let mut line = String::new();
        let stdout = running.stdout.take().ok_or("Worker stdout unavailable")?;
        BufReader::new(stdout).read_line(&mut line)?;
        assert_eq!(line, "Archive replay worker ready\n");
        let signal = Command::new("sh")
            .args(["-c", "kill -TERM \"$1\"", "replay-test"])
            .arg(running.id().to_string())
            .status()?;
        assert!(signal.success());
        assert!(running.wait()?.success());
        assert!(
            command(&url, &data)
                .arg("--once")
                .output()?
                .status
                .success()
        );
    }

    sqlx::query("INSERT INTO app_state(key,value) VALUES('maintenance:erased','{}')")
        .execute(&inspect)
        .await?;
    let erased = command(&url, &data).arg("--once").output()?;
    assert!(!erased.status.success());
    assert_eq!(
        String::from_utf8(erased.stderr)?,
        "Garmin AI worker storage is unavailable\n"
    );
    assert_eq!(
        result(&inspect, capture.id).await?,
        (2, "done".into(), 9, Some(40))
    );
    Ok(())
}

fn command(url: &str, path: &Path) -> Command {
    let binary = env::var_os("GA_TEST_REPLAY_BINARY")
        .unwrap_or_else(|| env!("CARGO_BIN_EXE_garmin-ai-rust").into());
    let mut command = Command::new(binary);
    command
        .env_clear()
        .current_dir(path)
        .env("GA_DATABASE_URL", url)
        .env("GA_DATA_DIR", path)
        .env("GA_TIMEZONE", "UTC")
        .arg("replay");
    command
}

async fn result(pool: &PgPool, raw: Uuid) -> Result<(i32, String, i32, Option<i32>), sqlx::Error> {
    sqlx::query_as("SELECT job.attempts,job.status,raw.parser_version,
           (SELECT steps FROM health_days WHERE day='2026-10-21')
         FROM source_payloads AS raw JOIN jobs AS job ON job.dedup_key=concat('raw-replay:',raw.id,':9')
         WHERE raw.id=$1")
        .bind(raw).fetch_one(pool).await
}

async fn recorded_timezone(pool: &PgPool, raw: Uuid) -> Result<String, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT value->>'timezone' FROM app_state WHERE key=concat('ingest-meta:',$1::uuid)",
    )
    .bind(raw)
    .fetch_one(pool)
    .await
}
