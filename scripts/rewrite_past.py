#!/usr/bin/env python3
"""rewrite_past: GPT-6-Luna が旧い依頼文で書いた記事(10/3〜10/7 号)を、チューニング後の依頼文で書き直す(1回限り)。

編集長(2026-10-08):「過去の記事を全部書き直してもいいよ。今回に限っては免責なしに全部やって良い」
「(対象は)GPT-6-Luna 執筆記事ども」「データが残ってないならしょうがない」。訂正の注記は付けない(今回限り)。

- 入力は、その号で執筆に渡した依頼文の記録(ops の metrics/work/<日付>/write-<slug>-0.md)。依頼文はいまのもので作り直す
- 号の日付より後に出た情報は書かせない(読んだページが後から更新されていても、その号の時点の内容だけ)
- 既に訂正が入った記事は、訂正後の内容で書かせ、訂正の記録(corrections)はそのまま残す
- 本番と同じ検算(renderlib.check_output)と校閲(compose.claude_review)を通った記事だけ差し替える。
  見送り・検算不合格・校閲のブロック・答えが無いものは元の記事のまま(一覧を出す)
- 差し替えは作業ツリーの docs/_posts に書くだけ。コミット・反映は人(または呼び出し側)が行う
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import writer_bench as wb  # noqa: E402
from pipelib import ROOT  # noqa: E402

PAST_RULE = ("- 書き直し: これは {date} 号の記事の書き直し。{date} 06:00 より後に出た情報(ページの更新・追記・後日の結果・完売など)は書かない。"
             "読んだページに更新日や追記があれば、{date} 時点で出ていた内容だけを使う\n")


def front(path: Path) -> tuple[dict, str]:
    import yaml
    t = path.read_text(encoding="utf-8")
    _, fm, body = t.split("---\n", 2)
    return yaml.safe_load(fm), body


def build_cases(ops: Path, dates: list[str]) -> dict[str, dict]:
    cases = {}
    for d in dates:
        for p in sorted((ROOT / "docs" / "_posts").glob(f"{d}-*.md")):
            fm, _ = front(p)
            slug = fm["slug"]
            if not (ops / "metrics" / "work" / d / f"write-{slug}-0.md").exists():
                print(f"依頼文の記録が無い(対象外): {p.name}", flush=True)
                continue
            c = wb.make_case(ops, d, slug) if (ops / "metrics" / f"plan-{d}.json").exists() else None
            if c is None:
                continue
            c["trigger"] = PAST_RULE.format(date=d) + c["trigger"]
            for cr in fm.get("corrections") or []:
                c["trigger"] += f"- 訂正済み: この記事には訂正がある。訂正後の内容で書く: {json.dumps(cr, ensure_ascii=False)}\n"
            c["rank"] = fm.get("rank") or c["rank"]
            cases[p.name] = c
    return cases


def apply_answer(path: Path, case: dict, ans: dict) -> list[str]:
    """検算して、元の frontmatter の記録(訂正・号・slug・候補)を残したまま本文と内容の欄を差し替える。戻りは不合格の理由。"""
    import renderlib
    import yaml
    from compose import RANK_BY_LENGTH, body_length, classify_source, weakest_src, yaml_dump_keeping_strings
    fact_by_id = {f["id"]: f["text"] for m in case["materials"] for f in (m.get("facts") or []) if isinstance(f, dict)}
    problems = renderlib.check_output(ans, fact_by_id, case["materials"], rank=case["rank"], edition=case["date"])
    if problems:
        return problems
    old_fm, _ = front(path)
    tmp = path.with_suffix(".rewrite.tmp")
    art = {"slug": old_fm["slug"], "brand": old_fm["brand"], "rank": old_fm.get("rank"), "candidate_ids": old_fm.get("candidate_ids") or []}
    renderlib.render_article(tmp, case["date"], art, ans, classify_source, weakest_src, yaml_dump_keeping_strings)
    new_fm, new_body = front(tmp)
    tmp.unlink()
    content_keys = ("src", "title", "lede", "tags", "sources", "event_date", "title_fact_ids", "lede_fact_ids", "verified_facts")
    fm = dict(old_fm)
    for k in content_keys:
        if k in new_fm:
            fm[k] = new_fm[k]
        else:
            fm.pop(k, None)
    path.write_text("---\n" + yaml_dump_keeping_strings(fm) + "---\n" + new_body, encoding="utf-8")
    fix_rank(path)
    return []


def fix_rank(path: Path) -> None:
    """枠(rank)を、本番(compose.assign_ranks)・検査(lint)と同じ数え方(compose.body_length)の長さで付け直す。
    一面・まとめ・ファン面は長さで動かさない。"""
    from compose import RANK_BY_LENGTH, body_length, yaml_dump_keeping_strings
    fm, body = front(path)
    if fm.get("rank") in ("lead", "roundup", "culture"):
        return
    n = body_length(path)
    rank = next(r for r, lo in RANK_BY_LENGTH if n >= lo and r != "lead")
    if rank != fm.get("rank"):
        fm["rank"] = rank
        path.write_text("---\n" + yaml_dump_keeping_strings(fm) + "---\n" + body, encoding="utf-8")


def latest_raw_answer(label: str, stem: str) -> dict | None:
    """生の出力の記録のうち、いちばん新しい正常終了の答え(schema どおり)。無ければ None。"""
    best = None
    for p in (ROOT / "metrics" / "work" / "bench" / "raw").glob(f"{label}-{stem}*.txt"):
        if not re.fullmatch(re.escape(f"{label}-{stem}") + r"(-\d+)?\.txt", p.name):
            continue
        t = p.read_text(encoding="utf-8")
        at = (re.search(r"^# at: (\S+)", t, re.M) or [None, ""])[1]
        if not re.search(r"^# exit: 0$", t, re.M) or "# file: last-message\n" not in t:
            continue
        try:
            ans = json.loads(t.split("# file: last-message\n", 1)[1])
        except ValueError:
            continue
        if wb.schema_ok(ans, json.loads(wb.SCHEMA_OUT.read_text(encoding="utf-8"))) and (best is None or at > best[0]):
            best = (at, ans)
    return best[1] if best else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dates", nargs="+")
    ap.add_argument("--ops", default=str(Path.home() / "git" / "imas-ops"))
    ap.add_argument("--label", default="rewrite")
    ap.add_argument("--only", nargs="*", default=None, help="この記事(ファイル名)だけ")
    ap.add_argument("--from-raw", action="store_true", help="書かせず、前に書いた答えを生の出力の記録から読む")
    a = ap.parse_args()
    cases = build_cases(Path(a.ops), a.dates)
    if a.only:
        cases = {k: v for k, v in cases.items() if k in a.only}
    print(f"書き直す記事: {len(cases)}本", flush=True)
    answers = {}
    if a.from_raw:
        # 書いた答えを、生の出力の記録(metrics/work/bench/raw/<ラベル>-<記事>[-n].txt)から読む(書き直し直さない)
        for n in cases:
            answers[n] = latest_raw_answer(a.label, n[:-3])
            print(f"記録から: {n[:-3]} {(answers[n] or {}).get('status')}", flush=True)
    else:
        # 書かせる(本番と同じ codex exec・出力 schema)
        import concurrent.futures
        shorts = {n: wb.prompt_file("bench", f"{a.label}-{n[:-3]}", wb.render(c)) for n, c in cases.items()}
        with concurrent.futures.ThreadPoolExecutor(wb.PARALLEL) as ex:
            for res in ex.map(lambda n: wb.write_one(n[:-3], shorts[n], a.label, wb.CODEX_WRITE_MODEL), cases):
                answers[res["case"] + ".md"] = res["answer"]
                print(f"書いた: {res['case']} {(res['answer'] or {}).get('status')}", flush=True)
    kept: dict[str, str] = {}
    applied: dict[str, list[str]] = {}
    backups: dict[str, str] = {}
    confirmed: set[str] = set()
    try:
        replace_and_review(answers, cases, kept, applied, backups, confirmed)
    except BaseException:
        # 校閲を通ったと確かめる前に止まったら(例外・中断)、差し替えた記事を全部元に戻す(未校閲の記事を残さない。監査指摘)
        for n, text in backups.items():
            if n not in confirmed:
                (ROOT / "docs" / "_posts" / n).write_text(text, encoding="utf-8")
        raise
    done = [n for ns in applied.values() for n in ns if n not in kept]
    out = {"replaced": done, "kept": kept}
    (ROOT / "metrics" / "work" / "bench" / f"{a.label}-result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"差し替え {len(done)}本 / 元のまま {len(kept)}本", flush=True)
    for n, why in kept.items():
        print(f"  元のまま: {n}: {why}", flush=True)
    return 0


def replace_and_review(answers: dict, cases: dict, kept: dict, applied: dict, backups: dict, confirmed: set) -> None:
    """検算を通った答えで差し替え、号ごとに校閲する。校閲を通ったものだけ confirmed に入れる(それ以外は元に戻す)。"""
    for n, ans in answers.items():
        path = ROOT / "docs" / "_posts" / n
        if not ans or ans.get("status") != "ok":
            kept[n] = f"見送り・答えなし({(ans or {}).get('decline_code')})"
            continue
        backups[n] = path.read_text(encoding="utf-8")
        probs = apply_answer(path, cases[n], ans)
        if probs:
            kept[n] = "検算不合格: " + " / ".join(probs[:2])
            continue
        applied.setdefault(cases[n]["date"], []).append(n)
    # 校閲(本番と同じ)。ブロック・判定できないものは元に戻す
    import compose
    for d, names in applied.items():
        # 校閲は本番と同じ1巡目として呼ぶ。本番の記録(metrics/review-<日付>-1.json。発行の判定に使った)は控えて戻す
        rec = ROOT / "metrics" / f"review-{d}-1.json"
        saved_rec = rec.read_bytes() if rec.exists() else None
        try:
            r = compose.claude_review(d, 1, targets=names, editorial=False, paper=False)
        finally:
            if saved_rec is None:
                rec.unlink(missing_ok=True)
            else:
                rec.write_bytes(saved_rec)
        blocked = {str(b.get("file") or "").split("/")[-1] for b in r.get("blockers") or []}
        failed = {str(f).split(":")[0] for f in r.get("failed") or []}
        for n in names:
            if n in blocked or any(n in f for f in failed) or r.get("verdict") == "error":
                (ROOT / "docs" / "_posts" / n).write_text(backups[n], encoding="utf-8")
                kept[n] = "校閲ブロック・判定なし: " + "; ".join(str(b.get("issue"))[:80] for b in r.get("blockers") or []
                                                         if str(b.get("file") or "").endswith(n))
            else:
                confirmed.add(n)


if __name__ == "__main__":
    sys.exit(main())
