use std::num::{NonZeroU16, NonZeroU64};

use reqwest::Url;
use time::Date;

use crate::GarminError;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LegacyCapability {
    IntradaySteps,
    DetailedStress,
    RespirationSeries,
    TrainingReadiness,
    TrainingStatus,
    MaxMetrics,
    Endurance,
    Hill,
    Devices,
    ActivityDetails,
    ActivityTypedSplits,
    ActivityZones,
    ActivityWeather,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DateRange {
    start: Date,
    end: Date,
}

impl DateRange {
    pub fn new(start: Date, end: Date) -> Result<Self, GarminError> {
        if start > end || !(1..=9999).contains(&start.year()) || !(1..=9999).contains(&end.year()) {
            return Err(GarminError::InvalidRequest);
        }
        Ok(Self { start, end })
    }

    pub fn contains(self, date: Date) -> bool {
        (self.start..=self.end).contains(&date)
    }

    pub(crate) fn add_query(self, url: &mut Url) {
        url.query_pairs_mut()
            .append_pair("startDate", &self.start.to_string())
            .append_pair("endDate", &self.end.to_string());
    }
}

#[derive(Clone, Copy)]
pub enum PersonalRead {
    ProfileSettings,
    Daily(Date),
    DailySteps(DateRange),
    HeartRate(Date),
    Sleep(Date),
    Hrv(Date),
    DailyStress(DateRange),
    BodyBattery(DateRange),
    Spo2(Date),
    DailyHydration(DateRange),
    WeightRange(DateRange),
    Activities {
        offset: u32,
        limit: NonZeroU16,
        dates: DateRange,
    },
    Activity(NonZeroU64),
    LegacyOnly(LegacyCapability),
}

impl PersonalRead {
    pub(crate) fn url(self, base: &Url) -> Result<Url, GarminError> {
        let join = |path: &str| base.join(path).map_err(|_| GarminError::InvalidRequest);
        let dated = |path: &str, name: &str, date: Date| {
            if !(1..=9999).contains(&date.year()) {
                return Err(GarminError::InvalidRequest);
            }
            let mut url = join(path)?;
            url.query_pairs_mut().append_pair(name, &date.to_string());
            Ok(url)
        };
        let range =
            |path: &str, dates: DateRange| join(&format!("{path}/{}/{}", dates.start, dates.end));

        match self {
            Self::ProfileSettings => join("userprofile-service/userprofile/settings"),
            Self::Daily(date) => dated(
                "usersummary-service/usersummary/daily",
                "calendarDate",
                date,
            ),
            Self::DailySteps(dates) => range("usersummary-service/stats/steps/daily", dates),
            Self::HeartRate(date) => {
                dated("wellness-service/wellness/dailyHeartRate", "date", date)
            }
            Self::Sleep(date) => dated("sleep-service/sleep/dailySleep", "date", date),
            Self::Hrv(date) => {
                if !(1..=9999).contains(&date.year()) {
                    return Err(GarminError::InvalidRequest);
                }
                join(&format!("hrv-service/hrv/{date}"))
            }
            Self::DailyStress(dates) => range("usersummary-service/stats/stress/daily", dates),
            Self::BodyBattery(dates) => {
                let mut url = join("wellness-service/wellness/bodyBattery/reports/daily")?;
                url.query_pairs_mut()
                    .append_pair("startDate", &dates.start.to_string())
                    .append_pair("endDate", &dates.end.to_string());
                Ok(url)
            }
            Self::Spo2(date) => {
                if !(1..=9999).contains(&date.year()) {
                    return Err(GarminError::InvalidRequest);
                }
                join(&format!(
                    "wellness-service/wellness/daily/spo2acclimation/{date}"
                ))
            }
            Self::DailyHydration(dates) => {
                range("usersummary-service/stats/hydration/daily", dates)
            }
            Self::WeightRange(dates) => {
                let mut url = range("weight-service/weight/range", dates)?;
                url.query_pairs_mut().append_pair("includeAll", "true");
                Ok(url)
            }
            Self::Activities {
                offset,
                limit,
                dates,
            } => {
                let mut url = join("activitylist-service/activities/search/activities")?;
                url.query_pairs_mut()
                    .append_pair("start", &offset.to_string())
                    .append_pair("limit", &limit.to_string());
                dates.add_query(&mut url);
                Ok(url)
            }
            Self::Activity(id) => join(&format!("activity-service/activity/{id}")),
            Self::LegacyOnly(capability) => Err(GarminError::UnsupportedCapability(capability)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use time::Month;

    #[test]
    fn paths_follow_the_published_resource_shapes() -> Result<(), Box<dyn std::error::Error>> {
        let base = Url::parse("https://personal.connectapi.garmin.com/")?;
        let start = Date::from_calendar_date(2026, Month::November, 1)?;
        let end = Date::from_calendar_date(2026, Month::November, 2)?;
        let dates = DateRange::new(start, end)?;
        let activity = NonZeroU64::try_from(42)?;
        let cases = [
            (
                PersonalRead::ProfileSettings,
                "userprofile-service/userprofile/settings",
            ),
            (
                PersonalRead::Daily(start),
                "usersummary-service/usersummary/daily?calendarDate=2026-11-01",
            ),
            (
                PersonalRead::DailySteps(dates),
                "usersummary-service/stats/steps/daily/2026-11-01/2026-11-02",
            ),
            (
                PersonalRead::HeartRate(start),
                "wellness-service/wellness/dailyHeartRate?date=2026-11-01",
            ),
            (
                PersonalRead::Sleep(start),
                "sleep-service/sleep/dailySleep?date=2026-11-01",
            ),
            (PersonalRead::Hrv(start), "hrv-service/hrv/2026-11-01"),
            (
                PersonalRead::DailyStress(dates),
                "usersummary-service/stats/stress/daily/2026-11-01/2026-11-02",
            ),
            (
                PersonalRead::BodyBattery(dates),
                "wellness-service/wellness/bodyBattery/reports/daily?startDate=2026-11-01&endDate=2026-11-02",
            ),
            (
                PersonalRead::Spo2(start),
                "wellness-service/wellness/daily/spo2acclimation/2026-11-01",
            ),
            (
                PersonalRead::DailyHydration(dates),
                "usersummary-service/stats/hydration/daily/2026-11-01/2026-11-02",
            ),
            (
                PersonalRead::WeightRange(dates),
                "weight-service/weight/range/2026-11-01/2026-11-02?includeAll=true",
            ),
            (
                PersonalRead::Activities {
                    offset: 10,
                    limit: NonZeroU16::try_from(20)?,
                    dates,
                },
                "activitylist-service/activities/search/activities?start=10&limit=20&startDate=2026-11-01&endDate=2026-11-02",
            ),
            (
                PersonalRead::Activity(activity),
                "activity-service/activity/42",
            ),
        ];
        for (request, path) in cases {
            assert_eq!(request.url(&base)?, base.join(path)?);
        }
        assert!(matches!(
            DateRange::new(end, start),
            Err(GarminError::InvalidRequest)
        ));
        let too_early = Date::from_calendar_date(0, Month::November, 1)?;
        assert_eq!(
            PersonalRead::Daily(too_early).url(&base),
            Err(GarminError::InvalidRequest)
        );
        Ok(())
    }
}
