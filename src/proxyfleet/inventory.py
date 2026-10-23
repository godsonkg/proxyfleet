"""Load and validate a fleet inventory file (YAML or JSON)."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import string
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SCHEMA_VERSION = 1
STATES = ("active", "observing", "disabled", "retired")
KNOWN_CLIENTS = ("surge", "loon", "mihomo")
TEMPLATE_FIELDS = {"flag", "name", "protocol", "country"}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")
PROTOCOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,31}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,252}$")
# Characters that break Surge/Loon proxy lines or policy member lists.
FORBIDDEN_NODE_CHARS = set("=,\"\\\r\n")

TOP_KEYS = {"version", "clients", "naming", "groups", "roles", "lifecycle", "machines", "retired_names"}
MACHINE_KEYS = {"name", "host", "country", "state", "protocols", "roles"}
ROLE_KEYS = {"label", "protocols", "countries"}


class InventoryError(Exception):
    """Raised with every problem found, so users can fix them in one pass."""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Role:
    key: str
    label: str
    protocols: tuple[str, ...] | None = None
    countries: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Machine:
    name: str
    host: str
    country: str
    state: str
    protocols: dict[str, tuple[str, ...]]
    allow: tuple[str, ...]
    deny: tuple[str, ...]


@dataclass(frozen=True)
class Inventory:
    clients: tuple[str, ...]
    naming: dict[str, str]
    fleet_group: str
    role_group: str
    roles: dict[str, Role]
    observing_deny: tuple[str, ...]
    machines: tuple[Machine, ...]
    retired_names: tuple[str, ...]
    sha256: str = field(default="")

    def name_template(self, client: str) -> str:
        return self.naming.get(client, self.naming["default"])


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of keeping the last."""


def _construct_mapping(loader, node, deep=False):
    keys = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in keys:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key: {key}", key_node.start_mark)
        keys.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def country_flag(country: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in country)


def _check_template(template, where, errors):
    if not isinstance(template, str) or not template.strip():
        errors.append(f"{where}: must be a non-empty string")
        return
    try:
        fields = {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}
    except ValueError:
        errors.append(f"{where}: invalid template")
        return
    unknown = fields - TEMPLATE_FIELDS
    if unknown:
        errors.append(f"{where}: unknown placeholder(s) {', '.join(sorted(unknown))}")
    if "name" not in fields:
        errors.append(f"{where}: must contain {{name}}")
    if "protocol" not in fields:
        errors.append(f"{where}: must contain {{protocol}}")


def _str_list(value, where, errors, pattern=None, upper=False):
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{where}: must be a list of strings")
        return ()
    items = tuple(v.upper() if upper else v for v in value)
    seen = set()
    for item in items:
        if pattern and not pattern.match(item):
            errors.append(f"{where}: invalid value {item!r}")
        if item.lower() in seen:
            errors.append(f"{where}: duplicate value {item!r}")
        seen.add(item.lower())
    return items


def _unknown_keys(data, allowed, where, errors):
    for key in data:
        if key not in allowed:
            errors.append(f"{where}: unknown key {key!r}")


def parse(data, sha256: str = "") -> Inventory:
    errors: list[str] = []
    if not isinstance(data, dict):
        raise InventoryError(["inventory: top level must be a mapping"])
    _unknown_keys(data, TOP_KEYS, "inventory", errors)
    if data.get("version") != SCHEMA_VERSION:
        errors.append(f"version: must be {SCHEMA_VERSION}")

    clients = _str_list(data.get("clients", list(KNOWN_CLIENTS)), "clients", errors)
    for client in clients:
        if client not in KNOWN_CLIENTS:
            errors.append(f"clients: unsupported client {client!r} (supported: {', '.join(KNOWN_CLIENTS)})")
    if not clients:
        errors.append("clients: at least one client is required")

    naming = data.get("naming") or {}
    if not isinstance(naming, dict):
        errors.append("naming: must be a mapping")
        naming = {}
    naming = {"default": "{flag} {name}-{protocol}", **naming}
    for key, template in naming.items():
        if key != "default" and key not in KNOWN_CLIENTS:
            errors.append(f"naming: unknown client {key!r}")
        _check_template(template, f"naming.{key}", errors)

    groups = data.get("groups") or {}
    if not isinstance(groups, dict):
        errors.append("groups: must be a mapping")
        groups = {}
    _unknown_keys(groups, {"fleet", "role"}, "groups", errors)
    fleet_group = groups.get("fleet", "Fleet")
    role_group = groups.get("role", "Fleet-{role}")
    if not isinstance(fleet_group, str) or not fleet_group.strip():
        errors.append("groups.fleet: must be a non-empty string")
    if not isinstance(role_group, str) or "{role}" not in role_group:
        errors.append("groups.role: must be a string containing {role}")

    roles: dict[str, Role] = {}
    raw_roles = data.get("roles") or {}
    if not isinstance(raw_roles, dict):
        errors.append("roles: must be a mapping")
        raw_roles = {}
    for key, spec in raw_roles.items():
        where = f"roles.{key}"
        spec = spec or {}
        if not isinstance(key, str) or not ROLE_RE.match(key):
            errors.append(f"{where}: role keys must match {ROLE_RE.pattern}")
            continue
        if not isinstance(spec, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        _unknown_keys(spec, ROLE_KEYS, where, errors)
        label = spec.get("label", key.capitalize())
        if not isinstance(label, str) or not label.strip():
            errors.append(f"{where}.label: must be a non-empty string")
        protocols = _str_list(spec.get("protocols"), f"{where}.protocols", errors, PROTOCOL_RE) \
            if "protocols" in spec else None
        countries = _str_list(spec.get("countries"), f"{where}.countries", errors, COUNTRY_RE, upper=True) \
            if "countries" in spec else None
        roles[key] = Role(key, label, protocols, countries)

    if isinstance(role_group, str) and isinstance(fleet_group, str):
        group_names = [fleet_group] + [role_group.replace("{role}", r.label) for r in roles.values()]
        duplicates = sorted({g for g in group_names if group_names.count(g) > 1})
        if duplicates:
            errors.append(f"groups: duplicate group name(s) {', '.join(duplicates)}; give roles distinct labels")
        for group in group_names:
            if FORBIDDEN_NODE_CHARS & set(group):
                errors.append(f"groups: group name {group!r} contains a forbidden character (= , \" \\ or newline)")

    lifecycle = data.get("lifecycle") or {}
    if not isinstance(lifecycle, dict):
        errors.append("lifecycle: must be a mapping")
        lifecycle = {}
    _unknown_keys(lifecycle, {"observing"}, "lifecycle", errors)
    observing = lifecycle.get("observing") or {}
    if not isinstance(observing, dict):
        errors.append("lifecycle.observing: must be a mapping")
        observing = {}
    _unknown_keys(observing, {"deny_roles"}, "lifecycle.observing", errors)
    observing_deny = _str_list(observing.get("deny_roles"), "lifecycle.observing.deny_roles", errors)
    for role in observing_deny:
        if role not in roles:
            errors.append(f"lifecycle.observing.deny_roles: unknown role {role!r}")

    machines: list[Machine] = []
    raw_machines = data.get("machines")
    if not isinstance(raw_machines, list) or not raw_machines:
        errors.append("machines: must be a non-empty list")
        raw_machines = []
    names: set[str] = set()
    hosts: set[str] = set()
    for index, raw in enumerate(raw_machines):
        where = f"machines[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        _unknown_keys(raw, MACHINE_KEYS, where, errors)
        name = raw.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            errors.append(f"{where}.name: must match {NAME_RE.pattern}")
            name = f"#{index}"
        else:
            where = f"machines[{name}]"
            if name.lower() in names:
                errors.append(f"{where}: duplicate machine name")
            names.add(name.lower())

        host = raw.get("host")
        if not isinstance(host, str) or not HOST_RE.match(host):
            errors.append(f"{where}.host: must be an IP address or host name")
            host = ""
        else:
            try:
                host = str(ipaddress.ip_address(host))
            except ValueError:
                host = host.lower()
            if host in hosts:
                errors.append(f"{where}.host: duplicate host {host}")
            hosts.add(host)

        country = raw.get("country")
        if not isinstance(country, str) or not COUNTRY_RE.match(country.upper()):
            errors.append(f"{where}.country: must be an ISO 3166-1 alpha-2 code")
            country = "ZZ"
        country = country.upper()

        state = raw.get("state")
        if state not in STATES:
            errors.append(f"{where}.state: must be one of {', '.join(STATES)}")

        raw_protocols = raw.get("protocols")
        protocols: dict[str, tuple[str, ...]] = {}
        if not isinstance(raw_protocols, dict):
            errors.append(f"{where}.protocols: must be a mapping of client to protocol list")
            raw_protocols = {}
        for client in raw_protocols:
            if client not in clients:
                errors.append(f"{where}.protocols: client {client!r} is not listed in clients")
        for client in clients:
            if client not in raw_protocols:
                errors.append(f"{where}.protocols.{client}: missing (use [] if unsupported)")
            protocols[client] = _str_list(raw_protocols.get(client), f"{where}.protocols.{client}",
                                          errors, PROTOCOL_RE)
        if state in ("active", "observing") and not any(protocols.values()):
            errors.append(f"{where}: enabled machine has no client protocols")

        raw_roles_spec = raw.get("roles") or {}
        if not isinstance(raw_roles_spec, dict):
            errors.append(f"{where}.roles: must be a mapping with allow/deny")
            raw_roles_spec = {}
        _unknown_keys(raw_roles_spec, {"allow", "deny"}, f"{where}.roles", errors)
        allow = _str_list(raw_roles_spec.get("allow"), f"{where}.roles.allow", errors)
        deny = _str_list(raw_roles_spec.get("deny"), f"{where}.roles.deny", errors)
        for role in (*allow, *deny):
            if role not in roles:
                errors.append(f"{where}.roles: unknown role {role!r}")
        overlap = sorted(set(allow) & set(deny))
        if overlap:
            errors.append(f"{where}.roles: {', '.join(overlap)} in both allow and deny")
        if state == "observing":
            blocked = sorted(set(allow) & set(observing_deny))
            if blocked:
                errors.append(f"{where}: observing machine cannot allow {', '.join(blocked)}")

        machines.append(Machine(name, host, country, state, protocols, allow, deny))

    retired_names = _str_list(data.get("retired_names"), "retired_names", errors)

    if errors:
        raise InventoryError(errors)

    inventory = Inventory(
        clients=clients, naming=naming, fleet_group=fleet_group, role_group=role_group,
        roles=roles, observing_deny=observing_deny, machines=tuple(machines),
        retired_names=retired_names, sha256=sha256)
    _check_node_names(inventory)
    return inventory


def _check_node_names(inventory: Inventory) -> None:
    # Imported here to keep the dependency one-way: plan builds on inventory.
    from .plan import node_names, retired_node_names

    errors = []
    retired = retired_node_names(inventory)
    for client in inventory.clients:
        seen: set[str] = set()
        for name in node_names(inventory, client, include_observing=True):
            if FORBIDDEN_NODE_CHARS & set(name):
                errors.append(f"{client}: node name {name!r} contains a forbidden character (= , \" \\ or newline)")
            if name in seen:
                errors.append(f"{client}: duplicate node name {name!r}")
            if name in retired:
                errors.append(f"{client}: node name {name!r} is retired and must not be reused")
            seen.add(name)
    if errors:
        raise InventoryError(errors)


def load(path: str | Path) -> Inventory:
    return loads(Path(path).read_bytes(), str(path))


def loads(raw: bytes, path: str = "inventory") -> Inventory:
    """Parse and validate inventory bytes; `path` only labels error messages."""
    try:
        data = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader)
    except UnicodeDecodeError:
        raise InventoryError([f"{path}: must be UTF-8"]) from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = f" (line {mark.line + 1})" if mark else ""
        problem = getattr(error, "problem", None) or "invalid YAML"
        raise InventoryError([f"{path}: {problem}{location}"]) from None
    return parse(data, hashlib.sha256(raw).hexdigest())
