use std::time::Duration;

use ga_domain::{access::Scope, timestamp::StoredInstant, tracker_nl::TrackerExtraction};
use serde_json::{Value, json};
use uuid::Uuid;

mod reply;
mod safety;
use super::{TelegramDiaryCommitError, TelegramInboundLease, tracker_form};
use crate::{
    Database,
    diary::actions::{TrackerTextContext, TrackerTextPreparation, TrackerTextSession},
    share_policy::{ShareCategory, ShareDestination, ShareRequirement},
};
use reply::{ReplyPreferences, finish_in, legacy_pending, request, selected_version};
pub(super) use reply::{control_in, fallback_in};
pub use safety::TrackerSafetyStatus;

pub enum TelegramTrackerTextPreparation {
    Complete,
    Model(Box<TelegramTrackerTextSession>),
}

/// The selected legacy prompt is checked again in the write transaction after model work.
pub struct TelegramTrackerTextSession {
    inner: TrackerTextSession,
    pending: Value,
    timezone: String,
    locale: String,
    version: Uuid,
}

impl TelegramTrackerTextSession {
    pub fn prompt(&self) -> Result<String, crate::diary::actions::TrackerTextError> {
        self.inner.prompt()
    }

    pub fn sharing_requirements(
        &self,
    ) -> Result<Vec<ShareRequirement<'static>>, crate::diary::actions::TrackerTextError> {
        self.inner.sharing_requirements()
    }

    pub fn channel_requirement(&self) -> ShareRequirement<'static> {
        ShareRequirement {
            version_id: self.version,
            categories: &[ShareCategory::Schema],
        }
    }

    pub async fn finish(
        mut self,
        database: &Database,
        lease: &TelegramInboundLease,
        extraction: Option<TrackerExtraction>,
        safety_unavailable: bool,
    ) -> Result<(), TelegramDiaryCommitError> {
        tokio::time::timeout(Duration::from_millis(2500), async {
            let envelope = super::diary::text_envelope(lease)?;
            Database::verify_telegram_text_in(&mut self.inner.txn, lease, &envelope).await?;
            super::local_text::verify_order(&mut self.inner.txn, lease).await?;
            let current = legacy_pending(&mut self.inner.txn, &envelope).await?;
            if current.as_ref() != Some(&self.pending) {
                return Err(TelegramDiaryCommitError::ModelContextChanged);
            }
            let result =
                if tracker_form::allowed(&mut self.inner.txn, &envelope, self.version, false)
                    .await?
                {
                    if let Some(extraction) = extraction {
                        self.inner.source = "telegram_text";
                        match self.inner.apply_uncommitted(extraction).await {
                            Ok(value) => value,
                            Err(
                                crate::diary::actions::TrackerTextError::Invalid
                                | crate::diary::actions::TrackerTextError::Missing,
                            ) => {
                                self.inner.txn.rollback().await?;
                                let mut txn =
                                    database.begin_telegram_text(lease, &envelope).await?;
                                if legacy_pending(&mut txn, &envelope).await?.as_ref()
                                    != Some(&self.pending)
                                {
                                    return Err(TelegramDiaryCommitError::ModelContextChanged);
                                }
                                finish_in(
                                    &mut txn,
                                    lease,
                                    &envelope,
                                    self.pending,
                                    &json!({"invalid":true}),
                                    ReplyPreferences {
                                        timezone: &self.timezone,
                                        locale: &self.locale,
                                        safety_unavailable: false,
                                    },
                                )
                                .await?;
                                txn.commit().await?;
                                return Ok(());
                            }
                            Err(_) => return Err(TelegramDiaryCommitError::Database),
                        }
                    } else {
                        self.inner.fallback.clone()
                    }
                } else {
                    json!({"channel_denied":true})
                };
            finish_in(
                &mut self.inner.txn,
                lease,
                &envelope,
                self.pending,
                &result,
                ReplyPreferences {
                    timezone: &self.timezone,
                    locale: &self.locale,
                    safety_unavailable,
                },
            )
            .await?;
            self.inner.txn.commit().await?;
            Ok(())
        })
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?
    }
}

impl Database {
    pub async fn telegram_tracker_model_requirement(
        &self,
        lease: &TelegramInboundLease,
    ) -> Result<ShareRequirement<'static>, TelegramDiaryCommitError> {
        let envelope = super::diary::text_envelope(lease)?;
        let mut txn = self.begin_telegram_text(lease, &envelope).await?;
        super::local_text::verify_order(&mut txn, lease).await?;
        let pending = legacy_pending(&mut txn, &envelope)
            .await?
            .ok_or(TelegramDiaryCommitError::ModelContextChanged)?;
        let version_id = selected_version(&pending)?;
        let sensitive: bool = sqlx::query_scalar(
            "SELECT coalesce((SELECT privacy='sensitive' FROM event_definition_versions WHERE id=$1),false)",
        ).bind(version_id).fetch_one(&mut *txn).await?;
        txn.commit().await?;
        Ok(ShareRequirement {
            version_id,
            categories: if sensitive {
                &[
                    ShareCategory::Schema,
                    ShareCategory::Facts,
                    ShareCategory::OriginalText,
                ]
            } else {
                &[ShareCategory::Schema, ShareCategory::Facts]
            },
        })
    }

    pub async fn prepare_telegram_tracker_text(
        &self,
        lease: &TelegramInboundLease,
        timezone: &str,
        locale: &str,
        destination: ShareDestination<'_>,
        safety_unavailable: bool,
    ) -> Result<TelegramTrackerTextPreparation, TelegramDiaryCommitError> {
        let envelope = super::diary::text_envelope(lease)?;
        let mut txn = self.begin_telegram_text(lease, &envelope).await?;
        super::local_text::verify_order(&mut txn, lease).await?;
        let pending = legacy_pending(&mut txn, &envelope)
            .await?
            .ok_or(TelegramDiaryCommitError::ModelContextChanged)?;
        let version = selected_version(&pending)?;
        if !tracker_form::allowed(&mut txn, &envelope, version, false).await? {
            finish_in(
                &mut txn,
                lease,
                &envelope,
                pending,
                &json!({"channel_denied":true}),
                ReplyPreferences {
                    timezone,
                    locale,
                    safety_unavailable,
                },
            )
            .await?;
            txn.commit().await?;
            return Ok(TelegramTrackerTextPreparation::Complete);
        }
        txn.commit().await?;
        let request = request(&envelope, version)?;
        let actor = format!("telegram:{}", envelope.sender_ref());
        let message_at =
            StoredInstant::try_from(envelope.occurred_at().unwrap_or(envelope.received_at()))
                .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        let prepared = self
            .prepare_tracker_text(
                &request,
                TrackerTextContext {
                    owner: envelope.owner_id(),
                    scopes: [Scope::ReadDiary, Scope::WriteDiary].into_iter().collect(),
                    actor: &actor,
                    message_at,
                    configured_timezone: timezone,
                    configured_locale: locale,
                },
                Some(destination),
            )
            .await;
        match prepared {
            Ok(TrackerTextPreparation::Model(inner)) => Ok(TelegramTrackerTextPreparation::Model(
                Box::new(TelegramTrackerTextSession {
                    inner: *inner,
                    pending,
                    timezone: timezone.to_owned(),
                    locale: locale.to_owned(),
                    version,
                }),
            )),
            result => {
                let mut txn = self.begin_telegram_text(lease, &envelope).await?;
                if legacy_pending(&mut txn, &envelope).await?.as_ref() != Some(&pending) {
                    return Err(TelegramDiaryCommitError::ModelContextChanged);
                }
                let result = match result {
                    Ok(TrackerTextPreparation::Complete(value)) => value,
                    Err(
                        crate::diary::actions::TrackerTextError::Missing
                        | crate::diary::actions::TrackerTextError::Invalid,
                    ) => json!({"invalid":true}),
                    _ => return Err(TelegramDiaryCommitError::Database),
                };
                finish_in(
                    &mut txn,
                    lease,
                    &envelope,
                    pending,
                    &result,
                    ReplyPreferences {
                        timezone,
                        locale,
                        safety_unavailable,
                    },
                )
                .await?;
                txn.commit().await?;
                Ok(TelegramTrackerTextPreparation::Complete)
            }
        }
    }
}
