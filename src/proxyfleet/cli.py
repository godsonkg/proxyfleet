"""Command line interface for proxyfleet."""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

from . import __version__, lifecycle
from .build import BuildError, build
from .collect import CollectError, DirSource, SshSource, collect
from .diff import DiffError, compare, format_text, load_build
from .fsutil import OutputError
from .inventory import InventoryError, load
from .plan import build_plan
from .publish import PublishError, default_config_path, load_config, prepare, publish
from .rotate import RotateError, rollback, rotate

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


def cmd_collect(args) -> int:
    inventory = _load(args.inventory)
    if inventory is None:
        return EXIT_INVALID
    if args.source_dir:
        source = DirSource(Path(args.source_dir))
    else:
        try:
            source = SshSource(user=args.ssh_user, options=args.ssh_option or [],
                               remote_command=args.remote_command, timeout=args.timeout,
                               attempts=args.attempts)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return EXIT_USAGE
    try:
        manifest = collect(inventory, source, Path(args.out), include_observing=args.include_observing,
                           progress=lambda line: print(line, flush=True))
    except CollectError as error:
        print(f"error: collection failed; {args.out} was not changed:", file=sys.stderr)
        for line in error.failures:
            print(f"  - {line}", file=sys.stderr)
        return EXIT_INVALID
    except OutputError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INVALID
    total = sum(len(c["nodes"]) for m in manifest["fragments"].values() for c in m.values())
    print(f"collected {total} nodes from {len(manifest['fragments'])} machines into {args.out}")
    print("warning: these files contain node credentials; keep them out of Git, cloud drives and chats",
          file=sys.stderr)
    return EXIT_OK


def cmd_build(args) -> int:
    inventory = _load(args.inventory)
    if inventory is None:
        return EXIT_INVALID
    try:
        manifest = build(inventory, Path(args.fragments), Path(args.out), include_observing=args.include_observing)
    except BuildError as error:
        print(f"error: build failed; {args.out} was not changed:", file=sys.stderr)
        for line in error.failures:
            print(f"  - {line}", file=sys.stderr)
        return EXIT_INVALID
    except OutputError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INVALID
    counts = ", ".join(f"{client} {count}" for client, count in manifest["contract"]["counts"].items())
    print(f"built {args.out}: {counts} nodes")
    for relative in manifest["files"]:
        print(f"  {relative}")
    print("warning: these files contain node credentials; keep them out of Git, cloud drives and chats",
          file=sys.stderr)
    return EXIT_OK


def cmd_diff(args) -> int:
    """Exit 0 when nothing changed, 1 when something did, 2 on errors (like diff(1))."""
    old_path, new_path = Path(args.old), Path(args.new)
    try:
        old = load_build(old_path) if old_path.exists() else None
        new = load_build(new_path)
    except DiffError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE
    result = compare(old, new)
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        for line in format_text(result):
            print(line)
    return 1 if result["changed"] or old is None else EXIT_OK


def cmd_publish(args) -> int:
    build_dir = Path(args.build)
    try:
        if args.dry_run:
            manifest, clients = prepare(build_dir)
            counts = ", ".join(f"{c} {manifest['contract']['counts'][c]}" for c in clients)
            print(f"dry run: {build_dir} is ready to publish ({counts} nodes); nothing was uploaded")
            return EXIT_OK
        config = load_config(Path(args.config) if args.config else default_config_path())
        summary = publish(build_dir, config, verify_timeout=args.verify_timeout,
                          progress=lambda line: print(line, flush=True))
    except PublishError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INVALID
    counts = ", ".join(f"{client} {count}" for client, count in summary["clients"].items())
    print(f"published batch {summary['batch']}: {counts} nodes; HTTPS serves the new files")
    return EXIT_OK


def _confirm(question: str) -> bool | None:
    """True/False from the user, or None when there is no one to ask."""
    if not sys.stdin.isatty():
        return None
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:  # Windows reports the NUL device as a terminal.
        return None


def cmd_rotate_path(args) -> int:
    config_path = Path(args.config) if args.config else default_config_path()
    try:
        if args.rollback:
            rollback(config_path, admin=args.admin, ssh_options=args.ssh_option)
            print("rolled back: the server serves the path in your publish config only")
            return EXIT_OK
        if not args.yes:
            answer = _confirm("Rotate the subscription path? Every client must then be given the new URL.")
            if answer is None:
                print("error: rotating breaks every client's current subscription URL; "
                      "pass --yes to confirm", file=sys.stderr)
                return EXIT_USAGE
            if not answer:
                print("cancelled; nothing was changed")
                return EXIT_OK
        result = rotate(config_path, admin=args.admin, ssh_options=args.ssh_option, timeout=args.timeout,
                        progress=lambda line: print(line, flush=True))
    except (PublishError, RotateError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_INVALID
    print(f"rotated: {', '.join(result['files'])} now served only under the new path; the old one returns 404")
    print(f"the new URL is in {config_path}; give it to every client (it is not printed here)")
    return EXIT_OK


def cmd_machine_list(args) -> int:
    inventory = _load(args.inventory)
    if inventory is None:
        return EXIT_INVALID
    clients = list(inventory.clients)
    rows = [["NAME", "HOST", "CC", "STATE", *[c.upper() for c in clients], "ROLES"]]
    for m in inventory.machines:
        rows.append([m.name, m.host, m.country, m.state, *[str(len(m.protocols[c])) for c in clients],
                     ",".join(m.allow) or "-"])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    return EXIT_OK


def cmd_machine_change(args) -> int:
    path = Path(args.inventory)
    try:
        if args.action == "add":
            if bool(args.discover) == bool(args.protocol):
                print("error: give either --discover or at least one --protocol CLIENT=P1,P2", file=sys.stderr)
                return EXIT_USAGE
            if args.discover:
                inventory = load(path)
                source = SshSource(user=args.ssh_user, options=args.ssh_option, remote_command=args.remote_command)
                protocols = lifecycle.discover_protocols(source, args.name, args.host, inventory.clients)
            else:
                protocols = {}
                for spec in args.protocol:
                    client, sep, values = spec.partition("=")
                    if not sep:
                        print(f"error: --protocol must look like CLIENT=P1,P2, got {spec!r}", file=sys.stderr)
                        return EXIT_USAGE
                    protocols[client.strip()] = [v.strip() for v in values.split(",") if v.strip()]
            change = lifecycle.add(path, args.name, args.host, args.country, protocols, args.allow, args.deny)
        elif args.action == "promote":
            change = lifecycle.promote(path, args.name, args.allow)
        elif args.action == "disable":
            change = lifecycle.disable(path, args.name)
        elif args.action == "enable":
            change = lifecycle.enable(path, args.name, observing=args.observing)
        elif args.action == "retire":
            change = lifecycle.retire(path, args.name)
        else:
            change = lifecycle.set_host(path, args.name, args.new_host)
    except (lifecycle.LifecycleError, InventoryError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        for line in getattr(error, "details", None) or getattr(error, "errors", []):
            print(f"  - {line}", file=sys.stderr)
        return EXIT_INVALID
    for line in lifecycle.describe(change):
        print(line)
    if args.dry_run:
        print("dry run: nothing was written")
        return EXIT_OK
    if args.action == "retire" and not args.yes:
        answer = _confirm(f"Retire {args.name}? Its names can never be reused.")
        if answer is None:
            print("error: retiring is permanent; pass --yes to confirm", file=sys.stderr)
            return EXIT_USAGE
        if not answer:
            print("cancelled; nothing was written")
            return EXIT_OK
    lifecycle.save(path, change)
    print(f"updated {path}")
    if args.action == "add":
        print("next: collect and build with --include-observing to test it as a canary, then `machine promote`")
    elif change.before.machines != change.after.machines:
        print("next: collect, build, diff and publish to put the change live")
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

    col = sub.add_parser("collect", help="fetch node fragments from every enabled machine")
    col.add_argument("inventory", help="path to fleet.yaml")
    col.add_argument("--out", required=True, help="output directory (replaced only if every fragment passes)")
    col.add_argument("--include-observing", action="store_true",
                     help="include observing machines (canary build)")
    col.add_argument("--from", dest="source_dir", metavar="DIR",
                     help="read <DIR>/<machine>/{surge.txt,loon.txt,mihomo.yaml} instead of using SSH")
    col.add_argument("--ssh-user", help="SSH user (default: from your SSH config)")
    col.add_argument("--ssh-option", action="append", metavar="OPT",
                     help="extra ssh argument, repeatable, e.g. --ssh-option=-p --ssh-option=2222")
    col.add_argument("--remote-command", default="frm-node", help="frm-node command on the server")
    col.add_argument("--timeout", type=float, default=60, help="seconds per SSH attempt")
    col.add_argument("--attempts", type=int, default=3, choices=range(1, 6), metavar="1-5",
                     help="SSH attempts per fragment")
    col.set_defaults(func=cmd_collect)

    bld = sub.add_parser("build", help="turn a collection into Surge, Loon and Mihomo files")
    bld.add_argument("inventory", help="path to fleet.yaml")
    bld.add_argument("--fragments", required=True, metavar="DIR", help="output directory of `collect`")
    bld.add_argument("--out", required=True, help="output directory (replaced only if the build succeeds)")
    bld.add_argument("--include-observing", action="store_true",
                     help="build the canary (the collection must also include observing machines)")
    bld.set_defaults(func=cmd_build)

    dif = sub.add_parser("diff", help="compare two builds by nodes, groups and node details",
                         description="Compare two build outputs. Node details are compared locally; "
                                     "only node and group names are printed. "
                                     "Exit status: 0 no changes, 1 changes, 2 error.")
    dif.add_argument("old", help="previous build output (may not exist yet)")
    dif.add_argument("new", help="new build output")
    dif.add_argument("--json", action="store_true", help="machine-readable output")
    dif.set_defaults(func=cmd_diff)

    pub = sub.add_parser("publish", help="upload a build to your proxyfleet server and confirm it over HTTPS")
    pub.add_argument("build", help="build output directory")
    pub.add_argument("--config", help=f"publish config (default: {default_config_path()})")
    pub.add_argument("--verify-timeout", type=float, default=90, metavar="SECONDS",
                     help="how long to wait for HTTPS to serve the new files (default: 90)")
    pub.add_argument("--dry-run", action="store_true", help="check the build without uploading")
    pub.set_defaults(func=cmd_publish)

    rot = sub.add_parser("rotate-path", help="replace the secret subscription path, e.g. after it leaked",
                         description="Serve the subscription under a new secret path, update publish.yaml, "
                                     "then retire the old path. Neither path is printed.")
    rot.add_argument("--config", help=f"publish config (default: {default_config_path()})")
    rot.add_argument("--admin", default="root", metavar="USER",
                     help="SSH user that may run the server helper (root, or a user with passwordless sudo)")
    rot.add_argument("--ssh-option", action="append", default=[], metavar="OPT", help="extra ssh argument, repeatable")
    rot.add_argument("--timeout", type=float, default=30, metavar="SECONDS",
                     help="how long to wait for each HTTPS check (default: 30)")
    rot.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    rot.add_argument("--rollback", action="store_true", help="undo an interrupted rotation")
    rot.set_defaults(func=cmd_rotate_path)

    mac = sub.add_parser("machine", help="add, promote, disable, enable, retire or re-address machines")
    mac_sub = mac.add_subparsers(dest="action", required=True)
    lst = mac_sub.add_parser("list", help="list machines")
    lst.add_argument("inventory", help="path to fleet.yaml")
    lst.set_defaults(func=cmd_machine_list)

    def change_parser(name, help_text):
        p = mac_sub.add_parser(name, help=help_text)
        p.add_argument("inventory", help="path to fleet.yaml")
        p.add_argument("name", help="machine name")
        p.add_argument("--dry-run", action="store_true", help="show the change without writing it")
        p.set_defaults(func=cmd_machine_change)
        return p

    add = change_parser("add", "add a machine in the observing state")
    add.add_argument("--host", required=True, help="IP address or host name")
    add.add_argument("--country", required=True, help="ISO 3166-1 alpha-2 code, e.g. US")
    add.add_argument("--protocol", action="append", default=[], metavar="CLIENT=P1,P2",
                     help="protocols per client, repeatable, e.g. surge=AnyTLS,Snell-v6")
    add.add_argument("--discover", action="store_true",
                     help="ask the machine over SSH (`frm-node sub fragment`) instead of --protocol")
    add.add_argument("--ssh-user", help="SSH user for --discover")
    add.add_argument("--ssh-option", action="append", default=[], metavar="OPT", help="extra ssh argument")
    add.add_argument("--remote-command", default="frm-node", help="frm-node command on the server")
    add.add_argument("--allow", action="append", default=[], metavar="ROLE", help="role to allow, repeatable")
    add.add_argument("--deny", action="append", default=[], metavar="ROLE", help="role to deny, repeatable")
    pro = change_parser("promote", "make an observing machine active")
    pro.add_argument("--allow", action="append", default=[], metavar="ROLE",
                     help="also allow this role (removes it from deny), repeatable")
    change_parser("disable", "stop publishing a machine for now")
    ena = change_parser("enable", "publish a disabled machine again")
    ena.add_argument("--observing", action="store_true", help="re-enable as observing instead of active")
    ret = change_parser("retire", "remove a machine for good; its names can never be reused")
    ret.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    host = change_parser("set-host", "change a machine's IP address or host name")
    host.add_argument("new_host", help="new IP address or host name")

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
