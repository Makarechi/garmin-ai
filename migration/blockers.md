# Open verification conditions

- The measured environment is a Docker Desktop Linux arm64 VM with component limits. R02 and
  R37 still require comparable worst-case workloads and operating-system reserve measurements
  on a representative small Linux host, including any gateway and maintenance processes.
- The R00 default test run excludes explicitly opted-in live provider checks. A separate
  synthetic extension run covers the 54 default-profile extension skips. The exact live
  Garmin account/device, Telegram and model checks remain pending until the corresponding
  implementation and authorized target are available.
- The working differential captures use synthetic PostgreSQL and archives. Versioned restored
  databases, historic backups, actual restart/fault outcomes, and signed-off release reports
  are needed before an M1 or M2 compatibility claim.
- A production cutover requires the named deployment target and explicit authorization,
  successful rehearsal, backup/restore evidence, and a rollback plan for writes after the switch.
  Continue the in-scope local implementation and verification while those release conditions
  are pending.
