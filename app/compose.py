"""Minimal compose-file helpers: list service images, edit one image line
while preserving the rest of the file byte-for-byte (comments, ordering).
"""
import re

import yaml


def services_images(compose_text: str) -> dict:
    """service name -> image ref (only services with an explicit image)."""
    try:
        data = yaml.safe_load(compose_text) or {}
    except yaml.YAMLError:
        return {}
    out = {}
    for name, svc in (data.get("services") or {}).items():
        img = (svc or {}).get("image")
        if isinstance(img, str) and img.strip():
            out[name] = img.strip()
    return out


def set_image(compose_text: str, service: str, new_image: str) -> str:
    """Rewrite only the `image:` line of one service; everything else is kept.

    Raises ValueError if the service/image line cannot be found.
    """
    lines = compose_text.splitlines()
    out = []
    in_services = False
    cur_svc = None
    svc_indent = None
    replaced = False

    for ln in lines:
        stripped = ln.strip()
        indent = len(ln) - len(ln.lstrip())

        if re.match(r"^services\s*:\s*(#.*)?$", stripped) and indent == 0:
            in_services = True
            cur_svc, svc_indent = None, None
            out.append(ln)
            continue

        if in_services:
            if not stripped or stripped.startswith("#"):
                out.append(ln)
                continue
            if indent == 0:
                in_services = False
                cur_svc, svc_indent = None, None
                out.append(ln)
                continue
            m = re.match(r"^([\w.-]+)\s*:", stripped)
            if m and (svc_indent is None or indent <= svc_indent):
                cur_svc = m.group(1)
                svc_indent = indent
                out.append(ln)
                continue
            if cur_svc and indent > svc_indent:
                im = re.match(r"^image\s*:\s*(.*?)(\s+#.*)?$", stripped)
                if im and cur_svc == service:
                    comment = im.group(2) or ""
                    out.append(" " * indent + f"image: {new_image}{comment}")
                    replaced = True
                    continue
        out.append(ln)

    if not replaced:
        raise ValueError(f"Service '{service}' or its image line not found in compose file")
    trailing = "\n" if compose_text.endswith("\n") else ""
    return "\n".join(out) + trailing