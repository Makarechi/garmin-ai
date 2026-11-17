use std::{
    cmp::Ordering,
    collections::{BTreeMap, BTreeSet},
    fmt,
    io::{self, Write},
};

use serde::{Serialize, Serializer, ser::SerializeSeq};
use serde_json::Value;
use time::OffsetDateTime;

use super::{Annotation, EventFields, TimelineError};

const MAX_RESPONSE_BYTES: usize = 16 * 1024 * 1024;
const SEMANTICS: &str = "Layers preserve overlapping evidence; segment label is a legacy display projection. Calendar entries are plans, not attendance. Open symptoms have no recorded end, not confirmed persistence.";

pub(super) fn timeline(
    start: OffsetDateTime,
    end: OffsetDateTime,
    annotations: &[Annotation],
    events: &Value,
) -> Result<Vec<u8>, TimelineError> {
    let mut layers = BTreeMap::from([
        ("sleep", Vec::new()),
        ("activity", Vec::new()),
        ("wellbeing", Vec::new()),
        ("context", Vec::new()),
        ("plans", Vec::new()),
    ]);
    for (id, annotation) in annotations.iter().enumerate() {
        let layer = layers
            .get_mut(annotation.layer)
            .ok_or(TimelineError::Database)?;
        layer.push(LayerAnnotation {
            fields: Fields::known(id, annotation),
            start: Timestamp(annotation.start),
            end: (!annotation.missing_end()).then_some(Timestamp(annotation.end)),
        });
    }
    let mut output = BoundedOutput(Vec::new());
    let document = Timeline {
        segments: Segments {
            start,
            end,
            annotations,
        },
        layers,
        events,
        semantics: SEMANTICS,
    };
    serde_json::to_writer(&mut output, &document).map_err(|error| {
        if error.io_error_kind() == Some(io::ErrorKind::FileTooLarge) {
            TimelineError::ResponseLimit
        } else {
            TimelineError::Database
        }
    })?;
    Ok(output.0)
}

#[derive(Serialize)]
struct Timeline<'a> {
    segments: Segments<'a>,
    layers: BTreeMap<&'static str, Vec<LayerAnnotation<'a>>>,
    events: &'a Value,
    semantics: &'static str,
}

#[derive(Serialize)]
struct LayerAnnotation<'a> {
    #[serde(flatten)]
    fields: Fields<'a>,
    start: Timestamp,
    end: Option<Timestamp>,
}

#[derive(Serialize)]
struct Segment<'a> {
    start: Timestamp,
    end: Timestamp,
    annotations: Vec<usize>,
    #[serde(flatten)]
    fields: Fields<'a>,
    #[serde(skip_serializing_if = "Option::is_none")]
    overlapping_evidence: Option<Vec<&'a Value>>,
}

#[derive(Serialize)]
struct Fields<'a> {
    label: &'a str,
    confidence: f64,
    status: &'a str,
    source: Option<&'a str>,
    evidence: &'a Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    layer: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    annotation_id: Option<usize>,
    #[serde(flatten)]
    event: Option<&'a EventFields>,
}

impl<'a> Fields<'a> {
    fn known(id: usize, annotation: &'a Annotation) -> Self {
        Self {
            label: &annotation.label,
            confidence: annotation.confidence,
            status: &annotation.status,
            source: Some(&annotation.source),
            evidence: &annotation.evidence,
            layer: Some(annotation.layer),
            annotation_id: Some(id),
            event: annotation.event.as_ref(),
        }
    }

    fn unknown(evidence: &'a Value) -> Self {
        Self {
            label: "unknown",
            confidence: 0.0,
            status: "unknown",
            source: None,
            evidence,
            layer: None,
            annotation_id: None,
            event: None,
        }
    }
}

struct Segments<'a> {
    start: OffsetDateTime,
    end: OffsetDateTime,
    annotations: &'a [Annotation],
}

impl Serialize for Segments<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let duration: Vec<_> = self
            .annotations
            .iter()
            .enumerate()
            .filter(|(_, annotation)| {
                !annotation.missing_end() && annotation.end > annotation.start
            })
            .collect();
        let mut boundaries = BTreeSet::from([self.start, self.end]);
        for (_, annotation) in &duration {
            boundaries.insert(annotation.start);
            boundaries.insert(annotation.end);
        }
        let mut seq = serializer.serialize_seq(Some(boundaries.len() - 1))?;
        let empty = Value::Object(serde_json::Map::new());
        let mut overlapping = Vec::with_capacity(duration.len());
        for (&left, &right) in boundaries.iter().zip(boundaries.iter().skip(1)) {
            overlapping.clear();
            overlapping.extend(
                duration
                    .iter()
                    .filter(|(_, candidate)| candidate.start <= left && candidate.end >= right),
            );
            overlapping.sort_by(|(_, left), (_, right)| {
                left.priority
                    .cmp(&right.priority)
                    .then_with(|| {
                        left.confidence
                            .partial_cmp(&right.confidence)
                            .unwrap_or(Ordering::Equal)
                    })
                    .then_with(|| {
                        (&left.source, &left.evidence_order)
                            .cmp(&(&right.source, &right.evidence_order))
                    })
            });
            let (fields, overlapping_evidence) = match overlapping.split_last() {
                Some(((id, best), earlier)) => (
                    Fields::known(*id, best),
                    Some(earlier.iter().map(|(_, item)| &item.evidence).collect()),
                ),
                None => (Fields::unknown(&empty), None),
            };
            seq.serialize_element(&Segment {
                start: Timestamp(left),
                end: Timestamp(right),
                annotations: overlapping.iter().map(|(id, _)| *id).collect(),
                fields,
                overlapping_evidence,
            })?;
        }
        seq.end()
    }
}

#[derive(Clone, Copy)]
struct Timestamp(OffsetDateTime);

impl Serialize for Timestamp {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(self)
    }
}

impl fmt::Display for Timestamp {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let at = self.0;
        write!(
            f,
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}",
            at.year(),
            u8::from(at.month()),
            at.day(),
            at.hour(),
            at.minute(),
            at.second(),
        )?;
        if at.microsecond() != 0 {
            write!(f, ".{:06}", at.microsecond())?;
        }
        let offset = at.offset().whole_seconds();
        let seconds = offset.unsigned_abs();
        let sign = if offset < 0 { '-' } else { '+' };
        write!(f, "{sign}{:02}:{:02}", seconds / 3600, seconds / 60 % 60)?;
        if !seconds.is_multiple_of(60) {
            write!(f, ":{:02}", seconds % 60)?;
        }
        Ok(())
    }
}

struct BoundedOutput(Vec<u8>);

impl Write for BoundedOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > MAX_RESPONSE_BYTES - self.0.len() {
            return Err(io::Error::from(io::ErrorKind::FileTooLarge));
        }
        self.0.write(bytes)
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
