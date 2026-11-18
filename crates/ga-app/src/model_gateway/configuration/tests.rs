use std::{collections::BTreeMap, error::Error};

use ga_db::Database;
use ga_domain::model_consent::{ModelConsentError, authorized_models};
use serde_json::{Value, json};

use super::*;

#[tokio::test]
async fn resolves_legacy_gemini_consent_and_rejects_rotated_credentials()
-> Result<(), Box<dyn Error>> {
    let now = "2026-10-25T12:35:00+00:00".parse()?;
    let mut variables = BTreeMap::from([
        ("GA_LLM_ENABLED", "yes".into()),
        ("GA_GEMINI_MODEL", "synthetic-primary".into()),
        ("GA_GEMINI_API_KEY", "synthetic-provider-key-one".into()),
        ("GA_GEMINI_FALLBACK_MODELS", json!(["synthetic-unapproved","synthetic-fallback"]).to_string()),
        ("GA_LLM_CONSENT", json!({
            "provider":"gemini","model":"synthetic-primary","fallback_models":["synthetic-fallback"],
            "categories":["diary"],"granted_at":"2026-10-25T11:35:00+00:00","policy_revision":1,
        }).to_string()),
    ]);
    let settings = load(&variables)?.ok_or("Expected legacy model")?;
    assert_eq!(settings.deadline, Duration::from_secs(110));
    let configured = settings.configured();
    let database = fixture_database()?;
    let gateway = configured.gateway(database.clone(), false, now)?;
    let operation = ModelOperation::Structured(ModelCategories::DIARY);
    let policy = configured.current();
    assert_eq!(
        authorized_models(
            gateway.binding(),
            policy.configuration,
            policy.consent,
            operation,
            now
        )?,
        ["synthetic-primary", "synthetic-fallback"],
    );
    let disallowed = authorized_models(
        gateway.binding(),
        policy.configuration,
        policy.consent,
        ModelOperation::Transcribe,
        now,
    );
    assert_eq!(disallowed, Err(ModelConsentError::ConsentRequired));

    variables.insert("GA_GEMINI_API_KEY", "synthetic-provider-key-two".into());
    let replaced = load(&variables)?.ok_or("Expected model with rotated credential")?;
    let replaced = replaced.configured();
    let current = replaced.current();
    assert_eq!(
        authorized_models(
            gateway.binding(),
            current.configuration,
            current.consent,
            operation,
            now
        ),
        Err(ModelConsentError::ConsentRequired)
    );
    assert!(replaced.gateway(database.clone(), false, now).is_ok());

    variables.remove("GA_LLM_CONSENT");
    let denied = load(&variables)?.ok_or("Expected configured provider")?;
    assert!(matches!(
        denied.configured().gateway(database, false, now),
        Err(GatewayError::Consent(ModelConsentError::ConsentRequired)),
    ));
    Ok(())
}

#[tokio::test]
async fn selects_one_explicit_text_provider_and_reads_its_named_secret()
-> Result<(), Box<dyn Error>> {
    let now = "2026-10-25T12:35:00+00:00".parse()?;
    for (provider, expected) in [
        ("openai", StructuredProvider::OpenAi),
        ("anthropic", StructuredProvider::Anthropic),
    ] {
        let instance = format!("model:{provider}:synthetic");
        let grant = json!({"provider":provider,"provider_instance_id":instance,
            "model":"synthetic-model","categories":["diary","audio"],
            "granted_at":"2026-10-25T11:35:00+00:00","policy_revision":1});
        let entry = json!({"id":instance,"kind":"model","provider":provider,
            "request_timeout_seconds":31.5,
            "config":{"model":"synthetic-model","structured_output":true,"timeout_seconds":12.5,"max_output_tokens":512},
            "secret_refs":{"api_key":"GA_PLUGIN_SELECTED_SYNTHETIC"}});
        let variables = BTreeMap::from([
            ("GA_LLM_ENABLED", "true".into()),
            (
                "GA_INTEGRATIONS",
                json!([entry, {"id":"model:gemini:primary",
                "kind":"model","provider":"gemini","enabled":false}])
                .to_string(),
            ),
            ("GA_LLM_CONSENT", grant.to_string()),
        ]);
        let mut selected = Vec::new();
        let settings = RuntimeModelSettings::resolve(
            |name| {
                assert_ne!(name, "GA_GEMINI_API_KEY");
                Ok(variables.get(name).cloned())
            },
            |name| {
                selected.push(name.to_owned());
                Ok(Some("synthetic-provider-key".into()))
            },
        )?
        .ok_or("Expected explicitly configured model")?;
        assert_eq!(selected, ["GA_PLUGIN_SELECTED_SYNTHETIC"]);
        assert_eq!(settings.deadline, Duration::from_millis(31_500));
        let configured = settings.configured();
        let gateway = configured.gateway(fixture_database()?, false, now)?;
        let policy = configured.current();
        assert_eq!(
            gateway.binding().provider,
            ModelProvider::Structured(expected)
        );
        assert!(gateway.binding() == policy.configuration.binding);
        assert_eq!(
            authorized_models(
                gateway.binding(),
                policy.configuration,
                policy.consent,
                ModelOperation::Transcribe,
                now
            ),
            Err(ModelConsentError::UnsupportedCapability),
        );
    }
    Ok(())
}

#[test]
fn omits_deselected_models_and_rejects_out_of_scope_secret_names() -> Result<(), Box<dyn Error>> {
    let off = BTreeMap::from([("GA_LLM_ENABLED", "false".into())]);
    assert!(load(&off)?.is_none());
    let other = json!({"id":"channel:telegram:synthetic","kind":"channel","provider":"telegram"});
    let active = json!({"id":"model:gemini:synthetic","kind":"model","provider":"gemini"});
    let bad_reference = json!({"id":"model:openai:synthetic","provider":"openai","kind":"model",
        "config":{"model":"synthetic","structured_output":true},
        "secret_refs":{"api_key":"GA_DATABASE_URL"}});
    let scenarios = [
        (vec![other], Ok(false)),
        (
            vec![active.clone(), active],
            Err(ModelEnvironmentError::Multiple),
        ),
        (vec![bad_reference], Err(ModelEnvironmentError::Invalid)),
        (
            vec![json!({"id":"model:external:synthetic","kind":"model","provider":"external"})],
            Err(ModelEnvironmentError::UnsupportedProvider),
        ),
    ];
    for (integrations, expected) in scenarios {
        let variables = BTreeMap::from([
            ("GA_LLM_ENABLED", "true".into()),
            ("GA_INTEGRATIONS", Value::Array(integrations).to_string()),
        ]);
        assert_eq!(
            RuntimeModelSettings::resolve(
                |name| {
                    assert_ne!(name, "GA_GEMINI_API_KEY");
                    Ok(variables.get(name).cloned())
                },
                |_| panic!("Invalid configuration must never read a secret"),
            )
            .map(|settings| settings.is_some()),
            expected
        );
    }
    Ok(())
}

fn load(
    values: &BTreeMap<&str, String>,
) -> Result<Option<RuntimeModelSettings>, ModelEnvironmentError> {
    RuntimeModelSettings::resolve(
        |name| Ok(values.get(name).cloned()),
        |_| panic!("Legacy Gemini must have no plugin secret references"),
    )
}

fn fixture_database() -> Result<Database, Box<dyn Error>> {
    Database::connect_lazy("postgresql+psycopg://fixture@127.0.0.1:1/model_settings_test")
        .map_err(|_| "Invalid synthetic model test database URL".into())
}
