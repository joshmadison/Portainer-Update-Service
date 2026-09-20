"""Collects current images from all stacks and checks registries for updates.

Hardened: repo-identity digest matching, stale-while-revalidate caching,
non-blocking reads, update-lag tracking (first-seen timestamps).
"""
import json
import re
import threading
import time

from . import compose as compose_mod
from . import dockerhub
from .config import DATA_DIR, get
from .history import current_status
from .portainer import Portainer, PortainerError

_lock = threading.Lock()
_last_result: dict = {}
_refresh_thread = None

# update-lag state: "registry/repo:tag" -> first_seen timestamp
LAG_FILE = DATA_DIR / "update_state.json"
_lag_lock = threading.Lock()


def _load_lag() -> dict:
    if LAG_FILE.exists():
        try:
            return json.loads(LAG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_lag(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LAG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(LAG_FILE)


def collect_images(client: Portainer, eid, log=None) -> list:
    """Every (stack, service, image) triple deployed on this endpoint."""
    out = []
    for stack in client.stacks():
        if stack.get("EndpointId") != eid:
            continue
        sid = stack["Id"]
        name = stack.get("Name", f"stack-{sid}")
        try:
            content = client.stack_file(sid, eid)
        except PortainerError as e:
            if log:
                log(f"[WARN] {name}: compose file unavailable ({e})")
            continue
        for svc, image in compose_mod.services_images(content).items():
            out.append({
                "stack_id": sid, "stack": name,
                "service": svc, "image": image,
                "env": stack.get("Env") or [],
            })
    return out


def local_digests(client: Portainer, eid) -> dict:
    """container Image ref -> RepoDigests (all containers, incl. stopped)."""
    digests = {}
    try:
        cs = client.all_containers(eid)
    except PortainerError:
        return digests
    for c in cs:
        img = c.get("Image")
        if not img or "${" in img:
            continue
        if img in digests:
            continue
        try:
            insp = client.image_inspect(img, eid)
            digests[img] = insp.get("RepoDigests") or []
        except PortainerError:
            continue
    return digests


def _match_local_digest(image_resolved, digests) -> str | None:
    """Find the local RepoDigest whose REPO identity matches the compose ref.
    Old logic picked RepoDigests[0] of any name-prefix match, which could take
    a digest from a completely different tag of the same image."""
    reg, repo, _tag = dockerhub.parse_image(image_resolved)
    if repo is None:
        return None
    for container_img, rds in digests.items():
        c_reg, c_repo, _ = dockerhub.parse_image(container_img.split("@")[0])
        if c_repo != repo:
            continue
        for rd in rds:
            # RepoDigests look like 'repo@sha256:...' or 'library/repo@sha256:...'
            rd_repo = rd.split("@")[0]
            if rd_repo == repo or rd_repo.endswith("/" + repo) or \
               repo == f"library/{rd_repo.rsplit('/', 1)[-1]}":
                return rd
        return rds[0] if rds else None
    return None


def run_check(client: Portainer, eid, force=False, log=None) -> dict:
    """Check all stack images for updates. Non-reentrant via checker._lock."""
    global _last_result
    if not _lock.acquire(blocking=False):
        return _last_result or {"checked_at": None, "stacks": {}, "running": True}
    try:
        started = time.time()
        items = collect_images(client, eid, log)
        digests = local_digests(client, eid)
        lag = _load_lag()

        stacks_result: dict = {}
        seen_lag_keys = set()
        for it in items:
            image = it["image"]
            env_map = {e.get("name"): (e.get("value") or "")
                       for e in (it["env"] or []) if isinstance(e, dict)}
            image_resolved = re.sub(
                r"\$\{(\w+)\}", lambda m: env_map.get(m.group(1), m.group(0)), image)
            if "${" in image_resolved:
                continue  # still unresolved, skip

            local = _match_local_digest(image_resolved, digests)
            res = dockerhub.check_update(image_resolved, local, force=force)

            # update-lag bookkeeping: track how long an update has been
            # pending; entries for images that no longer exist are pruned
            lag_key = image_resolved
            seen_lag_keys.add(lag_key)
            if res["update_available"]:
                rec = lag.get(lag_key)
                if rec is None or rec.get("newest") != res.get("latest_tag"):
                    lag[lag_key] = {"newest": res.get("latest_tag"),
                                    "first_seen": time.time()}
                lag_days = round((time.time() - lag[lag_key]["first_seen"]) / 86400, 1)
            else:
                lag.pop(lag_key, None)
                lag_days = None
            entry = stacks_result.setdefault(it["stack"], {})
            entry[it["service"]] = {
                "image": image_resolved,
                "mode": res["mode"],
                "current_tag": res["current_tag"],
                "latest_tag": res["latest_tag"],
                "remote_digest": res["remote_digest"],
                "update_available": res["update_available"],
                "note": res["note"],
                "lag_days": lag_days,
            }
        # prune lag state for images that disappeared from all stacks
        for gone in set(lag) - seen_lag_keys:
            lag.pop(gone, None)
        _save_lag(lag)

        _last_result = {
            "checked_at": time.time(),
            "duration_s": round(time.time() - started, 1),
            "stacks": stacks_result,
            "running": False,
        }
        if log:
            n = sum(1 for svcs in stacks_result.values() for v in svcs.values()
                    if v["update_available"])
            log(f"[OK] Update check done: {n} update(s) available across {len(stacks_result)} stack(s).")
        return _last_result
    finally:
        _lock.release()


def get_cached(force_refresh=None) -> dict:
    """Returns the cached snapshot immediately (non-blocking reads).
    Stale cache triggers a background refresh (single-flight via _lock).
    The VERY FIRST call (no check ever completed) runs synchronously once,
    so the UI's first stack view has real data instead of an empty table.
    Pass force_refresh=True to start an explicit background re-check."""
    global _refresh_thread
    with _lock:
        res = dict(_last_result) if _last_result else None
    ttl_min = int(get("check_cache_minutes", 30) or 30)
    stale = not (res and res.get("checked_at")
                 and time.time() - res["checked_at"] < ttl_min * 60)
    first_call = res is None
    if stale or force_refresh:
        if first_call:
            # only the first ever read blocks - one-time cost, then cache-only
            client = _new_client()
            if client:
                return run_check(client, client.endpoint_id)
        elif _lock.acquire(blocking=False):
            _lock.release()
            _refresh_thread = threading.Thread(
                target=_bg_refresh, kwargs={"force": bool(force_refresh)}, daemon=True)
            if not _refresh_thread.is_alive():
                _refresh_thread.start()
    if res is None:
        return {"checked_at": None, "stacks": {}, "running": False}
    return res


def _bg_refresh(force=False):
    client = _new_client()
    if not client:
        return
    run_check(client, client.endpoint_id, force=force)


def _new_client():
    import socket
    client = Portainer(get("portainer_url"), get("portainer_api_key"),
                       tls_verify=bool(get("tls_verify", False)))
    try:
        client.resolve_endpoint(socket.gethostname())
    except PortainerError:
        return None
    return client


def status_snapshot() -> dict:
    st = current_status()
    with _lock:
        check = dict(_last_result)
    updates = sum(1 for svcs in check.get("stacks", {}).values()
                  for v in svcs.values() if v.get("update_available"))
    return {
        "state": st.get("state", "idle"),
        "last_run": st.get("last_run"),
        "current_run": st.get("current_run"),
        "runs_total": st.get("runs_total", 0),
        "check": {"checked_at": check.get("checked_at"),
                  "duration_s": check.get("duration_s"),
                  "updates_available": updates},
        "stacks": check.get("stacks", {}),
    }