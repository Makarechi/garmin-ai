# Storage durability and recovery

Linux containers are the normal runtime. Native Windows storage operations use
`FlushFileBuffers` on the containing volume to persist directory metadata.
This documented Windows barrier requires administrative privileges and affects
the whole volume. An unavailable volume, denied access, unsupported network
storage, or failed flush aborts the operation; no barrier is silently skipped.
Use the Linux container runtime when these native Windows requirements cannot
be met. Directory-handle flushing is not treated as a documented substitute.
See [Microsoft's FlushFileBuffers documentation](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers).

Backup creation, snapshot verification, and unpacking reserve a private
`.garmin-ai-plaintext` directory beneath their staging parent. A persistent
`operation.lock` serializes access, and `active` contains temporary plaintext.
The next operation removes an interrupted `active` tree and flushes the removal
before writing new plaintext. Normal exit also removes and flushes the tree.
Do not store user files in this reserved directory or remove its lock file.
Unrelated directories are never reclaimed. Older versions used anonymous `tmp*`
directories; inspect those manually after stopping operations because their
ownership cannot safely be inferred in an arbitrary unpack destination.

A daily backup receives at most eight attempts per retry cycle. After a failed
cycle, scheduling waits one hour before requeueing the same UTC day's job with
a fresh attempt budget and synchronization deadline. Fixing the key or volume
therefore allows recovery that day. Running, pending, and successful jobs are
preserved; disabling backups prevents scheduling these recovery cycles.
