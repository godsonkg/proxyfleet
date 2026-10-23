"""Derive node names, role groups and the node contract from an inventory.

Everything here is a pure function of the inventory: no network, no files.
"""

from __future__ import annotations

from .inventory import Inventory, Machine, country_flag


def enabled_machines(inventory: Inventory, include_observing: bool = False) -> list[Machine]:
    states = {"active", "observing"} if include_observing else {"active"}
    return [m for m in inventory.machines if m.state in states]


def node_name(inventory: Inventory, machine: Machine, client: str, protocol: str) -> str:
    return inventory.name_template(client).format(
        flag=country_flag(machine.country), name=machine.name,
        protocol=protocol, country=machine.country)


def node_names(inventory: Inventory, client: str, include_observing: bool = False) -> list[str]:
    """Node names for one client, in inventory order then protocol order."""
    return [node_name(inventory, m, client, p)
            for m in enabled_machines(inventory, include_observing)
            for p in m.protocols[client]]


def retired_node_names(inventory: Inventory) -> set[str]:
    """Names that must never come back: retired machines plus the history list."""
    names = set(inventory.retired_names)
    for machine in inventory.machines:
        if machine.state == "retired":
            names.add(machine.name)
            for client in inventory.clients:
                names.update(node_name(inventory, machine, client, p) for p in machine.protocols[client])
    return names


def role_groups(inventory: Inventory, client: str) -> dict[str, list[str]]:
    """Role group name -> members. Only active machines; empty groups are omitted.

    A machine joins a role when the role is in its allow list and not in its
    deny list. A role may further restrict which protocols and countries count.
    """
    groups: dict[str, list[str]] = {}
    for role in inventory.roles.values():
        members = []
        for machine in enabled_machines(inventory):
            if role.key not in machine.allow or role.key in machine.deny:
                continue
            if role.countries is not None and machine.country not in role.countries:
                continue
            for protocol in machine.protocols[client]:
                if role.protocols is not None and protocol.lower() not in {p.lower() for p in role.protocols}:
                    continue
                members.append(node_name(inventory, machine, client, protocol))
        if members:
            groups[inventory.role_group.replace("{role}", role.label)] = members
    return groups


def contract(inventory: Inventory, include_observing: bool = False) -> dict:
    """What a published batch must contain, bound to the exact inventory file."""
    return {
        "version": 1,
        "inventory_sha256": inventory.sha256,
        "include_observing": include_observing,
        "counts": {c: len(node_names(inventory, c, include_observing)) for c in inventory.clients},
    }


def client_groups(inventory: Inventory, client: str, include_observing: bool) -> dict[str, list[str]]:
    nodes = node_names(inventory, client, include_observing)
    fleet = {inventory.fleet_group: nodes} if nodes else {}
    return {**fleet, **role_groups(inventory, client)}


def build_plan(inventory: Inventory, include_observing: bool = False) -> dict:
    return {
        "contract": contract(inventory, include_observing),
        "clients": {
            client: {
                "nodes": node_names(inventory, client, include_observing),
                "groups": client_groups(inventory, client, include_observing),
            }
            for client in inventory.clients
        },
    }
