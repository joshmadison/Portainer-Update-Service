"""Container registry update checks.

Supports docker.io and ghcr.io anonymously (token exchange), quay.io is
best-effort skipped. Comparison modes:
  - tag "latest": digest comparison
  - semver-ish tag ("1.2.3", "v1.2.3", "16", "7.2"): newest stable semver wins
  - digest-pinned refs (nginx@sha256:...): compare pinned digest vs local
Results are cached; failures get a short negative TTL to avoid poisoning.
Tokens are cached per repo to respect registry rate limits.
"""
import re
import threading
import time
import uuid

import requests

from .config import get

UA = "portainer-update-service/1.1"
HEADERS_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

_cache: dict = {}
_cache_lock = threading.Lock()
_token_cache: dict = {}
_inflight: dict = {}


def parse_image(ref: str):
    """'nginx:1.27' -> ('docker.io', 'library/nginx', '1.27').

    Digest-pinned refs (nginx@sha256:abc) return tag='@sha256:abc' and keep
    the digest separate from tag parsing.
    """
    ref = (ref or "").strip()
    if not ref or "${" in ref or "$" in ref:
        return None, None, None
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
        digest = "@" + digest
    registry, repo = "docker.io", ref
    m = re.match(r"^([^/\\]+)/(.+)$", ref)
    if m and ("." in m.group(1) or ":" in m.group(1) or m.group(1) == "localhost"):
        registry, repo = m.group(1), m.group(2)
    if registry == "docker.io" and "/" not in repo:
        repo = "library/" + repo
    tag = "latest"
    i = repo.rfind(":")
    if i > repo.rfind("/"):
        tag, repo = repo[i + 1:], repo[:i]
    if digest:
        tag = digest  # digest pins: treat digest as the tag identity
    return registry, repo, tag


def _token(registry, repo, ttl=300):
    """Fetch (and cache) an anonymous pull token for one repo."""
    key = f"{registry}/{repo}"
    now = time.time()
    with _cache_lock:
        c = _token_cache.get(key)
        if c and now - c["ts"] < ttl:
            return c["tok"]
    tok = None
    try:
        if registry == "docker.io":
            r = requests.get("https://auth.docker.io/token",
                             params={"service": "registry.docker.io",
                                     "scope": f"repository:{repo}:pull"},
                             headers={"User-Agent": UA}, timeout=10)
            if r.status_code == 200:
                tok = r.json().get("token")
        elif registry == "ghcr.io":
            r = requests.get("https://ghcr.io/token",
                             params={"scope": f"repository:{repo}:pull"},
                             headers={"User-Agent": UA}, timeout=10)
            if r.status_code == 200:
                tok = r.json().get("token")
    except requests.RequestException:
        return None
    with _cache_lock:
        _token_cache[key] = {"ts": now, "tok": tok}
    return tok


def remote_digest(registry, repo, tag):
    """Manifest/list digest for repo:tag. Returns None on any failure."""
    if tag.startswith("@"):
        tag = tag[1:]
    if registry == "docker.io":
        url = f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}"
    elif registry == "ghcr.io":
        url = f"https://ghcr.io/v2/{repo}/manifests/{tag}"
    else:
        return None
    h = {"User-Agent": UA, "Accept": HEADERS_ACCEPT}
    tok = _token(registry, repo)
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    try:
        r = requests.head(url, headers=h, timeout=15, allow_redirects=True)
        if r.status_code != 200:
            return None
        return r.headers.get("Docker-Content-Digest")
    except requests.RequestException:
        return None


def _is_prerelease(pre: str) -> bool:
    return bool(re.match(r"^(rc|pre|beta|alpha|b|a|dev|snapshot|nightly|test)", pre, re.I))


# distro suffixes that appear in tags like '18.6-trixie' / '1.27-alpine' -
# these are VARIANTS of a version, not newer versions
_DISTRO_SUFFIX = re.compile(
    r"^(alpine|bookworm|bullseye|trixie|buster|slim|alpine3\.\d+|"
    r"bookworm-otel|trixie-otel|alpine-otel|slim-otel|"
    r"alpine-perl|bookworm-perl|bullseye-perl|trixie-perl|perl|otel|"
    r"bookworm-slim|trixie-slim|alpine-slim|fpm|apache|alpine-fpm)$", re.I)


def _strip_variant(tag: str) -> str | None:
    """'18.6-trixie' -> '18.6'; returns None for pure-variant tags
    ('trixie', 'bookworm' alone are pointers to the default variant)."""
    if "-" not in tag:
        return tag
    base, suffix = tag.rsplit("-", 1)
    if not _DISTRO_SUFFIX.match(suffix):
        return None  # unknown suffix (e.g. -rc1 handling happens elsewhere)
    if not re.match(r"^[\d.]+$", base):
        return None  # 'latest-trixie' etc. - not a version
    return base


def semver_key(tag):
    """Lenient semver comparator key.

    Accepts v-prefix, 1-3 numeric components, optional prerelease/build:
      '16' -> (16,0,0,1)  '7.2' -> (7,2,0,1)  '1.2.3' -> (1,2,3,1)
      'v3.1.2' -> (3,1,2,1)  '1.2.3-beta.1' -> (1,2,3,0)  (below release)
    Distro-variant tags map to their base version: '18.6-trixie' -> (18,6,0,1),
    '1.27-alpine' -> (1,27,0,1). Pure variant pointers ('trixie', 'bookworm')
    and non-numeric tags ('latest', 'main') return None.
    """
    t = tag[1:] if tag.startswith("v") else tag
    t = _strip_variant(t) or t if "-" in t else t
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$", t)
    if not m:
        return None
    maj, mi, pat = int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)
    pre = m.group(4)
    if pre and m.group(2) is None:
        return None  # '1-beta2' / '1-alpine' style suffix on major-only, not a version
    rank = 0 if (pre and _is_prerelease(pre)) else 1
    return (maj, mi, pat, rank)


def hub_tags(repo, page_size=100, max_pages=2):
    """Docker Hub API v2 tag list - returns NEWEST-CREATED-FIRST, so 1-2
    requests cover the newest versions (the registry /tags/list endpoint is
    lexicographically ordered and buries new versions behind hundreds of
    older variant tags). No auth required. Best effort."""
    url = f"https://hub.docker.com/v2/repositories/{repo}/tags"
    tags = []
    try:
        for _ in range(max_pages):
            r = requests.get(url, params={"page_size": page_size}, timeout=20)
            if r.status_code != 200:
                break
            try:
                d = r.json()
            except ValueError:
                break
            tags.extend(t.get("name") for t in d.get("results") or [] if t.get("name"))
            nxt = d.get("next")
            if not nxt:
                break
            url = nxt
    except (requests.RequestException, ValueError):
        pass
    return tags


def newest_semver_tags(registry, repo):
    """Tags window for semver comparison: newest-first where possible."""
    if registry == "docker.io":
        tags = hub_tags(repo)
        if tags:
            return tags
    return list_tags(registry, repo)


def _abs_registry_url(registry, url):
    """Registry Link headers are RELATIVE paths ('/v2/.../tags/list?last=...').
    requests raises MissingSchema on those - absolutize them."""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    host = {"docker.io": "https://registry-1.docker.io",
            "ghcr.io": "https://ghcr.io"}[registry]
    return host + url


def list_tags(registry, repo, max_pages=None, page_size=100):
    """Fetch tags. Bounded pagination. docker.io returns tags effectively in
    LEXICOGRAPHIC order - we must scan the whole bounded window and take the
    max ourselves (early-stopping at the first newer tag misses the true
    newest). ghcr.io returns newest-first, so we can stop after page 1."""
    if max_pages is None:
        max_pages = 5
    if registry == "docker.io":
        base = f"https://registry-1.docker.io/v2/{repo}/tags/list"
    elif registry == "ghcr.io":
        base = f"https://ghcr.io/v2/{repo}/tags/list"
    else:
        return []
    h = {"User-Agent": UA}
    tok = _token(registry, repo)
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    tags, url, pages = [], base, 0
    try:
        while url and pages < max_pages:
            # params ONLY on the first request: the Link-header URL already
            # carries the pagination cursor - passing params again would
            # replace it and re-fetch page 1 forever.
            params = ({"n": page_size} if registry == "docker.io" else {"page_size": page_size}) \
                if url == base else None
            r = requests.get(url, headers=h, params=params, timeout=20)
            if r.status_code != 200:
                break
            batch = r.json().get("tags") or []
            tags.extend(batch)
            if registry == "ghcr.io" and batch:
                return tags  # ghcr is newest-first: first batch has the newest
            nxt = r.links.get("next")
            url = _abs_registry_url(registry, nxt["url"]) if nxt else None
            pages += 1
    except requests.RequestException:
        pass
    return tags


def available_versions(image_ref, force=False):
    """All known tags for an image ref, split into semver + other. Cached.
    Uses the Hub API for docker.io (newest-first -> nicer picker)."""
    registry, repo, tag = parse_image(image_ref)
    if registry is None:
        return {"current_tag": None, "semver_tags": [], "other_tags": [], "registry": None}
    key = f"tags:{registry}/{repo}"
    now = time.time()
    with _cache_lock:
        c = _cache.get(key)
        if c and not force and now - c["ts"] < c["ttl"]:
            return c["val"]
    tags = hub_tags(repo) if registry == "docker.io" else list_tags(registry, repo)
    semvers = sorted({t for t in tags if semver_key(t)},
                     key=semver_key, reverse=True)
    others = [t for t in tags if not semver_key(t)]
    val = {"registry": registry, "repo": repo, "current_tag": tag.lstrip("@"),
           "semver_tags": semvers[:60], "other_tags": others[:60]}
    # negative cache: an empty result is usually a rate limit/hiccup -> short TTL
    ttl = 60 if not tags else 3600
    with _cache_lock:
        _cache[key] = {"ts": now, "ttl": ttl, "val": val}
    return val


def check_update(image_ref, local_digest=None, force=False):
    """Decide whether an update is available for one image ref.

    Returns dict:
      mode        'digest' | 'semver' | 'pinned-digest' | 'unknown'
      current_tag / latest_tag / remote_digest / update_available / note
    Failure results are cached with a short TTL (negative cache) so one
    rate-limit blip does not freeze the view for the full TTL.
    """
    registry, repo, tag = parse_image(image_ref)
    if registry is None:
        return {"mode": "unknown", "current_tag": None, "latest_tag": None,
                "remote_digest": None, "update_available": False,
                "note": "image tag contains variables - cannot check"}
    now = time.time()
    ttl = int(get("check_cache_minutes", 30) or 30) * 60
    key = f"upd:{registry}/{repo}:{tag}"
    with _cache_lock:
        c = _cache.get(key)
        if c and not force and now - c["ts"] < c["ttl"]:
            return c["val"]

    # single-flight: concurrent misses share one registry round-trip
    ev = _inflight.get(key)
    if ev is not None and not force:
        ev.wait(timeout=120)
        with _cache_lock:
            c = _cache.get(key)
        if c:
            return c["val"]
    ev = threading.Event()
    _inflight[key] = ev
    try:
        val = _check_update_uncached(registry, repo, tag, local_digest)
        eff_ttl = ttl
        if val["mode"] == "unknown" and not val["update_available"]:
            eff_ttl = 60  # negative cache: retry failures quickly
        with _cache_lock:
            _cache[key] = {"ts": now, "ttl": eff_ttl, "val": val}
        return val
    finally:
        _inflight.pop(key, None)
        ev.set()


def _check_update_uncached(registry, repo, tag, local_digest):
    if registry not in ("docker.io", "ghcr.io"):
        return {"mode": "unknown", "current_tag": tag.lstrip("@"), "latest_tag": None,
                "remote_digest": None, "update_available": False,
                "note": f"registry '{registry}' not supported"}

    # digest-pinned refs: compare pinned digest vs local, no registry call
    if tag.startswith("@"):
        pinned = tag[1:]
        if local_digest and pinned.split(":")[-1] in local_digest:
            return {"mode": "pinned-digest", "current_tag": "sha256:" + pinned.split(":")[-1][:12],
                    "latest_tag": None, "remote_digest": pinned,
                    "update_available": False, "note": "digest-pinned, matches local"}
        return {"mode": "pinned-digest", "current_tag": "sha256:" + pinned.split(":")[-1][:12],
                "latest_tag": None, "remote_digest": pinned,
                "update_available": False,
                "note": "digest-pinned (intentionally immutable)" if not local_digest
                        else "digest differs from local image"}

    rdigest = remote_digest(registry, repo, tag)
    if rdigest is None:
        return {"mode": "unknown", "current_tag": tag, "latest_tag": None,
                "remote_digest": None, "update_available": False,
                "note": "remote digest not reachable (rate limit? private repo?)"}

    cur_key = semver_key(tag)
    if cur_key:
        tags = newest_semver_tags(registry, repo)
        newest = max((t for t in tags if semver_key(t)), key=semver_key, default=None)
        newer = bool(newest and semver_key(newest) > cur_key)
        return {"mode": "semver", "current_tag": tag, "latest_tag": newest,
                "remote_digest": rdigest, "update_available": newer,
                "note": "" if newer else "pinned version is current"}
    elif local_digest:
        return {"mode": "digest", "current_tag": tag, "latest_tag": None,
                "remote_digest": rdigest,
                "update_available": rdigest.split(":")[-1] != local_digest.split(":")[-1],
                "note": ""}
    return {"mode": "digest", "current_tag": tag, "latest_tag": None,
            "remote_digest": rdigest, "update_available": False,
            "note": "no local digest - cannot verify (image not pulled?)"}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()