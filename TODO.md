# Sisyphus TODO

Read PLAN.md for contracts and HANDOFF.md for the latest exact state. Check boxes only when implemented and verified; keep skipped external checks explicit.

- [x] M0: record complete implementation plan and handoff instructions.
- [x] M1: package and pytest plugin registration.
- [x] M1: strict config, safe snapshots, typed errors.
- [x] M1: immutable image resolution and fresh rootless Podman execution.
- [x] M1: saved setup, command timeout and cleanup, safe output access.
- [x] M1: structured evidence and pytest result classification.
- [x] M1: `sisyphus test` and working offline example.
- [x] M1: unit/plugin tests, lint, package build.
- [x] M1: real rootless Podman integration checks.
- [x] M2: durable run state, locks, attempt budgets, resume.
- [x] M2: sandboxed persistent editor and generic repair protocol.
- [x] M2: `sisyphus solve` with deterministic end-to-end repair test.
- [x] M3: model adapter, bounded tools/context, credential boundary.
- [x] M3: first-class OpenRouter provider configuration.
- [x] M3: Jina research, restricted public HTTPS/Git downloads, input provenance and vendored offline replay.
- [ ] M3: small/large model comparison (local Qwen greeting and C build passed; see EVALUATION.md).
- [x] M4: bounded serial observation, device leases, and scoped Unix-socket capability API.
- [x] M4: PocketBeagle acceptance test plus `sisyphus.toml`; no seeded recipe or prescribed build/transfer sequence.
- [x] TOML-only configuration; remove inline Python parsing while preserving automatic empty recipes and prepared artifacts.
- [x] Create the private GitHub repository `acalinp/sisyphus` and publish `main`.
- [ ] M4: complete and validate the authorized PocketBeagle 2 hardware run (started 2026-09-07; reset/DFU/missing-recipe rejection verified).
- [ ] M4: Rubik Pi 3 fixture and real ADB/kexec/Alpine test.
- [ ] M4: custom distribution example using pinned prior recipes.
- [ ] M5: explicit source/generated distinction and clean final build.
- [ ] M5: immutable export, provenance, README/artifact assertions.
- [ ] M5: optional trusted publisher.

## Next action

Continue the authorized PocketBeagle run identified in HANDOFF.md. The first reset and ROM DFU
baseline succeeded; the agent is building its recipe from scratch. Inspect saved state before
resuming. Keep model-size comparisons as a separate follow-up; only the local Qwen endpoint has
been evaluated so far.

## Follow-ups to include in their relevant milestones

- [x] Record the agreed PocketBeagle 2 Alpine scenario in `redux/test_bringup.py` with
  DUT-level serial configuration and `gpioctl pulse`.
  Acceptance now requires an automatic serial root shell and a successful
  `cat /etc/os-release` reporting `ID=alpine` within the 60-second readiness budget.
  Python syntax parsing and `ruff check redux` passed; no scenario was executed.
- [x] Remove the redux TOML at the user's request and add a short `instructions.md`
  asking an agent to build the harness. Redux now contains only those instructions
  and the unchanged example; directory contents and instruction text were checked.
- [ ] Review and implement the proposed redux fixture/expectation interfaces before
  attempting that scenario. The Alpine build script, U-Boot image, and loader remain
  agent deliverables; no solution has been seeded or run.
- [x] M2: coordinate snapshot creation with a paused editor; lock active solve runs.
- [x] M2: retain trusted test/config identity and intended test selection; prevent inherited overrides and changed collection from weakening acceptance.
- [x] M2: recover after SIGKILL, stop orphaned test workers, and remove run-owned replay containers on resume (verified by real process-kill tests).
- [ ] Exercise actual host reboot/power-loss recovery separately; SIGKILL checks do not simulate storage loss or machine reboot.
- [x] M3: contained relative symlinks, directory-FD traversal, and containment/swap checks. Absolute links remain rejected; build generated trees in sandbox scratch.
- [x] M3: configurable memory/process/snapshot byte/file-count limits and bounded Git transfer/checkout checks.
- [ ] M3: filesystem quotas for workshop roots and total retained-run storage (current checks are not hard disk quotas).
- [ ] M3: optimize large source snapshots only after source identity and clean-run semantics are preserved.
- [ ] M5: implement source/generated distinction before claiming that supplied binaries were rebuilt.
