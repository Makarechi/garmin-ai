# Open verification conditions

- The strict parity checker reports incomplete areas. Resolve the remaining
  compatibility decisions and run the required comparisons against the exact
  candidate revision. [Compatibility register](deviations.md)
- The pinned Python 3.15.2 run passed 4,342 of 4,369 collected cases. The 27
  exclusions include 23 explicitly live cases and four cases covered by
  separate isolated checks for filesystem aliases and Compose. Garmin login,
  MFA and device coverage, the private Telegram bot, and live model providers
  still need their authorized targets.
- The 2-CPU, 1 GiB Linux guest passed a short comparison of startup, restart and
  encrypted recovery in both runtimes. The separate capped replay completed in
  Rust with no swap or OOM and recorded the database reaching its memory limit.
  After R36, measure the whole stable release during the genuine 168-hour
  unattended window. [Resource checkpoints](evidence/native-resource-checkpoints-linux-arm64.json),
  [observer](rust-soak-observer.md)
- Physical AMD64 and ARM64 installation and historical upgrade passed, and the
  two independent-host recovery paths verified restored tables and files.
  Repeat the release checks and rehearsal with the final accepted source.
  [Installation and recovery evidence](evidence/native-install-recovery-linux.json)
- The production cutover needs the named installation and explicit authorization,
  verified backup and recovery, and a rollback plan for writes after the switch.
  [Cutover procedure](cutover-runbook.md)
