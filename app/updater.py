"""The updater engine - the full deploy pipeline as Python modules.

Sections:
  preflight_checks()      -> docker reachable, portainer API, disk space, compose dir
  backup_portainer()      -> tar of Portainer /data (volume or bind mount)
  cleanup_docker()        -> image+buildcache prune ONLY (containers/networks of
                             stopped stacks are preserved - they mark user intent)
  redeploy_stacks()       -> parallel redeploy with verification + sequential retry
                             (excludes this app's own stack - see self_update_run())
  self_update_run()       -> deliberate 'Update PUS' redeploy of this app's stack
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
from .config import DATA_DIR, get
from .jobs import gate
from .portainer import Portainer, PortainerError


# --------------------------------------------------------------------- helpers
def _run_cmd(args, timeout=600, cwd=None):
    """Run a local CLI command, capturing output robustly.

    text=True decodes with the process locale - docker/compose output with
    non-UTF8 bytes can raise UnicodeDecodeError (an uncaught ValueError).
    errors='replace' + a broad except guarantee: a CLI failure is ALWAYS a
    returncode, never an exception escaping the caller."""
    docker = shutil.which(args[0])
    if not docker:
        return 1, f"{args[0]} CLI not found"
    try:
        p = subprocess.run([docker, *args[1:]], capture_output=True, text=True,
                           errors="replace", timeout=timeout, cwd=cwd)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, "command timed out"
    except (OSError, ValueError) as e:
        return 1, str(e)


def _cli(args, timeout=600):
    """Run a local docker CLI command (prune, backups). Returns (rc, stdout)."""
    return _run_cmd(["docker", *args], timeout=timeout)


def _df_usage(path="/"):
    try:
        p = subprocess.run(["df", path], capture_output=True, text=True, timeout=10)
        m = re.search(r"(\d+)%", p.stdout)
        return int(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def _hostname() -> str:
    import socket
    return socket.gethostname()


def _project_name(stack_name):
    return (stack_name or "").lower()


def _containers_ready(client, project, eid, log=None, created_after=None):
    """Port of count_ready_containers(): 'ready|total'. Ready = healthy/running
    without healthcheck, or exited 0.

    created_after: unix ts. When set, only containers CREATED after this
    timestamp count - this distinguishes a REAL redeploy (new containers)
    from the old, still-running set (which made the wait loop pass in <1s
    while the actual pull/deploy was still running)."""
    try:
        cs = client.containers_by_project(project, eid)
    except PortainerError as e:
        if log:
            log(f"container query failed for {project}: {e}")
        return 0, 0
    ready = total = 0
    for c in cs:
        if created_after is not None and (c.get("Created") or 0) <= created_after:
            continue  # old container from before the redeploy
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
    from pathlib import Path
    compose_dir = "/host-portainer"
    if not Path(compose_dir).exists():
        log(f"[INFO] {compose_dir} not mounted - Portainer self-update "
            f"unavailable. Add the volume mount to the stack if wanted.")
    elif (Path(compose_dir) / "docker-compose.yml").exists():
        log("[OK] Portainer compose file found")
    else:
        log(f"[WARN] no docker-compose.yml in {compose_dir} - check the "
            f"mount points at Portainer's compose dir")
    return ok, []


# --------------------------------------------------------------------- backup
def backup_portainer(client: Portainer, log, backup_dir) -> bool:
    """Portainer datastore backup via the Portainer API (/api/backup).
    Admin-key required. Replaces the old alpine-tar-of-/data approach -
    the API backup restores cleanly via /api/restore and needs no
    ephemeral container."""
    log("Creating Portainer backup (via Portainer API)...")
    try:
        data = client.backup()
    except PortainerError as e:
        log(f"[WARN] Portainer API backup failed: {e}")
        return False
    if not data or len(data) < 100:
        log("[WARN] Portainer API backup returned empty data, skipping.")
        return False
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest_dir = backup_dir / f"portainer_{ts}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / "portainer_data.tar.gz"
    archive.write_bytes(data)
    log(f"[OK] Portainer backup created: {archive}")
    # rotation
    backups = sorted(backup_dir.glob("portainer_*"))
    keep = int(get("keep_backups", 5))
    for old in backups[:-keep] if len(backups) > keep else []:
        shutil.rmtree(old, ignore_errors=True)
    log(f"[OK] Backup rotation completed (keeping last {keep}).")
    return True


# --------------------------------------------------------------------- cleanup
def cleanup_docker(client: Portainer, eid, log) -> None:
    """Image + buildcache prune ONLY - via the Portainer API (admin key).

    The bash script ran `docker system prune -af`, which also deletes STOPPED
    containers and their networks - those mark 'intentionally stopped' user
    intent that redeploy_single_stack() relies on, and they hold one-off data.
    Images of stopped stacks are still protected by their containers.
    Portainer API: POST /images/prune (dangling=false) + POST /build/prune
    (all=true). NOTE: both require an ADMIN Portainer API key.
    """
    log("Pruning unused images and build cache (via Portainer API)...")
    try:
        img = client.prune_images(eid)
    except PortainerError as e:
        log(f"[WARN] Image prune failed: {e}")
        log("[INFO] Prune routes are admin-only - use an admin Portainer API key.")
        return
    reclaimed_img = img.get("SpaceReclaimed") or 0
    try:
        bld = client.prune_build_cache(eid)
    except PortainerError as e:
        log(f"[WARN] Build-cache prune failed: {e}")
        bld = {}
    reclaimed_bld = bld.get("SpaceReclaimed") or 0
    total_mb = (reclaimed_img + reclaimed_bld) // (1024 * 1024)
    log(f"[OK] Prune completed (reclaimed ~{total_mb} MB).")
    # persist reclaim stats for the 30-day chart
    try:
        from .history import record_prune_stats
        record_prune_stats(
            reclaimed_mb=total_mb,
            images_deleted=len(img.get("ImagesDeleted") or []),
        )
    except Exception as e:  # noqa: BLE001 - stats must never kill the run
        log(f"[WARN] prune stats not recorded: {e}")


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
    """Detect the compose project(s) that RUN this service (if any), so the
    engine can exclude them from update runs (redeploying the running
    service itself would kill this process mid-verification).

    Deterministic detection - two signals, either matches:
    1. Mount identity: this container's /app/data bind-mount host path
       (read from /proc/self/mountinfo) is compared against every
       container's inspect() Mounts - THE definitive signal, immune to
       hostname/container_name/stack-name mismatches.
    2. Name match: this container's hostname equals a container's name
       (works when the stack sets hostname: explicitly).
    """
    import socket

    # signal 1: real host source path of OUR /app/data bind mount
    own_source = None
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 4 and parts[4] == str(DATA_DIR):
                    own_source = parts[3]
                    break
    except OSError:
        pass

    names = set()
    try:
        cs = client.all_containers(eid)
    except PortainerError:
        return sorted(names)
    for c in cs:
        lbl = ((c.get("Labels") or {}).get("com.docker.compose.project") or "").lower()
        if not lbl:
            continue
        cid = c.get("Id")
        # signal 1: same /app/data bind source
        if own_source:
            try:
                info = client.inspect_container(cid, eid)
                for m in info.get("Mounts") or []:
                    if (m.get("Type") == "bind"
                            and m.get("Destination") == str(DATA_DIR)
                            and m.get("Source") == own_source):
                        names.add(lbl)
            except PortainerError:
                pass
        # signal 2: container name == our hostname
        for n in c.get("Names") or []:
            try:
                if n.lstrip("/").lower() == socket.gethostname().lower():
                    names.add(lbl)
            except OSError:
                pass
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

    # Only containers created AFTER this moment count as "the redeploy's"
    # containers - the old set would otherwise satisfy the wait instantly
    # (that made runs look "done" in 1s while the pull was still going).
    redeploy_started = time.time()
    log(f"[INFO] {name}: HTTP 200, verifying containers...")
    deadline = time.time() + int(get("deploy_wait_time", 300))
    interval = 5
    expected = n_services
    last_report = 0.0
    while time.time() < deadline:
        ready, total = _containers_ready(client, project, eid, log,
                                         created_after=redeploy_started)
        if ready >= expected and ready > 0:
            log(f"[OK] {name}: all {expected} services ready.")
            return True
        if log and time.time() - last_report >= 30:
            last_report = time.time()
            log(f"[INFO] {name}: waiting... {ready}/{expected} new container(s) ready so far")
        time.sleep(interval)
    ready, total = _containers_ready(client, project, eid, log,
                                     created_after=redeploy_started)
    log(f"[ERROR] {name}: only {ready}/{expected} services ready after wait ({total} containers).")
    return False


def redeploy_stacks(client, eid, stacks, log, max_parallel=None) -> tuple:
    """Parallel redeploy + sequential retry. Returns (ok, failed_names).

    Stacks matching self_stack_names (this app's own stack) are EXCLUDED
    entirely: redeploying the running service itself would kill this
    process mid-verification. Use /api/self_update (Settings button) to
    update it deliberately. Returns (ok, failed, excluded_names).
    """
    max_parallel = max_parallel or int(get("max_parallel_deploys", 3))
    if not stacks:
        log("[ERROR] No stacks found on this endpoint.")
        return False, [], []

    self_names = set(_self_stack_names(client, eid))
    parallel = [s for s in stacks if (s.get("Name") or "") not in self_names]
    excluded = [s.get("Name", "") for s in stacks if (s.get("Name") or "") in self_names]
    if excluded:
        log(f"[INFO] Excluding this app's own stack from the run "
            f"(use 'Update PUS' in Settings): {', '.join(excluded)}")
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
            return False, still, excluded
    log(f"[OK] All {len(parallel)} stacks deployed successfully.")
    return True, [], excluded


def self_update_run(runlog) -> bool:
    """Deliberate self-update of this app's own stack (Settings button).

    Runs OUTSIDE the normal update run's history: finalizes the caller's
    log first, then redeploys the own stack. The redeploy terminates this
    container mid-request - the new container takes over on the next
    scheduled run. History is finalized FIRST so the run is recorded."""
    client = Portainer(get("portainer_url"), get("portainer_api_key"),
                       endpoint_id=get("portainer_endpoint_id"),
                       tls_verify=bool(get("tls_verify", False)))
    try:
        eid = client.resolve_endpoint(_hostname())
    except PortainerError as e:
        runlog.log(f"[ERROR] {e}")
        runlog.finish(False)
        return False
    self_names = set(_self_stack_names(client, eid))
    if not self_names:
        runlog.log("[ERROR] Could not identify this app's own stack - "
                   "is this app really deployed as a Portainer stack?")
        runlog.finish(False)
        return False
    try:
        stacks = [s for s in client.stacks() if s.get("EndpointId") == eid]
    except PortainerError as e:
        runlog.log(f"[ERROR] Could not fetch stacks: {e}")
        runlog.finish(False)
        return False
    own = next((s for s in stacks if (s.get("Name") or "") in self_names), None)
    if not own:
        runlog.log(f"[ERROR] No stack matching self project(s) "
                   f"{', '.join(sorted(self_names))} found.")
        runlog.finish(False)
        return False
    ok = redeploy_single_stack(client, own, eid, runlog.log)
    runlog.finish(ok)
    # if we get here the redeploy failed or completed before the container
    # was replaced; either way the caller records the result
    return ok


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


def _update_portainer_compose_cli(compose_dir: str, log) -> bool:
    """Self-update for Portainer, which runs as a PLAIN compose project
    (never a Portainer stack - it manages the other stacks, it can't be
    its own client). `docker compose pull && up -d` in the mounted compose
    dir, docker socket required. Read-only dir mount is sufficient: compose
    only reads the yml, the daemon does the rest."""
    log("Portainer self-update: compose-CLI mode (plain compose project)...")
    if not (Path(compose_dir) / "docker-compose.yml").exists():
        log(f"[ERROR] no docker-compose.yml in {compose_dir} - cannot "
            f"self-update. Check the mount.")
        return False
    try:
        p = subprocess.run(["docker", "compose", "pull"], capture_output=True,
                           text=True, errors="replace", timeout=900, cwd=compose_dir)
        up = subprocess.run(["docker", "compose", "up", "-d"], capture_output=True,
                            text=True, errors="replace", timeout=900, cwd=compose_dir)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log(f"[ERROR] Portainer self-update failed: {e}")
        return False
    out = (p.stdout + p.stderr + up.stdout + up.stderr).strip()
    if p.returncode != 0 or up.returncode != 0:
        log(f"[ERROR] Portainer self-update failed: {out[:300]}")
        return False
    # compose up returned - Portainer restarts: wait for the API
    time.sleep(5)
    rc, out3 = _cli(["ps", "-q", "--filter", "name=^portainer$",
                     "--filter", "status=running"])
    if rc == 0 and out3:
        log("[OK] Portainer self-update completed.")
        return True
    log("[ERROR] compose up reported success but Portainer is not running.")
    return False


def update_portainer(client, eid, log) -> bool:
    """Update Portainer itself (if include_portainer=true).

    Portainer runs outside Portainer (plain compose), so self-update runs
    `docker compose pull && docker compose up -d` inside Portainer's own
    compose dir, mounted read-only at /host-portainer, along with the
    docker socket.
    """
    if not get("include_portainer", False):
        log("[INFO] Portainer self-update disabled (include_portainer=false).")
        return True
    return _update_portainer_compose_cli("/host-portainer", log)


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
def _ip_like(value: str) -> bool:
    """True if s looks like an IPv4/IPv6 address (not a Docker keyword such
    as 'host-gateway', which Docker stores unresolved in ExtraHosts and
    re-resolves at every container start - keywords can never go stale)."""
    if not value:
        return False
    v = value.strip("[]")
    # IPv4: four dot-separated octets
    if v.count(".") == 3:
        octs = v.split(".")
        try:
            return all(0 <= int(o) <= 255 for o in octs)
        except ValueError:
            return False
    # IPv6 heuristic: contains ':' and only hex/colon chars
    return ":" in v and all(c in "0123456789abcdefABCDEF:" for c in v)


def _default_bridge_gateway(client, eid, log) -> str:
    """Gateway of the default bridge network (docker0) - the address Docker
    resolves 'host-gateway' to at container creation. This is the ONLY
    correct reference for extra_hosts host-gateway checks: comparing
    against the container's own (custom) network gateways produces false
    positives, because a custom network's gateway is subnet-specific and
    almost never equals docker0's."""
    try:
        insp = client.network_inspect("bridge", eid)
        for cfg in insp.get("IPAM", {}).get("Config") or []:
            gw = cfg.get("Gateway")
            if gw:
                return gw
    except (PortainerError, AttributeError) as e:
        log(f"[WARN] gateway check: default bridge inspect failed ({e}) - "
            f"host-gateway checks skipped.")
    return ""


def _repair_stale_gateways(client, eid, log) -> bool:
    """GENERIC repair (no config needed): extra_hosts 'host.docker.internal' /
    'host-gateway' entries are resolved at container CREATION time and baked
    into /etc/hosts - Docker resolves 'host-gateway' to the DEFAULT BRIDGE
    (docker0) gateway at that moment. It is NOT the container's own network
    gateway (custom network gateways are subnet-specific). If the docker0
    bridge was later recreated with a different subnet (daemon restart,
    address pool changes), the running container keeps dialing the OLD
    gateway IP -> connection failures to host-published targets.

    Detection: compare each running container's host-gateway mapping against
    the CURRENT default-bridge gateway (NOT the container's own networks -
    that comparison is always false for containers not on docker0).
    Fix: redeploy the owning compose stack (re-evaluates host-gateway)."""
    host_gw = _default_bridge_gateway(client, eid, log)
    if not host_gw:
        log("[INFO] no default bridge gateway - host-gateway check skipped.")
        return True
    if not shutil.which("docker"):
        # docker CLI missing (container mode): replicate the check via the
        # Portainer docker-proxy API - inspect every running container
        log("[INFO] docker CLI not found - gateway check via Portainer API...")
        try:
            cs = client.all_containers(eid)
        except PortainerError as e:
            log(f"[WARN] gateway check skipped: container query failed ({e})")
            return True
        ok = True
        checked = 0
        for c in cs:
            if c.get("State") != "running":
                continue
            cid = c.get("Id")
            labels = c.get("Labels") or {}
            project = (labels.get("com.docker.compose.project") or "").lower()
            cname = (c.get("Names") or ["?"])[0].lstrip("/")
            try:
                insp = client.inspect_container(cid, eid)
            except PortainerError as e:
                log(f"[WARN] gateway check: inspect {cname} failed ({e})")
                continue
            mapped = []
            for h in (insp.get("Config", {}).get("ExtraHosts") or []):
                alias, _, ip = (h or "").partition(":")
                # only EXPLICIT IPs can go stale. The keyword 'host-gateway'
                # is stored unresolved and re-resolved at every container
                # start - it can never be stale (comparing it against any IP
                # was the false-positive generator).
                if alias in ("host.docker.internal", "host-gateway") and ip:
                    if _ip_like(ip):
                        mapped.append(ip)
            if not mapped:
                continue
            checked += 1
            stale = [ip for ip in mapped if ip != host_gw]
            if not stale:
                log(f"[OK] {cname}: host-gateway ({mapped[0]}) matches docker0 ({host_gw}).")
                continue
            if not project:
                log(f"[WARN] {cname}: stale host-gateway ({stale[0]} vs docker0 {host_gw}) "
                    f"but no compose project label - cannot auto-recreate.")
                ok = False
                continue
            log(f"[WARN] {cname}: stale host-gateway ({stale[0]} vs gateways {gateways}).")
            stack = next((s for s in (client.stacks() or [])
                          if (s.get("Name") or "").lower() == project
                          and s.get("EndpointId") == eid), None)
            if not stack:
                log(f"[WARN] stale gateway on {cname}: project '{project}' has no "
                    f"matching Portainer stack - recreate manually.")
                ok = False
                continue
            if redeploy_single_stack(client, stack, eid, log):
                log(f"[OK] {project} recreated with current gateway.")
            else:
                log(f"[ERROR] {project} recreate failed - manual recreate needed.")
                ok = False
        if not checked:
            log("[INFO] no containers with host-gateway mappings found - nothing to check.")
        return ok
    # ---- docker-CLI branch (kept for host installs with CLI available) ----
    rc, out = _cli(["ps", "-q"])
    if rc != 0:
        log(f"[WARN] docker ps failed ({out[:120]}) - gateway check skipped.")
        return True
    if not out.strip():
        log("[INFO] no running containers, skipping gateway check.")
        return True
    ids = out.split()
    # index on missing map keys errors out - guard with default
    rc, out = _cli(["inspect", "--format",
                    "{{.Name}}|{{index .Config.Labels \"com.docker.compose.project\"}}|"
                    "{{range .HostConfig.ExtraHosts}}{{.}} {{end}}", *ids])
    if rc != 0:
        log(f"[WARN] container inspect failed, skipping gateway check: {out[:150]}")
        return True

    ok = True
    for line in out.splitlines():
        parts = (line.split("|") + ["", "", ""])[:3]
        cname, project, hosts_str = parts
        cname = cname.lstrip("/")
        # extra_hosts entries look like "host.docker.internal:192.168.1.5"
        mapped = []
        for h in hosts_str.split():
            if ":" in h:
                alias, ip = h.split(":", 1)
                # only EXPLICIT IPs can go stale (keyword re-resolves at start)
                if alias in ("host.docker.internal", "host-gateway") and _ip_like(ip):
                    mapped.append(ip)
        if not mapped:
            continue
        stale = [ip for ip in mapped if ip != host_gw]
        if not stale:
            log(f"[OK] {cname}: host-gateway mapping ({mapped[0]}) matches docker0 ({host_gw}).")
            continue
        if not project:
            log(f"[WARN] {cname}: stale host-gateway ({stale[0]} vs docker0 {host_gw}) "
                f"but no compose project label - cannot auto-recreate. Manual recreate needed.")
            ok = False
            continue
        log(f"[WARN] {cname}: stale host-gateway ({stale[0]} vs docker0 {host_gw}).")
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


def _run_repair_rule(rule, client, eid, log) -> bool:
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
    Everything runs via the Portainer API (containers/exec + network connect).
    """
    name = rule.get("name", "unnamed repair rule")
    when = rule.get("when_containers") or []
    if when:
        try:
            cs = client.all_containers(eid)
        except PortainerError as e:
            log(f"[INFO] repair rule '{name}': container query failed ({e}), skipping.")
            return True
        present = {(c.get("Names") or [""])[0].lstrip("/")
                   for c in cs if c.get("State") == "running"}
        missing = [c for c in when if c not in present]
        if missing:
            log(f"[INFO] repair rule '{name}': prerequisites not running "
                f"({', '.join(missing)}), skipping.")
            return True
    dns = rule.get("dns_check")
    fix = rule.get("fix") or {}
    if dns:
        src, host = dns.get("from_container"), dns.get("hostname")
        # find the source container's id
        src_cid = None
        try:
            for c in client.all_containers(eid):
                if (c.get("Names") or [""])[0].lstrip("/") == src:
                    src_cid = c["Id"]
                    break
        except PortainerError as e:
            log(f"[ERROR] repair rule '{name}': container query failed ({e})")
            return False
        if not src_cid:
            log(f"[INFO] repair rule '{name}': source container '{src}' not found, skipping.")
            return True
        try:
            code, out = client.exec_in_container(src_cid, ["getent", "hosts", host], eid)
        except PortainerError as e:
            log(f"[WARN] repair rule '{name}': exec failed ({e}).")
            code, out = 1, ""
        if code == 0:
            log(f"[OK] repair rule '{name}': {host} resolvable from {src}.")
            return True
        log(f"[WARN] repair rule '{name}': {host} NOT resolvable from {src}. Fixing...")
    if fix.get("connect_network") and fix.get("connect_container"):
        try:
            nets = client.networks(eid)
            net_id = next((n["Id"] for n in nets if n.get("Name") == fix["connect_network"]), None)
            if not net_id:
                log(f"[ERROR] repair rule '{name}': network '{fix['connect_network']}' not found.")
                return False
            cid = None
            for c in client.all_containers(eid):
                if (c.get("Names") or [""])[0].lstrip("/") == fix["connect_container"]:
                    cid = c["Id"]
                    break
            if not cid:
                log(f"[ERROR] repair rule '{name}': container '{fix['connect_container']}' not found.")
                return False
            client.connect_network(net_id, cid, eid)
        except PortainerError as e:
            log(f"[ERROR] repair rule '{name}': network connect failed: {e}")
            return False
    for c in fix.get("restart_containers") or []:
        try:
            for cc in client.all_containers(eid):
                if (cc.get("Names") or [""])[0].lstrip("/") == c:
                    client.restart_container(cc["Id"], eid)
                    break
        except PortainerError as e:
            log(f"[WARN] repair rule '{name}': restart of {c} failed: {e}")
    if dns:
        time.sleep(3)
        try:
            code, _ = client.exec_in_container(src_cid, ["getent", "hosts", host], eid)
        except PortainerError as e:
            log(f"[ERROR] repair rule '{name}': post-fix exec failed: {e}")
            return False
        if code == 0:
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
            ok = _run_repair_rule(rule, client, eid, log) and ok
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
    cleanup_docker(client, eid, runlog.log)
    runlog.step("prune", True)

    try:
        stacks = [s for s in client.stacks() if s.get("EndpointId") == eid]
    except PortainerError as e:
        runlog.log(f"[ERROR] Could not fetch stacks: {e}")
        return False

    ok_stacks, failed, excluded = redeploy_stacks(client, eid, stacks, runlog.log)
    runlog.step("redeploy_stacks", ok_stacks, f"failed: {', '.join(failed)}" if failed else "")
    ok_sweep = reconciliation_sweep(client, eid, stacks, runlog.log)
    runlog.step("reconciliation_sweep", ok_sweep)
    ok_repairs = post_deployment_repairs(client, eid, runlog.log)
    runlog.step("post_deployment_repairs", ok_repairs)
    # Portainer self-update LAST: restarting the API mid-run would blind
    # the sweep. After a self-update, WAIT for the API to come back so a
    # follow-up check/sweep doesn't run against a restarting Portainer.
    ok_port = update_portainer(client, eid, runlog.log)
    runlog.step("update_portainer", ok_port)
    if ok_port and get("include_portainer", False):
        wait_portainer_ready(client, runlog.log)

    return ok_stacks and ok_port and ok_sweep and ok_repairs