"""checker digest matching + updater.redeploy_single_stack with a fake client
+ Flask API smoke (boot behavior, auth gate, CSRF gate)."""
import json

import pytest

from app import checker
from app.dockerhub import parse_image


class TestMatchLocalDigest:
    def test_repo_identity_must_match(self):
        digests = {
            "postgres@sha256:aaa": ["postgres@sha256:aaa"],
            "library/nginx@sha256:bbb": ["library/nginx@sha256:bbb"],
        }
        # compose ref for nginx must match nginx's digest, not postgres'
        assert checker._match_local_digest(
            "nginx:1.27", digests) == "library/nginx@sha256:bbb"

    def test_wrong_tag_same_repo_takes_available(self):
        digests = {"library/nginx@sha256:ccc": ["library/nginx@sha256:ccc"]}
        assert checker._match_local_digest(
            "nginx:other", digests) == "library/nginx@sha256:ccc"

    def test_no_match_returns_none(self):
        assert checker._match_local_digest("alpine", {"postgres@sha256:a": []}) is None

    def test_unresolvable_ref(self):
        assert checker._match_local_digest("${VAR}/app", {}) is None


class TestRedeploySingleStack:
    def _stack(self, sid=94, name="my_stack"):
        return {"Id": sid, "Name": name, "EndpointId": 3, "Env": [],
                "GitConfig": None}

    def test_intentionally_stopped_skips(self, fake_client, tmp_state):
        from app.updater import redeploy_single_stack
        c = fake_client(
            containers=[{"Id": "c1", "State": "exited", "Names": ["/svc"],
                         "Labels": {"com.docker.compose.project": "my_stack"}}],
            stacks=[self._stack()])
        assert redeploy_single_stack(c, self._stack(), 3, lambda m: None) is True
        # must NOT have called update_stack
        assert not c.stack_files

    def test_file_stack_updates_content(self, fake_client, tmp_state, monkeypatch):
        from app.updater import redeploy_single_stack
        compose = "services:\n  a:\n    image: nginx:1.0\n"
        c = fake_client(
            containers=[{"Id": "c1", "State": "running", "Names": ["/a"],
                         "Labels": {"com.docker.compose.project": "my_stack"},
                         "Created": 1}],
            stacks=[self._stack()],
            stack_files={94: compose})
        # wait loop stubbed: report the new container as ready immediately
        monkeypatch.setattr(
            "app.updater._containers_ready",
            lambda client, project, eid, log=None, created_after=None: (1, 1))
        assert redeploy_single_stack(c, self._stack(), 3, lambda m: None) is True
        assert c.stack_files[94].startswith("services:")

    def test_git_stack_uses_git_redeploy(self, fake_client, tmp_state, monkeypatch):
        from app.updater import redeploy_single_stack
        stack = self._stack()
        stack["GitConfig"] = {"URL": "https://github.com/x/y.git"}
        compose = "services:\n  a:\n    image: nginx:1.0\n"
        c = fake_client(
            containers=[{"Id": "c1", "State": "running", "Names": ["/a"],
                         "Labels": {"com.docker.compose.project": "my_stack"},
                         "Created": 1}],
            stacks=[stack],
            stack_files={94: compose})
        monkeypatch.setattr(
            "app.updater._containers_ready",
            lambda client, project, eid, log=None, created_after=None: (1, 1))
        assert redeploy_single_stack(c, stack, 3, lambda m: None) is True
        # the git path must have been taken, not the StackFileContent PUT
        assert getattr(c, "git_redeployed", None) == 94


def _time_create_after():
    return 1_700_000_000.0


class TestApiSmoke:
    @pytest.fixture()
    def client(self, tmp_state):
        from app.main import app
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c

    def test_healthz(self, client, monkeypatch):
        # scheduler isn't running in tests - fake a fresh heartbeat
        import time as t
        monkeypatch.setattr("app.main.scheduler.last_tick", t.time(), raising=False)
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_index_served(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"Portainer Update Service" in r.data

    def test_status_shape(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.get_json()
        for key in ("state", "configured", "scheduler", "runs_total"):
            assert key in body

    def test_csrf_cross_origin_rejected(self, client):
        r = client.post("/api/update", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_csrf_same_origin_allowed(self, client):
        r = client.post("/api/update", headers={"Origin": "http://localhost"})
        assert r.status_code != 403  # (may be 409 busy or ok - not rejected as cross-site)

    def test_auth_gate_with_token(self, client, monkeypatch):
        from app import config as cfg
        monkeypatch.setattr("app.config._cfg", None)
        monkeypatch.setenv("PUS_AUTH_TOKEN", "secret123")
        cfg.load(force=True)
        # no token -> 401
        r = client.post("/api/update")
        assert r.status_code == 401
        # wrong token -> 401
        r = client.post("/api/update", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        # correct token -> past auth (409 busy or success)
        r = client.post("/api/update", headers={"Authorization": "Bearer secret123"})
        assert r.status_code != 401
        # reads are never gated
        r = client.get("/api/status")
        assert r.status_code == 200

    def test_run_log_path_traversal_blocked(self, client):
        r = client.get("/api/runs/..%2f..%2fetc%2fpasswd/log")
        assert r.status_code in (400, 404)  # never 200