#!/bin/zsh
set -euo pipefail

repo_root="${0:A:h:h}"
runtime_root="$HOME/Library/Application Support/AntraTelegram"
app_root="$runtime_root/app"
venv_root="$runtime_root/.venv"

install -d -m 700 "$runtime_root" "$app_root" "$runtime_root/data" "$runtime_root/Music"

if [[ ! -x "$venv_root/bin/python" ]]; then
  ditto "$repo_root/.venv" "$venv_root"
fi

for package in antra antra_shared antra_telegram; do
  install -d -m 700 "$app_root/$package"
  rsync -a --delete --exclude '__pycache__/' "$repo_root/$package/" "$app_root/$package/"
done

for file in requirements-runtime.txt requirements-telegram.txt; do
  install -m 600 "$repo_root/$file" "$app_root/$file"
done

for file in LICENSE MODIFICATIONS.md; do
  if [[ -f "$repo_root/$file" ]]; then
    install -m 600 "$repo_root/$file" "$app_root/$file"
  fi
done

if [[ ! -f "$app_root/.env.telegram" ]]; then
  install -m 600 "$repo_root/.env.telegram" "$app_root/.env.telegram"
fi

"$venv_root/bin/python" -m compileall -q "$app_root/antra" "$app_root/antra_shared" "$app_root/antra_telegram"
printf 'Telegram runtime deployed to %s\n' "$runtime_root"
