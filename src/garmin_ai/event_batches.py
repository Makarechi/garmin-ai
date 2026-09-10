"""Atomic diary drafts with explicit local links, stable under Telegram retries."""

from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, StrictInt

from garmin_ai.events import EventInput, StrictModel, create_event, lock_writes


class DraftLink(StrictModel):
    child_index: StrictInt = Field(ge=0, le=9)
    parent_index: StrictInt = Field(ge=0, le=9)


def validate_links(events, links):
    parents = {}
    for item in links:
        link = DraftLink.model_validate(item.model_dump())
        if max(link.child_index, link.parent_index) >= len(events):
            raise ValueError("Draft link index outside event batch")
        child, parent = events[link.child_index], events[link.parent_index]
        if link.child_index in parents:
            raise ValueError("Draft has more than one parent link")
        if child.payload.type != "medication" or parent.payload.type != "migraine":
            raise ValueError("Draft links require a medication child and migraine parent")
        if child.payload.reason_event_id is not None or parent.status != "confirmed":
            raise ValueError("Draft link conflicts with an existing relation or unconfirmed parent")
        parents[link.child_index] = link.parent_index
    return parents


def create_batch(session, events, links, *, actor, update_id):
    events = [EventInput.model_validate(event.model_dump()) for event in events]
    parents = validate_links(events, links)
    order = [index for index in range(len(events)) if index not in parents] + list(parents)
    lock_writes(session)
    created = {}
    with session.begin_nested():
        for index in order:
            event = events[index]
            if index in parents:
                payload = event.payload.model_copy(
                    update={"reason_event_id": created[parents[index]].id}
                )
                event = EventInput.model_validate(
                    event.model_copy(update={"payload": payload}).model_dump()
                )
            created[index] = create_event(
                session,
                event,
                actor=actor,
                idempotency_key=f"telegram:{update_id}:{index}",
                operation_id=uuid5(NAMESPACE_URL, f"garmin-ai/telegram/{update_id}"),
            )
    return [created[index] for index in range(len(events))]
