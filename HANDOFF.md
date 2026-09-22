# Sisyphus handoff — 2026-09-06

## Redux bringup design — 2026-09-07

The user requested the agreed scenario in `redux`, with the original serial configuration
on DUT construction. Redux now contains only `test_bringup.py` and a short
`instructions.md` asking an agent to develop the pytest harness and repair loop.
The user explicitly requested removal of the draft `sisyphus.toml`; it is deleted.
The test specifies reset with `gpioctl pulse`, independent reset/ROM DFU confirmation, an agent-owned
`build-alpine.sh` using Alpine's standard BusyBox-based mkinitfs initramfs, prepared
`u-boot.img` and `load-uboot.sh`, host `fastboot devices -l` matching the selected DUT,
host `fastboot boot` of the build artifact, and an automatic serial root shell.
The user requested dropping directly into a shell. The build now starts that shell without
a login/password prompt after Alpine startup. The proposed `serial.run` waits for a fresh
shell and executes `cat /etc/os-release`, requiring success and an `ID=alpine` output line.
It must separate command output from terminal echo and preserve command exit status;
shell readiness and command execution share a 60-second timeout. Alpine documents serial
autologin in https://wiki.alpinelinux.org/wiki/TTY_Autologin. This is a functional shell
and reported distro identity check, not attestation of all firmware contents.
The 60-second budget starts after the boot command returns; that command has a separate
30-second timeout. USB port and serial by-id path match the existing example.

This is explicitly a design sketch. Proposed lab/host fixtures, expectation methods, and
artifact handoff are not implemented. The existing fastboot example, saved runs, and package
implementation are unchanged. No build/loader solution was seeded. The instructions
preserve the scenario's assertions, trusted host lab operations, fresh offline candidate
sandboxes, scoped device grants, and evidence outside the recipe.

Verification: Python `ast.parse` and TOML `tomllib.load` passed without importing or
executing the scenario; `uv run --offline --no-sync ruff check redux` passed. No pytest,
container, model, build, or hardware execution was performed for this design-only change.
Remaining work is to review/implement the proposed interfaces before attempting the new
scenario; there is no environment blocker to writing the example.
For the subsequent TOML removal/instructions-only change, checked that `redux` contains
exactly the test and instructions and reviewed the short instruction text. The test was
unchanged; no execution checks were repeated and no hardware was accessed.
For the subsequent automatic-shell revision, Python syntax parsing and scoped redux lint
passed again. The shell API is a design proposal; no model, container, or hardware ran.

## OpenRouter support — 2026-09-07

`[editor] provider = "openrouter"` now selects `https://openrouter.ai/api/v1`, reads the key
from host-only `OPENROUTER_API_KEY`, and sends the `X-OpenRouter-Title: Sisyphus` attribution
header. Both `base_url` and `api_key_env` remain overridable. The generic
`openai-compatible` provider remains the backward-compatible default. This was validated with a
scripted HTTPS transport; no live OpenRouter request or paid model was used.

The first real OpenRouter attempt (`1428f0d13262487694a9e58a3e32c61e`) exposed that this
standalone Python installation has no usable default CA file. The model HTTPS client now reuses
the verified OS CA-bundle loader already used by research downloads. The failed run rejected the
empty recipe, then stopped before receiving a model response; resume it rather than starting over.
An unauthenticated `GET /api/v1/models` through the corrected Python TLS context returned HTTP 200.
After the fix, targeted model/config/solver tests passed (**80 passed**), the software/container
suite passed around the retained-workshop assertion (**191 passed, 1 deselected**), source/tests
lint passed, and `uv build --offline` passed. No paid completion was issued during diagnosis.

Verification:

- `uv run --offline pytest -q tests/test_model.py tests/test_config.py tests/test_solver.py`:
  **80 passed**.
- Full software/container suite excluding the environment-sensitive container-absence assertion:
  **191 passed, 1 deselected**. The omitted `test_real_timeout_removes_container` passed its own
  timeout cleanup but then found five pre-existing retained `sisyphus-workshop-*` containers from
  PocketBeagle runs, including authorized run `843c50e0bd7f4ac88b54fdd4924bd959`; they were not
  removed or altered.
- `uv run --offline ruff check src tests` passed. `ruff check .` remains blocked by pre-existing
  E501 lines in `examples/pocketbeagle2-fastboot/test_fastboot.py` and its saved run snapshots.
- `uv build --offline` passed.

## Current hardware run — authorized 2026-09-07

The user connected the board and explicitly said "ok ready to run the example". The earlier
hardware stop boundary is lifted for this experiment. Use physical USB port `3-2.3`, the
configured Raspberry Pi Debug Probe serial path (currently /dev/ttyACM1), and the user's
`/home/calinp/p/fun/xiao-gpio/dist/gpioctl pulse` reset command. Do not replace their wiring.

Active run: `examples/pocketbeagle2-fastboot/.sisyphus/runs/843c50e0bd7f4ac88b54fdd4924bd959`.
Started from an empty recipe with the configured Qwen endpoint and default 10-attempt budget.
First trial: reset returned `OK GPIO D1=PULSED DURATION=100MS LEVEL=HIGH`; ROM DFU was confirmed;
the sandbox rejected missing `run`, and repair 1 started. This is not fastboot acceptance.
The Jina key is injected from the private /tmp file into the trusted host environment only.
Inspect run.json and retained evidence for the latest phase before resuming or starting anything.

Latest user decision: use **only `sisyphus.toml` for configuration** and remove inline Python
settings support. The PocketBeagle example now has exactly two authored files: `test_fastboot.py`
for wiring/acceptance and `sisyphus.toml` for the unchanged image, model endpoint, research grants,
and limits. The AST reader, test-file configuration discovery, and inline help are removed.
TOML validation now creates the empty recipe directory only after all settings/path checks pass;
existing scripts and prepared artifacts are preserved. No seeded solution has been added.

The hardware experiment is now running with the user-configured ports and reset command.
The agent started with an empty recipe and must create the solution itself.
The selected existing node:24-bookworm image has basic Python/Git/native build tools; the agent
must obtain its own cross-toolchain/dependencies from granted hosts, prepare artifacts in the
workshop, and save its launcher/artifacts/build recipe/README under recipe/. Fresh offline replay
can reuse saved binaries. With no reset_command, each trial needs a manual ROM DFU reset.

An enclosing TOML project retains authority over candidate/reference/state directories before
pytest imports any candidate code. Explicit Python configurations are no longer parsed as settings.
Resume still restores saved settings and pins the test and configuration identities. Prepared
artifacts participate in source snapshots and export. Only `run` and README are prescribed outputs.

## Project and current state

Destination: `/home/calinp/p/fun/sisyphus`; latest staging `/tmp/sisyphus-toml` is disposable after sync.
Alo is unchanged. The project is published as the public GitHub repository
`git@github.com:acalinp/sisyphus.git`; no host dependency installation was performed.

M0–M2 and the model editor remain operational. New work includes:

- Opt-in `[research]`: Jina search/Reader, byte downloads, and native Git imports. `download_hosts`
  is an exact hostname allowlist for Git/bytes. Research results are immutable read-only files
  under the run's research/ directory, mounted at /research only in the workshop. The agent uses
  existing file/shell tools to inspect them and vendors required inputs into its recipe.
- Search/Reader use fixed Jina HTTPS endpoints. Arbitrary URLs require public HTTPS; direct
  downloads revalidate hosts/DNS at up to three redirects and connect to the checked IP, retaining
  normal TLS verification. Jina/model requests refuse redirects; credentials stay on the host.
- Git runs in an offline rootless container. A short-lived Unix-socket CONNECT proxy is its only
  egress and permits granted public HTTPS hosts on port 443. No host Git config/credentials,
  template hooks, prompts, submodules, LFS, or redirects. Fetch image is pinned on first use.
  Wire bytes and checkout size/count are bounded; budget cleanup kills the whole Git writer group.
- Research metadata records source URL/query, retrieval time, bytes/hash or Git commit, and tree
  digest. Resume checks saved inputs; acceptance provenance records the research catalog. Inputs
  required for replay belong in recipe/vendor with origin records: replay stays offline and has
  no research mount. No second dependency language was introduced.
- Snapshot traversal now uses directory FDs, supports contained relative symlinks, and bounds
  bytes/file count. Absolute/escaping links and special files remain rejected. `[limits]` configures
  memory_mb, processes, snapshot_mb, and snapshot_files. Builds should use sandbox scratch for
  generated trees with absolute symlinks. These checks are not hard filesystem quotas.
- `SerialLog`: trusted Linux termios capture with explicit lifetime, timestamps, bounded disk/ring
  storage, observation offsets, error propagation, and restoration/cleanup.
- `Capability`: a trusted short-lived Unix socket explicitly granted through recipe.run(access=[...]).
  Only trusted handlers choose host operations; the candidate sends structured arguments/bytes.
  No /dev/bus/usb, serial device, host home, or engine socket mount is introduced.
- `DFUBoard`: cross-project USB/serial leases, stable physical-port selection, optional trusted reset
  argv, ROM DFU baseline, descriptor listing, agent-selected DFU transfers, serial read/write,
  and independent fastboot version
  query. Serial is captured outside the candidate even on failure. Lab logs and payload hashes
  are retained. The workshop receives no device grant.
- `examples/pocketbeagle2-fastboot`: `test_fastboot.py` and `sisyphus.toml` only. Generic
  board-access documentation is retained as an evidence attachment for on-demand agent reads.
  Hardware is gated by SISYPHUS_HARDWARE=1; skips remain incomplete, never acceptance.

## Verification

Current TOML-only implementation verification (2026-09-06):

- Full software suite: **190 passed in 78.99s**, including 21 real-container checks, using
  SISYPHUS_TEST_IMAGE=docker.io/library/ubuntu:24.04 and
  SISYPHUS_FETCH_TEST_IMAGE=docker.io/library/node:24-bookworm.
- `uv run --offline ruff check .` and `uv build --offline` passed.
- Static comparison confirmed that PocketBeagle TOML exactly preserves the former settings.
  Its test was parsed for syntax only; no hardware example, model endpoint, or research request ran.
- Regressions cover TOML creation of an empty recipe, preservation/export of prepared artifacts,
  rejection of inline settings, trusted collection boundaries, and saved-run identities.
- Model feedback explicitly lists granted download hosts, so the agent retains visibility of
  its research permissions after settings move out of the test source. No credentials are added.

Previous single-file simplification verification (2026-09-06, superseded implementation):

- Full software suite: **188 passed in 78.04s**, including 21 real-container checks, with the
  same SISYPHUS_TEST_IMAGE / SISYPHUS_FETCH_TEST_IMAGE values below.
- After tightening enclosing-project discovery and adding four regression cases:
  `uv run --offline pytest -q tests/test_config.py tests/test_plugin.py tests/test_solver.py`
  → **77 passed in 7.69s**. That suite contained 192 tests; the 192 together were not rerun.
- `uv run --offline ruff check .` and `uv build --offline` passed.
- The actual PocketBeagle file was only statically checked; neither its test nor a recipe ran.
  No physical devices, model endpoint, Jina, or external Git/downloads were accessed this turn.
- A software-test escalation review timed out; its permitted single retry succeeded. No unresolved
  test blocker remains. Lab transport responses now expose raw status/text instead of the old
  staged `transferred` flag. The removed unrun starter was its only example consumer.

Previous milestone verification, retained for provenance:

Environment: Python 3.14.6, pytest 9.1.1, uv 0.11.21, rootless Podman 5.8.6. Local images used:
ubuntu:24.04 for ordinary container checks, node:24-bookworm for Python/Git/proxy checks.

```sh
SISYPHUS_TEST_IMAGE=docker.io/library/ubuntu:24.04 \
SISYPHUS_FETCH_TEST_IMAGE=docker.io/library/node:24-bookworm \
UV_CACHE_DIR=/tmp/sisyphus-uv-cache uv run --offline pytest -q
# 168 passed in 73.62s, including 20 real-container checks.

# Added after strengthening Git checkout-budget cleanup:
SISYPHUS_FETCH_TEST_IMAGE=docker.io/library/node:24-bookworm \
UV_CACHE_DIR=/tmp/sisyphus-uv-cache uv run --offline pytest -q tests/test_access_podman.py -k checkout_budget
# 1 passed, 2 deselected in 3.12s.
# Suite at that milestone: 169 tests, including 21 container checks.

UV_CACHE_DIR=/tmp/sisyphus-uv-cache uv run --offline ruff check .
UV_CACHE_DIR=/tmp/sisyphus-uv-cache uv build --offline
# Lint and wheel/sdist build passed; runtime client and Git helper confirmed in wheel.
```

Tests cover DNS/private-address/redirect denial, pinned-IP connections, bounded downloads and
checksum mismatch, provider-key isolation, on-demand page storage, research tamper detection,
contained links and a directory-swap race, resource limits, pseudo-terminal serial behavior,
capability envelopes/cleanup, actual container-to-socket access and absence in subsequent replay,
and actual network-none proxy enforcement. Existing solve locking/resume/SIGKILL tests still pass.
The normal suite makes no external research requests or physical board calls. In this Codex
sandbox even binding Unix sockets requires escalation; run socket/container tests outside that
restricted tool sandbox. Do not silently skip failed configured container tests.

Live broker checks passed: public octocat/Hello-World clone, a 13-byte README download, Jina Reader,
and Jina Search for PocketBeagle 2 USB DFU. The clone resolved commit
7fd1a60b01f91b314f59955a4e4d4e80d8edf11d. A second real clone passed after writer-group cleanup was
added, and its saved provenance verified. These are broker smoke tests, not new model evaluations.
See RESEARCH-EVALUATION.md/json; earlier local Qwen greeting/compiler results remain in EVALUATION.md.

The first HTTPS smoke attempts failed because standalone Python lacked a default CA file. Loading
the installed OS CA bundle fixed it without weakening certificate/hostname checks; explicit
SSL_CERT_FILE/SSL_CERT_DIR overrides are preserved.

## Credentials and saved evidence

The user supplied a Jina key in conversation. It was used only via the host subprocess environment;
its value is not in any repository file, saved configuration, transcript, or exported recipe.
A private mode-0600 temporary file `/tmp/sisyphus-jina-key` holds it for this session's continuation;
never print/copy its contents into project files or logs. Load it into JINA_API_KEY only for the
trusted solver/broker process when needed. The local model endpoint needs no API key:
`http://10.134.6.235:8080/v1`, model `qwen3.8-27b`.

Research evidence is archived under `.sisyphus/evaluations/2026-09-06/research/`, with a separate
`git-cleanup/` archive for the final Git smoke. Archived run metadata contains original temporary
absolute paths: inspect these archives, do not resume them from the copied location. A scan found
zero matches of the supplied credential in staged project files and research evidence.

## Preserved contracts

- Trusted test/lab code runs on the host; candidate/model-generated code never executes there.
- Every recipe invocation gets a fresh container/root and copy of frozen source. The workshop
  persists but is paused during snapshots/tests; processes restart before each repair.
- Settings, local image IDs, references/editor, trusted project identity, package versions,
  test selection/collection, phases and spent attempt budgets are retained. Research is separately
  checked. Changing tests/configuration requires a new run.
- Resume tests first and preserves spent attempts. Parent lifetime pipe and inherited project lock
  prevent orphaned test workers/concurrent solve; cleanup targets only containers of this run.
- Candidate rejection differs from infrastructure error/incomplete tests. Agent messages, skips,
  narrowed collection, and no recipe execution cannot accept a result.
- Accepted source comes from the successful frozen attempt. No general clean-source rebuild proof,
  dependency authenticity guarantee, or binary publishing policy has been added.

## Remaining work after hardware connection

1. Configure and authorize the first PocketBeagle trial. Validate/reset wiring, tool availability,
   the agent-created recipe, board security/memory variant, DFU re-enumeration, and real fastboot behavior.
2. Evaluate smaller/larger models; only the supplied 27B Qwen endpoint has been exercised so far.
3. Add hard storage quotas and optimize large snapshots if measured workloads require it.
4. Later: fastboot boot/Linux tests, Rubik Pi ADB/kexec, custom distro reuse, source/generated final
   rebuild checks, and optional trusted publishing. No support is claimed for those yet.

Read AGENTS.md, PLAN.md and TODO.md before continuing. Preserve the explicit hardware stop boundary.

## GitHub repository — 2026-09-22

Created `https://github.com/acalinp/sisyphus`, configured `origin` as
`git@github.com:acalinp/sisyphus.git`, and published the initial `main` branch. The repository was
made public after scanning every tracked file and Git object, including unreachable staging blobs,
for credential files, private keys, embedded URL credentials, bearer tokens, and common provider
key formats. Matches were limited to environment-variable names and deliberate fake test values;
no API key or other secret was found. No implementation or hardware checks were rerun because this
change only records and publishes the existing project state. The sibling Alo project was not
accessed or changed.
