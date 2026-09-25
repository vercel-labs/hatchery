import pytest

from hatchery.worker import provider


def test_sandbox_names_are_stable_distinct_portable_and_never_empty():
    name = provider.sandbox_name("hatchery:chat_1:thread")
    assert name == provider.sandbox_name("hatchery:chat_1:thread")
    assert name != provider.sandbox_name("hatchery:chat_2:thread")
    assert name.startswith("hatchery-")
    assert provider.validate_name(name) == name
    with pytest.raises(ValueError, match="process_id"):
        provider.sandbox_name("")


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "self//note", "self/./note"])
def test_inspection_paths_cannot_escape_workspace(path):
    with pytest.raises(ValueError, match="relative to /workspace"):
        provider.validate_workspace_path(path)


def test_inspection_paths_include_runtime_and_repository_roots():
    assert provider.validate_workspace_path(".hatchery/install.log") == ".hatchery/install.log"
    assert provider.validate_workspace_path("repos/app/README.md") == "repos/app/README.md"
    assert provider.validate_workspace_path("", allow_root=True) == ""


def test_timeouts_are_exit_124():
    assert provider.ExecResult(124).timed_out
    assert not provider.ExecResult(1).timed_out
