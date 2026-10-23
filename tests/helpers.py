"""Shared test helpers: fake fragments in the layout frm-node produces, and fake tools."""

import shutil
import subprocess
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fleet.example.yaml"
FILES = {"surge": "surge.txt", "loon": "loon.txt", "mihomo": "mihomo.yaml"}


def fragment(client, names):
    if client == "mihomo":
        body = "".join(f"  - name: {n}\n    type: anytls\n    server: 192.0.2.1\n    password: fake\n" for n in names)
        return "proxies:\n" + (body or "  []\n")
    return "".join(f"{n} = anytls, 192.0.2.1, 443, password=fake\n" for n in names)


def write_tree(root, inventory, include_observing=False, skip=None, rename=None, reverse=False):
    states = {"active", "observing"} if include_observing else {"active"}
    for machine in inventory.machines:
        if machine.state not in states:
            continue
        for client, protocols in machine.protocols.items():
            if not protocols or (machine.name, client) == skip:
                continue
            names = [f"{machine.name}-{p}" for p in protocols]
            if reverse:
                names.reverse()
            if rename and (machine.name, client) == rename[0]:
                names = [rename[1] if n == rename[2] else n for n in names]
            path = root / machine.name / FILES[client]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fragment(client, names), encoding="utf-8")


def make_build(tmp_path, name, inventory_path=EXAMPLE, edit=None, include_observing=False):
    """Collect fake fragments and build them into tmp_path/name."""
    from proxyfleet.build import build
    from proxyfleet.collect import DirSource, collect
    from proxyfleet.inventory import load

    inventory = load(inventory_path)
    src = tmp_path / f"{name}-src"
    write_tree(src, inventory, include_observing=include_observing)
    if edit:
        edit(src)
    collect(inventory, DirSource(src), tmp_path / f"{name}-collected", include_observing=include_observing)
    build(inventory, tmp_path / f"{name}-collected", tmp_path / name, include_observing=include_observing)
    return tmp_path / name


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeSftp:
    """Executes the `lcd`/`put` batch file into an inbox directory, recording the order."""

    def __init__(self, inbox, after_upload=None, exit_code=0):
        self.inbox, self.after_upload, self.exit_code = inbox, after_upload, exit_code
        self.commands, self.order = [], []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if self.exit_code:
            return subprocess.CompletedProcess(command, self.exit_code)
        batch_file = Path(command[command.index("-b") + 1])
        lines = batch_file.read_bytes().decode("ascii").splitlines()
        local = Path(kwargs["cwd"])
        self.inbox.mkdir(parents=True, exist_ok=True)
        for line in lines:
            verb, name = line.split()
            assert verb == "put" and "/" not in name and "\\" not in name
            shutil.copy(local / name, self.inbox / name)
            self.order.append(name)
        if self.after_upload:
            self.after_upload()
        return subprocess.CompletedProcess(command, 0)
