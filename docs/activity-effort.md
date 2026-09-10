# GA-22: reported activity effort

An `activity_effort` diary event stores an explicitly reported integer perceived_exertion from 0 to 10, an existing activity_id and optional notes. It is an individual subjective rating, not a validated clinical scale, inferred recovery score or measured running performance. No rating is generated from heart rate, pace or vague wording.

The report is a point in time, canonicalizing equal start/end to a point. Its timestamp cannot precede the referenced activity start. New inferred reports are rejected. API/MCP event writes retain existing scope, revision, idempotency, audit and undo behavior. Generic events reads and the wellbeing timeline layer preserve the activity relation and reported rating. Diary labels distinguish effort from device scores.

The extraction prompt requires an explicit numeric rating and unambiguous activity ID; it does not guess a session. A session-selection UI and post-workout check-in scheduling remain later work. This phase neither changes Garmin activity values nor implements a fitness recommendation or performance comparison from RPE. Synthetic tests cover links, bounds, forbidden inference, correction/undo and timeline visibility.
