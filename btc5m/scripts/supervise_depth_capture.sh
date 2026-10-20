#!/bin/bash
# Canonical launchd handoff/fallback supervisor for capture_depth_ws.py.
#
# launchd is preferred.  If launchd is unavailable, a bounded-or-unbounded
# fallback loop starts the capture directly.  The capture's nonblocking flock
# is the correctness boundary when multiple supervisors race; pgrep is only a
# compatibility optimization for capture processes started before lock support.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PROJECT_ROOT="${DEPTH_CAPTURE_PROJECT_ROOT:-$DEFAULT_PROJECT_ROOT}"
PYTHON_BIN="${DEPTH_CAPTURE_PYTHON:-$PROJECT_ROOT/.venv/bin/python3}"
CAPTURE_SCRIPT="${DEPTH_CAPTURE_SCRIPT:-$PROJECT_ROOT/scripts/capture_depth_ws.py}"
WORKDIR="${DEPTH_CAPTURE_WORKDIR:-$PROJECT_ROOT}"
STDOUT_LOG="${DEPTH_CAPTURE_STDOUT_LOG:-$WORKDIR/state/cache/depth_capture_stdout.log}"
STDERR_LOG="${DEPTH_CAPTURE_STDERR_LOG:-$WORKDIR/state/cache/depth_capture_stderr.log}"
PLIST_PATH="${DEPTH_CAPTURE_PLIST_PATH:-$PROJECT_ROOT/ops/launchd/com.rytty.depth_capture.plist}"
PLIST_LABEL="${DEPTH_CAPTURE_PLIST_LABEL:-com.rytty.depth_capture}"
LOCK_FILE="${DEPTH_CAPTURE_LOCK_FILE:-$WORKDIR/state/cache/capture_depth_ws.lock}"
LAUNCHCTL_BIN="${DEPTH_CAPTURE_LAUNCHCTL:-$(command -v launchctl 2>/dev/null || true)}"
LAUNCHD_UID="${DEPTH_CAPTURE_UID:-$(id -u)}"

BACKOFF_SECONDS="${DEPTH_CAPTURE_BACKOFF_INITIAL_SECONDS:-5}"
BACKOFF_MAX_SECONDS="${DEPTH_CAPTURE_BACKOFF_MAX_SECONDS:-60}"
MAX_LOOPS="${DEPTH_CAPTURE_MAX_LOOPS:-0}"
PROCESS_GUARD="${DEPTH_CAPTURE_PROCESS_GUARD:-1}"
BOOTSTRAP_ONLY="${BOOTSTRAP_ONLY:-0}"

mkdir -p "$(dirname "$STDOUT_LOG")" "$(dirname "$STDERR_LOG")" "$(dirname "$LOCK_FILE")"

timestamp() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

log_error() {
    local message="$1"
    printf '[%s] %s\n' "$(timestamp)" "$message" >&2
    printf '[%s] %s\n' "$(timestamp)" "$message" >>"$STDERR_LOG"
}

run_launchctl() {
    "$LAUNCHCTL_BIN" "$@"
}

# Return 0 when launchd already owns the service or bootstrap succeeds.
# When the service is absent, bootstrap is invoked exactly once.
try_launchd_handoff() {
    local domain="gui/$LAUNCHD_UID"
    local service_target="$domain/$PLIST_LABEL"
    local output rc

    if [[ -z "$LAUNCHCTL_BIN" || ! -x "$LAUNCHCTL_BIN" ]]; then
        log_error "launchctl unavailable at '${LAUNCHCTL_BIN:-<unset>}'"
        return 1
    fi

    output="$(run_launchctl print "$service_target" 2>&1)"
    rc=$?
    if [[ $rc -eq 0 ]]; then
        [[ -n "$output" ]] && printf '%s\n' "$output" >>"$STDERR_LOG"
        log_error "launchd service $service_target is already loaded; handoff complete"
        return 0
    fi

    [[ -n "$output" ]] && printf '%s\n' "$output" >>"$STDERR_LOG"
    log_error "launchd service $service_target is not loaded; attempting one bootstrap"

    output="$(run_launchctl bootstrap "$domain" "$PLIST_PATH" 2>&1)"
    rc=$?
    if [[ -n "$output" ]]; then
        printf '%s\n' "$output" >&2
        printf '%s\n' "$output" >>"$STDERR_LOG"
    fi
    if [[ $rc -eq 0 ]]; then
        log_error "launchd bootstrap succeeded; handoff complete"
        return 0
    fi

    log_error "launchd bootstrap failed with exit $rc"
    return 1
}

log_error "supervisor PID=$$ starting"

if try_launchd_handoff; then
    exit 0
fi

# Test/probe mode is a launchd handoff check only.  It must never start the
# fallback capture process after any launchctl failure.
if [[ "$BOOTSTRAP_ONLY" == "1" ]]; then
    log_error "BOOTSTRAP_ONLY=1: launchd handoff failed; no fallback process started"
    exit 1
fi

loop_count=0
last_exit=0
while [[ "$MAX_LOOPS" -eq 0 || "$loop_count" -lt "$MAX_LOOPS" ]]; do
    loop_count=$((loop_count + 1))

    # Protect the pre-deployment capture process that does not yet own a lock.
    # This is not the singleton primitive: two supervisors can race this check,
    # and CaptureInstanceLock resolves that race in the child process.
    if [[ "$PROCESS_GUARD" == "1" ]] && pgrep -f "$CAPTURE_SCRIPT" >/dev/null 2>&1; then
        log_error "capture already visible via pgrep; spawn skipped (loop $loop_count)"
        last_exit=0
    else
        if ! cd "$WORKDIR"; then
            log_error "cannot cd to $WORKDIR"
            exit 1
        fi

        PYTHONPATH="$WORKDIR:$WORKDIR/scripts${PYTHONPATH:+:$PYTHONPATH}" \
            "$PYTHON_BIN" "$CAPTURE_SCRIPT" --lock-file "$LOCK_FILE" \
            >>"$STDOUT_LOG" 2>>"$STDERR_LOG"
        last_exit=$?

        if [[ "$last_exit" -eq 75 ]]; then
            log_error "capture singleton lock is owned; duplicate start rejected with exit 75"
        else
            log_error "capture exited with code $last_exit"
        fi
    fi

    if [[ "$MAX_LOOPS" -ne 0 && "$loop_count" -ge "$MAX_LOOPS" ]]; then
        break
    fi

    if [[ "$BACKOFF_SECONDS" != "0" ]]; then
        sleep "$BACKOFF_SECONDS"
    fi
    BACKOFF_SECONDS=$((BACKOFF_SECONDS * 2))
    if [[ "$BACKOFF_SECONDS" -gt "$BACKOFF_MAX_SECONDS" ]]; then
        BACKOFF_SECONDS="$BACKOFF_MAX_SECONDS"
    fi
done

exit "$last_exit"
