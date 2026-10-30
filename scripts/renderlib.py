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
# 出典 label の上限(字)。執筆 schema(schema/article-out.schema.json の sources[].label maxLength)と同じ値
LABEL_MAX = 200
FACT_NOTE = re.compile(r"\s*<!--\s*((?:[FN]\d+\s*)+)-->\s*$")
_ZEN = str.maketrans("０１２３４５６７８９／．－", "0123456789/.-")


def draft_key(path, keep_notice: bool = False) -> str:
    """稿の指紋(見出し・リード・本文と、見出し・リードの根拠 id・verified_facts)。稿ごとの「F 番号 → 事実」の記録を、どの稿のものかに
    結び付ける(巻き戻しで旧い稿に戻っても、その稿の記録が選ばれる)。根拠だけを付け替えた改稿も別の稿になる(監査指摘)。
    枠・出典の種別など、稿を採った後にコードが付け直す欄は含めない。公式ポータルのメンテナンスの断り書き(コードが後から付け外しする段落)も
    含めない(2026-10-23: 稿を記録した後に、別のセッションが見分けたメンテナンスで断り書きが付き、記録が「別の稿のもの」になって
    台帳が見出しだけになった)。keep_notice=True は断り書きを含めた旧い指紋(この直しより前に残した記録を引く台帳だけが使う)"""
    import hashlib
    import json as _json
    import yaml
    text = Path(path).read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    fm = (yaml.safe_load(m.group(1)) or {}) if m else {}
    body = text[m.end():] if m else text
    if not keep_notice:
        body = strip_notice(body)
    cites = _json.dumps([fm.get("title_fact_ids") or [], fm.get("lede_fact_ids") or [], fm.get("verified_facts") or []],
                        ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256("\n\0".join([str(fm.get("title") or ""), str(fm.get("lede") or ""), body.strip(), cites])
                          .encode("utf-8")).hexdigest()[:16]


def factmap_valid(data) -> bool:
    """稿ごとの事実の記録(metrics/factmap-<号>.json)の形: {slug: {稿の指紋: {F 番号: 事実の文}}}(と、壊れた印 "__corrupted__": 文字列)。
    JSON として読めても形が崩れていれば壊れている(保存も台帳も同じ基準で見る。監査指摘)。"""
    if not isinstance(data, dict):
        return False
    for slug, drafts in data.items():
        if slug == "__corrupted__":
            if not isinstance(drafts, str):
                return False
            continue
        if not isinstance(drafts, dict):
            return False
        for key, fmap in drafts.items():
            if not (isinstance(key, str) and isinstance(fmap, dict)
                    and all(isinstance(k, str) and isinstance(v, str) for k, v in fmap.items())):
                return False
    return True


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
                                     "deadline", "verify", "via", "unbacked_facts", "source_change") if c.get(k) not in (None, "", [])}
        # 続報予約に添えた素材が予約と別の話題から来たとき(まとめ記事の別の話題)、元の話題を見せる
        if c.get("src_dedup_key") and c.get("src_dedup_key") != c.get("dedup_key"):
            row["other_topic"] = c["src_dedup_key"]
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
    hay = (hay or "").translate(_ZEN)        # 全角の数字・記号(１０/１)も日付として見る
    year_ok = years is None or y in years
    # 年付きの日付(2099年9月13日 / 2099/9/13 …)の月日部分を、年なし表記の照合対象から外す
    # (別の年の日付を号の年の「9月13日」として拾わない。監査指摘)
    hay_no_year = re.sub(r"(?<!\d)20\d{2}\s*[年/.\-]\s*\d{1,2}\s*[月/.\-]\s*\d{1,2}\s*日?", " ", hay)
    for form in date_forms(iso):
        with_year = str(y) in form
        if not year_ok and not with_year:
            continue
        # 後ろの数字の境界は、数字で終わる書き方(9/1)にだけ要る。「10月1日4時59分」の「10月1日」は日付
        # (2026-10-14: 早朝に終わる催しの終了日を、時刻が続くため素材に無いと見ていた。監査指摘)
        tail = r"(?!\d)" if form[-1].isdigit() else ""
        if re.search(r"(?<!\d)" + re.escape(form) + tail, hay if with_year else hay_no_year):
            return True
    return False


# リードの文末。上限ちょうどで切れた文を採らない(2026-10-31: 上限 140 字ちょうどで「…各日の出演者と配信URL」と切れたまま校閲を抜けて紙面に載った)
LEDE_END = re.compile(r"[。．！？!?」』）)]$")


def lede_problem(lede) -> str:
    """リードが文末まで書き切れていなければ、その説明(書き切れていれば空)。執筆の検算と、既存稿の見直し(compose.contract_issues)で使う。"""
    lede = str(lede or "").strip()
    return f"リードが文の途中で終わっている(…{lede[-12:]})。短くして文末まで書き切る" if lede and not LEDE_END.search(lede) else ""


# 続報予約の種別ごとに、予約が指す出来事の日(occurs_on)が予約の日(予約のファイルの日付 = 記事を出す号)から何日後になりうるか。
# 先頭が既定。締切前は締切の3日前の号に、06:00 より前に終わる締切・終了は前日の号に予約する(prompts/assemble-ledger.md)。
# 締切前の4日後は、06:00 より前の締切を前日の号に予約し、その3日前にも予約したとき。ほかの種別はずれない
RESERVATION_LAG = {"締切前": (3, 4), "締切": (0, 1), "終了": (0, 1)}


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


_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")


def split_list_blocks(markdown: str) -> list[str]:
    """段落の Markdown を、書き出す単位に分ける: 空行で分け、さらに**地の文と箇条書きの境目**でも分ける。

    kramdown は、地の文の直後(空行なし)に続く `- 項目` を箇条書きにせず、同じ段落に吸収する
    (「次の通り。 - A - B」と1行で出る)。執筆の契約は「1 block の中に空行を入れない」なので、
    境目の空行はここで機械が入れる(執筆に空行を書かせない・差し戻さない)。
    """
    out: list[str] = []
    for chunk in re.split(r"\n\s*\n", markdown or ""):
        cur: list[str] = []
        cur_is_list: bool | None = None
        for ln in chunk.split("\n"):
            if not ln.strip():
                continue
            is_item = bool(_LIST_ITEM.match(ln))
            # 箇条書きの続きの行(字下げされた行)は、その箇条書きに付ける
            if cur and cur_is_list and not is_item and ln.startswith((" ", "\t")):
                cur.append(ln)
                continue
            if cur and is_item != cur_is_list:
                out.append("\n".join(cur).strip())
                cur = []
            cur.append(ln)
            cur_is_list = is_item
        if cur:
            out.append("\n".join(cur).strip())
    return [x for x in out if x]


def split_list_items(block: str) -> list[str]:
    """箇条書きの block を**1項目ずつ**に分ける(地の文の block はそのまま返す)。

    書き直しの照合用。執筆の契約は「箇条書きを1 block にまとめてよい」だが強制はしないので、同じ4項目を
    1 block で返す稿も、項目ごとに別 block で返す稿もあり得る。照合を項目単位に揃えないと、分け方が違うだけの
    正しい局所修正が「指摘に無い段落を足した」で落ちる(監査指摘)。地の文と箇条書きの境目は split_list_blocks
    が既に分けているので、ここは1 block 内の項目だけを分ける。続きの字下げ行はその項目に付ける。"""
    lines = (block or "").split("\n")
    if not any(_LIST_ITEM.match(ln) for ln in lines):
        return [block]
    items: list[str] = []
    cur: list[str] = []
    for ln in lines:
        if _LIST_ITEM.match(ln) and cur:
            items.append("\n".join(cur))
            cur = []
        cur.append(ln)
    if cur:
        items.append("\n".join(cur))
    return [x for x in items if x.strip()]


_ESC_NL = re.compile(r"\\r\\n|\\n|\\r")      # 文字としての「\n」(バックスラッシュ + n)


_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def unescape_text(markdown: str) -> str:
    """本文の Markdown に文字として残った `\\n` を本物の改行に戻す。コード(`…` と ```…```)の中は触らない
    (`\\n` という字面が正当に要るのはコードの中だけ。監査指摘)。"""
    text = markdown or ""
    out, pos = [], 0
    for m in _CODE.finditer(text):
        out.append(_ESC_NL.sub("\n", text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_ESC_NL.sub("\n", text[pos:]))
    return "".join(out)


def unescape_newlines(out: dict) -> int:
    """執筆の出力に**文字として**残った `\\n` を、本物の改行に戻す(その場で書き換える)。直した箇所の数を返す。

    モデルが JSON の中で改行を二重にエスケープすると、本文に「\\n- 項目\\n- 項目」がそのまま残り、箇条書きが
    1行に潰れて紙面に出る(実測 2026-09-21: セットリスト 37 曲が1段落になった。09-15 にも1件)。
    文章に `\\n` という字面が要ることは無いので、形の欠陥として機械で直す(差し戻さない)。
    1行しか許さない項目(見出し・リード・label・tags)では空白にする。
    """
    n = 0
    for b in out.get("blocks") or []:
        if isinstance(b, dict) and isinstance(b.get("markdown"), str):
            fixed = unescape_text(b["markdown"])
            if fixed != b["markdown"]:
                b["markdown"] = fixed
                n += 1
    for key in ("title", "lede"):
        if isinstance(out.get(key), str) and _ESC_NL.search(out[key]):
            out[key] = _ESC_NL.sub(" ", out[key]).strip()
            n += 1
    for s in out.get("sources") or []:
        if isinstance(s, dict) and isinstance(s.get("label"), str) and _ESC_NL.search(s["label"]):
            s["label"] = _ESC_NL.sub(" ", s["label"]).strip()
            n += 1
    if isinstance(out.get("tags"), list):
        fixed = [_ESC_NL.sub(" ", t).strip() if isinstance(t, str) else t for t in out["tags"]]
        n += sum(1 for a, b in zip(out["tags"], fixed) if a != b)
        out["tags"] = fixed
    return n


def strip_invisible(out: dict) -> int:
    """見出し・リード・段落から不可視文字(is_ignorable)を取り除く(その場で書き換える)。直した欄の数を返す。

    公式の告知を写すと、絵文字に付く異体字セレクタ(🗓️ の U+FE0F)や結合子が付いてくる。書き手には見えず、
    「不可視文字がある」と差し戻しても同じ告知を写すので直らない(実測 2026-10-24: High×Joker の 2DMV の記事が
    2回とも戻されて落ちた)。出典 label(clean_label)と同じく、形の欠陥として機械で落とす。
    落としたあとの値を検算・紙面が使うので、見えない字で字面を変える手は残らない。取り除くと空になる欄は
    そのままにする(検算の不可視文字の検査が戻す)。
    """
    n = 0
    def strip(v):
        return "".join(ch for ch in v if not is_ignorable(ch))
    for key in ("title", "lede"):
        v = out.get(key)
        if isinstance(v, str) and strip(v) != v and strip(v).strip():
            out[key] = strip(v)
            n += 1
    for b in out.get("blocks") or []:
        v = b.get("markdown") if isinstance(b, dict) else None
        if isinstance(v, str) and strip(v) != v and strip(v).strip():
            b["markdown"] = strip(v)
            n += 1
    return n


def split_heading_blocks(out: dict) -> int:
    """中見出し(`## …` 1行)と本文を空行で1 block に束ねた段落を、見出しの block と本文の block に分ける
    (その場で書き換える)。分けた block の数を返す。

    中見出しだけの block は根拠が要らないので、分けても根拠は増えも減りもしない(本文側に元の fact_ids を残す)。
    書き直しの依頼文の「現在の記事」では中見出しに根拠の控えが付かないため、書き手が見出しを直後の段落と
    同じ block に入れて返し、「1 block に複数段落」で2巡とも戻されて記事が校閲ブロックで落ちた(実測 2026-10-08)。
    形の違いなので機械で直す(差し戻さない)。
    地の文と箇条書きを空行で区切って1 block にしたもの(「次の通り。」+空行+「- 項目…」、「## 見出し」+箇条書き+空行+
    注記)も同じく分ける。空行が無ければ書き出し(split_list_blocks)が同じ境目で分けて同じ根拠を付けるので、
    空行1つの違いで戻す理由が無い(実測 2026-10-10: 書き直しが「品目は次の通り。」と品目の箇条書きを空行で1 block に
    入れて返し、「1 block に複数段落」で戻されて指摘が次の巡に残った。初稿でも2本が同じ形で差し戻された)。
    地の文の段落が2つ以上ある block は分けない(根拠の束ねは検算が止める)。
    """
    n = 0
    blocks = []
    for b in out.get("blocks") or []:
        md = b.get("markdown") if isinstance(b, dict) else None
        chunks = [c.strip() for c in re.split(r"\n\s*\n", md.strip())] if isinstance(md, str) else []
        chunks = [c for c in chunks if c]
        prose = [u for u in split_list_blocks(md) if not heading_only(u) and not _LIST_ITEM.match(u)] if chunks else []
        if len(chunks) > 1 and len(prose) <= 1:
            for c in chunks:
                blocks.append(dict(b, markdown=c, fact_ids=[] if heading_only(c) else list(b.get("fact_ids") or [])))
            n += 1
        else:
            blocks.append(b)
    if n:
        out["blocks"] = blocks
    return n


# ページを選ばない共有・計測用のクエリ(比べる前に外す)。どのホストでも外すものと、そのホストでだけ外すもの。
# ここに無いクエリはページを選ぶものとして扱い、名前・値・個数まで一致を求める(2026-10-14 監査指摘: 「片方がもう片方を含めば同じ」
# にしたら、読んだ /item と読んでいない別の商品 /item?id=2 が同じになり、utm_source の値が両側で違う同じ商品は別になった)
SHARE_QUERY = re.compile(r"^(?:utm_\w+|fbclid|gclid|yclid|igshid|igsh|_ga|_gl|mc_cid|mc_eid)$", re.I)
SHARE_QUERY_BY_HOST = {"x.com": ("s", "t", "ref_src", "ref_url"), "youtube.com": ("si", "feature", "pp", "t", "ab_channel"),
                       "youtu.be": ("si", "feature", "t"), "open.spotify.com": ("si",)}


_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def norm_escapes(s: str) -> str:
    """%エンコードの書き方だけを揃える(RFC 3986 6.2.2 の同値): ASCII の外の文字は UTF-8 のバイトの %XX に、%xx は大文字に、
    非予約文字(英数字と - . _ ~)の %XX は文字に戻す。予約文字(/ ? & = % など)の %XX は戻さない(%2F とパスの区切りの /、
    %252F と %2F は別のページ。2026-10-14 監査指摘: 退避の置き換えで %2F と %252F が同じになった)。バイトのまま扱い、不正な UTF-8 も潰さない"""
    import urllib.parse
    s = urllib.parse.quote(s, safe="".join(chr(i) for i in range(0x21, 0x7f)))
    return re.sub(r"%([0-9A-Fa-f]{2})",
                  lambda m: chr(int(m.group(1), 16)) if chr(int(m.group(1), 16)) in _UNRESERVED else "%" + m.group(1).upper(), s)


def page_key(url) -> tuple | None:
    """同じページかを比べる形: ホスト(小文字。www.・m.・mobile. を外し、twitter.com は x.com)、ポート(http の 80・https の 443 は無し)、
    パス(%エンコードの書き方を揃え(norm_escapes)、末尾の / を外す)、共有・計測用を外したクエリの並び(名前順。値はバイトのまま比べる)。
    scheme と # 以降は見ない。"""
    import urllib.parse
    try:
        u = urllib.parse.urlsplit(str(url or "").strip())
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        return None
    # 末尾ドット(FQDN の表記)と英数字でない名前(IDNA の表記)も揃える。pipelib.site_host と同じ揃え方で、断り書きの判定の
    # 「読めた・取得失敗・メンテナンス」の照合が表記の違いで崩れない(Sol 監査 Z2 r3: dot.example. を読み直して読めたのに付けた)
    host = host.rstrip(".")
    if host and not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            pass
    if not host:
        return None
    host = re.sub(r"^(?:www|m|mobile)\.", "", host)
    host = "x.com" if host == "twitter.com" else host
    path = norm_escapes(u.path).rstrip("/")
    drop = SHARE_QUERY_BY_HOST.get(host, ())
    # latin-1 はバイトと1対1(不正な UTF-8 の値を置換文字に潰して別の値を同じにしない)
    query = tuple(sorted((k, v) for k, v in urllib.parse.parse_qsl(norm_escapes(u.query), keep_blank_values=True, encoding="latin-1")
                         if not SHARE_QUERY.match(k) and k not in drop))
    # 既定のポートは scheme ごと(http の 443・https の 80 は既定ではなく、別のサービスを指す。2026-10-14 監査指摘)
    return host, (None if (u.scheme.lower(), port) in (("http", 80), ("https", 443)) else port), path, query


def same_page(a, b) -> bool:
    """a と b が同じページか(page_key が一致する)。共有用の ?si=・utm_* などの有無・値の違いでは素材・読んだページと同じ出典を落とさず、
    ページを選ぶクエリ(?id=・?v= など)が違えば・片方にしか無ければ別のページ。"""
    ka = page_key(a)
    return ka is not None and ka == page_key(b)


def check_output(out: dict, fact_by_id: dict[str, str], materials: list[dict], rank: str = "",
                 edition: str = "", fetched=()) -> list[str]:
    """出力の**形**の検算(schema は型しか見ない)。通らない理由を返す(空なら合格)。

    見るのは形だけ: 見出し・リード・段落に根拠 id が付いていて実在する / new_facts・sources の url は素材の url か、
    fetch_page.py が本文を取れたページ(fetched: セッションの記録 pipelib.fetched_urls) / tags は2〜4 / event_date は日付の形 /
    見送りは理由付き / 1 block = 1 段落 /
    HTML・参照リンク・不可視文字・制御文字を書いていない / 見出し・リードに Markdown 記法が無い(lint と同じ markdown_in_plain) /
    label は不可視文字を除いた値が空でなく Markdown 記号を含まない
    (roundup・culture の素材の件数は検めない。残った項目が1件でも載せる)。
    **中身の判断(出典を隠していないか、日付が素材と合うか、new_facts の事実が読んだページに本当にあるか)は校閲(モデル)の
    仕事で、ここではしない**(校閲の機械化はしない。編集長の指示)。fetched は「ページを読めたか」の記録で、中身は見ない。
    fetched=None は記録が無い答え(記録を始める前に書かれた答えを読み直す道具)だけが渡す。そのときは読めたかを見ない
    検算の前に、文字として残った `\\n` を本物の改行に戻し(unescape_newlines)、中見出しと本文を束ねた block を
    分け(split_heading_blocks)、見出し・リード・段落の不可視文字を取り除く(strip_invisible)。どれも out をその場で直す。
    """
    fixed = unescape_newlines(out)
    if fixed:
        print(f"  文字として残った \\n を改行に戻した({fixed} か所)", flush=True)
    split = split_heading_blocks(out)
    if split:
        print(f"  中見出し・箇条書きと本文を束ねた段落を分けた({split} か所)", flush=True)
    stripped = strip_invisible(out)
    if stripped:
        print(f"  見出し・リード・段落から不可視文字を取り除いた({stripped} か所)", flush=True)
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
    if not str(out.get("title") or "").strip() or not str(out.get("lede") or "").strip():
        problems.append("見出しかリードが空")
    # 見出し・リードは文字のまま紙面に出る。発行前の検査(lint.check_plain_text)と同じ基準で見て、執筆の段で差し戻す
    # (2026-10-14 監査指摘: `**販売開始**` の見出しを採ってから lint が赤にし、組み直し・書き直し・記事を外すへ進んだ)
    for key, label in (("title", "見出し"), ("lede", "リード")):
        m = markdown_in_plain(str(out.get(key) or "").strip())
        if m:
            problems.append(f"{label}に Markdown 記法 {m.group(0)!r} がある(この欄は記号がそのまま紙面に出る)")
    if lede_problem(out.get("lede")):
        problems.append(lede_problem(out.get("lede")))
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
        bad = sorted(set(bad_ids))[:6]
        # 執筆が直せるように、原因まで書く(実測 2026-09-25: 書き直しで verified_facts の N1 を new_facts に写さず、
        # 「無い事実 id: ['N1']」で2回戻されて記事が落ちた)
        problems.append(f"無い事実 id: {bad}"
                        + ("(N の id は new_facts に id・text・url を書いたものだけ使える。前の稿の verified_facts を使うなら書き写す)"
                           if any(str(i).startswith("N") for i in bad) else ""))
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
    # 不可視文字は検算の前に取り除いてある(strip_invisible)。ここに残るのは取り除くと空になる欄だけ
    if any(is_ignorable(ch) for x in texts for ch in x):
        problems.append("本文・見出し・リードに不可視文字(ゼロ幅・異体字セレクタ・書式制御)がある")
    # 参照形式のリンク・参照定義行も書かせない(表示文を変えずに字面を変える手)
    if any(re.search(r"^[ \t]*\[[^\]]+\]:[ \t]*\S|\]\[", x, flags=re.M) for x in texts):
        problems.append("本文・見出し・リードに参照形式の Markdown リンクがある(インライン形式で書く)")
    unsupported = [i for i, b in enumerate(out.get("blocks") or [])
                   if not (b.get("fact_ids") or []) and not heading_only(b.get("markdown"))]
    if unsupported:
        problems.append(f"根拠の事実 id が無い段落: {unsupported[:6]}")
    # roundup・culture の素材の件数は検めない。まとめる件数の下限は計画(validate_plan の ROUNDUP_MIN_ITEMS)が持ち、
    # 執筆で確かめて残った項目が1件でもあれば載せる(編集長の決定 2026-09-18〜19: 残った素材が2件の roundup を
    # 「3件以上」で落として号から消えた)
    seen = set()
    from pipelib import clean_url   # URL の唯一の入口(scheme・userinfo・制御文字・長さを検める)
    for f in new_facts:
        if f.get("url") and clean_url(f.get("url")) != f.get("url"):
            problems.append(f"new_facts の url の形が不正: {str(f.get('url'))[:60]}")
    for s in out.get("sources") or []:
        u = s.get("url") or ""
        if not re.match(r"^https?://", u) or clean_url(u) != u:
            problems.append(f"出典 url の形が不正: {u[:60]}")
        # 素材に無い URL(執筆が自分で見つけた出典)も、fetch_page.py で本文を取れたページなら許す(下の fetched の検査)。
        # 中身が記事と一致するかの確認は校閲(項目3)の仕事。「new_facts に書いたものだけ」と縛ったら、公式ストアや
        # チケットページを出典にした記事が4本落ちた(実測 2026-09-13)
        if u in seen:
            problems.append(f"出典 url が重複: {u[:60]}")
        seen.add(u)
        # 落とすのは Markdown として解釈される形(リンク `[x](y)`・コード)だけ。角括弧や # や _ は
        # 商品名・ハッシュタグ・ID に普通に出る(実測 2026-09-15: 「倉本千奈 [Wonder Scale]」で記事が落ちた)
        # label の制御文字・不可視文字は差し戻さず、書き出すときに機械で取り除く(clean_label)。
        # 出典ページの題名そのものにゼロ幅空白が入っていることがあり、差し戻しても執筆は同じ題名を写す
        # (実測 2026-09-19: 公式ポータルの題名の U+200B で差し戻し2回、記事が落ちた)。
        # 検査は**取り除いたあとの値**で行う(不可視文字で Markdown 記号を割った label が、書き出しで
        # リンクに化けるのを通さない。監査指摘)。取り除いて空になる label も通さない
        label = clean_label(s.get("label"))
        if not label:
            problems.append(f"出典 label が空: {s.get('label')!r}")
        # 発行前の検査(lint.check_plain_text)と同じ基準(markdown_in_plain)で見る。基準が割れていると、ここを通った label が
        # 発行前の検査で赤になる(2026-10-08: ローソンプリントの商品名「コレクション__ブロマイド228」の `__` を通していた)
        if markdown_in_plain(label) or re.search(r"\]\(", label):
            problems.append(f"出典 label に Markdown 記号: {s.get('label')!r}")
    if any(unicodedata.category(ch) == "Cc" and ch not in "\n\t" for x in texts for ch in x):
        problems.append("本文・見出し・リードに制御文字がある(壊れたテキスト)")
    # 根拠にしたページ(new_facts・sources の url)は、素材の url か、fetch_page.py が本文を取れたページ(fetched)だけ。
    # 校閲は出典を fetch_page.py だけで読むので、ほかの手段(Web 検索・curl)で読んだページや存在しない URL は確かめられない
    # (2026-10-14 rule_bench: 執筆が fetch_page.py の取れなかった報道を Web 検索で読んで期間を断定し、綴りの誤った URL
    # news.denfamicogamer.jp を new_facts と出典に書いた答えが検算を通った)。見るのは記録の有無(形)で、
    # 「使った事実の出典を隠していないか」「読んだページに書いた事実が本当にあるか」は校閲(モデル)の判断(校閲の機械化はしない)
    if fetched is not None:
        pool = [str(m.get("url") or "") for m in materials if isinstance(m, dict)] + [str(u) for u in fetched]
        cited = [str(f.get("url") or "") for f in new_facts] + [str(s.get("url") or "") for s in out.get("sources") or []]
        unread = [u for u in dict.fromkeys(cited) if u and not any(same_page(u, p) for p in pool)]
        if unread:
            problems.append("fetch_page.py で本文を取れていないページを根拠にしている: " + " / ".join(u[:100] for u in unread[:4])
                            + "(new_facts・sources の url は、素材の url か、python3 scripts/fetch_page.py で本文を取れたページだけ。"
                            "そのページを fetch_page.py で読み直すか、そのページの事実と出典を外す)")
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


# 紙面にそのまま出る欄(出典の label など)に残してはいけない Markdown 記法。lint もこれを使う(基準を1つにする)
MD_IN_PLAIN = re.compile(r"\*\*|__|\[[^\]]{1,60}\]\([^)]{1,200}\)|^#{1,6}\s|`")


def markdown_in_plain(value):
    """紙面に文字のまま出る欄(見出し・リード・digest の行)に残った Markdown 記法の一致(無ければ None)。
    執筆の検算(check_output)と発行前の検査(lint.check_plain_text)が同じこれで見る(基準が割れると、検算を通った稿が lint で赤になる)。"""
    return MD_IN_PLAIN.search(str(value or ""))


def clean_label(s) -> str:
    """出典 label を書き出す形に整える: 制御文字(改行・タブを含む)と不可視文字を取り除き、空白を1つに畳み、LABEL_MAX 字まで。
    出典ページの題名に紛れたゼロ幅空白などを、執筆に差し戻さず機械で落とす。
    上限は執筆 schema(article-out の label maxLength)と同じにする。80 字だった頃、告知タイトルが途中で切れて校閲 R16 が
    何巡もブロックし、記事が落ちた(2026-09-26 号: 316 label 中 73 が丁度 80 字)。"""
    import unicodedata
    t = "".join(ch for ch in str(s or "") if unicodedata.category(ch) != "Cc" and not is_ignorable(ch))
    return re.sub(r"\s+", " ", t).strip()[:LABEL_MAX]


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


# 本文の末尾にコードが付ける断り書きの段落(出典のサイトのメンテナンス。2026-10-22・23 編集長の決定)。印で見分け、
# 字数(枠)・書き直しの段落の照合・書き手に見せる稿からは外す(書き手の言葉ではない)
# 外すのは、決まった文面(pipelib.maintenance_notice の型・発行済みの号の文面)に印が付いた段落だけ(書き手の文に印が紛れても外さない)。
# 断り書きは承認の指紋(pipelib.article_hash)にも入れない(コードの文面で、校閲の判断の対象ではない)。
# 印は 10/23 号(公式ポータルだけ)で付けた記事と同じものを使い続ける
NOTICE_MARK = "<!-- notice:portal-maintenance -->"


def strip_notice(body: str) -> str:
    """本文から断り書きの段落を外す(無ければそのまま)。"""
    from pipelib import (ARCHIVE_NOTICE_TAILS, MAINTENANCE_LEGACY_NOTICES, MAINTENANCE_NOTICE_TAIL,   # 循環を避けて関数の中で読む
                         MAINTENANCE_SITE_NAMES)
    name = "(?:" + "|".join([*(re.escape(n) for n in MAINTENANCE_SITE_NAMES.values()), r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)+"]) + ")"
    names = name + "(?:・" + name + ")*"
    day = r"\d{4}年\d{1,2}月\d{1,2}日"
    read = r"(?:収集時点\(\d{1,2}月\d{1,2}日\)|\d{1,2}月\d{1,2}日)"
    head, tail = MAINTENANCE_NOTICE_TAIL.split("{date}")
    # 1文ずつ: メンテナンス(原本なし)・保存の原本で書いた(メンテナンス / 読み直せない)。断り書きはこれらの文を「※」1つで続けたもの
    sentences = [names + re.escape(head) + day + re.escape(tail)]
    for t in ARCHIVE_NOTICE_TAILS.values():
        a, rest = t.split("{date}")
        b, c = rest.split("{read}")
        sentences.append(names + re.escape(a) + day + re.escape(b) + read + re.escape(c))
    generic = "※(?:" + "|".join(sentences) + ")+"
    alts = "|".join([generic, *(re.escape(n) for n in MAINTENANCE_LEGACY_NOTICES.values())])
    return re.sub(r"(?:\A|\n[ \t]*\n)(?:" + alts + r") " + re.escape(NOTICE_MARK) + r"[ \t]*(?=\n|\Z)", "", body or "")


def with_notice(text: str, notice: str | None) -> str:
    """記事ファイルの本文の末尾の断り書きを、notice(None なら無し)に揃える。何度掛けても同じ(冪等)。"""
    m = re.match(r"\A(---\n.*?\n---\n)(.*)\Z", text, flags=re.S)
    head, body = (m.group(1), m.group(2)) if m else ("", text)
    body = strip_notice(body).rstrip("\n")
    if notice:
        body = f"{body}\n\n{notice} {NOTICE_MARK}"
    return head + body + "\n"


def render_article(path: Path, date: str, art: dict, out: dict, source_type_of, weakest_src,
                   dump_yaml, follows: list[str] | None = None) -> None:
    """検算済みの出力から記事ファイルを作る。段落の根拠 id は HTML コメントで残す。
    follows は、素材の「出典の更新」(source_change)が指す発行済み記事(この記事はその続報。紙面で互いにリンクする)"""
    sources = []
    for s in out.get("sources") or []:
        sources.append({"label": clean_label(s.get("label")), "url": s["url"], "type": source_type_of(s["url"])})
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
    if follows:
        fm["follows"] = sorted(dict.fromkeys(str(f) for f in follows))
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
        for md in split_list_blocks(strip_fact_notes(b.get("markdown", ""))):
            paras.append(f"{md} <!-- {ids} -->" if ids else md)
    body = "\n\n".join(paras)
    from pipelib import write_checked      # 書式(article.schema)で検めてから書く(循環を避けて関数の中で読む)
    write_checked(path, "---\n" + dump_yaml(fm) + "---\n" + body + "\n")
