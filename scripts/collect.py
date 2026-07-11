#!/usr/bin/env python3
"""collect: 定点観測+探索(Claude sonnet の Web 調査 10 クエリ+Grok の X 調査 10 クエリ)を
まとめて candidates/<収集日>.json に記録し、edition ブランチへ push する。(PIPELINE §1〜2)

  python3 scripts/collect.py [--no-git] [--skip-watch] [--skip-claude] [--skip-grok]

- 定点観測(A-1): sources.yml の一覧差分 → 新着 URL を facts 化(Claude 1コール)
- 探索(A-2): claude -p(WebSearch)×10クエリ 並列
- X 動向(B) : grok -p(--json-schema)×10クエリ 並列
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
from pipelib import (ROOT, CLAUDE_MODEL, JST, append_metric, checkout_edition_branch,
                     commit_and_push, edition_date, extract_json_array, git, notify, now_jst)

SCHEMA_PATH = ROOT / "prompts" / "explore-item-schema.json"
STATE_PATH = ROOT / "stock" / "watch-state.json"
UA = "Mozilla/5.0 (compatible; ImasNewsCollect/1.0)"
X_HOSTS = ("x.com", "twitter.com")

ITEM_FORMAT = (
    "JSON配列だけを出力。各要素は "
    '{"title":短い見出し,"brand":"general|765|cg|million|shiny|sidem|gaku|dsva|joint|other",'
    '"kind":"official|semi|party|media|fan|trend","url":"実在するURL","event_date":"YYYY-MM-DD or 空文字",'
    '"deadline":"締切・終了日 YYYY-MM-DD or 空文字","facts":["確認できた事実(日付・期限・場所・価格を含める)"],'
    '"dedup_key":"英小文字ハイフンの話題ID(毎年ある定例企画は年を含める。例: shiny-summer-pair-2026)",'
    '"engagement":"高|中|低","mentioned_idols":["言及アイドル名"]}。'
    "kindの定義: official=アイマス公式(公式ポータル・ブランド公式サイト・公式Xアカウント)のみ/semi=公式レーベル・公式ストア等(日本コロムビア・ランティス・アソビストア等)/party=主催者・販売元・自治体・コラボ先などその他の当事者/media=報道/fan=ファン発/trend=現象。実在の情報のみ・憶測や未確認の噂は除外・個人への批判は除外。JSON以外のテキスト禁止。"
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
            seen = state.get(s["id"], [])
            state[s["id"]] = (found + [u for u in seen if u not in found])[:500]
            stats[s["id"]] = {"found": len(found), "new": len([n for n in new_items if n["source_id"] == s["id"]])}
        except Exception as e:
            stats[s["id"]] = {"error": str(e)[:120]}
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # 新着を facts 化(Claude 1コール・最大12件/回)
    cands = []
    batch = new_items[:12]
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
    return cands, {"stats": stats, "new": len(new_items), "facted": len(cands)}


# ---- A-2 / B 探索 --------------------------------------------------------------

def build_prompts() -> list[dict]:
    queries = yaml.safe_load((ROOT / "prompts" / "queries.yml").read_text(encoding="utf-8"))
    return queries


def claude_exec(prompt: str, timeout: int = 300) -> list:
    r = subprocess.run(
        ["claude", "-p", prompt, "--model", CLAUDE_MODEL,
         "--allowedTools", "WebSearch,WebFetch"],
        capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=ROOT)
    return extract_json_array(r.stdout)


def run_explores(skip_claude: bool, skip_grok: bool) -> tuple[list[dict], dict]:
    queries = build_prompts()
    procs = []
    for q in queries:
        window = "直近72時間" if q["key"] in ("trend", "fan-culture") else "直近48時間"
        if not skip_claude:
            cp = (f"{window}のアイドルマスター関連情報のうち「{q['topic']}」について、Web検索で公式サイト・報道・"
                  f"特設ページを調査し、確認できた事実を最大8件。" + ITEM_FORMAT)
            procs.append((q["key"], "claude", subprocess.Popen(
                ["claude", "-p", cp, "--model", CLAUDE_MODEL, "--allowedTools", "WebSearch,WebFetch"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                stdin=subprocess.DEVNULL, cwd=ROOT)))
        if not skip_grok:
            gp = (f"{window}のX(Twitter)上のアイドルマスター関連のうち「{q['topic']}」を調査し、"
                  f"公式告知とエンゲージメントの高い話題を最大8件。" + ITEM_FORMAT)
            procs.append((q["key"], "grok", subprocess.Popen(
                ["grok", "-p", gp, "--json-schema", str(SCHEMA_PATH)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                stdin=subprocess.DEVNULL, cwd=ROOT)))
    items, per = [], {}
    deadline = time.time() + 600
    for key, via, p in procs:
        try:
            out, _ = p.communicate(timeout=max(30, deadline - time.time()))
            got = extract_json_array(out)
        except subprocess.TimeoutExpired:
            p.kill()
            got = []
        for it in got:
            it["_via"] = via
        items += got
        per[f"{via}:{key}"] = len(got)
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
            for k in ("event_date", "deadline"):
                v = (it.get(k) or "").strip()
                if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
                    c["event_date" if k == "event_date" else "deadline"] = v
            if url in seen_url:  # URL 重複は facts をマージ
                tgt = seen_url[url]
                tgt["facts"] = list(dict.fromkeys(tgt["facts"] + c["facts"]))
                continue
            seen_url[url] = c
            out.append(c)
        except Exception:
            continue
    return out


def verify(cands: list[dict]) -> dict:
    """簡易 verify: X は Grok 観測を信頼。それ以外は URL 生存で confirmed。"""
    counts = {"confirmed": 0, "unconfirmed": 0, "failed": 0}
    for c in cands:
        try:
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
    day = now_jst().strftime("%Y-%m-%d")
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

    watch_cands, watch_info = ([], {"skipped": True}) if args.skip_watch else run_watch(claude_exec)
    explore_items, per_query = run_explores(args.skip_claude, args.skip_grok)
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
        notify("collect", f"想定外のエラー: {e}", ok=False)
        sys.exit(1)
