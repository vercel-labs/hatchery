#!/bin/sh
# Expose Vercel's local dev port and print the command needed to start it.
set -eu

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
port=3000
log="$(mktemp -t hatchery-vgrok.XXXXXX)"
vgrok_pid=""

cleanup() {
  trap - EXIT INT TERM
  if [ -n "$vgrok_pid" ]; then
    kill "$vgrok_pid" 2>/dev/null || true
    wait "$vgrok_pid" 2>/dev/null || true
  fi
  rm -f "$log"
}
trap cleanup EXIT INT TERM

command -v sfw >/dev/null 2>&1 || {
  echo "error: sfw is required to run pnpm through Socket Firewall" >&2
  exit 1
}

cd "$root"
sfw pnpm dlx @styfle/vgrok "$port" >"$log" 2>&1 &
vgrok_pid=$!

printf 'Starting vgrok on port %s' "$port"
while :; do
  if grep -q 'Ready at https://' "$log"; then
    break
  fi
  if ! kill -0 "$vgrok_pid" 2>/dev/null; then
    printf '\n' >&2
    cat "$log" >&2
    echo "error: vgrok exited before producing a public URL" >&2
    exit 1
  fi
  printf '.'
  sleep 1
done
printf '\n'

public_url="$(sed -n 's/.*Ready at \(https:\/\/[^[:space:]]*\).*/\1/p' "$log" | tail -n 1)"
printf '\nRun in another terminal:\n\n'
printf "export HATCHERY_PUBLIC_URL='%s'\n" "$public_url"
printf 'vercel dev\n\n'
printf 'Keep this process running to keep the reverse proxy open.\n'

wait "$vgrok_pid"
