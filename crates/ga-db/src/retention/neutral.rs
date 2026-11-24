use std::collections::BTreeSet;

use ga_domain::{identity::python_cursor_uuid, timestamp::python_isoformat_with_display};
use serde_json::Value;
use time::OffsetDateTime;
use uuid::Uuid;

use super::{
    PgTransaction, RetentionError, RetentionWindow, cursor_interval, cursor_pair, parse_cursor,
    stamp,
};

const TERMINAL: &[&str] = &[
    "provider_accepted",
    "delivered",
    "read",
    "cancelled",
    "expired",
    "failed",
];

struct Position {
    microseconds: i64,
    display: String,
    id: Uuid,
}

impl Position {
    fn parse(value: &Value) -> Result<Self, RetentionError> {
        let (date, id) = cursor_pair(value).ok_or(RetentionError::Cursor)?;
        let (microseconds, display) =
            python_isoformat_with_display(date).ok_or(RetentionError::Cursor)?;
        let id = id
            .as_str()
            .and_then(python_cursor_uuid)
            .ok_or(RetentionError::Cursor)?;
        Ok(Self {
            microseconds,
            display,
            id,
        })
    }

    fn from_row((at, id): (OffsetDateTime, Uuid)) -> Result<Self, RetentionError> {
        Ok(Self {
            microseconds: at.unix_timestamp() * 1_000_000 + i64::from(at.microsecond()),
            display: stamp(at)?,
            id,
        })
    }

    fn render(&self) -> String {
        format!("[\"{}\", \"{}\"]", self.display, self.id)
    }
}

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
                inbound_after: Some(Position::parse(&value)?),
                ..Self::default()
            });
        }
        let object = value.as_object().ok_or(RetentionError::Cursor)?;
        let after = |name: &str| {
            object
                .get(name)
                .filter(|value| !value.is_null())
                .map(Position::parse)
                .transpose()
        };
        Ok(Self {
            inbound_after: after("inbound_after")?,
            inbound_done: object.get("inbound_done").and_then(Value::as_bool) == Some(true),
            orphan_after: after("orphan_after")?,
        })
    }

    fn render(&self) -> String {
        let render =
            |position: Option<&Position>| position.map_or_else(|| "null".into(), Position::render);
        format!(
            "{{\"inbound_after\": {}, \"inbound_done\": {}, \"orphan_after\": {}}}",
            render(self.inbound_after.as_ref()),
            self.inbound_done,
            render(self.orphan_after.as_ref()),
        )
    }
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
    let inbound: Vec<(OffsetDateTime, Uuid)> = if cursor.inbound_done {
        Vec::new()
    } else {
        sqlx::query_as(
            "SELECT received_at,id FROM inbound_messages
             WHERE status IN ('processed','invalid') AND received_at<$1
               AND envelope->>'_text_redacted' IS DISTINCT FROM 'true'
               AND ($2::interval IS NULL OR
                    (received_at,id)>(('1970-01-01'::timestamp + $2) AT TIME ZONE 'UTC',$3))
             ORDER BY received_at,id LIMIT $4 FOR UPDATE",
        )
        .bind(window.cutoff)
        .bind(
            cursor
                .inbound_after
                .as_ref()
                .map(|at| cursor_interval(at.microseconds)),
        )
        .bind(cursor.inbound_after.as_ref().map(|at| at.id))
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

    let inbound_count = i64::try_from(inbound.len()).unwrap_or(i64::MAX);
    let remaining = window.limit - inbound_count;
    let mut orphan: Vec<(OffsetDateTime, Uuid)> = if remaining > 0 {
        sqlx::query_as(
            "SELECT created_at,id FROM outbox_messages
             WHERE inbound_message_id IS NULL AND created_at<$1 AND state=ANY($2)
               AND intent->>'_text_redacted' IS DISTINCT FROM 'true'
               AND ($3::interval IS NULL OR
                    (created_at,id)>(('1970-01-01'::timestamp + $3) AT TIME ZONE 'UTC',$4))
             ORDER BY created_at,id LIMIT $5 FOR UPDATE",
        )
        .bind(window.cutoff)
        .bind(TERMINAL)
        .bind(
            cursor
                .orphan_after
                .as_ref()
                .map(|at| cursor_interval(at.microseconds)),
        )
        .bind(cursor.orphan_after.as_ref().map(|at| at.id))
        .bind(remaining + 1)
        .fetch_all(&mut **txn)
        .await?
    } else {
        Vec::new()
    };
    let orphan_more = i64::try_from(orphan.len()).unwrap_or(i64::MAX) > remaining;
    orphan.truncate(usize::try_from(remaining).unwrap_or(usize::MAX));
    if window.apply {
        let direct: Vec<_> = orphan.iter().map(|(_, id)| *id).collect();
        redact(txn, &eligible, &direct, &window.now).await?;
    }
    let inbound_more = !cursor.inbound_done && inbound_count == window.limit;
    let next = if inbound_more || orphan_more {
        if let Some(row) = inbound.last().copied() {
            cursor.inbound_after = Some(Position::from_row(row)?);
        }
        if let Some(row) = orphan.last().copied() {
            cursor.orphan_after = Some(Position::from_row(row)?);
        }
        cursor.inbound_done = !inbound_more;
        Some(cursor.render())
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
