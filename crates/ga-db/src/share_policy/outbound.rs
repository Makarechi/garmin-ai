use std::collections::HashSet;

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
    let Some(references) = document.get("evidence_refs") else {
        return Ok(requirements.is_empty());
    };
    let Some(references) = references.as_array().filter(|parts| parts.len() <= 100) else {
        return Ok(false);
    };
    let mut definitions = HashSet::new();
    let mut exact_versions = HashSet::new();
    let mut schema_versions = HashSet::new();
    for reference in references {
        let Some(reference) = reference.as_str() else {
            return Ok(false);
        };
        let (reference_id, recorded) =
            if let Some(id) = reference.strip_prefix("definition-schema-version:") {
                (id, &mut schema_versions)
            } else if let Some(id) = reference.strip_prefix("definition:") {
                (id, &mut definitions)
            } else if let Some(id) = reference.strip_prefix("definition-version:") {
                (id, &mut exact_versions)
            } else {
                continue;
            };
        let Ok(id) = reference_id.parse::<Uuid>() else {
            return Ok(false);
        };
        if reference_id != id.to_string() {
            return Ok(false);
        }
        recorded.insert(id);
    }
    if definitions.is_empty() && exact_versions.is_empty() && schema_versions.is_empty() {
        return Ok(requirements.is_empty());
    }
    let Some(destination) = destination else {
        return Ok(false);
    };
    let mut exact_definitions = HashSet::new();
    let mut evidenced_versions = HashSet::new();
    for version_id in exact_versions {
        let definition: Option<Uuid> =
            sqlx::query_scalar("SELECT definition_id FROM event_definition_versions WHERE id=$1")
                .bind(version_id)
                .fetch_optional(&mut **txn)
                .await?;
        let Some(definition) = definition.filter(|definition| definitions.contains(definition))
        else {
            return Ok(false);
        };
        if !facts_allowed(txn, version_id, destination).await? {
            return Ok(false);
        }
        exact_definitions.insert(definition);
        evidenced_versions.insert(version_id);
    }
    let mut schema_only = HashSet::new();
    for version_id in schema_versions.difference(&evidenced_versions) {
        let definition: Option<Uuid> =
            sqlx::query_scalar("SELECT definition_id FROM event_definition_versions WHERE id=$1")
                .bind(version_id)
                .fetch_optional(&mut **txn)
                .await?;
        let Some(definition) = definition.filter(|definition| definitions.contains(definition))
        else {
            return Ok(false);
        };
        if !allows(
            txn,
            ShareRequirement {
                version_id: *version_id,
                categories: &[ShareCategory::Schema],
            },
            destination,
        )
        .await?
        {
            return Ok(false);
        }
        exact_definitions.insert(definition);
        schema_only.insert(*version_id);
    }
    for definition in definitions.difference(&exact_definitions) {
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
        if !facts_allowed(txn, version_id, destination).await? {
            return Ok(false);
        }
        evidenced_versions.insert(version_id);
    }
    // Exact schema-only references retain the category used by local form prompts.
    // Every explicit dependency must be represented for send-time validation and
    // definition-wide cancellation when the recipient consent changes.
    Ok(requirements.iter().all(|requirement| {
        (evidenced_versions.contains(&requirement.version_id)
            || schema_only.contains(&requirement.version_id)
                && requirement
                    .categories
                    .iter()
                    .all(|category| *category == ShareCategory::Schema))
            && !requirement.categories.is_empty()
            && !requirement
                .categories
                .contains(&ShareCategory::OriginalText)
    }))
}

async fn facts_allowed(
    txn: &mut Transaction<'_, Postgres>,
    version_id: Uuid,
    destination: ShareDestination<'_>,
) -> Result<bool, SharePolicyError> {
    allows(
        txn,
        ShareRequirement {
            version_id,
            categories: &[ShareCategory::Schema, ShareCategory::Facts],
        },
        destination,
    )
    .await
}
