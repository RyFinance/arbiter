"""Hermetic tests for the repository-owned depth-capture supervisor."""
from __future__ import annotations

import os
import plistlib
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = PROJECT_ROOT / "scripts" / "supervise_depth_capture.sh"
REPO_PLIST = PROJECT_ROOT / "ops" / "launchd" / "com.rytty.depth_capture.plist"
EXPECTED_LABEL = "com.rytty.depth_capture"
EXPECTED_LOCK = PROJECT_ROOT / "state" / "cache" / "capture_depth_ws.lock"


class MockLaunchctl:
    """A launchctl replacement that records its real argv, one call per line."""

    def __init__(
        self,
        root: Path,
        *,
        print_rc: int,
        bootstrap_rc: int,
        print_output: str = "",
        bootstrap_output: str = "",
        name: str = "launchctl",
    ) -> None:
        self.path = root / name
        self.calls_path = root / f"{name}.calls"
        root.mkdir(parents=True, exist_ok=True)

        script = f"""#!/bin/bash
command_name="${{1:-}}"
{{
    printf '%s' "$command_name"
    shift || true
    for argument in "$@"; do
        printf '\t%s' "$argument"
    done
    printf '\n'
}} >> {shlex.quote(str(self.calls_path))}

case "$command_name" in
    print)
        printf '%s\n' {shlex.quote(print_output)}
        exit {print_rc}
        ;;
    bootstrap)
        printf '%s\n' {shlex.quote(bootstrap_output)}
        exit {bootstrap_rc}
        ;;
    *)
        printf 'unexpected mock launchctl command: %s\n' "$command_name" >&2
        exit 64
        ;;
esac
"""
        self.path.write_text(script, encoding="utf-8")
        self.path.chmod(
            stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
        )

    def calls(self) -> list[list[str]]:
        if not self.calls_path.exists():
            return []
        return [line.split("\t") for line in self.calls_path.read_text().splitlines()]


def _hermetic_env(tmp_path: Path, launchctl: Path) -> tuple[dict[str, str], dict[str, Path]]:
    workdir = tmp_path / "workdir"
    logs = tmp_path / "logs"
    workdir.mkdir(parents=True)
    logs.mkdir(parents=True)

    capture_marker = tmp_path / "capture_started"
    capture_script = tmp_path / "capture_should_not_run.py"
    capture_script.write_text(
        "from pathlib import Path\n"
        f"Path({str(capture_marker)!r}).write_text('started')\n",
        encoding="utf-8",
    )
    plist_path = tmp_path / "fixture.plist"
    plist_path.write_text("test fixture only\n", encoding="utf-8")

    env = os.environ.copy()
    for key in list(env):
        if key.startswith("DEPTH_CAPTURE_") or key == "BOOTSTRAP_ONLY":
            env.pop(key)
    env.update(
        {
            "BOOTSTRAP_ONLY": "1",
            "DEPTH_CAPTURE_PROJECT_ROOT": str(tmp_path / "project-root"),
            "DEPTH_CAPTURE_PYTHON": sys.executable,
            "DEPTH_CAPTURE_SCRIPT": str(capture_script),
            "DEPTH_CAPTURE_WORKDIR": str(workdir),
            "DEPTH_CAPTURE_STDOUT_LOG": str(logs / "stdout.log"),
            "DEPTH_CAPTURE_STDERR_LOG": str(logs / "stderr.log"),
            "DEPTH_CAPTURE_PLIST_PATH": str(plist_path),
            "DEPTH_CAPTURE_PLIST_LABEL": EXPECTED_LABEL,
            "DEPTH_CAPTURE_LOCK_FILE": str(tmp_path / "locks" / "capture.lock"),
            "DEPTH_CAPTURE_LAUNCHCTL": str(launchctl),
            "DEPTH_CAPTURE_UID": "4242",
            "DEPTH_CAPTURE_BACKOFF_INITIAL_SECONDS": "0",
            "DEPTH_CAPTURE_BACKOFF_MAX_SECONDS": "0",
            "DEPTH_CAPTURE_MAX_LOOPS": "1",
            "DEPTH_CAPTURE_PROCESS_GUARD": "0",
        }
    )
    paths = {
        "capture_marker": capture_marker,
        "capture_script": capture_script,
        "plist": plist_path,
        "stdout": logs / "stdout.log",
        "stderr": logs / "stderr.log",
        "lock": tmp_path / "locks" / "capture.lock",
        "workdir": workdir,
    }
    return env, paths


def _run_supervisor(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SUPERVISOR)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


class TestRepoPlist:
    def test_plist_is_valid_xml(self) -> None:
        result = subprocess.run(
            ["plutil", "-lint", str(REPO_PLIST)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_plist_semantics_are_complete(self) -> None:
        with REPO_PLIST.open("rb") as handle:
            plist = plistlib.load(handle)

        assert plist["Label"] == EXPECTED_LABEL
        assert plist["ProgramArguments"] == [
            str(PROJECT_ROOT / ".venv" / "bin" / "python3"),
            str(PROJECT_ROOT / "scripts" / "capture_depth_ws.py"),
            "--lock-file",
            str(EXPECTED_LOCK),
        ]
        assert plist["WorkingDirectory"] == str(PROJECT_ROOT)
        assert plist["KeepAlive"] is True
        assert plist["RunAtLoad"] is True
        assert plist["ThrottleInterval"] == 5
        assert plist["EnvironmentVariables"]["DEPTH_CAPTURE_LOCK_FILE"] == str(
            EXPECTED_LOCK
        )


class TestLaunchdHandoff:
    def test_already_loaded_is_success_without_bootstrap(self, tmp_path: Path) -> None:
        mock = MockLaunchctl(tmp_path / "mock", print_rc=0, bootstrap_rc=99)
        env, paths = _hermetic_env(tmp_path, mock.path)

        result = _run_supervisor(env)

        assert result.returncode == 0, result.stderr
        assert mock.calls() == [["print", f"gui/4242/{EXPECTED_LABEL}"]]
        assert "already loaded" in result.stderr
        assert not paths["capture_marker"].exists()

    def test_absent_service_bootstraps_exactly_once_with_real_argv(
        self, tmp_path: Path
    ) -> None:
        mock = MockLaunchctl(
            tmp_path / "mock",
            print_rc=1,
            bootstrap_rc=0,
            print_output="service not found",
        )
        env, paths = _hermetic_env(tmp_path, mock.path)

        result = _run_supervisor(env)

        assert result.returncode == 0, result.stderr
        assert mock.calls() == [
            ["print", f"gui/4242/{EXPECTED_LABEL}"],
            ["bootstrap", "gui/4242", str(paths["plist"])],
        ]
        assert sum(call[0] == "bootstrap" for call in mock.calls()) == 1
        assert not paths["capture_marker"].exists()

    def test_bootstrap_only_failure_never_starts_fallback(self, tmp_path: Path) -> None:
        mock = MockLaunchctl(
            tmp_path / "mock",
            print_rc=1,
            bootstrap_rc=5,
            bootstrap_output="Input/output error",
        )
        env, paths = _hermetic_env(tmp_path, mock.path)

        result = _run_supervisor(env)

        assert result.returncode == 1
        assert mock.calls() == [
            ["print", f"gui/4242/{EXPECTED_LABEL}"],
            ["bootstrap", "gui/4242", str(paths["plist"])],
        ]
        assert "Input/output error" in result.stderr
        assert "no fallback process started" in result.stderr
        assert not paths["capture_marker"].exists()

    def test_missing_launchctl_is_visible_and_never_falls_back(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "missing" / "launchctl"
        env, paths = _hermetic_env(tmp_path, missing)

        result = _run_supervisor(env)

        assert result.returncode == 1
        assert "launchctl unavailable" in result.stderr
        assert "no fallback process started" in result.stderr
        assert not paths["capture_marker"].exists()


def _write_racing_capture(path: Path) -> None:
    """Write a child that synchronizes two starts, then uses the real Python lock."""
    path.write_text(
        """from __future__ import annotations
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ["REAL_CAPTURE_MODULE_DIR"])
from capture_depth_ws import CaptureInstanceLock, CaptureLockUnavailable

parser = argparse.ArgumentParser()
parser.add_argument("--lock-file", required=True)
args = parser.parse_args()
attempts = Path(os.environ["RACE_ATTEMPTS"])
events = Path(os.environ["RACE_EVENTS"])

with attempts.open("a", encoding="utf-8") as handle:
    handle.write(f"{os.getpid()}\\n")
    handle.flush()
    os.fsync(handle.fileno())

deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    if len(attempts.read_text(encoding="utf-8").splitlines()) >= 2:
        break
    time.sleep(0.01)
else:
    raise SystemExit("second fallback child did not reach race barrier")

try:
    instance_lock = CaptureInstanceLock.acquire(args.lock_file)
except CaptureLockUnavailable:
    with events.open("a", encoding="utf-8") as handle:
        handle.write(f"contended {os.getpid()}\\n")
    raise SystemExit(75)

with instance_lock:
    with events.open("a", encoding="utf-8") as handle:
        handle.write(f"owner_start {os.getpid()}\\n")
        handle.flush()
        os.fsync(handle.fileno())
    time.sleep(0.30)
    with events.open("a", encoding="utf-8") as handle:
        handle.write(f"owner_end {os.getpid()}\\n")
        handle.flush()
        os.fsync(handle.fileno())
""",
        encoding="utf-8",
    )


def test_concurrent_fallback_supervisors_have_exactly_one_capture_owner(
    tmp_path: Path,
) -> None:
    capture_script = tmp_path / "racing_capture.py"
    attempts = tmp_path / "attempts.log"
    events_path = tmp_path / "events.log"
    _write_racing_capture(capture_script)

    mock_one = MockLaunchctl(
        tmp_path / "mock-one", print_rc=1, bootstrap_rc=1, name="launchctl-one"
    )
    mock_two = MockLaunchctl(
        tmp_path / "mock-two", print_rc=1, bootstrap_rc=1, name="launchctl-two"
    )
    env_one, paths_one = _hermetic_env(tmp_path / "one", mock_one.path)
    env_two, paths_two = _hermetic_env(tmp_path / "two", mock_two.path)
    shared_lock = tmp_path / "shared" / "capture.lock"

    for env, paths in ((env_one, paths_one), (env_two, paths_two)):
        env.pop("BOOTSTRAP_ONLY")
        env.update(
            {
                "DEPTH_CAPTURE_SCRIPT": str(capture_script),
                "DEPTH_CAPTURE_LOCK_FILE": str(shared_lock),
                "DEPTH_CAPTURE_PROCESS_GUARD": "0",
                "REAL_CAPTURE_MODULE_DIR": str(PROJECT_ROOT / "scripts"),
                "RACE_ATTEMPTS": str(attempts),
                "RACE_EVENTS": str(events_path),
            }
        )

    first = subprocess.Popen(
        [str(SUPERVISOR)], env=env_one, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    second = subprocess.Popen(
        [str(SUPERVISOR)], env=env_two, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    first_stdout, first_stderr = first.communicate(timeout=15)
    second_stdout, second_stderr = second.communicate(timeout=15)

    assert len(attempts.read_text(encoding="utf-8").splitlines()) == 2
    events = events_path.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("owner_start ") for line in events) == 1
    assert sum(line.startswith("owner_end ") for line in events) == 1
    assert sum(line.startswith("contended ") for line in events) == 1

    active = 0
    maximum_active = 0
    for event in events:
        if event.startswith("owner_start "):
            active += 1
            maximum_active = max(maximum_active, active)
        elif event.startswith("owner_end "):
            active -= 1
    assert maximum_active == 1
    assert active == 0
    assert sorted([first.returncode, second.returncode]) == [0, 75]
    assert "SINGLETON LOCK CONTENTION" not in first_stdout + second_stdout
    assert "duplicate start rejected with exit 75" in first_stderr + second_stderr

    for path in (
        paths_one["stdout"],
        paths_one["stderr"],
        paths_two["stdout"],
        paths_two["stderr"],
        shared_lock,
    ):
        assert str(path).startswith(str(tmp_path))
