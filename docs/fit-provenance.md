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
