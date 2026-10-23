"""Upload a build to a proxyfleet server and confirm it over HTTPS.

The server (server/setup.sh) accepts batches over an SFTP-only account and
publishes them under a secret 64-hex path. The batch's .ready marker is
uploaded last, so the server never sees a partial batch. After uploading,
each file is downloaded over HTTPS until its SHA-256 matches.

The secret path is never printed: messages name files, never URLs.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from .build import PROVIDER_FILES
from .diff import DiffError, load_build
from .fsutil import make_private_dir, write_private

BASE_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/[0-9a-f]{64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
CONFIG_KEYS = {"host", "port", "user", "identity_file", "base_url"}
MAX_DOWNLOAD = 1024 * 1024 + 1


class PublishError(Exception):
    pass


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class PublishConfig:
    host: str
    user: str
    base_url: str
    port: int | None = None
    identity_file: Path | None = None


def load_config(path: Path) -> PublishConfig:
    """Read publish.yaml. It holds the secret path, so it must stay private."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PublishError(f"publish config not found: {path}") from None
    except (yaml.YAMLError, UnicodeDecodeError):
        raise PublishError(f"{path} is not valid YAML") from None
    if not isinstance(data, dict):
        raise PublishError(f"{path}: expected a mapping")
    errors = [f"unknown key {key!r}" for key in data if key not in CONFIG_KEYS]
    host, user, base_url = data.get("host"), data.get("user"), data.get("base_url")
    port, identity = data.get("port"), data.get("identity_file")
    if not isinstance(host, str) or not HOST_RE.match(host):
        errors.append("host: must be a host name or IP address")
    if not isinstance(user, str) or not USER_RE.match(user):
        errors.append("user: must be a Unix user name such as proxyfleet")
    if not isinstance(base_url, str) or not BASE_URL_RE.match(base_url.rstrip("/")):
        # Do not echo the value: it is the secret.
        errors.append("base_url: must be https://<host>/<64 lowercase hex characters>")
    if port is not None and (not isinstance(port, int) or not 1 <= port <= 65535):
        errors.append("port: must be 1-65535")
    if identity is not None and not isinstance(identity, str):
        errors.append("identity_file: must be a path")
    if errors:
        raise PublishError(f"{path} is invalid: " + "; ".join(errors))
    identity_path = Path(identity).expanduser() if identity else None
    if identity_path is not None and not identity_path.is_file():
        raise PublishError(f"identity_file not found: {identity_path}")
    if sys.platform != "win32" and path.stat().st_mode & 0o077:
        raise PublishError(f"{path} is readable by other users; run: chmod 600 {path}")
    return PublishConfig(host, user, base_url.rstrip("/"), port, identity_path)


def https_get(url: str, timeout: float = 15) -> bytes:
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "proxyfleet"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            data = response.read(MAX_DOWNLOAD)
    except urllib.error.HTTPError as error:
        raise FetchError(f"HTTP {error.code}") from None
    except (urllib.error.URLError, ssl.SSLError, OSError) as error:
        reason = getattr(error, "reason", error)
        kind = "TLS error" if isinstance(reason, ssl.SSLError) else "connection failed"
        raise FetchError("timed out" if isinstance(reason, (TimeoutError, socket.timeout)) else kind) from None
    if len(data) >= MAX_DOWNLOAD:
        raise FetchError("response too large")
    return data


def _batch_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(6)


def prepare(build_dir: Path) -> tuple[dict, list[str]]:
    """Load and check a build for publishing. Returns (manifest, clients)."""
    try:
        manifest = load_build(build_dir)
    except DiffError as error:
        raise PublishError(str(error)) from None
    manifest.pop("_details", None)
    if manifest.get("include_observing"):
        raise PublishError("this is a canary build (--include-observing); canaries are never published")
    counts = manifest["contract"].get("counts", {})
    clients = [c for c in PROVIDER_FILES if c in counts]
    if not clients:
        raise PublishError("the build has no clients")
    empty = [c for c in clients if not counts[c]]
    if empty:
        raise PublishError(f"refusing to publish an empty node list for {', '.join(empty)}")
    return manifest, clients


def _write_batch(staging: Path, build_dir: Path, manifest: dict, clients: list[str], batch: str) -> list[str]:
    """Write the batch files and return the upload order, with .ready last."""
    counts = manifest["contract"]["counts"]
    contract = [
        "PF_CONTRACT_VERSION=1",
        "PF_CLIENTS=" + ",".join(clients),
        "PF_INVENTORY_SHA256=" + manifest["inventory_sha256"],
        *[f"PF_{c.upper()}_COUNT={counts[c]}" for c in clients],
    ]
    names = []
    for client in clients:
        name = f"{batch}.{PROVIDER_FILES[client]}"
        write_private(staging / name, (build_dir / "providers" / PROVIDER_FILES[client]).read_bytes())
        names.append(name)
    write_private(staging / f"{batch}.contract.env", ("\n".join(contract) + "\n").encode())
    names.append(f"{batch}.contract.env")
    # LF line endings: the server checks this with sha256sum.
    sums = "".join(f"{hashlib.sha256((staging / n).read_bytes()).hexdigest()}  {n}\n" for n in names)
    write_private(staging / f"{batch}.sha256", sums.encode())
    write_private(staging / f"{batch}.ready", b"ready\n")
    return [*names, f"{batch}.sha256", f"{batch}.ready"]


def sftp_command(config: PublishConfig, batch_file: Path) -> list[str]:
    command = ["sftp", "-b", str(batch_file), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if config.port:
        command += ["-P", str(config.port)]
    if config.identity_file:
        command += ["-i", str(config.identity_file)]
    return command + [f"{config.user}@{config.host}"]


def publish(build_dir: Path, config: PublishConfig, *, verify_timeout: float = 90,
            interval: float = 3, runner: Callable = subprocess.run, fetch: Callable[[str], bytes] = https_get,
            clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
            progress: Callable[[str], None] = lambda _: None) -> dict:
    manifest, clients = prepare(build_dir)
    batch = _batch_id()
    summary = {"batch": batch, "clients": {c: manifest["contract"]["counts"][c] for c in clients}}

    staging = Path(tempfile.gettempdir()) / f"proxyfleet-upload-{batch}"
    make_private_dir(staging)
    try:
        order = _write_batch(staging, build_dir, manifest, clients, batch)
        # sftp runs inside the staging directory, so the batch file holds only
        # ASCII batch names and never a local path (which may be non-ASCII on Windows).
        batch_file = staging / "sftp.batch"
        write_private(batch_file, "".join(f"put {name}\n" for name in order).encode())

        progress(f"uploading batch {batch}")
        try:
            result = runner(sftp_command(config, batch_file), cwd=staging, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=False)
        except FileNotFoundError:
            raise PublishError("sftp not found on PATH") from None
        except subprocess.TimeoutExpired:
            raise PublishError("upload timed out; check the server before retrying") from None
        if result.returncode != 0:
            raise PublishError(f"upload failed (sftp exit {result.returncode})")

        progress("waiting for the server to publish and checking over HTTPS")
        expected = {PROVIDER_FILES[c]: hashlib.sha256((build_dir / "providers" / PROVIDER_FILES[c]).read_bytes()).hexdigest()
                    for c in clients}
        deadline = clock() + verify_timeout
        last = "no check made"
        while True:
            pending = []
            for name, digest in expected.items():
                try:
                    data = fetch(f"{config.base_url}/{name}")
                except FetchError as error:
                    pending.append(f"{name}: {error}")
                    continue
                if hashlib.sha256(data).hexdigest() != digest:
                    pending.append(f"{name}: old content")
            if not pending:
                break
            last = "; ".join(pending)
            if clock() >= deadline:
                raise PublishError(f"uploaded, but HTTPS did not serve the new files within {verify_timeout:g}s "
                                   f"({last}); check the server before retrying")
            sleep(interval)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return summary


def default_config_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "proxyfleet" / "publish.yaml"
