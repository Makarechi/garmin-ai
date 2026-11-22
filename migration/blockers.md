# Open verification conditions

The [native release checkpoint](evidence/native-release-checkpoint-44617c2.json)
records the verified synthetic runs for `44617c219d262292c7487a37ee6794a9e4c8bc7e`.

- The strict parity check has nine open areas. Four decisions concern
  overlapping storage paths, which intraday observation remains current, diary
  write admission under load, and the extreme JSON nesting boundary. Record
  the decisions and rerun the required comparisons on the accepted candidate.
  [Compatibility register](deviations.md)
- The pinned Python 3.15.2 run passed 4,342 of 4,369 collected cases. The 27
  exclusions include 23 explicitly live cases and four cases covered by
  separate isolated checks for filesystem aliases and Compose. The checkpoint
  CI suite recorded 80 skips. Its installed-extension step passed the same 54
  skipped extension tests. Three filesystem cases require the separate
  case-insensitive comparison, and 23 live model-provider checks remain open.
  Garmin login, MFA and device coverage, the private Telegram bot, and live
  model providers still need their authorized targets.
  [Separate filesystem evidence](evidence/native-install-recovery-linux.json)
- The 2-CPU, 1 GiB Linux guest passed a short comparison of startup, restart and
  encrypted recovery in both runtimes. The separate capped replay completed in
  Rust with no swap or OOM and recorded the database reaching its memory limit.
  After R36, measure the whole stable release during the genuine 168-hour
  unattended window. [Resource checkpoints](evidence/native-resource-checkpoints-linux-arm64.json),
  [observer](rust-soak-observer.md)
- Five native installations on physical AMD64 and ARM64 runners and four
  independent-host recovery directions passed for the checkpoint revision.
  Each recovery verified 30 tables and three files. The native-to-native
  recovery retained the acknowledged write, resumed a queued job and accepted
  the next edit. Repeat the release checks and rehearsal with the final
  accepted source. Publication requires review of the distributable image and
  its license notices.
- The production cutover needs the named installation and explicit authorization,
  verified backup and recovery, and a rollback plan for writes after the switch.
  [Cutover procedure](cutover-runbook.md)
