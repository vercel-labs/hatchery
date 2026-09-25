"""The agent's Python dependency environment, shared by every sandbox command.

Ported from agentmesh `sandbox/dependencies.py`. Root `self/requirements.txt` becomes a
content-addressed virtual environment under `/workspace/.hatchery/venvs`.
"""

import dataclasses
import re

from hatchery.worker import provider

ENVIRONMENT_ROOT = f"{provider.WORKSPACE}/.hatchery"
VENV_ROOT = f"{ENVIRONMENT_ROOT}/venvs"
# The durable Bash child has a 720-second ceiling. Leave room for a maximum
# 480-second user command, this setup, and transport cleanup in one activation.
INSTALL_TIMEOUT_SECONDS = 150

# Fixed paths and single-quoted shell constants keep workspace content out of the
# command language. Package installers run without invocation-specific secrets.
SYNC_SCRIPT = f"""\
set -u
root='{ENVIRONMENT_ROOT}'
venvs='{VENV_ROOT}'
requirements='{provider.WORKSPACE}/self/requirements.txt'
mkdir -p "$venvs" "$root/pip-cache"
exec 9>"$root/install.lock"
flock 9
if [ -f "$requirements" ]; then
  digest="$(sha256sum "$requirements" | cut -d ' ' -f 1)"
else
  digest='none'
fi
venv="$venvs/$digest"
if [ ! -f "$venv/.ready" ]; then
  printf 'installing\\n' > "$root/install.status.tmp"
  mv "$root/install.status.tmp" "$root/install.status"
  rm -rf "$venv"
  if command -v uv >/dev/null 2>&1; then
    UV_CACHE_DIR="$root/pip-cache" uv venv --seed --system-site-packages "$venv" &&
      if [ -f "$requirements" ]; then
        UV_CACHE_DIR="$root/pip-cache" uv pip install --python "$venv/bin/python" -r "$requirements"
      fi
  else
    python3 -m venv --system-site-packages "$venv" &&
      if [ -f "$requirements" ]; then
        "$venv/bin/python" -m pip install --cache-dir "$root/pip-cache" -r "$requirements"
      fi
  fi > "$root/install.log" 2>&1
  outcome=$?
  if [ "$outcome" -ne 0 ]; then
    printf 'failed\\n' > "$root/install.status.tmp"
    mv "$root/install.status.tmp" "$root/install.status"
    tail -c 8192 "$root/install.log" >&2
    exit "$outcome"
  fi
  touch "$venv/.ready"
fi
printf '%s\\n' "$digest" > "$root/requirements.sha256.tmp"
mv "$root/requirements.sha256.tmp" "$root/requirements.sha256"
printf 'ok\\n' > "$root/install.status.tmp"
mv "$root/install.status.tmp" "$root/install.status"
printf '%s\\n' "$venv"
"""


@dataclasses.dataclass(frozen=True)
class PythonEnvironment:
    path: str | None
    warning: str = ""


def environment_from_result(exit_code: int, stdout: str, stderr: str) -> PythonEnvironment:
    """Validate installer output, preserving failures as a warning so Bash can repair them."""
    path = stdout.strip()
    if exit_code == 0 and re.fullmatch(rf"{re.escape(VENV_ROOT)}/(?:none|[0-9a-f]{{64}})", path):
        return PythonEnvironment(path)
    detail = stderr.strip()[:8192] or f"installer exited {exit_code}"
    return PythonEnvironment(None, f"workspace requirements installation failed: {detail}")
