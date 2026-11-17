use std::num::{NonZeroU16, NonZeroU64};

use reqwest::Url;
use time::Date;

use super::endpoint;
use crate::GarminError;

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
pub enum LegacyRead {
    Daily(DailyEndpoint, Date),
    Devices,
    Activities { start: u32, limit: NonZeroU16 },
    Activity(ActivityEndpoint, NonZeroU64),
}

impl LegacyRead {
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
                .map_err(|_| GarminError::InvalidConfiguration)?
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
        let days = [
            ("daily", DailyEndpoint::Summary),
            ("steps", DailyEndpoint::Steps),
            ("heart_rate", DailyEndpoint::HeartRate),
            ("sleep", DailyEndpoint::Sleep),
            ("hrv", DailyEndpoint::Hrv),
            ("stress", DailyEndpoint::Stress),
            ("body_battery", DailyEndpoint::BodyBattery),
            ("body_battery_events", DailyEndpoint::BodyBatteryEvents),
            ("respiration", DailyEndpoint::Respiration),
            ("spo2", DailyEndpoint::Spo2),
            ("readiness", DailyEndpoint::Readiness),
            ("training_status", DailyEndpoint::TrainingStatus),
            ("max_metrics", DailyEndpoint::MaxMetrics),
            ("endurance", DailyEndpoint::Endurance),
            ("hill", DailyEndpoint::Hill),
            ("hydration", DailyEndpoint::Hydration),
            ("body_composition", DailyEndpoint::BodyComposition),
            ("intensity", DailyEndpoint::Intensity),
            ("resting_hr", DailyEndpoint::RestingHeartRate),
            ("all_day_events", DailyEndpoint::AllDayEvents),
        ]
        .map(|(name, endpoint)| (name, LegacyRead::Daily(endpoint, day)));
        let activities = [
            ("activity", ActivityEndpoint::Summary),
            ("activity_details", ActivityEndpoint::Details),
            ("activity_splits", ActivityEndpoint::Splits),
            ("activity_typed_splits", ActivityEndpoint::TypedSplits),
            ("activity_zones", ActivityEndpoint::Zones),
            ("activity_weather", ActivityEndpoint::Weather),
        ]
        .map(|(name, endpoint)| (name, LegacyRead::Activity(endpoint, activity)));
        let other = [
            ("devices", LegacyRead::Devices),
            (
                "activities",
                LegacyRead::Activities {
                    start: 10,
                    limit: NonZeroU16::try_from(100)?,
                },
            ),
        ];
        let mut actual = BTreeMap::new();
        for (name, resource) in days.into_iter().chain(activities).chain(other) {
            let url = resource.url(&base, Some("Synthetic Profile"))?;
            actual.insert(
                name.to_owned(),
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
