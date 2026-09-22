# Natural-language tracker input

Natural-language input is an optional adapter over the generated tracker forms. It cannot create
new field identities, choose a hidden tracker, bypass permissions or weaken validation.

The interpreter receives at most five active tracker contracts. Labels and the owner message are
marked as untrusted data. Its structured result names the immutable definition version and stable
field IDs, and every extracted value or changed time points to an exact character range in the
source message. The application verifies those ranges before passing the result through the same
form service used by the dashboard.

Setup wishes and facts are separate operations. “I want to track stretching” can only return a
tracker preview with the existing explicit confirmation token; it cannot create a stretching
entry. Definition changes are also returned as proposals and are never activated by the model.
An entry correction requires an explicitly selected event and preserves unmentioned values and
times from that immutable version.

The channel, actor and permissions come from trusted application code, not from the message or
model. Provider output outside the bounded tracker and field IDs is rejected. Quantity values need
literal unit evidence, interval endpoints need literal clock evidence, and the generated schema
performs the final bounds and required-field checks. Stored entries retain the original text and
only source offsets as evidence references.

If the configured model is unavailable, the endpoint returns the deterministic generated form (or
the tracker builder for a caller allowed to manage definitions). No fact is dropped or guessed,
and deterministic forms remain fully usable without a network model.
