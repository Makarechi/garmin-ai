use std::collections::{BTreeMap, BTreeSet};

use time::OffsetDateTime;
use uuid::Uuid;

use super::{PgTransaction, RetentionError, RetentionWindow, parse_cursor, parse_timestamp, stamp};

const REDACTED_REPLY: &str = "Срок хранения технического текста этого ответа истёк. Записи дневника доступны через /history.";

type Position = (OffsetDateTime, i64);

pub(super) fn cursor(raw: &str) -> Result<Position, RetentionError> {
    let value = parse_cursor(raw)?;
    let [date, identity] = value
        .as_array()
        .map(Vec::as_slice)
        .ok_or(RetentionError::Cursor)?
    else {
        return Err(RetentionError::Cursor);
    };
    let at = date
        .as_str()
        .and_then(parse_timestamp)
        .ok_or(RetentionError::Cursor)?;
    let id = identity
        .as_i64()
        .or_else(|| identity.as_str()?.parse().ok())
        .ok_or(RetentionError::Cursor)?;
    Ok((at, id))
}

pub(super) struct Page {
    pub scanned: usize,
    pub eligible: usize,
    pub jobs: usize,
    pub transcripts: usize,
    pub next: Option<String>,
}

struct AssociatedJob {
    id: Uuid,
    dedup_key: String,
    final_before_cutoff: bool,
}

pub(super) async fn prune(
    txn: &mut PgTransaction<'_>,
    window: &RetentionWindow,
    after: Option<Position>,
) -> Result<Page, RetentionError> {
    let candidates: Vec<(i64, OffsetDateTime, bool)> = sqlx::query_as(
        "SELECT id,received_at,jsonb_typeof(payload)='object' FROM telegram_updates
         WHERE status IN ('processed','invalid') AND received_at<$1
           AND payload->>'_text_redacted' IS DISTINCT FROM 'true'
           AND ($2::timestamptz IS NULL OR (received_at,id)>($2,$3))
         ORDER BY received_at,id LIMIT $4 FOR UPDATE",
    )
    .bind(window.cutoff)
    .bind(after.map(|(at, _)| at))
    .bind(after.map(|(_, id)| id))
    .bind(window.limit)
    .fetch_all(&mut **txn)
    .await?;
    let identities: Vec<_> = candidates.iter().map(|(id, _, _)| id.to_string()).collect();
    let jobs = associated_jobs(txn, window.cutoff, &identities).await?;
    let state = associated_state(txn, &identities).await?;
    let mut eligible = 0;
    let mut job_count = 0;
    let mut transcript_count = 0;
    for ((id, _, object), identity) in candidates.iter().zip(identities) {
        let reply = format!("telegram:reply:{id}");
        let transcript = format!("telegram:transcript:{id}");
        let Some(related) = jobs.get(&identity).filter(|jobs| {
            jobs.iter()
                .any(|job| job.dedup_key == format!("telegram:{id}"))
                && jobs.iter().all(|job| job.final_before_cutoff)
        }) else {
            continue;
        };
        if !object || !state.contains(&reply) {
            continue;
        }
        let has_transcript = state.contains(&transcript);
        eligible += 1;
        job_count += related.len();
        transcript_count += usize::from(has_transcript);
        if window.apply {
            redact_update(
                txn,
                *id,
                related,
                &reply,
                has_transcript.then_some(&transcript),
                &window.now,
            )
            .await?;
        }
    }
    let next = if candidates.len() as i64 == window.limit {
        let (id, at, _) = candidates.last().ok_or(RetentionError::Database)?;
        Some(format!("[\"{}\", {id}]", stamp(*at)?))
    } else {
        None
    };
    Ok(Page {
        scanned: candidates.len(),
        eligible,
        jobs: job_count,
        transcripts: transcript_count,
        next,
    })
}

async fn associated_jobs(
    txn: &mut PgTransaction<'_>,
    cutoff: OffsetDateTime,
    identities: &[String],
) -> Result<BTreeMap<String, Vec<AssociatedJob>>, RetentionError> {
    let rows: Vec<(String, Uuid, String, bool)> = sqlx::query_as(
        "SELECT payload->>'update_id',id,dedup_key,(status='done' AND coalesce(completed_at<$2,false))
         FROM jobs WHERE payload->>'update_id'=ANY($1)
         AND kind IN ('telegram_update','telegram_control','telegram_ack','telegram_failure')
         FOR UPDATE",
    )
    .bind(identities).bind(cutoff).fetch_all(&mut **txn).await?;
    let mut grouped: BTreeMap<String, Vec<AssociatedJob>> = BTreeMap::new();
    for (key, id, dedup_key, final_before_cutoff) in rows {
        grouped.entry(key).or_default().push(AssociatedJob {
            id,
            dedup_key,
            final_before_cutoff,
        });
    }
    Ok(grouped)
}

async fn associated_state(
    txn: &mut PgTransaction<'_>,
    identities: &[String],
) -> Result<BTreeSet<String>, RetentionError> {
    let keys: Vec<_> = identities
        .iter()
        .flat_map(|id| {
            [
                format!("telegram:reply:{id}"),
                format!("telegram:transcript:{id}"),
            ]
        })
        .collect();
    let rows: Vec<String> =
        sqlx::query_scalar("SELECT key FROM app_state WHERE key=ANY($1) FOR UPDATE")
            .bind(&keys)
            .fetch_all(&mut **txn)
            .await?;
    Ok(rows.into_iter().collect())
}

async fn redact_update(
    txn: &mut PgTransaction<'_>,
    id: i64,
    jobs: &[AssociatedJob],
    reply: &str,
    transcript: Option<&String>,
    at: &str,
) -> Result<(), RetentionError> {
    sqlx::query(
        "UPDATE telegram_updates SET payload=jsonb_build_object(
            '_text_redacted',true,'_ordering_epoch',coalesce(payload->'_ordering_epoch','0'::jsonb),
            '_channel_instance',coalesce(payload->'_channel_instance',
                '{\"channel\":\"telegram\",\"instance_id\":\"primary\"}'::jsonb),
            'update_id',coalesce(payload->'update_id',to_jsonb(id)),
            'receipt',gen_random_uuid(),'redacted_at',$2::text)
         WHERE id=$1",
    )
    .bind(id)
    .bind(at)
    .execute(&mut **txn)
    .await?;
    let jobs: Vec<_> = jobs.iter().map(|job| job.id).collect();
    sqlx::query(
        "UPDATE jobs SET payload=jsonb_build_object('update_id',$2::bigint,'_text_redacted',true)
         WHERE id=ANY($1)",
    )
    .bind(&jobs)
    .bind(id)
    .execute(&mut **txn)
    .await?;
    if let Some(transcript) = transcript {
        sqlx::query(
            "UPDATE app_state SET value='{\"text\":\"\",\"_text_redacted\":true}',updated_at=now() WHERE key=$1",
        )
        .bind(transcript)
        .execute(&mut **txn)
        .await?;
    }
    let changed = sqlx::query(
        "UPDATE app_state SET value=jsonb_build_object(
           'text',$2::text,'status','redacted','keyboard',false,
           'kind',coalesce(value->'kind','\"diary\"'::jsonb)),updated_at=now()
         WHERE key=$1 AND jsonb_typeof(value)='object'",
    )
    .bind(reply)
    .bind(REDACTED_REPLY)
    .execute(&mut **txn)
    .await?;
    if changed.rows_affected() != 1 {
        return Err(RetentionError::Database);
    }
    Ok(())
}
