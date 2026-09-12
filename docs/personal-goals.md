# GA-22: explicit personal goals

The owner can choose any subset of wellbeing, sleep, running and migraine understanding, including no goals. No selection is inferred from Garmin values or model text. Unconfigured preferences are distinct from an explicit empty selection.

Telegram `/goals` displays the selection without a model; `/goals сон самочувствие` replaces it, and `/goals нет` disables all goals. This control command can run while an earlier model-backed message waits. The help/onboarding response advertises it. The analysis prompt receives the explicit selection and must not initiate sporting optimization when running is absent. A direct running question remains answerable without changing preferences.

GET `/preferences/goals` requires read:diary. PUT requires read:diary and write:diary with the current integer revision and a distinct goal list. Concurrent stale writes return conflict. Reapplying the same selection is a no-op; the last twenty prior selections are retained in private app_state. The state survives restart and follows full backup/erasure. It is not a medical objective, consent grant, notification schedule or permission to alter the diary.

Acceptance: subset/empty/unconfigured semantics, stale revisions, read-only rejection, offline Telegram persistence and context propagation. Synthetic tests verify those contracts; live model adherence is unverified. Goal-aware proactive policies, scheduled check-ins, RPE and goal editing in a web onboarding screen remain subsequent phases.
