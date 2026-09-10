# ADR: pre-event context uses source time and versioned observations

Accepted for GA-04. Daily HealthDay rows remain latest-value projections. Their
calendar date does not establish that a score was available before an activity.
The previous configured-day sleep join is therefore intentionally removed.

`metric_observations` preserves readiness/recovery revisions and scored sleep
sessions with source calendar date, source reference, observation time, ingestion
time, fetch time and the timezone used when interpreting the source. These
per-observation timezone records retain interpretation history without claiming
to know the wearer's historical location. Device remains unknown when the source
contract does not identify it. Account fingerprint is copied only from an
established local account binding; older/unbound observations remain unknown.

`feature_at` requires both event and knowledge cutoffs. Retrospective mode may
use later-ingested corrections whose source observation preceded the event.
As-known mode forbids a knowledge cutoff after the event. Import/replay does not
backdate ingestion time to the archived fetch time. Every result, including an
unknown, exposes both cutoffs, purpose, feature version and source references.

Readiness timestamps must carry an explicit UTC offset. A naive or missing
timestamp is retained with `time_unknown` and cannot become pre-event context.
No midnight is invented. Sleep uses the explicit GMT session end, strictly before
the activity, with at most a 24-hour gap. This is an engineering matching limit,
not a medical rule. Timestamped joins do not depend on current configured zone or
the activity's zone label. Calendar-only nightly HRV remains unknown in this
context until a source contract provides a trustworthy session interval.

Migration 84a03c619f2e adds the version store without rewriting HealthDay or
inventing times for historical daily rows. Existing raw payloads can be normalized
again by the version-6 parser. New exports include observations; the two previously
supported export revisions restore with an empty observation table. No historical
backtest may treat such replayed rows as knowledge available before ingestion.

Running efficiency currently exposes retrospective pre-event context. Rankings
remain descriptive; it does not infer treatment or training decisions. Migration
does not automatically replay archives or infer a travel itinerary.
