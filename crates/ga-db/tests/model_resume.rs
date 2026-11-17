use std::{collections::BTreeMap, env, error::Error, fs, str::FromStr};

use ga_db::{
    Database,
    model_gate::{ModelGateError, ModelResume},
};
use ga_domain::model_gate::ModelGateIdentity;
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[derive(Deserialize)]
struct Case {
    name: String,
    instance: String,
    key: String,
    models: Vec<Option<String>>,
    invalid: Vec<Option<String>>,
    fingerprint: String,
    saved: Value,
    cleared: Value,
}

fn identity(instance: &str, key: &str) -> Result<ModelGateIdentity, Box<dyn Error>> {
    if instance.starts_with("model:gemini:") {
        Ok(ModelGateIdentity::gemini(
            instance,
            "fixture-model",
            true,
            &["fallback"],
            &["fallback"],
            key,
        )?)
    } else {
        Ok(ModelGateIdentity::plugin(
            instance,
            "openai",
            &BTreeMap::from([("api_key", Some(key))]),
        )?)
    }
}

#[tokio::test]
#[ignore = "requires pinned cross-language fallback records and disposable TimescaleDB"]
async fn persists_fallback_resume_and_fences_stale_attempts() -> Result<(), Box<dyn Error>> {
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
    let inspector = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspector)
        .await?;
    assert_eq!(actual, target);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let cases: Vec<Case> = serde_json::from_slice(&fs::read(env::var("GA_TEST_MODEL_RESUME")?)?)?;
    for case in cases {
        let model_gate = identity(&case.instance, &case.key)?;
        let models: Vec<_> = case.models.iter().map(Option::as_deref).collect();
        let excluded: Vec<_> = case.invalid.iter().map(Option::as_deref).collect();
        let resume = ModelResume {
            job_id: Uuid::new_v4(),
            lease_token: Uuid::new_v4(),
            request_fingerprint: &case.fingerprint,
            models: &models,
        };
        sqlx::query("INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts,lease_token,lease_until)
             VALUES($1,'agent_insights',$2::jsonb,$3,'running',now(),1,$4,now()+interval '5 minutes')")
            .bind(resume.job_id).bind(case.cleared.to_string()).bind(format!("synthetic-resume:{}", case.name)).bind(resume.lease_token)
            .execute(&inspector).await?;
        let mut gate = db.lock_model_gate(&model_gate).await?;
        assert!(gate.read_resume(resume).await?.is_empty());
        gate.record_resume(resume, &excluded).await?;
        assert_eq!(
            stored(&inspector, resume.job_id).await?,
            case.saved,
            "{} saved",
            case.name
        );
        drop(gate);
        let mut gate = db.lock_model_gate(&model_gate).await?;
        assert_eq!(
            json!(gate.read_resume(resume).await?),
            case.saved["provider_resume"]["invalid_models"]
        );
        let wrong_prompt = "b".repeat(64);
        let other_prompt = ModelResume {
            request_fingerprint: &wrong_prompt,
            ..resume
        };
        assert!(gate.read_resume(other_prompt).await?.is_empty());
        assert!(!gate.clear_resume(other_prompt).await?);
        let reordered: Vec<_> = models.iter().copied().rev().collect();
        if reordered != models {
            assert!(
                gate.read_resume(ModelResume {
                    models: &reordered,
                    ..resume
                })
                .await?
                .is_empty()
            );
        }
        drop(gate);
        let rotated = identity(&case.instance, "synthetic-other-credential")?;
        let mut other_gate = db.lock_model_gate(&rotated).await?;
        assert!(other_gate.read_resume(resume).await?.is_empty());
        assert!(!other_gate.clear_resume(resume).await?);
        drop(other_gate);

        let mut gate = db.lock_model_gate(&model_gate).await?;
        let newer = Uuid::new_v4();
        sqlx::query("UPDATE jobs SET lease_token=$2 WHERE id=$1")
            .bind(resume.job_id)
            .bind(newer)
            .execute(&inspector)
            .await?;
        assert!(matches!(
            gate.read_resume(resume).await,
            Err(ModelGateError::LeaseLost)
        ));
        assert!(matches!(
            gate.clear_resume(resume).await,
            Err(ModelGateError::LeaseLost)
        ));
        assert!(matches!(
            gate.record_resume(resume, &[]).await,
            Err(ModelGateError::LeaseLost)
        ));
        assert_eq!(stored(&inspector, resume.job_id).await?, case.saved);
        let resume = ModelResume {
            lease_token: newer,
            ..resume
        };
        assert!(gate.clear_resume(resume).await?);
        assert_eq!(stored(&inspector, resume.job_id).await?, case.cleared);
        assert!(!gate.clear_resume(resume).await?);
        if case.name != "primary" {
            continue;
        }

        for malformed in [
            json!({"identity_version":1}),
            json!({"identity_version":2, "configuration":""}),
            json!({"identity_version":2, "padding":"x".repeat(40_000)}),
        ] {
            sqlx::query("UPDATE jobs SET payload=payload || jsonb_build_object('provider_resume',$2::jsonb) WHERE id=$1")
              .bind(resume.job_id).bind(malformed.to_string()).execute(&inspector).await?;
            assert!(matches!(
                gate.read_resume(resume).await,
                Err(ModelGateError::UnsafeResume)
            ));
            assert!(matches!(
                gate.record_resume(resume, &excluded).await,
                Err(ModelGateError::UnsafeResume)
            ));
            assert!(matches!(
                gate.clear_resume(resume).await,
                Err(ModelGateError::UnsafeResume)
            ));
            assert_eq!(
                stored(&inspector, resume.job_id).await?["provider_resume"],
                malformed
            );
        }
        sqlx::query("UPDATE jobs SET payload=payload-'provider_resume' WHERE id=$1")
            .bind(resume.job_id)
            .execute(&inspector)
            .await?;
        sqlx::query("ALTER TABLE jobs ADD CONSTRAINT synthetic_resume_failure CHECK (NOT payload ? 'provider_resume') NOT VALID")
            .execute(&inspector).await?;
        assert!(matches!(
            gate.record_resume(resume, &excluded).await,
            Err(ModelGateError::Database)
        ));
        assert_eq!(stored(&inspector, resume.job_id).await?, case.cleared);
        sqlx::query("ALTER TABLE jobs DROP CONSTRAINT synthetic_resume_failure")
            .execute(&inspector)
            .await?;
    }
    Ok(())
}

async fn stored(pool: &PgPool, id: Uuid) -> Result<Value, Box<dyn Error>> {
    let payload: String = sqlx::query_scalar("SELECT payload::text FROM jobs WHERE id=$1")
        .bind(id)
        .fetch_one(pool)
        .await?;
    Ok(serde_json::from_str(&payload)?)
}
