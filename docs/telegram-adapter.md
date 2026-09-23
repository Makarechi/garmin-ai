# Telegram adapter cutover

Telegram now has an explicit adapter boundary. It authenticates the configured owner and private
chat before producing a channel-neutral inbound envelope. Provider update, message, callback, file,
and sender identifiers remain opaque outside that adapter. Text, voice, reply relationships, edits,
and legacy callback values receive explicit neutral shapes.

During the compatibility window every accepted update is written atomically to both the retained
Telegram inbox and the neutral inbox. The existing dispatcher remains the only consumer, so the
shadow path cannot repeat a diary mutation or send a second reply. Both inbox lifecycle states are
updated together. The migration already maps older retained updates into the same neutral table.
`GA_TELEGRAM_DISPATCHER_VERSION=neutral-shadow-v1` is the default cutover version;
`legacy-v1` is a rollback switch that stops the neutral inbox write while retaining the same sole
legacy consumer. Neither mode runs two domain dispatchers.

The outbound `TelegramChannel` implements the common channel contract. It renders neutral text and
actions, applies delivery policy immediately before a send, reports a successful API call only as
`provider_accepted`, and preserves network ambiguity as `uncertain`. Unsupported attachments remain
queued with an explicit reason. Existing buttons continue through the legacy callback handler until
definition-driven actions become the only issued format; an old value is therefore either processed
by the established revision checks or rejected by that handler without bypassing them.
