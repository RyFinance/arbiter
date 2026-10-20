"""Process-level singleton tests for scripts/capture_depth_ws.py."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
CAPTURE_SCRIPT = SCRIPTS_DIR / "capture_depth_ws.py"

HOLDER_CODE = """
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from capture_depth_ws import CaptureInstanceLock

lock_path = Path(sys.argv[2])
ready_path = Path(sys.argv[3])
hold_seconds = float(sys.argv[4])
with CaptureInstanceLock.acquire(lock_path):
    ready_path.write_text("ready", encoding="utf-8")
    time.sleep(hold_seconds)
"""


def _start_holder(
    tmp_path: Path, lock_path: Path, *, hold_seconds: float = 1.0
) -> tuple[subprocess.Popen[str], Path]:
    ready_path = tmp_path / f"ready-{time.monotonic_ns()}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            HOLDER_CODE,
            str(SCRIPTS_DIR),
            str(lock_path),
            str(ready_path),
            str(hold_seconds),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if ready_path.exists():
            return process, ready_path
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"lock holder exited before ready: rc={process.returncode} "
                f"stdout={stdout!r} stderr={stderr!r}"
            )
        time.sleep(0.01)
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)
    raise AssertionError(f"lock holder timed out: stdout={stdout!r} stderr={stderr!r}")


def _capture_command(lock_path: Path | None, output_path: Path) -> list[str]:
    command = [
        sys.executable,
        str(CAPTURE_SCRIPT),
        "--output",
        str(output_path),
        "--duration",
        "1",
        "--token",
        "test-token",
    ]
    if lock_path is not None:
        command.extend(["--lock-file", str(lock_path)])
    return command


def test_cli_lock_contention_exits_75_then_reacquires_after_owner_exit(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "locks" / "capture.lock"
    output_path = tmp_path / "writer-must-not-start" / "capture.jsonl"
    holder, _ = _start_holder(tmp_path, lock_path)

    try:
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
        assert owner["pid"] == holder.pid
        assert owner["lock_path"] == str(lock_path.resolve())
        assert owner["script"] == str(CAPTURE_SCRIPT.resolve())
        assert owner["acquired_at"]

        contender = subprocess.run(
            _capture_command(lock_path, output_path),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert contender.returncode == 75
        assert "SINGLETON LOCK CONTENTION" in contender.stderr
        assert "exiting 75" in contender.stderr
        assert f'"pid":{holder.pid}' in contender.stderr
        assert not output_path.parent.exists(), "writer initialized before lock acquisition"
    finally:
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.terminate()
            holder.wait(timeout=5)

    assert holder.returncode == 0

    reacquired_ready = tmp_path / "reacquired-ready"
    reacquired = subprocess.run(
        [
            sys.executable,
            "-c",
            HOLDER_CODE,
            str(SCRIPTS_DIR),
            str(lock_path),
            str(reacquired_ready),
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert reacquired.returncode == 0, reacquired.stderr
    assert reacquired_ready.read_text(encoding="utf-8") == "ready"
    new_owner = json.loads(lock_path.read_text(encoding="utf-8"))
    assert new_owner["pid"] != holder.pid


def test_environment_lock_override_is_used_before_writer_initialization(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "env-lock" / "capture.lock"
    output_path = tmp_path / "env-writer-must-not-start" / "capture.jsonl"
    holder, _ = _start_holder(tmp_path, lock_path)
    env = os.environ.copy()
    env["DEPTH_CAPTURE_LOCK_FILE"] = str(lock_path)

    try:
        contender = subprocess.run(
            _capture_command(None, output_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert contender.returncode == 75
        assert str(lock_path.resolve()) in contender.stderr
        assert not output_path.parent.exists()
    finally:
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.terminate()
            holder.wait(timeout=5)


def test_default_lock_path_is_stable() -> None:
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from capture_depth_ws import DEFAULT_LOCK_PATH
    finally:
        sys.path.pop(0)

    assert DEFAULT_LOCK_PATH == PROJECT_ROOT / "state" / "cache" / "capture_depth_ws.lock"
