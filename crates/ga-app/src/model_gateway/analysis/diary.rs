use std::sync::LazyLock;

use ga_db::{
    Database,
    conversation_memory::AnalysisSubject,
    diary::DiaryEventRange,
    health_analysis::{
        DailyAnalysisEvidence, DailyAnalysisQuery, PERIOD_COMPARISON, PERSONAL_BASELINE,
        SLEEP_ANALYSIS,
    },
    share_policy::{DestinationKind, ShareDestination, SharePolicyError},
};
use ga_domain::{
    model_consent::{ModelBinding, ModelCategories, ModelOperation, authorized_models},
    timestamp::StoredInstant,
};
use serde_json::{Map, Value, json};

use super::{AnalysisReadTools, AnalysisToolRead, OfferedReadTool};
use crate::{model_gateway::CurrentModelPolicy, read_tool_catalog::ReadTool};

const EVENTS: &str = "events";

static DESCRIPTION: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Events));
static BASELINE: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Baseline));
static COMPARISON: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::ComparePeriods));
static SLEEP: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Sleep));

fn model_description(tool: ReadTool) -> Value {
    let entry = tool.catalog_entry();
    json!({"name":entry["name"],"description":entry["description"],"schema":entry["input_schema"]})
}

/// Agent reads bound to an authenticated question and its configured model.
pub struct DiaryAnalysisTools<'a> {
    database: &'a Database,
    context: AnalysisSubject<'a>,
    model: ShareDestination<'a>,
    daily_timezone: Option<&'a str>,
}

impl<'a> DiaryAnalysisTools<'a> {
    pub fn new(
        database: &'a Database,
        context: impl Into<AnalysisSubject<'a>>,
        model_instance: &'a str,
    ) -> Result<Self, SharePolicyError> {
        Ok(Self {
            database,
            context: context.into(),
            model: ShareDestination::new(DestinationKind::Model, model_instance)?,
            daily_timezone: None,
        })
    }

    pub async fn with_daily_access(
        mut self,
        binding: ModelBinding<'_>,
        policy: CurrentModelPolicy<'_>,
        at: StoredInstant,
        uses_voice: bool,
        configured_timezone: &'a str,
    ) -> Self {
        let categories = ModelCategories::HEALTH_AND_DIARY;
        let operation = if uses_voice {
            ModelOperation::StructuredFromVoice(categories)
        } else {
            ModelOperation::Structured(categories)
        };
        if authorized_models(binding, policy.configuration, policy.consent, operation, at).is_ok()
            && self.database.analysis_health_allowed(self.context).await
        {
            self.daily_timezone = Some(configured_timezone);
        }
        self
    }

    pub fn offered_tools(&self) -> Vec<OfferedReadTool<'static>> {
        let mut offered = vec![OfferedReadTool {
            name: EVENTS,
            description: &DESCRIPTION,
        }];
        if self.daily_timezone.is_some() {
            offered.extend([
                OfferedReadTool {
                    name: PERSONAL_BASELINE,
                    description: &BASELINE,
                },
                OfferedReadTool {
                    name: PERIOD_COMPARISON,
                    description: &COMPARISON,
                },
                OfferedReadTool {
                    name: SLEEP_ANALYSIS,
                    description: &SLEEP,
                },
            ]);
        }
        offered
    }
}

impl AnalysisReadTools for DiaryAnalysisTools<'_> {
    fn categories(&self) -> ModelCategories {
        if self.daily_timezone.is_some() {
            ModelCategories::HEALTH_AND_DIARY
        } else {
            ModelCategories::DIARY
        }
    }

    async fn context_current(&self) -> bool {
        self.database
            .analysis_diary_context_current(self.context)
            .await
    }

    async fn read(&self, name: &str, args: &Map<String, Value>) -> Option<AnalysisToolRead> {
        if let Some(timezone) = self.daily_timezone
            && DailyAnalysisEvidence::is_daily_tool(name)
        {
            let query = DailyAnalysisQuery::from_arguments(name, args)?;
            return Some(AnalysisToolRead {
                value: self
                    .database
                    .read_analysis_daily(self.context, &query, timezone)
                    .await
                    .ok()??,
                tracker_versions: Default::default(),
            });
        }
        if name != EVENTS {
            return None;
        }
        let page = self
            .database
            .list_analysis_events(
                self.context,
                self.model,
                DiaryEventRange::from_analysis_arguments(args)?,
            )
            .await
            .ok()?;
        Some(AnalysisToolRead {
            value: page.value,
            tracker_versions: page.tracker_versions,
        })
    }
}
