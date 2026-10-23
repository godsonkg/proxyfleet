"""Collect raw node fragments for every enabled machine and client.

Fragments are stored exactly as received. Each one must contain exactly the
nodes the inventory expects for that machine, named "<machine>-<protocol>",
which is what `frm-node sub fragment --prefix <machine>` produces.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .fragments import FILE_NAMES, FragmentError, is_safe_name, node_names
from .fsutil import MARKER, AtomicOutput, make_private_dir, write_private
from .inventory import Inventory, Machine
from .plan import enabled_machines

REMOTE_COMMAND_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
SSH_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
MAX_FRAGMENT_BYTES = 1024 * 1024


class SourceError(Exception):
    pass


class CollectError(Exception):
    def __init__(self, failures: list[str]):
        super().__init__("\n".join(failures))
        self.failures = failures


def expected_names(machine: Machine, client: str) -> list[str]:
    return [f"{machine.name}-{protocol}" for protocol in machine.protocols[client]]


class DirSource:
    """Fragments already on disk: <root>/<machine>/surge.txt, loon.txt, mihomo.yaml."""

    def __init__(self, root: Path):
        self.root = root

    def describe(self) -> str:
        return f"directory {self.root}"

    def fetch(self, machine: Machine, client: str) -> bytes:
        path = self.root / machine.name / FILE_NAMES[client]
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise SourceError(f"missing {path}") from None
        if len(data) > MAX_FRAGMENT_BYTES:
            raise SourceError("fragment larger than 1 MiB")
        return data


@dataclass
class SshSource:
    """Run `frm-node sub fragment` on each machine over SSH."""

    user: str | None = None
    options: list[str] = field(default_factory=list)
    remote_command: str = "frm-node"
    timeout: float = 60
    attempts: int = 3
    retry_delay: float = 1
    runner: Callable = subprocess.run

    def __post_init__(self):
        if self.user is not None and not SSH_USER_RE.match(self.user):
            raise ValueError("invalid SSH user")
        if not REMOTE_COMMAND_RE.match(self.remote_command):
            raise ValueError("invalid remote command")

    def describe(self) -> str:
        return "ssh"

    def command(self, machine: Machine, client: str) -> list[str]:
        target = f"{self.user}@{machine.host}" if self.user else machine.host
        # Machine names and client names are validated, so the remote string is shell-safe.
        remote = f"{self.remote_command} sub fragment --format {client} --prefix {machine.name} --stdout"
        return ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                *self.options, "--", target, remote]

    def fetch(self, machine: Machine, client: str, allow_empty: bool = False) -> bytes:
        """Fragment bytes. Empty output counts as a failure unless `allow_empty`
        (frm-node prints nothing for a client none of its protocols support)."""
        reason = "no attempt made"
        for attempt in range(1, self.attempts + 1):
            try:
                result = self.runner(self.command(machine, client), stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired:
                reason = f"timed out after {self.timeout:g}s"
            except FileNotFoundError:
                raise SourceError("ssh not found on PATH") from None
            else:
                if result.returncode == 0 and (allow_empty or result.stdout.strip()):
                    if len(result.stdout) > MAX_FRAGMENT_BYTES:
                        raise SourceError("fragment larger than 1 MiB")
                    return result.stdout
                reason = f"ssh exit {result.returncode}" if result.returncode else "empty output"
            if attempt < self.attempts:
                time.sleep(self.retry_delay)
        raise SourceError(f"{reason} after {self.attempts} attempt(s)")


def _check(machine: Machine, client: str, data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FragmentError("not UTF-8") from None
    names = node_names(client, text)
    expected = expected_names(machine, client)
    missing = [n for n in expected if n not in names]
    unexpected = [n for n in names if n not in expected]
    if missing or unexpected:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if unexpected:
            shown = [n for n in unexpected if is_safe_name(n)]
            hidden = len(unexpected) - len(shown)
            parts.append("unexpected " + ", ".join(shown + ([f"{hidden} unrecognised name(s) not shown"] if hidden else [])))
        raise FragmentError("; ".join(parts))
    return names


def collect(inventory: Inventory, source, out: Path, include_observing: bool = False,
            progress: Callable[[str], None] = lambda _: None) -> dict:
    """Fetch and check every fragment, then replace `out` in one step.

    Nothing is written to `out` unless every fragment passes, so a failed run
    leaves the previous collection untouched.
    """
    jobs = [(m, c) for m in enabled_machines(inventory, include_observing)
            for c in inventory.clients if m.protocols[c]]
    fetched = {}
    failures = []
    for machine, client in jobs:
        try:
            data = source.fetch(machine, client)
            names = _check(machine, client, data)
        except (SourceError, FragmentError) as error:
            failures.append(f"{machine.name} {client}: {error}")
            progress(f"FAIL {machine.name} {client}")
            continue
        fetched[(machine.name, client)] = (data, names)
        progress(f"ok   {machine.name} {client}: {len(names)} nodes")
    if not jobs:
        failures.append("no enabled machine has protocols to collect")
    if failures:
        raise CollectError(failures)

    manifest = {
        "proxyfleet": __version__,
        "kind": "collect",
        "inventory_sha256": inventory.sha256,
        "include_observing": include_observing,
        "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source.describe(),
        "fragments": {},
    }
    with AtomicOutput(out) as staging:
        for (name, client), (data, names) in fetched.items():
            machine_dir = staging / name
            if not machine_dir.exists():
                make_private_dir(machine_dir)
            write_private(machine_dir / FILE_NAMES[client], data)
            manifest["fragments"].setdefault(name, {})[client] = {
                "file": f"{name}/{FILE_NAMES[client]}",
                "sha256": hashlib.sha256(data).hexdigest(),
                "nodes": names,
            }
        write_private(staging / MARKER, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return manifest
