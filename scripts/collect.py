#!/usr/bin/env python3
"""collect: 定点観測+探索(Claude haiku の Web 調査 10 クエリ+Grok の X 調査 10 クエリ)を
まとめて candidates/<号日付>.json に記録し、edition ブランチへ push する。(PIPELINE §1〜2)

  python3 scripts/collect.py [--no-git] [--skip-watch] [--skip-claude] [--skip-grok]

- 定点観測(A-1): sources.yml の一覧差分 → 新着 URL を facts 化(Claude 1コール)
- 探索(A-2): claude -p(WebSearch)×10クエリ 並列(執筆より要求精度が低いため haiku。品質劣化があれば
  COLLECT_MODEL=sonnet に戻す。各コールに --max-budget-usd で暴走防止の上限あり)
- X 動向(B) : grok -p(--output-format json --always-approve)×10クエリ ウェーブ実行
- 正規化・URL 重複マージ → candidates へ追記 → 簡易 verify → commit & push
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import (ENV, ROOT, COLLECT_MODEL, EXPLORE_MAX_BUDGET_USD, JST, append_metric,
                     checkout_edition_branch, commit_and_push, edition_date,
                     extract_json_array, git, notify, notify_crash, now_jst)

SCHEMA_PATH = ROOT / "prompts" / "explore-item-schema.json"
# 定点観測の新着を1回の実行で facts 化する上限。1回の Claude 呼び出しに載る量の都合で
# 区切るだけであり、超過分は捨てずに次回へ繰り越す(run_watch の状態保存を参照)。
WATCH_BATCH = int(ENV.get("WATCH_BATCH", "12"))
# Grok(X 動向)を回す時刻。JST の「時」をカンマ区切りで指定する。空なら毎回回す。
# SuperGrok は週次のセッション上限があり、1回の収集で10セッション消費するため、
# 収集の頻度とは別に絞る必要がある。ブランド10面は減らさない(絞るのは回数だけ)。
GROK_HOURS = ENV.get("GROK_HOURS", "").strip()
# 注: grok のヘッドレス実行には --always-approve が必須(無いとツール実行が承認待ちで
# Cancelled になり前置きだけ返る)。--json-schema は max_tokens 切りで全滅するため使わず、
# --output-format json のエンベロープを parse_grok で寛容にパースする。いずれも実測に基づく。
STATE_PATH = ROOT / "stock" / "watch-state.json"
UA = "Mozilla/5.0 (compatible; ImasNewsCollect/1.0)"
X_HOSTS = ("x.com", "twitter.com")

ITEM_FORMAT = (
    "JSON配列だけを出力。各要素は "
    '{"title":短い見出し,"brand":"general|765|cg|million|shiny|sidem|gaku|dsva|joint|other",'
    '"kind":"official|semi|party|media|fan|trend","url":"実在するURL","event_date":"YYYY-MM-DD or 空文字",'
    '"published_date":"情報の初出日=ページ掲載日・ポスト投稿日 YYYY-MM-DD or 空文字",'
    '"deadline":"締切・終了日 YYYY-MM-DD or 空文字","facts":["確認できた事実(日付・期限・場所・価格を含める)"],'
    '"dedup_key":"英小文字ハイフンの話題ID(毎年ある定例企画は年を含める。例: shiny-summer-pair-2026)",'
    '"engagement":"高|中|低","mentioned_idols":["言及アイドル名"]}。'
    "kindの定義: official=アイマス公式(公式ポータル・ブランド公式サイト・公式Xアカウント)のみ/semi=公式レーベル・公式ストア等(日本コロムビア・ランティス・アソビストア等)/party=主催者・販売元・自治体・コラボ先などその他の当事者/media=報道/fan=ファン発/trend=現象。実在の情報のみ・憶測や未確認の噂は除外・個人への批判は除外。JSON以外のテキスト禁止。"
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
        if not skip_grok:
            gp = (f"{window}のX(Twitter)上のアイドルマスター関連のうち「{q['topic']}」を調査し、"
                  f"公式告知とエンゲージメントの高い話題を最大8件。" + ITEM_FORMAT)
            grok_jobs.append((q["key"], gp))

    # Grok は同時実行に弱い(10並列で9本即死を実測)ためウェーブ実行
    GROK_WAVE = 4
    for i in range(0, len(grok_jobs), GROK_WAVE):
        wave = []
        for key, gp in grok_jobs[i:i + GROK_WAVE]:
            wave.append((key, subprocess.Popen(
                ["grok", "-p", gp, "--output-format", "json", "--always-approve"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                stdin=subprocess.DEVNULL, cwd=ROOT)))
        for key, p in wave:
            try:
                out, err = p.communicate(timeout=360)
            except subprocess.TimeoutExpired:
                p.kill()
                out, err = "", "timeout"
            got = parse_grok(out)
            if not got:
                print(f"grok:{key} 0件 stderr: {(err or '').strip()[:200]}", flush=True)
            for it in got:
                it["_via"] = "grok"
            items += got
            per[f"grok:{key}"] = len(got)

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
