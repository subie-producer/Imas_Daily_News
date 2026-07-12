#!/usr/bin/env python3
"""compose: 当日号の紙面を生成する。(PIPELINE §6。毎朝 04:00 起動)

  python3 scripts/compose.py [--plan] [--max-rounds 2]

役割分担: 執筆=Claude(sonnet 系・ヘッドレス) / 機械算出=derive.py / 校閲=Codex。
二段構成(執筆時コンタミの構造的排除):
1. edition ブランチ同期 → 続報キュー(本日トリガー)と素材を整理
2. 選定: claude が候補全体から記事計画(slug→candidate_ids 対応表)を JSON で出力
   → compose が機械検証(候補の実在・verify・blocklist・lead 一意)
3. 個別執筆: 記事ごとに、計画で選ばれた候補 JSON だけを機械的に切り出して渡し、
   独立した claude セッションが1本書く(他候補の情報がコンテキストに存在しない)
4. 組版: 別セッションが号スナップショット(digest)・社説・stories/scheduled 更新
5. derive.py --write → lint(ゲート) → codex 校閲往復 → commit & push(発行は 06:00 の release)
"""
import argparse
import datetime
import json
import re
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


def load_scheduled(date: str) -> list[dict]:
    """発行日の続報予約(素材スナップショット同梱)。過去の candidates を遡らないための日付別ストア。"""
    p = ROOT / "stock" / "scheduled" / f"{date}.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


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


BRANDS = {"general", "765", "cg", "million", "shiny", "sidem", "gaku", "dsva", "joint", "other"}
RANKS = {"lead", "large", "medium", "small"}
# 出典バッジの信頼順(強い順)。記事 src は素材候補のうち最も強いもの
SRC_ORDER = ["公式", "準公式", "当事者", "報道", "ファン", "もちより", "未確認"]


def load_window_candidates(date: str) -> dict:
    """当該号の素材索引(id→素材)。号ファイル candidates/<date>.json(収集は号キーで蓄積)+
    当日の続報予約(stock/scheduled)のみ。過去の candidates は読まない。"""
    items = {}
    p = ROOT / "candidates" / f"{date}.json"
    if p.exists():
        for c in json.loads(p.read_text(encoding="utf-8")):
            if c.get("id"):
                items[c["id"]] = c
    for s in load_scheduled(date):
        items[s["id"]] = s
    return items


def load_blocklist() -> dict:
    p = ROOT / "stock" / "blocklist.yml"
    if not p.exists():
        return {}
    return {e["dedup_key"]: e.get("reason", "") for e in yaml.safe_load(p.read_text(encoding="utf-8")) or []}


def plan_prompt(date: str, number: int, triggers: list[dict], feedback: str = "") -> str:
    weekday = "月火水木金土日"[datetime.date.fromisoformat(date).weekday()]
    files = f"candidates/{date}.json"
    fb = f"\n## 前回計画の機械検証エラー(必ず解消すること)\n{feedback}\n" if feedback else ""
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の編集長です。{date}({weekday}曜)号の**記事計画**(どの話題を、どの候補を素材に、どの扱いで書くか)だけを作ってください。記事本文はまだ書きません。

## 素材(読むもの)
- {files} … 収集済み候補。各要素の id が候補IDです
- stock/stories.yml … 既報台帳(published_facts と同内容=新事実なしの話題は記事化しない。編集規程8)
- stock/blocklist.yml … 使用禁止候補(dedup_key 単位。verify 値に関わらず素材にしない)
- REQUIREMENTS.md 5章(編集規程)
- stock/pending.yml … 日付未確定の追跡事項(新観測で日付が判明していたら計画に入れる)
- 本日の続報予約(stock/scheduled/{date}.json。**必ず記事化する**。あふれは small へ。
  各予約の id は素材スナップショットとして candidate_ids に使える。新観測があれば統合すること):
{json.dumps([{k: t.get(k) for k in ("id", "dedup_key", "brand", "subject", "kind", "note")} for t in triggers], ensure_ascii=False, indent=2)}

## 選定規則
- verify が failed の候補・blocklist の候補は素材にしない
- 同一話題を複数エンジンが観測している場合は1記事に統合し、その記事の candidate_ids に全部載せる
- 記事化基準を満たす話題は**全部**計画に入れる(「多いから落とす」は禁止。紙面は無制限。編集規程11)。10〜14本は最低限の目安・下限8本
- lead はちょうど1本。その日最も重要な話題に与える
- 個人への攻撃・プライバシー侵害になり得る話題、読んだ人が嫌な気分になる炎上・係争は入れない。個人の SNS 投稿は単体で記事化しない(規程4・5。規程11より優先)
- 声優個人のアイマス外活動・関係者の動向は対象外(規程4)
{fb}
## 出力
`metrics/plan-{date}.json` に次の形式の JSON を書く(Write ツール使用。これ以外のファイルは作らない):
{{
  "articles": [
    {{"slug": "英小文字ハイフンの記事ID(号内一意)",
      "brand": "general|765|cg|million|shiny|sidem|gaku|dsva|joint|other",
      "rank": "lead|large|medium|small",
      "angle": "記事の切り口・見出しの方向性(1文)",
      "dedup_key": "主話題の dedup_key",
      "candidate_ids": ["素材にする候補の id(統合分は全部)"]}}
  ],
  "editorial_topic": "社説の主題(その日の紙面から1題・1文)"
}}
最後に記事本数と rank 内訳を1行で報告してください。
"""


def article_prompt(date: str, art: dict, materials: list[dict], story_facts: list[str],
                   trigger: dict | None, src: str) -> str:
    weekday = "月火水木金土日"[datetime.date.fromisoformat(date).weekday()]
    from lint import BODY_RANGE  # 分量規程は lint と同一定義を使う
    lo, hi = BODY_RANGE[art["rank"]]
    trig = (f"\n- この記事は続報トリガー({trigger['kind']}: {trigger.get('note') or trigger['subject']})の消化です。"
            "トリガーの当日性(締切・開幕等)を記事の軸にすること" if trigger else "")
    prev = ("\n## 既報(この話題で報道済みの事実。同じ事実の繰り返しを記事の軸にしない)\n"
            + "\n".join(f"- {f}" for f in story_facts)) if story_facts else ""
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の記者です。{date}({weekday}曜)号の記事を**1本だけ**書いてください。

## 素材(この JSON がこの記事に使ってよい情報の全てです)
{json.dumps(materials, ensure_ascii=False, indent=2)}
{prev}

## 執筆前の出典照合(必須)
素材の各 url を WebFetch で開き、facts が実際にページで確認できるか照合する。
ページで確認できない fact は使わない(素材の facts 自体が収集段階の誤りを含み得る)。
x.com など取得不能な URL は例外(Grok 観測を出典として信頼する既定どおり)。
**記事の存在理由になる中核の事実が確認できない場合は、ファイルを作らず「ABORT: 理由」とだけ出力して終了すること。**

## 出力
`docs/_posts/{date}-{art['slug']}.md` を Write ツールで作成(これ以外のファイルは作らない・読む必要もない):
- frontmatter は次の値を**そのまま**使う: slug: {art['slug']} / edition: {date} / brand: {art['brand']} / src: {src} / rank: {art['rank']} / corrected: false / corrections: [] / candidate_ids: {json.dumps(art['candidate_ids'])}
- title(全角換算〜28字)・lede(1文)・tags(2〜4個)・sources(素材の url から。label は内容がわかる短い日本語、type は各候補の source_type)・event_date(素材にあれば)は自分で書く
- 本文は {lo}〜{hi} 字(rank: {art['rank']} の分量規程)。切り口: {art['angle']}{trig}

## 絶対規則
- 素材の facts(照合済みのもの)に無い事実を書かない。推測・一般知識での補完は禁止
- 相対表現(本日/昨日/明日)は発行日 {date} 基準。絶対日付と同一文で相対語を併用しない(時制 lint)
- 内規の文言(「全記事に必須」等)を紙面に書かない
- 事実の伝聞元がファン発・未確認の場合は断定を避ける文体にする
"""


def assembly_prompt(date: str, number: int, editorial_topic: str, aborted: list[str]) -> str:
    weekday = "月火水木金土日"[datetime.date.fromisoformat(date).weekday()]
    ab = (f"\n- 計画されたが出典照合で不成立になり存在しない記事: {', '.join(aborted)}(digest 等から参照しないこと)"
          if aborted else "")
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の編集部です。{date}({weekday}曜)号(number: {number})の記事群は docs/_posts/{date}-*.md に**執筆済み**です。組版と台帳更新だけを行ってください。

## 作成物
1. docs/_editions/{date}.md … 号スナップショット(frontmatter のみ。number: {number}, issued_at: "{date}T06:00:00+09:00"。
   形式は直近の既存号と schema/edition.schema.json を確認。pages/article_count/corrected_count/ranking/birthdays は
   後で scripts/derive.py が上書きするため仮値でよい。digest はあなたが本気で組む: 4群固定・SP1画面制約(各群4行・計12行)。
   lead が存在しない場合のみ、最も重要な記事の rank を lead に昇格させ本文を lead の分量(800〜1200字)に加筆する)
2. docs/_editorials/{date}.md … 社説1本。主題: {editorial_topic}
3. stock/stories.yml … 記事化した各話題の published_facts を追記(新規話題はエントリ追加。dedup_key は記事 frontmatter の candidate_ids から candidates を引く)
4. stock/scheduled/<未来日>.json … 記事・候補から新しく判明した未来日程を続報予約する(締切前3日・締切・開幕・千秋楽・発売・結果)。
   **素材スナップショット同梱が必須**: 形式は schema/scheduled.schema.json と既存ファイルを確認し、元候補の
   title/url/source_type/facts/src_candidate_id を必ず写す(発火日は古い candidates を読まないため、ここが唯一の素材になる)。
   既に同じ id の予約がある場合は重複させない
5. stock/pending.yml … 日付未確定の追跡事項の増減(日付が判明した項目は scheduled へ移して消す)

## 注意
- 記事本文の事実関係は校閲済みの前提で**書き換えない**(digest・社説は記事に書いてあることだけを使う)
- 内規の文言を紙面に書かない{ab}

## 仕上げ
- `python3 scripts/derive.py --date {date} --write` を実行して機械算出フィールドを確定する
- `python3 scripts/lint.py --base origin/main` を実行し、エラー0まで自分で修正する(警告も可能な限り解消)
- 完了したら digest 4群の見出しを最後に報告する
"""


def claude_run(prompt: str, timeout: int = 2400) -> str:
    r = subprocess.run(
        ["claude", "-p", prompt, "--model", CLAUDE_MODEL, "--dangerously-skip-permissions"],
        capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=ROOT)
    return r.stdout


def validate_plan(plan: dict, cands: dict, blocklist: dict) -> list[str]:
    errors = []
    arts = plan.get("articles") if isinstance(plan, dict) else None
    if not arts:
        return ["articles が空、または plan JSON の形式不正"]
    slugs = [a.get("slug", "") for a in arts]
    if len(set(slugs)) != len(slugs):
        errors.append("slug が重複している")
    leads = [a["slug"] for a in arts if a.get("rank") == "lead"]
    if len(leads) != 1:
        errors.append(f"lead はちょうど1本(現在 {len(leads)} 本: {leads})")
    for a in arts:
        slug = a.get("slug", "?")
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug or ""):
            errors.append(f"{slug}: slug が英小文字ハイフン形式でない")
        if a.get("brand") not in BRANDS:
            errors.append(f"{slug}: brand 不正({a.get('brand')})")
        if a.get("rank") not in RANKS:
            errors.append(f"{slug}: rank 不正({a.get('rank')})")
        if not a.get("candidate_ids"):
            errors.append(f"{slug}: candidate_ids が空")
        for cid in a.get("candidate_ids", []):
            c = cands.get(cid)
            if c is None:
                errors.append(f"{slug}: 候補 {cid} が発行日±1日の candidates に存在しない")
                continue
            if c.get("verify") == "failed":
                errors.append(f"{slug}: 候補 {cid} は verify=failed(使用不可)")
            if c.get("dedup_key") in blocklist:
                errors.append(f"{slug}: 候補 {cid} は blocklist 対象({c.get('dedup_key')})")
    return errors


def parse_front_matter(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
        m = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
        return yaml.safe_load(m.group(1)) if m else None
    except Exception:
        return None


def validate_article_file(date: str, art: dict, cands: dict, src: str) -> list[str]:
    """個別執筆の機械検収: 計画どおりの frontmatter か・出典が系譜内か。"""
    path = ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md"
    if not path.exists():
        return ["ファイル未作成"]
    fm = parse_front_matter(path)
    if not fm:
        return ["frontmatter がパース不能"]
    errors = []
    for key, want in (("slug", art["slug"]), ("edition", date), ("brand", art["brand"]),
                      ("rank", art["rank"]), ("src", src)):
        got = fm.get(key)
        got = got.isoformat() if hasattr(got, "isoformat") else got
        if got != want:
            errors.append(f"{key} が計画と不一致(計画 {want} / 実際 {got})")
    if sorted(fm.get("candidate_ids") or []) != sorted(art["candidate_ids"]):
        errors.append("candidate_ids が計画と不一致")
    own_urls = {cands[cid].get("url", "") for cid in art["candidate_ids"] if cid in cands}
    for s in fm.get("sources") or []:
        if s.get("url") not in own_urls:
            errors.append(f"出典 URL が素材候補群に無い(系譜外): {s.get('url')}")
    return errors


def write_articles(date: str, plan: dict, cands: dict, triggers: list[dict],
                   stories: dict, wave: int = 4) -> tuple[list[dict], list[str]]:
    """記事ごとに素材を機械的に切り出して個別 claude セッションで執筆(wave 並列)。"""
    trig_by_key = {t["dedup_key"]: t for t in triggers}
    jobs = []
    for art in plan["articles"]:
        materials = [cands[cid] for cid in art["candidate_ids"]]
        src = min((c.get("source_type", "未確認") for c in materials),
                  key=lambda s: SRC_ORDER.index(s) if s in SRC_ORDER else len(SRC_ORDER))
        dks = {c.get("dedup_key") for c in materials} | {art.get("dedup_key")}
        facts = [f for dk in dks if dk in stories for f in stories[dk]]
        jobs.append((art, src, article_prompt(date, art, materials, facts,
                                              trig_by_key.get(art.get("dedup_key")), src)))
    written, aborted = [], []
    for i in range(0, len(jobs), wave):
        procs = []
        for art, src, prompt in jobs[i:i + wave]:
            procs.append((art, src, subprocess.Popen(
                ["claude", "-p", prompt, "--model", CLAUDE_MODEL, "--dangerously-skip-permissions"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                stdin=subprocess.DEVNULL, cwd=ROOT)))
        for art, src, p in procs:
            try:
                out, _ = p.communicate(timeout=900)
            except subprocess.TimeoutExpired:
                p.kill()
                out = ""
            if "ABORT:" in (out or "")[-2000:] and not (ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md").exists():
                reason = out.rsplit("ABORT:", 1)[-1].strip()[:200]
                print(f"記事 {art['slug']} は出典照合で不成立: {reason}", flush=True)
                aborted.append(art["slug"])
                continue
            errs = validate_article_file(date, art, cands, src)
            if errs:
                # 検収エラーは同一素材で1回だけ書き直させる
                fixp = (f"docs/_posts/{date}-{art['slug']}.md の機械検収エラーを修正してください(Edit ツール使用):\n- "
                        + "\n- ".join(errs))
                subprocess.run(["claude", "-p", fixp, "--model", CLAUDE_MODEL, "--dangerously-skip-permissions"],
                               capture_output=True, text=True, timeout=600,
                               stdin=subprocess.DEVNULL, cwd=ROOT)
                errs = validate_article_file(date, art, cands, src)
            if errs:
                print(f"記事 {art['slug']} 検収不合格: {errs}", flush=True)
                aborted.append(art["slug"])
                bad = ROOT / "docs" / "_posts" / f"{date}-{art['slug']}.md"
                if bad.exists():
                    bad.unlink()
            else:
                written.append(art)
    return written, aborted


def load_story_facts() -> dict:
    p = ROOT / "stock" / "stories.yml"
    if not p.exists():
        return {}
    out = {}
    for e in yaml.safe_load(p.read_text(encoding="utf-8")) or []:
        out[e.get("story_id")] = e.get("published_facts", [])
    return out


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
    triggers = load_scheduled(date)
    number = next_number()
    if args.plan:
        print(plan_prompt(date, number, triggers))
        return 0

    if (ROOT / "docs" / "_editions" / f"{date}.md").exists():
        notify("compose", f"{date}: 号スナップショットが既に存在(compose 済み?)。中止")
        return 0

    # 締切ガード(規程: 締切=04:00時点の candidates)。スイープ未実施なら警告して続行
    latest = None
    # 日付名ファイルのみ対象(review-*.json が辞書順で後ろに並び、誤検知するため)
    for md in sorted((ROOT / "metrics").glob("????-??-??.json"))[-2:]:
        try:
            for run in json.loads(md.read_text(encoding="utf-8")).get("collect", []):
                latest = max(latest or run["at"], run["at"])
        except Exception:
            pass
    stale = (latest is None or
             (now_jst() - datetime.datetime.fromisoformat(latest)).total_seconds() > 7200)
    if stale:
        notify("compose", f"{date}: 直近2時間の collect 実行記録が無い(締切前スイープ未実施?)。手持ちの candidates で続行", ok=False)

    # 1a. 選定: 記事計画の生成と機械検証(1回だけ再計画を許す)
    cands = load_window_candidates(date)
    blocklist = load_blocklist()
    if not cands:
        notify("compose", f"{date}: 発行日±1日の candidates が空。compose 続行不能", ok=False)
        return 1
    plan_path = ROOT / "metrics" / f"plan-{date}.json"
    plan, errors = None, ["未実行"]
    for attempt in (1, 2):
        plan_path.unlink(missing_ok=True)  # 残骸の誤読防止(書込失敗時に旧計画を読まない)
        fb = "" if attempt == 1 else "\n".join(f"- {e}" for e in errors)
        claude_run(plan_prompt(date, number, triggers, feedback=fb), timeout=1800)
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except Exception:
            errors = [f"{plan_path.name} が存在しないか JSON として読めない"]
            continue
        errors = validate_plan(plan, cands, blocklist)
        if not errors:
            break
    if errors:
        notify("compose", f"{date}: 記事計画が機械検証を通らず。人間判断が必要\n- " + "\n- ".join(errors[:8]), ok=False)
        commit_and_push(branch, f"compose {date}: 計画不成立(要人間判断)", "compose")
        return 1
    n_plan = len(plan["articles"])
    print(f"計画: {n_plan}本 (lead: {[a['slug'] for a in plan['articles'] if a['rank']=='lead']})", flush=True)

    # 1b. 個別執筆: 記事ごとに素材を機械切り出しして独立セッションで書く
    written, aborted = write_articles(date, plan, cands, triggers, load_story_facts())
    print(f"執筆: {len(written)}/{n_plan}本(不成立 {len(aborted)}: {aborted})", flush=True)
    if len(written) < 1 or (aborted and len(written) < 8):
        notify("compose", f"{date}: 執筆成立 {len(written)}本/計画 {n_plan}本(不成立: {aborted})。下限割れの疑い", ok=False)
    if not written:
        commit_and_push(branch, f"compose {date}: 執筆全滅(要人間判断)", "compose")
        return 1

    # 1c. 組版: 号スナップショット・社説・台帳更新(lint 自己修正まで)
    log = claude_run(assembly_prompt(date, number, plan.get("editorial_topic", ""), aborted))
    print(log[-1000:], flush=True)

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
                              "lint_green": code == 0, "planned": n_plan, "written": len(written),
                              "aborted": aborted, "duration_s": int(time.time() - t0)})
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
