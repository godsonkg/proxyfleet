"""Change machines in fleet.yaml without hand-editing it.

Edits go through ruamel.yaml's round-trip mode, so comments and layout are
kept and only the touched lines change. A file whose layout cannot be
reproduced exactly is refused rather than silently reformatted. Every edit is
validated as a whole inventory before it is written, and written atomically.
"""

from __future__ import annotations

import io
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

import ruamel.yaml
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.util import load_yaml_guess_indent

from .collect import SourceError
from .fragments import FragmentError, node_names as fragment_node_names
from .inventory import COUNTRY_RE, NAME_RE, PROTOCOL_RE, Inventory, InventoryError, Machine, loads
from .plan import node_names, role_groups


class LifecycleError(Exception):
    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.details = details or []


@dataclass
class Change:
    summary: str
    before: Inventory
    after: Inventory
    text: str


class InventoryEditor:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.original = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise LifecycleError(f"inventory not found: {path}") from None
        try:
            self.before = loads(self.original.encode("utf-8"), str(path))
        except InventoryError as error:
            raise LifecycleError(f"{path} is invalid; fix it first", error.errors) from None
        _, indent, offset = load_yaml_guess_indent(self.original)
        self.yaml = ruamel.yaml.YAML()
        self.yaml.preserve_quotes = True
        self.yaml.width = 4096
        self.yaml.indent(mapping=2, sequence=indent or 2, offset=offset or 0)
        self.data = self.yaml.load(self.original)
        if self._dump() != self.original:
            raise LifecycleError(f"{path} uses a layout proxyfleet cannot edit without reformatting it; "
                                 "edit it by hand (2-space indentation, as in examples/fleet.example.yaml)")

    def _dump(self) -> str:
        out = io.StringIO()
        self.yaml.dump(self.data, out)
        return out.getvalue()

    def machine(self, name: str) -> CommentedMap:
        for entry in self.data["machines"]:
            if entry.get("name") == name:
                return entry
        raise LifecycleError(f"no machine named {name!r}")

    def finish(self, summary: str, text: str | None = None) -> Change:
        text = self._dump() if text is None else text
        try:
            after = loads(text.encode("utf-8"), str(self.path))
        except InventoryError as error:
            raise LifecycleError("the change would make the inventory invalid; nothing was written",
                                 error.errors) from None
        return Change(summary, self.before, after, text)


def save(path: Path, change: Change) -> None:
    """Write atomically, keeping the file's permissions."""
    temp = path.with_name(f".{path.name}.edit-{secrets.token_hex(4)}")
    mode = path.stat().st_mode & 0o777
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(change.text.encode("utf-8"))
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _flow(items) -> CommentedSeq:
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()
    return seq


def _set_state(editor: InventoryEditor, name: str, allowed_from: tuple[str, ...], to: str) -> Change:
    entry = editor.machine(name)
    state = entry.get("state")
    if state not in allowed_from:
        raise LifecycleError(f"{name} is {state}; only a machine that is {' or '.join(allowed_from)} can become {to}")
    entry["state"] = to
    return editor.finish(f"{name}: {state} -> {to}")


def add(path: Path, name: str, host: str, country: str, protocols: dict[str, list[str]],
        allow: list[str], deny: list[str]) -> Change:
    editor = InventoryEditor(path)
    if not NAME_RE.match(name):
        raise LifecycleError(f"machine name must match {NAME_RE.pattern}")
    if any(str(m.get("name", "")).lower() == name.lower() for m in editor.data["machines"]):
        raise LifecycleError(f"a machine named {name!r} already exists (retired names cannot be reused)")
    if not COUNTRY_RE.match(country.upper()):
        raise LifecycleError("country must be an ISO 3166-1 alpha-2 code such as US")
    unknown = [c for c in protocols if c not in editor.before.clients]
    if unknown:
        raise LifecycleError(f"client {unknown[0]!r} is not listed in the inventory's clients")
    entry = CommentedMap([
        ("name", name), ("host", host), ("country", country.upper()), ("state", "observing"),
        ("protocols", CommentedMap([(c, _flow(protocols.get(c, []))) for c in editor.before.clients])),
    ])
    if allow or deny:
        roles = CommentedMap()
        if allow:
            roles["allow"] = _flow(allow)
        if deny:
            roles["deny"] = _flow(deny)
        entry["roles"] = roles
    editor.data["machines"].append(entry)
    text = editor._dump()
    item = re.search(r"^( *)- name:", editor.original, re.M)
    if item and re.search(rf"\n\n{item.group(1)}- name:", editor.original):
        # Keep the file's blank line between machines.
        text = text.replace(f"\n{item.group(1)}- name: {name}\n", f"\n\n{item.group(1)}- name: {name}\n", 1)
    return editor.finish(f"{name}: added as observing", text)


def promote(path: Path, name: str, allow: list[str]) -> Change:
    editor = InventoryEditor(path)
    entry = editor.machine(name)
    if allow:
        roles = entry.setdefault("roles", CommentedMap())
        current = roles.setdefault("allow", _flow([]))
        for role in allow:
            if role not in current:
                current.append(role)
        deny = roles.get("deny")
        if deny is not None:
            for role in allow:
                if role in deny:
                    deny.remove(role)
    return _set_state(editor, name, ("observing",), "active")


def disable(path: Path, name: str) -> Change:
    return _set_state(InventoryEditor(path), name, ("active", "observing"), "disabled")


def enable(path: Path, name: str, observing: bool = False) -> Change:
    return _set_state(InventoryEditor(path), name, ("disabled",), "observing" if observing else "active")


def retire(path: Path, name: str) -> Change:
    return _set_state(InventoryEditor(path), name, ("active", "observing", "disabled"), "retired")


def set_host(path: Path, name: str, host: str) -> Change:
    editor = InventoryEditor(path)
    entry = editor.machine(name)
    old = entry.get("host")
    entry["host"] = host
    return editor.finish(f"{name}: host {old} -> {host}")


def discover_protocols(source, name: str, host: str, clients: tuple[str, ...]) -> dict[str, list[str]]:
    """Ask the machine (e.g. over SSH with frm-node) which protocols each client gets."""
    probe = Machine(name=name, host=host, country="ZZ", state="observing",
                    protocols={c: () for c in clients}, allow=(), deny=())
    result, failures = {}, []
    for client in clients:
        try:
            names = fragment_node_names(client, source.fetch(probe, client, allow_empty=True).decode("utf-8"))
        except (SourceError, FragmentError) as error:
            failures.append(f"{client}: {error}")
            continue
        except UnicodeDecodeError:
            failures.append(f"{client}: output is not UTF-8")
            continue
        protocols = []
        for node in names:
            protocol = node[len(name) + 1:] if node.startswith(f"{name}-") else ""
            if not PROTOCOL_RE.match(protocol):
                failures.append(f"{client}: unexpected node name (expected {name}-<protocol>)")
                break
            protocols.append(protocol)
        else:
            result[client] = protocols
    if failures:
        raise LifecycleError(f"could not discover protocols on {name}", failures)
    if not any(result.values()):
        raise LifecycleError(f"{name} reported no protocols for any client")
    return result


def describe(change: Change) -> list[str]:
    """What the change does to the production (active-only) node lists and groups."""
    lines = [change.summary]
    for client in change.after.clients:
        before, after = node_names(change.before, client), node_names(change.after, client)
        added = [n for n in after if n not in before]
        removed = [n for n in before if n not in after]
        if added or removed:
            lines.append(f"[{client}] {len(before)} -> {len(after)} nodes")
            lines += [f"  + {n}" for n in added] + [f"  - {n}" for n in removed]
        old_groups, new_groups = role_groups(change.before, client), role_groups(change.after, client)
        for group in [*new_groups, *[g for g in old_groups if g not in new_groups]]:
            plus = [n for n in new_groups.get(group, []) if n not in old_groups.get(group, [])]
            minus = [n for n in old_groups.get(group, []) if n not in new_groups.get(group, [])]
            if plus or minus:
                lines.append(f"  group {group}: " + ", ".join([f"+{n}" for n in plus] + [f"-{n}" for n in minus]))
    if len(lines) == 1:
        lines.append("no change to published node lists (observing and disabled machines are not published)")
    return lines

