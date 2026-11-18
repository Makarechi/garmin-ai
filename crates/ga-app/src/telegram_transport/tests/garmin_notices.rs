use std::{env, error::Error, path::Path, str::FromStr, time::Duration};

use ga_db::{
    Database,
    outbox_claims::{ReplyDestination, ReplyDispatchResult},
};
use ga_domain::timestamp::StoredInstant;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use tokio::time::{sleep, timeout};
use uuid::Uuid;

use super::{bind, request, respond};
use crate::{
    garmin_worker::{GarminReauthNoticeSettings, garmin_auth_url},
    telegram_settings::TelegramIngressSettings,
};

const SOURCE: &str = "source:garmin:primary";
const URL: &str = "https://fixture.invalid/garmin-auth";
const SECRET_VERSION: &str = "projects/fixture/secrets/garmin-password/versions/2";

fn notice_settings(secret: &str) -> Result<GarminReauthNoticeSettings, Box<dyn Error>> {
    GarminReauthNoticeSettings::resolve(
        None,
        Path::new("unused-fixture"),
        Some(URL),
        Some("owner@fixture.invalid"),
        Some(secret.to_owned()),
    )?
    .ok_or_else(|| "Missing Garmin notice configuration".into())
}

#[test]
fn limits_garmin_buttons_to_a_managed_https_login() -> Result<(), Box<dyn Error>> {
    for invalid in [
        "http://fixture.invalid/garmin-auth",
        "https://user:secret@fixture.invalid/garmin-auth",
        "https://fixture.invalid/other",
        "https://fixture.invalid/garmin-auth?next=another",
        "https://fixture.invalid/garmin-auth#section",
    ] {
        assert!(garmin_auth_url(invalid).is_none());
    }
    assert_eq!(notice_settings(SECRET_VERSION)?.source, SOURCE);
    assert_eq!(
        notice_settings(SECRET_VERSION)?
            .launch
            .as_ref()
            .map(reqwest::Url::as_str),
        Some(URL)
    );
    assert!(
        notice_settings("projects/fixture/secrets/garmin-password/versions/latest")?
            .launch
            .is_none()
    );
    assert!(
        GarminReauthNoticeSettings::resolve(
            Some(r#"[{"kind":"source","provider":"garmin","id":"private","enabled":false}]"#),
            Path::new("unused-fixture"),
            Some(URL),
            Some("owner@fixture.invalid"),
            Some(SECRET_VERSION.into()),
        )?
        .is_none()
    );
    Ok(())
}

async fn set_connection(pool: &PgPool, status: &str, reason: &str) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES('integration:garmin',$1::jsonb)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    )
    .bind(json!({"status":status,"reason_class":reason}).to_string())
    .execute(pool)
    .await?;
    Ok(())
}

#[tokio::test]
#[ignore = "uses a disposable TimescaleDB and a local fake Telegram endpoint"]
async fn starts_garmin_reauth_notices_through_the_ordinary_reply_worker()
-> Result<(), Box<dyn Error>> {
    let url = env::var("GA_TEST_DATABASE_URL")?;
    let address = url
        .strip_prefix("postgresql+psycopg://")
        .ok_or("Expected fixture URL")?;
    let options =
        PgConnectOptions::from_str(&format!("postgresql://{address}"))?.disable_statement_logging();
    let target = options
        .get_database()
        .filter(|name| name.ends_with("_test"))
        .ok_or("Expected disposable database")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(target, actual);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let settings =
        TelegramIngressSettings::selected(Some("42"), None)?.ok_or("Missing Telegram settings")?;
    let owner: Uuid = sqlx::query_scalar("SELECT id FROM people WHERE singleton")
        .fetch_one(&pool)
        .await?;
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    ).bind(owner).execute(&pool).await?;
    let binding = settings.confirmed_binding(&database).await?;
    let chat = binding.external_owner_id;
    let at: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&pool)
        .await?;
    let now = StoredInstant::try_from(at)?;
    let destination = || ReplyDestination {
        owner_id: owner,
        channel: "telegram",
        instance_id: "primary",
    };
    let launch = notice_settings(SECRET_VERSION)?;
    let (listener, transport) = bind().await?;

    set_connection(&pool, "reauth_required", "AuthenticationRequired").await?;
    let auth_key = format!("auth:{}", at.date());
    let auth_receipt = format!("outbox:{auth_key}:0");
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES($1,'{\"status\":\"sent\",\"message_id\":19}')",
    )
    .bind(&auth_receipt)
    .execute(&pool)
    .await?;
    database
        .queue_telegram_garmin_notice(owner, "primary", chat, SOURCE, now)
        .await?;
    assert!(
        database
            .claim_outbox_reply(destination(), now, 60)
            .await?
            .is_none()
    );
    sqlx::query("DELETE FROM app_state WHERE key=$1")
        .bind(&auth_receipt)
        .execute(&pool)
        .await?;
    sqlx::query("INSERT INTO app_state(key,value) VALUES('preferences:onboarding',$1::jsonb)")
        .bind(
            json!({"channel":{"channel":"web","instance_id":"local"},
                     "fallback_channels":[{"channel":"telegram","instance_id":"primary"}],
                     "source_instance_ids":[SOURCE]})
            .to_string(),
        )
        .execute(&pool)
        .await?;
    database
        .queue_telegram_garmin_notice(owner, "primary", chat, SOURCE, now)
        .await?;
    assert!(
        database
            .claim_outbox_reply(destination(), now, 60)
            .await?
            .is_none()
    );
    sqlx::query("DELETE FROM app_state WHERE key='preferences:onboarding'")
        .execute(&pool)
        .await?;

    database
        .queue_telegram_garmin_notice(owner, "primary", chat, SOURCE, now)
        .await?;
    let stale = database
        .claim_outbox_reply(destination(), now, 60)
        .await?
        .ok_or("Missing queued Garmin notice")?;
    set_connection(&pool, "active", "").await?;
    assert_eq!(
        transport
            .dispatch_runtime_reply(&database, binding, stale, &[], Some(&launch))
            .await?,
        ReplyDispatchResult::Cancelled
    );

    let account_key = format!("account-binding:{}", at.date());
    let account_receipt = format!("outbox:{account_key}:0");
    set_connection(&pool, "reauth_required", "AccountMismatch").await?;
    let server = async {
        for (index, (receipt, expected)) in [(&account_receipt, None), (&auth_receipt, Some(URL))]
            .into_iter()
            .enumerate()
        {
            let (mut stream, _) = listener.accept().await?;
            let wire = request(&mut stream).await?;
            let fields = &wire["fields"];
            assert_eq!(wire["endpoint"], "sendMessage");
            assert_eq!(fields["chat_id"], chat);
            if let Some(url) = expected {
                assert_eq!(
                    fields["reply_markup"],
                    json!({"inline_keyboard":[[{"text":"Войти в Garmin","web_app":{"url":url}}]]})
                );
                assert!(
                    fields["text"]
                        .as_str()
                        .unwrap_or_default()
                        .contains("почтовый код")
                );
            } else {
                assert_eq!(fields["reply_markup"], Value::Null);
                assert!(
                    fields["text"]
                        .as_str()
                        .unwrap_or_default()
                        .contains("enroll-account --confirm-existing-owner")
                );
            }
            let message_id = 91i64 + i64::try_from(index)?;
            respond(
                &mut stream,
                "200 OK",
                &[],
                &json!({
                    "ok":true,"result":{"message_id":message_id,"chat":{"id":chat,"type":"private"}}
                })
                .to_string(),
            )
            .await?;
            loop {
                let sent: bool = sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM app_state WHERE key=$1
                      AND value->>'status'='sent' AND value->>'message_id'=$2)",
                )
                .bind(receipt)
                .bind(message_id.to_string())
                .fetch_one(&pool)
                .await?;
                if sent {
                    break;
                }
                sleep(Duration::from_millis(20)).await;
            }
            if index == 0 {
                set_connection(&pool, "reauth_required", "AuthenticationRequired").await?;
            }
        }
        assert!(
            timeout(Duration::from_millis(350), listener.accept())
                .await
                .is_err()
        );
        Ok::<_, Box<dyn Error>>(())
    };
    timeout(Duration::from_secs(8), async {
        tokio::select! {
            result = server => result,
            worker = transport.run_replies_with_garmin_notices(&database, &settings, Some(&launch)) => worker,
        }
    })
    .await??;
    let rows: Vec<(String, String, String)> = sqlx::query_as(
        "SELECT dedup_key,status,payload->>'key' FROM jobs
         WHERE kind='telegram_connection_notice' ORDER BY dedup_key",
    )
    .fetch_all(&pool)
    .await?;
    assert_eq!(
        rows,
        vec![
            (account_key.clone(), "done".into(), account_key),
            (auth_key.clone(), "done".into(), auth_key),
        ]
    );
    Ok(())
}
