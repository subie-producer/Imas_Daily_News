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
from pipelib import ROOT, git, notify, now_jst

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

    if problems:
        notify("watch", "異常検知:\n- " + "\n- ".join(problems), ok=False)
        return 1
    print(f"watch OK: {today} 号発行済み・残存ブランチなし・collect {runs}回", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        notify("watch", f"想定外のエラー: {e}", ok=False)
        sys.exit(1)
