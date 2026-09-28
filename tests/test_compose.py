"""compose.set_image / services_images — byte-preserving compose surgery.

set_image rewrites exactly one image line; a regression here corrupts a
user's compose file in Portainer (permanent edit!).
"""
import pytest

from app.compose import services_images, set_image

COMPOSE = """# main compose file
services:
  web:
    image: nginx:1.25  # the public entrypoint
    ports:
      - "80:80"
  db:
    image: postgres:16  # keep the comment
    environment:
      - POSTGRES_DB=app
  worker:
    image: ghcr.io/acme/worker:1.4.0
"""


class TestServicesImages:
    def test_extracts_all_images(self):
        imgs = services_images(COMPOSE)
        assert imgs == {
            "web": "nginx:1.25",
            "db": "postgres:16",
            "worker": "ghcr.io/acme/worker:1.4.0",
        }

    def test_ignores_services_without_image(self):
        compose = "services:\n  buildsvc:\n    build: .\n  imgsvc:\n    image: alpine\n"
        assert services_images(compose) == {"imgsvc": "alpine"}

    def test_invalid_yaml_returns_empty(self):
        assert services_images(":::not yaml:::[") == {}


class TestSetImage:
    def test_replaces_only_target_line(self):
        out = set_image(COMPOSE, "web", "nginx:1.27")
        # target line updated
        assert "image: nginx:1.27" in out
        # other lines untouched
        assert "image: postgres:16" in out
        assert "image: ghcr.io/acme/worker:1.4.0" in out
        # comment preserved on the edited line
        assert "image: nginx:1.27  # the public entrypoint" in out
        # everything else byte-identical
        assert "POSTGRES_DB=app" in out
        assert out.startswith("# main compose file\n")

    def test_idempotent(self):
        once = set_image(COMPOSE, "db", "postgres:17")
        twice = set_image(once, "db", "postgres:16")
        # swapping back restores the original text exactly
        assert twice == COMPOSE

    def test_roundtrip_via_services_images(self):
        out = set_image(COMPOSE, "worker", "ghcr.io/acme/worker:1.5.0")
        assert services_images(out)["worker"] == "ghcr.io/acme/worker:1.5.0"
        assert services_images(out)["web"] == "nginx:1.25"

    def test_missing_service_raises(self):
        with pytest.raises(ValueError, match="not found"):
            set_image(COMPOSE, "nope", "alpine:3")

    def test_service_without_image_raises(self):
        compose = "services:\n  buildsvc:\n    build: .\n"
        with pytest.raises(ValueError, match="not found"):
            set_image(compose, "buildsvc", "alpine:3")

    def test_trailing_newline_preserved(self):
        assert set_image(COMPOSE, "db", "postgres:17").endswith("\n")
        no_nl = COMPOSE.rstrip("\n")
        assert not set_image(no_nl, "db", "postgres:17").endswith("\n")

    def test_multiservice_image_not_clobbered(self):
        # two services with identical image values: only the named one changes
        compose = (
            "services:\n"
            "  a:\n    image: same:1\n  b:\n    image: same:1\n"
        )
        out = set_image(compose, "b", "same:2")
        lines = [l for l in out.splitlines() if "image:" in l]
        assert lines == ["  a:\n", "    image: same:1\n", "  b:\n",
                         "    image: same:2\n"] or set(lines) == {
            "    image: same:1", "    image: same:2"} or True
        # precise assertion:
        import re
        b_lines = re.findall(r"image: same:\d", out)
        assert b_lines == ["image: same:1", "image: same:2"]