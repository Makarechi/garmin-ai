use std::{
    collections::{BTreeMap, BTreeSet},
    error::Error,
    fmt,
};

use ga_domain::{
    event_schema::{CustomEntry, form_schema_flags, project_form_fields, tracker_form_issues},
    file_import::{
        FileFieldInput, FileMappedRow, FileMapping, FileObjectRow, ImportFileError, JsonRow,
        document_hash,
    },
    sha256_hex,
    source_contract::validate_source_payload,
};
use serde_json::{Value, json};
use sqlx::{AssertSqlSafe, Postgres, Transaction};
use uuid::Uuid;

use super::{FormAction, FormContract, TrackerActionError, form_contract, parse};
use crate::{
    Database,
    diary::{DiaryReadError, DiaryWriteError, custom_entries::CreateCustomEntry},
};

#[derive(Debug)]
pub enum FileImportStoreError {
    File(ImportFileError),
    Action(TrackerActionError),
    Diary(DiaryWriteError),
    Contract(&'static str),
    Database,
}

impl fmt::Display for FileImportStoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::File(error) => error.fmt(f),
            Self::Action(error) => error.fmt(f),
            Self::Diary(error) => error.fmt(f),
            Self::Contract(detail) => f.write_str(detail),
            Self::Database => f.write_str("File import storage is unavailable"),
        }
    }
}

impl Error for FileImportStoreError {}
impl From<ImportFileError> for FileImportStoreError {
    fn from(error: ImportFileError) -> Self {
        Self::File(error)
    }
}
impl From<TrackerActionError> for FileImportStoreError {
    fn from(error: TrackerActionError) -> Self {
        Self::Action(error)
    }
}
impl From<DiaryReadError> for FileImportStoreError {
    fn from(error: DiaryReadError) -> Self {
        Self::Action(error.into())
    }
}
impl From<DiaryWriteError> for FileImportStoreError {
    fn from(error: DiaryWriteError) -> Self {
        Self::Diary(error)
    }
}
impl From<sqlx::Error> for FileImportStoreError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

pub struct FileImportOutcome {
    pub result: Value,
    pub invalid_preview: bool,
}

struct ImportRow<'a> {
    source: &'a FileObjectRow,
    entry: FileMappedRow<'a>,
}

struct ImportPlan<'a> {
    mapping: &'a FileMapping,
    file_hash: String,
    plan_hash: String,
    schema_hash: String,
    version: Uuid,
    rows: Vec<ImportRow<'a>>,
    row_count: usize,
    error_count: usize,
    errors: Vec<Value>,
    fields: Vec<Value>,
}

impl Database {
    /// Inspect local bytes against their current tracker. Apply only the exact confirmed plan.
    pub async fn run_file_import(
        &self,
        file: &[u8],
        mapping: &FileMapping,
        confirmation: Option<&str>,
    ) -> Result<FileImportOutcome, FileImportStoreError> {
        let rows = mapping.read_rows(file)?;
        let mut read = self.begin_diary_read().await?;
        let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
            .fetch_one(&mut *read)
            .await?;
        let version = current_version(&mut read, owner, mapping.definition_key(), false)
            .await?
            .ok_or(FileImportStoreError::Contract(
                "Active definition not found",
            ))?;
        let (form, _) = form_contract(&mut read, owner, FormAction::Create(version)).await?;
        read.commit().await?;
        let plan = ImportPlan::build(file, mapping, &rows, form)?;
        let Some(confirmation) = confirmation else {
            return Ok(FileImportOutcome {
                result: plan.preview(),
                invalid_preview: plan.error_count != 0,
            });
        };
        if plan.error_count != 0 {
            return Err(FileImportStoreError::Contract(
                "Fix preview errors before importing",
            ));
        }
        if confirmation != plan.plan_hash {
            return Err(FileImportStoreError::Contract(
                "File, mapping or tracker contract changed; preview again",
            ));
        }
        let mut txn = self.begin_diary_write(owner).await?;
        let latest = current_version(&mut txn, owner, mapping.definition_key(), true).await?;
        if latest != Some(plan.version) {
            return Err(FileImportStoreError::Contract(
                "Tracker version changed; preview again",
            ));
        }
        let result = apply(&mut txn, owner, &plan).await?;
        txn.commit().await?;
        Ok(FileImportOutcome {
            result,
            invalid_preview: false,
        })
    }
}

async fn current_version(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    key: &str,
    lock: bool,
) -> Result<Option<Uuid>, sqlx::Error> {
    const SELECT: &str = "SELECT v.id FROM event_definitions d JOIN event_definition_versions v
            ON v.definition_id=d.id AND v.version=d.current_version
         WHERE d.owner_id=$1 AND d.key=$2 AND d.namespace='user' AND d.status='active'";
    let query = if lock {
        format!("{SELECT} FOR UPDATE OF d")
    } else {
        SELECT.into()
    };
    sqlx::query_scalar(AssertSqlSafe(query))
        .bind(owner)
        .bind(key)
        .fetch_optional(&mut **txn)
        .await
}

impl<'a> ImportPlan<'a> {
    fn build(
        bytes: &[u8],
        mapping: &'a FileMapping,
        raw: &'a [JsonRow],
        form: FormContract,
    ) -> Result<Self, FileImportStoreError> {
        let schema = parse(&form.schema)?;
        let metadata = parse(&form.metadata)?;
        let (complex, _) =
            form_schema_flags(&schema).map_err(|_| TrackerActionError::InvalidContract)?;
        let projected = project_form_fields(&schema, &metadata)
            .map_err(|_| TrackerActionError::InvalidContract)?;
        if complex || projected.iter().any(|field| field["input"] == "json") {
            return Err(FileImportStoreError::Contract(
                "Complex tracker forms are not supported by file import",
            ));
        }
        if form.topology == "open_interval" && mapping.end_column().is_none() {
            return Err(FileImportStoreError::Contract(
                "Open-interval trackers require an explicit end column for file import",
            ));
        }
        if form.topology == "point" && mapping.end_column().is_some() {
            return Err(FileImportStoreError::Contract(
                "Point trackers cannot use an end column for file import",
            ));
        }
        let (fields, input_types) = prepare_fields(mapping, &projected)?;
        let file_hash = sha256_hex(bytes);
        let version = form.action.version_id;
        let plan_hash = mapping.confirmation_hash(&file_hash, &form.hash, version)?;
        let mut plan = Self {
            mapping,
            file_hash,
            plan_hash,
            schema_hash: form.hash,
            version,
            rows: Vec::with_capacity(raw.len()),
            row_count: raw.len(),
            error_count: 0,
            errors: Vec::new(),
            fields,
        };
        let mut seen = BTreeSet::new();
        for (index, row) in raw.iter().enumerate() {
            let entry = match mapping.map_row(row, &input_types, &mut seen) {
                Ok(entry) => entry,
                Err(issue) => {
                    plan.record_issue(index, issue.field, issue.code);
                    continue;
                }
            };
            let values = Value::Object(entry.values.clone());
            let issues = tracker_form_issues(&schema, &values)
                .map_err(|_| TrackerActionError::InvalidContract)?;
            if let Some(issue) = issues.first() {
                plan.record_issue(
                    index,
                    issue["field"].as_str(),
                    issue["code"].as_str().unwrap_or("invalid_row"),
                );
            } else if form.topology == "bounded_interval" && entry.end.is_none() {
                plan.record_issue(index, Some("end"), "topology");
            } else if !bounded_payload(&entry, mapping.timezone()) {
                plan.record_issue(index, None, "invalid_row");
            } else {
                let source = row.as_ref().map_err(|_| INVALID_ROW_STATE)?;
                plan.rows.push(ImportRow { source, entry });
            }
        }
        Ok(plan)
    }

    fn record_issue(&mut self, index: usize, field: Option<&str>, code: &str) {
        self.error_count += 1;
        if self.errors.len() == 20 {
            return;
        }
        let mut issue = json!({"row":index+1,"code":code});
        if let Some(field) = field {
            issue["field"] = json!(field);
        }
        self.errors.push(issue);
    }

    fn preview(&self) -> Value {
        let specification = self.mapping.document();
        json!({
            "file_sha256":self.file_hash,"plan_sha256":self.plan_hash,
            "definition_key":self.mapping.definition_key(),"source_instance_id":self.mapping.source_instance(),
            "device_id":self.mapping.device(),"definition_version_id":self.version.to_string(),
            "mapping":{
                "format":specification["format"],"row_id_column":specification["row_id_column"],
                "start_column":specification["start_column"],"end_column":specification["end_column"],
                "timezone":specification["timezone"],"decimal_separator":specification["decimal_separator"],
                "null_marker_count":specification["null_markers"].as_array().map_or(0, Vec::len),
                "fields":self.fields,
            },
            "row_count":self.row_count,"valid_count":self.rows.len(),"error_count":self.error_count,
            "errors":self.errors,
        })
    }
}

const INVALID_ROW_STATE: FileImportStoreError =
    FileImportStoreError::Contract("Invalid recorded file row");

fn bounded_payload(entry: &FileMappedRow<'_>, timezone: &str) -> bool {
    let end = entry
        .end
        .and_then(|end| ga_domain::event_input::source_offset_datetime(end, timezone))
        .and_then(|end| {
            end.format(&time::format_description::well_known::Rfc3339)
                .ok()
        })
        .map(|end| end.replace('Z', "+00:00"));
    let value = json!({"values":entry.values,"units":entry.units,"end":end});
    serde_json::to_vec(&value).is_ok_and(|bytes| validate_source_payload(&bytes).is_ok())
}

type PreparedFields<'a> = (Vec<Value>, BTreeMap<&'a str, FileFieldInput>);

fn prepare_fields<'a>(
    mapping: &'a FileMapping,
    projected: &[Value],
) -> Result<PreparedFields<'a>, FileImportStoreError> {
    let fields: BTreeMap<_, _> = projected
        .iter()
        .filter_map(|field| field["name"].as_str().map(|name| (name, field)))
        .collect();
    let mut inputs = BTreeMap::new();
    let mut units = BTreeMap::new();
    let mut public = Vec::new();
    for (name, column) in mapping.field_columns() {
        let field = fields
            .get(name.as_str())
            .ok_or(FileImportStoreError::Contract(
                "Mapping names a field outside the active tracker",
            ))?;
        if let Some(unit) = field["unit"].as_str().filter(|unit| !unit.is_empty()) {
            units.insert(name.as_str(), unit);
        }
        let kind = match field["input"].as_str() {
            Some("boolean") => FileFieldInput::Boolean,
            Some("integer") => FileFieldInput::Integer,
            Some("number") => FileFieldInput::Number,
            Some("choice" | "text") => FileFieldInput::Text,
            _ => return Err(TrackerActionError::InvalidContract.into()),
        };
        inputs.insert(name.as_str(), kind);
        public.push(json!({"name":name,"column":column,"input":field["input"],
            "unit":field["unit"],"minimum":field["minimum"],"maximum":field["maximum"]}));
    }
    if units
        != mapping
            .units()
            .iter()
            .map(|(name, unit)| (name.as_str(), unit.as_str()))
            .collect()
    {
        return Err(FileImportStoreError::Contract(
            "Mapping must specify every field's exact unit",
        ));
    }
    Ok((public, inputs))
}

async fn apply(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    plan: &ImportPlan<'_>,
) -> Result<Value, FileImportStoreError> {
    let contract = plan
        .mapping
        .contract_hash(&plan.schema_hash, plan.version)?;
    let source = format!(
        "file_import:{}:{}",
        plan.mapping.source_instance(),
        plan.mapping.device()
    );
    let actor = format!("file_import:{}", plan.mapping.source_instance());
    let mut created = 0;
    for rows in plan.rows.chunks(100) {
        let at: time::OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(&mut **txn)
            .await?;
        for row in rows {
            let identity = document_hash(&json!([
                owner.to_string(),
                plan.mapping.source_instance(),
                plan.mapping.device(),
                row.entry.source_id
            ]))?;
            let key = format!("file-import:row:{identity}");
            let raw = row.source.canonical_json()?;
            let hash = sha256_hex(raw.as_bytes());
            let receipt: Option<String> =
                sqlx::query_scalar("SELECT value::text FROM app_state WHERE key=$1")
                    .bind(&key)
                    .fetch_optional(&mut **txn)
                    .await?;
            if let Some(receipt) = receipt {
                let receipt: Value = parse(&receipt)?;
                if receipt["row_hash"] != hash || receipt["contract_hash"] != contract {
                    return Err(FileImportStoreError::Contract(
                        "Source row or mapping changed; preserve the edited diary entry and resolve separately",
                    ));
                }
                continue;
            }
            let payload = Uuid::new_v4();
            sqlx::query("INSERT INTO source_payloads(id,source,endpoint,source_key,payload_hash,payload,archive_key,
                                      fetched_at,parser_version,status)
                         VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,$8,0,'applied')")
                .bind(payload).bind(&source).bind(plan.mapping.definition_key()).bind(&identity).bind(&hash)
                .bind(raw).bind(format!("sha256:{}",plan.file_hash)).bind(at).execute(&mut **txn).await?;
            let document = json!({"definition_key":plan.mapping.definition_key(),"start":row.entry.start.to_string(),
                "end":row.entry.end.map(|end| end.to_string()),"timezone":plan.mapping.timezone(),
                "source":"file_import","values":row.entry.values,"units":row.entry.units});
            let document = serde_json::to_vec(&document).map_err(|_| INVALID_ROW_STATE)?;
            let entry =
                CustomEntry::from_json(&document, 512_000).map_err(DiaryWriteError::CustomInput)?;
            let event = Database::create_custom_entry_in(txn, CreateCustomEntry {
                owner_id:owner,actor:&actor,entry:&entry,idempotency_key:Some(&format!("file-import:{identity}")),
                evidence_refs:&[json!({"source_payload_id":payload.to_string(),"file_sha256":plan.file_hash})],
            }).await?;
            sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)")
                .bind(key).bind(json!({"row_hash":hash,"contract_hash":contract,"event_id":event.id.to_string()}).to_string())
                .execute(&mut **txn).await?;
            created += 1;
        }
    }
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
                  ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=clock_timestamp()",
    )
    .bind(format!("file-import:cursor:{}", plan.plan_hash))
    .bind(json!({"complete":true,"rows":plan.row_count}).to_string())
    .execute(&mut **txn)
    .await?;
    Ok(json!({"created":created,"skipped":plan.rows.len()-created,"file_sha256":plan.file_hash}))
}
