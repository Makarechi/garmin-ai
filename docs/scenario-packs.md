# Scenario packs

Scenario packs group trusted first-party definitions, forms, reminder rules and analysis
capabilities. They are preferences, not data containers: disabling a pack never deletes or
hides historical diary records.

The built-in catalog contains `general_diary`, `wellbeing`, `sleep`, `caffeine`, `migraine` and
`training`. Medical validation remains in trusted application code. A data-only user definition
cannot use the system namespace, replace the medication contract or gain privileged behavior by
renaming a field.

Each owner setting keeps independent controls for:

- tracking and collection;
- reminders and action visibility;
- LLM access;
- an optional outcome goal.

Changing one control does not imply another. Turning off reminders cancels queued questions for
that pack, while its records, relations and exports remain available.

## Compatibility defaults

An upgraded installation is detected from retained diary, health, source, channel or preference
state and receives all legacy packs enabled. A clean installation receives only the general diary
until the owner chooses more topics. During an interrupted rollout, a database with no explicit
pack rows uses the legacy behavior, so the old interface does not disappear before configuration
has been written.

Telegram renders its standard shortcuts from the selected packs. Old callbacks for a disabled
pack are rejected without writing a fact. Core pack definitions do not import Telegram types;
other channels can render the same capabilities differently.
