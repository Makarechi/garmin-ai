# Native tracker consent management

The native HTTP service lists, updates and revokes tracker sharing grants using the existing routes and bearer scopes. The tracker management creation route requires both definitions and integrations scopes. The integrations route requires the integrations scope. Authorization happens before reading a consent or processing a request body.

The response preserves the original timezone offset on a grant and normalizes the default policy revision for older records. Restricting categories or revoking a grant uses the same consent and delivery fences as the existing storage operations. Malformed requests return a generic validation error without reflecting submitted values.

The paired Linux check exercised 28 requests against pinned Python and Rust on separate disposable databases. Successful responses, scope errors, status codes and cache headers matched. Malformed request bodies returned the same status with the native generic detail. A final comparison matched consent, conversation and delivery state after a grant was narrowed and revoked.
