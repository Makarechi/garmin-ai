# Native model gate state

The native gateway can identify and read saved model cooldowns from the
Python database. It uses the same state keys, advisory-lock keys and resume
configuration digests for the primary Gemini instance, secondary Gemini
instances and model plugins. Credential changes select fresh state or
ignore a record for an earlier configuration.

A secondary Gemini instance can read the older shared record only when
it has no record of its own and the shared record has no explicit instance.
The scheduler pause and request pause stay distinct. Expired and malformed
deadlines are ignored, and arbitrary saved reason text is not returned.

Eight identities and seventeen synthetic TimescaleDB cases were checked
against the pinned Python gate and scheduler. Cases included missing
secret references, Unicode, reordered fallback lists, credential rotation,
parallel instances, malformed deadlines and unequal UTC offsets. The native
fixture peaked at 7,057,408 bytes in a 32 MiB Linux ARM64 container with
no swap. The application still needs to write outcomes and resume fallback
under the corresponding lock.
