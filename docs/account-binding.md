# Single-owner Garmin binding (GA-26)

Every synchronization entry checks the authenticated account fingerprint before
fetching or archiving health data and rechecks inside each write transaction. A
different account raises `AccountMismatch`; diary reads/writes remain available.
Restoring/recreating a reader reloads its authenticated identity. No account is
inferred from email, display name, environment username or health values.

The supported upstream identity contract is the numeric `profileId` from the
authenticated `/userprofile-service/socialProfile` response. Missing/malformed IDs
fail closed. The locked Python client already uses that endpoint during login but
does not retain its ID; the reader retrieves it explicitly. The field also appears
in the [Go client's SocialProfile model](https://pkg.go.dev/github.com/abrander/garmin-connect#SocialProfile).
This is an upstream integration contract, not a live-account verification in this PR.

Only a versioned SHA-256 fingerprint and generated instance UUID are persisted in
`app_state`. The fingerprint is a pseudonymous identifier, not anonymization or an
authentication credential. The database/export is still private. Binding creation
is serialized with diary/ingestion writes; concurrent different owners cannot both
enroll. Existing bindings never rebind, including when confirmation is supplied.

Empty stores enroll on their first guarded sync. Stores containing diary, audit,
raw provenance or conversation state require an explicit local acknowledgment:

```sh
uv run garmin-ai enroll-account --confirm-existing-owner
```

Verify that the current token cache belongs to the original owner before using it.
If those tokens must be renewed first, `login --confirm-existing-owner` performs
the same explicit legacy enrollment with the newly authenticated candidate.
For another person, create a separate instance. These commands do not provide
multi-user tenancy or transfer existing records between accounts.

Login authenticates candidate credentials without the upstream `GARMINTOKENS`
fallback and checks any active database binding before publishing candidate tokens.
A mismatch leaves the configured token file unchanged. Before database migration,
or after erasure, login can save credentials without enrolling/resuming ingestion;
the first actual sync still must pass the regular database guard. The setup check
is only used under the existing standalone storage/runtime lock. Partial schemas
fail closed. The upstream locked client atomically replaces its token file.

Probe reports carry the account fingerprint. Configured active databases are checked
before collecting a probe. Import rejects missing or mismatched provenance before
reading archived payloads. For an old report only, `import-probe --confirm-legacy-owner`
explicitly attests it belongs to the currently authenticated owner; it cannot override
a conflicting fingerprint already present in the report. Pre-database probe collection
does not authorize import into a populated database.

Bindings participate in ordinary encrypted backup/export and restore; no schema
migration is required. Erase removes the binding with other database content and
keeps the existing ingestion fence. Synthetic tests cover mismatched sync paths,
reauth/token preservation, enrollment races, legacy enrollment, probe provenance,
empty/erased setup and export/restore. Live Garmin authentication was not performed.

First enrollment now shares a coordination lock with ordinary owner-data writes.
Token publication and its file/directory durability barriers occur before the
enrollment transaction commits; a publication failure rolls back a new binding.
Backup status alone is operational metadata. Deterministic ownership errors keep
the reader's cached fingerprint rather than repeating remote identity requests.

An unbound database with retained raw files requires explicit local ownership
confirmation. A file-only probe may reuse retained storage only when the saved
coverage report carries the same authenticated fingerprint; missing provenance
requires local enrollment, and a different owner is rejected before fetching.
These checks include probe/import and every canonical ingestion transaction.
