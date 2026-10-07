"""Run the public source/channel kit against a separately installed fixture."""

import pytest
from pydantic import ValidationError

from garmin_ai.config import IntegrationInstance, Settings
from garmin_ai.extension_tck import check_channel_adapter_sync, check_source_adapter
from garmin_ai.integrations import IntegrationUnavailable, default_registry, integration_statuses
from garmin_ai.source_contracts import SourcePage, SourceRecord


@pytest.fixture(autouse=True)
def installed_fixture():
    pytest.importorskip("synthetic_adapters")


def selected_settings():
    return Settings(
        integrations=[
            IntegrationInstance(
                id="source:sample:one", kind="source", provider="sample", config={"label": "one"}
            ),
            IntegrationInstance(
                id="channel:sample:one", kind="channel", provider="sample", config={"label": "one"}
            ),
        ]
    )


def test_installed_source_and_channel_pass_contract_kit():
    settings = selected_settings()
    registry = default_registry(settings)
    statuses = integration_statuses(settings, registry)
    assert {(status.kind, status.provider, status.available) for status in statuses} == {
        ("source", "sample", True),
        ("channel", "sample", True),
    }
    assert all(
        status.contract_version == 1
        and status.implementation_version == "0.0.1"
        and status.verification_level == "local_configuration"
        for status in statuses
    )
    assert all(
        status.verification_level == "declared"
        for status in integration_statuses(settings, registry, validate_runtime=False)
    )
    source = registry.create(settings.integrations[0], settings)
    channel = registry.create(settings.integrations[1], settings)
    assert check_source_adapter(source, instance_id="source:sample:one") == {
        "pages": 2,
        "records": 3,
        "instance_id": "source:sample:one",
    }
    assert check_channel_adapter_sync(channel, instance_id="channel:sample:one") == {
        "instance_id": "channel:sample:one",
        "state": "provider_accepted",
    }
    source.close()
    assert source.closed


def test_installation_does_not_enable_plugin_and_instances_stay_separate():
    settings = selected_settings()
    assert integration_statuses(Settings(integrations=[])) == []
    disabled = settings.model_copy(
        update={"integrations": [settings.integrations[0].model_copy(update={"enabled": False})]}
    )
    assert integration_statuses(disabled)[0].reason == "integration is disabled"
    with pytest.raises(IntegrationUnavailable, match="disabled"):
        default_registry(disabled).create(disabled.integrations[0], disabled)

    second = settings.integrations[0].model_copy(
        update={"id": "source:sample:two", "config": {"label": "two"}}
    )
    together = settings.model_copy(update={"integrations": [settings.integrations[0], second]})
    registry = default_registry(together)
    one = registry.create(together.integrations[0], together)
    two = registry.create(second, together)
    assert one.instance_id != two.instance_id and one.label != two.label
    assert check_source_adapter(two, instance_id="source:sample:two")["records"] == 3
    one.close()
    assert one.closed and not two.closed


def test_source_contract_rejects_duplicate_records_and_payload_on_deletion():
    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    record = SourceRecord(
        source_record_id="one",
        observed_at=now,
        effective_at=now,
        source_timezone="UTC",
        source_reference="sample:one",
        payload={"value": 1},
    )
    with pytest.raises(ValidationError, match="repeat a record identity"):
        SourcePage(
            instance_id="source:sample:one",
            page_kind="partial",
            fetched_at=now,
            records=[record, record],
        )
    with pytest.raises(ValidationError, match="cannot carry"):
        SourceRecord.model_validate(
            {**record.model_dump(mode="json"), "operation": "delete", "payload": {"value": 1}}
        )
