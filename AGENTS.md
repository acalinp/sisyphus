# Sisyphus contributor instructions

- Read PLAN.md, TODO.md, and HANDOFF.md first. Maintain them as work progresses.
- Sisyphus is a pytest runner with a repair loop. The ordinary test controls experiment sequencing and success. Do not reintroduce a fixed capture/prepare/replay/verify phase configuration.
- Test and lab code are trusted host code; the recipe and model-generated code run only in a sandbox. Never import candidate code on the host. Never fall back to unsandboxed execution in production.
- Agent state may persist; saved recipes must execute in fresh sandboxes without workshop-only state or credentials.
- M2 is implemented: preserve project locking, paused-editor snapshots, stored settings/pinned inputs, exact test collection, spent budgets, and the pytest worker's parent-lifetime pipe. Resume must test before asking for another repair.
- M3 model tools run only via Podman. Keep the model HTTP client on the host, never pass provider credentials into containers, and preserve no-redirect/no-proxy behavior. Command editors remain supported.
- Keep evidence outside the candidate, and make logs available on demand. Do not fill model context with complete logs by default.
- Distinguish candidate rejection, infrastructure failure, and incomplete execution. Skips, xfails, empty suites, and agent messages are not acceptance.
- On 2026-09-07 the user connected USB/UART and explicitly authorized running the PocketBeagle example. Its configured USB port, serial path, and reset command may be used for that run; preserve the user wiring and inspect saved state before resuming.
- Research/Git downloads are brokered; vendor build inputs for offline replay. Do not grant the workshop general network or device access.
- Default to offline execution. Do not describe a network namespace as an internet-only security policy.
- Prefer a small Python package and pytest conventions over custom workflow abstractions. Hardware helpers are ordinary trusted fixtures.
- Run `uv run pytest`, `uv run ruff check .`, and `uv build` for changes to implementation. Real container/hardware checks must be reported separately from injected fake-backend tests.
- Keep the sibling Alo project unchanged. Do not create a remote, publish, or commit unless requested.
- Before handing off, update TODO.md and HANDOFF.md with exact progress, verification, remaining work, and any environment blockers.

- Keep the PocketBeagle example to a test plus `sisyphus.toml`. TOML is the only configuration format; do not add inline settings support. Its assertions describe outcomes; the agent owns source/toolchain choices, artifact names, build flags, and transfer ordering. Prepared artifacts belong in the saved recipe for reuse after resets. Do not add a seeded solution or boot-stage policy to the transport helper.
