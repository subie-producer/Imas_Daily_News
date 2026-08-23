#!/usr/bin/env python3
"""アイマスNEWS(α) 動的ページのアプリ。

紙面そのもの(記事・号・社説)は発行後に変わらないため静的配信のままで、
**問い合わせに応じて形が変わるページ**だけをここが担う:

  /tags/            タグ索引
  /tags/<タグ>/      タグ別の記事一覧(ページ送り)
  /search           全文検索 + タグ/ブランド絞り込み(FTS5)
  /healthz          稼働確認

データは scripts/indexer.py が作る SQLite(FTS5)。発行のたびに追記され、
アプリは読み取り専用で開く。前段は Caddy → Cloudflare。
"""
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, abort, g, redirect, render_template, request, url_for

DB_PATH = Path(os.environ.get("PAPER_DB", str(Path.home() / "srv" / "imas-news" / "data" / "paper.db")))
SITE_URL = os.environ.get("SITE_URL", "https://imas-news.ofa.tokyo").rstrip("/")
PER_PAGE = 50
# タグページは発行時にしか変わらないのでエッジで長く持たせる(発行時にパージ)。
# 検索は任意クエリでキャッシュが際限なく増えるため短めにする。
CACHE_TAGS = "public, max-age=600, s-maxage=86400"
CACHE_SEARCH = "public, max-age=60, s-maxage=600"

app = Flask(__name__)
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True


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
    try:
        n = db().execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        at = db().execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
        return {"ok": True, "articles": n, "indexed_at": at[0] if at else None}
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}, 503


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


@app.errorhandler(404)
def not_found(_e):
    # 静的側の 404 ページに寄せて見た目を揃える
    p = Path(os.environ.get("STATIC_ROOT", str(Path.home() / "srv" / "imas-news" / "current"))) / "404.html"
    if p.exists():
        return p.read_text(encoding="utf-8"), 404
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
