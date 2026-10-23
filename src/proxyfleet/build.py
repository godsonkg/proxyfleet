"""Turn a collection into subscription files for each client.

Input is a `collect` output. Every fragment is checked again against the
collection manifest and the current inventory, nodes are renamed with the
inventory's naming templates and put in inventory order, and the result is
re-read and compared with the plan before anything replaces the output.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import __version__
from .collect import expected_names
from .fragments import FILE_NAMES, FragmentError, node_names
from .fsutil import MARKER, AtomicOutput, make_private_dir, write_private
from .inventory import Inventory
from .plan import client_groups, contract, enabled_machines, node_name
from .plan import node_names as planned_names

PROVIDER_FILES = {"surge": "surge.txt", "loon": "loon.txt", "mihomo": "mihomo.yaml"}
SNIPPET_FILES = {"surge": "surge.conf", "loon": "loon.conf", "mihomo": "mihomo.yaml"}
# Node fragments legitimately hold passwords, UUIDs and PSKs, but never these.
FORBIDDEN_MATERIAL = [
    (re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"), "PEM private key"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "GitHub token"),
]


class BuildError(Exception):
    def __init__(self, failures: list[str]):
        super().__init__("\n".join(failures))
        self.failures = failures


def _read_collection(inventory: Inventory, fragments: Path, include_observing: bool):
    try:
        manifest = json.loads((fragments / MARKER).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BuildError([f"{fragments} is not a collect output (no {MARKER})"]) from None
    except (ValueError, UnicodeDecodeError):
        raise BuildError([f"{fragments / MARKER} is not valid JSON"]) from None
    if not isinstance(manifest, dict) or manifest.get("kind") != "collect":
        raise BuildError([f"{fragments} is not a collect output"])
    if manifest.get("inventory_sha256") != inventory.sha256:
        raise BuildError(["the inventory changed since this collection; run collect again"])
    if manifest.get("include_observing") != include_observing:
        wanted = "with" if include_observing else "without"
        raise BuildError([f"this collection was not made {wanted} --include-observing"])
    return manifest


def _load_fragments(inventory, fragments, manifest, include_observing):
    """Return {client: {expected fragment name: entry}} where entry is a line rest or a dict."""
    entries = {client: {} for client in inventory.clients}
    failures = []
    recorded = manifest.get("fragments") or {}
    for machine in enabled_machines(inventory, include_observing):
        for client in inventory.clients:
            if not machine.protocols[client]:
                continue
            where = f"{machine.name} {client}"
            info = (recorded.get(machine.name) or {}).get(client)
            if not isinstance(info, dict):
                failures.append(f"{where}: not in the collection")
                continue
            path = fragments / machine.name / FILE_NAMES[client]
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                failures.append(f"{where}: fragment file missing")
                continue
            if hashlib.sha256(data).hexdigest() != info.get("sha256"):
                failures.append(f"{where}: fragment changed since collection; run collect again")
                continue
            try:
                text = data.decode("utf-8")
                for pattern, label in FORBIDDEN_MATERIAL:
                    if pattern.search(text):
                        raise FragmentError(f"contains a {label}")
                if sorted(node_names(client, text)) != sorted(expected_names(machine, client)):
                    raise FragmentError("node names differ from the inventory; run collect again")
                entries[client].update(node_entries(client, text))
            except (UnicodeDecodeError, FragmentError) as error:
                failures.append(f"{where}: {error}")
    if failures:
        raise BuildError(failures)
    return entries


ITEM_RE = re.compile(r"^(\s*)- ")
# After dedenting an item, its own keys sit after "- " or exactly two spaces;
# deeper indentation belongs to nested mappings such as ws-opts.
NAME_LINE_RE = re.compile(r"^((?:- |  (?! ))name:\s*)([\"']?)(.*?)\2\s*$")


def _mihomo_blocks(text: str) -> dict[str, list[str]]:
    """Split a block-style `proxies:` list into per-node line blocks, dedented.

    Values are kept byte-for-byte: re-serialising YAML could change how
    Mihomo (YAML 1.2) reads values such as unquoted numbers with leading zeros.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "proxies:"]
    if len(starts) != 1 or lines[starts[0]] != "proxies:":
        raise FragmentError("unsupported Mihomo layout: expected a top-level 'proxies:' line")
    body = [line for line in lines[starts[0] + 1:] if line.strip() and not line.lstrip().startswith("#")]
    if body in ([], ["  []"], ["[]"]):
        return {}
    match = ITEM_RE.match(body[0])
    if not match:
        raise FragmentError("unsupported Mihomo layout: expected block-style '- name:' items")
    indent = match.group(1)
    blocks, current = [], None
    for line in body:
        if line.startswith(indent + "- "):
            current = [line[len(indent):]]
            blocks.append(current)
        elif line.startswith(indent + "  ") and current is not None:
            current.append(line[len(indent):])
        else:
            raise FragmentError("unsupported Mihomo layout: inconsistent indentation")
    result = {}
    for block in blocks:
        names = [m for m in (NAME_LINE_RE.match(line) for line in block) if m]
        if len(names) != 1:
            raise FragmentError("unsupported Mihomo layout: each item needs exactly one plain 'name:' line")
        result[names[0].group(3)] = block
    return result


def _parse_item(block: list[str]) -> dict:
    return yaml.safe_load("proxies:\n" + "".join(f"  {line}\n" for line in block))["proxies"][0]


def _rename_mihomo_block(block: list[str], new_name: str) -> list[str]:
    """Replace only the item's own name line, then prove nothing else changed."""
    out = []
    for line in block:
        match = NAME_LINE_RE.match(line)
        if match:
            # A JSON string is a valid YAML double-quoted scalar.
            line = f"{match.group(1)}{json.dumps(new_name, ensure_ascii=False)}"
        out.append(line)
    if _parse_item(out) != {**_parse_item(block), "name": new_name}:
        raise BuildError(["internal check failed: renaming a Mihomo node changed other fields"])
    return out


def node_entries(client: str, text: str) -> dict:
    if client == "mihomo":
        blocks = _mihomo_blocks(text)
        parsed = {p["name"]: p for p in yaml.safe_load(text)["proxies"] or []}
        if set(blocks) != set(parsed):
            raise FragmentError("unsupported Mihomo layout: could not locate every node")
        return blocks
    entries = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", ";", "//")):
            name, _, rest = stripped.partition("=")
            entries[name.strip()] = rest.strip()
    return entries


def _dump_yaml(data) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1000)


def render(inventory: Inventory, entries: dict, include_observing: bool) -> dict[str, str]:
    """Return {relative path: text} for every output file."""
    files = {}
    for client in inventory.clients:
        nodes = []
        for machine in enabled_machines(inventory, include_observing):
            for protocol in machine.protocols[client]:
                source = f"{machine.name}-{protocol}"
                nodes.append((node_name(inventory, machine, client, protocol), entries[client][source]))
        groups = client_groups(inventory, client, include_observing)
        if client == "mihomo":
            items = "".join(f"  {line}\n" for name, block in nodes for line in _rename_mihomo_block(block, name))
            proxies = "proxies:\n" + (items or "  []\n")
            files[f"providers/{PROVIDER_FILES[client]}"] = proxies
            proxy_groups = [{"name": g, "type": "select", "proxies": members} for g, members in groups.items()]
            files[f"snippets/{SNIPPET_FILES[client]}"] = proxies + _dump_yaml({"proxy-groups": proxy_groups})
        else:
            lines = [f"{name} = {rest}" for name, rest in nodes]
            files[f"providers/{PROVIDER_FILES[client]}"] = "".join(f"{line}\n" for line in lines)
            sep = ", " if client == "surge" else ","
            group_lines = [f"{g} = select{sep}{sep.join(members)}" for g, members in groups.items()]
            files[f"snippets/{SNIPPET_FILES[client]}"] = (
                "[Proxy]\n" + "".join(f"{line}\n" for line in lines)
                + "\n[Proxy Group]\n" + "".join(f"{line}\n" for line in group_lines))
    return files


def _self_check(inventory: Inventory, files: dict[str, str], include_observing: bool) -> None:
    """Re-read what we are about to write and compare it with the plan."""
    failures = []
    for client in inventory.clients:
        want_nodes = planned_names(inventory, client, include_observing)
        want_groups = client_groups(inventory, client, include_observing)
        provider = files[f"providers/{PROVIDER_FILES[client]}"]
        snippet = files[f"snippets/{SNIPPET_FILES[client]}"]
        if client == "mihomo":
            got_provider = [p["name"] for p in yaml.safe_load(provider)["proxies"]]
            data = yaml.safe_load(snippet)
            got_snippet = [p["name"] for p in data["proxies"]]
            got_groups = {g["name"]: g["proxies"] for g in data["proxy-groups"] or []}
        else:
            got_provider = node_names(client, provider)
            proxy_part, _, group_part = snippet.partition("\n[Proxy Group]\n")
            got_snippet = node_names(client, proxy_part.replace("[Proxy]\n", "", 1))
            got_groups = {}
            for line in group_part.splitlines():
                name, _, rest = line.partition(" = ")
                got_groups[name] = [m.strip() for m in rest.split(",")[1:]]
        if got_provider != want_nodes or got_snippet != want_nodes:
            failures.append(f"{client}: rendered nodes differ from the plan")
        if got_groups != want_groups:
            failures.append(f"{client}: rendered groups differ from the plan")
    if failures:
        raise BuildError([f"internal check failed: {f}" for f in failures])


def build(inventory: Inventory, fragments: Path, out: Path, include_observing: bool = False) -> dict:
    collection = _read_collection(inventory, fragments, include_observing)
    entries = _load_fragments(inventory, fragments, collection, include_observing)
    files = render(inventory, entries, include_observing)
    _self_check(inventory, files, include_observing)

    manifest = {
        "proxyfleet": __version__,
        "kind": "build",
        "inventory_sha256": inventory.sha256,
        "include_observing": include_observing,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "collection": {
            "collected_at": collection.get("collected_at"),
            "manifest_sha256": hashlib.sha256((fragments / MARKER).read_bytes()).hexdigest(),
        },
        "contract": {
            **contract(inventory, include_observing),
            "nodes": {c: planned_names(inventory, c, include_observing) for c in inventory.clients},
            "groups": {c: client_groups(inventory, c, include_observing) for c in inventory.clients},
        },
        "files": {},
    }
    with AtomicOutput(out) as staging:
        for sub in ("providers", "snippets"):
            make_private_dir(staging / sub)
        for relative, text in files.items():
            data = text.encode("utf-8")
            write_private(staging / relative, data)
            manifest["files"][relative] = hashlib.sha256(data).hexdigest()
        write_private(staging / MARKER, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return manifest
