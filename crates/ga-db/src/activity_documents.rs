use std::{collections::BTreeMap, error::Error, fmt};

use ga_domain::{
    event_input::known_timezone,
    garmin_health::{HealthReplacement, HealthStatus},
    garmin_summary::{activity_identity, historical_summary_fields, summary_fields},
    timestamp::StoredInstant,
};
use serde_json::{Map, Value, json};
use sqlx::{Acquire, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{
    Database, health_projection,
    raw_capture::{CaptureError, GARMIN_LEGACY_SOURCE, lock_legacy_scope},
};

pub(crate) const PARSER_VERSION: i32 = 9;

#[derive(Clone, Copy)]
pub enum ActivityDocument {
    Details,
    Splits,
    TypedSplits,
    Zones,
    Weather,
}

impl ActivityDocument {
    pub const fn endpoint(self) -> &'static str {
        match self {
            Self::Details => "activity_details",
            Self::Splits => "activity_splits",
            Self::TypedSplits => "activity_typed_splits",
            Self::Zones => "activity_zones",
            Self::Weather => "activity_weather",
        }
    }
}

pub struct ApplyActivityDocument<'a> {
    pub account_fingerprint: &'a str,
    pub activity_id: &'a str,
    pub document: ActivityDocument,
    pub source_id: Uuid,
    pub requested_at: StoredInstant,
    pub timezone: &'a str,
}

#[derive(Clone, Copy)]
pub enum ActivitySummaryKind {
    Single,
    Page,
}

impl ActivitySummaryKind {
    pub const fn endpoint(self) -> &'static str {
        match self {
            Self::Single => "activity",
            Self::Page => "activities",
        }
    }
}

pub struct ApplyActivitySummary<'a> {
    pub account_fingerprint: &'a str,
    pub source_key: &'a str,
    pub summary: ActivitySummaryKind,
    pub source_id: Uuid,
    pub requested_at: StoredInstant,
    pub timezone: &'a str,
}

pub struct ApplyHealthPayload<'a> {
    pub account_fingerprint: &'a str,
    pub endpoint: &'a str,
    pub calendar_key: &'a str,
    pub source_id: Uuid,
    pub requested_at: StoredInstant,
    pub timezone: &'a str,
}

#[derive(Clone, Copy)]
enum Projection<'a> {
    Document(ActivityDocument),
    Summary(ActivitySummaryKind),
    Health(&'a str, Option<&'a HealthReplacement>),
}

impl<'a> Projection<'a> {
    fn endpoint(self) -> &'a str {
        match self {
            Self::Document(document) => document.endpoint(),
            Self::Summary(kind) => kind.endpoint(),
            Self::Health(endpoint, _) => endpoint,
        }
    }

    fn replacement(self) -> Option<&'a HealthReplacement> {
        match self {
            Self::Health(_, proof) => proof,
            _ => None,
        }
    }

    fn shared(self) -> bool {
        matches!(
            self,
            Self::Summary(_) | Self::Health("daily" | "heart_rate" | "body_battery", _)
        )
    }
}

pub(crate) struct ApplyActivity<'a> {
    pub(crate) account_fingerprint: &'a str,
    pub(crate) source_key: &'a str,
    projection: Projection<'a>,
    pub(crate) source_id: Uuid,
    pub(crate) requested_at: StoredInstant,
    pub(crate) timezone: &'a str,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DocumentStatus {
    Archived,
    Normalized,
    Empty,
    Unchanged,
    Stale,
    Error,
}

impl DocumentStatus {
    fn label(self) -> &'static str {
        match self {
            Self::Archived => "archived",
            Self::Normalized => "normalized",
            Self::Empty => "empty",
            Self::Unchanged => "unchanged",
            Self::Stale => "stale",
            Self::Error => "error",
        }
    }
}

#[derive(Debug)]
pub enum DocumentError {
    Access(CaptureError),
    SourceMissing,
    InvalidRecordedState,
    InvalidSummary,
    InvalidHealthPayload,
    Database,
}

impl fmt::Display for DocumentError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Access(_) => "Activity document publication could not be authorized",
            Self::SourceMissing => "Captured activity document was not found in its expected scope",
            Self::InvalidRecordedState => {
                "Recorded activity document state could not be interpreted"
            }
            Self::InvalidSummary => "Captured Garmin activity summary is invalid",
            Self::InvalidHealthPayload => "Captured Garmin health payload is invalid",
            Self::Database => "Activity document state could not be stored",
        })
    }
}

impl Error for DocumentError {}

impl From<sqlx::Error> for DocumentError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl Database {
    /// Apply a complete captured legacy document without modifying the activity summary.
    pub async fn apply_activity_document(
        &self,
        request: ApplyActivityDocument<'_>,
    ) -> Result<DocumentStatus, DocumentError> {
        self.apply_activity(ApplyActivity {
            account_fingerprint: request.account_fingerprint,
            source_key: request.activity_id,
            projection: Projection::Document(request.document),
            source_id: request.source_id,
            requested_at: request.requested_at,
            timezone: request.timezone,
        })
        .await
    }

    /// Apply a captured single summary or list page while retaining omitted fields.
    pub async fn apply_activity_summary(
        &self,
        request: ApplyActivitySummary<'_>,
    ) -> Result<DocumentStatus, DocumentError> {
        self.apply_activity(ApplyActivity {
            account_fingerprint: request.account_fingerprint,
            source_key: request.source_key,
            projection: Projection::Summary(request.summary),
            source_id: request.source_id,
            requested_at: request.requested_at,
            timezone: request.timezone,
        })
        .await
    }

    /// Apply one retained daily or intraday response using its original source identity.
    pub async fn apply_health_payload(
        &self,
        request: ApplyHealthPayload<'_>,
    ) -> Result<DocumentStatus, DocumentError> {
        self.apply_captured_health(request, None).await
    }

    /// Publish a complete adapter-attested interval and retain the removed samples' history.
    pub async fn apply_attested_health_payload(
        &self,
        request: ApplyHealthPayload<'_>,
        replacement: &HealthReplacement,
    ) -> Result<DocumentStatus, DocumentError> {
        replacement
            .validate_endpoint(request.endpoint)
            .map_err(|_| DocumentError::InvalidHealthPayload)?;
        time::Date::parse(
            request.calendar_key,
            &time::format_description::well_known::Iso8601::DEFAULT,
        )
        .map_err(|_| DocumentError::InvalidHealthPayload)?;
        self.apply_captured_health(request, Some(replacement)).await
    }

    async fn apply_captured_health(
        &self,
        request: ApplyHealthPayload<'_>,
        replacement: Option<&HealthReplacement>,
    ) -> Result<DocumentStatus, DocumentError> {
        if matches!(request.endpoint, "activity" | "activities")
            || request.endpoint.starts_with("activity_")
        {
            return Err(DocumentError::InvalidHealthPayload);
        }
        self.apply_activity(ApplyActivity {
            account_fingerprint: request.account_fingerprint,
            source_key: request.calendar_key,
            projection: Projection::Health(request.endpoint, replacement),
            source_id: request.source_id,
            requested_at: request.requested_at,
            timezone: request.timezone,
        })
        .await
    }
    async fn apply_activity(
        &self,
        request: ApplyActivity<'_>,
    ) -> Result<DocumentStatus, DocumentError> {
        let endpoint = request.projection.endpoint();
        let mut txn = self
            .begin_legacy_source(request.account_fingerprint, endpoint, request.source_key)
            .await
            .map_err(DocumentError::Access)?;
        let (hash, status, parser, empty) = sqlx::query_as::<_, (String, String, i32, bool)>(
            "SELECT payload_hash, status, parser_version,
                        payload IN ('null'::jsonb, '{}'::jsonb, '[]'::jsonb)
                 FROM source_payloads
                 WHERE id = $1 AND source = $2 AND endpoint = $3 AND source_key = $4
                   AND payload IS NOT NULL FOR UPDATE",
        )
        .bind(request.source_id)
        .bind(GARMIN_LEGACY_SOURCE)
        .bind(endpoint)
        .bind(request.source_key)
        .fetch_optional(&mut *txn)
        .await?
        .ok_or(DocumentError::SourceMissing)?;

        let state_key = format!(
            "ingest:{GARMIN_LEGACY_SOURCE}:{endpoint}:{}",
            request.source_key
        );
        let meta_key = format!("ingest-meta:{}", request.source_id);
        let mut state = read_state(&mut txn, &state_key).await?;
        if last_request(&state)?.is_some_and(|latest| request.requested_at < latest) {
            if status == "pending" {
                raw_status(&mut txn, request.source_id, "stale", parser).await?;
            }
            txn.commit().await?;
            return Ok(DocumentStatus::Stale);
        }

        let replacement = request
            .projection
            .replacement()
            .map(HealthReplacement::document);
        let unchanged = state.get("hash").and_then(Value::as_str) == Some(&hash)
            && parser == PARSER_VERSION
            && !matches!(status.as_str(), "pending" | "error")
            && state.get("replacement").unwrap_or(&Value::Null)
                == replacement.as_ref().unwrap_or(&Value::Null);
        let mut metadata = read_state(&mut txn, &meta_key).await?;
        let requested = request.requested_at.to_string();
        let attempt = json!({
            "hash": hash,
            "source_ref": request.source_id.to_string(),
            "requested_at": requested,
            "replacement": replacement,
            "completeness": if replacement.is_some() { "adapter_attested" } else { "unverified" },
        });

        let mut outcome = None;
        if !unchanged || request.projection.shared() {
            let mut effects = txn.begin().await?;
            match apply_effects(&mut effects, &request, empty, &status, parser, unchanged).await {
                Ok(status) => outcome = Some(status),
                Err(_) => {
                    effects.rollback().await?;
                    raw_status(&mut txn, request.source_id, "error", parser).await?;
                    metadata.insert("timezone".to_owned(), json!(request.timezone));
                    metadata.insert("failed_parser_version".to_owned(), json!(PARSER_VERSION));
                    put_state(&mut txn, &meta_key, &Value::Object(metadata)).await?;
                    let mut attempt = attempt;
                    attempt["parser_version"] = json!(PARSER_VERSION);
                    state.insert("status".to_owned(), json!("error"));
                    state.insert("latest_attempt".to_owned(), attempt);
                    put_state(&mut txn, &state_key, &Value::Object(state)).await?;
                    txn.commit().await?;
                    return Ok(DocumentStatus::Error);
                }
            }
            effects.commit().await?;
            if !unchanged {
                metadata.insert("timezone".to_owned(), json!(request.timezone));
            }
        }

        metadata.insert("applied_at".to_owned(), json!(requested));
        metadata.remove("failed_parser_version");
        put_state(&mut txn, &meta_key, &Value::Object(metadata)).await?;
        let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut *txn)
            .await?;
        let fetched =
            StoredInstant::try_from(now).map_err(|_| DocumentError::InvalidRecordedState)?;
        let raw_outcome = match outcome {
            Some(outcome) => outcome.label(),
            None => status.as_str(),
        };
        let mut value = attempt;
        value["fetched_at"] = json!(fetched.to_string());
        value["status"] = json!(raw_outcome);
        if raw_outcome == "empty"
            && replacement.is_none()
            && let Some(reference) = state.get("source_ref").and_then(Value::as_str)
            && !reference.is_empty()
            && reference != request.source_id.to_string()
        {
            value["parser_version"] = json!(PARSER_VERSION);
            state.insert("status".to_owned(), json!("empty"));
            state.insert("latest_attempt".to_owned(), value);
            value = Value::Object(state);
        }
        put_state(&mut txn, &state_key, &value).await?;
        txn.commit().await?;
        if unchanged {
            Ok(DocumentStatus::Unchanged)
        } else {
            outcome.ok_or(DocumentError::InvalidRecordedState)
        }
    }
}

fn last_request(state: &Map<String, Value>) -> Result<Option<StoredInstant>, DocumentError> {
    state
        .get("latest_attempt")
        .and_then(|attempt| attempt.get("requested_at"))
        .filter(|value| !value.is_null())
        .or_else(|| state.get("requested_at").filter(|value| !value.is_null()))
        .map(|value| {
            value
                .as_str()
                .ok_or(DocumentError::InvalidRecordedState)?
                .parse()
                .map_err(|_| DocumentError::InvalidRecordedState)
        })
        .transpose()
}

async fn read_state(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
) -> Result<Map<String, Value>, DocumentError> {
    let result: Option<String> =
        sqlx::query_scalar("SELECT value::text FROM app_state WHERE key = $1")
            .bind(key)
            .fetch_optional(&mut **txn)
            .await?;
    result
        .map(|value| serde_json::from_str(&value).map_err(|_| DocumentError::InvalidRecordedState))
        .transpose()
        .map(Option::unwrap_or_default)
}

pub(crate) async fn put_state(
    txn: &mut Transaction<'_, Postgres>,
    key: &str,
    value: &Value,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO app_state (key,value) VALUES ($1,$2::jsonb)
         ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()
         WHERE app_state.value IS DISTINCT FROM excluded.value",
    )
    .bind(key)
    .bind(value.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

async fn raw_status(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    status: &str,
    parser: i32,
) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE source_payloads SET status = $2, parser_version = $3 WHERE id = $1")
        .bind(id)
        .bind(status)
        .bind(parser)
        .execute(&mut **txn)
        .await?;
    Ok(())
}

async fn apply_document(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    endpoint: &str,
) -> Result<(), sqlx::Error> {
    let activity: Option<String> =
        sqlx::query_scalar("SELECT id FROM activities WHERE id = $1 FOR UPDATE")
            .bind(request.source_key)
            .fetch_optional(&mut **txn)
            .await?;
    if activity.is_some() {
        let owner_key = format!("activity-parts-owner:{}:{endpoint}", request.source_key);
        put_state(
            txn,
            &owner_key,
            &json!({"source_ref": request.source_id.to_string()}),
        )
        .await?;
        sqlx::query("DELETE FROM activity_parts WHERE activity_id = $1 AND kind = $2")
            .bind(request.source_key)
            .bind(endpoint)
            .execute(&mut **txn)
            .await?;
        sqlx::query(
            "INSERT INTO activity_parts (activity_id, kind, sequence, payload)
                 SELECT $1, $2, (element.ordinality - 1)::int, element.value
                 FROM source_payloads AS source
                 CROSS JOIN LATERAL jsonb_array_elements(
                     CASE WHEN jsonb_typeof(source.payload) = 'array' THEN source.payload
                          ELSE jsonb_build_array(source.payload) END
                 ) WITH ORDINALITY AS element(value, ordinality)
                 WHERE source.id = $3",
        )
        .bind(request.source_key)
        .bind(endpoint)
        .bind(request.source_id)
        .execute(&mut **txn)
        .await?;
        supersede_insights(txn).await?;
    }

    Ok(())
}

async fn apply_effects(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    empty: bool,
    previous_status: &str,
    previous_parser: i32,
    unchanged: bool,
) -> Result<DocumentStatus, DocumentError> {
    let outcome = if empty && request.projection.replacement().is_none() {
        DocumentStatus::Empty
    } else {
        match request.projection {
            Projection::Document(document) => {
                apply_document(txn, request, document.endpoint()).await?;
                DocumentStatus::Archived
            }
            Projection::Summary(kind) => {
                apply_summaries(txn, request, kind).await?;
                DocumentStatus::Normalized
            }
            Projection::Health(endpoint, replacement) => {
                match health_projection::publish(txn, request, endpoint, unchanged, replacement)
                    .await?
                {
                    HealthStatus::Normalized => DocumentStatus::Normalized,
                    HealthStatus::Archived => DocumentStatus::Archived,
                    HealthStatus::Empty => DocumentStatus::Empty,
                }
            }
        }
    };
    raw_status(txn, request.source_id, outcome.label(), PARSER_VERSION).await?;
    if previous_parser != PARSER_VERSION
        && (previous_parser > 0 || matches!(previous_status, "error" | "stale"))
    {
        invalidate_replay_outputs(txn).await?;
    }
    Ok(outcome)
}

async fn apply_summaries(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    kind: ActivitySummaryKind,
) -> Result<(), DocumentError> {
    let document: String =
        sqlx::query_scalar("SELECT payload::text FROM source_payloads WHERE id = $1")
            .bind(request.source_id)
            .fetch_one(&mut **txn)
            .await?;
    let document: Value =
        serde_json::from_str(&document).map_err(|_| DocumentError::InvalidSummary)?;
    let mut entries = match (kind, document) {
        (ActivitySummaryKind::Single, entry @ Value::Object(_)) => vec![entry],
        (ActivitySummaryKind::Page, Value::Array(entries)) => entries,
        _ => return Err(DocumentError::InvalidSummary),
    }
    .into_iter()
    .map(|entry| activity_identity(&entry).map(|id| (id, entry)))
    .collect::<Result<Vec<_>, _>>()
    .map_err(|_| DocumentError::InvalidSummary)?;
    entries.sort_by(|(left, _), (right, _)| left.cmp(right));
    let mut changed = false;
    for (id, entry) in entries {
        if matches!(kind, ActivitySummaryKind::Single) && id != request.source_key {
            return Err(DocumentError::InvalidSummary);
        }
        changed |= apply_summary(txn, request, &id, &entry).await?;
    }
    if changed {
        supersede_insights(txn).await?;
    }
    Ok(())
}

async fn apply_summary(
    txn: &mut Transaction<'_, Postgres>,
    request: &ApplyActivity<'_>,
    id: &str,
    entry: &Value,
) -> Result<bool, DocumentError> {
    let state_key = format!("activity-version:{id}");
    lock_legacy_scope(txn, &state_key)
        .await
        .map_err(DocumentError::Access)?;
    let state = read_state(txn, &state_key).await?;
    if last_request(&state)?.is_some_and(|latest| latest > request.requested_at) {
        return Ok(false);
    }
    let fields = summary_fields(entry).map_err(|_| DocumentError::InvalidSummary)?;
    let installed: Option<String> = sqlx::query_scalar(
        "SELECT to_jsonb(activities)::text FROM activities WHERE id = $1 FOR UPDATE",
    )
    .bind(id)
    .fetch_optional(&mut **txn)
    .await?;
    let installed: Option<Value> = installed
        .map(|installed| serde_json::from_str(&installed))
        .transpose()
        .map_err(|_| DocumentError::InvalidRecordedState)?;
    let fallback = installed
        .as_ref()
        .and_then(|value| value["timezone"].as_str())
        .unwrap_or(request.timezone);
    let zone = fields
        .get("timezone")
        .and_then(Value::as_str)
        .unwrap_or(fallback);
    if !known_timezone(zone) {
        return Err(DocumentError::InvalidSummary);
    }
    let (mut owners, mut legacy) = summary_owners(txn, id, installed.as_ref(), &state).await?;
    let source = json!(request.source_id.to_string());
    legacy.retain(|field| field.as_str().is_none_or(|name| !fields.contains_key(name)));
    legacy.sort_by(|left, right| left.as_str().cmp(&right.as_str()));
    for name in fields.keys() {
        owners.insert(name.clone(), source.clone());
    }
    let changed = publish_summary_row(txn, id, fallback, &Value::Object(fields)).await?;
    put_state(
        txn,
        &state_key,
        &json!({
            "requested_at": request.requested_at.to_string(),
            "owners": owners,
            "owners_initialized": true,
            "legacy_owner_fields": legacy,
        }),
    )
    .await?;
    Ok(changed)
}

type SummaryOwners = (Map<String, Value>, Vec<Value>);

async fn summary_owners(
    txn: &mut Transaction<'_, Postgres>,
    id: &str,
    installed: Option<&Value>,
    state: &Map<String, Value>,
) -> Result<SummaryOwners, DocumentError> {
    let owners = state
        .get("owners")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let legacy = state
        .get("legacy_owner_fields")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if state.is_empty()
        || !owners.is_empty()
        || state.get("owners_initialized") == Some(&Value::Bool(true))
    {
        return Ok((owners, legacy));
    }

    use futures_util::TryStreamExt;
    let mut candidates = sqlx::query_as::<_, (Uuid, String, OffsetDateTime, bool)>(
        "SELECT raw.id, entry.value::text,
             coalesce(nullif(metadata.value->>'applied_at', '')::timestamptz, raw.fetched_at) AS applied_at,
             raw.status != 'error' OR nullif(metadata.value->>'applied_at','') IS NOT NULL
         FROM source_payloads AS raw
         LEFT JOIN app_state AS metadata ON metadata.key = 'ingest-meta:' || raw.id::text
         CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(raw.payload) = 'array'
                    THEN raw.payload ELSE jsonb_build_array(raw.payload) END) AS entry(value)
         WHERE raw.endpoint IN ('activity', 'activities') AND raw.parser_version > 0
           AND raw.status IN ('normalized', 'partial', 'error') AND entry.value->>'activityId' = $1
         ORDER BY applied_at",
    )
    .bind(id)
    .fetch(&mut **txn);
    let mut chosen: BTreeMap<String, (Uuid, OffsetDateTime, bool, bool)> = BTreeMap::new();
    let mut last_applied: BTreeMap<String, OffsetDateTime> = BTreeMap::new();
    while let Some((source, document, at, applied)) = candidates.try_next().await? {
        let document: Value =
            serde_json::from_str(&document).map_err(|_| DocumentError::InvalidRecordedState)?;
        let Ok(fields) = historical_summary_fields(&document) else {
            continue;
        };
        for (field, value) in fields {
            let matched =
                installed.is_some_and(|row| same_summary_value(&field, &row[&field], &value));
            let previous = chosen.get(&field);
            if !applied
                && (!matched
                    || previous.is_some_and(|(_, _, matched, applied)| *matched && *applied))
            {
                continue;
            }
            if applied {
                last_applied.insert(field.clone(), at);
            }
            if previous.is_none_or(|(_, prior_at, prior_match, _)| {
                (matched && !prior_match) || (matched == *prior_match && at > *prior_at)
            }) {
                chosen.insert(field, (source, at, matched, applied));
            }
        }
    }
    let mut owners = Map::new();
    let mut protected = Vec::new();
    for (field, (source, at, matched, _)) in chosen {
        if matched && last_applied.get(&field).is_some_and(|latest| at < *latest) {
            protected.push(json!(field));
        }
        owners.insert(field, json!(source.to_string()));
    }
    Ok((owners, protected))
}

fn same_summary_value(field: &str, current: &Value, historical: &Value) -> bool {
    if let (Some(current), Some(historical)) = (current.as_f64(), historical.as_f64()) {
        return current == historical;
    }
    if matches!(field, "start" | "end")
        && let (Some(current), Some(historical)) = (current.as_str(), historical.as_str())
        && let (Ok(current), Ok(historical)) = (
            current.parse::<StoredInstant>(),
            historical.parse::<StoredInstant>(),
        )
    {
        return current == historical;
    }
    current == historical
}

async fn publish_summary_row(
    txn: &mut Transaction<'_, Postgres>,
    id: &str,
    zone: &str,
    supplied: &Value,
) -> Result<bool, sqlx::Error> {
    let result = sqlx::query(
        r#"INSERT INTO activities (id, kind, name, start, "end", timezone, duration_seconds,
             moving_seconds, distance_m, avg_hr, max_hr, avg_speed_mps, calories, cadence,
             ascent_m, descent_m, aerobic_effect, anaerobic_effect, training_load, details)
         SELECT $1, kind, name, start, "end", timezone, duration_seconds,
             moving_seconds, distance_m, avg_hr, max_hr, avg_speed_mps, calories, cadence,
             ascent_m, descent_m, aerobic_effect, anaerobic_effect, training_load, '{}'::jsonb
         FROM jsonb_populate_record(NULL::activities,
             jsonb_build_object('kind','unknown','timezone',$2::text) ||
             coalesce((SELECT to_jsonb(existing) FROM activities AS existing WHERE id = $1), '{}'::jsonb) ||
             $3::jsonb)
         ON CONFLICT (id) DO UPDATE SET
             kind=excluded.kind, name=excluded.name, start=excluded.start, "end"=excluded."end",
             timezone=excluded.timezone, duration_seconds=excluded.duration_seconds,
             moving_seconds=excluded.moving_seconds, distance_m=excluded.distance_m,
             avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, avg_speed_mps=excluded.avg_speed_mps,
             calories=excluded.calories, cadence=excluded.cadence, ascent_m=excluded.ascent_m,
             descent_m=excluded.descent_m, aerobic_effect=excluded.aerobic_effect,
             anaerobic_effect=excluded.anaerobic_effect, training_load=excluded.training_load, updated_at=now()
         WHERE (activities.kind, activities.name, activities.start, activities."end",
             activities.timezone, activities.duration_seconds, activities.moving_seconds,
             activities.distance_m, activities.avg_hr, activities.max_hr, activities.avg_speed_mps,
             activities.calories, activities.cadence, activities.ascent_m, activities.descent_m,
             activities.aerobic_effect, activities.anaerobic_effect, activities.training_load)
         IS DISTINCT FROM (excluded.kind, excluded.name, excluded.start, excluded."end",
             excluded.timezone, excluded.duration_seconds, excluded.moving_seconds,
             excluded.distance_m, excluded.avg_hr, excluded.max_hr, excluded.avg_speed_mps,
             excluded.calories, excluded.cadence, excluded.ascent_m, excluded.descent_m,
             excluded.aerobic_effect, excluded.anaerobic_effect, excluded.training_load)"#,
    )
    .bind(id)
    .bind(zone)
    .bind(supplied.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(result.rows_affected() != 0)
}

pub(crate) async fn supersede_insights(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE insights SET status = 'superseded'
         WHERE status IN ('candidate', 'accepted', 'delivered', 'uncertain')",
    )
    .execute(&mut **txn)
    .await?;
    Ok(())
}

pub(crate) async fn invalidate_replay_outputs(
    txn: &mut Transaction<'_, Postgres>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "WITH chats AS (
             UPDATE conversations SET memory_epoch = gen_random_uuid(),
                 state = state - 'analysis_turns', updated_at = now()
         ), pending AS (
             DELETE FROM app_state WHERE key LIKE 'analysis:conversation:pending%'
         ), legacy AS (
             UPDATE app_state SET value = value || '{\"turns\":[]}'::jsonb,
                 updated_at = now() WHERE key = 'analysis:conversation'
         ), prompts AS (
             UPDATE pending_questions SET status = 'cancelled' WHERE kind = 'context'
                 AND status IN ('pending', 'sending', 'sent', 'uncertain')
         )
         INSERT INTO app_state (key, value) VALUES ('replay:generation', $1::jsonb)
         ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()",
    )
    .bind(json!({"id": Uuid::new_v4().to_string()}).to_string())
    .execute(&mut **txn)
    .await?;
    supersede_insights(txn).await
}
