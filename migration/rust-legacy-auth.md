# Native legacy Garmin authentication

The Rust login follows the mobile JSON authentication flow from the pinned Python SDK (`5511c729b4f92713c6b6bfdd5f8d1ebb4363f4cd`). Login and verification share an isolated cookie jar. The native token exchange and authenticated profile reads use a separate client. The transport uses system certificate roots and rejects redirects. Authentication replies, errors and token files have explicit size limits. The caller must bind an MFA continuation to the authorized owner interaction.

The current token file keeps the Python SDK's `di_token`, `di_refresh_token` and `di_client_id` fields. The store uses a private directory, rejects symbolic and hard links, writes a private temporary sibling, synchronizes it, and then atomically replaces `garmin_tokens.json`. Only one Rust process may rotate the file at a time. The application must coordinate with the Python runtime and backups before enabling production writes. The store itself grants no entitlement to replace an owner's tokens.

The loopback test covered initial login, an incorrect verification code, successful MFA, reading a stable profile, saving, reopening the directory and native client, refreshing after rejection, and verifying the same owner before saving the rotated tokens. A changed profile was rejected without replacing the previous file. The tests also covered rate limiting, attempt limits and unsafe token paths using synthetic data.

This implements the direct mobile JSON and native DI path. The Python SDK's other login strategies, application wiring and remaining legacy data groups are outside this step. Real account behavior, including provider challenges, must be verified with the owner's authorization before removing the bridge.
[Pinned Python Garmin client](https://github.com/cyberjunky/python-garminconnect/blob/5511c729b4f92713c6b6bfdd5f8d1ebb4363f4cd/garminconnect/client.py)
