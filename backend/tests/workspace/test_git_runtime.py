"""The worker uses host Git, or extracts the vendored Linux x86_64 build."""

import collections.abc
import os
import pathlib

import pytest

from hatchery.workspace import git_runtime


@pytest.fixture(autouse=True)
def clear_runtime_cache() -> collections.abc.Iterator[None]:
    git_runtime.resolve_git.cache_clear()
    yield
    git_runtime.resolve_git.cache_clear()


def test_resolve_git_prefers_the_worker_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hatchery.workspace.git_runtime.shutil.which",
        lambda *_args, **_kwargs: "/safe/git",
    )

    runtime = git_runtime.resolve_git()

    assert runtime.executable == "/safe/git"
    assert runtime.environment({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}


def test_resolve_git_extracts_linux_bundle_with_helpers(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "git-runtime"
    monkeypatch.setattr(
        "hatchery.workspace.git_runtime.shutil.which", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("hatchery.workspace.git_runtime.platform.system", lambda: "Linux")
    monkeypatch.setattr("hatchery.workspace.git_runtime.platform.machine", lambda: "x86_64")
    monkeypatch.setattr(git_runtime, "_cache_path", lambda: destination)

    runtime = git_runtime.resolve_git()
    env = runtime.environment({"PATH": "/usr/bin", "HOME": "/tmp/home"})

    assert runtime.executable == str(destination / "bin" / "git")
    assert os.access(runtime.executable, os.X_OK)
    assert env["PATH"] == f"{destination / 'bin'}:/usr/bin"
    assert env["GIT_EXEC_PATH"] == str(destination / "libexec" / "git-core")
    assert pathlib.Path(env["GIT_EXEC_PATH"], "git-remote-http").is_file()
    assert pathlib.Path(env["GIT_EXEC_PATH"], "git-remote-https").is_file()
    assert pathlib.Path(env["GIT_SSL_CAINFO"]).is_file()


def test_resolve_git_rejects_incompatible_gitless_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hatchery.workspace.git_runtime.shutil.which", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("hatchery.workspace.git_runtime.platform.system", lambda: "Linux")
    monkeypatch.setattr("hatchery.workspace.git_runtime.platform.machine", lambda: "aarch64")

    with pytest.raises(
        git_runtime.GitRuntimeUnavailable,
        match="bundled Git supports Linux x86_64",
    ):
        git_runtime.resolve_git()
