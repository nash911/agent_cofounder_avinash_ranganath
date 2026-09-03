"""Shared helpers for the harness tests. **Test use only.**

Every helper drives ``python3 -m harness`` as a real subprocess with
``HARNESS_PI_BIN`` pointing at :mod:`harness.tests.fake_pi`, so the exercised
code path is exactly the one the runner uses -- minus the model.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent.parent
FAKE_PI = TESTS_DIR / "fake_pi.py"


def scratch_root() -> pathlib.Path:
    """A writable scratch directory, preferring this session's scratchpad."""
    override = os.environ.get("HARNESS_TEST_SCRATCH")
    root = pathlib.Path(override or os.environ.get("TMPDIR") or "/tmp")
    root.mkdir(parents=True, exist_ok=True)
    return root


def harness_environment(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    env["HARNESS_PI_BIN"] = str(FAKE_PI)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    env.pop("HARNESS_COLOR", None)
    # Never let a developer's real Pi configuration leak into a test run.
    env.pop("HARNESS_PAYLOAD_LOG", None)
    for name in ("FAKE_PI_HANG", "FAKE_PI_SLOW", "FAKE_PI_ERROR", "FAKE_PI_ERROR_ONCE",
                 "FAKE_PI_LINES", "FAKE_PI_GARBAGE", "FAKE_PI_WRITE_REPORT", "FAKE_PI_COMPACT",
                 "HARNESS_PI_AUTO_RETRY", "HARNESS_RESUME_ATTEMPTS",
                 "HARNESS_RESUME_BACKOFF_S", "HARNESS_RESUME_MIN_BUDGET_S"):
        env.pop(name, None)
    if extra:
        env.update(extra)
    return env


def harness_arguments(
    run_dir: pathlib.Path,
    *,
    timeout_ms: int,
    cwd: Optional[pathlib.Path] = None,
    idea_file: Optional[pathlib.Path] = None,
    thinking: str = "off",
) -> List[str]:
    return [
        sys.executable,
        "-m",
        "harness",
        "--idea-file",
        str(idea_file or (REPO_ROOT / "contract-public" / "development-idea.txt")),
        "--session-root",
        str(run_dir / "sessions"),
        "--cwd",
        str(cwd or (REPO_ROOT / "app-template")),
        "--timeout-ms",
        str(timeout_ms),
        "--repo-root",
        str(REPO_ROOT),
        "--thinking",
        thinking,
    ]


def spawn_harness(
    run_dir: pathlib.Path,
    *,
    timeout_ms: int,
    env_extra: Optional[Dict[str, str]] = None,
    cwd: Optional[pathlib.Path] = None,
    idea_file: Optional[pathlib.Path] = None,
) -> "subprocess.Popen[bytes]":
    run_dir.mkdir(parents=True, exist_ok=True)
    # The runner opens these two files itself; here the test plays that role.
    stdout_handle = (run_dir / "events.jsonl").open("wb")
    stderr_handle = (run_dir / "harness.stderr.log").open("wb")
    try:
        return subprocess.Popen(
            harness_arguments(run_dir, timeout_ms=timeout_ms, cwd=cwd, idea_file=idea_file),
            cwd=str(REPO_ROOT),
            env=harness_environment(env_extra),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()


def run_harness(
    run_dir: pathlib.Path,
    *,
    timeout_ms: int,
    env_extra: Optional[Dict[str, str]] = None,
    cwd: Optional[pathlib.Path] = None,
    idea_file: Optional[pathlib.Path] = None,
    wait_s: float = 120.0,
) -> Tuple[int, bytes, str]:
    """Run the harness to completion. Returns (exit code, stdout bytes, stderr)."""
    process = spawn_harness(
        run_dir, timeout_ms=timeout_ms, env_extra=env_extra, cwd=cwd, idea_file=idea_file
    )
    try:
        code = process.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        raise AssertionError("harness did not exit within {0}s".format(wait_s))
    stdout = (run_dir / "events.jsonl").read_bytes()
    stderr = (run_dir / "harness.stderr.log").read_text(encoding="utf-8", errors="replace")
    return code, stdout, stderr


def session_dir(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / "sessions" / "1-builder"


def wait_for(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
