from __future__ import annotations

from .state import Run


def feedback(run: Run, attempt: dict, result: dict) -> dict:
    """Describe evidence, without pulling its bodies into the repair prompt."""
    base = f"/evidence/{attempt['number']:04d}/evidence"
    failures = [
        {
            "test": report["nodeid"],
            "phase": report["phase"],
            "summary": report.get("summary", "")[-1200:],
            "path": f"{base}/{report['evidence']}" if report.get("evidence") else None,
        }
        for report in result["tests"]
        if report["outcome"] in {"reject", "error"}
    ]
    entries = []
    directory = run.path / "attempts" / f"{attempt['number']:04d}" / "evidence"
    for command in result["commands"]:
        for stream in ("stdout", "stderr"):
            relative = command[stream]
            path = directory / relative
            entries.append(
                {
                    "path": f"{base}/{relative}",
                    "bytes": path.stat().st_size if path.exists() else None,
                    "description": f"{command['kind']} {stream}; candidate output",
                }
            )
    attachments = directory / "attachments"
    if attachments.is_dir():
        for path in sorted(attachments.iterdir()):
            if (
                path.is_file()
                and not path.is_symlink()
                and not path.name.endswith(".metadata.json")
            ):
                entries.append(
                    {
                        "path": f"{base}/attachments/{path.name}",
                        "bytes": path.stat().st_size,
                        "description": "Trusted attachment",
                    }
                )
    return {
        "version": 1,
        "attempt": attempt["number"],
        "outcome": result["outcome"],
        "recipe": "/recipe",
        "tests": "/tests",
        "revision": result["digests"]["recipe"],
        "remaining_attempts": run.data["limit"] - len(run.data["attempts"]),
        "failures": failures[:5],
        "more_failures": max(0, len(failures) - 5),
        "evidence": entries[:50],
        "more_evidence": max(0, len(entries) - 50),
        "full_result": f"{base}/result.json",
        "references": {name: f"/refs/{name}" for name in run.data["references"]},
        "instruction": "Read the trusted test and relevant evidence. Edit only the recipe. "
        "Save required dependency setup. Return a version 1 JSON status on stdout; "
        "write diagnostics to stderr. Only the next test can establish success.",
    }
