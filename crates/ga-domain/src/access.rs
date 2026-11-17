#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum Scope {
    ReadHealth = 1,
    ReadDiary = 2,
    WriteDiary = 4,
    WriteWearable = 8,
    ManageDefinitions = 16,
    ManageIntegrations = 32,
    Admin = 64,
}

impl Scope {
    pub fn from_name(name: &str) -> Option<Self> {
        Some(match name {
            "read:health" => Self::ReadHealth,
            "read:diary" => Self::ReadDiary,
            "write:diary" => Self::WriteDiary,
            "write:wearable" => Self::WriteWearable,
            "manage:definitions" => Self::ManageDefinitions,
            "manage:integrations" => Self::ManageIntegrations,
            "admin" => Self::Admin,
            _ => return None,
        })
    }
}

#[derive(Default, Clone, Copy, Debug, Eq, PartialEq)]
pub struct Scopes(u8);

impl FromIterator<Scope> for Scopes {
    fn from_iter<T: IntoIterator<Item = Scope>>(values: T) -> Self {
        Self(values.into_iter().fold(0, |bits, scope| bits | scope as u8))
    }
}

impl Scopes {
    pub fn includes(self, scope: Scope) -> bool {
        self.0 & scope as u8 != 0
    }

    pub fn permits(self, required: &[Scope]) -> bool {
        self.includes(Scope::Admin) || required.iter().all(|scope| self.includes(*scope))
    }

    pub fn permits_tool(self, name: &str, input: ReadToolInput<'_>) -> bool {
        use Scope::{ReadDiary, ReadHealth};
        let required: &[Scope] = match (name, input) {
            ("generic_analysis" | "resource_catalog", ReadToolInput::Discovery) => {
                return self.permits(&[ReadDiary]) || self.permits(&[ReadHealth]);
            }
            ("resource_catalog", ReadToolInput::Catalog(kind)) => match kind {
                "events" | "tracker_metrics" => &[ReadDiary],
                "system_metrics" | "raw_sources" => &[ReadHealth],
                _ => return false,
            },
            ("generic_analysis", ReadToolInput::Analysis { operation, metric }) => {
                if operation == "query_entries"
                    || metric.is_some_and(|metric| metric.starts_with("user."))
                {
                    &[ReadDiary]
                } else {
                    &[ReadHealth]
                }
            }
            (_, ReadToolInput::Discovery) => match name {
                "analysis_coffee_sleep"
                | "timeline"
                | "insights_list"
                | "analysis_event_windows"
                | "analysis_migraine_windows"
                | "analysis_sleep" => &[ReadDiary, ReadHealth],
                "device_history"
                | "health_snapshot"
                | "health_range"
                | "metric_series"
                | "activities"
                | "activity_details"
                | "data_freshness"
                | "personal_baseline"
                | "analysis_compare_periods"
                | "analysis_running_efficiency"
                | "analysis_lagged_association"
                | "analysis_stress_by_hour" => &[ReadHealth],
                "events" | "event_definitions" | "wellbeing_observations" => &[ReadDiary],
                _ => return false,
            },
            _ => return false,
        };
        self.permits(required)
    }
}

/// Catalog and analysis parameters are supplied only after their normal input validation.
#[derive(Clone, Copy, Debug)]
pub enum ReadToolInput<'a> {
    Discovery,
    Catalog(&'a str),
    Analysis {
        operation: &'a str,
        metric: Option<&'a str>,
    },
}
