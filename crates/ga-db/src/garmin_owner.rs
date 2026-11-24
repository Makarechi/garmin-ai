use std::{error::Error, fmt, time::Duration};

use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use crate::{Database, ReadinessError, check_readiness};

mod setup;
pub use setup::{GarminSetupError, LocalGarminSetup, LocalGarminSetupMode};

const EXISTING_NAMESPACE: &str = "socialProfile.profileId:v1";
const LEGACY_BINDING_KEY: &str = "account:garmin";

type BindingRow = (Uuid, Option<String>, bool, Option<String>);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct GarminOwner {
    owner_id: Uuid,
}

impl GarminOwner {
    #[must_use]
    pub fn owner_id(self) -> Uuid {
        self.owner_id
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OwnerVerificationError {
    InvalidFingerprint,
    EnrollmentRequired,
    Mismatch,
    InvalidStoredBinding,
    Storage(ReadinessError),
    Database,
}

impl fmt::Display for OwnerVerificationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidFingerprint => "Authenticated Garmin fingerprint is invalid",
            Self::EnrollmentRequired => "Garmin owner enrollment is required",
            Self::Mismatch => "Garmin account does not match the existing owner",
            Self::InvalidStoredBinding => "Stored Garmin owner binding is invalid",
            Self::Storage(_) => "Garmin owner storage is unavailable",
            Self::Database => "Garmin owner lookup failed",
        })
    }
}

impl Error for OwnerVerificationError {}

impl From<sqlx::Error> for OwnerVerificationError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Confirm identity against both representations of an established Garmin owner.
    /// This lookup never enrolls an owner or repairs a stored binding.
    pub async fn verify_existing_garmin_owner(
        &self,
        authenticated_fingerprint: &str,
    ) -> Result<GarminOwner, OwnerVerificationError> {
        if !is_fingerprint(authenticated_fingerprint) {
            return Err(OwnerVerificationError::InvalidFingerprint);
        }
        tokio::time::timeout(
            Duration::from_millis(1500),
            self.verify_existing_garmin_owner_transaction(authenticated_fingerprint),
        )
        .await
        .map_err(|_| OwnerVerificationError::Database)?
    }

    async fn verify_existing_garmin_owner_transaction(
        &self,
        authenticated_fingerprint: &str,
    ) -> Result<GarminOwner, OwnerVerificationError> {
        let mut txn = self.work.begin_with("BEGIN READ ONLY").await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(OwnerVerificationError::Storage)?;
        let owner = verify_owner_in_transaction(&mut txn, authenticated_fingerprint).await;
        txn.rollback().await?;
        owner
    }
}

pub(crate) async fn verify_owner_in_transaction(
    txn: &mut Transaction<'_, Postgres>,
    authenticated_fingerprint: &str,
) -> Result<GarminOwner, OwnerVerificationError> {
    if !is_fingerprint(authenticated_fingerprint) {
        return Err(OwnerVerificationError::InvalidFingerprint);
    }
    let rows = owner_bindings(txn).await?;
    verify_binding(&rows, authenticated_fingerprint)
}

/// Existing cryptographic identity only. Offline maintenance never enrolls an owner.
pub(crate) async fn stored_garmin_fingerprint(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<Option<String>, OwnerVerificationError> {
    let rows = owner_bindings(txn).await?;
    match rows.as_slice() {
        [] | [(_, None, false, None)] => Ok(None),
        [(_, canonical, _, legacy)] => {
            let candidate = canonical
                .as_deref()
                .or(legacy.as_deref())
                .ok_or(OwnerVerificationError::InvalidStoredBinding)?;
            verify_binding(&rows, candidate)?;
            Ok(Some(candidate.to_owned()))
        }
        _ => Err(OwnerVerificationError::InvalidStoredBinding),
    }
}

async fn owner_bindings(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<Vec<BindingRow>, sqlx::Error> {
    sqlx::query_as::<_, BindingRow>(
        "SELECT people.id, connection.external_id, legacy.key IS NOT NULL,
                    CASE WHEN jsonb_typeof(legacy.value->'fingerprint') = 'string'
                         THEN legacy.value->>'fingerprint' END
             FROM people
             LEFT JOIN source_connections AS connection ON connection.owner_id = people.id
                 AND connection.provider = 'garmin' AND connection.namespace = $1
             LEFT JOIN app_state AS legacy ON legacy.key = $2
             LIMIT 2",
    )
    .bind(EXISTING_NAMESPACE)
    .bind(LEGACY_BINDING_KEY)
    .fetch_all(&mut **txn)
    .await
}

fn verify_binding(
    rows: &[BindingRow],
    authenticated: &str,
) -> Result<GarminOwner, OwnerVerificationError> {
    let (id, canonical, legacy_exists, legacy) = match rows {
        [] => return Err(OwnerVerificationError::EnrollmentRequired),
        [row] => row,
        _ => return Err(OwnerVerificationError::InvalidStoredBinding),
    };
    if *legacy_exists && !legacy.as_deref().is_some_and(is_fingerprint) {
        return Err(OwnerVerificationError::InvalidStoredBinding);
    }
    let expected = [canonical.as_deref(), legacy.as_deref()];
    let mut found = false;
    for fingerprint in expected.into_iter().flatten() {
        if !is_fingerprint(fingerprint) {
            return Err(OwnerVerificationError::InvalidStoredBinding);
        }
        if fingerprint != authenticated {
            return Err(OwnerVerificationError::Mismatch);
        }
        found = true;
    }
    if !found {
        return Err(OwnerVerificationError::EnrollmentRequired);
    }
    Ok(GarminOwner { owner_id: *id })
}

fn is_fingerprint(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
