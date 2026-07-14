#!/usr/bin/env python3
"""watch: 発行忘れ・パイプライン異常の監視。(PIPELINE §7。毎朝 09:00 起動)

  python3 scripts/watch.py

- 今日の号が main に存在するか(発行確認)
- 発行日を過ぎた edition ブランチの残存(発行忘れ)
- 昨日〜今日の collect 実行回数(metrics)
異常があれば Discord に通知する。正常時は標準出力のみ。
"""
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import ROOT, git, notify, notify_crash, now_jst

def main() -> int:
    today = now_jst().strftime("%Y-%m-%d")
    problems = []
    git("fetch", "origin", "--prune")

    # 1. 今日の号が発行されているか
    if git("cat-file", "-e", f"origin/main:docs/_editions/{today}.md", check=False).returncode != 0:
        problems.append(f"{today} 号が main に存在しない(発行されていない)")

    # 2. 古い edition ブランチの残存
    r = git("branch", "-r", "--list", "origin/edition/*")
    for line in r.stdout.splitlines():
        name = line.strip().removeprefix("origin/")
        date = name.split("/", 1)[1]
        if date <= today and date != (now_jst() + datetime.timedelta(days=0)).strftime("%Y-%m-%d"):
            if date < today:
                problems.append(f"発行日超過の {name} が残存(発行忘れ/失敗)")
    # 今日のブランチが残っている=今日の発行が済んでいない(1 と重複するので個別には出さない)

    # 3. collect の実行回数(今日+昨日)
    runs = 0
    for d in (today, (now_jst() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")):
        p = ROOT / "metrics" / f"{d}.json"
        if p.exists():
            try:
                runs += len(json.loads(p.read_text(encoding="utf-8")).get("collect", []))
            except Exception:
                pass
    if runs == 0:
        problems.append("直近2日間 collect の実行記録が無い(timer 停止?)")

    # 4. 探索エンジンの静かな全滅検知: 直近の collect で claude/grok の取得数合計が 0
    latest = None
    for d in ((now_jst() - datetime.timedelta(days=1)).strftime("%Y-%m-%d"), today):
        p = ROOT / "metrics" / f"{d}.json"
        if p.exists():
            try:
                for run in json.loads(p.read_text(encoding="utf-8")).get("collect", []):
                    if latest is None or run["at"] > latest["at"]:
                        latest = run
            except Exception:
                pass
    if latest:
        for engine in ("claude", "grok"):
            keys = [k for k in latest.get("per_query", {}) if k.startswith(engine + ":")]
            if keys and sum(latest["per_query"][k] for k in keys) == 0:
                problems.append(
                    f"直近の collect({latest['at'][11:16]})で {engine} の取得が全クエリ0件"
                    "(認証切れ・CLI仕様変更・引数エラーの疑い)")

    # 4b. ビルド劣化の先行監視: 記事総数が閾値超過(PIPELINE §9.5 の改修トリガー)
    POSTS_THRESHOLD = 2500
    r = git("ls-tree", "-r", "--name-only", "origin/main", "docs/_posts/")
    n_posts = len([l for l in r.stdout.splitlines() if l.endswith(".md")])
    if n_posts > POSTS_THRESHOLD:
        problems.append(
            f"記事総数 {n_posts} 本が閾値 {POSTS_THRESHOLD} を超過。"
            "Pages ビルド劣化前に『号スナップショットへの記事リスト持たせ』改修を実施すること(PIPELINE §9.5)")

    if problems:
        notify("watch", "異常検知:\n- " + "\n- ".join(problems), ok=False)
        return 1
    print(f"watch OK: {today} 号発行済み・残存ブランチなし・collect {runs}回", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        notify_crash("watch", e)
        sys.exit(1)
