#!/usr/bin/env python3
"""出典が足りずに見送った記事(slug)について、出典を取りに行き、確かめて号の素材に入れる入口。

組版で NO_PRIMARY_SOURCE・SOURCE_MISMATCH の見送りになった記事を、その号に戻すための正規の道具。
当番の組版の復帰(oncall.rerun_stage)が、落とした記事を組み直す(--reuse-plan --carry-review)前にこれを呼ぶ。
2026-10-26 号: 当番は取得の欠陥を直して2本を戻したが、残る3本(当日 23:59 締切のシャニマス8.5周年プレゼントを含む)を
「外部」「後続号に戻るかもしれない」で終えた。公式 X・当事者のページは実在し、手での救済(Grok の深掘り → Luna の確かめ →
verify → 素材への merge → 計画の素材への追加)で3本とも当日号に戻った。その手順を道具にしたもの。

手順(どれも収集の既存の関数。素材の事実はこの道具が作らない):
1. 当たりの調べ(記事ごと・並列): モデル(定点観測の facts 化と同じ道具立て)が、記事の素材と見送りの理由から、
   公式・当事者・報道のページの URL と、X の原本で確かめる問いを挙げる(prompts/source-rescue.md)
2. 並列に:
   - X: 問いを面ごとにまとめ、週の予算(deep_budget)の範囲で Grok に深掘りさせ(deep_dive_grok)、Luna が確かめる
     (verify_grok_faces)。答えの無い問いは予算の範囲で1回だけ角度を変えて投げ直す(収集の深掘りと同じ)
   - ページ: 記事ごとに facts 化する(facts_batch)
3. 正規化(normalize)→ 裏取り(verify)→ 号の素材へ入れる(merge_into_day_file)
4. 使える候補(verify が failed でないもの)を、その記事の計画(metrics/plan-<号>.json)の candidate_ids に足す

冪等: 素材は URL で重ねずに合わせ(merge_into_day_file)、計画の candidate_ids は重ねない。何度走らせても素材・計画は増えない。
結果は metrics/work/<号>/source-rescue/report.json と標準出力に残す。出典に届かなかった記事は、試した出典(URL・応答・
問いと見つかった投稿)を書く(当番の報告と組み直しの通知がそれを載せる)。

使い方: source_rescue.py --date YYYY-MM-DD --slug SLUG [--slug SLUG ...] [--until EPOCH] [--dry-run]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect  # noqa: E402
from pipelib import (CLI_BLOCKED_EXIT, CliBlocked, exit_on_term, now_jst, render_prompt, schema_ok,  # noqa: E402
                     source_lacking_drops, write_checked)

MAX_PAGES = 4               # 記事1本で読むページの上限(当たりの調べの答え)
MAX_ASKS = 2                # 記事1本で X の原本を探す問いの上限
PLAN_TIMEOUT = 600          # 当たりの調べのセッションの上限(秒)
VERIFY_RESERVE_SEC = 360    # 締切のうち、正規化・裏取り・素材への書き込みに残す秒数(裏取り1件の待ち VERIFY_ONE_MAX が入る)
VERIFY_ONE_MAX = 300        # 候補1件の裏取りに掛かりうる待ち(秒。取得 15 秒 + 描画 90 秒 × 3 回 + 余裕)。これが残っていなければ始めない
VERIFY_PARALLEL = 6
VERIFY_LATE_NOTE = "出典を揃える入口の締切までに裏取りできなかった(素材に入れない)"
TASK_SCHEMA = {"type": "object", "required": ["kind", "why"],
               "properties": {"kind": {"enum": ["page", "x"]}, "why": {"type": "string"},
                              "url": {"type": "string", "pattern": r"^https?://\S+$"},
                              "question": {"type": "string", "minLength": 1}}}
GOOD_SOURCE_TYPES = collect.GOOD_SOURCE_TYPES


def root() -> Path:
    return collect.ROOT


def rescue_dir(date: str) -> Path:
    return root() / "metrics" / "work" / date / "source-rescue"


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except (OSError, ValueError):
        return default


def materials(date: str) -> dict[str, dict]:
    """号の素材(候補ファイルと当日の続報予約)の id → 素材。"""
    out = {c["id"]: c for c in read_json(root() / "candidates" / f"{date}.json", []) if isinstance(c, dict) and c.get("id")}
    out.update({s["id"]: s for s in read_json(root() / "stock" / "scheduled" / f"{date}.json", [])
                if isinstance(s, dict) and s.get("id")})
    return out


def material_text(ids: list[str], mats: dict[str, dict]) -> str:
    """当たりの調べに渡す素材(人が読める形。1行が長くならない)。"""
    rows = []
    for i, cid in enumerate(ids):
        c = mats.get(cid)
        if not c:
            rows.append(f"### {i + 1}. {cid}\n- (号の素材に無い)")
            continue
        facts = "\n".join(f"  - {f}" for f in (c.get("facts") or [])[:15])
        rows.append(f"### {i + 1}. {c.get('title') or '(無題)'}\n- url: {c.get('url')}\n- 種別: {c.get('source_type')}"
                    f" / 裏取り: {c.get('verify')}{' (' + str(c['verify_note'])[:200] + ')' if c.get('verify_note') else ''}"
                    + (f"\n- リンク元: {c['link_from']}" if c.get("link_from") else "")
                    + (f"\n- 締切: {c['deadline']}" if c.get("deadline") else "")
                    + (f"\n- 開催日: {c['event_date']}" if c.get("event_date") else "")
                    + (f"\n- facts:\n{facts}" if facts else ""))
    return "\n\n".join(rows) or "(素材なし)"


def plan_tasks(date: str, art: dict, why: str, mats: dict[str, dict]) -> tuple[list[dict] | None, str]:
    """記事1本の出典の当たり(page / x)。戻りは (当たり, 失敗の理由)。答えが読めない・形が崩れていれば (None, 理由)。
    形の崩れた当たりが1つでもあれば答え全体を採らない(渡した形で検める。部分だけ採って黙って当たりを失わない)。"""
    prompt = render_prompt("source-rescue", DATE=date, TODAY=now_jst().strftime("%Y-%m-%d"), SLUG=art["slug"],
                           BRAND=art.get("brand") or "", ANGLE=art.get("angle") or "", WHY=why or "(記録なし)",
                           MAX_PAGES=MAX_PAGES, MAX_ASKS=MAX_ASKS, MATERIAL=material_text(art.get("candidate_ids") or [], mats))
    try:
        got = collect.claude_exec(prompt, timeout=PLAN_TIMEOUT, what=f"出典を揃える入口の当たりの調べ {art['slug']}",
                                  kind="source-rescue")
    except CliBlocked as e:
        return None, f"モデル CLI の停止({e})"
    if got is collect.DEADLINE_CUT:
        return None, "締切で起動しなかった・打ち切った"
    if got is None:
        return None, "答えが読めない(時間切れ・異常終了・JSON でない)"
    if not all(schema_ok(t, TASK_SCHEMA) and (t.get("url") if t["kind"] == "page" else t.get("question")) for t in got):
        return None, "答えが渡した形のとおりでない"
    pages = [t for t in got if t["kind"] == "page"][:MAX_PAGES]
    asks = [t for t in got if t["kind"] == "x"][:MAX_ASKS]
    return pages + asks, ""


def face_of(brand: str, queries: list[dict]) -> dict | None:
    """記事の面(brand)の Grok の面(queries.yml)。合同は joint-other、総合はトレンドの面。"""
    by_brand = {}
    for q in queries:
        by_brand.setdefault(q["brand"], q)
    return by_brand.get(brand) or by_brand.get("general")


def answered_posts(text: str, asks: list[dict]) -> list[tuple[str, str, int]]:
    """深掘りの書き出しの投稿ごとの (投稿の鍵, url, 答えた問いの番号 1始まり)。番号の無い投稿は、問いが1つならその問い。
    範囲外の番号・番号の無い投稿(問いが複数)は入れない。同じ投稿が別の問いの答えとして2回出れば2行(どちらの記事にも渡す)。"""
    out = []
    for b in collect.post_blocks(text):
        u = collect.post_url(b)
        if not u:
            continue
        m = collect.DEEP_ASK_NO.search(b)
        n = int(m.group(1)) if m else (1 if len(asks) == 1 else 0)
        if 0 < n <= len(asks):
            out.append((collect.post_key(u), u, n))
    return out


def grok_phase(asks_by_slug: dict[str, list[dict]], arts: dict[str, dict], outdir: Path, log: dict) -> list[tuple[str, dict]]:
    """X の原本の深掘り。戻りは [(slug, Luna が確かめた候補)]。問いごとの結果(見つかった投稿)は log に書く。"""
    queries = collect.build_prompts()
    deep: dict[str, list[dict]] = {}
    # 問い → slug は、問いの**オブジェクト**で引く(id)。問いの文が同じ2記事を、値の等しさ(.index)で先の記事に寄せない(Sol 監査 r4)。
    # 選定(plan_deep_dive)・投げ直し(unanswered_asks)は同じオブジェクトを渡すので、対応は途中で失われない
    owner: dict[int, str] = {}
    for slug, asks in asks_by_slug.items():
        q = face_of(arts[slug].get("brand") or "", queries)
        if not q:
            continue
        for a in asks:
            ask = {"question": a["question"], "why": a.get("why") or ""}
            owner[id(ask)] = slug
            deep.setdefault(q["key"], []).append(ask)
    if not deep:
        return []
    used = collect.grok_week_usage()
    budget = collect.deep_budget(used)
    log["grok"] = {"budget": budget, "used_7d": used}
    out: list[tuple[str, dict]] = []
    ledger = collect.lost_posts_ledger()
    errs: dict[str, str] = {}
    by_key = {q["key"]: q for q in queries}

    def one_round(chosen: dict[str, list[dict]], suffix: str, retry: bool) -> None:
        failed = collect.deep_dive_grok(queries, outdir, chosen, errs, suffix=suffix, retry=retry)
        items, _ = collect.verify_grok_faces([by_key[k] for k in chosen], outdir, suffix=suffix, ledger=ledger)
        posts: dict[str, set[str]] = {}       # 投稿 → 答えた問いの記事(同じ投稿が複数の記事の問いに答えることがある)
        for k, asks in chosen.items():
            text = collect.read_written(outdir / f"{k}{suffix}.md")
            for a in asks:
                st = log.setdefault("asks", {}).setdefault(owner[id(a)], {}).setdefault(a["question"], [])
                if k in failed:
                    st.append(f"(深掘りが正常に終わらなかった: {errs.get(k, '')[:160]})")
            for pk, url, n in answered_posts(text, asks):
                slug = owner[id(asks[n - 1])]
                posts.setdefault(pk, set()).add(slug)
                log["asks"][slug][asks[n - 1]["question"]].append(url)
        for it in items:
            it["_via"] = "grok"
            slugs = posts.get(collect.post_key(it["url"])) or posts.get(collect.post_key(it.get("link_from") or ""))
            if not slugs:
                # 投稿に結び付かない候補(リンク先)は、その面の問いが1記事だけならその記事の素材
                every = {owner[id(a)] for k in chosen for a in deep[k]}
                slugs = every if len(every) == 1 else set()
            for slug in sorted(slugs):
                out.append((slug, dict(it)))
            if not slugs:
                log.setdefault("unattributed", []).append(it.get("url"))

    chosen = collect.plan_deep_dive(deep, budget)
    if chosen:
        started = time.time()
        one_round(chosen, "-deep", False)
        retry = {k: v for k, asks in chosen.items()
                 for v in [collect.unanswered_asks(collect.read_written(outdir / f"{k}-deep.md"), asks)] if v}
        spent = sum(collect.grok_search_counts(started).values())
        budget2 = max(0, min(budget - max(2 * sum(len(v) for v in chosen.values()), spent),
                             collect.deep_budget(collect.grok_week_usage())))
        chosen2 = collect.plan_deep_dive(retry, budget2)
        if chosen2:
            one_round(chosen2, "-deep2", True)
    picked = {id(a) for v in chosen.values() for a in v}
    skipped = {owner[id(a)] for asks in deep.values() for a in asks if id(a) not in picked}
    for slug in skipped:
        log.setdefault("asks", {}).setdefault(slug, {})["(予算)"] = [f"週の予算の外で調べなかった(予算 {budget}回・直近7日 {used}回)"]
    collect.report_lost_posts(ledger)
    return out


def page_phase(slug: str, pages: list[dict], brand: str, log: dict) -> list[tuple[str, dict]]:
    """記事1本の公式・当事者・報道のページを facts 化する。ページごとの応答は log に書く。"""
    batch = [{"url": p["url"], "title": "", "brand": brand, "csr": False} for p in pages]
    blocked: set[str] = set()
    cands, tried = collect.facts_batch(batch, collect.claude_exec, {}, oncall_rerun=True, blocked=blocked, unavailable=set())
    if blocked:
        # facts 化のモデル CLI が認証切れ・利用上限で答えなかった(facts_batch が受けて、読めなかった回に数えない)
        log.setdefault("pages", {})[slug] = [{"url": p["url"], "why": p.get("why"), "result": "モデル CLI の停止で facts 化できなかった"}
                                             for p in pages]
        return []
    rows = []
    for p in pages:
        key = collect.page_key(p["url"])
        got = [c for c in cands if collect.page_key(collect.clean_url(c.get("url")) or "") == key or c.get("link_from") == p["url"]]
        code = collect.BODY_HTTP.get(p["url"])
        rows.append({"url": p["url"], "why": p.get("why"),
                     "result": (f"候補 {len(got)}件" if got else ("事実を起こせなかった" if p["url"] in tried else "読めなかった"))
                               + (f"(本文の取得 HTTP {code})" if code else "")})
    log.setdefault("pages", {})[slug] = rows
    for c in cands:
        c["_via"] = "explore"     # URL はモデルが選んだ(定点観測の巡回先ではない): 写しの照合を省かせない(collect.verify)
    return [(slug, c) for c in cands]


def verify_until(cands: list[dict], until: float | None) -> None:
    """候補を裏取りする(collect.verify。並列)。締切(until)があれば、締切で待つのをやめる:
    - 1件の裏取りに掛かりうる上限(VERIFY_ONE_MAX)が締切までに残っていない候補は始めない
    - 締切までに終わらなかった裏取りは待たずに捨てる(写しの裏取りは複製で行い、終わったものだけを採る。描画・本文の受信が
      締切を越えて長引いても、入口は締切で先へ進む。Sol 監査 r2: 描画の待ちの上限は1回90秒で、本文の受信にも総時間の上限が無い)
    どちらも、裏取りしていない候補として VERIFY_LATE_NOTE を付ける(写しの不一致とはしない。素材にも計画にも入れない)。
    裏取りのスレッドはデーモンにし、捨てた裏取りが工程の終わりを引き延ばさない(残った描画の子は呼び手の当番が刈り取る)。"""
    if not cands:
        return
    gate = threading.Semaphore(VERIFY_PARALLEL)
    done: dict[int, dict] = {}

    def one(i: int, c: dict) -> None:
        with gate:
            if until is not None and time.time() + VERIFY_ONE_MAX > until:
                return
            mine = json.loads(json.dumps(c, ensure_ascii=False))
            collect.verify([mine])
            done[i] = mine
    threads = [threading.Thread(target=one, args=(i, c), daemon=True) for i, c in enumerate(cands)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(None if until is None else max(0.0, until - time.time()))
    got = dict(done)     # 締切の時点で終わっていた裏取りだけ(このあと終わったものは採らない)
    for i, c in enumerate(cands):
        if i in got:
            c.clear()
            c.update(got[i])
        else:
            c["verify"], c["verify_note"] = "unconfirmed", VERIFY_LATE_NOTE


def reverify_stored(date: str, fresh: list[dict], until: float | None) -> None:
    """取り直した候補と同じ URL の既存の候補が、取得の失敗(相手の 429 等)で failed のまま号の素材にあれば、いま裏取りし直して
    結果を置き換える。素材への合わせ方(merge_into_day_file)は弱いほうの裏取りを採るので、古い取得の失敗が残り、取り直せた出典が
    使えないままになる。写しの不一致(URL の取り違え)など、取得の失敗以外の failed は置き換えない。"""
    p = root() / "candidates" / f"{date}.json"
    rows = read_json(p, [])
    urls = {c["url"] for c in fresh if c.get("verify") != "failed"}
    stale = [r for r in rows if isinstance(r, dict) and r.get("url") in urls and r.get("verify") == "failed"
             and str(r.get("verify_note") or "").startswith("出典を取得できない")]
    if not stale:
        return
    redo = [{k: v for k, v in r.items() if k not in ("verify", "verify_note", "unbacked_facts", "periods", "render_error")} for r in stale]
    verify_until(redo, until)
    changed = False
    for r, n in zip(stale, redo):
        if n.get("verify_note") != VERIFY_LATE_NOTE and n.get("verify") != "failed":
            rows[rows.index(r)] = n
            changed = True
    if changed:
        write_checked(p, json.dumps(rows, ensure_ascii=False, indent=2) + "\n")   # 書式(candidates.schema)で検めてから書く


def unique_ids(cands: list[dict], taken: set[str]) -> None:
    """正規化の id(分・経路・番号)が、号の素材の id と重ならないようにする(同じ分の収集・入口の再実行)。"""
    for c in cands:
        base, n = c["id"], 1
        while c["id"] in taken:
            c["id"] = f"{base}-r{n}"
            n += 1
        taken.add(c["id"])


def write_plan(date: str, plan: dict) -> None:
    """計画を一時ファイルに書いてから差し替える(書きかけで止まっても壊れた計画を残さない)。"""
    p = root() / "metrics" / f"plan-{date}.json"
    tmp = root() / "metrics" / "work" / f".{p.name}.{os.getpid()}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def rescue(date: str, slugs: list[str], until: float | None = None, dry_run: bool = False) -> dict:
    """出典を揃える本体。戻りは記事ごとの結果(report.json と同じ形)。"""
    collect.DEADLINE = (until - VERIFY_RESERVE_SEC) if until else None
    plan = read_json(root() / "metrics" / f"plan-{date}.json", {})
    arts = {a["slug"]: a for a in (plan.get("articles") or []) if isinstance(a, dict) and a.get("slug") in slugs}
    whys = source_lacking_drops(date, root())
    drops = read_json(root() / "metrics" / f"drops-{date}.json", {})
    mats = materials(date)
    report: dict = {"date": date, "at": now_jst().isoformat(timespec="seconds"), "dry_run": dry_run, "articles": {}}
    for s in slugs:
        if s not in arts:
            report["articles"][s] = {"outcome": "計画に無い記事", "why": "", "tried": []}
    if not arts:
        return report
    log: dict = {}
    # Grok の書き出しは実行ごとに新しい置き場へ(前の実行の書き出しが残る置き場だと、今回 Grok が書かずに終わった面で、前の実行の
    # 答えを今回の問いの答えとして読む。Sol 監査 r3)。中身は記録として残す(metrics/work は Git 管理外)
    rescue_dir(date).mkdir(parents=True, exist_ok=True)
    outdir = Path(tempfile.mkdtemp(prefix=f"grok-{now_jst():%H%M%S}-", dir=rescue_dir(date)))
    # 1. 当たりの調べ(記事ごとに並列)
    tasks: dict[str, list[dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(arts)) as ex:
        futs = {s: ex.submit(plan_tasks, date, a, whys.get(s) or str((drops.get(s) or {}).get("why") or ""), mats)
                for s, a in arts.items()}
        for s, f in futs.items():
            got, err = f.result()
            tasks[s] = got or []
            if err:
                log.setdefault("plan_error", {})[s] = err
    # 2. X の原本とページを並列に
    asks = {s: [t for t in ts if t["kind"] == "x"] for s, ts in tasks.items()}
    pages = {s: [t for t in ts if t["kind"] == "page"] for s, ts in tasks.items()}
    found: list[tuple[str, dict]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(arts) + 1) as ex:
        futs = [ex.submit(grok_phase, {s: v for s, v in asks.items() if v}, arts, outdir, log)]
        futs += [ex.submit(page_phase, s, v, arts[s].get("brand") or "other", log) for s, v in pages.items() if v]
        for f in futs:
            try:
                found += f.result()
            except Exception as e:      # noqa: BLE001  片方の失敗で、もう片方の結果を捨てない
                log.setdefault("errors", []).append(f"{type(e).__name__}: {str(e)[:300]}")
    # 3. 正規化 → 裏取り → 素材へ
    by_slug: dict[str, list[dict]] = {}
    for s, it in found:
        by_slug.setdefault(s, []).append(it)
    cands_of = {s: collect.normalize(items) for s, items in by_slug.items()}
    every = [c for cs in cands_of.values() for c in cs]
    unique_ids(every, set(mats))
    collect.DEADLINE = None      # 裏取りはセッションを起動しない。締切は verify_until が候補ごとに守る
    verify_until(every, until)
    # 締切までに裏取りできなかった候補は、素材にも計画にも入れない(確かめていない出典で書かせない)
    checked = {s: [c for c in cs if c.get("verify_note") != VERIFY_LATE_NOTE] for s, cs in cands_of.items()}
    added: dict[str, list[str]] = {}
    stored: dict[str, dict] = {}
    if not dry_run and any(checked.values()):
        fresh = [c for cs in checked.values() for c in cs]
        reverify_stored(date, fresh, until)
        collect.merge_into_day_file(fresh, date)
        stored = {c.get("url"): c for c in read_json(root() / "candidates" / f"{date}.json", []) if isinstance(c, dict)}
        for s, cs in checked.items():
            ids = [stored[c["url"]]["id"] for c in cs if c["url"] in stored and stored[c["url"]].get("verify") != "failed"]
            a = arts[s]
            new = [i for i in dict.fromkeys(ids) if i not in (a.get("candidate_ids") or [])]
            if new:
                a["candidate_ids"] = list(a.get("candidate_ids") or []) + new
                added[s] = new
        if added:
            write_plan(date, plan)
    # 4. 記事ごとの結果。素材に入れたなら、保存した候補(同じ URL の既存の候補と合わせた後)がその記事の計画にあるかで決める
    for s in arts:
        cs = cands_of.get(s, [])
        if dry_run:
            usable = [c for c in checked.get(s, [])]
        else:
            ids = set(arts[s].get("candidate_ids") or [])
            usable = [stored[c["url"]] for c in checked.get(s, []) if c["url"] in stored and stored[c["url"]].get("id") in ids]
        good = [c for c in usable if c.get("verify") != "failed" and c.get("source_type") in GOOD_SOURCE_TYPES]
        tried = [{"page": r["url"], "why": r.get("why"), "result": r["result"]} for r in (log.get("pages") or {}).get(s, [])]
        tried += [{"x": q, "posts": v} for q, v in ((log.get("asks") or {}).get(s) or {}).items()]
        report["articles"][s] = {
            "outcome": "揃えた" if good else "揃わない",
            "why": whys.get(s) or str((drops.get(s) or {}).get("why") or ""),
            "plan_error": (log.get("plan_error") or {}).get(s, ""),
            "tried": tried,
            "candidates": [{"id": c["id"], "url": c["url"], "source_type": c.get("source_type"),
                            "verify": (stored.get(c["url"]) or c).get("verify"),
                            "note": str((stored.get(c["url"]) or c).get("verify_note") or "")[:200]} for c in cs],
            "added_ids": added.get(s, []),
        }
    report["grok"] = log.get("grok")
    report["errors"] = log.get("errors", []) + [f"投稿に結び付かない候補: {u}" for u in log.get("unattributed", [])]
    return report


def report_text(report: dict, slugs: list[str] | None = None) -> str:
    """結果を人が読める形にする(当番の報告・組み直しの通知に載せる)。揃わなかった記事は、試した出典を具体的に書く。"""
    lines = []
    for s, r in report.get("articles", {}).items():
        if slugs is not None and s not in slugs:
            continue
        lines.append(f"- {s}: {r['outcome']}" + (f"(足した素材 {', '.join(r.get('added_ids') or [])})" if r.get("added_ids") else ""))
        if r.get("plan_error"):
            lines.append(f"  当たりの調べ: {r['plan_error']}")
        if r["outcome"] != "揃えた":
            for t in r.get("tried") or []:
                if "page" in t:
                    lines.append(f"  ページ {t['page']}: {t['result']}" + (f"({t['why']})" if t.get("why") else ""))
                else:
                    lines.append(f"  X「{t['x']}」: " + (", ".join(map(str, t['posts'])) or "投稿なし"))
            for c in r.get("candidates") or []:
                lines.append(f"  候補 {c['url']}: {c['source_type']} / {c['verify']}" + (f"({c['note']})" if c.get("note") else ""))
            if not r.get("tried") and not r.get("plan_error"):
                lines.append("  当たりが1つも無かった(出典の当たりの調べが [] を答えた)")
    return "\n".join(lines)


def main() -> int:
    exit_on_term()
    ap = argparse.ArgumentParser(description="出典が足りずに見送った記事の出典を揃え、号の素材に入れる")
    ap.add_argument("--date", required=True)
    ap.add_argument("--slug", action="append", default=[], help="対象の記事。省くと出典不足の見送り(drops の記録)の全部")
    ap.add_argument("--until", type=float, default=None, help="終える時刻(epoch 秒)")
    ap.add_argument("--dry-run", action="store_true", help="素材・計画を書き換えず、当たりと確かめの結果だけを出す")
    a = ap.parse_args()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.date):
        ap.error("--date は YYYY-MM-DD")
    slugs = a.slug or sorted(source_lacking_drops(a.date, root()))
    if not slugs:
        print("出典不足の見送りは無い", flush=True)
        return 0
    rep = rescue(a.date, slugs, a.until, a.dry_run)
    out = rescue_dir(a.date) / "report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    text = report_text(rep)
    out.with_suffix(".txt").write_text(text + "\n", encoding="utf-8")     # 当番の組み直しの通知が載せる
    print(text, flush=True)
    print(f"結果: {out}", flush=True)
    blocked = [s for s, r in rep["articles"].items() if "モデル CLI の停止" in str(r.get("plan_error"))]
    return CLI_BLOCKED_EXIT if blocked and len(blocked) == len(rep["articles"]) else 0


if __name__ == "__main__":
    sys.exit(main())
