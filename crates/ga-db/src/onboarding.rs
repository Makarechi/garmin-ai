use std::{collections::BTreeSet, time::Duration};

use ga_domain::{
    event_input::known_timezone, event_schema::TrackerSetupDraft, localization::catalog_text,
    timestamp::StoredInstant,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{Postgres, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

use crate::{
    Database,
    diary::{ManageCustomDefinition, actions::TrackerSetupStoreError},
    scenario_packs::{ScenarioPackError, apply_onboarding_packs, pack_keys, scenario_packs_in},
    share_policy::OWNER_ONBOARDING_LOCK,
};

const KEY: &str = "preferences:onboarding";

#[derive(Debug)]
pub enum OnboardingError {
    Validation(&'static str),
    Pack(ScenarioPackError),
    Tracker(TrackerSetupStoreError),
    Database,
}

impl From<sqlx::Error> for OnboardingError {
    fn from(_: sqlx::Error) -> Self {
        Self::Database
    }
}

impl From<ScenarioPackError> for OnboardingError {
    fn from(error: ScenarioPackError) -> Self {
        Self::Pack(error)
    }
}

impl From<TrackerSetupStoreError> for OnboardingError {
    fn from(error: TrackerSetupStoreError) -> Self {
        Self::Tracker(error)
    }
}

#[derive(Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(deny_unknown_fields)]
struct ChannelSelection {
    channel: String,
    instance_id: String,
}

impl ChannelSelection {
    fn valid(&self) -> bool {
        (1..=100).contains(&self.channel.chars().count())
            && (1..=200).contains(&self.instance_id.chars().count())
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PlanFields {
    locale: String,
    timezone: String,
    units: String,
    #[serde(default)]
    selected_packs: BTreeSet<String>,
    #[serde(default)]
    reminder_packs: BTreeSet<String>,
    #[serde(default)]
    trackers: Vec<Value>,
    #[serde(default)]
    source_instance_ids: BTreeSet<String>,
    #[serde(default)]
    channel: Option<ChannelSelection>,
    #[serde(default)]
    fallback_channels: Vec<ChannelSelection>,
    #[serde(default)]
    model_categories: BTreeSet<String>,
}

pub struct OnboardingPlan {
    fields: PlanFields,
    trackers: Vec<TrackerSetupDraft>,
}

impl OnboardingPlan {
    pub fn parse(value: Value) -> Result<Self, OnboardingError> {
        use OnboardingError::Validation;
        let mut fields: PlanFields =
            serde_json::from_value(value).map_err(|_| Validation("Invalid arguments"))?;
        if !matches!(fields.locale.as_str(), "en" | "ru")
            || !matches!(fields.units.as_str(), "metric" | "imperial")
            || fields.source_instance_ids.len() > 20
            || fields.trackers.len() > 32
            || fields.fallback_channels.len() > 3
            || fields
                .channel
                .as_ref()
                .is_some_and(|channel| !channel.valid())
            || fields
                .fallback_channels
                .iter()
                .any(|channel| !channel.valid())
            || fields
                .model_categories
                .iter()
                .any(|category| !matches!(category.as_str(), "health" | "diary" | "audio"))
        {
            return Err(Validation("Invalid arguments"));
        }
        if !known_timezone(&fields.timezone) {
            return Err(Validation("Unknown timezone"));
        }
        let keys: BTreeSet<_> = pack_keys()?.collect();
        if fields
            .selected_packs
            .union(&fields.reminder_packs)
            .any(|key| !keys.contains(key.as_str()))
        {
            return Err(Validation("Unknown scenario pack"));
        }
        if !fields.reminder_packs.is_subset(&fields.selected_packs) {
            return Err(Validation("Reminders require a selected pack"));
        }
        let trackers: Vec<_> = std::mem::take(&mut fields.trackers)
            .iter()
            .map(TrackerSetupDraft::from_json)
            .collect::<Result<_, _>>()
            .map_err(|_| Validation("Invalid arguments"))?;
        let tracker_keys: BTreeSet<_> = trackers
            .iter()
            .map(|tracker| tracker.definition().key())
            .collect();
        if tracker_keys.len() != trackers.len() {
            return Err(Validation("Tracker manifest contains duplicate keys"));
        }
        let fallbacks: BTreeSet<_> = fields.fallback_channels.iter().collect();
        if fallbacks.len() != fields.fallback_channels.len() {
            return Err(Validation("Fallback channels must be unique"));
        }
        if fields
            .channel
            .as_ref()
            .is_some_and(|primary| fallbacks.contains(primary))
        {
            return Err(Validation("Primary channel cannot also be a fallback"));
        }
        Ok(Self { fields, trackers })
    }
}

#[derive(Serialize)]
pub struct OwnerProfile {
    pub locale: String,
    pub timezone: String,
    pub units: String,
}

impl Database {
    pub async fn onboarding_status(
        &self,
        owner: Uuid,
        integrations: Vec<Value>,
    ) -> Result<Value, OnboardingError> {
        let mut txn = self.begin_scenario_write(owner).await?;
        let profile = owner_profile(&mut txn, owner).await?;
        let complete: bool =
            sqlx::query_scalar("SELECT EXISTS(SELECT FROM app_state WHERE key=$1)")
                .bind(KEY)
                .fetch_one(&mut *txn)
                .await?;
        let packs = scenario_packs_in(&mut txn, owner).await?;
        txn.commit().await?;
        let message = if complete {
            "onboarding.complete"
        } else {
            "onboarding.no_sources"
        };
        Ok(json!({
            "complete":complete, "locale":profile.locale, "timezone":profile.timezone, "units":profile.units,
            "supported_locales":["en","ru"], "packs":packs, "integrations":integrations,
            "can_finish_without_sources":true, "message":catalog_text(&profile.locale, message),
        }))
    }

    pub async fn tracker_profile(&self, owner: Uuid) -> Result<OwnerProfile, OnboardingError> {
        let mut txn = self.control.begin_with("BEGIN READ ONLY").await?;
        sqlx::query("SET LOCAL statement_timeout='750ms'")
            .execute(&mut *txn)
            .await?;
        crate::check_readiness(&mut txn)
            .await
            .map_err(|_| OnboardingError::Database)?;
        let profile = owner_profile(&mut txn, owner).await?;
        txn.commit().await?;
        Ok(profile)
    }

    pub async fn apply_onboarding(
        &self,
        owner: Uuid,
        plan: OnboardingPlan,
    ) -> Result<Value, OnboardingError> {
        tokio::time::timeout(Duration::from_secs(5), async {
            let mut txn = self.begin_scenario_write(owner).await?;
            let locked: bool = sqlx::query_scalar("SELECT pg_try_advisory_xact_lock($1)")
                .bind(OWNER_ONBOARDING_LOCK)
                .fetch_one(&mut *txn)
                .await?;
            if !locked {
                return Err(OnboardingError::Database);
            }
            let PlanFields {
                locale, timezone, units, selected_packs, reminder_packs,
                source_instance_ids, channel, fallback_channels, model_categories, ..
            } = plan.fields;
            let changed = sqlx::query("UPDATE people SET locale=$2,timezone=$3,units=$4 WHERE id=$1 AND singleton")
                .bind(owner).bind(&locale).bind(&timezone).bind(&units)
                .execute(&mut *txn).await?.rows_affected();
            if changed != 1 {
                return Err(OnboardingError::Database);
            }
            let llm = model_categories.contains("health") && model_categories.contains("diary");
            let packs = apply_onboarding_packs(&mut txn, owner, &selected_packs, &reminder_packs, llm).await?;
            let created_trackers = store_trackers(&mut txn, owner, &plan.trackers).await?;
            let revision: Option<String> = sqlx::query_scalar(
                "SELECT coalesce(value->>'revision','0') FROM app_state WHERE key=$1"
            ).bind(KEY).fetch_optional(&mut *txn).await?;
            let revision = revision.as_deref().unwrap_or("0").parse::<i64>().ok()
                .and_then(|revision| revision.checked_add(1)).ok_or(OnboardingError::Database)?;
            let now: OffsetDateTime = sqlx::query_scalar("SELECT clock_timestamp()").fetch_one(&mut *txn).await?;
            let completed = StoredInstant::try_from(now).map_err(|_| OnboardingError::Database)?;
            let preferences = json!({
                "revision":revision, "locale":locale, "timezone":timezone, "units":units,
                "selected_packs":selected_packs, "source_instance_ids":source_instance_ids,
                "channel":channel, "fallback_channels":fallback_channels,
                "model_categories":model_categories, "completed_at":completed.to_string(),
            });
            sqlx::query("INSERT INTO app_state(key,value) VALUES($1,$2::jsonb)
                         ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=clock_timestamp()")
                .bind(KEY).bind(preferences.to_string()).execute(&mut *txn).await?;
            txn.commit().await?;
            Ok(json!({
                "complete":true, "message":catalog_text(&locale,"onboarding.complete"),
                "preferences":preferences, "created_trackers":created_trackers, "packs":packs,
            }))
        })
        .await
        .map_err(|_| OnboardingError::Database)?
    }
}

async fn owner_profile(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
) -> Result<OwnerProfile, OnboardingError> {
    let (locale, timezone, units) =
        sqlx::query_as("SELECT locale,timezone,units FROM people WHERE id=$1 AND singleton")
            .bind(owner)
            .fetch_one(&mut **txn)
            .await?;
    Ok(OwnerProfile {
        locale,
        timezone,
        units,
    })
}

async fn store_trackers(
    txn: &mut Transaction<'_, Postgres>,
    owner: Uuid,
    trackers: &[TrackerSetupDraft],
) -> Result<Vec<Value>, OnboardingError> {
    let request = ManageCustomDefinition {
        owner_id: owner,
        authenticated_actor: "onboarding",
        may_manage_definitions: true,
    };
    let mut created = Vec::new();
    for draft in trackers {
        let exists: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT FROM event_definitions d JOIN tracker_configs t
               ON t.definition_id=d.id AND t.owner_id=d.owner_id WHERE d.owner_id=$1 AND d.key=$2)",
        )
        .bind(owner)
        .bind(draft.definition().key())
        .fetch_one(&mut **txn)
        .await?;
        if !exists {
            created.push(Database::create_tracker_in(txn, request, draft).await?);
        }
    }
    Ok(created)
}
