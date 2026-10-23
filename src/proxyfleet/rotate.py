"""Rotate the secret subscription path, for example after the URL leaked.

1. Record what the current path serves.
2. prepare:  the server serves the same release under the old and new path.
3. Check the new path serves identical files.
4. Update publish.yaml to the new path.
5. finalize: the server drops the old path.
6. Check the new path still works and the old one returns 404.

If anything fails before finalize, the server is rolled back and publish.yaml
is restored, so the old path keeps working. Neither path is ever printed.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Callable

from .publish import USER_RE, FetchError, PublishConfig, https_get, load_config

FILES = ("surge.txt", "loon.txt", "mihomo.yaml")
HELPER = "/usr/local/libexec/proxyfleet-rotate"
MARKERS = {"prepare": "PF_ROTATE_PREPARED", "finalize": "PF_ROTATE_FINALIZED", "rollback": "PF_ROTATE_ROLLED_BACK"}


class RotateError(Exception):
    pass


def _origin_and_token(base_url: str) -> tuple[str, str]:
    origin, _, token = base_url.rpartition("/")
    return origin, token


def remote_command(config: PublishConfig, admin: str, mode: str, ssh_options: list[str]) -> list[str]:
    if not USER_RE.match(admin):
        raise RotateError("--admin must be a Unix user name")
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if config.port:
        command += ["-p", str(config.port)]
    helper = f"{HELPER} {mode}" if admin == "root" else f"sudo -n {HELPER} {mode}"
    return [*command, *ssh_options, "--", f"{admin}@{config.host}", helper]


def run_remote(runner: Callable, config: PublishConfig, admin: str, mode: str, ssh_options: list[str],
               old: str, new: str = "") -> None:
    """Run one helper phase. Tokens go over stdin, never on a command line."""
    try:
        result = runner(remote_command(config, admin, mode, ssh_options), input=f"{old}\n{new}\n".encode(),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)
    except FileNotFoundError:
        raise RotateError("ssh not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise RotateError(f"{mode} timed out") from None
    if result.returncode == 0 and MARKERS[mode] in result.stdout.decode("utf-8", "replace").split():
        return
    # Only the helper's own messages are shown; they never contain a token.
    reasons = [line[len("rotate: "):] for line in result.stderr.decode("utf-8", "replace").splitlines()
               if line.startswith("rotate: ")]
    raise RotateError(f"{mode} failed on the server" + (f": {reasons[-1]}" if reasons else
                      f" (ssh exit {result.returncode}; is {admin} allowed to run {HELPER}?)"))


def served_files(fetch: Callable[[str], bytes], base_url: str) -> list[str]:
    """The subscription files the path serves. 404 means that client is not published."""
    files = []
    for name in FILES:
        try:
            fetch(f"{base_url}/{name}")
            files.append(name)
        except FetchError as error:
            if error.status != 404:
                raise RotateError(f"cannot read the current subscription ({name}: {error})") from None
    if not files:
        raise RotateError("the path in publish.yaml serves no subscription files "
                          "(nothing published yet, or publish.yaml holds an old path)")
    return files


def _wait(check: Callable[[], list[str]], timeout: float, clock: Callable[[], float],
          sleep: Callable[[float], None], what: str) -> None:
    deadline = clock() + timeout
    while True:
        pending = check()
        if not pending:
            return
        if clock() >= deadline:
            raise RotateError(f"{what} not confirmed within {timeout:g}s ({'; '.join(pending)})")
        sleep(2)


def _same_on_both(fetch, old_base, new_base, names):
    """The new path serves exactly what the old path serves right now.

    Compared live rather than against an earlier snapshot, because a publish
    may land during the rotation; the server then serves it under both paths.
    """
    def check():
        pending = []
        for name in names:
            try:
                if fetch(f"{new_base}/{name}") != fetch(f"{old_base}/{name}"):
                    pending.append(f"{name}: different content")
            except FetchError as error:
                pending.append(f"{name}: {error}")
        return pending
    return check


def _available(fetch, base_url, names):
    def check():
        pending = []
        for name in names:
            try:
                fetch(f"{base_url}/{name}")
            except FetchError as error:
                pending.append(f"{name}: {error}")
        return pending
    return check


def _gone(fetch, base_url, names):
    def check():
        pending = []
        for name in names:
            try:
                fetch(f"{base_url}/{name}")
                pending.append(f"{name}: still served")
            except FetchError as error:
                if error.status != 404:
                    pending.append(f"{name}: {error}")
        return pending
    return check


def replace_token_in_config(path: Path, old: str, new: str) -> bytes:
    """Swap the token in publish.yaml in place, keeping comments and permissions.

    Returns the original bytes so the change can be undone.
    """
    original = path.read_bytes()
    text = original.decode("utf-8")
    if text.count(old) != 1:
        raise RotateError(f"expected the current token exactly once in {path}")
    write_config_atomically(path, text.replace(old, new).encode("utf-8"))
    return original


def write_config_atomically(path: Path, data: bytes) -> None:
    temp = path.with_name(f".{path.name}.rotate-{secrets.token_hex(4)}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def rotate(config_path: Path, *, admin: str = "root", ssh_options: list[str] | None = None,
           timeout: float = 30, runner: Callable = subprocess.run, fetch: Callable[[str], bytes] = https_get,
           clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
           new_token: Callable[[], str] = lambda: secrets.token_hex(32),
           progress: Callable[[str], None] = lambda _: None) -> dict:
    ssh_options = ssh_options or []
    config = load_config(config_path)
    origin, old = _origin_and_token(config.base_url)
    new = new_token()
    new_base = f"{origin}/{new}"

    stage = "reading the current subscription"
    prepared = finalized = False
    original_config = None
    try:
        files = served_files(fetch, config.base_url)
        stage = "prepare"
        progress("serving the subscription under both the old and the new path")
        run_remote(runner, config, admin, "prepare", ssh_options, old, new)
        prepared = True
        stage = "checking the new path"
        _wait(_same_on_both(fetch, config.base_url, new_base, files), timeout, clock, sleep, "new path")
        stage = "updating publish.yaml"
        original_config = replace_token_in_config(config_path, old, new)
        stage = "finalize"
        progress("switching the server to the new path only")
        run_remote(runner, config, admin, "finalize", ssh_options, old, new)
        finalized = True
    except RotateError as error:
        notes = []
        if prepared and not finalized:
            try:
                run_remote(runner, config, admin, "rollback", ssh_options, old)
                notes.append("the server was rolled back and the old path still works")
            except RotateError:
                notes.append("rolling back the server also failed; run `proxyfleet rotate-path --rollback`")
        if original_config is not None and not finalized:
            write_config_atomically(config_path, original_config)
            notes.append("publish.yaml was restored")
        raise RotateError(f"rotation failed during {stage}: {error}" + "".join(f"; {n}" for n in notes)) from None

    # After finalize the new path is permanent; failures here need a manual look, not a rollback.
    progress("checking the new path works and the old path is gone")
    try:
        _wait(_available(fetch, new_base, files), timeout, clock, sleep, "new path after finalize")
        _wait(_gone(fetch, config.base_url, files), timeout, clock, sleep, "old path removal")
    except RotateError as error:
        raise RotateError(f"the rotation finished and publish.yaml uses the new path, but {error}; "
                          f"check the server") from None
    return {"files": sorted(files)}


def rollback(config_path: Path, *, admin: str = "root", ssh_options: list[str] | None = None,
             runner: Callable = subprocess.run) -> None:
    """Undo an unfinished rotation, e.g. after the client was interrupted."""
    config = load_config(config_path)
    _, old = _origin_and_token(config.base_url)
    run_remote(runner, config, admin, "rollback", ssh_options or [], old)
