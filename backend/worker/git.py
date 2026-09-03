"""Git and GitHub behavior shared by sandbox setup and in-sandbox shims."""

import argparse
import base64
import dataclasses
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid

GITHUB_BOT_NAME = "GitHub"
GITHUB_BOT_EMAIL = "noreply@github.com"
SIGNING_REJECTION = "must have verified signatures"
SIGNING_GUARD = "HATCHERY_SIGN_IN_PROGRESS"
GRAPHQL_URL = "https://api.github.com/graphql"
CREATE_COMMIT_MUTATION = """
mutation CreateCommit($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid signature { isValid } }
  }
}
"""
GIT_CONFIG_ENV = (
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
)
PR_PATTERN = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)(?=$|[^0-9])")


@dataclasses.dataclass(frozen=True)
class GitInvocation:
    subcommand: str = ""
    subcommand_index: int = -1
    directory: str = ""


@dataclasses.dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclasses.dataclass(frozen=True)
class Commit:
    sha: str
    tree: str
    parents: tuple[str, ...]
    message: str
    committer_name: str = ""
    committer_email: str = ""
    signature: str = "N"


def github_network_policy(token: str | None):
    """Inject GitHub authorization at the Sandbox network boundary."""
    from vercel import sandbox as vercel_sandbox

    git_rules = api_rules = ()
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        git_rules = (
            vercel_sandbox.NetworkPolicyRule(
                transform=[vercel_sandbox.NetworkPolicyTransform(headers={"Authorization": f"Basic {basic}"})]
            ),
        )
        api_rules = (
            vercel_sandbox.NetworkPolicyRule(
                transform=[vercel_sandbox.NetworkPolicyTransform(headers={"Authorization": f"Bearer {token}"})]
            ),
        )
    return vercel_sandbox.NetworkPolicy.custom(
        allow={"github.com": git_rules, "api.github.com": api_rules, "*": ()}
    )


async def git_credentials(user_token: str | None = None, connector: str | None = None) -> str | None:
    """Resolve one credential chain: an explicit user grant, then Connect App."""
    if user_token:
        return user_token
    connector = connector or os.environ.get("GITHUB_CONNECTOR")
    if not connector:
        return None
    from vercel import connect

    return await connect.get_token(connector, subject=connect.ConnectAppTokenSubject())


async def configure_git_auth(box) -> None:
    """Canonicalize persistent GitHub auth without storing a real credential."""
    legacy = ("git@github.com:", "ssh://git@github.com/")
    for base in legacy:
        await box.run_process(
            "git",
            ["config", "--global", "--unset-all", f"url.{base}.insteadOf", "^https://github\\.com/?$"],
            capture_output=True,
        )
    helper = '!f() { echo "username=x-access-token"; echo "password=hatchery-network-policy"; }; f'
    await box.run_process(
        "git",
        ["config", "--global", "--unset-all", "credential.helper"],
        capture_output=True,
    )
    await box.run_process(
        "git",
        ["config", "--global", "--replace-all", "credential.https://github.com.helper", helper],
        check=True,
        capture_output=True,
    )
    await box.run_process(
        "git",
        ["config", "--global", "--unset-all", "url.https://github.com/.insteadOf"],
        capture_output=True,
    )
    for value in legacy:
        await box.run_process(
            "git",
            ["config", "--global", "--add", "url.https://github.com/.insteadOf", value],
            check=True,
            capture_output=True,
        )


async def configure_gh(box) -> None:
    """Seed gh's config version only when the config does not exist."""
    await box.run_process(
        "/bin/sh",
        [
            "-lc",
            "set -e; p=${XDG_CONFIG_HOME:-$HOME/.config}/gh/config.yml; "
            "if [ ! -e \"$p\" ]; then mkdir -p \"$(dirname \"$p\")\"; printf 'version: 1\\n' >\"$p\"; chmod 600 \"$p\"; fi",
        ],
        check=True,
        capture_output=True,
    )


async def configure(box, identity: tuple[str, str] | None = None) -> None:
    await configure_git_auth(box)
    await configure_gh(box)
    values = []
    if identity is not None:
        values.extend((("user.name", identity[0]), ("user.email", identity[1])))
    values.extend((("commit.gpgsign", "false"), ("tag.gpgsign", "false")))
    for key, value in values:
        await box.run_process(
            "git",
            ["config", "--global", "--replace-all", key, value],
            check=True,
            capture_output=True,
        )


def parse_git_args(args: list[str]) -> GitInvocation:
    directory = ""
    waiting = ""
    value_options = {
        "-c", "-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
        "--config-env", "--attr-source",
    }
    for index, arg in enumerate(args):
        if waiting:
            if waiting == "-C" and arg:
                directory = arg if os.path.isabs(arg) or not directory else os.path.join(directory, arg)
            waiting = ""
            continue
        if arg in value_options:
            waiting = arg
        elif arg.startswith("-"):
            continue
        else:
            return GitInvocation(arg, index, directory)
    return GitInvocation(directory=directory)


def neutralize_commit_signing(args: list[str]) -> list[str]:
    """Disable explicit commit signing while preserving argv shape."""
    result = list(args)
    invocation = parse_git_args(result)
    if invocation.subcommand != "commit":
        return result
    value_options = {
        "-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c",
        "--reedit-message", "--fixup", "--squash", "--author", "--date", "-t",
        "--template", "--cleanup", "--trailer", "--pathspec-from-file",
    }
    waiting = False
    for index in range(invocation.subcommand_index + 1, len(result)):
        arg = result[index]
        if waiting:
            waiting = False
            continue
        if arg == "--":
            break
        if arg in value_options:
            waiting = True
            continue
        if arg == "-S" or arg.startswith("-S") or arg == "--gpg-sign" or arg.startswith("--gpg-sign="):
            result[index] = "--no-gpg-sign"
    return result


def needs_signed_push(output: str) -> bool:
    return SIGNING_REJECTION in output.lower()


def scrub_git_config_env(env: dict[str, str] | list[str]) -> dict[str, str] | list[str]:
    def unsafe(name: str) -> bool:
        return name in GIT_CONFIG_ENV or name.startswith("GIT_CONFIG_KEY_") or name.startswith("GIT_CONFIG_VALUE_")

    if isinstance(env, dict):
        return {name: value for name, value in env.items() if not unsafe(name)}
    return [entry for entry in env if not unsafe(entry.partition("=")[0])]


def validate_pr_url(url: str) -> bool:
    return PR_PATTERN.fullmatch(url.strip()) is not None


def find_pr_url(output: str) -> str:
    match = PR_PATTERN.search(output)
    return match.group(0) if match else ""


def find_last_pr_url(output: str) -> str:
    matches = list(PR_PATTERN.finditer(output))
    return matches[-1].group(0) if matches else ""


def parse_pr_url(output: str) -> str:
    """Read a created PR URL from gh api JSON, then reduced/plain output."""
    start = output.find("{")
    if start >= 0:
        try:
            value = json.loads(output[start:])
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict) and int(value.get("number") or 0) > 0:
            url = str(value.get("html_url") or "")
            if validate_pr_url(url):
                return url
    return find_pr_url(output)


def _positional_gh_words(args: list[str]) -> list[str]:
    return [arg for arg in args if not arg.startswith("-")][:2]


def _api_pr_create(args: list[str]) -> bool:
    value_flags = {
        "-X", "--method", "-f", "--raw-field", "-F", "--field", "-H", "--header",
        "-q", "--jq", "-t", "--template", "-p", "--preview", "--input", "--hostname", "--cache",
    }
    positional = []
    method = ""
    has_body = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("--", "-"):
            index += 1
            continue
        if not arg.startswith("-"):
            positional.append(arg)
            index += 1
            continue
        name, separator, value = arg.partition("=")
        if not name.startswith("--") and len(arg) > 2:
            name, value, separator = arg[:2], arg[2:], "="
        if not separator and name in value_flags and index + 1 < len(args):
            index += 1
            value = args[index]
        if name in ("-X", "--method"):
            method = value.strip().upper()
        if name in ("-f", "--raw-field", "-F", "--field", "--input"):
            has_body = True
        index += 1
    if len(positional) < 2 or positional[0] != "api":
        return False
    endpoint = positional[1].strip()
    parsed = urllib.parse.urlsplit(endpoint if "://" in endpoint else f"https://api.github.com/{endpoint.lstrip('/')}")
    parts = parsed.path.strip("/").split("/")
    collection = len(parts) == 4 and parts[0] == "repos" and parts[1] and parts[2] and parts[3] == "pulls"
    return bool(collection and (method == "POST" or not method and has_body))


def is_pr_create(args: list[str]) -> bool:
    return _positional_gh_words(args) == ["pr", "create"] or _api_pr_create(args)


def record_pr(url: str, repo_path: str = "", report=None) -> dict[str, str] | None:
    if not validate_pr_url(url):
        return None
    value = {"url": url, "repo_path": repo_path}
    if report is not None:
        report(value)
    return value


def run_gh(args: list[str], *, executable: str = "gh", cwd: str | None = None, env: dict[str, str] | None = None, report=None) -> ProcessResult:
    completed = subprocess.run([executable, *args], cwd=cwd, env=env, text=True, capture_output=True, check=False)
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    if completed.returncode == 0 and is_pr_create(args) and "--dry-run" not in args:
        url = parse_pr_url(completed.stdout) if args and args[0] == "api" else find_last_pr_url(completed.stdout)
        if url:
            record_pr(url, _git_toplevel(cwd), report or _report_pr)
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


def is_signed_by_app(commit: Commit | dict) -> bool:
    name = commit.committer_name if isinstance(commit, Commit) else str(commit.get("committer_name") or "")
    email = commit.committer_email if isinstance(commit, Commit) else str(commit.get("committer_email") or "")
    return name == GITHUB_BOT_NAME and email == GITHUB_BOT_EMAIL


def first_unsigned_commit(commits: list[Commit | dict]) -> int:
    for index, commit in enumerate(commits):
        signature = commit.signature if isinstance(commit, Commit) else str(commit.get("signature") or "N")
        if signature not in ("G", "U") and not is_signed_by_app(commit):
            return index
    return -1


def sign_request(
    commits: list[Commit | dict],
    owner: str,
    repo: str,
    *,
    base_oid: str = "",
    branch: str = "",
    env=None,
) -> dict:
    items = []
    for value in commits:
        commit = value if isinstance(value, Commit) else Commit(**value)
        item = {"sha": commit.sha, "tree_sha": commit.tree, "parents": list(commit.parents), "message": commit.message}
        if commit.committer_name and commit.committer_email and not is_signed_by_app(commit):
            item["original_author"] = {"name": commit.committer_name, "email": commit.committer_email}
        items.append(item)
    request = {
        "repo": {"owner": owner, "name": repo},
        "base_oid": base_oid,
        "branch": branch,
        "commits": items,
    }
    if env is not None:
        request["env"] = scrub_git_config_env(env)
    return request


def origin_owner_repo(path: str = ".", *, remote: str | None = None) -> tuple[str, str]:
    if remote is None:
        completed = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "origin is unavailable")
        remote = completed.stdout.strip()
    match = re.search(r"github\.com[/:]([^/]+)/([^/#]+?)(?:\.git)?$", remote)
    if not match:
        raise ValueError("origin is not a GitHub repository")
    return match.group(1), match.group(2)


def push_with_signing_fallback(
    args: list[str], *, executable: str = "git", cwd: str | None = None,
    env: dict[str, str] | None = None, sign=None,
) -> ProcessResult:
    clean_args = neutralize_commit_signing(args)
    invocation = parse_git_args(clean_args)
    if invocation.subcommand != "push" or (env or os.environ).get(SIGNING_GUARD):
        return _run_inherit(executable, clean_args, cwd, env)
    first = _run_inherit(executable, clean_args, cwd, env)
    if first.returncode == 0 or not needs_signed_push(first.stderr):
        return first
    print("hatchery: push rejected because commits require verified signatures; signing and retrying", file=sys.stderr)
    if sign is None:
        sign = _sign_chain
    repo_dir = invocation.directory or cwd or os.getcwd()
    sign_env = dict(scrub_git_config_env(dict(env or os.environ)))
    sign_env[SIGNING_GUARD] = "1"
    try:
        sign(repo_dir, sign_env)
    except Exception as error:
        print(f"hatchery: signing fallback failed: {error}", file=sys.stderr)
        return first
    return _run_inherit(executable, clean_args, cwd, env)


def _run_inherit(executable: str, args: list[str], cwd: str | None, env: dict[str, str] | None) -> ProcessResult:
    completed = subprocess.run([executable, *args], cwd=cwd, env=env, text=True, capture_output=True, check=False)
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


def _git_toplevel(cwd: str | None) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=cwd, text=True, capture_output=True, check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else str(pathlib.Path(cwd or os.getcwd()).resolve())


def _report_pr(value: dict[str, str]) -> None:
    url = os.environ.get("HATCHERY_PR_URL", "http://127.0.0.1:8787/pr-created")
    payload = dict(value)
    payload["task_id"] = os.environ.get("HATCHERY_ACTIVE_TASK", "")
    payload["workspace"] = os.environ.get("HATCHERY_WORKSPACE", "")
    body = json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=body, headers={"content-type": "application/json"}),
            timeout=10,
        ):
            pass
    except OSError as error:
        print(f"hatchery: pull request was created but could not be recorded: {error}", file=sys.stderr)


def _git(repo_dir: str, env: dict[str, str], *args: str) -> str:
    completed = subprocess.run(
        ["/usr/bin/git", "-C", repo_dir, *args], env=env, text=True,
        capture_output=True, check=False,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed.stdout


def _commit_chain(repo_dir: str, env: dict[str, str]) -> tuple[str, list[Commit]]:
    candidates = ["@{upstream}", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/heads/main"]
    base = ""
    for candidate in candidates:
        try:
            base = _git(repo_dir, env, "merge-base", candidate, "HEAD").strip()
            break
        except RuntimeError:
            continue
    if not base:
        raise RuntimeError("could not find the base branch for commit signing")
    separator = "\x1f"
    record = "\x1e"
    output = _git(
        repo_dir, env, "log", "--reverse", f"--format=%H{separator}%T{separator}%P{separator}%B{separator}%cn{separator}%ce{separator}%G?{record}",
        f"{base}..HEAD",
    )
    commits = []
    for raw in output.split(record):
        if not raw.strip():
            continue
        fields = raw.strip("\n").split(separator)
        if len(fields) != 7:
            raise RuntimeError("could not parse commit chain")
        commits.append(Commit(fields[0], fields[1], tuple(fields[2].split()), fields[3], fields[4], fields[5], fields[6]))
    return base, commits


def _commit_changes(repo_dir: str, env: dict[str, str], parent: str, commit: str) -> dict:
    names = subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            repo_dir,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "--no-renames",
            "-r",
            "-z",
            parent,
            commit,
        ],
        env=env,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    additions = []
    deletions = []
    for raw_path in names:
        if not raw_path:
            continue
        path = raw_path.decode("utf-8")
        old = _git(repo_dir, env, "ls-tree", parent, "--", path).split()
        new = _git(repo_dir, env, "ls-tree", commit, "--", path).split()
        old_mode = old[0] if old else ""
        new_mode = new[0] if new else ""
        if old_mode not in ("", "100644", "100755") or new_mode not in (
            "",
            "100644",
            "100755",
        ):
            raise RuntimeError(f"GitHub signing does not support the Git mode for {path}")
        if old_mode != new_mode and "100755" in (old_mode, new_mode):
            raise RuntimeError(f"GitHub signing does not support executable mode changes for {path}")
        if not new:
            deletions.append({"path": path})
            continue
        content = subprocess.run(
            ["/usr/bin/git", "-C", repo_dir, "show", f"{commit}:{path}"],
            env=env,
            capture_output=True,
            check=True,
        ).stdout
        additions.append(
            {"path": path, "contents": base64.b64encode(content).decode()}
        )
    return {"additions": additions, "deletions": deletions}


def _sign_chain(repo_dir: str, env: dict[str, str]) -> None:
    """Ask GitHub to replay commits onto a temporary, automatically signed branch."""
    base, commits = _commit_chain(repo_dir, env)
    first = first_unsigned_commit(commits)
    if first < 0:
        return
    commits = commits[first:]
    base = commits[0].parents[0] if commits[0].parents else base
    owner, repo = origin_owner_repo(repo_dir)
    branch = f"hatchery/sign-{uuid.uuid4().hex}"
    remote = f"https://github.com/{owner}/{repo}.git"
    _git(repo_dir, env, "push", remote, f"{base}:refs/heads/{branch}")
    try:
        request = sign_request(commits, owner, repo, base_oid=base, branch=branch)
        parent = base
        for item in request["commits"]:
            item["file_changes"] = _commit_changes(
                repo_dir, env, parent, str(item["sha"])
            )
            parent = str(item["sha"])
        signed = []
        expected = base
        for item in request["commits"]:
            message = str(item.get("message") or "")
            headline, separator, message_body = message.partition("\n")
            author = item.get("original_author")
            if isinstance(author, dict) and author.get("name") and author.get("email"):
                trailer = f"Co-Authored-By: {author['name']} <{author['email']}>"
                message_body = message_body.rstrip()
                if trailer not in message_body:
                    message_body = f"{message_body}\n\n{trailer}".strip()
            variables = {
                "input": {
                    "branch": {
                        "repositoryNameWithOwner": f"{owner}/{repo}",
                        "branchName": branch,
                    },
                    "expectedHeadOid": expected,
                    "message": {
                        "headline": headline,
                        **({"body": message_body} if separator or message_body else {}),
                    },
                    "fileChanges": item["file_changes"],
                }
            }
            body = json.dumps({"query": CREATE_COMMIT_MUTATION, "variables": variables}).encode()
            api_request = urllib.request.Request(
                GRAPHQL_URL,
                data=body,
                headers={
                    "authorization": "Bearer sandbox-network-policy-placeholder",
                    "accept": "application/vnd.github+json",
                    "content-type": "application/json",
                    "x-github-api-version": "2026-03-10",
                },
            )
            with urllib.request.urlopen(api_request, timeout=60) as response:
                result = json.load(response)
            errors = result.get("errors") if isinstance(result, dict) else None
            if errors:
                raise RuntimeError(f"GitHub signed commit failed: {str(errors)[:500]}")
            created = result["data"]["createCommitOnBranch"]["commit"]
            if not (created.get("signature") or {}).get("isValid"):
                raise RuntimeError("GitHub created a commit without a valid signature")
            expected = str(created["oid"])
            signed.append(expected)
        _git(repo_dir, env, "fetch", remote, signed[-1])
        _git(repo_dir, env, "reset", "--hard", signed[-1])
    finally:
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                repo_dir,
                "push",
                remote,
                f":refs/heads/{branch}",
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", choices=("git", "gh"))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    real = f"/usr/bin/{parsed.command}"
    if parsed.command == "git":
        return push_with_signing_fallback(parsed.args, executable=real).returncode
    return run_gh(parsed.args, executable=real).returncode


if __name__ == "__main__":
    raise SystemExit(main())
