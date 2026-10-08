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
        "verified_capabilities": ["text"],
        "unverified_capabilities": [],
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


def test_source_payload_has_bounded_json_shape_and_size():
    from datetime import UTC, datetime

    base = {
        "source_record_id": "one",
        "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
        "effective_at": datetime(2026, 1, 1, tzinfo=UTC),
        "source_timezone": "UTC",
        "source_reference": "synthetic:one",
    }
    assert len(SourceRecord(**base, payload={f"key_{i}": i for i in range(256)}).payload) == 256
    for payload, reason in (
        ({"text": "x" * 17_000}, "byte limit"),
        ({"nested": [[[[[[[[[1]]]]]]]]]}, "nesting limit"),
        ({"values": list(range(257))}, "collection limit"),
        ({"value": float("nan")}, "finite JSON values"),
        ({"value": {1, 2}}, "finite JSON values"),
    ):
        with pytest.raises(ValidationError, match=reason):
            SourceRecord(**base, payload=payload)


def test_source_probe_accepts_more_than_ten_pages_and_bounds_intervals():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 1, 2, tzinfo=UTC)

    class DenseSource:
        capabilities = SourceCapabilities(cursor=True, max_page_size=2, time_semantics="interval")

        def close(self):
            pass

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

    class OverlappingPages(DenseSource):
        def read_page(self, *, start, end, cursor, limit):
            page = super().read_page(start=start, end=end, cursor=cursor, limit=limit)
            if cursor == "2":
                previous = super().read_page(start=start, end=end, cursor=None, limit=limit)
                page.records = [previous.records[-1], page.records[0]]
                page.next_cursor = "3"
            return page

    assert check_source_adapter(OverlappingPages(), instance_id="source:synthetic:one") == {
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

    class MutatedPayload(DenseSource):
        def read_page(self, *, start, end, cursor, limit):
            page = super().read_page(start=start, end=end, cursor=cursor, limit=limit)
            page.records[0].payload["text"] = "x" * 17_000
            return page

    with pytest.raises(ValidationError, match="byte limit"):
        check_source_adapter(MutatedPayload(), instance_id="source:synthetic:one")

    class FinalPageSnapshot(DenseSource):
        def read_page(self, *, start, end, cursor, limit):
            page = super().read_page(start=start, end=end, cursor=cursor, limit=limit)
            if page.next_cursor is None:
                page.page_kind = "complete_interval_snapshot"
            return page

    with pytest.raises(AssertionError, match="cannot follow a pagination cursor"):
        check_source_adapter(FinalPageSnapshot(), instance_id="source:synthetic:one")

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

    class MissingClose(DenseSource):
        close = None

    with pytest.raises(AssertionError, match="close lifecycle"):
        check_source_adapter(MissingClose(), instance_id="source:synthetic:one")

    class EmptySource(DenseSource):
        def read_page(self, *, start, end, cursor, limit):
            return SourcePage(
                instance_id="source:synthetic:one",
                page_kind="partial",
                fetched_at=now,
            )

    with pytest.raises(AssertionError, match="at least one synthetic record"):
        check_source_adapter(EmptySource(), instance_id="source:synthetic:one")

    class InvalidCapabilities(DenseSource):
        capabilities = SourceCapabilities.model_construct(
            observations=True, cursor=True, max_page_size=2, time_semantics="unsupported"
        )

    with pytest.raises(ValidationError, match="time_semantics"):
        check_source_adapter(InvalidCapabilities(), instance_id="source:synthetic:one")


def test_calendar_day_probe_uses_source_local_day_overlap():
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 1, 2, tzinfo=UTC)
    zone = ZoneInfo("Pacific/Kiritimati")

    class DailySource:
        capabilities = SourceCapabilities(time_semantics="calendar_day")

        def __init__(self, local_day):
            self.local_day = local_day

        def close(self):
            pass

        def read_page(self, *, start, end, cursor, limit):
            return SourcePage(
                instance_id="source:daily:one",
                page_kind="partial",
                fetched_at=now,
                records=[
                    SourceRecord(
                        source_record_id="day-1",
                        observed_at=now,
                        effective_at=self.local_day,
                        source_timezone="Pacific/Kiritimati",
                        source_reference="synthetic:day-1",
                    )
                ],
            )

    overlapping = DailySource(datetime(2026, 1, 1, tzinfo=zone))
    assert check_source_adapter(overlapping, instance_id="source:daily:one")["records"] == 1
    outside = DailySource(datetime(2025, 12, 31, tzinfo=zone))
    with pytest.raises(AssertionError, match="outside the requested window"):
        check_source_adapter(outside, instance_id="source:daily:one")


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

    async def oversized_render(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={"rendered": result.rendered.model_copy(update={"texts": ["x" * 201]})}
        )

    monkeypatch.setattr(channel, "deliver", oversized_render)
    with pytest.raises(AssertionError, match="declared channel limit"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")

    async def dropped_render(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={"rendered": result.rendered.model_copy(update={"texts": []})}
        )

    monkeypatch.setattr(channel, "deliver", dropped_render)
    with pytest.raises(AssertionError, match="omitted the probe text"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")

    from garmin_ai.channels import (
        ActionRef,
        AttachmentRef,
        ChannelInstanceRef,
        ExternalMessageRef,
    )

    for update, reason in (
        ({"medium": "voice"}, "undeclared voice"),
        (
            {"actions": [ActionRef(action_id="one", label="One", operation_id=uuid4())]},
            "undeclared action",
        ),
        ({"attachments": [AttachmentRef(kind="image")]}, "undeclared attachment"),
        ({"mode": "edit"}, "undeclared edit"),
        (
            {
                "reply_to": ExternalMessageRef(
                    channel_instance=ChannelInstanceRef(channel="alternate", instance_id="one"),
                    external_message_id="fictional",
                )
            },
            "undeclared reply",
        ),
    ):

        async def unsupported_render(intent, *, now, changes=update):
            result = await original_deliver(intent, now=now)
            return result.model_copy(
                update={"rendered": result.rendered.model_copy(update=changes)}
            )

        monkeypatch.setattr(channel, "deliver", unsupported_render)
        with pytest.raises(AssertionError, match=reason):
            check_channel_adapter_sync(channel, instance_id="channel:alternate:one")

    async def malformed_receipt(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={"receipt": result.receipt.model_copy(update={"observed_at": "not-a-date"})}
        )

    monkeypatch.setattr(channel, "deliver", malformed_receipt)
    with pytest.raises(ValidationError, match="observed_at"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")

    from garmin_ai.channels import ChannelCapabilities, DeliveryPolicy

    channel._capabilities = ChannelCapabilities.model_construct(
        text=True, max_text_length=1_000_001
    )
    with pytest.raises(ValidationError, match="max_text_length"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")
    channel._capabilities = ChannelCapabilities(text=True, max_text_length=200)

    monkeypatch.setattr(
        channel,
        "delivery_policy",
        lambda _intent, *, now: DeliveryPolicy.model_construct(
            allow_delivery=True, retry_after="not-a-date"
        ),
    )
    with pytest.raises(ValidationError, match="retry_after"):
        check_channel_adapter_sync(channel, instance_id="channel:alternate:one")


def test_channel_probe_accepts_configured_synthetic_recipient(monkeypatch):
    from garmin_ai.channels import DeliveryPolicy

    settings = selected_settings()
    channel = default_registry(settings).create(settings.integrations[1], settings)
    expected_owner = uuid4()
    expected_conversation = uuid4()
    original_policy = channel.delivery_policy

    def bound_policy(intent, *, now):
        if intent.owner_id != expected_owner or intent.conversation_id != expected_conversation:
            return DeliveryPolicy(allow_delivery=False)
        return original_policy(intent, now=now)

    monkeypatch.setattr(channel, "delivery_policy", bound_policy)
    with pytest.raises(AssertionError, match="rejected its own text probe"):
        check_channel_adapter_sync(channel, instance_id="channel:sample:one")
    assert (
        check_channel_adapter_sync(
            channel,
            instance_id="channel:sample:one",
            owner_id=expected_owner,
            conversation_id=expected_conversation,
        )["state"]
        == "provider_accepted"
    )


def test_channel_probe_reports_optional_capabilities_as_unverified():
    from garmin_ai.channels import ChannelCapabilities, InMemoryChannel

    channel = InMemoryChannel(
        ChannelCapabilities(
            text=True,
            actions=True,
            attachments=True,
            voice=True,
            edit=True,
            reply=True,
            initiatives=True,
        )
    )
    result = check_channel_adapter_sync(channel, instance_id="channel:sample:one")
    assert result["verified_capabilities"] == ["text"]
    assert result["unverified_capabilities"] == [
        "actions",
        "attachments",
        "voice",
        "edit",
        "reply",
        "initiatives",
    ]


def test_channel_text_probe_rejects_advertised_voice_render(monkeypatch):
    from garmin_ai.channels import ChannelCapabilities, InMemoryChannel

    channel = InMemoryChannel(ChannelCapabilities(text=True, voice=True))
    original_deliver = channel.deliver

    async def voice_render(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(
            update={"rendered": result.rendered.model_copy(update={"medium": "voice"})}
        )

    monkeypatch.setattr(channel, "deliver", voice_render)
    with pytest.raises(AssertionError, match="must render as text"):
        check_channel_adapter_sync(channel, instance_id="channel:sample:one")

    async def no_render(intent, *, now):
        result = await original_deliver(intent, now=now)
        return result.model_copy(update={"rendered": None})

    monkeypatch.setattr(channel, "deliver", no_render)
    with pytest.raises(AssertionError, match="requires a rendered delivery"):
        check_channel_adapter_sync(channel, instance_id="channel:sample:one")


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
    invalid = registry.status(source, settings, validate_runtime=True)
    assert invalid.reason == "invalid integration configuration"
    assert (invalid.contract_version, invalid.implementation_version) == (1, "0.0.1")
    missing_secret = source.model_copy(update={"config": {"label": "one"}})
    secret_status = registry.status(missing_secret, settings, validate_runtime=True)
    assert secret_status.reason == "missing secret reference: example"
    assert (secret_status.contract_version, secret_status.implementation_version) == (1, "0.0.1")


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

    class InvalidModel(ValidModel):
        def structured(self, *_args):
            return ModelProbe.model_construct(urgent=[])

    invalid = InvalidModel()
    with pytest.raises(ValidationError, match="urgent"):
        check_model_adapter(invalid)
    assert invalid.closed
