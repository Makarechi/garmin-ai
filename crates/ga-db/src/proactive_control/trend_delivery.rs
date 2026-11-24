use std::future::Future;

use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{Connection, PgConnection, Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    DiaryQuestionOutcome, NotificationDecision, NotificationExclusions, NotificationPolicy,
    ProactiveControlError,
    policy::decision_in,
    question_delivery::{
        PROVIDER_DEADLINE, STORAGE_DEADLINE, prepare_storage, telegram_notification_connection,
        write_marker,
    },
};
use crate::{
    Database,
    channel_ingress::telegram_poll::telegram_primary,
    garmin_integration::garmin_pause,
    health_analysis::{owner_timezone, stored_trend_current},
    outbox_claims::ReplyDestination,
    replay::{GARMIN_PARSER_VERSION, projection_pending},
    telegram_binding::{TelegramBindingError, confirmed_pairing},
};

struct Publication {
    id: Uuid,
    token: Uuid,
    key: String,
    reservation: String,
    text: String,
}

impl Database {
    /// Send one currently eligible trend, retaining the legacy outbox and weekly budget keys.
    pub async fn dispatch_trend_notification<F>(
        &self,
        destination: ReplyDestination<'_>,
        expected_chat: i64,
        settings: &NotificationPolicy<'_>,
        now: StoredInstant,
        send: impl FnOnce(String) -> F,
    ) -> Result<bool, ProactiveControlError>
    where
        F: Future<Output = DiaryQuestionOutcome>,
    {
        if destination.channel != "telegram" {
            return Ok(false);
        }
        let channel = format!("telegram:{}", destination.instance_id);
        let (mut connection, publication) = tokio::time::timeout(STORAGE_DEADLINE, async {
            let mut connection = telegram_notification_connection(self, true).await?;
            let mut txn = connection.begin().await?;
            prepare_storage(&mut txn).await?;
            let publication = prepare(
                &mut txn,
                destination,
                expected_chat,
                settings,
                &channel,
                now,
            )
            .await?;
            txn.commit().await?;
            Ok::<_, ProactiveControlError>((connection, publication))
        })
        .await
        .map_err(|_| ProactiveControlError::Database)??;
        let Some(publication) = publication else {
            return Ok(false);
        };
        let result = tokio::time::timeout(PROVIDER_DEADLINE, send(publication.text.clone()))
            .await
            .unwrap_or(DiaryQuestionOutcome::Uncertain);
        tokio::time::timeout(
            STORAGE_DEADLINE,
            finish(&mut connection, publication, &channel, now, result),
        )
        .await
        .map_err(|_| ProactiveControlError::Database)??;
        Ok(true)
    }
}

async fn prepare(
    txn: &mut Transaction<'_, Postgres>,
    destination: ReplyDestination<'_>,
    expected_chat: i64,
    settings: &NotificationPolicy<'_>,
    channel: &str,
    now: StoredInstant,
) -> Result<Option<Publication>, ProactiveControlError> {
    let paired = confirmed_pairing(txn, destination.instance_id, expected_chat).await;
    if matches!(paired, Err(TelegramBindingError::Busy)) {
        return Err(ProactiveControlError::Busy);
    }
    if !matches!(paired, Ok(owner) if owner == destination.owner_id)
        || !telegram_primary(txn, destination.instance_id).await?
        || garmin_pause(&mut **txn, now)
            .await
            .map_err(|_| ProactiveControlError::InvalidStoredState)?
            .is_some()
    {
        return Ok(None);
    }
    let rows = sqlx::query_as::<_, (Uuid, String, String, String, Option<String>)>(
        "SELECT insight.id,split_part(insight.dedup_key,':',2) AS metric,
                insight.statement,insight.evidence::text,last.value::text
           FROM insights insight LEFT JOIN app_state last
             ON last.key=concat('insight:last:',split_part(insight.dedup_key,':',2))
           WHERE insight.category='trend' AND insight.status='accepted'
             AND starts_with(insight.dedup_key,'trend:')
             AND split_part(insight.dedup_key,':',2) IN
                  ('sleep_score','sleep_seconds','hrv_nightly_avg','resting_hr','stress_avg')
             AND (NOT EXISTS (SELECT 1 FROM module_configs WHERE owner_id=$2)
                  OR EXISTS (SELECT 1 FROM module_configs WHERE owner_id=$2
                             AND tracking_enabled AND reminders_enabled
                             AND pack_key=CASE WHEN split_part(insight.dedup_key,':',2) IN ('sleep_score','sleep_seconds')
                                          THEN 'sleep' ELSE 'wellbeing' END))
             AND octet_length(insight.statement)<=2500 AND octet_length(insight.evidence::text)<=10000
             AND (insight.generated_at >= $1 - interval '1 day' OR last.value->>'reservation'=insight.id::text)
             AND insight.generated_at <= $1
           ORDER BY coalesce(last.value->>'reservation'=insight.id::text,false) DESC,
                    insight.generated_at DESC,insight.id
           LIMIT 3",
    )
    .bind(OffsetDateTime::from(now))
    .bind(destination.owner_id)
    .fetch_all(&mut **txn)
    .await?;
    // Idle polls need no archive-wide projection check. Retain the replay fence
    // and validate readiness before reserving or sending any candidate.
    if rows.is_empty() {
        return Ok(None);
    }
    if projection_pending(txn, GARMIN_PARSER_VERSION).await? {
        return Ok(None);
    }
    let timezone = owner_timezone(txn, settings.timezone)
        .await
        .map_err(|_| ProactiveControlError::InvalidStoredState)?;
    for (id, metric, text, evidence, recent) in rows {
        let key = format!("outbox:insight:{id}:0");
        let reservation = format!("insight:last:{metric}");
        let marker: Option<String> =
            sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
                .bind(&key)
                .fetch_optional(&mut **txn)
                .await?;
        let marker = json_value(marker.as_deref())?;
        match marker["status"].as_str() {
            Some("sent") => {
                recorded_delivery(txn, id, "delivered", &reservation, now).await?;
                continue;
            }
            Some("sending" | "uncertain") => {
                recorded_delivery(txn, id, "uncertain", &reservation, now).await?;
                continue;
            }
            Some("pending") => {}
            None if marker.is_null() => {}
            _ => continue,
        }
        if marker
            .get("channel_instance_id")
            .and_then(Value::as_str)
            .is_some_and(|recorded| recorded != channel)
            || saved_at(&marker["retry_at"])?.is_some_and(|time| time > now)
        {
            continue;
        }
        let recent = json_value(recent.as_deref())?;
        let retry = recent["reservation"] == id.to_string();
        if !retry
            && saved_at(&recent["at"])?.is_some_and(|last| {
                OffsetDateTime::from(last) > OffsetDateTime::from(now) - time::Duration::days(7)
            })
        {
            continue;
        }
        let policy = NotificationPolicy {
            timezone: &timezone,
            destination: Some(channel),
            exclusions: NotificationExclusions {
                insight_key: retry.then_some(reservation.as_str()),
                ..NotificationExclusions::default()
            },
            ..*settings
        };
        if decision_in(txn, &policy, now).await? != NotificationDecision::Allow {
            return Ok(None);
        }
        let evidence = json_value(Some(&evidence))?;
        if !stored_trend_current(txn, &metric, &evidence)
            .await
            .map_err(|_| ProactiveControlError::InvalidStoredState)?
        {
            sqlx::query(
                "UPDATE insights SET status='cancelled',
                    evidence=evidence || '{\"cancel_reason\":\"evidence_changed\"}'::jsonb
                 WHERE id=$1 AND status='accepted'",
            )
            .bind(id)
            .execute(&mut **txn)
            .await?;
            if retry {
                sqlx::query("DELETE FROM app_state WHERE key=$1 AND value->>'reservation'=$2")
                    .bind(&reservation)
                    .bind(id.to_string())
                    .execute(&mut **txn)
                    .await?;
            }
            continue;
        }
        let token = Uuid::new_v4();
        write_marker(
            txn,
            &reservation,
            json!({"at":now.to_string(),"reservation":id.to_string()}),
        )
        .await?;
        write_marker(
            txn,
            &key,
            json!({
                "status":"sending","started_at":now.to_string(),"formatted":true,
                "channel_instance_id":channel,"attempt_token":token.to_string(),
            }),
        )
        .await?;
        return Ok(Some(Publication {
            id,
            token,
            key,
            reservation,
            text,
        }));
    }
    Ok(None)
}

async fn finish(
    connection: &mut PgConnection,
    publication: Publication,
    channel: &str,
    now: StoredInstant,
    result: DiaryQuestionOutcome,
) -> Result<(), ProactiveControlError> {
    let mut txn = connection.begin().await?;
    prepare_storage(&mut txn).await?;
    let matching: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM app_state
           WHERE key=$1 AND value->>'attempt_token'=$2 AND value->>'status'='sending')",
    )
    .bind(&publication.key)
    .bind(publication.token.to_string())
    .fetch_one(&mut *txn)
    .await?;
    if !matching {
        return Err(ProactiveControlError::Busy);
    }
    let mut marker = json!({"formatted":true,"channel_instance_id":channel});
    let final_status = match result {
        DiaryQuestionOutcome::Accepted(message_id) if message_id > 0 => {
            marker["status"] = json!("sent");
            marker["message_id"] = json!(message_id);
            marker["kind"] = json!("diary");
            Some("delivered")
        }
        DiaryQuestionOutcome::RetryAt(at) => {
            marker["status"] = json!("pending");
            marker["retry_at"] = json!(at.max(now).to_string());
            None
        }
        _ => {
            marker["status"] = json!("uncertain");
            Some("uncertain")
        }
    };
    if let Some(status) = final_status {
        recorded_delivery(
            &mut txn,
            publication.id,
            status,
            &publication.reservation,
            now,
        )
        .await?;
    }
    write_marker(&mut txn, &publication.key, marker).await?;
    txn.commit().await?;
    Ok(())
}

async fn recorded_delivery(
    txn: &mut Transaction<'_, Postgres>,
    id: Uuid,
    status: &str,
    reservation: &str,
    now: StoredInstant,
) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE insights SET status=$2 WHERE id=$1 AND status='accepted'")
        .bind(id)
        .bind(status)
        .execute(&mut **txn)
        .await?;
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES ($1,jsonb_build_object('at',$2::text))
         ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=clock_timestamp()
         WHERE app_state.value->>'reservation'=$3 OR app_state.value->>'at' IS NULL",
    )
    .bind(reservation)
    .bind(now.to_string())
    .bind(id.to_string())
    .execute(&mut **txn)
    .await?;
    Ok(())
}

fn json_value(value: Option<&str>) -> Result<Value, ProactiveControlError> {
    value
        .map_or(Ok(Value::Null), serde_json::from_str)
        .map_err(|_| ProactiveControlError::InvalidStoredState)
}

fn saved_at(value: &Value) -> Result<Option<StoredInstant>, ProactiveControlError> {
    if value.is_null() {
        return Ok(None);
    }
    StoredInstant::from_legacy_input(value)
        .map(Some)
        .ok_or(ProactiveControlError::InvalidStoredState)
}
