use std::{error::Error, fmt, time::Duration};

use ga_domain::timestamp::StoredInstant;
use serde_json::{Map, Value, json};
use sqlx::{AssertSqlSafe, Connection, PgConnection, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    EXISTING_NAMESPACE, LEGACY_BINDING_KEY, OwnerVerificationError, is_fingerprint, owner_bindings,
    verify_binding,
};
use crate::{
    Database, SCHEMA_REVISION,
    database_export::{TABLES, TOKEN_PUBLICATION_LOCK},
    garmin_integration::{GarminTransportOutcome, record_transport_in},
    storage_maintenance::StorageMaintenanceError,
};

const ACCOUNT_BINDING_LOCK: i64 = 72_104_627;
const BOOTSTRAP_TABLES: &[&str] = &[
    "app_state",
    "jobs",
    "people",
    "source_connections",
    "channel_bindings",
    "event_definitions",
    "event_definition_versions",
    "metric_definitions",
    "metric_definition_versions",
    "module_configs",
];

#[derive(Clone, Copy)]
pub enum LocalGarminSetupMode {
    Login { confirm_existing_owner: bool },
    Enroll,
}

impl LocalGarminSetupMode {
    fn confirms_legacy(self) -> bool {
        matches!(
            self,
            Self::Enroll
                | Self::Login {
                    confirm_existing_owner: true
                }
        )
    }
}

#[derive(Debug)]
pub enum GarminSetupError {
    Lock(StorageMaintenanceError),
    Owner(OwnerVerificationError),
    MigrationRequired,
    StorageDisabled,
    ConfirmationRequired,
    TokenPublication,
    Database,
}

impl fmt::Display for GarminSetupError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            Self::Lock(error) => return error.fmt(formatter),
            Self::Owner(error) => return error.fmt(formatter),
            Self::MigrationRequired => "Migrate the database before verifying its Garmin owner",
            Self::StorageDisabled => "Restore or resume storage before enrolling its Garmin owner",
            Self::ConfirmationRequired => "Confirm the existing owner using local enrollment",
            Self::TokenPublication => "The verified Garmin credentials could not be stored",
            Self::Database => "The Garmin owner could not be saved",
        };
        formatter.write_str(message)
    }
}
impl Error for GarminSetupError {}
impl From<sqlx::Error> for GarminSetupError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

enum Layout {
    Empty,
    Erased,
    Active,
}

/// Exclusive local setup session. Runtime/backup ownership remains fenced
/// during authentication and through publication of the verified token file.
pub struct LocalGarminSetup {
    connection: PgConnection,
    layout: Layout,
    mode: LocalGarminSetupMode,
}

impl Database {
    /// The caller holds the filesystem storage lock before opening a setup session.
    pub async fn acquire_local_garmin_setup(
        &self,
        mode: LocalGarminSetupMode,
    ) -> Result<LocalGarminSetup, GarminSetupError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut connection = self
                .locked_maintenance_connection()
                .await
                .map_err(GarminSetupError::Lock)?;
            for lock in [TOKEN_PUBLICATION_LOCK, ACCOUNT_BINDING_LOCK] {
                let acquired: bool = sqlx::query_scalar("SELECT pg_try_advisory_lock($1)")
                    .bind(lock)
                    .fetch_one(&mut connection)
                    .await?;
                if !acquired {
                    return Err(GarminSetupError::Lock(StorageMaintenanceError::Busy));
                }
            }
            let layout = layout(&mut connection).await?;
            if matches!(mode, LocalGarminSetupMode::Enroll) {
                match layout {
                    Layout::Empty => return Err(GarminSetupError::MigrationRequired),
                    Layout::Erased => return Err(GarminSetupError::StorageDisabled),
                    Layout::Active => {}
                }
            }
            Ok(LocalGarminSetup {
                connection,
                layout,
                mode,
            })
        })
        .await
        .map_err(|_| GarminSetupError::Database)?
    }
}

impl LocalGarminSetup {
    pub async fn publish(
        mut self,
        fingerprint: &str,
        retained_archive: bool,
        save_tokens: impl FnOnce() -> Result<(), GarminSetupError>,
    ) -> Result<(), GarminSetupError> {
        if !is_fingerprint(fingerprint) {
            return Err(GarminSetupError::Owner(
                OwnerVerificationError::InvalidFingerprint,
            ));
        }
        match self.layout {
            Layout::Empty if retained_archive && !self.mode.confirms_legacy() => {
                return Err(GarminSetupError::ConfirmationRequired);
            }
            Layout::Empty | Layout::Erased => return save_tokens(),
            Layout::Active => {}
        }
        let mut txn = self.connection.begin().await?;
        sqlx::query("SET LOCAL statement_timeout='1500ms'")
            .execute(&mut *txn)
            .await?;
        let rows = owner_bindings(&mut txn).await?;
        match verify_binding(&rows, fingerprint) {
            Ok(_) => repair_binding(&mut txn, &rows[0], fingerprint).await?,
            Err(OwnerVerificationError::EnrollmentRequired) => {
                if !self.mode.confirms_legacy()
                    && (retained_archive || contains_legacy_data(&mut txn).await?)
                {
                    return Err(GarminSetupError::ConfirmationRequired);
                }
                let owner = if let Some((id, ..)) = rows.first() {
                    *id
                } else {
                    sqlx::query_scalar(
                        "INSERT INTO people(id,locale,timezone,units)
                         VALUES(gen_random_uuid(),'ru','Europe/Bratislava','metric') RETURNING id",
                    )
                    .fetch_one(&mut *txn)
                    .await?
                };
                let id = Uuid::new_v4().to_string();
                let details = json!({"instance_id":id,"identity_contract":EXISTING_NAMESPACE});
                insert_source(
                    &mut txn,
                    owner,
                    fingerprint,
                    "authenticated_profile",
                    &details,
                )
                .await?;
                save_legacy_binding(&mut txn, fingerprint, &id).await?;
            }
            Err(error) => return Err(GarminSetupError::Owner(error)),
        }
        let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut *txn)
            .await?;
        let at = StoredInstant::try_from(now).map_err(|_| GarminSetupError::Database)?;
        record_transport_in(&mut txn, at, GarminTransportOutcome::AuthorizedLogin)
            .await
            .map_err(|_| GarminSetupError::Database)?;
        save_tokens()?;
        txn.commit().await?;
        Ok(())
    }
}

async fn layout(connection: &mut PgConnection) -> Result<Layout, GarminSetupError> {
    let has_ledger: bool =
        sqlx::query_scalar("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            .fetch_one(&mut *connection)
            .await?;
    let tables: Vec<_> = TABLES.iter().map(|(table, _)| *table).collect();
    let count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM unnest($1::text[]) name
         WHERE to_regclass(format('public.%I',name)) IS NOT NULL",
    )
    .bind(&tables)
    .fetch_one(&mut *connection)
    .await?;
    if count == 0 && !has_ledger {
        return Ok(Layout::Empty);
    }
    if !has_ledger || usize::try_from(count).ok() != Some(tables.len()) {
        return Err(GarminSetupError::MigrationRequired);
    }
    let revisions: Vec<String> =
        sqlx::query_scalar("SELECT version_num FROM public.alembic_version LIMIT 2")
            .fetch_all(&mut *connection)
            .await?;
    if !matches!(revisions.as_slice(), [revision] if revision == SCHEMA_REVISION) {
        return Err(GarminSetupError::MigrationRequired);
    }
    let erased: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM public.app_state WHERE key='maintenance:erased')",
    )
    .fetch_one(connection)
    .await?;
    Ok(if erased {
        Layout::Erased
    } else {
        Layout::Active
    })
}

async fn contains_legacy_data(txn: &mut Transaction<'_, Postgres>) -> Result<bool, sqlx::Error> {
    let predicate = TABLES
        .iter()
        .filter(|(table, _)| !BOOTSTRAP_TABLES.contains(table))
        .map(|(table, _)| format!("EXISTS(SELECT 1 FROM public.\"{table}\")"))
        .collect::<Vec<_>>()
        .join(" OR ");
    sqlx::query_scalar(AssertSqlSafe(format!(
        "SELECT {predicate}
            OR EXISTS(SELECT 1 FROM event_definitions WHERE namespace<>'system')
            OR EXISTS(SELECT 1 FROM metric_definitions WHERE namespace<>'system')
            OR EXISTS(SELECT 1 FROM app_state
              WHERE key NOT IN ('runtime:heartbeat','telegram:offset','proactive:enabled','integration:garmin',
                    'proactive:generation','backup:last_success','registry:system:contract_digest',
                    'registry:metric:catalog_digest','registry:canonical:validated')
              AND NOT starts_with(key,'outbox:auth:') AND NOT starts_with(key,'outbox:account-binding:'))"
    ))).fetch_one(&mut **txn).await
}

async fn repair_binding(
    txn: &mut Transaction<'_, Postgres>,
    (owner, canonical, has_legacy, _): &super::BindingRow,
    fingerprint: &str,
) -> Result<(), sqlx::Error> {
    if canonical.is_none() {
        let state: String = sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
            .bind(LEGACY_BINDING_KEY)
            .fetch_one(&mut **txn)
            .await?;
        let value: Value = serde_json::from_str(&state)
            .map_err(|_| sqlx::Error::Protocol("Invalid Garmin binding".into()))?;
        let details = ["instance_id", "identity_contract"]
            .into_iter()
            .filter_map(|key| {
                value
                    .get(key)
                    .filter(|part| !part.is_null())
                    .map(|part| (key.to_owned(), part.clone()))
            })
            .collect::<Map<_, _>>();
        insert_source(
            txn,
            *owner,
            fingerprint,
            "legacy_account_binding",
            &Value::Object(details),
        )
        .await?;
    } else if !has_legacy {
        let instance: Option<String> = sqlx::query_scalar(
            "SELECT details->>'instance_id' FROM source_connections
             WHERE owner_id=$1 AND provider='garmin' AND namespace=$2",
        )
        .bind(owner)
        .bind(EXISTING_NAMESPACE)
        .fetch_one(&mut **txn)
        .await?;
        let instance = instance
            .filter(|id| !id.is_empty())
            .unwrap_or_else(|| owner.to_string());
        save_legacy_binding(txn, fingerprint, &instance).await?;
    }
    Ok(())
}

async fn insert_source(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    fingerprint: &str,
    method: &str,
    details: &Value,
) -> Result<(), sqlx::Error> {
    sqlx::query("INSERT INTO source_connections(id,owner_id,provider,namespace,external_id,confirmation_method,details)
                 VALUES(gen_random_uuid(),$1,'garmin',$2,$3,$4,$5::jsonb)")
        .bind(owner).bind(EXISTING_NAMESPACE).bind(fingerprint).bind(method).bind(details.to_string())
        .execute(&mut **txn).await?;
    Ok(())
}

async fn save_legacy_binding(
    txn: &mut Transaction<'_, Postgres>,
    fingerprint: &str,
    instance: &str,
) -> Result<(), sqlx::Error> {
    let value = json!({"instance_id":instance,"fingerprint":fingerprint,"identity_contract":EXISTING_NAMESPACE});
    sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
        .bind(LEGACY_BINDING_KEY)
        .bind(value.to_string())
        .execute(&mut **txn)
        .await?;
    Ok(())
}
