# Rust Garmin Personal API client

The client uses Garmin's documented Personal API HTTPS origin and API-key exchange. It shares token acquisition, pacing and a server-requested cooldown between clones of the same client. It retries a rejected cached token once, classifies a rejected new token or insufficient access separately, and does not follow redirects. Error messages carry no upstream body, URL, token or key.

JSON is validated and returned in its original bytes, including unknown fields. An empty response, a JSON null and a capability unavailable through the Personal API have distinct outcomes. Original activity downloads stream into caller-owned staging under a byte limit. Publication belongs to the caller and can only follow a successful full response.

The [published Garmin Personal API](https://developer.garmin.com/gc-developer-program/personal/swagger-docs/) defines the supported paths and parameters. The [authentication instructions](https://developer.garmin.com/gc-developer-program/personal/personal-api/) document exchanging an API key for a JWT. The client does not infer historical account identity from Personal API profile settings.

The loopback tests inject synthetic keys and JWTs, refresh, HTTP failures, transport timeouts, redirects, shapes of JSON and binary responses, and truncation. The Postman simulation checks the default exchange and routes separately against a local stand-in. These probes require no Garmin credentials. Live coverage, account continuity and ingestion are separate milestones.

To rerun the standalone simulation, start it in one terminal and set `port` to its printed port in the Rust test command in another.

```sh
postman simulation run postman/simulations/garmin-personal.sim.yaml --port auto --no-history
GA_TEST_GARMIN_SIM_URL="http://127.0.0.1:${port}/garmin-personal/" \
  cargo test --locked -p ga-garmin local_postman_simulation -- --ignored
```
