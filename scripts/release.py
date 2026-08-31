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


# --dry-run のときに立てる。**手元の確認で本物の警報を鳴らさないため。**
# 実測: 発行前の点検で --dry-run を回したら、号スナップショットがまだ無いのは当然なのに
# 「発行中止」の警報が Discord に飛び、発行不良と見分けが付かなくなった(2026-08-31 20:49)。
DRY_RUN = False


def notify(msg: str, ok: bool = True) -> None:
    prefix = "✅" if ok else "🚨"
    text = f"{prefix} アイマスNEWS release: {msg}"
    if DRY_RUN:
        print(f"[dry-run・通知しない] {text}", flush=True)
        return
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


def lint_failure_summary(r: subprocess.CompletedProcess) -> str:
    """lint 失敗時の通知文。エラーの中身(最大5件)まで載せる(「lint 赤」だけでは原因が追えない)。"""
    errs = re.findall(r"^::error(?: file=([^:]*))?::(.*)$", r.stdout, re.MULTILINE)
    if not errs:  # lint 自体のクラッシュ(traceback 等)
        tail = (r.stderr.strip() or r.stdout.strip())[-500:]
        return f"lint が異常終了(exit {r.returncode}):\n```\n{tail}\n```"
    lines = [f"- {Path(f).name + ': ' if f else ''}{m}" for f, m in errs[:5]]
    if len(errs) > 5:
        lines.append(f"- …ほか {len(errs) - 5} 件")
    return f"lint エラー {len(errs)} 件:\n" + "\n".join(lines)


def review_verdict(date: str) -> tuple[str, str]:
    """その号の校閲結果(最後の往復)を読む。(verdict, 説明) を返す。

    compose が `metrics/review-<日付>-<往復数>.json` に残す。
    往復数は数として比べる(文字列順だと 10 が 9 より前に来る)。
    """
    files = sorted((ROOT / "metrics").glob(f"review-{date}-*.json"),
                   key=lambda p: int(p.stem.rsplit("-", 1)[-1]) if p.stem.rsplit("-", 1)[-1].isdigit() else -1)
    if not files:
        return "missing", "校閲の記録が無い(compose が最後まで走っていない)"
    try:
        d = json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception as e:
        return "unreadable", f"{files[-1].name} が読めない: {e}"
    if d.get("verdict") == "approve":
        return "approve", files[-1].name
    blockers = d.get("blockers") or []
    head = "; ".join(f"{b.get('file')}: {(b.get('issue') or '')[:80]}" for b in blockers[:3])
    return "block", f"{files[-1].name} に残ブロック {len(blockers)}件 — {head}"


def deploy_site() -> str:
    """自前オリジンへ配信する。失敗しても発行そのものは巻き戻さない。

    紙面は main への push で確定しており、配信は「届け方」の工程である。
    ここで失敗しても current は直前のリリースを指したままなので、読者には
    前号が見え続ける(壊れた紙面が出るより望ましい)。復旧は deploy.py の再実行。
    """
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "deploy.py")],
                       cwd=ROOT, capture_output=True, text=True, timeout=2400)
    print(r.stdout[-1500:], flush=True)
    if r.returncode == 0:
        return "自前オリジンへ配信済み。"
    tail = (r.stderr.strip() or r.stdout.strip())[-400:]
    notify(f"配信に失敗しました(紙面は main に確定済み。current は前号のまま)。"
           f"`python3 scripts/deploy.py` で再実行してください:\n```\n{tail}\n```", ok=False)
    return "⚠️配信は失敗(要再実行)。"


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
    # 通知の抑止は**最初の判定より前**に立てる。作業ツリーや号スナップショットの
    # 検査は dry でも走るので、ここが遅れると点検で警報が飛ぶ
    global DRY_RUN
    DRY_RUN = dry

    # 0. 作業ツリーが汚れていたら触らない(自動実行の安全弁)
    dirty = git("status", "--porcelain").stdout.strip()
    if dirty:
        files = dirty.splitlines()
        listing = "\n".join(files[:10]) + (f"\n…ほか {len(files) - 10} 件" if len(files) > 10 else "")
        notify(f"{date}: 作業ツリーに未コミットの変更があるため発行を中止:\n```\n{listing}\n```", ok=False)
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

    # 3. マージ前ゲート: 校閲(REQUIREMENTS 4.5)
    #
    # lint しか見ていなかったため、**校閲が approve していない号がそのまま出ていた**
    # (実測: 10号中3号。2026-08-27号は「出典にない事実」の指摘を残したまま配信)。
    # compose はブロックされた記事を落としてから取り直すので、ここまで来て
    # 未 approve なら、機械で落とせない指摘(一面・社説・号スナップショット)が
    # 残っているということ。人が見るべき状態なので発行しない。
    verdict, detail = review_verdict(date)
    if verdict != "approve":
        notify(f"{date}: 発行中止(第{number}号)。校閲が approve していない({detail})", ok=False)
        return 1

    # 4. マージ前ゲート: lint(REQUIREMENTS 4.4)
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lint.py"), "--base", "origin/main"],
        cwd=ROOT, capture_output=True, text=True,
    )
    print(r.stdout[-2000:], flush=True)
    if r.returncode != 0:
        notify(f"{date}: 発行中止(第{number}号)。{lint_failure_summary(r)}", ok=False)
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

    # 5. 自前オリジンへ配信(main へ push した紙面をビルドして current を差し替える)
    #    ここが実際の「読者に届く」工程。GitHub Pages は main への push で
    #    自動追従するフォールバックとして残している。
    deployed = deploy_site()

    # 6. 発行済みブランチの削除 → 翌日ブランチ作成
    git("branch", "-D", branch, check=False)
    if branch_exists(branch, remote=True):
        git("push", "origin", "--delete", branch, check=False)
    ensure_next_branch(nxt, dry=False)

    notify(f"{date}: {label}({count}本)を発行しました。{deployed} 翌日ブランチ {nxt} 準備済み")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr, flush=True)
        tail = "\n".join(tb.strip().splitlines()[-6:])
        notify(f"想定外のエラーで停止: {e}\n```\n{tail}\n```\n(全文: journalctl --user -u imas-release)", ok=False)
        sys.exit(1)
