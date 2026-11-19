//! Request identity and local relevance for generated tracker text commands.

use std::collections::BTreeSet;

use serde_json::{Value, json};
use uuid::Uuid;

use crate::{event_schema::chat_form::casefold, sha256_hex, tracker_selection::alnum};

#[derive(Debug, Clone, Copy)]
pub struct InvalidTrackerTextRequest;

impl std::fmt::Display for InvalidTrackerTextRequest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Invalid tracker text request")
    }
}
impl std::error::Error for InvalidTrackerTextRequest {}

pub struct TrackerTextRequest {
    pub text: String,
    pub operation_id: String,
    pub selected_event_id: Option<Uuid>,
    pub selected_definition_version_id: Option<Uuid>,
}

impl TrackerTextRequest {
    pub fn from_value(raw: &Value) -> Result<Self, InvalidTrackerTextRequest> {
        let object = raw.as_object().ok_or(InvalidTrackerTextRequest)?;
        if object.keys().any(|key| {
            !matches!(
                key.as_str(),
                "text" | "operation_id" | "selected_event_id" | "selected_definition_version_id"
            )
        }) {
            return Err(InvalidTrackerTextRequest);
        }
        let text = |key, max| {
            object
                .get(key)
                .and_then(Value::as_str)
                .filter(|value| (1..=max).contains(&value.chars().count()))
                .map(str::to_owned)
                .ok_or(InvalidTrackerTextRequest)
        };
        let identifier = |key| match object.get(key) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(value))
                if !value.contains(':') || value.starts_with("urn:uuid:") =>
            {
                Uuid::parse_str(value)
                    .map(Some)
                    .map_err(|_| InvalidTrackerTextRequest)
            }
            _ => Err(InvalidTrackerTextRequest),
        };
        Ok(Self {
            text: text("text", 16000)?,
            operation_id: text("operation_id", 160)?,
            selected_event_id: identifier("selected_event_id")?,
            selected_definition_version_id: identifier("selected_definition_version_id")?,
        })
    }

    pub fn document(&self) -> Value {
        json!({"text":self.text,"operation_id":self.operation_id,"selected_event_id":self.selected_event_id.map(|id|id.to_string()),"selected_definition_version_id":self.selected_definition_version_id.map(|id|id.to_string())})
    }

    pub fn request_hashes(&self) -> Result<Vec<String>, InvalidTrackerTextRequest> {
        let mut document = self.document();
        let mut hashes = vec![sha256_hex(
            &serde_json::to_vec(&document).map_err(|_| InvalidTrackerTextRequest)?,
        )];
        if self.selected_definition_version_id.is_none() {
            document
                .as_object_mut()
                .ok_or(InvalidTrackerTextRequest)?
                .remove("selected_definition_version_id");
            hashes.push(sha256_hex(
                &serde_json::to_vec(&document).map_err(|_| InvalidTrackerTextRequest)?,
            ));
        }
        Ok(hashes)
    }

    pub fn operation_key(&self, actor: &str) -> String {
        format!(
            "nl-operation:{}",
            sha256_hex(format!("{actor}\0{}", self.operation_id).as_bytes())
        )
    }
}

fn terms(value: &str) -> BTreeSet<String> {
    casefold(value)
        .split(|ch| !alnum(ch))
        .filter(|token| token.chars().count() >= 3)
        .map(str::to_owned)
        .collect()
}

pub struct TrackerRelevance {
    wanted: BTreeSet<String>,
}

impl TrackerRelevance {
    pub fn new(text: &str) -> Self {
        Self {
            wanted: terms(text),
        }
    }

    pub fn score<'a>(&self, values: impl IntoIterator<Item = &'a str>) -> usize {
        let available: BTreeSet<_> = values.into_iter().flat_map(terms).collect();
        let exact = self.wanted.intersection(&available).count();
        let prefix = self
            .wanted
            .iter()
            .filter(|word| {
                available
                    .iter()
                    .any(|term| term.chars().take(5).eq(word.chars().take(5)))
            })
            .count();
        exact * 10 + prefix
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_frozen_tracker_text_requests_and_scores() -> Result<(), Box<dyn std::error::Error>> {
        let captured: Value =
            serde_json::from_str(include_str!("../contracts/tracker-nl-contracts.json"))?;
        for case in captured["requests"].as_array().ok_or("Expected requests")? {
            let parsed = TrackerTextRequest::from_value(&case["input"]);
            assert_eq!(parsed.is_ok(), case["valid"], "request {}", case["input"]);
            if let Ok(request) = parsed {
                assert_eq!(request.document(), case["document"]);
                assert_eq!(json!(request.request_hashes()?), case["hashes"]);
            }
        }
        for case in captured["scores"].as_array().ok_or("Expected scores")? {
            let values = case[1]
                .as_array()
                .ok_or("Expected values")?
                .iter()
                .map(|value| value.as_str().ok_or("Expected text"))
                .collect::<Result<Vec<_>, _>>()?;
            assert_eq!(
                json!(
                    TrackerRelevance::new(case[0].as_str().ok_or("Expected text")?).score(values)
                ),
                case[2],
                "score {case}"
            );
        }
        Ok(())
    }
}
