#!/usr/bin/env python3
"""page_archive: 出典ページの原本の保存(読んだ時点の HTML・本文・最終 URL・状態コード・取得時刻)。

編集長 2026-10-26「収集するタイミングでこういう記事だった(HTMLぺたー)すりゃいいんだよ。読み直す度にアクセスするから失敗するんだよ」。
確かめ・執筆・書き直し・校閲・当番が出典を読むたびに相手のサイトへ取りに行き、公式ポータルのメンテナンス・ランティスの 503・
Anime Store.JP の 429 で読めず、収集時に本文で確かめた素材まで「出典を確かめられない」として見送っていた(10/22・10/26 号)。

- 収集(定点観測の本文・裏取り verify・探索と X の確かめのセッションが fetch_page.py で読んだページ)が読んだ時点の原本を版として残す
- fetch_page.py は保存があれば必ず読み直し、同じなら「確認済み」、変わっていれば新しい版を残して新旧を見せ、読めなければ保存を返す

置き場は ops の本体の metrics/work/pages/(Git 管理外。どの作業ツリー(当番・監査)から走っても本体の同じ置き場を読み書きする: archive_dir)。
URL ごとのフォルダに index.json(版の一覧)・v<n>.json(版の情報と本文)・v<n>.html.gz(HTML)を置く。
"""
import datetime
import difflib
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = datetime.timezone(datetime.timedelta(hours=9))
ARCHIVE_ENV = "IMAS_PAGE_ARCHIVE"     # 置き場の差し替え(テスト・収集の隔離されたセッションの一時の置き場)
ARCHIVE_CONF = "PAGE_ARCHIVE"         # ops の本体の .env に書く置き場(無ければ本体の metrics/work/pages)
MAX_VERSIONS = 10                     # 1つの URL に残す版の数(収集時点の最新の版は必ず残す)
MAX_AGE_DAYS = 120                    # 最後に読んだ・確かめた日からこれより古い URL は消す
MAX_BYTES = 4 * 1024 ** 3             # 置き場全体の上限。超えたら最後に読んだ日の古い URL から消す
HTML_MAX_BYTES = 5_000_000            # 残す HTML の上限(圧縮前。fetch_page の読み取りの上限と同じ)
DIFF_CHARS = 8000                     # 新旧の違いを見せる上限の字数


def main_worktree(root: Path) -> Path:
    """root が git worktree(当番・監査の使い捨ての作業ツリー)なら、その本体の作業ツリー。本体ならそのまま。"""
    g = root / ".git"
    try:
        if g.is_file():
            m = re.match(r"gitdir:\s*(.+)", g.read_text(encoding="utf-8").strip())
            if m:
                gd = Path(m.group(1).strip())
                gd = gd if gd.is_absolute() else (root / gd)
                common = gd
                cd = gd / "commondir"
                if cd.is_file():
                    c = Path(cd.read_text(encoding="utf-8").strip())
                    common = c if c.is_absolute() else (gd / c)
                common = common.resolve()
                if common.name == ".git":
                    return common.parent
    except OSError:
        pass
    return root


def archive_dir() -> Path:
    """置き場(呼ぶたびに決める)。環境変数 IMAS_PAGE_ARCHIVE > 本体の .env の PAGE_ARCHIVE > 本体の metrics/work/pages。
    当番の作業ツリーには .env が無く環境変数も絞られるので、作業ツリーからは本体を辿って同じ置き場を使う"""
    v = os.environ.get(ARCHIVE_ENV)
    if v:
        return Path(v)
    main = main_worktree(ROOT)
    try:
        for line in (main / ".env").read_text(encoding="utf-8").splitlines():
            k, _, val = line.strip().partition("=")
            if k.strip() == ARCHIVE_CONF and val.strip():
                return Path(val.strip())
    except OSError:
        pass
    return main / "metrics" / "work" / "pages"


def page_key(url: str) -> str:
    """同じページの鍵: ホストは小文字、http/https・フラグメント(#)・パスの末尾の / を区別しない(取得するのはページ全体)。"""
    u = urllib.parse.urlsplit(str(url or "").strip())
    path = u.path.rstrip("/") or "/"
    return f"{(u.hostname or '').lower()}{(':' + str(u.port)) if u.port else ''}{path}{('?' + u.query) if u.query else ''}"


def page_dir(url: str, base: Path | None = None) -> Path:
    h = hashlib.sha256(page_key(url).encode("utf-8")).hexdigest()
    return (base or archive_dir()) / h[:2] / h


def norm_text(text: str) -> str:
    """読みやすい本文(連なった空行を1つに詰める。fetch_page.py の出力と同じ)。"""
    return re.sub(r"\n[ \t　]*(?:\n[ \t　]*)+", "\n\n", str(text or "")).strip()


def compare_lines(text: str) -> list[str]:
    """照合に使う行(行ごとに空白を詰め、空行を除く)。空白・改行の揺れだけを変化と数えない。"""
    return [ln for ln in (re.sub(r"[ \t　]+", " ", x).strip() for x in str(text or "").splitlines()) if ln]


def text_sha(text: str) -> str:
    return hashlib.sha256("\n".join(compare_lines(text)).encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.datetime.now(JST).isoformat(timespec="seconds")


class _Lock:
    """URL のフォルダの排他(並列の fetch_page.py が同じページの版を同時に書かない)。"""
    def __init__(self, d: Path):
        self.d = d

    def __enter__(self):
        import fcntl
        self.d.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.d / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        os.close(self.fd)


def _write_atomic(p: Path, data: bytes) -> None:
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)


def _index(d: Path) -> dict:
    try:
        idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
        return idx if isinstance(idx, dict) and isinstance(idx.get("versions"), list) else {"versions": []}
    except (OSError, ValueError):
        return {"versions": []}


def _load(d: Path, v: dict) -> dict | None:
    """版の情報に本文を付けて返す(読めなければ None)。"""
    try:
        row = json.loads((d / f"v{int(v['n'])}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(row, dict) or not isinstance(row.get("text"), str):
        return None
    return {**row, **v, "text": row["text"]}       # 版の情報は index が正(収集の版への格上げ・確かめた時刻は index だけを更新する)


def fetched_at(v: dict) -> str:
    """版をいつ読んだか: 収集の版は収集が読んだいちばん新しい時刻(collected)、ほかは取得時刻(at)。"""
    return str((v.get("collected") if v.get("via") == "collect" else None) or v.get("at") or "")


def _by_time(vs: list[dict]) -> list[dict]:
    """版を取得時刻の古い順に並べる(取り込みの順ではない。Sol 監査 r1: 探索の預かりの古い版をあとで写すと、いちばん新しい版を取り違えた)。"""
    return sorted(vs, key=lambda v: (str(v.get("at") or ""), int(v.get("n") or 0)))


def _keep_collect(vs: list[dict]):
    """残す収集の版(収集が読んだ時刻のいちばん新しい版)の番号。"""
    col = [v for v in vs if v.get("via") == "collect"]
    return max(col, key=lambda v: (fetched_at(v), int(v.get("n") or 0)))["n"] if col else None


def save(url: str, text: str, html=None, final: str | None = None, status: int | None = 200, via: str = "read",
         at: str | None = None, base: Path | None = None, rendered: bool = False) -> dict | None:
    """読めたページの原本を残す。その時刻の直前の版(取得時刻で並べる)と本文が同じなら版を増やさず、確かめた時刻(seen)だけ更新する
    (収集が同じ本文を読んだら収集の版に格上げし、収集が読んだ時刻を collected に残す)。戻りはその版の情報(本文は含まない)。
    本文が空なら残さない。via は collect(収集が読んだ)/ read(執筆・校閲などが読み直した)。rendered は描画して読んだ本文か"""
    text = norm_text(text)
    if not text:
        return None
    at = at or now_iso()
    sha = text_sha(text)
    d = page_dir(url, base)
    with _Lock(d):
        idx = _index(d)
        vs = idx["versions"]
        prev = [v for v in _by_time(vs) if str(v.get("at") or "") <= at]
        if prev and prev[-1].get("sha") == sha:
            v = prev[-1]
            v["seen"] = max(str(v.get("seen") or ""), at)
            if via == "collect":
                v["via"] = "collect"      # 収集も同じ本文を読んだ: 収集時点の版として扱う
                v["collected"] = max(str(v.get("collected") or ""), at)
            v["rendered"] = bool(v.get("rendered") or rendered)
            _write_atomic(d / "index.json", json.dumps(idx, ensure_ascii=False).encode("utf-8"))
            return dict(v)
        n = max([int(v.get("n") or 0) for v in vs] + [0]) + 1
        meta = {"n": n, "at": at, "seen": at, "via": via, "collected": at if via == "collect" else None, "url": url,
                "final": final if final and final != url else None, "status": status, "sha": sha, "chars": len(text),
                "rendered": bool(rendered), "html": None}
        if html:
            raw = html.encode("utf-8", "replace") if isinstance(html, str) else bytes(html)
            _write_atomic(d / f"v{n}.html.gz", gzip.compress(raw[:HTML_MAX_BYTES], compresslevel=6))
            meta["html"] = f"v{n}.html.gz"
        _write_atomic(d / f"v{n}.json", json.dumps({**meta, "text": text}, ensure_ascii=False).encode("utf-8"))
        vs.append(meta)
        idx["url"] = url
        # 版の数を抑える: 取得時刻の古い版から消す。収集が読んだいちばん新しい版と、取得時刻のいちばん新しい版は残す
        while len(vs) > MAX_VERSIONS:
            keep = {_keep_collect(vs), _by_time(vs)[-1]["n"]}
            drop = next((v for v in _by_time(vs) if v["n"] not in keep), None)
            if drop is None:
                break
            vs.remove(drop)
            for name in (f"v{drop['n']}.json", f"v{drop['n']}.html.gz"):
                (d / name).unlink(missing_ok=True)
        _write_atomic(d / "index.json", json.dumps(idx, ensure_ascii=False).encode("utf-8"))
        return dict(meta) if any(v["n"] == n for v in vs) else None


def versions(url: str, base: Path | None = None) -> list[dict]:
    """残っている版(取得時刻の古い順。本文は含まない)。"""
    return _by_time(_index(page_dir(url, base))["versions"])


def latest(url: str, base: Path | None = None) -> dict | None:
    """取得時刻のいちばん新しい版(本文つき)。無ければ None。"""
    d = page_dir(url, base)
    for v in reversed(_by_time(_index(d)["versions"])):
        got = _load(d, v)
        if got:
            return got
    return None


def baseline(url: str, base: Path | None = None) -> dict | None:
    """読み直しと照らす版(本文つき): 収集が読んだ時刻のいちばん新しい版(素材の archived_at の版)。収集が読んでいないページは、
    取得時刻のいちばん新しい版"""
    d = page_dir(url, base)
    vs = _index(d)["versions"]
    col = sorted((v for v in vs if v.get("via") == "collect"), key=lambda v: (fetched_at(v), int(v.get("n") or 0)))
    for v in reversed(col or _by_time(vs)):
        got = _load(d, v)
        if got:
            return got
    return None


def html_of(url: str, v: dict, base: Path | None = None) -> bytes | None:
    """版の HTML(圧縮を解いたもの)。無ければ None。"""
    if not v.get("html"):
        return None
    try:
        return gzip.decompress((page_dir(url, base) / v["html"]).read_bytes())
    except (OSError, EOFError, gzip.BadGzipFile):
        return None


def diff(old: str, new: str, limit: int = DIFF_CHARS) -> str:
    """新旧の本文の違い(行ごと。- が古い版・+ が新しい版。前後1行を添える)。上限の字数で切る。"""
    lines = [ln for ln in difflib.unified_diff(compare_lines(old), compare_lines(new), lineterm="", n=1)
             if not ln.startswith(("---", "+++"))]
    out = "\n".join(lines)
    return out if len(out) <= limit else out[:limit] + f"\n…(以下 {len(out) - limit}字を略した)"


def note_change(url: str, final: str | None, old: dict, new: dict, base: Path | None = None) -> None:
    """内容が変わったことを置き場の台帳(changes.jsonl)に残す(続報・訂正の材料。同じ変化は1行だけ)。"""
    root = base or archive_dir()
    row = {"url": url, "final": final if final and final != url else None, "from_at": old.get("at"), "from_sha": old.get("sha"),
           "to_at": new.get("at"), "to_sha": new.get("sha"), "noted_at": now_iso()}
    p = root / "changes.jsonl"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with _Lock(root / ".changes"):
            if any(r.get("url") == url and r.get("from_sha") == old.get("sha") and r.get("to_sha") == new.get("sha")
                   for r in changes(root)):
                return
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"PAGE_ARCHIVE_FAILED: 変化の台帳を書けない({type(e).__name__}: {e})", file=sys.stderr)


def changes(base: Path | None = None) -> list[dict]:
    """変化の台帳(changes.jsonl)の行(読めない行は飛ばす)。"""
    try:
        lines = ((base or archive_dir()) / "changes.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("url"):
            out.append(row)
    return out


def ingest(spool: Path, keep=None, base: Path | None = None) -> int:
    """一時の置き場(収集の隔離されたセッションが fetch_page.py で残したもの)の版を、本体の置き場へ写す(収集時点の版として)。
    keep(url → bool)を渡したら、それが真のページだけを写す。戻りは写した版の数。"""
    n = 0
    for idx_p in sorted(Path(spool).glob("*/*/index.json")):
        d = idx_p.parent
        for v in _index(d)["versions"]:
            got = _load(d, v)
            if not got or (keep is not None and not (keep(got.get("url") or "") or keep(got.get("final") or ""))):
                continue
            if save(got["url"], got["text"], html=html_of(got["url"], got, base=Path(spool)), final=got.get("final"),
                    status=got.get("status"), via="collect", at=got.get("at"), base=base, rendered=bool(got.get("rendered"))):
                n += 1
    return n


def prune(now: float | None = None, base: Path | None = None, max_age_days: float = MAX_AGE_DAYS,
          max_bytes: int = MAX_BYTES) -> dict:
    """古いものを消す: 最後に読んだ・確かめた時刻(seen)が max_age_days より前の URL を消し、全体が max_bytes を超えたら
    最後に読んだ時刻の古い URL から消す。戻りは {"removed": 消した URL の数, "bytes": 残った大きさ}。"""
    root = base or archive_dir()
    now = time.time() if now is None else now
    rows = []
    for idx_p in root.glob("*/*/index.json"):
        d = idx_p.parent
        vs = _index(d)["versions"]
        last = max([str(t or "") for v in vs for t in (v.get("seen"), v.get("at"), v.get("collected"))] or [""])
        try:
            ts = datetime.datetime.fromisoformat(last).timestamp() if last else idx_p.stat().st_mtime
        except ValueError:
            ts = idx_p.stat().st_mtime
        size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
        rows.append((ts, size, d))
    removed = 0
    total = 0
    keep = []
    for ts, size, d in rows:
        if now - ts > max_age_days * 86400:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
        else:
            keep.append((ts, size, d))
            total += size
    for ts, size, d in sorted(keep):
        if total <= max_bytes:
            break
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
        total -= size
    return {"removed": removed, "bytes": total}
