import io
import tarfile

import pytest

from hatchery.worker import transfer
from hatchery.workspace import files


def test_unpack_skips_links_and_git_metadata_but_keeps_modes():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("self/scripts/run")
        info.size, info.mode = 3, 0o755
        archive.addfile(info, io.BytesIO(b"abc"))
        link = tarfile.TarInfo("self/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        archive.addfile(link)
        git = tarfile.TarInfo("self/repo/.git/config")
        git.size = 1
        archive.addfile(git, io.BytesIO(b"x"))
        other = tarfile.TarInfo("collective/other/AGENTS.md")
        other.size = 1
        archive.addfile(other, io.BytesIO(b"b"))

    assert transfer.unpack(buffer.getvalue(), ["self"]) == {
        "self/scripts/run": files.File(b"abc", executable=True)
    }


def test_pack_round_trips_and_rejects_paths_outside_sandbox_roots():
    tree = {
        "self/AGENTS.md": files.File(b"persona"),
        "scratchpad/run.sh": files.File(b"echo", executable=True),
    }
    assert transfer.unpack(transfer.pack(tree), ["self", "scratchpad"]) == tree
    with pytest.raises(ValueError, match="canonical"):
        transfer.pack({"repos/app/file": files.File(b"x")})
