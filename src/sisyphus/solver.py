from __future__ import annotations

import os
import shutil
import sys

from .editor import Workshop
from .errors import InfrastructureError
from .evidence import write_json
from .feedback import feedback
from .sandbox import Podman, logged_process
from .snapshot import snapshot
from .state import ProjectLock, Run, read_json


def pytest_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "PYTHON"))
    }
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


class TestDriver:
    __test__ = False

    def __init__(self, backend: Podman, lock: ProjectLock):
        self.backend = backend
        self.lock = lock

    def __call__(self, run: Run, entry: dict) -> dict:
        directory = run.path / "attempts" / f"{entry['number']:04d}"
        directory.mkdir()
        digest = snapshot(run.config.candidate, directory / "source", limits=run.config.limits)
        entry["digest"] = digest
        run.save()
        spec = {
            "version": 1,
            "run_id": run.data["id"],
            "settings": run.config.record(),
            "source": str(directory / "source"),
            "evidence": str(directory / "evidence"),
            "references": run.data["references"],
            "image": run.data["image"],
            "digests": {
                "recipe": digest,
                **{
                    f"reference:{name}": value
                    for name, value in run.data["reference_digests"].items()
                },
            },
        }
        write_json(directory / "trial.json", spec)
        assert self.lock.fd is not None
        args = [
            sys.executable,
            "-I",
            "-m",
            "sisyphus.trial",
            "-p",
            "sisyphus.plugin",
            "--sisyphus-trial",
            str(directory / "trial.json"),
            "--rootdir",
            str(run.config.file.parent),
            "--confcutdir",
            str(run.config.file.parent),
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-q",
            run.data["selection"],
        ]
        try:
            process = logged_process(
                args,
                directory,
                run.data["test_timeout"],
                cwd=run.config.file.parent,
                env=pytest_environment(),
                keep_stdin=True,
                pass_fds=(self.lock.fd,),
            )
        finally:
            self.backend.cleanup_trials(run.data["id"])
        entry["pytest_returncode"] = process.returncode
        run.save()
        if process.timed_out or process.output_limited:
            raise InfrastructureError(
                f"Trusted test exceeded its time/output budget; see {directory}"
            )
        result = read_json(directory / "evidence" / "result.json")
        if (
            result.get("version") != 1
            or result.get("run_id") != run.data["id"]
            or result.get("digests") != spec["digests"]
            or result.get("image") != run.data["image"]
            or snapshot(directory / "source", limits=run.config.limits) != digest
        ):
            raise InfrastructureError("Test report identity does not match the frozen trial")
        outcome = result.get("outcome")
        if outcome not in {"pass", "reject", "error", "incomplete"}:
            raise InfrastructureError("Test report has an invalid verdict")
        if outcome == "pass" and process.returncode != 0:
            raise InfrastructureError("A nonzero pytest process cannot establish acceptance")
        if process.returncode not in (0, 1) and outcome not in {"error", "incomplete"}:
            raise InfrastructureError("Pytest infrastructure failed despite the recorded verdict")
        if result.get("deselected"):
            raise InfrastructureError("Test selection was narrowed during collection")
        collected = result.get("collected")
        if not isinstance(collected, list) or any(not isinstance(node, str) for node in collected):
            raise InfrastructureError("Test report has no valid collection identity")
        if run.data["collected"] is None:
            run.data["collected"] = collected
            run.save()
        elif collected != run.data["collected"]:
            raise InfrastructureError("Collected tests changed between attempts")
        return result


class Solver:
    def __init__(
        self, run: Run, lock: ProjectLock, *, backend=None, driver=None, workshop=None, report=print
    ):
        self.run = run
        self.backend = backend if backend is not None else Podman()
        self.driver = driver if driver is not None else TestDriver(self.backend, lock)
        self.workshop = workshop if workshop is not None else Workshop(run, self.backend)
        self.report = report

    def execute(self) -> Run:
        run = self.run
        self.report(f"Run: {run.path}")
        try:
            if run.data["phase"] in {"accepted", "discarded"}:
                self.workshop.remove()
                self.backend.cleanup_trials(run.data["id"])
                if run.data["phase"] == "accepted":
                    accepted = read_json(run.path / "accepted" / "acceptance.json")
                    if (
                        snapshot(run.path / "accepted" / "recipe", limits=run.config.limits)
                        != accepted["digests"]["recipe"]
                    ):
                        raise InfrastructureError("Accepted recipe was modified after verification")
                return run
            # A previous parent may have died during repair. Freeze first, then inspect files.
            if self.workshop.exists() or run.data["workshop_created"]:
                self.workshop.ensure()
            self.backend.cleanup_trials(run.data["id"])
            run.verify_trusted()
            for entry in [*run.data["attempts"], *run.data["repairs"]]:
                if entry["status"] == "running":
                    entry["status"] = "interrupted"
            run.set_phase("ready", last_error=None)
            while len(run.data["attempts"]) < run.data["limit"]:
                run.verify_trusted()
                entry = {"number": len(run.data["attempts"]) + 1, "status": "running"}
                run.data["attempts"].append(entry)
                run.set_phase("testing")
                self.report(f"Attempt {entry['number']}/{run.data['limit']}: testing")
                result = self.driver(run, entry)
                run.verify_trusted()
                entry["status"] = result["outcome"]
                run.save()
                self.report(f"Attempt {entry['number']}: {result['outcome']}")
                if result["outcome"] == "pass":
                    self.workshop.remove()
                    self.accept(entry, result)
                    return run
                if result["outcome"] != "reject":
                    run.set_phase(result["outcome"])
                    return run
                if len(run.data["attempts"]) >= run.data["limit"]:
                    break
                self.workshop.ensure()
                before = snapshot(run.config.candidate, limits=run.config.limits)
                repair = {"number": len(run.data["repairs"]) + 1, "status": "running"}
                run.data["repairs"].append(repair)
                run.set_phase("repairing")
                directory = run.path / "repairs" / f"{repair['number']:04d}"
                directory.mkdir()
                write_json(directory / "feedback.json", feedback(run, entry, result))
                self.report(f"Repair {repair['number']}: editing in sandbox")
                response = self.workshop.repair(directory / "feedback.json", directory)
                repair.update(status=response["status"], message=response.get("message", ""))
                run.verify_trusted()
                after = snapshot(run.config.candidate, limits=run.config.limits)
                repair.update(before=before, after=after)
                run.save()
                if response["status"] == "error":
                    raise InfrastructureError(response.get("message") or "Editor reported an error")
                if response["status"] == "blocked" or before == after:
                    run.set_phase(
                        "blocked",
                        last_error=response.get("message")
                        or "Editor returned without changing the recipe",
                    )
                    return run
                run.set_phase("ready")
            run.set_phase("exhausted")
        except KeyboardInterrupt:
            for entry in [*run.data["attempts"], *run.data["repairs"]]:
                if entry["status"] == "running":
                    entry["status"] = "interrupted"
            run.set_phase("interrupted", last_error="Interrupted; resume will run a fresh test")
            raise
        except (InfrastructureError, OSError) as exc:
            for entry in [*run.data["attempts"], *run.data["repairs"]]:
                if entry["status"] == "running":
                    entry["status"] = "error"
            run.set_phase("error", last_error=str(exc))
            self.report(f"Infrastructure error: {exc}")
        finally:
            try:
                if self.workshop.exists():
                    self.workshop.freeze()
                self.backend.cleanup_trials(run.data["id"])
            except InfrastructureError as exc:
                run.set_phase("error", last_error=f"Cleanup failed: {exc}")
                self.report(run.data["last_error"])
        return run

    def accept(self, entry: dict, result: dict) -> None:
        run = self.run
        source = run.path / "attempts" / f"{entry['number']:04d}" / "source"
        target = run.path / "accepted"
        # An interrupted export can be replaced from a newly verified immutable source.
        if target.exists():
            shutil.rmtree(target)
        digest = snapshot(source, target / "recipe", limits=run.config.limits)
        if digest != result["digests"]["recipe"]:
            raise InfrastructureError("Accepted source differs from the tested revision")
        write_json(
            target / "acceptance.json",
            {
                "version": 1,
                "run_id": run.data["id"],
                "attempt": entry["number"],
                "digests": result["digests"],
                "image": result["image"],
                "selection": run.data["selection"],
                "collected": run.data["collected"],
                "trusted_digest": run.data["trusted_digest"],
                "research_items": run.data.get("research_items", {}),
                "evidence": f"../attempts/{entry['number']:04d}/evidence/result.json",
            },
        )
        run.set_phase("accepted", accepted=str(target))
        self.report(f"Accepted recipe: {target / 'recipe'}")
