"""Shared test helpers: fake fragments in the layout frm-node produces."""

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
