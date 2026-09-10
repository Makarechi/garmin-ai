# FIT message provenance and replay (GA-10, structured archive slice)

The FIT parser retains every data message present in the file, including device,
sport, workout, swimming, monitoring and developer definitions when recorded.
It does not infer messages that the device did not write. Existing native fields
remain available at the top level of ActivityPart payloads; `_fit.fields` preserves
all values with field numbers, units, types, raw values and developer indices.
Developer names cannot overwrite native fields. Message numbers and order remain
available, and device_info/developer_data_id messages retain their source metadata.

The active FIT hash, successfully parsed archive pointer, source status and parser
version must all match before a duplicate request skips parsing and ActivityPart
writes. The request-order watermark still advances. A→B→A and parser upgrades
reparse; parser failures retain the original file and the previous parsed parts.
CRC validation, compressed/uncompressed size limits and in-memory ZIP reads remain.

Parser version 8 adds this representation. Synthetic valid-CRC device_info data
and actual pinned fitdecode field types exercise the parser and namespace contract.
No original health files or credentials are committed.

This slice exposes structured FIT evidence through existing activity parts. Typed
activity sample channels, sport-specific derived features and adaptive detail-fetch
scheduling remain separate GA-10 work.
Default activity details expose only known FIT summary/metadata families. Monitoring,
HR, accelerometer, record and unknown families remain archived and are available with
`include_samples=true`; filtering happens before pagination.

Default details use an explicit reviewed registry of 83 nonsample families from the pinned FIT profile, including zone summaries, exercise titles, settings and sport summaries. The 37 known raw sensor/sample families (including individual jumps and segment track points) and unknown families remain opt-in. Upgrading the FIT profile requires reviewing this registry; unknown messages are still archived.
