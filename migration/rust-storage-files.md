# R32/R33 shared storage maintenance lock

The Rust guard opens the original `storage.lock` with a nonblocking exclusive file lock. An ordinary operation respects the `erased` and `activating` markers. A recovery operation can hold the same lock while those markers are present. The guard leaves the markers and lock file in place and refuses redirected directories or lock files.

The pinned Python reference at `ee3a14a48a6ad30c1085951bac2f53c9088c2450` and native Linux ARM64 were run against the same disposable directory. Each process was refused while the other held the lock, and the existing lock was available after release. The native tests also checked both markers, protected an unrelated file from a redirected lock, and passed in a 32 MiB cgroup.

Run `migration/check_storage_files.py` with the pinned Python environment and the native test executable built with `cargo test --locked -p ga-archive --test storage_files --no-run`.
