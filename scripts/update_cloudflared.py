#!/usr/bin/env python3
"""update_cloudflared: 配信のトンネル(cloudflared)を最新の版に上げ、つながることを確かめる。毎月1日 01:00(imas-cloudflared.timer)。

  python3 scripts/update_cloudflared.py [--check]

cloudflared は ~/.local/bin に手で置いた実行ファイルで、自動更新を切って動かしている(--no-autoupdate)。毎日の道具の更新
(update_clis.py)の対象でもなく、2026-10-27 に Cloudflare から「中間証明書の侵害で 2026.10.2 未満は使えなくなる」と告知が来るまで
2026.8.2 のままだった。告知の時点で2か月の猶予があったので、編集長「毎月1日に定期としましょう」。

- 最新の版は GitHub の release から取る。配布元が release の本文に載せている sha256 と照らしてから入れ替える
- 入れ替えの前に、新しい実行ファイルが自分の版を名乗れること(--version)を確かめる
- 旧版は cloudflared.<版>.bak に残す(直近 BACKUP_KEEP 個)
- トンネルを再起動し、接続が CONNECTIONS 本張れて、外から公開ページが 200 で返るかを確かめる。だめなら旧版に戻して再起動し、異常として通知する
  (当番がなぜなぜする)
- 再起動で数秒、配信が落ちる(1台構成)。発行(06:00)・組版(03:00〜)と重ならない時刻に回す
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import ENV, diagnose_anomalies, edition_date, notify, now_jst

BIN = Path.home() / ".local" / "bin" / "cloudflared"
UNIT = "imas-tunnel.service"
RELEASE_API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
CONNECTIONS = 4          # cloudflared が張る接続の本数(既定の HA 接続数)
CONNECT_WAIT = 120       # 再起動から接続が揃うまで待つ秒
BACKUP_KEEP = 2
UA = {"User-Agent": "ImasNewsBot/1.0"}


def asset_name() -> str:
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    return f"cloudflared-linux-{arch}"


def installed_version(path: Path = BIN) -> str:
    """実行ファイルが名乗る版(「cloudflared version 2026.10.4 (built …)」の 2026.10.4)。読めなければ空。"""
    try:
        r = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"version (\d+\.\d+\.\d+)", r.stdout or "")
    return m.group(1) if r.returncode == 0 and m else ""


def latest_release() -> dict:
    with urllib.request.urlopen(urllib.request.Request(RELEASE_API, headers=UA), timeout=30) as r:
        return json.load(r)


def release_sha256(body: str, name: str) -> str:
    """release の本文の「<ファイル名>: <sha256>」から、そのファイルの sha256 を取る(fips 版などの別名と取り違えない)。"""
    m = re.search(rf"(?m)^\s*{re.escape(name)}:\s*([0-9a-f]{{64}})\s*$", body or "")
    return m.group(1) if m else ""


def download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)


def restart_and_verify() -> tuple[bool, str]:
    """トンネルを再起動し、接続が揃い、外から公開ページが返るか。戻りは (つながったか, 状態の説明)。"""
    since = now_jst().strftime("%Y-%m-%d %H:%M:%S")
    r = subprocess.run(["systemctl", "--user", "restart", UNIT], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return False, f"再起動に失敗(exit {r.returncode}): {(r.stderr or '').strip()[-200:]}"
    n = 0
    end = time.time() + CONNECT_WAIT
    while time.time() < end:
        j = subprocess.run(["journalctl", "--user", "-u", UNIT, "--since", since, "-o", "cat", "--no-pager"],
                           capture_output=True, text=True, timeout=60)
        n = (j.stdout or "").count("Registered tunnel connection")
        if n >= CONNECTIONS:
            break
        time.sleep(5)
    if n < CONNECTIONS:
        return False, f"{CONNECT_WAIT}秒で接続が {n}/{CONNECTIONS} 本しか張れない"
    site = ENV.get("SITE_URL", "").rstrip("/")
    if not site:
        return True, f"接続 {n} 本(SITE_URL 未設定のため外からの確認は省略)"
    # 端のキャッシュが古い 200 を返さないよう、毎回違う問い合わせを付けて取る
    url = f"{site}/?tunnel-check={int(time.time())}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={**UA, "Cache-Control": "no-cache"}), timeout=30) as resp:
            code = resp.status
    except Exception as e:
        return False, f"接続 {n} 本だが、外から {site} を開けない({type(e).__name__}: {e})"
    return code == 200, f"接続 {n} 本・外から {site} が {code}"


def prune_backups() -> None:
    baks = sorted(BIN.parent.glob(BIN.name + ".*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in baks[BACKUP_KEEP:]:
        p.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="入れ替えず、今の版と最新の版を表示するだけ")
    args = ap.parse_args()
    cur = installed_version()
    try:
        rel = latest_release()
    except Exception as e:
        notify("update", f"cloudflared の最新の版を GitHub から取れない({type(e).__name__}: {e})。{cur or '今の版'} のまま", ok=False)
        diagnose_anomalies("update", now_jst().strftime("%Y-%m-%d"), edition=edition_date())
        return 1
    latest = (rel.get("tag_name") or "").lstrip("v")
    print(f"cloudflared: 今 {cur or '(不明)'} / 最新 {latest}", flush=True)
    if args.check or (cur and cur == latest):
        return 0
    name = asset_name()
    want = release_sha256(rel.get("body") or "", name)
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == name), None)
    failed = ""
    with tempfile.TemporaryDirectory(prefix="imas-cloudflared-") as wd:
        new = Path(wd) / name
        if not want or not asset:
            failed = f"release {latest} に {name} の{'実行ファイル' if not asset else ' sha256'}が見当たらない"
        else:
            try:
                download(asset["browser_download_url"], new)
            except Exception as e:
                failed = f"{name} を取れない({type(e).__name__}: {e})"
        if not failed:
            got = hashlib.sha256(new.read_bytes()).hexdigest()
            new.chmod(0o755)
            if got != want:
                failed = f"{name} の sha256 が release の記載と違う(記載 {want} / 取得 {got})。入れ替えない"
            elif installed_version(new) != latest:
                failed = f"取った {name} が版 {latest} を名乗らない({installed_version(new) or '起動できない'})。入れ替えない"
        if failed:
            notify("update", f"cloudflared を {latest} に上げられなかった: {failed}。{cur or '今の版'} のまま", ok=False)
            diagnose_anomalies("update", now_jst().strftime("%Y-%m-%d"), edition=edition_date())
            return 1
        bak = BIN.with_name(f"{BIN.name}.{cur or 'unknown'}.bak")
        shutil.copy2(BIN, bak)
        staged = BIN.with_name(BIN.name + ".new")
        shutil.copy2(new, staged)
        os.replace(staged, BIN)       # 同じディレクトリの中で差し替える(途中で止まっても半端な実行ファイルを残さない)
    ok, state = restart_and_verify()
    if ok:
        prune_backups()
        notify("update", f"cloudflared を更新した: {cur or '(不明)'} → {latest}({state})")
        return 0
    # つながらない: 旧版に戻して再起動し、戻った状態も添えて異常にする
    os.replace(shutil.copy2(bak, BIN.with_name(BIN.name + ".new")), BIN)
    ok2, state2 = restart_and_verify()
    notify("update", f"cloudflared {latest} でトンネルがつながらない({state})。旧版 {cur or '(不明)'} に戻した: "
           f"{'つながった' if ok2 else 'まだつながらない'}({state2})", ok=False)
    diagnose_anomalies("update", now_jst().strftime("%Y-%m-%d"), edition=edition_date())
    return 1


if __name__ == "__main__":
    sys.exit(main())
