//! Run native schema transitions using the existing Alembic revision ledger.

mod catalog;

use std::{error::Error, fmt};

use sqlx::{Postgres, Transaction};

use crate::{
    Database, OWNER_ENROLLMENT_LOCK, SCHEMA_REVISION, STORAGE_LIFECYCLE_LOCK,
    runtime_lease::RUNTIME_LOCK,
};

const BEFORE_REVISIONS: &str = "b7c4e1a92d60";
const BEFORE_FENCE: &str = "c8f51d3a7e20";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SchemaState {
    Empty,
    BeforeRevisions,
    BeforeFence,
    Current,
}

impl SchemaState {
    pub fn revision(self) -> Option<&'static str> {
        match self {
            Self::Empty => None,
            Self::BeforeRevisions => Some(BEFORE_REVISIONS),
            Self::BeforeFence => Some(BEFORE_FENCE),
            Self::Current => Some(SCHEMA_REVISION),
        }
    }
}

#[derive(Debug)]
pub enum SchemaMigrationError {
    Busy,
    Unsupported,
    Occupied,
    Registry,
    Database,
}

impl fmt::Display for SchemaMigrationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Busy => "Stop the worker or other storage maintenance before upgrading",
            Self::Unsupported => "The recorded database revision is unsupported",
            Self::Occupied => "A database without a Garmin AI revision must be empty",
            Self::Registry => "Application definitions could not be initialized safely",
            Self::Database => "The database schema could not be inspected or upgraded",
        })
    }
}

impl Error for SchemaMigrationError {}

impl From<sqlx::Error> for SchemaMigrationError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    pub async fn schema_status(&self) -> Result<SchemaState, SchemaMigrationError> {
        let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
        let state = schema_state(&mut txn).await?;
        txn.rollback().await?;
        Ok(state)
    }

    /// Install or upgrade a supported schema. Schema, transformations and the
    /// version ledger commit together under the original database runtime locks.
    pub async fn migrate_schema(&self) -> Result<SchemaState, SchemaMigrationError> {
        self.migrate(false).await
    }

    /// The application command commits the schema and its active registry together.
    pub async fn migrate_and_initialize(&self) -> Result<SchemaState, SchemaMigrationError> {
        self.migrate(true).await
    }

    async fn migrate(&self, initialize: bool) -> Result<SchemaState, SchemaMigrationError> {
        let mut txn = self.work.begin().await?;
        protect(&mut txn).await?;
        let state = schema_state(&mut txn).await?;
        match state {
            SchemaState::Empty => install(&mut txn).await?,
            SchemaState::BeforeRevisions => {
                sqlx::raw_sql(include_str!("schema_migration/measurement_revisions.sql"))
                    .execute(&mut *txn)
                    .await?;
                advance(&mut txn, BEFORE_REVISIONS, BEFORE_FENCE).await?;
                delivery_fence(&mut txn).await?;
            }
            SchemaState::BeforeFence => delivery_fence(&mut txn).await?,
            SchemaState::Current => {}
        }
        if initialize {
            catalog::initialize(&mut txn).await?;
        }
        txn.commit().await?;
        Ok(state)
    }
}

async fn protect(txn: &mut Transaction<'_, Postgres>) -> Result<(), SchemaMigrationError> {
    sqlx::query("SET LOCAL lock_timeout='2s'")
        .execute(&mut **txn)
        .await?;
    sqlx::query("SET LOCAL search_path=pg_catalog,public")
        .execute(&mut **txn)
        .await?;
    for lock in [RUNTIME_LOCK, STORAGE_LIFECYCLE_LOCK, OWNER_ENROLLMENT_LOCK] {
        let available: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
            .bind(lock)
            .fetch_one(&mut **txn)
            .await?;
        if !available {
            return Err(SchemaMigrationError::Busy);
        }
    }
    Ok(())
}

async fn schema_state(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<SchemaState, SchemaMigrationError> {
    let version_table: bool =
        sqlx::query_scalar("SELECT pg_catalog.to_regclass('public.alembic_version') IS NOT NULL")
            .fetch_one(&mut **txn)
            .await?;
    if !version_table {
        return if has_existing_objects(txn).await? {
            Err(SchemaMigrationError::Occupied)
        } else {
            Ok(SchemaState::Empty)
        };
    }
    let revisions: Vec<String> =
        sqlx::query_scalar("SELECT version_num FROM public.alembic_version LIMIT 2")
            .fetch_all(&mut **txn)
            .await?;
    match revisions.as_slice() {
        [revision] if revision == SCHEMA_REVISION => Ok(SchemaState::Current),
        [revision] if revision == BEFORE_FENCE => Ok(SchemaState::BeforeFence),
        [revision] if revision == BEFORE_REVISIONS => Ok(SchemaState::BeforeRevisions),
        _ => Err(SchemaMigrationError::Unsupported),
    }
}

async fn has_existing_objects(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<bool, SchemaMigrationError> {
    Ok(sqlx::query_scalar(
        "SELECT EXISTS (
            SELECT FROM pg_catalog.pg_class item
            JOIN pg_catalog.pg_namespace space ON space.oid=item.relnamespace
            WHERE space.nspname='public' AND item.relkind IN ('r','p','v','m','S','f')
            AND NOT EXISTS (SELECT FROM pg_catalog.pg_depend dep
                WHERE dep.classid='pg_catalog.pg_class'::regclass
                  AND dep.objid=item.oid AND dep.deptype='e')
         ) OR EXISTS (
            SELECT FROM pg_catalog.pg_proc item
            JOIN pg_catalog.pg_namespace space ON space.oid=item.pronamespace
            WHERE space.nspname='public'
            AND NOT EXISTS (SELECT FROM pg_catalog.pg_depend dep
                WHERE dep.classid='pg_catalog.pg_proc'::regclass
                  AND dep.objid=item.oid AND dep.deptype='e')
         )",
    )
    .fetch_one(&mut **txn)
    .await?)
}

async fn install(txn: &mut Transaction<'_, Postgres>) -> Result<(), SchemaMigrationError> {
    sqlx::query("CREATE SCHEMA IF NOT EXISTS public")
        .execute(&mut **txn)
        .await?;
    sqlx::query("CREATE EXTENSION IF NOT EXISTS timescaledb WITH SCHEMA public")
        .execute(&mut **txn)
        .await?;
    sqlx::raw_sql(include_str!("schema_migration/bootstrap.sql"))
        .execute(&mut **txn)
        .await?;
    sqlx::query(
        "SELECT public.create_hypertable('public.measurements', 'ts', create_default_indexes => FALSE)",
    ).execute(&mut **txn).await?;
    sqlx::query(
        "INSERT INTO public.people(id,singleton,locale,timezone,units)
         VALUES(gen_random_uuid(),TRUE,'ru','Europe/Bratislava','metric')",
    )
    .execute(&mut **txn)
    .await?;
    sqlx::query("INSERT INTO public.alembic_version(version_num) VALUES($1)")
        .bind(SCHEMA_REVISION)
        .execute(&mut **txn)
        .await?;
    Ok(())
}

async fn delivery_fence(txn: &mut Transaction<'_, Postgres>) -> Result<(), SchemaMigrationError> {
    sqlx::query("ALTER TABLE public.outbox_messages ADD COLUMN memory_fence JSONB")
        .execute(&mut **txn)
        .await?;
    advance(txn, BEFORE_FENCE, SCHEMA_REVISION).await
}

async fn advance(
    txn: &mut Transaction<'_, Postgres>,
    before: &str,
    after: &str,
) -> Result<(), SchemaMigrationError> {
    let rows = sqlx::query("UPDATE public.alembic_version SET version_num=$2 WHERE version_num=$1")
        .bind(before)
        .bind(after)
        .execute(&mut **txn)
        .await?
        .rows_affected();
    if rows != 1 {
        return Err(SchemaMigrationError::Unsupported);
    }
    Ok(())
}
