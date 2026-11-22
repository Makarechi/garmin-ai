# Native erase and resume commands

`erase-all` requires the legacy confirmation. It verifies both private file roots, publishes the local erasure markers, and commits the database fence before removing raw data and tokens. The in-progress marker keeps native recovery, Python activation, and Python file access closed until native erasure finishes. Completing the command leaves the original erased marker in place. `resume-storage` commits database activation before clearing the local markers, with the maintenance locks held throughout.

Backups and maintenance files must live outside both private data roots. The native checks compare the actual directories and reject an overlap even when the configured paths use different capitalization on the volume. This check happens before erasure changes the database or creates a local marker.

The native Linux ARM64 commands matched pinned Python on disposable TimescaleDB. `check_erasure_commands.py` killed the native process at the erasure checkpoint, ran a partial removal with an injected redirect, and then resumed erasure. Recovery and resume were refused during the partial operation. An injected activation failure kept the database fenced until a successful retry. All 30 tables, both sequences, and the independently stored backup matched the reference outcomes.
