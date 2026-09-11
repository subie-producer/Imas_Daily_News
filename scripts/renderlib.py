"""renderlib: 執筆セッションの構造化出力 → 記事ファイル。構造は**生成時に**強制する。

以前の執筆は Markdown ファイルを自由に書き、frontmatter の固定項目・日付の形・出典の
種別を**事後に**コードや別セッションで直していた(監査の設計レビュー: 構造制御と
校閲の往復が混ざっている)。ここでは:

- 執筆セッションは判断と文章だけを JSON で返す(schema/article-out.schema.json)。
  段落ごとに根拠の事実 id(F1…)を付ける
- コードが frontmatter を作る。slug/edition/brand/candidate_ids は計画、出典の種別は判定表、
  src は最弱、rank は仮、訂正欄は固定値
- 事実 id・出典・タグ・日付はここで検算し、通らないものは記事を不成立(abort)にする。
  lint は最後の不変条件の検査で、赤ならバグ
"""
import datetime
import re
from pathlib import Path

import yaml

ABORT_CODES = ("NO_PRIMARY_SOURCE", "SOURCE_MISMATCH", "TOO_FEW_MATERIALS", "NOT_NEWS", "OTHER")


def materials_with_ids(materials: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """素材の facts に F1, F2 … を振って、執筆に渡す形にする。戻りは (素材, {id: 事実})。"""
    out, fact_by_id = [], {}
    n = 0
    for c in materials:
        facts = []
        for f in c.get("facts") or []:
            n += 1
            fid = f"F{n}"
            fact_by_id[fid] = str(f)
            facts.append({"id": fid, "text": str(f)})
        row = {k: c.get(k) for k in ("id", "title", "url", "source_type", "published_date", "event_date",
                                     "deadline", "verify", "via") if c.get(k) not in (None, "")}
        row["facts"] = facts
        out.append(row)
    return out, fact_by_id


def check_output(out: dict, fact_by_id: dict[str, str], materials: list[dict]) -> list[str]:
    """出力の中身の検算(schema は形しか見ない)。通らない理由を返す(空なら合格)。"""
    problems = []
    if out.get("status") == "abort":
        return []  # abort は理由付きの正当な答え
    known_urls = {c.get("url") for c in materials if c.get("url")}
    if not out.get("title", "").strip() or not out.get("lede", "").strip():
        problems.append("見出しかリードが空")
    if not out.get("blocks"):
        problems.append("本文が空")
    if not out.get("sources"):
        problems.append("出典が無い")
    bad_ids = [i for key in ("title_fact_ids", "lede_fact_ids") for i in (out.get(key) or []) if i not in fact_by_id]
    bad_ids += [i for b in (out.get("blocks") or []) for i in (b.get("fact_ids") or []) if i not in fact_by_id]
    if bad_ids:
        problems.append(f"無い事実 id: {sorted(set(bad_ids))[:6]}")
    unsupported = [i for i, b in enumerate(out.get("blocks") or []) if not (b.get("fact_ids") or [])]
    if unsupported:
        problems.append(f"根拠の事実 id が無い段落: {unsupported[:6]}")
    for s in out.get("sources") or []:
        u = s.get("url") or ""
        if not re.match(r"^https?://", u):
            problems.append(f"出典 url の形が不正: {u[:60]}")
        if re.search(r"[\[\]*_`#]", s.get("label") or ""):
            problems.append(f"出典 label に Markdown 記号: {s.get('label')!r}")
    for t in out.get("tags") or []:
        if re.search(r"[\[\]*_`#\n]", t):
            problems.append(f"tag に記号: {t!r}")
    if out.get("event_date"):
        try:
            datetime.date.fromisoformat(out["event_date"])
        except ValueError:
            problems.append(f"event_date が日付でない: {out['event_date']!r}")
    return problems


def render_article(path: Path, date: str, art: dict, out: dict, source_type_of, weakest_src,
                   dump_yaml) -> None:
    """検算済みの出力から記事ファイルを作る。"""
    sources = []
    for s in out.get("sources") or []:
        sources.append({"label": str(s.get("label") or "")[:80], "url": s["url"], "type": source_type_of(s["url"])})
    fm = {
        "slug": art["slug"], "edition": date, "brand": art["brand"],
        "src": weakest_src(s["type"] for s in sources) if sources else "未確認",
        "rank": art.get("rank") or "small", "corrected": False, "corrections": [],
        "candidate_ids": list(art["candidate_ids"]),
        "title": out["title"].strip(), "lede": out["lede"].strip(),
        "tags": [str(t).strip() for t in (out.get("tags") or []) if str(t).strip()],
        "sources": sources,
    }
    if out.get("event_date"):
        fm["event_date"] = out["event_date"]
    body = "\n\n".join(b["markdown"].strip() for b in out.get("blocks") or [] if b.get("markdown", "").strip())
    path.write_text("---\n" + dump_yaml(fm) + "---\n" + body + "\n", encoding="utf-8")
