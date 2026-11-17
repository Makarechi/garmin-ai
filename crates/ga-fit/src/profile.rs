use std::{collections::HashMap, sync::LazyLock};

use serde::Deserialize;

use crate::FitError;

pub(crate) static PROFILE: LazyLock<Result<FitProfile, FitError>> = LazyLock::new(load);

pub(crate) struct FitProfile {
    pub types: HashMap<String, FieldType>,
    pub messages: HashMap<u16, Message>,
}

pub(crate) struct FieldType {
    pub values: HashMap<i64, String>,
}

pub(crate) struct Message {
    pub name: String,
    pub fields: HashMap<u8, Field>,
}

#[derive(Deserialize)]
pub(crate) struct Field {
    pub number: u8,
    pub name: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub units: Option<String>,
    pub scale: Option<f64>,
    pub offset: Option<i64>,
    #[serde(default)]
    pub components: Vec<Component>,
    #[serde(default)]
    pub subfields: Vec<Field>,
    #[serde(default)]
    pub references: Vec<(u8, i64)>,
}

#[derive(Deserialize)]
pub(crate) struct Component {
    pub number: u8,
    pub scale: Option<f64>,
    pub offset: Option<i64>,
    pub accumulate: bool,
    pub bits: u8,
    pub bit_offset: u16,
}

type EncodedFieldType = (String, u8, HashMap<i64, String>);
type EncodedMessage = (u16, String, Vec<Field>);

#[derive(Deserialize)]
struct EncodedProfile {
    version: String,
    types: Vec<EncodedFieldType>,
    messages: Vec<EncodedMessage>,
}

fn load() -> Result<FitProfile, FitError> {
    let encoded: EncodedProfile = serde_json::from_str(include_str!("../profile/21.171.json"))
        .map_err(|_| FitError::InvalidProfile)?;
    if encoded.version != "21.171" {
        return Err(FitError::InvalidProfile);
    }
    Ok(FitProfile {
        types: encoded
            .types
            .into_iter()
            .map(|(name, _, values)| (name, FieldType { values }))
            .collect(),
        messages: encoded
            .messages
            .into_iter()
            .map(|(number, name, fields)| {
                let fields = fields
                    .into_iter()
                    .map(|field| (field.number, field))
                    .collect();
                (number, Message { name, fields })
            })
            .collect(),
    })
}
