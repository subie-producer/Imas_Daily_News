#!/usr/bin/env python3
"""unit_failed: 定時の工程(imas-*.service)と当番の unit が失敗したら、人に知らせて当番に渡す(systemd の OnFailure から起動)。

  python3 scripts/unit_failed.py <失敗した unit の名前>

工程は自分の異常を通知してから非0で終わる。それができずに止まった(OOM で殺された・シグナル・時間切れ・通知せずに非0)ときは、
誰も知らせない(2026-10-27: 09:00 の watch が 6.4G を使って 16:38 に OOM で殺されたが、人には何も届かなかった)。
その走行が人に通知を届けたか・当番に渡したかは、走行ごとの記録(pipelib.note_unit_run。表示した行ではなく届いたか)で見る。
- 止まり方が OOM・シグナル・時間切れ・コアダンプなら、必ず知らせて当番に渡す(工程の中からは知らせられない)
- 終了コードで終わった(exit-code)とき: 通知が1つでも届いていない・何も届けていない・当番に渡していないなら、知らせて当番に渡す
  (当番に渡し済みなら知らせるだけ)。全部届けて当番に渡し終えた走行は、非0で終わったことだけを短く知らせる(当番を重ねない。
  別の理由で非0になった走行も人には見える)
- 当番の unit 自身の失敗は知らせるだけ(当番に当番を重ねない)。人に渡し終えた終わり方(ONCALL_HANDED_EXIT)で通知が届いていれば何もしない
"""
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import ONCALL_HANDED_EXIT, diagnose_anomalies, edition_date, notify, now_jst, unit_run_notes

# 止まり方(systemd の Result)の読み方
RESULTS = {"oom-kill": "メモリ不足で OOM killer に殺された", "signal": "シグナルで止まった", "timeout": "時間の上限で止められた",
           "core-dump": "異常終了した(コアダンプ)", "watchdog": "応答が無く止められた", "exit-code": "非0で終わった",
           "start-limit-hit": "起動し直しの上限に達した", "resources": "起動に必要な資源が無かった"}


def unit_props(unit: str) -> dict[str, str]:
    """失敗した unit の状態(OnFailure の環境変数 MONITOR_* を優先し、無ければ systemctl から読む)。"""
    keys = ("Result", "ExecMainCode", "ExecMainStatus", "InvocationID", "MemoryPeak", "MemorySwapPeak")
    r = subprocess.run(["systemctl", "--user", "show", unit, *[f"--property={k}" for k in keys]],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    props = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    env = {"Result": "MONITOR_SERVICE_RESULT", "InvocationID": "MONITOR_INVOCATION_ID", "ExecMainStatus": "MONITOR_EXIT_STATUS"}
    for k, e in env.items():
        if os.environ.get(e):
            props[k] = os.environ[e]
    return props


def stage_of(unit: str) -> str:
    """unit の名前から工程(当番の --stage)を引く。当番の unit は oncall。"""
    if unit.startswith("imas-oncall-"):
        return "oncall"
    m = re.fullmatch(r"imas-([a-z]+)\.service", unit)
    return m.group(1) if m else ""


def gib(v: str) -> str:
    try:
        return f"{int(v) / 2**30:.1f}G"
    except ValueError:
        return "不明"


def hand_to_oncall(stage: str, msg: str) -> None:
    """止まった工程を当番に渡す。組版・発行が止まったら紙面が出ないので、当番が直して取り直す(組版はこれから出す号、発行は今日の号)。
    ほかは原因を直すだけ(次の定時の回が取り直す。収集の欠けた回は組版が名指しする)"""
    today = now_jst().strftime("%Y-%m-%d")
    if stage == "compose":
        diagnose_anomalies(stage, edition_date(), rerun=True, reason=msg)
    elif stage == "release":
        diagnose_anomalies(stage, today, rerun=True, reason=msg)
    else:
        diagnose_anomalies(stage, today, rerun=False, reason=msg, edition=edition_date())


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("使い方: unit_failed.py <unit>", file=sys.stderr)
        return 2
    unit = argv[0]
    props = unit_props(unit)
    result = props.get("Result", "")
    if result == "success":
        return 0
    stage = stage_of(unit)
    status = props.get("ExecMainStatus", "?")
    notes = unit_run_notes(props.get("InvocationID", ""))
    sent = [n for n in notes if "notice" in n and n.get("delivered") is True]
    unsent = [n for n in notes if "notice" in n and n.get("delivered") is not True]
    handed = any(n.get("escalated") for n in notes)
    job = stage if stage and stage != "oncall" else "systemd"
    if result == "exit-code" and sent and not unsent and (handed or stage == "oncall"):
        if stage == "oncall" and status == str(ONCALL_HANDED_EXIT):
            print(f"{unit}: 当番が人に渡し終えた終わり方で、通知は届いている(二重にしない)", flush=True)
            return 0
        msg = (f"{unit} が非0で終わった(終了 {status})。この走行は異常の通知 {len(sent)}件を届け"
               + ("、当番に渡し済み" if handed else "") + f"。journalctl --user -u {unit} で経過を確かめられる")
        return 0 if notify(job, msg, ok=False, ledger=False) else 1
    why = (f"通知 {len(unsent)}件が届いていない" if unsent else "異常の通知を1つも届けていない" if not sent
           else "当番に渡す前に終わった")
    msg = (f"{unit} が{RESULTS.get(result, f'失敗した({result or '止まり方が読めない'})')}"
           f"(終了 {status}・メモリのピーク {gib(props.get('MemoryPeak', ''))}"
           f"・スワップのピーク {gib(props.get('MemorySwapPeak', ''))})。"
           + ("工程は自分で知らせる前に止まった" if result != "exit-code" else f"この走行は{why}")
           + f"。journalctl --user -u {unit} で経過を確かめること")
    # 当番に渡し済みの走行(終了コードで終わった)は、届かなかった通知を知らせ直すだけ(同じ走行の当番を重ねない)
    to_oncall = stage not in ("", "oncall") and not (result == "exit-code" and handed)
    delivered = notify(job, msg, ok=False, ledger=to_oncall)
    if to_oncall:
        hand_to_oncall(stage, msg)
    return 0 if delivered else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
