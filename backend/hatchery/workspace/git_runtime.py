"""Resolve Git from the host or Hatchery's pinned Vercel-compatible bundle."""

import collections.abc
import dataclasses
import functools
import hashlib
import importlib.resources
import io
import os
import pathlib
import platform
import shutil
import tarfile
import tempfile
import threading

GIT_VERSION = "2.52.0"
GIT_ARCHIVE = "git-minimal-static-v2.52.0-linux-amd64.tar.xz"
GIT_ARCHIVE_ROOT = "git-minimal-static-v2.52.0-linux-amd64"
GIT_ARCHIVE_SHA256 = "f67868fc539512c1a3fa5757a048839e000010fc75665cbb486747d58bbecb20"

_EXTRACT_LOCK = threading.Lock()


class GitRuntimeUnavailable(RuntimeError):
    """Neither a system Git nor a compatible bundled Git can run."""


@dataclasses.dataclass(frozen=True)
class GitRuntime:
    """A Git executable and any environment required by its relocatable bundle."""

    executable: str
    root: pathlib.Path | None = None

    def environment(self, base: collections.abc.Mapping[str, str]) -> dict[str, str]:
        env = dict(base)
        if self.root is None:
            return env
        env.update(
            {
                "PATH": f"{self.root / 'bin'}{os.pathsep}{base.get('PATH', os.defpath)}",
                "GIT_EXEC_PATH": str(self.root / "libexec" / "git-core"),
                "GIT_TEMPLATE_DIR": str(self.root / "share" / "git-core" / "templates"),
                "GIT_SSL_CAINFO": str(self.root / "share" / "git-minimal" / "curl-ca-bundle.crt"),
            }
        )
        return env


def _cache_path() -> pathlib.Path:
    return (
        pathlib.Path(tempfile.gettempdir())
        / "hatchery-git"
        / f"{GIT_VERSION}-{GIT_ARCHIVE_SHA256[:12]}"
    )


def _valid_bundle(root: pathlib.Path) -> bool:
    required = (
        root / "bin" / "git",
        root / "libexec" / "git-core" / "git-remote-http",
        root / "libexec" / "git-core" / "git-remote-https",
        root / "share" / "git-minimal" / "curl-ca-bundle.crt",
    )
    return all(path.is_file() for path in required)


def _extract_bundle() -> pathlib.Path:
    destination = _cache_path()
    if _valid_bundle(destination):
        return destination

    with _EXTRACT_LOCK:
        if _valid_bundle(destination):
            return destination
        try:
            payload = (
                importlib.resources.files(__package__)
                .joinpath("_vendor", "git", GIT_ARCHIVE)
                .read_bytes()
            )
        except (FileNotFoundError, OSError) as exc:
            raise GitRuntimeUnavailable("bundled Git artifact is missing") from exc
        if hashlib.sha256(payload).hexdigest() != GIT_ARCHIVE_SHA256:
            raise GitRuntimeUnavailable("bundled Git artifact failed its checksum") from None

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = pathlib.Path(tempfile.mkdtemp(prefix=".hatchery-git-", dir=destination.parent))
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:xz") as archive:
                members = archive.getmembers()
                prefix = f"{GIT_ARCHIVE_ROOT}/"
                if any(
                    member.name != GIT_ARCHIVE_ROOT and not member.name.startswith(prefix)
                    for member in members
                ):
                    raise GitRuntimeUnavailable("bundled Git artifact has an invalid layout")
                archive.extractall(temporary, members=members, filter="data")

            extracted = temporary / GIT_ARCHIVE_ROOT
            if not _valid_bundle(extracted):
                raise GitRuntimeUnavailable("bundled Git artifact is incomplete")
            for path in (extracted / "bin", extracted / "libexec" / "git-core"):
                for executable in path.iterdir():
                    if executable.is_file():
                        executable.chmod(executable.stat().st_mode | 0o111)
            try:
                extracted.replace(destination)
            except OSError:
                if not _valid_bundle(destination):
                    raise
            if not _valid_bundle(destination):
                raise GitRuntimeUnavailable("bundled Git could not be prepared")
            return destination
        except (OSError, tarfile.TarError) as exc:
            raise GitRuntimeUnavailable("bundled Git could not be extracted") from exc
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


@functools.lru_cache(maxsize=1)
def resolve_git() -> GitRuntime:
    """Prefer the host Git; fall back to the pinned Vercel Linux x86_64 build."""
    system = shutil.which("git", path=os.defpath)
    if system is not None:
        return GitRuntime(system)
    machine = platform.machine().lower()
    if platform.system() != "Linux" or machine not in {"amd64", "x86_64"}:
        raise GitRuntimeUnavailable(
            "Git is required on this worker; bundled Git supports Linux x86_64"
        )
    root = _extract_bundle()
    return GitRuntime(str(root / "bin" / "git"), root)
