use std::{io::Write, time::Duration};

use futures_util::TryStreamExt;
use ga_archive::json_spool::{JsonDocument, JsonSpool};
use uuid::Uuid;

use super::{DiaryReadError, reads::model_diary_allowed};
use crate::Database;

pub struct DefinitionPage<'a> {
    pub after_key: Option<&'a str>,
    pub definition_key: Option<&'a str>,
    pub before_version: Option<String>,
    pub limit: u16,
}

impl Database {
    /// Discover immutable contracts with version-specific model sharing checks.
    pub async fn event_definitions(
        &self,
        page: DefinitionPage<'_>,
        model_owner: Option<Uuid>,
    ) -> Result<JsonDocument, DiaryReadError> {
        let work = async {
            let mut txn = self.begin_diary_read().await?;
            sqlx::query("SET LOCAL statement_timeout = '120s'")
                .execute(&mut *txn)
                .await?;
            if let Some(owner) = model_owner {
                model_diary_allowed(&mut txn, owner).await?;
            }
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
                .fetch_one(&mut *txn)
                .await?;
                if present {
                    return Err(DiaryReadError::Database);
                }
            }
            let mut output = JsonSpool::default();
            let mut chunks = sqlx::query_scalar::<_, Vec<u8>>(concat!(
                include_str!("definition_discovery.sql"),
                include_str!("../share_policy/consent.sql"),
                include_str!("definition_discovery_page.sql"),
            ))
            .bind(page.after_key)
            .bind(page.definition_key)
            .bind(page.before_version.as_deref())
            .bind(i64::from(page.limit))
            .bind(model_owner.map(|_| "model:mcp:local"))
            .fetch(&mut *txn);
            while let Some(chunk) = chunks.try_next().await? {
                output
                    .write_all(&chunk)
                    .map_err(|_| DiaryReadError::Database)?;
            }
            drop(chunks);
            txn.commit().await?;
            output.finish().map_err(|_| DiaryReadError::Database)
        };
        tokio::time::timeout(Duration::from_secs(120), work)
            .await
            .map_err(|_| DiaryReadError::Database)?
    }
}
