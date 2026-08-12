#!/bin/sh
# Point connect trigger webhooks at *this* code: the latest deployment of the
# current git branch. On main that is plain production targeting (the connect
# default); on any other branch, webhooks follow that branch's newest preview
# deployment, so `git push` is all it takes to update the live bot.
#
# One destination at a time on purpose: two destinations means two deployments
# both reply to every event.
set -e
branch="$(git branch --show-current)"
: "${branch:?detached head — check out a branch first}"

for svc in github slack; do
  vercel connect detach "$svc/fabricator" --yes
  if [ "$branch" = "main" ]; then
    vercel connect attach "$svc/fabricator" --triggers --trigger-path "/chat/v1/$svc" --yes
  else
    vercel connect attach "$svc/fabricator" --triggers --trigger-branch "$branch" --trigger-path "/chat/v1/$svc" --yes
  fi
done
echo "triggers -> $branch"
