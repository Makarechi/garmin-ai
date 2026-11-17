# Telegram reply destination checks

The native reply worker checks the current onboarding selection and the recorded private chat pairing immediately before sending. It keeps both settings locks through the actual provider request and receipt recording. A retired channel or changed pairing cancels the reply. The shared outbox entry point requires the Telegram-specific check before it can deliver to Telegram.

The local HTTPS and disposable database test kept both settings gates closed to a concurrent write during delivery. It verified that a deselected channel and changed chat pairing each cancelled a claimed reply without a network request. Ordinary owner replies still work while unsolicited questions are paused. The same test exercised accepted, deferred, partial and unsupported replies and ran within 32 MiB on ARM64, peaking at 12,734,464 bytes without swap.
