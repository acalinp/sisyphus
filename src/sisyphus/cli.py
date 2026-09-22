from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .config import Config, discover
from .editor import Workshop
from .errors import InfrastructureError
from .sandbox import Podman
from .solver import Solver
from .state import ProjectLock, Run, attempt_limit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sisyphus",
        description="Run ordinary pytest acceptance tests against sandboxed recipes.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser("test", help="Run ordinary pytest")
    test.add_argument("pytest_args", nargs=argparse.REMAINDER, help="Arguments forwarded to pytest")
    solve = commands.add_parser("solve", help="Test, repair in a sandbox, and repeat")
    solve.add_argument("test", nargs="?", help="One trusted test file, optionally with ::test_name")
    solve.add_argument("--config", type=Path, help="Path to sisyphus.toml")
    solve.add_argument("--attempts", type=int, help="Total test attempt budget (default 10)")
    solve.add_argument("--test-timeout", type=float, help="Whole pytest timeout in seconds")
    solve.add_argument("--resume", type=Path, metavar="RUN", help="Resume a saved run directory")
    discard = commands.add_parser("discard", help="Release a saved workshop and abandon its run")
    discard.add_argument("run", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "test":
            return subprocess.call([sys.executable, "-m", "pytest", *args.pytest_args])
        backend = Podman()
        if args.command == "discard":
            run = Run.load(args.run)
            with ProjectLock(run.config.state):
                run = Run.load(args.run)
                Workshop(run, backend).remove()
                backend.cleanup_trials(run.data["id"])
                if run.data["phase"] != "accepted":
                    run.set_phase("discarded")
            print(f"Released workshop: {run.path}")
            return 0
        if args.resume:
            if args.test or args.config or args.test_timeout:
                parser.error("--resume uses the saved test, configuration, and timeout")
            loaded = Run.load(args.resume)
            config = loaded.config
        else:
            if not args.test:
                parser.error("solve requires a test file or --resume RUN")
            file = args.config or discover(Path(args.test.split("::", 1)[0]))
            if file is None:
                parser.error("Cannot find sisyphus.toml; use --config")
            config = Config.load(file)
        with ProjectLock(config.state) as lock:
            if args.resume:
                run = Run.load(args.resume)
                if args.attempts is not None:
                    limit = attempt_limit(args.attempts)
                    if limit < run.data["limit"]:
                        parser.error("Resume can increase the total budget, but cannot reset it")
                    run.data["limit"] = limit
                    run.save()
            else:
                run = Run.create(
                    config,
                    args.test,
                    args.attempts if args.attempts is not None else 10,
                    args.test_timeout if args.test_timeout is not None else config.timeout + 60,
                    backend,
                )
            Solver(run, lock, backend=backend).execute()
            print(f"Sisyphus: {run.data['phase']}")
            return {"accepted": 0, "discarded": 0, "error": 2}.get(run.data["phase"], 1)
    except (InfrastructureError, OSError) as exc:
        print(f"Sisyphus: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
