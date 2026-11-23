# Contributing

Garmin AI is a self-hosted personal diary and analytics project. You can contribute
without a Garmin account, Telegram bot, model API key, or access to anyone's health
data. The project's own code is available under [Apache-2.0](LICENSE). Third-party
dependencies and container images retain their own licenses.

## First local check

Install Rust with rustup. The repository selects the toolchain in
`rust-toolchain.toml`. Run:

```sh
cargo fmt --all --check
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo test --locked --workspace
```

Run integration tests against a dedicated loopback TimescaleDB database ending
in `_test`. Set `GA_TEST_DATABASE_URL`, initialize it with
`GA_DATABASE_URL="$GA_TEST_DATABASE_URL" target/release/garmin-ai-rust migrate`,
and run the relevant ignored Cargo test with `-- --ignored`.
Use fictional data and test credentials.

## Packaging and browser checks

The repository's `uv` environment installs development tools for browser and
packaging checks. Install the version in `.python-version` and run:

```sh
uv sync --locked
uv run --locked pytest -q tests/test_release_bundle.py tests/test_rust_notices.py
uv run --locked playwright install chromium
cargo build --locked --release -p ga-app
GA_TEST_NATIVE_WEB=target/release/garmin-ai-rust \
GA_TEST_NATIVE_DEMO=target/release/garmin-ai-rust uv run --locked pytest -q \
  tests/test_web_chat_browser.py tests/test_dashboard_demo_browser.py
```

## Where to work

| Area | Main paths |
| --- | --- |
| Diary, definitions, forms | `crates/ga-db`, `crates/ga-domain` |
| Storage, sources and ingestion | `crates/ga-db`, `crates/ga-archive`, `crates/ga-garmin`, `crates/ga-fit` |
| Telegram, HTTP and job execution | `crates/ga-app` |
| Model transport and validation | `crates/ga-model`, `crates/ga-domain` |
| Embedded translations, prompts and web UI | `src/garmin_ai/locales`, `src/garmin_ai/prompts`, `src/garmin_ai/static` |
| Tests and architecture | `crates/*/tests`, `tests/`, `docs/architecture.md` |

Four useful kinds of contribution are: a declarative tracker pack, translation, or
example; a source adapter or importer; a channel or model adapter; and a regression
fix, usability improvement, or documentation correction. Start with a small issue
and one focused pull request. The [extension stage guide](docs/community-extension-stage.md)
describes the current runtime boundary. The [extension contract and test kit](docs/extension-sdk.md)
has runnable source, channel and model fixtures and states which paths are still
experimental. Do not assume an installed package is automatically enabled.

Open a PR against `main` with the problem, what changed, and the exact checks you ran.
Include a synthetic example when behavior changes. Maintainers review and merge PRs;
contributors do not need paid tools or permission to request a review. The project's
maintainer workflow is in `AGENTS.md`, not a requirement for external contributors.

Only synthetic or explicitly redacted data belongs in Git. Never attach Garmin tokens,
raw health exports, FIT files, original voice recordings, database dumps, backup keys,
or unredacted logs to an issue or PR. Inspect the staged diff before committing.
Report a suspected vulnerability privately using [the security instructions](SECURITY.md).
