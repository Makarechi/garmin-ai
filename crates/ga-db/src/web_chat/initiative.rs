use serde_json::Value;
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{Conversation, WebChatError, timestamp};
use crate::{
    outbox_claims::ReplyDestination,
    proactive_control::{TrackerCheckinDecision, TrackerCheckinFence, revalidate_tracker_checkin},
};

/// Validate a due check-in at display and again when the browser acknowledges it.
pub(super) async fn can_show_checkin(
    txn: &mut Transaction<'_, Postgres>,
    conversation: Conversation,
    outbox: Uuid,
) -> Result<bool, WebChatError> {
    type CheckinRow = (String, String, Option<OffsetDateTime>, OffsetDateTime);
    let row: Option<CheckinRow> = sqlx::query_as(
        "SELECT dedup_key,intent::text,next_attempt_at,clock_timestamp() FROM outbox_messages
         WHERE id=$1 AND owner_id=$2 AND conversation_id=$3 AND state='queued'
           AND starts_with(dedup_key,'rule:') AND intent->'initiative'='true'::jsonb",
    )
    .bind(outbox)
    .bind(conversation.owner)
    .bind(conversation.id)
    .fetch_optional(&mut **txn)
    .await?;
    let Some((key, raw, retry, at)) = row else {
        return Ok(false);
    };
    if retry.is_some_and(|retry| retry > at) {
        return Ok(false);
    }
    let mut intent: Value = serde_json::from_str(&raw).map_err(|_| WebChatError::Database)?;
    let expiry = intent["expires_at"].clone();
    let decision = revalidate_tracker_checkin(
        txn,
        &ReplyDestination {
            owner_id: conversation.owner,
            channel: "web",
            instance_id: "local",
        },
        outbox,
        &key,
        &mut intent,
        timestamp(at)?,
        TrackerCheckinFence::Browser,
    )
    .await
    .map_err(|_| WebChatError::Database)?;
    use TrackerCheckinDecision::{Cancel, Defer, Expire, Ready};
    let (state, retry) = match decision {
        Cancel => ("cancelled", None),
        Expire => ("expired", None),
        Defer(at) => ("queued", Some(OffsetDateTime::from(at))),
        Ready => ("queued", None),
    };
    if state != "queued" || retry.is_some() || expiry != intent["expires_at"] {
        sqlx::query(
            "UPDATE outbox_messages SET state=$2,next_attempt_at=$3,intent=$4::jsonb,updated_at=now()
             WHERE id=$1 AND state='queued'",
        )
        .bind(outbox)
        .bind(state)
        .bind(retry)
        .bind(intent.to_string())
        .execute(&mut **txn)
        .await?;
    }
    Ok(matches!(decision, Ready))
}
