#!/usr/bin/env python3
"""derive: 号スナップショットの機械算出フィールドを記事群から導出する。(REQUIREMENTS 3.3)

  python3 scripts/derive.py --date YYYY-MM-DD [--write]

- pages / article_count / corrected_count: 記事群から再計算
- birthdays: 名鑑(docs/_data/idols.json)から機械抽出
- ranking: スコア = 紙面(title+lede+本文+digest)の名鑑アイドル登場回数×1.0
           + 当日 candidates の探索(explore)由来エントリでの言及回数×0.5(トレンド分)
  上位8名。同点は前号順位優先、次いで名鑑順(characterId)。delta は前号比。
  ※重み(1.0/0.5)は暫定。第0号期間の実測で確定する(REQUIREMENTS 8章)。
--write で docs/_editions/<date>.md の該当フィールドを上書きする(digest 等は保持)。
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import ROOT

WEIGHT_PAPER = 1.0
WEIGHT_TREND = 0.5


def parse_frontmatter(path: Path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", text, re.DOTALL)
    return yaml.safe_load(m.group(1)), m.group(2)


def norm_dates(v):
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: norm_dates(x) for k, x in v.items()}
    if isinstance(v, list):
        return [norm_dates(x) for x in v]
    return v


def compute(date: str) -> dict:
    idols = json.loads((ROOT / "docs" / "_data" / "idols.json").read_text(encoding="utf-8"))
    posts = []
    for p in sorted((ROOT / "docs" / "_posts").glob(f"{date}-*.md")):
        fm, body = parse_frontmatter(p)
        posts.append((norm_dates(fm), body))

    ed_path = ROOT / "docs" / "_editions" / f"{date}.md"
    ed_fm, _ = parse_frontmatter(ed_path)
    ed_fm = norm_dates(ed_fm)

    # 紙面テキスト(title+lede+本文+digest)
    paper_text = ""
    for fm, body in posts:
        paper_text += fm.get("title", "") + fm.get("lede", "") + body
    for g in ed_fm.get("digest", []):
        for r in g.get("rows", []):
            paper_text += r.get("t", "") + r.get("d", "")

    # トレンドテキスト(当日までの candidates の explore 由来)
    trend_text = ""
    for cf in sorted((ROOT / "candidates").glob("*.json")):
        try:
            for c in json.loads(cf.read_text(encoding="utf-8")):
                if c.get("origin") == "explore":
                    trend_text += c.get("title", "") + " ".join(c.get("facts", []))
        except Exception:
            continue

    # スコアリング
    scores = {}
    for idol in idols:
        name = idol["name"]
        s = paper_text.count(name) * WEIGHT_PAPER + trend_text.count(name) * WEIGHT_TREND
        if s > 0:
            scores[name] = (s, idol)

    # 前号(日付順で直前の号)
    prev_rank = {}
    editions = sorted((ROOT / "docs" / "_editions").glob("*.md"))
    dates = [e.stem for e in editions if e.stem < date]
    if dates:
        pfm, _ = parse_frontmatter(ROOT / "docs" / "_editions" / f"{dates[-1]}.md")
        pfm = norm_dates(pfm)
        prev_rank = {r["name"]: r["n"] for r in pfm.get("ranking", [])}

    def sort_key(item):
        name, (s, idol) = item
        return (-s, prev_rank.get(name, 99), idol["characterId"])

    top = sorted(scores.items(), key=sort_key)[:8]
    ranking = []
    for i, (name, (s, idol)) in enumerate(top):
        if name in prev_rank:
            pn = prev_rank[name]
            delta = "up" if i + 1 < pn else ("down" if i + 1 > pn else "stay")
        else:
            delta = "new"
        ranking.append({"n": i + 1, "name": name, "brand": idol["brand"], "delta": delta})

    mmdd = f"{date[5:7]}-{date[8:10]}"
    birthdays = [{"name": i["name"], "brand": i["brand"]} for i in idols if i["birthday"] == mmdd]

    derived = {
        "pages": len({fm["brand"] for fm, _ in posts}),
        "article_count": len(posts),
        "corrected_count": sum(1 for fm, _ in posts if fm.get("corrected")),
        "ranking": ranking,
        "birthdays": birthdays,
        "_scores": {n: s for n, (s, _) in sorted(scores.items(), key=sort_key)[:16]},
    }
    return derived, ed_fm, ed_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    derived, ed_fm, ed_path = compute(args.date)
    print(json.dumps({k: v for k, v in derived.items()}, ensure_ascii=False, indent=1))
    if not args.write:
        return 0
    # birthdays: note(ひとこと)は LLM 側の責務なので、既存 note を name 一致で温存する
    old_notes = {b["name"]: b.get("note") for b in ed_fm.get("birthdays", []) if b.get("note")}
    for b in derived["birthdays"]:
        if b["name"] in old_notes:
            b["note"] = old_notes[b["name"]]
    for k in ("pages", "article_count", "corrected_count", "ranking", "birthdays"):
        ed_fm[k] = derived[k]
    _, body = parse_frontmatter(ed_path)
    fmtext = yaml.safe_dump(ed_fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    ed_path.write_text(f"---\n{fmtext}---\n{body}", encoding="utf-8")
    print(f"updated {ed_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
