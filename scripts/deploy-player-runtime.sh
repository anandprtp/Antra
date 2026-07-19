#!/bin/zsh
set -euo pipefail

repo_root="${0:A:h:h}"
source_root="$repo_root/player-web"
runtime_root="$HOME/Library/Application Support/AntraPlayerWeb"
app_root="$runtime_root/app"
lock_changed=1

if [[ -f "$app_root/package-lock.json" ]] && cmp -s \
  "$source_root/package-lock.json" \
  "$app_root/package-lock.json"; then
  lock_changed=0
fi

install -d -m 700 "$runtime_root" "$app_root"
rsync -a --delete \
  --exclude 'node_modules/' \
  --exclude '.vinext/' \
  --exclude '.wrangler/' \
  --exclude 'dist/' \
  "$source_root/" "$app_root/"

cd "$app_root"
if (( lock_changed )) || [[ ! -d node_modules ]]; then
  npm ci
fi
npm run build

printf 'Player runtime deployed to %s\n' "$runtime_root"
