# Fallback conversation binding

After a known `FAILED` attempt, a tracker initiative may move to the next configured route only when exactly one target conversation exists for the same owner and channel instance. The new intent binds that conversation, retains the operation and logical notification identity, and rechecks target sharing consent through the existing queue service. Automatic fallback is limited to plain text initiatives; provider message references, forms, actions, attachments, and voice requests stay on the original route.

Pre-send revalidation cancels older queued fallback intents whose conversation still points to the primary route. `UNCERTAIN`, provider-accepted, and delivered attempts do not trigger rerouting.

The delivery adapter still makes the final capability and provider-policy decision. A configured route with no running adapter remains queued under the existing retry policy; this slice does not add another live channel adapter.
