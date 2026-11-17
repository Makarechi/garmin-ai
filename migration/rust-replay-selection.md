# Read-only replay inventory

The native inventory reads archived Garmin Connect sources under the verified
owner's binding. Each read returns at most 128 source rows in stable identifier
order. It determines whether each retained source is canonical for the target
parser version. The rules include newer failed attempts, failure markers,
previous FIT archives, and ownership of partial projections.

The paired test uses the pinned Python selector on the same disposable
TimescaleDB. The native ARM64 build matches 154 upgrade decisions across two
pages and one rollback decision. It exercises missing ownership metadata,
partially retained fields, a different account, and sources already on the
target version. The read creates no queue jobs or state records.
