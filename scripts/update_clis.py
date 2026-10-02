#!/usr/bin/env python3
"""update_clis: 道具の CLI(Claude Code / Codex / Grok)を更新し、実際に答えが返ることを確かめる。毎日 01:30(imas-update.timer)。

  python3 scripts/update_clis.py [--no-update]

2026-10-02 の 02:00 の収集で、Grok CLI 1.0.5 が API に「版が古い(426 Upgrade Required)」と拒まれ、9面すべてが0件になった。
候補はいつもの半分以下(42件)、号は16本に落ちた。CLI はどれも人が手で更新するまで古いまま動いており、古くなって拒まれる日が来れば
その工程が全滅する。編集長「定期的に Codex/Grok/Claude のアップデートを仕掛けるようにしないと駄目だ」。

- 工程の排他(job_lock)を取ってから更新する(収集・組版の最中に実行ファイルを差し替えない)
- 更新のあと、各 CLI に小さな問いを投げて**答えの中身**で動作を確かめる(Grok は API に拒まれても終了コードが 0 だった)
- 更新の失敗・動作確認の失敗は異常として通知し、当番がなぜなぜする。版が変わったら知らせる
- 版の記録は metrics/cli-versions.json(Git 管理外)
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import (ROOT, CODEX_WRITE_MODEL, JobLockTimeout, diagnose_anomalies, edition_date, job_lock, notify, now_jst,
                     tool_path)

RECORD = ROOT / "metrics" / "cli-versions.json"
QUESTION = "1+1は? 半角の数字だけを答えて"
# 工程の排他(job_lock)を 02:00 の収集の前に必ず手放す期限。定時(01:30 起動)ではその日の 01:55、手で回したときは起動から 25分
# (更新が長引くと 02:00 の収集がロックを取れず、Grok を回す唯一の回を失う。監査指摘 r110)
DEADLINE_HHMM = (1, 55)
_deadline = 0.0


def remaining() -> float:
    """期限までの残り秒。期限と同じ時計(now_jst)で測る。"""
    return _deadline - now_jst().timestamp()


def set_deadline() -> None:
    """期限を**ロックを待つ前に**決める(あとから延ばさない。監査指摘 r111)。定時の起動(systemd から呼ばれた)は、起動が遅れても
    その日の 01:55(もう過ぎていれば即期限切れ=何もせず異常)。手で回したときは起動から 25分。"""
    import os
    global _deadline
    now = now_jst()
    if os.environ.get("INVOCATION_ID"):
        _deadline = now.replace(hour=DEADLINE_HHMM[0], minute=DEADLINE_HHMM[1], second=0, microsecond=0).timestamp()
    else:
        _deadline = now.timestamp() + 25 * 60


def run(args: list[str], timeout: int = 600, cwd: Path | None = None) -> subprocess.CompletedProcess:
    import os
    timeout = max(1, int(min(timeout, remaining()))) if _deadline else timeout
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              cwd=cwd or ROOT, env={**os.environ, "PATH": tool_path()})
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(args, 124, "", f"{type(e).__name__}: {e}")


def version(tool: str) -> str:
    r = run([tool, "--version"], timeout=60)
    m = re.search(r"\d+\.\d+\.\d+", r.stdout or "")
    return m.group(0) if m else f"(不明: {(r.stderr or r.stdout).strip()[-80:]})"


def smoke(tool: str, workdir: Path) -> tuple[bool, str]:
    """小さな問いを投げて、答えに 2 が返るか。戻りは (動いたか, 失敗なら理由)。"""
    if tool == "claude":
        r = run(["claude", "-p", QUESTION, "--model", "haiku", "--max-budget-usd", "0.5"], timeout=300, cwd=workdir)
    elif tool == "codex":
        r = run(["codex", "exec", "-m", CODEX_WRITE_MODEL, "--skip-git-repo-check", "-s", "read-only", QUESTION], timeout=300, cwd=workdir)
    else:
        pf = workdir / "grok-smoke.md"
        pf.write_text(QUESTION + "\n", encoding="utf-8")
        r = run(["grok", "--prompt-file", str(pf), "--always-approve", "--max-turns", "1"], timeout=300, cwd=workdir)
    out = (r.stdout or "").strip()
    if re.search(r"(^|\D)2(\D|$)", out.splitlines()[-1] if out else ""):
        return True, ""
    return False, f"exit {r.returncode} / 出力 {out[-160:]!r} / エラー {(r.stderr or '').strip()[-240:]!r}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-update", action="store_true", help="更新せず、動作確認だけする")
    args = ap.parse_args()
    set_deadline()
    # ロックを待つのも期限の5分前まで(23:30 の収集の居残りなどを待つ。待ちすぎて 02:00 の収集の前にロックを持たない)
    wait = int((remaining() - 300) // 60)
    try:
        if wait < 0:
            raise JobLockTimeout(f"期限({DEADLINE_HHMM[0]:02d}:{DEADLINE_HHMM[1]:02d})を過ぎて起動した")
        _lock = job_lock("update", wait_min=wait)
    except JobLockTimeout as e:
        notify("update", f"道具の更新ができなかった({e})。古い版のまま次の工程が走る", ok=False)
        return 1
    rows, changed, failed = {}, [], []
    with tempfile.TemporaryDirectory(prefix="imas-cli-smoke-") as wd:
        for tool in ("claude", "codex", "grok"):
            if remaining() < 120:
                # 期限までに更新と動作確認を終えられない。ロックを手放して収集に譲り、未更新を異常として残す
                failed.append(f"{tool}: 期限({DEADLINE_HHMM[0]:02d}:{DEADLINE_HHMM[1]:02d})までに更新できず、古い版のまま")
                rows[tool] = {"skipped": "期限切れ"}
                continue
            before = version(tool)
            up = None
            if not args.no_update:
                up = run([tool, "update"], timeout=900)
            after = version(tool)
            ok, why = smoke(tool, Path(wd))
            rows[tool] = {"before": before, "after": after, "update_exit": up.returncode if up else None, "works": ok, "why": why}
            if before != after:
                changed.append(f"{tool} {before} → {after}")
            if up is not None and up.returncode != 0:
                failed.append(f"{tool} の更新が失敗(exit {up.returncode}): {(up.stderr or up.stdout or '').strip()[-240:]}")
            if not ok:
                failed.append(f"{tool} {after} が答えを返さない: {why}")
            print(f"{tool}: {before} → {after} / 動作 {'OK' if ok else 'NG'}", flush=True)
    try:
        RECORD.parent.mkdir(parents=True, exist_ok=True)
        RECORD.write_text(json.dumps({"at": now_jst().isoformat(timespec="seconds"), "tools": rows}, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    except OSError as e:
        print(f"版の記録を保存できない({e})", flush=True)
    if changed:
        notify("update", "道具の CLI を更新した: " + " / ".join(changed))
    if failed:
        # 古い版や壊れた版のまま収集・組版が走ると、その工程が全滅する(2026-10-02 の Grok)。申告で終えず当番がなぜなぜする
        notify("update", "道具の CLI の更新・動作確認に失敗(このままだと次の工程で全滅しうる):\n- " + "\n- ".join(failed), ok=False)
    diagnose_anomalies("update", now_jst().strftime("%Y-%m-%d"), rerun=False, edition=edition_date())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
