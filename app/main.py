"""Flask app: REST API + static frontend for the Portainer update service.

Hardened: shared job gate for all mutating routes, optional Bearer-token auth,
input validation, non-blocking /api/stacks, run-progress endpoints, /healthz.
"""
import re
import socket
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from . import checker, compose as compose_mod, dockerhub, history, updater
from .config import get, load, set as cfg_set, save as cfg_save, ConfigError
from .history import RunLogger
from .jobs import gate
from .portainer import Portainer, PortainerError
from .scheduler import scheduler

app = Flask(__name__, static_folder=None)
UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def _err(msg, code=400):
    return jsonify({"error": msg}), code


# ------------------------------------------------------------------- auth
MUTATING = {"POST", "PUT", "DELETE"}


@app.before_request
def _auth_gate():
    if request.method not in MUTATING:
        return None
    token = get("auth_token", "")
    if not token:
        return None  # auth disabled
    supplied = request.headers.get("Authorization", "")
    # constant-time compare to avoid timing leaks (plain == short-circuits)
    import hmac
    ok = hmac.compare_digest(supplied, f"Bearer {token}")
    if not ok:
        return _err("unauthorized - set Authorization: Bearer <auth_token>", 401)


def _client() -> Portainer:
    """Portainer client; raises ConfigError-free PortainerError on failure.
    Routes wanting a friendly 'not configured' message should check
    _configured() first."""
    client = Portainer(get("portainer_url"), get("portainer_api_key"),
                       endpoint_id=get("portainer_endpoint_id"),
                       tls_verify=bool(get("tls_verify", False)))
    client.resolve_endpoint(socket.gethostname())
    return client


def _configured() -> bool:
    return bool(get("portainer_url") and get("portainer_api_key"))


def _client_or_error():
    """Returns (client, None) or (None, error_response)."""
    if not _configured():
        return None, _err("not configured - enter Portainer URL and API key in Settings", 400)
    try:
        return _client(), None
    except PortainerError as e:
        return None, _err(str(e), 502)


# ------------------------------------------------------------------- static UI
@app.route("/")
def index():
    return send_from_directory(UI_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(UI_DIR, filename)


# ------------------------------------------------------------------- status
@app.route("/api/status")
def api_status():
    snap = checker.status_snapshot()
    snap["scheduler"] = {
        "busy": gate.is_busy(),
        "current": gate.current(),
        "interval_hours": get("update_interval_hours", 168),
        "schedule_mode": get("update_schedule_mode", "interval"),
        "schedule_time": get("update_schedule_time", "03:30"),
        "schedule_day": int(get("update_schedule_day", 0)),
        "schedule_desc": scheduler.schedule_desc(),
        "server_time_local": time.strftime("%a %d.%m. %H:%M"),
        "server_tz": time.strftime("%Z"),
        "next_check": scheduler.next_check,
        "next_full_update": scheduler.next_full_update,
        "consecutive_failures": scheduler.consecutive_failures,
        "last_tick": scheduler.last_tick,
        "now": time.time(),
    }
    snap["configured"] = _configured()
    snap["endpoint_configured"] = get("portainer_endpoint_id") is not None
    snap["auth_enabled"] = bool(get("auth_token", ""))
    return jsonify(snap)


@app.route("/healthz")
def healthz():
    """Liveness + scheduler heartbeat. 503 if the scheduler tick is stale."""
    tick = scheduler.last_tick
    stale = tick is None or time.time() - tick > 120
    return jsonify({
        "ok": not stale,
        "last_tick": tick,
        "state": history.current_status().get("state"),
        "consecutive_failures": scheduler.consecutive_failures,
    }), (200 if not stale else 503)


@app.route("/api/test", methods=["GET", "POST"])
def api_test():
    """Settings 'Test connection' button: verify API + resolve endpoint.

    Accepts optional body {"url": "...", "api_key": "..."} to test the
    FIRST-RUN connection BEFORE saving anything to config.
    """
    body = request.get_json(silent=True) or {}
    url = body.get("portainer_url") or get("portainer_url")
    key = body.get("portainer_api_key") or get("portainer_api_key")
    if not url or not key or key == "***":
        return _err("not configured - Portainer URL and API key required", 400)
    client = Portainer(url, key, tls_verify=bool(get("tls_verify", False)))
    try:
        st = client.status()
        endpoint_id = client.resolve_endpoint(socket.gethostname())
    except PortainerError as e:
        return _err(str(e), 502)
    return jsonify({"ok": True, "version": st.get("Version") or st.get("version") or "?",
                    "endpoint_id": endpoint_id})


@app.route("/api/endpoints")
def api_endpoints():
    """Scan Portainer for available endpoints (Settings 'Scan' button).
    Accepts ?url=&key= params to scan BEFORE saving credentials."""
    url = request.args.get("url") or get("portainer_url")
    key = request.args.get("key") or get("portainer_api_key")
    if not url or not key or key == "***":
        return _err("not configured - Portainer URL and API key required", 400)
    client = Portainer(url, key, tls_verify=bool(get("tls_verify", False)))
    try:
        eps = client.endpoints()
    except PortainerError as e:
        return _err(str(e), 502)
    return jsonify({"ok": True, "endpoints": [
        {"id": e.get("Id"), "name": e.get("Name"), "type": e.get("Type"),
         "url": e.get("Snapshots", [{}])[0].get("DockerURL", "") if e.get("Snapshots") else ""}
        for e in eps
    ]})


@app.route("/api/history")
def api_history():
    return jsonify({"history": history.history(),
                    "prune_stats_30d": history.prune_stats()})


@app.route("/api/runs/current")
def api_runs_current():
    """Live progress of the active run (steps + log tail)."""
    cur = gate.current()
    if not cur.get("active"):
        return jsonify({"active": False})
    tail = []
    p = history.run_log_path(cur["run_id"]) if cur.get("run_id") else None
    if p:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-15:]
        except OSError:
            pass
    return jsonify({"active": True, "run_id": cur["run_id"],
                    "holder": cur["holder"], "since": cur["since"],
                    "steps": cur["steps"], "log_tail": tail})


@app.route("/api/runs/<run_id>/log")
def api_run_log(run_id):
    p = history.run_log_path(run_id)
    if not p:
        return _err("log not found", 404)
    return app.response_class(p.read_text(encoding="utf-8", errors="replace"),
                              mimetype="text/plain")


# ------------------------------------------------------------------- stacks
# tiny TTL caches so the 15s UI polling doesn't hammer Portainer with a
# fresh TCP+TLS session on every tick (data is informational, staleness OK)
_TTL_STACKS = 10      # seconds - stack list redeploys show up within 10s
_TTL_INVENTORY = 120  # seconds - dashboard card only
_stacks_cache: dict = {}    # endpoint_id -> (ts, payload)
_inventory_cache: dict = {}  # endpoint_id -> (ts, payload)


def _cache_get(cache: dict, key, ttl: float):
    hit = cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _cache_put(cache: dict, key, payload):
    cache[key] = (time.time(), payload)
    return payload


@app.route("/api/stacks")
def api_stacks():
    """Cache-only read: NEVER triggers registry calls in the request thread."""
    client, e = _client_or_error()
    if e:
        return e
    eid = client.endpoint_id
    check = checker.get_cached()  # returns snapshot immediately, refreshes in bg
    cached = _stacks_cache.get(eid)
    if cached and time.time() - cached[0] < _TTL_STACKS:
        stacks = cached[1]
    else:
        stacks = []
        for s in client.stacks():
            if s.get("EndpointId") != eid:
                continue
            stacks.append({"Id": s["Id"], "Name": s.get("Name"),
                           "EndpointId": eid, "Status": s.get("Status")})
        _stacks_cache[eid] = (time.time(), stacks)
    out = []
    for s in stacks:
        name = s["Name"]
        svc_check = (check.get("stacks") or {}).get(name, {})
        out.append({
            "id": s["Id"], "name": name, "status": s.get("Status"),
            "services": svc_check,
            "updates_available": sum(1 for v in svc_check.values()
                                     if v.get("update_available")),
        })
    return jsonify({"endpoint_id": eid,
                    "checked_at": check.get("checked_at"),
                    "check_running": bool(check.get("running")),
                    "stacks": out})


@app.route("/api/stacks/<int:stack_id>")
def api_stack_detail(stack_id):
    client, e = _client_or_error()
    if e:
        return e
    eid = client.endpoint_id
    try:
        stacks = client.stacks()
    except PortainerError as err:
        return _err(str(err), 502)
    stack = next((s for s in stacks if s["Id"] == stack_id), None)
    if not stack:
        return _err("stack not found", 404)
    try:
        content = client.stack_file(stack_id, eid)
    except PortainerError as e:
        return _err(str(e), 502)
    images = compose_mod.services_images(content)
    check = checker.get_cached().get("stacks", {}).get(stack.get("Name"), {})
    return jsonify({
        "id": stack_id, "name": stack.get("Name"),
        "compose": content, "images": images,
        "checks": check,
    })


@app.route("/api/stacks/<int:stack_id>/image", methods=["POST"])
def api_stack_set_image(stack_id):
    """Pin a service to a specific version (or 'latest').

    Body: {"service": "...", "image": "nginx:1.27.3"}
    Edits the compose file via the Portainer API and redeploys the stack.
    """
    body = request.get_json(silent=True) or {}
    service, image = body.get("service"), (body.get("image") or "").strip()
    if not service or not re.match(r"^[A-Za-z0-9_./:@-]+$", image):
        return _err("service and valid image required")
    if not gate.try_acquire("pin-version"):
        return _err("another job is running - see /api/runs/current", 409)
    try:
        client = _client()
        eid = client.endpoint_id
        stacks = client.stacks()
        stack = next((s for s in stacks if s["Id"] == stack_id), None)
        if not stack:
            return _err("stack not found", 404)
        content = client.stack_file(stack_id, eid)
        new_content = compose_mod.set_image(content, service, image)
        client.update_stack(stack_id, eid, new_content, stack.get("Env") or [])
        return jsonify({"ok": True, "service": service, "image": image,
                        "redeployed": True})
    except (ValueError, PortainerError) as e:
        return _err(str(e), 500)
    finally:
        gate.release()


@app.route("/api/stacks/<int:stack_id>/redeploy", methods=["POST"])
def api_stack_redeploy(stack_id):
    if not gate.try_acquire("redeploy"):
        return _err("another job is running - see /api/runs/current", 409)
    runlog = None
    try:
        client = _client()
        eid = client.endpoint_id
        stack = next((s for s in client.stacks() if s["Id"] == stack_id), None)
        if not stack:
            return _err("stack not found", 404)
        runlog = RunLogger("redeploy", "manual")
        gate.set_runlog(runlog)
        ok = updater.redeploy_single_stack(client, stack, eid, runlog.log)
        runlog.finish(ok)
        return jsonify({"ok": ok, "run_id": runlog.run_id})
    except (PortainerError, KeyError) as e:
        if runlog:
            runlog.finish(False)
        return _err(str(e), 500)
    finally:
        gate.release()


# ------------------------------------------------------------------ versions
@app.route("/api/versions")
def api_versions():
    """Known tags for an image ref: /api/versions?image=nginx"""
    image = request.args.get("image", "")
    if not image or len(image) > 300:
        return _err("image query param required")
    return jsonify(dockerhub.available_versions(image))


# ------------------------------------------------------------------- actions
@app.route("/api/check", methods=["POST"])
def api_check():
    """Fire-and-forget update check. The gate is held for the DURATION of
    the check (released in the worker thread), so a full update can't start
    mid-check."""
    if not gate.try_acquire("check"):
        return _err("another job is running - see /api/runs/current", 409)
    try:
        try:
            client = _client()
        except PortainerError as e:
            return _err(str(e), 502)
        force = bool((request.get_json(silent=True) or {}).get("force"))
        started = threading.Thread(
            target=_check_worker, args=(client, client.endpoint_id, force),
            daemon=True)
        started.start()
        return jsonify({"ok": True, "started": True})
    except Exception as e:  # noqa: BLE001 - spawn failure must release the gate
        gate.release()
        return _err(f"could not start check: {e}", 500)


def _check_worker(client, eid, force):
    """Runs OUTSIDE the request context - releases the gate when done."""
    try:
        checker.run_check(client, eid, force=force)
    except Exception as e:  # noqa: BLE001 - defensive: check must never crash silently
        import sys
        print(f"[check] worker crashed: {e}", file=sys.stderr, flush=True)
    finally:
        gate.release()


@app.route("/api/update", methods=["POST"])
def api_update():
    run_id = scheduler.trigger_update_async("manual")
    if not run_id:
        return _err("another job is running - see /api/runs/current", 409)
    return jsonify({"ok": True, "run_id": run_id})


@app.route("/api/inventory")
def api_inventory():
    """Docker inventory for the dashboard: unused images (reclaimable),
    unused networks, totals. Read-only, TTL-cached (120s)."""
    client, e = _client_or_error()
    if e:
        return e
    eid = client.endpoint_id
    cached = _inventory_cache.get(eid)
    if cached and time.time() - cached[0] < _TTL_INVENTORY:
        return jsonify(cached[1])
    try:
        imgs = client.images(eid)
        cs = client.all_containers(eid)
        nets = client.networks(eid)
    except PortainerError as err:
        return _err(str(err), 502)
    used_ids = {c.get("ImageID") for c in cs if c.get("ImageID")}
    unused = [i for i in imgs if i.get("Id") not in used_ids]
    unused_mb = sum((i.get("Size") or 0) for i in unused) // (1024 * 1024)
    containers_per_net: dict = {}
    for c in cs:
        for nid in ((c.get("NetworkSettings", {}) or {}).get("Networks") or {}):
            containers_per_net[nid] = containers_per_net.get(nid, 0) + 1
    unused_nets = [n for n in nets if containers_per_net.get(n.get("Id"), 0) == 0]
    payload = {
        "images_total": len(imgs),
        "images_unused": len(unused),
        "images_unused_mb": unused_mb,
        "networks_total": len(nets),
        "networks_unused": len(unused_nets),
        "containers_total": len(cs),
    }
    _inventory_cache[eid] = (time.time(), payload)
    return jsonify(payload)


@app.route("/api/prune", methods=["POST"])
def api_prune():
    client, e = _client_or_error()
    if e:
        return e
    if not gate.try_acquire("prune"):
        return _err("another job is running - see /api/runs/current", 409)
    try:
        runlog = RunLogger("prune", "manual")
        gate.set_runlog(runlog)
        updater.cleanup_docker(client, client.endpoint_id, runlog.log)
        runlog.step("docker_prune", True)
        runlog.finish(True)
        return jsonify({"ok": True, "run_id": runlog.run_id})
    finally:
        gate.release()


# ------------------------------------------------------------------ settings
@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    cfg = dict(load())
    # never expose the secrets themselves
    cfg["portainer_api_key"] = "***" if cfg.get("portainer_api_key") else ""
    cfg["auth_token"] = "***" if cfg.get("auth_token") else ""
    # flat alias mirroring cfg["repairs"]["enabled"] (UI checkbox reads this)
    cfg["repairs_enabled"] = bool(cfg.get("repairs", {}).get("enabled", True))
    return jsonify(cfg)


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    body = request.get_json(silent=True) or {}
    allowed = {"portainer_url", "portainer_api_key", "portainer_endpoint_id",
               "update_interval_hours", "update_schedule_mode",
               "update_schedule_time", "update_schedule_day",
               "tls_verify", "max_parallel_deploys",
               "deploy_wait_time", "keep_backups", "portainer_compose_dir",
               "check_cache_minutes", "listen_port", "auth_token",
               "notify_webhook", "self_stack_name", "include_portainer",
               "repairs_enabled"}
    # two-pass: validate EVERYTHING first, then apply - a bad key must never
    # leave earlier keys mutated in memory (memory/disk divergence)
    from .config import validate
    validated = {}
    for k, v in body.items():
        if k not in allowed:
            return _err(f"unknown setting: {k}")
        if k == "portainer_api_key" and v == "***":
            continue
        if k == "auth_token" and v == "***":
            continue
        try:
            validated[k] = validate(k, v)  # raises ConfigError on garbage
        except ConfigError as e:
            return _err(str(e))
    for k, v in validated.items():
        cfg_set(k, v)
    cfg_save()
    return jsonify({"ok": True, "applied": sorted(validated)})


# ----------------------------------------------------------------------- main
def main():
    load()
    from .history import mark_interrupted
    mark_interrupted()  # convert stale 'running' markers from a crash
    scheduler.start()
    host = get("listen_host", "127.0.0.1")
    port = int(get("listen_port", 8090))
    # production WSGI server (waitress): graceful threads, no dev-server warning
    from waitress import serve
    print(f"[update-service] listening on http://{host}:{port}")
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()