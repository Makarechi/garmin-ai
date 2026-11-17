use std::{env, error::Error, fs, str::FromStr};

use ga_db::Database;
use ga_domain::{
    model_consent::{
        ModelBinding, ModelCategories, ModelConfiguration, ModelConsent, ModelOperation,
        ModelProvider, authorized_models,
    },
    model_response::StructuredProvider,
};
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

#[tokio::test]
#[ignore = "uses the pinned consent fixture and disposable TimescaleDB"]
async fn matches_pinned_provider_and_onboarding_choices() -> Result<(), Box<dyn Error>> {
    let fixture: Value = serde_json::from_slice(&fs::read(env::var("GA_TEST_MODEL_CONSENT")?)?)?;
    for case in fixture["model"].as_array().ok_or("Missing model cases")? {
        let outcome = authorize(case)?;
        if case["error"].is_string() {
            assert_eq!(outcome, json!({"error": case["error"]}), "{}", case["name"]);
        } else {
            assert_eq!(
                outcome,
                json!({"models": case["models"]}),
                "{}",
                case["name"]
            );
        }
    }
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable test database")?;
    let inspect = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&inspect)
        .await?;
    assert_eq!(target, actual);
    let db = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    const KEY: &str = "preferences:onboarding";
    for case in fixture["onboarding"]
        .as_array()
        .ok_or("Missing onboarding cases")?
    {
        if case[0] == true {
            sqlx::query(
                "INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            )
            .bind(KEY)
            .bind(case[1].to_string())
            .execute(&inspect)
            .await?;
        } else {
            sqlx::query("DELETE FROM app_state WHERE key=$1")
                .bind(KEY)
                .execute(&inspect)
                .await?;
        }
        let categories =
            ModelCategories::from_labels(labels(&case[2], &[])?).ok_or("Invalid categories")?;
        let actual = db.model_onboarding_allows(categories).await?;
        assert_eq!(json!(actual), case[3], "{case}");
    }
    Ok(())
}

fn authorize(case: &Value) -> Result<Value, Box<dyn Error>> {
    let name = case["provider"].as_str().unwrap_or("gemini");
    let instance = format!("model:{name}:primary");
    let binding = ModelBinding {
        provider: provider(name)?,
        instance_id: &instance,
        primary_model: "main",
        credential_generation: if case["key_changed"] == true {
            "rotated"
        } else {
            "prepared"
        },
    };
    let prepared = ModelBinding {
        primary_model: case["prepared_model"].as_str().unwrap_or("main"),
        credential_generation: "prepared",
        ..binding
    };
    let fallback_models = labels(&case["fallbacks"], &["backup"])?;
    let configuration = ModelConfiguration {
        binding,
        enabled: case["enabled"].as_bool().unwrap_or(true),
        structured_output: case["structured"].as_bool().unwrap_or(true),
        fallback_enabled: case["fallback_enabled"].as_bool().unwrap_or(true),
        fallback_models: &fallback_models,
    };
    let consent_fallbacks = labels(&case["consent_fallbacks"], &["backup", "extra"])?;
    let scope = labels(&case["scope"], &["diary", "health", "audio"])?;
    let consent = if case["missing"] == true {
        None
    } else {
        let grant_date = if case["future"] == true {
            "2026-10-25T09:00:01Z"
        } else {
            "2026-10-25T08:59:59Z"
        };
        Some(ModelConsent {
            provider: provider(case["consent_provider"].as_str().unwrap_or(name))?,
            instance_id: case["consent_instance"].as_str().unwrap_or(&instance),
            primary_model: case["consent_model"].as_str().unwrap_or("main"),
            fallback_models: &consent_fallbacks,
            categories: ModelCategories::from_labels(scope)
                .ok_or("Invalid consent fixture categories")?,
            granted_at: grant_date.parse()?,
            policy_revision: 1,
        })
    };
    let operation = match case["operation"].as_str() {
        Some("configure") => ModelOperation::Configure,
        Some("voice") => ModelOperation::Transcribe,
        _ => ModelOperation::Structured(
            ModelCategories::from_labels(labels(&case["categories"], &["diary"])?)
                .ok_or("Invalid requested categories")?,
        ),
    };
    Ok(
        match authorized_models(
            prepared,
            configuration,
            consent,
            operation,
            "2026-10-25T09:00:00Z".parse()?,
        ) {
            Ok(models) => json!({"models": models}),
            Err(error) => json!({"error": format!("{error:?}")}),
        },
    )
}

fn provider(name: &str) -> Result<ModelProvider, Box<dyn Error>> {
    match name {
        "gemini" => Ok(ModelProvider::Gemini),
        "openai" => Ok(ModelProvider::Structured(StructuredProvider::OpenAi)),
        "anthropic" => Ok(ModelProvider::Structured(StructuredProvider::Anthropic)),
        _ => Err("Unsupported fixture provider".into()),
    }
}

fn labels<'a>(value: &'a Value, default: &[&'a str]) -> Result<Vec<&'a str>, Box<dyn Error>> {
    value.as_array().map_or_else(
        || Ok(default.to_vec()),
        |items| {
            items
                .iter()
                .map(|item| item.as_str().ok_or_else(|| "Invalid fixture label".into()))
                .collect()
        },
    )
}
