# Sisyphus implementation plan

## Purpose

Sisyphus is a pytest runner that asks a sandboxed agent to repair the program under test. The user writes an ordinary Python acceptance test; the agent edits an ordinary recipe directory. Running the same test with or without an agent must mean the same thing.

This is a new project, not a refactor or copy of Alo. The user explicitly rejected a framework organized around a fixed sequence of capture/prepare/replay/verify hooks. Test code owns the experiment's sequence. Sisyphus owns sandbox execution, evidence, and the repair loop.

## Desired interface

```python
def test_boot(recipe, lab):
    board = lab.board("pocketbeagle2")
    with board.record():
        board.reset_into("dfu")
        recipe.run(["./boot"], access=[board.usb], timeout=300)
        board.serial.expect("login:", timeout=60)
        board.console.run("uname -m").expect("aarch64")
```

`lab` and the board API are user-supplied trusted pytest fixtures, not a promised built-in hardware abstraction. Implement the general recipe/evidence mechanisms first, then demonstrate a concrete lab integration. Do not disguise unimplemented board helpers as working features.

```sh
pytest test_boot.py                 # Test the saved recipe.
sisyphus solve test_boot.py         # Test, repair, repeat.
```

Minimal authored project: `sisyphus.toml` for sandbox/model/research settings and
`test_boot.py` for the ordinary acceptance test. TOML is the only configuration format.
Sisyphus validates settings and creates the initially empty `recipe/`; existing contents are
preserved. Trusted `conftest.py` remains optional for shared fixtures. Generated recipes, build
inputs, prepared binaries, and README are agent outputs, not required scaffolding.

The test docstring describes the assignment. Assertions define acceptance. Access declarations are enforced independently of prose. Explicit required deliverables belong in assertions too.

The 2026-09-07 `redux/test_bringup.py` design sketch makes a PocketBeagle 2 scenario
explicit: confirm reset into ROM DFU, build Alpine with its standard mkinitfs initramfs,
require an agent-created U-Boot image and loader, independently check fastboot, boot the
build artifact, and reach an automatic serial shell that reports Alpine in `/etc/os-release`.
Serial configuration belongs to DUT construction. Its lab/host fixtures and interfaces are proposals, not
implemented features. It adds no fixed runner phases or seeded recipe implementation.
At the user's request, `redux` contains only the example and a short `instructions.md`
asking an agent to develop its harness; the draft `sisyphus.toml` has been removed.

## Architecture and invariants

1. **Trusted test / untrusted recipe.** Pytest and user lab fixtures run on the host. Candidate Python is never imported on the host. Recipe commands run only through a sandbox backend. Production has no automatic unsandboxed fallback.
2. **One test language.** Use Python and pytest fixtures/assertions/context managers. Do not introduce a task graph, YAML phase language, or agent-defined verifier.
3. **Persistent workshop / fresh execution.** The editor keeps a workshop and caches. Each recipe invocation starts from a frozen source snapshot and a fresh root filesystem; saved artifacts and explicitly saved setup provide dependencies. Multiple dependent commands belong in one recipe script. Later optional sessions must not silently weaken this default.
4. **Same meaning with and without an agent.** The solver invokes ordinary pytest with the same plugin and configuration. Agent completion, test skips, expected failures, missing reports, and empty test collections never establish acceptance.
5. **Evidence is outside the candidate.** The host records command output and test results. The agent receives read-only evidence and a small index, not automatically concatenated logs. Candidate-authored output is labeled as such.
6. **Errors have different meanings.** Assertion failures and recipe command failures reject the candidate. Invalid configuration, container startup failure, observation infrastructure failures, test collection/setup/teardown errors, and interrupted execution stop the repair loop as infrastructure failures. Conservatively classify unexpected Python exceptions as infrastructure errors; callers can use assertions for expected behavioral failures.
7. **Snapshot identity.** Record the recipe digest, reference digests, resolved base image ID, exact command, outcome, and evidence paths. The running workshop cannot edit the tested snapshot. Final accepted outputs must refer to that snapshot, not a subsequently changed worktree.
8. **Least access.** Only candidate, declared read-only references, scratch, and explicitly granted endpoints enter the sandbox. No host home, engine socket, model credential, or publishing credential enters recipe execution. References cannot overlap the candidate or state. Validate symlinks, special files, mount paths, environment names, and configuration keys.
9. **Declared networking.** Initial implementation is offline and fails closed on unsupported grants. Later internet access requires actual egress enforcement; a private network namespace does not itself deny host/LAN access. Research and dependency downloads are explicit permissions.
10. **Reusable output.** The recipe is runnable without an agent and has an ordinary CLI. Reuse prior recipes as pinned dependencies/read-only references. There is no recursive solver invocation or workflow scheduler in the MVP.

## Implementation stages

### M0 — Persist this design and a handoff

- Write PLAN.md, TODO.md, HANDOFF.md, and AGENTS.md in `/home/calinp/p/fun/sisyphus` before implementation.
- Maintain a precise completed/pending distinction. Never describe a mock backend test as a real sandbox verification.
- Leave Alo unchanged. No Git commit, repository creation on a hosting service, or publishing is required.

### M1 — Working pytest acceptance runner (first useful release)

Files: `config.py`, `evidence.py`, `sandbox.py`, `recipe.py`, `plugin.py`, `cli.py`.

- Python >=3.11; package with `pyproject.toml`, pytest plugin entry point, and `sisyphus` console command.
- Strict `sisyphus.toml` with recipe path, operator-selected base image, optional setup argv, read-only references, and bounded command timeout. Configuration stays outside the writable recipe.
- `recipe.run(argv, timeout=..., env=...)` returns exit status and paths/read access for stdout, stderr, and generated files. Nonzero exits reject by default. Optional `check=False` allows tests to assert expected failures explicitly. Timeouts reject; engine failures raise a separate infrastructure exception.
- Rootless Podman backend only. Resolve the configured image to an immutable local ID, disable networking, prevent implicit image pulls during execution, and use unique disposable container names. Killing a timed-out host client must also forcibly remove its container. Use a fresh writable root so saved setup can install dependencies; sandbox root is not host root.
- Safe source/reference snapshots exclude no files silently. Reject symbolic links and non-regular files initially, explaining this restriction. Refine later only with containment tests. Bound copy size/count eventually for hostile or accidental huge trees.
- A recipe invocation executes optional saved setup and requested command inside the same disposable container. Setup failure cannot be suppressed by `check=False` on the requested command.
- Store versioned attempt metadata and command evidence outside candidate and references. Write manifests atomically. Store short exception summaries plus complete test failure evidence on disk.
- Record all pytest phase reports and produce pass/reject/error/incomplete outcomes. Tests using no recipe invocation cannot accidentally satisfy a solve run; explain the acceptance rule clearly.
- `pytest` works normally. `sisyphus test PATH` is a convenience subprocess wrapper that displays its evidence directory and propagates a meaningful exit status.
- Include an offline, hardware-free example and focused unit/plugin integration tests; test a real container when Podman is usable.

Acceptance: A user can run a real test against a recipe in a fresh sandbox, see an assertion failure, inspect evidence, fix the recipe manually, and rerun to pass. Invalid infrastructure cannot look like a repairable candidate failure.

### M2 — Durable run state and generic repair loop

- `sisyphus solve TEST [--attempts N]` runs pytest in a new subprocess per attempt. Persist selected tests, configuration, pinned environment, reference/source digests, phase, budgets, and last verdict. Lock one active solver per project/run; lab fixtures lock hardware separately.
- First run tests the initial recipe. On rejection, send a bounded feedback document to an editor adapter, then rerun the tests. On pass, freeze/export the exact successful snapshot. On infrastructure error, stop without spending repair turns.
- Do not accept all-skipped/xfail/empty suites or partial selection by an agent. Prevent unexpected pytest arguments/plugins/environment from changing the intended test collection. Do not load candidate `conftest.py` or Python modules.
- Persistent sandboxed editor container sees candidate RW, references/evidence RO, and scratch/cache. It never sees host tests as writable, host home, engine socket, or hardware by default. Test source can be shared read-only as task context.
- Define a small editor adapter protocol around `repair(feedback) -> changed/blocked/error`; JSON on stdin/out, separate human logs. An external command adapter runs inside the workshop, not on the host. A deterministic adapter can exercise the whole orchestration path without model credentials.
- Resume handles interruptions conservatively: an interrupted experiment is incomplete, not accepted; reconstruct test environment and re-establish hardware state by running the test again. Record cleanup failures explicitly.

Acceptance: A deterministic sandboxed editor fixes the offline example; exactly the same pytest test then passes; restart/resume preserves budgets and evidence. There is no host-executed agent shell.

M2 implementation decisions:

- Select one explicit test file, optionally a pytest node ID. Preserve assertions/fixtures and pin the collected node IDs. Disable inherited selection/configuration overrides and automatic third-party plugins in solve; required project plugins belong in trusted conftest code.
- Keep trusted tests at their original host locations so relative lab helpers retain their meaning. Hash the trusted project before/after attempts and on resume; references and editor code are separately frozen. Only the selected test source is copied into the editor's read-only context mount.
- Persist the workshop's container root filesystem, and pause all its processes before taking source snapshots or executing tests. Before the next repair, stop/start its processes to clear unfinished previous commands while retaining installed tools and files.
- Pass feedback on stdin and accept one versioned JSON status on stdout; stderr holds diagnostics. A no-change response is blocked. An editor response cannot accept a recipe.
- Use a project flock inherited by the pytest child and a parent-lifetime pipe. After abrupt parent death, the worker exits and releases its lease. Resume removes only replay containers bearing that run's labels.
- Charge interrupted trials against the original total attempt budget. Resume may explicitly increase the total but never resets spent attempts. A resumed unfinished run performs a fresh test first.
- Export the exact successful source snapshot and an acceptance record. This is not yet a clean rebuild proof or a final binary release.
- `sisyphus discard RUN` releases a retained workshop without deleting evidence. The M3 model adapter can plug into this tested loop without changing the test API.

### M3 — Model-backed editing and research

Implemented 2026-09-06: a stdlib host-side Chat Completions client, strict model configuration, four sandbox tools, bounded messages/results/steps, persistent repair notebook, saved request/response/usage records, and provider credential isolation. First-class OpenRouter configuration was added 2026-09-07 with provider URL/key defaults and attribution; live paid-provider use remains opt-in. Existing command editors remain supported. The user selected `http://10.134.6.235:8080/v1`, model `qwen3.8-27b`, without a key. The model uses host-only provider access; workshop/replay remain offline. Live greeting repair and C compilation/README repair passed; see EVALUATION.md. Jina search/Reader, restricted public HTTPS/Git imports, vendored offline inputs, contained relative symlinks, and configurable snapshot/container limits are now implemented. Model-size comparison, hard storage quotas, and large-tree optimization remain follow-ups.

- Start with one model adapter and one agent, not a multi-agent framework. Provider choice should be configurable through an adapter, without coupling the test API to the provider.
- Minimal tools: bounded file reads/search, edits, sandbox shell, and a request to finish the current repair turn. Host owns trial orchestration. Optional local checks stay in the workshop.
- Send goal/test context, latest failure, evidence index, candidate revision, budget, and a short notebook. Do not append entire historical logs or research pages. Preserve full raw evidence for on-demand reads.
- Model credentials stay in a host-side broker or narrowly scoped workshop-only adapter; never in replay, stored config, recipe outputs, or retained logs. Redact credential values in diagnostic channels.
- Add explicit HTTPS/research egress through an enforced proxy; deny private/loopback/link-local destinations including after DNS resolution and redirects. Keep provider access distinct from arbitrary candidate internet access.
- Support dependency fetching in a declared policy; pin/checksum downloaded build inputs. Package installation must be recorded in setup. Measure small-model performance on the same tasks before adding sophisticated context machinery.

Acceptance: One configured model repairs the offline example and a compiler example. Larger evidence stays on disk. An attempted read of an undeclared host file or use of replay credentials fails.

### M4 — Hardware and observation example

Written and software-tested: bounded serial capture, exclusive USB/serial leases, scoped capability sockets, and the PocketBeagle 2 DFU-to-fastboot test and its `sisyphus.toml`. The test defines only the baseline, launcher interface, fastboot response, and README requirement; the agent discovers/builds the solution and saves reusable artifacts. DFU transport exposes actual descriptors and candidate-selected transfers, with no prescribed bootloader stages. The example has not been built or run on hardware. User authorization explicitly stops before device probing/reset/transfers until USB and UART are connected and a run is authorized.

- Implement a trusted subprocess/serial recording helper with context-manager lifetime, readiness, timestamps, bounded storage, and cleanup on exceptions. No mandatory fixed capture phase: tests decide where observations begin and end.
- A `lab` fixture owns one device lease, stable physical identity, reset, and observation. Use existing serial/USB libraries where appropriate; hardware adapters live outside the coordinator.
- Pass explicit capability objects via `recipe.run(access=[...])`. Validate and translate only trusted capabilities issued by the host fixture; candidates cannot manufacture new host mounts.
- Prefer a per-device transport endpoint to mounting all USB devices. Handle USB re-enumeration across ROM/bootloader/kernel states. Record transport requests. Never interpret candidate-provided strings as host shell commands.
- State clearly that arbitrary DUT execution grants substantial DUT control; RAM-only policies require an enforceable transport policy or appropriately bounded trust.
- Implement one real board flow at a time: PocketBeagle 2 DFU -> fastboot; then Rubik Pi 3 ADB -> kexec -> Alpine; then custom distribution reuse. These require actual lab access and board-specific research, not simulated claims of hardware support.

Acceptance: An explicit reset establishes baseline, candidate runs, independent observations decide success, cleanup releases the board. Device loss before baseline is infrastructure failure; failed boot after a confirmed baseline is a candidate rejection when the test says so.

### M5 — Reproducible outputs and optional publishing

- Deliver saved setup/build/load scripts, pinned inputs, patches/configuration, README, and requested binaries. Tests assert required files and relevant behavior.
- Add an explicit clean final test with generated output/build caches removed. Do not claim a clean container alone proves that checked-in binaries were rebuilt. Define source vs generated outputs before adding reuse of generated outputs.
- Record source/reference/base image/fixture identities, exact test selection, artifact hashes, and evidence in the export manifest. Export from the accepted frozen attempt.
- Optional trusted publishing command uploads only verified declared artifacts; credentials never enter the editor/recipe, and publishing does not define acceptance. No automatic publication in initial releases.
- Distinguish rebuildability, repeatable behavior, and bit-for-bit reproducibility in documentation.

Acceptance: A user can rebuild and rerun without an agent, identify exactly what passed, and optionally publish that immutable result.

## Verification approach

- Unit tests: strict config/path handling, snapshots/digests, command construction, timeout/container cleanup, error classification, evidence atomicity and containment.
- Pytest subprocess tests: ordinary pass, assertion rejection, command rejection, setup/collection/teardown failure, skips/xfails/empty suite, no recipe call, complete evidence on failure, relative project discovery. Use an explicitly injected fake backend only in tests.
- Real rootless Podman integration: working candidate, failed command, fresh filesystem across invocations, read-only reference, no network, timeout cleanup, saved setup, output retrieval. Skip with a stated reason if unavailable; document exactly what was exercised.
- Solver tests: deterministic repair, no repair on infrastructure errors, bounded attempts, resume/locking, accepted snapshot immutability, editor cannot write trusted tests/evidence.
- Commands: `uv run pytest`, `uv run ruff check .`, `uv run python -m build` (or `uv build`). Keep normal tests offline once dependencies are installed.

## Non-goals

No compatibility layer with Alo, task graph, built-in universal board SDK, automatic hosted publication, plugin marketplace, arbitrary host execution backend, or claim that assertion-based tests prove correctness beyond their observations.

## Documentation sources checked

- Pytest plugin structure: https://docs.pytest.org/en/stable/how-to/writing_plugins.html
- Pytest hooks/API: https://docs.pytest.org/en/stable/reference/reference.html
- Podman sandbox options: https://docs.podman.io/en/latest/markdown/podman-run.1.html

These inform implementation details; the proposed design is specific to Sisyphus.

## Current delivery scope

Implement remaining research/Git/download primitives and source-build prerequisites, then the trusted serial/USB capability and PocketBeagle 2 fastboot example. The user explicitly prohibits executing this hardware example until they connect the board and USB-serial and authorize the run. Exercise mocks/pseudo-terminals and software containers only. Use vendored inputs in the recipe initially rather than adding a second dependency resolution language. Git imports record resolved commits; byte downloads and research documents record hashes. Workshop and replay remain offline.
