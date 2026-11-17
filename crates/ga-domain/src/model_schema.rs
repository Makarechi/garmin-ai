mod anthropic;

use std::{borrow::Cow, error::Error, fmt};

use jiter::JsonValue;
use serde_json::{Map, Value, json};

use crate::{
    legacy_json::{Utf8Layout, write_value},
    model_response::StructuredProvider,
};

type Fields<'a> = [(Cow<'a, str>, JsonValue<'a>)];

const OPENAI_CONSTRAINTS: &[&str] = &[
    "pattern",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
];
const FORMATS: &[&str] = &[
    "date-time",
    "time",
    "date",
    "duration",
    "email",
    "hostname",
    "ipv4",
    "ipv6",
    "uuid",
];
const LOCAL_REF: &str = "#/$defs/";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModelSchemaError {
    InvalidDocument,
    InvalidRoot,
    UntypedValue,
    UnsupportedComposition,
    OpenDictionary,
    UnsupportedArray,
    UnsupportedReference,
    RecursiveSchema,
    Complexity,
}

impl fmt::Display for ModelSchemaError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidDocument => "Invalid structured response schema",
            Self::InvalidRoot => "Structured output requires an object root",
            Self::UntypedValue => "Untyped schema values are not supported",
            Self::UnsupportedComposition => "Unsupported schema composition",
            Self::OpenDictionary => "Open dictionaries are not supported",
            Self::UnsupportedArray => "Positional or untyped arrays are not supported",
            Self::UnsupportedReference => "Unsupported schema reference",
            Self::RecursiveSchema => "Recursive schemas are not supported",
            Self::Complexity => "Anthropic schema exceeds complexity limits",
        })
    }
}
impl Error for ModelSchemaError {}

/// Translate an approved application schema for a provider. Callers must still
/// validate the result against the original schema and application rules.
pub fn compile_model_schema(
    provider: StructuredProvider,
    original: &[u8],
) -> Result<Value, ModelSchemaError> {
    // Required property order and the order of nullable reductions follow
    // the application schema document, even when property names are unsorted.
    let original =
        JsonValue::parse(original, false).map_err(|_| ModelSchemaError::InvalidDocument)?;
    let original = object(&original).ok_or(ModelSchemaError::InvalidDocument)?;
    let definitions = field(original, "$defs")
        .map(|definitions| object(definitions).ok_or(ModelSchemaError::InvalidDocument))
        .transpose()?
        .unwrap_or_default();
    let mut root = original;
    let mut visited = std::collections::BTreeSet::new();
    while let Some(reference) = field(root, "$ref") {
        let name = reference_name(reference)?;
        if !visited.insert(name) {
            return Err(ModelSchemaError::UnsupportedReference);
        }
        root = field(definitions, name)
            .and_then(object)
            .ok_or(ModelSchemaError::UnsupportedReference)?;
    }
    if text(field(root, "type")) != Some("object") || field(root, "properties").is_none() {
        return Err(ModelSchemaError::InvalidRoot);
    }
    if provider == StructuredProvider::Anthropic {
        anthropic::reject_recursive(definitions)?;
    }
    let compiler = Compiler {
        provider,
        definitions,
    };
    let mut output = compiler.compile_object(root)?;
    if !definitions.is_empty() {
        output["$defs"] = Value::Object(compiler.compile_fields(definitions)?);
    }
    if provider == StructuredProvider::Anthropic {
        anthropic::limit_unions(root, definitions, &mut output)?;
    }
    Ok(output)
}

struct Compiler<'a, 'src> {
    provider: StructuredProvider,
    definitions: &'a Fields<'src>,
}

impl Compiler<'_, '_> {
    fn compile(&self, node: &JsonValue<'_>) -> Result<Value, ModelSchemaError> {
        let fields = object(node).ok_or(ModelSchemaError::InvalidDocument)?;
        self.compile_object(fields)
    }

    fn compile_fields(&self, fields: &Fields<'_>) -> Result<Map<String, Value>, ModelSchemaError> {
        fields
            .iter()
            .map(|(name, value)| Ok((name.to_string(), self.compile(value)?)))
            .collect()
    }

    fn compile_object(&self, fields: &Fields<'_>) -> Result<Value, ModelSchemaError> {
        if !["type", "$ref", "anyOf", "oneOf", "const", "enum"]
            .iter()
            .any(|key| field(fields, key).is_some())
        {
            return Err(ModelSchemaError::UntypedValue);
        }
        if ["allOf", "not", "if", "then", "else", "patternProperties"]
            .iter()
            .any(|key| field(fields, key).is_some())
        {
            return Err(ModelSchemaError::UnsupportedComposition);
        }
        let node_type = text(field(fields, "type"));
        let items = field(fields, "items");
        if field(fields, "prefixItems").is_some()
            || node_type == Some("array") && items.and_then(object).is_none()
        {
            return Err(ModelSchemaError::UnsupportedArray);
        }

        let keep_constraints = self.provider == StructuredProvider::OpenAi;
        let mut output = Map::new();
        for (name, value) in fields {
            if matches!(name.as_ref(), "type" | "enum")
                || keep_constraints && OPENAI_CONSTRAINTS.contains(&name.as_ref())
            {
                output.insert(name.to_string(), scalar(value)?);
            }
        }
        if let Some(format) = text(field(fields, "format")).filter(|format| {
            FORMATS.contains(format)
                || self.provider == StructuredProvider::Anthropic && *format == "uri"
        }) {
            output.insert("format".into(), json!(format));
        }
        if let Some(reference) = field(fields, "$ref") {
            let name = reference_name(reference)?;
            if field(self.definitions, name).is_none() {
                return Err(ModelSchemaError::UnsupportedReference);
            }
            output.insert("$ref".into(), scalar(reference)?);
        }
        if let Some(constant) = field(fields, "const") {
            output.insert("enum".into(), Value::Array(vec![scalar(constant)?]));
        }
        if let Some(choices) = field(fields, "anyOf").or_else(|| field(fields, "oneOf"))
            && !matches!(choices, JsonValue::Null)
        {
            let choices = array(choices).ok_or(ModelSchemaError::InvalidDocument)?;
            let compiled = choices
                .iter()
                .map(|choice| self.compile(choice))
                .collect::<Result<_, _>>()?;
            output.insert("anyOf".into(), Value::Array(compiled));
        }
        if node_type == Some("object") || field(fields, "properties").is_some() {
            let properties = field(fields, "properties")
                .and_then(object)
                .ok_or(ModelSchemaError::OpenDictionary)?;
            if !matches!(
                field(fields, "additionalProperties"),
                None | Some(JsonValue::Bool(false))
            ) {
                return Err(ModelSchemaError::OpenDictionary);
            }
            let required: Vec<_> = properties
                .iter()
                .map(|(name, _)| json!(name.as_ref()))
                .collect();
            output.insert("type".into(), json!("object"));
            output.insert(
                "properties".into(),
                Value::Object(self.compile_fields(properties)?),
            );
            output.insert("required".into(), Value::Array(required));
            output.insert("additionalProperties".into(), Value::Bool(false));
        }
        if let Some(items) = items {
            output.insert("items".into(), self.compile(items)?);
        }
        Ok(Value::Object(output))
    }
}

fn object<'a, 'src>(node: &'a JsonValue<'src>) -> Option<&'a Fields<'src>> {
    match node {
        JsonValue::Object(fields) => Some(fields),
        _ => None,
    }
}

fn array<'a, 'src>(node: &'a JsonValue<'src>) -> Option<&'a [JsonValue<'src>]> {
    match node {
        JsonValue::Array(items) => Some(items),
        _ => None,
    }
}

fn field<'a, 'src>(fields: &'a Fields<'src>, name: &str) -> Option<&'a JsonValue<'src>> {
    fields
        .iter()
        .rev()
        .find_map(|(key, value)| (key == name).then_some(value))
}

fn text<'a>(value: Option<&'a JsonValue<'_>>) -> Option<&'a str> {
    match value {
        Some(JsonValue::Str(text)) => Some(text),
        _ => None,
    }
}

fn reference_name<'a>(node: &'a JsonValue<'_>) -> Result<&'a str, ModelSchemaError> {
    text(Some(node))
        .and_then(|reference| reference.strip_prefix(LOCAL_REF))
        .ok_or(ModelSchemaError::UnsupportedReference)
}

fn scalar(value: &JsonValue<'_>) -> Result<Value, ModelSchemaError> {
    let mut bytes = Vec::new();
    write_value(&mut bytes, value, Utf8Layout::Compact)
        .map_err(|_| ModelSchemaError::InvalidDocument)?;
    serde_json::from_slice(&bytes).map_err(|_| ModelSchemaError::InvalidDocument)
}
