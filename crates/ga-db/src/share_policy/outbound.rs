use serde_json::Value;
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{
    DestinationKind, ShareCategory, ShareDestination, SharePolicyError, ShareRequirement, allows,
};

pub(crate) async fn intent_sharing_allowed(
    txn: &mut Transaction<'_, Postgres>,
    target: &str,
    document: &Value,
    requirements: &[ShareRequirement<'_>],
) -> Result<bool, SharePolicyError> {
    let destination = ShareDestination::new(DestinationKind::Channel, target).ok();
    for requirement in requirements {
        let Some(destination) = destination else {
            return Ok(false);
        };
        if !allows(txn, *requirement, destination).await? {
            return Ok(false);
        }
    }
    let Some(references) = document.get("evidence_refs") else {
        return Ok(true);
    };
    let Some(references) = references.as_array().filter(|parts| parts.len() <= 100) else {
        return Ok(false);
    };
    for reference in references {
        let Some(reference) = reference.as_str() else {
            return Ok(false);
        };
        let Some(definition) = reference.strip_prefix("definition:") else {
            continue;
        };
        let (Ok(definition), Some(destination)) = (definition.parse::<Uuid>(), destination) else {
            return Ok(false);
        };
        let version: Option<Uuid> = sqlx::query_scalar(
            "SELECT v.id FROM event_definitions d JOIN event_definition_versions v
                ON v.definition_id=d.id AND v.version=d.current_version WHERE d.id=$1",
        )
        .bind(definition)
        .fetch_optional(&mut **txn)
        .await?;
        let Some(version_id) = version else {
            return Ok(false);
        };
        if !allows(
            txn,
            ShareRequirement {
                version_id,
                categories: &[ShareCategory::Schema, ShareCategory::Facts],
            },
            destination,
        )
        .await?
        {
            return Ok(false);
        }
    }
    Ok(true)
}
