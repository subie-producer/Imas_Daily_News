"""紙面のデータ(記事・号・社説・ブランド・タグ語彙)を、版(release)のフォルダから読んでメモリに持つ。

版のフォルダは docs/ と同じ並び(_posts/ _editions/ _editorials/ _data/ about/index.md)。アプリは起動時と、
配信の current が別の版に切り替わったときに読み直す。ページを描くたびにファイルを読まない。

並び順は Jekyll の site.posts に合わせる(日付の新しい順、同じ日付はパスの逆順)。号の記事の並び
(段の順 lead → culture、段の中は site.posts の順)・続報の関係も、ここで1回だけ組む。
"""
import datetime
import html
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from markdown_it import MarkdownIt

# front matter の読み込みは C 実装で(Python 実装では全記事で 7秒。読み込みの大半を占めた)
LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
RANKS = ("lead", "large", "medium", "small", "roundup", "culture")
FM = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


# ---- 引用符と記号の活字への置き換え(以前の変換 kramdown の smart_quotes・typographic_syms と同じ規則) ----
# markdown-it の置き換えは使わない: replacements は「!!!!」を「!!!」に縮めて本文を変え、smartquotes は「"アイ"」の " を
# 置き換えない(kramdown は前の文字が空白・括弧でなければ閉じの ” にする)。Ruby の \w \s \b は ASCII だけなので re.ASCII
_PUNCT = r"""[!"#$%'()*+,\-./:;<=>?@\[\\\]^_`{|}~]"""
_CLOSE = r"[^ \\\t\r\n\[{(\-]"
_SQ_RULES = [(re.compile(p, re.ASCII | re.M | re.S), s) for p, s in (
    (r"""("|')(?=[_*]{1,2}\S)""", ["lquote1"]),
    (rf"""("|')(?={_PUNCT}(?!\.\.)\B)""", ["rquote1"]),
    (r"""(\s?)"'(?=\w)""", [1, "“", "‘"]),
    (r"""(\s?)'"(?=\w)""", [1, "‘", "“"]),
    (r"""(\s?)'(?=\d\ds)""", [1, "’"]),
    (r"""(\s)('|")(?=\w)""", [1, "lquote2"]),
    (rf"""({_CLOSE})('|")""", [1, "rquote2"]),
    (r"""("|')(?=\s|s\b|$)""", ["rquote1"]),
    (r"""(.?)'""", [1, "‘"]),
    (r"""(.?)\"""", [1, "“"]),
)]
_SQ_SUBST = {("r", '"'): "”", ("r", "'"): "’", ("l", '"'): "“", ("l", "'"): "‘"}
_TYPO = re.compile(r"---|--|\.\.\.")
_TYPO_SUBST = {"---": "—", "--": "–", "...": "…"}


def smartify(s: str) -> str:
    """kramdown と同じ規則で、引用符を活字の引用符に、--- -- ... をダッシュ・三点リーダーに置き換える。"""
    out, i, n = [], 0, len(s)
    while i < n:
        m = re.search(r"""['"]|---|--|\.\.\.""", s[i:])
        if not m:
            out.append(s[i:])
            break
        q = i + m.start()
        if m.group() in _TYPO_SUBST:
            out.append(s[i:q] + _TYPO_SUBST[m.group()])
            i = q + len(m.group())
            continue
        p = q - 1 if q - 1 >= i and s[q - 1] != "\\" else q     # 引用符の1つ前の文字から規則を当てる(kramdown の span の始まり)
        out.append(s[i:p])
        for rx, subst in _SQ_RULES:
            mm = rx.match(s, p)
            if mm:
                break
        for x in subst:
            if isinstance(x, int):
                out.append(mm.group(x) or "")
            elif x[1:6] == "quote":
                out.append(_SQ_SUBST[(x[0], mm.group(int(x[-1])))])
            else:
                out.append(x)
        i = mm.end()
    return "".join(out)


def _smart_rule(state) -> None:
    for tok in state.tokens:
        if tok.type != "inline" or not tok.children:
            continue
        for child in tok.children:
            if child.type == "text":
                child.content = smartify(child.content)


def _close_of(tokens: list, i: int) -> int:
    """tokens[i](…_open)に対応する …_close の位置。"""
    want, lv = tokens[i].type.replace("_open", "_close"), tokens[i].level
    j = i + 1
    while j < len(tokens) and not (tokens[j].type == want and tokens[j].level == lv):
        j += 1
    return j


def _list_paragraphs(state) -> None:
    """箇条書きの項目の段落に <p> を付けるかを、kramdown(parser/kramdown/list.rb)と同じ規則で項目ごとに決める。
    項目の最初の段落を <p> なしにするのは、次の2つをどちらも満たすとき:
      (1) 段落のすぐあとが空行でない。または、最後の項目で、段落と空行だけからなる
      (2) 最後の項目でない。または、項目が1つだけ。または、前の項目のどれかが <p> なし(か最初が段落でない)
    2つ目からの段落は常に <p>。(markdown-it は箇条書き全体で決め、1か所でも空行があれば全項目に <p> を付ける)"""
    tokens, lines = state.tokens, state.src.split("\n")
    n_lines = len(lines) - 1 if state.src.endswith("\n") else len(lines)
    for i, tok in enumerate(tokens):
        if tok.type not in ("bullet_list_open", "ordered_list_open"):
            continue
        end = _close_of(tokens, i)
        items = [j for j in range(i + 1, end) if tokens[j].type == "list_item_open" and tokens[j].level == tok.level + 1]
        bare_before = False         # 前の項目に <p> なし(か最初が段落でない)があったか
        for n, j in enumerate(items):
            close = _close_of(tokens, j)
            blocks = [k for k in range(j + 1, close) if tokens[k].level == tok.level + 2 and tokens[k].nesting == 1]
            paras = [k for k in blocks if tokens[k].type == "paragraph_open"]
            for k in paras:
                tokens[k].hidden = tokens[_close_of(tokens, k)].hidden = False
            first_is_p = bool(blocks) and tokens[blocks[0]].type == "paragraph_open"
            if not first_is_p:
                bare_before = True
                continue
            p = tokens[blocks[0]]
            after = p.map[1] if p.map else None
            # 段落のすぐあとの行が空行か(末尾の改行のあとの空文字列は行ではない)
            blank_after = after is not None and after < n_lines and not lines[after].strip()
            only_p_blank = blank_after and len(blocks) == 1
            last = n == len(items) - 1
            bare = (not blank_after or (last and only_p_blank)) and (not last or len(items) == 1 or bare_before)
            if bare:
                p.hidden = tokens[_close_of(tokens, blocks[0])].hidden = True
                bare_before = True


def _md() -> MarkdownIt:
    # 以前の変換(Jekyll の kramdown・GFM 入力)に寄せる: 生の HTML(<!-- F1 --> の根拠の印)を通す。
    # 引用符・記号の置き換えは smartify(上)で行う
    md = MarkdownIt("commonmark", {"html": True})
    # 文字をつなぐ前に置き換える(\" のようにエスケープした引用符は置き換えない。kramdown と同じ)
    md.core.ruler.before("text_join", "kramdown_smart", _smart_rule)
    md.core.ruler.push("kramdown_lists", _list_paragraphs)

    def code_inline(self, tokens, idx, options, env):     # 以前の出力と同じ class(紙面の CSS が当たる)
        return f'<code class="language-plaintext highlighter-rouge">{html.escape(tokens[idx].content, quote=False)}</code>'
    md.add_render_rule("code_inline", code_inline)
    return md


MD = _md()


def heading_id(text: str, used: dict) -> str:
    """kramdown の GFM 入力と同じ見出しの id(小文字にし、文字・数字・_・-・空白以外を落とし、空白を - に。重複は -1, -2)。"""
    t = re.sub(r"[^\w\- \t]", "", text.lower()).replace(" ", "-").replace("\t", "-")
    n = used.get(t, 0)
    used[t] = n + 1
    return t if n == 0 else f"{t}-{n}"


def render_markdown(text: str) -> str:
    tokens = MD.parse(text)
    used: dict = {}
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open":
            # id は見出しの文字だけから作る(末尾の根拠の印 <!-- F3 --> などの HTML は入れない。kramdown と同じ)
            text = "".join(c.content for c in tokens[i + 1].children or [] if c.type in ("text", "code_inline"))
            tok.attrSet("id", heading_id(text, used))
    return MD.renderer.render(tokens, MD.options, {})


def strip_html(s: str) -> str:
    """タグとコメントを落とした平文(文字参照は文字に戻す。描くときにテンプレートがエスケープする)。"""
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<!--.*?-->|<[^>]+>", "", s, flags=re.DOTALL)).strip())


def as_date(v) -> datetime.date | None:
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()):
        return datetime.date.fromisoformat(v.strip())
    return None


def read_doc(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = FM.match(text)
    if not m:
        return {}, text
    return (yaml.load(m.group(1), Loader=LOADER) or {}), m.group(2)


@dataclass
class Doc:
    data: dict
    url: str
    path: str                      # 版のフォルダからの相対パス(_posts/2026-11-16-x.md)
    content: str = ""              # 本文の HTML
    excerpt: str = ""              # 本文の最初の段落の HTML(Jekyll の excerpt)
    date: datetime.date | None = None

    def __getattr__(self, k):      # テンプレートからは front matter の項目をそのまま引く(無ければ None)
        if k.startswith("__"):
            raise AttributeError(k)
        return self.data.get(k)

    @property
    def stem(self) -> str:
        return Path(self.path).stem


@dataclass
class Site:
    root: Path
    posts: list = field(default_factory=list)          # site.posts の順
    editions: list = field(default_factory=list)       # 日付の古い順
    editorials: list = field(default_factory=list)
    brands: list = field(default_factory=list)
    tags: dict = field(default_factory=dict)
    about: Doc | None = None
    by_url: dict = field(default_factory=dict)
    by_stem: dict = field(default_factory=dict)
    edition_by_date: dict = field(default_factory=dict)
    edition_posts: dict = field(default_factory=dict)  # 号の日付 → 段の順に並べた記事
    followers: dict = field(default_factory=dict)      # 記事の stem → その記事を follows に挙げた記事(site.posts の順)
    editorial_by_date: dict = field(default_factory=dict)

    def brand(self, bid) -> dict:
        return next((b for b in self.brands if str(b.get("id")) == str(bid)), {})


def _body(doc: Doc, body: str) -> None:
    doc.content = render_markdown(body)
    first = body.lstrip("\n").split("\n\n", 1)[0]
    doc.excerpt = render_markdown(first) if first.strip() else ""


def load(root: Path) -> Site:
    root = Path(root)
    site = Site(root=root)
    site.brands = yaml.safe_load((root / "_data" / "brands.yml").read_text(encoding="utf-8")) or []
    site.tags = yaml.safe_load((root / "_data" / "tags.yml").read_text(encoding="utf-8")) or {}

    posts = []
    for p in sorted((root / "_posts").glob("*.md")):
        m = re.match(r"(\d{4}-\d{2}-\d{2})-(.+)\.md$", p.name)
        if not m:
            continue
        data, body = read_doc(p)
        date = as_date(data.get("date")) or datetime.date.fromisoformat(m.group(1))
        doc = Doc(data=data, url=f"/articles/{date:%Y-%m-%d}-{m.group(2)}/", path=f"_posts/{p.name}", date=date)
        _body(doc, body)
        posts.append(doc)
    posts.sort(key=lambda d: (d.date, d.path))
    site.posts = list(reversed(posts))
    for d in site.posts:
        site.by_url[d.url] = d
        site.by_stem[d.stem] = d

    for p in sorted((root / "_editions").glob("*.md")):
        data, _ = read_doc(p)
        doc = Doc(data=data, url=f"/editions/{p.stem}/", path=f"_editions/{p.name}", date=as_date(data.get("date")))
        site.editions.append(doc)
    site.editions.sort(key=lambda d: (d.date or datetime.date.min))
    for e in site.editions:
        site.edition_by_date[e.date] = e
        site.by_url[e.url] = e

    for p in sorted((root / "_editorials").glob("*.md")):
        data, body = read_doc(p)
        doc = Doc(data=data, url=f"/editorials/{p.stem}/", path=f"_editorials/{p.name}", date=as_date(data.get("edition")))
        _body(doc, body)
        site.editorials.append(doc)
        site.by_url[doc.url] = doc
        if doc.date:
            site.editorial_by_date[doc.date] = doc
    site.editorials.sort(key=lambda d: (d.date or datetime.date.min, d.path))

    about = root / "about" / "index.md"
    if about.exists():
        data, body = read_doc(about)
        site.about = Doc(data=data, url="/about/", path="about/index.md")
        _body(site.about, body)

    for d in site.posts:
        ed = as_date(d.data.get("edition"))
        if ed and d.data.get("rank") in RANKS:
            site.edition_posts.setdefault(ed, {r: [] for r in RANKS})[d.data["rank"]].append(d)
        for f in d.data.get("follows") or []:
            site.followers.setdefault(str(f), []).append(d)
    site.edition_posts = {k: [p for r in RANKS for p in v[r]] for k, v in site.edition_posts.items()}
    return site


class Holder:
    """配信中の版の Site。current の指す先が変わったら読み直す(読み直しの間は前の版で答える)。
    新しい版が読めなければ、前の版で答え続け、同じ版を読み直さない(要求のたびに読み直して遅くならない)。
    読めなかった版は failed に残す(/healthz が知らせる)。配信は current を切り替える前に同じ load で版を確かめる。"""

    def __init__(self, current: Path):
        self.current = Path(current)
        self.site: Site | None = None
        self.target: str = ""
        self.failed: dict = {}          # 読めなかった版 → 例外の要約
        self.lock = threading.Lock()

    def get(self) -> Site:
        target = str(self.current.resolve())
        if self.site is not None and (target == self.target or target in self.failed):
            return self.site
        with self.lock:
            if (self.site is None or target != self.target) and (self.site is None or target not in self.failed):
                try:
                    site = load(Path(target))
                except Exception as e:      # noqa: BLE001 — 前の版があれば、それで答え続ける
                    self.failed[target] = f"{type(e).__name__}: {str(e)[:300]}"
                    if self.site is None:
                        raise
                else:
                    self.site, self.target = site, target
        return self.site


def escape(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)
