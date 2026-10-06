# Garmin reauthentication from Telegram

When Garmin rejects stored tokens, the worker records `reauth_required` and sends
the owner one notification per UTC day. With the optional web flow configured,
the notification includes a Mini App button. The form requests only Garmin's
one-time email code. The permanent Garmin password is read from one pinned
Google Secret Manager version; it is never put in Telegram, `.env`, the diary,
the database, or the application's logs.

The form accepts only fresh Telegram Mini App `initData` signed by this bot for
the paired owner. It is available only while the Garmin connection is marked
`reauth_required`. The in-progress Garmin client is kept in API memory for at
most five minutes and allows at most three code attempts. A restart discards
the session; reopening the button starts a new login. Successful account
verification writes the new Garmin tokens and resumes the existing sync queue.
The web form can restore only an already confirmed Garmin owner; first enrollment
and owner mismatch still require local verification.
The token update holds the same database operation lock used by backups.

## Deployment

1. Set up a dedicated Google Cloud service account for the VM. Give it
   `roles/secretmanager.secretAccessor` on **only** the Garmin password secret
   and attach it to the VM with `cloud-platform` access scope. Do not create a
   service account key file. Enable the Secret Manager API in the project.
2. Add the Garmin password as a Secret Manager version using the Google Cloud
   Console or another secret-safe method. Do not place it in command arguments,
   terminal output, shell history, `.env`, or chat. Pin the complete version
   resource name in `GA_GARMIN_PASSWORD_SECRET_VERSION`.
3. Set `GA_GARMIN_EMAIL`, `GA_GARMIN_AUTH_HOST`, and
   `GA_GARMIN_AUTH_URL=https://<host>/garmin-auth` in the private `.env`.
   `GA_GARMIN_AUTH_HOST` can be a controlled domain pointing at the VM. A fixed
   VM public IP can also be used: Let's Encrypt now issues short-lived IP
   certificates, and the gateway requests that certificate profile. Reserve
   the existing VM IP before restarting the VM.
4. Publish only TCP 80 and 443 to the gateway. Keep the API and database ports
   bound to localhost. The gateway's only upstream path is `/garmin-auth`;
   every other path returns 404. Start it with the
   `garmin-web-auth` Compose profile after the application rollout.
5. Confirm a trusted HTTPS certificate and that other API paths return 404.
   Test a forged Telegram session, wrong owner, expired session, code error,
   successful code, restored token status, and a fresh Garmin sync. Confirm
   password and code are absent from logs, database, and backups of application
   state. Never test with real credentials in fixtures.

The existing terminal `garmin-ai login` remains available for recovery if
Secret Manager, the HTTPS gateway, or the Telegram form is unavailable.

References: [Telegram Mini Apps](https://core.telegram.org/bots/webapps),
[Google Secret Manager best practices](https://docs.cloud.google.com/secret-manager/docs/best-practices),
[Let's Encrypt IP certificates](https://letsencrypt.org/2026/03/11/shorter-certs-certbot).
