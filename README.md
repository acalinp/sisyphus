# Sisyphus

**An ordinary test, an editable recipe, and a sandboxed repair loop.**

```python
def test_greeting(recipe):
    result = recipe.run(["sh", "hello"])
    assert result.stdout.strip() == "hello, world"
```

The test runs on the host. The recipe runs in a fresh rootless container. A configured editor can repair the recipe in a persistent sandbox; the test decides whether it worked.

## Status

The pytest runner, resumable solve loop, and model editor work. Use a local OpenAI-compatible Chat Completions endpoint, or the deterministic command editor. Opt-in Jina research, public Git/downloads, trusted serial capture, and scoped board capabilities are implemented. The PocketBeagle 2 example is written but has not been run on hardware. Trusted artifact publishing remains planned. Local Qwen repaired both included model exercises; see [EVALUATION.md](EVALUATION.md) for results. Plans and continuation notes are in [PLAN.md](PLAN.md), [TODO.md](TODO.md), and [HANDOFF.md](HANDOFF.md).

## Try it

Requirements: Linux, Python 3.11+, uv, and working rootless Podman. Solve also requires working rootless container pause/unpause (cgroups v2). The selected base image must provide `/bin/sh` and `sleep infinity` and already exist locally. No images are pulled by the test runner.

```sh
uv sync
podman pull docker.io/library/ubuntu:24.04
cd examples/hello
uv run --project ../.. pytest test_hello.py
# Equivalent convenience command:
uv run --project ../.. sisyphus test test_hello.py
```

To start from a failure, change `examples/hello/recipe/hello` to print `hello, moon`. Change it back to `hello, world` and run the same test again. The test is the specification; changes belong in the recipe.

Or let the example's deterministic editor make that change:

```sh
# From examples/hello:
uv run --project ../.. sisyphus solve test_hello.py --attempts 3
```

The command tests the original recipe, gives its failure to the sandboxed editor, and reruns the same test. It prints the saved run directory and the accepted recipe's location.

## Define a project

Start with two authored files:

```text
project/
  sisyphus.toml     # Sandbox, model, research permissions, and limits.
  test_hello.py     # Starting condition, recipe invocation, and acceptance assertions.
```

Sisyphus creates an empty `recipe/` beside the configuration after validating its settings.
With a model configured, `sisyphus solve test_hello.py` tests that directory and asks the agent
to create the missing program. `pytest test_hello.py` runs the same contract without the agent.
Configuration is TOML only; test modules do not declare runtime settings.

The agent's launcher, prepared binaries, build instructions, and README belong in `recipe/`.
Prepared artifacts survive fresh replays and are included in the accepted export. Existing
recipe files are preserved when loading configuration. The runtime launcher can reuse saved
binaries without rebuilding or fetching them on each reset; a rebuild recipe and pinned inputs
should accompany them. Workshop-only files and installations do not survive fresh replay.

```toml
# sisyphus.toml is trusted and stays outside the editable recipe.
[recipe]
path = "recipe"
image = "docker.io/library/ubuntu:24.04"
timeout = 300                     # Total invocation budget, in seconds.
# setup = ["sh", "setup"]        # Optional saved dependency setup.

# [references]
# prior_recipe = "../known-good"  # Available read-only at /refs/prior_recipe.
```

Paths resolve relative to this configuration. The image is resolved to a local immutable ID for the test session. Unknown keys and overlapping candidate/reference/state paths are rejected. Snapshots support regular files, directories, and contained relative symbolic links. Absolute/escaping links, FIFOs, sockets, and devices are rejected; keep virtual environments and absolute build-tree links outside the recipe. All files are included, with no hidden ignore patterns.

The pytest plugin discovers `sisyphus.toml` from the pytest root, working directory, or selected test path. An enclosing project takes precedence over nested configurations to preserve its access boundaries. Use `--sisyphus-config /path/to/sisyphus.toml` when invoking tests from elsewhere. It is inert in projects without a configuration unless they request its fixtures. Candidate, reference, and evidence directories are excluded from collection. Explicit discovery inside them is rejected before their `conftest.py` can load. If an editable directory starts with `test`, select the trusted test file explicitly to avoid pytest's initial `test*` directory probing.

## Use a model

```toml
[editor]
model = "qwen3.8-27b"
base_url = "http://10.134.6.235:8080/v1"
timeout = 300
# max_steps = 24
```

This is the local endpoint used during development; replace the URL and model for your server. No key is needed for this endpoint. For an authenticated HTTPS provider, set `api_key_env = "PROVIDER_API_KEY"` and supply that variable in the host environment. Sisyphus stores only its name. The host makes the API requests; model commands execute in the offline workshop. API access grants no network access to the workshop or replay. URLs cannot embed credentials, redirects are refused, and ambient HTTP proxy settings are ignored.

OpenRouter is available as a first-class provider. Its API URL and standard key variable are
selected automatically:

```toml
[editor]
provider = "openrouter"
model = "openai/gpt-5.2"
timeout = 300
```

Set `OPENROUTER_API_KEY` in the host environment before running `sisyphus solve`. You can override
`base_url` or `api_key_env` when needed. OpenRouter requests include the `X-OpenRouter-Title:
Sisyphus` attribution header. Choose an OpenRouter model that supports tool calling.

Run the deliberately broken model examples from disposable copies so the originals stay reusable:

```sh
# From the repository, after uv sync and loading the required local image:
cp -r examples/hello-model /tmp/my-sisyphus-hello
.venv/bin/sisyphus solve /tmp/my-sisyphus-hello/test_hello.py --config /tmp/my-sisyphus-hello/sisyphus.toml

# The C example uses the existing node:24-bookworm image, which includes cc:
cp -r examples/compile /tmp/my-sisyphus-compile
.venv/bin/sisyphus solve /tmp/my-sisyphus-compile/test_sum.py --config /tmp/my-sisyphus-compile/sisyphus.toml
```

Without research enabled, the model has four tools: `read_file`, `write_file`, `shell`, and `finish`. All file operations and shell commands execute inside Podman. Paths are sandbox paths; they cannot select host files. The model starts with short failure summaries, available test paths, an evidence index, and the previous repair's notebook. It reads test source and full logs on demand.

Each repair allows 24 model requests by default (configurable from 1 to 100), at most 4096 generated tokens per response, and a 48000-byte message context. Old complete tool exchanges are evicted when needed; the initial instructions and latest failure stay. File reads return at most 6000 bytes using byte offsets. Shell output returns at most 6000 bytes per stream while full bounded logs remain on disk. File writes accept up to 24000 UTF-8 bytes; use shell patches for larger files. `/tmp/sisyphus-notes.txt` can hold working notes across context eviction; the `finish` message becomes the next repair's short notebook.

Model transcripts, token usage when supplied by the endpoint, and tool logs are retained under `repairs/NNNN/model-NNNN/`. Provider credentials never enter prompts, tool arguments, replay, or saved settings. Echoed credentials are redacted before provider output is stored or used. API errors stop with an infrastructure result; use resume after correcting the endpoint. Reaching the step limit requests a test of any edits already made, never acceptance. A timed-out tool has all workshop processes stopped before another tool can run.

This adapter targets Chat Completions servers with function calling and includes first-class
OpenRouter configuration. Local Qwen has been exercised end to end; OpenRouter transport behavior
is covered by scripted-provider tests rather than a live paid request. It adds no SDK dependency
and performs no automatic model discovery or image pulls. The compiler example exercises a local
compiler and README assertions; package-manager networking and general clean-source rebuild
verification are still unfinished. Saved package files can be installed by an offline setup script.

## Research and source repositories

Enable the optional research tools in trusted configuration:

```toml
[research]
jina_key_env = "JINA_API_KEY"
download_hosts = ["github.com", "raw.githubusercontent.com", "codeload.github.com", "release-assets.githubusercontent.com"]
fetch_image = "docker.io/library/node:24-bookworm"
max_mb = 512
timeout = 300
```

`search(query)` and `read_url(url)` use Jina and return short previews plus saved file paths.
`clone_repo(url, ref)` returns a read-only shallow Git clone and exact resolved commit.
`download(url, sha256)` saves exact bytes; an empty hash discovers it, a supplied hash must match.
Use ordinary shell/read tools to browse the saved files at `/research/<id>/`. Research is data,
not additional instructions. Jina credentials stay in the host environment and never enter
containers. The provider key is not saved in configuration or provenance.

Copy required inputs into `recipe/vendor/` along with their `origin.json`, and record commits
or hashes in the recipe's README/source manifest. Every trial remains offline and has no
/research mount. This intentionally uses vendored inputs rather than a new dependency language.
Saved inputs are hashed and checked on resume; accepted provenance records the research catalog.
A hash records identity, not whether an upstream release is authentic or suitable for the board.

Byte downloads permit up to three redirects, rechecking HTTPS, the exact hostname allowlist,
and public DNS addresses at each hop. Credentials are dropped on cross-host redirects. The
actual connection uses the checked IP with normal TLS hostname/certificate verification. Jina
requests refuse redirects. Arbitrary private/loopback/link-local targets are denied; the explicitly
configured local model endpoint is a separate permitted host connection.

Git runs in a disposable **network-none** container. A Unix-socket CONNECT proxy is its only
egress, permitting only granted public HTTPS hosts. Git has no host credentials/configuration,
interactive prompts, template hooks, automatic submodules, or LFS fetching. Its image must
already exist locally with git, python3, CA certificates, /bin/sh, and sleep infinity. The fetched
image ID is pinned on first use. Git redirects are refused; use a canonical repository URL.
Fetch stderr is retained on failure. Research may be repeated without changing trial acceptance.

Live Jina search/Reader, a public Git clone, and an exact-byte download have been verified.
The host TLS client uses the installed OS CA bundle when standalone Python lacks a default
cafile, while honoring explicit SSL_CERT_FILE/SSL_CERT_DIR and retaining certificate checks.

## Resource settings

```toml
[limits]
memory_mb = 4096
processes = 512
snapshot_mb = 8192
snapshot_files = 300000
```

Defaults are 2048 MiB, 256 processes, 4096 MiB per snapshot, and 200000 snapshot entries.
Snapshot traversal uses directory file descriptors and refuses external links/special files.
Git transfers have a wire-byte budget; the Git helper additionally limits individual files and
monitors checkout size/count. These are bounded checks, not filesystem quotas: workshop root
storage and total retained runs are not quota-limited. Large source trees are still copied.

## Hardware experiments

The [PocketBeagle 2 test](examples/pocketbeagle2-fastboot/test_fastboot.py) has its runtime
settings in [sisyphus.toml](examples/pocketbeagle2-fastboot/sisyphus.toml). The test establishes
ROM USB DFU, executes the agent's `sh run`, checks a real fastboot
response, and requires a README. It supplies no source choices, binary names, build flags,
transfer sequence, or candidate code. Serial capture is diagnostic evidence, not an extra
acceptance criterion. **The example has not been run on hardware.**

Before the first authorized run, edit its USB port, serial path, and optional trusted reset
command. With no reset command, the operator must reset into ROM DFU before each trial;
unattended iteration requires a working reset command. Host `dfu-util` and `fastboot` must
be installed and permitted to access that port. The configured local node:24-bookworm image
provides Python/Git and a native compiler; no board-specific toolchain is preselected or
promised. The agent can fetch packages/toolchains from the granted hosts and prepare them
inside its sandbox. Jina uses the host's `JINA_API_KEY`. No image is implicitly pulled.

After connecting/configuring hardware, from that example directory:

```sh
SISYPHUS_HARDWARE=1 uv run --project ../.. sisyphus solve test_fastboot.py
# Test the saved recipe without another model turn:
SISYPHUS_HARDWARE=1 uv run --project ../.. pytest test_fastboot.py
```

Trusted Python owns a device lease, bounded serial capture, reset, and independent verification.
`recipe.run(..., access=[board.access])` grants a short-lived Unix socket for that board's DFU
and serial operations. Replay does not receive USB device trees or the host serial path; the
workshop receives no capability. Records include serial bytes/timestamps, scoped command logs,
and payload hashes. Temporary capability paths never appear in the editable recipe.

The helper has been tested using pseudo-terminals, fake DFU/fastboot responses, and real
container-to-socket calls. Those tests establish software boundaries, not board compatibility.
Arbitrary DUT firmware/console access grants substantial DUT control; RAM-only intent is not
an enforced property of the uploaded code. The helper exposes DFU transport and console access, with no hardcoded boot sequence.

## Solve and resume

Alternatively, use a trusted editor directory and command:

```toml
[editor]
path = "editor"
command = ["sh", "/editor/repair"]
timeout = 300
```

The command executes **inside** the rootless workshop using the same pinned base image as the recipe. It receives one JSON feedback document on stdin. It may edit `/recipe`, use `/refs/<name>`, inspect the selected test under `/tests`, and read previous trials under `/evidence`. Editor code at `/editor`, tests, references, and evidence are mounted read-only. The host project, engine socket, and host environment are not exposed.

The workshop filesystem persists across repair turns and resumes, so installed tools and files under `/tmp` or `/opt` survive. Sisyphus pauses the entire container before inspecting or testing the recipe. It restarts the container's processes before each repair, clearing unfinished previous commands while retaining files. A fresh test container gets none of that workshop-only state.

The feedback contains short failure summaries, the candidate revision, remaining attempts, and an index of evidence paths and sizes. Full logs stay on disk. The editor returns exactly one JSON object on stdout; diagnostics go to stderr:

```json
{"version":1,"status":"changed","message":"Corrected the greeting."}
```

Valid statuses are `changed`, `blocked`, and `error`. `changed` requests another test; it never establishes acceptance. No actual recipe change is treated as blocked. Malformed responses, failed editor commands, and timeouts are infrastructure failures. Responses are limited to 8 KiB.

```sh
sisyphus solve test_boot.py --attempts 10 --test-timeout 900
sisyphus solve test_boot.py::test_ram_boot --config ./sisyphus.toml
sisyphus solve --resume .sisyphus/runs/<id>
sisyphus solve --resume .sisyphus/runs/<id> --attempts 20
sisyphus discard .sisyphus/runs/<id>
```

The attempt budget includes the initial test and interrupted trials. Resume can increase the total budget but cannot reset it. Every resumed unfinished run starts with a fresh test of the current recipe. Infrastructure errors and incomplete tests stop without invoking the editor; exhausted or blocked runs can be resumed. `discard` removes the saved workshop and marks an unfinished run abandoned, retaining its evidence.

One project lock covers the solver and its pytest child. If the parent dies during a test, the child detects the closed parent pipe and exits; resume then removes leftover trial containers belonging to that run. A lock held during this short shutdown causes a clear busy error rather than starting a concurrent experiment.

Runs store their settings, resolved image ID, frozen references/editor, test selection, collected node IDs, and budget. Trusted project files and installed Python package versions are checked before and after attempts and on resume. Keep generated lab diagnostics in the evidence/state directory or temporary storage. Changing trusted tests, configuration, or helpers requires a new run; changing the editable recipe is allowed. Changes to live references do not change a saved run's frozen inputs.

Solve selects one explicit Python test file (optionally a node ID). It disables inherited `PYTEST_*`/`PYTHON*` overrides, automatic third-party plugin loading, pytest `addopts`, and parent-directory conftests. Declare required plugins in trusted project `conftest.py`. A changed collection, deselection, skipped test, or missing report cannot establish acceptance. The test assertions and recipe fixture are the same ones used by plain pytest.

Saved runs live under `.sisyphus/runs/<id>/`:

```text
run.json             # Phase, settings, identities, budget, attempts, and repairs.
references/          # Pinned read-only inputs.
editor/              # Pinned editor adapter.
context/             # Read-only copy of the selected test for the editor.
attempts/0001/       # Frozen source, pytest logs, and structured evidence.
repairs/0001/        # JSON feedback, response, and editor diagnostics.
accepted/recipe/     # Exact source snapshot that passed.
accepted/acceptance.json
```

Accepted source is exported from the tested snapshot, so later edits to the working recipe do not change it. This is source export and acceptance provenance; clean rebuild verification and binary publishing remain separate future work.

## Recipe fixture

```python
result = recipe.run(
    ["sh", "build"],
    timeout=120,
    env={"TARGET": "arm64"},
)
assert "built" in result.stdout
assert result.file("out/kernel.bin").stat().st_size > 0
```

- Commands are argument lists, never implicit shell strings. Use `["sh", "-c", "..."]` explicitly if needed; this shell still runs inside the container.
- Each `run` copies the same frozen recipe into a new writable directory and creates a fresh container. Generated files and installed dependencies do not persist between invocations. Put dependent operations into one script.
- Optional `setup` runs inside that same fresh container before the requested command. Its failure always rejects the invocation. With networking disabled, setup can use declared local inputs; online package installation is not supported yet.
- `/recipe` is writable; declared references are read-only under `/refs/<name>`. The container has an offline network and defaults to a 256-process limit and 2 GiB memory limit; trusted [limits] settings can change these. Sandbox root maps to the invoking host user, not host root.
- Host home, trusted tests, evidence, engine socket, and host environment variables are not mounted/passed into the container. Explicit `env` values must not contain replay credentials. `SISYPHUS_*` names are reserved.
- `stdout`/`stderr` read retained files on demand. Each stream retains at most 16 MiB; exceeding the limit rejects the command and stops its container. Logs are not automatically inserted into assertion messages.
- A nonzero command raises `RecipeRejected`. Use `check=False` when the test deliberately expects a failing command, then assert `result.returncode`. Timeouts and output limits always raise. Podman's reserved exec exit 125 is conservatively treated as infrastructure failure, including a recipe that itself exits 125.
- `result.file()` allows only candidate-relative regular files without symlinks. Returned paths are for trusted assertions/read access, not host execution or imports.

There is deliberately no production local-process fallback. Unit tests inject a fake backend explicitly; real examples always require Podman.

## Evidence and outcomes

Each configured pytest session creates `.sisyphus/attempts/<id>/` outside the recipe:

```text
result.json           # Versioned verdict, image/source identities, test/command index.
source/               # Frozen recipe used by this attempt.
references/           # Frozen read-only reference inputs.
commands/0001/        # stdout.log and stderr.log, separate for setup and command.
tests/                # Detailed pytest failure reports.
work/                 # Generated files from each disposable invocation.
attachments/          # Optional trusted observations.
```

A trusted lab fixture or test can attach an existing observation:

```python
evidence.attach(serial_log_path, name="serial.log", description="Boot serial output")
```

Assertions, `pytest.fail()`, and failed recipe commands produce **reject**. Unexpected exceptions and setup/teardown/collection failures produce **error**. Skips, xfails, no tests, and sessions with no recipe command are **incomplete**. Only a complete passing test session that actually exercises a recipe produces **pass**. Incomplete acceptance also returns a nonzero pytest exit code.

Tests must not execute or import candidate files on the host. User-provided tests and lab fixtures are trusted code; Python itself is not a sandbox. Stop external editors before testing so the initial snapshot reflects one coherent revision. Solve freezes its own editor automatically, but cannot freeze an unrelated external editor. Parallel pytest workers and concurrent use of one DUT are not supported.

A fresh sandbox demonstrates independence from its previous filesystem. It does **not** establish that prebuilt files included in the recipe were rebuilt, nor does passing an assertion prove behavior the test did not check. Clean rebuild/export verification is a later milestone.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv build

# Opt in to real sandbox tests with an existing local image:
SISYPHUS_TEST_IMAGE=docker.io/library/ubuntu:24.04 \
SISYPHUS_FETCH_TEST_IMAGE=docker.io/library/node:24-bookworm uv run pytest -m podman
```

The model examples are intentionally failing and are excluded from the package's own test collection. No real board support is claimed yet.
