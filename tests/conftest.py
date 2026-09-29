"""Shared fixtures: tmp state dirs + a fake Portainer client.

Every test runs with PUS_DATA_DIR pointed at a fresh tmp dir BEFORE the
app modules load (config.py reads the env var at import time), so tests
never touch the real data/ directory and never see each other's state.
"""
import importlib
import os
import sys
import time
from pathlib import Path

import pytest

# ensure repo root on sys.path when pytest is invoked from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def tmp_state(tmp_path, monkeypatch):
    """Fresh PUS_DATA_DIR + PUS_CONFIG_DIR + reloaded config -> isolated state.

    PUS_CONFIG_DIR is critical: without it, the developer's live
    config/config.yaml (with real credentials) would leak into every test.
    """
    state = tmp_path / "pus-state"
    conf = tmp_path / "pus-config"
    conf.mkdir()
    monkeypatch.setenv("PUS_DATA_DIR", str(state))
    monkeypatch.setenv("PUS_CONFIG_DIR", str(conf))
    # clear cached config so every test starts from defaults
    monkeypatch.setattr("app.config._cfg", None)
    import app.config as cfg
    importlib.reload(cfg)
    # re-export reloaded objects for consumers that hold module references
    import app.history as history
    importlib.reload(history)
    yield state


@pytest.fixture()
def fake_configured(monkeypatch):
    """Make _client_or_error()-style flows see a configured (dummy) Portainer:
    the fake client never actually connects, so the URL can be anything."""
    monkeypatch.setenv("PUS_PORTAINER_URL", "http://dummy.local:9000")
    monkeypatch.setenv("PUS_PORTAINER_API_KEY", "ptr_dummy")
    monkeypatch.setattr("app.config._cfg", None)
    import app.config as cfg
    importlib.reload(cfg)
    import app.history as history
    importlib.reload(history)
    yield


@pytest.fixture()
def fake_client():
    """A duck-typed stand-in for app.portainer.Portainer.

    Implements only the methods the production code calls, returning
    canned data. Used by checker/updater tests to run the real logic
    against a fake API surface.
    """

    class FakeContainer(dict):
        pass

    class FakePortainer:
        def __init__(self, containers=None, stacks=None, stack_files=None):
            self.containers = containers or []
            self.stacks_list = stacks or []
            self.stack_files = stack_files or {}
            self.endpoint_id = 3

        def all_containers(self, eid=None):
            return self.containers

        def containers_by_project(self, project, eid=None):
            return [c for c in self.containers
                    if (c.get("Labels") or {}).get(
                        "com.docker.compose.project") == project]

        def stacks(self):
            return self.stacks_list

        def stack_file(self, stack_id, eid=None):
            return self.stack_files.get(stack_id, "")

        def status(self):
            return {"Version": "2.45.1", "InstanceID": "x"}

        def docker_info(self, eid=None):
            return {"Containers": len(self.containers)}

        def inspect_container(self, cid, eid=None):
            for c in self.containers:
                if c.get("Id") == cid:
                    return c
            return {}

        def update_stack(self, stack_id, eid, content, env):
            self.stack_files[stack_id] = content

        def redeploy_git_stack(self, stack_id, eid, env=None, **kw):
            self.git_redeployed = stack_id

        def images(self, eid=None):
            return []

        def networks(self, eid=None):
            return []

        def backup(self):
            return b""

    return FakePortainer


@pytest.fixture()
def frozen_time(monkeypatch):
    """Freeze time.time() at a fixed point; returns the instant."""
    instant = 1_700_000_000.0
    monkeypatch.setattr(time, "time", lambda: instant)
    return instant