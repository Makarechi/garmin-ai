# GA-27: verify encrypted snapshots before publication

Every `encrypt_file` call, including scheduled and manual full backups, now rereads
its private staged ciphertext before publishing the requested destination. Streaming
AES-GCM decryption authenticates the complete stored bytes; a SHA-256 digest and byte
count must match the plaintext stream actually read during encryption. A corrupt,
truncated or valid-but-different staged ciphertext fails before exclusive publication.
The temporary ciphertext is removed on failure; a concurrently created destination is
never removed or overwritten. Existing backup format and recovery keys stay compatible.

Verification decrypts one bounded chunk at a time and discards it in memory. It writes
no additional plaintext file, emits no content or key, and does not store its internal
digest in logs. The cost is one additional full ciphertext read and decryption per
backup. This detects read-back corruption at creation time; it cannot guarantee media
will remain intact later or validate the semantic completeness of a database export.

Existing restore tests remain the check of counts/content and recovery behavior.
Off-host storage, periodic isolated restore drills, disk-space staging budgets, and
long-term media revalidation are separate GA-27 work. Tests inject corruption into
synthetic staged files and verify publication/cleanup boundaries without private data.
