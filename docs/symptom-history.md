# GA-13: symptom observations

Completion criteria: a later pain rating is a separate dated observation, original severity remains available, retries do not duplicate the observation, and references cannot silently point to removed episodes.

`symptom_observation` records a point in time linked to a confirmed migraine episode, with optional severity, reported aura, symptoms and impact. At least one observation is required. It uses the existing diary storage, audit trail, revision checks and Telegram idempotency. Removing or changing the parent episode requires removing linked observations first, just as for linked medication.

The extraction prompt distinguishes a newly reported symptom change from correction of an earlier recording error. History displays the time and severity. Positive symptom points veto a full-day negative migraine control; a zero/unspecified rating never proves the entire day symptom-free.

Synthetic tests cover a 7/10 onset followed by 3/10, replay, absent parent and deletion protection. No live model was invoked; extraction quality depends on the provider. Approximate onset/end ranges and medication outcome records remain later GA-13 slices. These records are subjective reports, not diagnoses.
