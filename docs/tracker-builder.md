# Tracker builder and generated forms

The tracker builder turns a bounded setup draft into the same immutable event-definition contract
used by the API. It does not generate Python, SQL, prompts or executable extensions.

The setup flow has two explicit steps:

1. `POST /tracker-setups/preview` validates the title, stable keys, fields, units, bounds, topology,
   shortcut and reminder preference. It returns the exact definition and form plus a confirmation
   token. The server keeps only a hash of that preview for 15 minutes; an invented, expired or
   already-used token is rejected.
2. `POST /tracker-setups` accepts that unchanged draft and token, activates definition version 1
   and stores its personal shortcut and reminder preference. A changed draft must be previewed
   again.

`GET /actions` lists actions derived from active definitions. Labels are display text only; action
and field identity use immutable UUIDs and stable field IDs. `GET /forms/{action_id}` returns a
channel-neutral `FormSpec`. The dashboard is one renderer; future channel adapters can render the
same contract without importing browser or Telegram types into the domain service.

Every submission includes its action identity and schema hash. A create form stops with a conflict
after a definition gains a new active version, so an old UI cannot silently write data under a new
contract. Edit actions also contain the event revision and continue to validate against the
immutable version already bound to that event. Optional blank fields are omitted rather than
invented, and validation responses contain field IDs and safe messages, never the rejected value.

Reminder settings are stored with the tracker but do not themselves prove diary completeness or
create missing facts. Generic scheduling consumes these preferences in the initiative-routing
stage.
