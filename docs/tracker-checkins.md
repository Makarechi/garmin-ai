# Tracker check-ins

Check-ins are configured per active tracker. A rule records its schedule or evidence condition,
topic, consent, timezone, quiet hours, daily budget, conversation, primary channel, and any explicit
fallback. Missing data can trigger a question but never creates or infers a fact.

Every question is a durable neutral outbox intent. The rule and active tracker version are checked
when it is queued and again immediately before delivery. Disabling a tracker or rule cancels old
queued intents. Quiet hours keep the intent with a future eligible time. A provider-accepted or
uncertain attempt is never copied to another channel; fallback is possible only after a known
failure and only when configured by the owner.
