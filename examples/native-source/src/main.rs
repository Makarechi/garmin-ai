use std::{
    error::Error,
    io::{Read, stdin},
};

use serde_json::{Value, json};

const RECORDS: [(&str, &str, u8); 3] = [
    ("walk-1", "2026-01-01T09:00:00+00:00", 15),
    ("walk-2", "2026-01-01T16:00:00+00:00", 25),
    ("walk-3", "2026-01-01T18:00:00+00:00", 10),
];

fn read_page(instance: &str, params: &Value) -> Option<Value> {
    // The probe sends UTC timestamps at second precision.
    let start = params["start"].as_str()?;
    let end = params["end"].as_str()?;
    let limit = usize::try_from(params["limit"].as_u64()?).ok()?;
    if !(1..=2).contains(&limit) || start >= end {
        return None;
    }
    let offset = match params.get("cursor")? {
        Value::Null => 0,
        Value::String(cursor) => cursor.parse::<usize>().ok()?,
        _ => return None,
    };
    let selected: Vec<_> = RECORDS
        .iter()
        .filter(|(_, at, _)| start <= *at && *at < end)
        .collect();
    if offset > selected.len() {
        return None;
    }
    let page = &selected[offset..(offset + limit).min(selected.len())];
    let next = offset + page.len();
    let records: Vec<_> = page
        .iter()
        .map(|(id, at, minutes)| {
            json!({
                "source_record_id": id, "operation": "upsert", "observed_at": at,
                "effective_at": at, "source_timezone": "UTC", "source_reference": format!("sample:{id}"),
                "payload": {"walk_minutes": minutes, "unit": "minutes"},
            })
        })
        .collect();
    Some(json!({
        "instance_id": instance, "page_kind": "partial", "fetched_at": "2026-01-02T00:00:00+00:00",
        "next_cursor": (next < selected.len()).then(|| next.to_string()), "records": records,
    }))
}

fn main() -> Result<(), Box<dyn Error>> {
    let mut input = Vec::new();
    stdin().take(65537).read_to_end(&mut input)?;
    if input.len() > 65536 {
        return Err("request exceeds source extension limit".into());
    }
    let request: Value = serde_json::from_slice(&input)?;
    let instance = request["instance_id"].as_str().ok_or("missing instance")?;
    let request_id = request["request_id"].as_u64().ok_or("missing request ID")?;
    let mut response = json!({"version": 1, "request_id": request_id});
    if request["version"] != 1 {
        response["error"] = json!("unsupported");
    } else {
        match request["method"].as_str() {
            Some("describe") => {
                response["result"] = json!({
                    "kind": "source",
                    "instance_id": instance,
                    "implementation_version": env!("CARGO_PKG_VERSION"),
                    "capabilities": {"observations":true,"cursor":true,"max_page_size":2},
                });
            }
            Some("read_page") => match read_page(instance, &request["params"]) {
                Some(page) => response["result"] = page,
                None => response["error"] = json!("invalid_request"),
            },
            _ => response["error"] = json!("unsupported"),
        }
    }
    println!("{response}");
    Ok(())
}
