# Model consent during a provider request

The native model gateway now checks the saved onboarding categories under the same settings lock used by the existing update path. It keeps the shared lock and tracker permissions until the provider attempt ends. If settings are being changed, the attempt defers before sending. A later attempt rechecks the committed policy.

The regression demonstrated that another transaction could previously acquire the settings lock while the synthetic provider was receiving a request. After the fix, that transaction could acquire the lock only after the request ended. A conflicting edit blocked the next attempt, and a saved revocation prevented further requests. The existing Gemini, OpenAI and Anthropic gateway scenarios also passed against local fixtures.

The ARM64 runs used 15,007,744 bytes for the race test and 15,335,424 bytes for the existing gateway regression under a 32 MiB limit with no swap. No real model request was sent.
