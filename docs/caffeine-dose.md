# GA-13: explicit caffeine dose basis

Completion criteria: two servings do not multiply an already-total dose; per-serving values are multiplied once; legacy records without a basis remain unknown; dose provenance survives storage and is visible in diary history.

Caffeine payloads now declare `dose_basis` (`total`, `per_serving`, `unknown`) and `dose_provenance` (`estimated`, `reported_label`, `unknown`), with optional source notes. Existing milligram fields follow the declared basis. Servings remain independent. The shared `caffeine_total` projection resolves total milligrams for event query results and Telegram history without rewriting stored facts.

Old records default to unknown basis and are never silently interpreted as either per-serving or total. The extraction prompt requires an explicit basis and distinguishes estimates from user-reported labels. No physiological dose recommendation or age-based formula is added.

Synthetic regressions cover total/per-serving two-espresso inputs, legacy ambiguity and label provenance. No live language model or private diary data was used. Remaining GA-13 slices include approximate time intervals, symptom histories, incomplete medication observations and beverage presets.
