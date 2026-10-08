"""Run the public source/channel kit against a separately installed fixture."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from garmin_ai.config import IntegrationInstance, Settings
from garmin_ai.extension_tck import (
    ModelProbe,
    check_channel_adapter_sync,
    check_model_adapter,
    check_source_adapter,
)
from garmin_ai.integrations import IntegrationUnavailable, default_registry, integration_statuses
from garmin_ai.source_contracts import SourceCapabilities, SourcePage, SourceRecord


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
    with pytest.raises(ValidationError, match="Effective end"):
        SourceRecord.model_validate(
            {**record.model_dump(mode="json"), "effective_end": now.isoformat()}
        )


def test_source_probe_accepts_more_than_ten_pages_and_bounds_intervals():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 1, 2, tzinfo=UTC)

    class DenseSource:
        capabilities = SourceCapabilities(cursor=True, max_page_size=2, time_semantics="interval")

        def read_page(self, *, start, end, cursor, limit):
            offset = int(cursor or "0")
            records = [
                SourceRecord(
                    source_record_id=f"row-{i}",
                    observed_at=now,
                    effective_at=now - timedelta(hours=1),
                    effective_end=now - timedelta(minutes=30),
                    source_timezone="UTC",
                    source_reference=f"synthetic:{i}",
                )
                for i in range(offset, min(offset + limit, 25))
            ]
            return SourcePage(
                instance_id="source:synthetic:one",
                page_kind="partial",
                fetched_at=now,
                records=records,
                next_cursor=str(offset + len(records)) if offset + len(records) < 25 else None,
            )

    assert check_source_adapter(DenseSource(), instance_id="source:synthetic:one") == {
        "pages": 13,
        "records": 25,
        "instance_id": "source:synthetic:one",
    }
    with pytest.raises(AssertionError, match="page budget"):
        check_source_adapter(DenseSource(), instance_id="source:synthetic:one", max_pages=10)

    class MissingEnd(DenseSource):
        def read_page(self, *, start, end, cursor, limit):
            page = super().read_page(start=start, end=end, cursor=cursor, limit=limit)
            page.records[0].effective_end = None
            return page

    with pytest.raises(AssertionError, match="effective end"):
        check_source_adapter(MissingEnd(), instance_id="source:synthetic:one")

    class Overnight(DenseSource):
        def read_page(self, *, start, end, cursor, limit):
            page = super().read_page(start=start, end=end, cursor=cursor, limit=limit)
            if cursor is None:
                page.records[0].effective_at = start - timedelta(minutes=30)
                page.records[0].effective_end = start + timedelta(minutes=30)
            return page

    assert check_source_adapter(Overnight(), instance_id="source:synthetic:one")["records"] == 25

    class Outside(Overnight):
        def read_page(self, *, start, end, cursor, limit):
            page = super().read_page(start=start, end=end, cursor=cursor, limit=limit)
            if cursor is None:
                page.records[0].effective_end = start
            return page

    with pytest.raises(AssertionError, match="outside the requested window"):
        check_source_adapter(Outside(), instance_id="source:synthetic:one")


def test_channel_probe_uses_provider_and_rejects_foreign_receipt(monkeypatch):
    settings = selected_settings()
    channel = default_registry(settings).create(settings.integrations[1], settings)
    original_policy = channel.delivery_policy
    original_deliver = channel.deliver

    def policy(intent, *, now):
        assert intent.channel_instance.channel == "alternate"
        assert intent.channel_instance.instance_id == "one"
        return original_policy(intent, now=now)

    monkeypatch.setattr(channel, "delivery_policy", policy)
    assert (
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")["state"]
        == "provider_accepted"
    )

    async def stale(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={"receipt": result.receipt.model_copy(update={"intent_id": uuid4()})}
        )

    monkeypatch.setattr(channel, "deliver", stale)
    with pytest.raises(AssertionError, match="matching observed receipt"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")

    from garmin_ai.channels import DeliveryState

    async def delivered(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={
                "state": DeliveryState.DELIVERED,
                "receipt": result.receipt.model_copy(update={"state": DeliveryState.READ}),
            }
        )

    monkeypatch.setattr(channel, "deliver", delivered)
    assert check_channel_adapter_sync(channel, instance_id="channel:alternate:one")["state"] == (
        "delivered"
    )

    async def stale_render(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={"rendered": result.rendered.model_copy(update={"intent_id": uuid4()})}
        )

    monkeypatch.setattr(channel, "deliver", stale_render)
    with pytest.raises(AssertionError, match="Rendered delivery"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")


def test_declared_status_does_not_require_local_plugin_config_or_secrets(monkeypatch):
    settings = selected_settings()
    source = settings.integrations[0].model_copy(
        update={"config": {}, "secret_refs": {"example": "GA_MISSING_TEST_SECRET"}}
    )
    monkeypatch.delenv("GA_MISSING_TEST_SECRET", raising=False)
    registry = default_registry(settings)
    assert (
        registry.status(source, settings, validate_runtime=False).verification_level == "declared"
    )
    assert registry.status(source, settings, validate_runtime=True).reason == (
        "invalid integration configuration"
    )


def test_model_probe_closes_adapter_after_failure():
    class BrokenModel:
        closed = False

        def structured(self, *_args):
            raise RuntimeError("synthetic failure")

        def close(self):
            self.closed = True

    model = BrokenModel()
    with pytest.raises(RuntimeError, match="synthetic failure"):
        check_model_adapter(model)
    assert model.closed


def test_model_probe_accepts_any_schema_valid_boolean():
    class ValidModel:
        closed = False

        def structured(self, *_args):
            return ModelProbe(urgent=True)

        def close(self):
            self.closed = True

    model = ValidModel()
    assert check_model_adapter(model) == {"structured_output": True}
    assert model.closed
