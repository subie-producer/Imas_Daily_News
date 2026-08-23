#!/usr/bin/env bash
# Cloudflare Tunnel の初期構築(1回だけ実行する)。
#
#   bash scripts/tunnel_setup.sh news.example.com
#
# 前提: 先に `cloudflared tunnel login` をブラウザで済ませ、対象ドメインの
#       証明書 (~/.cloudflared/cert.pem) を取得しておくこと。
#
# やること: トンネル作成 → 設定生成 → DNS(CNAME)登録 → systemd 有効化
# ポート開放も固定IPも不要(cloudflared がこのPCから外向きに接続を張るため)。
set -euo pipefail

HOSTNAME_ARG="${1:-}"
TUNNEL_NAME="${TUNNEL_NAME:-imas-news}"
ETC_DIR="$HOME/srv/imas-news/etc"
CONFIG="$ETC_DIR/cloudflared.yml"
ORIGIN="http://127.0.0.1:8080"

if [ -z "$HOSTNAME_ARG" ]; then
  echo "使い方: bash scripts/tunnel_setup.sh <公開ホスト名>" >&2
  echo "  例: bash scripts/tunnel_setup.sh news.example.com" >&2
  exit 2
fi

if [ ! -f "$HOME/.cloudflared/cert.pem" ]; then
  echo "エラー: ~/.cloudflared/cert.pem がありません。先に次を実行してください:" >&2
  echo "  cloudflared tunnel login" >&2
  echo "(ブラウザが開くので、配信に使うドメインを選んで承認する)" >&2
  exit 1
fi

mkdir -p "$ETC_DIR"

# -- 1. トンネル(無ければ作る。再実行しても壊れないように) --
if cloudflared tunnel list --output json 2>/dev/null | grep -q "\"name\":\"$TUNNEL_NAME\""; then
  echo "トンネル '$TUNNEL_NAME' は既に存在します"
else
  echo "トンネル '$TUNNEL_NAME' を作成します"
  cloudflared tunnel create "$TUNNEL_NAME"
fi

TUNNEL_ID="$(cloudflared tunnel list --output json | python3 -c "
import json,sys
for t in json.load(sys.stdin):
    if t['name'] == '$TUNNEL_NAME':
        print(t['id']); break
")"
if [ -z "$TUNNEL_ID" ]; then
  echo "エラー: トンネルIDを取得できませんでした" >&2
  exit 1
fi
echo "トンネルID: $TUNNEL_ID"

# -- 2. 設定ファイル --
cat > "$CONFIG" <<EOF
# アイマスNEWS 配信トンネル(scripts/tunnel_setup.sh が生成)。
# このPCから Cloudflare へ外向きに接続を張り、受けたリクエストをローカルの
# Caddy(127.0.0.1:8080)へ流す。ポート開放・固定IP・自前TLSはいずれも不要。
tunnel: $TUNNEL_ID
credentials-file: $HOME/.cloudflared/$TUNNEL_ID.json

# WSL は再起動やスリープ復帰でネットワークが切れやすいため、再接続を粘らせる
retries: 10
grace-period: 30s

ingress:
  - hostname: $HOSTNAME_ARG
    service: $ORIGIN
  # どのホスト名にも一致しなかった場合(必須の終端ルール)
  - service: http_status:404
EOF
echo "設定を書き出しました: $CONFIG"

# -- 3. DNS(CNAME → トンネル)。既存レコードがあれば上書きされる --
echo "DNS を登録します: $HOSTNAME_ARG → $TUNNEL_NAME"
cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME_ARG"

# -- 4. 設定の妥当性確認 → systemd で常駐 --
cloudflared --config "$CONFIG" tunnel ingress validate

systemctl --user daemon-reload
systemctl --user enable --now imas-tunnel.service
sleep 3
systemctl --user --no-pager status imas-tunnel.service | head -12

echo
echo "完了。次を確認してください:"
echo "  https://$HOSTNAME_ARG/"
echo "  https://$HOSTNAME_ARG/tags/"
echo
echo "あわせて .env の SITE_URL を https://$HOSTNAME_ARG に更新し、"
echo "python3 scripts/deploy.py で絶対URL(feed/sitemap/OGP)を作り直してください。"
