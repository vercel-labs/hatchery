"""The provider contract, exercised through the scripted double."""

import pytest

from hatchery.worker import provider, scripted
from hatchery.workspace import files


def seed(tree: dict[str, bytes] | None = None) -> provider.Seed:
    async def build() -> files.Tree:
        return {path: files.File(content) for path, content in (tree or {}).items()}

    return build


async def test_repeated_acquire_keeps_edits_and_does_not_reseed():
    sandboxes = scripted.ScriptedSandboxProvider()
    first = await sandboxes.acquire("thread", seed=seed({"self/AGENTS.md": b"seed"}))
    await first.sandbox.upload({"self/AGENTS.md": files.File(b"edited")})

    repeated = await sandboxes.acquire("thread", seed=seed({"self/AGENTS.md": b"newer commit"}))

    assert first.fresh and not repeated.fresh
    assert await repeated.sandbox.download(["self"]) == {"self/AGENTS.md": files.File(b"edited")}


async def test_different_names_have_independent_files():
    sandboxes = scripted.ScriptedSandboxProvider()
    alice = await sandboxes.acquire("thread-a", seed=seed({"self/AGENTS.md": b"alice"}))
    bob = await sandboxes.acquire("thread-b", seed=seed({"self/AGENTS.md": b"bob"}))
    await alice.sandbox.upload({"self/AGENTS.md": files.File(b"alice edited")})
    await sandboxes.release(alice.sandbox, keep=True)

    assert alice.sandbox.name == "thread-a" and bob.sandbox.name == "thread-b"
    assert await bob.sandbox.download(["self"]) == {"self/AGENTS.md": files.File(b"bob")}


async def test_inspection_reads_retained_thread_files_outside_git_memory():
    sandboxes = scripted.ScriptedSandboxProvider()
    first = await sandboxes.acquire("thread-a", seed=seed({"self/AGENTS.md": b"Alice"}))
    sandboxes.sandboxes["thread-a"].inspection_files["repos/app/untracked.log"] = b"live\n"
    await sandboxes.release(first.sandbox, keep=True)
    await sandboxes.acquire("thread-b", seed=seed({"self/AGENTS.md": b"Bob"}))

    root = await sandboxes.list_directory("thread-a")
    assert [entry.name for entry in root.entries] == [
        ".hatchery",
        "collective",
        "repos",
        "scratchpad",
        "self",
        "wiki",
    ]
    repository = await sandboxes.list_directory("thread-a", "repos/app")
    assert repository.entries[0].path == "repos/app/untracked.log"
    preview = await sandboxes.read_file("thread-a", repository.entries[0].path, limit=4)
    assert preview.content == b"live" and preview.truncated
    assert (await sandboxes.read_file("thread-b", "self/AGENTS.md", limit=256)).content == b"Bob"


async def test_retained_sandbox_resumes_uncommitted_edits_and_rejects_use_while_stopped():
    sandboxes = scripted.ScriptedSandboxProvider()
    first = await sandboxes.acquire("thread", seed=seed({"self/AGENTS.md": b"committed"}))
    await first.sandbox.upload({"self/AGENTS.md": files.File(b"uncommitted")})
    await sandboxes.release(first.sandbox, keep=True)
    await sandboxes.release(first.sandbox, keep=True)

    with pytest.raises(RuntimeError, match="stopped"):
        await first.sandbox.download(["self"])
    resumed = await sandboxes.acquire("thread", seed=seed())
    assert not resumed.fresh
    assert await resumed.sandbox.download(["self"]) == {
        "self/AGENTS.md": files.File(b"uncommitted")
    }


@pytest.mark.parametrize("how", ["destroyed", "lost"])
async def test_a_missing_sandbox_is_reseeded_fresh_from_the_supplied_tree(how):
    sandboxes = scripted.ScriptedSandboxProvider(persist=how == "destroyed")
    first = await sandboxes.acquire("thread", seed=seed({"self/AGENTS.md": b"old commit"}))
    await first.sandbox.upload({"self/scratch.md": files.File(b"uncommitted")})
    await sandboxes.release(first.sandbox, keep=how == "lost")

    resumed = await sandboxes.acquire("thread", seed=seed({"self/AGENTS.md": b"latest commit"}))

    assert resumed.fresh
    assert await resumed.sandbox.download(["self"]) == {
        "self/AGENTS.md": files.File(b"latest commit")
    }


async def test_scripted_results_are_returned_verbatim_and_unscripted_commands_fail():
    sandboxes = scripted.ScriptedSandboxProvider(
        {"run-job": provider.ExecResult(17, "started\n", "permission denied\n")}
    )
    acquired = await sandboxes.acquire("thread", seed=seed())

    assert await acquired.sandbox.exec("run-job", timeout=1) == provider.ExecResult(
        17, "started\n", "permission denied\n"
    )
    with pytest.raises(AssertionError, match="unscripted command"):
        await acquired.sandbox.exec("exit 0", timeout=1)


INVALID_TRANSFER_PATHS = (
    pytest.param("", id="empty"),
    pytest.param(".", id="working-directory"),
    pytest.param("other/a", id="unknown-root"),
    pytest.param("/self/a", id="absolute"),
    pytest.param("self/../b", id="traversal"),
    pytest.param("self//b", id="repeated-separator"),
    pytest.param("self/./b", id="dot-component"),
    pytest.param("self/a/", id="trailing-separator"),
    pytest.param("self/a\x00b", id="null-byte"),
    pytest.param("self/a\nb", id="newline"),
    pytest.param("self/..\\b", id="backslash"),
    pytest.param("self/.git/config", id="git-database"),
    pytest.param("wiki/nested/.GIT/objects", id="nested-git-database"),
    pytest.param("self", id="root-as-file"),
)


@pytest.mark.parametrize("path", INVALID_TRANSFER_PATHS)
async def test_upload_rejects_noncanonical_paths(path):
    acquired = await scripted.ScriptedSandboxProvider().acquire("thread", seed=seed())
    with pytest.raises(ValueError, match="canonical"):
        await acquired.sandbox.upload({path: files.File(b"bad")})


@pytest.mark.parametrize("path", INVALID_TRANSFER_PATHS[:-1])
async def test_download_rejects_noncanonical_paths(path):
    acquired = await scripted.ScriptedSandboxProvider().acquire("thread", seed=seed())
    with pytest.raises(ValueError, match="canonical"):
        await acquired.sandbox.download([path])


async def test_root_export_includes_new_executables_and_excludes_other_roots():
    sandboxes = scripted.ScriptedSandboxProvider()
    acquired = await sandboxes.acquire(
        "thread", seed=seed({"collective/other/AGENTS.md": b"other"})
    )
    await acquired.sandbox.upload(
        {
            "self/scripts/new.sh": files.File(b"echo hello", executable=True),
            "scratchpad/notes.md": files.File(b"disposable"),
        }
    )

    assert await acquired.sandbox.download(["self"]) == {
        "self/scripts/new.sh": files.File(b"echo hello", executable=True)
    }
    assert await acquired.sandbox.download(["scratchpad"]) == {
        "scratchpad/notes.md": files.File(b"disposable")
    }
    assert await acquired.sandbox.download(["wiki", "self/missing.md"]) == {}


async def test_memory_refresh_preserves_sandbox_local_scratchpad():
    sandboxes = scripted.ScriptedSandboxProvider()
    acquired = await sandboxes.acquire(
        "thread", seed=seed({"self/old.md": b"old", "wiki/keep.md": b"keep"})
    )
    await acquired.sandbox.upload({"scratchpad/notes.md": files.File(b"disposable")})

    await acquired.sandbox.replace(
        ["self", "wiki"],
        {"self/new.md": files.File(b"new"), "wiki/keep.md": files.File(b"updated")},
    )

    assert await acquired.sandbox.download(["scratchpad"]) == {
        "scratchpad/notes.md": files.File(b"disposable")
    }
    assert await acquired.sandbox.download(["self", "wiki"]) == {
        "self/new.md": files.File(b"new"),
        "wiki/keep.md": files.File(b"updated"),
    }


async def test_root_export_omits_python_cache_artifacts():
    acquired = await scripted.ScriptedSandboxProvider().acquire(
        "thread",
        seed=seed(
            {
                "self/main.py": b"print('ok')\n",
                "self/__pycache__/main.cpython-314.pyc": b"bytecode",
                "self/module.pyo": b"optimized",
            }
        ),
    )

    assert await acquired.sandbox.download(["self"]) == {
        "self/main.py": files.File(b"print('ok')\n")
    }


async def test_scripted_file_effects_update_wiki_delete_memory_and_cannot_touch_collective():
    sandboxes = scripted.ScriptedSandboxProvider(
        {
            "remember": scripted.ScriptedCommand(
                provider.ExecResult(0, "remembered"),
                writes={"wiki/new.md": files.File(b"learned")},
                deletes=["self/obsolete.md"],
            ),
            "bad": scripted.ScriptedCommand(
                provider.ExecResult(0), writes={"collective/x/y": files.File(b"bad")}
            ),
        }
    )
    acquired = await sandboxes.acquire("thread", seed=seed({"self/obsolete.md": b"old"}))

    assert await acquired.sandbox.exec("remember", timeout=3) == provider.ExecResult(
        0, "remembered"
    )
    assert await acquired.sandbox.download(["self", "wiki"]) == {
        "wiki/new.md": files.File(b"learned")
    }
    with pytest.raises(ValueError, match="readonly collective"):
        await acquired.sandbox.exec("bad", timeout=3)


async def test_serve_deploy_records_revision_and_data_root():
    sandboxes = scripted.ScriptedSandboxProvider()
    acquired = await sandboxes.acquire("serve", seed=seed(), purpose="serve")
    await acquired.sandbox.deploy({"self/api/hello/route.py": files.File(b"ok")}, "a" * 40)

    assert (await sandboxes.read_file("serve", ".hatchery/serve-revision", limit=64)).content == (
        b"a" * 40
    )
    assert "data" in {entry.name for entry in (await sandboxes.list_directory("serve")).entries}
    with pytest.raises(ValueError, match="Git commit SHA"):
        await acquired.sandbox.deploy({}, "main")
