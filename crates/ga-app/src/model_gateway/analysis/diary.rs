use std::sync::LazyLock;

use ga_db::{
    Database,
    conversation_memory::AnalysisSubject,
    diary::{DefinitionPage, DiaryEventRange, EVENT_DEFINITIONS},
    health_analysis::{
        ACTIVITIES, ACTIVITY_DETAILS, AnalysisTimelineQuery, COFFEE_SLEEP, DATA_FRESHNESS,
        DEVICE_HISTORY, DailyAnalysisAccess, DailyAnalysisEvidence, DailyAnalysisQuery,
        EVENT_WINDOWS, HEALTH_RANGE, HEALTH_SNAPSHOT, INSIGHTS_LIST, LAGGED_ASSOCIATION,
        METRIC_SERIES, MIGRAINE_WINDOWS, PERIOD_COMPARISON, PERSONAL_BASELINE, RUNNING_EFFICIENCY,
        SLEEP_ANALYSIS, STRESS_HOURS, TIMELINE, WELLBEING_OBSERVATIONS,
        generic::{GENERIC_ANALYSIS, GenericAnalysisError, GenericAnalysisEvidence},
    },
    resource_catalog::{CatalogError, RESOURCE_CATALOG, ResourcePage},
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

static DEFINITIONS: LazyLock<Value> =
    LazyLock::new(|| model_description(ReadTool::EventDefinitions));
static DESCRIPTION: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Events));
static BASELINE: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Baseline));
static COMPARISON: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::ComparePeriods));
static SLEEP: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Sleep));
static FRESHNESS: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Freshness));
static GENERIC: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::GenericAnalysis));
static LAGS: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::LaggedAssociation));
static TIMELINE_TOOL: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Timeline));
static INSIGHTS: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Insights));
static WINDOWS: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::EventWindows));
static COFFEE: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::CoffeeSleep));
static SNAPSHOT: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Snapshot));
static RANGE: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Range));
static SERIES: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::MetricSeries));
static RUNNING: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::RunningEfficiency));
static ACTIVITY_LIST: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::Activities));
static DETAILS: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::ActivityDetails));
static DEVICES: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::DeviceHistory));
static STRESS: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::StressHours));
static WELLBEING: LazyLock<Value> =
    LazyLock::new(|| model_description(ReadTool::WellbeingObservations));
static MIGRAINE: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::MigraineWindows));
static CATALOG: LazyLock<Value> = LazyLock::new(|| model_description(ReadTool::ResourceCatalog));

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
    wellbeing_read: Option<(&'a str, StoredInstant)>,
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
            wellbeing_read: None,
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
        let authorized = |categories| {
            let operation = if uses_voice {
                ModelOperation::StructuredFromVoice(categories)
            } else {
                ModelOperation::Structured(categories)
            };
            authorized_models(binding, policy.configuration, policy.consent, operation, at).is_ok()
        };
        if authorized(ModelCategories::HEALTH_AND_DIARY)
            && let Some(access) = self.database.analysis_health_access(self.context).await
        {
            self.daily_read = Some((configured_timezone, question_at, access));
        }
        let wellbeing = if let Some((_, _, access)) = &self.daily_read {
            access.wellbeing
        } else {
            authorized(ModelCategories::DIARY)
                && self.database.analysis_wellbeing_access(self.context).await
        };
        if wellbeing {
            self.wellbeing_read = Some((configured_timezone, question_at));
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
            OfferedReadTool {
                name: EVENT_DEFINITIONS,
                description: &DEFINITIONS,
            },
            OfferedReadTool {
                name: RESOURCE_CATALOG,
                description: &CATALOG,
            },
        ];
        if self.wellbeing_read.is_some() {
            offered.push(OfferedReadTool {
                name: WELLBEING_OBSERVATIONS,
                description: &WELLBEING,
            });
        }
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
            offered.extend([
                OfferedReadTool {
                    name: METRIC_SERIES,
                    description: &SERIES,
                },
                OfferedReadTool {
                    name: EVENT_WINDOWS,
                    description: &WINDOWS,
                },
            ]);
            if access.summaries {
                offered.extend([
                    OfferedReadTool {
                        name: HEALTH_SNAPSHOT,
                        description: &SNAPSHOT,
                    },
                    OfferedReadTool {
                        name: HEALTH_RANGE,
                        description: &RANGE,
                    },
                    OfferedReadTool {
                        name: TIMELINE,
                        description: &TIMELINE_TOOL,
                    },
                ]);
            }
            if access.coffee_sleep {
                offered.push(OfferedReadTool {
                    name: COFFEE_SLEEP,
                    description: &COFFEE,
                });
            }
            if access.running {
                offered.push(OfferedReadTool {
                    name: RUNNING_EFFICIENCY,
                    description: &RUNNING,
                });
            }
            if access.training {
                offered.extend([
                    OfferedReadTool {
                        name: DEVICE_HISTORY,
                        description: &DEVICES,
                    },
                    OfferedReadTool {
                        name: ACTIVITIES,
                        description: &ACTIVITY_LIST,
                    },
                    OfferedReadTool {
                        name: ACTIVITY_DETAILS,
                        description: &DETAILS,
                    },
                ]);
            }
            if access.wellbeing {
                offered.push(OfferedReadTool {
                    name: STRESS_HOURS,
                    description: &STRESS,
                });
            }
            if access.insights {
                offered.push(OfferedReadTool {
                    name: INSIGHTS_LIST,
                    description: &INSIGHTS,
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
        if name == EVENT_DEFINITIONS {
            let page = DefinitionPage::parse(args)?;
            let result = self
                .database
                .read_analysis_definitions(self.context, self.model, page)
                .await
                .ok()?;
            return Some(AnalysisToolRead {
                value: result.value,
                schema_versions: result.schema_versions,
                ..Default::default()
            });
        }
        if name == RESOURCE_CATALOG {
            let page = ResourcePage::parse(args).ok()?;
            return match self
                .database
                .read_model_resource_catalog(self.context, self.model, self.categories(), page)
                .await
            {
                Ok(read) => Some(AnalysisToolRead {
                    value: read.value,
                    schema_versions: read.schema_versions,
                    ..Default::default()
                }),
                Err(CatalogError::ResultTooLarge) => Some(AnalysisToolRead {
                    value: json!({
                        "error": "result_too_large",
                        "instruction": "Request a smaller page or one resource. Do not treat this as empty data.",
                    }),
                    ..Default::default()
                }),
                Err(_) => None,
            };
        }
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
                    ..Default::default()
                }),
                Err(GenericAnalysisError::ResultTooLarge) => Some(AnalysisToolRead {
                    value: json!({
                        "error": "result_too_large",
                        "instruction": "Narrow the date range or use an analysis tool. Do not treat this as empty data.",
                    }),
                    ..Default::default()
                }),
                Err(_) => None,
            };
        }
        if name == TIMELINE {
            self.daily_read.as_ref()?;
            let query = AnalysisTimelineQuery::from_arguments(args)?;
            let result = self
                .database
                .read_analysis_timeline(self.context, self.model, &query)
                .await
                .ok()?;
            return Some(AnalysisToolRead {
                value: result.value,
                tracker_versions: result.tracker_versions,
                ..Default::default()
            });
        }
        let read_access = if name == WELLBEING_OBSERVATIONS {
            self.wellbeing_read
        } else {
            self.daily_read
                .as_ref()
                .map(|&(timezone, at, _)| (timezone, at))
        };
        if let Some((timezone, at)) = read_access
            && DailyAnalysisEvidence::is_daily_tool(name)
        {
            let query = DailyAnalysisQuery::from_arguments(name, args)?;
            return Some(AnalysisToolRead {
                value: self
                    .database
                    .read_analysis_daily(self.context, &query, timezone, at)
                    .await
                    .ok()??,
                ..Default::default()
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
            ..Default::default()
        })
    }
}
