"""dockerhub.parse_image / semver_key / _strip_variant — the update-detection brain.

These pure functions decide which services get update badges; a regression
here silently corrupts update detection everywhere.
"""
import pytest

from app.dockerhub import parse_image, semver_key, _strip_variant


class TestParseImage:
    @pytest.mark.parametrize("ref,expected", [
        # plain library image, no tag -> latest, docker.io, library/ prefix
        ("nginx", ("docker.io", "library/nginx", "latest")),
        # explicit tag
        ("nginx:1.27", ("docker.io", "library/nginx", "1.27")),
        # version + registry
        ("ghcr.io/immich-app/immich-server:v1.103.0",
         ("ghcr.io", "immich-app/immich-server", "v1.103.0")),
        # registry with port
        ("myhost.lan:5000/app/img:2.0", ("myhost.lan:5000", "app/img", "2.0")),
        # docker.io with explicit namespace (no library/ injection)
        ("acme/paperless:2.11", ("docker.io", "acme/paperless", "2.11")),
        # localhost registry
        ("localhost/foo:latest", ("localhost", "foo", "latest")),
        # digest pin
        ("nginx@sha256:abc123", ("docker.io", "library/nginx", "@sha256:abc123")),
        # digest + tag (digest wins as identity)
        ("nginx:1.27@sha256:abc", ("docker.io", "library/nginx", "@sha256:abc")),
        # env-var refs are rejected -> (None, None, None)
        ("${REGISTRY}/app:${TAG}", (None, None, None)),
        ("$IMAGE", (None, None, None)),
        ("", (None, None, None)),
        (None, (None, None, None)),
    ])
    def test_table(self, ref, expected):
        assert parse_image(ref) == expected

    def test_tag_not_confused_with_port(self):
        # the :5000 in the registry must not be parsed as a tag
        reg, repo, tag = parse_image("myhost.lan:5000/app/img")
        assert (reg, repo, tag) == ("myhost.lan:5000", "app/img", "latest")


class TestStripVariant:
    @pytest.mark.parametrize("tag,expected", [
        ("18.6-trixie", "18.6"),
        ("1.27-alpine", "1.27"),
        ("3.13-bookworm-slim", None),   # multi-dash: rsplit gives 'slim' base
        ("trixie", "trixie"),           # no dash -> returned as-is
        ("latest-trixie", None),        # not a version base
        ("1.2.3-unknownsuffix", None),  # unknown suffix -> not a variant
    ])
    def test_table(self, tag, expected):
        assert _strip_variant(tag) == expected


class TestSemverKey:
    @pytest.mark.parametrize("tag,expected", [
        ("16", (16, 0, 0, 1)),
        ("7.2", (7, 2, 0, 1)),
        ("1.2.3", (1, 2, 3, 1)),
        ("v3.1.2", (3, 1, 2, 1)),
        # prerelease ranks below release
        ("1.2.3-beta.1", (1, 2, 3, 0)),
        ("2.0.0-rc1", (2, 0, 0, 0)),
        # distro variants map to base version
        ("18.6-trixie", (18, 6, 0, 1)),
        ("1.27-alpine", (1, 27, 0, 1)),
        # non-versions -> None
        ("latest", None),
        ("main", None),
        ("trixie", None),
        ("bookworm", None),
        # major-only with suffix is not a version
        ("1-beta2", None),
        # build metadata tolerated
        ("1.2.3+build5", (1, 2, 3, 1)),
    ])
    def test_table(self, tag, expected):
        assert semver_key(tag) == expected

    def test_ordering(self):
        # release beats prerelease at same version; newer > older
        assert semver_key("1.2.3") > semver_key("1.2.3-beta.1")
        assert semver_key("1.10.0") > semver_key("1.9.0")
        assert semver_key("2.0.0") > semver_key("1.99.99")
        assert semver_key("1.27-alpine") == semver_key("1.27")