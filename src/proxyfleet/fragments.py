"""Read node names out of client fragments without exposing their contents.

Fragments carry full credentials, so every error here names a line number or
a node name at most, never the text of the line.
"""

from __future__ import annotations

import re

import yaml

# Expected names are "<machine>-<protocol>"; anything else is not echoed back,
# because a malformed line's "name" may really be part of a credential.
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

FILE_NAMES = {"surge": "surge.txt", "loon": "loon.txt", "mihomo": "mihomo.yaml"}
COMMENT_PREFIXES = ("#", ";", "//")


class FragmentError(Exception):
    pass


def is_safe_name(name: str) -> bool:
    return bool(SAFE_NAME_RE.match(name))


def node_names(client: str, text: str) -> list[str]:
    """Node names in fragment order. Raises FragmentError on anything unexpected."""
    names = _mihomo_names(text) if client == "mihomo" else _line_names(text)
    seen = set()
    for name in names:
        if name in seen:
            raise FragmentError(f"duplicate node name {name!r}" if is_safe_name(name) else "duplicate node name (not shown)")
        seen.add(name)
    return names


def _line_names(text: str) -> list[str]:
    names = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(COMMENT_PREFIXES):
            continue
        if stripped.startswith("["):
            raise FragmentError(f"line {number}: section headers are not allowed in a fragment")
        name, separator, _ = stripped.partition("=")
        name = name.strip()
        if not separator or not name:
            raise FragmentError(f"line {number}: expected 'Name = ...'")
        names.append(name)
    return names


def _mihomo_names(text: str) -> list[str]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        # The parser's message may quote the offending line.
        raise FragmentError("invalid YAML") from None
    if not isinstance(data, dict) or set(data) != {"proxies"}:
        raise FragmentError("expected a mapping with only 'proxies'")
    proxies = [] if data["proxies"] is None else data["proxies"]
    if not isinstance(proxies, list):
        raise FragmentError("'proxies' must be a list")
    names = []
    for index, proxy in enumerate(proxies):
        name = proxy.get("name") if isinstance(proxy, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise FragmentError(f"proxies[{index}]: missing name")
        names.append(name)
    return names
