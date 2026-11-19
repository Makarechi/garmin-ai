use std::collections::{BTreeMap, BTreeSet};

use ga_domain::{
    event_input::known_timezone,
    legacy_json::{Utf8Layout, encode_ascii, integer_argument},
    sha256_hex,
    timestamp::StoredInstant,
};
use serde::Deserialize;
use serde_json::{Number, Value, json};
use uuid::Uuid;

use super::CalendarError;

#[derive(Clone, Copy, Deserialize, Eq, Ord, PartialEq, PartialOrd)]
#[serde(rename_all = "snake_case")]
pub(super) enum Category {
    Work,
    Personal,
    Travel,
    Exercise,
    Other,
}

impl Category {
    fn label(self) -> &'static str {
        match self {
            Self::Work => "work",
            Self::Personal => "personal",
            Self::Travel => "travel",
            Self::Exercise => "exercise",
            Self::Other => "other",
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Grant {
    id: String,
    categories: BTreeSet<Category>,
    granted_at: Value,
}

pub struct CalendarSources {
    grants: Vec<(Uuid, BTreeSet<Category>, StoredInstant)>,
}

pub(super) type AllowedSources<'a> = BTreeMap<Uuid, &'a BTreeSet<Category>>;

impl CalendarSources {
    pub fn parse(input: &str) -> Result<Self, CalendarError> {
        let configured: Vec<Grant> =
            serde_json::from_str(input).map_err(|_| CalendarError::Invalid)?;
        if configured.len() > 32 {
            return Err(CalendarError::Invalid);
        }
        let mut grants = Vec::with_capacity(configured.len());
        let mut ids = BTreeSet::new();
        for grant in configured {
            let id = Uuid::parse_str(&grant.id).map_err(|_| CalendarError::Invalid)?;
            let at = StoredInstant::from_legacy_input(&grant.granted_at)
                .ok_or(CalendarError::Invalid)?;
            if grant.categories.is_empty() || !ids.insert(id) {
                return Err(CalendarError::Invalid);
            }
            grants.push((id, grant.categories, at));
        }
        Ok(Self { grants })
    }

    pub(super) fn allowed(&self, at: StoredInstant) -> AllowedSources<'_> {
        self.grants
            .iter()
            .filter_map(|(id, categories, granted)| (*granted <= at).then_some((*id, categories)))
            .collect()
    }
}

#[derive(Clone, Copy, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
enum Status {
    Busy,
    Cancelled,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ItemFields {
    source_id: String,
    id: String,
    revision: Value,
    status: Status,
    start: Option<Value>,
    end: Option<Value>,
    timezone: Option<String>,
    category: Option<Category>,
}

pub(super) struct CalendarItem {
    pub(super) source: Uuid,
    pub(super) id: Uuid,
    pub(super) document: Value,
    pub(super) digest: String,
    pub(super) interval: Option<(StoredInstant, StoredInstant, Category)>,
}

impl CalendarItem {
    pub(super) fn parse(value: Value) -> Result<Self, CalendarError> {
        let raw: ItemFields = serde_json::from_value(value).map_err(|_| CalendarError::Invalid)?;
        let source = Uuid::parse_str(&raw.source_id).map_err(|_| CalendarError::Invalid)?;
        let id = Uuid::parse_str(&raw.id).map_err(|_| CalendarError::Invalid)?;
        let revision = integer_argument(&raw.revision)
            .map(|revision| revision.to_string())
            .filter(|revision| revision != "0" && !revision.starts_with('-'))
            .and_then(|revision| revision.parse::<Number>().ok())
            .ok_or(CalendarError::Invalid)?;
        let interval = match (
            raw.status,
            raw.start,
            raw.end,
            raw.timezone.as_deref(),
            raw.category,
        ) {
            (Status::Busy, Some(start), Some(end), Some(timezone), Some(category))
                if known_timezone(timezone) =>
            {
                let start =
                    StoredInstant::from_legacy_input(&start).ok_or(CalendarError::Invalid)?;
                let end = StoredInstant::from_legacy_input(&end).ok_or(CalendarError::Invalid)?;
                checked_range(start, end)?;
                Some((start, end, category))
            }
            (Status::Cancelled, None, None, None, None) => None,
            _ => return Err(CalendarError::Invalid),
        };
        let document = json!({
            "source_id":source.to_string(),"id":id.to_string(),"revision":revision,
            "status":if raw.status == Status::Busy {"busy"} else {"cancelled"},
            "start":interval.map(|(start,_,_)| start.to_string()),
            "end":interval.map(|(_,end,_)| end.to_string()),
            "timezone":raw.timezone,"category":interval.map(|(_,_,category)| category.label()),
        });
        let bytes = serde_json::to_vec(&document).map_err(|_| CalendarError::Invalid)?;
        let encoded =
            encode_ascii(&bytes, Utf8Layout::Archive, 8192).map_err(|_| CalendarError::Invalid)?;
        Ok(Self {
            source,
            id,
            document,
            digest: sha256_hex(encoded.as_bytes()),
            interval,
        })
    }

    pub(super) fn permitted(&self, sources: &AllowedSources<'_>) -> bool {
        sources.get(&self.source).is_some_and(|categories| {
            self.interval
                .is_none_or(|(_, _, category)| categories.contains(&category))
        })
    }
}

pub struct CalendarBatch(pub(super) Vec<CalendarItem>);

impl CalendarBatch {
    pub fn parse(value: Value) -> Result<Self, CalendarError> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Batch {
            items: Vec<Value>,
        }
        let body: Batch = serde_json::from_value(value).map_err(|_| CalendarError::Invalid)?;
        if !(1..=100).contains(&body.items.len()) {
            return Err(CalendarError::Invalid);
        }
        Ok(Self(
            body.items
                .into_iter()
                .map(CalendarItem::parse)
                .collect::<Result<_, _>>()?,
        ))
    }
}

pub(super) fn checked_range(start: StoredInstant, end: StoredInstant) -> Result<(), CalendarError> {
    let micros = end.unix_microseconds() - start.unix_microseconds();
    ((1..=31 * 86_400 * 1_000_000).contains(&micros))
        .then_some(())
        .ok_or(CalendarError::Invalid)
}
