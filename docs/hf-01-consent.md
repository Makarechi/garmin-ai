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
keyboard using current permissions. Channel consent revocation clears only
that destination's retained analytical conversation context. Sensitive tracker
forms opened in another channel remain untouched.

When two Telegram instances emit the same provider `update_id`, the legacy
dispatcher assigns the later binding a stable negative storage ID. Jobs, replies
and outbox keys use this storage ID; the provider ID remains in the payload and
the neutral ingress identity. Poll offsets and ordering state are scoped by
instance. Urgent fallback replies carry the authenticated binding and no
sensitive tracker dependency, so the consent guard does not hide emergency
guidance. Tracker submissions need schema consent; replies that include a
model clarification also retain a facts dependency and are blocked if that
consent is revoked before delivery.

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
The focused suite after review fixes passed 99 tests. The full Linux CI suite
is the merge gate. No production data, live Garmin,
Telegram or model provider was used.
