#!/usr/bin/env python3
"""child_guard: 執筆・書き直しの子(モデルの CLI)を、子が起動した道具の実行ごと片付けてから終わる。

  python3 scripts/child_guard.py --status <ファイル> <コマンド> [引数...]

自分を孤児の受け皿(PR_SET_CHILD_SUBREAPER)にしてからコマンドを起動する。コマンドの子孫は、別のセッション・プロセスグループに
抜けても、環境を消しても、親が先に終われば init ではなくここへ付け替わる。コマンドが終わったら(または SIGTERM を受けたら)、
自分の子孫を全部止め、子が1つも残っていない(waitpid が ECHILD を返す)ことを確かめてから、コマンドの終了コードで終わる。

起動できたか・片付けを確かめられたかは、終了コードではなく --status のファイル(JSON: started・cleaned・note)で返す。
終了コードはコマンドのもの(どの値もコマンドが返しうるので、ここの結果と取り違えない。2026-10-14 監査: 96 を起動の失敗に
使ったら、CLI が普通に返した 96 を起動の失敗と取り違えた)。

2026-10-14 監査: 子の CLI が正常終了・異常終了しても、道具が別のセッションに抜けて残り、compose が記事を控えへ戻したあとに
承認済みの記事を書き換えた(プロセスグループ・環境の印では見つけられない道具がある。/proc/<pid>/environ を読めなくする等)。
"""
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PR_SET_CHILD_SUBREAPER = 36


def descendants(root: int) -> list[int]:
    """root の子孫(/proc の親子関係をたどる。stat は道具が自分を読めなくしても読める)。"""
    kids: dict[int, list[int]] = {}
    for st in Path("/proc").glob("[0-9]*/stat"):
        try:
            pid = int(st.parent.name)
            ppid = int(st.read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(pid)
    out, todo = [], [root]
    while todo:
        for k in kids.get(todo.pop(), []):
            out.append(k)
            todo.append(k)
    return out


def clean(grace: float = 10.0) -> bool:
    """子孫を全部止めて回収する。子が1つも残らなければ True(孤児はここへ付け替わるので、ECHILD は子孫が無いことの証拠)。"""
    end = time.monotonic() + grace
    while True:
        for q in descendants(os.getpid()):
            try:
                os.kill(q, signal.SIGKILL)
            except OSError:
                pass
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def report(path: str, started: bool, cleaned: bool, note: str = "") -> None:
    """結果を --status のファイルへ書く(一度に差し替える。読む側が書きかけを読まない)。"""
    if not path:
        return
    tmp = f"{path}.tmp"
    Path(tmp).write_text(json.dumps({"started": started, "cleaned": cleaned, "note": note}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    args = sys.argv[1:]
    status = ""
    if args[:1] == ["--status"] and len(args) >= 2:
        status, args = args[1], args[2:]
    if not args:
        print("使い方: child_guard.py --status <ファイル> <コマンド> [引数...]", file=sys.stderr)
        return 2
    libc = ctypes.CDLL(None, use_errno=True)
    subreaper = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    termed: list = []
    child: list = []

    def on_term(signum, frame):    # compose の時間切れ: コマンドを止めて片付けへ進む
        termed.append(signum)
        try:
            if child:
                child[0].kill()
        except OSError:
            pass
    signal.signal(signal.SIGTERM, on_term)     # 起動より前に受け付ける(起動の前後に来た SIGTERM も取りこぼさない)
    try:
        child.append(subprocess.Popen(args))
    except OSError as e:
        report(status, False, True, f"起動できない: {args[0]}: {e}")
        print(f"child_guard: 起動できない: {args[0]}: {e}", file=sys.stderr)
        return 127
    if termed:
        child[0].kill()
    rc = child[0].wait()
    ok = clean() and subreaper     # 受け皿になれなかったら、init へ付け替わった孤児は追えない(片付けたと言わない)
    note = "" if ok else ("孤児の受け皿になれない" if not subreaper else "止めても残っている")
    report(status, True, ok, note)
    if not ok:
        print(f"child_guard: 道具の実行を片付けられなかった({note})", file=sys.stderr)
    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    sys.exit(main())
