# Research and lab software verification — 2026-09-06

Live broker checks passed:

| Operation | Source | Result |
| --- | --- | --- |
| Git clone | octocat/Hello-World, master | Resolved 7fd1a60b01f91b314f59955a4e4d4e80d8edf11d |
| Byte download | Repository README from raw.githubusercontent.com | 13 bytes, SHA-256 recorded |
| Jina Reader | git-scm.com/docs/git-clone | Saved readable document with provenance |
| Jina Search | PocketBeagle 2 USB DFU on beagleboard.org | Saved search result list |

These were direct broker smoke checks, not live LLM evaluations. Existing Qwen results remain
in EVALUATION.md. Exact input hashes, URLs and the fetch image ID are in RESEARCH-EVALUATION.json.
The full smoke run is archived locally in `.sisyphus/evaluations/2026-09-06/research/`. It records
original temporary absolute paths, so it is an evidence archive, not a resumable copied project.

The initial Python HTTPS requests failed because standalone Python could not find a CA bundle.
Loading the installed OS trust bundle fixed this; certificate and hostname checks stay enabled.
No supplied credential was found in project files or retained research evidence. The Jina key
was supplied only via the host environment; it was not placed in the repository or containers.

Software verification: the full 168-test suite passed in 73.62 seconds, including 20 real-container
checks. An additional focused Git checkout-budget check was then added to cover whole-writer-group
termination. Current total is 169 tests, including 21 container checks. See HANDOFF.md for exact
commands and the final focused result. Pseudo-terminal tests cover serial capture, timestamps,
fresh observation offsets, disconnects, and output budgets. Mock lab tests cover DFU stage order,
physical-port selectors, ROM disconnect handling, and independent fastboot queries.

The PocketBeagle example received Python AST and shell syntax checks only. Its Containerfile,
build script, and hardware test were not executed. No USB probing, serial adapter opening,
reset, DFU transfer, fastboot query, flashing, or publication was performed against physical hardware.
It is a starting recipe for the first authorized device session; sources are not vendored yet.
