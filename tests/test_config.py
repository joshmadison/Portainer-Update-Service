"""config.validate + load/save round-trip + the env-var coercion rules (M1)."""
import os

import pytest


class TestValidate:
    def test_int_ranges(self):
        from app.config import validate
        assert validate("max_parallel_deploys", "5") == 5
        with pytest.raises(Exception, match=">= 1"):
            validate("max_parallel_deploys", 0)
        with pytest.raises(Exception, match="<= 10"):
            validate("max_parallel_deploys", 99)

    def test_schedule_time_format(self):
        from app.config import validate
        assert validate("update_schedule_time", "03:30") == "03:30"
        with pytest.raises(Exception, match="HH:MM"):
            validate("update_schedule_time", "24:00")
        with pytest.raises(Exception, match="HH:MM"):
            validate("update_schedule_time", "3:5")

    def test_urls(self):
        from app.config import validate
        assert validate("portainer_url", "https://x:9443") == "https://x:9443"
        with pytest.raises(Exception, match="http"):
            validate("portainer_url", "portainer:9443")

    def test_bools_reject_strings(self):
        from app.config import validate
        with pytest.raises(Exception, match="true/false"):
            validate("include_portainer", "yes")
        assert validate("include_portainer", True) is True

    def test_unknown_key(self):
        from app.config import validate
        with pytest.raises(Exception, match="unknown setting"):
            validate("made_up", 1)

    def test_strings_stripped(self):
        from app.config import validate
        assert validate("portainer_api_key", "  ptr_x  ") == "ptr_x"


class TestLoadEnvCoercion:
    """M1 regression: string env values must stay LITERAL strings."""

    def test_auth_token_no_stays_string(self, tmp_state, monkeypatch):
        from app import config as cfg
        monkeypatch.setenv("PUS_AUTH_TOKEN", "no")
        c = cfg.load(force=True)
        assert c["auth_token"] == "no"  # NOT False (which silently disabled auth)

    def test_numeric_key_stays_string(self, tmp_state, monkeypatch):
        from app import config as cfg
        monkeypatch.setenv("PUS_AUTH_TOKEN", "0123456789")
        c = cfg.load(force=True)
        assert c["auth_token"] == "0123456789"  # not int 123456789

    def test_hash_not_stripped_as_comment(self, tmp_state, monkeypatch):
        from app import config as cfg
        monkeypatch.setenv("PUS_AUTH_TOKEN", "abc#xyz")
        c = cfg.load(force=True)
        assert c["auth_token"] == "abc#xyz"

    def test_int_keys_still_coerce(self, tmp_state, monkeypatch):
        from app import config as cfg
        monkeypatch.setenv("PUS_LISTEN_PORT", "9090")
        c = cfg.load(force=True)
        assert c["listen_port"] == 9090

    def test_bool_keys_coerce(self, tmp_state, monkeypatch):
        from app import config as cfg
        monkeypatch.setenv("PUS_TLS_VERIFY", "true")
        c = cfg.load(force=True)
        assert c["tls_verify"] is True
        monkeypatch.setenv("PUS_TLS_VERIFY", "false")
        c = cfg.load(force=True)
        assert c["tls_verify"] is False

    def test_repairs_enabled_env(self, tmp_state, monkeypatch):
        from app import config as cfg
        monkeypatch.setenv("PUS_REPAIRS_ENABLED", "0")
        c = cfg.load(force=True)
        assert c["repairs"]["enabled"] is False
        assert c["repairs_enabled"] is False
        monkeypatch.setenv("PUS_REPAIRS_ENABLED", "true")
        c = cfg.load(force=True)
        assert c["repairs_enabled"] is True


class TestSaveLoadRoundTrip:
    def test_roundtrip(self, tmp_state):
        from app import config as cfg
        cfg.load(force=True)
        cfg.set("max_parallel_deploys", 7)
        cfg.set("portainer_api_key", "ptr_secret")
        cfg.set("repairs_enabled", False)
        cfg.save()
        cfg2 = cfg.load(force=True)
        assert cfg2["max_parallel_deploys"] == 7
        assert cfg2["portainer_api_key"] == "ptr_secret"
        assert cfg2["repairs"]["enabled"] is False
        assert cfg2["repairs_enabled"] is False

    def test_settings_post_two_pass(self, tmp_state):
        # a bad key must never leave earlier keys mutated (two-pass validation)
        from app import config as cfg
        from app.main import app
        app.config["TESTING"] = True
        client = app.test_client()
        cfg.load(force=True)
        before = cfg.load()["max_parallel_deploys"]
        r = client.post("/api/settings", json={
            "max_parallel_deploys": 5, "made_up_key": "x"})
        assert r.status_code == 400
        assert cfg.load()["max_parallel_deploys"] == before  # unchanged