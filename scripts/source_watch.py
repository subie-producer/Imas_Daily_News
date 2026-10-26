#!/usr/bin/env python3
"""source_watch: 発行済み記事の出典の見回りと、出典の変更の扱い(編集長 2026-10-26)。

「読み直しは必須」「履歴を全部追っているんだから、訂正欄ではない(訂正欄は私たちが悪いものでやる)。元のデータを出したヤツの責任として
訂正するべきだ。『変更になったので注意してください』なのか、『当初〇〇が参加するとありましたが訂正されました』なのか、『お詫びの品が
増えました』なのか。記事の中で時系列を扱え」。

1. 見回り(recheck): 読者にまだ効く発行済み記事(event_date か素材の締切が今日以降、または話題に続報予約がある)の出典(X 以外・原本の
   あるページ)を、収集のたびに「最後に確かめたのが古い順」に読み直す(fetch_page.py。変われば原本の置き場の変化の台帳に残る)。
   件数で間引かない: 対象の総数と収集の回数から、全部を RECHECK_CYCLE_DAYS 日で一巡できる件数まで1回の上限と並列を上げ、一巡の日数を
   毎回記録する(超えたら異常として申告する)
2. 変化の扱い(process_changes): 変化の台帳(page_archive.changes)のうち、発行済み記事の出典のページの変化ごとに
   - 判定と文は小さな構造化セッション: 書き手のモデル(prompts/source-update.md)が種類(変更・出典元の訂正・追加・表示の揺れ・記事に
     関係なし)と更新の文を schema で出し、校閲のモデル(prompts/source-update-review.md)が検める(block なら1回だけ直させる)
   - 事務処理はコード: 記事の frontmatter の updates に {at, kind, text, source, from_at, to_at} を末尾に足す(本文・訂正欄は触らない。
     append-only の検査は updates の追記だけを通す)。人へ通知する(記事・変更前後・出典のリンク)
   - 読者に関わる変化は、その号の「出典の更新」の素材(source_change)にもする(続報の記事の材料。選定・執筆が既報と照らして書く)
"""
import datetime
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import page_archive
from pipelib import (CODEX_WRITE_MODEL, ENV, FETCH_RECORD_ENV, REVIEW_MODEL, ROOT, anomaly, append_metric, changed_record, cli_check,
                     classify_source, now_jst, notify, partial_output, prompt_file, read_answer, record_rows, render_prompt,
                     run_group, save_raw, schema_ok, write_checked)

RECHECK_DAYS = 60                   # 見回る記事を探す範囲(発行した号の日付から)
RECHECK_MAX = int(ENV.get("RECHECK_MAX", "60"))           # 1回の見回りで読み直す件数の下限(一巡の日数を守るため自動で上げる)
RECHECK_PARALLEL = int(ENV.get("RECHECK_PARALLEL", "4"))  # 並列に読み直す本数の下限(件数に合わせて上げる)
RECHECK_PER_SLOT = 15               # 並列1本が1回の見回りで読み直す件数の目安(これを超えたら並列を上げる)
RECHECK_CYCLE_DAYS = 2              # 対象を全部一巡する日数の上限(編集長: 回をまたいで全部回すことを数で保証する)
RECHECK_BUDGET_SEC = int(ENV.get("RECHECK_BUDGET_SEC", "1200"))   # 1回の見回りの時間の上限
UPDATE_KINDS = ("変更", "出典元の訂正", "追加")            # 記事に時系列の更新として足す種類
MAX_TRIES = 3                       # 判定・校閲の答えが読めない変化をやり直す回数
SESSION_TIMEOUT_SEC = 600           # 判定・校閲のセッション1本の待ちの上限(収集の締切の残りでも切る)
CANDIDATE_DIFF_CHARS = 60000        # 出典の更新の素材に入れる違いの上限の字数(判定に渡す違いと同じ範囲を含む大きさ)
X_HOSTS = ("x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com")
FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def collect_runs_per_day(root: Path = ROOT) -> int:
    """収集の1日の回数(ops/systemd/imas-collect.timer の OnCalendar の数。読めなければ 5)。"""
    try:
        n = len(re.findall(r"(?m)^OnCalendar=", (root / "ops" / "systemd" / "imas-collect.timer").read_text(encoding="utf-8")))
    except OSError:
        n = 0
    return n or 5


def _is_x(url: str) -> bool:
    import urllib.parse
    return (urllib.parse.urlsplit(str(url or "")).hostname or "").lower().removeprefix("www.") in X_HOSTS


def _load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def published(today: str, root: Path = ROOT, days: int = RECHECK_DAYS) -> list[dict]:
    """発行した号(today までの days 日)の記事: id(ファイル名)・号・slug・見出し・面・話題キー・event_date・素材の締切・出典の url
    (sources・verified_facts・素材の url。X は除く)。"""
    d0 = datetime.date.fromisoformat(today)
    lo = (d0 - datetime.timedelta(days=days)).isoformat()
    cands: dict[str, dict] = {}
    plans: dict[str, dict] = {}
    out = []
    for p in sorted((root / "docs" / "_posts").glob("*.md")):
        ed = p.name[:10]
        if not (lo <= ed <= today):
            continue
        m = FM_RE.match(p.read_text(encoding="utf-8"))
        try:
            fm = (yaml.safe_load(m.group(1)) or {}) if m else {}
        except yaml.YAMLError:
            continue
        if ed not in cands:
            cands[ed] = {}
            for src in (root / "stock" / "scheduled" / f"{ed}.json", root / "candidates" / f"{ed}.json"):
                for c in _load_json(src) or []:
                    if isinstance(c, dict) and c.get("id"):
                        cands[ed][c["id"]] = c
            plan = _load_json(root / "metrics" / f"plan-{ed}.json") or {}
            plans[ed] = {a.get("slug"): a.get("dedup_key") for a in plan.get("articles") or [] if isinstance(a, dict)}
        mats = [cands[ed][i] for i in fm.get("candidate_ids") or [] if i in cands[ed]]
        urls = [str(s.get("url") or "") for s in fm.get("sources") or [] if isinstance(s, dict)]
        urls += [str(f.get("url") or "") for f in fm.get("verified_facts") or [] if isinstance(f, dict)]
        urls += [str(c.get("url") or "") for c in mats]
        seen, uniq = set(), []
        for u in urls:
            k = page_archive.page_key(u)
            if u.startswith("http") and not _is_x(u) and k not in seen:
                seen.add(k)
                uniq.append(u)
        slug = str(fm.get("slug") or p.stem[11:])
        keys = {k for k in [plans[ed].get(slug), *(c.get("dedup_key") for c in mats)] if k}
        out.append({"id": p.stem, "path": p, "edition": ed, "slug": slug, "title": str(fm.get("title") or ""),
                    "brand": fm.get("brand"), "dedup_key": plans[ed].get(slug) or next(iter(sorted(keys)), None), "keys": keys,
                    "event_date": str(fm.get("event_date") or ""), "deadlines": sorted({str(c["deadline"]) for c in mats if c.get("deadline")}),
                    "urls": uniq, "updates": list(fm.get("updates") or [])})
    return out


def active(arts: list[dict], today: str, root: Path = ROOT) -> list[dict]:
    """読者にまだ効く記事: event_date か素材の締切が今日以降、または話題に今日以降の続報予約がある。"""
    reserved: set[str] = set()
    for p in (root / "stock" / "scheduled").glob("*.json"):
        if p.stem >= today:
            reserved |= {str(r.get("dedup_key")) for r in _load_json(p) or [] if isinstance(r, dict) and r.get("dedup_key")}
    return [a for a in arts if a["event_date"] >= today or any(d >= today for d in a["deadlines"]) or a["keys"] & reserved]


def last_checked(url: str) -> str:
    """原本の置き場で、そのページを最後に読んだ・確かめた時刻(無ければ空)。"""
    return max([str(t or "") for v in page_archive.versions(url) for t in (v.get("seen"), v.get("at"), v.get("collected"))] or [""])


def recheck_plan(arts: list[dict], now: datetime.datetime, runs: int) -> dict:
    """見回りの計画: 対象(原本のある出典のページ。同じページは1つ)、1回の上限(一巡が RECHECK_CYCLE_DAYS 日に収まる件数まで上げる)、
    並列、最後に確かめたのが古い順に選んだ今回の分、見込みの一巡の日数。"""
    seen, targets = set(), []
    for a in arts:
        for u in a["urls"]:
            k = page_archive.page_key(u)
            if k in seen:
                continue
            seen.add(k)
            try:
                if page_archive.versions(u):
                    targets.append(u)
            except OSError:
                continue
    cap = max(RECHECK_MAX, math.ceil(len(targets) / max(1, runs * RECHECK_CYCLE_DAYS)))
    parallel = max(RECHECK_PARALLEL, math.ceil(cap / RECHECK_PER_SLOT))
    order = sorted(targets, key=lambda u: (last_checked(u), u))
    chosen = order[:cap]
    oldest = last_checked(order[0]) if order else ""
    try:
        age_h = (now - datetime.datetime.fromisoformat(oldest)).total_seconds() / 3600 if oldest else 0.0
    except ValueError:
        age_h = 0.0
    return {"targets": targets, "chosen": chosen, "cap": cap, "parallel": parallel, "runs_per_day": runs,
            "cycle_days": round(len(targets) / max(1, cap * runs), 2), "oldest_age_h": round(age_h, 1)}


def recheck(today: str, arts: list[dict], deadline_passed=lambda: False, root: Path = ROOT, fetch=None) -> dict:
    """見回る: 計画の分を fetch_page.py で読み直す(変われば原本の置き場の台帳に残る)。時間の上限・収集の締切を過ぎたら新しく始めない。
    一巡の日数を記録し(metrics の recheck)、上限を超えたら(読み残しで一巡が延びる・最後に確かめたのが一巡の日数より前)異常として申告する。
    戻りは記録した数字。fetch(url, record) を渡せば読み直しを差し替える(テスト)"""
    plan = recheck_plan(active(arts, today, root), now_jst(), collect_runs_per_day(root))
    rec = root / "metrics" / "work" / today / "recheck" / f"{time.time_ns()}-{os.getpid()}.jsonl"     # 回ごとに別の記録
    rec.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    done: list[str] = []
    lock = threading.Lock()

    def one(url: str) -> None:
        if deadline_passed() or time.time() - t0 > RECHECK_BUDGET_SEC:
            return
        if fetch:
            fetch(url, rec)
        else:
            try:
                run_group([sys.executable, str(root / "scripts" / "fetch_page.py"), url, "--chars", "100"], timeout=180, cwd=root,
                          env={**os.environ, FETCH_RECORD_ENV: str(rec)})
            except subprocess.TimeoutExpired:
                pass
        with lock:
            done.append(url)
    with ThreadPoolExecutor(max_workers=plan["parallel"]) as ex:
        list(ex.map(one, plan["chosen"]))
    changed = [r["url"] for r in record_rows(changed_record(rec))]
    # 読み直せた(相手のサイトから本文を読めた)件数は、試みた件数ではなく読み取りの記録で数える(原本を返した・読めなかったものは
    # 数えない。Sol 監査 b1: 取得に失敗しても読み直せたと数え、一巡の日数が実際より短く出た)
    read_ok = {r["url"] for r in record_rows(rec) if not r.get("archived")} & set(done)
    stats = {k: plan[k] for k in ("cap", "parallel", "runs_per_day", "cycle_days", "oldest_age_h")}
    stats.update(targets=len(plan["targets"]), chosen=len(plan["chosen"]), tried=len(done), checked=len(read_ok), changed=len(changed),
                 elapsed_s=int(time.time() - t0))
    # 実際に読めた件数で一巡の日数を見積もり直す(時間切れ・取得の失敗で読み残すと延びる)
    # 1件も読めなければ一巡を見積もれない(None。申告する。Sol 監査 b3)
    stats["cycle_days_actual"] = (round(len(plan["targets"]) / (len(read_ok) * plan["runs_per_day"]), 2) if read_ok else None) \
        if plan["targets"] else 0.0
    append_metric("recheck", stats)
    print(f"出典の見回り: 対象 {stats['targets']}件のうち {stats['checked']}件を読み直した(上限 {stats['cap']}・並列 {stats['parallel']}・"
          f"一巡 {stats['cycle_days_actual']}日・最も古い確認 {stats['oldest_age_h']}時間前)、変化 {stats['changed']}件", flush=True)
    if plan["targets"] and (stats["cycle_days_actual"] is None or stats["cycle_days_actual"] > RECHECK_CYCLE_DAYS
                            or stats["oldest_age_h"] > RECHECK_CYCLE_DAYS * 24):
        anomaly("collect", f"出典の見回りが {RECHECK_CYCLE_DAYS}日で一巡しない: 対象 {stats['targets']}件、今回 {stats['checked']}/{stats['chosen']}件"
                           f"(上限 {stats['cap']}・並列 {stats['parallel']}・{stats['elapsed_s']}秒)、最も古い確認 {stats['oldest_age_h']}時間前")
    return stats


# ---- 変化の扱い -----------------------------------------------------------------------------------------------

def handled_path() -> Path:
    return page_archive.archive_dir() / "changes-handled.json"


def change_key(row: dict) -> str:
    return f"{page_archive.page_key(row.get('url'))}|{row.get('from_sha')}|{row.get('to_sha')}"


def load_state() -> dict:
    """変化の扱いの状態: marks(記事×ページごとに、記事に反映した版と本文)・tries(記事×ページごとのやり直しの回数)・
    pending(まだ素材の保存を確かめていない「出典の更新」の素材)。"""
    got = _load_json(handled_path())
    got = got if isinstance(got, dict) else {}
    return {k: got.get(k) if isinstance(got.get(k), dict) else {} for k in ("marks", "tries", "pending")}


def save_state(done: dict) -> None:
    p = handled_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def affected(row: dict, arts: list[dict]) -> list[dict]:
    """変化したページを出典(sources・verified_facts・素材)にしている記事。"""
    keys = {page_archive.page_key(u) for u in (row.get("url"), row.get("final")) if u}
    return [a for a in arts if any(page_archive.page_key(u) in keys for u in a["urls"])]


def _day(at: str) -> str:
    try:
        t = datetime.datetime.fromisoformat(str(at))
        return f"{t.month}月{t.day}日"
    except ValueError:
        return str(at)[:10]


def run_session(kind: str, prompt: str, schema_name: str, date: str, name: str, root: Path = ROOT, timeout: float = SESSION_TIMEOUT_SEC):
    """判定・校閲の構造化セッション1本: 書き手は codex(CODEX_WRITE_MODEL)、校閲は claude(REVIEW_MODEL)。正常に終わり、渡した schema に
    合う答えだけを返す(それ以外は None)。CLI の停止は CliStopped で上げる(この変化は次の回にやり直す)"""
    schema = (root / "schema" / schema_name).read_text(encoding="utf-8")
    short = prompt_file(date, name, prompt)
    out_p = root / "metrics" / "work" / date / f"{name}.answer.json"
    if kind == "write":
        # 出典を fetch_page.py で読めるよう、執筆と同じく通信を開く
        cmd = ["codex", "exec", "-m", CODEX_WRITE_MODEL, "-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true",
               "--output-schema", str(root / "schema" / schema_name), "--output-last-message", str(out_p), short]
        tool = "codex"
    else:
        cmd = ["claude", "-p", short, "--model", REVIEW_MODEL, "--json-schema", schema, "--dangerously-skip-permissions",
               "--disallowedTools", "WebFetch"]
        tool = "claude"
    try:
        r = run_group(cmd, timeout=max(1, timeout), cwd=root)
        so, se, code = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        (so, se), code = partial_output(e), None
    text = out_p.read_text(encoding="utf-8") if kind == "write" and out_p.exists() else (so or "")
    out_p.unlink(missing_ok=True)
    save_raw(date, name, so, se, code, files={"answer": text})
    if code is not None and code != 0 and cli_check(tool, "collect", f"出典の変更の{'判定' if kind == 'write' else '校閲'}", code, so or "", se or ""):
        raise CliStopped(tool)
    if code != 0:
        return None
    ans, _ = read_answer(text)
    return ans if isinstance(ans, dict) and schema_ok(ans, schema) else None


class CliStopped(RuntimeError):
    """モデル CLI が認証切れ・利用上限で答えなかった(投稿は済み。変化は次の回にやり直す)。"""


def judge(art: dict, row: dict, diff: str, date: str, prev: dict | None = None, issue: str = "", root: Path = ROOT,
          timeout: float = SESSION_TIMEOUT_SEC):
    """書き手のモデルが変化の種類と更新の文を出す(prompts/source-update.md)。"""
    prompt = render_prompt("source-update", ARTICLE=f"docs/_posts/{art['id']}.md", URL=row["url"], OLD_AT=_day(row.get("from_at")),
                           NEW_AT=_day(row.get("to_at")), DIFF=diff)
    if prev is not None:
        prompt += render_prompt("source-update.retry", KIND=prev.get("kind", ""), TEXT=prev.get("text", ""), ISSUE=issue)
    h = hashlib.sha256(change_key(row).encode()).hexdigest()[:8]
    return run_session("write", prompt, "source-update.schema.json", date, f"source-update-{art['slug'][:40]}-{h}{'-2' if prev else ''}", root,
                       timeout)


def review(art: dict, row: dict, diff: str, ans: dict, date: str, root: Path = ROOT, timeout: float = SESSION_TIMEOUT_SEC):
    """校閲のモデルが更新の案を検める(prompts/source-update-review.md)。"""
    prompt = render_prompt("source-update-review", ARTICLE=f"docs/_posts/{art['id']}.md", URL=row["url"], OLD_AT=_day(row.get("from_at")),
                           NEW_AT=_day(row.get("to_at")), KIND=ans["kind"], TEXT=ans["text"], DIFF=diff)
    h = hashlib.sha256((change_key(row) + ans["text"]).encode()).hexdigest()[:8]
    return run_session("review", prompt, "source-update-review.schema.json", date, f"source-update-review-{art['slug'][:40]}-{h}", root,
                       timeout)


def with_update(text: str, entry: dict) -> str:
    """記事ファイルの frontmatter の updates の末尾に entry を足した全文(本文と、ほかの欄は1字も変えない)。"""
    m = FM_RE.match(text)
    head, body = m.group(1), text[m.end():]
    fm = yaml.safe_load(head) or {}
    ups = list(fm.get("updates") or []) + [entry]
    # 最上位の updates: の塊を外し、末尾に書き直す(ほかの行には触らない)
    lines, out, skip = head.split("\n"), [], False
    for ln in lines:
        if re.match(r"^updates:", ln):
            skip = True
            continue
        if skip and (ln.startswith((" ", "-")) or not ln.strip()):
            continue
        skip = False
        out.append(ln)
    block = yaml.safe_dump({"updates": ups}, allow_unicode=True, sort_keys=False, width=1000).rstrip("\n")
    new = "---\n" + "\n".join(out + [block]) + "\n---\n" + body
    got = yaml.safe_load(FM_RE.match(new).group(1)) or {}
    if {k: v for k, v in got.items() if k != "updates"} != {k: v for k, v in fm.items() if k != "updates"} or got.get("updates") != ups:
        raise ValueError("updates の追記で frontmatter のほかの欄が変わった")
    return new


def update_candidate(row: dict, arts: list[dict], old: dict, new: dict, today: str) -> dict:
    """読者に関わる出典の変化を、その号の「出典の更新」の素材にする(続報の記事の材料。既報は選定・執筆が storylink で照らす)。
    id はページといまの版で決まる(同じ変化をやり直しても同じ素材になる)"""
    # 変わった行は全部(行数で切らない。Sol 監査 b4: 複数の告知が並ぶページで、記事の変化が先頭の20行より後ろにあると素材から落ちた)
    lines = page_archive.diff(old["text"], new["text"], limit=CANDIDATE_DIFF_CHARS).splitlines()
    plus = [ln[1:].strip() for ln in lines if ln.startswith("+") and ln[1:].strip()]
    minus = [ln[1:].strip() for ln in lines if ln.startswith("-") and ln[1:].strip()]
    h = hashlib.sha256(f"{page_archive.page_key(row['url'])}|{row.get('to_sha')}".encode()).hexdigest()[:12]
    a = arts[0]
    return {"id": f"update-{h}", "title": f"出典の更新: {a['title']}"[:200], "brand": a["brand"] or "general",
            "source_type": classify_source(row["url"]), "url": row["url"], "found_at": now_jst().isoformat(timespec="seconds"),
            "dedup_key": a["dedup_key"] or f"update-{h}", "verify": "confirmed", "via": "watch", "origin": "watch",
            "published_date": today,
            "facts": [f"変更後({_day(row.get('to_at'))}に読んだ版): {x}" for x in plus]
                     + [f"変更前({_day(row.get('from_at'))}に読んだ版): {x}" for x in minus],
            "source_change": {"from_at": str(row.get("from_at") or ""), "to_at": str(row.get("to_at") or ""),
                              "articles": [x["id"] for x in arts]},
            "archived_at": page_archive.fetched_at(new)}


def article_url(art_id: str) -> str:
    base = (ENV.get("SITE_URL") or "").rstrip("/")
    return f"{base}/articles/{art_id}/" if base else f"docs/_posts/{art_id}.md"


SESSION_MIN_SEC = 60        # 判定・校閲のセッションを始めるのに要る残り時間(これより短ければ次の回へ回す)


def _mark(new: dict, result: str) -> dict:
    """記事×ページに反映した版の控え(本文も持つ。原本の置き場の版が上限で消えても、次の変化と比べられる)。"""
    return {"sha": new["sha"], "at": new.get("at"), "text": new.get("text"), "result": result}


def process_changes(today: str, arts: list[dict], root: Path = ROOT, judge_fn=judge, review_fn=review,
                    late=lambda: False, left=lambda: SESSION_TIMEOUT_SEC) -> tuple[list[dict], list[dict]]:
    """出典の変化を扱う。戻りは (この号に足す「出典の更新」の素材(まだ素材の保存を確かめていないものを全部), 足した更新)。
    見回りの範囲の発行済み記事の出典(X 以外)の全部について、記事×ページごとに「その記事に反映した版(marks。無ければ記事を出した時点に
    読まれていた版)」から「いまの版(いちばん新しく読んだ版)」への違いを1回だけ判定する(Sol 監査 b1・b2・b4: 台帳の行ごと・ページごとに
    扱うと、やり直しで新しい版から古い版への違いを告知した・元の内容に戻った変化を一部の記事だけ失った・裏取りが先に新しい版を残すと
    変化を黙って失った)。
    判定・校閲が答えなかった記事は進めない(次の回にやり直す。MAX_TRIES 回で諦めて申告)。CLI の停止・時間切れは数えず、この回は止める。
    収集の締切(late)を過ぎた・残り時間(left)が足りなければ、どのセッションも始めない。セッションの待ちは残り時間で切る。
    素材は記事を書く前に控え(pending)、記事を書いたら反映した版を進める(途中で止まっても、素材を失わず、同じ更新を重ねない)"""
    st = load_state()
    applied = []
    has_time = lambda: not late() and left() >= SESSION_MIN_SEC
    stopped = False
    # 見回りの範囲(発行60日)の外の記事の控えは捨てる
    ids = {a["id"] for a in arts}
    for k in [k for k in st["marks"] if k.split("|", 1)[0] not in ids]:
        st["marks"].pop(k, None)
        st["tries"].pop(k, None)
    for art in arts:
        if stopped or late():
            break
        published_at = f"{art['edition']}T06:00:00+09:00"
        for url in art["urls"]:
            if stopped or late():
                break
            page = page_archive.page_key(url)
            k = f"{art['id']}|{page}"
            try:
                new = page_archive.latest(url)
            except Exception:       # noqa: BLE001
                new = None
            if not new:
                continue
            m = st["marks"].get(k)
            if not m:
                # 初めて見る記事×出典: 記事を出した時点に読まれていた原本(発行の 06:00 までに読んだいちばん新しい版)から比べる。
                # 変化の台帳の行に頼らない(Sol 監査 b4: 裏取りが先に新しい版を残すと、台帳に行が出ず変化を黙って失った)
                v0 = page_archive.version_before(url, published_at)
                m = _mark(v0, "発行時点") if v0 else _mark(new, "見張り始め")
                st["marks"][k] = m
            cur = m.get("sha")
            if cur == new["sha"]:
                continue
            old = page_archive.version(url, cur)
            if not old:
                # 原本の置き場の版が上限で消えていても、控えた本文で比べる(黙って扱い済みにしない。Sol 監査 b3)
                if m.get("text") is None:
                    st["marks"][k] = _mark(new, "比べる元の版が無い")
                    anomaly("collect", f"出典の変化を比べる元の版が無く、記事 {art['id']} に反映できない: {url}(いまの版まで扱い済みにした)")
                    continue
                old = {"sha": cur, "text": m["text"], "at": m.get("at")}
            row = {"url": url, "final": None, "from_sha": old["sha"], "to_sha": new["sha"],
                   "from_at": page_archive.fetched_at(old), "to_at": str(new.get("at") or "")}
            if any(u.get("source") == url and u.get("to_at") == row["to_at"] for u in art["updates"] if isinstance(u, dict)):
                st["marks"][k] = _mark(new, "足し済み")     # 書いたあと止まった回の続き
                continue
            diff = page_archive.diff(old["text"], new["text"])
            outcome = None
            try:
                outcome = decide(art, row, diff, today, root, judge_fn, review_fn, has_time, left)
            except CliStopped:
                # 認証切れ・利用上限(投稿は済み): やり直しの回数に数えず、この回はこれ以上セッションを起動しない
                stopped = True
                break
            if outcome == "deferred":
                stopped = True      # 時間が無い: 次の回へ(やり直しの回数に数えない)
                break
            if outcome is None:
                tries = int(st["tries"].get(k) or 0) + 1
                st["tries"][k] = tries
                if tries >= MAX_TRIES:
                    st["marks"][k] = _mark(new, "判定できなかった")
                    st["tries"].pop(k, None)
                    anomaly("collect", f"出典の変更を {tries}回扱えなかった(判定・校閲の答えが読めない): {url} 記事 {art['id']}")
                continue
            if isinstance(outcome, str):
                # 表示の揺れ・記事に関係なし・校閲が通さなかった: この記事はいまの版まで扱った
                st["marks"][k] = _mark(new, outcome)
                st["tries"].pop(k, None)
                continue
            ans = outcome
            entry = {"at": now_jst().isoformat(timespec="seconds"), "kind": ans["kind"], "text": ans["text"].strip(),
                     "source": url, "from_at": row["from_at"], "to_at": row["to_at"]}
            # 素材を先に控える(このあと止まっても素材は残る。同じ変化なら同じ id)
            cand = update_candidate(row, [art], old, new, today)
            if cand["id"] in st["pending"]:
                arts_of = st["pending"][cand["id"]]["source_change"]["articles"]
                if art["id"] not in arts_of:
                    arts_of.append(art["id"])
            else:
                st["pending"][cand["id"]] = cand
            save_state(st)
            try:
                write_checked(art["path"], with_update(art["path"].read_text(encoding="utf-8"), entry))
            except Exception as e:      # noqa: BLE001  1本の失敗でほかの記事・収集を止めない(申告する)
                anomaly("collect", f"記事 {art['id']} に出典の変更による更新を足せない({type(e).__name__}: {str(e)[:160]})")
                continue
            art["updates"].append(entry)
            st["marks"][k] = _mark(new, ans["kind"])
            st["tries"].pop(k, None)
            save_state(st)
            applied.append({"article": art["id"], "entry": entry, "row": row})
            notify("collect", f"出典の変更を記事に足した({ans['kind']}): {article_url(art['id'])}\n「{entry['text']}」\n"
                              f"出典 {url}({_day(row['from_at'])} → {_day(row['to_at'])})\n変わったところ:\n{diff[:600]}")
    save_state(st)
    return list(st["pending"].values()), applied


def decide(art: dict, row: dict, diff: str, today: str, root: Path, judge_fn, review_fn, has_time, left):
    """1つの記事×変化の判定と校閲。戻りは 足す答え(dict)/ 足さない理由(str)/ None(答えが読めない)/ "deferred"(時間が無い)。
    どのセッションも、始める前に締切と残り時間を確かめる(Sol 監査 b2: 判定のあとで締切を過ぎても校閲を起動した)"""
    timeout = lambda: min(SESSION_TIMEOUT_SEC, left())
    if not has_time():
        return "deferred"
    # 答えが無いとき、締切の残りで切った(時間切れ)なら deferred(やり直しの回数に数えない。Sol 監査 b3)
    unread = lambda: "deferred" if not has_time() else None
    ans = judge_fn(art, row, diff, today, root=root, timeout=timeout())
    if ans is None:
        return unread()
    if ans["kind"] not in UPDATE_KINDS:
        return ans["kind"]
    if not has_time():
        return "deferred"
    verdict = review_fn(art, row, diff, ans, today, root=root, timeout=timeout())
    if verdict and verdict["verdict"] == "block":
        if not has_time():
            return "deferred"
        again = judge_fn(art, row, diff, today, prev=ans, issue=verdict["issue"], root=root, timeout=timeout())
        if again is None:
            return unread()
        if again["kind"] not in UPDATE_KINDS:
            return again["kind"]
        if not has_time():
            return "deferred"
        ans, verdict = again, review_fn(art, row, diff, again, today, root=root, timeout=timeout())
    if verdict is None:
        return unread()
    if verdict["verdict"] != "approve" or not ans["text"].strip():
        # 判断の結果(校閲が通さなかった)で、工程の異常ではない。人には知らせる
        notify("collect", f"出典の変更による更新を校閲が通さず、記事に足さなかった: {article_url(art['id'])} / 出典 {row['url']} / "
                          f"案「{ans['text'][:120]}」/ 指摘 {verdict.get('issue', '')[:200]}")
        return "校閲が通さなかった"
    return ans


def clear_pending(ids) -> None:
    """収集が候補の保存を確かめた「出典の更新」の素材を、返す列から外す。"""
    st = load_state()
    for i in ids:
        st["pending"].pop(i, None)
    save_state(st)
