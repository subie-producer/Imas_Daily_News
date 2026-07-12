#!/usr/bin/env python3
"""compose: 当日号の紙面を生成する。(PIPELINE §6。毎朝 04:00 起動)

  python3 scripts/compose.py [--plan] [--max-rounds 2]

役割分担: 執筆=Claude(sonnet 系・ヘッドレス) / 機械算出=derive.py / 校閲=Codex。
1. edition ブランチ同期 → 続報キュー(本日トリガー)と素材を整理
2. claude -p が記事群・号・社説・stories/upcoming 更新を書き、lint 自己修正まで行う
3. derive.py --write で機械算出フィールドを確定
4. lint(ゲート) → codex 校閲 → block なら claude に修正させ再校閲(最大 --max-rounds 往復)
5. commit & push(発行は 06:00 の release が行う)
"""
import argparse
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import (ROOT, CLAUDE_MODEL, REVIEW_MODEL, append_metric,
                     checkout_edition_branch, commit_and_push, edition_date, git,
                     notify, now_jst)

PAPER_STAGE = None  # main() で .env から


def todays_triggers(date: str) -> list[dict]:
    p = ROOT / "stock" / "upcoming.yml"
    if not p.exists():
        return []
    out = []
    for e in yaml.safe_load(p.read_text(encoding="utf-8")) or []:
        for t in e.get("triggers", []):
            td = t.get("date")
            td = td.isoformat() if hasattr(td, "isoformat") else str(td)
            if td == date:
                out.append({"dedup_key": e["dedup_key"], "brand": e.get("brand"),
                            "subject": e.get("subject"), "kind": t.get("kind"), "note": t.get("note")})
    return out


def prune_upcoming(date: str) -> None:
    """過去日のトリガーを掃除し、トリガーも pending も無くなったエントリを落とす(無限成長対策)。"""
    p = ROOT / "stock" / "upcoming.yml"
    if not p.exists():
        return
    entries = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    kept = []
    for e in entries:
        trigs = []
        for t in e.get("triggers", []):
            td = t.get("date")
            td = td.isoformat() if hasattr(td, "isoformat") else str(td)
            if td >= date:
                trigs.append(t)
        e["triggers"] = trigs
        if trigs or e.get("pending"):
            kept.append(e)
    header = ("# 続報キュー(PIPELINE.md §3)。初報の compose 時に未来トリガーを予約する。\n"
              "# compose が毎朝、当日トリガーを消化し、過去日トリガーを掃除する。\n")
    p.write_text(header + yaml.safe_dump(kept, allow_unicode=True, sort_keys=False,
                                         default_flow_style=False), encoding="utf-8")


def next_number() -> int:
    from pipelib import load_env
    if load_env().get("PAPER_STAGE", "test") != "live":
        return 0
    nums = []
    for e in (ROOT / "docs" / "_editions").glob("*.md"):
        import re
        m = re.search(r"^number:\s*(\d+)", e.read_text(encoding="utf-8"), re.MULTILINE)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) + 1 if nums else 1


def build_prompt(date: str, number: int, triggers: list[dict]) -> str:
    weekday = "月火水木金土日"[datetime.date.fromisoformat(date).weekday()]
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の編集部です。{date}({weekday}曜)号(number: {number})の紙面を、このリポジトリに作成してください。

## 必読(先に読むこと)
- REQUIREMENTS.md の 3章(データ契約)と 5章(編集規程。特に規程1・2・4・5・8・9・10・11)
- PIPELINE.md の §3(続報の制度化と記事化判定)
- schema/*.json(機械可読契約)
- 既存の号(docs/_posts/・docs/_editions/ の最新日付)を1つ開いて形式を確認

## 素材
- candidates/*.json … 収集済み候補(verify: failed は使用不可)。**出典に使う URL は必ず candidates に存在するものだけ**。記事化基準(対象範囲・続報判定)を満たす候補は本数を理由に落とさないこと
- stock/stories.yml … 既報台帳。published_facts と同内容の記事は書かない(規程8)
- 本日トリガーの続報キュー(必ず記事化候補として処理。あふれは small へ):
{json.dumps(triggers, ensure_ascii=False, indent=2)}

## 作成物
1. docs/_posts/{date}-<slug>.md … 記事。**記事化基準を満たす話題は全部書く。「多いから落とす」ことは禁止**(紙面は無制限)。10〜14本は最低限の目安・上限なし、あふれる日は rank を small に寄せて全て載せる。下限8本・lead は必ず1本(分量規程9・本数規程11)
2. docs/_editions/{date}.md … 号スナップショット(frontmatter のみ。number: {number}, issued_at: "{date}T06:00:00+09:00"。
   pages/article_count/corrected_count/ranking/birthdays は後で scripts/derive.py が上書きするため仮値でよいが、スキーマは満たすこと。digest はあなたが本気で組む: 4群固定・SP1画面制約)
3. docs/_editorials/{date}.md … 社説1本(その日の紙面から1題)
4. stock/stories.yml … 記事化した話題の published_facts を追記(新規話題はエントリ追加)
5. stock/upcoming.yml … 新しく判明した未来日程をトリガー予約(締切前3日・締切・開幕・千秋楽・発売・結果)

## 絶対規則
- candidates の facts に無い事実を書かない(推測・一般知識での補完は禁止)
- 相対表現(本日/昨日/明日)は発行日 {date} 基準。絶対日付と同一文で矛盾させない
- X(x.com)の URL は公式アカウントの一次告知のみ出典に使える(バッジ「公式」)
- バッジ「公式」は**アイマス公式**(公式ポータル・ブランド公式サイト・公式Xアカウント)のみ。レーベル・公式ストア(コロムビア・ランティス・アソビストア等)は「準公式」、その他の主催者・販売元・自治体・コラボ先は「当事者」(REQUIREMENTS 2.5)
- 内規の文言(「全記事に必須」「毎日1本」等)を紙面に書かない(規程10)
- 個人への攻撃・プライバシー侵害になり得る話題、読んだ人が嫌な気分になる炎上・係争は記事化しない。個人の SNS 投稿は単体で記事化しない(規程4・5)。「全部書く」(規程11)より優先する

## 仕上げ
- `python3 scripts/derive.py --date {date} --write` を実行して機械算出フィールドを確定する
- `python3 scripts/lint.py --base origin/main` を実行し、エラー0まで自分で修正する(警告も可能な限り解消)
- 完了したら、作成した記事本数と各 rank の内訳を最後に報告する
"""


def claude_run(prompt: str, timeout: int = 2400) -> str:
    r = subprocess.run(
        ["claude", "-p", prompt, "--model", CLAUDE_MODEL, "--dangerously-skip-permissions"],
        capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=ROOT)
    return r.stdout


def run_lint(date: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "lint.py"), "--base", "origin/main"],
                       capture_output=True, text=True, cwd=ROOT)
    return r.returncode, r.stdout[-1500:]


def codex_review(date: str, round_no: int) -> dict:
    checklist = (ROOT / "prompts" / "review-checklist.md").read_text(encoding="utf-8").replace("{DATE}", date)
    if round_no > 1:
        checklist += f"\n\nこれは再校閲({round_no}回目)です。前回の指摘への修正が反映されています。"
    out = ROOT / "metrics" / f"review-{date}-{round_no}.json"
    r = subprocess.run(
        ["codex", "exec", "-m", REVIEW_MODEL,
         "--output-schema", str(ROOT / "prompts" / "review-schema.json"),
         "--output-last-message", str(out), checklist],
        capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL, cwd=ROOT)
    try:
        return json.loads(out.read_text(encoding="utf-8"))
    except Exception:
        return {"verdict": "block", "blockers": [{"file": "-", "issue": f"校閲実行失敗: {r.stderr[-200:]}", "quote": ""}], "comments": []}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="プロンプトを表示して終了(実行しない)")
    ap.add_argument("--date", default=None, help="対象発行日(再試行用。既定: 次の06:00の日付)")
    ap.add_argument("--max-rounds", type=int, default=2)
    args = ap.parse_args()
    t0 = time.time()
    date = args.date or edition_date()
    branch = f"edition/{date}"
    triggers = None

    if not args.plan and not checkout_edition_branch(date, "compose"):
        return 1
    triggers = todays_triggers(date)
    if not args.plan:
        prune_upcoming(date)
    number = next_number()
    prompt = build_prompt(date, number, triggers)
    if args.plan:
        print(prompt)
        return 0

    if (ROOT / "docs" / "_editions" / f"{date}.md").exists():
        notify("compose", f"{date}: 号スナップショットが既に存在(compose 済み?)。中止")
        return 0

    # 締切ガード(規程: 締切=04:00時点の candidates)。スイープ未実施なら警告して続行
    latest = None
    for md in sorted((ROOT / "metrics").glob("*.json"))[-2:]:
        try:
            for run in json.loads(md.read_text(encoding="utf-8")).get("collect", []):
                latest = max(latest or run["at"], run["at"])
        except Exception:
            pass
    stale = (latest is None or
             (now_jst() - datetime.datetime.fromisoformat(latest)).total_seconds() > 7200)
    if stale:
        notify("compose", f"{date}: 直近2時間の collect 実行記録が無い(締切前スイープ未実施?)。手持ちの candidates で続行", ok=False)

    # 1. 執筆(Claude が lint 自己修正まで行う)
    log = claude_run(prompt)
    print(log[-1500:], flush=True)

    # 2. 機械算出の確定 + lint ゲート
    subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"), "--date", date, "--write"],
                   cwd=ROOT, capture_output=True, text=True)
    code, lint_out = run_lint(date)
    print(lint_out, flush=True)
    if code != 0:
        # 一度だけ Claude に lint 修正を依頼
        claude_run(f"アイマスNEWS {date}号の lint がエラーです。`python3 scripts/lint.py --base origin/main` を実行し、"
                   f"エラー0になるまで docs/ と stock/ を修正してください。修正後 derive.py --date {date} --write も再実行すること。")
        code, lint_out = run_lint(date)
        if code != 0:
            notify("compose", f"{date}: lint 赤が解消できず。人間判断が必要\n{lint_out[-500:]}", ok=False)
            commit_and_push(branch, f"compose {date}: lint未解消(要人間判断)", "compose")
            return 1

    # 3. 校閲往復
    rounds = 0
    review = None
    for rounds in range(1, args.max_rounds + 2):
        review = codex_review(date, rounds)
        if review.get("verdict") == "approve":
            break
        if rounds > args.max_rounds:
            break
        fix = ("校閲AIから以下のブロック指摘がありました。candidates の facts と照合して記事を修正してください。"
               "修正後に derive.py --write と lint を再実行してエラー0にすること。\n"
               + json.dumps(review.get("blockers", []), ensure_ascii=False, indent=2))
        claude_run(fix, timeout=1200)
        subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"), "--date", date, "--write"],
                       cwd=ROOT, capture_output=True, text=True)

    approved = review and review.get("verdict") == "approve"
    code, _ = run_lint(date)
    ok = approved and code == 0
    append_metric("compose", {"edition": date, "rounds": rounds, "approved": bool(approved),
                              "lint_green": code == 0, "duration_s": int(time.time() - t0)})
    commit_and_push(branch, f"compose {date}: 紙面生成(校閲{'approve' if approved else '未approve'}・{rounds}往復)", "compose")
    if ok:
        notify("compose", f"{date}号 準備完了(校閲{rounds}往復で approve)。06:00 に発行されます")
        return 0
    notify("compose", f"{date}号: 校閲未解決または lint 赤。発行前に人間判断が必要", ok=False)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        notify("compose", f"想定外のエラー: {e}", ok=False)
        sys.exit(1)
