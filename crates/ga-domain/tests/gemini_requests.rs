use std::{env, error::Error, fs};

use ga_domain::model_request::{
    ApprovedModelSchema, GeminiModelRequest, MAX_GEMINI_AUDIO_BYTES, ModelRequestError,
};
use serde_json::{Value, json};

const MODEL: &str = "synthetic-gemini-2";
const AUDIO: &[u8] = &[0, 127, 128, 255, 42];

#[test]
#[ignore = "requires requests captured from the pinned Gemini SDK mock transport"]
fn matches_gemini_text_and_voice_wire_requests() -> Result<(), Box<dyn Error>> {
    let cases: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_GEMINI_REQUESTS")?)?)?;
    for case in cases {
        let name = case["name"].as_str().ok_or("Missing approved contract")?;
        let level = case["level"].as_str().ok_or("Missing thinking level")?;
        let contract = match name {
            "agent-step" => Some(ApprovedModelSchema::AgentStep),
            "interpretation" => Some(ApprovedModelSchema::Interpretation),
            "safety-screen" => Some(ApprovedModelSchema::SafetyScreen),
            "tracker-extraction" => Some(ApprovedModelSchema::TrackerExtraction),
            "model-probe" => Some(ApprovedModelSchema::ModelProbe),
            "transcript" => None,
            _ => return Err("Unexpected fixture contract".into()),
        };
        let request = match contract {
            Some(contract) => GeminiModelRequest::structured(
                contract,
                MODEL,
                level,
                "Synthetic instruction",
                "Synthetic prompt ☕",
            )?,
            None => GeminiModelRequest::transcribe(MODEL, level, AUDIO, "audio/ogg")?,
        };
        let wire: Value = serde_json::from_slice(&serde_json::to_vec(request.body())?)?;
        assert_eq!(wire, case["wire"], "{name} {level}");
        for entry in case["ordered_maps"]
            .as_array()
            .ok_or("Missing wire schema field order")?
        {
            let pointer = entry[0].as_str().ok_or("Expected wire schema pointer")?;
            let fields = wire["response_format"]["schema"]
                .pointer(pointer)
                .and_then(Value::as_object)
                .ok_or("Missing wire schema fields")?;
            assert_eq!(
                json!(fields.keys().collect::<Vec<_>>()),
                entry[1],
                "{name} {level} {pointer}"
            );
        }
        if contract.is_none() {
            let reply = br#"{"status":"completed","steps":[{"type":"model_output","content":[{"type":"text","text":"{\"text\":\"synthetic\"}"}]}]}"#;
            assert_eq!(
                request.decode_response(reply)?.value,
                json!({"text":"synthetic"})
            );
        }
    }
    Ok(())
}

#[test]
fn rejects_audio_over_the_pinned_limit_and_preserves_the_full_valid_payload()
-> Result<(), Box<dyn Error>> {
    let audio = vec![0x5a; MAX_GEMINI_AUDIO_BYTES + 1];
    assert!(matches!(
        GeminiModelRequest::transcribe(MODEL, "", &audio, "audio/ogg"),
        Err(ModelRequestError::InputTooLarge)
    ));
    let voice =
        GeminiModelRequest::transcribe(MODEL, "", &audio[..MAX_GEMINI_AUDIO_BYTES], "audio/ogg")?;
    let base64 = voice
        .body()
        .pointer("/input/0/content/0/data")
        .and_then(Value::as_str)
        .ok_or("Missing encoded audio")?;
    assert_eq!(base64.len(), MAX_GEMINI_AUDIO_BYTES.div_ceil(3) * 4);
    assert!(base64.starts_with("WlpaWlpa") && base64.ends_with("Wlo="));
    assert!(matches!(
        GeminiModelRequest::transcribe("", "", AUDIO, "audio/ogg"),
        Err(ModelRequestError::InvalidConfiguration)
    ));
    Ok(())
}
