use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Value, json};

use super::{
    ExtractionError, TrackerExtraction,
    calendar::{self, DatetimeSource},
    clock, context,
    patterns::regex,
    values, verified_quote,
};
use crate::{
    event_input::known_timezone,
    event_payload::python_whitespace,
    file_import::decimal::Decimal,
    timestamp::{StoredInstant, python_timezone},
};

pub struct ValidatedTrackerSubmission {
    pub document: Value,
    pub evidence_refs: Vec<Value>,
}

impl TrackerExtraction {
    pub fn validated_submission(
        &self,
        text: &str,
        candidate: &Value,
        form: &Value,
        timezone: &str,
        now: StoredInstant,
    ) -> Result<ValidatedTrackerSubmission, ExtractionError> {
        let extraction = &self.document;
        let fields = extraction["fields"]
            .as_array()
            .ok_or(ExtractionError::Invalid)?;
        if fields.is_empty() && regex("negation")?.is_match(text)? {
            return Err(ExtractionError::Fact);
        }
        let start = extraction
            .get("start")
            .filter(|value| !value.is_null())
            .unwrap_or(&form["initial_start"]);
        let end = extraction
            .get("end")
            .filter(|value| !value.is_null())
            .unwrap_or(&form["initial_end"]);
        let start_time =
            StoredInstant::legacy_input_with_offset(start).ok_or(ExtractionError::Invalid)?;
        let end_time = if end.is_null() {
            None
        } else {
            Some(StoredInstant::legacy_input_with_offset(end).ok_or(ExtractionError::Invalid)?)
        };
        let timezone = form["initial_timezone"]
            .as_str()
            .filter(|value| !value.is_empty())
            .unwrap_or(timezone);
        let zone = python_timezone(timezone).ok_or(ExtractionError::Invalid)?;
        let local_clock = |time| -> Result<[usize; 2], ExtractionError> {
            let instant = StoredInstant::try_from(time).map_err(|_| ExtractionError::Invalid)?;
            let (local, _) = calendar::local_datetime(instant, &zone)?;
            Ok([local.hour() as usize, local.minute() as usize])
        };
        let interval_clocks = if form["topology"] == "bounded_interval"
            && !extraction["start"].is_null()
            && !extraction["end"].is_null()
        {
            Some([
                local_clock(start_time)?,
                local_clock(end_time.ok_or(ExtractionError::Invalid)?)?,
            ])
        } else {
            None
        };
        let metadata: BTreeMap<_, _> = candidate["fields"]
            .as_array()
            .ok_or(ExtractionError::Invalid)?
            .iter()
            .map(|field| {
                field["field_id"]
                    .as_str()
                    .map(|id| (id, field))
                    .ok_or(ExtractionError::Invalid)
            })
            .collect::<Result<_, _>>()?;
        let non_temporal = dotted_quantity_spans(text, fields, &metadata)?;
        if form["topology"] == "bounded_interval" && end_time.is_none() {
            return Err(ExtractionError::Invalid);
        }
        for (field, time) in [("start", Some(start_time)), ("end", end_time)] {
            if extraction[field].is_null() {
                continue;
            }
            let evidence = &extraction[format!("{field}_evidence")];
            let (quote, source_start) = verified_quote(text, evidence)?;
            if context::uncertain(text, source_start, source_start + quote.len())? {
                return Err(ExtractionError::Fact);
            }
            let evidence = calendar::evidence(
                time.ok_or(ExtractionError::Invalid)?,
                DatetimeSource {
                    quote,
                    source: text,
                    source_start,
                    timezone,
                    now,
                    interval_hyphens: interval_clocks.is_some(),
                    interval_clocks,
                    non_temporal: &non_temporal,
                },
            )?;
            if evidence["status"] != "supported" {
                return Err(ExtractionError::Temporal { field, evidence });
            }
        }
        let mut mapped = form["initial_values"]
            .as_object()
            .ok_or(ExtractionError::Invalid)?
            .clone();
        let mut units = form["initial_units"]
            .as_object()
            .ok_or(ExtractionError::Invalid)?
            .clone();
        let mut seen = BTreeSet::new();
        let mut evidence_refs = Vec::new();
        for field in fields {
            let id = field["field_id"].as_str().ok_or(ExtractionError::Invalid)?;
            let contract = metadata.get(id).ok_or(ExtractionError::Invalid)?;
            if !seen.insert(id) {
                return Err(ExtractionError::Invalid);
            }
            let (quote, offset) = verified_quote(text, &field["evidence"])?;
            if context::uncertain(text, offset, offset + quote.len())? {
                return Err(ExtractionError::Fact);
            }
            let expected_unit = contract["unit"].as_str();
            let mut attached = BTreeSet::new();
            if contract["semantic"] == "quantity" {
                if field["unit"].as_str() != expected_unit || field["unit_evidence"].is_null() {
                    return Err(ExtractionError::Invalid);
                }
                let (unit_quote, unit_offset) = verified_quote(text, &field["unit_evidence"])?;
                let spans = values::unit_spans(expected_unit, unit_quote, text, unit_offset)?;
                if spans.is_empty() {
                    return Err(ExtractionError::Field);
                }
                attached.extend(spans.into_iter().map(|(start, _)| start));
            } else if !field["unit"].is_null() && field["unit"].as_str() != expected_unit {
                return Err(ExtractionError::Invalid);
            }
            let supported = if contract["semantic"] == "nominal" {
                values::categorical(&field["value"], quote, text, offset)?
            } else {
                values::value(&field["value"], quote, text, offset, None, &attached)?
            };
            if !supported {
                return Err(ExtractionError::Field);
            }
            let name = contract["name"].as_str().ok_or(ExtractionError::Invalid)?;
            mapped.insert(name.to_owned(), field["value"].clone());
            if let Some(unit) = expected_unit.filter(|unit| !unit.is_empty()) {
                units.insert(name.to_owned(), json!(unit));
            }
            evidence_refs.push(json!({"schema_version":"tracker.nl.v1","field_id":id,"start":field["evidence"]["start"],"end":field["evidence"]["end"]}));
        }
        if !known_timezone(timezone)
            || end_time.is_some_and(|end| end < start_time)
            || mapped.len() > 32
            || units.len() > 32
        {
            return Err(ExtractionError::Invalid);
        }
        let start = StoredInstant::legacy_input_with_json(start)
            .ok_or(ExtractionError::Invalid)?
            .1;
        let end = if end.is_null() {
            None
        } else {
            Some(
                StoredInstant::legacy_input_with_json(end)
                    .ok_or(ExtractionError::Invalid)?
                    .1,
            )
        };
        Ok(ValidatedTrackerSubmission {
            document: json!({"action_id":form["id"],"operation_id":null,"schema_hash":form["schema_hash"],"submission_id":null,"start":start,"end":end,"timezone":timezone,"values":mapped,"units":units}),
            evidence_refs,
        })
    }
}

pub(super) fn dotted_quantity_spans(
    text: &str,
    fields: &[Value],
    metadata: &BTreeMap<&str, &Value>,
) -> Result<BTreeSet<(usize, usize)>, ExtractionError> {
    let temporal = clock::temporal_spans(text)?;
    let mut result = BTreeSet::new();
    for field in fields {
        let Some(id) = field["field_id"].as_str() else {
            continue;
        };
        let Some(contract) = metadata.get(id) else {
            continue;
        };
        if contract["semantic"] != "quantity" || !field["value"].is_number() {
            continue;
        }
        let Some(unit) = contract["unit"].as_str().filter(|value| !value.is_empty()) else {
            continue;
        };
        if field["unit"] != unit {
            continue;
        }
        let Ok((number, start)) = verified_quote(text, &field["evidence"]) else {
            continue;
        };
        let Ok((unit_quote, unit_start)) = verified_quote(text, &field["unit_evidence"]) else {
            continue;
        };
        let end = start + number.len();
        if !regex("decimal_quantity")?.is_match(number)?
            || unit_start < end
            || !text[end..unit_start]
                .trim_matches(python_whitespace)
                .is_empty()
        {
            continue;
        }
        let spans = values::unit_spans(Some(unit), unit_quote, text, unit_start)?;
        let attached = spans.iter().map(|&(start, _)| start).collect();
        if spans.is_empty()
            || !values::complete_number(text, start, end, &attached)
            || temporal
                .iter()
                .any(|&(begin, stop)| start < stop && end > begin)
        {
            continue;
        }
        let token = values::normalize_decimal(number).ok_or(ExtractionError::Invalid)?;
        if Decimal::new(&token).as_ref() == Some(&values::numeric_decimal(&field["value"])?) {
            result.insert((start, end));
        }
    }
    Ok(result)
}
