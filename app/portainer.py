"""Portainer API client (HTTP only - no docker CLI required).

The Docker API is reached through Portainer's docker proxy
(/api/endpoints/<id>/docker/...) so the app works even without
local docker CLI access.
"""
import json
from urllib.parse import quote

import requests


class PortainerError(RuntimeError):
    pass


class Portainer:
    def __init__(self, base_url, api_key, endpoint_id=None, tls_verify=False):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = api_key
        self.session.verify = bool(tls_verify)
        self.endpoint_id = endpoint_id

    # ------------------------------------------------------------------ HTTP
    def _req(self, method, path, **kw):
        timeout = kw.pop("timeout", 30)
        try:
            r = self.session.request(method, self.base + path, timeout=timeout, **kw)
        except requests.RequestException as e:
            raise PortainerError(f"{method} {path} failed: {e}") from e
        if r.status_code >= 400:
            raise PortainerError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r

    # ---------------------------------------------------------------- system
    def status(self):
        return self._req("GET", "/api/status").json()

    def endpoints(self):
        return self._req("GET", "/api/endpoints").json()

    def resolve_endpoint(self, hostname: str) -> int:
        """Same strategy as the bash script: exact match -> partial -> single local."""
        if self.endpoint_id is not None:
            return self.endpoint_id
        eps = self.endpoints()
        h = (hostname or "").lower()
        for e in eps:
            if (e.get("Name") or "").lower() == h:
                self.endpoint_id = e["Id"]
                return self.endpoint_id
        for e in eps:
            if h and h in (e.get("Name") or "").lower():
                self.endpoint_id = e["Id"]
                return self.endpoint_id
        local = [e for e in eps if e.get("Type") == 1]
        if len(local) == 1:
            self.endpoint_id = local[0]["Id"]
            return self.endpoint_id
        listing = "; ".join(f"ID {e.get('Id')} '{e.get('Name')}' type={e.get('Type')}" for e in eps)
        raise PortainerError(f"Could not resolve endpoint for host '{hostname}'. Set portainer_endpoint_id. Endpoints: {listing}")

    # ---------------------------------------------------------------- stacks
    def stacks(self):
        return self._req("GET", "/api/stacks").json()

    def stack_file(self, stack_id, eid):
        r = self._req("GET", f"/api/stacks/{stack_id}/file?endpointId={eid}")
        return r.json().get("StackFileContent") or ""

    def update_stack(self, stack_id, eid, compose_content, env, prune=True, pull=True):
        payload = {
            "StackFileContent": compose_content,
            "Env": env or [],
            "Prune": prune,
            "PullImage": pull,
        }
        self._req("PUT", f"/api/stacks/{stack_id}?endpointId={eid}",
                  json=payload, timeout=300)

    # ---------------------------------------------------------- docker proxy
    def _docker(self, method, path, eid=None, **kw):
        eid = eid or self.endpoint_id
        return self._req(method, f"/api/endpoints/{eid}/docker{path}", **kw)

    def docker_info(self, eid=None):
        return self._docker("GET", "/info", eid).json()

    def containers_by_project(self, project, eid=None, all_=True):
        filters = json.dumps({"label": [f"com.docker.compose.project={project}"]})
        return self._docker(
            "GET", f"/containers/json?all={1 if all_ else 0}&filters={filters}", eid
        ).json()

    def all_containers(self, eid=None):
        return self._docker("GET", "/containers/json?all=1", eid).json()

    def inspect_container(self, cid, eid=None):
        return self._docker("GET", f"/containers/{cid}/json", eid).json()

    def image_inspect(self, image_ref, eid=None):
        ref = quote(image_ref, safe=":@")
        return self._docker("GET", f"/images/{ref}/json", eid).json()