# Native model gateway

The application can prepare Gemini, OpenAI and Anthropic requests through one gateway. It verifies the selected instance and credential generation, checks saved onboarding categories and the exact tracker versions being shared, and holds the existing sharing fence during each send. The same overall deadline covers initial checks and all candidates. Gemini voice input uses the audio grant, and the text adapters have no voice capability.

Gemini reuses one prepared request while changing the selected model. The gateway holds the compatible provider lock and resumes a fallback only for an active job claim with a matching request fingerprint. It records cooldowns and optional Gemini quota notices through existing storage. Bookkeeping errors after a successful send leave the successful result available to the caller.

The Linux ARM64 test uses a disposable database and local synthetic replies. It exercises all three providers, Gemini voice, busy locks, stale job leases, changed credentials, withheld tracker sharing, consent withdrawn between attempts, resuming only the remaining candidate, recording one quota notice, and a failed state write during fallback. The run used 16,093,184 bytes in a 32 MiB container with no swap. Runtime settings selection and the dialogue engine still need to call the gateway.
