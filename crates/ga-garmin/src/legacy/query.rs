use std::num::{NonZeroU16, NonZeroU64};

use reqwest::Url;
use time::{Date, format_description::well_known::Iso8601};

use super::endpoint;
use crate::{DateRange, GarminError};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DailyEndpoint {
    Summary,
    Steps,
    HeartRate,
    Sleep,
    Hrv,
    Stress,
    BodyBattery,
    BodyBatteryEvents,
    Respiration,
    Spo2,
    Readiness,
    TrainingStatus,
    MaxMetrics,
    Endurance,
    Hill,
    Hydration,
    BodyComposition,
    Intensity,
    RestingHeartRate,
    AllDayEvents,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ActivityEndpoint {
    Summary,
    Details,
    Splits,
    TypedSplits,
    Zones,
    Weather,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LegacyResource {
    Day(DailyEndpoint),
    Devices,
    Activity(ActivityEndpoint),
    ActivityPage,
    ActivityFit,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LegacyInventoryEntry {
    pub name: &'static str,
    pub method: &'static str,
    pub resource: LegacyResource,
}

impl LegacyInventoryEntry {
    const fn new(name: &'static str, method: &'static str, resource: LegacyResource) -> Self {
        Self {
            name,
            method,
            resource,
        }
    }

    #[must_use]
    pub fn scope(self) -> &'static str {
        match self.resource {
            LegacyResource::Day(_) => "day",
            LegacyResource::Devices => "global",
            LegacyResource::Activity(_) | LegacyResource::ActivityFit => "activity",
            LegacyResource::ActivityPage => "page",
        }
    }
}

/// The ordered legacy coverage inventory, including the two specialized readers.
pub const LEGACY_CAPABILITIES: &[LegacyInventoryEntry] = &[
    LegacyInventoryEntry::new(
        "daily",
        "get_stats",
        LegacyResource::Day(DailyEndpoint::Summary),
    ),
    LegacyInventoryEntry::new(
        "steps",
        "get_steps_data",
        LegacyResource::Day(DailyEndpoint::Steps),
    ),
    LegacyInventoryEntry::new(
        "heart_rate",
        "get_heart_rates",
        LegacyResource::Day(DailyEndpoint::HeartRate),
    ),
    LegacyInventoryEntry::new(
        "sleep",
        "get_sleep_data",
        LegacyResource::Day(DailyEndpoint::Sleep),
    ),
    LegacyInventoryEntry::new(
        "hrv",
        "get_hrv_data",
        LegacyResource::Day(DailyEndpoint::Hrv),
    ),
    LegacyInventoryEntry::new(
        "stress",
        "get_stress_data",
        LegacyResource::Day(DailyEndpoint::Stress),
    ),
    LegacyInventoryEntry::new(
        "body_battery",
        "get_body_battery",
        LegacyResource::Day(DailyEndpoint::BodyBattery),
    ),
    LegacyInventoryEntry::new(
        "body_battery_events",
        "get_body_battery_events",
        LegacyResource::Day(DailyEndpoint::BodyBatteryEvents),
    ),
    LegacyInventoryEntry::new(
        "respiration",
        "get_respiration_data",
        LegacyResource::Day(DailyEndpoint::Respiration),
    ),
    LegacyInventoryEntry::new(
        "spo2",
        "get_spo2_data",
        LegacyResource::Day(DailyEndpoint::Spo2),
    ),
    LegacyInventoryEntry::new(
        "readiness",
        "get_training_readiness",
        LegacyResource::Day(DailyEndpoint::Readiness),
    ),
    LegacyInventoryEntry::new(
        "training_status",
        "get_training_status",
        LegacyResource::Day(DailyEndpoint::TrainingStatus),
    ),
    LegacyInventoryEntry::new(
        "max_metrics",
        "get_max_metrics",
        LegacyResource::Day(DailyEndpoint::MaxMetrics),
    ),
    LegacyInventoryEntry::new(
        "endurance",
        "get_endurance_score",
        LegacyResource::Day(DailyEndpoint::Endurance),
    ),
    LegacyInventoryEntry::new(
        "hill",
        "get_hill_score",
        LegacyResource::Day(DailyEndpoint::Hill),
    ),
    LegacyInventoryEntry::new(
        "hydration",
        "get_hydration_data",
        LegacyResource::Day(DailyEndpoint::Hydration),
    ),
    LegacyInventoryEntry::new(
        "body_composition",
        "get_body_composition",
        LegacyResource::Day(DailyEndpoint::BodyComposition),
    ),
    LegacyInventoryEntry::new(
        "intensity",
        "get_intensity_minutes_data",
        LegacyResource::Day(DailyEndpoint::Intensity),
    ),
    LegacyInventoryEntry::new(
        "resting_hr",
        "get_rhr_day",
        LegacyResource::Day(DailyEndpoint::RestingHeartRate),
    ),
    LegacyInventoryEntry::new(
        "all_day_events",
        "get_all_day_events",
        LegacyResource::Day(DailyEndpoint::AllDayEvents),
    ),
    LegacyInventoryEntry::new("devices", "get_devices", LegacyResource::Devices),
    LegacyInventoryEntry::new(
        "activity",
        "get_activity",
        LegacyResource::Activity(ActivityEndpoint::Summary),
    ),
    LegacyInventoryEntry::new(
        "activity_details",
        "get_activity_details",
        LegacyResource::Activity(ActivityEndpoint::Details),
    ),
    LegacyInventoryEntry::new(
        "activity_splits",
        "get_activity_splits",
        LegacyResource::Activity(ActivityEndpoint::Splits),
    ),
    LegacyInventoryEntry::new(
        "activity_typed_splits",
        "get_activity_typed_splits",
        LegacyResource::Activity(ActivityEndpoint::TypedSplits),
    ),
    LegacyInventoryEntry::new(
        "activity_zones",
        "get_activity_hr_in_timezones",
        LegacyResource::Activity(ActivityEndpoint::Zones),
    ),
    LegacyInventoryEntry::new(
        "activity_weather",
        "get_activity_weather",
        LegacyResource::Activity(ActivityEndpoint::Weather),
    ),
    LegacyInventoryEntry::new("activities", "get_activities", LegacyResource::ActivityPage),
    LegacyInventoryEntry::new(
        "activity_fit",
        "download_activity",
        LegacyResource::ActivityFit,
    ),
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LegacyRead {
    Daily(DailyEndpoint, Date),
    Devices,
    Activities { start: u32, limit: NonZeroU16 },
    ActivitiesInRange { limit: NonZeroU16, dates: DateRange },
    Activity(ActivityEndpoint, NonZeroU64),
}

impl LegacyRead {
    /// Interpret a date, device, or activity endpoint saved by the scheduler.
    pub fn queued_endpoint(name: &str, key: &str) -> Result<Self, GarminError> {
        let resource = LEGACY_CAPABILITIES
            .iter()
            .find(|entry| entry.name == name)
            .ok_or(GarminError::InvalidRequest)?
            .resource;
        match resource {
            LegacyResource::Day(endpoint) => {
                let day = Date::parse(key, &Iso8601::DATE)
                    .ok()
                    .filter(|day| (1..=9999).contains(&day.year()))
                    .ok_or(GarminError::InvalidRequest)?;
                Ok(Self::Daily(endpoint, day))
            }
            LegacyResource::Devices if key == "global" => Ok(Self::Devices),
            LegacyResource::Activity(endpoint) => {
                let id = key.parse().map_err(|_| GarminError::InvalidRequest)?;
                Ok(Self::Activity(endpoint, id))
            }
            _ => Err(GarminError::InvalidRequest),
        }
    }

    pub(super) fn url(self, base: &Url, display_name: Option<&str>) -> Result<Url, GarminError> {
        match self {
            Self::Daily(resource, day) => resource.url(base, day, display_name),
            Self::Devices => endpoint(base, "device-service/deviceregistration/devices"),
            Self::Activities { start, limit } => {
                if limit.get() > 1000 {
                    return Err(GarminError::InvalidRequest);
                }
                let mut url = endpoint(base, "activitylist-service/activities/search/activities")?;
                url.query_pairs_mut()
                    .append_pair("start", &start.to_string())
                    .append_pair("limit", &limit.to_string());
                Ok(url)
            }
            Self::ActivitiesInRange { limit, dates } => {
                let mut url = Self::Activities { start: 0, limit }.url(base, display_name)?;
                dates.add_query(&mut url);
                Ok(url)
            }
            Self::Activity(resource, id) => resource.url(base, id),
        }
    }
}

impl DailyEndpoint {
    fn url(self, base: &Url, day: Date, display_name: Option<&str>) -> Result<Url, GarminError> {
        if !(1..=9999).contains(&day.year()) {
            return Err(GarminError::InvalidRequest);
        }
        let day = day.to_string();
        let day = day.as_str();
        let dated_path = |path: &str| endpoint(base, &format!("{path}/{day}"));
        let query = |path: &str, pairs: &[(&str, &str)]| -> Result<Url, GarminError> {
            let mut url = endpoint(base, path)?;
            url.query_pairs_mut().extend_pairs(pairs);
            Ok(url)
        };
        let profiled = |path: &str, pairs: &[(&str, &str)]| -> Result<Url, GarminError> {
            let name = display_name
                .filter(|name| !name.trim().is_empty() && !matches!(*name, "." | ".."))
                .ok_or(GarminError::ProfileUnavailable)?;
            let mut url = query(path, pairs)?;
            url.path_segments_mut()
                .map_err(|()| GarminError::InvalidConfiguration)?
                .push(name);
            Ok(url)
        };
        match self {
            Self::Summary => profiled(
                "usersummary-service/usersummary/daily",
                &[("calendarDate", day)],
            ),
            Self::Steps => profiled(
                "wellness-service/wellness/dailySummaryChart",
                &[("date", day)],
            ),
            Self::HeartRate => {
                profiled("wellness-service/wellness/dailyHeartRate", &[("date", day)])
            }
            Self::Sleep => profiled(
                "wellness-service/wellness/dailySleepData",
                &[("date", day), ("nonSleepBufferMinutes", "60")],
            ),
            Self::Hrv => dated_path("hrv-service/hrv"),
            Self::Stress => dated_path("wellness-service/wellness/dailyStress"),
            Self::BodyBattery => query(
                "wellness-service/wellness/bodyBattery/reports/daily",
                &[("startDate", day), ("endDate", day)],
            ),
            Self::BodyBatteryEvents => dated_path("wellness-service/wellness/bodyBattery/events"),
            Self::Respiration => dated_path("wellness-service/wellness/daily/respiration"),
            Self::Spo2 => dated_path("wellness-service/wellness/daily/spo2"),
            Self::Readiness => dated_path("metrics-service/metrics/trainingreadiness"),
            Self::TrainingStatus => dated_path("metrics-service/metrics/trainingstatus/aggregated"),
            Self::MaxMetrics => endpoint(
                base,
                &format!("metrics-service/metrics/maxmet/daily/{day}/{day}"),
            ),
            Self::Endurance => query(
                "metrics-service/metrics/endurancescore",
                &[("calendarDate", day)],
            ),
            Self::Hill => query(
                "metrics-service/metrics/hillscore",
                &[("calendarDate", day)],
            ),
            Self::Hydration => dated_path("usersummary-service/usersummary/hydration/daily"),
            Self::BodyComposition => query(
                "weight-service/weight/dateRange",
                &[("startDate", day), ("endDate", day)],
            ),
            Self::Intensity => dated_path("wellness-service/wellness/daily/im"),
            Self::RestingHeartRate => profiled(
                "userstats-service/wellness/daily",
                &[("fromDate", day), ("untilDate", day), ("metricId", "60")],
            ),
            Self::AllDayEvents => query(
                "wellness-service/wellness/dailyEvents",
                &[("calendarDate", day)],
            ),
        }
    }
}

impl ActivityEndpoint {
    fn url(self, base: &Url, id: NonZeroU64) -> Result<Url, GarminError> {
        let suffix = match self {
            Self::Summary => "",
            Self::Details => "/details",
            Self::Splits => "/splits",
            Self::TypedSplits => "/typedsplits",
            Self::Zones => "/hrTimeInZones",
            Self::Weather => "/weather",
        };
        let mut url = endpoint(base, &format!("activity-service/activity/{id}{suffix}"))?;
        if self == Self::Details {
            url.query_pairs_mut()
                .append_pair("maxChartSize", "2000")
                .append_pair("maxPolylineSize", "4000");
        }
        Ok(url)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{collections::BTreeMap, error::Error};
    use time::Month;

    #[test]
    fn native_routes_match_all_requests_captured_from_the_pinned_sdk() -> Result<(), Box<dyn Error>>
    {
        let base = Url::parse("https://connectapi.garmin.com/")?;
        let day = Date::from_calendar_date(2026, Month::November, 1)?;
        let activity = NonZeroU64::try_from(42)?;
        let expected: BTreeMap<String, (String, BTreeMap<String, String>)> = serde_json::from_str(
            include_str!("../../../../migration/contracts/garmin-legacy-requests.json"),
        )?;
        let mut actual = BTreeMap::new();
        for entry in LEGACY_CAPABILITIES {
            let resource = match entry.resource {
                LegacyResource::Day(endpoint) => LegacyRead::Daily(endpoint, day),
                LegacyResource::Devices => LegacyRead::Devices,
                LegacyResource::Activity(endpoint) => LegacyRead::Activity(endpoint, activity),
                LegacyResource::ActivityPage => LegacyRead::Activities {
                    start: 10,
                    limit: NonZeroU16::try_from(100)?,
                },
                LegacyResource::ActivityFit => continue,
            };
            let url = resource.url(&base, Some("Synthetic Profile"))?;
            actual.insert(
                entry.name.to_owned(),
                (
                    url.path().to_owned(),
                    url.query_pairs().into_owned().collect(),
                ),
            );
        }
        let original_export = super::super::reader::activity_export(&base, activity)?;
        actual.insert(
            "activity_fit".to_owned(),
            (original_export.path().to_owned(), BTreeMap::new()),
        );
        assert_eq!(actual, expected);
        Ok(())
    }

    #[test]
    fn scoped_activity_query_sends_both_date_bounds() -> Result<(), Box<dyn Error>> {
        let base = Url::parse("https://connectapi.garmin.com/")?;
        let day = Date::from_calendar_date(2026, Month::November, 20)?;
        let resource = LegacyRead::ActivitiesInRange {
            limit: NonZeroU16::try_from(100)?,
            dates: DateRange::new(day, day.next_day().ok_or(GarminError::InvalidRequest)?)?,
        };
        let url = resource.url(&base, None)?;
        assert_eq!(
            url.path(),
            "/activitylist-service/activities/search/activities"
        );
        assert_eq!(
            url.query(),
            Some("start=0&limit=100&startDate=2026-11-20&endDate=2026-11-21")
        );
        Ok(())
    }

    #[test]
    fn profile_names_never_change_the_destination_or_other_query_values()
    -> Result<(), Box<dyn Error>> {
        let base = Url::parse("https://connectapi.garmin.com/")?;
        let day = Date::from_calendar_date(2026, Month::November, 1)?;
        let resource = LegacyRead::Daily(DailyEndpoint::Summary, day);
        assert_eq!(
            resource.url(&base, None),
            Err(GarminError::ProfileUnavailable)
        );
        assert_eq!(
            resource.url(&base, Some("..")),
            Err(GarminError::ProfileUnavailable)
        );
        let path = resource.url(&base, Some("../synthetic#?.example"))?;
        assert_eq!(path.host_str(), base.host_str());
        assert_eq!(
            path.path(),
            "/usersummary-service/usersummary/daily/..%2Fsynthetic%23%3F.example"
        );
        assert_eq!(path.query(), Some("calendarDate=2026-11-01"));
        Ok(())
    }
}
