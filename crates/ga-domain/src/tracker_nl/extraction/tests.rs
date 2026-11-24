use std::collections::BTreeSet;

use serde_json::{Value, json};

use super::*;

fn byte(text: &str, point: &Value) -> Result<usize, Box<dyn Error>> {
    let point = point
        .as_u64()
        .and_then(|point| usize::try_from(point).ok())
        .ok_or("Expected source code point")?;
    Ok(from_code_points(text, point..point)
        .ok_or("Expected source offset")?
        .utf8
        .start)
}
fn points(text: &str, spans: impl IntoIterator<Item = (usize, usize)>) -> Value {
    json!(
        spans
            .into_iter()
            .map(|(start, end)| [text[..start].chars().count(), text[..end].chars().count()])
            .collect::<Vec<_>>()
    )
}

#[test]
fn matches_frozen_value_and_context_evidence() -> Result<(), Box<dyn Error>> {
    let mut captured: Value = serde_json::from_str(include_str!(
        "../../../contracts/tracker-extraction-evidence.json"
    ))?;
    let bounds: Value = serde_json::from_str(include_str!(
        "../../../contracts/tracker-extraction-calendar-bounds.json"
    ))?;
    captured["evidence"]
        .as_array_mut()
        .ok_or("Expected evidence")?
        .extend(
            bounds["evidence"]
                .as_array()
                .ok_or("Expected bound cases")?
                .iter()
                .cloned(),
        );
    let mut checked = 0;
    for case in captured["evidence"].as_array().ok_or("Expected evidence")? {
        let args = case["args"].as_array().ok_or("Expected arguments")?;
        let kwargs = &case["kwargs"];
        let function = case["function"].as_str().ok_or("Expected function")?;
        let result = match function {
            "_has_uncertain_fact_context"
            | "_has_contextual_negation"
            | "_approximate_temporal_span" => {
                let source = args[0].as_str().ok_or("Expected source")?;
                let start = byte(source, &args[1])?;
                let end = byte(source, &args[2])?;
                json!(match function {
                    "_has_uncertain_fact_context" => context::uncertain(source, start, end)?,
                    "_has_contextual_negation" => context::negated(source, start, end)?,
                    _ => context::approximate(source, start, end)?,
                })
            }
            "_temporal_number_spans" => {
                let source = args[0].as_str().ok_or("Expected source")?;
                points(source, clock::temporal_spans(source)?)
            }
            "_clock_tokens" => {
                let source = args[0].as_str().ok_or("Expected source")?;
                let interval: Option<[[usize; 2]; 2]> = if kwargs["interval_clocks"].is_null() {
                    None
                } else {
                    Some(serde_json::from_value(kwargs["interval_clocks"].clone())?)
                };
                let (clocks, ranges) =
                    clock::tokens(source, kwargs["interval_hyphens"] == true, interval)?;
                json!([
                    clocks
                        .into_iter()
                        .map(|[start, end, hour, minute]| [
                            source[..start].chars().count(),
                            source[..end].chars().count(),
                            hour,
                            minute
                        ])
                        .collect::<Vec<_>>(),
                    ranges
                        .into_iter()
                        .map(|byte| source[..byte].chars().count())
                        .collect::<Vec<_>>()
                ])
            }
            "_complete_number_in_source" => {
                let source = args[0].as_str().ok_or("Expected source")?;
                let mut attached = BTreeSet::new();
                if let Some(items) = args
                    .get(3)
                    .or_else(|| kwargs.get("attached_unit_starts"))
                    .and_then(Value::as_array)
                {
                    for point in items {
                        attached.insert(byte(source, point)?);
                    }
                }
                json!(values::complete_number(
                    source,
                    byte(source, &args[1])?,
                    byte(source, &args[2])?,
                    &attached
                ))
            }
            "_value_is_evidenced" | "_categorical_value_is_evidenced" | "_unit_spans" => {
                let quote = args[1].as_str().ok_or("Expected quote")?;
                let source = kwargs["source_text"].as_str().unwrap_or(quote);
                let offset = if kwargs["source_text"].is_string() {
                    byte(source, kwargs.get("source_start").unwrap_or(&json!(0)))?
                } else {
                    0
                };
                if function == "_unit_spans" {
                    points(
                        source,
                        values::unit_spans(args[0].as_str(), quote, source, offset)?,
                    )
                } else if function == "_categorical_value_is_evidenced" {
                    json!(values::categorical(&args[0], quote, source, offset)?)
                } else {
                    let mut attached = BTreeSet::new();
                    if let Some(items) = kwargs["attached_unit_starts"].as_array() {
                        for point in items {
                            attached.insert(byte(source, point)?);
                        }
                    }
                    json!(values::value(
                        &args[0],
                        quote,
                        source,
                        offset,
                        kwargs["semantic"].as_str(),
                        &attached
                    )?)
                }
            }
            "_explicit_offset_matches" => {
                let quote = args[0].as_str().ok_or("Expected offset quote")?;
                let seconds = args[1]["seconds"]
                    .as_number()
                    .map(|value| value.as_str().trim_end_matches(".0").parse::<i32>())
                    .transpose()?;
                json!(calendar::explicit_offset(quote, seconds)?)
            }
            "_datetime_evidence" => {
                let value = StoredInstant::legacy_input_with_offset(&args[0]["datetime"])
                    .ok_or("Expected proposed time")?;
                let quote = args[1].as_str().ok_or("Expected time quote")?;
                let timezone = args[2].as_str().ok_or("Expected timezone")?;
                let now = StoredInstant::from_legacy_input(&args[3]["datetime"])
                    .ok_or("Expected trusted message time")?;
                let source = kwargs["text"].as_str().unwrap_or(quote);
                let source_start = if kwargs["quote_start"].is_number() {
                    byte(source, &kwargs["quote_start"])?
                } else {
                    source.find(quote).ok_or("Expected matching quote")?
                };
                let interval_clocks = if kwargs["interval_clocks"].is_null() {
                    None
                } else {
                    Some(serde_json::from_value(kwargs["interval_clocks"].clone())?)
                };
                let mut non_temporal = BTreeSet::new();
                if let Some(items) = kwargs["non_temporal_clock_spans"].as_array() {
                    for span in items {
                        non_temporal.insert((byte(source, &span[0])?, byte(source, &span[1])?));
                    }
                }
                calendar::evidence(
                    value,
                    calendar::DatetimeSource {
                        quote,
                        source,
                        source_start,
                        timezone,
                        now,
                        interval_hyphens: kwargs["interval_hyphens"] == true,
                        interval_clocks,
                        non_temporal: &non_temporal,
                    },
                )?
            }
            "_verify_evidence" => {
                let source = args[0].as_str().ok_or("Expected source")?;
                let result = verified_quote(source, &args[1]);
                assert_eq!(result.is_err(), case.get("error").is_some(), "{case}");
                checked += 1;
                continue;
            }
            "_dotted_quantity_spans" => {
                let text = args[0].as_str().ok_or("Expected source")?;
                let fields = args[1].as_array().ok_or("Expected fields")?;
                let metadata = args[2]
                    .as_object()
                    .ok_or("Expected metadata")?
                    .iter()
                    .map(|(key, value)| (key.as_str(), value))
                    .collect();
                points(
                    text,
                    submission::dotted_quantity_spans(text, fields, &metadata)?,
                )
            }
            "_validated_submission" => {
                let text = args[0].as_str().ok_or("Expected source")?;
                let extraction = TrackerExtraction {
                    document: args[1].clone(),
                };
                let timezone = args[4].as_str().ok_or("Expected timezone")?;
                let now = StoredInstant::from_legacy_input(&args[5]["datetime"])
                    .ok_or("Expected trusted message time")?;
                let result =
                    extraction.validated_submission(text, &args[2], &args[3], timezone, now);
                if let Some(error) = case["error"].as_str() {
                    let error_type = match result {
                        Err(ExtractionError::Fact) => "FactContextError",
                        Err(ExtractionError::Field) => "FieldEvidenceError",
                        Err(ExtractionError::Temporal { .. }) => "TemporalEvidenceError",
                        Err(ExtractionError::Invalid) => "ValueError",
                        _ => panic!("Expected source error {case}"),
                    };
                    assert_eq!(error_type, error, "{case}");
                    checked += 1;
                    continue;
                }
                let result = result?;
                json!([result.document, result.evidence_refs])
            }
            _ => continue,
        };
        assert!(case.get("error").is_none(), "Expected source error {case}");
        assert_eq!(result, case["result"], "{case}");
        checked += 1;
    }
    assert_eq!(
        checked,
        captured["evidence"]
            .as_array()
            .ok_or("Expected evidence")?
            .len()
    );
    println!("Matched {checked} frozen value, context, unit and clock calls");
    Ok(())
}

#[test]
fn matches_frozen_model_extraction_normalization() -> Result<(), Box<dyn Error>> {
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../../../contracts/tracker-extraction-models.json"
    ))?;
    for case in &rows {
        let input = case["raw_input"]
            .as_str()
            .map(serde_json::from_str)
            .transpose()?
            .unwrap_or_else(|| case["input"].clone());
        let result = TrackerExtraction::from_value(&input);
        assert_eq!(result.is_ok(), case["valid"], "{case}");
        if let Ok(result) = result {
            assert_eq!(result.document(), &case["document"], "{case}");
        }
    }
    println!("Matched {} frozen model extraction contracts", rows.len());
    Ok(())
}
