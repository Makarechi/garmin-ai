# Layered timeline and context coverage

The shared HTTP/MCP/assistant `timeline` tool preserves overlapping annotations in
sleep, activity, wellbeing, context and plans layers. Each annotation has its own
source, status and evidence. Segment annotation IDs reference the complete set of
overlapping intervals; the old segment label remains a display projection for existing
clients. Point events are retained as points and never cover elapsed time. Results are
bounded to 500 annotations and 31 days; larger requests must narrow the interval.

Calendar sources are explicitly planned, even if their imported interval is marked
confirmed. An open migraine or illness means no recorded end, not independently
confirmed current symptoms. Physiological sample series remain in `metric_series`;
there is no invented physiological or calendar observation in an empty layer.

Context suppression uses the union of bounded activity, confirmed timeline and manual
context intervals. It requires full duration coverage. A point note or one-minute
annotation cannot suppress a 40-minute question. Overlaps are counted once, calendar
plans do not establish actual activity, and partial coverage retains explicit gaps.
Late activity still cancels a question once it covers the interval. Exact linked
replies acknowledge the question without claiming coverage beyond the recorded event.

An explicit “не помню” response can acknowledge a context question without creating
an event. The interval stays unknown and that question is not repeated. Corrections
and deletion of linked factual replies revalidate the question against retained data.

The existing conservative detector retains observation-quality/coverage gates. This
change does not add a sensor-aware or time-of-day baseline, or a physiological chart.
Those GA-18 slices depend on device history and the later baseline work. No causal
classifier is trained from annotations.
