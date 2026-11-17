# Native HTTP device history and freshness

Both tools require health-read permission and appear in discovery for matching credentials. Device history uses the established FIT field allowlist and the verified health snapshot. Its output omits private device identifiers and is temporarily unavailable when the archive needs replay.

The freshness report stays available during replay and shows archive and sync progress. Observation channels become available when the archive is ready. The local date follows the owner's saved timezone after onboarding or the server setting before onboarding.

The paired HTTP suite matched 136 responses against pinned Python on disposable TimescaleDB. It covered discovery, denied scopes, date and pagination validation, device evidence and redaction, configured and saved timezones, and both tools during pending replay. Existing daily, activity, diary, and timeline cases passed in the same run.
