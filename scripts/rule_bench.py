#!/usr/bin/env python3
"""rule_bench: 依頼文の規則を、固定の事例で実際のモデルに判断させて検める(依頼文を変えるときに人が走らせる)。

  python3 scripts/rule_bench.py --run [<事例>…] [--label <ラベル>]   # 全部(か名指しの事例)を1回ずつ判断させ、合格/不合格を出す
  python3 scripts/rule_bench.py --check                              # 事例ファイルの形だけを検める(モデルは呼ばない)

監査指摘(2026-10-14): 回帰テストは依頼文に決まった文言があるかだけを見ていて、逆の規則を書き足しても文言が残れば通った
(一部の素材だけが食い違うなら残りで書く / 信頼できる報道があれば書く / P1 は同じ知らせだけ)。規則が効くかはモデルに判断させないと分からない。

- 事例(bench/rules/cases/<名前>.json)は、依頼文に埋める変数(素材・切り口・記事ファイル)、出典のページの本文(固定)、期待する判断の形(expect)。
  素材は候補(candidates)の行の形で持つ。執筆には本番と同じく renderlib.materials_with_ids で F1… を振って渡す(expect の id はその番号)
- 執筆(kind: write)は compose.article_prompt で依頼文を作り、writer_bench.write_one(本番の執筆と同じ codex exec・モデル・出力 schema)で書かせる。
  校閲(kind: review_article / review_paper)は、事例の記事・素材・既報の台帳を置いた作業フォルダを ROOT にして compose.claude_review をそのまま呼ぶ
  (本番と同じ claude -p・モデル・判定 schema・指摘のまとめ方)
- 照らすのは答えの形の欄だけ(判断はモデル、コードは形): 執筆は status・decline_code・根拠に付けた事実 id(見出し・リード・段落)・
  sources と new_facts の url。校閲は claude_review がまとめた blockers の rule_id と file。内容の良し悪しはコードで判定しない
- 出典のページは毎回取りに行かない: 事例の本文を置いたファイルを環境変数 IMAS_FETCH_PAGES で渡し、fetch_page.py はそれを返す(事例に無いページは取れない)。
  ただし執筆(codex)の Web 検索は本物の Web に届く。「公式・報道に届かない」ことを前提にする事例は、Web に出典の無い話題で作る
  (2026-10-14 の初回: 本物の話題のまとめ記事の事例で、執筆が Web 検索で本物の報道を見つけて書いた)
- 1事例1回(揺れを見る繰り返しはしない)。不合格は依頼文の欠陥の候補で、直すかは人が決める
- selfcheck・watch・当番の検査には結びつけない(依頼文を変えるときに人が走らせるもの。発行の工程とは別。手順は PIPELINE.md 4g4)
"""
import argparse
import concurrent.futures
import copy
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipelib  # noqa: E402
from pipelib import CODEX_WRITE_MODEL, REVIEW_MODEL, ROOT, schema_errors  # noqa: E402

CODE = Path(__file__).resolve().parent.parent          # 事例・依頼文・道具はコードと同じ版
CASES = CODE / "bench" / "rules" / "cases"
OUT = ROOT / "metrics" / "work" / "bench" / "rules"
KINDS = ("write", "review_article", "review_paper")
RANKS = ("lead", "large", "medium", "small", "roundup", "culture")
DECLINE_CODES = ("NO_PRIMARY_SOURCE", "SOURCE_MISMATCH", "TOO_FEW_MATERIALS", "NOT_NEWS", "OTHER")   # renderlib.DECLINE_CODES と同じ
FIXED_PAGES_ENV = "IMAS_FETCH_PAGES"                  # fetch_page.py が読む(事例の出典の本文)
PARALLEL = 6
PROMPT_FILES = ("prompts/write-article*.md", "prompts/revise-article.md", "prompts/review-*.md", "prompts/review-schema.json",
                "schema/article-out.schema.json")


def load_cases(names: list[str] | None = None) -> dict[str, dict]:
    cases = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES.glob("*.json"))}
    if names:
        missing = [n for n in names if n not in cases]
        if missing:
            sys.exit(f"事例が無い: {missing}")
        cases = {n: cases[n] for n in names}
    return cases


def norm_url(u) -> str:
    return str(u or "").strip().rstrip("/")


def material_ids(rows: list[dict]) -> dict[str, str]:
    """素材の事実 id(本番の執筆に渡すのと同じ通し番号)→ その素材の url。"""
    out, n = {}, 0
    for r in rows:
        for _ in r.get("facts") or []:
            n += 1
            out[f"F{n}"] = r.get("url") or ""
    return out


def case_problems(case: dict) -> list[str]:
    """事例ファイルの形(走らせる前に検める。モデルは呼ばない)。空なら走らせてよい。"""
    p = []
    kind, exp = case.get("kind"), case.get("expect")
    if kind not in KINDS:
        return [f"kind が {KINDS} のどれでもない: {kind!r}"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(case.get("date") or "")):
        p.append(f"date が日付でない: {case.get('date')!r}")
    for k in ("rule", "origin"):
        if not isinstance(case.get(k), str) or not case[k].strip():
            p.append(f"{k}(どの依頼文のどの規則か / どこから作ったか)が無い")
    pages = case.get("pages")
    if not isinstance(pages, dict) or not all(re.match(r"^https?://", str(u)) and isinstance(t, str) for u, t in pages.items()):
        p.append("pages は {url: 本文} の形")
        pages = {}
    if not isinstance(exp, dict):
        return p + ["expect が無い"]
    if kind == "write":
        rows = case.get("materials")
        if not isinstance(rows, list) or not rows:
            return p + ["materials(素材の候補の行)が無い"]
        p += [f"materials: {e}" for e in schema_errors("candidates/2000-01-01.json", json.dumps(rows, ensure_ascii=False))]
        for k in ("slug", "angle"):
            if not isinstance(case.get(k), str) or not case[k]:
                p.append(f"{k} が無い")
        if case.get("rank") not in RANKS:
            p.append(f"rank が {RANKS} のどれでもない")
        if case.get("purpose") not in ("新情報", "期日の知らせ"):
            p.append("purpose は 新情報 / 期日の知らせ")
        trig = case.get("trigger")
        if trig is not None and not (isinstance(trig, dict) and trig.get("kind") and trig.get("subject")):
            p.append("trigger は null か {kind, subject, note}")
        if not isinstance(case.get("story_facts"), list) or not all(isinstance(x, str) for x in case["story_facts"]):
            p.append("story_facts は文字列の配列(既報が無ければ [])")
        ids = material_ids(rows if isinstance(rows, list) else [])
        urls = {norm_url(r.get("url")) for r in rows if isinstance(r, dict)}
        for r in rows:
            u = str((r or {}).get("url") or "")
            if u and not re.match(r"^https?://(x|twitter)\.com/", u) and u not in pages:
                p.append(f"素材の url の本文が pages に無い(執筆は素材の url を全部読む): {u}")
        unknown = set(exp) - {"status", "decline_codes", "cite", "not_cite", "sources", "not_sources"}
        if unknown:
            p.append(f"expect に知らない欄: {sorted(unknown)}")
        if exp.get("status") not in ("ok", "decline"):
            p.append("expect.status は ok / decline")
        if exp.get("status") == "decline" and not (exp.get("decline_codes") and set(exp["decline_codes"]) <= set(DECLINE_CODES)):
            p.append(f"見送りを期待するなら expect.decline_codes を {DECLINE_CODES} から")
        if exp.get("status") == "ok" and exp.get("decline_codes"):
            p.append("書くことを期待する事例に decline_codes がある")
        for k in ("cite", "not_cite"):
            bad = [i for i in exp.get(k) or [] if i not in ids]
            if bad:
                p.append(f"expect.{k} に素材に無い事実 id: {bad}")
        for k in ("sources", "not_sources"):
            bad = [u for u in exp.get(k) or [] if norm_url(u) not in urls]
            if bad:
                p.append(f"expect.{k} に素材に無い url: {bad}")
        if exp.get("status") == "ok" and not any(exp.get(k) for k in ("cite", "not_cite", "sources", "not_sources")):
            p.append("書くことを期待する事例に、使う・使わない素材の期待(cite / not_cite / sources / not_sources)が無い")
        return p
    posts = case.get("posts")
    if not isinstance(posts, dict) or not posts:
        return p + ["posts({ファイル名: 記事の全文})が無い"]
    date = str(case.get("date") or "")
    for n, text in posts.items():
        if not re.fullmatch(rf"{re.escape(date)}-[a-z0-9][a-z0-9-]*\.md", n):
            p.append(f"記事のファイル名が {date}-<slug>.md でない: {n}")
        p += [f"{n}: {e}" for e in schema_errors(f"docs/_posts/{n}", text if isinstance(text, str) else "")]
    if kind == "review_article" and len(posts) != 1:
        p.append("review_article の記事は1本(校閲は記事1本ずつ)")
    rows = case.get("candidates")
    if not isinstance(rows, list):
        p.append("candidates(素材の候補の行)が配列でない")
        rows = []
    p += [f"candidates: {e}" for e in schema_errors(f"candidates/{date}.json", json.dumps(rows, ensure_ascii=False))]
    if kind == "review_article":
        have = {r.get("id") for r in rows if isinstance(r, dict)}
        for n, text in posts.items():
            m = re.match(r"\A---\s*\n(.*?)\n---", text or "", re.S)
            fm = (yaml.safe_load(m.group(1)) or {}) if m else {}
            miss = [c for c in fm.get("candidate_ids") or [] if not str(c).startswith("sched-") and c not in have]
            if miss:
                p.append(f"{n} の candidate_ids の素材が candidates に無い: {miss}")
    if not isinstance(case.get("stories_before", []), list):
        p.append("stories_before は既報の台帳の行の配列(無ければ [])")
    unknown = set(exp) - {"blockers", "no_blockers"}
    if unknown:
        p.append(f"expect に知らない欄: {sorted(unknown)}")
    if not (exp.get("blockers") or exp.get("no_blockers")):
        p.append("expect に blockers / no_blockers のどちらも無い")
    files = {f"docs/_posts/{n}" for n in posts}
    for key in ("blockers", "no_blockers"):
        for e in exp.get(key) or []:
            if not isinstance(e, dict) or not re.fullmatch(r"[A-Z]\d+", str(e.get("rule") or "")):
                p.append(f"expect.{key} の rule は項目番号(P1・R5 など): {e!r}")
                continue
            bad = [f for f in e.get("files") or [] if f not in files]
            if bad:
                p.append(f"expect.{key} の files が事例の記事でない: {bad}")
            if "n_files" in e and not (isinstance(e["n_files"], int) and e["n_files"] >= 1 and key == "blockers"):
                p.append(f"expect.{key} の n_files は blockers の正の整数だけ")
    return p


def rule_of(rule_id) -> str:
    """rule_id の頭の項目番号(「P1 同じ主題」→ P1、「R-5」→ R5)。形だけを読む。"""
    m = re.match(r"\s*([A-Z])\s*-?\s*(\d+)", str(rule_id or ""))
    return f"{m.group(1)}{m.group(2)}" if m else ""


def cited_ids(ans: dict) -> set[str]:
    """見出し・リード・段落の根拠に付けた事実 id。"""
    ids = list(ans.get("title_fact_ids") or []) + list(ans.get("lede_fact_ids") or [])
    for b in ans.get("blocks") or []:
        if isinstance(b, dict):
            ids += list(b.get("fact_ids") or [])
    return {str(i).strip() for i in ids}


def judge_write(expect: dict, ans) -> list[str]:
    """執筆の答えの形を期待と照らす(空なら合格)。"""
    if not isinstance(ans, dict):
        return ["答えが無い・読めない(正常に終わったセッションの、出力 schema どおりの答えが無い)"]
    st = ans.get("status")
    if st != expect["status"]:
        why = f": {ans.get('decline_code')} {str(ans.get('decline_detail') or '')[:160]}" if st == "decline" else ""
        return [f"status が {st}(期待は {expect['status']}){why}"]
    if st == "decline":
        codes = expect.get("decline_codes") or []
        return [] if not codes or ans.get("decline_code") in codes else [f"decline_code が {ans.get('decline_code')}(期待は {codes})"]
    p = []
    cited = cited_ids(ans)
    p += [f"{i} を根拠に使っていない" for i in expect.get("cite") or [] if i not in cited]
    p += [f"{i} を根拠に使った" for i in expect.get("not_cite") or [] if i in cited]
    srcs = {norm_url(s.get("url")) for s in ans.get("sources") or [] if isinstance(s, dict)}
    used = srcs | {norm_url(f.get("url")) for f in ans.get("new_facts") or [] if isinstance(f, dict)}
    p += [f"出典に {u} が無い" for u in expect.get("sources") or [] if norm_url(u) not in srcs]
    p += [f"{u} を出典・確かめた事実に使った" for u in expect.get("not_sources") or [] if norm_url(u) in used]
    return p


def judge_review(expect: dict, merged) -> list[str]:
    """校閲の答え(claude_review がまとめたもの)の blockers の形を期待と照らす(空なら合格)。"""
    if not isinstance(merged, dict) or merged.get("verdict") not in ("approve", "block"):
        m = merged if isinstance(merged, dict) else {}
        return [f"校閲の判定が無い({m.get('verdict')}: {m.get('failed') or m.get('error') or ''})"]
    files: dict[str, set[str]] = {}
    for b in merged.get("blockers") or []:
        if isinstance(b, dict):
            files.setdefault(rule_of(b.get("rule_id")), set()).add(str(b.get("file") or ""))
    p = []
    for e in expect.get("blockers") or []:
        got = files.get(e["rule"], set())
        if not got:
            p.append(f"{e['rule']} の指摘が無い")
        elif e.get("files") and got - set(e["files"]):
            p.append(f"{e['rule']} の指摘の対象が事例の記事でない: {sorted(got - set(e['files']))}")
        elif e.get("n_files") and len(got) != e["n_files"]:
            p.append(f"{e['rule']} の指摘の対象が {len(got)} 本(期待は {e['n_files']} 本): {sorted(got)}")
    for e in expect.get("no_blockers") or []:
        got = files.get(e["rule"], set())
        if e.get("files"):
            got = got & set(e["files"])
        if got:
            p.append(f"{e['rule']} の指摘がある: {sorted(got)}")
    return p


def summary(kind: str, ans) -> str:
    """答えの要点(報告用)。"""
    if not isinstance(ans, dict):
        return "答えなし"
    if kind == "write":
        if ans.get("status") == "decline":
            return f"decline {ans.get('decline_code')}: {str(ans.get('decline_detail') or '')[:140]}"
        return (f"ok 「{ans.get('title')}」 根拠 {sorted(cited_ids(ans), key=lambda i: (i[0], int(re.sub(r'\D', '', i) or 0)))} "
                f"出典 {[s.get('url') for s in ans.get('sources') or [] if isinstance(s, dict)]}")
    bl = [f"{b.get('rule_id')} {b.get('file')}" for b in ans.get("blockers") or [] if isinstance(b, dict)]
    return f"{ans.get('verdict')} 指摘 {bl}"


def run_write(name: str, case: dict, label: str, work: Path) -> dict:
    import compose
    import renderlib
    import writer_bench
    mats_in, fact_by_id = renderlib.materials_with_ids(case["materials"])
    art = {"slug": case["slug"], "rank": case["rank"], "angle": case["angle"], "purpose": case["purpose"]}
    prompt = compose.article_prompt(case["date"], art, mats_in, case["story_facts"], case.get("trigger"))
    short = pipelib.prompt_file("bench", f"rules-{label}-{name}", prompt)
    res = writer_bench.write_one(f"rules-{name}", short, label, CODEX_WRITE_MODEL)
    ans = res["answer"]
    check = []
    if ans:
        # 本番の検算も掛けて残す(通らなければ本番では差し戻しになる形の誤り)。合否は expect だけで決める
        try:
            check = renderlib.check_output(copy.deepcopy(ans), fact_by_id, case["materials"], rank=case["rank"], edition=case["date"])
        except Exception as e:      # noqa: BLE001
            check = [f"検算できない({type(e).__name__})"]
    return {"answer": ans, "exit": res["exit"], "check_problems": check}


def run_review(name: str, case: dict, label: str, work: Path) -> dict:
    """事例の記事・素材・既報の台帳だけを置いた作業フォルダを ROOT にして、本番の校閲(compose.claude_review)を呼ぶ。"""
    import compose
    date = case["date"]
    posts = work / "docs" / "_posts"
    posts.mkdir(parents=True)
    for n, text in case["posts"].items():
        pipelib.write_checked(posts / n, text)
    (work / "candidates").mkdir()
    pipelib.write_checked(work / "candidates" / f"{date}.json", json.dumps(case["candidates"], ensure_ascii=False, indent=1) + "\n")
    (work / "metrics").mkdir()
    (work / "metrics" / f"stories-before-{date}.yml").write_text(
        yaml.safe_dump(case.get("stories_before") or [], allow_unicode=True, sort_keys=False), encoding="utf-8")
    # 校閲が使う道具(fetch_page.py)・出典の判定表・名鑑は、コードと同じ版を指す(作業フォルダからは書き換えない)
    for rel in ("scripts", "source_types.yml", "docs/_data"):
        (work / rel).symlink_to(CODE / rel)
    saved = (compose.ROOT, pipelib.ROOT)
    compose.ROOT = pipelib.ROOT = work
    try:
        merged = compose.claude_review(date, 1, targets=(list(case["posts"]) if case["kind"] == "review_article" else []),
                                       editorial=False, paper=case["kind"] == "review_paper")
    finally:
        compose.ROOT, pipelib.ROOT = saved
    return {"answer": {k: merged.get(k) for k in ("verdict", "blockers", "comments", "failed") if k in merged}}


def run_one(name: str, label: str) -> dict:
    """事例1つを1回走らせて、答えと照らした結果を OUT/<label>/<name>.json に残す(--run が事例ごとに別のプロセスで呼ぶ)。"""
    case = load_cases([name])[name]
    probs = case_problems(case)
    if probs:
        sys.exit(f"{name}: 事例の形が崩れている: {probs[:5]}")
    work = OUT / label / name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    pages = work / "pages.json"
    pages.write_text(json.dumps(case["pages"], ensure_ascii=False), encoding="utf-8")
    os.environ[FIXED_PAGES_ENV] = str(pages)      # 子(codex・claude とその道具)が引き継ぐ
    res = (run_write if case["kind"] == "write" else run_review)(name, case, label, work)
    judge = judge_write if case["kind"] == "write" else judge_review
    res.update({"case": name, "kind": case["kind"], "rule": case["rule"], "problems": judge(case["expect"], res["answer"])})
    res["pass"] = not res["problems"]
    res["summary"] = summary(case["kind"], res["answer"])
    (OUT / label / f"{name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def prompt_hash() -> str:
    h = hashlib.sha256()
    for pat in PROMPT_FILES:
        for p in sorted(CODE.glob(pat)):
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


def run(names: list[str], label: str) -> int:
    cases = load_cases(names or None)
    bad = {n: case_problems(c) for n, c in cases.items()}
    bad = {n: p for n, p in bad.items() if p}
    if bad:
        sys.exit(f"事例の形が崩れている(走らせない): {bad}")
    d = OUT / label
    d.mkdir(parents=True, exist_ok=True)
    (d / "_meta.json").write_text(json.dumps({"write_model": CODEX_WRITE_MODEL, "review_model": REVIEW_MODEL, "prompt_hash": prompt_hash(),
                                              "cases": sorted(cases)}, ensure_ascii=False), encoding="utf-8")

    def child(n: str) -> dict:
        # 事例ごとに別のプロセス(出典の本文の環境変数と、校閲の作業フォルダを事例ごとに分ける)
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--one", n, "--label", label],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=3600)
        (d / f"{n}.log").write_text((r.stdout or "") + (r.stderr or ""), encoding="utf-8")
        f = d / f"{n}.json"
        if r.returncode != 0 or not f.exists():
            return {"case": n, "pass": False, "problems": [f"走らせられない(exit {r.returncode}): {(r.stderr or r.stdout or '')[-300:]}"],
                    "summary": ""}
        return json.loads(f.read_text(encoding="utf-8"))
    with concurrent.futures.ThreadPoolExecutor(PARALLEL) as ex:
        results = list(ex.map(child, cases))
    for r in results:
        print(f"{'合格' if r['pass'] else '不合格'} {r['case']}: {r.get('summary', '')}"
              + ("" if r["pass"] else f"\n    → {' / '.join(r['problems'])}"), flush=True)
    n_ok = sum(1 for r in results if r["pass"])
    print(f"rule_bench: {n_ok}/{len(results)} 合格(記録 {d.relative_to(ROOT)}/)", flush=True)
    return 0 if n_ok == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", nargs="*", metavar="CASE", help="事例を走らせる(名指しが無ければ全部)")
    ap.add_argument("--label", default=datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
    ap.add_argument("--one", metavar="CASE", help=argparse.SUPPRESS)
    ap.add_argument("--check", action="store_true", help="事例ファイルの形だけを検める")
    a = ap.parse_args()
    if a.one:
        r = run_one(a.one, a.label)
        print(f"{'合格' if r['pass'] else '不合格'} {a.one}: {r['summary']}")
        return 0
    if a.check:
        bad = {n: case_problems(c) for n, c in load_cases().items()}
        for n, p in bad.items():
            print(f"{n}: " + ("ok" if not p else " / ".join(p)))
        return 1 if any(bad.values()) else 0
    if a.run is not None:
        return run(a.run, a.label)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
