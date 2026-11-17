# Existing Garmin owner verification

The native lookup checks the existing single owner and compares an authenticated
legacy Garmin fingerprint against both of the Python schema's stored forms. A
canonical `source_connections` binding or the older `account:garmin` binding can
establish identity on its own. When both exist, both must agree. Missing,
conflicting and malformed bindings fail separately. The transaction takes the
existing storage and owner fences and never creates or repairs an owner record.

The real PostgreSQL 17.11 and TimescaleDB 2.29.2 fixture covered both individual
binding forms, agreement and disagreement, malformed legacy data and concurrent
owner enrollment. It confirmed that the lookup leaves the missing counterpart
absent. CI runs this test on the disposable database after the pinned Python
schema has been applied.

This check is for the existing legacy Garmin identity namespace. A Personal API
session still needs authorized account-continuity evidence before it can link to
that owner's retained measurements.
