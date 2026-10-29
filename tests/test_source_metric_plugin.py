"""Exercise confirmed mappings through the separately installed source extension."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_source_metric_mapping import install_metric
from test_source_metric_projection import confirm_mapping

from garmin_ai.config import IntegrationInstance, Settings
from garmin_ai.metric_definitions import aggregate_metric
from garmin_ai.models import MetricObservation
from garmin_ai.resource_catalog import list_resources
from garmin_ai.source_runtime import poll_source_instance


def test_installed_source_projects_paginated_records(db, db_engine):
    pytest.importorskip("synthetic_adapters")
    configured = Settings(
        _env_file=None,
        integrations=[
            IntegrationInstance(
                id="source:sample:one", kind="source", provider="sample", config={"label": "one"}
            )
        ],
    )
    metric = install_metric(db)
    confirm_mapping(db, configured, "source:sample:one", metric.id)
    db.commit()
    now = datetime(2026, 1, 2, tzinfo=UTC)
    assert (
        poll_source_instance(db_engine, configured, "source:sample:one", now=now, max_pages=1)[
            "status"
        ]
        == "partial"
    )
    assert poll_source_instance(db_engine, configured, "source:sample:one", now=now)["status"] == (
        "complete"
    )
    assert poll_source_instance(db_engine, configured, "source:sample:one", now=now)["status"] == (
        "complete"
    )
    db.expire_all()
    rows = db.scalars(select(MetricObservation)).all()
    assert sorted(row.value for row in rows) == [10, 15, 25]
    assert (
        aggregate_metric(db, "user.walk.minutes", now - timedelta(days=2), now)["observations"] == 3
    )
    resources = list_resources(
        db,
        kind="tracker_metrics",
        after_key=None,
        resource_key="user.walk.minutes",
        limit=10,
        detail=True,
    )
    assert resources["resources"][0]["availability"]["canonical_observations"] == 3
