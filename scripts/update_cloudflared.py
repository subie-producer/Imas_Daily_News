#!/usr/bin/env python3
"""update_cloudflared: 配信のトンネル(cloudflared)を最新の版に上げ、つながることを確かめる。毎月1日 01:00(imas-cloudflared.timer)。

  python3 scripts/update_cloudflared.py [--check]

cloudflared は ~/.local/bin に手で置いた実行ファイルで、自動更新を切って動かしている(--no-autoupdate)。毎日の道具の更新
(update_clis.py)の対象でもなく、2026-10-27 に Cloudflare から「中間証明書の侵害で 2026.10.2 未満は使えなくなる」と告知が来るまで
2026.8.2 のままだった。告知の時点で2か月の猶予があったので、編集長「毎月1日に定期としましょう」。

- 最新の版は GitHub の release から取る。配布元が release の本文に載せている sha256 と照らしてから入れ替える
- 入れ替えの前に、新しい実行ファイルが自分の版を名乗れること(--version)を確かめる
- 旧版は cloudflared.<版>.bak に残す(直近 BACKUP_KEEP 個)
- トンネルを再起動し、今回の起動(InvocationID)の cloudflared が /ready で今 CONNECTIONS 本張れていると言い、外から公開ページが 200 で返るかを確かめる。だめなら旧版に戻して
  再起動し、異常として通知する(当番がなぜなぜする)。途中の例外でも、戻しに失敗しても、必ず通知する
- 再起動で数秒、配信が落ちる(1台構成)。発行(06:00)・組版(03:00〜)と重ならない時刻に回す。止まっていて追いつきで
  発行の時間帯(QUIET_FROM〜QUIET_UNTIL)に起動したときは、その終わりまで待ってから上げる
- --check は今の版と最新の版を表示するだけ(通知も当番の呼び出しもしない)
"""
import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
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
# 再起動しない時間帯(組版 03:00〜発行 06:00 とその前後)。timer の Persistent の追いつきがここに当たったら終わりまで待つ
# (Sol 監査 r1 の later: 01:00 に止まっていた月は、復帰の時刻にそのまま再起動していた)
QUIET_FROM = datetime.time(2, 30)
QUIET_UNTIL = datetime.time(7, 0)


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


def tunnel_invocation() -> str:
    """トンネルの今の起動の InvocationID(起動ごとに変わる。journal の _SYSTEMD_INVOCATION_ID と同じ)。取れなければ空。
    例外は投げない(取れないせいで旧版に戻した後の再起動まで飛ばしていた。Sol 監査 r2)。"""
    try:
        r = subprocess.run(["systemctl", "--user", "show", "-p", "InvocationID", "--value", UNIT],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def metrics_addr(log: str) -> str:
    """今回の起動の journal の「Starting metrics server on 127.0.0.1:20241/metrics」から、metrics の番地。無ければ空。"""
    found = re.findall(r"Starting metrics server on (\S+?)/metrics", log or "")
    return found[-1] if found else ""


def ready_connections(addr: str) -> int:
    """cloudflared の /ready が返す、今張れている接続の本数(readyConnections)。読めなければ 0。
    journal の登録の行を数えると、切れた接続も数えてしまう(Sol 監査 r2: 4本登録・3本切断でも4本と数えた)。"""
    try:
        with urllib.request.urlopen(f"http://{addr}/ready", timeout=10) as r:
            return int(json.load(r).get("readyConnections") or 0)
    except Exception:
        # 接続が無いと /ready は 503 を返す(本文は同じ形)
        return 0


def restart_and_verify() -> tuple[bool, str]:
    """トンネルを再起動し、接続が揃い、外から公開ページが返るか。戻りは (つながったか, 状態の説明)。
    例外は投げない(時間切れなども「つながらない」として返し、呼び手が旧版に戻して通知する。Sol 監査 r1)。"""
    try:
        return _restart_and_verify()
    except Exception as e:
        return False, f"再起動・確認の途中で例外({type(e).__name__}: {e})"


def _restart_and_verify() -> tuple[bool, str]:
    before = tunnel_invocation()
    r = subprocess.run(["systemctl", "--user", "restart", UNIT], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return False, f"再起動に失敗(exit {r.returncode}): {(r.stderr or '').strip()[-200:]}"
    # 数えるのは今回の起動の行だけ(時刻の --since は秒に丸められ、再起動の前の行が入っていた。Sol 監査 r1)
    inv = tunnel_invocation()
    if not inv or inv == before:
        return False, f"再起動しても新しい起動が見えない(InvocationID {before or '空'} → {inv or '空'})"
    # 今回の起動の metrics の番地を journal から取り、/ready の今の本数を見る
    n, addr = 0, ""
    end = time.time() + CONNECT_WAIT
    while time.time() < end:
        if not addr:
            j = subprocess.run(["journalctl", "--user", f"_SYSTEMD_INVOCATION_ID={inv}", "-o", "cat", "--no-pager"],
                               capture_output=True, text=True, timeout=60)
            addr = metrics_addr(j.stdout)
        n = ready_connections(addr) if addr else 0
        if n >= CONNECTIONS:
            break
        time.sleep(5)
    if not addr:
        return False, f"{CONNECT_WAIT}秒で今回の起動の metrics の番地が journal に出ない(接続の本数を確かめられない)"
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


def publishing_wait(now: datetime.datetime) -> float:
    """now が再起動しない時間帯(QUIET_FROM〜QUIET_UNTIL)なら、その終わりまでの秒。外なら 0。"""
    if not (QUIET_FROM <= now.time() < QUIET_UNTIL):
        return 0.0
    return (datetime.datetime.combine(now.date(), QUIET_UNTIL, tzinfo=now.tzinfo) - now).total_seconds()


def wait_out_quiet() -> None:
    """再起動しない時間帯なら、外に出るまで待つ。待ちから戻るたびに時刻を見直す(時計が戻る・待ちが短く終わることがある。
    Sol 監査 r2)。起動のときと、差し替えの直前(取るのに時間がかかって時間帯に入ることがある)に呼ぶ。"""
    while (wait := publishing_wait(now_jst())) > 0:
        print(f"発行の時間帯なので {QUIET_UNTIL:%H:%M} まで待ってから上げる({int(wait)}秒)", flush=True)
        time.sleep(wait)


def fail(msg: str) -> int:
    """更新できなかった・つながらない: 異常として通知し(台帳に入る)、当番に渡す。"""
    notify("update", msg, ok=False)
    diagnose_anomalies("update", now_jst().strftime("%Y-%m-%d"), edition=edition_date())
    return 1


def replace_bin(src: Path) -> None:
    """BIN を src の中身に差し替える。同じディレクトリの .new から os.replace(途中で止まっても半端な実行ファイルを残さない)。
    失敗しても .new は残さない(Sol 監査 r1)。"""
    staged = BIN.with_name(BIN.name + ".new")
    try:
        shutil.copy2(src, staged)
        os.replace(staged, BIN)
    finally:
        staged.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="入れ替えず、今の版と最新の版を表示するだけ(通知しない)")
    args = ap.parse_args()
    if not args.check:
        wait_out_quiet()
    cur = installed_version()
    try:
        rel = latest_release()
    except Exception as e:
        msg = f"cloudflared の最新の版を GitHub から取れない({type(e).__name__}: {e})。{cur or '今の版'} のまま"
        if args.check:      # 表示だけ: 当番を起こさない(Sol 監査 r1)
            print(msg, flush=True)
            return 1
        return fail(msg)
    latest = (rel.get("tag_name") or "").lstrip("v")
    print(f"cloudflared: 今 {cur or '(不明)'} / 最新 {latest}", flush=True)
    if args.check or (cur and cur == latest):
        return 0
    if not BIN.is_file():
        # 手で置いた実行ファイルが無い: 戻す先が無いので入れ替えない(人が置き直す)
        return fail(f"{BIN} が無い。cloudflared {latest} に上げられない(手で置き直す)")
    try:
        return update(cur, latest, rel)
    except Exception as e:
        # 差し替えの前の例外(取った実行ファイルの読み書き・旧版の写し)。BIN は入れ替わっていない
        return fail(f"cloudflared を {latest} に上げる途中で例外({type(e).__name__}: {e})。{cur or '今の版'} のまま")


def update(cur: str, latest: str, rel: dict) -> int:
    name = asset_name()
    want = release_sha256(rel.get("body") or "", name)
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == name), None)
    if not want or not asset:
        return fail(f"cloudflared を {latest} に上げられなかった: release {latest} に {name} の"
                    f"{'実行ファイル' if not asset else ' sha256'}が見当たらない。{cur or '今の版'} のまま")
    # BIN と同じディレクトリの .new に直に取り、照らしてから os.replace で差し替える。差し替えが最後の段で、その後に
    # 後片付けの例外が起きる段を置かない(一時ディレクトリの片付けの失敗で、差し替えた後なのに「旧版のまま」と通知していた。Sol 監査 r2)
    staged = BIN.with_name(BIN.name + ".new")
    bak = BIN.with_name(f"{BIN.name}.{cur or 'unknown'}.bak")
    try:
        failed = ""
        try:
            download(asset["browser_download_url"], staged)
        except Exception as e:
            failed = f"{name} を取れない({type(e).__name__}: {e})"
        if not failed:
            got = hashlib.sha256(staged.read_bytes()).hexdigest()
            staged.chmod(0o755)
            if got != want:
                failed = f"{name} の sha256 が release の記載と違う(記載 {want} / 取得 {got})。入れ替えない"
            elif installed_version(staged) != latest:
                failed = f"取った {name} が版 {latest} を名乗らない({installed_version(staged) or '起動できない'})。入れ替えない"
        if failed:
            return fail(f"cloudflared を {latest} に上げられなかった: {failed}。{cur or '今の版'} のまま")
        shutil.copy2(BIN, bak)
        wait_out_quiet()      # 取るのに時間がかかって発行の時間帯に入っていたら、ここで待つ
        os.replace(staged, BIN)
    finally:
        try:
            staged.unlink(missing_ok=True)
        except OSError as e:
            print(f"(取った実行ファイル {staged} を消せない: {e})", flush=True)
    # ここから先は例外を投げない(差し替えた後に抜けると、新しい版のまま通知もせずに終わる。Sol 監査 r1)
    ok, state = restart_and_verify()
    if ok:
        try:
            prune_backups()
        except OSError as e:
            print(f"(古い旧版の片付けに失敗: {e})", flush=True)
        notify("update", f"cloudflared を更新した: {cur or '(不明)'} → {latest}({state})")
        return 0
    # つながらない: 旧版に戻して再起動し、戻った状態も添えて異常にする
    try:
        replace_bin(bak)
    except Exception as e:
        return fail(f"cloudflared {latest} でトンネルがつながらない({state})。旧版 {cur or '(不明)'} に戻せない"
                    f"({type(e).__name__}: {e})。{bak} を手で {BIN} に戻して imas-tunnel を再起動する")
    ok2, state2 = restart_and_verify()
    return fail(f"cloudflared {latest} でトンネルがつながらない({state})。旧版 {cur or '(不明)'} に戻した: "
                f"{'つながった' if ok2 else 'まだつながらない'}({state2})")


if __name__ == "__main__":
    sys.exit(main())
