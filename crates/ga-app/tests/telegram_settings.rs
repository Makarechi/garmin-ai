use std::{env, error::Error, fs, str::FromStr};

use ga_app::telegram_settings::{TelegramIngressSettings, TelegramSettingsError};
use ga_db::{Database, telegram_binding::TelegramBindingError};
use serde_json::Value;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use uuid::Uuid;

#[test]
#[ignore = "uses the pinned Python integration selection recording"]
fn matches_configured_telegram_namespaces() -> Result<(), Box<dyn Error>> {
    let fixture: Vec<Value> =
        serde_json::from_slice(&fs::read(env::var("GA_TEST_TELEGRAM_SETTINGS")?)?)?;
    for case in fixture {
        let selected = TelegramIngressSettings::selected(
            case["owner"].as_str(),
            case["integrations"].as_str(),
        )?;
        assert_eq!(
            selected.as_ref().map(TelegramIngressSettings::instance_id),
            case["namespace"].as_str(),
            "{}",
            case["name"]
        );
        if let Some(selected) = selected {
            assert_eq!(
                selected.external_owner_id().to_string(),
                case["owner"].as_str().ok_or("Missing owner")?
            );
        }
    }
    assert!(matches!(
        TelegramIngressSettings::selected(Some("4503599627370496"), None),
        Err(TelegramSettingsError::InvalidOwner)
    ));
    assert!(matches!(
        TelegramIngressSettings::selected(
            Some("42"),
            Some(r#"[{"id":"channel:telegram:","kind":"channel","provider":"telegram"}]"#)
        ),
        Err(TelegramSettingsError::InvalidIntegrations)
    ));
    Ok(())
}

#[tokio::test]
#[ignore = "uses a disposable TimescaleDB with existing recorded channel pairings"]
async fn requires_the_exact_saved_owner_and_instance() -> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let expected = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let current: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(current, expected);
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;

    let settings =
        TelegramIngressSettings::selected(Some("42"), None)?.ok_or("Missing primary settings")?;
    assert!(matches!(
        settings.confirmed_binding(&database).await,
        Err(TelegramSettingsError::Pairing(
            TelegramBindingError::Missing
        ))
    ));

    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method,confirmed_at)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','legacy_configuration',now()),
               (gen_random_uuid(),$1,'telegram','secondary','43','explicit_pairing',now())",
    ).bind(owner).execute(&pool).await?;
    let actual = settings.confirmed_binding(&database).await?;
    assert_eq!(actual.owner_id, owner);
    assert_eq!(actual.external_owner_id, 42);
    assert_eq!(actual.instance_id, "primary");

    let wrong =
        TelegramIngressSettings::selected(Some("43"), None)?.ok_or("Missing wrong settings")?;
    assert!(matches!(
        wrong.confirmed_binding(&database).await,
        Err(TelegramSettingsError::Pairing(
            TelegramBindingError::Mismatch
        ))
    ));
    let other = TelegramIngressSettings::selected(
        Some("43"),
        Some(r#"[{"id":"channel:telegram:secondary","kind":"channel","provider":"telegram"}]"#),
    )?
    .ok_or("Missing secondary settings")?;
    assert_eq!(
        other.confirmed_binding(&database).await?.instance_id,
        "secondary"
    );
    let mut lock = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104627)")
        .execute(&mut *lock)
        .await?;
    assert!(matches!(
        settings.confirmed_binding(&database).await,
        Err(TelegramSettingsError::Pairing(TelegramBindingError::Busy))
    ));
    lock.rollback().await?;
    assert_eq!(settings.confirmed_binding(&database).await?.owner_id, owner);
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM channel_bindings")
        .fetch_one(&pool)
        .await?;
    assert_eq!(count, 2);
    Ok(())
}
