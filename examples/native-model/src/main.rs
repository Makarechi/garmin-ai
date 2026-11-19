use std::{
    error::Error,
    fs::OpenOptions,
    io::Write,
    io::{Read, stdin},
    path::Path,
    thread::sleep,
    time::{Duration, Instant},
};

use serde_json::{Value, json};

const MAX_REQUEST_BYTES: usize = 1024 * 1024;

fn main() -> Result<(), Box<dyn Error>> {
    let mut input = Vec::new();
    stdin()
        .take(MAX_REQUEST_BYTES as u64 + 1)
        .read_to_end(&mut input)?;
    if input.len() > MAX_REQUEST_BYTES {
        return Err("request exceeds the extension limit".into());
    }
    let request: Value = serde_json::from_slice(&input)?;
    let instance = request["instance_id"].as_str().ok_or("missing instance")?;
    let id = request["request_id"].as_u64().ok_or("missing request ID")?;
    let params = &request["params"];
    let context = &request["context"];
    if let Some(log) = context["config"]["invocation_log"].as_str() {
        if context["secrets"]["api_key"] != "synthetic-native-model-secret"
            || std::env::var("GA_PLUGIN_NATIVE_SYNTHETIC_MODEL_KEY").is_ok()
        {
            return Err("unexpected fixture context".into());
        }
        let method = request["method"].as_str().ok_or("missing fixture method")?;
        writeln!(
            OpenOptions::new().create(true).append(true).open(log)?,
            "{method}"
        )?;
    }
    let result = match (request["version"].as_u64(), request["method"].as_str()) {
        (Some(1), Some("describe")) => Ok(json!({
            "kind":"model","instance_id":instance,"implementation_version":env!("CARGO_PKG_VERSION"),
            "capabilities":{"structured_output":true}
        })),
        (Some(1), Some("structured"))
            if params["prompt"] == "Synthetic probe"
                && params["schema"]["properties"]["urgent"]["type"] == "boolean" =>
        {
            Ok(json!({"value":{"urgent":false},"usage":{"input_tokens":4,"output_tokens":3}}))
        }
        (Some(1), Some("structured"))
            if params["model"] == "synthetic-extension-model"
                && params["schema"]["title"] == "Interpretation" =>
        {
            let prompt = params["prompt"].as_str().ok_or("missing fixture prompt")?;
            if prompt.contains("hold extension output") {
                let release = context["config"]["release"]
                    .as_str()
                    .ok_or("missing fixture release flag")?;
                let until = Instant::now() + Duration::from_secs(4);
                while !Path::new(release).exists() && Instant::now() < until {
                    sleep(Duration::from_millis(10));
                }
                if !Path::new(release).exists() {
                    return Err("fixture release deadline expired".into());
                }
            }
            if prompt.contains("unavailable extension upstream") {
                Err("unavailable")
            } else if prompt.contains("invalid extension output") {
                Ok(json!({"value":{"intent":"log","confidence":"synthetic-invalid"}}))
            } else {
                Ok(json!({"value":{
                    "intent":"log","confidence":1,"response_language":"en",
                    "events":[{"start":"2026-09-20T20:00:00Z","timezone":"UTC",
                        "payload":{"type":"note","description":"Synthetic native extension note"}}]
                }, "usage":{"input_tokens":10,"output_tokens":4}}))
            }
        }
        (Some(1), Some("structured")) => Err("invalid_request"),
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
