import json
import subprocess
import sys
import pytest

from proxyfleet.cli import main
from proxyfleet.collect import CollectError, DirSource, SshSource, collect
from proxyfleet.fsutil import OutputError
from proxyfleet.inventory import load

from helpers import EXAMPLE, fragment, write_tree

POSIX = sys.platform != "win32"


@pytest.fixture
def inventory():
    return load(EXAMPLE)


def test_collect_from_directory(tmp_path, inventory):
    write_tree(tmp_path / "src", inventory)
    out = tmp_path / "out"
    manifest = collect(inventory, DirSource(tmp_path / "src"), out)
    assert manifest["inventory_sha256"] == inventory.sha256
    assert manifest["fragments"]["US-WEST"]["loon"]["nodes"] == ["US-WEST-AnyTLS", "US-WEST-Reality"]
    assert "US-NEW" not in manifest["fragments"]
    saved = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert saved["fragments"] == manifest["fragments"]
    assert (out / "JP-TYO" / "mihomo.yaml").read_text(encoding="utf-8").startswith("proxies:")


def test_canary_includes_observing(tmp_path, inventory):
    write_tree(tmp_path / "src", inventory, include_observing=True)
    manifest = collect(inventory, DirSource(tmp_path / "src"), tmp_path / "out", include_observing=True)
    assert manifest["fragments"]["US-NEW"]["surge"]["nodes"] == ["US-NEW-AnyTLS"]


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_output_is_private(tmp_path, inventory):
    write_tree(tmp_path / "src", inventory)
    out = tmp_path / "out"
    collect(inventory, DirSource(tmp_path / "src"), out)
    assert (out.stat().st_mode & 0o777) == 0o700
    assert ((out / "US-WEST").stat().st_mode & 0o777) == 0o700
    assert ((out / "US-WEST" / "surge.txt").stat().st_mode & 0o777) == 0o600


def test_name_mismatch_is_reported_without_content(tmp_path, inventory):
    write_tree(tmp_path / "src", inventory, rename=(("JP-TYO", "surge"), "JP-TYO-Hy2", "JP-TYO-Hysteria2"))
    with pytest.raises(CollectError) as info:
        collect(inventory, DirSource(tmp_path / "src"), tmp_path / "out")
    message = str(info.value)
    assert "JP-TYO surge: missing JP-TYO-Hysteria2; unexpected JP-TYO-Hy2" in message
    assert "fake" not in message and "192.0.2.1" not in message


def test_unrecognised_names_are_not_echoed(tmp_path, inventory):
    write_tree(tmp_path / "src", inventory)
    path = tmp_path / "src" / "US-WEST" / "surge.txt"
    path.write_text(path.read_text(encoding="utf-8") + "token hunter2 = anytls\n", encoding="utf-8")
    with pytest.raises(CollectError) as info:
        collect(inventory, DirSource(tmp_path / "src"), tmp_path / "out")
    assert "1 unrecognised name(s) not shown" in str(info.value)
    assert "hunter2" not in str(info.value)


def test_every_failure_is_reported(tmp_path, inventory):
    write_tree(tmp_path / "src", inventory, skip=("US-WEST", "loon"))
    (tmp_path / "src" / "JP-TYO" / "mihomo.yaml").write_text("proxies: [\n", encoding="utf-8")
    with pytest.raises(CollectError) as info:
        collect(inventory, DirSource(tmp_path / "src"), tmp_path / "out")
    assert len(info.value.failures) == 2


def test_failure_keeps_previous_output(tmp_path, inventory):
    write_tree(tmp_path / "src", inventory)
    out = tmp_path / "out"
    first = collect(inventory, DirSource(tmp_path / "src"), out)
    (tmp_path / "src" / "US-WEST" / "surge.txt").unlink()
    with pytest.raises(CollectError):
        collect(inventory, DirSource(tmp_path / "src"), out)
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8")) == first
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out", "src"]


def test_success_replaces_previous_output(tmp_path, inventory):
    write_tree(tmp_path / "src", inventory)
    out = tmp_path / "out"
    collect(inventory, DirSource(tmp_path / "src"), out)
    (out / "stale.txt").write_text("old", encoding="utf-8")
    collect(inventory, DirSource(tmp_path / "src"), out)
    assert not (out / "stale.txt").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out", "src"]


def test_refuses_to_replace_unrelated_directory(tmp_path, inventory):
    write_tree(tmp_path / "src", inventory)
    out = tmp_path / "documents"
    out.mkdir()
    (out / "thesis.docx").write_text("precious", encoding="utf-8")
    with pytest.raises(OutputError):
        collect(inventory, DirSource(tmp_path / "src"), out)
    assert (out / "thesis.docx").read_text(encoding="utf-8") == "precious"


class FakeRunner:
    """Stands in for subprocess.run; serves fragments as frm-node would."""

    def __init__(self, inventory, fail_first=0, exit_code=255, timeout=False):
        self.inventory, self.fail_first, self.exit_code, self.timeout = inventory, fail_first, exit_code, timeout
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self.timeout:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if len(self.calls) <= self.fail_first:
            return subprocess.CompletedProcess(command, self.exit_code, b"", b"")
        remote = command[-1].split()
        client, name = remote[remote.index("--format") + 1], remote[remote.index("--prefix") + 1]
        machine = next(m for m in self.inventory.machines if m.name == name)
        names = [f"{name}-{p}" for p in machine.protocols[client]]
        return subprocess.CompletedProcess(command, 0, fragment(client, names).encode(), b"")


def test_ssh_source_builds_safe_commands(tmp_path, inventory):
    runner = FakeRunner(inventory)
    source = SshSource(user="root", options=["-p", "2222"], runner=runner, retry_delay=0)
    collect(inventory, source, tmp_path / "out")
    first = runner.calls[0]
    assert first[:6] == ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    assert first[6:9] == ["-p", "2222", "--"]
    assert first[-2] == "root@203.0.113.10"
    assert first[-1] == "frm-node sub fragment --format surge --prefix US-WEST --stdout"
    assert len(runner.calls) == 6


def test_ssh_retries_then_succeeds(tmp_path, inventory):
    runner = FakeRunner(inventory, fail_first=2)
    collect(inventory, SshSource(runner=runner, retry_delay=0), tmp_path / "out")
    assert len(runner.calls) == 8


def test_ssh_gives_up_after_attempts(tmp_path, inventory):
    runner = FakeRunner(inventory, fail_first=100)
    with pytest.raises(CollectError) as info:
        collect(inventory, SshSource(runner=runner, attempts=2, retry_delay=0), tmp_path / "out")
    assert "US-WEST surge: ssh exit 255 after 2 attempt(s)" in info.value.failures
    assert not (tmp_path / "out").exists()


def test_ssh_timeout(tmp_path, inventory):
    runner = FakeRunner(inventory, timeout=True)
    with pytest.raises(CollectError) as info:
        collect(inventory, SshSource(runner=runner, attempts=1, timeout=5, retry_delay=0), tmp_path / "out")
    assert "US-WEST surge: timed out after 5s after 1 attempt(s)" in info.value.failures


@pytest.mark.parametrize("kwargs", [{"user": "root; rm -rf /"}, {"remote_command": "frm-node; reboot"}])
def test_ssh_source_rejects_unsafe_settings(kwargs):
    with pytest.raises(ValueError):
        SshSource(**kwargs)


def test_cli_collect(tmp_path, capsys, inventory):
    write_tree(tmp_path / "src", inventory)
    code = main(["collect", str(EXAMPLE), "--from", str(tmp_path / "src"), "--out", str(tmp_path / "out")])
    captured = capsys.readouterr()
    assert code == 0
    assert "collected 12 nodes from 2 machines" in captured.out
    assert "contain node credentials" in captured.err


def test_cli_collect_failure(tmp_path, capsys):
    code = main(["collect", str(EXAMPLE), "--from", str(tmp_path / "missing"), "--out", str(tmp_path / "out")])
    err = capsys.readouterr().err
    assert code == 1
    assert "was not changed" in err and "US-WEST surge: missing" in err


def test_cli_rejects_bad_ssh_user(tmp_path, capsys):
    assert main(["collect", str(EXAMPLE), "--out", str(tmp_path / "o"), "--ssh-user", "a b"]) == 2
