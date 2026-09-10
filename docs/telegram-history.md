# GA-15: Telegram diary correction

Completion criteria for this slice: navigate beyond ten records, select an exact record without copying an ID, reject expired or changed selections, preserve retry-safe response buttons, and let a new diary topic follow an optional coffee refinement.

`/history` shows ten records per page with edit/delete buttons and a next-page cursor. Edit and migraine-close selections expire after fifteen minutes and bind the event revision. Deleted or changed records require a fresh selection. Deletion uses the existing audited soft delete and `/undo`. A return-to-history button lets the owner change the selected record.

Multiple open migraines get individual close selectors. Existing free-text clarification remains supported. Optional refinements after coffee, alcohol, or migraine logging permit a clearly different new event kind or a question; mandatory unresolved input still uses the existing guardrails.

Inline keyboards are stored with the durable Telegram reply so a retried job uses the original selectors. Only synthetic database fixtures and fake Telegram delivery are used in validation; no live bot or language model was contacted. Natural-language intent quality still depends on the provider. This slice does not implement a general form editor or edits to compound voice messages (GA-16).
