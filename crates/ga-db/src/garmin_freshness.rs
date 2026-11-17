use ga_domain::timestamp::StoredInstant;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

pub(crate) async fn record_freshness(
    txn: &mut Transaction<'_, Postgres>,
    endpoint: &str,
    source_key: &str,
    status: &str,
    requested_at: StoredInstant,
    source_id: Option<Uuid>,
) -> Result<(), sqlx::Error> {
    let fetched = status != "fetch_error";
    let metric = match endpoint {
        "heart_rate" => Some("heart_rate_bpm"),
        "stress" => Some("stress_score"),
        _ => None,
    };
    let mut normalized = fetched && !matches!(status, "stale" | "error");
    if normalized && let Some(metric) = metric {
        normalized = sqlx::query_scalar(
            "SELECT EXISTS (
                SELECT 1 FROM measurements WHERE source_ref=$1 AND metric=$2
                  AND quality='observed' AND ts <= $3
            )",
        )
        .bind(source_id)
        .bind(metric)
        .bind(time::OffsetDateTime::from(requested_at))
        .fetch_one(&mut **txn)
        .await?;
    }
    sqlx::query(
        "INSERT INTO app_state(key,value)
         VALUES($1,jsonb_build_object('fetched_at',$2::text,
             'success_at', CASE WHEN $7 THEN $2::text END,
             'normalized_at', CASE WHEN $3 THEN $2::text END,
             'status',$4::text,'source_key',$5::text,'source_ref',$6::text))
         ON CONFLICT(key) DO UPDATE SET updated_at=now(),
           value=excluded.value || jsonb_build_object(
                 'success_at', CASE WHEN $7 THEN to_jsonb($2::text) ELSE app_state.value->'success_at' END,
                 'normalized_at', CASE WHEN $3 THEN to_jsonb($2::text) ELSE app_state.value->'normalized_at' END)
         WHERE COALESCE(NULLIF(app_state.value->>'fetched_at',''),
                        NULLIF(app_state.value->>'success_at',''))::timestamptz <= $2::timestamptz
            OR COALESCE(NULLIF(app_state.value->>'fetched_at',''),
                        NULLIF(app_state.value->>'success_at','')) IS NULL",
    )
    .bind(format!("freshness:{endpoint}:{source_key}"))
    .bind(requested_at.to_string())
    .bind(normalized)
    .bind(status)
    .bind(source_key)
    .bind(source_id.map(|id| id.to_string()))
    .bind(fetched)
    .execute(&mut **txn)
    .await?;
    Ok(())
}
