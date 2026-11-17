mod history_window;

use std::{error::Error, fmt};

use ga_domain::{
    event_input::known_timezone, garmin_health::HealthReplacement, timestamp::StoredInstant,
};
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::{Date, format_description::well_known::Iso8601};
use uuid::Uuid;

use crate::{
    Database, ReadinessError,
    activity_documents::{
        ActivityDocument, ActivitySummaryKind, ApplyActivity, DocumentError, DocumentStatus,
        Projection,
    },
    check_readiness,
    garmin_freshness::record_freshness,
    jobs::{FailureReason, GarminJobKind, GarminJobLease, JobOutcome, JobStoreError},
    raw_capture::{
        CaptureError, GARMIN_LEGACY_SOURCE, authorize_legacy_mutation, lock_legacy_scope,
        required_packs,
    },
};

#[derive(Debug, Eq, PartialEq)]
pub enum EndpointPreflight {
    Fetch {
        endpoint: String,
        source_key: String,
    },
    CollectionDisabled,
    Superseded,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EndpointPublication {
    Published(DocumentStatus),
    Retry,
    CollectionDisabled,
    Superseded,
}

pub struct PublishEndpoint<'a> {
    pub authenticated_fingerprint: &'a str,
    pub lease: &'a GarminJobLease,
    pub source_id: Uuid,
    pub requested_at: StoredInstant,
    pub timezone: &'a str,
    pub replacement: Option<&'a HealthReplacement>,
}

#[derive(Debug)]
pub enum EndpointError {
    Storage(ReadinessError),
    Capture(CaptureError),
    Job(JobStoreError),
    Projection(DocumentError),
    InvalidScope,
    Database,
}

impl fmt::Display for EndpointError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Storage(_) => "Garmin endpoint storage is unavailable",
            Self::Capture(_) => "Garmin endpoint access could not be verified",
            Self::Job(_) => "Garmin endpoint job lease is unavailable",
            Self::Projection(_) => "Garmin endpoint projection could not be applied",
            Self::InvalidScope => "Garmin endpoint job scope is invalid",
            Self::Database => "Garmin endpoint state could not be saved",
        })
    }
}
impl Error for EndpointError {}
impl From<sqlx::Error> for EndpointError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}
impl From<JobStoreError> for EndpointError {
    fn from(error: JobStoreError) -> Self {
        Self::Job(error)
    }
}

struct Endpoint {
    name: String,
    key: String,
    window: Option<String>,
    packs: &'static [&'static str],
}

impl Endpoint {
    fn from_lease(lease: &GarminJobLease, account: &str) -> Result<Self, EndpointError> {
        let payload: Value =
            serde_json::from_str(lease.payload_json()).map_err(|_| EndpointError::InvalidScope)?;
        if lease.kind() != GarminJobKind::Endpoint
            || payload.get("account").is_some_and(|saved| {
                !saved.is_null() && saved.as_str() != Some("") && saved.as_str() != Some(account)
            })
        {
            return Err(EndpointError::InvalidScope);
        }
        let name = payload["endpoint"]
            .as_str()
            .ok_or(EndpointError::InvalidScope)?;
        let packs = required_packs(name)
            .filter(|_| name != "activities")
            .ok_or(EndpointError::InvalidScope)?;
        let key = payload["key"]
            .as_str()
            .filter(|key| !key.is_empty())
            .ok_or(EndpointError::InvalidScope)?;
        let activity = name == "activity" || name.starts_with("activity_");
        if !(activity
            || name == "devices" && key == "global"
            || name != "devices"
                && Date::parse(key, &Iso8601::DATE)
                    .is_ok_and(|day| (1..=9999).contains(&day.year())))
        {
            return Err(EndpointError::InvalidScope);
        }
        let window = match payload.get("sync_window") {
            None | Some(Value::Null) => None,
            Some(Value::String(value)) if value.is_empty() => None,
            Some(Value::String(value))
                if !activity
                    && name != "devices"
                    && *value == format!("syncwindow:{account}:{name}:{key}") =>
            {
                Some(value.clone())
            }
            _ => return Err(EndpointError::InvalidScope),
        };
        Ok(Self {
            name: name.to_owned(),
            key: key.to_owned(),
            window,
            packs,
        })
    }

    fn scope(&self) -> String {
        format!("{GARMIN_LEGACY_SOURCE}:{}:{}", self.name, self.key)
    }

    fn projection<'a>(&'a self, replacement: Option<&'a HealthReplacement>) -> Projection<'a> {
        let document = match self.name.as_str() {
            "activity_details" => ActivityDocument::Details,
            "activity_splits" => ActivityDocument::Splits,
            "activity_typed_splits" => ActivityDocument::TypedSplits,
            "activity_zones" => ActivityDocument::Zones,
            "activity_weather" => ActivityDocument::Weather,
            "activity" => return Projection::Summary(ActivitySummaryKind::Single),
            _ => return Projection::Health(&self.name, replacement),
        };
        Projection::Document(document)
    }
}

#[derive(Clone, Copy)]
enum Eligibility {
    Work,
    Disabled,
    Superseded,
}

impl Database {
    /// Verify the saved job, owner and permitted endpoint before a remote read.
    pub async fn preflight_garmin_endpoint(
        &self,
        account: &str,
        lease: &GarminJobLease,
        at: StoredInstant,
    ) -> Result<EndpointPreflight, EndpointError> {
        let endpoint = Endpoint::from_lease(lease, account)?;
        let (txn, eligibility) = self.begin_endpoint(&endpoint, account, lease, at).await?;
        let result = match eligibility {
            Eligibility::Work => EndpointPreflight::Fetch {
                endpoint: endpoint.name,
                source_key: endpoint.key,
            },
            Eligibility::Disabled => EndpointPreflight::CollectionDisabled,
            Eligibility::Superseded => EndpointPreflight::Superseded,
        };
        txn.commit().await?;
        Ok(result)
    }

    /// Publish captured data, history progress and the current lease outcome atomically.
    pub async fn publish_garmin_endpoint(
        &self,
        request: PublishEndpoint<'_>,
    ) -> Result<EndpointPublication, EndpointError> {
        let endpoint = Endpoint::from_lease(request.lease, request.authenticated_fingerprint)?;
        if !known_timezone(request.timezone)
            || request
                .replacement
                .is_some_and(|replacement| replacement.validate_endpoint(&endpoint.name).is_err())
        {
            return Err(EndpointError::InvalidScope);
        }
        let (mut txn, eligibility) = self
            .begin_endpoint(
                &endpoint,
                request.authenticated_fingerprint,
                request.lease,
                request.requested_at,
            )
            .await?;
        let result = match eligibility {
            Eligibility::Disabled => EndpointPublication::CollectionDisabled,
            Eligibility::Superseded => EndpointPublication::Superseded,
            Eligibility::Work => publish(&mut txn, &endpoint, &request).await?,
        };
        txn.commit().await?;
        Ok(result)
    }

    /// Record a transport failure against the live lease and preserve prior successful data.
    pub async fn finish_garmin_endpoint_fetch_error(
        &self,
        account: &str,
        lease: &GarminJobLease,
        at: StoredInstant,
        reason: FailureReason,
        retry_at: Option<StoredInstant>,
    ) -> Result<(), EndpointError> {
        let endpoint = Endpoint::from_lease(lease, account)?;
        let (mut txn, eligibility) = self.begin_endpoint(&endpoint, account, lease, at).await?;
        if let Eligibility::Work = eligibility {
            record_freshness(
                &mut txn,
                &endpoint.name,
                &endpoint.key,
                "fetch_error",
                at,
                None,
            )
            .await?;
            lease
                .finish_in(&mut txn, JobOutcome::Failed { reason, retry_at })
                .await?;
        }
        txn.commit().await?;
        Ok(())
    }

    async fn begin_endpoint(
        &self,
        endpoint: &Endpoint,
        account: &str,
        lease: &GarminJobLease,
        at: StoredInstant,
    ) -> Result<(Transaction<'static, Postgres>, Eligibility), EndpointError> {
        let mut txn = self.work.begin().await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(EndpointError::Storage)?;
        let enabled = match authorize_legacy_mutation(&mut txn, account, endpoint.packs).await {
            Ok(()) => true,
            Err(CaptureError::CollectionDisabled) => false,
            Err(error) => return Err(EndpointError::Capture(error)),
        };
        lease.check_live(&mut txn).await?;
        let eligibility = if history_window::superseded(&mut txn, endpoint, lease.id()).await? {
            Eligibility::Superseded
        } else if !enabled {
            history_window::disable(&mut txn, endpoint, at).await?;
            Eligibility::Disabled
        } else {
            lock_legacy_scope(&mut txn, &endpoint.scope())
                .await
                .map_err(EndpointError::Capture)?;
            Eligibility::Work
        };
        if !matches!(eligibility, Eligibility::Work) {
            lease.finish_in(&mut txn, JobOutcome::Completed).await?;
        }
        Ok((txn, eligibility))
    }
}

async fn publish(
    txn: &mut Transaction<'_, Postgres>,
    endpoint: &Endpoint,
    request: &PublishEndpoint<'_>,
) -> Result<EndpointPublication, EndpointError> {
    let status = Database::apply_activity_in(
        txn,
        ApplyActivity {
            account_fingerprint: request.authenticated_fingerprint,
            source_key: &endpoint.key,
            projection: endpoint.projection(request.replacement),
            source_id: request.source_id,
            requested_at: request.requested_at,
            timezone: request.timezone,
        },
    )
    .await
    .map_err(EndpointError::Projection)?;

    if status != DocumentStatus::Error {
        history_window::complete(
            txn,
            endpoint,
            status,
            request.source_id,
            request.requested_at,
        )
        .await?;
    }
    if status != DocumentStatus::Stale {
        record_freshness(
            txn,
            &endpoint.name,
            &endpoint.key,
            status.label(),
            request.requested_at,
            Some(request.source_id),
        )
        .await?;
    }
    let (job, result) = if status == DocumentStatus::Error {
        (
            JobOutcome::Failed {
                reason: FailureReason::InvalidGarminWork,
                retry_at: None,
            },
            EndpointPublication::Retry,
        )
    } else {
        (
            JobOutcome::Completed,
            EndpointPublication::Published(status),
        )
    };
    request.lease.finish_in(txn, job).await?;
    Ok(result)
}
