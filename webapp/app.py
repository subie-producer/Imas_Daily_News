#!/usr/bin/env python3
"""アイマスNEWS(α) 動的ページのアプリ。

紙面のページは全部ここが描く(ビルドしない。編集長の決定 2026-11-16):

  /  /editions/<日付>/  /articles/<日付-slug>/  /editorials/<日付>/  /archive/  /about/
  /feed.xml  /sitemap.xml  /robots.txt  /release-id.txt  /404.html
  /tags/            タグ索引
  /tags/<タグ>/      タグ別の記事一覧(ページ送り)
  /search           全文検索 + タグ/ブランド絞り込み(FTS5)
  /healthz          稼働確認

紙面のデータは配信の版(current が指す releases/<日時>/ の Markdown 一式)を paper.load で読んで持つ。
current が別の版に切り替わったら読み直す。タグ・検索は scripts/indexer.py が作る SQLite(FTS5)を読み取り専用で開く。
静的ファイルは /assets/ だけ(Caddy が配る)。前段は Caddy → Cloudflare。
"""
import datetime
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from flask import Flask, Response, abort, g, redirect, render_template, request, url_for

import paper

DB_PATH = Path(os.environ.get("PAPER_DB", str(Path.home() / "srv" / "imas-news" / "data" / "paper.db")))
SITE_URL = os.environ.get("SITE_URL", "https://news.imas.fans").rstrip("/")
# feed の id は購読者の既読の目印なので、ドメインを移っても変えない(2026-11-17 に imas-news.ofa.tokyo から移った)
FEED_ID_BASE = "https://imas-news.ofa.tokyo"
SITE_ROOT = Path(os.environ.get("SITE_ROOT", str(Path.home() / "srv" / "imas-news" / "current")))
SITE_TITLE = "アイマスNEWS(α)"
SITE_DESC = "AI編集部が毎日自動発行する非公式アイドルマスターファン新聞"
RELEASE_ID = "release-id.txt"
PER_PAGE = 50
# 紙面とタグページは発行・訂正のときにしか変わらないのでエッジで長く持たせる(発行・訂正の配信でパージする)。
# オリジン(このアプリ)が落ちているときは、エッジが持っている古い版で1週間まで答える(stale-if-error。
# ビルドした静的ファイルを Caddy が配っていたときは、アプリが落ちても紙面は読めた)。
# 検索は任意クエリでキャッシュが際限なく増えるため短めにする。
CACHE_PAGES = "public, max-age=600, s-maxage=86400, stale-if-error=604800"
CACHE_TAGS = CACHE_PAGES
CACHE_SEARCH = "public, max-age=60, s-maxage=600"
JST = datetime.timezone(datetime.timedelta(hours=9))

app = Flask(__name__)
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True
HOLDER = paper.Holder(SITE_ROOT)


def site() -> paper.Site:
    if "site" not in g:
        try:
            g.site = HOLDER.get()
        except FileNotFoundError:
            abort(503)
    return g.site


# ---- テンプレートの道具(以前の Liquid のフィルタと同じ結果を返す) ----

def fmt(v, f: str) -> str:
    d = paper.as_date(v)
    if d is None and isinstance(v, str):
        try:
            d = datetime.datetime.fromisoformat(v.strip())
        except ValueError:
            return v
    return d.strftime(f) if d is not None else ""


def trunc(s, n: int) -> str:
    """Liquid の truncate: n 文字を超えたら、末尾の ... を含めて n 文字にする。"""
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: max(0, n - 3)] + "..."


def uri_escape(s: str) -> str:
    return quote(str(s), safe="!#$&'()*+,/:;=?@[]~-._")


def norm_tags(tags) -> list:
    """タグの正規化(docs/_data/tags.yml の alias・drop。表記ゆれを寄せ、重複を落とす)。"""
    spec = site().tags or {}
    alias, drop, out = spec.get("alias") or {}, spec.get("drop") or [], []
    for raw in tags or []:
        t = alias.get(raw, raw)
        if t not in drop and t not in out:
            out.append(t)
    return out


def neighbors(eposts: list, page) -> dict:
    urls = [p.url for p in eposts]
    if page.url not in urls:
        return {"prev": eposts[-1] if eposts else None, "next": None}
    i = urls.index(page.url)
    return {"prev": eposts[i - 1] if i > 0 else None, "next": eposts[i + 1] if i + 1 < len(eposts) else None}


def groupby_month(eds) -> list:
    out: list = []
    for e in eds:
        name = fmt(e.date, "%Y.%m")
        if not out or out[-1][0] != name:
            out.append((name, []))
        out[-1][1].append(e)
    return out


app.jinja_env.globals.update(fmt=fmt, trunc=trunc, uri_escape=uri_escape, norm_tags=norm_tags, neighbors=neighbors,
                             page_date=paper.as_date, brand=lambda bid: site().brand(bid))
app.jinja_env.filters["groupby_month"] = groupby_month


def db() -> sqlite3.Connection:
    if "db" not in g:
        if not DB_PATH.exists():
            abort(503)
        # 読み取り専用で開く。発行時のインデックス更新(WAL)と同時に走っても安全
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def brands_map() -> dict:
    return {r["id"]: r for r in db().execute("SELECT id, label, color FROM brands ORDER BY ord")}


def brand_list() -> list:
    return list(db().execute("SELECT id, label FROM brands ORDER BY ord"))


def fts_query(q: str) -> str:
    """利用者の入力を FTS5 のクエリに変換する。空白区切りの語は AND。
    trigram トークナイザなので各語をフレーズとして引用し、記号は落とす。"""
    terms = []
    for raw in q.replace("　", " ").split():
        t = raw.replace('"', "").strip()
        # trigram は3文字未満を索引できないため、短すぎる語は落とす(0件を防ぐ)
        if len(t) >= 3:
            terms.append(f'"{t}"')
    return " AND ".join(terms)


@app.route("/healthz")
def healthz():
    """稼働確認。索引と、紙面の版(配信中の版・読めなかった版)を返す。紙面の版が読めなければ 503。"""
    try:
        n = db().execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        at = db().execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}, 503
    try:
        s = HOLDER.get()
    except Exception as e:      # noqa: BLE001 — 版が1つも読めない
        return {"ok": False, "error": f"紙面の版を読めない: {type(e).__name__}: {str(e)[:200]}"}, 503
    target = str(SITE_ROOT.resolve())
    paper_info = {"release": Path(s.root).name, "posts": len(s.posts), "editions": len(s.editions),
                  "current": Path(target).name, "unreadable": HOLDER.failed.get(target)}
    return {"ok": not paper_info["unreadable"], "articles": n, "indexed_at": at[0] if at else None, "paper": paper_info}


@app.route("/tags/")
def tags_index():
    rows = list(db().execute("""
        SELECT tag, COUNT(*) AS n FROM article_tags
        GROUP BY tag ORDER BY n DESC, tag
    """))
    total_articles = db().execute("SELECT COUNT(*) FROM articles").fetchone()[0]

    # 件数で層を分けて出す。全部を平坦に並べるとタグが増えたとき読めなくなる。
    # キー名を items にすると Jinja2 で dict.items メソッドに解決されるため entries とする
    groups = [
        {"label": "よく使われているタグ", "entries": [t for t in rows if t["n"] >= 10]},
        {"label": "ときどき使われるタグ", "entries": [t for t in rows if 3 <= t["n"] < 10]},
        {"label": "その他のタグ", "entries": [t for t in rows if t["n"] < 3]},
    ]
    groups = [gr for gr in groups if gr["entries"]]

    resp = app.make_response(render_template(
        "tags.html", tags=rows, groups=groups, total_articles=total_articles, site_url=SITE_URL))
    resp.headers["Cache-Control"] = CACHE_TAGS
    return resp


@app.route("/tags/<path:tag>/")
def tag_page(tag: str):
    tag = tag.strip("/")
    total = db().execute("SELECT COUNT(*) FROM article_tags WHERE tag = ?", (tag,)).fetchone()[0]
    if not total:
        abort(404)

    page = max(1, request.args.get("page", 1, type=int))
    pages = max(1, -(-total // PER_PAGE))
    if page > pages:
        return redirect(url_for("tag_page", tag=tag, page=pages))

    articles = list(db().execute("""
        SELECT a.* FROM articles a
        JOIN article_tags t ON t.path = a.path
        WHERE t.tag = ?
        ORDER BY a.edition DESC, a.slug
        LIMIT ? OFFSET ?
    """, (tag, PER_PAGE, (page - 1) * PER_PAGE)))

    # 同じ記事に付いている他のタグ = 掘り下げの入口
    related = list(db().execute("""
        SELECT t2.tag AS tag, COUNT(*) AS n
        FROM article_tags t1 JOIN article_tags t2 ON t1.path = t2.path
        WHERE t1.tag = ? AND t2.tag != ?
        GROUP BY t2.tag ORDER BY n DESC, t2.tag LIMIT 10
    """, (tag, tag)))

    resp = app.make_response(render_template(
        "tag.html", tag=tag, articles=articles, related=related, brands=brands_map(),
        total=total, page=page, pages=pages, base_qs="", site_url=SITE_URL))
    resp.headers["Cache-Control"] = CACHE_TAGS
    return resp


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    sel_tags = [t for t in request.args.getlist("tag") if t.strip()]
    brand = (request.args.get("brand") or "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    active = bool(q or sel_tags or brand)

    articles, total, pages, related = [], 0, 1, []
    if active:
        where, params = [], []
        joins = ""
        if q:
            expr = fts_query(q)
            if expr:
                joins += " JOIN search s ON s.path = a.path"
                where.append("search MATCH ?")
                params.append(expr)
            else:
                # 2文字以下しか無い入力は trigram で引けないため見出しの部分一致で拾う
                where.append("(a.title LIKE ? OR a.lede LIKE ?)")
                params += [f"%{q}%", f"%{q}%"]
        for t in sel_tags:
            where.append("EXISTS (SELECT 1 FROM article_tags x WHERE x.path = a.path AND x.tag = ?)")
            params.append(t)
        if brand:
            where.append("a.brand = ?")
            params.append(brand)

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = db().execute(f"SELECT COUNT(*) FROM articles a{joins}{clause}", params).fetchone()[0]
        pages = max(1, -(-total // PER_PAGE))
        page = min(page, pages)
        order = "bm25(search), a.edition DESC" if (q and joins) else "a.edition DESC, a.slug"
        articles = list(db().execute(
            f"SELECT a.* FROM articles a{joins}{clause} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [PER_PAGE, (page - 1) * PER_PAGE]))

        if articles:
            paths = [a["path"] for a in articles]
            ph = ",".join("?" * len(paths))
            related = list(db().execute(
                f"""SELECT tag, COUNT(*) AS n FROM article_tags
                    WHERE path IN ({ph}) {'AND tag NOT IN (' + ','.join('?' * len(sel_tags)) + ')' if sel_tags else ''}
                    GROUP BY tag ORDER BY n DESC, tag LIMIT 10""",
                paths + sel_tags))

    top_tags = [] if active else list(db().execute("""
        SELECT tag, COUNT(*) AS n FROM article_tags GROUP BY tag ORDER BY n DESC, tag LIMIT 30
    """))

    def qs(**over) -> str:
        pairs = []
        val_q = over.get("q", q)
        if val_q:
            pairs.append(("q", val_q))
        for t in over.get("tags", sel_tags):
            pairs.append(("tag", t))
        if over.get("brand", brand):
            pairs.append(("brand", over.get("brand", brand)))
        return urlencode(pairs) + ("&" if pairs else "")

    resp = app.make_response(render_template(
        "search.html", q=q, sel_tags=sel_tags, brand=brand, articles=articles,
        brands=brands_map(), brand_list=brand_list(), total=total, page=page, pages=pages,
        related=related, top_tags=top_tags, active=active, base_qs=qs(),
        add_tag_url=lambda t: "/search?" + qs(tags=sel_tags + [t]).rstrip("&"),
        drop_tag_url=lambda t: "/search?" + qs(tags=[x for x in sel_tags if x != t]).rstrip("&"),
        site_url=SITE_URL))
    resp.headers["Cache-Control"] = CACHE_SEARCH
    return resp


# ---- 紙面のページ ----

def iso(d, t: str = "00:00:00") -> str:
    return f"{d:%Y-%m-%d}T{t}+09:00" if d else ""


def released_at() -> str:
    """この版を配信に出した時刻(feed の updated・社説の公開時刻)。版の名前(YYYYmmddTHHMMSS)から。"""
    try:
        return datetime.datetime.strptime(Path(site().root).name[:15], "%Y%m%dT%H%M%S").strftime("%Y-%m-%dT%H:%M:%S+09:00")
    except ValueError:
        return datetime.datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def seo_for(path: str, title: str = "", description: str = "", published: str = "", kind: str = "WebPage") -> dict:
    url = SITE_URL + path
    title = paper.smartify(title or SITE_TITLE)     # 見出しの引用符は活字に(以前の jekyll-seo-tag の smartify と同じ)
    description = description or SITE_DESC
    ld = {"@context": "https://schema.org", "@type": kind}
    if published:
        ld.update({"@type": "BlogPosting", "dateModified": published, "datePublished": published, "description": description,
                   "headline": title, "mainEntityOfPage": {"@type": "WebPage", "@id": url}, "url": url})
    else:
        ld.update({"description": description, "headline": title})
        if kind == "WebSite":
            ld["name"] = SITE_TITLE
        ld["url"] = url
    return {"title": title, "title_tag": f"{SITE_TITLE} | {SITE_DESC}" if path == "/" else f"{title} | {SITE_TITLE}",
            "description": description, "url": url, "published": published,
            "ld_json": json.dumps(ld, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")}


def page(template: str, status: int = 200, **ctx):
    resp = app.make_response((render_template(template, site=site(), site_url=SITE_URL, **ctx), status))
    resp.headers["Cache-Control"] = CACHE_PAGES
    return resp


@app.route("/")
def front():
    s = site()
    if not s.editions:
        return page("paper/about.html", doc=paper.Doc(data={}, url="/", path="", content="<main style=\"max-width:720px;margin:40px auto;padding:0 20px\"><p>準備中。</p></main>"),
                    seo=seo_for("/", kind="WebSite"))
    return page("paper/edition.html", ed=s.editions[-1], seo=seo_for("/", kind="WebSite"))


@app.route("/editions/<name>/")
def edition(name: str):
    ed = site().by_url.get(f"/editions/{name}/")
    if ed is None:
        abort(404)
    # 号のページの見出しは、以前と同じくファイル名から(2026-11-16 → "2026 11 16")
    return page("paper/edition.html", ed=ed, seo=seo_for(ed.url, title=name.replace("-", " "), published=iso(ed.date)))


@app.route("/articles/<name>/")
def article(name: str):
    doc = site().by_url.get(f"/articles/{name}/")
    if doc is None:
        abort(404)
    return page("paper/article.html", page=doc,
                seo=seo_for(doc.url, title=str(doc.title or ""), description=paper.strip_html(doc.excerpt), published=iso(doc.date)))


@app.route("/editorials/<name>/")
def editorial(name: str):
    doc = site().by_url.get(f"/editorials/{name}/")
    if doc is None:
        abort(404)
    return page("paper/editorial.html", page=doc,
                seo=seo_for(doc.url, title=str(doc.title or ""), description=str(doc.data.get("excerpt") or paper.strip_html(doc.excerpt)),
                            published=released_at()))


@app.route("/archive/")
def archive():
    return page("paper/archive.html", seo=seo_for("/archive/", title=f"バックナンバー — {SITE_TITLE}"))


@app.route("/about/")
def about():
    doc = site().about
    if doc is None:
        abort(404)
    return page("paper/about.html", doc=doc, seo=seo_for("/about/", title=str(doc.title or ""), kind="WebSite"))


@app.route("/404.html")
def page_404():
    return page("paper/404.html", seo=seo_for("/404.html", title=f"ページが見つかりません — {SITE_TITLE}"))


def xml(body: str) -> Response:
    resp = Response(body, mimetype="application/xml")
    resp.headers["Cache-Control"] = CACHE_PAGES
    return resp


def x(s) -> str:
    return paper.escape(s)


@app.route("/feed.xml")
def feed():
    """Atom の feed(最新10本。以前の jekyll-feed と同じ id・項目)。"""
    s = site()
    posts = sorted(s.posts, key=lambda p: p.path)
    posts = sorted(posts, key=lambda p: p.date, reverse=True)[:10]
    out = ['<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom" xml:lang="ja">',
           f'<link href="{SITE_URL}/feed.xml" rel="self" type="application/atom+xml" />',
           f'<link href="{SITE_URL}/" rel="alternate" type="text/html" hreflang="ja" />',
           f"<updated>{released_at()}</updated><id>{FEED_ID_BASE}/feed.xml</id>",
           f'<title type="html">{x(SITE_TITLE)}</title><subtitle>{x(SITE_DESC)}</subtitle>']
    for p in posts:
        url = SITE_URL + p.url
        out.append(
            f'<entry><title type="html">{x(p.title)}</title><link href="{url}" rel="alternate" type="text/html" title="{x(p.title)}" />'
            f"<published>{iso(p.date)}</published><updated>{iso(p.date)}</updated>"
            f"<id>{FEED_ID_BASE}/articles/{p.stem[11:]}</id>"
            f'<content type="html" xml:base="{url}"><![CDATA[{p.content.replace("]]>", "]]&gt;")}]]></content>'
            "<author><name></name></author>"
            + "".join(f'<category term="{x(t)}" />' for t in (p.tags or []))
            + f'<summary type="html"><![CDATA[{paper.strip_html(p.excerpt).replace("]]>", "]]&gt;")}]]></summary></entry>')
    out.append("</feed>")
    return xml("".join(out))


@app.route("/sitemap.xml")
def sitemap():
    s = site()
    rows = [(e.url, iso(e.date)) for e in s.editions]
    rows += [(e.url, released_at()) for e in s.editorials]
    rows += [(p.url, iso(p.date)) for p in sorted(s.posts, key=lambda p: p.url)]
    rows += [("/archive/", ""), ("/", ""), ("/about/", "")]
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
           'xsi:schemaLocation="http://www.sitemaps.org/schemas/sitemap/0.9 http://www.sitemaps.org/schemas/sitemap/0.9/sitemap.xsd" '
           'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, mod in rows:
        out.append(f"<url>\n<loc>{SITE_URL}{x(loc)}</loc>\n" + (f"<lastmod>{mod}</lastmod>\n" if mod else "") + "</url>")
    out.append("</urlset>\n")
    return xml("\n".join(out))


@app.route("/robots.txt")
def robots():
    resp = Response(f"Sitemap: {SITE_URL}/sitemap.xml\n", mimetype="text/plain")
    resp.headers["Cache-Control"] = CACHE_PAGES
    return resp


@app.route("/" + RELEASE_ID)
def release_id():
    """配信中の版の名前(外から新しい版が見えるかを deploy が確かめる)。キャッシュさせない。"""
    p = Path(site().root) / RELEASE_ID
    resp = Response(p.read_text(encoding="utf-8") if p.exists() else "", mimetype="text/plain")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(404)
def not_found(_e):
    try:
        return page("paper/404.html", status=404, seo=seo_for("/404.html", title=f"ページが見つかりません — {SITE_TITLE}"))
    except Exception:   # noqa: BLE001 — 版が読めないときも 404 は返す
        return "404 Not Found", 404


@app.errorhandler(503)
def unavailable(_e):
    return "インデックスを準備中です。しばらくしてから再度お試しください。", 503


if __name__ == "__main__":
    from waitress import serve
    host = os.environ.get("BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("BIND_PORT", "8081"))
    print(f"imas-news app: http://{host}:{port} (db={DB_PATH})", flush=True)
    serve(app, host=host, port=port, threads=8, ident=None)
