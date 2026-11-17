# Native reply delivery fence

Before exposing a claimed reply, Rust checks its owner and channel, lease, message revision, memory epochs, expiry, and tracker sharing requirements. The Python storage, write, and channel-consent locks stay held from verification through the provider attempt. Before the attempt, Rust rotates the claim token in a committed transaction. Reusing the original claim cannot send the reply again.

The provider observation remains separate from the delivery state. A missing observation becomes uncertain. A failure to persist the observation leaves the rotated claim eligible for normal uncertain-send recovery. The expired claim can never be used again.

The synthetic Linux ARM64 database test verifies provider acceptance, content withheld after an edit or consent revocation, the shared locks during delivery, an expired message, duplicate attempts, and recovery after an injected receipt write failure. The run used 6,807,552 bytes in a 32 MiB container without swap. Channel transports call this interface once their own authentication and delivery rules are available.
