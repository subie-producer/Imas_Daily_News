#!/usr/bin/env python3
"""deploy: 紙面を自前オリジンへ配信する。

  python3 scripts/deploy.py [--skip-purge] [--dry-run] [--budget-sec 秒]

紙面はビルドしない(編集長の決定 2026-11-16。Jekyll のビルドは記事数の2乗で伸び、11/6 は17分かかった):
  版 — docs の Markdown 一式(_posts・_editions・_editorials・_data・about)と assets を releases/<日時> に写したもの
  描く — webapp(imas-app)が current の版を読み、全ページを要求のたびに描く(記事・号・社説・アーカイブ・feed・sitemap)
  索引 — /tags/ と /search は webapp が SQLite を引いて描く

手順: アプリの依存の確認 → 版を写す → これから配るコードで全ページを描いて検証 → releases/<日時> へ配置
      → current を原子的入替(アプリは次の要求で新しい版を読む) → インデックス更新 → アプリのコード同期
      → 旧リリースの世代整理 → Cloudflare キャッシュパージ

設計上の要点:
- 版は symlink の張り替えだけで切り替わる。中途半端なツリーは配信されず、切り戻しも symlink を戻すだけで済む。
- 検証に通らない版は配信しない(壊れた紙面を世界に出さないためのゲート)。検証は**これから配るコード**(webapp/)を
  アプリの python で走らせ、全ページを描く(配信中のコードで確かめても、新しいコードの不具合は捕まらない)。
- 版 → 索引 の順で更新する。逆順だと、索引に載った記事のリンクが 404 になる時間帯ができる。
- 索引の更新は1つの取引で、取り込んだリリースの名前を索引に残す。失敗すれば索引は前の取り込みのまま
  残る。再試行しても通らなければ、current を索引が取り込んだリリースへ戻して静的と索引を揃える
  (新しい静的と古い索引を残さない)。
- 予算(--budget-sec)のうちに必ず終える。release が打ち切る前に、戻す・知らせるまで済ませる。
- 最後に「配信の状態」(current と索引がどの版か)を1行出す。release はそれを通知に載せる。
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import ENV, ROOT, JST, notify, notify_crash, now_jst, set_quiet

DOCS = ROOT / "docs"
SRV = Path(ENV.get("SRV_ROOT", str(Path.home() / "srv" / "imas-news")))
RELEASES = SRV / "releases"
CURRENT = SRV / "current"
KEEP_RELEASES = int(ENV.get("KEEP_RELEASES", "5"))

WEBAPP_SRC = ROOT / "webapp"
APP_DIR = SRV / "app"
APP_PY = Path(ENV.get("APP_PYTHON", str(SRV / "venv" / "bin" / "python")))
DB = SRV / "data" / "paper.db"
# 版に写すもの(docs の中の、アプリが読むデータと、Caddy が配る assets)
SOURCES = ("_posts", "_editions", "_editorials", "_data", "about", "assets")

# 索引の更新の再試行(書き込みの競合 database is locked は待てば通ることがある)。1回の上限は INDEX_TIMEOUT_SEC
# (全記事の付け直しでも実測 5.5秒)。残りの予算が足りなければ、それより短く切る・再試行をやめる
INDEX_ATTEMPTS = 3
INDEX_RETRY_SEC = 20
INDEX_TIMEOUT_SEC = 300
INDEX_MIN_SEC = 10
VERIFY_TIMEOUT_SEC = 600      # 全ページを描く検証(実測: 約3,000ページで数十秒)
DEPS_TIMEOUT_SEC = 300        # アプリの依存の取り込み(requirements.txt が変わったときだけ)
# 配信の時間の予算(--budget-sec)。release は deploy を打ち切るので、その手前で必ず終える。検証と索引の試行は、残りが
# RESERVE_SEC を切る前にやめる(戻す・アプリの再起動・旧版の整理・パージ・通知に残す時間。再起動の上限 130秒+パージ 30秒+通知)
# 2026-10-14 監査指摘: ビルド 1800秒+索引 300秒×3+待ち 60秒が release の待つ 2400秒を越え、戻す前に打ち切られた
BUDGET_SEC = 2340
RESERVE_SEC = 240
DEADLINE = 0.0


def left() -> float:
    """配信の予算の残り(秒)。"""
    return DEADLINE - time.time()

# 配信の結果の行(release が成功の通知に載せる)。成功の通知は「何が変わったか」と「外から新しい版が見えたか」を言う
# (編集長 2026-10-31「毎度のこれ、意味のあるログになってるかな?」: 全体の本数・版の名前・毎回同じ「パージ省略」
# 「アプリのコードに変更なし」だけで、何を配信したかも、読者に届いたかも書いていなかった)
RESULT_MARK = "配信の結果: "
RELEASE_ID = "release-id.txt"       # 外から新しい版が見えるかを確かめる印(配信の版の名前だけを書く)
CHANGES_SHOWN = 5                   # 通知に見出しを並べる記事の数
# 外からの確認の読み直し。この機械(WSL)の DNS は断続的に落ちるので、1回の失敗で「見えない」と言わない(誤報で当番が起きる。
# 2026-10-31)。読み・待ちは配信の予算の残りに収め、通知の分(SERVED_MARGIN_SEC)は残す
SERVED_ATTEMPTS = 3
SERVED_RETRY_SEC = 10
SERVED_TIMEOUT_SEC = 30
SERVED_MARGIN_SEC = 15
SERVED_MIN_SEC = 3
# パージの後、読者が最初に開くページ(トップと、トップから張った記事・号)を外から1回ずつ読み、エッジに貯めておく
# (編集長 2026-11-19「トップページの記事はできれば自動キャッシュ」)。貯まるのはこの機械がつながる Cloudflare の拠点だけ
WARM_LINK = re.compile(r'href="(/(?:articles|editions)/[^"?#]+/)"')
WARM_WORKERS = 4
WARM_TIMEOUT_SEC = 20
WARM_MAX_BYTES = 4_000_000          # 1ページの上限(トップで約80KB)。貯めさせるには最後まで読む
WARM_SHOWN = 5                      # 通知に並べる読めなかったページの数


def manifest_path() -> Path:
    """最後に配信した版の記事の控え(元の md の指紋・訂正の数・見出し)。公開の領域の外に置く。"""
    return SRV / "state" / "posts-manifest.json"


def post_manifest(posts: Path | None = None) -> dict[str, list]:
    """記事の控え {ファイル名: [元の md の指紋, 訂正の数, 見出し]}。posts は版の _posts(省略時は docs/_posts)。"""
    import yaml
    out = {}
    for p in sorted((posts or ROOT / "docs" / "_posts").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        fm = {}
        if text.startswith("---\n") and "\n---\n" in text[4:]:
            try:
                fm = yaml.safe_load(text[4:text.index("\n---\n", 4)]) or {}
            except yaml.YAMLError:
                fm = {}
        if not isinstance(fm, dict):
            fm = {}
        corr = fm.get("corrections")
        out[p.name] = [hashlib.sha256(text.encode("utf-8")).hexdigest(), len(corr) if isinstance(corr, list) else 0,
                       str(fm.get("title") or p.stem)]
    return out


def read_manifest() -> dict | None:
    try:
        m = json.loads(manifest_path().read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def write_manifest(m: dict) -> None:
    try:
        manifest_path().parent.mkdir(parents=True, exist_ok=True)
        tmp = manifest_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, manifest_path())
    except OSError as e:
        print(f"記事の控えを保存できない({e})。次の配信は変わった記事を数えられない", flush=True)


def describe_changes(old: dict | None, new: dict) -> str:
    """前に配信した版と比べて、足した・訂正した・直した・外した記事を言う。"""
    if old is None:
        return "前に配信した版の控えが無いので、変わった記事は数えられない"

    def same(v) -> bool:
        return isinstance(v, list) and len(v) == 3

    old = {k: v for k, v in old.items() if same(v)}
    groups = [("足した記事", [new[k][2] for k in new if k not in old]),
              ("訂正した記事", [new[k][2] for k in new if k in old and new[k][1] > old[k][1]]),
              ("直した記事", [new[k][2] for k in new if k in old and new[k][0] != old[k][0] and new[k][1] <= old[k][1]]),
              ("外した記事", [old[k][2] for k in old if k not in new])]
    parts = []
    for label, titles in groups:
        if titles:
            shown = "・".join(f"「{t}」" for t in titles[:CHANGES_SHOWN])
            more = f" ほか{len(titles) - CHANGES_SHOWN}本" if len(titles) > CHANGES_SHOWN else ""
            parts.append(f"{label} {len(titles)}本({shown}{more})")
    return " / ".join(parts) if parts else "記事の変更なし"


def served_release_id(timeout: float = SERVED_TIMEOUT_SEC) -> str:
    """外(公開の URL)から見えている版の名前。読めなければ例外。"""
    site = ENV.get("SITE_URL", "").rstrip("/")
    if not site:
        raise RuntimeError("SITE_URL が未設定")
    req = urllib.request.Request(f"{site}/{RELEASE_ID}?v={time.time_ns()}",
                                 headers={"User-Agent": "ImasNewsBot/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(200).decode("utf-8", errors="replace").strip()


def within(fn, timeout: float):
    """fn(timeout=...) を timeout 秒で見切る。urlopen の timeout は名前解決の待ちを止めないので、使い捨てのスレッドで読み、
    締切を過ぎたら待たずに TimeoutError にする(スレッドは daemon なので、止まったままでも終了を妨げない。Sol 監査 r3)"""
    box: dict = {}

    def run():
        try:
            box["v"] = fn(timeout=timeout)
        except BaseException as e:      # noqa: BLE001  読みの例外は呼び出し側へそのまま渡す
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"{timeout:.0f}秒で読めない(名前解決・接続のどこかで止まった)")
    if "e" in box:
        raise box["e"]
    return box["v"]


def check_served(stamp: str) -> tuple[bool, str]:
    """外から新しい版が見えるか。戻りは (見えたか, 説明)。読めない・古い版が見えたら SERVED_RETRY_SEC おきに
    SERVED_ATTEMPTS 回まで読み直し、全部だめなときだけ見えないと言う(予算の残りが尽きれば、そこでやめる)。"""
    msg = "外から新しい版を確かめられない(配信の時間の予算が残っていない)"
    for i in range(SERVED_ATTEMPTS):
        budget = min(SERVED_TIMEOUT_SEC, left() - SERVED_MARGIN_SEC)
        if budget < SERVED_MIN_SEC:
            break
        try:
            seen = within(served_release_id, budget)
        except Exception as e:      # noqa: BLE001  トンネル・DNS・オリジンのどこで落ちても「見えない」と言う
            msg = f"外から新しい版を確かめられない({type(e).__name__}: {str(e)[:120]})"
        else:
            if seen == stamp:
                return True, "外から新しい版を確認"
            msg = f"外から見える版が違う(見えた版 {seen[:40]!r} / 配信した版 {stamp})"
        if i == SERVED_ATTEMPTS - 1 or left() - SERVED_RETRY_SEC - SERVED_MARGIN_SEC < SERVED_MIN_SEC:
            break
        print(f"外からの確認 {i + 1}/{SERVED_ATTEMPTS}: {msg}。{SERVED_RETRY_SEC}秒後に読み直す", flush=True)
        time.sleep(SERVED_RETRY_SEC)
    return False, f"{msg}({i + 1}回読んだ)" if SERVED_ATTEMPTS > 1 else msg


WARM_FAILED = "キャッシュの温めに失敗"


def warm_page(url: str, timeout: float = WARM_TIMEOUT_SEC) -> bytes:
    """外から1ページ読む(エッジに貯めさせる)。200 でなければ例外(4xx・5xx は urlopen が HTTPError を上げ、ほかはここで上げる)。
    本文は最後まで読めたときだけ返す: 上限つきの read は途中で接続が切れても例外を出さないので、Content-Length と
    読んだ長さを照らす。上限を越えるページも読み切れない(貯まったと言えない)ので例外(Sol 監査 r1)。"""
    req = urllib.request.Request(url, headers={"User-Agent": "ImasNewsBot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        status = getattr(r, "status", None)
        if status != 200:      # urlopen は 2xx を例外にしない(204 は本文が無く、貯まるページが無い。Sol 監査 r2)
            raise ValueError(f"200 でない応答({status})")
        body = r.read(WARM_MAX_BYTES)
        if len(body) >= WARM_MAX_BYTES and r.read(1):
            raise ValueError(f"本文が上限 {WARM_MAX_BYTES} バイトを越える")
        length = r.headers.get("Content-Length")
        if length is not None and length.strip().isdigit() and int(length) != len(body):
            raise ValueError(f"本文が途中で切れた({len(body)}/{int(length)} バイト)")
        return body


def warm_cache() -> str:
    """パージの後、トップとトップから張った記事・号のページを外から読み、エッジに貯める。戻りは通知の1文
    (読めないページがあれば WARM_FAILED で始める。読者がそのページを開くと 200 が返らないかもしれないので異常に数える)。
    時間の予算は外からの確認と同じく、通知の分(SERVED_MARGIN_SEC)を残して使う。"""
    site = ENV.get("SITE_URL", "").rstrip("/")
    if not site:
        return ""

    def budget() -> float:
        return min(WARM_TIMEOUT_SEC, left() - SERVED_MARGIN_SEC)

    if budget() < SERVED_MIN_SEC:
        return "キャッシュの温めは省いた(配信の時間の予算が残っていない)"
    try:
        top = within(lambda timeout: warm_page(f"{site}/", timeout), budget()).decode("utf-8", errors="replace")
    except Exception as e:      # noqa: BLE001  トップが読めないこと自体を異常として言う
        return f"{WARM_FAILED}: トップを読めない({type(e).__name__}: {str(e)[:120]})"
    paths = list(dict.fromkeys(WARM_LINK.findall(top)))
    bad: list[str] = []
    skipped = 0
    # 実際の読み取りの同時数の枠。within は時間切れで待つのをやめても読み取りのスレッドは残る(名前解決で止まったまま)ので、
    # 枠は読み取りのスレッドが終わるまで返さない。止まったスレッドが枠を埋めたら、次のページは枠を待って時間切れになる(Sol 監査 r3)
    slots = threading.BoundedSemaphore(WARM_WORKERS)
    stuck = threading.Event()      # 1ページでも時間切れになったら、残りは読みに行かない(同じ所で止まるのを待ち続けない)

    def read_in_slot(url: str, timeout: float) -> bytes:
        if not slots.acquire(timeout=timeout):
            raise TimeoutError(f"{timeout:.0f}秒待っても、先に読み始めたページが終わらない")
        try:
            return warm_page(url, timeout)
        finally:
            slots.release()

    def one(path: str) -> None:
        nonlocal skipped
        if stuck.is_set():
            bad.append(f"{path}(先のページが時間切れのため読まない)")
            return
        t = budget()
        if t < SERVED_MIN_SEC:
            skipped += 1
            return
        try:
            within(lambda timeout: read_in_slot(site + path, timeout), t)
        except Exception as e:      # noqa: BLE001  どこで落ちても、そのページは読めないと言う
            if isinstance(e, TimeoutError):
                stuck.set()
            bad.append(f"{path}({type(e).__name__}: {str(e)[:80]})")

    with concurrent.futures.ThreadPoolExecutor(WARM_WORKERS) as ex:
        list(ex.map(one, paths))
    done = len(paths) - len(bad) - skipped
    if bad:
        return (f"{WARM_FAILED}: トップから張ったページ {len(bad)}/{len(paths)} が外から読めない: "
                + " / ".join(sorted(bad)[:WARM_SHOWN]) + (" ほか" if len(bad) > WARM_SHOWN else ""))
    tail = f"(予算が尽きて {skipped} ページは省いた)" if skipped else ""
    return f"キャッシュを温めた: トップと、そこから張った {done}/{len(paths)} ページ{tail}"

# 公開の状態。止まった所で実態が違うので、段ごとに言い直し、最後に STATE_MARK の行で出す(release が通知に載せる)
STATE_MARK = "配信の状態: "
STATE = "current は前の版のまま(差し替える前に止まった)"


def say_state(s: str) -> None:
    global STATE
    STATE = s

def stage(dest: Path) -> None:
    """版を写す(docs の SOURCES を dest へ)。ビルドはしない。"""
    dest.mkdir(parents=True)
    for name in SOURCES:
        src = DOCS / name
        if not src.exists():
            raise RuntimeError(f"版に写すものが無い: docs/{name}")
        shutil.copytree(src, dest / name) if src.is_dir() else shutil.copy2(src, dest / name)


def ensure_deps(timeout: float = DEPS_TIMEOUT_SEC) -> str:
    """アプリの python に webapp/requirements.txt の依存を入れる(前回入れた内容と同じなら何もしない)。
    入らなければ RuntimeError(検証の前に止め、current は差し替えない)。入れたときだけ説明を返す。"""
    req = WEBAPP_SRC / "requirements.txt"
    mark = SRV / "state" / "app-requirements.txt"
    if mark.exists() and mark.read_bytes() == req.read_bytes():
        return ""
    r = subprocess.run([str(APP_PY), "-m", "pip", "install", "-q", "-r", str(req)],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"アプリの依存を入れられない:\n{(r.stderr or r.stdout)[-600:]}")
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_bytes(req.read_bytes())
    return "アプリの依存を入れ直しました"


def verify(dest: Path, timeout: float = VERIFY_TIMEOUT_SEC) -> tuple[list[str], int]:
    """配信して良い版かを、これから配るコード(webapp/verify.py)で全ページ描いて確かめる。戻りは (問題の一覧, 記事数)。"""
    try:
        r = subprocess.run([str(APP_PY), str(WEBAPP_SRC / "verify.py"), "--site", str(dest)],
                           capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "SITE_URL": ENV.get("SITE_URL", "https://news.imas.fans")})
    except subprocess.TimeoutExpired:
        return [f"全ページの検証が {timeout:.0f}秒で終わらない"], 0
    errors = [ln[len("ERROR: "):] for ln in r.stdout.splitlines() if ln.startswith("ERROR: ")]
    try:
        n_posts = int(json.loads(r.stdout.strip().splitlines()[-1])["articles"])
    except (IndexError, ValueError, KeyError, TypeError):
        n_posts = 0
        errors.append(f"検証の結果を読めない(exit {r.returncode}):\n{(r.stderr or r.stdout)[-600:]}")
    if r.returncode != 0 and not errors:
        errors.append(f"検証が異常終了(exit {r.returncode}):\n{(r.stderr or r.stdout)[-600:]}")
    return errors, n_posts


def reindex(run_id: str, timeout: float = INDEX_TIMEOUT_SEC) -> int:
    """動的ページ(タグ・検索)が引く SQLite インデックスを更新する。
    記事は不変なので差分更新で済み、増えた分だけを入れ直す。
    run_id(このリリースの名前)は indexer が同じ取引で meta に残す。索引がどのリリースと揃っているかの記録になる。"""
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "indexer.py"), "--db", str(DB), "--run-id", run_id],
                       cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"インデックス更新に失敗:\n{(r.stderr or r.stdout)[-600:]}")
    for line in r.stdout.strip().splitlines():
        print(line, flush=True)
    return indexed_count()


def indexed_count() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    finally:
        conn.close()


def indexed_run() -> str | None:
    """索引が最後に取り込んだ配信の run_id(indexer の取引で書く。無ければ None)。読めなければ sqlite3.Error。"""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'run_id'").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def reindex_or_restore(release: Path, prev: Path | None, run_id: str, skip_purge: bool) -> int | None:
    """索引を更新する(INDEX_ATTEMPTS 回まで。予算の残りが RESERVE_SEC を切る前にやめる)。通れば索引の記事数を返す。
    通らなければ None を返し、current を索引が取り込んだリリース(meta の run_id = リリースの名前)へ戻して静的と索引を揃える。
    そのリリースが分からない(記録が無い・消えている)ときは、この配信の前の状態(prev)へ戻し、揃っているとは言わない。
    2026-10-14 監査指摘: current を切り替えた後に索引の更新が落ちると、新しい静的と古い索引(取り下げた記事が検索に残る)が
    残り、パージにも進まなかった。release は「current は前号のまま」と誤って通知していた。
    同 再監査: 「この配信の run_id でない」は「索引が prev と同じ版」の証明にならない(前の配信が索引の状態を確かめられずに
    新しい版を残していた)。戻す先は索引の記録から決める"""
    err = ""
    for i in range(INDEX_ATTEMPTS):
        budget = min(INDEX_TIMEOUT_SEC, left() - RESERVE_SEC)
        if budget < INDEX_MIN_SEC:
            err += "\n(配信の時間の予算が残っていないので、索引の試行をやめた)"
            break
        try:
            return reindex(run_id, budget)
        except Exception as e:      # noqa: BLE001  indexer の異常終了・時間切れ・DB の読み出し
            err = str(e)
        wait = INDEX_RETRY_SEC * (i + 1)
        if i < INDEX_ATTEMPTS - 1 and left() - wait - RESERVE_SEC >= INDEX_MIN_SEC:
            print(f"索引の更新に失敗({i + 1}/{INDEX_ATTEMPTS})。{wait}秒後に再試行:\n{err[-300:]}", flush=True)
            time.sleep(wait)
    try:
        ran, unread = indexed_run(), None
    except sqlite3.Error as e:
        ran, unread = None, e
    if unread is None and ran == run_id:
        # 取引は通っていた(その後の後始末で落ちた)。索引は静的と同じ版なので、戻さずに進む
        say_state(f"current・索引とも新しい版({release.name})")
        print(f"indexer は異常終了したが、索引はこの版({run_id})まで進んでいる:\n{err[-300:]}", flush=True)
        return indexed_count()
    if unread is not None:
        say_state(f"current は新しい版({release.name})。索引がどの版か確かめられない({unread})。静的と索引が食い違っている可能性がある")
        notify("deploy", f"索引の更新に失敗し、索引の状態も確かめられない。{STATE}:\n```\n{err[-600:]}\n```", ok=False)
        return None
    match = RELEASES / ran if ran and Path(ran).name == ran and (RELEASES / ran).is_dir() else None
    if match is not None:
        swap(match)
        say_state(f"current を索引が取り込んだ版({match.name})に戻した(静的と索引が揃っている)。新しい版({release.name})は配信していない")
    elif prev is not None:
        swap(prev)
        say_state(f"current は前の版({prev.name})に戻した。索引がどのリリースを取り込んだかの記録が無い"
                  f"({'記録 ' + ran + ' のリリースが無い' if ran else '取り込みの記録なし'})ので、揃っているかは確かめられない。"
                  f"新しい版({release.name})は配信していない")
    else:
        say_state(f"current は新しい版({release.name})のまま。戻せる版が無い。索引は前の取り込み({ran or '記録なし'})のまま"
                  "(静的と索引が食い違う)")
        notify("deploy", f"索引の更新に失敗した。{STATE}:\n```\n{err[-600:]}\n```", ok=False)
        return None
    # 切り替えていた間にエッジが新しい版を載せたかもしれないので、戻した版でも落とす
    purge_msg = "" if skip_purge else purge_cache()
    notify("deploy", f"索引の更新に失敗したので配信を戻した。{STATE}。{purge_msg + '。' if purge_msg else ''}\n```\n{err[-600:]}\n```",
           ok=False)
    return None


def sync_app() -> bool:
    """アプリのコードを配信領域へ同期する。変更があったときだけ True。
    紙面の更新でアプリを再起動する必要はない(DB を読むだけなので)。"""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    changed = False
    for src in sorted(WEBAPP_SRC.rglob("*")):
        if src.is_dir() or "__pycache__" in src.parts:
            continue
        dst = APP_DIR / src.relative_to(WEBAPP_SRC)
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            changed = True
    return changed


def restart_app() -> str:
    r = subprocess.run(["systemctl", "--user", "restart", "imas-app.service"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return f"⚠️アプリの再起動に失敗: {r.stderr.strip()[:200]}"
    for _ in range(20):
        time.sleep(0.5)
        h = subprocess.run(["systemctl", "--user", "is-active", "imas-app.service"],
                           capture_output=True, text=True)
        if h.stdout.strip() == "active":
            return "アプリを再起動しました"
    return "⚠️アプリが active になりません"


def swap(release: Path) -> Path | None:
    """current を新リリースへ原子的に張り替える。戻り値は直前のリリース(切り戻し用)。"""
    prev = CURRENT.resolve() if CURRENT.is_symlink() and CURRENT.exists() else None
    tmp = CURRENT.with_name("current.new")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(release)
    # os.replace は同一ディレクトリ内の symlink を原子的に差し替える
    os.replace(tmp, CURRENT)
    return prev


def prune(keep: int) -> list[str]:
    rels = sorted([d for d in RELEASES.iterdir() if d.is_dir()])
    live = CURRENT.resolve() if CURRENT.exists() else None
    removed = []
    for d in rels[:-keep] if keep > 0 else []:
        if live and d.resolve() == live:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed


def purge_cache() -> str:
    """Cloudflare のエッジキャッシュを落として新しい紙面を反映させる。
    s-maxage を長く取っている(オリジン停止時もエッジで読める)ため、
    発行のたびにここでパージしないと更新が届かない。
    CF_ZONE_ID はカンマ区切りで複数書ける(同じ紙面を複数のドメインで配っている間は、全部のゾーンを落とす)。"""
    token = ENV.get("CF_API_TOKEN", "")
    zones = [z.strip() for z in ENV.get("CF_ZONE_ID", "").split(",") if z.strip()]
    if not token or not zones:
        return ""       # パージを使っていない(エッジのキャッシュも効かせていない)。毎回言っても情報にならない
    failed = []
    for zone in zones:
        req = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache",
            data=json.dumps({"purge_everything": True}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read(1_000_000).decode())     # API の短い答え。上限まで(終わらない応答を溜め込まない。2026-10-27)
            if not body.get("success"):
                failed.append(f"{zone}: {body.get('errors')}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            failed.append(f"{zone}: {e}")
    if failed:
        return f"パージ失敗({len(failed)}/{len(zones)} ゾーン): " + " / ".join(failed)
    return "キャッシュパージ完了" + (f"({len(zones)} ゾーン)" if len(zones) > 1 else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-purge", action="store_true", help="Cloudflare のキャッシュパージを行わない")
    ap.add_argument("--dry-run", action="store_true", help="版を写して検証するだけで、配信は差し替えない")
    ap.add_argument("--budget-sec", type=float, default=BUDGET_SEC,
                    help="この秒数のうちに必ず終える(release が打ち切る時間より短く渡す)")
    ap.add_argument("--from-release", action="store_true",
                    help="発行(release)からの配信。成功は release の通知に載るので、ここでは成功を知らせない(失敗は知らせる)")
    args = ap.parse_args()
    global DEADLINE
    DEADLINE = time.time() + args.budget_sec
    say_state("current は前の版のまま(差し替える前に止まった)")
    # 試験実行(--dry-run)では Discord へ通知しない。本物の警報と見分けが付かなくなる
    set_quiet(args.dry_run)

    RELEASES.mkdir(parents=True, exist_ok=True)
    stamp = now_jst().strftime("%Y%m%dT%H%M%S")
    staging = RELEASES / f".staging-{stamp}"
    release = RELEASES / stamp

    try:
        deps_msg = ensure_deps(timeout=max(1.0, min(DEPS_TIMEOUT_SEC, left() - RESERVE_SEC - INDEX_MIN_SEC)))
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        notify("deploy", f"配信を中止(アプリの依存を入れられない)。{STATE}\n{str(e)[-600:]}", ok=False)
        return 1
    if deps_msg:
        print(deps_msg, flush=True)

    print(f"版を写す → {staging}", flush=True)
    shutil.rmtree(staging, ignore_errors=True)
    stage(staging)
    # 検証が長引いても、索引の1回と後始末の分は残す(足りなければ時間切れで止め、current は差し替えない)
    verify_limit = max(1.0, min(VERIFY_TIMEOUT_SEC, left() - RESERVE_SEC - INDEX_MIN_SEC))
    errors, n_posts = verify(staging, timeout=verify_limit)
    if errors:
        shutil.rmtree(staging, ignore_errors=True)
        notify("deploy", "配信を中止(全ページの検証に失敗。current は前の版のまま):\n- " + "\n- ".join(e[:300] for e in errors[:6]), ok=False)
        return 1
    print(f"検証OK: 全ページを描けた(記事{n_posts}本)", flush=True)
    # 外から新しい版が見えるかを確かめる印(アプリが /release-id.txt で返す)。記事の控えは版に写した記事から取る
    (staging / RELEASE_ID).write_text(stamp + "\n", encoding="utf-8")
    manifest = post_manifest(staging / "_posts")

    if args.dry_run:
        print(f"[dry-run] {staging} は配信せず残置します", flush=True)
        return 0

    # 静的紙面の差し替え(原子的)
    os.replace(staging, release)
    prev = swap(release)
    say_state(f"current は新しい版({release.name})。索引はまだ前の版(静的と索引が食い違う)")
    print(f"current → {release.name}(直前: {prev.name if prev else 'なし'})", flush=True)

    # 動的ページのデータを更新。静的側を先に切り替えているので、記事ページは
    # 既に新しく、索引だけが数秒遅れて追いつく形になる(逆順だと索引に載った
    # 記事のリンクが 404 になる時間ができる)。通らなければ current を前の版へ戻す
    n_indexed = reindex_or_restore(release, prev, stamp, args.skip_purge)
    if n_indexed is None:
        return 1
    say_state(f"current・索引とも新しい版({release.name})。キャッシュのパージはまだ")

    app_msg = restart_app() if sync_app() else ""      # 再起動したときだけ言う
    if app_msg:
        print(app_msg, flush=True)

    removed = prune(KEEP_RELEASES)
    if removed:
        print(f"旧リリース削除: {', '.join(removed)}", flush=True)

    purge_msg = "" if args.skip_purge else purge_cache()       # パージしたときだけ結果を言う(省略は言わない)
    if purge_msg:
        print(purge_msg, flush=True)
    say_state(f"current・索引とも新しい版({release.name})。{purge_msg}".rstrip("。"))

    changes = describe_changes(read_manifest(), manifest)
    write_manifest(manifest)
    served, served_msg = check_served(stamp)
    # 温めるのは、パージが通り外から新しい版が見えたときだけ(古い版を貯め直さない・パージしていなければ貯まりっぱなし)
    warm_msg = warm_cache() if served and purge_msg.startswith("キャッシュパージ完了") else ""
    if warm_msg:
        print(warm_msg, flush=True)
    mismatch = f"静的 {n_posts}本と索引 {n_indexed}本が食い違う" if n_posts != n_indexed else ""
    # 異常: 食い違い・外から見えない(トンネル・オリジンの不調)・アプリの再起動やパージの失敗・トップから張ったページが読めない。
    # 発行からの配信でも知らせる(成功の通知は release に任せるので、ここで黙ると誰にも届かない)。手での配信は成功も異常も1通にまとめる
    trouble = (bool(mismatch) or not served or app_msg.startswith("⚠️") or purge_msg.startswith("パージ失敗")
               or warm_msg.startswith(WARM_FAILED))
    print(f"{RESULT_MARK}{served_msg}", flush=True)
    if trouble or not args.from_release:
        parts = [mismatch, served_msg, app_msg, purge_msg, warm_msg]
        if not served:
            parts.append(f"current・索引とも新しい版({release.name})")
        notify("deploy", "。".join([f"配信を更新: {changes}"] + [m for m in parts if m]), ok=not trouble)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        notify_crash("deploy", e)
        code = 1
    print(STATE_MARK + STATE, flush=True)
    sys.exit(code)
