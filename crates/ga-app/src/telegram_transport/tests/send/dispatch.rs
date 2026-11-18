use super::{accepted, bind, request, respond};
use crate::telegram_input::TelegramBinding;

use std::{env, error::Error, str::FromStr, time::Duration};

use ga_db::{
    Database,
    delivery::DeliveryState,
    outbox_claims::{ReplyDestination, ReplyDispatchResult},
};
use ga_domain::{channel::telegram_conversation_id, timestamp::StoredInstant};
use serde_json::json;
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};
use time::OffsetDateTime;
use tokio::time::timeout;
use uuid::Uuid;

#[tokio::test]
#[ignore = "requires disposable TimescaleDB and a local Telegram transport"]
async fn persists_partial_acceptance_and_defers_only_unaccepted_sends() -> Result<(), Box<dyn Error>>
{
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
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, expected);
    let owner_id: Uuid = sqlx::query_scalar("SELECT id FROM people")
        .fetch_one(&pool)
        .await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let binding = TelegramBinding {
        owner_id,
        instance_id: "primary",
        external_owner_id: 42,
    };
    sqlx::query(
        "INSERT INTO channel_bindings(id,owner_id,channel,channel_instance_id,external_id,confirmation_method)
         VALUES(gen_random_uuid(),$1,'telegram','primary','42','fixture')",
    ).bind(owner_id).execute(&pool).await?;
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES('proactive:enabled','{\"enabled\":false}')",
    )
    .execute(&pool)
    .await?;
    let conversation = telegram_conversation_id(owner_id, binding.instance_id, 42);
    sqlx::query(
        "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
         VALUES($1,$2,'telegram','primary',gen_random_uuid(),'{}',false)",
    ).bind(conversation).bind(owner_id).execute(&pool).await?;
    let destination = || ReplyDestination {
        owner_id,
        channel: "telegram",
        instance_id: "primary",
    };

    for (number, status, second) in [
        (2510, "provider_accepted", true),
        (2511, "queued", false),
        (2512, "uncertain", true),
    ] {
        let outbox_id = Uuid::from_u128(number);
        seed(&pool, binding, conversation, outbox_id, false).await?;
        let claim = database
            .claim_outbox_reply(destination(), clock(&pool).await?, 120)
            .await?
            .ok_or("Missing outbox claim")?;
        assert_eq!(claim.outbox_id, outbox_id);
        if number == 2510 {
            assert_eq!(
                database
                    .dispatch_outbox_reply(destination(), claim, &[], |_| async {
                        panic!("Telegram cannot bypass its saved binding check")
                    })
                    .await?,
                ReplyDispatchResult::ClaimUnavailable,
            );
        }
        let (listener, transport) = bind().await?;
        let server = async {
            let (mut first, _) = listener.accept().await?;
            assert_eq!(
                request(&mut first).await?["fields"]["text"],
                "synthetic first"
            );
            if number == 2510 {
                let mut settings = pool.begin().await?;
                for lock in [72_104_621_i64, 72_104_627] {
                    let writeable: bool =
                        sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
                            .bind(lock)
                            .fetch_one(&mut *settings)
                            .await?;
                    assert!(!writeable, "Telegram policy was mutable during transport");
                }
                settings.rollback().await?;
            }
            if second {
                respond(&mut first, "200 OK", &[], &accepted(701, 42)).await?;
                let (mut next, _) = listener.accept().await?;
                assert_eq!(
                    request(&mut next).await?["fields"]["text"],
                    "synthetic second"
                );
                if status == "provider_accepted" {
                    respond(&mut next, "200 OK", &[], &accepted(702, 42)).await?;
                } else {
                    respond(
                        &mut next,
                        "429 Too Many Requests",
                        &[("Retry-After", "15")],
                        "{}",
                    )
                    .await?;
                }
            } else {
                respond(
                    &mut first,
                    "429 Too Many Requests",
                    &[("Retry-After", "15")],
                    "{}",
                )
                .await?;
            }
            assert!(
                timeout(Duration::from_millis(50), listener.accept())
                    .await
                    .is_err()
            );
            Ok::<_, Box<dyn Error>>(())
        };
        let (server, outcome) = timeout(Duration::from_secs(5), async {
            tokio::join!(
                server,
                transport.dispatch_claimed_reply(&database, binding, claim, &[])
            )
        })
        .await?;
        server?;
        let outcome = outcome?;
        let row: (String, Option<String>, Option<OffsetDateTime>) = sqlx::query_as(
            "SELECT state,provider_reference,next_attempt_at FROM outbox_messages WHERE id=$1",
        )
        .bind(outbox_id)
        .fetch_one(&pool)
        .await?;
        assert_eq!(row.0, status);
        match status {
            "queued" => {
                let ReplyDispatchResult::Deferred { retry_at } = outcome else {
                    return Err("Expected deferral".into());
                };
                assert_eq!(row.2, Some(OffsetDateTime::from(retry_at)));
                assert!(row.1.is_none());
                assert!(retry_at > clock(&pool).await?);
            }
            "provider_accepted" => {
                assert!(matches!(
                    outcome,
                    ReplyDispatchResult::Recorded {
                        state: DeliveryState::ProviderAccepted,
                        ..
                    }
                ));
                assert_eq!(row.1.as_deref(), Some("702"));
            }
            "uncertain" => {
                assert!(matches!(
                    outcome,
                    ReplyDispatchResult::Recorded {
                        state: DeliveryState::Uncertain,
                        ..
                    }
                ));
                assert_eq!(row.1.as_deref(), Some("701"));
                assert!(row.2.is_none());
                let detail: String = sqlx::query_scalar(
                    "SELECT detail FROM message_delivery_receipts WHERE outbox_message_id=$1",
                )
                .bind(outbox_id)
                .fetch_one(&pool)
                .await?;
                assert_eq!(
                    detail,
                    "Telegram accepted only part of a multi-message intent"
                );
            }
            _ => return Err("Unexpected stored state".into()),
        }
        assert_eq!(
            transport
                .dispatch_claimed_reply(&database, binding, claim, &[])
                .await?,
            ReplyDispatchResult::ClaimUnavailable
        );
    }
    let unsupported = Uuid::from_u128(2513);
    seed(&pool, binding, conversation, unsupported, true).await?;
    let claim = database
        .claim_outbox_reply(destination(), clock(&pool).await?, 120)
        .await?
        .ok_or("Missing unsupported reply")?;
    assert_eq!(claim.outbox_id, unsupported);
    let (listener, transport) = bind().await?;
    assert!(matches!(
        transport
            .dispatch_claimed_reply(&database, binding, claim, &[])
            .await?,
        ReplyDispatchResult::Deferred { .. }
    ));
    assert!(
        timeout(Duration::from_millis(50), listener.accept())
            .await
            .is_err()
    );
    for (number, disable) in [(2514, true), (2515, false)] {
        let pending = Uuid::from_u128(number);
        seed(&pool, binding, conversation, pending, false).await?;
        let claim = database
            .claim_outbox_reply(destination(), clock(&pool).await?, 120)
            .await?
            .ok_or("Missing stale-policy test claim")?;
        assert_eq!(claim.outbox_id, pending);
        if disable {
            sqlx::query(
                "INSERT INTO app_state(key,value) VALUES('preferences:onboarding','{\"channel\":null,\"fallback_channels\":[]}')",
            ).execute(&pool).await?;
        } else {
            sqlx::query("DELETE FROM app_state WHERE key='preferences:onboarding'")
                .execute(&pool)
                .await?;
            sqlx::query("UPDATE channel_bindings SET external_id='43' WHERE owner_id=$1")
                .bind(owner_id)
                .execute(&pool)
                .await?;
        }
        let (listener, transport) = bind().await?;
        assert_eq!(
            transport
                .dispatch_claimed_reply(&database, binding, claim, &[])
                .await?,
            ReplyDispatchResult::Cancelled,
        );
        assert!(
            timeout(Duration::from_millis(50), listener.accept())
                .await
                .is_err()
        );
        let state: String = sqlx::query_scalar("SELECT state FROM outbox_messages WHERE id=$1")
            .bind(pending)
            .fetch_one(&pool)
            .await?;
        assert_eq!(state, "cancelled");
    }
    let stale_reply = Uuid::from_u128(2516);
    seed(&pool, binding, conversation, stale_reply, false).await?;
    let claim = database
        .claim_outbox_reply(destination(), clock(&pool).await?, 120)
        .await?
        .ok_or("Missing pre-pairing reply")?;
    assert_eq!(claim.outbox_id, stale_reply);
    let new_binding = TelegramBinding {
        external_owner_id: 43,
        ..binding
    };
    let (listener, transport) = bind().await?;
    let (received, result) = tokio::join!(
        async {
            let Ok(Ok((mut stream, _))) =
                timeout(Duration::from_millis(350), listener.accept()).await
            else {
                return Ok::<_, Box<dyn Error>>(false);
            };
            let outgoing = request(&mut stream).await?;
            assert_eq!(outgoing["fields"]["chat_id"], 43);
            respond(&mut stream, "400 Bad Request", &[], "{}").await?;
            Ok(true)
        },
        transport.dispatch_claimed_reply(&database, new_binding, claim, &[]),
    );
    assert!(
        !received?,
        "The replacement chat received an earlier conversation's reply"
    );
    assert_eq!(result?, ReplyDispatchResult::Cancelled);
    let state: String = sqlx::query_scalar("SELECT state FROM outbox_messages WHERE id=$1")
        .bind(stale_reply)
        .fetch_one(&pool)
        .await?;
    assert_eq!(state, "cancelled");
    let current_conversation = telegram_conversation_id(owner_id, new_binding.instance_id, 43);
    sqlx::query(
        "INSERT INTO conversations(id,owner_id,channel,channel_instance_id,memory_epoch,state,share_owner_memory)
         VALUES($1,$2,'telegram','primary',gen_random_uuid(),'{}',false)",
    ).bind(current_conversation).bind(owner_id).execute(&pool).await?;
    let current_reply = Uuid::from_u128(2517);
    seed(
        &pool,
        new_binding,
        current_conversation,
        current_reply,
        false,
    )
    .await?;
    let claim = database
        .claim_outbox_reply(destination(), clock(&pool).await?, 120)
        .await?
        .ok_or("Missing current reply")?;
    assert_eq!(claim.outbox_id, current_reply);
    let (listener, transport) = bind().await?;
    let (accepted_current, result) = timeout(Duration::from_secs(3), async {
        tokio::join!(
            async {
                for message_id in [704, 705] {
                    let (mut stream, _) = listener.accept().await?;
                    assert_eq!(request(&mut stream).await?["fields"]["chat_id"], 43);
                    respond(&mut stream, "200 OK", &[], &accepted(message_id, 43)).await?;
                }
                Ok::<_, Box<dyn Error>>(())
            },
            transport.dispatch_claimed_reply(&database, new_binding, claim, &[]),
        )
    })
    .await?;
    accepted_current?;
    assert!(matches!(
        result?,
        ReplyDispatchResult::Recorded {
            state: DeliveryState::ProviderAccepted,
            ..
        }
    ));
    println!(
        "Verified durable acceptance, partial evidence, 429 deferral and unsupported media without live calls"
    );
    Ok(())
}

async fn clock(pool: &PgPool) -> Result<StoredInstant, Box<dyn Error>> {
    let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(pool)
        .await?;
    Ok(StoredInstant::try_from(now)?)
}

async fn seed(
    pool: &PgPool,
    binding: TelegramBinding<'_>,
    conversation: Uuid,
    outbox: Uuid,
    unsupported: bool,
) -> Result<(), sqlx::Error> {
    let mut intent = json!({
        "intent_id":outbox.to_string(),"owner_id":binding.owner_id.to_string(),
        "conversation_id":conversation.to_string(),"channel_instance":{"channel":"telegram","instance_id":"primary"},
        "blocks":[{"text":"synthetic first"},{"text":"synthetic second"}],
    });
    if unsupported {
        intent["attachments"] = json!([{"kind":"file","filename":"synthetic.csv"}]);
    }
    sqlx::query(
        "INSERT INTO outbox_messages(id,owner_id,conversation_id,operation_id,intent,dedup_key,state,attempts)
         VALUES($1,$2,$3,gen_random_uuid(),$4::jsonb,$5,'queued',0)",
    ).bind(outbox).bind(binding.owner_id).bind(conversation).bind(intent.to_string()).bind(format!("synthetic:{outbox}"))
     .execute(pool).await?;
    Ok(())
}
