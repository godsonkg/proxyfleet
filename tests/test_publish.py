import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from proxyfleet.cli import main
from proxyfleet.publish import FetchError, PublishConfig, PublishError, load_config, prepare, publish

from helpers import EXAMPLE, make_build

TOKEN = "a" * 64
BASE_URL = f"https://sub.example.com/{TOKEN}"
CONFIG = PublishConfig(host="sub.example.com", user="proxyfleet", base_url=BASE_URL)
SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "server" / "proxyfleet-publish.sh"
LINUX = sys.platform.startswith("linux") and all(shutil.which(t) for t in ("bash", "flock", "sha256sum"))


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
        lines = Path(command[command.index("-b") + 1]).read_text().splitlines()
        local = Path(lines[0][len('lcd "'):-1])
        self.inbox.mkdir(parents=True, exist_ok=True)
        for line in lines[1:]:
            name = line.split()[1]
            shutil.copy(local / name, self.inbox / name)
            self.order.append(name)
        if self.after_upload:
            self.after_upload()
        return subprocess.CompletedProcess(command, 0)


def serve_from(directory):
    """A fetch function that serves <directory>/<file> for BASE_URL/<file>."""
    def fetch(url):
        assert url.startswith(BASE_URL + "/")
        path = directory / url[len(BASE_URL) + 1:]
        if not path.is_file():
            raise FetchError("HTTP 404")
        return path.read_bytes()
    return fetch


@pytest.fixture
def built(tmp_path):
    return make_build(tmp_path, "built")


# --- config -------------------------------------------------------------------

def write_config(tmp_path, **values):
    data = {"host": "sub.example.com", "user": "proxyfleet", "base_url": BASE_URL, **values}
    path = tmp_path / "publish.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    if sys.platform != "win32":
        path.chmod(0o600)
    return path


def test_load_config(tmp_path):
    config = load_config(write_config(tmp_path, port=2222))
    assert config.base_url == BASE_URL and config.port == 2222


@pytest.mark.parametrize("values, message", [
    ({"base_url": f"http://sub.example.com/{TOKEN}"}, "base_url: must be https"),
    ({"base_url": "https://sub.example.com/" + "A" * 64}, "base_url: must be https"),
    ({"base_url": "https://sub.example.com/abc"}, "base_url: must be https"),
    ({"user": "root; rm"}, "user: must be"),
    ({"host": "bad host"}, "host: must be"),
    ({"port": 0}, "port: must be"),
    ({"colour": "blue"}, "unknown key 'colour'"),
])
def test_bad_config(tmp_path, values, message):
    with pytest.raises(PublishError) as info:
        load_config(write_config(tmp_path, **values))
    assert message in str(info.value)


def test_config_errors_never_echo_the_secret(tmp_path):
    secret = "f" * 63
    with pytest.raises(PublishError) as info:
        load_config(write_config(tmp_path, base_url=f"https://sub.example.com/{secret}"))
    assert secret not in str(info.value)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_config_must_be_private(tmp_path):
    path = write_config(tmp_path)
    path.chmod(0o644)
    with pytest.raises(PublishError) as info:
        load_config(path)
    assert "chmod 600" in str(info.value)


def test_missing_identity_file(tmp_path):
    with pytest.raises(PublishError) as info:
        load_config(write_config(tmp_path, identity_file=str(tmp_path / "nope")))
    assert "identity_file not found" in str(info.value)


# --- checks before upload -------------------------------------------------------

def test_canary_builds_are_never_published(tmp_path):
    canary = make_build(tmp_path, "canary", include_observing=True)
    with pytest.raises(PublishError) as info:
        prepare(canary)
    assert "canary" in str(info.value)


def test_empty_client_is_refused(tmp_path):
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for machine in data["machines"]:
        machine["protocols"]["loon"] = []
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(PublishError) as info:
        prepare(make_build(tmp_path, "b", inventory_path=path))
    assert "empty node list for loon" in str(info.value)


def test_hand_edited_build_is_refused(tmp_path, built):
    path = built / "providers" / "surge.txt"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(PublishError) as info:
        prepare(built)
    assert "does not match its manifest" in str(info.value)


# --- upload and verification (fake server) -------------------------------------

def test_upload_order_and_batch_contents(tmp_path, built):
    inbox = tmp_path / "inbox"
    sftp = FakeSftp(inbox)
    # Serve the build itself, as if the server had published it instantly.
    summary = publish(built, CONFIG, runner=sftp, fetch=serve_from(built / "providers"))
    batch = summary["batch"]
    assert summary["clients"] == {"surge": 4, "loon": 4, "mihomo": 4}
    assert sftp.order == [f"{batch}.surge.txt", f"{batch}.loon.txt", f"{batch}.mihomo.yaml",
                          f"{batch}.contract.env", f"{batch}.sha256", f"{batch}.ready"]
    contract = (inbox / f"{batch}.contract.env").read_text()
    assert contract.splitlines() == [
        "PF_CONTRACT_VERSION=1", "PF_CLIENTS=surge,loon,mihomo",
        f"PF_INVENTORY_SHA256={yaml.safe_load((built / 'manifest.json').read_text())['inventory_sha256']}",
        "PF_SURGE_COUNT=4", "PF_LOON_COUNT=4", "PF_MIHOMO_COUNT=4"]
    for line in (inbox / f"{batch}.sha256").read_text().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256((inbox / name).read_bytes()).hexdigest() == digest
    command = sftp.commands[0]
    assert command[:6] == ["sftp", "-b", command[2], "-o", "BatchMode=yes", "-o"]
    assert command[-1] == "proxyfleet@sub.example.com"
    assert not Path(command[2]).exists(), "staging directory must be removed"


def test_waits_until_https_serves_new_files(tmp_path, built):
    clock = FakeClock()
    served = tmp_path / "served"
    served.mkdir()
    for name in ("surge.txt", "loon.txt", "mihomo.yaml"):
        (served / name).write_text("old\n")

    def flip_after_two_polls(url, calls=[]):  # noqa: B006 - deliberate call counter
        calls.append(url)
        if len(calls) == 6:
            shutil.copytree(built / "providers", served, dirs_exist_ok=True)
        return serve_from(served)(url)

    publish(built, CONFIG, runner=FakeSftp(tmp_path / "inbox"), fetch=flip_after_two_polls,
            clock=clock, sleep=clock.sleep)
    assert clock.now == 6


def test_verify_timeout_names_files_not_urls(tmp_path, built):
    clock = FakeClock()
    with pytest.raises(PublishError) as info:
        publish(built, CONFIG, runner=FakeSftp(tmp_path / "inbox"), fetch=serve_from(tmp_path / "nothing"),
                verify_timeout=10, clock=clock, sleep=clock.sleep)
    message = str(info.value)
    assert "within 10s" in message and "surge.txt: HTTP 404" in message
    assert TOKEN not in message and "https://" not in message


def test_sftp_failure(tmp_path, built):
    with pytest.raises(PublishError) as info:
        publish(built, CONFIG, runner=FakeSftp(tmp_path / "inbox", exit_code=255), fetch=lambda url: b"")
    assert "upload failed (sftp exit 255)" in str(info.value)


def test_cli_dry_run_uploads_nothing(built, capsys):
    assert main(["publish", str(built), "--dry-run"]) == 0
    assert "dry run" in capsys.readouterr().out


def test_cli_reports_errors_without_secret(tmp_path, built, capsys):
    config = write_config(tmp_path, base_url="https://sub.example.com/" + "b" * 63)
    assert main(["publish", str(built), "--config", str(config)]) == 1
    assert "b" * 63 not in capsys.readouterr().err


# --- client against the real server script (Linux) ------------------------------

@pytest.fixture
def server(tmp_path):
    import grp  # POSIX only; these tests are skipped elsewhere

    base = tmp_path / "srv"
    (base / "releases").mkdir(parents=True)
    token_file = tmp_path / "path-token"
    token_file.write_text(TOKEN + "\n")
    env = {**os.environ, "PF_BASE_DIR": str(base), "PF_UPLOAD_DIR": str(base / "upload"),
           "PF_RELEASES_DIR": str(base / "releases"), "PF_CURRENT_LINK": str(base / "current"),
           "PF_TOKEN_FILE": str(token_file), "PF_LOCK_FILE": str(tmp_path / "publish.lock"),
           "PF_PUBLISH_GROUP": grp.getgrgid(os.getgid()).gr_name}

    def run():
        return subprocess.run(["bash", str(SERVER_SCRIPT)], env=env, capture_output=True, text=True)
    run.base, run.inbox = base, base / "upload"
    run.live = base / "current" / TOKEN
    return run


@pytest.mark.skipif(not LINUX, reason="server script targets Linux")
def test_end_to_end_with_real_server(tmp_path, built, server):
    results = []
    sftp = FakeSftp(server.inbox, after_upload=lambda: results.append(server()))
    publish(built, CONFIG, runner=sftp, fetch=serve_from(server.live))
    assert results[0].returncode == 0, results[0].stderr
    for name in ("surge.txt", "loon.txt", "mihomo.yaml"):
        assert (server.live / name).read_bytes() == (built / "providers" / name).read_bytes()
    assert sorted(os.listdir(server.inbox)) == []
    assert oct((server.live).stat().st_mode & 0o777) == "0o750"


@pytest.mark.skipif(not LINUX, reason="server script targets Linux")
def test_second_publish_switches_atomically_and_keeps_history(tmp_path, built, server):
    for _ in range(2):
        publish(built, CONFIG, runner=FakeSftp(server.inbox, after_upload=server), fetch=serve_from(server.live))
    assert len(os.listdir(server.base / "releases")) == 2
    assert (server.base / "current").is_symlink()


def upload_only(tmp_path, built, server):
    """Upload a batch without running the server, so a test can tamper with it."""
    sftp = FakeSftp(server.inbox)
    with pytest.raises(PublishError):
        publish(built, CONFIG, runner=sftp, fetch=serve_from(tmp_path / "nothing"), verify_timeout=0)
    return sftp.order[0].split(".")[0]


@pytest.mark.skipif(not LINUX, reason="server script targets Linux")
@pytest.mark.parametrize("tamper, reason", [
    (lambda inbox, b: (inbox / f"{b}.surge.txt").write_text("X = anytls, 192.0.2.9, 1, password=x\n"),
     "SHA-256 mismatch"),
    (lambda inbox, b: (inbox / f"{b}.mihomo.yaml").unlink(), "missing or unsafe batch file"),
    (lambda inbox, b: _rewrite(inbox, b, "contract.env", "PF_SURGE_COUNT=4", "PF_SURGE_COUNT=5"),
     "surge node count 4 does not match contract 5"),
    (lambda inbox, b: _rewrite(inbox, b, "contract.env", "PF_CONTRACT_VERSION=1", "PF_CONTRACT_VERSION=2"),
     "unsupported contract version"),
    (lambda inbox, b: _rewrite(inbox, b, "contract.env", "PF_CLIENTS=surge,loon,mihomo",
                               "PF_CLIENTS=surge,loon,mihomo,quantumult"), "invalid client list"),
])
def test_server_rejects_bad_batches(tmp_path, built, server, tamper, reason):
    batch = upload_only(tmp_path, built, server)
    tamper(server.inbox, batch)
    result = server()
    assert result.returncode == 1
    assert reason in result.stderr
    assert "password" not in result.stderr and "192.0.2" not in result.stderr
    assert (server.inbox / f"{batch}.failed").exists()
    assert not (server.base / "current").exists()


def _rewrite(inbox, batch, suffix, old, new):
    """Edit a batch file and re-sign it, so only the semantic check can catch it."""
    path = inbox / f"{batch}.{suffix}"
    path.write_text(path.read_text().replace(old, new))
    lines = []
    for line in (inbox / f"{batch}.sha256").read_text().splitlines():
        _, name = line.split("  ")
        lines.append(f"{hashlib.sha256((inbox / name).read_bytes()).hexdigest()}  {name}")
    (inbox / f"{batch}.sha256").write_text("\n".join(lines) + "\n")


@pytest.mark.skipif(not LINUX, reason="server script targets Linux")
def test_server_ignores_incomplete_batches(tmp_path, built, server):
    batch = upload_only(tmp_path, built, server)
    (server.inbox / f"{batch}.ready").unlink()
    result = server()
    assert result.returncode == 0 and result.stdout == ""
    assert not (server.base / "current").exists()


@pytest.mark.skipif(not LINUX, reason="server script targets Linux")
def test_server_rejects_bad_batch_names(server):
    server.inbox.mkdir(parents=True)
    (server.inbox / "not-a-batch.ready").write_text("ready\n")
    result = server()
    assert result.returncode == 1 and "invalid name" in result.stderr
