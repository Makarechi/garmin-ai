use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::{Connection, Postgres, Transaction};
use uuid::Uuid;

use super::{ModelGateError, ModelGateSession, finish, prepare};

const IDENTITY_VERSION: u64 = 2;
const MAX_RESUME_BYTES: i32 = 32 * 1024;

/// A fallback sequence belongs to one job claim and one exact prepared request.
#[derive(Clone, Copy)]
pub struct ModelResume<'a> {
    pub job_id: Uuid,
    pub lease_token: Uuid,
    pub request_fingerprint: &'a str,
    pub models: &'a [Option<&'a str>],
}

impl ModelResume<'_> {
    fn validate(self) -> Result<(), ModelGateError> {
        if valid_digest(self.request_fingerprint)
            && (1..=9).contains(&self.models.len())
            && self.models.iter().flatten().all(|model| valid_model(model))
        {
            Ok(())
        } else {
            Err(ModelGateError::UnsafeResume)
        }
    }
}

#[derive(Deserialize)]
struct SavedResume {
    identity_version: u64,
    configuration: String,
    models: Vec<Option<String>>,
    request_fingerprint: String,
    invalid_models: Vec<Option<String>>,
}

impl SavedResume {
    fn matches_request(&self, resume: ModelResume<'_>, configuration: &str) -> bool {
        self.configuration == configuration
            && self.request_fingerprint == resume.request_fingerprint
    }

    fn valid(&self) -> bool {
        self.identity_version == IDENTITY_VERSION
            && valid_digest(&self.configuration)
            && valid_digest(&self.request_fingerprint)
            && (1..=9).contains(&self.models.len())
            && self.models.iter().flatten().all(|model| valid_model(model))
            && self.invalid_models.len() <= self.models.len()
            && self
                .invalid_models
                .iter()
                .all(|model| self.models.contains(model))
    }
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn valid_model(model: &str) -> bool {
    (1..=200).contains(&model.chars().count())
}

impl ModelGateSession<'_> {
    /// Recover earlier invalid replies for this ordered model list only.
    pub async fn read_resume(
        &mut self,
        resume: ModelResume<'_>,
    ) -> Result<Vec<Option<String>>, ModelGateError> {
        resume.validate()?;
        let mut txn = self.connection.begin_with("BEGIN READ ONLY").await?;
        let result = async {
            prepare(&mut txn).await?;
            load_resume(&mut txn, resume, false).await
        }
        .await;
        let saved = finish(txn, result).await?;
        let Some(saved) = saved else {
            return Ok(Vec::new());
        };
        if !saved.matches_request(resume, self.identity.resume_configuration())
            || !saved
                .models
                .iter()
                .map(Option::as_deref)
                .eq(resume.models.iter().copied())
        {
            return Ok(Vec::new());
        }
        Ok(saved.invalid_models)
    }

    /// Persist invalid replies before deferring for the shared provider deadline.
    pub async fn record_resume(
        &mut self,
        resume: ModelResume<'_>,
        invalid_models: &[Option<&str>],
    ) -> Result<(), ModelGateError> {
        resume.validate()?;
        let mut txn = self.connection.begin().await?;
        let result = async {
            prepare(&mut txn).await?;
            load_resume(&mut txn, resume, true).await?;
            let invalid: Vec<_> = resume
                .models
                .iter()
                .copied()
                .filter(|model| invalid_models.contains(model))
                .collect();
            let saved = json!({
                "identity_version": IDENTITY_VERSION,
                "configuration": self.identity.resume_configuration(),
                "models": resume.models,
                "request_fingerprint": resume.request_fingerprint,
                "invalid_models": invalid,
            });
            let changed = sqlx::query(
                "UPDATE jobs SET payload=payload || jsonb_build_object('provider_resume',$3::jsonb)
                 WHERE id=$1 AND lease_token=$2 AND status='running' AND lease_until>clock_timestamp()",
            )
            .bind(resume.job_id)
            .bind(resume.lease_token)
            .bind(saved.to_string())
            .execute(&mut *txn)
            .await?
            .rows_affected();
            if changed != 1 {
                return Err(ModelGateError::LeaseLost);
            }
            Ok(())
        }.await;
        finish(txn, result).await
    }

    /// Remove only the record for this request and provider configuration.
    pub async fn clear_resume(&mut self, resume: ModelResume<'_>) -> Result<bool, ModelGateError> {
        resume.validate()?;
        let mut txn = self.connection.begin().await?;
        let result = async {
            prepare(&mut txn).await?;
            let Some(saved) = load_resume(&mut txn, resume, true).await? else {
                return Ok(false);
            };
            if !saved.matches_request(resume, self.identity.resume_configuration()) {
                return Ok(false);
            }
            let changed = sqlx::query(
                "UPDATE jobs SET payload=payload-'provider_resume'
                 WHERE id=$1 AND lease_token=$2 AND status='running' AND lease_until>clock_timestamp()",
            )
            .bind(resume.job_id)
            .bind(resume.lease_token)
            .execute(&mut *txn)
            .await?
            .rows_affected();
            if changed != 1 {
                return Err(ModelGateError::LeaseLost);
            }
            Ok(true)
        }.await;
        finish(txn, result).await
    }
}

async fn load_resume(
    txn: &mut Transaction<'_, Postgres>,
    resume: ModelResume<'_>,
    write: bool,
) -> Result<Option<SavedResume>, ModelGateError> {
    macro_rules! selection {
        ($suffix:literal) => {
            concat!(
                "SELECT CASE WHEN NOT (payload ? 'provider_resume') THEN NULL
                    WHEN octet_length((payload->'provider_resume')::text)<=$3
                        THEN (payload->'provider_resume')::text ELSE 'false' END
                 FROM jobs WHERE id=$1 AND lease_token=$2 AND status='running'
                   AND lease_until>clock_timestamp() AND jsonb_typeof(payload)='object'",
                $suffix
            )
        };
    }
    let sql = if write {
        selection!(" FOR UPDATE NOWAIT")
    } else {
        selection!("")
    };
    let stored: Option<String> = sqlx::query_scalar(sql)
        .bind(resume.job_id)
        .bind(resume.lease_token)
        .bind(MAX_RESUME_BYTES)
        .fetch_optional(&mut **txn)
        .await?
        .ok_or(ModelGateError::LeaseLost)?;
    let Some(stored) = stored else {
        return Ok(None);
    };
    let saved: Value = serde_json::from_str(&stored).map_err(|_| ModelGateError::UnsafeResume)?;
    if saved.as_object().is_some_and(serde_json::Map::is_empty) {
        return Ok(None);
    }
    let saved: SavedResume =
        serde_json::from_value(saved).map_err(|_| ModelGateError::UnsafeResume)?;
    if !saved.valid() {
        return Err(ModelGateError::UnsafeResume);
    }
    Ok(Some(saved))
}
