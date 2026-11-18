# Native container build

`docker buildx build --load --platform linux/arm64 -f Dockerfile.rust -t garmin-ai-rust:local .` builds the native image. The same command accepts `linux/amd64`. The image starts the HTTP service on port 8080 by default.

The runtime uses UID and GID 1000, with certificate and timezone data. It receives the existing `GA_*` settings at startup. The data, token, state and backup paths are under `/app`. The built-in probe checks HTTP liveness. `garmin-ai-rust healthcheck --ready` also requires an accessible database at the current schema.

Both image architectures passed the core-only probe on a read-only filesystem with Linux capabilities dropped and networking disabled. A separate run installed an empty TimescaleDB, upgraded a database at `f103aa712b44`, confirmed `c54f8a72d130`, repeated migration, and checked HTTP readiness. The local ARM64 Docker VM ran AMD64 through emulation.
