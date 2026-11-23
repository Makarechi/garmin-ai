use std::collections::BTreeSet;

use ga_domain::{
    legacy_json::{Utf8Layout, encode_ascii},
    model_consent::ModelCategories,
    sha256_hex,
};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{CatalogError, CatalogModel, ResourcePage, resource_catalog_in};
use crate::{
    Database, check_readiness,
    conversation_memory::{AnalysisSubject, analysis_channel_in},
    health_analysis::HealthAnalysisError,
    share_policy::{DestinationKind, ShareDestination},
};

pub const RESOURCE_CATALOG: &str = "resource_catalog";
const MAX_MODEL_RESULT_BYTES: usize = 96_000;

pub struct ModelResourcePage {
    pub value: Value,
    pub schema_versions: BTreeSet<Uuid>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CatalogEvidence {
    catalog_arguments: Map<String, Value>,
    catalog_result_hash: String,
}

impl CatalogEvidence {
    #[must_use]
    pub fn from_read(evidence: &Value) -> Option<Self> {
        if evidence["tool"] != RESOURCE_CATALOG || evidence["result"].get("error").is_some() {
            return None;
        }
        let arguments = evidence["arguments"].as_object()?;
        ResourcePage::parse(arguments).ok()?;
        Some(Self {
            catalog_arguments: arguments.clone(),
            catalog_result_hash: result_hash(&evidence["result"])?,
        })
    }

    pub(crate) fn specification(&self) -> Value {
        json!({
            "tool": RESOURCE_CATALOG,
            "arguments": self.catalog_arguments,
            "result_hash": self.catalog_result_hash,
        })
    }

    pub(crate) async fn current(
        &self,
        txn: &mut Transaction<'_, Postgres>,
        owner: Uuid,
        model: ShareDestination<'_>,
        channel: ShareDestination<'_>,
        categories: ModelCategories,
    ) -> Result<bool, CatalogError> {
        let page = ResourcePage::parse(&self.catalog_arguments)?;
        let selected = read_model_page(txn, owner, model, channel, categories, page).await?;
        Ok(result_hash(&selected.value).as_deref() == Some(self.catalog_result_hash.as_str()))
    }
}

impl Database {
    pub async fn read_model_resource_catalog(
        &self,
        context: AnalysisSubject<'_>,
        model: ShareDestination<'_>,
        categories: ModelCategories,
        page: ResourcePage<'_>,
    ) -> Result<ModelResourcePage, CatalogError> {
        let mut txn = self
            .work
            .begin_with("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .await?;
        sqlx::query("SET LOCAL statement_timeout = '750ms'")
            .execute(&mut *txn)
            .await?;
        check_readiness(&mut txn)
            .await
            .map_err(|error| CatalogError::Health(HealthAnalysisError::Storage(error)))?;
        let channel = analysis_channel_in(&mut txn, context)
            .await
            .map_err(|_| CatalogError::Database)?
            .ok_or(CatalogError::ModelConsent)?;
        let channel = ShareDestination::new(DestinationKind::Channel, &channel)
            .map_err(|_| CatalogError::ModelConsent)?;
        let value = read_model_page(
            &mut txn,
            context.binding().owner_id,
            model,
            channel,
            categories,
            page,
        )
        .await?;
        txn.commit().await?;
        Ok(value)
    }
}

async fn read_model_page(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    model: ShareDestination<'_>,
    channel: ShareDestination<'_>,
    categories: ModelCategories,
    page: ResourcePage<'_>,
) -> Result<ModelResourcePage, CatalogError> {
    let required = if page.kind.health() {
        ModelCategories::HEALTH
    } else {
        ModelCategories::DIARY
    };
    if model.kind() != DestinationKind::Model
        || channel.kind() != DestinationKind::Channel
        || !categories.covers(required)
    {
        return Err(CatalogError::ModelConsent);
    }
    let (document, schema_versions) = resource_catalog_in(
        txn,
        page,
        Some(CatalogModel {
            owner,
            destination: model.instance(),
            channel: Some(channel.instance()),
        }),
        categories.covers(ModelCategories::HEALTH),
    )
    .await?;
    if document.byte_len() > MAX_MODEL_RESULT_BYTES as u64 {
        return Err(CatalogError::ResultTooLarge);
    }
    let value = serde_json::from_reader(document.reader()).map_err(|_| CatalogError::Database)?;
    Ok(ModelResourcePage {
        value,
        schema_versions,
    })
}

fn result_hash(value: &Value) -> Option<String> {
    let mut ordered = value.clone();
    ordered.sort_all_objects();
    let json = serde_json::to_vec(&ordered).ok()?;
    let encoded = encode_ascii(&json, Utf8Layout::Archive, MAX_MODEL_RESULT_BYTES).ok()?;
    Some(sha256_hex(encoded.as_bytes()))
}
