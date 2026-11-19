use std::{collections::BTreeMap, env, error::Error, future::pending, str::FromStr, sync::Mutex};

use ga_db::share_policy::{ShareCategory, TrackerShareGrant};
use ga_domain::{access::Scope, model_gate::ModelGateReason};
use ga_model::ModelUnavailability;
use serde_json::{Value, json};
use sqlx::{ConnectOptions, PgPool, postgres::PgConnectOptions};

use super::*;

const PRIMARY: &str = "fixture-primary";
const SECONDARY: &str = "fixture-secondary";
const FALLBACK: &[&str] = &[SECONDARY];

#[derive(Clone, Copy)]
enum Responses {
    InvalidThenStall,
    Success,
    RevokeAfterInvalid,
    CheckSettingsFence,
    CheckChannelFence,
    UnavailableThenSuccess,
    Quota,
}

struct Mock {
    attempted: Mutex<Vec<String>>,
    pool: PgPool,
    responses: Responses,
}

impl Mock {
    fn new(pool: &PgPool, responses: Responses) -> Self {
        Self {
            attempted: Mutex::default(),
            pool: pool.clone(),
            responses,
        }
    }

    fn attempts(&self) -> Vec<String> {
        self.attempted
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }
}

impl Sender for Mock {
    async fn send(
        &self,
        request: &PreparedRequest,
        remaining: Duration,
    ) -> Result<StructuredModelOutput, ModelTransportError> {
        assert!(!remaining.is_zero());
        let model = match request {
            PreparedRequest::Gemini(request) => &request.body()["model"],
            PreparedRequest::Structured(request) => &request.body()["model"],
            PreparedRequest::Extension(request) => &request.params()["model"],
        }
        .as_str()
        .ok_or(ModelTransportError::InvalidConfiguration)?;
        self.attempted
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .push(model.to_owned());
        let primary = model == PRIMARY;
        match (self.responses, primary) {
            (Responses::InvalidThenStall, false) => return pending().await,
            (Responses::RevokeAfterInvalid, true) => update_preferences(&self.pool, false)
                .await
                .map_err(|_| ModelTransportError::InvalidConfiguration)?,
            (Responses::CheckSettingsFence | Responses::CheckChannelFence, _) => {
                let mut change = self
                    .pool
                    .begin()
                    .await
                    .map_err(|_| ModelTransportError::InvalidConfiguration)?;
                let channel = matches!(self.responses, Responses::CheckChannelFence);
                let locks: &[i64] = if channel {
                    &[72104621, 72104631, 72104632]
                } else {
                    &[72104621]
                };
                for lock in locks {
                    let writable: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
                        .bind(lock)
                        .fetch_one(&mut *change)
                        .await
                        .map_err(|_| ModelTransportError::InvalidConfiguration)?;
                    assert!(
                        !writable,
                        "the selected sharing policy was writable during transport"
                    );
                }
                if channel {
                    let diary_writable: bool =
                        sqlx::query_scalar("SELECT pg_try_advisory_xact_lock(72104619)")
                            .fetch_one(&mut *change)
                            .await
                            .map_err(|_| ModelTransportError::InvalidConfiguration)?;
                    assert!(
                        diary_writable,
                        "the model attempt held the general diary lock"
                    );
                }
                change
                    .rollback()
                    .await
                    .map_err(|_| ModelTransportError::InvalidConfiguration)?;
            }
            (Responses::Quota, _) => {
                return Err(ModelTransportError::Unavailable {
                    reason: ModelUnavailability::RateLimited,
                    retry_after: Duration::from_secs(120),
                });
            }
            (Responses::UnavailableThenSuccess, true) => {
                return Err(ModelTransportError::Unavailable {
                    reason: ModelUnavailability::MissingModel,
                    retry_after: Duration::from_secs(120),
                });
            }
            _ => {}
        }
        let invalid = primary
            && matches!(
                self.responses,
                Responses::InvalidThenStall | Responses::RevokeAfterInvalid
            );
        let gemini_data = if invalid { "{}" } else { "{\"urgent\":false}" };
        match request {
            PreparedRequest::Gemini(request) => {
                let gemini_data = if request.body()["input"].is_array() {
                    "{\"text\":\"synthetic transcript\"}"
                } else {
                    gemini_data
                };
                request.decode_response(
                &serde_json::to_vec(&json!({
                    "status":"completed",
                    "steps":[{"type":"model_output","content":[{"type":"text","text":gemini_data}]}],
                    "usage":{"total_input_tokens":10,"total_output_tokens":5},
                })).map_err(|_| ModelTransportError::InvalidConfiguration)?
            )
            }
            PreparedRequest::Structured(request) => {
                let response = match request.provider() {
                    StructuredProvider::OpenAi => br#"{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"{\"urgent\":false}"}]}],"usage":{"input_tokens":10,"output_tokens":5}}"#.as_slice(),
                    StructuredProvider::Anthropic => br#"{"stop_reason":"end_turn","content":[{"type":"text","text":"{\"urgent\":false}"}],"usage":{"input_tokens":10,"output_tokens":5}}"#.as_slice(),
                };
                request.decode_response(response)
            }
            PreparedRequest::Extension(request) => request.decode_response(
                br#"{"value":{"urgent":false},"usage":{"input_tokens":10,"output_tokens":5}}"#,
            ),
        }
        .map_err(ModelTransportError::Response)
    }
}

fn input() -> ModelInput<'static> {
    ModelInput::Structured {
        contract: ApprovedModelSchema::ModelProbe,
        instruction: "Return the synthetic result",
        prompt: "Synthetic test",
        categories: ModelCategories::HEALTH,
    }
}

fn current_policy<'a>(
    gateway: &'a ModelGateway<'_>,
    fallbacks: &'a [&'a str],
    granted_at: StoredInstant,
) -> CurrentModelPolicy<'a> {
    let binding = gateway.binding();
    CurrentModelPolicy {
        configuration: ModelConfiguration {
            binding,
            enabled: true,
            structured_output: true,
            fallback_enabled: !fallbacks.is_empty(),
            fallback_models: fallbacks,
        },
        consent: Some(ModelConsent {
            provider: binding.provider,
            instance_id: binding.instance_id,
            primary_model: binding.primary_model,
            fallback_models: fallbacks,
            categories: ModelCategories::HEALTH,
            granted_at,
            policy_revision: 1,
        }),
    }
}

#[tokio::test]
#[ignore = "runs the native model gateway against disposable TimescaleDB and in-process responses"]
async fn enforces_consent_and_resumes_without_repeating_invalid_paid_attempts()
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
        .ok_or("Expected disposable DB")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let current: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(current, target);
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    update_preferences(&pool, true).await?;
    let grant = "2026-01-01T00:00:00Z".parse()?;
    let gateway = ModelGateway::gemini(
        database.clone(),
        SelectedModel {
            instance_id: "model:gemini:primary",
            primary_model: PRIMARY,
            deadline: Duration::from_secs(1),
        },
        GeminiOptions {
            key: "synthetic-gemini",
            fallback_enabled: true,
            fallback_models: FALLBACK,
            consent_models: FALLBACK,
            thinking_level: "",
            notify_quota: true,
        },
    )?;
    let policy = || current_policy(&gateway, FALLBACK, grant);
    let job = ActiveModelJob {
        id: Uuid::from_u128(800),
        lease_token: Uuid::from_u128(801),
    };
    sqlx::query(
        "INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,lease_until,lease_token,attempts)
        VALUES($1,'synthetic','{}','synthetic-gateway-job','running',now(),now()+interval '1 minute',$2,1)",
    )
    .bind(job.id)
    .bind(job.lease_token)
    .execute(&pool).await?;

    let blocked = Mock::new(&pool, Responses::Success);
    let mut python = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104634)")
        .execute(&mut *python)
        .await?;
    assert!(matches!(
        gateway
            .execute_with(input(), &policy, &[], Some(job), &blocked)
            .await,
        Err(GatewayError::Attempt(FallbackError::Cooldown {
            reason: ModelGateReason::Busy,
            ..
        }))
    ));
    python.rollback().await?;
    assert!(blocked.attempts().is_empty());

    let stale = ActiveModelJob {
        lease_token: Uuid::from_u128(802),
        ..job
    };
    assert!(matches!(
        gateway
            .execute_with(input(), &policy, &[], Some(stale), &blocked)
            .await,
        Err(GatewayError::State(ModelGateError::LeaseLost))
    ));
    assert!(blocked.attempts().is_empty());

    let stalled = Mock::new(&pool, Responses::InvalidThenStall);
    assert!(matches!(
        gateway
            .execute_with(input(), &policy, &[], Some(job), &stalled)
            .await,
        Err(GatewayError::Attempt(FallbackError::Cooldown {
            reason: ModelGateReason::ModelCooldown,
            ..
        }))
    ));
    assert_eq!(stalled.attempts(), [PRIMARY, SECONDARY]);
    let checkpoint: String =
        sqlx::query_scalar("SELECT (payload->'provider_resume')::text FROM jobs WHERE id=$1")
            .bind(job.id)
            .fetch_one(&pool)
            .await?;
    let checkpoint: Value = serde_json::from_str(&checkpoint)?;
    assert_eq!(checkpoint["invalid_models"], json!([PRIMARY]));
    assert_eq!(checkpoint["models"], json!([PRIMARY, SECONDARY]));
    assert_eq!(checkpoint["identity_version"], 2);
    let resumed = Mock::new(&pool, Responses::Success);
    let outcome = gateway
        .execute_with(input(), &policy, &[], Some(job), &resumed)
        .await?;
    assert_eq!(outcome.value, json!({"urgent":false}));
    assert_eq!(resumed.attempts(), [SECONDARY]);
    let resume_cleared: bool =
        sqlx::query_scalar("SELECT NOT (payload ? 'provider_resume') FROM jobs WHERE id=$1")
            .bind(job.id)
            .fetch_one(&pool)
            .await?;
    assert!(resume_cleared);

    let rejected_tracker = [ShareRequirement {
        version_id: Uuid::from_u128(803),
        categories: &[ga_db::share_policy::ShareCategory::Facts],
    }];
    assert!(matches!(
        gateway
            .execute_with(input(), &policy, &rejected_tracker, Some(job), &blocked)
            .await,
        Err(GatewayError::Attempt(FallbackError::ConsentRequired))
    ));
    assert!(blocked.attempts().is_empty());

    let mut tracker_change = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104632)")
        .execute(&mut *tracker_change)
        .await?;
    assert!(matches!(
        gateway
            .execute_with(input(), &policy, &[], Some(job), &blocked)
            .await,
        Err(GatewayError::Attempt(FallbackError::Cooldown {
            reason: ModelGateReason::Busy,
            ..
        }))
    ));
    tracker_change.rollback().await?;
    assert!(blocked.attempts().is_empty());

    let revoked = Mock::new(&pool, Responses::RevokeAfterInvalid);
    assert!(matches!(
        gateway
            .execute_with(input(), &policy, &[], Some(job), &revoked)
            .await,
        Err(GatewayError::Attempt(FallbackError::ConsentRequired))
    ));
    assert_eq!(revoked.attempts(), [PRIMARY]);
    update_preferences(&pool, true).await?;

    let wrong_settings = || {
        let mut decision = policy();
        decision.configuration.binding.credential_generation = "replaced";
        decision
    };
    assert!(matches!(
        gateway
            .execute_with(input(), &wrong_settings, &[], Some(job), &blocked)
            .await,
        Err(GatewayError::Consent(ModelConsentError::ConsentRequired))
    ));
    assert!(blocked.attempts().is_empty());

    sqlx::raw_sql(
        "CREATE FUNCTION fail_synthetic_gate_write() RETURNS trigger LANGUAGE plpgsql AS $$
         BEGIN RAISE EXCEPTION 'synthetic bookkeeping failure'; END $$;
         CREATE TRIGGER fail_synthetic_gate_write BEFORE INSERT OR UPDATE ON app_state
         FOR EACH ROW WHEN (NEW.key='provider:gemini:gate') EXECUTE FUNCTION fail_synthetic_gate_write();"
    ).execute(&pool).await?;
    let failed_write = Mock::new(&pool, Responses::UnavailableThenSuccess);
    let outcome = gateway
        .execute_with(input(), &policy, &[], Some(job), &failed_write)
        .await?;
    assert_eq!(outcome.value, json!({"urgent":false}));
    assert_eq!(failed_write.attempts(), [PRIMARY, SECONDARY]);

    sqlx::query("DROP TRIGGER fail_synthetic_gate_write ON app_state")
        .execute(&pool)
        .await?;
    let quota = Mock::new(&pool, Responses::Quota);
    assert!(matches!(
        gateway
            .execute_with(input(), &policy, &[], Some(job), &quota)
            .await,
        Err(GatewayError::Attempt(FallbackError::Provider(
            ModelTransportError::Unavailable {
                reason: ModelUnavailability::RateLimited,
                ..
            }
        )))
    ));
    assert_eq!(quota.attempts(), [PRIMARY, SECONDARY]);
    assert!(matches!(
        gateway
            .execute_with(input(), &policy, &[], Some(job), &blocked)
            .await,
        Err(GatewayError::Attempt(FallbackError::Cooldown {
            reason: ModelGateReason::Quota,
            ..
        }))
    ));
    let notices: i64 =
        sqlx::query_scalar("SELECT count(*) FROM jobs WHERE kind='telegram_provider_notice'")
            .fetch_one(&pool)
            .await?;
    assert_eq!(notices, 1);
    assert!(blocked.attempts().is_empty());

    sqlx::query("DELETE FROM app_state WHERE key='provider:gemini:gate'")
        .execute(&pool)
        .await?;
    sqlx::query(r#"UPDATE app_state SET value='{"model_categories":["audio","health"]}'::jsonb WHERE key='preferences:onboarding'"#)
        .execute(&pool).await?;
    let voice_policy = || {
        let mut selection = policy();
        if let Some(consent) = &mut selection.consent {
            consent.categories = ModelCategories::AUDIO;
        }
        selection
    };
    let voice = Mock::new(&pool, Responses::Success);
    let outcome = gateway
        .execute_with(
            ModelInput::Voice {
                audio: b"synthetic",
                mime_type: "audio/ogg",
            },
            &voice_policy,
            &[],
            None,
            &voice,
        )
        .await?;
    assert_eq!(outcome.value, json!({"text":"synthetic transcript"}));
    assert_eq!(voice.attempts(), [PRIMARY]);

    for (provider, instance_id) in [
        (StructuredProvider::OpenAi, "model:openai:fixture"),
        (StructuredProvider::Anthropic, "model:anthropic:fixture"),
    ] {
        let structured = ModelGateway::structured(
            database.clone(),
            SelectedModel {
                instance_id,
                primary_model: PRIMARY,
                deadline: Duration::from_secs(1),
            },
            StructuredOptions {
                provider,
                secrets: &BTreeMap::from([("api_key", Some("synthetic-structured-model"))]),
                max_output_tokens: 1024,
                transport_timeout: Duration::from_secs(60),
            },
        )?;
        let policy = || current_policy(&structured, &[], grant);
        let mock = Mock::new(&pool, Responses::Success);
        assert!(matches!(
            structured
                .execute_with(
                    ModelInput::Voice {
                        audio: b"synthetic",
                        mime_type: "audio/ogg"
                    },
                    &policy,
                    &[],
                    None,
                    &mock
                )
                .await,
            Err(GatewayError::Consent(
                ModelConsentError::UnsupportedCapability
            ))
        ));
        assert!(mock.attempts().is_empty());
        assert_eq!(
            structured
                .execute_with(input(), &policy, &[], None, &mock)
                .await?
                .value,
            json!({"urgent":false})
        );
        assert_eq!(mock.attempts(), [PRIMARY]);
    }
    println!("Verified the model gateway for Gemini fallback, OpenAI, and Anthropic");
    Ok(())
}

#[tokio::test]
#[ignore = "uses disposable TimescaleDB to test concurrent model consent edits"]
async fn keeps_saved_onboarding_consent_live_until_transport_finishes() -> Result<(), Box<dyn Error>>
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
        .ok_or("Expected disposable DB")?;
    let pool = PgPool::connect_with(options.clone()).await?;
    let actual: String = sqlx::query_scalar("SELECT current_database()")
        .fetch_one(&pool)
        .await?;
    assert_eq!(actual, expected);
    update_preferences(&pool, true).await?;
    let database = Database::connect_lazy(&url).map_err(|_| "Invalid fixture connection")?;
    let gateway = ModelGateway::gemini(
        database,
        SelectedModel {
            instance_id: "model:gemini:primary",
            primary_model: PRIMARY,
            deadline: Duration::from_secs(2),
        },
        GeminiOptions {
            key: "synthetic-gemini",
            fallback_enabled: false,
            fallback_models: &[],
            consent_models: &[],
            thinking_level: "",
            notify_quota: false,
        },
    )?;
    let grant = "2026-01-01T00:00:00Z".parse()?;
    let policy = || current_policy(&gateway, &[], grant);
    let send = Mock::new(&pool, Responses::CheckSettingsFence);
    gateway
        .execute_with(input(), &policy, &[], None, &send)
        .await?;
    assert_eq!(send.attempts(), [PRIMARY]);
    verify_tracker_channel_fence(&gateway, &pool, grant).await?;

    let mut change = pool.begin().await?;
    sqlx::query("SET LOCAL statement_timeout='750ms'")
        .execute(&mut *change)
        .await?;
    sqlx::query("SELECT pg_advisory_xact_lock(72104621)")
        .execute(&mut *change)
        .await?;
    assert!(matches!(
        gateway
            .execute_with(input(), &policy, &[], None, &send)
            .await,
        Err(GatewayError::Attempt(FallbackError::Cooldown {
            reason: ModelGateReason::Busy,
            ..
        }))
    ));
    sqlx::query("UPDATE app_state SET value='{\"model_categories\":[]}'::jsonb WHERE key='preferences:onboarding'")
        .execute(&mut *change).await?;
    change.commit().await?;
    assert!(matches!(
        gateway
            .execute_with(input(), &policy, &[], None, &send)
            .await,
        Err(GatewayError::Attempt(FallbackError::ConsentRequired))
    ));
    assert_eq!(send.attempts(), [PRIMARY]);
    Ok(())
}

async fn verify_tracker_channel_fence(
    gateway: &ModelGateway<'_>,
    pool: &PgPool,
    granted_at: StoredInstant,
) -> Result<(), Box<dyn Error>> {
    sqlx::raw_sql(
        "INSERT INTO event_definitions(id,owner_id,namespace,key,status,revision,current_version)
         SELECT '00000000-0000-0000-0000-000000000031',id,'user','user.synthetic','active',1,1 FROM people;
         INSERT INTO event_definition_versions(id,definition_id,version,schema,schema_hash,topology,field_metadata,labels,privacy,allowed_operations)
         VALUES('00000000-0000-0000-0000-000000000032','00000000-0000-0000-0000-000000000031',
            1,'{}',repeat('0',64),'point','{}','{\"en\":\"Synthetic\"}','sensitive','[\"query\"]');",
    ).execute(pool).await?;
    let database = &gateway.database;
    let definition_id = Uuid::from_u128(0x31);
    let version_id = Uuid::from_u128(0x32);
    let scopes = [Scope::ManageIntegrations].into_iter().collect();
    let model = ShareDestination::new(DestinationKind::Model, gateway.binding().instance_id)?;
    let channel = ShareDestination::new(DestinationKind::Channel, "telegram:synthetic")?;
    let model_requirements = [ShareRequirement {
        version_id,
        categories: &[ShareCategory::Facts],
    }];
    let channel_requirements = [ShareRequirement {
        version_id,
        categories: &[ShareCategory::Schema, ShareCategory::Facts],
    }];
    for (destination, requirements) in [
        (model, &model_requirements),
        (channel, &channel_requirements),
    ] {
        database
            .grant_tracker_share(
                scopes,
                TrackerShareGrant {
                    definition_id,
                    destination,
                    categories: requirements[0].categories,
                    granted_at,
                },
            )
            .await?;
    }
    let scope = || runtime::ModelRequestScope {
        uses_voice_text: false,
        requirements: &model_requirements,
        channel: Some(ModelChannelSharing {
            destination: channel,
            requirements: &channel_requirements,
            conversation: None,
        }),
        job: None,
    };
    let model_policy = || current_policy(gateway, &[], granted_at);
    let sender = Mock::new(pool, Responses::CheckChannelFence);
    gateway
        .execute_with_context(input(), &model_policy, scope(), &sender)
        .await?;
    assert_eq!(sender.attempts(), [PRIMARY]);
    assert!(
        database
            .revoke_tracker_share(scopes, definition_id, channel)
            .await?
    );
    assert!(
        database
            .tracker_version_allowed(model_requirements[0], model)
            .await?
    );
    assert!(matches!(
        gateway
            .execute_with_context(input(), &model_policy, scope(), &sender)
            .await,
        Err(GatewayError::Attempt(FallbackError::ConsentRequired))
    ));
    assert_eq!(sender.attempts(), [PRIMARY]);
    Ok(())
}

async fn update_preferences(pool: &PgPool, allowed: bool) -> Result<(), sqlx::Error> {
    let categories = if allowed {
        json!(["health"])
    } else {
        json!([])
    };
    sqlx::query(
        "INSERT INTO app_state(key,value) VALUES('preferences:onboarding',jsonb_build_object('model_categories',$1::jsonb))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    )
    .bind(categories.to_string())
    .execute(pool)
    .await?;
    Ok(())
}
