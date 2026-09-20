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
    client = Portainer(get("portainer_url"), get("portainer_api_key"),
                       endpoint_id=get("portainer_endpoint_id"),
                       tls_verify=bool(get("tls_verify", False)))
    client.resolve_endpoint(socket.gethostname())
    return client


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
        "next_check": scheduler.next_check,
        "next_full_update": scheduler.next_full_update,
        "consecutive_failures": scheduler.consecutive_failures,
        "last_tick": scheduler.last_tick,
        "now": time.time(),
    }
    snap["configured"] = bool(get("portainer_url") and get("portainer_api_key"))
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
    """Settings 'Test connection' button: verify API + resolve endpoint."""
    try:
        client = _client()
        st = client.status()
    except PortainerError as e:
        return _err(str(e), 502)
    return jsonify({"ok": True, "version": st.get("Version") or st.get("version") or "?",
                    "endpoint_id": client.endpoint_id})


@app.route("/api/history")
def api_history():
    return jsonify(history.history())


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
@app.route("/api/stacks")
def api_stacks():
    """Cache-only read: NEVER triggers registry calls in the request thread."""
    try:
        client = _client()
        eid = client.endpoint_id
    except PortainerError as e:
        return _err(str(e), 502)
    check = checker.get_cached()  # returns snapshot immediately, refreshes in bg
    stacks = []
    for s in client.stacks():
        if s.get("EndpointId") != eid:
            continue
        name = s.get("Name")
        svc_check = (check.get("stacks") or {}).get(name, {})
        stacks.append({
            "id": s["Id"], "name": name, "status": s.get("Status"),
            "services": svc_check,
            "updates_available": sum(1 for v in svc_check.values()
                                     if v.get("update_available")),
        })
    return jsonify({"endpoint_id": eid,
                    "checked_at": check.get("checked_at"),
                    "check_running": bool(check.get("running")),
                    "stacks": stacks})


@app.route("/api/stacks/<int:stack_id>")
def api_stack_detail(stack_id):
    try:
        client = _client()
        eid = client.endpoint_id
        stacks = client.stacks()
    except PortainerError as e:
        return _err(str(e), 502)
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
    if not gate.try_acquire("check"):
        return _err("another job is running - see /api/runs/current", 409)
    try:
        try:
            client = _client()
        except PortainerError as e:
            return _err(str(e), 502)
        force = bool((request.get_json(silent=True) or {}).get("force"))
        threading.Thread(target=_check_worker, args=(client, client.endpoint_id, force),
                         daemon=True).start()
        return jsonify({"ok": True, "started": True})
    finally:
        gate.release()


def _check_worker(client, eid, force):
    try:
        checker.run_check(client, eid, force=force)
    finally:
        pass  # gate already released by api_check (check is fire-and-forget)


@app.route("/api/update", methods=["POST"])
def api_update():
    run_id = scheduler.trigger_update_async("manual")
    if not run_id:
        return _err("another job is running - see /api/runs/current", 409)
    return jsonify({"ok": True, "run_id": run_id})


@app.route("/api/prune", methods=["POST"])
def api_prune():
    if not gate.try_acquire("prune"):
        return _err("another job is running - see /api/runs/current", 409)
    try:
        runlog = RunLogger("prune", "manual")
        gate.set_runlog(runlog)
        updater.cleanup_docker(runlog.log)
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
    return jsonify(cfg)


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    body = request.get_json(silent=True) or {}
    allowed = {"portainer_url", "portainer_api_key", "portainer_endpoint_id",
               "update_interval_hours", "tls_verify", "max_parallel_deploys",
               "deploy_wait_time", "keep_backups", "portainer_compose_dir",
               "check_cache_minutes", "listen_port", "auth_token",
               "notify_webhook"}
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