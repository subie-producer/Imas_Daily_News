"""renderlib: 執筆セッションの構造化出力 → 記事ファイル。構造は**生成時に**強制する。

以前の執筆は Markdown ファイルを自由に書き、frontmatter の固定項目・日付の形・出典の
種別を**事後に**コードや別セッションで直していた(監査の設計レビュー: 構造制御と
校閲の往復が混ざっている)。ここでは:

- 執筆セッションは判断と文章だけを JSON で返す(schema/article-out.schema.json)。
  段落ごとに根拠の事実 id(F1…)を付ける。素材に無い事実を一次情報から取ったなら
  `new_facts`(N1…)に「読んだ URL と事実」を書く
- コードが frontmatter を作る。slug/edition/brand/candidate_ids は計画、出典の種別は判定表、
  src は最弱、rank は仮、訂正欄は固定値。段落の根拠 id は HTML コメントで本文に残す
  (校閲が段落と素材の事実を突き合わせられるように)
- 事実 id・出典・タグ・日付はここで検算し、通らないものは記事を不成立にする。
  lint は最後の不変条件の検査で、赤ならバグ
"""
import datetime
import re
from pathlib import Path

DECLINE_CODES = ("NO_PRIMARY_SOURCE", "SOURCE_MISMATCH", "TOO_FEW_MATERIALS", "NOT_NEWS", "OTHER")
FACT_NOTE = re.compile(r"\s*<!--\s*((?:[FN]\d+\s*)+)-->\s*$")
_ZEN = str.maketrans("０１２３４５６７８９／．－", "0123456789/.-")


def materials_with_ids(materials: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """素材の facts に F1, F2 … を振って、執筆に渡す形にする。戻りは (素材, {id: 事実})。

    `unbacked_facts`(収集が出典本文で確かめられなかった値)も**そのまま渡す**。
    落とすと執筆は「その値は無い」と思って書かず、逆に確かめれば書けたものが消える。
    id は付けない(根拠にはできない。読んで確かめたら new_facts に書く)
    """
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
                                     "deadline", "verify", "via", "unbacked_facts") if c.get(k) not in (None, "", [])}
        row["facts"] = facts
        out.append(row)
    return out, fact_by_id


def date_forms(iso: str) -> list[str]:
    """2026-09-12 → その日付の書き方いろいろ(素材の文言と照合するため)。"""
    try:
        d = datetime.date.fromisoformat(iso)
    except ValueError:
        return []
    # 区切りなしの8桁(20260913)は作らない: 商品コードや URL の一部と区別できない(監査指摘)
    forms = []
    # 年付き・年なし × 区切り × ゼロ埋めの有無、を全部作る(「2026/09/13」を落とさない。監査指摘)
    for m, day in ((str(d.month), str(d.day)), (f"{d.month:02d}", f"{d.day:02d}"),
                   (str(d.month), f"{d.day:02d}"), (f"{d.month:02d}", str(d.day))):
        forms += [f"{m}月{day}日", f"{m}/{day}", f"{m}.{day}", f"{m}-{day}",
                  f"{d.year}年{m}月{day}日", f"{d.year}/{m}/{day}", f"{d.year}.{m}.{day}", f"{d.year}-{m}-{day}"]
    return list(dict.fromkeys(forms))


def years_in(text: str) -> set[int]:
    """文言に現れる西暦(2000〜2099)。年なしの日付表記を照合するときの許容年になる。"""
    return {int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", (text or "").translate(_ZEN))}


def date_mentioned(iso: str, hay: str, years: set[int] | None = None) -> bool:
    """その日付の書き方のどれかが文言に**日付として**現れるか。数字の境界を見る
    (「9/1」が「9/13」の一部として当たらないように)。年なしの表記(9月13日)は、その年が
    `years`(素材や号の日付に出てくる年)に入っているときだけ根拠にする。
    年が許容されないなら年付きの表記だけを探す(2099-09-13 を「9月13日」で通さない。監査指摘)。"""
    try:
        y = datetime.date.fromisoformat(iso).year
    except ValueError:
        return False
    year_ok = years is None or y in years
    # 年付きの日付(2099年9月13日 / 2099/9/13 …)の月日部分を、年なし表記の照合対象から外す
    # (別の年の日付を号の年の「9月13日」として拾わない。監査指摘)
    hay_no_year = re.sub(r"(?<!\d)20\d{2}\s*[年/.\-]\s*\d{1,2}\s*[月/.\-]\s*\d{1,2}\s*日?", " ", hay)
    for form in date_forms(iso):
        with_year = str(y) in form
        if not year_ok and not with_year:
            continue
        if re.search(r"(?<!\d)" + re.escape(form) + r"(?!\d)", hay if with_year else hay_no_year):
            return True
    return False


def fact_index(materials: list[dict]) -> dict[str, int]:
    """F id → 素材の添字(materials_with_ids と同じ順で振り直す)。"""
    cand_of: dict[str, int] = {}
    n = 0
    for idx, c in enumerate(materials):
        for _ in c.get("facts") or []:
            n += 1
            cand_of[f"F{n}"] = idx
    return cand_of


def heading_only(markdown) -> bool:
    return bool(re.fullmatch(r"#{2,3} [^\n]+", str(markdown or "").strip()))


def check_output(out: dict, fact_by_id: dict[str, str], materials: list[dict], rank: str = "",
                 edition: str = "") -> list[str]:
    """出力の**形**の検算(schema は型しか見ない)。通らない理由を返す(空なら合格)。

    見るのは形だけ: 見出し・リード・段落に根拠 id が付いていて実在する / 出典 URL は素材か new_facts の
    もの / tags は2〜4 / event_date は日付の形 / 見送りは理由付き / roundup・culture は3件以上の素材を
    使っている / 1 block = 1 段落 / HTML・参照リンク・不可視文字を書いていない。
    **中身の判断(出典を隠していないか、日付が素材と合うか、new_facts を本当に読んだか)は校閲(モデル)の
    仕事で、ここではしない**(校閲の機械化はしない。編集長の指示)
    """
    problems = []
    if out.get("status") == "decline":
        if out.get("decline_code") not in DECLINE_CODES:
            problems.append("decline に decline_code が無い")
        if not str(out.get("decline_detail") or "").strip():
            problems.append("decline に decline_detail が無い")
        return problems
    if out.get("decline_code"):
        problems.append("ok なのに decline_code がある")
    new_facts = [f for f in (out.get("new_facts") or []) if isinstance(f, dict)]
    known_ids = dict(fact_by_id)
    for f in new_facts:
        if not re.match(r"^https?://", str(f.get("url") or "")) or not str(f.get("text") or "").strip():
            problems.append(f"new_facts の形が不正: {str(f)[:60]}")
        if f.get("id") in known_ids:
            problems.append(f"new_facts の id が重複: {f.get('id')}")
        known_ids[f.get("id")] = str(f.get("text") or "")
    known_urls = {c.get("url") for c in materials if c.get("url")} | {f.get("url") for f in new_facts}
    if not str(out.get("title") or "").strip() or not str(out.get("lede") or "").strip():
        problems.append("見出しかリードが空")
    if not out.get("blocks"):
        problems.append("本文が空")
    if not out.get("sources"):
        problems.append("出典が無い")
    for key, label in (("title_fact_ids", "見出し"), ("lede_fact_ids", "リード")):
        if not (out.get(key) or []):
            problems.append(f"{label}に根拠の事実 id が無い")
    bad_ids = [i for key in ("title_fact_ids", "lede_fact_ids") for i in (out.get(key) or []) if i not in known_ids]
    bad_ids += [i for b in (out.get("blocks") or []) for i in (b.get("fact_ids") or []) if i not in known_ids]
    if bad_ids:
        problems.append(f"無い事実 id: {sorted(set(bad_ids))[:6]}")
    # 1 block = 1 段落。空行で複数段落を1つの根拠で束ねさせない(根拠の水増しになる。監査指摘)。
    # 中見出しだけの block(`## …` 1行)は根拠が要らない
    multi = [i for i, b in enumerate(out.get("blocks") or []) if re.search(r"\n\s*\n", str(b.get("markdown") or "").strip())]
    if multi:
        problems.append(f"1 block に複数段落(空行)がある: {multi[:6]}")
    # HTML コメントは執筆が書くものではない(根拠の控えはコードが付ける)。引用の途中に挟んで
    # 検査をすり抜ける手を塞ぐ(監査指摘)
    texts = [str(x or "") for x in [out.get("title"), out.get("lede")] + [b.get("markdown") for b in (out.get("blocks") or [])]]
    if any("<!--" in x for x in texts):
        problems.append("本文・見出し・リードに HTML コメントがある")
    # 生の HTML タグ・文字参照・ゼロ幅/書式制御文字も書かせない(読者に見えない差で字面を変える手。監査指摘)
    import unicodedata
    if any(re.search(r"</?[A-Za-z][^>]*>|&#?\w+;", x) for x in texts):
        problems.append("本文・見出し・リードに HTML タグか文字参照がある")
    if any(is_ignorable(ch) for x in texts for ch in x):
        problems.append("本文・見出し・リードに不可視文字(ゼロ幅・異体字セレクタ・書式制御)がある")
    # 参照形式のリンク・参照定義行も書かせない(表示文を変えずに字面を変える手)
    if any(re.search(r"^[ \t]*\[[^\]]+\]:[ \t]*\S|\]\[", x, flags=re.M) for x in texts):
        problems.append("本文・見出し・リードに参照形式の Markdown リンクがある(インライン形式で書く)")
    unsupported = [i for i, b in enumerate(out.get("blocks") or [])
                   if not (b.get("fact_ids") or []) and not heading_only(b.get("markdown"))]
    if unsupported:
        problems.append(f"根拠の事実 id が無い段落: {unsupported[:6]}")
    cand_of = fact_index(materials)
    if rank in ("roundup", "culture"):
        # 束ねの記事は3件以上の素材から書けていること
        used = {cand_of[i] for b in (out.get("blocks") or []) for i in (b.get("fact_ids") or []) if i in cand_of}
        if len(used) < 3:
            problems.append(f"{rank} なのに使った素材が {len(used)} 件(3件以上)")
    seen = set()
    for s in out.get("sources") or []:
        u = s.get("url") or ""
        if not re.match(r"^https?://", u):
            problems.append(f"出典 url の形が不正: {u[:60]}")
        elif u not in known_urls:
            problems.append(f"素材にも new_facts にも無い出典 url(読んだなら new_facts に事実を書く): {u[:70]}")
        if u in seen:
            problems.append(f"出典 url が重複: {u[:60]}")
        seen.add(u)
        if re.search(r"[\[\]*_`#]", s.get("label") or ""):
            problems.append(f"出典 label に Markdown 記号: {s.get('label')!r}")
    # 「使った事実の出典を隠していないか」「素材に無い URL を本当に読んだか」は校閲(モデル)の判断。
    # ここでは見ない(校閲の機械化はしない。編集長の指示)
    tags = [str(t).strip() for t in (out.get("tags") or []) if str(t).strip()]
    if not 2 <= len(tags) <= 4:
        problems.append(f"tags が {len(tags)} 個(2〜4個)")
    for t in tags:
        if re.search(r"[\[\]*_`#\n]", t):
            problems.append(f"tag に記号: {t!r}")
    if out.get("event_date"):
        ev = str(out["event_date"])
        try:
            datetime.date.fromisoformat(ev)
        except ValueError:
            problems.append(f"event_date が日付でない: {ev!r}")
        # その日付が出来事の日として素材と合うか(年ズレを含む)は校閲の判断(校閲項目 4・12)
    return problems


def is_ignorable(ch: str) -> bool:
    """読者に見えない符号位置(Default_Ignorable 相当): 書式制御(Cf)、結合字素接合子、異体字セレクタ、
    モンゴル文字の異体字セレクタ、ゼロ幅系。字面を変えずに照合を外す手に使われる(監査指摘)。"""
    import unicodedata
    o = ord(ch)
    return (unicodedata.category(ch) == "Cf" or o == 0x034F or 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF
            or 0x180B <= o <= 0x180F or 0x200B <= o <= 0x200F
            or o in (0x2028, 0x2029, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFEFF,
                     0x115F, 0x1160, 0x3164, 0xFFA0))   # ハングルの filler(見えない字。監査指摘)


def visible_text(s) -> str:
    """読者に見える字面だけにする(引用の照合用。監査指摘)。
    HTML コメント・タグ・文字参照、Markdown のリンク/画像(インライン・参照形式)/装飾/バックスラッシュ、
    参照定義行、NFKC 正規化、不可視文字・制御文字・空白を落とす。"""
    import html as _html
    import unicodedata
    t = str(s or "")
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"^[ \t]*\[[^\]]+\]:[ \t]*\S.*$", "", t, flags=re.M)   # 参照定義行 [id]: url
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)      # 画像 → 代替文
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)       # リンク → 表示文
    t = re.sub(r"!?\[([^\]]*)\]\[[^\]]*\]", r"\1", t)    # 参照形式 [表示文][id] / [表示文][]
    t = re.sub(r"<[^>]+>", "", t)                         # HTML タグ
    t = _html.unescape(t)                                  # 文字参照
    t = re.sub(r"\\(.)", r"\1", t)                        # バックスラッシュのエスケープ
    t = unicodedata.normalize("NFKC", t)
    t = "".join(ch for ch in t if not is_ignorable(ch) and unicodedata.category(ch) not in ("Cc", "Zs", "Zl", "Zp"))
    return re.sub(r"[*_~`#>|\[\]-]", "", t)


def strip_fact_notes(body: str) -> str:
    """本文から根拠 id のコメントを外す(字数の測定・表示用)。"""
    return re.sub(r"\s*<!--\s*(?:[FN]\d+\s*)+-->", "", body)


def render_article(path: Path, date: str, art: dict, out: dict, source_type_of, weakest_src,
                   dump_yaml) -> None:
    """検算済みの出力から記事ファイルを作る。段落の根拠 id は HTML コメントで残す。"""
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
    # 見出し・リードの根拠も残す(校閲が突き合わせる。監査指摘)
    fm["title_fact_ids"] = [str(i) for i in (out.get("title_fact_ids") or [])]
    fm["lede_fact_ids"] = [str(i) for i in (out.get("lede_fact_ids") or [])]
    new_facts = [f for f in (out.get("new_facts") or []) if isinstance(f, dict)]
    if new_facts:
        fm["verified_facts"] = [{"id": f["id"], "text": str(f["text"]).strip(), "url": f["url"]} for f in new_facts]
    paras = []
    for b in out.get("blocks") or []:
        ids = " ".join(dict.fromkeys(str(i) for i in (b.get("fact_ids") or [])))
        # 1 block に空行区切りの複数段落が入っていても、段落ごとに根拠を付ける(監査指摘)
        for md in re.split(r"\n\s*\n", strip_fact_notes(b.get("markdown", ""))):
            md = md.strip()
            if not md:
                continue
            paras.append(f"{md} <!-- {ids} -->" if ids else md)
    body = "\n\n".join(paras)
    path.write_text("---\n" + dump_yaml(fm) + "---\n" + body + "\n", encoding="utf-8")
