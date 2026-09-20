"""Background scheduler: runs update checks + full updates on a schedule
configurable from the UI (interval hours OR time-of-day daily/weekly).
No external cron needed.

Hardened: boot grace period, persisted next-run timestamps + schedule spec,
consecutive failure backoff, exception logging (never bare except), shared
job gate.
"""
import json
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import checker, updater
from .config import DATA_DIR, get, load
from .history import RunLogger
from .jobs import gate

NEXT_FILE = DATA_DIR / "schedule.json"
BOOT_GRACE_S = 600        # no scheduled full update in the first 10 min after start
MAX_BACKOFF_S = 6 * 3600  # consecutive-failure backoff ceiling

def _load_next() -> dict:
    if NEXT_FILE.exists():
        try:
            return json.loads(NEXT_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}

def _save_next(next_check: float, next_full: float) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = NEXT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "next_check": next_check, "next_full_update": next_full,
        "spec": _spec(),
    }), encoding="utf-8")
    tmp.replace(NEXT_FILE)

def _spec() -> str:
    """Compact identity of the current schedule configuration.
    Persisted alongside next-run timestamps: if the config's spec changes
    while the app is off, the next-run times are recomputed on boot."""
    mode = get("update_schedule_mode", "interval")
    if mode == "interval":
        return f"interval:{get('update_interval_hours', 168)}"
    t = get("update_schedule_time", "03:30")
    if mode == "weekly":
        return f"weekly:{int(get('update_schedule_day', 0))}:{t}"
    return f"daily:{t}"

def _schedule_desc() -> str:
    """Human-readable description of the active schedule."""
    mode = get("update_schedule_mode", "interval")
    if mode == "interval":
        return f"every {get('update_interval_hours', 168)}h"
    t = get("update_schedule_time", "03:30")
    if mode == "weekly":
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"{days[int(get('update_schedule_day', 0)) % 7]} at {t}"
    return f"daily at {t}"

def _next_full_from(now_ts: float) -> float:
    """Compute the next full-update epoch from the configured schedule."""
    mode = get("update_schedule_mode", "interval")
    if mode != "daily" and mode != "weekly":
        return now_ts + max(1, int(get("update_interval_hours", 168))) * 3600
    t = get("update_schedule_time", "03:30")
    try:
        hh, mm = (int(x) for x in t.split(":", 1))
    except (ValueError, AttributeError):
        hh, mm = 3, 30  # defensive fallback for hand-edited config
    now = datetime.fromtimestamp(now_ts)  # naive local time (TZ env applies)
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    if mode == "weekly":
        target_wd = int(get("update_schedule_day", 0)) % 7
        while cand.weekday() != target_wd:
            cand += timedelta(days=1)
    return time.mktime(cand.timetuple())  # DST-aware local -> epoch

class Scheduler:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self.next_full_update = None
        self.next_check = None
        self._started_at = None
        self.consecutive_failures = 0
        self.last_tick = None
        self.last_error = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        # rehydrate persisted schedule; apply boot grace to the full update.
        # If the saved schedule spec differs from the current config (mode/
        # time changed in the UI or config.yaml edited), recompute from now.
        saved = _load_next()
        now = time.time()
        self._started_at = now
        self.next_check = max(saved.get("next_check", now + 15), now + 10)
        full = saved.get("next_full_update", _next_full_from(now))
        if saved.get("spec") != _spec():
            full = _next_full_from(now)  # schedule changed while we were off
        self.next_full_update = max(full, now + BOOT_GRACE_S)
        self._persist()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _persist(self):
        _save_next(self.next_check or 0, self.next_full_update or 0)

    def _loop(self):
        while not self._stop.wait(10):
            self.last_tick = time.time()
            load()  # pick up config changes from the UI
            try:
                now = time.time()
                if now >= self.next_check:
                    self._run_check()
                    self._bump_check()
                if now >= self.next_full_update:
                    self._run_update("scheduled")
                    self._bump_full()
            except Exception:
                # never swallow: log to stderr (visible in journald/systemd)
                self.last_error = traceback.format_exc()
                print(f"[scheduler] loop error:\n{self.last_error}", file=sys.stderr,
                      flush=True)
                # push the schedule forward anyway so a bad config value
                # cannot wedge the loop into a 10-second hot cycle
                self._bump_check()
                self._bump_full()

    def schedule_desc(self) -> str:
        return _schedule_desc()

    def _interval(self) -> int:
        try:
            return max(1, int(get("update_interval_hours", 168)))
        except (TypeError, ValueError):
            return 168

    def _bump_check(self):
        self.next_check = time.time() + self._interval() * 3600
        self._persist()

    def _bump_full(self):
        """Compute the next full-update slot. For time-of-day schedules the
        base comes from _next_full_from(); consecutive-failure backoff adds
        ON TOP of the next slot (never skips a slot completely)."""
        now = time.time()
        if get("update_schedule_mode", "interval") == "interval":
            base = self._interval() * 3600
            # consecutive-failure backoff
            if self.consecutive_failures > 0:
                backoff = min(MAX_BACKOFF_S, base * min(self.consecutive_failures, 6))
                base = max(base, backoff)
            self.next_full_update = now + base
        else:
            nxt = _next_full_from(now)
            if self.consecutive_failures > 0:
                backoff = min(MAX_BACKOFF_S, 3600 * min(self.consecutive_failures, 6))
                nxt = max(nxt, now + backoff)
            self.next_full_update = nxt
        self._persist()

    def _client(self):
        from .portainer import Portainer, PortainerError
        import socket
        client = Portainer(get("portainer_url"), get("portainer_api_key"),
                           endpoint_id=get("portainer_endpoint_id"),
                           tls_verify=bool(get("tls_verify", False)))
        try:
            client.resolve_endpoint(socket.gethostname())
        except PortainerError:
            return None
        return client

    def _run_check(self):
        if not gate.try_acquire("check"):
            return
        try:
            client = self._client()
            if not client:
                # not configured yet - retry soon instead of waiting a full
                # interval (first-run flow: user may be mid-setup)
                self.next_check = time.time() + 600
                return
            checker.run_check(client, client.endpoint_id)
        finally:
            gate.release()

    def _run_update(self, trigger: str):
        if not gate.try_acquire("update"):
            return
        runlog = None
        try:
            runlog = RunLogger("full", trigger)
            gate.set_runlog(runlog)  # attach logger to the lock we already hold
            ok = updater.run_full_update(runlog)
            runlog.finish(ok)
            if ok:
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1
                self._notify_failure(runlog)
        except Exception:
            self.consecutive_failures += 1
            self.last_error = traceback.format_exc()
            print(f"[scheduler] update crashed:\n{self.last_error}", file=sys.stderr,
                  flush=True)
            if runlog:
                runlog.finish(False)
        finally:
            # run_full_update may have finalized the runlog already (self-stack
            # redeploy kills the process before returning) - finish() is
            # idempotent, so a normal return also lands here safely.
            gate.release()
        # a manual run also satisfies the schedule - no immediate double run
        self._bump_full()

    def _notify_failure(self, runlog):
        url = get("notify_webhook", "")
        if not url:
            return
        try:
            import requests
            requests.post(url, timeout=10, json={
                "title": "Portainer update run FAILED",
                "run_id": runlog.run_id,
                "steps": [s for s in runlog.steps if not s["ok"]],
            })
        except Exception as e:  # noqa: BLE001 - notification must not kill the run
            print(f"[scheduler] notify failed: {e}", file=sys.stderr, flush=True)

    # ------------------------------------------------------------- API hooks
    def trigger_check_async(self):
        threading.Thread(target=self._run_check, daemon=True).start()

    def trigger_update_async(self, trigger="manual"):
        """Start a full update if the gate is free. Returns run_id or None.

        Order matters: acquire the gate FIRST, then create the RunLogger -
        a RunLogger created before a failed acquire would leak a file handle
        and leave status.json stuck in 'running'."""
        if not gate.try_acquire("update"):
            return None
        runlog = None
        try:
            runlog = RunLogger("full", trigger)
            gate.set_runlog(runlog)
            threading.Thread(target=self._update_worker, args=(runlog,), daemon=True).start()
            return runlog.run_id
        except Exception:
            # thread could not start (or RunLogger failed): unwind cleanly so
            # the gate is never held forever and status is never stuck
            gate.release()
            if runlog:
                runlog.finish(False)
            print("[scheduler] trigger failed:\n" + traceback.format_exc(),
                  file=sys.stderr, flush=True)
            return None

    def _update_worker(self, runlog):
        try:
            ok = updater.run_full_update(runlog)
            runlog.finish(ok)
            if ok:
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1
                self._notify_failure(runlog)
        except Exception:
            self.consecutive_failures += 1
            print(f"[update] crashed:\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            runlog.finish(False)
        finally:
            # finish() is idempotent (self-stack update may have finalized it
            # already before the process was replaced)
            gate.release()
        self._bump_full()  # manual run satisfies the schedule

    def is_busy(self):
        return gate.is_busy()

scheduler = Scheduler()