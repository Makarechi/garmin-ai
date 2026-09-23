# HF-01: consent for the authenticated Telegram destination

Status: implemented and covered by synthetic entry point tests. This closes the
channel namespace defect B-01. UNI-11 and UNI-17 remain partial until the later
handoff tasks and release acceptance are complete.

The authenticated Telegram ingress stores its `ChannelInstanceRef` with each
queued update. Processing checks that binding against the active instance and
uses it for history, generated actions, custom forms, selected records and
model-visible custom data. History selectors and pending custom forms are bound
to the same instance. Queued replies retain only version/category dependencies;
delivery checks current consent before each network send and builds the default
keyboard using current permissions. Channel consent revocation also clears
retained analytical conversation context.

Compatibility rules: pre-existing Telegram updates, selectors and pending
forms without an instance binding belong only to `telegram:primary`. Untagged
queued replies created before this change are suppressed if a sensitive tracker
exists, because their text has no auditable consent dependencies. No database
migration is needed; the new fields are additive JSON values. Replaying a queued
update uses the stored binding and cannot change its destination through current
configuration.

Verification uses synthetic facts and a disposable TimescaleDB database. The
new entry point test fails in both consent directions on `main` at `12ceb21`
and passes after the fix. Local focused suite: 182 passed, one Windows-only
runtime test deselected because this host denies the archive volume `fsync`.
The full Linux CI suite is the merge gate. No production data, live Garmin,
Telegram or model provider was used.
