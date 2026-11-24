mod unicode;

use std::collections::BTreeSet;

use serde_json::{Number, Value};
use sha2::{Digest, Sha256};

use super::{GeminiModelRequest, ModelRequestError, check_model};
use crate::{legacy_json::write_float, lower_hex};

impl GeminiModelRequest {
    /// Compute the pinned provider resume identity before any request or fallback is sent.
    /// The wire body has a different field order and wraps audio after this identity is formed.
    pub fn resume_fingerprint(
        &self,
        models: &[&str],
        categories: &[&str],
    ) -> Result<String, ModelRequestError> {
        if !(1..=9).contains(&models.len()) {
            return Err(ModelRequestError::InvalidConfiguration);
        }
        for model in models {
            check_model(model)?;
        }
        let categories: BTreeSet<_> = categories.iter().copied().collect();
        let voice = self.body["input"].is_array();
        let allowed = if voice {
            categories.len() == 1 && categories.contains("audio")
        } else {
            !categories.is_empty()
                && categories
                    .iter()
                    .all(|value| matches!(*value, "diary" | "health"))
        };
        if !allowed {
            return Err(ModelRequestError::InvalidConfiguration);
        }
        let mut hash = PythonRepresentation(Sha256::new());
        hash.write(b"(");
        hash.strings(models.iter().copied());
        hash.write(b", ");
        hash.strings(categories);
        hash.write(b", {'system_instruction': ");
        hash.value(&self.body["system_instruction"])?;
        hash.write(b", 'input': ");
        if voice {
            let audio = &self.body["input"][0]["content"][0];
            hash.write(b"[{'type': 'audio', 'mime_type': ");
            hash.value(&audio["mime_type"])?;
            hash.write(b", 'data': ");
            hash.value(&audio["data"])?;
            hash.write(b"}]");
        } else {
            hash.value(&self.body["input"])?;
        }
        hash.write(b", 'store': False, 'generation_config': ");
        hash.value(&self.body["generation_config"])?;
        hash.write(b", 'timeout': 60, 'response_format': {'type': 'text', 'mime_type': 'application/json', 'schema': ");
        hash.value(&self.body["response_format"]["schema"])?;
        hash.write(b"}})");
        Ok(lower_hex(&hash.0.finalize()))
    }
}

struct PythonRepresentation(Sha256);

impl PythonRepresentation {
    fn write(&mut self, bytes: &[u8]) {
        self.0.update(bytes);
    }

    fn strings<'a>(&mut self, values: impl IntoIterator<Item = &'a str>) {
        self.write(b"[");
        for (index, value) in values.into_iter().enumerate() {
            if index != 0 {
                self.write(b", ");
            }
            self.string(value);
        }
        self.write(b"]");
    }

    fn value(&mut self, value: &Value) -> Result<(), ModelRequestError> {
        match value {
            Value::Null => self.write(b"None"),
            Value::Bool(true) => self.write(b"True"),
            Value::Bool(false) => self.write(b"False"),
            Value::Number(value) => self.number(value)?,
            Value::String(value) => self.string(value),
            Value::Array(values) => {
                self.write(b"[");
                for (index, value) in values.iter().enumerate() {
                    if index != 0 {
                        self.write(b", ");
                    }
                    self.value(value)?;
                }
                self.write(b"]");
            }
            Value::Object(values) => {
                self.write(b"{");
                for (index, (key, value)) in values.iter().enumerate() {
                    if index != 0 {
                        self.write(b", ");
                    }
                    self.string(key);
                    self.write(b": ");
                    self.value(value)?;
                }
                self.write(b"}");
            }
        }
        Ok(())
    }

    fn number(&mut self, value: &Number) -> Result<(), ModelRequestError> {
        let original = value.as_str();
        if original
            .bytes()
            .any(|byte| matches!(byte, b'.' | b'e' | b'E'))
        {
            let mut repr = Vec::with_capacity(32);
            let parsed = value
                .as_f64()
                .ok_or(ModelRequestError::InvalidConfiguration)?;
            write_float(&mut repr, parsed).map_err(|_| ModelRequestError::InvalidConfiguration)?;
            self.write(&repr);
        } else {
            self.write(if original == "-0" {
                b"0"
            } else {
                original.as_bytes()
            });
        }
        Ok(())
    }

    fn string(&mut self, value: &str) {
        let quote = if value.contains('\'') && !value.contains('"') {
            b'"'
        } else {
            b'\''
        };
        self.write(&[quote]);
        let mut copied = 0;
        for (index, character) in value.char_indices() {
            let escapes = character == char::from(quote) || character == '\\';
            if !escapes && printable(character) {
                continue;
            }
            self.write(&value.as_bytes()[copied..index]);
            copied = index + character.len_utf8();
            if escapes {
                self.write(&[b'\\', character as u8]);
                continue;
            }
            match character {
                '\t' => self.write(b"\\t"),
                '\n' => self.write(b"\\n"),
                '\r' => self.write(b"\\r"),
                character => {
                    let code = u32::from(character);
                    let (prefix, digits) = match code {
                        0..=0xff => (b'x', 2),
                        0x100..=0xffff => (b'u', 4),
                        _ => (b'U', 8),
                    };
                    let mut escape = [0; 10];
                    escape[0] = b'\\';
                    escape[1] = prefix;
                    for digit in 0..digits {
                        let value = ((code >> (4 * (digits - digit - 1))) & 0xf) as usize;
                        escape[digit + 2] = b"0123456789abcdef"[value];
                    }
                    self.write(&escape[..digits + 2]);
                }
            }
        }
        self.write(&value.as_bytes()[copied..]);
        self.write(&[quote]);
    }
}

fn printable(character: char) -> bool {
    if character.is_ascii() {
        return (b' '..=b'~').contains(&(character as u8));
    }
    let code = u32::from(character);
    let after = unicode::UNPRINTABLE.partition_point(|&(start, _)| start <= code);
    unicode::UNPRINTABLE
        .get(after.wrapping_sub(1))
        .is_none_or(|&(_, end)| code > end)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_pinned_python_printability_for_every_unicode_scalar() {
        let mut digest = Sha256::new();
        for character in (0..=0x0010_ffff).filter_map(char::from_u32) {
            digest.update([u8::from(printable(character))]);
        }
        assert_eq!(
            lower_hex(&digest.finalize()),
            "e7ffd6a8217a5c864cd75d7e6ff0a52f972812dc7691421114fa50394ed874d7"
        );
    }
}
