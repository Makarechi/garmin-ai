use std::sync::LazyLock;

use ga_db::{
    Database,
    conversation_memory::AnalysisReadContext,
    diary::DiaryEventRange,
    share_policy::{DestinationKind, ShareDestination, SharePolicyError},
};
use ga_domain::{model_consent::ModelCategories, timestamp::StoredInstant};
use serde_json::{Map, Value, json};

use super::{AnalysisReadTools, AnalysisToolRead, OfferedReadTool};

const EVENTS: &str = "events";

static DESCRIPTION: LazyLock<Value> = LazyLock::new(|| {
    json!({
        "name": EVENTS,
        "description": "Read confirmed, inferred, or pending diary events with their status; excludes deleted records.",
        "schema": {
            "additionalProperties": false,
            "properties": {
                "start": {"format": "date-time", "title": "Start", "type": "string"},
                "end": {"format": "date-time", "title": "End", "type": "string"},
                "kind": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Kind"}
            },
            "required": ["start", "end"],
            "title": "eventsArgs",
            "type": "object"
        }
    })
});

/// A diary-only model tool bound to an authenticated question and its configured model.
pub struct DiaryAnalysisTools<'a> {
    database: &'a Database,
    context: AnalysisReadContext<'a>,
    model: ShareDestination<'a>,
}

impl<'a> DiaryAnalysisTools<'a> {
    pub fn new(
        database: &'a Database,
        context: AnalysisReadContext<'a>,
        model_instance: &'a str,
    ) -> Result<Self, SharePolicyError> {
        Ok(Self {
            database,
            context,
            model: ShareDestination::new(DestinationKind::Model, model_instance)?,
        })
    }

    pub fn offered_tool(&self) -> OfferedReadTool<'static> {
        OfferedReadTool {
            name: EVENTS,
            description: &DESCRIPTION,
        }
    }
}

impl AnalysisReadTools for DiaryAnalysisTools<'_> {
    fn categories(&self) -> ModelCategories {
        ModelCategories::DIARY
    }

    async fn context_current(&self) -> bool {
        self.database
            .analysis_diary_context_current(self.context)
            .await
    }

    async fn read(&self, name: &str, args: &Map<String, Value>) -> Option<AnalysisToolRead> {
        if name != EVENTS {
            return None;
        }
        let page = self
            .database
            .list_analysis_events(self.context, self.model, event_range(args)?)
            .await
            .ok()?;
        Some(AnalysisToolRead {
            value: page.value,
            tracker_versions: page.tracker_versions,
        })
    }
}

fn event_range(args: &Map<String, Value>) -> Option<DiaryEventRange<'_>> {
    if args
        .keys()
        .any(|key| !matches!(key.as_str(), "start" | "end" | "kind"))
    {
        return None;
    }
    let start = StoredInstant::from_legacy_input(args.get("start")?)?;
    let end = StoredInstant::from_legacy_input(args.get("end")?)?;
    let kind = match args.get("kind") {
        None | Some(Value::Null) => None,
        Some(value) => Some(value.as_str()?),
    };
    Some(DiaryEventRange {
        start,
        end,
        kind,
        limit: 500,
    })
}
