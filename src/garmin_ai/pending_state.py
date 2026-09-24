"""Channel-scoped diary clarification storage."""


def pending_key(session):
    destination = session.info.get("channel_destination_instance_id")
    if destination in {None, "telegram:primary"}:
        return "conversation:pending"
    return f"conversation:pending:{destination}"
