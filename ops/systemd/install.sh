#!/bin/sh
# systemd user timer の導入(PIPELINE.md §9)
set -e
cd "$(dirname "$0")"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cp imas-*.service imas-*.timer "$UNIT_DIR/"
systemctl --user daemon-reload
for t in imas-collect imas-compose imas-release imas-watch; do
  systemctl --user enable --now "$t.timer"
done
# セッションを閉じても user systemd(timer)が生き続けるようにする
loginctl enable-linger "$USER" 2>/dev/null || \
  echo "NOTE: linger を有効化できませんでした。'sudo loginctl enable-linger $USER' を実行してください"
echo "--- 登録済み timer ---"
systemctl --user list-timers 'imas-*' --no-pager
