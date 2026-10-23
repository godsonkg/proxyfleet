"""Command line interface: `proxyfleet validate` and `proxyfleet plan`."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .inventory import InventoryError, load
from .plan import build_plan

EXIT_OK, EXIT_INVALID, EXIT_USAGE = 0, 1, 2


def _load(path):
    try:
        return load(path)
    except FileNotFoundError:
        print(f"error: inventory not found: {path}", file=sys.stderr)
    except InventoryError as error:
        print(f"error: {path} is invalid:", file=sys.stderr)
        for line in error.errors:
            print(f"  - {line}", file=sys.stderr)
    return None


def cmd_validate(args) -> int:
    inventory = _load(args.inventory)
    if inventory is None:
        return EXIT_INVALID
    states = {}
    for machine in inventory.machines:
        states[machine.state] = states.get(machine.state, 0) + 1
    summary = ", ".join(f"{count} {state}" for state, count in sorted(states.items()))
    print(f"ok: {len(inventory.machines)} machines ({summary})")
    return EXIT_OK


def cmd_plan(args) -> int:
    inventory = _load(args.inventory)
    if inventory is None:
        return EXIT_INVALID
    plan = build_plan(inventory, include_observing=args.include_observing)
    if args.json:
        json.dump(plan, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return EXIT_OK
    contract = plan["contract"]
    scope = "active + observing (canary)" if args.include_observing else "active"
    print(f"inventory sha256: {contract['inventory_sha256']}")
    print(f"scope: {scope}")
    for client, data in plan["clients"].items():
        print(f"\n[{client}] {len(data['nodes'])} nodes")
        for name in data["nodes"]:
            print(f"  {name}")
        for group, members in data["groups"].items():
            if group == inventory.fleet_group:
                print(f"  group {group}: all {len(members)} nodes")
                continue
            print(f"  group {group}: {len(members)}")
            for member in members:
                print(f"    {member}")
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="proxyfleet",
        description="Manage a fleet of self-hosted proxy servers and plan client subscriptions.")
    parser.add_argument("--version", action="version", version=f"proxyfleet {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="check an inventory file for errors")
    validate.add_argument("inventory", help="path to fleet.yaml")
    validate.set_defaults(func=cmd_validate)

    plan = sub.add_parser("plan", help="show node names, groups and counts per client")
    plan.add_argument("inventory", help="path to fleet.yaml")
    plan.add_argument("--include-observing", action="store_true",
                      help="include observing machines (canary build)")
    plan.add_argument("--json", action="store_true", help="machine-readable output")
    plan.set_defaults(func=cmd_plan)

    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return EXIT_OK if exit_.code == 0 else EXIT_USAGE
    # Node names contain emoji; legacy Windows consoles default to a non-UTF-8 code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8")
    try:
        return args.func(args)
    except BrokenPipeError:
        # Output piped into `head` and similar; not an error.
        sys.stdout = None
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
