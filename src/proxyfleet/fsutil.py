"""Private output directories that are replaced atomically or not at all."""

from __future__ import annotations

import getpass
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

MARKER = "manifest.json"


class OutputError(Exception):
    pass


def make_private_dir(path: Path) -> None:
    """Create a directory only the current user can read, before anything is written into it."""
    path.mkdir(mode=0o700)
    if sys.platform == "win32":
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(OI)(CI)F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode != 0:
            shutil.rmtree(path, ignore_errors=True)
            raise OutputError(f"could not restrict permissions on {path}")
    else:
        os.chmod(path, 0o700)


def write_private(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


class AtomicOutput:
    """Build into a hidden sibling directory; swap it into place only on success.

    An existing destination is replaced only if it is empty or a previous
    proxyfleet output (contains manifest.json), so an unrelated directory
    passed by mistake is never deleted.
    """

    def __init__(self, destination: Path):
        self.destination = destination
        if destination.exists():
            if not destination.is_dir():
                raise OutputError(f"{destination} exists and is not a directory")
            if any(destination.iterdir()) and not (destination / MARKER).is_file():
                raise OutputError(f"{destination} is not empty and is not a proxyfleet output; refusing to replace it")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.staging = destination.parent / f".{destination.name}.tmp-{secrets.token_hex(4)}"

    def __enter__(self) -> Path:
        make_private_dir(self.staging)
        return self.staging

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            shutil.rmtree(self.staging, ignore_errors=True)
            return False
        old = None
        if self.destination.exists():
            old = self.destination.parent / f".{self.destination.name}.old-{secrets.token_hex(4)}"
            os.replace(self.destination, old)
        try:
            os.replace(self.staging, self.destination)
        except OSError:
            if old is not None:
                os.replace(old, self.destination)
            shutil.rmtree(self.staging, ignore_errors=True)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        return False
