import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from proxyfleet.cli import main
from proxyfleet.publish import FetchError, PublishConfig, load_config, publish
from proxyfleet.rotate import RotateError, remote_command, replace_token_in_config, rollback, rotate

from helpers import FakeClock, FakeSftp, make_build

ROOT = Path(__file__).resolve().parents[1]
OLD = "a" * 64
NEW = "b" * 64
LINUX = sys.platform.startswith("linux") and all(shutil.which(t) for t in ("bash", "flock", "sha256sum"))
needs_linux = pytest.mark.skipif(not LINUX, reason="server scripts target Linux")

CONFIG_TEXT = f"""# my publish config
host: sub.example.com
user: proxyfleet
# keep this secret
base_url: https://sub.example.com/{OLD}
"""


def write_config(directory, text=CONFIG_TEXT):
    path = directory / "publish.yaml"
    path.write_text(text, encoding="utf-8")
    if sys.platform != "win32":
        path.chmod(0o600)
    return path


# --- unit ------------------------------------------------------------------------

def test_replace_token_keeps_comments_and_mode(tmp_path):
    path = write_config(tmp_path)
    original = replace_token_in_config(path, OLD, NEW)
    assert original.decode() == CONFIG_TEXT
    assert path.read_text(encoding="utf-8") == CONFIG_TEXT.replace(OLD, NEW)
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["publish.yaml"]


def test_replace_token_requires_exactly_one_occurrence(tmp_path):
    path = write_config(tmp_path, CONFIG_TEXT + f"# old: {OLD}\n")
    with pytest.raises(RotateError):
        replace_token_in_config(path, OLD, NEW)


def test_remote_command_never_carries_tokens():
    config = PublishConfig("sub.example.com", "proxyfleet", f"https://sub.example.com/{OLD}", port=2222)
    root = remote_command(config, "root", "prepare", ["-i", "key"])
    assert root == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", "2222", "-i", "key",
                    "--", "root@sub.example.com", "/usr/local/libexec/proxyfleet-rotate prepare"]
    assert remote_command(config, "admin", "finalize", [])[-1] == "sudo -n /usr/local/libexec/proxyfleet-rotate finalize"
    with pytest.raises(RotateError):
        remote_command(config, "root;reboot", "prepare", [])


def test_cli_requires_confirmation_when_not_interactive(tmp_path, capsys, monkeypatch):
    path = write_config(tmp_path)
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    assert main(["rotate-path", "--config", str(path)]) == 2
    assert "--yes" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == CONFIG_TEXT


# --- against the real server scripts (Linux) ------------------------------------

class Server:
    """The real publish and rotate scripts in a temp dir, with Caddy emulated.

    `caddy` and `systemctl` are stubs; fetch() serves exactly the paths that the
    site file's @subscription matcher allows, like the real Caddy site would.
    """

    def __init__(self, tmp_path):
        self.base = tmp_path / "srv"
        (self.base / "releases").mkdir(parents=True)
        self.token_file = tmp_path / "path-token"
        self.token_file.write_text(OLD + "\n")
        self.site = tmp_path / "proxyfleet.caddy"
        self.site.write_text("sub.example.com {\n"
                             f"    @subscription path /{OLD}/surge.txt /{OLD}/loon.txt /{OLD}/mihomo.yaml\n"
                             "    handle @subscription {\n    }\n}\n")
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        for tool in ("caddy", "systemctl"):
            (stubs / tool).write_text('#!/bin/sh\n[ "$1" = validate ] && exit "${FAKE_CADDY_EXIT:-0}"\nexit 0\n')
            (stubs / tool).chmod(0o755)
        import grp  # POSIX only
        self.env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}",
                    "PF_BASE_DIR": str(self.base), "PF_UPLOAD_DIR": str(self.base / "upload"),
                    "PF_RELEASES_DIR": str(self.base / "releases"), "PF_CURRENT_LINK": str(self.base / "current"),
                    "PF_TOKEN_FILE": str(self.token_file), "PF_LOCK_FILE": str(tmp_path / "publish.lock"),
                    "PF_PUBLISH_GROUP": grp.getgrgid(os.getgid()).gr_name,
                    "PF_CADDY_SITE": str(self.site), "PF_CADDYFILE": str(tmp_path / "Caddyfile")}
        self.commands = []
        self.after = {}

    def publisher(self):
        subprocess.run(["bash", str(ROOT / "server" / "proxyfleet-publish.sh")], env=self.env, check=True,
                       capture_output=True)

    def ssh(self, command, input, **kwargs):
        """Fake ssh: run the helper locally with the same stdin."""
        self.commands.append(command)
        mode = command[-1].split()[-1]
        result = subprocess.run(["bash", str(ROOT / "server" / "proxyfleet-rotate.sh"), mode], input=input,
                                env=self.env, capture_output=True)
        if mode in self.after:
            self.after.pop(mode)()
        return result

    def fetch(self, url):
        path = urlsplit(url).path
        line = next(l for l in self.site.read_text().splitlines() if "@subscription path" in l)
        if path not in line.split()[2:]:
            raise FetchError("HTTP 404", status=404)
        file = self.base / "current" / path.lstrip("/")
        if not file.is_file():
            raise FetchError("HTTP 404", status=404)
        return file.read_bytes()

    def publish(self, build_dir, config):
        publish(build_dir, config, runner=FakeSftp(self.base / "upload", after_upload=self.publisher),
                fetch=self.fetch)


@pytest.fixture
def server(tmp_path):
    return Server(tmp_path)


@pytest.fixture
def published(tmp_path, server):
    config_path = write_config(tmp_path)
    server.publish(make_build(tmp_path, "built"), load_config(config_path))
    return config_path


def do_rotate(server, config_path, **kwargs):
    clock = FakeClock()
    return rotate(config_path, runner=server.ssh, fetch=server.fetch, clock=clock, sleep=clock.sleep,
                  new_token=lambda: NEW, **kwargs)


def url(token, name="surge.txt"):
    return f"https://sub.example.com/{token}/{name}"


@needs_linux
def test_rotation_end_to_end(server, published):
    before = server.fetch(url(OLD))
    result = do_rotate(server, published)
    assert result["files"] == ["loon.txt", "mihomo.yaml", "surge.txt"]
    assert published.read_text(encoding="utf-8") == CONFIG_TEXT.replace(OLD, NEW)
    assert server.fetch(url(NEW)) == before
    with pytest.raises(FetchError):
        server.fetch(url(OLD))
    assert server.token_file.read_text().strip() == NEW
    assert not Path(str(server.token_file) + ".next").exists()
    assert not Path(str(server.site) + ".rotate-backup").exists()
    for command in server.commands:
        assert OLD not in " ".join(command) and NEW not in " ".join(command)


@needs_linux
def test_publish_after_rotation_uses_new_path(tmp_path, server, published):
    do_rotate(server, published)
    rebuilt = make_build(tmp_path, "rebuilt")
    server.publish(rebuilt, load_config(published))
    assert server.fetch(url(NEW)) == (rebuilt / "providers" / "surge.txt").read_bytes()
    assert (server.base / "current" / NEW).is_dir()


@needs_linux
def test_publish_during_rotation_serves_both_paths(tmp_path, server, published):
    def edit(src):
        path = src / "JP-TYO" / "surge.txt"
        path.write_text(path.read_text().replace("password=fake", "password=new"))
    rebuilt = make_build(tmp_path, "rebuilt", edit=edit)
    server.after["prepare"] = lambda: server.publish(rebuilt, load_config(published))
    do_rotate(server, published)
    assert server.fetch(url(NEW)) == (rebuilt / "providers" / "surge.txt").read_bytes()


@needs_linux
def test_caddy_rejection_changes_nothing(server, published, monkeypatch):
    site_before = server.site.read_text()
    server.env["FAKE_CADDY_EXIT"] = "1"
    with pytest.raises(RotateError) as info:
        do_rotate(server, published)
    assert "Caddy rejected the configuration" in str(info.value)
    assert server.site.read_text() == site_before
    assert published.read_text(encoding="utf-8") == CONFIG_TEXT
    assert server.fetch(url(OLD))
    assert not Path(str(server.token_file) + ".next").exists()
    assert not list((server.base / "releases").glob(f"*/{NEW}"))


@needs_linux
def test_failed_new_path_check_rolls_back(server, published):
    real_fetch = server.fetch
    def broken_new_path(target):
        if NEW in target:
            raise FetchError("HTTP 502", status=502)
        return real_fetch(target)
    clock = FakeClock()
    with pytest.raises(RotateError) as info:
        rotate(published, runner=server.ssh, fetch=broken_new_path, clock=clock, sleep=clock.sleep,
               new_token=lambda: NEW)
    message = str(info.value)
    assert "checking the new path" in message and "rolled back" in message
    assert NEW not in message and OLD not in message
    assert published.read_text(encoding="utf-8") == CONFIG_TEXT
    assert server.fetch(url(OLD))
    with pytest.raises(FetchError):
        server.fetch(url(NEW))
    assert not list((server.base / "releases").glob(f"*/{NEW}"))


@needs_linux
def test_manual_rollback_after_interruption(server, published):
    subprocess.run(["bash", str(ROOT / "server" / "proxyfleet-rotate.sh"), "prepare"],
                   input=f"{OLD}\n{NEW}\n".encode(), env=server.env, check=True, capture_output=True)
    assert server.fetch(url(NEW))
    rollback(published, runner=server.ssh)
    assert server.fetch(url(OLD))
    with pytest.raises(FetchError):
        server.fetch(url(NEW))
    assert not Path(str(server.token_file) + ".next").exists()


@needs_linux
def test_stale_config_is_refused_before_touching_the_server(tmp_path, server, published):
    stale = write_config(published.parent, CONFIG_TEXT.replace(OLD, "c" * 64))
    with pytest.raises(RotateError) as info:
        do_rotate(server, stale)
    assert "publish.yaml holds an old path" in str(info.value)
    assert server.commands == []
    assert server.token_file.read_text().strip() == OLD


@needs_linux
def test_second_rotation_while_one_is_pending_is_refused(server, published):
    subprocess.run(["bash", str(ROOT / "server" / "proxyfleet-rotate.sh"), "prepare"],
                   input=f"{OLD}\n{NEW}\n".encode(), env=server.env, check=True, capture_output=True)
    clock = FakeClock()
    with pytest.raises(RotateError) as info:
        rotate(published, runner=server.ssh, fetch=server.fetch, clock=clock, sleep=clock.sleep,
               new_token=lambda: "d" * 64)
    assert "a rotation is already in progress" in str(info.value)
