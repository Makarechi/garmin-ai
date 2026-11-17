use std::time::Duration;

use ga_domain::model_consent::ModelCategories;
use serde_json::json;
use sqlx::{Postgres, Transaction};

use super::{ModelGateError, prepare};
use crate::Database;

impl Database {
    /// Recheck the owner's saved data categories before each provider attempt.
    pub async fn model_onboarding_allows(
        &self,
        categories: ModelCategories,
    ) -> Result<bool, ModelGateError> {
        tokio::time::timeout(Duration::from_millis(1500), async {
            let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
            prepare(&mut txn).await?;
            let allowed = onboarding_allows_model(&mut txn, categories).await?;
            txn.commit().await?;
            Ok(allowed)
        })
        .await
        .map_err(|_| ModelGateError::Database)?
    }
}

pub(crate) async fn onboarding_allows_model(
    txn: &mut Transaction<'_, Postgres>,
    categories: ModelCategories,
) -> Result<bool, sqlx::Error> {
    let requested: Vec<_> = categories.labels().collect();
    sqlx::query_scalar(
        "SELECT coalesce((SELECT CASE
            WHEN jsonb_typeof(value) <> 'object' OR NOT (value ? 'model_categories')
                THEN $1::jsonb = '[]'::jsonb
            WHEN jsonb_typeof(value->'model_categories') = 'array' THEN
                NOT EXISTS (SELECT 1 FROM jsonb_array_elements(value->'model_categories') AS item
                     WHERE jsonb_typeof(item) IN ('object','array'))
                AND value->'model_categories' @> $1::jsonb
            ELSE false END
         FROM app_state WHERE key='preferences:onboarding'), true)",
    )
    .bind(json!(requested).to_string())
    .fetch_one(&mut **txn)
    .await
}
