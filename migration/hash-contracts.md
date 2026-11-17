# Initial identity and digest contracts

This register is anchored at Python baseline `ee3a14a48a6ad30c1085951bac2f53c9088c2450`.
It records the initial archive, owner and source mappings for R04. Extend it with the rest of
the digests and UUID namespaces before those operations are ported.

| Contract | Digest input and storage identity | Python reference |
| --- | --- | --- |
| Archive JSON | UTF-8 bytes from Python JSON with sorted keys, literal Unicode, default space after each separator, and nonfinite numbers rejected. SHA-256 over those exact bytes. | `archive.LocalArchive.put_json` |
| Archive binary | Unmodified FIT or ZIP bytes. The lowercase SHA-256 digest gives the first two characters of the directory plus the complete filename stem and original type extension. | `archive.LocalArchive.put_bytes` |
| Raw Garmin revision | Source name, logical endpoint, source key and payload digest identify a stored revision. Fetch/application order is tracked separately. A→B→A keeps the original A identity but makes A current when the newest fetch succeeds. | `ingest.ingest`, `fit.store_fit`, `models.SourcePayload` |
| Garmin account binding | SHA-256 of UTF-8 `garmin:socialProfile:profileId:v1:` followed by the base-10 ID normalized without leading zeroes. Validate non-boolean, ASCII decimal, length at most 19 and unsigned positive range below 2⁶³. Compare this to the stored binding while retaining a separate internal owner UUID. | `accounts.profile_fingerprint` |
| Source plugin revision | Compact sorted UTF-8 JSON with literal Unicode from the source record's parsed JSON model. SHA-256 over the complete value. Store the raw document under the selected source instance, endpoint and external record ID. The inline archive key is `inline:` plus the digest. | `source_runtime._digest`, `source_runtime.poll_source_instance` |
| Source plugin cursor and record key | SHA-256 of the exact UTF-8 external cursor or record ID. The record's state key includes the source instance ID. | `source_runtime._cursor_digest`, `source_runtime.poll_source_instance` |
| File import confirmation | SHA-256 of input file bytes. A second SHA-256 covers compact sorted literal-Unicode JSON of the file hash, parsed mapping, target schema hash and definition version ID. | `file_source.build_plan` |
| Source metric mapping confirmation | SHA-256 over compact sorted literal-Unicode JSON of the resolved confirmation document. Preserve that document's fields and their types when porting the preview/apply path. | `source_metric_mapping._digest` |
| Versioned event and metric schema hashes | SHA-256 of compact sorted JSON with Python's default ASCII escaping and nonfinite numbers rejected. Legacy optional fields excluded by the Python hash functions remain excluded. Normalize metric `allowed_methods` ordering explicitly. | `definitions`, `metric_definitions.metric_hash` |

Preserve the archive representation on the compatible write path. FIT/ZIP remain binary.
A future raw HTTP representation would require separate versioning and a tested hash and
deduplication transition. Archived legacy JSON continues to mean the serialized Python value.

No common serializer can replace the behaviors above without matching byte test vectors.
Include deeply nested Unicode keys, composed and decomposed strings, NULL versus an omitted
value, negative zero, whole-number floats, exponent forms, wide integers and time offsets.
The initial differential corpus covers a subset. Build the remaining per-contract vectors as
the owner tasks port those inputs.
