"""Run history + status persistence (the script's log/report blocks).

Hardened: unique run ids (uuid suffix), atomic history rotation,
status 'running' marker written on start, run-log retention.
"""
import json
import threading
import time
import uuid
from pathlib import Path

from .config import DATA_DIR, RUNS_DIR

STATUS_FILE = DATA_DIR / "status.json"
HISTORY_FILE = DATA_DIR / "history.jsonl"
STATS_FILE = DATA_DIR / "stats.jsonl"   # prune reclaim stats (90d window)
STATS_FILE_MAX_AGE_DAYS = 30
KEEP_RUNS = 200
KEEP_LOGS_PER_KIND = 20
RUN_LOG_MAX_AGE_DAYS = 30

_lock = threading.Lock()


def record_prune_stats(reclaimed_mb: int, images_deleted: int) -> None:
    """Append one prune outcome for the 30-day reclaim chart."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "reclaimed_mb": reclaimed_mb,
             "images_deleted": images_deleted}
    with _lock:
        with open(STATS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def prune_stats(days: int = 30) -> dict:
    """Aggregate prune stats over the last N days."""
    if not STATS_FILE.exists():
        return {"reclaimed_mb_30d": 0, "prune_runs_30d": 0,
                "images_deleted_30d": 0}
    cutoff = time.time() - STATS_FILE_MAX_AGE_DAYS * 86400
    mb = imgs = runs = 0
    with _lock:
        for line in STATS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("ts", 0) < cutoff:
                continue
            mb += e.get("reclaimed_mb") or 0
            imgs += e.get("images_deleted") or 0
            runs += 1
    return {"reclaimed_mb_30d": mb, "prune_runs_30d": runs,
            "images_deleted_30d": imgs}


def prune_stats_rotation() -> None:
    """Drop stats older than 90 days (chart needs 30)."""
    if not STATS_FILE.exists():
        return
    cutoff = time.time() - 90 * 86400
    with _lock:
        lines = []
        for line in STATS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(line).get("ts", 0) >= cutoff:
                    lines.append(line)
            except json.JSONDecodeError:
                continue
        tmp = STATS_FILE.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(STATS_FILE)


def _write_status(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(STATUS_FILE)


def current_status() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"state": "idle", "runs": []}


def mark_running(run_id: str, kind: str) -> None:
    with _lock:
        st = current_status()
        st["state"] = "running"
        st["current_run"] = {"run_id": run_id, "kind": kind, "started": time.time()}
        _write_status(st)


def mark_interrupted() -> None:
    """Called at startup: convert a stale 'running' marker."""
    st = current_status()
    if st.get("state") == "running":
        cur = st.pop("current_run", None)
        st["state"] = "interrupted"
        if cur:
            entry = {
                "run_id": cur.get("run_id"), "kind": cur.get("kind"),
                "trigger": "startup-recovery", "started": cur.get("started"),
                "finished": time.time(), "duration_s": None,
                "success": False, "interrupted": True, "steps": [],
            }
            with _lock:
                with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            st["last_run"] = entry
        _write_status(st)


def history() -> list:
    if not HISTORY_FILE.exists():
        return []
    with _lock:
        out = []
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def run_log_path(run_id: str) -> Path | None:
    if not run_id or not _RUN_ID_RE.match(run_id):
        return None
    p = (RUNS_DIR / f"{run_id}.log").resolve()
    return p if p.exists() and p.parent == RUNS_DIR.resolve() else None


import re  # noqa: E402
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def prune_run_logs() -> int:
    """Delete old run logs (retention policy). Returns deleted count."""
    if not RUNS_DIR.exists():
        return 0
    cutoff = time.time() - RUN_LOG_MAX_AGE_DAYS * 86400
    by_kind: dict = {}
    deleted = 0
    # run_id format: YYYYmmdd_HHMMSS_<kind>_<uuid4> -> kind is part index 2
    for p in RUNS_DIR.glob("*.log"):
        parts = p.stem.split("_")
        kind = parts[2] if len(parts) >= 3 else ""
        by_kind.setdefault(kind, []).append(p)
    for kind, files in by_kind.items():
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for i, p in enumerate(files):
            try:
                if i >= KEEP_LOGS_PER_KIND or p.stat().st_mtime < cutoff:
                    p.unlink()
                    deleted += 1
            except OSError:
                continue
    return deleted


class RunLogger:
    """Captures a run's output to data/runs/<id>.log and records status/history."""

    def __init__(self, kind: str, trigger: str):
        self.run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{kind}_{uuid.uuid4().hex[:4]}"
        self.kind = kind
        self.trigger = trigger
        self.started = time.time()
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self.path = RUNS_DIR / f"{self.run_id}.log"
        self._fh = open(self.path, "w", encoding="utf-8")
        self.steps: list = []
        mark_running(self.run_id, kind)

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except (ValueError, OSError):
            pass  # closed after finish - late logs are dropped silently

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"name": name, "ok": ok, "detail": detail})
        self.log(f"{'[OK]' if ok else '[ERROR]'} {name} {detail}".rstrip())

    def finish(self, ok: bool) -> dict:
        """Finalize the run. Idempotent: the caller and an early self-finalize
        (before the service redeploys itself and dies) may both call this."""
        if getattr(self, "_finished", False):
            return self._entry
        self._finished = True
        try:
            self._fh.write(f"[{time.strftime('%H:%M:%S')}] Run finished, success={ok}\n")
            self._fh.close()
        except (ValueError, OSError):
            pass
        entry = {
            "run_id": self.run_id,
            "kind": self.kind,
            "trigger": self.trigger,
            "started": self.started,
            "finished": time.time(),
            "duration_s": round(time.time() - self.started, 1),
            "success": ok,
            "steps": self.steps,
        }
        self._entry = entry
        with _lock:
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            # atomic rotation: tmp file then replace
            lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
            if len(lines) > KEEP_RUNS:
                tmp = HISTORY_FILE.with_suffix(".tmp")
                tmp.write_text("\n".join(lines[-KEEP_RUNS:]) + "\n", encoding="utf-8")
                tmp.replace(HISTORY_FILE)
            st = current_status()
            st.pop("current_run", None)
            st["state"] = "success" if ok else "failed"
            st["last_run"] = entry
            st["runs_total"] = st.get("runs_total", 0) + 1
            _write_status(st)
        prune_run_logs()
        return entry