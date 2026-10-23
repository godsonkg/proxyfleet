import difflib
import os
import subprocess
import sys

import pytest

from proxyfleet import lifecycle
from proxyfleet.cli import main
from proxyfleet.collect import SshSource
from proxyfleet.inventory import load

from helpers import EXAMPLE, fragment

ORIGINAL = EXAMPLE.read_text(encoding="utf-8")


@pytest.fixture
def fleet(tmp_path):
    path = tmp_path / "fleet.yaml"
    path.write_bytes(EXAMPLE.read_bytes())
    if sys.platform != "win32":
        path.chmod(0o600)
    return path


def changed_lines(path):
    """Lines that differ from the example, as (removed, added)."""
    diff = list(difflib.ndiff(ORIGINAL.splitlines(), path.read_text(encoding="utf-8").splitlines()))
    return [l[2:] for l in diff if l.startswith("- ")], [l[2:] for l in diff if l.startswith("+ ")]


def apply(path, change):
    lifecycle.save(path, change)
    return load(path)


def test_promote_changes_only_the_state_line(fleet):
    inventory = apply(fleet, lifecycle.promote(fleet, "US-NEW", []))
    assert next(m for m in inventory.machines if m.name == "US-NEW").state == "active"
    assert changed_lines(fleet) == (["    state: observing"], ["    state: active"])


def test_promote_can_add_roles_and_lifts_them_from_deny(fleet):
    text = ORIGINAL.replace("    roles:\n      allow: [general]\n\n  - name: HK-OLD",
                            "    roles:\n      allow: [general]\n      deny: [streaming]\n\n  - name: HK-OLD")
    fleet.write_text(text, encoding="utf-8")
    inventory = apply(fleet, lifecycle.promote(fleet, "US-NEW", ["streaming", "telegram"]))
    machine = next(m for m in inventory.machines if m.name == "US-NEW")
    assert machine.allow == ("general", "streaming", "telegram") and machine.deny == ()


def test_comments_are_kept(fleet):
    apply(fleet, lifecycle.retire(fleet, "JP-TYO"))
    text = fleet.read_text(encoding="utf-8")
    for line in ORIGINAL.splitlines():
        if line.lstrip().startswith("#"):
            assert line in text


@pytest.mark.parametrize("action, name, message", [
    (lambda p, n: lifecycle.promote(p, n, []), "US-WEST", "US-WEST is active; only a machine that is observing"),
    (lambda p, n: lifecycle.enable(p, n), "US-WEST", "only a machine that is disabled"),
    (lambda p, n: lifecycle.disable(p, n), "HK-OLD", "HK-OLD is retired"),
    (lambda p, n: lifecycle.retire(p, n), "HK-OLD", "HK-OLD is retired"),
    (lambda p, n: lifecycle.retire(p, n), "NOPE", "no machine named 'NOPE'"),
])
def test_invalid_transitions(fleet, action, name, message):
    with pytest.raises(lifecycle.LifecycleError) as info:
        action(fleet, name)
    assert message in str(info.value)
    assert fleet.read_text(encoding="utf-8") == ORIGINAL


def test_disable_then_enable(fleet):
    apply(fleet, lifecycle.disable(fleet, "JP-TYO"))
    inventory = apply(fleet, lifecycle.enable(fleet, "JP-TYO", observing=True))
    assert next(m for m in inventory.machines if m.name == "JP-TYO").state == "observing"


def test_set_host(fleet):
    apply(fleet, lifecycle.set_host(fleet, "JP-TYO", "198.51.100.21"))
    assert changed_lines(fleet) == (["    host: 198.51.100.20"], ["    host: 198.51.100.21"])
    with pytest.raises(lifecycle.LifecycleError) as info:
        lifecycle.set_host(fleet, "JP-TYO", "203.0.113.10")
    assert "duplicate host 203.0.113.10" in "\n".join(info.value.details)


def test_add_appends_an_observing_machine_in_the_file_style(fleet):
    change = lifecycle.add(fleet, "SG-NEW", "192.0.2.50", "sg", {"surge": ["AnyTLS"], "mihomo": ["AnyTLS"]},
                           ["general"], [])
    inventory = apply(fleet, change)
    machine = inventory.machines[-1]
    assert (machine.name, machine.country, machine.state) == ("SG-NEW", "SG", "observing")
    assert machine.protocols == {"surge": ("AnyTLS",), "loon": (), "mihomo": ("AnyTLS",)}
    text = fleet.read_text(encoding="utf-8")
    assert "      mihomo: [AnyTLS]\n\n  - name: SG-NEW\n    host: 192.0.2.50\n" in text
    assert text.endswith("# Node names from machines removed from this file that must never reappear.\n"
                         "retired_names: []\n")


@pytest.mark.parametrize("kwargs, message", [
    ({"name": "us-west"}, "already exists"),
    ({"name": "HK-OLD"}, "already exists"),
    ({"name": "bad name"}, "machine name must match"),
    ({"country": "USA"}, "ISO 3166-1"),
    ({"protocols": {"quantumult": ["AnyTLS"]}}, "client 'quantumult'"),
])
def test_add_rejects_bad_input(fleet, kwargs, message):
    args = {"name": "SG-NEW", "host": "192.0.2.50", "country": "SG", "protocols": {"surge": ["AnyTLS"]},
            "allow": [], "deny": [], **kwargs}
    with pytest.raises(lifecycle.LifecycleError) as info:
        lifecycle.add(fleet, **args)
    assert message in str(info.value)


def test_add_observing_machine_cannot_allow_restricted_roles(fleet):
    with pytest.raises(lifecycle.LifecycleError) as info:
        lifecycle.add(fleet, "SG-NEW", "192.0.2.50", "SG", {"surge": ["AnyTLS"]}, ["ai"], [])
    assert "observing machine cannot allow ai" in "\n".join(info.value.details)
    assert fleet.read_text(encoding="utf-8") == ORIGINAL


def test_unreproducible_layout_is_refused(fleet):
    fleet.write_text(ORIGINAL.replace("\n  default:", "\n    default:").replace("\n  loon: \"", "\n    loon: \""),
                     encoding="utf-8")
    before = fleet.read_bytes()
    with pytest.raises(lifecycle.LifecycleError) as info:
        lifecycle.promote(fleet, "US-NEW", [])
    assert "cannot edit without reformatting" in str(info.value)
    assert fleet.read_bytes() == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_save_keeps_permissions_and_leaves_no_temp_files(fleet):
    apply(fleet, lifecycle.retire(fleet, "JP-TYO"))
    assert fleet.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in fleet.parent.iterdir()] == ["fleet.yaml"]


def test_describe_lists_published_changes(fleet):
    lines = lifecycle.describe(lifecycle.promote(fleet, "US-NEW", ["telegram"]))
    assert lines[0] == "US-NEW: observing -> active"
    assert "  + 🇺🇸 US-NEW-AnyTLS" in lines
    assert "  group 🛰️ Fleet-Telegram: +🇺🇸 US-NEW-AnyTLS" in lines
    assert lifecycle.describe(lifecycle.disable(fleet, "US-NEW"))[1].startswith("no change to published")


class FakeFrmNode:
    def __init__(self, protocols, exit_code=0):
        self.protocols, self.exit_code = protocols, exit_code

    def __call__(self, command, **kwargs):
        remote = command[-1].split()
        client, name = remote[remote.index("--format") + 1], remote[remote.index("--prefix") + 1]
        names = [f"{name}-{p}" for p in self.protocols.get(client, [])]
        return subprocess.CompletedProcess(command, self.exit_code, fragment(client, names).encode(), b"")


def test_discover_protocols():
    source = SshSource(runner=FakeFrmNode({"surge": ["AnyTLS", "Snell-v6"], "mihomo": ["Reality"]}), retry_delay=0)
    found = lifecycle.discover_protocols(source, "SG-NEW", "192.0.2.50", ("surge", "loon", "mihomo"))
    assert found == {"surge": ["AnyTLS", "Snell-v6"], "loon": [], "mihomo": ["Reality"]}


def test_discover_reports_every_failure():
    source = SshSource(runner=FakeFrmNode({}, exit_code=255), attempts=1, retry_delay=0)
    with pytest.raises(lifecycle.LifecycleError) as info:
        lifecycle.discover_protocols(source, "SG-NEW", "192.0.2.50", ("surge", "loon"))
    assert info.value.details == ["surge: ssh exit 255 after 1 attempt(s)", "loon: ssh exit 255 after 1 attempt(s)"]


# --- CLI ------------------------------------------------------------------------

def test_cli_dry_run_writes_nothing(fleet, capsys):
    assert main(["machine", "retire", str(fleet), "JP-TYO", "--dry-run"]) == 0
    assert "dry run: nothing was written" in capsys.readouterr().out
    assert fleet.read_text(encoding="utf-8") == ORIGINAL


def test_cli_retire_needs_confirmation(fleet, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    assert main(["machine", "retire", str(fleet), "JP-TYO"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert fleet.read_text(encoding="utf-8") == ORIGINAL
    assert main(["machine", "retire", str(fleet), "JP-TYO", "--yes"]) == 0
    assert next(m for m in load(fleet).machines if m.name == "JP-TYO").state == "retired"


def test_cli_add_with_protocols(fleet, capsys):
    assert main(["machine", "add", str(fleet), "SG-NEW", "--host", "192.0.2.50", "--country", "SG",
                 "--protocol", "surge=AnyTLS,Snell-v6", "--allow", "general"]) == 0
    assert load(fleet).machines[-1].protocols["surge"] == ("AnyTLS", "Snell-v6")


def test_cli_add_needs_protocols_or_discover(fleet, capsys):
    assert main(["machine", "add", str(fleet), "SG-NEW", "--host", "192.0.2.50", "--country", "SG"]) == 2
    assert main(["machine", "add", str(fleet), "SG-NEW", "--host", "192.0.2.50", "--country", "SG",
                 "--protocol", "surge"]) == 2


def test_cli_list(fleet, capsys):
    assert main(["machine", "list", str(fleet)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["NAME", "HOST", "CC", "STATE", "SURGE", "LOON", "MIHOMO", "ROLES"]
    assert out[2].split() == ["JP-TYO", "198.51.100.20", "JP", "active", "2", "2", "2", "general,telegram,streaming"]
