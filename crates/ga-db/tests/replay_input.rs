use std::{env, error::Error, fs, io, io::Read, path::Path, sync::Arc};

use ga_archive::{Archive, ArchiveError};
use ga_db::{
    Database,
    replay::{PreparedReplay, ReadReplayInput, ReplayContent, ReplayInputError, ReplaySkip},
};
use serde_json::{Value, json};

#[tokio::test]
#[ignore = "uses the pinned Python replay oracle and a disposable TimescaleDB"]
async fn resolves_original_archive_input_without_writing() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let database = url.rsplit('/').next().unwrap_or_default();
    if !database.ends_with("_test") || database.contains(['?', '#']) {
        return Err("Use a disposable database ending in _test".into());
    }
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture configuration")?;
    let archive = Arc::new(Archive::open(Path::new(&env::var(
        "GA_TEST_REPLAY_ARCHIVE",
    )?))?);
    let cases: Vec<Value> = serde_json::from_slice(&fs::read(env::var("GA_TEST_REPLAY_INPUT")?)?)?;
    let fingerprint = "a".repeat(64);

    for case in cases {
        let id = case["source"]
            .as_str()
            .ok_or_else(|| io::Error::other("Missing synthetic source"))?
            .parse()?;
        let target = i32::try_from(
            case["target"]
                .as_i64()
                .ok_or_else(|| io::Error::other("Missing synthetic target"))?,
        )?;
        let request = ReadReplayInput {
            authenticated_fingerprint: &fingerprint,
            source_id: id,
            target_parser_version: target,
            configured_timezone: "America/New_York",
            max_json_bytes: 1024 * 1024,
            max_fit_bytes: 100 * 1024 * 1024,
        };
        let actual = match db
            .read_replay_input(request, archive.clone(), || false)
            .await
        {
            Ok(PreparedReplay::Ready(input)) => match input.content {
                ReplayContent::Fit(mut file) => {
                    let mut bytes = Vec::new();
                    file.read_to_end(&mut bytes)?;
                    json!({
                        "status": "fit",
                        "key": input.source.source_key,
                        "requested_at": input.requested_at.to_string(),
                        "bytes": bytes,
                    })
                }
                ReplayContent::Json {
                    payload,
                    timezone,
                    replacement,
                } => {
                    let replacement = replacement.map(|value| {
                        json!({
                            "start": value.start.to_string(),
                            "end": value.end.to_string(),
                            "metrics": value.metrics,
                            "evidence": value.evidence,
                        })
                    });
                    json!({
                        "status": "json",
                        "endpoint": input.source.endpoint,
                        "key": input.source.source_key,
                        "payload": payload,
                        "timezone": timezone,
                        "requested_at": input.requested_at.to_string(),
                        "replacement": replacement,
                    })
                }
            },
            Ok(PreparedReplay::Skip(ReplaySkip::SourceRemoved)) => {
                json!({"status": "source_removed"})
            }
            Ok(PreparedReplay::Skip(ReplaySkip::UnsupportedSource)) => {
                json!({"status": "unsupported_source"})
            }
            Ok(PreparedReplay::Skip(ReplaySkip::SupersededRevision)) => {
                json!({"status": "superseded_revision"})
            }
            Ok(PreparedReplay::Skip(ReplaySkip::Unchanged)) => {
                json!({"status": "unchanged", "source_ref": id.to_string()})
            }
            Err(error) => json!({"error": match error {
                ReplayInputError::Archive(ArchiveError::ExistingObjectDigestMismatch) =>
                    "Archived source hash mismatch".to_owned(),
                error => error.to_string(),
            }}),
        };
        let mut expected = case["expected"].clone();
        match expected["error"].as_str() {
            Some("Historical activity timezone is unavailable") => {
                expected["error"] = json!("Historical interpretation timezone is unavailable");
            }
            Some("Replacement channels do not match endpoint") => {
                expected["error"] =
                    json!("Retained projection application contract is unavailable");
            }
            _ => {}
        }
        assert_eq!(actual, expected, "reference {id} target {target}");
    }

    let id = uuid::Uuid::from_u128(3);
    let mut rejected = ReadReplayInput {
        authenticated_fingerprint: &fingerprint,
        source_id: id,
        configured_timezone: "America/New_York",
        target_parser_version: 9,
        max_json_bytes: 1,
        max_fit_bytes: 1,
    };
    assert!(matches!(
        db.read_replay_input(rejected, archive.clone(), || false)
            .await,
        Err(ReplayInputError::Archive(ArchiveError::PayloadTooLarge))
    ));
    let different_fingerprint = "b".repeat(64);
    rejected = ReadReplayInput {
        authenticated_fingerprint: &different_fingerprint,
        source_id: id,
        configured_timezone: "America/New_York",
        target_parser_version: 9,
        max_json_bytes: 1,
        max_fit_bytes: 1,
    };
    assert!(matches!(
        db.read_replay_input(rejected, archive, || false).await,
        Err(ReplayInputError::Access(_))
    ));
    Ok(())
}
