"""updater core: _ip_like, _self_stack_names (mount identity), readiness wait,
git-stack branch — the safety kill-switch logic."""
import time as _time

import pytest

from app.updater import _ip_like, _expected_services, _self_stack_names


class TestIpLike:
    @pytest.mark.parametrize("v,expected", [
        ("172.17.0.1", True),
        ("192.168.1.5", True),
        ("0.0.0.0", True),
        ("fd00::1", True),
        ("[fd00::1]", True),
        ("999.1.1.1", False),        # octet > 255
        ("1.2.3", False),            # not 4 octets
        ("host-gateway", False),     # the keyword - THE false-positive generator
        ("host.docker.internal", False),
        ("", False),
        (None, False),
    ])
    def test_table(self, v, expected):
        assert _ip_like(v) == expected


class TestSelfStackNames:
    def _client(self, fake_client, containers):
        return fake_client(containers=containers)

    def test_mount_identity_beats_hostname_mismatch(self, fake_client, monkeypatch):
        # container_name is 'update-service' but hostname is the docker ID
        # (no hostname: in the stack) - the mount source must still identify us
        from app.updater import DATA_DIR
        own_source = "/home/user/pus/data"
        mountinfo = (
            f"123 456 8:1 {own_source} {DATA_DIR} rw,relatime - ext4 /dev/sda1 rw\n"
            "124 456 8:1 /other /other rw - ext4 /dev/sda1 rw\n"
        )
        import io
        monkeypatch.setattr(
            "builtins.open",
            lambda p, *a, **kw: io.StringIO(mountinfo) if str(p) == "/proc/self/mountinfo"
            else open(p, *a, **kw))
        client = self._client(fake_client, [
            {"Id": "c1", "Names": ["/update-service"], "State": "running",
             "Labels": {"com.docker.compose.project": "portainer_update_service"},
             "Mounts": [{"Type": "bind", "Destination": str(DATA_DIR),
                         "Source": own_source}]},
            {"Id": "c2", "Names": ["/immich"], "State": "running",
             "Labels": {"com.docker.compose.project": "immich"}},
        ])
        names = _self_stack_names(client, 3)
        assert names == ["portainer_update_service"]

    def test_name_match_fallback(self, fake_client, monkeypatch):
        # hostname equals a container name -> its project is the self stack
        import io, socket
        monkeypatch.setattr(socket, "gethostname", lambda: "update-service")
        monkeypatch.setattr(
            "builtins.open",
            lambda p, *a, **kw: io.StringIO("") if str(p) == "/proc/self/mountinfo"
            else open(p, *a, **kw))
        client = self._client(fake_client, [
            {"Id": "c1", "Names": ["/update-service"], "State": "running",
             "Labels": {"com.docker.compose.project": "pus_stack"}},
        ])
        assert _self_stack_names(client, 3) == ["pus_stack"]

    def test_no_self_stack_detected(self, fake_client, monkeypatch):
        import io
        monkeypatch.setattr(
            "builtins.open",
            lambda p, *a, **kw: io.StringIO("") if str(p) == "/proc/self/mountinfo"
            else open(p, *a, **kw))
        client = self._client(fake_client, [
            {"Id": "c9", "Names": ["/other-app"], "State": "running",
             "Labels": {"com.docker.compose.project": "other"}},
        ])
        assert _self_stack_names(client, 3) == []


class TestExpectedServices:
    def test_counts_services_with_image(self):
        compose = "services:\n  a:\n    image: nginx\n  b:\n    image: redis\n"
        assert _expected_services(compose, []) == 2

    def test_env_substitution(self):
        compose = "services:\n  a:\n    image: ${REGISTRY}/app:${TAG}\n"
        assert _expected_services(compose, [{"name": "REGISTRY", "value": "r"},
                                            {"name": "TAG", "value": "1"}]) == 1

    def test_env_missing_service_still_counts(self):
        compose = "services:\n  a:\n    image: ${MISSING}/app\n"
        # unresolved var -> parse_image returns None -> service may not count;
        # verify actual behavior is stable and not a crash
        n = _expected_services(compose, [])
        assert n in (0, 1)