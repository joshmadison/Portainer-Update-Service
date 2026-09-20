"""The updater engine - the full deploy pipeline as Python modules.

Sections:
  preflight_checks()      -> docker reachable, portainer API, disk space, compose dir
  backup_portainer()      -> tar of Portainer /data (volume or bind mount)
  cleanup_docker()        -> image+buildcache prune ONLY (containers/networks of
                             stopped stacks are preserved - they mark user intent)
  redeploy_stacks()       -> parallel redeploy with verification + sequential retry
  update_portainer()      -> compose pull/up of Portainer itself (LAST step)
  reconciliation_sweep()  -> post-run re-verify of all stacks
  post_deployment_repairs()-> network/gateway repairs (config-toggled)

Everything is orchestrated by run_full_update() which returns a structured
result consumed by the UI. Docker is reached THROUGH the Portainer API proxy,
except prune/backups which need local docker CLI (they run via subprocess).
"""
import json
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import compose as compose_mod
from .config import get
from .jobs import gate
from .portainer import Portainer, PortainerError


# --------------------------------------------------------------------- helpers
def _cli(args, timeout=600):
    """Run a local docker CLI command (prune, backups). Returns (rc, stdout)."""
    docker = shutil.which("docker")
    if not docker:
        return 1, "docker CLI not found"
    try:
        p = subprocess.run([docker, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, "command timed out"
    except OSError as e:
        return 1, str(e)


def _df_usage(path="/"):
    try:
        p = subprocess.run(["df", path], capture_output=True, text=True, timeout=10)
        m = re.search(r"(\d+)%", p.stdout)
        return int(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def _project_name(stack_name):
    return (stack_name or "").lower()


def _containers_ready(client, project, eid, log=None):
    """Port of count_ready_containers(): 'ready|total'. Ready = healthy/running
    without healthcheck, or exited 0."""
    try:
        cs = client.containers_by_project(project, eid)
    except PortainerError as e:
        if log:
            log(f"container query failed for {project}: {e}")
        return 0, 0
    ready = total = 0
    for c in cs:
        total += 1
        state = c.get("State", "")
        status = c.get("Status", "")
        if state == "running":
            # Status string like "Up 3 hours (healthy)"
            if "(healthy)" in status or "(health: starting)" not in status:
                try:
                    insp = client.inspect_container(c["Id"], eid)
                    health = (insp.get("State", {}).get("Health") or {}).get("Status")
                except PortainerError:
                    health = None
                if health in (None, "healthy"):
                    ready += 1
        elif state == "exited":
            # Docker Status string is "Exited (0) 2 minutes ago"
            if re.search(r"Exited \(0\)", status):
                ready += 1
    return ready, total


# -------------------------------------------------------------------- preflight
def preflight_checks(client: Portainer, log) -> tuple:
    ok = True
    # Portainer API reachable?
    try:
        st = client.status()
        version = st.get("Version") or st.get("version") or "?"
        log(f"[OK] Portainer API reachable (version {version})")
    except PortainerError as e:
        log(f"[ERROR] Portainer API not reachable: {e}")
        return False, []

    # Docker daemon reachable via Portainer proxy?
    eid = client.endpoint_id
    try:
        info = client.docker_info(eid)
        log(f"[OK] Docker daemon reachable (containers: {info.get('Containers', '?')})")
    except PortainerError as e:
        log(f"[ERROR] Docker daemon not reachable via Portainer: {e}")
        ok = False

    # disk space
    usage = _df_usage("/")
    if usage is not None:
        if usage > 90:
            log(f"[WARN] Root FS at {usage}% usage!")
        else:
            log(f"[OK] Root FS usage: {usage}%")

    # Portainer compose dir (only needed for self-update)
    compose_dir = get("portainer_compose_dir", "")
    if compose_dir:
        from pathlib import Path
        if (Path(compose_dir) / "docker-compose.yml").exists():
            log("[OK] Portainer compose file found")
        else:
            log(f"[WARN] Portainer compose file not found in {compose_dir} - self-update will be skipped")
    return ok, []


# --------------------------------------------------------------------- backup
def backup_portainer(client: Portainer, log, backup_dir) -> bool:
    log("Creating Portainer backup...")
    rc, out = _cli(["ps", "-q", "--filter", "name=^portainer$"])
    cid = out.splitlines()[0].strip() if rc == 0 and out else ""
    if not cid:
        rc, out = _cli(["ps", "-q", "--filter", "ancestor=portainer/portainer-ce:latest"])
        cid = out.splitlines()[0].strip() if rc == 0 and out else ""
    if not cid:
        log("[WARN] No running Portainer container found, skipping backup.")
        return False
    rc, out = _cli(["inspect", "-f",
                    "{{range .Mounts}}{{if eq .Destination \"/data\"}}{{.Type}}|{{.Name}}{{.Source}}{{end}}{{end}}",
                    cid])
    line = out.splitlines()[0].strip() if rc == 0 and out else ""
    if "|" not in line:
        log("[WARN] Portainer has no /data mount, skipping backup.")
        return False
    mtype, source = line.split("|", 1)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest_dir = backup_dir / f"portainer_{ts}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / "portainer_data.tar.gz"
    # `-v <path>:/data` works for both named volumes and bind mounts
    rc2, out2 = _cli(["run", "--rm", "-v", f"{source}:/data", "-v", f"{dest_dir}:/backup",
                      "alpine", "tar", "czf", "/backup/portainer_data.tar.gz", "-C", "/data", "."])
    if rc2 == 0:
        log(f"[OK] Portainer backup created: {archive}")
        # rotation
        backups = sorted(backup_dir.glob("portainer_*"))
        keep = int(get("keep_backups", 5))
        for old in backups[:-keep] if len(backups) > keep else []:
            shutil.rmtree(old, ignore_errors=True)
        log(f"[OK] Backup rotation completed (keeping last {keep}).")
        return True
    log(f"[WARN] Backup failed: {out2}")
    return False


# --------------------------------------------------------------------- cleanup
def cleanup_docker(log) -> None:
    """Image + buildcache prune ONLY.

    The bash script ran `docker system prune -af`, which also deletes STOPPED
    containers and their networks - those mark 'intentionally stopped' user
    intent that redeploy_single_stack() relies on, and they hold one-off data.
    Images of stopped stacks are still protected by their containers.
    """
    log("Pruning unused images and build cache (containers/networks preserved)...")
    rc1, out1 = _cli(["image", "prune", "-af"])
    rc2, out2 = _cli(["builder", "prune", "-af"])
    if rc1 == 0 and rc2 == 0:
        reclaimed = ""
        m = re.search(r"reclaimed\s+([0-9.]+\s*\w+)", out2 or out1)
        if m:
            reclaimed = f" (reclaimed {m.group(1)})"
        log(f"[OK] Prune completed{reclaimed}")
    else:
        log(f"[WARN] Prune failed: {(out1 or out2)[:200]}")


# --------------------------------------------------------------------- stacks
def _stack_env(stack):
    return stack.get("Env") or []


def _expected_services(compose_text, env):
    """Number of services defined in a compose file, with stack env vars
    resolved (prevents stacks with variable image tags from being
    misclassified as inactive)."""
    import yaml
    env_map = {}
    for e in env or []:
        if isinstance(e, dict) and e.get("name"):
            env_map[e["name"]] = e.get("value") or ""
    # lambda replacement: env VALUES are inserted literally (a re.sub with a
    # string replacement would interpret \1 etc. inside the env value)
    resolved = re.sub(r"\$\{(\w+)\}",
                      lambda m: env_map.get(m.group(1), m.group(0)),
                      compose_text)
    try:
        data = yaml.safe_load(resolved) or {}
        return len([s for s in (data.get("services") or {}) if s])
    except yaml.YAMLError:
        return -1  # unknown -> be lenient


def _self_stack_names(client, eid) -> list:
    """Detect the Portainer stack(s) that RUN this service (if any), so the
    engine can defer them to the very end of an update run.

    Sources:
    1. explicit config: self_stack_name (recommended when deploying as a
       Portainer stack - container hostnames don't always match the project)
    2. hostname heuristic: '<project>-<service>-<idx>' container hostname
    3. compose-project candidates whose containers mount THIS app's data dir
    """
    import socket
    names = set()

    explicit = (get("self_stack_name", "") or "").strip().lower()
    if explicit:
        names.add(explicit)

    try:
        cname = socket.gethostname().lower()
    except Exception:  # noqa: BLE001
        return sorted(names)

    try:
        cs = client.all_containers(eid)
    except PortainerError:
        return sorted(names)
    candidates = set()
    for c in cs:
        lbl = ((c.get("Labels") or {}).get("com.docker.compose.project") or "").lower()
        if lbl:
            candidates.add(lbl)
            # a compose container whose project name starts the hostname
            if cname and (lbl == cname or cname.startswith(lbl + "-")):
                names.add(lbl)
    # container hostname is often '<project>-<service>-<idx>'
    project = cname.split("-")[0] if "-" in cname else ""
    if project and project in candidates:
        names.add(project)
    return sorted(names)


def redeploy_single_stack(client, stack, eid, log) -> bool:
    sid = stack["Id"]
    name = stack.get("Name", f"stack-{sid}")
    project = _project_name(name)
    log(f"Processing stack: {name} (ID: {sid})")

    # intentionally stopped? containers exist, none running
    try:
        cs = client.containers_by_project(project, eid)
        if cs and not any(c.get("State") == "running" for c in cs):
            log(f"[INFO] {name}: {len(cs)} container(s) but none running -> intentionally stopped, skipping.")
            return True
    except PortainerError as e:
        log(f"[WARN] {name}: container check failed ({e}), continuing.")

    try:
        content = client.stack_file(sid, eid)
    except PortainerError as e:
        log(f"[ERROR] Could not retrieve compose file for {name}: {e}")
        return False
    if not content:
        log(f"[ERROR] Empty compose file for {name}")
        return False

    env = _stack_env(stack)
    n_services = _expected_services(content, env)
    if n_services == 0:
        log(f"[INFO] {name}: 0 services defined -> inactive, skipping.")
        return True

    try:
        client.update_stack(sid, eid, content, env)
    except PortainerError as e:
        log(f"[ERROR] Failed to redeploy {name}: {e}")
        return False

    log(f"[INFO] {name}: HTTP 200, verifying containers...")
    deadline = time.time() + int(get("deploy_wait_time", 300))
    interval = 5
    expected = n_services
    while time.time() < deadline:
        ready, total = _containers_ready(client, project, eid, log)
        if ready >= expected and ready > 0:
            log(f"[OK] {name}: all {expected} services ready.")
            return True
        time.sleep(interval)
    ready, total = _containers_ready(client, project, eid, log)
    log(f"[ERROR] {name}: only {ready}/{expected} services ready after wait ({total} containers).")
    return False


def redeploy_stacks(client, eid, stacks, log, max_parallel=None) -> tuple:
    """Parallel redeploy + sequential retry. Returns (ok, failed_names).

    Stacks matching self_stack_names are DEFERRED to the very end of the run
    (self-update protection): redeploying the running service itself would
    kill this process mid-verification. Returns (ok, failed, deferred_names).
    """
    max_parallel = max_parallel or int(get("max_parallel_deploys", 3))
    if not stacks:
        log("[ERROR] No stacks found on this endpoint.")
        return False, [], []

    self_names = set(_self_stack_names(client, eid))
    parallel = [s for s in stacks if (s.get("Name") or "") not in self_names]
    deferred = [s for s in stacks if (s.get("Name") or "") in self_names]
    if deferred:
        log(f"[INFO] Deferring self-stack redeploy to end of run: "
            f"{', '.join(s.get('Name') for s in deferred)}")
    log(f"Found {len(stacks)} stacks. Deploying {len(parallel)} now "
        f"(max {max_parallel} parallel)...")

    failed = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futs = {pool.submit(redeploy_single_stack, client, s, eid, log): s for s in parallel}
        for f in as_completed(futs):
            s = futs[f]
            try:
                if not f.result():
                    failed.append(s.get("Name", str(s.get("Id"))))
            except Exception as e:  # noqa: BLE001 - defensive: a job must never kill the run
                failed.append(s.get("Name", str(s.get("Id"))))
                log(f"[ERROR] stack {s.get('Name')} crashed: {e}")

    if failed:
        log(f"[WARN] {len(failed)} stack(s) failed in parallel pass, retrying sequentially: {', '.join(failed)}")
        still = []
        for name in failed:
            stack = next((s for s in stacks if s.get("Name") == name), None)
            if stack and not redeploy_single_stack(client, stack, eid, log):
                still.append(name)
        if still:
            log(f"[ERROR] Stacks failed permanently: {', '.join(still)}")
            return False, still, [s.get("Name") for s in deferred]
    log(f"[OK] All {len(parallel)} stacks deployed successfully.")
    return True, [], [s.get("Name") for s in deferred]


# ------------------------------------------------------------ portainer update
def wait_portainer_ready(client, log, timeout_s=90) -> bool:
    """After a Portainer self-update the API can take a while to come back.
    The reconciliation sweep must not run against a restarting API."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            client.status()
            log("[OK] Portainer API is back up.")
            return True
        except PortainerError:
            time.sleep(3)
    log(f"[ERROR] Portainer API not back after {timeout_s}s.")
    return False


def update_portainer(log) -> bool:
    compose_dir = get("portainer_compose_dir", "")
    if not compose_dir:
        log("[INFO] portainer_compose_dir not configured - skipping Portainer self-update.")
        return True
    from pathlib import Path
    if not (Path(compose_dir) / "docker-compose.yml").exists():
        log(f"[WARN] No docker-compose.yml in {compose_dir} - skipping self-update.")
        return True
    docker = shutil.which("docker")
    if not docker:
        log("[ERROR] docker CLI not found - cannot self-update Portainer.")
        return False
    try:
        p = subprocess.run([docker, "compose", "pull"],
                           capture_output=True, text=True, timeout=900, cwd=compose_dir)
        p2 = subprocess.run([docker, "compose", "up", "-d"],
                            capture_output=True, text=True, timeout=900, cwd=compose_dir)
        out = (p.stdout + p.stderr + p2.stdout + p2.stderr).strip()
        rc_ok = p.returncode == 0 and p2.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log(f"[ERROR] Portainer self-update failed: {e}")
        return False
    if not rc_ok:
        log(f"[ERROR] Portainer self-update failed: {out[:300]}")
        return False
    time.sleep(5)
    rc3, out3 = _cli(["ps", "-q", "--filter", "name=^portainer$", "--filter", "status=running"])
    if rc3 == 0 and out3:
        log("[OK] Portainer self-update completed.")
        return True
    log("[ERROR] compose up reported success but portainer is not running.")
    return False


# ---------------------------------------------------------- reconciliation
def reconciliation_sweep(client, eid, stacks, log) -> bool:
    log("Reconciliation sweep: verifying all stacks after update...")
    broken = []
    for stack in stacks:
        name = stack.get("Name", "?")
        project = _project_name(name)
        try:
            cs = client.containers_by_project(project, eid)
        except PortainerError as e:
            log(f"[ERROR] Sweep: {name}: query failed ({e})")
            broken.append(stack)  # treat as broken candidates instead of skipping
            continue
        if not cs:
            log(f"[INFO] Sweep: {name} has no containers, skipping.")
            continue
        if not any(c.get("State") == "running" for c in cs):
            log(f"[INFO] Sweep: {name} intentionally stopped, OK.")
            continue
        ready, total = _containers_ready(client, project, eid, log)
        if ready == 0:
            log(f"[ERROR] Sweep: {name} has 0/{total} ready containers.")
            broken.append(stack)
        else:
            log(f"[INFO] Sweep: {name} {ready}/{total} containers ready, OK.")
    if not broken:
        log("[OK] Reconciliation sweep: all stacks healthy.")
        return True
    log(f"[WARN] Sweep: {len(broken)} stack(s) broken, redeploying sequentially...")
    ok_all = True
    for stack in broken:
        if not redeploy_single_stack(client, stack, eid, log):
            log(f"[ERROR] Sweep: {stack.get('Name')} redeploy FAILED.")
            ok_all = False
    return ok_all


# ------------------------------------------------- network integrity repairs
def _repair_stale_gateways(client, eid, log) -> bool:
    """GENERIC repair (no config needed): extra_hosts 'host.docker.internal' /
    'host-gateway' entries are resolved at container CREATION time and baked
    into /etc/hosts. If the container's network was recreated afterwards (e.g.
    by a prune), the running container keeps dialing the OLD gateway IP ->
    connection failures to host-published targets. Detection: compare every
    running container's gateway mapping against its CURRENT network gateways.
    Fix: redeploy the owning compose stack (re-evaluates host-gateway)."""
    rc, out = _cli(["ps", "-q"])
    if rc != 0 or not out.strip():
        log("[INFO] no running containers, skipping gateway check.")
        return True
    ids = out.split()
    rc, out = _cli(["inspect", "--format",
                    "{{.Name}}|{{index .Config.Labels \"com.docker.compose.project\"}}|"
                    "{{range $k, $v := .NetworkSettings.Networks}}{{$v.Gateway}} {{end}}|"
                    "{{range .Config.ExtraHosts}}{{.}} {{end}}", *ids])
    if rc != 0:
        log(f"[WARN] container inspect failed, skipping gateway check: {out[:150]}")
        return True

    ok = True
    for line in out.splitlines():
        parts = (line.split("|") + ["", "", "", ""])[:4]
        cname, project, gw_str, hosts_str = parts
        cname = cname.lstrip("/")
        gateways = [g for g in gw_str.split() if g]
        # extra_hosts entries look like "host.docker.internal:192.168.1.5"
        mapped = []
        for h in hosts_str.split():
            if ":" in h:
                alias, ip = h.split(":", 1)
                if alias in ("host.docker.internal", "host-gateway"):
                    mapped.append(ip)
        if not mapped or not gateways:
            continue
        stale = [ip for ip in mapped if ip not in gateways]
        if not stale:
            log(f"[OK] {cname}: host-gateway mapping ({mapped[0]}) matches a current gateway.")
            continue
        if not project:
            log(f"[WARN] {cname}: stale host-gateway ({stale[0]} vs gateways {gateways}) "
                f"but no compose project label - cannot auto-recreate. Manual recreate needed.")
            ok = False
            continue
        log(f"[WARN] {cname}: stale host-gateway ({stale[0]} vs gateways {gateways}).")
        # skip if the whole stack is intentionally stopped
        rcn, running_out = _cli(["ps", "-q", "--filter", f"label=com.docker.compose.project={project}"])
        if rcn == 0 and not running_out.strip():
            log(f"[INFO] {project} fully stopped -> skip recreate (user intent).")
            continue
        try:
            stack = next((s for s in (client.stacks() or [])
                          if (s.get("Name") or "").lower() == project
                          and s.get("EndpointId") == eid), None)
        except PortainerError as e:
            log(f"[ERROR] cannot fetch stack '{project}' for recreate: {e}")
            ok = False
            continue
        if not stack:
            log(f"[WARN] stale gateway on {cname}: compose project '{project}' has no "
                f"matching Portainer stack - recreate manually.")
            ok = False
            continue
        if redeploy_single_stack(client, stack, eid, log):
            log(f"[OK] {project} recreated with current gateway.")
        else:
            log(f"[ERROR] {project} recreate failed - manual recreate needed.")
            ok = False
    return ok


def _run_repair_rule(rule, log) -> bool:
    """One configurable DNS/connectivity repair rule. Shape (config.yaml):

        - name: endpoint repair after network prune
          when_containers: [forwarder, apache]     # all must be running
          dns_check:
            from_container: forwarder
            hostname: apache
          fix:
            connect_network: shared-net
            connect_container: apache
            restart_containers: [forwarder]

    All fields optional; a rule without dns_check applies its fix directly.
    """
    name = rule.get("name", "unnamed repair rule")
    when = rule.get("when_containers") or []
    if when:
        rc, out = _cli(["ps", "--format", "{{.Names}}"])
        present = set(out.splitlines()) if rc == 0 else set()
        missing = [c for c in when if c not in present]
        if missing:
            log(f"[INFO] repair rule '{name}': prerequisites not running "
                f"({', '.join(missing)}), skipping.")
            return True
    dns = rule.get("dns_check")
    if dns:
        src, host = dns.get("from_container"), dns.get("hostname")
        rc, _ = _cli(["exec", src, "getent", "hosts", host])
        if rc == 0:
            log(f"[OK] repair rule '{name}': {host} resolvable from {src}.")
            return True
        log(f"[WARN] repair rule '{name}': {host} NOT resolvable from {src}. Fixing...")
    fix = rule.get("fix") or {}
    if fix.get("connect_network") and fix.get("connect_container"):
        rc, out = _cli(["network", "connect", fix["connect_network"], fix["connect_container"]])
        if rc != 0:
            log(f"[ERROR] repair rule '{name}': network connect failed: {out[:120]}")
            return False
    for c in fix.get("restart_containers") or []:
        _cli(["restart", c], timeout=60)
    if dns:
        time.sleep(3)
        rc, _ = _cli(["exec", dns["from_container"], "getent", "hosts", dns["hostname"]])
        if rc == 0:
            log(f"[OK] repair rule '{name}': repaired, DNS OK.")
            return True
        log(f"[ERROR] repair rule '{name}': still failing after fix. Manual check needed.")
        return False
    log(f"[OK] repair rule '{name}': fix applied.")
    return True


def post_deployment_repairs(client, eid, log) -> bool:
    """Network integrity repairs after prune/redeploys.

    Built-in (generic, no config): stale host-gateway detection for ANY
    running container. Optional (config.yaml): user-defined DNS/connectivity
    repair rules for setups like reverse proxies fronting externally-managed
    containers. See config.example.yaml for the rule format."""
    cfg = get("repairs", {})
    if not cfg.get("enabled", True):
        log("[INFO] Network integrity repairs disabled (repairs.enabled=false).")
        return True
    ok = _repair_stale_gateways(client, eid, log)
    for rule in cfg.get("rules") or []:
        try:
            ok = _run_repair_rule(rule, log) and ok
        except Exception as e:  # noqa: BLE001 - a bad rule must not kill the run
            log(f"[ERROR] repair rule '{rule.get('name', '?')}' crashed: {e}")
            ok = False
    return ok


# ----------------------------------------------------------------- orchestrator
def run_full_update(runlog) -> bool:
    """Full update run. Caller must hold the job gate (app.jobs.gate)."""
    cfg_url = get("portainer_url")
    cfg_key = get("portainer_api_key")
    if not cfg_url or not cfg_key:
        runlog.log("[ERROR] portainer_url / portainer_api_key not configured.")
        return False
    client = Portainer(cfg_url, cfg_key,
                       endpoint_id=get("portainer_endpoint_id"),
                       tls_verify=bool(get("tls_verify", False)))
    import socket
    try:
        eid = client.resolve_endpoint(socket.gethostname())
    except PortainerError as e:
        runlog.log(f"[ERROR] {e}")
        return False
    runlog.log(f"Using endpoint {eid} ({cfg_url})")

    ok, _ = preflight_checks(client, runlog.log)
    if not ok:
        runlog.log("[ERROR] Pre-flight checks failed. Aborting.")
        return False
    runlog.step("preflight", True)

    from pathlib import Path
    ok_backup = backup_portainer(client, runlog.log, Path(get("backup_dir")))
    runlog.step("backup", ok_backup, "" if ok_backup else "backup failed/skipped - continuing")
    cleanup_docker(runlog.log)
    runlog.step("prune", True)

    try:
        stacks = [s for s in client.stacks() if s.get("EndpointId") == eid]
    except PortainerError as e:
        runlog.log(f"[ERROR] Could not fetch stacks: {e}")
        return False

    ok_stacks, failed, deferred = redeploy_stacks(client, eid, stacks, runlog.log)
    runlog.step("redeploy_stacks", ok_stacks, f"failed: {', '.join(failed)}" if failed else "")
    ok_sweep = reconciliation_sweep(client, eid, stacks, runlog.log)
    runlog.step("reconciliation_sweep", ok_sweep)
    ok_repairs = post_deployment_repairs(client, eid, runlog.log)
    runlog.step("post_deployment_repairs", ok_repairs)
    # Portainer self-update LAST: restarting the API mid-run would blind the
    # sweep. After a self-update, WAIT for the API to come back so a follow-up
    # check/sweep doesn't run against a restarting Portainer.
    ok_port = update_portainer(runlog.log)
    runlog.step("update_portainer", ok_port)
    if ok_port:
        wait_portainer_ready(client, runlog.log)

    # --- SELF-STACK UPDATE: the very last action --------------------------
    # If this service runs as a Portainer stack on this host, redeploying it
    # would terminate this process. Therefore: finalize history FIRST (the
    # run is recorded as success even though the process is about to die),
    # then redeploy the deferred self-stack(s). The new container takes over
    # on the next run.
    if deferred:
        names = ", ".join(deferred)
        runlog.log(f"[INFO] Finalizing run before self-update of: {names}")
        runlog.finish(ok_stacks and ok_port and ok_sweep and ok_repairs)
        for dname in deferred:
            stack = next((s for s in stacks if s.get("Name") == dname), None)
            if stack:
                try:
                    redeploy_single_stack(client, stack, eid, runlog.log)
                except Exception as e:  # noqa: BLE001 - process may die here
                    runlog.log(f"[ERROR] self-update of {dname} failed: {e}")

    return ok_stacks and ok_port and ok_sweep and ok_repairs