# Installation owner and external bindings (UNI-02)

Every migrated installation has exactly one internal owner, created without Garmin,
Telegram or model credentials. The owner has a random UUID plus explicit locale,
timezone and unit preferences. A database constraint rejects a second owner.

External systems do not define that identity:

- Garmin is a source connection. Its existing versioned account fingerprint is
  preserved and still blocks data from a different authenticated account before
  ingestion.
- Telegram is a channel binding. The existing locally paired numeric ID is stored
  as an opaque string under the Telegram namespace when the service starts.
- New channel bindings require an explicit confirmation flag. Matching names,
  usernames, email addresses or phone numbers never links identities.

Personal-goal preferences retain their compatible application-state key but now
carry the internal owner ID. The migration backfills existing Garmin bindings and
goal ownership. Existing Telegram configuration is materialized on the first
service start because secrets and environment configuration are intentionally not
read by database migrations.

Owner, source and channel records are included in export, encrypted backup, restore
and erasure. Restoring an older compatible export creates a new internal owner for
that restored installation and converts its legacy Garmin binding.
