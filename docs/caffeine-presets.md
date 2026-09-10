# Owner-defined caffeine presets (GA-13)

Completion criteria: save only the selected owner-defined recipe, snapshot its dose
range and provenance, reject callbacks for changed recipes, and never duplicate an
accepted click. Editing configuration must not change previously recorded events.

`GA_CAFFEINE_PRESETS` is an empty JSON list by default. Up to 12 entries can specify
an opaque UUID `id`, a display `name`, and a `recipe` using the existing caffeine
payload contract: beverage, servings, dose basis, provenance, estimate and/or bounds.
There are no built-in doses or automatic medication presets. The worker already reads
the host `.env` through Compose; restart it after changing recipes. Setup preserves
and validates this JSON setting.

When configured, the coffee button displays these recipes and their total caffeine
estimate/range, plus a choice to record coffee with unknown dose. Selecting a recipe
records its full payload at the callback receipt time and offers the existing optional
refinement. A content fingerprint invalidates old buttons after a recipe/name changes;
repeating the same Telegram update reuses event idempotency. Recorded payloads are
independent snapshots. Missing click time asks for another selection instead of guessing.

This phase supplies configuration and Telegram selection. A graphical recipe editor,
usage-based ordering, beverage-specific volume fields, and medication presets remain
separate work. Synthetic tests only; no model or Telegram network calls are needed.
