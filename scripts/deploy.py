#!/usr/bin/env python3
"""deploy: 紙面を自前オリジンへ配信する。

  python3 scripts/deploy.py [--skip-purge] [--dry-run] [--budget-sec 秒]

紙面は二層で配信する:
  静的 — 記事・号・社説・アーカイブ。発行後に変わらないので Jekyll で生成して置く
  動的 — /tags/ と /search。問い合わせで形が変わるので webapp が SQLite を引いて描く

手順: 本番ビルド → 出力検証 → releases/<日時> へ配置 → current を原子的入替
      → インデックス更新(動的ページのデータ) → アプリのコード同期
      → 旧リリースの世代整理 → Cloudflare キャッシュパージ

設計上の要点:
- ビルドは **素の Jekyll**(bundler 非経由)。Gemfile の github-pages gem は safe
  モードとプラグイン制限を強制するため、本番ビルドでは経由しない。
- 静的側は symlink の張り替えだけで切り替わる。中途半端なツリーは配信されず、
  切り戻しも symlink を戻すだけで済む。
- 検証に通らないビルドは配信しない(壊れた紙面を世界に出さないためのゲート)。
- 静的 → 索引 の順で更新する。逆順だと、索引に載った記事のリンクが 404 になる
  時間帯ができる。
- 索引の更新は1つの取引で、取り込んだリリースの名前を索引に残す。失敗すれば索引は前の取り込みのまま
  残る。再試行しても通らなければ、current を索引が取り込んだリリースへ戻して静的と索引を揃える
  (新しい静的と古い索引を残さない)。
- 予算(--budget-sec)のうちに必ず終える。release が打ち切る前に、戻す・知らせるまで済ませる。
- 最後に「配信の状態」(current と索引がどの版か)を1行出す。release はそれを通知に載せる。
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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
DB = SRV / "data" / "paper.db"

# 索引の更新の再試行(書き込みの競合 database is locked は待てば通ることがある)。1回の上限は INDEX_TIMEOUT_SEC
# (全記事の付け直しでも実測 5.5秒)。残りの予算が足りなければ、それより短く切る・再試行をやめる
INDEX_ATTEMPTS = 3
INDEX_RETRY_SEC = 20
INDEX_TIMEOUT_SEC = 300
INDEX_MIN_SEC = 10
BUILD_TIMEOUT_SEC = 1800
# 配信の時間の予算(--budget-sec)。release は deploy を打ち切るので、その手前で必ず終える。ビルドと索引の試行は、残りが
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


def manifest_path() -> Path:
    """最後に配信した版の記事の控え(元の md の指紋・訂正の数・見出し)。公開の領域の外に置く。"""
    return SRV / "state" / "posts-manifest.json"


def post_manifest() -> dict[str, list]:
    """いまの記事の控え {ファイル名: [元の md の指紋, 訂正の数, 見出し]}。"""
    import yaml
    out = {}
    for p in sorted((ROOT / "docs" / "_posts").glob("*.md")):
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


def served_release_id() -> str:
    """外(公開の URL)から見えている版の名前。読めなければ例外。"""
    site = ENV.get("SITE_URL", "").rstrip("/")
    if not site:
        raise RuntimeError("SITE_URL が未設定")
    req = urllib.request.Request(f"{site}/{RELEASE_ID}?v={time.time_ns()}",
                                 headers={"User-Agent": "ImasNewsBot/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(200).decode("utf-8", errors="replace").strip()


def check_served(stamp: str) -> tuple[bool, str]:
    """外から新しい版が見えるか。戻りは (見えたか, 説明)。"""
    try:
        seen = served_release_id()
    except Exception as e:      # noqa: BLE001  トンネル・DNS・オリジンのどこで落ちても「見えない」と言う
        return False, f"外から新しい版を確かめられない({type(e).__name__}: {str(e)[:120]})"
    if seen == stamp:
        return True, "外から新しい版を確認"
    return False, f"外から見える版が違う(見えた版 {seen[:40]!r} / 配信した版 {stamp})"

# 公開の状態。止まった所で実態が違うので、段ごとに言い直し、最後に STATE_MARK の行で出す(release が通知に載せる)
STATE_MARK = "配信の状態: "
STATE = "current は前の版のまま(差し替える前に止まった)"


def say_state(s: str) -> None:
    global STATE
    STATE = s

# 配信前の最低限の健全性チェック(ここに無いものが欠けている紙面は出さない)。
# /tags/ と /search は動的アプリが描くのでビルド成果物には含まれない。
REQUIRED = ["index.html", "archive/index.html", "404.html"]


def build(dest: Path, timeout: float = BUILD_TIMEOUT_SEC) -> None:
    """本番設定でビルドする。url は .env の SITE_URL から一時設定として渡す。"""
    site_url = ENV.get("SITE_URL", "").rstrip("/")
    if not site_url:
        raise RuntimeError(".env に SITE_URL が未設定(例: SITE_URL=https://example.com)")

    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False, encoding="utf-8") as f:
        f.write(f'url: "{site_url}"\n')
        url_cfg = f.name

    # bundler を経由すると github-pages gem が safe モードを強制し _plugins/ が無効になる。
    # JEKYLL_NO_BUNDLER_REQUIRE で Jekyll 側の Gemfile 自動読込も止める。
    env = {k: v for k, v in os.environ.items() if k not in ("BUNDLE_GEMFILE", "RUBYOPT")}
    env["JEKYLL_NO_BUNDLER_REQUIRE"] = "true"
    env["JEKYLL_ENV"] = "production"
    try:
        r = subprocess.run(
            ["jekyll", "build",
             "--config", f"_config.yml,_config.production.yml,{url_cfg}",
             "--destination", str(dest)],
            cwd=DOCS, capture_output=True, text=True, timeout=timeout, env=env,
        )
    finally:
        os.unlink(url_cfg)

    if r.returncode != 0:
        raise RuntimeError(f"jekyll build 失敗(exit {r.returncode}):\n{(r.stderr or r.stdout)[-800:]}")
    for line in r.stdout.splitlines():
        if "done in" in line:
            print(line.strip(), flush=True)


def verify(dest: Path) -> list[str]:
    """配信して良いビルドかを機械検査する。"""
    errors = []
    for rel in REQUIRED:
        p = dest / rel
        if not p.exists() or p.stat().st_size == 0:
            errors.append(f"必須ファイルが無い/空: {rel}")

    posts = list((dest / "articles").glob("*/index.html")) if (dest / "articles").exists() else []
    if len(posts) < 1:
        errors.append("記事ページが1本も生成されていない")

    # baseurl が残っていると自前ドメイン配下でリンクが全部壊れる。
    # _config.production.yml を渡し忘れた場合をここで捕まえる
    idx = (dest / "index.html").read_text(encoding="utf-8") if (dest / "index.html").exists() else ""
    if '"/Imas_Daily_News/' in idx or "'/Imas_Daily_News/" in idx:
        errors.append("baseurl(/Imas_Daily_News)が残っている(_config.production.yml 未適用)")
    return errors


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
    purge_msg = "パージ省略(--skip-purge)" if skip_purge else purge_cache()
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
    発行のたびにここでパージしないと更新が届かない。"""
    token = ENV.get("CF_API_TOKEN", "")
    zone = ENV.get("CF_ZONE_ID", "")
    if not token or not zone:
        return ""       # パージを使っていない(エッジのキャッシュも効かせていない)。毎回言っても情報にならない
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache",
        data=json.dumps({"purge_everything": True}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read(1_000_000).decode())     # API の短い答え。上限まで(終わらない応答を溜め込まない。2026-10-27)
        return "キャッシュパージ完了" if body.get("success") else f"パージ失敗: {body.get('errors')}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return f"パージ失敗: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-purge", action="store_true", help="Cloudflare のキャッシュパージを行わない")
    ap.add_argument("--dry-run", action="store_true", help="ビルドと検証だけ行い、配信は差し替えない")
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

    print(f"ビルド中 → {staging}", flush=True)
    shutil.rmtree(staging, ignore_errors=True)
    # ビルドが長引いても、索引の1回と後始末の分は残す(足りなければ時間切れで止め、current は差し替えない)
    build_limit = max(1.0, min(BUILD_TIMEOUT_SEC, left() - RESERVE_SEC - INDEX_MIN_SEC))
    try:
        build(staging, timeout=build_limit)
    except subprocess.TimeoutExpired:
        shutil.rmtree(staging, ignore_errors=True)
        notify("deploy", f"配信を中止(ビルドが予算のうちに終わらない。上限 {build_limit:.0f}秒)。{STATE}", ok=False)
        return 1

    errors = verify(staging)
    if errors:
        shutil.rmtree(staging, ignore_errors=True)
        notify("deploy", "配信を中止(ビルド検証に失敗):\n- " + "\n- ".join(errors[:6]), ok=False)
        return 1

    n_posts = len(list((staging / "articles").glob("*/index.html")))
    print(f"検証OK: 静的ページ 記事{n_posts}本", flush=True)
    # 外から新しい版が見えるかを確かめる印。記事の控えはビルドの直後に取る(ビルドの元と同じ記事)
    (staging / RELEASE_ID).write_text(stamp + "\n", encoding="utf-8")
    manifest = post_manifest()

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

    purge_msg = "パージ省略(--skip-purge)" if args.skip_purge else purge_cache()
    if purge_msg:
        print(purge_msg, flush=True)
    say_state(f"current・索引とも新しい版({release.name})。{purge_msg}".rstrip("。"))

    changes = describe_changes(read_manifest(), manifest)
    write_manifest(manifest)
    served, served_msg = check_served(stamp)
    mismatch = f"静的 {n_posts}本と索引 {n_indexed}本が食い違う" if n_posts != n_indexed else ""
    # 異常: 食い違い・外から見えない(トンネル・オリジンの不調)・アプリの再起動やパージの失敗。発行からの配信でも知らせる
    # (成功の通知は release に任せるので、ここで黙ると誰にも届かない)。手での配信は成功も異常も1通にまとめる
    trouble = bool(mismatch) or not served or app_msg.startswith("⚠️") or purge_msg.startswith("パージ失敗")
    print(f"{RESULT_MARK}{served_msg}", flush=True)
    if trouble or not args.from_release:
        parts = [mismatch, served_msg, app_msg, purge_msg]
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
