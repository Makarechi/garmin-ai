use ga_db::jobs::telegram_inbound::{LocalTelegramText, TelegramInboundState};
use ga_domain::model_consent::ModelConsentError;

use super::{
    ActiveModelJob, Disposition, GatewayError, ModelDiaryWorker, Sender, TelegramDiaryCommitError,
    TelegramInboundLease, TelegramTransport, commit_disposition, model_disposition,
    provider_notice, wall_clock,
};
use crate::{model_gateway::ModelInput, telegram_transport::TelegramTransportError};
use ga_db::share_policy::{DestinationKind, ShareDestination};
use ga_domain::timestamp::StoredInstant;

enum VoiceNotice {
    Disabled,
    Changed,
    Oversized,
    Unsupported,
    Empty,
}

impl VoiceNotice {
    fn text(&self, locale: &str) -> &'static str {
        let russian = locale
            .split('-')
            .next()
            .is_some_and(|code| code.eq_ignore_ascii_case("ru"));
        match (self, russian) {
            (Self::Disabled, _) => {
                "Голосовые сообщения сейчас недоступны. Показатели доступны через /today, записи — через кнопки."
            }
            (Self::Changed, true) => {
                "Доступ к трекеру изменился. Голос не обработан; отправьте сообщение снова или напишите текст."
            }
            (Self::Changed, false) => {
                "Tracker access changed. Voice was not processed; resend the message or type it."
            }
            (Self::Oversized, true) => {
                "Голосовое сообщение слишком большое. Пришлите запись до 10 минут и 20 МБ или напишите текст."
            }
            (Self::Oversized, false) => {
                "Voice message is too large. Send up to 10 minutes and 20 MB, or type the message."
            }
            (Self::Unsupported, true) => {
                "Эта модель не поддерживает голос. Отправьте сообщение текстом."
            }
            (Self::Unsupported, false) => {
                "This model does not support voice. Send a text message instead."
            }
            (Self::Empty, _) => "Пришлите текст или голосовое сообщение.",
        }
    }

    fn state(&self) -> TelegramInboundState {
        match self {
            Self::Disabled | Self::Empty => TelegramInboundState::Processed,
            Self::Changed | Self::Oversized | Self::Unsupported => TelegramInboundState::Invalid,
        }
    }
}

impl<S: Sender> ModelDiaryWorker<'_, '_, S> {
    pub(super) async fn process_voice(
        &self,
        transport: &TelegramTransport,
        lease: &TelegramInboundLease,
        started: StoredInstant,
    ) {
        match self.transcribe_voice(transport, lease, started).await {
            Ok(None) => {}
            Ok(Some(VoiceNotice::Disabled)) => {
                if let Err(error) = self
                    .database
                    .apply_local_telegram_text(
                        lease,
                        LocalTelegramText::ProviderUnavailable,
                        self.locale,
                    )
                    .await
                {
                    self.finish(lease, self.locale, voice_commit_disposition(error))
                        .await;
                }
            }
            Ok(Some(notice)) => {
                let _ = self
                    .database
                    .finish_telegram_voice_notice(lease, self.locale, notice.state(), |locale| {
                        notice.text(locale)
                    })
                    .await;
            }
            Err(Disposition::Text(key)) => {
                let _ = self
                    .database
                    .finish_telegram_voice_notice(
                        lease,
                        self.locale,
                        TelegramInboundState::Processed,
                        |locale| super::template(locale, key),
                    )
                    .await;
            }
            Err(Disposition::Provider(error)) => {
                let provider = self.gateway.binding().provider;
                let _ = self
                    .database
                    .finish_telegram_voice_notice(
                        lease,
                        self.locale,
                        TelegramInboundState::Processed,
                        |locale| provider_notice(locale, provider, error),
                    )
                    .await;
            }
            Err(disposition) => self.finish(lease, self.locale, disposition).await,
        }
    }

    async fn transcribe_voice(
        &self,
        transport: &TelegramTransport,
        lease: &TelegramInboundLease,
        started: StoredInstant,
    ) -> Result<Option<VoiceNotice>, Disposition> {
        let voice = lease
            .voice()
            .map_err(commit_disposition)?
            .ok_or(Disposition::Gone)?;
        let envelope = lease.processing_envelope().map_err(|_| Disposition::Gone)?;
        let model =
            ShareDestination::new(DestinationKind::Model, self.gateway.binding().instance_id)
                .map_err(|_| Disposition::Gone)?;
        let channel_instance = format!("telegram:{}", envelope.channel().instance_id());
        let channel = ShareDestination::new(DestinationKind::Channel, &channel_instance)
            .map_err(|_| Disposition::Gone)?;
        if let Err(error) = self
            .gateway
            .authorize_transcription(self.configured.current(), started)
        {
            return Ok(Some(match error {
                ModelConsentError::UnsupportedCapability => VoiceNotice::Unsupported,
                _ => VoiceNotice::Disabled,
            }));
        }
        let Some(requirements) = self
            .database
            .telegram_voice_sharing(lease, model)
            .await
            .map_err(voice_commit_disposition)?
        else {
            return Ok(Some(VoiceNotice::Disabled));
        };
        let audio = match transport
            .download_voice(&voice.file_id, voice.duration_seconds, voice.declared_bytes)
            .await
        {
            Ok(audio) => audio,
            Err(TelegramTransportError::ResponseTooLarge) => {
                return Ok(Some(VoiceNotice::Oversized));
            }
            Err(TelegramTransportError::RateLimited(delay)) => {
                return Err(Disposition::Retry(delay.max(super::IDLE)));
            }
            Err(TelegramTransportError::Unavailable) => {
                return Err(Disposition::Retry(std::time::Duration::from_secs(5)));
            }
            Err(_) => return Ok(Some(VoiceNotice::Disabled)),
        };
        let current = self
            .database
            .telegram_voice_sharing(lease, model)
            .await
            .map_err(voice_commit_disposition)?;
        if current.as_deref() != Some(&requirements) {
            return Ok(Some(VoiceNotice::Changed));
        }
        let (id, lease_token) = lease.job_identity();
        let output = match self
            .gateway
            .transcribe_telegram_voice_with(
                ModelInput::Voice {
                    audio: &audio,
                    mime_type: &voice.media_type,
                },
                &|| self.configured.current(),
                &requirements,
                channel,
                ActiveModelJob { id, lease_token },
                self.sender,
            )
            .await
        {
            Ok(output) => output,
            Err(
                GatewayError::Consent(_)
                | GatewayError::Attempt(ga_model::fallback::FallbackError::ConsentRequired),
            ) => {
                return Ok(Some(VoiceNotice::Changed));
            }
            Err(error) => return Err(model_disposition(error)),
        };
        drop(audio);
        let text = output.value["text"]
            .as_str()
            .ok_or(Disposition::Text("agent.answer_unverified"))?;
        if text.trim().is_empty()
            && envelope
                .text()
                .is_none_or(|caption| caption.trim().is_empty())
        {
            return Ok(Some(VoiceNotice::Empty));
        }
        if self
            .gateway
            .authorize_transcription(self.configured.current(), wall_clock().unwrap_or(started))
            .is_err()
            || !self
                .database
                .store_telegram_voice_text(lease, text, model, &requirements)
                .await
                .map_err(voice_commit_disposition)?
        {
            return Ok(Some(VoiceNotice::Changed));
        }
        Ok(None)
    }
}

fn voice_commit_disposition(error: TelegramDiaryCommitError) -> Disposition {
    match error {
        TelegramDiaryCommitError::Database => Disposition::Retry(super::IDLE),
        error => commit_disposition(error),
    }
}
