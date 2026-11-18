use ga_domain::event_schema::{form_schema_flags, project_form_fields};
use serde_json::Value;

use super::{TrackerActionError, localized_label};

pub(super) fn flags(schema: &Value) -> Result<(bool, bool), TrackerActionError> {
    form_schema_flags(schema).map_err(|_| TrackerActionError::InvalidContract)
}

pub(super) fn fields(
    schema: &Value,
    metadata: &Value,
    locale: &str,
) -> Result<Vec<Value>, TrackerActionError> {
    let mut fields =
        project_form_fields(schema, metadata).map_err(|_| TrackerActionError::InvalidContract)?;
    for field in &mut fields {
        let labels = field
            .as_object_mut()
            .and_then(|fields| fields.remove("labels"))
            .ok_or(TrackerActionError::InvalidContract)?;
        field["label"] = Value::String(localized_label(&labels, locale)?.to_owned());
    }
    Ok(fields)
}
