"""Offline tests for the pinned Antigravity CLI installer used in the image build."""

import hashlib
import importlib.util
import io
import os
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def installer() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "install-antigravity.py"
    spec = importlib.util.spec_from_file_location("install_antigravity", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("target,machine,expected", [
    ("arm64", "x86_64", "arm64"), ("amd64", "aarch64", "amd64"),
    ("", "x86_64", "amd64"), ("", "aarch64", "arm64"),
])
def test_installer_arch_selection(installer: ModuleType, target: str, machine: str, expected: str) -> None:
    assert installer.release_arch(target, machine) == expected


@pytest.mark.parametrize("target,machine", [("386", "x86_64"), ("", "i686"), ("aarch64", "x86_64")])
def test_installer_rejects_unsupported_arch(installer: ModuleType, target: str, machine: str) -> None:
    with pytest.raises(installer.InstallError, match="unsupported architecture"):
        installer.release_arch(target, machine)


BINARY = b"synthetic binary"


def tar_data(name: str = "antigravity", kind: bytes = tarfile.REGTYPE) -> bytes:
    file = io.BytesIO()
    with tarfile.open(fileobj=file, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type = kind
        if kind == tarfile.REGTYPE:
            member.size = len(BINARY)
            archive.addfile(member, io.BytesIO(BINARY))
        else:
            member.linkname = "other"
            archive.addfile(member)
    return file.getvalue()


@pytest.mark.parametrize("name,kind", [
    ("other", tarfile.REGTYPE), ("../antigravity", tarfile.REGTYPE),
    ("antigravity", tarfile.SYMTYPE), ("antigravity", tarfile.LNKTYPE), ("antigravity", tarfile.CHRTYPE),
])
def test_installer_rejects_unsafe_member(installer: ModuleType, name: str, kind: bytes) -> None:
    with tarfile.open(fileobj=io.BytesIO(tar_data(name, kind)), mode="r:gz") as archive:
        with pytest.raises(installer.InstallError, match="unexpected release archive contents"):
            installer.binary_member(archive)


@pytest.mark.parametrize("arch,archive_path", [
    ("arm64", "linux-arm/cli_linux_arm64.tar.gz"),
    ("amd64", "linux-x64/cli_linux_x64.tar.gz"),
])
def test_installer_checksum_and_atomic_copy(
    installer: ModuleType, arch: str, archive_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tar_data()
    target = tmp_path / "agy"
    target.write_bytes(b"previous binary")

    def fake_urlopen(url: str, *, timeout: int) -> io.BytesIO:
        assert url == f"{installer.RELEASE_ROOT}/sample-version/{archive_path}"
        assert timeout == 120
        return io.BytesIO(data)

    monkeypatch.setattr(installer, "urlopen", fake_urlopen)
    with pytest.raises(installer.InstallError, match="SHA-512 checksum mismatch"):
        installer.install("sample-version", arch, "0" * 128, target)
    assert target.read_bytes() == b"previous binary"
    installer.install("sample-version", arch, hashlib.sha512(data).hexdigest(), target)
    assert target.read_bytes() == BINARY
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o755
    assert list(tmp_path.iterdir()) == [target]
