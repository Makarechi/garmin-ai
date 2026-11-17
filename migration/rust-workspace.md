# Rust workspace foundation (R05)

The workspace pins Rust 1.99.1 and tracks the resolved package lock. The executable exposes
help, version information and a loopback liveness server. The existing Python baseline supplies
the reference behavior for the remaining entrypoints as their Rust implementations land.

```sh
cargo fmt --all --check
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo test --locked --workspace
cargo build --locked --release --workspace
env -i ./target/release/garmin-ai-rust --version
```

The workspace forbids unsafe Rust and denies direct panic-on-error helpers in Clippy. The
HTTP server uses Axum and Tokio with only HTTP/1, JSON, network and signal features,
alongside Clap and serde_json. CI captures the locked package and license inventory and
checks the lock against RustSec advisories. The dependency check keeps the domain crate
on its approved JSON and hashing dependencies and prevents it from depending on other
workspace crates. [RustSec cargo audit](https://docs.rs/crate/cargo-audit/0.23.0/)

CI runs the same checks and release build on Linux amd64 and arm64 runners. Local Linux
verification can use the matching Rust image and the image's preinstalled compiler with
`rustup run 1.99.1 cargo test --locked --offline --workspace`. The local amd64 container runs on
an Apple Silicon host. The CI runners provide native verification for both architectures.
[GitHub-hosted runner labels](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job)

## Liveness

```sh
cargo run --locked -- serve --port 0
```

The server prints the bound loopback address. Send `GET /health/live` to get
`{"status":"alive"}`. The port and bind address accept the same local flags as the
Python CLI. The tests compare the Python response for the liveness path, cache control with
an authorization header, unsupported methods and an unknown path. Unix shutdown accepts
SIGTERM and Ctrl+C. Database readiness belongs to R07.
