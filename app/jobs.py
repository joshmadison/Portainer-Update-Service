"""Central run gate + current-run registry.

All mutating jobs (full update, prune, redeploy, version pin, registry check)
acquire this lock so the scheduler and API routes can never interleave.
The active RunLogger is registered here so /api/status and /api/runs/current
can surface run_id + live progress.

Metadata (holder/runlog/since) is guarded by _meta_lock and cleared BEFORE
releasing the real lock, so a fast re-acquirer can never observe stale state.
"""
import threading
import time


class JobGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._meta_lock = threading.Lock()
        self._holder = None
        self._runlog = None
        self._since = None

    def try_acquire(self, holder: str, runlog=None) -> bool:
        """Non-blocking acquire. Returns False if a job is running."""
        if self._lock.acquire(blocking=False):
            with self._meta_lock:
                self._holder = holder
                self._runlog = runlog
                self._since = time.time()
            return True
        return False

    def set_runlog(self, runlog) -> None:
        """Attach a RunLogger to the CURRENTLY HELD lock (never re-acquire -
        threading.Lock is not reentrant and would deadlock)."""
        with self._meta_lock:
            self._runlog = runlog

    def release(self):
        # clear metadata BEFORE releasing so the next holder starts clean
        # and observers never see holder=None while the lock is held
        with self._meta_lock:
            self._holder = None
            self._runlog = None
            self._since = None
        try:
            self._lock.release()
        except RuntimeError:
            # releasing an unowned lock - should never happen; report loudly
            import sys
            print("[jobs] release without acquire", file=sys.stderr, flush=True)

    def current(self) -> dict:
        with self._meta_lock:
            return {
                "active": self._holder is not None,
                "holder": self._holder,
                "since": self._since,
                "run_id": self._runlog.run_id if self._runlog else None,
                "steps": self._runlog.steps if self._runlog else [],
            }

    def is_busy(self) -> bool:
        with self._meta_lock:
            return self._holder is not None


# one gate per process: scheduler AND api routes share it
gate = JobGate()