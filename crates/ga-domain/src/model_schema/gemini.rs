use serde_json::{Map, Value};

use super::{LOCAL_REF, ModelSchemaError};

const MAX_DEPTH: usize = 64;
const MAX_PROJECTED_BYTES: usize = 1024 * 1024;

/// Expand the application-owned references and keep the schema keywords supported by
/// Gemini Interactions. The local application validator retains all original constraints.
pub fn compile_gemini_schema(original: &[u8]) -> Result<Value, ModelSchemaError> {
    if original.len() > MAX_PROJECTED_BYTES {
        return Err(ModelSchemaError::InvalidDocument);
    }
    let original: Value =
        serde_json::from_slice(original).map_err(|_| ModelSchemaError::InvalidDocument)?;
    if !original.is_object() {
        return Err(ModelSchemaError::InvalidDocument);
    }
    let mut compiler = Compiler {
        definitions: &original["$defs"],
        remaining: MAX_PROJECTED_BYTES,
        visiting: Vec::new(),
    };
    let projected = compiler.project(&original, 0)?;
    if projected["type"] != "object" || !projected["properties"].is_object() {
        return Err(ModelSchemaError::InvalidRoot);
    }
    Ok(projected)
}

struct Compiler<'a> {
    definitions: &'a Value,
    remaining: usize,
    visiting: Vec<&'a str>,
}

impl<'a> Compiler<'a> {
    fn project(&mut self, node: &'a Value, depth: usize) -> Result<Value, ModelSchemaError> {
        if depth > MAX_DEPTH {
            return Err(ModelSchemaError::InvalidDocument);
        }
        self.spend(8)?;
        match node {
            Value::Array(values) => values
                .iter()
                .map(|value| self.project(value, depth + 1))
                .collect(),
            Value::Object(fields) => self.project_object(fields, depth),
            value => {
                self.spend(
                    value
                        .as_str()
                        .map_or_else(|| value.to_string().len(), str::len),
                )?;
                Ok(value.clone())
            }
        }
    }

    fn project_object(
        &mut self,
        fields: &'a Map<String, Value>,
        depth: usize,
    ) -> Result<Value, ModelSchemaError> {
        if let Some(reference) = fields.get("$ref") {
            let name = reference
                .as_str()
                .and_then(|value| value.strip_prefix(LOCAL_REF))
                .filter(|name| !name.is_empty() && !name.contains('/'))
                .ok_or(ModelSchemaError::UnsupportedReference)?;
            if self.visiting.contains(&name) {
                return Err(ModelSchemaError::RecursiveSchema);
            }
            let target = self
                .definitions
                .get(name)
                .ok_or(ModelSchemaError::UnsupportedReference)?;
            self.visiting.push(name);
            let result = self.project(target, depth + 1);
            self.visiting.pop();
            return result;
        }
        let mut output = Map::new();
        for (key, value) in fields {
            self.spend(key.len())?;
            match key.as_str() {
                "properties" => {
                    let fields = value.as_object().ok_or(ModelSchemaError::InvalidDocument)?;
                    let projected = fields
                        .iter()
                        .map(|(name, node)| {
                            self.spend(2 * name.len() + 8)?;
                            Ok((name.clone(), self.project(node, depth + 1)?))
                        })
                        .collect::<Result<Map<_, _>, _>>()?;
                    output.insert(key.clone(), Value::Object(projected));
                }
                "type" | "description" | "enum" | "required" | "items" | "anyOf" => {
                    output.insert(key.clone(), self.project(value, depth + 1)?);
                }
                "oneOf" => {
                    output.insert("anyOf".into(), self.project(value, depth + 1)?);
                }
                "const" => {
                    self.spend(value.to_string().len())?;
                    output.insert("enum".into(), Value::Array(vec![value.clone()]));
                }
                _ => {}
            }
        }
        if let Some(properties) = output.get("properties").and_then(Value::as_object) {
            let required = properties.keys().cloned().map(Value::String).collect();
            output.insert("required".into(), Value::Array(required));
        }
        Ok(Value::Object(output))
    }

    fn spend(&mut self, bytes: usize) -> Result<(), ModelSchemaError> {
        self.remaining = self
            .remaining
            .checked_sub(bytes)
            .ok_or(ModelSchemaError::InvalidDocument)?;
        Ok(())
    }
}
