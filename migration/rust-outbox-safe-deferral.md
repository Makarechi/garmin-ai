# R26 Outbox safe deferral

An explicit provider rejection of the whole request can release the rotated neutral delivery lease and save an eligible retry time. The row remains queued until that time, and the original lease cannot send again. A missing observation or an unsuccessful storage write uses the existing uncertain-send recovery.

The Linux arm64 test verifies the saved retry deadline, exact-time eligibility, lease replacement and recovery after a failed receipt write. It used 7,786,496 bytes peak under a 32 MiB cap, with zero swap.
