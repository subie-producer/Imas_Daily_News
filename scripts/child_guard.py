#!/usr/bin/env python3
"""child_guard: 執筆・書き直しの子(モデルの CLI)を、子が起動した道具の実行ごと片付けてから終わる。

  python3 scripts/child_guard.py <コマンド> [引数...]

自分を孤児の受け皿(PR_SET_CHILD_SUBREAPER)にしてからコマンドを起動する。コマンドの子孫は、別のセッション・プロセスグループに
抜けても、環境を消しても、親が先に終われば init ではなくここへ付け替わる。コマンドが終わったら(または SIGTERM を受けたら)、
自分の子孫を全部止め、子が1つも残っていない(waitpid が ECHILD を返す)ことを確かめてから、コマンドの終了コードで終わる。
片付けを確かめられなければ CLEANUP_FAILED で終わる。

2026-10-14 監査: 子の CLI が正常終了・異常終了しても、道具が別のセッションに抜けて残り、compose が記事を控えへ戻したあとに
承認済みの記事を書き換えた(プロセスグループ・環境の印では見つけられない道具がある。/proc/<pid>/environ を読めなくする等)。
"""
import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PR_SET_CHILD_SUBREAPER = 36
CANNOT_START = 96             # コマンドを起動できなかった(compose は直接起動したときと同じく FileNotFoundError にする)
CLEANUP_FAILED = 97           # 子孫を片付けたと確かめられなかった(compose は答えを採らず、異常として申告する)


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


def main() -> int:
    if len(sys.argv) < 2:
        print("使い方: child_guard.py <コマンド> [引数...]", file=sys.stderr)
        return 2
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        print(f"child_guard: 孤児の受け皿になれない(errno {ctypes.get_errno()})。道具の実行を片付けられない", file=sys.stderr)
        return CLEANUP_FAILED
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
        child.append(subprocess.Popen(sys.argv[1:]))
    except OSError as e:
        print(f"child_guard: 起動できない: {sys.argv[1]}: {e}", file=sys.stderr)
        return CANNOT_START
    if termed:
        child[0].kill()
    rc = child[0].wait()
    if not clean():
        print("child_guard: 道具の実行を片付けられなかった(止めても残っている)", file=sys.stderr)
        return CLEANUP_FAILED
    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    sys.exit(main())
