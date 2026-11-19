use std::{
    error::Error,
    io::{Read, stdin},
};

use serde_json::{Value, json};

fn deliver(intent: &Value, now: &str) -> Option<Value> {
    let id = intent["intent_id"].as_str()?;
    let blocks = intent["blocks"].as_array()?;
    let texts: Vec<_> = blocks
        .iter()
        .map(|block| block["text"].as_str())
        .collect::<Option<_>>()?;
    if texts.is_empty()
        || texts.iter().any(|text| text.chars().count() > 4096)
        || intent["preferred_medium"] != "text"
        || !intent["actions"].as_array()?.is_empty()
        || !intent["attachments"].as_array()?.is_empty()
        || !intent["form"].is_null()
        || !intent["reply_to"].is_null()
        || !intent["replaces"].is_null()
    {
        return None;
    }
    Some(json!({
        "intent_id":id,"state":"provider_accepted",
        "rendered":{"intent_id":id,"texts":texts,"mode":"send","medium":"text"},
        "receipt":{"intent_id":id,"state":"provider_accepted","observed_at":now,
                   "provider_reference":"local-synthetic-receipt"},
    }))
}

fn main() -> Result<(), Box<dyn Error>> {
    let mut input = Vec::new();
    stdin().take(65537).read_to_end(&mut input)?;
    if input.len() > 65536 {
        return Err("request exceeds the extension limit".into());
    }
    let request: Value = serde_json::from_slice(&input)?;
    let instance = request["instance_id"].as_str().ok_or("missing instance")?;
    let id = request["request_id"].as_u64().ok_or("missing request ID")?;
    let params = &request["params"];
    let result = match (request["version"].as_u64(), request["method"].as_str()) {
        (Some(1), Some("describe")) => Ok(json!({
            "kind":"channel","instance_id":instance,"implementation_version":env!("CARGO_PKG_VERSION"),
            "capabilities":{"text":true,"initiatives":true,"max_text_length":4096}
        })),
        (Some(1), Some("delivery_policy")) => {
            Ok(json!({"allow_delivery":true,"allow_initiative":true}))
        }
        (Some(1), Some("deliver")) => params["now"]
            .as_str()
            .and_then(|now| deliver(&params["intent"], now))
            .ok_or("invalid_request"),
        _ => Err("unsupported"),
    };
    let mut response = json!({"version":1,"request_id":id});
    match result {
        Ok(result) => response["result"] = result,
        Err(error) => response["error"] = json!(error),
    }
    println!("{response}");
    Ok(())
}
