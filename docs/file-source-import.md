# Local CSV/JSON source import candidate

`garmin-ai import-file` reads a **local private file** and maps its rows into an
existing active custom tracker. It is a bounded source example, not an Apple
Health, Health Connect, or Garmin export parser. It does not upload the file to
the dashboard or call a network service. Keep the source and mapping files on
private storage; the database and encrypted backup retain the imported raw
rows and their provenance.

Create a tracker first in the dashboard. A file can contain 1–500 rows and must
be UTF-8 CSV with a unique header or a JSON array of objects, at most 512 KB.
Every row needs a stable source ID and an ISO timestamp with an explicit UTC
offset. The mapping supplies an IANA timezone; the timestamp offset must agree
with it. Source and device IDs form separate identity namespaces, so two
devices with the same row ID and time remain separate. Units must match the
tracker contract exactly. Set the decimal separator and any null/sentinel
markers explicitly; no unit, timezone or negative sentinel is guessed.
For a bounded interval tracker, add `end_column` to the mapping and an end
timestamp in every row. Missing ends are rejected; the imported entry keeps
the interval rather than treating it as an instant.
Both `decimal_separator` and `null_markers` are required in every mapping;
use an empty `null_markers` list when blank text is meaningful. Import paths
must be regular files, and reads stop at their size limits even if a file grows.
Every CSV row must fill its header width; an explicit empty cell remains distinct
from a missing cell. Numeric values that would change when stored in the tracker's
number format are rejected during preview, including oversized integers and
overly precise fractions.

For a tracker `user.energy_import` with an integer `energy` field in `count`, a
fictional `observations.csv` could be:

```csv
id,when,score
sample-001,2026-10-07T09:00:00+02:00,3
```

Save a private `mapping.json` beside it:

```json
{
  "format": "csv",
  "source_instance_id": "personal_csv",
  "device_id": "manual_sheet",
  "definition_key": "user.energy_import",
  "row_id_column": "id",
  "start_column": "when",
  "timezone": "Europe/Bratislava",
  "field_columns": {"energy": "score"},
  "units": {"energy": "count"},
  "decimal_separator": ".",
  "null_markers": [""]
}
```

With the configured local database and application installed, run:

```sh
garmin-ai import-file preview /private/observations.csv /private/mapping.json
garmin-ai import-file apply /private/observations.csv /private/mapping.json --confirm PLAN_SHA256_FROM_PREVIEW
```

Preview reports the selected columns, timezone, units, numeric bounds, counts,
row numbers and short validation codes, never row values or null-marker text.
It rejects fields containing the JSONB-incompatible null character or invalid
Unicode before confirmation, including fields not mapped into the tracker.
Review the tracker version and file/plan hashes before confirming. The
apply command rereads the file and mapping and refuses a changed plan. An
invalid row rejects the entire import. After a successful import, repeating the
same file skips its rows even after a restart. Changed data, mapping or tracker
contract for an existing source row stops with a conflict so it cannot overwrite
owner corrections; correct that entry through the diary and treat source changes
separately. The adapter declares no source deletions or automatic corrections,
and a missing row never deletes history.

The importer stores the raw parsed row, file hash, source and device identity,
observation time, tracker version and linked diary entry in the private
database. It uses the shared source page contract with an opaque cursor and
bounded pages. Its scope is local, synthetic-tested text and scalar tracker
fields; complex conditional forms are rejected. No external account or
device-format compatibility is implied.
