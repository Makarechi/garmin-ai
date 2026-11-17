use std::{
    env,
    error::Error,
    fs::{self, File},
    io::{self, BufReader},
    path::Path,
};

use embedded_io_adapters::std::FromStd;
use fitparser::de::{FitObject, FitStreamProcessor};
use rustyfit::{Decoder, DecoderEvent, StreamingIterator};
use serde::Serialize;
use serde_json::{Value, to_value};
use sha2::{Digest, Sha256};

type Field = (Option<u8>, u8, Value);
type Frame = (u16, Vec<Field>);

#[derive(Default)]
struct Trace {
    headers: usize,
    definitions: usize,
    crcs: usize,
    messages: usize,
    developer_fields: usize,
    first: Option<Frame>,
    last: Option<Frame>,
    first_developer_description: Option<Frame>,
    digest: Sha256,
}

#[derive(Serialize)]
struct Report {
    outcome: &'static str,
    headers: usize,
    definitions: usize,
    crcs: usize,
    messages: usize,
    developer_fields: usize,
    sha256: String,
    first: Option<Frame>,
    last: Option<Frame>,
    first_developer_description: Option<Frame>,
}

impl Trace {
    fn message(&mut self, number: u16, mut fields: Vec<Field>) -> serde_json::Result<()> {
        fields.sort_by_key(|(developer, number, _)| (*developer, *number));
        self.messages += 1;
        self.developer_fields += fields.iter().filter(|(dev, _, _)| dev.is_some()).count();
        let frame = (number, fields);
        self.digest.update(serde_json::to_vec(&frame)?);
        self.digest.update(b"\n");
        if self.first.is_none() {
            self.first = Some(frame.clone());
        }
        if number == 206 && self.first_developer_description.is_none() {
            self.first_developer_description = Some(frame.clone());
        }
        self.last = Some(frame);
        Ok(())
    }

    fn finish(self, outcome: &'static str) -> Report {
        let outcome = if outcome == "decoded" && (self.headers == 0 || self.headers != self.crcs) {
            "missing_crc"
        } else {
            outcome
        };
        Report {
            outcome,
            headers: self.headers,
            definitions: self.definitions,
            crcs: self.crcs,
            messages: self.messages,
            developer_fields: self.developer_fields,
            sha256: hex::encode(self.digest.finalize()),
            first: self.first,
            last: self.last,
            first_developer_description: self.first_developer_description,
        }
    }
}

fn fitparser(path: &Path) -> Result<Report, Box<dyn Error>> {
    let data = fs::read(path)?;
    let mut remaining = data.as_slice();
    let mut processor = FitStreamProcessor::new();
    let mut trace = Trace::default();
    while !remaining.is_empty() {
        let Ok((next, event)) = processor.deserialize_next(remaining) else {
            return Ok(trace.finish("decode_error"));
        };
        remaining = next;
        match event {
            FitObject::Header(_) => trace.headers += 1,
            FitObject::DefinitionMessage(_) => trace.definitions += 1,
            FitObject::Crc(_) => {
                trace.crcs += 1;
                processor.reset();
            }
            FitObject::DataMessage(message) => {
                let mut fields = Vec::new();
                for (number, value) in message.fields() {
                    fields.push((None, *number, to_value(value)?));
                }
                for ((developer, number), value) in message.developer_fields() {
                    fields.push((Some(*developer), *number, to_value(value)?));
                }
                let number = message.global_message_number();
                trace.message(number, fields)?;
                if number == 206 && processor.decode_message(message).is_err() {
                    return Ok(trace.finish("decode_error"));
                }
            }
        }
    }
    Ok(trace.finish("decoded"))
}

fn raw(value: &rustyfit::proto::Value) -> serde_json::Result<Value> {
    let mut tagged = to_value(value)?;
    match tagged.as_object_mut().and_then(|tag| tag.remove("c")) {
        Some(content) => Ok(content),
        None => Ok(Value::Null),
    }
}

fn rustyfit(path: &Path, expand_components: bool) -> Result<Report, Box<dyn Error>> {
    let input = FromStd::new(BufReader::new(File::open(path)?));
    let mut decoder = Decoder::builder()
        .expand_components(expand_components)
        .build();
    let mut stream = decoder.stream(input);
    let mut trace = Trace::default();
    while let Some(ev) = stream.next() {
        match ev {
            Ok(DecoderEvent::FileHeader(_)) => trace.headers += 1,
            Ok(DecoderEvent::MessageDefinition(_)) => trace.definitions += 1,
            Ok(DecoderEvent::Crc(_)) => trace.crcs += 1,
            Ok(DecoderEvent::Message(message)) => {
                let mut fields = Vec::new();
                for field in &message.fields {
                    fields.push((None, field.num, raw(&field.value)?));
                }
                for field in &message.developer_fields {
                    fields.push((
                        Some(field.developer_data_index),
                        field.num,
                        raw(&field.value)?,
                    ));
                }
                trace.message(message.num.0, fields)?;
            }
            Err(_) => return Ok(trace.finish("decode_error")),
        }
    }
    Ok(trace.finish("decoded"))
}

fn main() -> Result<(), Box<dyn Error>> {
    let arguments: Vec<_> = env::args_os().collect();
    let [_, decoder, input] = arguments.as_slice() else {
        return Err(io::Error::other("Expected a decoder and one synthetic FIT fixture").into());
    };
    let path = Path::new(input);
    let result = match decoder.to_str() {
        Some("fitparser") => fitparser(path)?,
        Some("rustyfit") => rustyfit(path, false)?,
        Some("rustyfit-components") => rustyfit(path, true)?,
        _ => return Err(io::Error::other("Unknown FIT decoder").into()),
    };
    println!("{}", serde_json::to_string(&result)?);
    Ok(())
}
