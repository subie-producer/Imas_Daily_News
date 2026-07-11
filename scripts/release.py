#!/usr/bin/env python3
"""発行(release): edition/<発行日> ブランチを main へ squash merge して push する。

PIPELINE.md §5 の実装。毎朝 06:00 JST に systemd user timer(ops/systemd/)から起動される。

  python3 scripts/release.py [--date YYYY-MM-DD] [--dry-run]

手順: 前提確認 → lint(赤なら中止) → main へ squash merge → push →
      発行済みブランチ削除 → 翌日ブランチ作成 → Discord 通知
標準ライブラリのみで動く(lint はサブプロセスで system python3 を使う)。
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
JST = ZoneInfo("Asia/Tokyo")


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()


def notify(msg: str, ok: bool = True) -> None:
    prefix = "✅" if ok else "🚨"
    text = f"{prefix} アイマスNEWS release: {msg}"
    print(text, flush=True)
    url = ENV.get("DISCORD_WEBHOOK_URL")
    if url:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({"content": text}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "ImasNewsBot/1.0"},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:  # 通知失敗は発行を止めない
            print(f"(Discord 通知失敗: {e})", flush=True)


def git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗: {r.stderr.strip()}")
    return r


def branch_exists(name: str, remote: bool = False) -> bool:
    ref = f"refs/remotes/origin/{name}" if remote else f"refs/heads/{name}"
    return git("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def ensure_next_branch(next_name: str, dry: bool) -> None:
    if branch_exists(next_name) or branch_exists(next_name, remote=True):
        print(f"翌日ブランチ {next_name} は既に存在", flush=True)
        if branch_exists(next_name):
            git("checkout", next_name)
        return
    if dry:
        print(f"[dry-run] {next_name} を main から作成して push する", flush=True)
        return
    git("checkout", "-b", next_name, "main")
    git("push", "-u", "origin", next_name)
    print(f"翌日ブランチ {next_name} を作成・push", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="発行日(既定: JST 今日)")
    ap.add_argument("--dry-run", action="store_true", help="push せず手順の検証のみ")
    args = ap.parse_args()
    date = args.date or datetime.datetime.now(JST).strftime("%Y-%m-%d")
    nxt = "edition/" + (datetime.date.fromisoformat(date) + datetime.timedelta(days=1)).isoformat()
    branch = f"edition/{date}"
    dry = args.dry_run

    # 0. 作業ツリーが汚れていたら触らない(自動実行の安全弁)
    if git("status", "--porcelain").stdout.strip():
        notify(f"{date}: 作業ツリーに未コミットの変更があるため発行を中止", ok=False)
        return 1

    git("fetch", "origin", "--prune")

    # 1. 発行対象ブランチの確認(既発行・休刊の判定)
    if not branch_exists(branch) and not branch_exists(branch, remote=True):
        if git("cat-file", "-e", f"origin/main:docs/_editions/{date}.md", check=False).returncode == 0:
            print(f"{date} 号は発行済み(main に存在)", flush=True)
            git("checkout", "main")
            git("pull", "--ff-only", "origin", "main")
            ensure_next_branch(nxt, dry)
            return 0
        notify(f"{date}: edition ブランチが存在しない(収集・compose 未実行?)。発行なし", ok=False)
        return 1

    # 2. ブランチを origin と同期して checkout
    if branch_exists(branch, remote=True):
        git("checkout", "-B", branch, f"origin/{branch}")
    else:
        git("checkout", branch)

    ed_file = ROOT / "docs" / "_editions" / f"{date}.md"
    if not ed_file.exists():
        notify(f"{date}: ブランチに号スナップショットが無い(compose 未実行?)。発行中止", ok=False)
        return 1
    m = re.search(r"^number:\s*(\d+)", ed_file.read_text(encoding="utf-8"), re.MULTILINE)
    number = int(m.group(1)) if m else -1
    m2 = re.search(r"^article_count:\s*(\d+)", ed_file.read_text(encoding="utf-8"), re.MULTILINE)
    count = int(m2.group(1)) if m2 else 0

    # 3. マージ前ゲート: lint(REQUIREMENTS 4.4)
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lint.py"), "--base", "origin/main"],
        cwd=ROOT, capture_output=True, text=True,
    )
    print(r.stdout[-2000:], flush=True)
    if r.returncode != 0:
        notify(f"{date}: lint 赤のため発行中止(第{number}号)", ok=False)
        return 1

    label = f"第{number}号" + ("(試験)" if number == 0 else "")
    if dry:
        notify(f"[dry-run] {date}: {label}({count}本)を squash merge → push → {branch} 削除 → {nxt} 作成、の手順を確認。実行は行わない")
        git("checkout", "main")
        return 0

    # 4. squash merge → push(これが「発行」)
    git("checkout", "main")
    git("pull", "--ff-only", "origin", "main")
    git("merge", "--squash", branch)
    if not git("status", "--porcelain").stdout.strip():
        print("差分なし(発行済み?)。コミットせず終了", flush=True)
    else:
        git("commit", "-m", f"{label} {date} 発行({count}本)")
        git("push", "origin", "main")

    # 5. 発行済みブランチの削除 → 翌日ブランチ作成
    git("branch", "-D", branch, check=False)
    if branch_exists(branch, remote=True):
        git("push", "origin", "--delete", branch, check=False)
    ensure_next_branch(nxt, dry=False)

    notify(f"{date}: {label}({count}本)を発行しました。翌日ブランチ {nxt} 準備済み")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        notify(f"想定外のエラー: {e}", ok=False)
        sys.exit(1)
