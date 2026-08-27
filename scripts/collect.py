#!/usr/bin/env python3
"""collect: 定点観測+探索(Claude haiku の Web 調査 10 クエリ+Grok の X 調査 10 クエリ)を
まとめて candidates/<号日付>.json に記録し、edition ブランチへ push する。(PIPELINE §1〜2)

  python3 scripts/collect.py [--no-git] [--skip-watch] [--skip-claude] [--skip-grok]

- 定点観測(A-1): sources.yml の一覧差分 → 新着 URL を facts 化(Claude 1コール)
- 探索(A-2): claude -p(WebSearch)×10クエリ 並列(執筆より要求精度が低いため haiku。品質劣化があれば
  COLLECT_MODEL=sonnet に戻す。各コールに --max-budget-usd で暴走防止の上限あり)
- X 動向(B) : grok(エージェント実行)。**全10面を1セッション**で、prompts/grok-collect.md の
  手順書に従わせ、結果をファイルに書かせる(標準出力への JSON 直吐きは出力上限で切れる)
- 正規化・URL 重複マージ → candidates へ追記 → 簡易 verify → commit & push
"""
import argparse
import html as html_lib
import datetime
import json
import shutil
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import (ENV, ROOT, COLLECT_MODEL, CODEX_WRITE_MODEL, EXPLORE_MAX_BUDGET_USD, JST, append_metric,
                     extract_periods, html_to_text,
                     checkout_edition_branch, commit_and_push, edition_date,
                     extract_json_array, git, notify, notify_crash, now_jst)

# 定点観測の新着を1回の実行で facts 化する上限。1回の Claude 呼び出しに載る量の都合で
# 区切るだけであり、超過分は捨てずに次回へ繰り越す(run_watch の状態保存を参照)。
WATCH_BATCH = int(ENV.get("WATCH_BATCH", "12"))
# 定点観測で facts 化のために渡すページ本文の量。切り詰めるとそのぶん facts が痩せる
WATCH_BODY_CHARS = int(ENV.get("WATCH_BODY_CHARS", "20000"))
# Grok(X 動向)を回す時刻。JST の「時」をカンマ区切りで指定する。空なら毎回回す。
# SuperGrok は週次のセッション上限があり、1回の収集で10セッション消費するため、
# 収集の頻度とは別に絞る必要がある。ブランド10面は減らさない(絞るのは回数だけ)。
GROK_HOURS = ENV.get("GROK_HOURS", "").strip()
# 収集の作業手順書。規程はここに置き、collect は当日固有の値だけを渡す。
GROK_PROCEDURE = ROOT / "prompts" / "grok-collect.md"
# 全10面を**1セッション**で処理する。面を分けて何度も起動すると、手順の理解と
# 検索の段取りという前段の作業をそのたびに繰り返す。消費はセッション数ではなく
# 作業量に比例するとみられる(実測: 3セッション化しても表示%は想定ほど下がらなかった)
# ため、重複作業を作らないことがそのまま節約になる。
# **1面あたり**の上限。10面を1セッションで回していた頃の値(120)が面別化後も
# 残っており、1面が7〜14ターン使っていた。消費は結局「検索した量」に比例するので、
# ここが実質の予算になる(実測: 9セッション92ターンで週次の15%)
# **これが唯一の効く上限。**検索回数はプロンプトで指示しても守られない
# (「最大10回」と書いても34回引いた)。Grok は自分の検索回数を数えていない。
# 実測では 1ターンあたり 8〜12検索なので、ターン数が実質の検索予算になる。
GROK_MAX_TURNS = int(ENV.get("GROK_MAX_TURNS", "3"))
# **1面あたりの X 検索回数の上限。**週次上限の実体はトークンでもセッション数でもなく
# X 検索の回数だった(実測: 10面252検索=15% / 1面47検索=4%。1検索あたり 0.06〜0.085%)。
# 週の予算はおよそ 1200〜1700検索で、日次で回すなら 100〜140検索が上限になる。
# ターン上限やトークン最適化では届かないので、検索回数そのものを絞る。
# プロンプトに書く目安(強制力は無い。実際に効くのは GROK_MAX_TURNS のほう)
GROK_MAX_SEARCHES = int(ENV.get("GROK_MAX_SEARCHES", "10"))
# 10面を1セッションで回すぶん長い。途中で切れても面ごとにファイルへ書かせているので
# そこまでの成果は残る
GROK_TIMEOUT = int(ENV.get("GROK_TIMEOUT", "3000"))
# 1面あたりに集めさせる件数の目安。
# 調査の窓が「直近48時間」なので、収集を1日に何度回しても同じ48時間を見直すだけで
# 大半が重複になる(実測: 12:48 の実行は正規化70件のうち新規22件=69%が重複)。
# したがって回数を増やすのではなく、**1日1回の深掘りで量を確保する**方針を取る。
# ただし件数を上げすぎると週次上限に当たる(20件設定の 2026-08-27 は1回で上限の14%を消費)。
# 記事に使われるのは候補の6割程度なので、目標を12件に下げても紙面の厚みは保てる見込み。
GROK_ITEMS = int(ENV.get("GROK_ITEMS", "12"))
# 面別セッションの同時実行数。多すぎると X 側で絞られるおそれがあるので控えめに置く
GROK_WAVE = int(ENV.get("GROK_WAVE", "3"))
# 注: grok のヘッドレス実行には --always-approve が必須(無いとツール実行が承認待ちで
# Cancelled になり前置きだけ返る)。結果は標準出力ではなく**ファイルに書かせる**。
# grok はエージェント型 CLI なので、面ごとにファイルを更新させれば1応答の出力上限に
# 縛られない。--json-schema が max_tokens 切りで全滅したのも、応答本文に全件を載せさせる
# 使い方そのものが原因だったとみている(いずれも実測に基づく)。
STATE_PATH = ROOT / "stock" / "watch-state.json"
UA = "Mozilla/5.0 (compatible; ImasNewsCollect/1.0)"
X_HOSTS = ("x.com", "twitter.com")

# 候補1件のスキーマと編集規程。標準出力に吐かせる場合(Claude)とファイルに書かせる場合
# (Grok)で共用するため、「どこへどう出すか」の指示は含めない。
ITEM_SCHEMA = (
    '{"title":短い見出し,"brand":"general|765|cg|million|shiny|sidem|gaku|dsva|joint|other",'
    '"kind":"official|semi|party|media|fan|trend","url":"実在するURL","event_date":"YYYY-MM-DD or 空文字",'
    '"published_date":"情報の初出日=ページ掲載日・ポスト投稿日 YYYY-MM-DD or 空文字",'
    '"deadline":"締切・終了日 YYYY-MM-DD or 空文字",'
    '"facts":["確認できた事実。**省略せず全部書く**"],'
    "【facts の量】facts は記事の素材になる。**ページに書いてあることを削らない**。"
    "会場・所在地・開場/開演時刻・席種と価格・受付や入金の全日程・枚数制限・対象者の条件・"
    "出演者(名前と役名)・商品の品目や型番・収録内容・特典・発送時期は、あるだけ列挙する。"
    "1行にまとめず、1事実1要素にする。実測で、本文8,219字のページから facts を359字しか"
    "起こしていなかった例がある(1/23)。それでは記事が書けない。"
    "【期間ラベル規則(最重要)】期間や日付を facts に書くときは、**原文の見出し・語句をそのまま先頭に付ける**。"
    "「受付期間: 8/26〜8/30」「入金期間: 8/26〜8/30」「一般先着販売: 8/31 12:00〜」のように書き、"
    "ラベルを外して「販売期間」「発売」などに言い換えない。チケットや受注は"
    "「先行抽選の申込受付」「抽選結果発表」「当選者の入金」「一般先着販売」「一般販売」が別々の期間として併存し、"
    "取り違えると誰がいつ買えるのかが逆になる。**原文にラベルが無い日付範囲は facts に書かない**"
    "(何の期間か分からないまま渡すと、執筆側が勝手に意味を補う)。"
    '"dedup_key":"英小文字ハイフンの話題ID(毎年ある定例企画は年を含める。例: shiny-summer-pair-2026)",'
    '"engagement":"高|中|低","mentioned_idols":["言及アイドル名"]}。'
    "kindの定義: official=アイマス公式(公式ポータル・ブランド公式サイト・公式Xアカウント)のみ/semi=公式レーベル・公式ストア等(日本コロムビア・ランティス・アソビストア等)/party=主催者・販売元・自治体・コラボ先などその他の当事者/media=報道/fan=ファン発/trend=現象。実在の情報のみ・憶測や未確認の噂は除外・個人への批判は除外。"
    "【factsの出所規則(最重要)】facts には url に指定したページ(またはポスト)の本文で直接確認できた事実だけを書く。"
    "検索結果一覧のスニペット・別ページ・別イベントの情報・自分の推測や一般知識を混ぜない。"
    "特に日時・期限・数値は url のページに書かれているものだけ。同じ作品の別施策(ゲーム内イベント・ガシャ等)を"
    "1つの候補に合成しない(別施策は別候補として、その施策自体の告知URLを付けて出す。告知URLを確認できないなら出さない)。"
    "【鮮度規則(最重要)】ページの掲載日・ポストの投稿日時を必ず確認し published_date に書く。"
    "掲載年が確認できないページの日付を年込みで推定しない。過年度の告知・すでに終了したイベント・施策は候補にしない"
    "(検索は古いページも返す。「M月D日」が一致しても年が違えば別の話題である。結果発表・受賞など本日新しく出た情報は可)。"
    "【ファン面の副産物】担当した面を調べる過程で、ファン創作・コスプレ・聖地巡礼・記念日の盛り上がり・"
    "界隈の現象が目に入ったら、それも候補にする(kind=fan または trend、brand=general)。"
    "**これらを探しに行く検索はしない。**面の調査で自然に見えたものだけを拾う。"
    "個人アカウント名は書かず、何が起きているかを書く。"
    "【対象外】同人イベント・ファン主催企画(オンリーイベント・同人誌即売会・非公式コラボ)は候補にしない。"
    "party はアイマス公式に関係する主催者・販売元・自治体・コラボ先企業のみで、ファン主催者は含まない。"
)

# 標準出力に JSON 配列だけを吐かせる場合(Claude 探索・定点観測の facts 化)
ITEM_FORMAT = "JSON配列だけを出力。各要素は " + ITEM_SCHEMA + "JSON以外のテキスト禁止。"


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", errors="replace")


def fetch_rendered(url: str, timeout: int = 30) -> str:
    r = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "scripts" / "fetch_rendered.py"),
         url, "--timeout", str(timeout)],
        capture_output=True, text=True, timeout=timeout + 60)
    return r.stdout if r.returncode == 0 else ""


# ---- A-1 定点観測 --------------------------------------------------------------

def run_watch(claude_call) -> tuple[list[dict], dict]:
    sources = yaml.safe_load((ROOT / "sources.yml").read_text(encoding="utf-8"))
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    new_items = []  # {source_id, brand, url, title, source_type}
    found_by_source = {}  # 状態の保存は facts 化の後に行うため、巡回結果を持ち越す
    stats = {}
    for s in sources:
        if not s.get("enabled"):
            continue
        try:
            html = fetch_rendered(s["url"]) if s["type"] == "portal" else http_get(s["url"])
            found = []
            for m in re.finditer(s["list_regex"], html, re.DOTALL):
                u = m.group(1)
                if not u.startswith("http"):
                    u = s["base"].rstrip("/") + "/" + u.lstrip("/")
                title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2) if m.lastindex >= 2 else "")).strip()
                if u not in found:
                    found.append(u)
                    if u not in state.get(s["id"], []):
                        new_items.append({"source_id": s["id"], "brand": s["brand"], "url": u,
                                          "title": title, "source_type": s.get("source_type", "公式"),
                                          "csr": s["type"] == "portal"})
            found_by_source[s["id"]] = found
            stats[s["id"]] = {"found": len(found), "new": len([n for n in new_items if n["source_id"] == s["id"]])}
        except Exception as e:
            stats[s["id"]] = {"error": str(e)[:120]}

    # 新着を facts 化。1回のプロンプトに詰め込める量に上限があるため件数で切るが、
    # **切った分は落とさず次回へ繰り越す**(下の状態保存を参照)。
    cands = []
    batch = new_items[:WATCH_BATCH]
    if batch and claude_call:
        blobs = []
        for it in batch:
            body = ""
            if it["csr"]:
                t = fetch_rendered(it["url"])
                # 本文の切り詰めは facts の情報量に直結する。2500字にしていたとき、
                # 本文8,219字のページから facts を359字しか起こせていなかった
                body = html_to_text(t.encode("utf-8", "replace"))[:WATCH_BODY_CHARS]
            blobs.append({"url": it["url"], "title": it["title"], "brand_hint": it["brand"], "rendered_text": body})
        prompt = (
            "以下はアイドルマスター関連の定点観測で見つかった新着ページです。rendered_text が空のものは URL を WebFetch で読み、"
            "各ページの内容を事実として抽出してください(まとめサイトの場合はページ内の一次ソースURLを url に採用)。"
            + ITEM_FORMAT + "\n\n" + json.dumps(blobs, ensure_ascii=False))
        cands = claude_call(prompt, timeout=420)
        for c in cands:
            c["_via"] = "watch"

    # 状態の保存は facts 化の**後**。既知にするのは「今回処理を試みた URL」だけで、
    # 上限を超えて手つかずのまま残った新着は未読のままにする。
    #   - 巡回直後に全件を既知にすると、上限超過分は次回 new と判定されず、
    #     candidates に一度も載らないまま消える(カバレッジの穴になる)
    #   - 処理を試みた URL は、結果が0件でも既知にする(毎回同じページを
    #     読み直して上限枠を食い潰さないため)
    # 途中で落ちた場合も未保存なので、次回の実行がそのまま拾い直す。
    attempted = {it["url"] for it in batch}
    deferred = [n for n in new_items if n["url"] not in attempted]
    deferred_urls = {n["url"] for n in deferred}
    for sid, found in found_by_source.items():
        keep = [u for u in found if u not in deferred_urls]
        seen = state.get(sid, [])
        state[sid] = (keep + [u for u in seen if u not in keep])[:500]
        if sid in stats:
            stats[sid]["deferred"] = len([n for n in deferred if n["source_id"] == sid])
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    if deferred:
        print(f"定点観測: 新着 {len(new_items)}件のうち {len(batch)}件を処理、"
              f"{len(deferred)}件を次回へ繰り越し", flush=True)
    return cands, {"stats": stats, "new": len(new_items), "facted": len(cands),
                   "deferred": len(deferred)}


# ---- A-2 / B 探索 --------------------------------------------------------------

def build_prompts() -> list[dict]:
    queries = yaml.safe_load((ROOT / "prompts" / "queries.yml").read_text(encoding="utf-8"))
    return queries


def parse_grok(out: str) -> list:
    """grok --output-format json はエンベロープ {"text": <本文>} で返す。素の JSON にも対応。"""
    try:
        obj = json.loads(out)
        out = obj.get("text", out)
    except (json.JSONDecodeError, AttributeError):
        pass
    return extract_json_array(out)


def claude_exec(prompt: str, timeout: int = 300) -> list:
    r = subprocess.run(
        ["claude", "-p", prompt, "--model", COLLECT_MODEL,
         "--allowedTools", "WebSearch,WebFetch",
         "--max-budget-usd", EXPLORE_MAX_BUDGET_USD],
        capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=ROOT)
    return extract_json_array(r.stdout)


def write_grok_prompt(outdir: Path, q: dict) -> Path:
    """1面ぶんの指示を書き出す(面ごとに1セッション)。

    規程は prompts/grok-collect.md(作業手順書)に置き、ここでは当日固有の値
    (その面の主題・目標件数・出力先)だけを渡す。
    """
    window = "直近72時間" if q["key"] in ("trend", "fan-culture") else "直近48時間"
    out = outdir / f"{q['key']}.jsonl"
    prompt = f"""まず `{GROK_PROCEDURE.relative_to(ROOT)}` を読み、その手順書に従って作業してください。

## あなたが調べる面(これ1面だけ)
brand="{q["brand"]}" … {q["topic"]}

対象期間: {window}
目標件数: {GROK_ITEMS}件程度(上限ではなく目標)

## 出力
`{out}` に**1行1件の JSON**(JSON Lines)で追記します。
他の面は別の担当が調べます。**このファイル以外は読まない・触らない**。

## 検索回数の上限(**厳守**)
**X の検索は、この面で最大 {GROK_MAX_SEARCHES} 回までです。**
週次の利用上限は検索回数で決まります。回数を使い切ったら、それまでに見つけたぶんで打ち切ってください。
件数の目標より**この上限が優先**します。

そのため、1回の検索を広く取ってください。
- 語をむやみに変えて何度も引かない。1クエリで拾えるものは1回で拾う
- 検索結果の一覧から候補を絞り込み、**中身の確認はポストやページを開いて**行う
  (開く操作は検索ではないので上限に数えません)

## 禁止(消費が跳ねます)
- **画像をダウンロードして読み込まない**(この紙面は画像を使いません)
- 書いたファイルを読み直さない

## 要素の形
{ITEM_SCHEMA}
"""
    pp = outdir / f"prompt-{q['key']}.md"
    pp.write_text(prompt, encoding="utf-8")
    return pp


def consolidate_grok(outdir: Path) -> list:
    """面ごとに書き捨てられた JSONL を Luna(codex)に束ねさせる。

    面別セッションは互いを見ないので、同じ話題が複数の面に出る・整形が面ごとに
    ぶれる、といったことが必ず起きる。それを Grok 側に整えさせると、
    そのぶん週次上限を検索以外に使うことになる(上限の実体は入力トークン量)。
    調停は安いモデルの仕事にして、Grok には調べることだけをさせる。

    Luna が失敗しても収集を落とさない。機械読み(read_grok_files)に必ず落とす。
    """
    files = sorted(outdir.glob("*.jsonl"))
    if not files:
        return []
    out = outdir / "normalized.json"
    out.unlink(missing_ok=True)
    prompt = (
        f"{outdir} にある *.jsonl は、面ごとに独立して収集された候補です"
        "(1行1件の JSON。面をまたいだ重複や、整形の崩れがあります)。\n"
        f"すべて読み、次の規則で1つの JSON 配列に束ねて `{out}` へ書いてください"
        "(Write ツール使用。他のファイルは作らない)。\n\n"
        "1. **同じ url の行は1件に統合**し、facts を重複なく合併する\n"
        "2. 同じ話題を指す行が別の url で複数あるなら、**それぞれ別の候補として残す**"
        "(統合しない。どちらが正しいかの判断はしない)\n"
        "3. 壊れた行は形だけ直す。**url が読み取れない行は捨てる**\n"
        "4. **書かれていない情報を足さない。**推測で値を埋めない。facts の文言は変えない\n"
        "5. 要素の形は次のとおり:\n" + ITEM_SCHEMA)
    try:
        subprocess.run(["codex", "exec", "-m", CODEX_WRITE_MODEL, "-s", "workspace-write", prompt],
                       capture_output=True, text=True, timeout=900,
                       stdin=subprocess.DEVNULL, cwd=ROOT)
    except Exception as e:
        print(f"grok: 調停(codex)に失敗 {e}。機械読みに切り替える", flush=True)
    if out.exists():
        try:
            v = json.loads(out.read_text(encoding="utf-8", errors="replace"))
            got = [x for x in v if isinstance(x, dict) and x.get("url")] if isinstance(v, list) else []
            if got:
                raw = read_grok_files(outdir)
                print(f"grok: 調停 {len(raw)}行 → {len(got)}件", flush=True)
                return got
        except json.JSONDecodeError:
            pass
    print("grok: 調停の結果を読めず、機械読みに切り替える", flush=True)
    return read_grok_files(outdir)


def read_grok_files(outdir: Path) -> list:
    """grok が面ごとに書き捨てた JSONL を全部読む。壊れた行は捨てる。

    整形の厳密さを Grok に求めない代わりに、ここは寛容に読む。
    """
    items, broken = [], []
    for p in sorted(outdir.glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip().rstrip(",")
            if not line.startswith("{"):
                continue
            try:
                v = json.loads(line)
            except json.JSONDecodeError:
                broken.append(line[:600])
                continue
            if isinstance(v, dict) and v.get("url"):
                items.append(v)
            else:
                broken.append(line[:600])
    # 面ごとの .json(配列で書かれた場合)も拾う。調停結果は対象外
    for p in sorted(outdir.glob("*.json")):
        if p.name == "normalized.json":
            continue
        try:
            v = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(v, list):
            items += [x for x in v if isinstance(x, dict) and x.get("url")]
    if broken:
        # 整形の厳密さを Grok に求めない代わり、崩れた行はここで拾い直す。
        # 直すのは形だけで、事実は足さない(Grok の観測を超える情報を作らないため)
        salvaged = salvage_broken(broken)
        print(f"grok: 崩れた行 {len(broken)}件 → {len(salvaged)}件を復旧", flush=True)
        items += salvaged
    return items


def salvage_broken(lines: list[str]) -> list:
    """JSON として読めなかった行を codex(luna)に整形し直させる。

    Grok に整形を作り込ませると、そのぶん検索に使える枠が減る(週次上限が実際の制約)。
    整形は安いモデルの仕事にする。事実の追加は禁じ、形を直すだけにさせる。
    """
    prompt = ("次の各行は JSON として壊れています。**各行を1つの JSON オブジェクトに直して**、"
              "JSON 配列だけを標準出力に書いてください。\n"
              "**書かれていない情報を足さないこと**(推測で値を埋めない)。"
              "url が読み取れない行は捨ててください。JSON 以外のテキストは出力しない。\n\n"
              + "\n".join(lines[:80]))
    try:
        r = subprocess.run(["codex", "exec", "-m", CODEX_WRITE_MODEL, "-s", "read-only",
                            "--skip-git-repo-check", prompt],
                           capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL, cwd=ROOT)
    except Exception:
        return []
    return [x for x in extract_json_array(r.stdout) if isinstance(x, dict) and x.get("url")]


def grok_scheduled_now(now=None) -> bool:
    """今回の実行で Grok を回すか。GROK_HOURS が空なら毎回回す(従来動作)。"""
    if not GROK_HOURS:
        return True
    hours = {h.strip().lstrip("0") or "0" for h in GROK_HOURS.split(",") if h.strip()}
    return str((now or now_jst()).hour) in hours


def run_explores(skip_claude: bool, skip_grok: bool) -> tuple[list[dict], dict]:
    queries = build_prompts()
    items, per = [], {}

    # Claude は全クエリ並列で問題なし(サーバ側セッション)
    claude_procs = []
    grok_jobs = []
    for q in queries:
        window = "直近72時間" if q["key"] in ("trend", "fan-culture") else "直近48時間"
        if not skip_claude:
            cp = (f"{window}のアイドルマスター関連情報のうち「{q['topic']}」について、Web検索で公式サイト・報道・"
                  f"特設ページを調査し、確認できた事実を最大8件。" + ITEM_FORMAT)
            claude_procs.append((q["key"], subprocess.Popen(
                ["claude", "-p", cp, "--model", COLLECT_MODEL, "--allowedTools", "WebSearch,WebFetch",
                 "--max-budget-usd", EXPLORE_MAX_BUDGET_USD],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                stdin=subprocess.DEVNULL, cwd=ROOT)))
    # -- Grok(X 動向) --
    # **面ごとに独立したセッション**で回す。
    # 以前は「セッション数ではなく作業量に比例する」と考えて1セッションに10面を
    # まとめていたが、実測はその逆だった: 1セッション内では面が進むたびに
    # それまでの全検索結果を読み直すため、1呼出あたりの入力が 15k → 2,439k まで膨らむ。
    # 総入力は呼出数の2乗で効き、週次消費%は入力トークン量にきれいに比例する
    #   (実測: 6.9M→5pp / 17.9M→14pp / 20.4M→16pp)。
    # 面ごとに切れば文脈がリセットされ、総入力は分割数ぶんの1になる。
    # セッション起動の固定費(手順書+プロンプト)は約2kトークンで、削減額に対して誤差。
    # ブランド10面は減らさない。減らすのは1セッションが抱える文脈の量だけ。
    if not skip_grok:
        outdir = ROOT / "candidates" / ".grok"
        shutil.rmtree(outdir, ignore_errors=True)
        outdir.mkdir(parents=True, exist_ok=True)
        for i in range(0, len(queries), GROK_WAVE):
            procs = []
            for q in queries[i:i + GROK_WAVE]:
                pp = write_grok_prompt(outdir, q)
                procs.append((q, subprocess.Popen(
                    ["grok", "--prompt-file", str(pp), "--always-approve",
                     "--cwd", str(ROOT), "--max-turns", str(GROK_MAX_TURNS)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                    stdin=subprocess.DEVNULL, cwd=ROOT)))
            for q, pr in procs:
                try:
                    pr.communicate(timeout=GROK_TIMEOUT)
                except subprocess.TimeoutExpired:
                    pr.kill()
                    print(f"grok: {q['key']} 面がタイムアウト(そこまでの記録は残る)", flush=True)
        got = consolidate_grok(outdir)
        if not got:
            print("grok 0件", flush=True)
        for it in got:
            it["_via"] = "grok"
        items += got

        # 面ごとの取得数に割り戻す(watch の全滅検知が per_query を見るため)
        by_brand = {}
        for it in got:
            b = str(it.get("brand", ""))
            by_brand[b] = by_brand.get(b, 0) + 1
        for q in queries:
            per[f"grok:{q['key']}"] = by_brand.get(q["brand"], 0)
        print(f"grok: {len(queries)}面を面別セッションで {len(got)}件(面別 {by_brand})", flush=True)

    # Claude の回収
    deadline = time.time() + 600
    for key, p in claude_procs:
        try:
            out, _ = p.communicate(timeout=max(30, deadline - time.time()))
            got = extract_json_array(out)
        except subprocess.TimeoutExpired:
            p.kill()
            got = []
        for it in got:
            it["_via"] = "claude"
        items += got
        per[f"claude:{key}"] = len(got)
    return items, per


# ---- 正規化・記録 --------------------------------------------------------------

KIND2SRC = {"official": "公式", "semi": "準公式", "party": "当事者", "media": "報道", "fan": "ファン", "trend": "ファン"}


def is_x(url: str) -> bool:
    return any(h in url for h in X_HOSTS)


# 面が判別できる語 → ブランド。名鑑(アイドル名)で拾えない作品名・ブランド呼称を補う。
BRAND_WORDS = {
    "dsva": ("vα-liv", "ヴイアラ", "va-liv", "valiv", "876プロ", "ディアリースターズ"),
    "gaku": ("学園アイドルマスター", "学マス", "初星学園", "gakuen"),
    "shiny": ("シャイニーカラーズ", "シャニマス", "シャニソン", "283プロ", "shinycolors"),
    "million": ("ミリオンライブ", "ミリシタ", "ミリアニ", "765プロオールスターズ"),
    "cg": ("シンデレラガールズ", "デレステ", "デレマス", "シンデレラ"),
    "sidem": ("sidem", "315プロ", "サイドエム"),
    "765": ("765pro allstars", "765ミリオンスターズ", "765as"),
    "joint": ("ツアーズ", "ツアマス", "iwsf", "合同ライブ", "ポプマス"),
}


def guess_brand(text: str, idols: dict) -> str | None:
    """本文から面を1つに特定できるときだけ返す(複数該当・不明なら None)。

    定点観測は sources.yml のブランドを起点にするため、公式ポータルのような
    横断ソースの新着は brand が general/other のまま残りやすい。実際
    「上水流宇宙 BIRTHDAY ONLINE LIVE 2026」が other になり、grok/claude が
    dsva と付けた同じ話題と別の面に分かれて二重に記事化された。
    アイドル名は名鑑(docs/_data/idols.json)で面が確定するので、機械で直す。
    """
    low = text.lower()
    hit = {b for b, words in BRAND_WORDS.items() if any(w.lower() in low for w in words)}
    hit |= {b for name, b in idols.items() if name and name in text}
    return hit.pop() if len(hit) == 1 else None


def load_idol_brands() -> dict:
    p = ROOT / "docs" / "_data" / "idols.json"
    if not p.exists():
        return {}
    try:
        return {x.get("name"): x.get("brand") for x in json.loads(p.read_text(encoding="utf-8"))}
    except Exception:
        return {}


def normalize(items: list[dict]) -> list[dict]:
    out, seen_url = [], {}
    ts = now_jst().isoformat(timespec="seconds")
    idols = load_idol_brands()
    for i, it in enumerate(items):
        try:
            url = (it.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            valid = {"general", "765", "cg", "million", "shiny", "sidem", "gaku", "dsva", "joint", "other"}
            brand = it.get("brand") if it.get("brand") in valid else "other"
            if brand in ("general", "other"):
                # 面が付いていない候補だけ補正する。collector が具体的な面を選んで
                # いるならその判断を尊重する(合同を各面へ割り直したりしない)
                g = guess_brand(" ".join([it.get("title") or "", it.get("dedup_key") or "",
                                          *(it.get("facts") or [])]), idols)
                if g:
                    brand = g
            dk = re.sub(r"[^a-z0-9-]", "-", (it.get("dedup_key") or "").lower()).strip("-") or f"auto-{i}"
            facts = [f for f in (it.get("facts") or []) if isinstance(f, str) and f.strip()]
            if it.get("engagement"):
                facts.append(f"エンゲージメント: {it['engagement']}")
            if it.get("mentioned_idols"):
                facts.append("言及アイドル: " + "、".join(it["mentioned_idols"]))
            c = {
                "id": f"{now_jst().strftime('%Y%m%d%H%M')}-{it.get('_via','x')}-{i}",
                "title": (it.get("title") or "")[:120] or "(無題)",
                "brand": brand,
                "source_type": KIND2SRC.get(it.get("kind"), "未確認"),
                "url": url,
                "found_at": ts,
                "dedup_key": dk,
                "facts": facts,
                "origin": "watch" if it.get("_via") == "watch" else "explore",
                "via": it.get("_via", "explore"),
                "verify": "unconfirmed",
            }
            for k in ("event_date", "deadline", "published_date"):
                v = (it.get(k) or "").strip()
                if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
                    c[k] = v
            if url in seen_url:  # URL 重複は facts をマージ
                tgt = seen_url[url]
                tgt["facts"] = list(dict.fromkeys(tgt["facts"] + c["facts"]))
                continue
            seen_url[url] = c
            out.append(c)
        except Exception:
            continue
    return out


STALE_DAYS = 14  # 収集窓は48〜72時間。これを大きく超えて古い情報は鮮度切れ(2025年告知を新報扱いした事故の再発防止)


def x_status_date(url: str) -> datetime.date | None:
    """x.com/twitter.com の status ID(Snowflake)から投稿日を復元する(機械検証可能な鮮度情報)。"""
    m = re.search(r"/status(?:es)?/(\d{15,20})", url)
    if not m:
        return None
    ms = (int(m.group(1)) >> 22) + 1288834974657  # Twitter epoch
    return datetime.datetime.fromtimestamp(ms / 1000, tz=JST).date()


def check_stale(c: dict) -> str | None:
    """鮮度切れなら理由を返す。X は Snowflake、それ以外は自己申告の published_date で判定。"""
    today = now_jst().date()
    posted = x_status_date(c["url"]) if is_x(c["url"]) else None
    if posted and (today - posted).days > STALE_DAYS:
        return f"Xポストが古い(投稿日 {posted.isoformat()})。過去の告知を新報として扱わない"
    pub = c.get("published_date")
    if pub and (today - datetime.date.fromisoformat(pub)).days > STALE_DAYS:
        return f"掲載日 {pub} が古い。過去の告知を新報として扱わない"
    return None


def verify(cands: list[dict]) -> dict:
    """簡易 verify: X は Grok 観測を信頼。それ以外は URL 生存で confirmed。鮮度切れは種別を問わず failed。"""
    counts = {"confirmed": 0, "unconfirmed": 0, "failed": 0}
    for c in cands:
        try:
            stale = check_stale(c)
            if stale:
                c["verify"] = "failed"
                c["verify_note"] = stale
                counts["failed"] += 1
                continue
            if is_x(c["url"]):
                c["verify"] = "confirmed" if (c["via"] == "grok" and c["source_type"] == "公式") else "unconfirmed"
            else:
                req = urllib.request.Request(c["url"], headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=15) as res:
                    ok = res.status < 400
                    body = res.read(400_000) if ok else b""
                    cs = res.headers.get_content_charset()
                if ok:
                    text = html_to_text(body, cs)
                    # CSR で本文が空同然なら描画してから読み直す(定点観測と同じ経路)
                    if len(text.strip()) < 400:
                        rendered = fetch_rendered(c["url"])
                        if rendered:
                            text = html_to_text(rendered.encode("utf-8", "replace"))
                    periods = extract_periods(text)
                    if periods:
                        c["periods"] = periods
                c["verify"] = "confirmed" if ok and c["source_type"] in ("公式", "準公式", "当事者", "報道") else ("unconfirmed" if ok else "failed")
        except Exception:
            c["verify"] = "failed"
        counts[c["verify"]] += 1
    return counts


def merge_into_day_file(cands: list[dict]) -> int:
    """candidates は号(edition)日付でキーする。1つの号の素材=1ファイルで、収集サイクル
    (07:30〜翌03:30)が暦日をまたいでも分割されない。パイプラインが前日以前の
    ファイルを読む必要は無い(未来日程は stock/scheduled、既報判定は stock/stories.yml)。"""
    day = edition_date()
    p = ROOT / "candidates" / f"{day}.json"
    existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    by_url = {c["url"]: c for c in existing}
    added = 0
    for c in cands:
        if c["url"] in by_url:
            tgt = by_url[c["url"]]
            merged = list(dict.fromkeys(tgt.get("facts", []) + c["facts"]))
            if len(merged) > len(tgt.get("facts", [])):
                tgt["facts"] = merged
        else:
            existing.append(c)
            by_url[c["url"]] = c
            added += 1
    p.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-git", action="store_true", help="ブランチ操作・push をしない(テスト用)")
    ap.add_argument("--skip-watch", action="store_true")
    ap.add_argument("--skip-claude", action="store_true")
    ap.add_argument("--skip-grok", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    date = edition_date()
    branch = f"edition/{date}"

    if not args.no_git and not checkout_edition_branch(date, "collect"):
        return 1

    # Grok は SuperGrok の週次上限を消費するため、収集のたびに回すと枠を使い切る
    # (実測: 1回の収集で10セッション。日5回だと週350セッションになり上限の数倍)。
    # 定点観測と Claude 探索は安価なので毎回回し、Grok の実行時刻だけを GROK_HOURS で絞る。
    skip_grok = args.skip_grok or not grok_scheduled_now()
    if skip_grok and not args.skip_grok:
        print(f"Grok は今回スキップ(GROK_HOURS={GROK_HOURS or '毎回'} の対象時刻ではない)", flush=True)

    watch_cands, watch_info = ([], {"skipped": True}) if args.skip_watch else run_watch(claude_exec)
    explore_items, per_query = run_explores(args.skip_claude, skip_grok)
    cands = normalize(watch_cands + explore_items)
    vcounts = verify(cands)
    added = merge_into_day_file(cands)
    dur = int(time.time() - t0)
    append_metric("collect", {"edition": date, "watch": watch_info, "per_query": per_query,
                              "normalized": len(cands), "added": added, "verify": vcounts,
                              "duration_s": dur})
    summary = f"{date}号向け: 新規{added}件(正規化{len(cands)}・{vcounts}) {dur}秒"
    print(summary, flush=True)
    if not args.no_git:
        commit_and_push(branch, f"collect {now_jst().strftime('%H:%M')}: +{added}件", "collect")
    if added == 0:
        notify("collect", f"{summary} — 新規0件", ok=False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        notify_crash("collect", e)
        sys.exit(1)
