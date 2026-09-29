#!/usr/bin/env python3
"""withdraw: 発行済みの記事を**取り下げる**(紙面から外す)。編集長の判断で使う。

  python3 scripts/withdraw.py --post 2026-09-29-<slug> --by 編集長 --reason "…" [--lead <slug>] [--at YYYY-MM-DD]

第1号以降は append-only で、記事は訂正ボックスで直して残すのが原則。ただし「アイマスに関係しない情報だけの記事」のように
残す意味が無いものは、編集長の判断で記事ごと外す(編集長 2026-09-29)。手で消すと、目次の行・一面・号の記事数・
その記事が作った未来の続報予約が取り残されるので、ここで1回に行う:

1. 記事ファイルを消す
2. withdrawn.yml の末尾に記録(post・at・by・reason)を足す。lint はこの記録を条件に削除を認める
3. 号スナップショット: 目次(digest)からその記事の行を除き、記事数・面数・訂正数を数え直す。本文は変えない
4. 一面を外したときは、残りの記事から一面を立て直す(--lead があればそれ、無ければ計画の lead_score 最大。
   roundup・culture は除く。組版と同じ規則)。立て直した記事は rank の行だけを lead にする
5. その記事が作った未来の続報予約(stock/scheduled。予約した日がその号で、元の候補がその記事の候補)を消す

git は触らない(commit と push は人がする。そのあと lint --base origin/main が通ることを確かめる)。
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
NON_LEAD = ("roundup", "culture")          # 一面に立てない段(compose.pick_lead と同じ)
RANK_ORDER = {"lead": 0, "large": 1, "medium": 2, "small": 3}
HEADER = ("# 取り下げた記事の記録(第1号以降は append-only。取り下げだけは、この記録を条件に記事の削除を認める)。\n"
          "# post: docs/_posts のファイル名(拡張子なし) / at: 取り下げた日 / by: 判断した人 / reason: 理由。末尾への追記だけ\n")


def split_doc(text: str) -> tuple[dict, str]:
    """frontmatter(dict)と、閉じの `\\n---` より後ろの生の文字列(本文。lint と同じ切り方)を返す。"""
    head, sep, rest = text.partition("\n---")
    if not text.startswith("---") or not sep:
        raise ValueError("frontmatter が読めない")
    return yaml.safe_load(head[3:]) or {}, rest


def pick_lead(date: str, posts: dict[str, dict], root: Path) -> str:
    """残りの記事から一面を選ぶ: 計画の lead_score 最大(同点は段の順・slug 順)。roundup・culture は一面に立てない。
    立てられる記事が無ければ止める(段を lead で上書きすると roundup・culture だったことが失われる。監査指摘 r98)。"""
    scores = {}
    plan = root / "metrics" / f"plan-{date}.json"
    if plan.exists():
        try:
            scores = {a.get("slug"): a.get("lead_score") or 0 for a in json.loads(plan.read_text(encoding="utf-8")).get("articles") or []}
        except (ValueError, AttributeError):
            scores = {}
    cand = [s for s, fm in posts.items() if fm.get("rank") not in NON_LEAD]
    if not cand:
        raise SystemExit("一面に立てられる記事(roundup・culture 以外)が残らない。この取り下げは道具ではできない(編集長の判断が要る)")
    return min(cand, key=lambda s: (-(scores.get(s) or 0), RANK_ORDER.get(posts[s].get("rank"), 9), s))


def rank_lead_text(text: str, name: str) -> str:
    """記事の frontmatter の rank の行だけを lead にした全文(ほかの行・本文は1字も変えない)。"""
    head, sep, rest = text.partition("\n---")
    new_head, n = re.subn(r"(?m)^rank: .*$", "rank: lead", head, count=1)
    if n != 1:
        raise SystemExit(f"{name}: rank の行が見つからない")
    return new_head + sep + rest


def plan_reservations(date: str, candidate_ids: set[str], root: Path) -> tuple[dict[Path, list | None], list[str]]:
    """その記事が作った未来の続報予約を除いた、書き直す予約ファイルの中身(None は消す)と、除く予約の id。"""
    writes, removed = {}, []
    for p in sorted((root / "stock" / "scheduled").glob("*.json")):
        if p.stem <= date:
            continue
        rows = json.loads(p.read_text(encoding="utf-8"))
        keep = [r for r in rows if not (r.get("reserved_on") == date and r.get("src_candidate_id") in candidate_ids)]
        if len(keep) != len(rows):
            removed += [r.get("id", "?") for r in rows if r not in keep]
            writes[p] = keep or None
    return writes, removed


def withdraw(stem: str, by: str, reason: str, at: str, lead: str = "", root: Path = ROOT) -> list[str]:
    """取り下げを作業ツリーに反映する。戻り値は人に見せる変更の一覧。
    **先に全部を読んで検め、変更を組み立ててから書く**(入力の誤りで、記事だけ消えて号が古いまま、を残さない。監査指摘 r98)。"""
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})-([a-z0-9][a-z0-9-]*)", stem)
    if not m:
        raise SystemExit(f"--post はファイル名(拡張子なし)で: {stem}")
    date, slug = m.group(1), m.group(2)
    posts_dir = root / "docs" / "_posts"
    post = posts_dir / f"{stem}.md"
    ed_path = root / "docs" / "_editions" / f"{date}.md"
    if not post.exists() or not ed_path.exists():
        raise SystemExit(f"記事か号が無い: {post.name} / {ed_path.name}")
    if not (by.strip() and reason.strip()):
        raise SystemExit("--by と --reason は空にできない")
    datetime.date.fromisoformat(at)
    post_fm, _ = split_doc(post.read_text(encoding="utf-8"))
    ed_fm, ed_rest = split_doc(ed_path.read_text(encoding="utf-8"))
    wd = root / "withdrawn.yml"
    done, writes = [f"記事を消す: {post.relative_to(root)}"], {}

    # 記録(末尾に足す)
    prefix = wd.read_text(encoding="utf-8") if wd.exists() else HEADER
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    writes[wd] = prefix + yaml.safe_dump([{"post": stem, "at": at, "by": by, "reason": reason}],
                                         allow_unicode=True, sort_keys=False, width=1000)
    # 書く前に、足したあとの記録を lint と同じ関数で読み直す。読めない・形式が違う・末尾に足した形になっていないなら何も変えずに止める
    # (元の記録が壊れている・`[]` や `...` で終わっている、など。監査指摘 r100)
    from lint import load_withdrawn
    old_rows, old_errs = load_withdrawn(prefix)
    new_rows, new_errs = load_withdrawn(writes[wd])
    if old_errs or new_errs or new_rows[:len(old_rows)] != old_rows or len(new_rows) != len(old_rows) + 1 \
            or new_rows[-1].get("post") != stem:
        raise SystemExit(f"withdrawn.yml に記録を正しく足せない(元の記録の形式を確かめること): {(old_errs or new_errs or ['末尾に足した形にならない'])[0]}")
    done.append("withdrawn.yml に記録を足す")

    # 号スナップショット(目次の行・一面・数)
    rest = {p.stem[11:]: split_doc(p.read_text(encoding="utf-8"))[0]
            for p in sorted(posts_dir.glob(f"{date}-*.md")) if p != post}
    n_rows = sum(1 for g in ed_fm.get("digest") or [] for r in g.get("rows") or [] if r.get("slug") == slug)
    for g in ed_fm.get("digest") or []:
        g["rows"] = [r for r in g.get("rows") or [] if r.get("slug") != slug]
    if n_rows:
        done.append(f"目次から {n_rows}行を除く")
    if ed_fm.get("lead_slug") == slug:
        new_lead = lead or pick_lead(date, rest, root)
        if new_lead not in rest or rest[new_lead].get("rank") in NON_LEAD:
            raise SystemExit(f"--lead {new_lead} は一面に立てられない(この号に無いか、roundup・culture)")
        lead_path = posts_dir / f"{date}-{new_lead}.md"
        writes[lead_path] = rank_lead_text(lead_path.read_text(encoding="utf-8"), lead_path.name)
        rest[new_lead]["rank"] = "lead"
        ed_fm["lead_slug"] = new_lead
        done.append(f"一面を {new_lead} に立て直す(rank の行だけ lead に)")
    elif lead:
        raise SystemExit("--lead は一面を取り下げるときだけ使う")
    ed_fm["article_count"] = len(rest)
    ed_fm["pages"] = len({fm.get("brand") for fm in rest.values()})
    ed_fm["corrected_count"] = sum(1 for fm in rest.values() if fm.get("corrected"))
    writes[ed_path] = ("---\n" + yaml.safe_dump(ed_fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
                       + "---" + ed_rest)
    done.append(f"号の記事数 {ed_fm['article_count']}・面数 {ed_fm['pages']}")

    # 未来の続報予約(読めない予約ファイルがあれば、ここで止まる。まだ何も書いていない)
    sched, gone = plan_reservations(date, set(post_fm.get("candidate_ids") or []), root)
    if gone:
        done.append("未来の続報予約を消す: " + ", ".join(gone))

    # ここまで検めてから書く
    for p, text in writes.items():
        p.write_text(text, encoding="utf-8")
    for p, rows in sched.items():
        if rows is None:
            p.unlink()
        else:
            p.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")   # 組版と同じ書き方
    post.unlink()
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", required=True, help="docs/_posts のファイル名(拡張子なし)")
    ap.add_argument("--by", required=True, help="判断した人")
    ap.add_argument("--reason", required=True, help="取り下げる理由")
    ap.add_argument("--at", default=datetime.date.today().isoformat(), help="取り下げた日(既定: 今日)")
    ap.add_argument("--lead", default="", help="一面を取り下げるとき、代わりに一面に立てる記事の slug(既定: lead_score 最大)")
    a = ap.parse_args()
    for line in withdraw(a.post, a.by, a.reason, a.at, a.lead):
        print(line)
    print("次: git add -A docs stock withdrawn.yml → commit → python3 scripts/lint.py --base origin/main が 0 errors であること")
    return 0


if __name__ == "__main__":
    sys.exit(main())
