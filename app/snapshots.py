"""Per-stack image snapshots for the rollback feature.

On every successful redeploy of a stack, the updater persists what that
stack's services looked like BEFORE the update. "Roll back stack X to its
last healthy state" then means: take the snapshot, set every service's
image back, redeploy.

Storage: data/snapshots.json - one entry per stack, always holding the
LAST state before the most recent snapshot-worthy change. History entries
rotate away, snapshots must not (they are the recovery anchor).
"""
import json
import threading
import time
from pathlib import Path

from .config import DATA_DIR

SNAPSHOTS_FILE = DATA_DIR / "snapshots.json"
KEEP_STACKS = 60

_lock = threading.Lock()


def _path() -> Path:
    return SNAPSHOTS_FILE


def _load_all() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_all(all_snaps: dict) -> None:
    """Atomic write + cap stack entries (newest wins by ts)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if len(all_snaps) > KEEP_STACKS:
        # keep the most recent KEEP_STACKS stacks
        ranked = sorted(all_snaps.items(),
                        key=lambda kv: kv[1].get("ts") or 0, reverse=True)
        all_snaps = dict(ranked[:KEEP_STACKS])
    tmp = _path().with_suffix(".tmp")
    tmp.write_text(json.dumps(all_snaps, indent=1), encoding="utf-8")
    tmp.replace(_path())


def snapshot_stack(stack_name: str, eid, services: dict, run_id: str = "") -> None:
    """Record the pre-update image state of one stack.

    services: {service_name: image_ref} - the CURRENT (pre-update) images.
    Overwrites the previous snapshot of this stack: the rollback target is
    always 'the state before the last touched update'.
    """
    with _lock:
        all_snaps = _load_all()
        all_snaps[stack_name] = {
            "eid": eid,
            "ts": time.time(),
            "run_id": run_id,
            "images": {k: v for k, v in services.items() if k and v},
        }
        _save_all(all_snaps)


def get_snapshot(stack_name: str) -> dict | None:
    """The stored pre-update snapshot of one stack, or None."""
    with _lock:
        return _load_all().get(stack_name)


def last_snapshot_time(stack_name: str) -> float | None:
    """When this stack's snapshot was taken (unix ts) or None."""
    snap = get_snapshot(stack_name)
    if not snap:
        return None
    return snap.get("ts") or None


def diff_snapshot(stack_name: str, current_images: dict) -> list:
    """Rows the rollback dialog will show: service, from (current), to
    (snapshot), plus whether the restore actually differs. Skips services
    that are identical. Returns [] when nothing would change."""
    snap = get_snapshot(stack_name)
    if not snap:
        return []
    rows = []
    for svc, old_img in (snap.get("images") or {}).items():
        cur = (current_images or {}).get(svc)
        if cur and cur != old_img:
            rows.append({"service": svc, "current": cur, "restore": old_img})
    return rows