# Neutral inbox, outbox, and dialogue

The neutral messaging tables are additive aliases during the Telegram cutover.
`telegram_updates` and legacy `app_state` outbox keys remain readable until the
adapter migration is complete.

## Processing guarantees

- Ingress is unique by channel, channel instance, external event ID, and revision.
  A retry returns the existing operation and outbox record.
- An edit has a new ingress revision, points to the earlier message, and keeps the
  same operation ID. Its reply has a revision-specific deduplication key.
- A domain handler and its `OutboundIntent` are written in one database
  transaction. Network delivery starts only after commit.
- Delivery workers claim only `queued` rows with a bounded lease. If a worker
  disappears after starting a network call, the row becomes `uncertain`; it is
  never automatically resent without an explicit reconciliation decision.
- Conversation pending state and the forget epoch belong to one internal
  conversation. Cross-channel memory is disabled unless the owner explicitly
  enables it later.
- Command dispatch uses semantic names and a neutral actor context. Slash-command
  parsing and provider update objects remain adapter concerns.

## Legacy state mapping

Migration keeps legacy keys so an installation can audit the cutover:

| Legacy state | Neutral state | Meaning after migration |
| --- | --- | --- |
| `pending` | `queued` | eligible for the normal claimed-delivery path |
| `sending` | `uncertain` | never blindly resent; requires reconciliation |
| `sent` | `provider_accepted` | provider accepted the request; not proof of delivery/read |
| `uncertain` | `uncertain` | ambiguity is preserved |
| `failed` | `failed` | failure is preserved |
| `cancelled` | `cancelled` | cancellation is preserved |

Retained provider message IDs remain opaque strings. If the matching legacy
Telegram update was already pruned, its outbox row is still migrated without a
false foreign-key relationship.

## Retention

Terminal neutral inbox/outbox text follows the existing explicit transport-text
retention operation. Identity, revision, operation, deduplication, and receipt
records remain, so a pruned retry cannot execute the domain operation again.
