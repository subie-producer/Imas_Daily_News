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
from pipelib import (ENV, ROOT, COLLECT_MODEL, EXPLORE_MAX_BUDGET_USD, JST, append_metric,
                     extract_periods, html_to_text,
                     checkout_edition_branch, commit_and_push, edition_date,
                     extract_json_array, git, notify, notify_crash, now_jst)

# 定点観測の新着を1回の実行で facts 化する上限。1回の Claude 呼び出しに載る量の都合で
# 区切るだけであり、超過分は捨てずに次回へ繰り越す(run_watch の状態保存を参照)。
WATCH_BATCH = int(ENV.get("WATCH_BATCH", "12"))
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
GROK_MAX_TURNS = int(ENV.get("GROK_MAX_TURNS", "120"))
# 10面を1セッションで回すぶん長い。途中で切れても面ごとにファイルへ書かせているので
# そこまでの成果は残る
GROK_TIMEOUT = int(ENV.get("GROK_TIMEOUT", "3000"))
# 1面あたりに集めさせる件数の目安。
# 調査の窓が「直近48時間」なので、収集を1日に何度回しても同じ48時間を見直すだけで
# 大半が重複になる(実測: 12:48 の実行は正規化70件のうち新規22件=69%が重複)。
# したがって回数を増やすのではなく、**1日1回の深掘りで量を確保する**方針を取る。
GROK_ITEMS = int(ENV.get("GROK_ITEMS", "20"))
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
    '"deadline":"締切・終了日 YYYY-MM-DD or 空文字","facts":["確認できた事実(日付・期限・場所・価格を含める)"],'
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
                body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.DOTALL)))[:2500]
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


def write_grok_prompt(outdir: Path, queries: list[dict], out_path: Path) -> Path:
    """全面を1セッションで調べさせる指示を書き出す。

    規程そのものは prompts/grok-collect.md(作業手順書)に置き、ここでは
    「その手順書を読め」と当日固有の値(面の一覧・目標件数・出力先)だけを渡す。
    面を分けて何セッションも起動すると、手順の理解と検索の段取りという前段の作業を
    そのたびに繰り返すことになる。消費はセッション数ではなく作業量に比例するため、
    重複作業をさせないのがそのまま節約になる。
    """
    lines = []
    for n, q in enumerate(queries, 1):
        window = "直近72時間" if q["key"] in ("trend", "fan-culture") else "直近48時間"
        lines.append(f'{n}. brand="{q["brand"]}" … {q["topic"]}({window}を対象)')
    prompt = f"""まず `{GROK_PROCEDURE.relative_to(ROOT)}` を読み、その手順書に従って作業してください。

## 調べる面({len(queries)}面。上から順に、1面ずつ処理する)
{chr(10).join(lines)}

## 面ごとの目標件数
{GROK_ITEMS}件程度(上限ではなく目標)

## 出力先
{out_path}

## 要素の形(手順書の「出力の形式」で参照している定義)
{ITEM_SCHEMA}
"""
    p = outdir / "prompt.md"
    p.write_text(prompt, encoding="utf-8")
    return p


def read_grok_file(path: Path) -> list:
    """grok が書いたファイルを読む。前後に説明文が混ざっても配列だけ取り出す。"""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        v = json.loads(text)
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return extract_json_array(text)


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
    # grok はエージェント型 CLI(Grok Build)であり、1回の起動が SuperGrok の
    # 週次セッションを1つ消費する。面ごとに起動すると1収集で10セッションになり、
    # 日5回では週350セッションで上限(実測 約118)の約3倍になる。
    #
    # そこで **複数面を1セッションにまとめる**。ブランドは減らさず起動回数だけを減らす。
    # 出力は標準出力ではなく**ファイルに書かせる**: エージェントは面ごとに書き足せるので
    # 1レスポンスの出力上限に縛られず、まとめても取りこぼしにくい(実測: 2面16件を
    # 面ごとに追記させて欠落なし)。stdout への JSON 直吐きは本来の使い方ではなく、
    # --json-schema が max_tokens で全滅した件も同じ理由と見ている。
    if not skip_grok:
        outdir = ROOT / "candidates" / ".grok"
        shutil.rmtree(outdir, ignore_errors=True)
        outdir.mkdir(parents=True, exist_ok=True)
        out_path = outdir / "found.json"
        prompt_path = write_grok_prompt(outdir, queries, out_path)

        p = subprocess.Popen(
            ["grok", "--prompt-file", str(prompt_path), "--always-approve",
             "--cwd", str(ROOT), "--max-turns", str(GROK_MAX_TURNS)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            stdin=subprocess.DEVNULL, cwd=ROOT)
        err = ""
        try:
            _, err = p.communicate(timeout=GROK_TIMEOUT)
        except subprocess.TimeoutExpired:
            p.kill()
            err = "timeout"
        got = read_grok_file(out_path)
        if not got:
            print(f"grok 0件 stderr: {(err or '').strip()[:200]}", flush=True)
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
        print(f"grok: 1セッションで {len(got)}件(面別 {by_brand})", flush=True)

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


def normalize(items: list[dict]) -> list[dict]:
    out, seen_url = [], {}
    ts = now_jst().isoformat(timespec="seconds")
    for i, it in enumerate(items):
        try:
            url = (it.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            valid = {"general", "765", "cg", "million", "shiny", "sidem", "gaku", "dsva", "joint", "other"}
            brand = it.get("brand") if it.get("brand") in valid else "other"
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
