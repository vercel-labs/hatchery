# hatchery

see AGENTS.md for layout and dev commands.

## local dev

DevBox sends task events to `HATCHERY_PUBLIC_URL`. On a laptop, run
`./scripts/reverse_proxy.sh`; it exposes port 3000 through Socket Firewall and
prints the commands to export the generated HTTPS origin and run `vercel dev`.
Keep the proxy script open in its terminal. In a sandbox, use its existing
forwarded public origin instead.
