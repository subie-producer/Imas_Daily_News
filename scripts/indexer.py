#!/usr/bin/env python3
"""indexer: 紙面(docs/_posts ほか)から検索用の SQLite インデックスを作る。

  python3 scripts/indexer.py [--full] [--db PATH]

動的ページ(タグ・検索・横断クエリ)はこの DB を引く。紙面ファイル自体は
append-only で不変なので、**内容ハッシュが変わったファイルだけ**を入れ直す
(既定は差分更新。--full で作り直す)。記事が数万本に育っても、毎回の更新は
その日に増えた分だけで済む。

タグの表記ゆれは docs/_data/tags.yml に従って投入時に正規化する
(過去記事の frontmatter は書き換えない = append-only を守る)。
"""
import argparse
import hashlib
import re
import sqlite3
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tags as tags_lib

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
DEFAULT_DB = Path.home() / "srv" / "imas-news" / "data" / "paper.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  slug        TEXT NOT NULL,
  edition     TEXT NOT NULL,
  url         TEXT NOT NULL,
  title       TEXT NOT NULL,
  lede        TEXT,
  body        TEXT,
  brand       TEXT,
  rank        TEXT,
  src         TEXT,
  event_date  TEXT,
  corrected   INTEGER DEFAULT 0,
  path        TEXT PRIMARY KEY,
  sha         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_edition ON articles(edition DESC);
CREATE INDEX IF NOT EXISTS idx_articles_brand   ON articles(brand);

CREATE TABLE IF NOT EXISTS article_tags (
  path TEXT NOT NULL REFERENCES articles(path) ON DELETE CASCADE,
  tag  TEXT NOT NULL,
  PRIMARY KEY (path, tag)
);
CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag);

CREATE TABLE IF NOT EXISTS editions (
  date          TEXT PRIMARY KEY,
  number        INTEGER,
  weekday       TEXT,
  article_count INTEGER,
  corrected_count INTEGER DEFAULT 0,
  lead_slug     TEXT,
  path          TEXT,
  sha           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS editorials (
  date    TEXT PRIMARY KEY,
  title   TEXT,
  excerpt TEXT,
  body    TEXT,
  path    TEXT,
  sha     TEXT NOT NULL
);

-- 全文検索。日本語は空白で切れないため trigram で引く(部分一致が効く)
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
  title, lede, body, tags, path UNINDEXED,
  tokenize = 'trigram'
);

-- 表示名・色は docs/_data/brands.yml が単一ソース。アプリが yml を直接読まなくて
-- 済むよう、インデックス更新時に取り込む
CREATE TABLE IF NOT EXISTS brands (
  id    TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  color TEXT NOT NULL,
  ord   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def parse_front_matter(text: str) -> tuple[dict, str]:
    m = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        return (yaml.safe_load(m.group(1)) or {}), m.group(2)
    except yaml.YAMLError:
        return {}, m.group(2)


def as_date(v) -> str:
    return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v or "")


def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def strip_markdown(body: str) -> str:
    """検索用の素文。見出し記号・リンク装飾を落として本文語だけ残す。"""
    body = re.sub(r"^#+\s*", "", body, flags=re.MULTILINE)
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
    body = re.sub(r"[*_`>]", "", body)
    return re.sub(r"\n{2,}", "\n", body).strip()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 読み取り(アプリ)と書き込み(発行時のインデックス更新)が同時に走るため WAL
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def index_articles(conn: sqlite3.Connection, spec: dict, full: bool) -> tuple[int, int]:
    known = {} if full else {r["path"]: r["sha"] for r in conn.execute("SELECT path, sha FROM articles")}
    seen, added = set(), 0

    for path in sorted((DOCS / "_posts").glob("*.md")):
        rel = str(path.relative_to(ROOT))
        seen.add(rel)
        text = path.read_text(encoding="utf-8")
        sha = sha_of(text)
        if known.get(rel) == sha:
            continue

        fm, body = parse_front_matter(text)
        if not fm.get("slug"):
            continue
        edition = as_date(fm.get("edition"))
        slug = str(fm["slug"])
        plain = strip_markdown(body)
        tag_list = tags_lib.normalize(fm.get("tags"), spec)

        conn.execute("DELETE FROM search WHERE path = ?", (rel,))
        conn.execute("DELETE FROM article_tags WHERE path = ?", (rel,))
        conn.execute("""
            INSERT INTO articles (slug, edition, url, title, lede, body, brand, rank, src,
                                  event_date, corrected, path, sha)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              slug=excluded.slug, edition=excluded.edition, url=excluded.url,
              title=excluded.title, lede=excluded.lede, body=excluded.body,
              brand=excluded.brand, rank=excluded.rank, src=excluded.src,
              event_date=excluded.event_date, corrected=excluded.corrected, sha=excluded.sha
        """, (slug, edition, f"/articles/{edition}-{slug}/", str(fm.get("title") or ""),
              str(fm.get("lede") or ""), plain, str(fm.get("brand") or ""),
              str(fm.get("rank") or ""), str(fm.get("src") or ""),
              as_date(fm.get("event_date")) or None,
              1 if fm.get("corrected") else 0, rel, sha))
        conn.executemany("INSERT OR IGNORE INTO article_tags (path, tag) VALUES (?,?)",
                         [(rel, t) for t in tag_list])
        conn.execute("INSERT INTO search (title, lede, body, tags, path) VALUES (?,?,?,?,?)",
                     (str(fm.get("title") or ""), str(fm.get("lede") or ""), plain,
                      " ".join(tag_list), rel))
        added += 1

    # 紙面は削除されない前提だが、訂正フローでの入替に追随できるよう掃除はしておく
    removed = 0
    if not full:
        for rel in [r["path"] for r in conn.execute("SELECT path FROM articles")]:
            if rel not in seen:
                conn.execute("DELETE FROM search WHERE path = ?", (rel,))
                conn.execute("DELETE FROM articles WHERE path = ?", (rel,))
                removed += 1
    return added, removed


def index_collection(conn: sqlite3.Connection, subdir: str, table: str, full: bool) -> int:
    src = DOCS / subdir
    if not src.exists():
        return 0
    known = {} if full else {r["date"]: r["sha"] for r in conn.execute(f"SELECT date, sha FROM {table}")}
    n = 0
    for path in sorted(src.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        sha = sha_of(text)
        date = path.stem
        if known.get(date) == sha:
            continue
        fm, body = parse_front_matter(text)
        rel = str(path.relative_to(ROOT))
        if table == "editions":
            conn.execute("""
                INSERT INTO editions (date, number, weekday, article_count, corrected_count,
                                      lead_slug, path, sha)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(date) DO UPDATE SET
                  number=excluded.number, weekday=excluded.weekday,
                  article_count=excluded.article_count, corrected_count=excluded.corrected_count,
                  lead_slug=excluded.lead_slug, sha=excluded.sha
            """, (as_date(fm.get("date")) or date, fm.get("number"), str(fm.get("weekday") or ""),
                  fm.get("article_count") or 0, fm.get("corrected_count") or 0,
                  str(fm.get("lead_slug") or ""), rel, sha))
        else:
            conn.execute("""
                INSERT INTO editorials (date, title, excerpt, body, path, sha)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(date) DO UPDATE SET
                  title=excluded.title, excerpt=excluded.excerpt, body=excluded.body, sha=excluded.sha
            """, (as_date(fm.get("date")) or date, str(fm.get("title") or ""),
                  str(fm.get("excerpt") or ""), body.strip(), rel, sha))
        n += 1
    return n


def index_brands(conn: sqlite3.Connection) -> int:
    p = DOCS / "_data" / "brands.yml"
    if not p.exists():
        return 0
    rows = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    conn.execute("DELETE FROM brands")
    conn.executemany(
        "INSERT INTO brands (id, label, color, ord) VALUES (?,?,?,?)",
        [(str(b["id"]), str(b["label"]), str(b["color"]), i) for i, b in enumerate(rows)])
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="差分更新せず作り直す")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    db_path = Path(args.db)
    if args.full and db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)

    spec = tags_lib.load()
    conn = connect(db_path)
    with conn:
        a_add, a_del = index_articles(conn, spec, args.full)
        n_ed = index_collection(conn, "_editions", "editions", args.full)
        n_eo = index_collection(conn, "_editorials", "editorials", args.full)
        index_brands(conn)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('indexed_at', ?)",
                     (time.strftime("%Y-%m-%dT%H:%M:%S"),))
    conn.execute("PRAGMA optimize")
    totals = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("articles", "article_tags", "editions", "editorials")}
    conn.close()

    if not args.quiet:
        print(f"インデックス更新: 記事 +{a_add}/-{a_del} 号 +{n_ed} 社説 +{n_eo} "
              f"({time.time() - t0:.2f}s)", flush=True)
        print(f"  合計: 記事 {totals['articles']} / タグ付与 {totals['article_tags']} / "
              f"号 {totals['editions']} / 社説 {totals['editorials']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
