use std::{collections::BTreeSet, time::Duration};

use futures_util::TryStreamExt;
use ga_domain::{access::Scopes, timestamp::StoredInstant};
use serde::Deserialize;
use serde_json::{Value, json};
use uuid::Uuid;

use super::{
    DestinationKind, ShareCategory, ShareDestination, SharePolicyError,
    consent_writes::{consent_key, require_permission},
};
use crate::Database;

pub(super) const POLICY_REVISION: u8 = 1;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SerializedConsent {
    definition_id: String,
    destination_kind: DestinationKind,
    destination_instance_id: String,
    categories: Vec<ShareCategory>,
    granted_at: Value,
    #[serde(default = "legacy_policy_revision")]
    policy_revision: u8,
}

fn legacy_policy_revision() -> u8 {
    POLICY_REVISION
}

/// A validated tracker sharing consent with its original timestamp offset.
pub struct TrackerShareConsent {
    pub(super) definition_id: Uuid,
    kind: DestinationKind,
    instance: String,
    pub(super) categories: Vec<ShareCategory>,
    pub(super) granted_at: StoredInstant,
    pub(super) granted_at_json: String,
}

impl TrackerShareConsent {
    pub fn parse(body: &[u8]) -> Result<Self, SharePolicyError> {
        let record = serde_json::from_slice::<SerializedConsent>(body)
            .map_err(|_| SharePolicyError::InvalidConsent)?;
        let definition_id = record
            .definition_id
            .parse()
            .map_err(|_| SharePolicyError::InvalidConsent)?;
        ShareDestination::new(record.destination_kind, &record.destination_instance_id)?;
        let (granted_at, granted_at_json) =
            StoredInstant::legacy_input_with_json(&record.granted_at)
                .ok_or(SharePolicyError::InvalidConsent)?;
        if record.policy_revision != POLICY_REVISION || record.categories.is_empty() {
            return Err(SharePolicyError::InvalidConsent);
        }
        Ok(Self {
            definition_id,
            kind: record.destination_kind,
            instance: record.destination_instance_id,
            categories: record.categories,
            granted_at,
            granted_at_json,
        })
    }

    pub(super) fn destination(&self) -> ShareDestination<'_> {
        ShareDestination {
            kind: self.kind,
            instance: &self.instance,
        }
    }

    #[must_use]
    pub fn document(&self) -> Value {
        consent_document(
            self.definition_id,
            self.destination(),
            &self.categories,
            &self.granted_at_json,
        )
    }

    pub(super) fn matches(&self, definition_id: Uuid, destination: ShareDestination<'_>) -> bool {
        self.definition_id == definition_id
            && self.kind == destination.kind()
            && self.instance == destination.instance()
    }
}

pub(super) fn consent_document(
    definition_id: Uuid,
    destination: ShareDestination<'_>,
    categories: &[ShareCategory],
    granted_at: &str,
) -> Value {
    let categories: BTreeSet<_> = categories.iter().map(|category| category.label()).collect();
    json!({
        "definition_id": definition_id.to_string(),
        "destination_kind": destination.kind().label(),
        "destination_instance_id": destination.instance(),
        "categories": categories,
        "granted_at": granted_at,
        "policy_revision": POLICY_REVISION,
    })
}

impl Database {
    pub async fn list_tracker_shares(
        &self,
        scopes: Scopes,
    ) -> Result<Vec<Value>, SharePolicyError> {
        require_permission(scopes)?;
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_share_check().await?;
            let mut records = sqlx::query_as::<_, (String, String)>(
                "SELECT key,value::text FROM app_state WHERE starts_with(key,'tracker-consent:') ORDER BY key",
            )
            .fetch(&mut *txn);
            let mut documents = Vec::new();
            while let Some((key, value)) = records.try_next().await? {
                let consent = TrackerShareConsent::parse(value.as_bytes())?;
                if key != consent_key(consent.definition_id, consent.destination()) {
                    return Err(SharePolicyError::InvalidConsent);
                }
                documents.push(consent.document());
            }
            drop(records);
            txn.commit().await?;
            Ok(documents)
        })
        .await
        .map_err(|_| SharePolicyError::Database)?
    }
}
