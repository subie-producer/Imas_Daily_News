#!/usr/bin/env python3
"""writer_bench: 執筆の依頼文 × 執筆モデルの品質を、固定のケース集で測る(モデルを変えるときに毎回回す)。

編集長(2026-10-08):「6-Luna 向けのプロンプトチューニングを最速で実施すること。記事を本気で書かせるとはどういうことかを
しっかりとチューンすること」「モデル変更時には毎回このプロンプトチューニングを実施し、同じ以上の品質を担保すること」。
10/2 に執筆モデルを GPT-6-Luna に替えてから、素材が 1,500 字ある初出の話題でも本文が 150〜270 字に縮んでいた。

- ケース(bench/writer/cases/<名前>.json)は、実際の号で執筆に渡した依頼文の変数(切り口・目的・続報予約・rank・既報・素材)。
  `--make <日付> <slug>…` で ops の記録(metrics/work/<日付>/write-<slug>-0.md)から作る
- `--run <ラベル>` で、いまの依頼文(prompts/write-article*.md)と執筆モデルで全ケースを書かせ、
  metrics/work/bench/<ラベル>/<ケース>.json に答えを残す(本番と同じ codex exec・出力 schema・検算)
- `--judge <ラベルA> <ラベルB>` で、監査モデル(Sol)にケースごとに2本を読み比べさせる(どちらが先かは伏せて入れ替える)。
  品質の判断はモデル。コードは形(検算・字数・素材の数値が本文に出たか)だけを数える
- `--record <ラベル>` で、合格した結果を bench/writer/pass-<執筆モデル>.json に残す。selfcheck はいまの執筆モデルの
  合格記録が無ければ赤(モデルを替えたらこの比較を通すまで発行の検査が通らない)
"""
import argparse
import concurrent.futures
import hashlib
import json
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import CODEX_WRITE_MODEL, ENV, ROOT, prompt_file, save_raw, schema_ok  # noqa: E402

AUDIT_MODEL = ENV.get("AUDIT_MODEL", "gpt-6.1-sol")      # 読み比べの判定(当番の監査と同じモデル)

CASES = ROOT / "bench" / "writer" / "cases"
PASS_DIR = ROOT / "bench" / "writer"
RESULTS = ROOT / "bench" / "writer" / "results"
OUT = ROOT / "metrics" / "work" / "bench"
SCHEMA_OUT = ROOT / "schema" / "article-out.schema.json"
PARALLEL = 6


def prompt_hash() -> str:
    h = hashlib.sha256()
    for p in sorted((ROOT / "prompts").glob("write-article*.md")):
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def make_case(ops: Path, date: str, slug: str) -> dict:
    """本番の依頼文の記録から、依頼文の変数を取り出す(依頼文を作り直して比べられるように)。"""
    text = (ops / "metrics" / "work" / date / f"write-{slug}-0.md").read_text(encoding="utf-8")
    plan = json.loads((ops / "metrics" / f"plan-{date}.json").read_text(encoding="utf-8"))
    art = next(a for a in plan["articles"] if a["slug"] == slug)
    grab = lambda pat: (re.search(pat, text, re.M) or [None, ""])[1]
    trig = re.search(r"^- 続報予約: (.*)$", text, re.M)
    story = text.split("## 既報(この話題で報道済みの事実)\n", 1)[1].split("\n\n## 素材", 1)[0].strip()
    mats = text.split("## 素材(この記事に使ってよい情報の全て)\n", 1)[1].strip()
    return {"date": date, "slug": slug, "rank": art["rank"], "angle": grab(r"^- 切り口: (.*)$"),
            "purpose": grab(r"^- 目的: (\S+?)\("), "trigger": trig.group(0) + "\n" if trig else "",
            "story_facts": story, "materials": json.loads(mats)}


def render(case: dict) -> str:
    import datetime
    import tags as tags_lib
    from pipelib import render_prompt
    rank_file = ROOT / "prompts" / f"write-article.{case['rank']}.md"
    return render_prompt(
        "write-article", DATE=case["date"], WEEKDAY="月火水木金土日"[datetime.date.fromisoformat(case["date"]).weekday()],
        ANGLE=case["angle"], PURPOSE=case["purpose"], TRIGGER=case["trigger"],
        RANK_RULES=rank_file.read_text(encoding="utf-8") if rank_file.exists() else "",
        TAG_VOCAB=tags_lib.vocabulary_block(), STORY_FACTS=case["story_facts"],
        MATERIALS=json.dumps(case["materials"], ensure_ascii=False, indent=2))


def write_one(name: str, short: str, label: str, model: str) -> dict:
    fd, out_name = tempfile.mkstemp(prefix=f"bench-{name}-", suffix=".txt")
    Path(out_name).unlink()
    cmd = ["codex", "exec", "-m", model, "-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true",
           "--output-last-message", out_name, "--output-schema", str(SCHEMA_OUT), short]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1500, stdin=subprocess.DEVNULL, cwd=ROOT)
        code, log = r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        code, log = None, "時間切れ"
    out = Path(out_name).read_text(encoding="utf-8") if Path(out_name).exists() else ""
    Path(out_name).unlink(missing_ok=True)
    save_raw("bench", f"{label}-{name}", "", log[-20000:], code, files={"last-message": out})
    try:
        ans = json.loads(out) if code == 0 else None
    except ValueError:
        ans = None
    if ans is not None and not schema_ok(ans, json.loads(SCHEMA_OUT.read_text(encoding="utf-8"))):
        ans = None
    return {"case": name, "answer": ans, "exit": code}


def body_text(ans: dict | None) -> str:
    if not ans or ans.get("status") != "ok":
        return ""
    return "\n\n".join(str(b.get("markdown") or "") for b in ans.get("blocks") or [])


def material_numbers(case: dict) -> set[str]:
    """素材の facts に出る数値の粒(価格・日付・時刻・個数)。本文に出たかを数えるための形の指標(内容の判断ではない)。"""
    import unicodedata
    text = " ".join((f.get("text") if isinstance(f, dict) else f) or "" for m in case["materials"] for f in (m.get("facts") or []))
    return set(re.findall(r"\d[\d,]*(?:円|日|時|分|個|種|枚|名|店|件|%|月)", unicodedata.normalize("NFKC", text)))


def metrics(case: dict, ans: dict | None) -> dict:
    import unicodedata
    body = unicodedata.normalize("NFKC", body_text(ans))
    nums = material_numbers(case)
    hit = {n for n in nums if n in body}
    # 本番と同じ形の検算(通らなければ本番では差し戻し・落とす記事になる)
    problems = []
    if ans:
        import renderlib
        fact_by_id = {f["id"]: f["text"] for m in case["materials"] for f in (m.get("facts") or []) if isinstance(f, dict)}
        try:
            problems = renderlib.check_output(ans, fact_by_id, case["materials"], rank=case["rank"], edition=case["date"])
        except Exception as e:      # noqa: BLE001
            problems = [f"検算できない({type(e).__name__})"]
    return {"status": (ans or {}).get("status") if ans else "unreadable", "chars": len(re.sub(r"\s", "", body)),
            "material_numbers": len(nums), "numbers_in_body": len(hit), "check_problems": problems[:5]}


def remeasure(label: str) -> None:
    """書かせ直さずに、残った答えから字数と数値の粒を数え直して表にする。"""
    cases = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES.glob("*.json"))}
    tot = [0, 0, 0]
    for n, c in cases.items():
        f = OUT / label / f"{n}.json"
        if not f.exists():
            continue
        m = metrics(c, json.loads(f.read_text(encoding="utf-8")).get("answer"))
        tot = [tot[0] + m["chars"], tot[1] + m["numbers_in_body"], tot[2] + m["material_numbers"]]
        print(f"{label} {n}: {m['status']} {m['chars']}字 数値 {m['numbers_in_body']}/{m['material_numbers']}"
              + (f" 検算 ✗ {m['check_problems']}" if m["check_problems"] else ""))
    print(f"{label} 計: {tot[0]}字 数値 {tot[1]}/{tot[2]}")


def run(label: str, model: str) -> None:
    cases = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES.glob("*.json"))}
    d = OUT / label
    d.mkdir(parents=True, exist_ok=True)
    # 依頼文は走らせる前に全部組み立てる(走っている間に依頼文を書き換えても、この回の比較が混ざらない)
    shorts = {n: prompt_file("bench", f"{label}-{n}", render(c)) for n, c in cases.items()}
    (d / "_meta.json").write_text(json.dumps({"model": model, "prompt_hash": prompt_hash()}, ensure_ascii=False), encoding="utf-8")
    with concurrent.futures.ThreadPoolExecutor(PARALLEL) as ex:
        for res in ex.map(lambda n: write_one(n, shorts[n], label, model), cases):
            res["metrics"] = metrics(cases[res["case"]], res["answer"])
            (d / f"{res['case']}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            m = res["metrics"]
            print(f"{res['case']}: {m['status']} {m['chars']}字 素材の数値 {m['numbers_in_body']}/{m['material_numbers']}", flush=True)


JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["better", "scores", "reason"],
    "properties": {
        "better": {"enum": ["X", "Y", "tie"]},
        "scores": {"type": "object", "additionalProperties": False, "required": ["X", "Y"],
                   "properties": {k: {"type": "object", "additionalProperties": False,
                                      "required": ["usefulness", "coverage", "accuracy", "readability"],
                                      "properties": {c: {"type": "integer", "minimum": 1, "maximum": 10}
                                                     for c in ("usefulness", "coverage", "accuracy", "readability")}}
                                  for k in ("X", "Y")}},
        "reason": {"type": "string", "maxLength": 800}}}


def judge_one(name: str, case: dict, a: dict | None, b: dict | None) -> dict:
    flip = random.Random(name).random() < 0.5
    x, y = (b, a) if flip else (a, b)
    show = lambda ans: json.dumps({k: (ans or {}).get(k) for k in ("status", "decline_code", "decline_detail", "title", "lede", "blocks",
                                                                    "new_facts", "sources")},
                                  ensure_ascii=False, indent=1) if ans else "(答えが無い・読めない)"
    prompt = ("アイマスNEWS(アイドルマスターのニュース紙面)の記事を2本読み比べ、読者にとってどちらが良い記事かを判定します。\n"
              f"この記事は {case['date']} 号(紙面が読者に届くのは {case['date']} 06:00)。本日・明日はこの日付が基準。\n"
              "記事の new_facts は、記者が出典のページ(url)を実際に読んで確かめて足した事実。素材に無くても new_facts にある具体は"
              "出典のある記述として扱う(必要なら url を `python3 scripts/fetch_page.py <url>` で読んで確かめてよい)。"
              "素材にも new_facts にも無い具体だけを accuracy の減点にする。\n"
              "見ること: usefulness = 読者が出典を開かずに、何が起きたか・自分が何をできるか(申込・購入・視聴・来場)を理解し行動できるか / "
              "coverage = 素材にある今日の話題の具体(価格・日程・時刻・会場・品目・条件・方法)を落とさず書いているか / "
              "accuracy = 素材に無いこと・素材と食い違うことを書いていないか(推測・誇張) / readability = 新聞記事として読みやすいか。\n"
              "既報を記事の軸にしないことは正しいが、読者が今日行動するのに要る具体まで落とすのは減点。長さそのものは評価しない。\n"
              "見送り(decline)は、素材から記事が成り立たないときだけ正しい。\n\n"
              f"## 素材\n{json.dumps(case['materials'], ensure_ascii=False, indent=1)}\n\n## 既報\n{case['story_facts']}\n\n"
              f"## 記事X\n{show(x)}\n\n## 記事Y\n{show(y)}\n")
    fd, out_name = tempfile.mkstemp(prefix=f"judge-{name}-", suffix=".json")
    sf = Path(tempfile.mkstemp(prefix="judge-schema-", suffix=".json")[1])
    sf.write_text(json.dumps(JUDGE_SCHEMA, ensure_ascii=False), encoding="utf-8")
    Path(out_name).unlink()
    # 判定役も new_facts の url を読んで確かめられるよう、通信を許す(書き込みは作業用の記録だけ)
    r = subprocess.run(["codex", "exec", "-m", AUDIT_MODEL, "-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=true",
                        "--skip-git-repo-check",
                        "--output-schema", str(sf), "-o", out_name, prompt_file("bench", f"judge-{name}", prompt)],
                       capture_output=True, text=True, timeout=1200, stdin=subprocess.DEVNULL, cwd=ROOT)
    sf.unlink(missing_ok=True)
    try:
        v = json.loads(Path(out_name).read_text(encoding="utf-8")) if r.returncode == 0 else None
    except (OSError, ValueError):
        v = None
    Path(out_name).unlink(missing_ok=True)
    if not v or not schema_ok(v, JUDGE_SCHEMA):
        return {"case": name, "winner": None}
    unflip = {"X": "B" if flip else "A", "Y": "A" if flip else "B", "tie": "tie"}
    return {"case": name, "winner": unflip[v["better"]],
            "A": v["scores"]["Y" if flip else "X"], "B": v["scores"]["X" if flip else "Y"], "reason": v["reason"]}


def label_dir(label: str) -> Path:
    """`rec:<モデル>` は、前に合格して保存した記事(bench/writer/results/<モデル>/)。それ以外は今回書かせた答え。"""
    return RESULTS / label[4:] if label.startswith("rec:") else OUT / label


def judge(la: str, lb: str) -> dict:
    cases = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES.glob("*.json"))}
    load = lambda lab, n: json.loads((label_dir(lab) / f"{n}.json").read_text(encoding="utf-8")).get("answer")
    with concurrent.futures.ThreadPoolExecutor(PARALLEL) as ex:
        res = list(ex.map(lambda n: judge_one(n, cases[n], load(la, n), load(lb, n)), cases))
    wins = {k: sum(1 for r in res if r["winner"] == k) for k in ("A", "B", "tie")}
    acc = lambda k: sum(r[k]["accuracy"] for r in res if r.get(k)) / max(1, sum(1 for r in res if r.get(k)))
    summary = {"A": la, "B": lb, "wins": wins, "unjudged": sum(1 for r in res if r["winner"] is None),
               "accuracy_avg": {"A": round(acc("A"), 2), "B": round(acc("B"), 2)}, "cases": res}
    (OUT / f"judge-{la.replace(':', '_')}-vs-{lb}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in res:
        print(f"{r['case']}: {r['winner']} {r.get('reason', '')[:160]}", flush=True)
    print(f"勝ち {wins} / 判定不能 {summary['unjudged']} / 正確さ平均 {summary['accuracy_avg']}", flush=True)
    return summary


def record(label: str, judge_file: str) -> None:
    """合格の記録: 新しい側(B)が、比べた側(A)に負け越さず、正確さの平均が下がっていないこと。"""
    meta = json.loads((OUT / label / "_meta.json").read_text(encoding="utf-8"))
    j = json.loads(Path(judge_file).read_text(encoding="utf-8"))
    if j["B"] != label:
        sys.exit("judge の B が記録するラベルと違う")
    ok = j["wins"]["B"] >= j["wins"]["A"] and j["accuracy_avg"]["B"] >= j["accuracy_avg"]["A"] - 0.3 and j["unjudged"] == 0
    if not ok:
        sys.exit(f"合格しない: {j['wins']} 正確さ {j['accuracy_avg']} 判定不能 {j['unjudged']}")
    p = PASS_DIR / f"pass-{meta['model']}.json"
    p.write_text(json.dumps({"model": meta["model"], "prompt_hash": meta["prompt_hash"], "compared_with": j["A"],
                             "wins": j["wins"], "accuracy_avg": j["accuracy_avg"],
                             "cases": sorted(c["case"] for c in j["cases"])}, ensure_ascii=False, indent=1) + "\n",
                 encoding="utf-8")
    # 合格した記事を保存する(次にモデルを変えたとき、旧モデルが使えなくても `rec:<モデル>` として読み比べられる)
    dst = RESULTS / meta["model"]
    dst.mkdir(parents=True, exist_ok=True)
    for f in (OUT / label).glob("*.json"):
        (dst / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"記録した: {p.relative_to(ROOT)} と {dst.relative_to(ROOT)}/")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--make", nargs="+", metavar=("DATE", "SLUG"))
    ap.add_argument("--ops", default=str(Path.home() / "git" / "imas-ops"))
    ap.add_argument("--run", metavar="LABEL")
    ap.add_argument("--model", default=CODEX_WRITE_MODEL)
    ap.add_argument("--judge", nargs=2, metavar=("LABEL_A", "LABEL_B"))
    ap.add_argument("--record", nargs=2, metavar=("LABEL", "JUDGE_FILE"))
    ap.add_argument("--measure", nargs="+", metavar="LABEL")
    a = ap.parse_args()
    for lab in a.measure or []:
        remeasure(lab)
    if a.make:
        CASES.mkdir(parents=True, exist_ok=True)
        date, slugs = a.make[0], a.make[1:]
        for s in slugs:
            (CASES / f"{s}.json").write_text(json.dumps(make_case(Path(a.ops), date, s), ensure_ascii=False, indent=1) + "\n",
                                             encoding="utf-8")
            print(f"ケース: {s}")
    if a.run:
        run(a.run, a.model)
    if a.judge:
        judge(*a.judge)
    if a.record:
        record(*a.record)
    return 0


if __name__ == "__main__":
    sys.exit(main())
