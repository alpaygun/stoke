"""Install the pinned Antigravity CLI from a verified release tarball."""

import hashlib
import os
import platform
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import BinaryIO
from urllib.request import urlopen

RELEASE_ROOT = "https://storage.googleapis.com/antigravity-public/antigravity-cli"
ARCHIVES = {
    "arm64": "linux-arm/cli_linux_arm64.tar.gz",
    "amd64": "linux-x64/cli_linux_x64.tar.gz",
}


class InstallError(Exception):
    """A safe installation failure."""


def release_arch(target_arch: str, machine: str) -> str:
    """Use Docker's target architecture, or the local machine outside BuildKit."""
    arch = target_arch or {"aarch64": "arm64", "x86_64": "amd64"}.get(machine)
    if arch is None or arch not in ARCHIVES:
        raise InstallError("unsupported architecture")
    return arch


def download_release(url: str, archive: BinaryIO, sha512: str) -> None:
    """Stream the tarball and verify its checksum before opening it."""
    digest = hashlib.sha512()
    with urlopen(url, timeout=120) as response:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            archive.write(chunk)
    if digest.hexdigest() != sha512.lower():
        raise InstallError("SHA-512 checksum mismatch")
    archive.seek(0)


def binary_member(archive: tarfile.TarFile) -> tarfile.TarInfo:
    """Accept a tarball containing only the expected regular file."""
    members = archive.getmembers()
    if (len(members) != 1 or members[0].name != "antigravity"
            or members[0].type not in {tarfile.REGTYPE, tarfile.AREGTYPE}):
        raise InstallError("unexpected release archive contents")
    return members[0]


def install(version: str, arch: str, sha512: str, target: Path) -> None:
    """Copy only the verified executable and atomically replace the target."""
    temporary = None
    try:
        with tempfile.TemporaryFile() as download:
            download_release(f"{RELEASE_ROOT}/{version}/{ARCHIVES[arch]}", download, sha512)
            with tarfile.open(fileobj=download, mode="r:gz") as archive:
                binary = archive.extractfile(binary_member(archive))
                assert binary is not None
                with binary, tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                    temporary = Path(output.name)
                    shutil.copyfileobj(binary, output)
        temporary.chmod(0o755)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    """Read the pinned Docker build arguments from the environment."""
    try:
        arch = release_arch(os.environ.get("TARGETARCH", ""), platform.machine())
        print(f"Installing Antigravity CLI ({arch})", flush=True)
        install(
            os.environ["ANTIGRAVITY_VERSION"], arch, os.environ[f"ANTIGRAVITY_SHA512_{arch.upper()}"],
            Path("/usr/local/bin/agy"),
        )
    except InstallError as error:
        raise SystemExit(f"Antigravity install failed: {error}") from None
    except (OSError, tarfile.TarError, KeyError, ValueError, EOFError):
        raise SystemExit("Antigravity install failed") from None
    print("Installed Antigravity CLI")


if __name__ == "__main__":
    main()
