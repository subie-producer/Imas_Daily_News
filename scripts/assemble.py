#!/usr/bin/env python3
"""assemble: 組版。判断は小さな構造化セッション、反映はコード。

  python3 scripts/assemble.py --date YYYY-MM-DD [--number N] [--dry-run]

以前の組版は Claude の1セッションに「号スナップショットの digest を組み、既報台帳に
事実を追記し、続報予約を作り、pending を整え、lint が通るまで自分で直す」を丸ごと
やらせていた。実測 2026-09-11: 40本で 24.7分、ツール呼び出し 96回(シェル 63・うち
即席 python 24、記事ファイルの編集 11、lint 3回)。判断が要るのは digest の12行と
「どの事実を既報として残すか」「どの日に続報を予約するか」だけで、その入力は見出し・
リード・面・日付付きの事実、40行分で足りる。残りは frontmatter と候補から決定的に出せる。

この設計では:

- コードが**入力を圧縮して**渡す(記事1本 = 数行)
- セッションは**判断だけを JSON で返す**(schema/assemble.schema.json)。ファイルに触らない
- コードが**反映する**。号スナップショット・stock/stories.yml・stock/scheduled/・stock/pending.yml
- 反映は**冪等**。この号の寄与(台帳の事実・予約)には号の印を付け、再実行時はまず
  それを剥がしてから付け直す。記事が落ちても増えても、もう一度走らせるだけでよい。
  組版前へ巻き戻す・成果物から抜く、という手順は要らない
- 校閲が既報判定に使う**組版前の台帳**(metrics/stories-before-<日付>.yml)もここで書く
  (この号の寄与を剥がした状態がそれである)
- lint はコードが回す。赤なら**組版の欠陥**なので直さずに報告する
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import renderlib
from pipelib import (ROOT, CLAUDE_MODEL, COMPOSE_ARTICLE_MAX_BUDGET_USD, classify_source,
                     extract_json_array, notify, prompt_file)

POSTS = ROOT / "docs" / "_posts"
EDITIONS = ROOT / "docs" / "_editions"
STORIES = ROOT / "stock" / "stories.yml"
SCHEDULED = ROOT / "stock" / "scheduled"
PENDING = ROOT / "stock" / "pending.yml"
BRANDS = ("general", "765", "cg", "million", "shiny", "sidem", "gaku", "dsva", "joint", "other")
KINDS = ("締切前", "締切", "開幕", "千秋楽", "発売", "結果", "終了", "開始", "その他")
K_VOCAB = "誕開終配売受締発演"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2}")
FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)


# ---------------------------------------------------------------- 入力
def load_posts(date: str) -> list[dict]:
    out = []
    for p in sorted(POSTS.glob(f"{date}-*.md")):
        m = FM_RE.match(p.read_text(encoding="utf-8"))
        if not m:
            continue
        fm = yaml.safe_load(m.group(1)) or {}
        fm["_name"] = p.name
        out.append(fm)
    return out


def load_materials(date: str) -> dict[str, dict]:
    """候補と、その日の続報予約(素材スナップショット)を id で引けるようにする。"""
    mats: dict[str, dict] = {}
    p = ROOT / "candidates" / f"{date}.json"
    if p.exists():
        for c in json.loads(p.read_text(encoding="utf-8")):
            mats[c["id"]] = c
    q = SCHEDULED / f"{date}.json"
    if q.exists():
        for s in json.loads(q.read_text(encoding="utf-8")):
            mats.setdefault(s["id"], {**s, "origin": "scheduled"})
    return mats


def load_yaml_list(p: Path) -> list:
    if not p.exists():
        return []
    return yaml.safe_load(p.read_text(encoding="utf-8")) or []


def dump_yaml(p: Path, data, header: str = "") -> None:
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=200)
    p.write_text((header + "\n" if header else "") + text, encoding="utf-8")


def primary_key(fm: dict, mats: dict) -> str:
    """記事の主題キー。素材の dedup_key の最頻値。無ければ slug。"""
    keys = [mats[i].get("dedup_key") for i in fm.get("candidate_ids") or [] if i in mats and mats[i].get("dedup_key")]
    if not keys:
        return fm.get("slug", "")
    # 最頻値。同数なら candidate_ids の並びで先に出たものを採る(set の反復順は
    # プロセスごとに変わり、再実行で story_id が変わっていた。監査指摘 P0-3)
    order = {k: i for i, k in reversed(list(enumerate(keys)))}
    return max(keys, key=lambda k: (keys.count(k), -order[k], k))


def build_input(date: str, posts: list[dict], mats: dict, stories: list[dict], pending: list[dict]) -> dict:
    by_id = {e.get("story_id"): e for e in stories}
    arts = []
    for fm in posts:
        ids = fm.get("candidate_ids") or []
        key = primary_key(fm, mats)
        # 事実には id を振る(F1, F2, …)。モデルは既報にする事実を id で指せるので、
        # 言い換えの検算で正しい事実を落とすことが無い(監査指摘)。日付付きを先に、
        # 価格・出演者など日付の無い事実も少し渡す(台帳に残せるように)
        raw = []
        for i in ids:
            for f in (mats.get(i) or {}).get("facts") or []:
                if f not in raw:
                    raw.append(f)
        dated = [f for f in raw if DATE_RE.search(f)]
        undated = [f for f in raw if not DATE_RE.search(f)]
        facts = [{"id": f"F{n + 1}", "text": f[:140]} for n, f in enumerate((dated[:6] + undated)[:9])]
        exist = by_id.get(key)
        arts.append({
            "slug": fm.get("slug"), "brand": fm.get("brand"), "rank": fm.get("rank"),
            "title": fm.get("title"), "lede": fm.get("lede"),
            "event_date": str(fm.get("event_date") or ""),
            "dedup_key": key, "candidate_ids": ids,
            "existing_story": ({"story_id": exist.get("story_id"), "subject": exist.get("subject"),
                                "known_facts": (exist.get("published_facts") or [])[-3:]} if exist else None),
            "facts": facts,
        })
    d = datetime.date.fromisoformat(date)
    tomorrow = (d + datetime.timedelta(days=1)).isoformat()
    tq = SCHEDULED / f"{tomorrow}.json"
    tomorrow_rows = [{"subject": s.get("subject"), "kind": s.get("kind"), "brand": s.get("brand")}
                     for s in (json.loads(tq.read_text(encoding="utf-8")) if tq.exists() else [])][:8]
    return {"date": date, "articles": arts, "tomorrow_reservations": tomorrow_rows,
            "pending": [{"dedup_key": x.get("dedup_key"), "subject": x.get("subject"), "watch": x.get("watch")}
                        for x in pending]}


# ---------------------------------------------------------------- 判断
def prompt(date: str, inp: dict) -> str:
    weekday = "月火水木金土日"[datetime.date.fromisoformat(date).weekday()]
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の組版担当です。{date}({weekday}曜)号の記事一覧を渡します。
**ファイルは読まず、書かず**、下の入力だけから判断を JSON で返してください。反映はプログラムが行います。

## 入力
```json
{json.dumps(inp, ensure_ascii=False, indent=1)}
```

## 返すもの

### digest(「本日の紙面」。4群固定・この順・合計12行以内)
- 本日: 発行日に起きること・発表されたこと / 昨日: 前日に起きて今日伝えること /
  継続中: 開催中・受付中のもの / 明日: 翌日に起きること(記事か tomorrow_reservations から)
- 各群3行を目安、最大4行。**記事の本数がいくつでも12行**(スマートフォン1画面の制約)。
  載せられない記事があるのは正常で、その日いちばん大きい話題から選ぶ
- k は {K_VOCAB} の1文字(誕=誕生日 開=開幕・開始 終=終了・千秋楽 配=配信 売=発売 受=受注・受付 締=締切 発=発表 演=出演)
- t は20字以内、d は25字以内。**入力にある事実だけ**を使う。slug は入力の記事のもの(記事が無い行は "")
- 一面(rank: lead)の記事は必ずどこかの群に入れる

### stories(既報台帳に残す事実。記事ごとに1件)
- story_id: existing_story があればその story_id、無ければ dedup_key
- subject: 話題の件名(60字以内)。既存があれば同じでよい
- published_facts: この記事が伝えた事実を1〜4件。**入力の facts の id(例 "F2")をそのまま書く**のが基本。
  facts に無く title/lede にだけある事実は、その文をほぼそのまま短く書く(140字以内)。
  既存の known_facts と同じ内容は繰り返さない。推測・言い換えの水増しをしない

### reservations(続報予約。**その日、読者が何かを見に行ける・できる日**だけ)
- 予約してよい: 締切(その3日前も「締切前」で)・開幕・千秋楽・発売・発表される結果・開始・終了
- 予約してはいけない: その日に何も起きない名目上の日付(在籍最終日、契約上の区切り)、
  もう終わっている催しの後日の日付、素材に日付が書かれていないもの
- **紙面が読者に届くのは 06:00**。締切・終了が当日 06:00 より前(ゲームの締切は 4:59 が定番)なら**前日**に予約する
- date は {date} より後。candidate_id はその記事の candidate_ids から(素材スナップショットの元になる)
- 迷ったら「その日、読者は何を見に行けるか・何ができるか」を note に一言で書けるか試す。書けなければ予約しない

### pending(日付未確定の追跡)
- pending_add: 記事で「後日発表」「詳細は追って」とされた事項(dedup_key・brand・subject・watch)
- pending_remove: 入力の pending のうち、この号の記事で日付が判明した・決着した dedup_key
"""


def run_session(text: str, date: str = "") -> dict:
    schema = (ROOT / "schema" / "assemble.schema.json").read_text(encoding="utf-8")
    # 入力(全記事の事実・台帳・予約)はファイルで渡す(引数に詰めると 128KB で落ちる)
    r = subprocess.run(["claude", "-p", prompt_file(date or "assemble", "assemble", text), "--model", CLAUDE_MODEL, "--json-schema", schema,
                        "--dangerously-skip-permissions", "--max-budget-usd", COMPOSE_ARTICLE_MAX_BUDGET_USD],
                       capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL, cwd=ROOT)
    out = (r.stdout or "").strip()
    try:
        return json.loads(out)
    except Exception:
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    raise RuntimeError(f"組版セッションの出力が読めない: {(r.stderr or out)[-300:]}")


# ---------------------------------------------------------------- 検証
def _date_forms(iso: str) -> list[str]:
    """2026-09-12 → その日付の書き方いろいろ(素材の facts と照合するため)。"""
    try:
        d = datetime.date.fromisoformat(iso)
    except ValueError:
        return []
    return [iso, f"{d.month}月{d.day}日", f"{d.month:02d}月{d.day:02d}日", f"{d.month}/{d.day}",
            f"{d.month:02d}/{d.day:02d}", f"{d.year}/{d.month}/{d.day}", f"{d.year}年{d.month}月{d.day}日",
            f"{d.year}.{d.month}.{d.day}", f"{d.month}.{d.day}", f"{d.month}-{d.day}", f"{d.year}-{d.month}-{d.day}"]


_ZEN = str.maketrans("０１２３４５６７８９／．－", "0123456789/.-")


def _norm(text: str) -> str:
    """全角数字・記号を半角にする(素材の日付照合用。監査指摘)。"""
    return (text or "").translate(_ZEN)


def _supported(fact: str, text: str, n: int = 6) -> bool:
    """fact の連続 n 文字のどれかが入力にあるか(自由文の事実が入力から来ているかの粗い検算)。"""
    f = re.sub(r"\s", "", fact)
    t = re.sub(r"\s", "", text)
    return any(f[i:i + n] in t for i in range(0, max(1, len(f) - n + 1)))


def validate(date: str, out: dict, posts: list[dict], mats: dict, inp: dict) -> tuple[dict, list[str]]:
    """モデルの答えを機械で検める。通らないものは捨てて理由を残す(直しはしない)。

    schema は形しか守らない。中身の危険(入力に無い記事を digest に書く、無関係な話題へ
    結合する、入力に無い事実を既報にする、素材に無い日付を予約する)はここで止める(監査指摘)。
    """
    notes: list[str] = []
    slugs = {fm.get("slug") for fm in posts}
    ids_of = {fm.get("slug"): set(fm.get("candidate_ids") or []) for fm in posts}
    brand_of = {fm.get("slug"): fm.get("brand") for fm in posts}
    art_in = {a["slug"]: a for a in inp.get("articles") or []}
    pending_keys = {x.get("dedup_key") for x in inp.get("pending") or []}
    subject_keys = {a.get("dedup_key") for a in art_in.values()} | {
        (mats.get(i) or {}).get("dedup_key") for s in ids_of.values() for i in s}

    digest = []
    seen_labels = []
    for g in out.get("digest") or []:
        rows = []
        for r in g.get("rows") or []:
            s = r.get("slug") or ""
            if s and s not in slugs:
                notes.append(f"digest: 無い記事 {s} を指す行を捨てた"); continue
            if not s and g.get("label") != "明日":
                # 記事を指さない行は「明日」の予約分だけ。他の群で作られたら入力に無い行
                notes.append(f"digest: 記事を指さない行を {g.get('label')} から捨てた({r.get('t')!r})"); continue
            # 行の文言が入力に根拠を持つか(記事なら見出し・リード・素材、明日の予約行なら予約の件名)
            if s:
                a = art_in.get(s) or {}
                basis = " ".join([str(a.get("title") or ""), str(a.get("lede") or "")]
                                 + [f["text"] for f in (a.get("facts") or [])])
            else:
                basis = " ".join(str(t.get("subject") or "") for t in (inp.get("tomorrow_reservations") or []))
            if not _supported(str(r.get("t") or ""), basis, n=3):
                notes.append(f"digest: 入力に根拠の無い行を捨てた({r.get('t')!r})"); continue
            if r.get("k") not in K_VOCAB or not r.get("t") or len(r["t"]) > 20 or len(r.get("d") or "") > 25:
                notes.append(f"digest: 形式外の行を捨てた({r.get('t')!r})"); continue
            row = {"k": r["k"], "t": r["t"], "d": r.get("d") or "", "brand": brand_of.get(s) or r.get("brand")}
            if row["brand"] not in BRANDS:
                notes.append(f"digest: 面が不正 {row['brand']}"); continue
            if s:
                row["slug"] = s
            rows.append(row)
        digest.append({"label": g.get("label"), "rows": rows[:4]})
        seen_labels.append(g.get("label"))
    if seen_labels != ["本日", "昨日", "継続中", "明日"]:
        notes.append(f"digest: 群が {seen_labels}。4群固定に直す")
        by = {g["label"]: g for g in digest}
        digest = [by.get(l, {"label": l, "rows": []}) for l in ("本日", "昨日", "継続中", "明日")]
    total = sum(len(g["rows"]) for g in digest)
    while total > 12:  # 多いぶんは末尾の群から削る
        for g in reversed(digest):
            if g["rows"] and total > 12:
                g["rows"].pop(); total -= 1
    lead = next((fm.get("slug") for fm in posts if fm.get("rank") == "lead"), None)
    if lead and not any(r.get("slug") == lead for g in digest for r in g["rows"]):
        fm = next(f for f in posts if f.get("slug") == lead)
        digest[0]["rows"].insert(0, {"k": "発", "t": str(fm.get("title") or "")[:20], "d": "", "brand": fm.get("brand"), "slug": lead})
        digest[0]["rows"] = digest[0]["rows"][:4]
        notes.append("digest: 一面が無かったので本日の先頭に足した")

    stories = []
    seen_story_slugs: set[str] = set()
    for s in out.get("stories") or []:
        slug = s.get("slug")
        a = art_in.get(slug)
        if a is None or not s.get("story_id") or not s.get("published_facts"):
            notes.append(f"stories: 不正な項目を捨てた({slug})"); continue
        if slug in seen_story_slugs:
            notes.append(f"stories: {slug} の2件目を捨てた(記事ごとに1件)"); continue
        allowed = {a.get("dedup_key")} | ({(a.get("existing_story") or {}).get("story_id")} if a.get("existing_story") else set())
        if s["story_id"] not in allowed:
            notes.append(f"stories: {slug} が無関係な話題 {s['story_id']} に結合しようとした → {a.get('dedup_key')} にする")
            s["story_id"] = a.get("dedup_key")
        fact_by_id = {f["id"]: f["text"] for f in (a.get("facts") or [])}
        basis = " ".join([str(a.get("title") or ""), str(a.get("lede") or "")] + list(fact_by_id.values()))
        facts = []
        for f in s["published_facts"][:4]:
            f = str(f).strip()
            if re.fullmatch(r"F\d+", f):
                if f in fact_by_id and fact_by_id[f] not in facts:
                    facts.append(fact_by_id[f])          # id で指した事実は素材そのもの
                else:
                    notes.append(f"stories: 無い事実 id {f} を捨てた({slug})")
                continue
            f = f[:140]
            # 自由文は見出し・リード・素材との4文字一致で粗く確かめる(言い換えは許す)
            if _supported(f, basis, n=4):
                facts.append(f)
            else:
                notes.append(f"stories: 入力に無い事実を捨てた({slug}: {f[:40]})")
        if not facts:
            notes.append(f"stories: {slug} は残る事実が無いので見出しを事実にする")
            facts = [str(a.get("title") or "")[:140]]
        seen_story_slugs.add(slug)
        stories.append({"slug": slug, "story_id": s["story_id"], "subject": (s.get("subject") or "")[:60],
                        "published_facts": facts})
    # **記事ごとに必ず1件。**返ってこなかった記事は見出しを事実として台帳に残す
    # (台帳に無い記事は翌日「既報」にならず、同じ話題がまた記事になる。監査指摘)
    for slug, a in art_in.items():
        if slug not in seen_story_slugs:
            notes.append(f"stories: {slug} の分が無いので見出しで補った")
            exist = a.get("existing_story") or {}
            stories.append({"slug": slug, "story_id": exist.get("story_id") or a.get("dedup_key"),
                            "subject": (exist.get("subject") or str(a.get("title") or ""))[:60],
                            "published_facts": [str(a.get("title") or "")[:140]]})

    res = []
    for r in out.get("reservations") or []:
        s = r.get("slug")
        cid = r.get("candidate_id")
        if s not in slugs or cid not in ids_of.get(s, set()) or cid not in mats:
            notes.append(f"reservations: 記事か素材が合わないので捨てた({s}/{cid})"); continue
        try:
            dt = datetime.date.fromisoformat(r.get("date") or "")
        except ValueError:
            notes.append(f"reservations: 日付が読めない {r.get('date')!r}"); continue
        if dt.isoformat() <= date or r.get("kind") not in KINDS:
            notes.append(f"reservations: 過去日か種別不正 {r.get('date')}/{r.get('kind')}"); continue
        c = mats[cid]
        hay = _norm(" ".join(list(c.get("facts") or []) + [str(c.get("event_date") or ""), str(c.get("deadline") or ""),
                                                           str(c.get("title") or "")]))
        # 締切前(3日前)は締切日そのものが素材にあればよい
        probe = [dt] + ([dt + datetime.timedelta(days=3)] if r.get("kind") == "締切前" else []) + \
                ([dt + datetime.timedelta(days=1)] if r.get("kind") in ("締切", "終了") else [])
        years = renderlib.years_in(hay + " " + str(c.get("published_date") or "") + " " + str(c.get("url") or "") + " " + date)
        if not any(renderlib.date_mentioned(p.isoformat(), hay, years) for p in probe):
            notes.append(f"reservations: 素材に無い日付 {dt.isoformat()} を捨てた({s})"); continue
        res.append({"slug": s, "candidate_id": cid, "date": dt.isoformat(), "kind": r["kind"],
                    "subject": (r.get("subject") or "")[:60], "note": (r.get("note") or "")[:120]})

    p_add = []
    for x in out.get("pending_add") or []:
        if not x.get("dedup_key") or x.get("brand") not in BRANDS:
            continue
        if x["dedup_key"] not in subject_keys:
            notes.append(f"pending_add: この号の主題でない {x['dedup_key']} を捨てた"); continue
        p_add.append({"dedup_key": x["dedup_key"], "brand": x.get("brand"), "subject": (x.get("subject") or "")[:60],
                      "watch": (x.get("watch") or "")[:140]})
    p_rm = []
    for x in out.get("pending_remove") or []:
        if str(x) in pending_keys:
            p_rm.append(str(x))
        else:
            notes.append(f"pending_remove: 無い項目 {x} を無視した")
    return {"digest": digest, "stories": stories, "reservations": res, "pending_add": p_add, "pending_remove": p_rm}, notes


# ---------------------------------------------------------------- 反映(冪等)
def strip_edition(date: str, stories: list[dict]) -> list[dict]:
    """台帳からこの号の寄与を剥がす。edition_facts[date] に記録した事実だけを消す。"""
    out = []
    for e in stories:
        ef = dict(e.get("edition_facts") or {})
        mine = ef.pop(date, None)
        if mine:
            e["published_facts"] = [f for f in e.get("published_facts") or [] if f not in mine]
        if ef:
            e["edition_facts"] = ef
        else:
            e.pop("edition_facts", None)
        if e.get("first_published") == date and not e.get("published_facts"):
            continue  # この号が作った話題で、剥がしたら空になった
        if e.get("first_published") == date and ef:
            # この号が作った話題だが、後続号の事実が残っている。初出を残っている最古の号にする
            # (剥がした号を初出として指し続けない。監査指摘)
            e["first_published"] = min(ef)
        out.append(e)
    return out


def rollback(date: str) -> list[str]:
    """この号の組版の寄与を stock から剥がし、組版前の状態に戻す(号を作り直すときに使う)。

    台帳は edition_facts[date] の印で剥がす。予約は reserved_on == date を消す。
    pending は初回に控えた組版前の写しに戻す。控え(metrics/*-before-<日付>.yml)は**消さない**:
    消すと次の組版が「組版後の状態」を新しい組版前として控え直し、消した追跡事項が
    戻らず、記事の既報判定にこの号自身の事実が混ざる(監査指摘)。
    """
    log = []
    stories = load_yaml_list(STORIES)
    # strip_edition は dict を**その場で**書き換えるので、印の有無は剥がす前に見る(監査指摘)
    had = any(date in (e.get("edition_facts") or {}) for e in stories)
    kept = strip_edition(date, stories)
    if had or len(kept) != len(stories):
        dump_yaml(STORIES, kept)
        log.append(f"台帳からこの号の寄与を剥がした({len(stories)}→{len(kept)} 話題)")
    for p in SCHEDULED.glob("*.json"):
        rows = json.loads(p.read_text(encoding="utf-8"))
        rest = [r for r in rows if r.get("reserved_on") != date]
        if len(rest) != len(rows):
            if rest:
                p.write_text(json.dumps(rest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            else:
                p.unlink()
            log.append(f"予約 {p.name}: この号の {len(rows) - len(rest)} 件を外した")
    before = ROOT / "metrics" / f"pending-before-{date}.yml"
    if before.exists():
        want = load_yaml_list(before)
        if load_yaml_list(PENDING) != want:
            dump_yaml(PENDING, want,
                      header="# 日付未確定の追跡事項(watchlist)。日付が判明したら組版が stock/scheduled/<日付>.json へ予約して、ここから消す。")
            log.append("pending を組版前の控えに戻した")
    return log


def baseline_stories(date: str) -> list[dict]:
    """組版前の台帳。

    **入力を作るときも反映するときも、同じこれを使う。**反映時だけ剥がすと、
    再実行のときに前回この号が足した事実を「既報」としてモデルに渡し、モデルが
    それを繰り返さず、反映直前に剥がされて台帳から消える(監査指摘 P0-2)。

    当日の再実行は「現在の台帳からこの号の寄与を剥がしたもの」で足りるが、後続号が
    台帳に入ったあとに過去号を組み直すと、後続号の事実が「組版前」に混ざる。
    初回に控えた metrics/stories-before-<日付>.yml があればそれを起点にする(監査指摘)。
    ただし後続号が同じ話題に足した事実は、控えには無いので現在の台帳から補う
    (剥がすのはこの号の寄与だけ)。
    """
    # **入力用と反映用は別の系統**(監査指摘)。ここは入力用: 初回に控えた組版前の台帳
    # そのもの(後続号の事実を混ぜない。混ぜると過去号の組み直しで未来の事実が「既報」に
    # 見え、当時残すべき事実を省く)。反映は apply が現在の台帳へ合流させる
    saved_p = ROOT / "metrics" / f"stories-before-{date}.yml"
    if saved_p.exists():
        return load_yaml_list(saved_p)
    return strip_edition(date, load_yaml_list(STORIES))


def baseline_pending(date: str, dry: bool) -> list[dict]:
    """組版前の pending。初回に控え(metrics/pending-before-<日付>.yml)、以後はそれを起点にする。

    pending には号の印を付けられない(削除は印が残らない)ので、控えから毎回組み立て直す。
    初回だけは現在の pending がそのまま控えになる(監査指摘 P0-4)。
    """
    p = ROOT / "metrics" / f"pending-before-{date}.yml"
    if p.exists():
        return load_yaml_list(p)
    cur = load_yaml_list(PENDING)
    if not dry:
        dump_yaml(p, cur)
    return cur


def apply(date: str, number: int, out: dict, posts: list[dict], mats: dict, dry: bool,
          stories: list[dict] | None = None) -> list[str]:
    log: list[str] = []
    d = datetime.date.fromisoformat(date)

    # 1. 既報台帳。**反映用**は現在の台帳からこの号の寄与を剥がしたもの(後続号の事実は残る)。
    #    **入力用**の控え(stories-before)は初回だけ書き、以後は触らない(監査指摘)
    before = ROOT / "metrics" / f"stories-before-{date}.yml"
    if not dry and not before.exists():
        dump_yaml(before, stories if stories is not None else strip_edition(date, load_yaml_list(STORIES)))
    stories = strip_edition(date, load_yaml_list(STORIES))
    by_id = {e.get("story_id"): e for e in stories}
    brand_of = {fm.get("slug"): fm.get("brand") for fm in posts}
    n_new = n_app = 0
    for s in out["stories"]:
        e = by_id.get(s["story_id"])
        if e is None:
            e = {"story_id": s["story_id"], "brand": brand_of.get(s["slug"]), "subject": s["subject"],
                 "status": "active", "first_published": date, "published_facts": []}
            stories.append(e); by_id[s["story_id"]] = e; n_new += 1
        added = [f for f in s["published_facts"] if f not in (e.get("published_facts") or [])]
        if added:
            e.setdefault("published_facts", []).extend(added)
            # 同じ話題に同号の記事が2本寄与しても、所有記録は**足す**(上書きすると
            # 先の記事の分が剥がせなくなる。監査指摘)
            mine = e.setdefault("edition_facts", {}).setdefault(date, [])
            mine.extend(f for f in added if f not in mine)
            n_app += len(added)
    log.append(f"台帳: 新規 {n_new} 話題 / 事実 {n_app} 件")

    # 2. 続報予約(この号が付けた分を剥がしてから付け直す)
    files = {}
    for p in SCHEDULED.glob("*.json"):
        rows = json.loads(p.read_text(encoding="utf-8"))
        kept = [r for r in rows if r.get("reserved_on") != date]
        if len(kept) != len(rows):
            files[p] = kept
    n_res = 0
    for r in out["reservations"]:
        c = mats[r["candidate_id"]]
        p = SCHEDULED / f"{r['date']}.json"
        rows = files.get(p)
        if rows is None:
            rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        dk = c.get("dedup_key") or r["slug"]
        rid = f"sched-{r['date']}-{dk}-{r['kind']}"
        if any(x.get("id") == rid for x in rows):
            continue
        entry = {"id": rid, "dedup_key": dk, "brand": brand_of.get(r["slug"]) or c.get("brand") or "other",
                 "subject": r["subject"] or c.get("title") or dk, "kind": r["kind"], "note": r["note"],
                 "reserved_on": date, "title": c.get("title") or "", "url": c.get("url") or "",
                 "source_type": classify_source(c.get("url") or ""), "facts": list(c.get("facts") or [])[:12],
                 "via": c.get("via") or "", "verify": c.get("verify") or "unconfirmed",
                 "src_candidate_id": r["candidate_id"]}
        if entry["source_type"] not in ("公式", "準公式", "当事者", "演者", "報道", "ファン", "二次情報", "もちより", "未確認"):
            entry["source_type"] = "未確認"
        for k in ("event_date", "deadline"):
            if c.get(k):
                entry[k] = str(c[k])
        rows.append(entry); files[p] = rows; n_res += 1
    log.append(f"予約: {n_res} 件")

    # 3. pending(組版前の控えから毎回組み立て直す。追加も削除も号の差分として効く)
    pending = [dict(x) for x in baseline_pending(date, dry)]
    rm = set(out["pending_remove"])
    pending = [x for x in pending if x.get("dedup_key") not in rm]
    have = {x.get("dedup_key") for x in pending}
    for x in out["pending_add"]:
        if x["dedup_key"] not in have:
            pending.append(x); have.add(x["dedup_key"])
    log.append(f"pending: -{len(rm)} +{len(out['pending_add'])} → {len(pending)} 件")

    # 4. 号スナップショット(機械算出欄は derive が上書きする)
    lead = next((fm.get("slug") for fm in posts if fm.get("rank") == "lead"), (posts[0].get("slug") if posts else ""))
    ed_path = EDITIONS / f"{date}.md"
    old = {}
    if ed_path.exists():
        m = FM_RE.match(ed_path.read_text(encoding="utf-8"))
        old = yaml.safe_load(m.group(1)) if m else {}
    snap = {"number": number if number is not None else old.get("number", 0), "date": date,
            "weekday": "月火水木金土日"[d.weekday()], "issued_at": f"{date}T06:00:00+09:00",
            "pages": old.get("pages", 0), "article_count": old.get("article_count", 0),
            "corrected_count": old.get("corrected_count", 0), "lead_slug": lead,
            "digest": out["digest"], "ranking": old.get("ranking", []), "birthdays": old.get("birthdays", [])}
    log.append(f"digest: {sum(len(g['rows']) for g in out['digest'])} 行 / 一面 {lead}")

    if dry:
        return log
    dump_yaml(STORIES, stories)
    for p, rows in files.items():
        if rows:
            p.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        else:
            p.unlink(missing_ok=True)
    dump_yaml(PENDING, pending, header="# 日付未確定の追跡事項(watchlist)。日付が判明したら組版が stock/scheduled/<日付>.json へ予約して、ここから消す。")
    ed_path.write_text("---\n" + yaml.safe_dump(snap, allow_unicode=True, sort_keys=False, width=200) + "---\n",
                       encoding="utf-8")
    return log


def run(date: str, number: int | None = None, dry: bool = False) -> int:
    posts = load_posts(date)
    if not posts:
        print(f"{date}: 記事が無い", flush=True)
        return 1
    mats = load_materials(date)
    # 入力も反映も**同じ組版前の状態**から(この号の寄与を剥がした台帳・控えた pending)
    stories = baseline_stories(date)
    pending = baseline_pending(date, dry)
    inp = build_input(date, posts, mats, stories, pending)
    out = run_session(prompt(date, inp), date)
    clean, notes = validate(date, out, posts, mats, inp)
    for n in notes:
        print("  検算:", n, flush=True)
    for line in apply(date, number, clean, posts, mats, dry, stories=stories):
        print("  " + line, flush=True)
    if dry:
        return 0
    subprocess.run([sys.executable, str(ROOT / "scripts" / "derive.py"), "--date", date, "--write"],
                   cwd=ROOT, capture_output=True, text=True)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "lint.py"), "--no-net"],
                       cwd=ROOT, capture_output=True, text=True)
    errs = [l for l in (r.stdout or "").splitlines() if l.startswith("::error")]
    print("  " + ((r.stdout or "").strip().splitlines() or ["lint: (出力なし)"])[-1], flush=True)
    if errs:
        # 組版の欠陥。セッションに直させない(直させると何が壊れたか分からなくなる)
        notify("compose", f"{date}: 組版のあと lint が赤い({len(errs)}件)。組版の欠陥として扱う:\n- "
                          + "\n- ".join(e.split("::", 2)[-1][:120] for e in errs[:5]), ok=False)
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--number", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="判断まで行い、ファイルへ反映しない")
    a = ap.parse_args()
    return run(a.date, a.number, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
