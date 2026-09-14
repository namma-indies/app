"""PID-1 watchdog for the worker; lease heartbeat alone cannot detect native hangs.

Only a completed claim iteration updates progress. Idle polls and handled HTTP
errors count; an indefinitely running CUDA call does not. Startup has the same
420s allowance. The 300s client job timeout cannot kill a native thread, so this
parent kills the worker process group. Clip decoders have their own bounded
process group; exiting container PID 1 lets the runtime tear down any survivors.
Kubernetes restarts the container; API lease fencing controls replay safety.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

PROGRESS_TIMEOUT = 420
STOP_GRACE = 335
HANG_GRACE = 35


def supervise(command, progress: Path, *, timeout=PROGRESS_TIMEOUT,
              stop_grace=STOP_GRACE, hang_grace=HANG_GRACE, interval=1):
    progress.unlink(missing_ok=True)
    child = subprocess.Popen(command, start_new_session=True)
    deadline = None
    last_progress = time.monotonic()
    previous = None

    def send(sig):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass

    def stop(signum, frame):
        nonlocal deadline
        if deadline is None:
            deadline = time.monotonic() + stop_grace
            send(signal.SIGTERM)

    original = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        while child.poll() is None:
            now = time.monotonic()
            try:
                stamp = progress.stat().st_mtime_ns
            except FileNotFoundError:
                stamp = None
            if stamp is not None and stamp != previous:
                previous, last_progress = stamp, now
            if deadline is None and now - last_progress >= timeout:
                print("gpu_worker: progress timeout; terminating process group", file=sys.stderr, flush=True)
                send(signal.SIGTERM)
                deadline = now + hang_grace
            if deadline is not None and now >= deadline:
                send(signal.SIGKILL)
                child.wait()
                return 1
            time.sleep(interval)
        return child.returncode if child.returncode >= 0 else 1
    finally:
        send(signal.SIGKILL)
        child.wait()
        for sig, handler in original.items():
            signal.signal(sig, handler)


def main():
    try:
        progress = Path(os.environ["MEDIA_GPU_PROGRESS_FILE"])
        if not progress.is_absolute():
            raise ValueError
        return supervise([sys.executable, "-m", "gpu_worker"], progress)
    except Exception:
        print("gpu_worker: supervisor failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
