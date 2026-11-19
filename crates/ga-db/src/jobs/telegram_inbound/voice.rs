use ga_domain::{
    channel::{InboundEnvelope, InboundError, InboundKind},
    event_input::EventSource,
    model_consent::ModelCategories,
    timestamp::StoredInstant,
};
use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;

use super::{
    TelegramDiaryCommitError, TelegramInboundLease, TelegramInboundState, history::prompt,
};
use crate::{
    Database,
    conversation_memory::ConversationBinding,
    jobs::{JobStoreError, begin},
    model_gate::onboarding::onboarding_allows_model,
    share_policy::{self, DestinationKind, ShareDestination, ShareRequirement},
};

pub struct TelegramVoice {
    pub file_id: String,
    pub media_type: String,
    pub declared_bytes: Option<u64>,
    pub duration_seconds: Option<u64>,
}

impl TelegramInboundLease {
    /// Keep the authenticated provider envelope intact for the lease fence.
    /// Its processing text can include the transcript saved for this job.
    pub fn processing_envelope(&self) -> Result<InboundEnvelope, InboundError> {
        let envelope = InboundEnvelope::parse(self.envelope_json().as_bytes())?;
        if let Some(text) = &self.voice_text {
            let mut document = envelope.document();
            document["text"] = Value::String(text.clone());
            return InboundEnvelope::parse(document.to_string().as_bytes());
        }
        Ok(envelope)
    }

    pub fn has_voice_text(&self) -> bool {
        self.voice_text.is_some()
    }

    pub fn diary_source(&self) -> EventSource {
        if self.has_voice_text() {
            EventSource::TelegramVoice
        } else {
            EventSource::TelegramText
        }
    }

    pub fn voice(&self) -> Result<Option<TelegramVoice>, TelegramDiaryCommitError> {
        let envelope = InboundEnvelope::parse(self.envelope_json().as_bytes())
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        if envelope.kind() != InboundKind::Voice || self.has_voice_text() {
            return Ok(None);
        }
        let raw = envelope.document();
        let attachment = &raw["attachments"][0];
        if attachment["kind"] != "voice" {
            return Err(TelegramDiaryCommitError::UnsupportedMessage);
        }
        Ok(Some(TelegramVoice {
            file_id: attachment["external_id"]
                .as_str()
                .ok_or(TelegramDiaryCommitError::UnsupportedMessage)?
                .to_owned(),
            media_type: attachment["media_type"]
                .as_str()
                .unwrap_or("audio/ogg")
                .to_owned(),
            declared_bytes: attachment["size_bytes"].as_u64(),
            duration_seconds: self.voice_duration_seconds,
        }))
    }
}

impl Database {
    /// Resolve tracker privacy before downloading or sending audio. The
    /// model gateway also checks the resulting grants at the send boundary.
    pub async fn telegram_voice_sharing(
        &self,
        lease: &TelegramInboundLease,
        model: ShareDestination<'_>,
    ) -> Result<Option<Vec<ShareRequirement<'static>>>, TelegramDiaryCommitError> {
        let envelope = lease
            .processing_envelope()
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        let mut txn = self.begin_telegram_text(lease, &envelope).await?;
        super::local_text::verify_order(&mut txn, lease).await?;
        let requirements = allowed(&mut txn, lease, &envelope, model).await?;
        txn.commit().await?;
        Ok(requirements)
    }

    /// Save the transcript and release the lease to the usual local screening
    /// and diary workers in the same transaction.
    pub async fn store_telegram_voice_text(
        &self,
        lease: &TelegramInboundLease,
        transcript: &str,
        model: ShareDestination<'_>,
        expected: &[ShareRequirement<'_>],
    ) -> Result<bool, TelegramDiaryCommitError> {
        let envelope = lease
            .processing_envelope()
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        if transcript.contains('\0') {
            return Err(TelegramDiaryCommitError::UnsupportedMessage);
        }
        let mut text = transcript.to_owned();
        if let Some(caption) = envelope.text().filter(|text| !text.is_empty()) {
            if !text.is_empty() {
                text.push('\n');
            }
            text.push_str(caption);
        }
        if text.chars().take(100_001).count() > 100_000 {
            return Err(TelegramDiaryCommitError::UnsupportedMessage);
        }
        let mut txn = self.begin_telegram_text(lease, &envelope).await?;
        super::local_text::verify_order(&mut txn, lease).await?;
        if allowed(&mut txn, lease, &envelope, model).await?.as_deref() != Some(expected) {
            return Ok(false);
        }
        let changed = sqlx::query(
            "UPDATE jobs SET status='pending', lease_token=NULL, lease_until=NULL, attempts=attempts-1,
                 payload=(payload - 'local_safety_checked')
                   || jsonb_build_object('voice_transcribed', true, 'tracker_caption_checked', true,
                       'voice_model_instance',$3::text,'voice_tracker_version',$4::text)
             WHERE id=$1 AND lease_token=$2 AND status='running'
               AND lease_until>clock_timestamp() AND payload->>'voice_transcribed' IS DISTINCT FROM 'true'",
        )
        .bind(lease.job_identity().0)
        .bind(lease.job_identity().1)
        .bind(model.instance())
        .bind(expected.first().map(|requirement| requirement.version_id.to_string()))
        .execute(&mut *txn)
        .await?;
        if changed.rows_affected() != 1 {
            return Err(JobStoreError::LeaseLost.into());
        }
        sqlx::query("UPDATE inbound_messages SET normalized_text=$2 WHERE id=$1")
            .bind(lease.message_id())
            .bind(text)
            .execute(&mut *txn)
            .await?;
        txn.commit().await?;
        Ok(true)
    }

    pub async fn finish_telegram_voice_notice(
        &self,
        lease: &TelegramInboundLease,
        configured_locale: &str,
        state: TelegramInboundState,
        message: impl FnOnce(&str) -> &'static str + Send,
    ) -> Result<(), TelegramDiaryCommitError> {
        let envelope = InboundEnvelope::parse(lease.envelope_json().as_bytes())
            .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
        if envelope.kind() != InboundKind::Voice {
            return Err(TelegramDiaryCommitError::UnsupportedMessage);
        }
        let mut txn = begin(&self.work).await?;
        Self::verify_telegram_binding_in(&mut txn, lease, &envelope).await?;
        super::local_text::verify_order(&mut txn, lease).await?;
        let binding = ConversationBinding {
            owner_id: envelope.owner_id(),
            conversation_id: envelope.conversation_id(),
        };
        let locale =
            super::local_text::preferred_locale(&mut txn, binding, configured_locale).await?;
        super::model_reply::queue_local_notice(&mut txn, lease, &envelope, message(&locale), state)
            .await?;
        txn.commit().await?;
        Ok(())
    }

    pub async fn finish_unavailable_telegram_voice(
        &self,
        lease: &TelegramInboundLease,
    ) -> Result<(), TelegramDiaryCommitError> {
        self.finish_telegram_voice_notice(
            lease,
            "",
            TelegramInboundState::Processed,
            |_| "Голосовые сообщения сейчас недоступны. Показатели доступны через /today, записи — через кнопки.",
        ).await
    }
}

pub(super) async fn require_current_transcript(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
) -> Result<(), TelegramDiaryCommitError> {
    let envelope = InboundEnvelope::parse(lease.envelope_json().as_bytes())
        .map_err(|_| TelegramDiaryCommitError::UnsupportedMessage)?;
    let (model, version): (Option<String>, Option<String>) = sqlx::query_as(
        "SELECT payload->>'voice_model_instance',payload->>'voice_tracker_version' FROM jobs WHERE id=$1"
    ).bind(lease.job_identity().0).fetch_one(&mut **txn).await?;
    let model = model
        .as_deref()
        .and_then(|id| ShareDestination::new(DestinationKind::Model, id).ok())
        .ok_or(TelegramDiaryCommitError::VoiceConsentChanged)?;
    let requirements = allowed(txn, lease, &envelope, model).await?;
    if requirements.is_some_and(|requirements| {
        requirements
            .first()
            .map(|requirement| requirement.version_id.to_string())
            == version
    }) {
        Ok(())
    } else {
        Err(TelegramDiaryCommitError::VoiceConsentChanged)
    }
}

async fn allowed(
    txn: &mut Transaction<'_, Postgres>,
    lease: &TelegramInboundLease,
    envelope: &InboundEnvelope,
    model: ShareDestination<'_>,
) -> Result<Option<Vec<ShareRequirement<'static>>>, TelegramDiaryCommitError> {
    if envelope.kind() != InboundKind::Voice
        || envelope
            .text()
            .is_some_and(|text| text.trim_start().starts_with('/'))
    {
        return Err(TelegramDiaryCommitError::UnsupportedMessage);
    }
    if !onboarding_allows_model(txn, ModelCategories::AUDIO)
        .await
        .map_err(|_| TelegramDiaryCommitError::Database)?
    {
        return Ok(None);
    }
    let binding = ConversationBinding {
        owner_id: envelope.owner_id(),
        conversation_id: envelope.conversation_id(),
    };
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **txn)
        .await?;
    let now = StoredInstant::try_from(at).map_err(|_| TelegramDiaryCommitError::Database)?;
    let prompt = prompt::inspect(txn, binding, lease, envelope, now).await?;
    if prompt.notice.is_some()
        || prompt.tracker_selection
        || prompt.draft.is_some_and(|draft| draft.sensitive)
    {
        return Ok(None);
    }
    if let Some(version) = prompt.tracker_version {
        let Ok(version) = version.parse() else {
            return Ok(None);
        };
        let requirement = super::tracker_text::tracker_model_requirement(txn, version).await?;
        if !share_policy::allows(txn, requirement, model)
            .await
            .map_err(|_| TelegramDiaryCommitError::Database)?
        {
            return Ok(None);
        }
        Ok(Some(vec![requirement]))
    } else {
        Ok(Some(Vec::new()))
    }
}
