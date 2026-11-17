# Rust workspace foundation (R05)

The workspace pins Rust 1.99.1 and tracks the resolved package lock. The first executable exposes
help and version information. The existing Python baseline supplies the reference behavior for
the remaining entrypoints as their Rust implementations land.

```sh
cargo fmt --all --check
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo test --locked --workspace
cargo build --locked --release --workspace
env -i ./target/release/garmin-ai-rust --version
```

The workspace forbids unsafe Rust and denies direct panic-on-error helpers in Clippy. The
executable and its tests use the standard library. The locked package inventory records the
workspace crate under Apache-2.0. Add the dependency graph rules and vulnerability checks
with the dependencies and layer crates that need them.

CI runs the same checks and release build on Linux amd64 and arm64 runners. Local Linux
verification can use the matching Rust image and the image's preinstalled compiler with
`rustup run 1.99.1 cargo test --locked --offline --workspace`. The local amd64 container runs on
an Apple Silicon host. The CI runners provide native verification for both architectures.
[GitHub-hosted runner labels](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job)
