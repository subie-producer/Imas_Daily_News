#!/usr/bin/env python3
"""unit_failed: 定時の工程(imas-*.service)と当番の unit が失敗したら、人に知らせて当番に渡す(systemd の OnFailure から起動)。

  python3 scripts/unit_failed.py <失敗した unit の名前>

工程は自分の異常を通知してから非0で終わる。それができずに止まった(OOM で殺された・シグナル・時間切れ・通知せずに非0)ときは、
誰も知らせない(2026-10-27: 09:00 の watch が 6.4G を使って 16:38 に OOM で殺されたが、人には何も届かなかった)。
- 止まり方が OOM・シグナル・時間切れ・コアダンプなら、必ず知らせる(工程の中からは知らせられない)
- 終了コードで終わった(exit-code)なら、その走行が自分で通知(🚨)していれば何もしない(二重にしない)。していなければ知らせる
- 知らせたら、その工程の当番に渡す(なぜなぜ。組版・発行は止まった工程を当番が直して取り直す)。当番の unit 自身の失敗は知らせるだけ
"""
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import diagnose_anomalies, edition_date, notify, now_jst

# 止まり方(systemd の Result)の読み方
RESULTS = {"oom-kill": "メモリ不足で OOM killer に殺された", "signal": "シグナルで止まった", "timeout": "時間の上限で止められた",
           "core-dump": "異常終了した(コアダンプ)", "watchdog": "応答が無く止められた", "exit-code": "非0で終わった",
           "start-limit-hit": "起動し直しの上限に達した", "resources": "起動に必要な資源が無かった"}
NOTICE = "🚨 アイマスNEWS"


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


def reported_itself(invocation: str) -> bool:
    """その走行が自分で人に異常を通知したか(journal にその走行の 🚨 の行がある)。読めなければ False(知らせる側に倒す)。"""
    if not invocation:
        return False
    r = subprocess.run(["journalctl", "--user", f"_SYSTEMD_INVOCATION_ID={invocation}", "-o", "cat", "--no-pager"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return r.returncode == 0 and NOTICE in r.stdout


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


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("使い方: unit_failed.py <unit>", file=sys.stderr)
        return 2
    unit = argv[0]
    props = unit_props(unit)
    result = props.get("Result", "")
    if result == "success":
        return 0
    if result == "exit-code" and reported_itself(props.get("InvocationID", "")):
        print(f"{unit}: 非0で終わったが、工程が自分で通知している(二重にしない)", flush=True)
        return 0
    stage = stage_of(unit)
    msg = (f"{unit} が{RESULTS.get(result, f'失敗した({result or '止まり方が読めない'})')}"
           f"(終了 {props.get('ExecMainStatus', '?')}・メモリのピーク {gib(props.get('MemoryPeak', ''))}"
           f"・スワップのピーク {gib(props.get('MemorySwapPeak', ''))})。工程は自分で知らせる前に止まった。"
           f"journalctl --user -u {unit} で経過を確かめること")
    delivered = notify(stage if stage and stage != "oncall" else "systemd", msg, ok=False, ledger=stage != "oncall")
    if stage and stage != "oncall":
        # 組版・発行が止まったら紙面が出ないので、当番が直して取り直す(組版はこれから出す号、発行は今日の号)。
        # ほかは原因を直すだけ(次の定時の回が取り直す。収集の欠けた回は組版が名指しする)
        today = now_jst().strftime("%Y-%m-%d")
        if stage == "compose":
            diagnose_anomalies(stage, edition_date(), rerun=True, reason=msg)
        elif stage == "release":
            diagnose_anomalies(stage, today, rerun=True, reason=msg)
        else:
            diagnose_anomalies(stage, today, rerun=False, reason=msg, edition=edition_date())
    return 0 if delivered else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
