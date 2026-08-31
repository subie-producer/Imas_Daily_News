#!/usr/bin/env python3
"""deploy: 紙面を自前オリジンへ配信する。

  python3 scripts/deploy.py [--skip-purge] [--dry-run]

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
"""
import argparse
import datetime
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

# 配信前の最低限の健全性チェック(ここに無いものが欠けている紙面は出さない)。
# /tags/ と /search は動的アプリが描くのでビルド成果物には含まれない。
REQUIRED = ["index.html", "archive/index.html", "404.html"]


def build(dest: Path) -> None:
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
            cwd=DOCS, capture_output=True, text=True, timeout=1800, env=env,
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


def reindex() -> int:
    """動的ページ(タグ・検索)が引く SQLite インデックスを更新する。
    記事は不変なので差分更新で済み、増えた分だけを入れ直す。"""
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "indexer.py")],
                       cwd=ROOT, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"インデックス更新に失敗:\n{(r.stderr or r.stdout)[-600:]}")
    for line in r.stdout.strip().splitlines():
        print(line, flush=True)
    db = SRV / "data" / "paper.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    finally:
        conn.close()


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
        return "CF_API_TOKEN/CF_ZONE_ID 未設定のためパージ省略"
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/zones/{zone}/purge_cache",
        data=json.dumps({"purge_everything": True}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        return "キャッシュパージ完了" if body.get("success") else f"パージ失敗: {body.get('errors')}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return f"パージ失敗: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-purge", action="store_true", help="Cloudflare のキャッシュパージを行わない")
    ap.add_argument("--dry-run", action="store_true", help="ビルドと検証だけ行い、配信は差し替えない")
    args = ap.parse_args()
    # 試験実行(--dry-run)では Discord へ通知しない。本物の警報と見分けが付かなくなる
    set_quiet(args.dry_run)

    RELEASES.mkdir(parents=True, exist_ok=True)
    stamp = now_jst().strftime("%Y%m%dT%H%M%S")
    staging = RELEASES / f".staging-{stamp}"
    release = RELEASES / stamp

    print(f"ビルド中 → {staging}", flush=True)
    shutil.rmtree(staging, ignore_errors=True)
    build(staging)

    errors = verify(staging)
    if errors:
        shutil.rmtree(staging, ignore_errors=True)
        notify("deploy", "配信を中止(ビルド検証に失敗):\n- " + "\n- ".join(errors[:6]), ok=False)
        return 1

    n_posts = len(list((staging / "articles").glob("*/index.html")))
    print(f"検証OK: 静的ページ 記事{n_posts}本", flush=True)

    if args.dry_run:
        print(f"[dry-run] {staging} は配信せず残置します", flush=True)
        return 0

    # 静的紙面の差し替え(原子的)
    os.replace(staging, release)
    prev = swap(release)
    print(f"current → {release.name}(直前: {prev.name if prev else 'なし'})", flush=True)

    # 動的ページのデータを更新。静的側を先に切り替えているので、記事ページは
    # 既に新しく、索引だけが数秒遅れて追いつく形になる(逆順だと索引に載った
    # 記事のリンクが 404 になる時間ができる)
    n_indexed = reindex()

    app_msg = restart_app() if sync_app() else "アプリのコードに変更なし"
    print(app_msg, flush=True)

    removed = prune(KEEP_RELEASES)
    if removed:
        print(f"旧リリース削除: {', '.join(removed)}", flush=True)

    purge_msg = "パージ省略(--skip-purge)" if args.skip_purge else purge_cache()
    print(purge_msg, flush=True)

    notify("deploy", f"配信を更新しました(記事 {n_posts}本・索引 {n_indexed}本・{release.name})。"
                     f"{app_msg}。{purge_msg}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        notify_crash("deploy", e)
        sys.exit(1)
