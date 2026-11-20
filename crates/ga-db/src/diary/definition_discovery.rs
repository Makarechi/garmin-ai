use std::{collections::BTreeSet, io::Write, time::Duration};

use futures_util::TryStreamExt;
use ga_archive::json_spool::{JsonDocument, JsonSpool};
use ga_domain::legacy_json::integer_argument;
use serde_json::{Map, Value};
use sqlx::{Postgres, Transaction};
use uuid::Uuid;

use super::{DiaryReadError, reads::model_diary_allowed};
use crate::{
    Database,
    conversation_memory::{AnalysisSubject, analysis_channel_in},
    health_analysis::compact_document,
    share_policy::{DestinationKind, ShareDestination},
};

pub const EVENT_DEFINITIONS: &str = "event_definitions";

pub struct ModelDefinitionPage {
    pub value: Value,
    pub schema_versions: BTreeSet<Uuid>,
}

pub struct DefinitionPage<'a> {
    pub after_key: Option<&'a str>,
    pub definition_key: Option<&'a str>,
    pub before_version: Option<String>,
    pub limit: u16,
}

impl<'a> DefinitionPage<'a> {
    pub fn parse(arguments: &'a Map<String, Value>) -> Option<Self> {
        if arguments.keys().any(|key| {
            !matches!(
                key.as_str(),
                "after_key" | "definition_key" | "before_version" | "limit"
            )
        }) {
            return None;
        }
        let text = |key| match arguments.get(key) {
            None | Some(Value::Null) => Some(None),
            Some(Value::String(value)) => Some(Some(value.as_str())),
            _ => None,
        };
        let before_version = match arguments.get("before_version") {
            None | Some(Value::Null) => None,
            Some(value) => Some(integer_argument(value)?.to_string()),
        };
        let limit = match arguments.get("limit") {
            None => 10,
            Some(value) => integer_argument(value)?.try_into().unwrap_or(0),
        };
        Some(Self {
            after_key: text("after_key")?,
            definition_key: text("definition_key")?,
            before_version,
            limit,
        })
    }
}

impl Database {
    /// Discover immutable contracts with version-specific model sharing checks.
    pub async fn event_definitions(
        &self,
        page: DefinitionPage<'_>,
        model_owner: Option<Uuid>,
    ) -> Result<JsonDocument, DiaryReadError> {
        self.definition_document(page, model_owner, false)
            .await
            .map(|(document, _)| document)
    }

    /// Return the owner's bare list and its continuation header from one snapshot.
    pub async fn owner_event_definitions(
        &self,
        page: DefinitionPage<'_>,
    ) -> Result<(JsonDocument, Option<String>), DiaryReadError> {
        self.definition_document(page, None, true).await
    }

    async fn definition_document(
        &self,
        page: DefinitionPage<'_>,
        model_owner: Option<Uuid>,
        owner_http: bool,
    ) -> Result<(JsonDocument, Option<String>), DiaryReadError> {
        let work = async {
            let mut txn = self.begin_diary_read().await?;
            sqlx::query("SET LOCAL statement_timeout = '120s'")
                .execute(&mut *txn)
                .await?;
            if let Some(owner) = model_owner {
                model_diary_allowed(&mut txn, owner).await?;
            }
            let document = definition_document_in(
                &mut txn,
                page,
                model_owner.map(|_| "model:mcp:local"),
                None,
                owner_http,
            )
            .await?;
            txn.commit().await?;
            Ok(document)
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| DiaryReadError::Database)?
    }
    pub async fn read_analysis_definitions(
        &self,
        context: AnalysisSubject<'_>,
        model: ShareDestination<'_>,
        page: DefinitionPage<'_>,
    ) -> Result<ModelDefinitionPage, DiaryReadError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.begin_diary_read().await?;
            let channel = analysis_channel_in(&mut txn, context)
                .await
                .map_err(|_| DiaryReadError::ContextChanged)?
                .ok_or(DiaryReadError::ContextChanged)?;
            let channel = ShareDestination::new(DestinationKind::Channel, &channel)
                .map_err(|_| DiaryReadError::InvalidDestination)?;
            let result =
                analysis_definitions_in(&mut txn, context.binding().owner_id, model, channel, page)
                    .await?;
            txn.commit().await?;
            Ok(result)
        })
        .await
        .map_err(|_| DiaryReadError::Database)?
    }
}

pub(crate) async fn analysis_definitions_in(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    model: ShareDestination<'_>,
    channel: ShareDestination<'_>,
    page: DefinitionPage<'_>,
) -> Result<ModelDefinitionPage, DiaryReadError> {
    if model.kind() != DestinationKind::Model || channel.kind() != DestinationKind::Channel {
        return Err(DiaryReadError::InvalidDestination);
    }
    model_diary_allowed(txn, owner).await?;
    let (document, _) = definition_document_in(
        txn,
        page,
        Some(model.instance()),
        Some(channel.instance()),
        false,
    )
    .await?;
    let value = compact_document(document).map_err(|_| DiaryReadError::Database)?;
    let mut schema_versions = BTreeSet::new();
    if let Some(rows) = value["rows"].as_array() {
        for row in rows.iter().filter(|row| row["namespace"] == "user") {
            let history = row["versions"]
                .as_array()
                .ok_or(DiaryReadError::InvalidEvent)?;
            for version in std::iter::once(&row["contract"]).chain(history) {
                let id = version["id"]
                    .as_str()
                    .and_then(|id| id.parse().ok())
                    .ok_or(DiaryReadError::InvalidEvent)?;
                schema_versions.insert(id);
            }
        }
    }
    Ok(ModelDefinitionPage {
        value,
        schema_versions,
    })
}

async fn definition_document_in(
    txn: &mut Transaction<'_, Postgres>,
    page: DefinitionPage<'_>,
    model: Option<&str>,
    channel: Option<&str>,
    owner_http: bool,
) -> Result<(JsonDocument, Option<String>), DiaryReadError> {
    if !(1..=50).contains(&page.limit)
        || page.before_version.as_deref().is_some_and(|value| {
            !value.bytes().all(|byte| byte.is_ascii_digit())
                || value.is_empty()
                || value.len() > 4300
                || value.bytes().all(|byte| byte == b'0')
        })
    {
        return Err(DiaryReadError::Limit);
    }
    if page
        .before_version
        .as_deref()
        .is_some_and(|value| value.parse::<i32>().is_err())
    {
        // The frozen Python query binds this cursor as a PostgreSQL integer,
        // and only executes that query when the unfiltered definition page exists.
        let present: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT FROM event_definitions
             WHERE ($1::text IS NULL OR key>$1) AND ($2::text IS NULL OR key=$2))",
        )
        .bind(page.after_key)
        .bind(page.definition_key)
        .fetch_one(&mut **txn)
        .await?;
        if present {
            return Err(DiaryReadError::Database);
        }
    }
    let mut output = JsonSpool::default();
    let mut cursor = None;
    let mut chunks = sqlx::query_as::<_, (Vec<u8>, Option<String>)>(concat!(
        include_str!("definition_discovery.sql"),
        include_str!("../share_policy/consent.sql"),
        include_str!("definition_discovery_page.sql"),
    ))
    .bind(page.after_key)
    .bind(page.definition_key)
    .bind(page.before_version.as_deref())
    .bind(i64::from(page.limit))
    .bind(model)
    .bind(owner_http)
    .bind(channel)
    .fetch(&mut **txn);
    while let Some((chunk, next_cursor)) = chunks.try_next().await? {
        output
            .write_all(&chunk)
            .map_err(|_| DiaryReadError::Database)?;
        cursor = next_cursor;
    }
    drop(chunks);
    Ok((
        output.finish().map_err(|_| DiaryReadError::Database)?,
        cursor,
    ))
}
