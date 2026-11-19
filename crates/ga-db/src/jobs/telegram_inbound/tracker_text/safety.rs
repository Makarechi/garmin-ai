use uuid::Uuid;

use super::super::{TelegramDiaryCommitError, TelegramInboundLease};
use super::reply::{legacy_pending, selected_version};
use crate::{
    Database,
    share_policy::{self, ShareDestination, ShareRequirement},
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TrackerSafetyStatus {
    Checked,
    Urgent,
    Unavailable,
}

impl TrackerSafetyStatus {
    pub fn from_stored(value: &str) -> Option<Self> {
        match value {
            "checked" => Some(Self::Checked),
            "urgent" => Some(Self::Urgent),
            "unavailable" => Some(Self::Unavailable),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Checked => "checked",
            Self::Urgent => "urgent",
            Self::Unavailable => "unavailable",
        }
    }
}

impl Database {
    pub async fn cached_telegram_tracker_safety(
        &self,
        lease: &TelegramInboundLease,
        requirement: ShareRequirement<'_>,
        destination: ShareDestination<'_>,
    ) -> Result<Option<TrackerSafetyStatus>, TelegramDiaryCommitError> {
        let envelope = super::super::diary::text_envelope(lease)?;
        let mut txn = self.begin_telegram_text(lease, &envelope).await?;
        super::super::local_text::verify_order(&mut txn, lease).await?;
        let pending = legacy_pending(&mut txn, &envelope)
            .await?
            .ok_or(TelegramDiaryCommitError::ModelContextChanged)?;
        if selected_version(&pending)? != requirement.version_id {
            return Err(TelegramDiaryCommitError::ModelContextChanged);
        }
        let allowed = share_policy::allows(&mut txn, requirement, destination)
            .await
            .map_err(|_| TelegramDiaryCommitError::Database)?;
        let stored: Option<String> =
            sqlx::query_scalar("SELECT payload->>'form_safety' FROM jobs WHERE id=$1")
                .bind(lease.job_identity().0)
                .fetch_one(&mut *txn)
                .await?;
        txn.commit().await?;
        Ok(if allowed {
            stored.as_deref().and_then(TrackerSafetyStatus::from_stored)
        } else {
            Some(TrackerSafetyStatus::Unavailable)
        })
    }

    pub async fn record_telegram_tracker_safety(
        &self,
        lease: &TelegramInboundLease,
        version: Uuid,
        status: TrackerSafetyStatus,
    ) -> Result<(), TelegramDiaryCommitError> {
        let envelope = super::super::diary::text_envelope(lease)?;
        let mut txn = self.begin_telegram_text(lease, &envelope).await?;
        super::super::local_text::verify_order(&mut txn, lease).await?;
        let pending = legacy_pending(&mut txn, &envelope)
            .await?
            .ok_or(TelegramDiaryCommitError::ModelContextChanged)?;
        if selected_version(&pending)? != version {
            return Err(TelegramDiaryCommitError::ModelContextChanged);
        }
        let changed = sqlx::query(
            "UPDATE jobs SET payload=jsonb_set(payload,'{form_safety}',to_jsonb($3::text))
            WHERE id=$1 AND lease_token=$2 AND status='running' AND lease_until>clock_timestamp()",
        )
        .bind(lease.job_identity().0)
        .bind(lease.job_identity().1)
        .bind(status.as_str())
        .execute(&mut *txn)
        .await?;
        if changed.rows_affected() != 1 {
            return Err(crate::jobs::JobStoreError::LeaseLost.into());
        }
        txn.commit().await?;
        Ok(())
    }
}
