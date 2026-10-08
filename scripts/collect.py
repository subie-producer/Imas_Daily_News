#!/usr/bin/env python3
"""collect: 定点観測+探索+X動向 をまとめて candidates/<号日付>.json に記録し、
edition ブランチへ push する。(PIPELINE §1〜2)

  python3 scripts/collect.py [--no-git] [--skip-watch] [--skip-explore] [--skip-grok]
                             [--force-grok]

- 定点観測(A-1): sources.yml の一覧差分 → 新着 URL の本文を Claude(COLLECT_MODEL)が
  facts 化する。渡された本文を読むだけの役なので安いモデルでよい
- 探索(A-2): **Luna(codex / EXPLORE_MODEL)× 面数ぶん並列**。Web を検索してネタを見つける。
  codex に WebSearch 専用ツールは無いが、sandbox の通信を開けばシェルから検索・取得ができる。
  **読み取り専用**で起動する(取得したページの指示でリポジトリを書き換えられないように)。
  codex には --max-budget-usd 相当が無いため、暴走を止めるのは EXPLORE_TIMEOUT だけ
- X 動向(B): 役割を分ける(編集長 2026-10-04)。
  1. Grok が**面ごとに独立セッション**で X の検索だけをし、見つけた投稿を全文のまま書き出す(prompts/grok-collect.md)
  2. Luna が面ごとにその投稿を読み、リンク先を開いて確かめて候補にする(verify_grok_faces / prompts/grok-verify.md)。
     X の原本でしか確かめられない問いも挙げる
  3. 問いがあれば、週の検索予算(セッション記録から数えた直近7日の実使用)を確かめて、Grok に原本を調べさせ(deep_dive_grok /
     prompts/grok-deep.md)、もう一度 Luna が確かめる
- 正規化・URL 重複マージ → candidates へ追記 → 簡易 verify → commit & push
"""
import argparse
import collections
import html as html_lib
import datetime
import json
import os
import shutil
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import (ENV, ROOT, COLLECT_MODEL, CODEX_WRITE_MODEL, EXPLORE_MODEL,
                     EXPLORE_MAX_BUDGET_USD, JST, JobLockTimeout, job_lock, prompt_file, clean_url, append_metric, classify_source,
                     extract_periods, html_to_text, loads_strict, needs_render, partial_output, quote_on_page, read_for_raw, reap, save_raw, schema_ok,
                     RenderFailed, render_page,
                     set_quiet, unbacked_facts,
                     anomaly, checkout_edition_branch, classify_retag_lint, collect_oncall_end, commit_and_push, diagnose_anomalies,
                     edition_date, mark_collect_oncall,
                     extract_json_array, git, notify, notify_crash, now_jst, prompt_part, render_prompt,
                     X_ANON_POST, x_post_author)

# 定点観測の新着を1回の実行で facts 化する上限。1回の Claude 呼び出しに載る量の都合で
# 区切るだけであり、超過分は捨てずに次回へ繰り越す(run_watch の状態保存を参照)。
WATCH_BATCH = int(ENV.get("WATCH_BATCH", "12"))
# facts 化のバッチを同時に走らせる数。新着は件数にかかわらず全部を処理する(上限で打ち切らない。編集長 2026-09-30
# 「全部処理してねーじゃん」)。件数が増えても時間が線形に伸びないよう、バッチは並列に走らせる
WATCH_PARALLEL = int(ENV.get("WATCH_PARALLEL", "3"))
# 通常の収集が2度読めずに諦めた新着を、当番の取り直しのために残す上限(state の _given_up)
GIVEN_UP_MAX = 200
# 定点観測で facts 化のために渡すページ本文の量。切り詰めるとそのぶん facts が痩せる
WATCH_BODY_CHARS = int(ENV.get("WATCH_BODY_CHARS", "20000"))
# 探索(Luna)1クエリの打ち切り。codex には --max-budget-usd 相当が無いので、
# 暴走を止められるのは時間だけになる。**起動時から**数える
EXPLORE_TIMEOUT = int(ENV.get("EXPLORE_TIMEOUT", "900"))
# Grok(X 動向)を回す時刻。JST の「時」をカンマ区切りで指定する。空なら毎回回す。
# SuperGrok は週次のセッション上限があり、1回の収集で10セッション消費するため、
# 収集の頻度とは別に絞る必要がある。ブランド10面は減らさない(絞るのは回数だけ)。
GROK_HOURS = ENV.get("GROK_HOURS", "").strip()
# **手数(--max-turns)では制限しない。**週次上限の実体は X 検索の回数だけで、ページを開く・コマンドを実行する・書く手数は
# 枠を消費しない。手数で縛ると、検索を終えたあとの確認や書き出しの途中で切られて成果ごと失う
# (実測: 3 → 9面すべて0件、8 → 9面中5面0件、12 → 9/15〜10/3 の171セッション中20本が打ち切り・7本は面ごと0件。
#  編集長 2026-10-04「手数の概念が悪い。Web Search だけをカウントしないと意味がない」)。
# 検索の回数は依頼文で指示し(GROK_MAX_SEARCHES)、実際の回数をセッション記録から数えて記録・超過を異常にする(grok_search_counts)。
# 暴走は1面あたりの時間(GROK_TIMEOUT)で止める
# 1面あたりの X 検索回数。**週次上限の実体はこれ**(実測 0.087%/検索 = 週およそ1150検索)。
# まとめ方式にしてから 9面23検索で 2% しか使わなかったので、予算に余裕がある。
#   4回/面 → 36検索/回 = 3.1%/日(週22%)
#   6回/面 → 54検索/回 = 4.7%/日(週33%)  ← ここを採る
#   8回/面 → 72検索/回 = 6.3%/日(週44%)
# 数字を書いても厳密には守られない(「最大10回」で34回引いた実績がある)ため、
# 掘る角度を列挙して「言い換えでは足さない」と縛るほうを主にしている。
GROK_MAX_SEARCHES = int(ENV.get("GROK_MAX_SEARCHES", "6"))
# 一括取得の取得件数。x_keyword_search の limit にそのまま渡す。
# 既定の 10 では1回で足りず引き直しを誘発する(検索回数=週次予算なので、
# 1回を広く取るほうが安い)
GROK_SWEEP_LIMIT = int(ENV.get("GROK_SWEEP_LIMIT", "40"))
# 10面を1セッションで回すぶん長い。途中で切れても面ごとにファイルへ書かせているので
# そこまでの成果は残る
GROK_TIMEOUT = int(ENV.get("GROK_TIMEOUT", "3000"))
# 週次上限の X 検索回数(実測 0.065〜0.087%/検索 → 週およそ1150〜1550回。少ないほうに合わせる)と、深掘りに使ってよい割合。
# 深掘り(deep_dive_grok)は、直近7日の実使用(セッション記録から数える)を差し引いた残りの範囲でだけ回す。
# 基本の調べ(1日1回・9面×6回=54回、週378回)を必ず残すため、深掘りは週の上限の DEEP_SHARE までに抑える
GROK_WEEKLY_SEARCHES = int(ENV.get("GROK_WEEKLY_SEARCHES", "1100"))
GROK_DEEP_SHARE = float(ENV.get("GROK_DEEP_SHARE", "0.8"))
# 1回の収集で深掘りに使う検索の上限(問い1つにつき2回まで)
GROK_DEEP_MAX = int(ENV.get("GROK_DEEP_MAX", "24"))
# 面別セッションの同時実行数。多すぎると X 側で絞られるおそれがあるので控えめに置く
GROK_WAVE = int(ENV.get("GROK_WAVE", "3"))
# 推論の深さ。既定は high で走っており、消費の最大費目が reasoning だった
# (1回の収集で 1.59MB。tool_result 495KB・assistant 120KB を大きく上回る)。
# 収集は「調べて写す」作業で深い推論を要さないため medium に落とす。
GROK_EFFORT = ENV.get("GROK_EFFORT", "medium")
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
# 収集の依頼文の共通部品(本文は prompts/)。規則(何を書いてよいか)と形(候補1件の JSON)を分けてある:
# Grok は日本語のまとめを書くので規則だけ、探索・定点観測・写し替えは JSON を返すので形も渡す
COLLECT_RULES = prompt_part("collect-rules")
COLLECT_ITEM = prompt_part("collect-item")


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", errors="replace")


def fetch_rendered(url: str, timeout: int = 30) -> str:
    """描画したページの HTML。失敗は RenderFailed(終了コードと stderr の要点つき)で上げる(空で返さない)。"""
    return render_page(url, timeout)


def rendered_or_note(url: str) -> tuple[str, str]:
    """描画を補いとして使う呼び出し側(facts 化の本文・裏取りの読み直し)向け: (HTML, 失敗の要点)。
    失敗しても素の HTML や WebFetch で続けられるので止めないが、原因はログと呼び出し側の記録に残す"""
    try:
        return fetch_rendered(url) or "", ""
    except RenderFailed as e:
        print(f"  描画取得の失敗: {e}", flush=True)
        return "", str(e)


# ---- A-1 定点観測 --------------------------------------------------------------

def list_source(s: dict, known: set[str], fetch=None) -> tuple[list[tuple[str, str]], int, bool]:
    """観測先の一覧から (URL, 見出し) を集める。戻りは (一覧の並び, 読んだページ数, 上限で打ち切ったか)。

    **件数で打ち切らない。**一覧の先頭ページだけを見ていたとき、公式ニュースは毎回 12 件しか見えず、実行と実行の
    あいだに 12 件を超えて更新されると、押し出された記事を定点観測が一度も見ない(編集長 2026-09-30「公式のニュース
    12件しか見ないとか設計不備過ぎる」)。`page_url`(`{n}` がページ番号)を持つ観測先は、**既に見た記事だけの
    ページに行き当たるまで**遡る。`max_pages` まで読んでもまだ新着が続くなら、打ち切ったことを返す(呼び出し側が異常として上げる)。
    """
    fetch = fetch or (fetch_rendered if s["type"] == "portal" else http_get)
    found: list[tuple[str, str]] = []
    max_pages = int(s.get("max_pages") or 1) if s.get("page_url") else 1
    pages = 0
    for n in range(1, max_pages + 1):
        url = s["url"] if n == 1 else s["page_url"].format(n=n)
        html = fetch(url)
        if not (html or "").strip():
            # 取得の失敗(空の応答)を一覧の終わりと取り違えない。手前のページだけ既読にすると、
            # 次回は手前で止まって奥の新着を見ない(監査指摘 r102)。この観測先は今回まるごと失敗にし、次回同じ境界からやり直す
            raise RuntimeError(f"{url} の取得に失敗(空の応答)")
        pages = n
        on_page = []
        for m in re.finditer(s["list_regex"], html, re.DOTALL):
            u = m.group(1)
            if not u.startswith("http"):
                u = s["base"].rstrip("/") + "/" + u.lstrip("/")
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2) if m.lastindex >= 2 else "")).strip()
            if u not in {x for x, _ in found} and u not in {x for x, _ in on_page}:
                on_page.append((u, title))
        found += on_page
        # 既に見た記事だけのページ(か、空のページ=一覧の終わり)に来たら止める
        if not on_page or all(u in known for u, _ in on_page):
            return found, pages, False
    return found, pages, bool(s.get("page_url"))


def run_watch(claude_call, oncall_rerun: bool = False) -> tuple[list[dict], dict]:
    sources = yaml.safe_load((ROOT / "sources.yml").read_text(encoding="utf-8"))
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    new_items = []  # {source_id, brand, url, title, source_type}
    found_by_source = {}  # 状態の保存は facts 化の後に行うため、巡回結果を持ち越す
    stats = {}
    for s in sources:
        if not s.get("enabled"):
            continue
        try:
            listed, pages, cut = list_source(s, set(state.get(s["id"], [])))
            if s["id"] not in state:
                # 初めて見る観測先: いまの一覧は「既に見た」として記録するだけ(過去の記事を新着として候補に流さない。
                # 流すと、古い知らせが今日の記事として載る)。次の実行から、これより新しいものが新着になる
                found_by_source[s["id"]] = [u for u, _ in listed]
                stats[s["id"]] = {"found": len(listed), "pages": pages, "new": 0, "baseline": True}
                continue
            found = []
            for u, title in listed:
                found.append(u)
                if u not in state.get(s["id"], []):
                    new_items.append({"source_id": s["id"], "brand": s["brand"], "url": u,
                                      "title": title, "source_type": s.get("source_type", "公式"),
                                      "csr": s["type"] == "portal"})
            found_by_source[s["id"]] = found
            stats[s["id"]] = {"found": len(found), "pages": pages,
                              "new": len([n for n in new_items if n["source_id"] == s["id"]])}
            if cut:
                # 上限のページまで読んでも新着が続いた = それより古い新着を見落としている可能性。申告で終えずに当番がなぜなぜする
                notify("collect", f"定点観測 {s['id']}: {pages}ページ読んでも既に見た記事に行き当たらない(新着が上限を超えた"
                                  f"か、一覧の形が変わった)。これより古い新着を見落としている可能性がある", ok=False)
        except Exception as e:
            # 既読は更新しない(found_by_source に入れない)ので、次回同じ境界からやり直す。見えない失敗にしない(当番のなぜなぜへ)
            # 原因(描画取得なら終了コードと stderr の要点)まで残す。切り詰めて「空の応答」だけにしない
            stats[s["id"]] = {"error": str(e)[:400]}
            notify("collect", f"定点観測 {s['id']}: 一覧を読めなかった({str(e)[:400]})。既読は更新せず、次回やり直す", ok=False)

    # 前回 facts 化の出力が読めずにやり直しになった新着(未処理の列)を**先に**処理する。それらは既読にしていないが、一覧の上では
    # 既読のページの奥に隠れるので、一覧を遡っても二度と見えない(監査指摘 r102)
    pending = [it for it in state.get("_pending", []) if isinstance(it, dict) and it.get("url")]
    # 当番の取り直しでは、通常の収集が2度読めずに諦めた新着(_given_up)も**先頭で**取り直す。諦めたものは既読なので、
    # 一覧からも未処理の列からも二度と見えない。これを当番が直しても拾えないと、落とした新着が選定リストに入らない(監査指摘)
    pending = ([it for it in state.get("_given_up", []) if isinstance(it, dict) and it.get("url")] if oncall_rerun else []) + pending
    pending = list({it["url"]: it for it in reversed(pending)}.values())[::-1]   # 同じ URL は先に出たほうだけ
    pending_urls = {it["url"] for it in pending}
    # 一覧にもまだ出ている繰り越し記事も、未処理の列の位置(先頭)で処理する(一覧の位置だと、また上限の外に回る。監査指摘 r103)
    new_items = pending + [n for n in new_items if n["url"] not in pending_urls]

    # 新着を facts 化。1回のプロンプトに載る量で WATCH_BATCH 件ずつに区切り、**件数にかかわらず全部を処理する**
    # (打ち切って残りを次の実行(5〜6時間後)へ回すと、その分だけ紙面が1日遅れる)。バッチは WATCH_PARALLEL 本ずつ同時に走らせる。
    # 次回へ回るのは、出力が読めなかったバッチ(やり直し)だけ
    from concurrent.futures import ThreadPoolExecutor
    batches = [new_items[i:i + WATCH_BATCH] for i in range(0, len(new_items), WATCH_BATCH)]
    cands, attempted = [], set()
    with ThreadPoolExecutor(max_workers=max(1, WATCH_PARALLEL)) as ex:
        for got, tried in ex.map(lambda b: facts_batch(b, claude_call, state, oncall_rerun), batches):
            cands += got
            attempted |= tried
    return finish_watch(new_items, attempted, found_by_source, stats, state, cands,
                        retried_given_up={it["url"] for it in state.get("_given_up", []) if isinstance(it, dict)} if oncall_rerun else set())


def facts_batch(batch: list[dict], claude_call, state: dict, oncall_rerun: bool = False) -> tuple[list[dict], set[str]]:
    """新着1バッチを facts 化する。戻りは (候補, 処理を試みた URL)。読めなかったページは、2回目で諦めたもの以外は試みたことにしない。"""
    cands = []
    if batch and claude_call:
        blobs = []
        for it in batch:
            body, why = "", ""
            if it["csr"]:
                t, why = rendered_or_note(it["url"])
                # 本文の切り詰めは facts の情報量に直結する。2500字にしていたとき、
                # 本文8,219字のページから facts を359字しか起こせていなかった
                body = html_to_text(t.encode("utf-8", "replace"))[:WATCH_BODY_CHARS]
            blobs.append({"url": it["url"], "title": it["title"], "brand_hint": it["brand"], "rendered_text": body, "render_error": why})
        # 素材は**人が読める形**(Markdown、1行が長くならない)で渡す。1行 60KB の JSON にすると Read ツールが
        # 行を切り詰めてモデルが本文を読めず、Bash で開けようとして時間切れになる(実測 2026-09-17: 420 秒)
        material = "\n\n".join(
            f"### {i + 1}. {b['url']}\n- title: {b['title'] or '(なし)'}\n- brand_hint: {b['brand_hint']}\n"
            + ("- 本文(取得済み):\n" + b["rendered_text"].strip() if b["rendered_text"].strip()
               else f"- 本文: (取得できず{'(' + b['render_error'][:200] + ')' if b['render_error'] else ''}。URL を WebFetch で読むこと)")
            for i, b in enumerate(blobs))
        prompt = render_prompt("watch-facts", RULES=COLLECT_RULES, ITEM=COLLECT_ITEM, MATERIAL=material)
        got = claude_call(prompt, timeout=420)
        # 出力は新着ページごとの結果(抽出済み/対象外/読めなかった)。処理済みにするのは、結果が契約どおりに返った
        # extracted・none のページだけ。読めなかった・応答に無い・形が崩れたページは未処理(0件の [] とは別)。
        # 以前は候補の配列だけを受け、[] をバッチ全件の「候補なし」として既読にしていたので、WebFetch で
        # 本文を読めなかったページも黙って既読になり、新着を失った(監査指摘 watch-read-status-contract)
        done = watch_page_results(got, len(batch))
        failed = [it for i, it in enumerate(batch) if i + 1 not in done]
        bad = state.setdefault("_unreadable", {})   # url → 読めなかった回数
        give_up = []
        if failed and not oncall_rerun:
            # 読めなかったページは既読にしない(次回そのまま拾い直す)。ただし同じ URL が2回読めなければ諦めて既読にする。
            # 毎回同じ先頭バッチをやり直すと、上限の外の新着が永久に後回しになる(監査指摘)
            # 当番の拾い直し(直後に同じバッチを再実行)では諦めない。原因が途中切れ等で直しが効いていなければここでも
            # 読めないが、既読にすると**原因未確定のまま新着を失う**(監査指摘 rerun-second-unreadable-drops-pending)。
            # 回数も進めず未処理の列に残し、残れば main が非0で「まだ直っていない」と申告する。
            for it in failed:
                bad[it["url"]] = bad.get(it["url"], 0) + 1
            give_up = [it for it in failed if bad[it["url"]] >= 2]
            for it in give_up:
                bad.pop(it["url"], None)   # 諦めたら回数も消す。残すと再登場時に1回で即既読になる(監査指摘)
                # 諦めても捨てない: 当番が原因を直したあとの取り直し(--oncall-rerun)が拾えるよう、諦めた新着として残す
                # (通常の収集では読み直さない。毎回先頭で読み直すと上限の外の新着が後回しになるため)
                state.setdefault("_given_up", []).append({**it, "given_up_at": now_jst().isoformat(timespec="seconds")})
        if failed:
            notify("collect", f"定点観測: facts 化で読めなかった新着 {len(failed)}/{len(batch)}件"
                              f"({'出力が読めない' if got is None else 'ページの結果が読めなかった・欠けた'})。次回に持ち越す"
                              f"(2度読めず通常の収集では諦めたもの {len(give_up)}件。当番が直したあとの取り直しで読み直す"
                              + "".join(f"\n  - {it['url']}" for it in give_up) + ")", ok=False)
        for i, it in enumerate(batch):
            if i + 1 in done:
                bad.pop(it["url"], None)
        cands = [c for i in sorted(done) for c in done[i]]
        for c in cands:
            c["_via"] = "watch"
        batch = [it for i, it in enumerate(batch) if i + 1 in done] + give_up
    return cands, {it["url"] for it in batch}


def watch_page_results(got, n: int) -> dict[int, list[dict]]:
    """facts 化の出力(新着ページごとの結果)から、処理済みのページ番号 → 候補 を返す。

    処理済みは status が extracted(候補1件以上)か none(候補0件)で、番号が 1〜n に1度だけ現れるページ。
    unreadable・番号の重複・形の崩れ(status と items が食い違う等)・応答に無いページは含めない(未処理として残す)。"""
    if not isinstance(got, list):
        return {}
    # 番号は真偽値を除いた整数だけ(Python では True が 1 として数えられ、ページ1が既読になる。監査指摘)
    page_no = lambda r: r["page"] if isinstance(r, dict) and type(r.get("page")) is int else None
    seen: dict[int, int] = {}
    for r in got:
        if page_no(r) is not None:
            seen[r["page"]] = seen.get(r["page"], 0) + 1
    done = {}
    for r in got:
        if not schema_ok(r, WATCH_PAGE_SCHEMA) or page_no(r) is None or not 1 <= r["page"] <= n or seen[r["page"]] != 1:
            continue
        items = r.get("items")
        # 候補は正規化で捨てられない形で事実のあるものだけ(1件でも崩れていれば、そのページは未処理。candidate_usable を共有。監査指摘)
        if not isinstance(items, list) or not all(candidate_usable(c) for c in items):
            continue
        if (r.get("status") == "extracted" and items) or (r.get("status") == "none" and not items):
            done[r["page"]] = items
    return done


def finish_watch(new_items: list[dict], attempted: set[str], found_by_source: dict, stats: dict, state: dict,
                 cands: list[dict], retried_given_up: set[str] = frozenset()) -> tuple[list[dict], dict]:
    # 状態の保存は facts 化の**後**。既知にするのは「今回処理を試みた URL」だけで、
    # 上限を超えて手つかずのまま残った新着は未読のままにする。
    #   - 巡回直後に全件を既知にすると、上限超過分は次回 new と判定されず、
    #     candidates に一度も載らないまま消える(カバレッジの穴になる)
    #   - 処理を試みた URL は、結果が0件でも既知にする(毎回同じページを
    #     読み直して上限枠を食い潰さないため)
    # 途中で落ちた場合も未保存なので、次回の実行がそのまま拾い直す。
    deferred = [n for n in new_items if n["url"] not in attempted]
    deferred_urls = {n["url"] for n in deferred}
    for sid, found in found_by_source.items():
        keep = [u for u in found if u not in deferred_urls]
        seen = state.get(sid, [])
        state[sid] = (keep + [u for u in seen if u not in keep])[:500]
        if sid in stats:
            stats[sid]["deferred"] = len([n for n in deferred if n["source_id"] == sid])
    # 未処理の列から処理したもの(もう一覧に出ていないことがある)も既読にする
    for it in new_items:
        if it["url"] in attempted and it["url"] not in state.get(it["source_id"], []):
            state[it["source_id"]] = ([it["url"]] + state.get(it["source_id"], []))[:500]
    # やり直しになった新着(出力が読めなかったバッチ)は未処理の列に残し、次回は一覧より先に処理する(一覧の上では既読のページの奥に隠れるため)
    state["_pending"] = deferred
    # 当番の取り直しで読み直した「諦めた新着」は、読めたら候補へ、読めなければ未処理の列(上)へ移ったので、諦めた列から外す。
    # 今回新しく諦めたもの(通常の収集)は残す。古いものから上限で切る
    given_up = [g for g in state.get("_given_up", []) if isinstance(g, dict) and g.get("url") not in retried_given_up]
    if len(given_up) > GIVEN_UP_MAX:
        notify("collect", f"定点観測: 諦めた新着が {len(given_up)}件たまり、古い {len(given_up) - GIVEN_UP_MAX}件を取り直しの対象から外した"
                          + "".join(f"\n  - {g['url']}" for g in given_up[:-GIVEN_UP_MAX][:20]), ok=False)
    state["_given_up"] = given_up[-GIVEN_UP_MAX:]
    # **ここでは保存しない。**既読の確定は candidates への書き込みと同じ成功境界にする。
    # 先に既読にすると、後段(正規化・verify・保存)が例外で落ちたとき、新着は既読なのに
    # candidates に無い、という取りこぼしになる(監査指摘)。main が保存後に書く
    stats["_state"] = state

    if deferred:
        print(f"定点観測: 新着 {len(new_items)}件のうち {len(attempted)}件を処理、"
              f"{len(deferred)}件を次回へ繰り越し", flush=True)
    return cands, {"stats": stats, "new": len(new_items), "facted": len(cands),
                   "deferred": len(deferred)}


# ---- A-2 / B 探索 --------------------------------------------------------------

def build_prompts() -> list[dict]:
    queries = yaml.safe_load((ROOT / "prompts" / "queries.yml").read_text(encoding="utf-8"))
    return queries


def read_written(p: Path) -> str:
    """セッションが書き出したファイルの中身。無い・読めない(ファイルでなくディレクトリが作られた など)は空として扱う
    (書き出しが無い面として、やり直し・異常へ回る。読み取りの例外で収集全体を止めない。監査指摘)。"""
    try:
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    except OSError:
        return ""


# 時間切れ・異常終了の書きかけを退けられなかった書き出し → その時の中身。正常に終わった試みが中身を書き換えるまで、
# この収集の間は「完了した書き出し」と読まない
STUCK_OUTPUTS: dict[Path, str] = {}


def grok_wrote(outdir: Path, q: dict) -> bool:
    """その面の Grok がまとめのファイルを残したか(「なし」と書いた面も残したことになる)。
    退けられなかった書きかけ(STUCK_OUTPUTS)は、その後の試みが正常に終わっても完了と読まない(監査指摘)。"""
    p = outdir / f"{q['key']}.md"
    return p not in STUCK_OUTPUTS and bool(read_written(p).strip())


def run_grok_faces(queries: list[dict], outdir: Path, errs: dict, retry: bool = False) -> set[str]:
    """面ごとに Grok の基本の調べ(X の検索だけ)を走らせる。戻りは正常に終わらなかった面。"""
    return run_grok_prompts([(q, write_grok_prompt(outdir, q, retry=retry)) for q in queries], errs)


def grok_basic(queries: list[dict], outdir: Path, grok_errs: dict) -> list[str]:
    """Grok の基本の調べを全面で走らせ、終わらなかった面だけ1回やり直す。戻りはやり直しても終わらなかった面(名指しで異常にした)。

    まとめのファイルを残せなかった面・正常に終わらなかった面は、「先に書く」順で1回だけやり直す
    (実測 2026-10-03: 学マスの面が打ち切られてファイルを書けず0件。公式Xの4コマ第174話を1日遅れで載せた)。
    未完了は終了状態と書き出しの両方で決める(書きかけを退けられずに残った面を完了とみなさない。監査指摘)。"""
    failed = run_grok_faces(queries, outdir, grok_errs)
    missing = [q for q in queries if not grok_wrote(outdir, q) or q["key"] in failed]
    if not missing:
        return []
    print(f"grok: まとめを残せなかった面 {', '.join(q['key'] for q in missing)} をやり直す", flush=True)
    failed = run_grok_faces(missing, outdir, grok_errs, retry=True)
    still = [q["key"] for q in missing if not grok_wrote(outdir, q) or q["key"] in failed]
    # やり直しも時間切れ・異常終了した面は、途中までの書き出しがあればそれを使う(全部は失わない)。未完了は下で名指しする
    for k in still:
        if read_written(outdir / f"{k}.md").strip():
            # 書きかけを退けられずに残った面: それを途中までの書き出しとして使い、そう名指しする
            grok_errs[k] = "退けられなかった書きかけを使う(調べが終わっていない): " + grok_errs.get(k, "")
            continue
        # 試みごとの途中までの書き出し(.partial-<n>.md)を全部つなぐ(初回と、やり直しで書けた投稿が違うことがある。監査指摘)
        part = "\n\n".join(t for t in (read_written(p).strip() for p in sorted(outdir.glob(f"{k}.partial-*.md"))) if t)
        if part.strip():
            try:
                (outdir / f"{k}.md").write_text(part, encoding="utf-8")
                grok_errs[k] = "途中までの書き出しを使う(調べが終わっていない): " + grok_errs.get(k, "")
            except OSError as e:      # 書き出し先がディレクトリになっている等。その面は失う(下で名指し)
                grok_errs[k] = f"途中までの書き出しも戻せない({type(e).__name__}): " + grok_errs.get(k, "")
    if still:     # 候補の数に関係なく名指しする(全面が未完了でも、途中までの書き出しから候補が出ると全面0件にならない。監査指摘)
        notify("collect", f"Grok(X 調査)の {', '.join(still)} 面が、やり直しても調べを終えられなかった"
                          "(途中までの書き出しがあればそれだけを使い、無ければその面の X の動きを丸ごと失う):\n"
                          + "\n".join(f"- {k}: {grok_errs.get(k, 'エラー出力なし')}" for k in still)
                          + "\n- セッション記録の失敗理由: " + grok_session_error(), ok=False)
    return still


def run_grok_prompts(targets: list[tuple[dict, Path]], errs: dict) -> set[str]:
    """Grok のセッションを GROK_WAVE 本ずつ走らせる。手数では縛らない(時間 GROK_TIMEOUT だけ)。エラー出力は errs に残す。
    戻りは、正常に終わらなかった(時間切れ・非0終了)面のキー(書き出しの有無では判定しない。監査指摘)。"""
    failed: set[str] = set()
    for i in range(0, len(targets), GROK_WAVE):
        procs = []
        for q, pp in targets[i:i + GROK_WAVE]:
            procs.append((q, subprocess.Popen(
                ["grok", "--prompt-file", str(pp), "--always-approve",
                 "--cwd", str(ROOT), "--reasoning-effort", GROK_EFFORT],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                stdin=subprocess.DEVNULL, cwd=ROOT, start_new_session=True), pp))
        for q, pr, pp in procs:
            timed_out = False
            try:
                _, err = pr.communicate(timeout=GROK_TIMEOUT)
                if pr.returncode or (err or "").strip():
                    errs[q["key"]] = f"exit {pr.returncode}: {(err or '').strip()[-300:]}"
                else:
                    errs.pop(q["key"], None)     # errs は最後の試みの状態(やり直しで直った面の古いエラーを残さない。監査指摘)
            except subprocess.TimeoutExpired:
                _, err = reap(pr)     # 子も落とし、それまでのエラー出力を上限つきで回収する
                timed_out = True
                errs[q["key"]] = f"時間切れ: {(err or '').strip()[-300:]}"
                print(f"grok: {q['key']} 面がタイムアウト(そこまでの記録は残る)", flush=True)
            out = pp.with_name(pp.name.removeprefix("prompt-"))
            # エラー出力の全文と、この試みの書き出しを、基本の調べ・やり直し・深掘りの別に残す(要約は末尾 300 字だけ。
            # 書き出しは次の試み・次の収集で消える。監査指摘)。経過は Grok のセッション記録に残る
            save_raw(edition_date(), f"grok-{pp.stem}", "", err or "", pr.returncode, files={out.name: read_for_raw(out)})
            if timed_out or pr.returncode != 0:
                failed.add(q["key"])
                # 時間切れ・異常終了の回が書いた書き出し(一部の投稿だけ・途中の「なし」)は答えにしない。試みごとに別の
                # <名前>.partial-<n>.md に退けて、面の書き出しが無い状態にする(基本の調べは「書けなかった面」としてやり直し・異常へ、
                # 深掘りは下で異常へ。非空のファイルを完了とみなして残りの検索を黙って失わない。やり直しが初回の途中出力を
                # 上書きしない。監査指摘)。退けられなければ、その面の異常として残して他の面を続ける
                if out.exists():
                    n = 1
                    while out.with_suffix(f".partial-{n}.md").exists():
                        n += 1
                    try:
                        out.replace(out.with_suffix(f".partial-{n}.md"))
                    except OSError as e:
                        # 退けられなければ、この収集の間はその書きかけを「完了」と読まない(STUCK_OUTPUTS に中身を控える。
                        # やり直しが新しく書かずに正常終了しても、古い書きかけで完了にしない。監査指摘)
                        STUCK_OUTPUTS[out] = read_written(out)
                        errs[q["key"]] = f"書きかけを退けられない({type(e).__name__}): " + errs.get(q["key"], "")
            elif out in STUCK_OUTPUTS and read_written(out) != STUCK_OUTPUTS[out]:
                STUCK_OUTPUTS.pop(out)      # 正常に終わった試みが新しく書いた: 完了した書き出し
    return failed


def grok_search_counts(since_ts: float) -> dict[str, int]:
    """since_ts 以降に始まった、このリポジトリの Grok セッションの面(brand)ごとの X 検索の回数(週次上限を消費するのはこれだけ)。
    回数は Grok のセッション記録(updates.jsonl の XSearch の呼び出し)から数える。"""
    import urllib.parse as _up
    base = Path.home() / ".grok" / "sessions" / _up.quote(str(ROOT), safe="")
    out: dict[str, int] = {}
    try:
        dirs = [d for d in base.iterdir() if d.is_dir() and d.stat().st_mtime >= since_ts]
    except OSError:
        return out
    for d in dirs:
        try:
            ch = (d / "chat_history.jsonl").read_text(encoding="utf-8", errors="replace")
            up = (d / "updates.jsonl").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'brand=\\"([a-z0-9-]+)', ch)
        if m:
            out[m.group(1)] = out.get(m.group(1), 0) + len(re.findall(r'"variant":\s*"XSearch"', up))
    return out


def grok_session_error(since_s: int = 7200) -> str:
    """直近 since_s 秒に、このリポジトリで開いた Grok セッションの失敗理由(Grok は API に拒まれても終了コード 0 で、
    理由はセッション記録 updates.jsonl にしか残らない)。見つからなければ「記録なし」。"""
    import urllib.parse as _up
    base = Path.home() / ".grok" / "sessions" / _up.quote(str(ROOT), safe="")
    reasons: list[str] = []
    try:
        for d in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if time.time() - d.stat().st_mtime > since_s:
                break
            u = d / "updates.jsonl"
            if u.exists():
                for m in re.finditer(r'"agent_result":"([^"]{0,300})"', u.read_text(encoding="utf-8", errors="replace")):
                    if "error" in m.group(1).lower() and m.group(1) not in reasons:
                        reasons.append(m.group(1))
    except OSError:
        pass
    return " / ".join(reasons[:3]) or "記録なし"


def parse_grok(out: str) -> list:
    """grok --output-format json はエンベロープ {"text": <本文>} で返す。素の JSON にも対応。"""
    try:
        obj = json.loads(out)
        out = obj.get("text", out)
    except (json.JSONDecodeError, AttributeError):
        pass
    return extract_json_array(out)


def explore_workdir(key: str) -> Path:
    """探索セッション専用の作業ディレクトリを作る(**リポジトリの外**)。

    探索は取得したページの中身を読む。そこに「このファイルを書き換えろ」と
    仕込まれていた場合、workspace-write で走る探索はリポジトリを書き換えられる。

    cwd をリポジトリ外に置けば、workspace-write の書き込み範囲からリポジトリが
    外れ、読み取り専用になる。探索が必要とするのは本文取得だけなので、
    実体を絶対パスで呼ぶだけの薄い入口を1つ置けば足りる。

    ここで止める。**探索に妙なページを踏ませない運用のほうが本筋**であり、
    セッション同士の相互汚染まで機械で塞ごうとすると、得るものに対して
    仕掛けが重くなりすぎる。
    """
    wd = Path(tempfile.mkdtemp(prefix=f"explore-{key}-"))
    (wd / "fetch_page.py").write_text(
        "#!/usr/bin/env python3\n"
        "# 本体はリポジトリ側(読み取り専用)。ここは呼び出すだけの入口\n"
        "import os, sys\n"
        f"os.execv(sys.executable, [sys.executable, {str(ROOT / 'scripts' / 'fetch_page.py')!r}]\n"
        "         + sys.argv[1:])\n", encoding="utf-8")
    return wd


def explore_argv(short: str) -> list[str]:
    """探索(Luna / codex)の起動引数。`short` は prompt_file が返す短い指示(素材はファイル側)。

    `-s read-only` では通信も遮断される(実測: 名前解決に失敗し fetch_page.py が
    動かない)。`sandbox_workspace_write.network_access` は名前のとおり
    workspace-write 用の設定なので、通信を使う以上 workspace-write で走らせる。
    ただし cwd は `explore_workdir()` が作るリポジトリ外のディレクトリなので、
    書き込めるのはそこと `/tmp` に限られ、リポジトリには手が届かない。
    """
    return ["codex", "exec", "-m", EXPLORE_MODEL, "-s", "workspace-write",
            # cwd が git リポジトリでないため、codex の作業前確認を外す
            "--skip-git-repo-check",
            "-c", "sandbox_workspace_write.network_access=true", short]


def claude_exec(prompt: str, timeout: int = 300):
    """定点観測の facts 化に使う Claude 呼び出し(探索とは別役)。

    **読めなかったら None を返す**(0件の [] とは別)。呼び出し側はそのバッチを
    既読にしない。以前は読めない出力も0件として既読にし、新着が二度と候補に
    ならなかった(監査指摘 P1-5)。
    """
    from pipelib import extract_json_array_strict, prompt_file
    import hashlib as _hl
    name = "watch-" + _hl.sha256(prompt.encode("utf-8")).hexdigest()[:8]
    try:
        r = subprocess.run(
            ["claude", "-p", prompt_file(edition_date(), name, prompt),
             "--model", COLLECT_MODEL,
             # 指示と素材はファイルなので **Read が要る**。WebSearch/WebFetch だけに絞っていたら、ファイルを読めずに
             # 420 秒待って落ちた(実測 2026-09-17)
             "--allowedTools", "Read,WebSearch,WebFetch",
             "--max-budget-usd", EXPLORE_MAX_BUDGET_USD],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=ROOT)
    except (subprocess.TimeoutExpired, OSError) as e:
        # 1バッチの失敗で収集全体を落とさない。読めなかった扱い(既読にしない)
        print(f"定点観測: facts 化のセッションが失敗({type(e).__name__})。このバッチは既読にしない", flush=True)
        save_raw(edition_date(), name, *partial_output(e))     # 時間切れまでの出力も残す
        return None
    # 生の出力を必ず残す(依頼文 metrics/work/<日付>/<name>.md と同じ名前。候補が出なかった理由を後から確かめるため。
    # 2026-10-04: まとめサイトの新着1件を失ったが出力が残っておらず、当番も原因を確定できなかった。当番の指摘 b39801ce34・8bdc37f36c)
    save_raw(edition_date(), name, r.stdout, r.stderr, r.returncode)
    if r.returncode != 0:
        # 異常終了の間際の出力は答えにしない(読めなかった扱い。既読にしない。監査指摘)
        print(f"定点観測: facts 化のセッションが異常終了(exit {r.returncode})。このバッチは既読にしない", flush=True)
        return None
    got = extract_json_array_strict(r.stdout)
    if got is None:
        print(f"定点観測: facts 化の出力が読めない(stderr: {(r.stderr or '')[-160:]})", flush=True)
    return got


def write_grok_prompt(outdir: Path, q: dict, retry: bool = False) -> Path:
    """1面ぶんの基本の調べ(X の検索だけ)の指示を書き出す(面ごとに1セッション)。

    **役割分担(編集長 2026-10-04)**: Grok は X の検索だけをして、見つけた投稿を全文のまま書き出す。リンク先を開いて確かめ、
    候補にするのは Luna(verify_grok_faces)。X の原本でしか分からない問いだけ、予算を確かめて Grok に深掘りさせる(deep_dive_grok)。
    以前は Grok に「リンク先を開いて確かめ、まとめる」まで頼み、JavaScript で描画されるページの解析に手数を使い切って
    面ごと0件になっていた(9/15〜10/3 の171セッション中20本が打ち切り・7本が0件)。
    """
    out = outdir / f"{q['key']}.md"
    days = 3 if q["key"] in ("trend", "fan-culture") else 2
    since = (now_jst() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    accounts = q.get("accounts") or []
    at = "、".join(f"@{a}" for a in accounts)
    froms = " OR ".join(f"from:{a}" for a in accounts)
    step1 = (f"1. 公式アカウントを1回で引く。`x_keyword_search` に `({froms}) since:{since}` を**1回だけ**渡す"
             f"({at} の投稿がまとめて取れる。1アカウントずつ引き直さない)" if froms
             else "1. (この面には公式アカウントの指定が無い。2 の角度から始める)")
    prompt = render_prompt("grok-collect", BRAND=q["brand"], TOPIC=q["topic"], TODAY=now_jst().strftime("%Y-%m-%d"),
                           SINCE=since, OUT=out, MAX_SEARCHES=GROK_MAX_SEARCHES, STEP1=step1,
                           RETRY=("- **やり直し**: 前回は書き出す前に終わった。1 の結果をまず書き、そのあとで 3 に進む\n"
                                  if retry else ""))
    pp = outdir / f"prompt-{q['key']}.md"
    pp.write_text(prompt, encoding="utf-8")
    return pp


def collect_health(watch_info, per_query: dict) -> list[str]:
    """収集の各系統で、取得に失敗したもの(人が読める説明)。空なら全系統の取得は正常。
    定点観測は観測先ごとの取得の誤りと未処理の繰り越し、探索は出力が読めない・打ち切られた面(per_query の explore_failed:<面>)。"""
    out = []
    stats = (watch_info or {}).get("stats") or {} if isinstance(watch_info, dict) else {}
    for sid, st in stats.items():
        if isinstance(st, dict) and st.get("error"):
            out.append(f"定点観測 {sid}: 一覧を読めなかった({st['error'][:80]})")
    if isinstance(watch_info, dict) and watch_info.get("deferred"):
        out.append(f"定点観測: 事実に起こせず次回へ回した新着 {watch_info['deferred']}件")
    out += [f"探索 {k.split(':', 1)[1]}: 出力が読めない・打ち切られた" for k, v in per_query.items() if k.startswith("explore_failed:") and v]
    return out


def wait_session(proc, deadline: float) -> bool:
    """codex のセッションを締切まで待つ。超えたらプロセスグループごと落とす。戻り値は締切内に終わったか。"""
    try:
        proc.wait(timeout=max(0, deadline - time.time()))
        return True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        proc.wait()
        return False


def read_json_list(p: Path) -> list | None:
    """セッションが書いた JSON 配列を読む。無い・読めない・配列でない・同じキーが2回ある(後の値で黙って上書きされる)なら None。"""
    try:
        v = loads_strict(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return v if isinstance(v, list) else None


GOOD_SOURCE_TYPES = ("公式", "準公式", "当事者", "報道")


def repoint_to_post(items: list[dict], posts_md: str) -> list[dict]:
    """候補の url が、X の投稿に貼られたリンク(動画など)で、種別が公式・準公式・当事者・報道でないなら、
    そのリンクを貼った投稿(公式の投稿を先)の url に付け直し、元の url は facts に残す。
    事実は投稿の本文から読んだもので、リンク先は出典として確かめられない(2026-10-05: 公式 X の
    コラボ動画告知が YouTube の url で候補になり、未確認の出典として執筆に見送られた)。"""
    # 投稿の区切りは突き合わせ(post_blocks / post_url)と共有する。url の無い塊(url の行が空・崩れた投稿)のリンクは
    # どの投稿にも帰属させない(前の投稿へ付け直して、誤った出典にしない。監査指摘)
    posts: list[tuple[str, list[str]]] = []
    for block in post_blocks(posts_md):
        url = post_url(block)
        if not url:
            continue
        links: list[str] = []
        in_links = False
        for line in block.splitlines():
            if line.startswith("- リンク:"):
                in_links = True
            elif in_links and (m := re.match(r"\s+-\s*(https?://\S+)", line)):
                links.append(m.group(1))
            elif not line.startswith(" "):
                in_links = False
        posts.append((url, links))
    posts.sort(key=lambda p: classify_source(p[0]) != "公式")
    for it in items:
        url = it["url"]
        if is_x(url) or classify_source(url) in GOOD_SOURCE_TYPES:
            continue
        post = next((p for p, links in posts if url in links), None)
        if post:
            it["url"] = post
            it["facts"] = [*(it.get("facts") or []), f"リンク: {url}"]
    return items


def verify_grok_faces(queries: list[dict], outdir: Path, suffix: str = "",
                      ledger: dict | None = None) -> tuple[list[dict], dict[str, list[dict]]]:
    """Grok が書き出した X の投稿(<key><suffix>.md)を、面ごとに Luna が読み、リンク先を開いて確かめて候補にする(並列)。
    戻りは (候補, 面 → X の原本でしか確かめられない問い)。「なし」だけの面・ファイルの無い面は飛ばす。
    投稿ごとの結果(posts.json)と入力の投稿を突き合わせ、結果の無い投稿だけで1回やり直す(suffix に -again)。
    2回とも残った投稿と、済んだ投稿は ledger(lost_posts_ledger)に記録する。ledger を渡されなければこの段の結果を最終として
    喪失を通知し、渡されたら通知は呼び手が最後の段(深掘りの確かめ)のあとで report_lost_posts で1度だけ出す
    (深掘りの前に通知すると、深掘りで済んだ投稿を失ったと言い、深掘りでも読めない投稿は二度通知した。当番の指摘 11fc3de74b)。
    深掘りの確かめ(suffix に -deep)の問い(deep.json)は使わないので、読まず、読めなくても通知しない。
    Luna は探索と同じくリポジトリ外の作業ディレクトリで走る(ページの中身に指示が仕込まれていても、リポジトリに手が届かない)。"""
    own = ledger is None
    if own:
        ledger = lost_posts_ledger()
    wants_deep = "-deep" not in suffix
    deadline = time.time() + EXPLORE_TIMEOUT
    jobs = []
    for q in queries:
        text = read_written(outdir / f"{q['key']}{suffix}.md").strip()
        if not text or re.fullmatch(r"(なし|見つからない)[。\s]*", text):
            continue
        wd = explore_workdir(f"verify-{q['key']}{suffix}")
        (wd / "x-posts.md").write_text(text + "\n", encoding="utf-8")
        prompt = render_prompt("grok-verify", INPUT=wd / "x-posts.md", OUT=wd / "items.json", DEEP=wd / "deep.json", POSTS=wd / "posts.json",
                               BRAND=q["brand"], TOPIC=q["topic"], TODAY=now_jst().strftime("%Y-%m-%d"),
                               RULES=COLLECT_RULES, ITEM=COLLECT_ITEM)
        # 進行ログ(標準出力・エラー)は作業ディレクトリのファイルで受け、生の出力に残す(ファイルを書かずに終わった回の原因を追うため)
        with open(wd / "session.log", "w", encoding="utf-8") as lf:
            jobs.append((q, wd, text, subprocess.Popen(explore_argv(prompt_file(edition_date(), f"grok-verify-{q['key']}{suffix}", prompt, base=wd)),
                                                       stdout=lf, stderr=subprocess.STDOUT, text=True,
                                                       stdin=subprocess.DEVNULL, cwd=wd, start_new_session=True)))
    items, deep = [], {}
    final = suffix.endswith("-again")
    again: list[dict] = []
    for q, wd, text, p in jobs:
        in_time = wait_session(p, deadline)
        # 何より先に、Luna が書いたものを生の出力として残す(このあとの処理で止まっても原因を追えるように)
        save_raw(edition_date(), f"grok-verify-{q['key']}{suffix}", "", "", p.returncode,
                 files={n: read_for_raw(wd / n) for n in ("x-posts.md", "items.json", "posts.json", "deep.json", "session.log")})
        # 時間切れ・異常終了のセッションが書いたファイルは答えにしない(全投稿を未処理としてやり直し・異常へ。監査指摘)
        got = read_json_list(wd / "items.json") if in_time and p.returncode == 0 else None
        # 投稿1件ずつの結果を、入力の投稿と突き合わせる。結果の無い・読めなかった投稿を黙って捨てない
        # (2026-10-06 の洗い出しで判明: 候補の配列だけを受けていたので、Luna が開けなかった・飛ばした投稿が記録なしに消えた。
        #  定点観測で新着を失ったのと同じ型)。候補そのものが読めなければ「候補にした」の申告も当てにならないので、全投稿が残り
        left = (post_blocks(text) or [text]) if got is None else unaccounted_posts(text, read_json_list(wd / "posts.json"), got)
        # 付け直し・正規化へ渡すのは、形を揃えた候補だけ(崩れた要素で収集全体を止めない。監査指摘)
        items += repoint_to_post([s for s in (shape_item(x) for x in got or []) if s], text)
        left_urls = {post_url(b) for b in left}
        ledger["settled"] |= {u for u in map(post_url, post_blocks(text)) if u and u not in left_urls}
        if left and not final:
            # 残った投稿だけを <key><suffix>-again.md に書き出し、もう1回だけ確かめさせる(下でまとめて)
            (outdir / f"{q['key']}{suffix}-again.md").write_text("\n\n".join(left) + "\n", encoding="utf-8")
            again.append(q)
        elif left:
            # 2回目でも残った投稿は、申告で終えない(当番のなぜなぜへ)。どの投稿を失うかを最後の段のあとで名指しする
            why = (f"{'候補の一覧が読めない' if got is None else '投稿ごとの結果に無い・読めなかった'}、"
                   f"{'締切内に終わった' if in_time else '時間切れ'}")
            ledger["lost"].setdefault(q["key"], []).extend((b, why) for b in left)
        raw_asks = read_json_list(wd / "deep.json") if in_time and p.returncode == 0 else None
        if raw_asks is None and wants_deep:
            # 「問いなし([])」と「書けなかった」を分ける。原本の確かめを黙って失わない(監査指摘 r116)
            notify("collect", f"Grok の {q['key']} 面: X の原本でしか確かめられない問い(deep.json)が読めない"
                              f"({'締切内に終わった' if in_time else '時間切れ'})。深掘りの機会を失う", ok=False)
        asks = [a for a in (raw_asks or []) if schema_ok(a, DEEP_ASK_SCHEMA) and a["question"].strip()]
        if wants_deep and raw_asks is not None and len(asks) < len(raw_asks):
            # 崩れた問いを「問いなし」にしない(深掘りの機会を黙って失わない。監査指摘)。正しい問いは使う
            notify("collect", f"Grok の {q['key']} 面: X の原本でしか確かめられない問いのうち {len(raw_asks) - len(asks)}件の形が崩れていて使えない"
                              "(深掘りの機会を失う)", ok=False)
        if asks and wants_deep:
            deep[q["key"]] = asks
        shutil.rmtree(wd, ignore_errors=True)
    print(f"grok: 確かめ{suffix or ''} {len(jobs)}面 → 候補 {len(items)}件、原本の問い {sum(len(v) for v in deep.values())}件", flush=True)
    if again:
        print(f"grok: 結果の無い投稿が残った面 {', '.join(q['key'] for q in again)} を、残った投稿だけでもう1回確かめる", flush=True)
        more, more_deep = verify_grok_faces(again, outdir, suffix=suffix + "-again", ledger=ledger)
        items += more
        for k, v in more_deep.items():
            deep.setdefault(k, []).extend(v)
    if own:
        report_lost_posts(ledger)
    return items, deep


def lost_posts_ledger() -> dict:
    """X の投稿の確かめの記録(全段で共有)。lost: 面 → [(2回確かめても残った投稿の塊, 理由)] / settled: どこかの段で済んだ投稿の url。"""
    return {"lost": {}, "settled": set()}


def report_lost_posts(ledger: dict) -> None:
    """最後の段まで確かめ終えて、どの段でも済まなかった投稿だけを、まとめて1度、投稿ごとに1行で名指しする。
    途中の段で残っても、後の段(深掘りの確かめ)で済んだ投稿は失っていない。同じ投稿が複数の段で残っても、
    複数の面で残っても(面どうしで同じアカウントを検索するので、同じ投稿が複数の面に書き出される)1行にし、所属の面を併記する
    (面ごとに数えると、同じ投稿の喪失を面の数だけ通知した。監査指摘 lost-post-cross-face)。"""
    rows: dict[str, list] = {}  # 投稿 → [表示, 面の並び, 理由の並び](最初に現れた順)
    for key in sorted(ledger["lost"]):
        for b, why in ledger["lost"][key]:
            u = post_url(b)
            if u and u in ledger["settled"]:
                continue
            row = rows.setdefault(u or b, [u or "(投稿に分けられない書き出し) " + b[:80], [], []])
            for lst, v in ((row[1], key), (row[2], why)):
                if v not in lst:
                    lst.append(v)
    if not rows:
        return
    lines = [f"- {shown}(面: {', '.join(keys)}。{' / '.join(whys)})" for shown, keys, whys in rows.values()]
    faces = sorted({k for _, keys, _ in rows.values() for k in keys})
    notify("collect", f"Grok の {', '.join(faces)} 面: X の投稿 {len(lines)}件を、深掘りまで確かめても候補にも「事実なし」にもできなかった。"
                      "この投稿の X の動きを失う:\n" + "\n".join(lines[:10])
                      + (f"\n- ほか {len(lines) - 10}件" if len(lines) > 10 else ""), ok=False)


# 投稿の url の行。見本は `- url: …` だが、行頭の記号が抜けた・全角のコロンなどの崩れも投稿の区切りとして拾う
# (区切りを取りこぼすと、その投稿は前の塊に吸われるか捨てられ、突き合わせから消える。監査指摘)。
# 字下げした行(投稿に付いたリンクの一覧)は区切りにしない
# 区切り(url の行があること)と値(その行の URL)は分けて読む: 値が空・不正でも url の行は投稿の区切り
# (区切りにしないと、その投稿が前の投稿の塊に吸われ、前の投稿の結果で済みにされて消える。監査指摘)。
# 値は同じ行の http(s) の URL だけ(空の url の行で次の行の文字を URL と読まない。監査指摘)
POST_SPLIT_LINE = re.compile(r"(?m)^(?:[-*・][ \t]*)?url[ \t]*[:：]", re.I)
POST_URL_LINE = re.compile(r"(?m)^(?:[-*・][ \t]*)?url[ \t]*[:：][ \t]*(https?://\S+)", re.I)
X_STATUS = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/\w+/status/\d+")


def post_blocks(text: str) -> list[str]:
    """Grok の書き出しを投稿ごとの塊に分ける。まず見出し(`### 番号`)で区切り、1つの区間に url の行が2つ以上あれば
    url の行でさらに区切る(見出しの抜けた投稿)。url の行が無い区間は、番号の見出しで始まるか X の投稿の URL を含めば
    url の無い塊として残す(url の行の抜けた投稿。突き合わせで必ず未処理になる)。位置(途中・末尾・先頭)に関係なく
    同じ規則で拾う(前の投稿の塊に吸われて、その投稿の結果で済みにされて消えるのを防ぐ。監査指摘)。"""
    # 投稿の中身(X の投稿 URL・投稿の欄)があるか。見出しは含めない(見出しだけの前置きを投稿にしない)
    has_post = lambda s: bool(X_STATUS.search(s) or re.search(r"(?m)^\s*[-*・]?\s*(本文|投稿者|投稿日時)\s*[:：]", s))
    out: list[str] = []
    for seg in re.split(r"(?m)^(?=#{1,6}\s)", text):
        seg = seg.strip()
        if not seg:
            continue
        starts = [m.start() for m in POST_SPLIT_LINE.finditer(seg)]
        if not starts:
            # url の行の無い区間でも、投稿の中身・番号だけの見出しがあれば投稿として残す
            # (見出しが「### 投稿2」のように番号で始まらない崩れも拾う。監査指摘)。題だけの見出し(# 765 面のまとめ)は投稿にしない
            if re.match(r"#{1,6}[ \t]*\d+[.．)]?[ \t]*(\n|$)", seg) or has_post(seg):
                out.append(seg)
            continue
        # 先頭の url の行より前は、見出しだけなら最初の塊に含め、投稿の中身があれば(見出しも url の行も無い投稿)別の塊にする
        if has_post(seg[:starts[0]]):
            out.append(seg[:starts[0]].strip())
            cuts = starts
        else:
            cuts = [0] + starts[1:]
        out += [seg[a:b].strip() for a, b in zip(cuts, cuts[1:] + [len(seg)])]
    return out


def post_url(block: str) -> str:
    m = POST_URL_LINE.search(block)
    return m.group(1) if m else ""


def unaccounted_posts(text: str, results, written: list) -> list[str]:
    """投稿の塊のうち、Luna の結果が済んでいないもの(読めなかった・結果に無い・形が崩れた)。
    済んだとみなすのは none と、**実際に書かれた候補**(written = items.json の中身)を指す extracted だけ(「候補にした」と
    言って候補を書いていない投稿を済みにしない。監査指摘)。結果の一覧そのものが読めなければ、全部の投稿が未処理。
    書き出しが投稿に分けられない(形が崩れた)なら、書き出し全体を1つの未処理にする(黙って0件にしない。監査指摘)。"""
    # 後段(normalize)で捨てられる形・事実の無い候補は「書かれた候補」に数えない(判定は candidate_usable で共有。監査指摘)
    items = {x["url"].strip() for x in written or [] if candidate_usable(x)}
    # 投稿1件ごとに結果を数え、**契約どおりの結果がちょうど1つ**ある投稿だけを済みにする
    # (同じ投稿に none と unreadable が返る矛盾を、none で済みにしない。監査指摘)
    # 形の検査(POST_RESULT_SCHEMA)より前に数える(崩れた結果も「同じ投稿への2つ目の結果」として矛盾に数える)
    per_url: dict[str, list[dict]] = {}
    for r in results if isinstance(results, list) else []:
        if not (isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"].strip()):
            continue    # 空の url の結果は、どの投稿の結果にも数えない(url の無い塊を済みにしてしまう。監査指摘)
        per_url.setdefault(r["url"].strip(), []).append(r)

    def settled(r) -> bool:
        # none は item が空、extracted は item が実際に書かれた候補の url(契約。依頼文 grok-verify 3b)
        if not schema_ok(r, POST_RESULT_SCHEMA):
            return False
        return (r["status"] == "none" and not r["item"].strip()) or (r["status"] == "extracted" and r["item"].strip() in items)
    done = {u for u, rs in per_url.items() if len(rs) == 1 and settled(rs[0])}
    blocks = post_blocks(text)
    if not blocks:
        return [text] if text.strip() else []
    return [b for b in blocks if not post_url(b) or post_url(b) not in done]     # url の無い塊は必ず未処理


def grok_week_usage(now_ts: float | None = None) -> int:
    """直近7日に、この利用者の**すべての** Grok セッションが使った X 検索の回数(週次上限の消費の実数)。
    利用枠は利用者で共有なので、本番のクローン以外(dev クローン・手での利用)の検索も差し引く(監査指摘 r116)。"""
    since = (now_ts or time.time()) - 7 * 86400
    n = 0
    for u in (Path.home() / ".grok" / "sessions").glob("*/*/updates.jsonl"):
        try:
            if u.stat().st_mtime >= since:
                n += len(re.findall(r'"variant":\s*"XSearch"', u.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return n


def deep_budget(used_7d: int) -> int:
    """この回の深掘りに使ってよい X 検索の回数。週の上限の GROK_DEEP_SHARE までの残りと、1回の上限 GROK_DEEP_MAX の小さいほう。"""
    return max(0, min(GROK_DEEP_MAX, int(GROK_WEEKLY_SEARCHES * GROK_DEEP_SHARE) - used_7d))


def plan_deep_dive(deep: dict[str, list[dict]], budget: int) -> dict[str, list[dict]]:
    """予算の範囲で、深掘りする問いを面ごとに選ぶ(問い1つに検索2回。面を順に1問ずつ配って偏らせない。1面3問まで)。"""
    chosen: dict[str, list[dict]] = {}
    left = budget
    for i in range(3):
        for key, asks in sorted(deep.items()):
            if i < len(asks) and left >= 2:
                chosen.setdefault(key, []).append(asks[i])
                left -= 2
    return chosen


def deep_dive_grok(queries: list[dict], outdir: Path, chosen: dict[str, list[dict]], errs: dict) -> set[str]:
    """選んだ問いだけを、Grok に X の原本で調べさせる(<key>-deep.md に書き出す)。戻りは正常に終わらなかった面。"""
    by_key = {q["key"]: q for q in queries}
    targets = []
    for key, asks in chosen.items():
        q = by_key[key]
        pp = outdir / f"prompt-{key}-deep.md"
        pp.write_text(render_prompt(
            "grok-deep", BRAND=q["brand"], TODAY=now_jst().strftime("%Y-%m-%d"), OUT=outdir / f"{key}-deep.md",
            MAX_SEARCHES=2 * len(asks),
            QUESTIONS="\n".join(f"{i + 1}. {a['question']}(なぜ X の原本が要るか: {a.get('why') or '記載なし'})" for i, a in enumerate(asks))),
            encoding="utf-8")
        targets.append((q, pp))
    return run_grok_prompts(targets, errs)


def grok_scheduled_now(now=None) -> bool:
    """今回の実行で Grok を回すか。GROK_HOURS が空なら毎回回す(従来動作)。"""
    if not GROK_HOURS:
        return True
    hours = {h.strip().lstrip("0") or "0" for h in GROK_HOURS.split(",") if h.strip()}
    return str((now or now_jst()).hour) in hours


def collect_explore(key: str, proc, out_f, err_f, deadline: float) -> tuple[list, str]:
    """探索プロセス1つを回収する。**締切は起動時から数えた絶対時刻**。

    残り時間が無ければ待たずに落とす(以前は `max(30, ...)` としており、
    締切を過ぎても1本あたり30秒ずつ延びていた)。
    出力は一時ファイルで受けるので、回収の順番待ちでパイプが詰まることはない。
    """
    remain = deadline - time.time()
    cut = ""
    try:
        if remain <= 0:
            raise subprocess.TimeoutExpired(proc.args, 0)
        proc.wait(timeout=remain)
    except subprocess.TimeoutExpired:
        # codex 本体を kill しても配下(fetch_page.py 等)は生き残るので、
        # プロセスグループごと落とす(start_new_session=True で独立させてある)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        proc.wait()              # ゾンビを残さない
        cut = " [打ち切り]"
    out = err = ""
    for f, box in ((out_f, "out"), (err_f, "err")):
        try:
            f.flush()
            text = Path(f.name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        finally:
            f.close()
            Path(f.name).unlink(missing_ok=True)
        if box == "out":
            out = text
        else:
            err = text
    err = (err or "") + cut
    save_raw(edition_date(), f"explore-{key}-{time.time_ns() % 10**6}", out, err, proc.returncode)   # 生の出力を必ず残す
    from pipelib import extract_json_array_strict
    # 打ち切り・異常終了の間際の出力は答えにしない(途中までの候補を「全部」と読まない。監査指摘)
    got = extract_json_array_strict(out) if not cut and proc.returncode == 0 else None
    if got is None:
        # 「読めなかった」は「0件」と別の失敗。前置き・途中切れ・JSON 破損で調査結果が丸ごと
        # 消える経路なので、そう分かる形で残す(監査指摘 P1-5)
        print(f"探索: {key} の出力が読めない(本文 {len(out or '')}字) stderr: {err.strip()[-200:]}", flush=True)
        err = (err or "") + "\n[出力が読めない]"
        got = []
    elif not got:
        # 失敗の原因を捨てない(全滅したときに理由が分からなくなる)
        print(f"探索: {key} が0件 stderr: {err.strip()[-200:]}", flush=True)
    else:
        # 要素の形を揃える(null・崩れた要素で収集全体を止めない)。崩れた要素があれば「読めない出力」として記録する
        # (面の取得の失敗として collect_health に上がる。監査指摘)
        shaped = [s for s in (shape_item(x) for x in got) if s]
        broken = sum(1 for x in got if not item_intact(x))     # 捨てた候補と、欄・要素を落として揃えた候補
        if broken:
            print(f"探索: {key} の候補 {broken}/{len(got)}件の形が崩れていた(捨てた・欄を落とした)", flush=True)
            err = (err or "") + f"\n[出力が読めない] 形の崩れた候補 {broken}件"
        got = shaped
    return got, err


def run_explores(skip_explore: bool, skip_grok: bool) -> tuple[list[dict], dict]:
    queries = build_prompts()
    items, per = [], {}

    # **探索役は Luna(codex)**。全クエリ並列。締切は**起動前**に決める
    # (起動後に決めると、後から起動したぶんだけ実質の持ち時間が延びる)
    explore_deadline = time.time() + EXPLORE_TIMEOUT
    explore_procs = []
    grok_jobs = []
    for q in queries:
        window = "直近72時間" if q["key"] in ("trend", "fan-culture") else "直近48時間"
        if not skip_explore:
            cp = render_prompt("explore", WINDOW=window, TOPIC=q["topic"], MAX_ITEMS=8,
                               RULES=COLLECT_RULES, ITEM=COLLECT_ITEM)
            wd = explore_workdir(q["key"])
            # 出力は**一時ファイル**へ落とす。PIPE のまま並列起動して順番に
            # communicate すると、後続プロセスはパイプが埋まった時点で止まり、
            # 正常な探索が打ち切り扱いになる(監査指摘)
            of = tempfile.NamedTemporaryFile(prefix=f"explore-{q['key']}-", suffix=".out",
                                             delete=False, mode="w+", encoding="utf-8")
            ef = tempfile.NamedTemporaryFile(prefix=f"explore-{q['key']}-", suffix=".err",
                                             delete=False, mode="w+", encoding="utf-8")
            explore_procs.append((q["key"], subprocess.Popen(
                # 指示と素材は隔離ディレクトリ側のファイルで渡す(引数に詰めない。cwd がリポジトリ外なので base=wd)
                explore_argv(prompt_file(edition_date(), f"explore-{q['key']}", cp, base=wd)), stdout=of, stderr=ef, text=True,
                # 打ち切り時に子孫ごと落とせるよう、独立したプロセスグループにする
                # cwd は隔離ディレクトリ。ここが workspace-write の書き込み範囲になる
                stdin=subprocess.DEVNULL, cwd=wd, start_new_session=True), of, ef, wd))

    # **探索は Grok より先に回収する。**後回しにすると、Grok が長引くあいだ
    # 探索プロセスが締切を超えて走り続けてしまう(監査指摘)
    for key, p, of, ef, wd in explore_procs:
        got, err = collect_explore(key, p, of, ef, explore_deadline)
        for it in got:
            it["_via"] = "explore"
        items += got
        per[f"explore:{key}"] = len(got)
        if "[出力が読めない]" in err or "[打ち切り]" in err:
            per[f"explore_failed:{key}"] = 1     # 0件(正常な空振り)と取得の失敗を分ける(collect_health)
        shutil.rmtree(wd, ignore_errors=True)

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
        grok_errs: dict[str, str] = {}
        grok_started = time.time()
        grok_basic(queries, outdir, grok_errs)
        # Luna が面ごとに確かめて候補にする。X の原本でしか確かめられない問いは、予算を確かめて Grok に深掘りさせ、もう一度 Luna が確かめる
        # 投稿の喪失は、深掘りの確かめまで終えた最終結果で1度だけ通知する(当番の指摘 11fc3de74b)
        ledger = lost_posts_ledger()
        got, deep = verify_grok_faces(queries, outdir, ledger=ledger)
        if deep:
            used = grok_week_usage()
            budget = deep_budget(used)
            chosen = plan_deep_dive(deep, budget)
            print(f"grok: 深掘りの予算 {budget}回(直近7日の使用 {used}/{GROK_WEEKLY_SEARCHES}回)。"
                  f"問い {sum(len(v) for v in deep.values())}件のうち {sum(len(v) for v in chosen.values())}件を調べる", flush=True)
            per["grok_week_searches"] = used
            if chosen:
                # 深掘りが時間切れ・異常終了した面(書き出しは退けてある)と、正常に終わったのに書き出しが無い・空の面は、
                # 選んだ問いの確かめを失う。名指しで異常にする(判定は終了状態と書き出しの両方。監査指摘)
                failed = deep_dive_grok(queries, outdir, chosen, grok_errs)
                lost = sorted(failed | {k for k in chosen if not read_written(outdir / f"{k}-deep.md").strip()})
                if lost:
                    notify("collect", "Grok の深掘りが時間切れ・異常終了し、X の原本の確かめを失った面: "
                                      + ", ".join(f"{k}({grok_errs.get(k, '')[:120]})" for k in lost), ok=False)
                more, _ = verify_grok_faces([q for q in queries if q["key"] in chosen], outdir, suffix="-deep", ledger=ledger)
                got += more
        report_lost_posts(ledger)
        # 全面が正常に終わって明示的に「なし」と書いた(エラー出力も無い)なら、正常な空振りで異常ではない(監査指摘)
        explicit_none = not grok_errs and all(
            re.fullmatch(r"(なし|見つからない)[。\s]*", read_written(outdir / f"{q['key']}.md").strip()) for q in queries)
        if not got and explicit_none:
            print("grok 0件(全面が正常に終わり「なし」)", flush=True)
        elif not got:
            print("grok 0件", flush=True)
            # 全面0件を黙って流さない(2026-10-02: CLI が古く API に 426 で拒まれ、9面すべて0件。記録は「grok 0件」だけで、
            # 候補が半分以下になり号が16本に落ちたのに誰も気づかなかった)。面ごとのエラーと、Grok のセッション記録の失敗理由を添える
            notify("collect", f"Grok(X 調査)が {len(queries)}面すべてで0件。候補の約半分を失う:\n"
                              + ("\n".join(f"- {k}: {v}" for k, v in grok_errs.items()) or "- 面ごとのエラー出力なし")
                              + "\n- セッション記録の失敗理由: " + grok_session_error(), ok=False)
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
        # 週次上限を消費するのは X 検索の回数だけ。面ごとに実数を記録し、指示の2倍を超えた面は異常にする(依頼文の数字は厳密には守られない)
        searches = grok_search_counts(grok_started)
        for b, n in searches.items():
            per[f"grok_searches:{b}"] = n
        over = {b: n for b, n in searches.items() if n > GROK_MAX_SEARCHES * 2}
        print(f"grok: X 検索 {sum(searches.values())}回(面別 {searches})", flush=True)
        if over:
            notify("collect", f"Grok(X 調査)の検索が指示({GROK_MAX_SEARCHES}回/面)の2倍を超えた面: {over}。週次の利用枠を余分に消費している", ok=False)

    return items, per


# ---- 正規化・記録 --------------------------------------------------------------

# 収集役が申告する kind は**種別の判定には使わない**(source_types.yml が決める)。
# 申告を採っていたころ、攻略サイトを「公式」と申告した候補がそのまま紙面の
# バッジになっていた。kind は収集役の見立てとして残すが、紙面には出ない


def is_x(url: str) -> bool:
    return any(h in url for h in X_HOSTS)


def needs_classify(cands: list[dict]) -> bool:
    """判定表の更新(合議)→ 紙面の付け直しを走らせるか。今回の候補に未確認があるか、**紙面に未確認の出典が
    残っている**とき。候補だけで決めると、空振りや判定済みの候補ばかりの収集では紙面の未確認が残り続ける
    (合議の対象は紙面の未確認も含むのに、入口が候補だけで閉じていた。監査指摘 2026-10-03)"""
    import classify_sources
    return any(c.get("source_type") == "未確認" and c.get("url") for c in cands) \
        or bool(classify_sources.unresolved_post_sources())


def x_post_url(url: str, fetch=None) -> str:
    """`x.com/i/status/<ID>`(投稿者の無い投稿 URL)を `x.com/<投稿者>/status/<ID>` に直す。

    種別は X のアカウント単位で決まるので、投稿者の無い形は判定できず、紙面に未確認の出典として残る
    (2026-10-03: Grok の trend 面が 16件すべてこの形で書いた)。投稿者は X の oEmbed が返す。
    引けなければ元のまま返す(合議が投稿者を引き直し、決まらなければ watch が未確認の出典として知らせる)
    """
    m = X_ANON_POST.fullmatch(url)
    if not m:
        return url
    handle = x_post_author(m.group(1), fetch or http_get)
    return f"https://x.com/{handle}/status/{m.group(1)}" if handle else url


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


ITEM_STR_KEYS = ("title", "brand", "kind", "url", "event_date", "published_date", "deadline", "dedup_key", "engagement", "quote", "_via")


def shape_item(it) -> dict | None:
    """候補1件の形を揃える(文字列の欄は文字列に、facts・mentioned_idols は文字列の配列に)。使えない(dict でない・
    url が不正)なら None。**正規化と、投稿・ページの突き合わせが同じ判定を使う**: 突き合わせで「候補を書いた」と
    数えたのに正規化で捨てる、という食い違いを作らない(監査指摘 r3: 日付が数値の候補が例外で黙って消え、元の投稿は処理済みになった)。"""
    if not isinstance(it, dict) or not clean_url(it.get("url") if isinstance(it.get("url"), str) else ""):
        return None
    s = {k: it[k] for k in ITEM_STR_KEYS if isinstance(it.get(k), str)}     # 文字列でない欄は無かったことにする(既定値が効く)
    s["facts"] = [f for f in (it.get("facts") if isinstance(it.get("facts"), list) else []) if isinstance(f, str) and f.strip()]
    s["mentioned_idols"] = [x for x in (it.get("mentioned_idols") if isinstance(it.get("mentioned_idols"), list) else [])
                            if isinstance(x, str) and x.strip()]
    return s


# モデルの出力の契約(prompts/collect-item.md ほか)の形。**処理済みにしてよいかの判定は、この schema で検める**
# (手で欄を1つずつ足すと、日付の形・真偽値の番号・重複など崩れ方が尽きなかった。2026-10-06 の監査)。
# 文字列の欄の null は「空」で何も失わないので許す。日付は YYYY-MM-DD か空
_S = {"type": ["string", "null"]}
_DATE = {"anyOf": [{"type": "null"}, {"type": "string", "pattern": r"^(\d{4}-\d{2}-\d{2})?$"}]}
ITEM_SCHEMA = {"type": "object", "required": ["url", "facts"],
               "properties": {"url": {"type": "string", "minLength": 1}, "title": _S, "brand": _S, "kind": _S,
                              "event_date": _DATE, "published_date": _DATE, "deadline": _DATE,
                              # 空白だけの事実は正規化で消える(事実なしで処理済みにしない。監査指摘)
                              "facts": {"type": "array", "items": {"type": "string", "pattern": r"\S"}, "minItems": 1},
                              "quote": _S, "dedup_key": _S, "engagement": _S,
                              "mentioned_idols": {"type": "array", "items": {"type": "string"}}}}
WATCH_PAGE_SCHEMA = {"type": "object", "required": ["page", "status", "items"],
                     "properties": {"page": {"type": "integer"}, "status": {"enum": ["extracted", "none", "unreadable"]},
                                    "items": {"type": "array"}}}
POST_RESULT_SCHEMA = {"type": "object", "required": ["url", "status", "item"],
                      "properties": {"url": {"type": "string", "minLength": 1},
                                     "status": {"enum": ["extracted", "none", "unreadable"]}, "item": {"type": "string"}}}
DEEP_ASK_SCHEMA = {"type": "object", "required": ["question"],
                   "properties": {"question": {"type": "string", "minLength": 1}, "why": _S}}


def item_intact(it) -> bool:
    """候補1件が依頼した形のまま(正規化で何も落とさない)か。ITEM_SCHEMA で検め、url は正規化できること。
    shape_item は崩れた欄・要素を落として使える形に揃えるが、**落としたことは「処理済み」の根拠にしない**
    (facts の要素が1つ崩れていても残りで「抽出した」となる・締切の形が崩れて正規化で消える、のまま既読・処理済みに
    なる。監査指摘)。"""
    return schema_ok(it, ITEM_SCHEMA) and shape_item(it) is not None


def candidate_usable(it) -> bool:
    """「候補を書いた」と数えてよいか: 依頼した形のまま(item_intact。事実 facts が1件以上ある)。
    url だけの候補を指して「抽出した」とされたページ・投稿を処理済みにしない(事実を抽出していないのに既読になる。監査指摘)。
    形の検査であって、事実の中身は見ない。"""
    return item_intact(it)


def normalize(items: list[dict]) -> list[dict]:
    out, seen_url = [], {}
    ts = now_jst().isoformat(timespec="seconds")
    idols = load_idol_brands()
    for i, raw in enumerate(items):
        it = shape_item(raw)
        if it is None:
            if isinstance(raw, dict) and raw.get("url"):
                print(f"候補の URL を捨てた(形が不正): {str(raw.get('url'))[:80]!r}", flush=True)
            continue
        try:
            # URL は pipelib.clean_url(唯一の入口)で正規化する。探索の出力は末尾に改行やゴミ(`\n-`)を
            # 付けてくる(実測 2026-09-15: 50件)。使えない形は捨てて理由を残す(黙って切り詰めない)
            url = clean_url(it.get("url"))
            if not url:
                if it.get("url"):
                    print(f"候補の URL を捨てた(形が不正): {str(it.get('url'))[:80]!r}", flush=True)
                continue
            url = x_post_url(url)
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
                # 種別は**URL から判定する**。収集役の自己申告(kind)は採らない。
                # 申告のままにしていたため、攻略サイトを「公式」と申告した候補が
                # そのまま紙面のバッジになっていた(実測 26件・18記事)
                "source_type": classify_source(url),
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
            # 出典ページからの写し(verify が本文と照らして URL の取り違えを捕まえる)
            if isinstance(it.get("quote"), str) and it["quote"].strip():
                c["quotes"] = [it["quote"].strip()[:200]]
            if url in seen_url and not c.get("quotes") and not seen_url[url].get("quotes"):  # URL 重複は facts をマージ
                tgt = seen_url[url]
                tgt["facts"] = list(dict.fromkeys(tgt["facts"] + c["facts"]))
                continue
            if url in seen_url:
                # 写しのある候補は、裏取り(写しの照合)の**前に**混ぜない。取り違えた候補の事実と写しを混ぜると、
                # このページの正しい候補まで failed になる(監査指摘)。別の候補として裏取りし、日別ファイルへ入れるときに結合する
                out.append(c)
                continue
            seen_url[url] = c
            out.append(c)
        except Exception as e:
            # 形は shape_item で揃えてあるので、ここに来るのはコードの欠陥。黙って捨てない
            print(f"候補を正規化できずに捨てた({type(e).__name__}: {e}): {it.get('url')[:80]!r}", flush=True)
            anomaly("collect", f"候補を正規化できずに捨てた({type(e).__name__}: {e}): {it.get('url')}")
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
    """候補の裏取り。鮮度切れは種別を問わず failed。

    以前 `confirmed` は「URL が生きている」だけを意味していた。名前が実態より
    強く、lint の側も「捏造の検査は collect の verify が担う」と書いて手を抜いていた。
    URL の実在は確かに見ているが、**書かれている事実が出典にあるか**は見ていなかった。

    そこで、取得した本文に facts の日付・金額が出てくるかまで見る。
    出てこない粒は `unbacked_facts` に残し、`confirmed` は名乗らせない。
    ただし**発行は止めない**。実測では未一致の多くが「ページ自身の掲載日」や
    画像の中の価格で、捏造の証拠にはならないため(`unbacked_facts` の説明を参照)。

    X は Grok の観測をもって verify とする(ログイン必須で機械的に読めないため)。
    """
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
                    # 描画の失敗は素の本文で続けるが、原因は判定の記録に残す。捨てると、描画の時間切れで写しが見えなかったのを
                    # 「URL の取り違え」と記録し、なぜ落ちたかを当番が再現して確かめることになる(監査指摘
                    # render-diagnostic-lost-in-classification と同じ型)
                    render_errs: list[str] = []
                    # CSR で本文が空同然か、描画必須のサイト(定点観測の portal)なら描画してから読み直す(定点観測と同じ経路)
                    if needs_render(c["url"], text):
                        rendered, why = rendered_or_note(c["url"])
                        render_errs += [why] if why else []
                        if rendered:
                            text = html_to_text(rendered.encode("utf-8", "replace"))
                    periods = extract_periods(text)
                    if periods:
                        c["periods"] = periods
                    # facts の日付・金額が本文にあるか。取ってあるのに捨てていた本文を使う。
                    #
                    # **食い違ったときも描画して確かめ直す。**素の HTML では
                    # 本文が JS で描かれるサイトがあり、ナビゲーションだけが取れて
                    # 「価格が本文に無い」と誤って判定していた(実測: 公式ポータルの
                    # 配信チケット記事で 14,000円 を取りこぼした)。
                    # portal 以外のサイトの描画漏れに備え、粒が欠けたときにも描画する
                    unbacked = unbacked_facts(c.get("facts") or [], text)
                    if unbacked:
                        rendered, why = rendered_or_note(c["url"])
                        render_errs += [why] if why else []
                        if rendered:
                            unbacked = unbacked_facts(c.get("facts") or [],
                                                      html_to_text(rendered.encode("utf-8", "replace")))
                    if unbacked:
                        c["unbacked_facts"] = unbacked[:12]
                    # **写しがそのページに無ければ、URL の取り違え。**事実は別のページのもので、この候補には出典が無い。
                    # 使わせない(failed)。日付・金額の粒は近い記事どうしで重なるので、粒の照合だけでは通ってしまう
                    # (2026-09-28〜10-01 の noctchill / Master ShowPiece)。描画しないと本文が出ないページがあるので、
                    # 無かったときだけ描画して確かめ直す
                    quotes = c.get("quotes") or []
                    missing = [q for q in quotes if quote_on_page(q, text) is False]
                    matched = any(quote_on_page(q, text) for q in quotes)
                    if missing:
                        rendered, why = rendered_or_note(c["url"])
                        render_errs += [why] if why else []
                        if rendered:
                            rtext = html_to_text(rendered.encode("utf-8", "replace"))
                            missing = [q for q in missing if quote_on_page(q, rtext) is False]
                            matched = matched or any(quote_on_page(q, rtext) for q in quotes)
                    if missing:
                        c["verify"] = "failed"
                        c["verify_note"] = (f"写しが出典の本文に無い(URL の取り違えの疑い): {missing[0][:60]}"
                                            + (f"。ただし描画できず素の本文で照合した({render_errs[-1][:200]})" if render_errs else ""))
                        counts["failed"] += 1
                        print(f"  裏取り: {c['url']} に写しが無い → 使わない({c.get('title')})", flush=True)
                        continue
                    # URL をモデルが選んだ候補(探索・Grok の確かめ)で、照合できる写しが無ければ、URL の取り違えを確かめられて
                    # いない。confirmed を名乗らせない(未確認として記録する。採否は選定・校閲のモデル。監査指摘)。
                    # 定点観測の候補は URL を巡回先からコードが決めるので要らない
                    if c.get("via") != "watch" and not matched:
                        c["verify_note"] = "出典ページからの写しが無い・短すぎて、URL の取り違えを確かめられない"
                    if render_errs:
                        c["render_error"] = render_errs[-1][:300]
                good_type = c["source_type"] in GOOD_SOURCE_TYPES
                c["verify"] = ("confirmed" if ok and good_type and not c.get("unbacked_facts") and not c.get("verify_note")
                               else ("unconfirmed" if ok else "failed"))
        except Exception as e:
            # 取れなかった原因を候補に残す(採否・当番の診断で「なぜ failed か」を追えるように)
            c["verify"] = "failed"
            c["verify_note"] = f"出典を取得できない: {type(e).__name__}: {e}"[:300]
        counts[c["verify"]] += 1
    return counts


def merge_into_day_file(cands: list[dict], day: str) -> int:
    """candidates は号(edition)日付でキーする。1つの号の素材=1ファイルで、収集サイクル
    (07:30〜翌03:30)が暦日をまたいでも分割されない。パイプラインが前日以前の
    ファイルを読む必要は無い(未来日程は stock/scheduled、既報判定は stock/stories.yml)。
    day は対象号(main で決めた date)を使う。壁時計から取り直すと、06:00 をまたいだ
    当番の拾い直しで checkout した号と別日のファイルへ書く(監査指摘 rerun-collect-ignores-target-edition)。"""
    p = ROOT / "candidates" / f"{day}.json"
    existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    by_url = {c["url"]: c for c in existing}
    added = 0
    # 写しの照合で落ちた候補(URL の取り違え)は、事実がこのページのものではない。このページの候補へ混ぜない
    # (混ぜると「弱いほうを採る」でこのページの正しい候補まで failed になる。監査指摘)。正しい候補を先に入れ、
    # 落ちた候補は同じ URL の候補が無いときだけ記録として残す。既にあるのが落ちた候補なら、正しい候補で置き換える
    misplaced = lambda x: bool(x.get("quotes")) and str(x.get("verify_note") or "").startswith("写しが出典の本文に無い")
    for c in sorted(cands, key=misplaced):
        if c["url"] in by_url:
            tgt = by_url[c["url"]]
            if misplaced(c):
                continue
            if misplaced(tgt):
                existing[existing.index(tgt)] = c
                by_url[c["url"]] = c
                continue
            merged = list(dict.fromkeys(tgt.get("facts", []) + c["facts"]))
            if len(merged) > len(tgt.get("facts", [])):
                tgt["facts"] = merged
                # **裏取りの結果も引き継ぐ。**facts だけ足して verify を据え置くと、
                # 後から来た「出典に無い事実」が confirmed の候補に紛れ込む(監査指摘)。
                # 同じ URL を何度も拾うのは通常経路なので、実際に起きる
                ub = list(dict.fromkeys((tgt.get("unbacked_facts") or [])
                                        + (c.get("unbacked_facts") or [])))
                if ub:
                    tgt["unbacked_facts"] = ub[:12]
                # **弱いほうを採る。**未一致の粒だけを見ていると、
                # 新しく拾ったときに URL が死んでいて failed でも、
                # 粒が欠けていなければ既存の confirmed が残る(監査指摘)
                order = ["failed", "unconfirmed", "confirmed"]
                cur = "unconfirmed" if ub else tgt.get("verify", "unconfirmed")
                new = c.get("verify", "unconfirmed")
                tgt["verify"] = min([cur, new], key=lambda v: order.index(v) if v in order else 1)
        else:
            existing.append(c)
            by_url[c["url"]] = c
            added += 1
    p.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added


def hand_to_oncall() -> None:
    """人に「異常」として通知したものは、申告で終えずに当番がなぜなぜする。直したら定点観測を取り直し、落とした新着を
    この号の選定リストに入れるまでが当番の仕事(編集長 2026-10-07「やらかしてドロップしたのは責任を持って修正して紙面に乗せろ」。
    以前は「収集は終わっているので再実行しない」で、直しても次の定時収集まで拾われず、最後の収集なら1号遅れた)。
    当番が取り直しまで終える時刻は、ここ(呼び出し時)で固定して渡す(起動し直しても延びない)。
    当番を呼べたら、**工程の排他を持ったまま**動いている印を置く(収集が終わって排他が空いた瞬間に組版が取ると、
    当番は取り直せない。組版は印を見て待ち、受け渡しの間は譲る。監査指摘)"""
    end_at = collect_oncall_end(now_jst())
    date = edition_date()
    if diagnose_anomalies("collect", date, rerun=True, extra_args=["--end-at", str(int(end_at))]):
        mark_collect_oncall(end_at, date)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-git", action="store_true", help="ブランチ操作・push をしない(テスト用)")
    ap.add_argument("--skip-watch", action="store_true")
    ap.add_argument("--skip-explore", "--skip-claude", dest="skip_explore",
                    action="store_true", help="Web 探索(Luna)を回さない")
    ap.add_argument("--skip-grok", action="store_true")
    ap.add_argument("--force-grok", action="store_true",
                    help="GROK_HOURS の時刻判定を無視して Grok を回す(手動の再収集用)")
    ap.add_argument("--date", metavar="YYYY-MM-DD", default=None,
                    help="対象号の発行日(既定は壁時計から算出)。当番の拾い直しは 06:00 の号境界や"
                         "発行後 watch で異常発生日と取り込み先の号がずれるため、取り込み先の号を明示する")
    ap.add_argument("--oncall-rerun", action="store_true",
                    help="当番が繰り越し(_pending)を拾い直す再実行。読めないバッチを諦めず未処理のまま残し、残れば非0で終える")
    args = ap.parse_args()
    # 試験実行(--no-git)では Discord へ通知しない。本物の警報と見分けが付かなくなる
    set_quiet(args.no_git)
    # 同じ作業ツリーを compose/release/当番と同時に触らない(監査指摘)。当番の再実行が長引くと
    # ここで待つ。空かなければこの回は諦める(次の timer で拾う)
    try:
        _lock = job_lock("collect", wait_min=30)
    except JobLockTimeout as e:
        notify("collect", str(e), ok=False)
        return 1
    t0 = time.time()
    date = args.date or edition_date()
    branch = f"edition/{date}"

    if not args.no_git and not checkout_edition_branch(date, "collect"):
        return 1

    # Grok は SuperGrok の週次上限を消費するため、収集のたびに回すと枠を使い切る
    # (実測: 1回の収集で10セッション。日5回だと週350セッションになり上限の数倍)。
    # 定点観測と Claude 探索は安価なので毎回回し、Grok の実行時刻だけを GROK_HOURS で絞る。
    skip_grok = args.skip_grok or (not args.force_grok and not grok_scheduled_now())
    if skip_grok and not args.skip_grok:
        print(f"Grok は今回スキップ(GROK_HOURS={GROK_HOURS or '毎回'} の対象時刻ではない)", flush=True)

    watch_cands, watch_info = ([], {"skipped": True}) if args.skip_watch else run_watch(claude_exec, args.oncall_rerun)
    explore_items, per_query = run_explores(args.skip_explore, skip_grok)
    cands = normalize(watch_cands + explore_items)
    vcounts = verify(cands)
    added = merge_into_day_file(cands, date)
    # candidates が保存できてから、定点観測の既読を確定する(同じ成功境界。監査指摘)
    watch_state = (watch_info.get("stats") or {}).pop("_state", None) if isinstance(watch_info, dict) else None
    if watch_state is not None:
        STATE_PATH.write_text(json.dumps(watch_state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    dur = int(time.time() - t0)
    append_metric("collect", {"edition": date, "watch": watch_info, "per_query": per_query,
                              "normalized": len(cands), "added": added, "verify": vcounts,
                              "duration_s": dur})
    summary = f"{date}号向け: 新規{added}件(正規化{len(cands)}・{vcounts}) {dur}秒"
    print(summary, flush=True)
    # 判定表に無い出典は「未確認」になる。放っておくと未確認の記事が増えるだけなので、
    # **何を足せばよいか**をここに出す。表は人が育てるものであり、
    # 既定を強い種別にして誤魔化さない(監査指摘)
    unknown = collections.Counter(
        urllib.parse.urlparse(c["url"]).netloc.lower().removeprefix("www.")
        for c in cands if c.get("source_type") == "未確認" and c.get("url"))
    if unknown:
        print(f"source_types.yml に無い出典 {sum(unknown.values())}件 / {len(unknown)}ドメイン: "
              + ", ".join(f"{h}({n})" for h, n in unknown.most_common(12)), flush=True)
    if needs_classify(cands) and args.oncall_rerun:
        # 当番の取り直しでは判定表の取引(合議。単独で最大30分)をしない。取り直しは次の定時工程の前に終えて、
        # 素材を commit するのが先(監査指摘)。未知の出典の判定は次の定時収集で行う
        print("当番の取り直しのため、未知の出典の判定は次の定時収集で行う", flush=True)
    elif needs_classify(cands):
        # **未知のドメインは合議で振り分ける。**表を人が育てるまで待つと、
        # 会場・チケット販売・自治体が未確認のまま紙面に載る。
        # 別ベンダーの2モデルが一致したものだけを足し、公式・準公式は自動で足さない
        # (過大表示はこの製品がいちばん避けたい事故なので、機械には名乗らせない)
        if not args.no_git:
            # 判定表の更新 → 紙面の種別付け直し → lint を**1つの取引**にする。どれかが失敗したら、
            # この取引が触った判定表と記事を開始時に戻し、commit しない(表だけ進んで次の lint が赤くなる、
            # 部分的な付け直しが混ざる、を防ぐ。監査指摘)
            ok, why = classify_retag_lint(date)
            if not ok:
                if "戻せない" in why:
                    notify("collect", f"{date}: 判定表の取引に失敗({why[:400]})。commit せずに終える。作業ツリーを確かめること", ok=False)
                    return 1
                notify("collect", f"{date}: 判定表の更新〜紙面の付け直し〜lint で失敗({why[:400]})。判定表と記事を"
                                  f"戻した(候補は残す)。次回の収集で再試行する", ok=False)
    if not args.no_git:
        if not commit_and_push(branch, f"collect {now_jst().strftime('%H:%M')}: +{added}件", "collect"):
            # 永続化できなかった収集は「無かった」のと同じ。成功として終わらない(監査指摘: fail-open)
            notify("collect", f"{date}: 候補を commit/push できなかった。作業ツリーを確かめること", ok=False)
            return 1
    # 新規0件の警報は「定時実行が空振りした」ことを知らせるためのもの。
    # 収集系統を手で止めた実行(--skip-*)では0件が当たり前なので鳴らさない
    # (鳴らすと本物の空振りと区別がつかず、警報として役に立たなくなる)
    skipped = [n for n, on in (("watch", args.skip_watch), ("explore", args.skip_explore),
                               ("grok", args.skip_grok)) if on]
    if added == 0 and not skipped:
        # 0件そのものは異常ではない(全系統が正常に動き、拾ったものが既知だった回もある。当番の指摘 3e0904d4fe:
        # 2026-10-05 18:33 は全9系統が取得に成功し、探索の結果も既存の候補だけだったのに異常として当番まで起動した)。
        # 取得に失敗した系統があるときだけ、その内訳を添えて異常にする
        failed = collect_health(watch_info, per_query)
        if failed:
            notify("collect", f"{summary} — 新規0件。取得に失敗した系統がある:\n- " + "\n- ".join(failed), ok=False)
        else:
            print(f"新規0件(全系統の取得は正常。拾ったもの {len(cands)}件はすべて既知だった)", flush=True)
    elif added == 0:
        print(f"新規0件({'/'.join(skipped)} を手動でスキップ中のため通知しない)", flush=True)
    # 当番の拾い直しで、読めないバッチが残った(=諦めずに繰り越した)場合は、直しが効いていない。
    # 読めた分と繰り越しは保存済み(既読にしていない)。成功として終えず、当番へ非0で返す(監査指摘)
    if args.oncall_rerun and (watch_info.get("deferred", 0) if isinstance(watch_info, dict) else 0) > 0:
        notify("collect", f"{date}: 当番の拾い直しでも facts 化できないバッチが {watch_info['deferred']}件 残った。"
                          f"繰り越しは既読にせず保持した。直しが効いていない(原因が未確定の可能性)", ok=False)
        return 1
    # 当番の取り直しで、観測先の一覧がまた取れなかったら成功にしない(一覧から新着を見つけられないので deferred は0のまま。
    # 落とした新着がその号の素材に入っていない。監査指摘)
    list_failed = sorted(sid for sid, st in ((watch_info or {}).get("stats") or {}).items()
                         if isinstance(st, dict) and st.get("error")) if isinstance(watch_info, dict) else []
    if args.oncall_rerun and list_failed:
        notify("collect", f"{date}: 当番の取り直しでも観測先の一覧が取れない: " + ", ".join(list_failed)
               + "。そこで落とした新着は、この号の素材に入っていない", ok=False)
        return 1
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        notify_crash("collect", e)
        code = 1
    hand_to_oncall()
    sys.exit(code)
