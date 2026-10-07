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

## Community starter packs

The dashboard's **Ready scenarios and pack exchange** section offers three versioned,
data-only starters:

| Pack | Owner entry | Optional source | Analysis boundary |
| --- | --- | --- | --- |
| Sleep and energy | Daily energy on a 1–5 scale | Garmin sleep score | Separate medians; no diagnosis or combined score |
| Running and effort | Perceived effort on a 1–10 scale | Garmin activity | Date proximity is not a verified activity link |
| Focus and walks | Focus on a 1–5 scale, optional walk minutes | None | Missing days are unknown; no causal conclusion |

All three use sensitive custom trackers. They do not enable reminders, Garmin
collection, messaging or model access. An owner must opt in to each destination
separately. The recipe describes bounded metric operations and limitations; it
does not contain executable SQL, Python or an unrestricted prompt. A recipe is
guidance, not an automatic analysis or a validated health model.

An owner with definition-management permission can choose a starter, edit its
JSON fields in the dashboard, or open a JSON pack file. **Show changes** lists
new definitions, already installed definitions and name conflicts, plus the
permissions that remain off. **Import pack** requires a fresh preview token;
changing the file after preview invalidates it. A conflicting tracker key must
be renamed and previewed again. Reimporting the same unchanged version is
idempotent. A higher pack version may retain unchanged tracker drafts and add
new trackers; changing a retained tracker still conflicts. Imported definitions
never replace existing ones, so historical
entries continue to resolve against their original version.

The same flow is available through `GET /community-packs`,
`POST /community-packs/preview` and `POST /community-packs/import`. The format is
`garmin-ai-community-pack-v1`; the server strictly validates the declared
trackers and analysis recipes. To share an adapted pack, save the edited JSON.
Inspect it before importing on another host. It contains definitions, labels,
scales and recipes only; it must not contain facts, chat history, identity,
location, credentials or destination bindings. The older
`/tracker-packs/export` endpoint exports a versioned definition contract for
inspection; its `garmin-ai-tracker-pack-v1` payload is a different format and
is not accepted by this importer.
