use std::sync::LazyLock;

use ga_db::{
    Database,
    conversation_memory::AnalysisSubject,
    diary::DiaryEventRange,
    health_analysis::{
        DATA_FRESHNESS, DailyAnalysisAccess, DailyAnalysisEvidence, DailyAnalysisQuery,
        LAGGED_ASSOCIATION, MIGRAINE_WINDOWS, PERIOD_COMPARISON, PERSONAL_BASELINE,
        RUNNING_EFFICIENCY, SLEEP_ANALYSIS, STRESS_HOURS,
        generic::{GENERIC_ANALYSIS, GenericAnalysisError, GenericAnalysisEvidence},
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
static FRESHNESS: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Freshness));
static GENERIC: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::GenericAnalysis));
static LAGS: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::LaggedAssociation));
static RUNNING: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::RunningEfficiency));
static STRESS: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::StressHours));
static MIGRAINE: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::MigraineWindows));

fn model_description(tool: ReadTool) -> Value {
    let entry = tool.catalog_entry();
    json!({"name":entry["name"],"description":entry["description"],"schema":entry["input_schema"]})
}

/// Agent reads bound to an authenticated question and its configured model.
pub struct DiaryAnalysisTools<'a> {
    database: &'a Database,
    context: AnalysisSubject<'a>,
    model: ShareDestination<'a>,
    daily_read: Option<(&'a str, StoredInstant, DailyAnalysisAccess)>,
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
            daily_read: None,
        })
    }

    pub async fn with_daily_access(
        mut self,
        binding: ModelBinding<'_>,
        policy: CurrentModelPolicy<'_>,
        at: StoredInstant,
        uses_voice: bool,
        configured_timezone: &'a str,
        question_at: StoredInstant,
    ) -> Self {
        let categories = ModelCategories::HEALTH_AND_DIARY;
        let operation = if uses_voice {
            ModelOperation::StructuredFromVoice(categories)
        } else {
            ModelOperation::Structured(categories)
        };
        if authorized_models(binding, policy.configuration, policy.consent, operation, at).is_ok()
            && let Some(access) = self.database.analysis_health_access(self.context).await
        {
            self.daily_read = Some((configured_timezone, question_at, access));
        }
        self
    }

    pub fn offered_tools(&self) -> Vec<OfferedReadTool<'static>> {
        let mut offered = vec![
            OfferedReadTool {
                name: EVENTS,
                description: &DESCRIPTION,
            },
            OfferedReadTool {
                name: GENERIC_ANALYSIS,
                description: &GENERIC,
            },
        ];
        if let Some((_, _, access)) = &self.daily_read {
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
                OfferedReadTool {
                    name: DATA_FRESHNESS,
                    description: &FRESHNESS,
                },
                OfferedReadTool {
                    name: LAGGED_ASSOCIATION,
                    description: &LAGS,
                },
            ]);
            if access.running {
                offered.push(OfferedReadTool {
                    name: RUNNING_EFFICIENCY,
                    description: &RUNNING,
                });
            }
            if access.wellbeing {
                offered.push(OfferedReadTool {
                    name: STRESS_HOURS,
                    description: &STRESS,
                });
            }
            if access.migraine {
                offered.push(OfferedReadTool {
                    name: MIGRAINE_WINDOWS,
                    description: &MIGRAINE,
                });
            }
        }
        offered
    }
}

impl AnalysisReadTools for DiaryAnalysisTools<'_> {
    fn categories(&self) -> ModelCategories {
        if self.daily_read.is_some() {
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

    async fn quality_report(&self) -> Option<(Value, bool)> {
        let &(zone, at, _) = self.daily_read.as_ref()?;
        self.database
            .read_analysis_quality(self.context, zone, at)
            .await
            .ok()?
    }

    async fn read(&self, name: &str, args: &Map<String, Value>) -> Option<AnalysisToolRead> {
        if name == GENERIC_ANALYSIS {
            let spec = GenericAnalysisEvidence::input(args)?;
            let read = self
                .database
                .read_model_generic(self.context, self.model, self.categories(), &spec)
                .await;
            return match read {
                Ok(read) => Some(AnalysisToolRead {
                    value: read.value,
                    tracker_versions: read.tracker_versions,
                }),
                Err(GenericAnalysisError::ResultTooLarge) => Some(AnalysisToolRead {
                    value: json!({
                        "error": "result_too_large",
                        "instruction": "Narrow the date range or use an analysis tool. Do not treat this as empty data.",
                    }),
                    tracker_versions: Default::default(),
                }),
                Err(_) => None,
            };
        }
        if let Some(&(timezone, at, _)) = self.daily_read.as_ref()
            && DailyAnalysisEvidence::is_daily_tool(name)
        {
            let query = DailyAnalysisQuery::from_arguments(name, args)?;
            return Some(AnalysisToolRead {
                value: self
                    .database
                    .read_analysis_daily(self.context, &query, timezone, at)
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
