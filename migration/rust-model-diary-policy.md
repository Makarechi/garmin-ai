# Native model-facing diary pages

The diary read checks onboarding, scenario pack sharing and each entry's
recorded definition before it applies the page limit. Sensitive records
must be permitted for the exact model and any selected channel. The same
stored-consent predicate is used for individual checks, page filtering and
delivery. Model results exclude original text, and channel replies retain
only the version references needed for a separate consent recheck.

Eleven native ARM64 pages matched the pinned behavior on disposable
TimescaleDB. The fixture also checks two deliberate privacy corrections.
A future-dated consent cannot consume the page budget, so the 1,200 denied
rows in the fixture leave the permitted page intact. Explicit requests for
unknown custom kinds return the same empty page as denied custom kinds.
Disabling the diary category stops model reads while owner reads continue.
The existing owner pagination and delivery lock fixtures still passed.
