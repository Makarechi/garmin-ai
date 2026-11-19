use std::{
    error::Error,
    io::{Read, stdin},
};

use serde_json::{Value, json};

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
            "kind":"model","instance_id":instance,"implementation_version":env!("CARGO_PKG_VERSION"),
            "capabilities":{"structured_output":true}
        })),
        (Some(1), Some("structured"))
            if params["prompt"] == "Synthetic probe"
                && params["schema"]["properties"]["urgent"]["type"] == "boolean" =>
        {
            Ok(json!({"value":{"urgent":false},"usage":{"input_tokens":4,"output_tokens":3}}))
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
