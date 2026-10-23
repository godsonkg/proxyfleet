"""Compare two builds by node names, order, counts, group membership and details.

Node details (passwords, ports, SNI...) are compared locally, node by node, and
only the names of nodes whose details changed are ever shown.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .build import NAME_LINE_RE, PROVIDER_FILES, node_entries
from .fragments import FragmentError
from .fsutil import MARKER


class DiffError(Exception):
    pass


def load_build(path: Path) -> dict:
    try:
        manifest = json.loads((path / MARKER).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DiffError(f"{path} is not a build output (no {MARKER})") from None
    except (ValueError, UnicodeDecodeError):
        raise DiffError(f"{path / MARKER} is not valid JSON") from None
    if not isinstance(manifest, dict) or manifest.get("kind") != "build":
        raise DiffError(f"{path} is not a build output")
    contract = manifest.get("contract")
    if not isinstance(contract, dict) or not isinstance(contract.get("nodes"), dict):
        raise DiffError(f"{path / MARKER} has no node contract")
    manifest["_details"] = {client: _node_details(path, manifest, client) for client in contract["nodes"]}
    return manifest


def _node_details(path: Path, manifest: dict, client: str) -> dict[str, str]:
    """{node name: details without the name}, after checking the file against the manifest."""
    relative = f"providers/{PROVIDER_FILES[client]}"
    try:
        data = (path / relative).read_bytes()
    except FileNotFoundError:
        raise DiffError(f"{path}: {relative} is missing") from None
    if hashlib.sha256(data).hexdigest() != manifest.get("files", {}).get(relative):
        raise DiffError(f"{path}: {relative} does not match its manifest; was it edited by hand?")
    try:
        entries = node_entries(client, data.decode("utf-8"))
    except (UnicodeDecodeError, FragmentError):
        raise DiffError(f"{path}: {relative} could not be read") from None
    if client == "mihomo":
        return {name: "\n".join(l for l in block if not NAME_LINE_RE.match(l)) for name, block in entries.items()}
    return entries


def _names_diff(old: list[str], new: list[str]) -> dict:
    return {
        "added": [n for n in new if n not in old],
        "removed": [n for n in old if n not in new],
        "reordered": set(old) == set(new) and old != new,
    }


def compare(old: dict | None, new: dict) -> dict:
    """Changes from `old` to `new`. With no `old`, everything in `new` is added."""
    old_contract = (old or {}).get("contract", {})
    new_contract = new["contract"]
    clients = list(new_contract["nodes"])
    clients += [c for c in old_contract.get("nodes", {}) if c not in clients]
    result = {
        "baseline": old is not None,
        "inventory_changed": old is not None and old.get("inventory_sha256") != new.get("inventory_sha256"),
        "scope_changed": old is not None and old.get("include_observing") != new.get("include_observing"),
        "clients": {},
    }
    for client in clients:
        old_nodes = old_contract.get("nodes", {}).get(client, [])
        new_nodes = new_contract["nodes"].get(client, [])
        old_groups = old_contract.get("groups", {}).get(client, {})
        new_groups = new_contract.get("groups", {}).get(client, {})
        old_details = (old or {}).get("_details", {}).get(client, {})
        new_details = new.get("_details", {}).get(client, {})
        details_changed = [n for n in new_nodes if n in old_details and old_details[n] != new_details.get(n)]
        groups = {}
        for group in [*new_groups, *[g for g in old_groups if g not in new_groups]]:
            change = _names_diff(old_groups.get(group, []), new_groups.get(group, []))
            if group not in old_groups:
                change["status"] = "added"
            elif group not in new_groups:
                change["status"] = "removed"
            elif change["added"] or change["removed"] or change["reordered"]:
                change["status"] = "changed"
            else:
                continue
            groups[group] = change
        result["clients"][client] = {
            "count": [len(old_nodes), len(new_nodes)],
            **_names_diff(old_nodes, new_nodes),
            "details_changed": details_changed,
            "groups": groups,
        }
    result["changed"] = (result["inventory_changed"] or result["scope_changed"] or any(
        c["added"] or c["removed"] or c["reordered"] or c["details_changed"] or c["groups"]
        for c in result["clients"].values()))
    return result


def format_text(diff: dict) -> list[str]:
    lines = []
    if not diff["baseline"]:
        lines.append("no previous build; showing the new build only")
    if diff["scope_changed"]:
        lines.append("warning: one build includes observing machines and the other does not")
    if diff["inventory_changed"]:
        lines.append("inventory: changed")
    for client, change in diff["clients"].items():
        old_count, new_count = change["count"]
        lines.append(f"[{client}] {old_count} -> {new_count} nodes")
        lines += [f"  + {name}" for name in change["added"]]
        lines += [f"  - {name}" for name in change["removed"]]
        if change["reordered"]:
            lines.append("  ~ order changed")
        for name in change["details_changed"]:
            lines.append(f"  ~ {name}: details changed (e.g. password, port or SNI; not shown)")
        for group, g in change["groups"].items():
            label = {"added": "new group", "removed": "group removed", "changed": "group"}[g["status"]]
            lines.append(f"  {label} {group}")
            if g["status"] == "changed":
                lines += [f"    + {name}" for name in g["added"]]
                lines += [f"    - {name}" for name in g["removed"]]
                if g["reordered"]:
                    lines.append("    ~ order changed")
    if diff["baseline"] and not diff["changed"]:
        lines.append("no changes")
    return lines
