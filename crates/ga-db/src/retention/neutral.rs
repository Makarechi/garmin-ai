use std::collections::BTreeSet;

use serde_json::Value;
use time::OffsetDateTime;
use uuid::Uuid;

use super::{PgTransaction, RetentionError, RetentionWindow, parse_cursor, parse_timestamp, stamp};

const TERMINAL: &[&str] = &[
    "provider_accepted",
    "delivered",
    "read",
    "cancelled",
    "expired",
    "failed",
];

type Position = (OffsetDateTime, Uuid);

#[derive(Default)]
pub(super) struct Cursor {
    inbound_after: Option<Position>,
    inbound_done: bool,
    orphan_after: Option<Position>,
}

impl Cursor {
    pub(super) fn parse(raw: Option<&str>) -> Result<Self, RetentionError> {
        let Some(raw) = raw else {
            return Ok(Self::default());
        };
        let value = parse_cursor(raw)?;
        if value.is_array() {
            return Ok(Self {
                inbound_after: Some(position(&value)?),
                ..Self::default()
            });
        }
        let object = value.as_object().ok_or(RetentionError::Cursor)?;
        let after = |name: &str| {
            object
                .get(name)
                .filter(|value| !value.is_null())
                .map(position)
                .transpose()
        };
        Ok(Self {
            inbound_after: after("inbound_after")?,
            inbound_done: object.get("inbound_done").and_then(Value::as_bool) == Some(true),
            orphan_after: after("orphan_after")?,
        })
    }

    fn render(&self) -> Result<String, RetentionError> {
        Ok(format!(
            "{{\"inbound_after\": {}, \"inbound_done\": {}, \"orphan_after\": {}}}",
            render_position(self.inbound_after)?,
            self.inbound_done,
            render_position(self.orphan_after)?,
        ))
    }
}

fn position(value: &Value) -> Result<Position, RetentionError> {
    let [date, id] = value
        .as_array()
        .map(Vec::as_slice)
        .ok_or(RetentionError::Cursor)?
    else {
        return Err(RetentionError::Cursor);
    };
    let date = date
        .as_str()
        .and_then(parse_timestamp)
        .ok_or(RetentionError::Cursor)?;
    let id = id
        .as_str()
        .and_then(|id| id.parse().ok())
        .ok_or(RetentionError::Cursor)?;
    Ok((date, id))
}

fn render_position(value: Option<Position>) -> Result<String, RetentionError> {
    let Some((date, id)) = value else {
        return Ok("null".into());
    };
    Ok(format!("[\"{}\", \"{id}\"]", stamp(date)?))
}

pub(super) struct Page {
    pub scanned: usize,
    pub eligible: usize,
    pub next: Option<String>,
}

pub(super) async fn prune(
    txn: &mut PgTransaction<'_>,
    window: &RetentionWindow,
    mut cursor: Cursor,
) -> Result<Page, RetentionError> {
    let inbound: Vec<Position> = if cursor.inbound_done {
        Vec::new()
    } else {
        sqlx::query_as(
            "SELECT received_at,id FROM inbound_messages
             WHERE status IN ('processed','invalid') AND received_at<$1
               AND envelope->>'_text_redacted' IS DISTINCT FROM 'true'
               AND ($2::timestamptz IS NULL OR (received_at,id)>($2,$3))
             ORDER BY received_at,id LIMIT $4 FOR UPDATE",
        )
        .bind(window.cutoff)
        .bind(cursor.inbound_after.map(|(at, _)| at))
        .bind(cursor.inbound_after.map(|(_, id)| id))
        .bind(window.limit)
        .fetch_all(&mut **txn)
        .await?
    };
    let ids: Vec<_> = inbound.iter().map(|(_, id)| *id).collect();
    let outbox: Vec<(Uuid, String)> = sqlx::query_as(
        "SELECT inbound_message_id,state FROM outbox_messages WHERE inbound_message_id=ANY($1) FOR UPDATE",
    )
    .bind(&ids).fetch_all(&mut **txn).await?;
    let unavailable: BTreeSet<_> = outbox
        .into_iter()
        .filter_map(|(id, state)| (!TERMINAL.contains(&state.as_str())).then_some(id))
        .collect();
    let eligible: Vec<_> = ids
        .into_iter()
        .filter(|id| !unavailable.contains(id))
        .collect();

    let remaining = window.limit - inbound.len() as i64;
    let mut orphan: Vec<Position> = if remaining > 0 {
        sqlx::query_as(
            "SELECT created_at,id FROM outbox_messages
             WHERE inbound_message_id IS NULL AND created_at<$1 AND state=ANY($2)
               AND intent->>'_text_redacted' IS DISTINCT FROM 'true'
               AND ($3::timestamptz IS NULL OR (created_at,id)>($3,$4))
             ORDER BY created_at,id LIMIT $5 FOR UPDATE",
        )
        .bind(window.cutoff)
        .bind(TERMINAL)
        .bind(cursor.orphan_after.map(|(at, _)| at))
        .bind(cursor.orphan_after.map(|(_, id)| id))
        .bind(remaining + 1)
        .fetch_all(&mut **txn)
        .await?
    } else {
        Vec::new()
    };
    let orphan_more = orphan.len() as i64 > remaining;
    orphan.truncate(remaining as usize);
    if window.apply {
        let direct: Vec<_> = orphan.iter().map(|(_, id)| *id).collect();
        redact(txn, &eligible, &direct, &window.now).await?;
    }
    let inbound_more = !cursor.inbound_done && inbound.len() as i64 == window.limit;
    let next = if inbound_more || orphan_more {
        cursor.inbound_after = inbound.last().copied().or(cursor.inbound_after);
        cursor.orphan_after = orphan.last().copied().or(cursor.orphan_after);
        cursor.inbound_done = !inbound_more;
        Some(cursor.render()?)
    } else {
        None
    };
    Ok(Page {
        scanned: inbound.len() + orphan.len(),
        eligible: eligible.len() + orphan.len(),
        next,
    })
}

async fn redact(
    txn: &mut PgTransaction<'_>,
    inbound: &[Uuid],
    orphan: &[Uuid],
    at: &str,
) -> Result<(), RetentionError> {
    sqlx::query(
        "WITH redacted AS (
            UPDATE inbound_messages SET normalized_text=NULL,envelope=jsonb_build_object(
                '_text_redacted',true,'receipt',gen_random_uuid(),'redacted_at',$2::text)
            WHERE id=ANY($1) RETURNING id,envelope
         )
         UPDATE outbox_messages sent SET intent=redacted.envelope,updated_at=now()
         FROM redacted WHERE sent.inbound_message_id=redacted.id",
    )
    .bind(inbound)
    .bind(at)
    .execute(&mut **txn)
    .await?;
    sqlx::query(
        "UPDATE outbox_messages SET intent=jsonb_build_object(
                '_text_redacted',true,'receipt',gen_random_uuid(),'redacted_at',$2::text),updated_at=now()
         WHERE id=ANY($1)",
    )
    .bind(orphan)
    .bind(at)
    .execute(&mut **txn)
    .await?;
    Ok(())
}
