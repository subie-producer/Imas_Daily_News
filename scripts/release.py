#!/usr/bin/env python3
"""発行(release): edition/<発行日> ブランチを main へ squash merge して push する。

PIPELINE.md §5 の実装。毎朝 06:00 JST に systemd user timer(ops/systemd/)から起動される。

  python3 scripts/release.py [--date YYYY-MM-DD] [--dry-run]

手順: 前提確認 → lint(赤なら中止) → main へ squash merge → push → origin に届いたことの確認 →
      配信 → 発行済みブランチ削除 → 翌日ブランチ作成 → Discord 通知
標準ライブラリのみで動く(lint はサブプロセスで system python3 を使う)。
"""
import argparse
import contextlib
import datetime
import json
import re
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
JST = ZoneInfo("Asia/Tokyo")


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()


# --dry-run のときに立てる。**手元の確認で本物の警報を鳴らさないため。**
# 実測: 発行前の点検で --dry-run を回したら、号スナップショットがまだ無いのは当然なのに
# 「発行中止」の警報が Discord に飛び、発行不良と見分けが付かなくなった(2026-08-31 20:49)。
DRY_RUN = False
RELEASE_DATE = ""


def notify(msg: str, ok: bool = True) -> None:
    prefix = "✅" if ok else "🚨"
    text = f"{prefix} アイマスNEWS release: {msg}"
    if DRY_RUN:
        print(f"[dry-run・通知しない] {text}", flush=True)
        return
    if not ok:
        from pipelib import anomaly
        anomaly("release", msg)      # 人に見せる異常は当番のなぜなぜの対象(main の終わりで渡す)
    print(text, flush=True)
    url = ENV.get("DISCORD_WEBHOOK_URL")
    if url:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({"content": text}).encode(),
                headers={"Content-Type": "application/json", "User-Agent": "ImasNewsBot/1.0"},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:  # 通知失敗は発行を止めない
            print(f"(Discord 通知失敗: {e})", flush=True)


def git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗: {r.stderr.strip()}")
    return r


def branch_exists(name: str, remote: bool = False) -> bool:
    ref = f"refs/remotes/origin/{name}" if remote else f"refs/heads/{name}"
    return git("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


@contextlib.contextmanager
def stop_as_exception():
    """systemctl stop の SIGTERM も Ctrl-C と同じく例外にする(後始末の except を通すため。既定の SIGTERM は即死)。"""
    def stop(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    old = signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def squash_into_main(branch: str, message: str) -> bool:
    """checkout 済みの main へ branch を squash merge し、差分があれば commit する。新しい commit を作ったかを返す。
    衝突・commit の失敗・中断(Ctrl-C・systemctl stop)では、この処理が作った index と作業ツリーの変更を squash 前の
    main へ戻してから例外を上げる(呼び出し側は開始時に clean を確かめている)。edition の commit には触らない。
    2026-10-14 監査指摘: main と edition の同じファイルが衝突すると、未解決の index(UU)と stage 済みの号が残り、
    次の release が dirty で止まった。squash には MERGE_HEAD が無く merge --abort は効かないので、reset で戻す"""
    pre = git("rev-parse", "HEAD").stdout.strip()
    try:
        with stop_as_exception():
            m = git("merge", "--squash", branch, check=False)
            if m.returncode != 0:
                conflicted = git("diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
                raise RuntimeError(f"git merge --squash {branch} 失敗"
                                   + (f"(衝突: {', '.join(conflicted[:8])})" if conflicted else "")
                                   + f": {(m.stdout + m.stderr).strip()[-400:]}")
            if git("diff", "--cached", "--quiet", check=False).returncode == 0:
                return False
            git("commit", "-m", message)
            return True
    except BaseException:
        git("reset", "--hard", pre, check=False)       # SQUASH_MSG も reset が消す
        raise


def main_missing_on_origin(date: str) -> str:
    """手元の main(この号を含む)が origin/main に届いているかを origin から取り直して確かめる。届いていなければ理由を返す。"""
    f = git("fetch", "origin", "main", check=False)
    if f.returncode != 0:
        return f"origin の main を取り直せない: {f.stderr.strip()[:200]}"
    if git("merge-base", "--is-ancestor", "main", "origin/main", check=False).returncode != 0:
        return "手元の main(号の commit)が origin/main に無い"
    if git("cat-file", "-e", f"origin/main:docs/_editions/{date}.md", check=False).returncode != 0:
        return f"origin/main に docs/_editions/{date}.md が無い"
    return ""


def lint_failure_summary(r: subprocess.CompletedProcess) -> str:
    """lint 失敗時の通知文。エラーの中身(最大5件)まで載せる(「lint 赤」だけでは原因が追えない)。"""
    errs = re.findall(r"^::error(?: file=([^:]*))?::(.*)$", r.stdout, re.MULTILINE)
    if not errs:  # lint 自体のクラッシュ(traceback 等)
        tail = (r.stderr.strip() or r.stdout.strip())[-500:]
        return f"lint が異常終了(exit {r.returncode}):\n```\n{tail}\n```"
    lines = [f"- {Path(f).name + ': ' if f else ''}{m}" for f, m in errs[:5]]
    if len(errs) > 5:
        lines.append(f"- …ほか {len(errs) - 5} 件")
    return f"lint エラー {len(errs)} 件:\n" + "\n".join(lines)


def review_verdict(date: str) -> tuple[str, str]:
    """その号の校閲結果(最後の往復)を読む。(verdict, 説明) を返す。

    compose が `metrics/review-<日付>-<往復数>.json` に残す。
    往復数は数として比べる(文字列順だと 10 が 9 より前に来る)。
    """
    files = sorted((ROOT / "metrics").glob(f"review-{date}-*.json"),
                   key=lambda p: int(p.stem.rsplit("-", 1)[-1]) if p.stem.rsplit("-", 1)[-1].isdigit() else -1)
    if not files:
        return "missing", "校閲の記録が無い(compose が最後まで走っていない)"
    try:
        d = json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception as e:
        return "unreadable", f"{files[-1].name} が読めない: {e}"
    if d.get("verdict") == "approve":
        # approve は「その時点の中身」への判定。校閲のあとに記事や digest が変わって
        # いたら、その approve は現在の紙面のものではない(監査指摘)。校閲が残した指紋と
        # 現在の指紋を照合する。指紋の無い古い記録は照合できないので、そのまま通す
        recorded = d.get("hashes") or {}
        if not recorded and date >= "2026-09-12":
            # 指紋の無い approve は「何を承認したか」が分からない。指紋を残す実装より
            # 後の号では、それを approve と見なさない(監査指摘)。それより前の号だけ通す
            return "stale", f"{files[-1].name} に指紋(hashes)が無い。何を承認したのか照合できない"
        if recorded:
            sys.path.insert(0, str(ROOT / "scripts"))
            from pipelib import review_manifest
            now = review_manifest(date)
            changed = sorted(k for k in set(recorded) | set(now) if recorded.get(k) != now.get(k))
            if changed:
                return "stale", (f"{files[-1].name} の approve のあとに中身が変わっている"
                                 f"({len(changed)}件: " + ", ".join(Path(c).name for c in changed[:4]) + ")")
        return "approve", files[-1].name
    blockers = d.get("blockers") or []
    head = "; ".join(f"{b.get('file')}: {(b.get('issue') or '')[:80]}" for b in blockers[:3])
    return "block", f"{files[-1].name} に残ブロック {len(blockers)}件 — {head}"


DEPLOY_STATE_MARK = "配信の状態: "      # deploy.py が最後に出す行(公開の状態。deploy.STATE_MARK と同じ)


def deploy_site() -> str:
    """自前オリジンへ配信する。失敗しても発行そのものは巻き戻さない。

    紙面は main への push で確定しており、配信は「届け方」の工程である。復旧は deploy.py の再実行。
    失敗したときの公開の状態(current と索引がどの版か)は、止まった所で違う。deploy.py が最後に出す
    「配信の状態」をそのまま伝える(2026-10-14 監査指摘: current を切り替えた後の索引の失敗でも
    「current は前号のまま」と通知していた)。
    """
    try:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "deploy.py")],
                           cwd=ROOT, capture_output=True, text=True, timeout=2400)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        r = subprocess.CompletedProcess(e.cmd, -9, out, "deploy.py が時間切れで止められた")
    print(r.stdout[-1500:], flush=True)
    if r.returncode == 0:
        return "自前オリジンへ配信済み。"
    states = [l[len(DEPLOY_STATE_MARK):] for l in r.stdout.splitlines() if l.startswith(DEPLOY_STATE_MARK)]
    state = states[-1] if states else "deploy.py の出力から読めない(current と索引を確かめること)"
    tail = (r.stderr.strip() or r.stdout.strip())[-400:]
    notify(f"配信に失敗しました(紙面は main に確定済み)。公開の状態: {state}。"
           f"`python3 scripts/deploy.py` で再実行してください:\n```\n{tail}\n```", ok=False)
    return "⚠️配信は失敗(要再実行)。"


def ensure_next_branch(next_name: str, dry: bool) -> None:
    if branch_exists(next_name) or branch_exists(next_name, remote=True):
        # **既にあるなら main を取り込む。**collect は翌日ブランチを main から先に切る。
        # その日の発行が遅れると、翌日ブランチは発行前の main から出発したまま、
        # 発行で main に入った修正(スクリプト・判定表)を持たない。以前は
        # 「既に存在」と言うだけで放置し、毎日人が手でマージして衝突を解いていた
        # (2026-09-07〜09、3日連続)。衝突するのは決まって2種類なので、機械で解く:
        #   metrics/<発行日>.json … 発行側(main)を採る
        #   stock/watch-state.json … 生きているブランチ側(新しいほう)を採る
        # それ以外の衝突は解かずに戻し、人へ知らせる
        print(f"翌日ブランチ {next_name} は既に存在。main を取り込む", flush=True)
        if dry:
            return
        if not branch_exists(next_name):
            git("checkout", "-B", next_name, f"origin/{next_name}")
        else:
            git("checkout", next_name)
            if branch_exists(next_name, remote=True):
                git("pull", "--ff-only", "origin", next_name, check=False)
        r = git("merge", "main", "-m", f"Merge branch 'main' into {next_name}(発行後の取り込み)", check=False)
        if r.returncode != 0:
            conflicted = [l.split("\t", 1)[1] for l in
                          git("diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines()
                          if "\t" in l] or git("diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
            others = []
            for f in conflicted:
                if re.fullmatch(r"metrics/\d{4}-\d{2}-\d{2}\.json", f):
                    git("checkout", "--theirs", f)      # main 側
                    git("add", f)
                elif f == "stock/watch-state.json":
                    git("checkout", "--ours", f)        # ブランチ側
                    git("add", f)
                else:
                    others.append(f)
            if others:
                git("merge", "--abort", check=False)
                notify(f"翌日ブランチ {next_name} への main の取り込みが衝突した(手で解くこと): "
                       + ", ".join(others[:8]), ok=False)
                return
            git("commit", "--no-edit", check=False)
        # union merge で判定表の行が二重になったら、ここで除いて同じ push に載せる(2026-09-17: 5行が二重)
        from pipelib import dedupe_source_table
        removed = dedupe_source_table()
        if removed:
            a = git("add", "--", "source_types.yml", check=False)
            c = git("commit", "-q", "-m", f"判定表: merge で二重になった {len(removed)} 行を除く", check=False) \
                if a.returncode == 0 else a
            if c.returncode != 0:   # 除去を commit できなければ、二重のまま push せず人へ(監査指摘)
                notify(f"翌日ブランチ {next_name}: 判定表の二重行を除いたが commit できない(push せず): "
                       f"{(c.stderr or c.stdout).strip()[:200]}", ok=False)
                return
            print(f"判定表の二重行 {len(removed)} 件を除いた", flush=True)
        git("push", "origin", next_name)
        print(f"翌日ブランチ {next_name} に main を取り込んで push", flush=True)
        return
    if dry:
        print(f"[dry-run] {next_name} を main から作成して push する", flush=True)
        return
    git("checkout", "-b", next_name, "main")
    git("push", "-u", "origin", next_name)
    print(f"翌日ブランチ {next_name} を作成・push", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="発行日(既定: JST 今日)")
    ap.add_argument("--dry-run", action="store_true", help="push せず手順の検証のみ")
    args = ap.parse_args()
    date = args.date or datetime.datetime.now(JST).strftime("%Y-%m-%d")
    nxt = "edition/" + (datetime.date.fromisoformat(date) + datetime.timedelta(days=1)).isoformat()
    branch = f"edition/{date}"
    global RELEASE_DATE
    RELEASE_DATE = date          # 終了時のなぜなぜに「異常の発生日」として渡す
    dry = args.dry_run
    # 通知の抑止は**最初の判定より前**に立てる。作業ツリーや号スナップショットの
    # 検査は dry でも走るので、ここが遅れると点検で警報が飛ぶ
    global DRY_RUN
    DRY_RUN = dry

    # 同じ作業ツリーを collect/compose/当番と同時に触らない(監査指摘)。組版が長引いていれば待つ
    from pipelib import JobLockTimeout, job_lock
    try:
        _lock = job_lock("release", wait_min=30)
    except JobLockTimeout as e:
        notify(str(e), ok=False)
        return 1

    # 0. 作業ツリーが汚れていたら触らない(自動実行の安全弁)
    dirty = git("status", "--porcelain").stdout.strip()
    if dirty:
        files = dirty.splitlines()
        listing = "\n".join(files[:10]) + (f"\n…ほか {len(files) - 10} 件" if len(files) > 10 else "")
        notify(f"{date}: 作業ツリーに未コミットの変更があるため発行を中止:\n```\n{listing}\n```", ok=False)
        return 1

    git("fetch", "origin", "--prune")

    # 1. 発行対象ブランチの確認(既発行・休刊の判定)
    if not branch_exists(branch) and not branch_exists(branch, remote=True):
        if git("cat-file", "-e", f"origin/main:docs/_editions/{date}.md", check=False).returncode == 0:
            print(f"{date} 号は発行済み(main に存在)", flush=True)
            git("checkout", "main")
            git("pull", "--ff-only", "origin", "main")
            ensure_next_branch(nxt, dry)
            return 0
        notify(f"{date}: edition ブランチが存在しない(収集・compose 未実行?)。発行なし", ok=False)
        return 1

    # 2. ブランチを origin と同期して checkout
    #
    # **origin へ強制的に合わせる前に、手元だけにある commit を確かめる。**
    # `checkout -B` は手元のブランチを origin の位置へ引き戻すので、push して
    # いない commit は黙って消える。実測 2026-09-04: 校閲が下ろさなかった記事を
    # 落として組版し直した commit が、発行の中止処理でそのまま消えた。
    # 消してよいものではないので、push してから進む
    if branch_exists(branch, remote=True):
        ahead = git("rev-list", f"origin/{branch}..{branch}", check=False).stdout.strip()
        if ahead and dry:
            # 点検(--dry-run)では push しない。origin へ引き戻す同期(checkout -B)もしない(手元だけの commit が消える)。
            # 手元のブランチをそのまま点検する(2026-10-14 監査指摘: dry-run でも push していた)
            print(f"[dry-run] {branch} に origin へ出していない commit が {len(ahead.splitlines())}件ある"
                  "(発行なら先に push する。点検では push しない)", flush=True)
            git("checkout", branch)
        else:
            if ahead:
                print(f"{branch} に origin へ出していない commit が {len(ahead.splitlines())}件ある。先に push する", flush=True)
                git("push", "origin", branch)
            git("checkout", "-B", branch, f"origin/{branch}")
    else:
        git("checkout", branch)

    ed_file = ROOT / "docs" / "_editions" / f"{date}.md"
    if not ed_file.exists():
        notify(f"{date}: ブランチに号スナップショットが無い(compose 未実行?)。発行中止", ok=False)
        return 1
    m = re.search(r"^number:\s*(\d+)", ed_file.read_text(encoding="utf-8"), re.MULTILINE)
    number = int(m.group(1)) if m else -1
    m2 = re.search(r"^article_count:\s*(\d+)", ed_file.read_text(encoding="utf-8"), re.MULTILINE)
    count = int(m2.group(1)) if m2 else 0

    # 3. マージ前ゲート: 校閲(REQUIREMENTS 4.5)
    #
    # lint しか見ていなかったため、**校閲が approve していない号がそのまま出ていた**
    # (実測: 10号中3号。2026-08-27号は「出典にない事実」の指摘を残したまま配信)。
    # compose はブロックされた記事を落としてから取り直すので、ここまで来て
    # 未 approve なら、機械で落とせない指摘(一面・社説・号スナップショット)が
    # 残っているということ。人が見るべき状態なので発行しない。
    verdict, detail = review_verdict(date)
    if verdict != "approve":
        notify(f"{date}: 発行中止(第{number}号)。校閲が approve していない({detail})", ok=False)
        # 人へ投げる前に当番へ。compose が途中で止まった号は、当番が直して compose を回し直す
        sys.path.insert(0, str(ROOT / "scripts"))
        from pipelib import escalate
        escalate("compose", date, f"release: 校閲が approve していない({detail})")
        return 1

    # 4. マージ前ゲート: lint(REQUIREMENTS 4.4)
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lint.py"), "--base", "origin/main"],
        cwd=ROOT, capture_output=True, text=True,
    )
    print(r.stdout[-2000:], flush=True)
    if r.returncode != 0:
        notify(f"{date}: 発行中止(第{number}号)。{lint_failure_summary(r)}", ok=False)
        # 組版が緑で通した号が発行時に赤なら、組版と発行の間の食い違い(基準コミット・台帳)か
        # lint 自体の欠陥。人ではなく当番に渡す(監査指摘)
        from pipelib import escalate
        escalate("release", date, "発行前の lint が赤:\n" + (r.stdout or "")[-3000:])
        return 1

    label = f"第{number}号" + ("(試験)" if number == 0 else "")
    if dry:
        notify(f"[dry-run] {date}: {label}({count}本)を squash merge → push → {branch} 削除 → {nxt} 作成、の手順を確認。実行は行わない")
        git("checkout", "main")
        return 0

    # 4. squash merge → push(これが「発行」)
    git("checkout", "main")
    git("pull", "--ff-only", "origin", "main")
    from pipelib import escalate
    try:
        made = squash_into_main(branch, f"{label} {date} 発行({count}本)")
    except RuntimeError as e:
        notify(f"{date}: 発行中止(第{number}号)。main への squash merge に失敗した(作業ツリーは squash 前の main に戻した。"
               f"{branch} はそのまま):\n```\n{str(e)[-600:]}\n```", ok=False)
        escalate("release", date, f"main への squash merge に失敗(作業ツリーは squash 前の main に戻した):\n{e}")
        return 1
    if not made:
        print("squash で新しい差分なし(main は既にこの号を含む)", flush=True)
    # 新しい commit を作ったかと、push が要るかは別に決める。手元の main が origin より先にあれば(前回 push できなかった
    # 号の commit を含む)ここで送り、origin に届いたことを確かめてから配信・ブランチ削除へ進む
    # (2026-10-14 監査指摘: main の push だけ失敗した後の再実行で、squash が「差分なし」になって push を省き、
    # origin/main に号が無いまま配信・edition ブランチの削除・発行の通知へ進んだ)
    if git("rev-list", "origin/main..main", check=False).stdout.strip():
        p = git("push", "origin", "main", check=False)
        if p.returncode != 0:
            notify(f"{date}: 発行中止(第{number}号)。main への push に失敗した(号の commit は手元の main にある。"
                   f"{branch} は消していない。release の再実行で push する):\n```\n{p.stderr.strip()[-400:]}\n```", ok=False)
            escalate("release", date, f"main への push に失敗(号の commit は手元の main にある):\n{p.stderr.strip()[-1000:]}")
            return 1
    missing = main_missing_on_origin(date)
    if missing:
        notify(f"{date}: 発行中止(第{number}号)。origin に号が届いたことを確かめられない({missing})。"
               f"{branch} は消していない", ok=False)
        escalate("release", date, f"origin に号が届いたことを確かめられない: {missing}")
        return 1

    # 5. 自前オリジンへ配信(main へ push した紙面をビルドして current を差し替える)
    #    ここが実際の「読者に届く」工程。GitHub Pages は main への push で
    #    自動追従するフォールバックとして残している。
    deployed = deploy_site()

    # 6. 発行済みブランチの削除 → 翌日ブランチ作成
    git("branch", "-D", branch, check=False)
    if branch_exists(branch, remote=True):
        git("push", "origin", "--delete", branch, check=False)
    ensure_next_branch(nxt, dry=False)

    notify(f"{date}: {label}({count}本)を発行しました。{deployed} 翌日ブランチ {nxt} 準備済み")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr, flush=True)
        tail = "\n".join(tb.strip().splitlines()[-6:])
        notify(f"想定外のエラーで停止: {e}\n```\n{tail}\n```\n(全文: journalctl --user -u imas-release)", ok=False)
        code = 1
    if not DRY_RUN:
        # 人に「異常」として通知したものは、申告で終えずに当番がなぜなぜする(止まった理由で既に当番を呼んでいれば重ねて呼ばない)。
        # 異常の発生日は発行日(journal をその日から読む)。修正の取り込み先は生きている号(発行済みなら今日の branch は無い)
        from pipelib import diagnose_anomalies, edition_date
        day = RELEASE_DATE or datetime.datetime.now(JST).strftime("%Y-%m-%d")
        # 発行できずに号の branch が残っているか(ローカルか origin のどちらか。監査指摘 r87: 新しい clone では origin にしか無い)
        still = branch_exists(f"edition/{day}") or branch_exists(f"edition/{day}", remote=True)
        diagnose_anomalies("release", day, rerun=False, edition=(day if still else edition_date()))
    sys.exit(code)
