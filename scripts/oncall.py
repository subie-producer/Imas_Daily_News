#!/usr/bin/env python3
"""oncall: 実装の欠陥で工程が止まったとき、人へ投げる前に**当番が直す**。

  python3 scripts/oncall.py --stage compose|release --date YYYY-MM-DD --reason "…"

パイプラインが「人間判断が必要」で止まるのは、ほぼ毎回コードかプロンプトの欠陥である
(実測: 2026-09-06〜12、7日連続で毎朝止まった。原因は全部こちらの実装)。人が起きて
直すまで号が出ないのは実用ではない。そこで:

1. **当番(Opus)** が、ログと状態から診断し、main から切った作業ツリーで最小の修正を書き、
   selfcheck と再現テストを通し、報告を JSON で返す(schema/oncall-fix.schema.json)
2. **監査(Sol)** が、その差分を敵対的にレビューし、判定を JSON で返す(oncall-review)
3. 当番が指摘を**敵対的に取り込む**: 受け入れるものは直し、当たらないものは根拠で反論する
   (oncall-integrate)。監査がもう一度見る。往復は最大2回
4. 合意(approve)したものだけ main へ入れ、生きている edition ブランチへ取り込み、
   止まった工程を再実行する(compose は --reuse-plan、release はそのまま)
5. 合意できなければ、両者の言い分を添えて人へ渡す

守ること:
- 当番が触ってよいのは scripts/ prompts/ schema/ と設計文書だけ。紙面・台帳・候補・metrics は触らない
- 同じ日・同じ工程で2回までしか試みない(ループ防止)
- 走行中の compose/release があるあいだは動かない
- 予算と時間の上限を持つ(ONCALL_MAX_BUDGET_USD / 1セッション 30分)
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import ENV, ROOT, notify

ONCALL_MODEL = ENV.get("ONCALL_MODEL", "opus")
AUDIT_MODEL = ENV.get("AUDIT_MODEL", "gpt-5.6-sol")
ONCALL_MAX_BUDGET_USD = ENV.get("ONCALL_MAX_BUDGET_USD", "15")
MAX_ATTEMPTS = 2
MAX_ROUNDS = 2
EDITABLE = ("scripts/", "prompts/", "schema/", "REQUIREMENTS.md", "PIPELINE.md", "AUDIT.md", "README.md",
            "source_types.yml", "sources.yml", ".gitattributes")


def sh(args, cwd, timeout=600, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=check,
                          stdin=subprocess.DEVNULL)


def lock_path(date: str, stage: str) -> Path:
    return ROOT / "metrics" / f"oncall-{date}-{stage}.json"


def gather_context(stage: str, date: str, reason: str) -> str:
    parts = [f"# 止まった工程: {stage} / 号: {date}\n\n## 呼び出し側の理由\n{reason}\n"]
    r = sh(["journalctl", "--user", "-u", f"imas-{stage}", "--since", f"{date} 00:00", "--no-pager", "-n", "250"],
           cwd=ROOT, timeout=60)
    if r.returncode == 0 and r.stdout.strip():
        lines = [re.sub(r"^.*?python3\[\d+\]: ", "", l) for l in r.stdout.splitlines()]
        parts.append("## journal(直近250行)\n" + "\n".join(lines[-250:]))
    r = sh(["git", "log", "--oneline", "-8"], cwd=ROOT)
    parts.append("## ops の直近 commit\n" + r.stdout)
    r = sh(["git", "status", "--short"], cwd=ROOT)
    parts.append("## ops の作業ツリー\n" + (r.stdout or "(clean)"))
    return "\n\n".join(parts)


def fix_prompt(stage: str, date: str, context: str, round_no: int, objections: list[dict] | None) -> str:
    obj = ""
    if objections:
        obj = ("\n\n## 監査からの指摘(敵対的に取り込む)\n"
               "受け入れるものは直し、当たらないものは**根拠を示して反論する**。馴れ合わない。\n"
               + json.dumps(objections, ensure_ascii=False, indent=1))
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の**当番エンジニア**です。自動発行の工程 `{stage}` が
{date}号で止まりました。人が起きるまで待たず、あなたが直します。この作業ツリーは main から切った
使い捨てで、直したものは監査の敵対的レビューを通ってから main に入ります。

## 何が起きたか
{context}

## やること
1. 診断する。ログと状態から**事実**を押さえる(推測は推測と書く)。再現できるなら再現する
2. 原因が**コードかプロンプトの欠陥**なら、最小の修正を書く。触ってよいのは
   scripts/ prompts/ schema/ と設計文書(REQUIREMENTS.md, PIPELINE.md, AUDIT.md)だけ。
   **docs/ stock/ candidates/ metrics/ は触らない**(紙面と台帳は当番の仕事ではない)
3. `python3 scripts/selfcheck.py` を通し、直した箇所を**実際に踏ませるテスト**を書いて走らせる
   (壊れた入力を与えて、直る前は落ち、直した後は通ることを見せる)
4. 原因がデータや環境で、コードを直しても意味が無いなら status=no_fix_needed。
   直せない・判断が要るなら status=cannot_fix(理由を書く)

## 設計の原則(これに反する直し方をしない)
- 判断は小さな構造化セッション、写す・数える・整える・反映するはコード
- 「lint に叩かれて直す」「同じプロンプトでやり直す」「別セッションに修理させる」を新しく作らない
- 成果物は入力の純関数(冪等)。巻き戻しや後始末セッションを増やさない
- 通知して続行、で済ませない。落ちたものはその場で潰す

最後に、報告を JSON で返してください(schema で形が決まっています)。{obj}
"""


def review_prompt(stage: str, date: str, fix_report: dict, diff: str, round_no: int, integ: dict | None) -> str:
    extra = ""
    if integ:
        extra = ("\n\n## 前回の指摘に対する当番の対応(受け入れ/反論)\n"
                 + json.dumps(integ, ensure_ascii=False, indent=1)
                 + "\n反論に根拠があれば認め、無ければ却下してください。")
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の監査役です。自動発行の工程 `{stage}` が {date}号で止まり、
当番(別モデル)が修正を書きました。**敵対的に**レビューしてください。馴れ合いは不要です。
「本当にそれが原因か」「その直し方で明日また止まらないか」「新しく壊れるものは何か」を掘ってください。

## 当番の報告
{json.dumps(fix_report, ensure_ascii=False, indent=1)}

## 差分(この作業ツリーで `git diff` / `git status` でも確認できます)
```diff
{diff[:60000]}
```

## 判定
- approve: この修正で工程が動き、データを壊さず、設計の原則(判断は小さな構造化セッション・
  事務処理はコード・冪等・通知して続行で済ませない)に反しない
- reject: must_fix に、根拠付きで問題を列挙する(推測は (推測) と明記)
{extra}
判定を JSON で返してください(schema で形が決まっています)。
"""


def run_claude(prompt: str, schema_file: Path, cwd: Path, timeout: int = 1800) -> dict:
    r = subprocess.run(["claude", "-p", prompt, "--model", ONCALL_MODEL, "--dangerously-skip-permissions",
                        "--json-schema", schema_file.read_text(encoding="utf-8"),
                        "--max-budget-usd", ONCALL_MAX_BUDGET_USD],
                       cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    out = (r.stdout or "").strip()
    try:
        return json.loads(out)
    except Exception:
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    raise RuntimeError(f"当番セッションの出力が読めない(exit {r.returncode}): {(r.stderr or out)[-300:]}")


def run_codex(prompt: str, schema_file: Path, cwd: Path, timeout: int = 1800) -> dict:
    fd, out_name = tempfile.mkstemp(prefix="oncall-review-", suffix=".json")
    os.close(fd)
    out_path = Path(out_name)
    try:
        subprocess.run(["codex", "exec", "-m", AUDIT_MODEL, "-s", "read-only", "--skip-git-repo-check",
                        "--output-schema", str(schema_file), "-o", str(out_path), prompt],
                       cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    finally:
        out_path.unlink(missing_ok=True)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
    raise RuntimeError(f"監査セッションの出力が読めない: {text[-300:]}")


def worktree_diff(wt: Path) -> str:
    sh(["git", "add", "-N", "."], cwd=wt)
    return sh(["git", "diff"], cwd=wt).stdout


def changed_paths(wt: Path) -> list[str]:
    sh(["git", "add", "-N", "."], cwd=wt)
    return [l[3:] for l in sh(["git", "status", "--short"], cwd=wt).stdout.splitlines() if l.strip()]


def out_of_bounds(paths: list[str]) -> list[str]:
    return [p for p in paths if not any(p.startswith(e) or p == e for e in EDITABLE)]


def rerun_stage(stage: str, date: str) -> int:
    if stage == "compose":
        cmd = [sys.executable, str(ROOT / "scripts" / "compose.py"), "--date", date, "--reuse-plan"]
        timeout = 7200
    else:
        cmd = [sys.executable, str(ROOT / "scripts" / "release.py"), "--date", date]
        timeout = 1800
    log = ROOT / "metrics" / f"oncall-{date}-{stage}-rerun.log"
    with log.open("w", encoding="utf-8") as f:
        r = subprocess.run(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, timeout=timeout,
                           stdin=subprocess.DEVNULL, env={**os.environ, "ONCALL": "off"})
    return r.returncode


def notify_long(job: str, text: str, ok: bool = True, limit: int = 1900) -> None:
    """Discord の 2000 字上限に合わせて分割して送る。"""
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    for i, c in enumerate(chunks):
        notify(job, (f"({i + 1}/{len(chunks)}) " if len(chunks) > 1 else "") + c, ok=ok)


def report_change(stage: str, date: str, fix: dict, transcript: list[dict], wt: Path,
                  commit: str, branch: str, targets: list[str]) -> None:
    """当番が入れた修正の報告。**必須**(編集長の指示: 勝手に変な変更を入れていないかを
    人が見られるように)。何を・なぜ・どう検証したか・監査の判定・差分の要点・戻し方。
    全文は metrics/oncall-<日付>-<工程>-report.md に、要約を Discord に流す。
    """
    stat = sh(["git", "show", "--stat", "--format=", commit], cwd=ROOT).stdout.strip()
    diff = sh(["git", "show", "--format=", "--unified=2", commit], cwd=ROOT).stdout
    reviews = [t.get("review") for t in transcript if t.get("review")]
    integ = [t.get("integrate") for t in transcript if t.get("integrate")]
    last = reviews[-1] if reviews else {}
    verdict = f"{last.get('verdict', '?')}(往復 {len(reviews)}回)"
    if integ:
        verdict += f" / 当番が受け入れた指摘 {sum(len(x.get('accepted') or []) for x in integ)}件・反論 {sum(len(x.get('refuted') or []) for x in integ)}件"
    head = (f"🛠 当番の修正報告: {stage} {date}\n"
            f"commit: {commit[:10]}(main と {', '.join(targets) or 'main'})/ 退避ブランチ: {branch}\n"
            f"診断: {(fix.get('diagnosis') or '')[:500]}\n"
            f"原因: {(fix.get('root_cause') or '')[:400]}\n"
            f"検証: {(fix.get('test_evidence') or '')[:500]}\n"
            f"監査(Sol): {verdict}\n"
            f"リスク: {(fix.get('risk') or '')[:300]} / 確信度 {fix.get('confidence', '?')}\n"
            f"変更:\n{stat[:600]}\n"
            f"戻し方: git revert {commit[:10]}(ops で実行し、生きているブランチにも取り込む)\n")
    full = head + "\n## 差分\n```diff\n" + diff + "\n```\n\n## 往復の記録\n" + json.dumps(transcript, ensure_ascii=False, indent=1)
    (ROOT / "metrics" / f"oncall-{date}-{stage}-report.md").write_text(full, encoding="utf-8")
    # Discord には要点と差分の先頭を送る(全文は metrics に残す)
    excerpt = "\n".join(l for l in diff.splitlines() if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))[:1500]
    notify_long("oncall", head + "差分の要点(全文: metrics/oncall-" + date + "-" + stage + "-report.md):\n```diff\n" + excerpt + "\n```")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["compose", "release"])
    ap.add_argument("--date", required=True)
    ap.add_argument("--reason", default="")
    ap.add_argument("--no-rerun", action="store_true")
    a = ap.parse_args()
    date, stage = a.date, a.stage

    lock = lock_path(date, stage)
    state = json.loads(lock.read_text(encoding="utf-8")) if lock.exists() else {"attempts": 0, "log": []}
    if state["attempts"] >= MAX_ATTEMPTS:
        notify("oncall", f"{date} {stage}: 当番は既に{MAX_ATTEMPTS}回試みた。人の判断が要る:\n"
                         + "\n".join(str(x)[:200] for x in state["log"][-3:]), ok=False)
        return 1
    if sh(["pgrep", "-f", "scripts/(compose|release).py"], cwd=ROOT).returncode == 0:
        notify("oncall", f"{date} {stage}: compose/release が走行中なので当番は待機", ok=False)
        return 1
    state["attempts"] += 1
    lock.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    notify("oncall", f"{date} {stage} が止まった。当番({ONCALL_MODEL})が診断・修正に入る({state['attempts']}回目)")

    context = gather_context(stage, date, a.reason)
    (ROOT / "metrics" / f"oncall-{date}-{stage}-context.txt").write_text(context, encoding="utf-8")

    sh(["git", "fetch", "origin"], cwd=ROOT, timeout=120)
    wt = Path(tempfile.mkdtemp(prefix=f"oncall-{date}-{stage}-"))
    shutil.rmtree(wt)
    sh(["git", "worktree", "prune"], cwd=ROOT)
    r = sh(["git", "worktree", "add", "--detach", str(wt), "origin/main"], cwd=ROOT, timeout=120)
    if r.returncode != 0 or not wt.exists():
        notify("oncall", f"{date} {stage}: 作業ツリーが作れない: {r.stderr[-200:]}", ok=False)
        return 1
    try:
        if (ROOT / ".env").exists():
            shutil.copy(ROOT / ".env", wt / ".env")
        schemas = ROOT / "schema"
        transcript: list[dict] = []
        objections: list[dict] | None = None
        integ: dict | None = None
        approved = False
        fix: dict = {}
        for rnd in range(1, MAX_ROUNDS + 1):
            print(f"当番 {rnd}回目", flush=True)
            if rnd == 1:
                fix = run_claude(fix_prompt(stage, date, context, rnd, None), schemas / "oncall-fix.schema.json", wt)
                transcript.append({"round": rnd, "fix": fix})
                if fix.get("status") == "cannot_fix":
                    break
            else:
                integ = run_claude(fix_prompt(stage, date, context, rnd, objections),
                                   schemas / "oncall-integrate.schema.json", wt)
                transcript.append({"round": rnd, "integrate": integ})
            bad = out_of_bounds(changed_paths(wt))
            if bad:
                transcript.append({"round": rnd, "out_of_bounds": bad})
                fix["status"] = "cannot_fix"
                fix["notes"] = f"触ってはいけないファイルを変えた: {bad}"
                break
            sc = sh([sys.executable, "scripts/selfcheck.py"], cwd=wt, timeout=300)
            if sc.returncode != 0:
                transcript.append({"round": rnd, "selfcheck": sc.stdout[-500:]})
                objections = [{"id": "selfcheck", "claim": "selfcheck が赤", "evidence": sc.stdout[-400:],
                               "severity": "blocks_publish"}]
                continue
            diff = worktree_diff(wt)
            if fix.get("status") == "no_fix_needed" and not diff.strip():
                approved = True
                break
            print(f"監査 {rnd}回目", flush=True)
            rev = run_codex(review_prompt(stage, date, fix, diff, rnd, integ), schemas / "oncall-review.schema.json", wt)
            transcript.append({"round": rnd, "review": rev})
            if rev.get("verdict") == "approve":
                approved = True
                break
            objections = rev.get("must_fix") or []
        (ROOT / "metrics" / f"oncall-{date}-{stage}-transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
        state["log"].append({"at": datetime.datetime.now().isoformat(timespec="minutes"), "approved": approved,
                             "status": fix.get("status"), "diagnosis": (fix.get("diagnosis") or "")[:300]})
        lock.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

        if not approved:
            last = transcript[-1]
            notify("oncall", f"{date} {stage}: 当番と監査が合意できなかった。人の判断が要る。\n"
                             f"診断: {(fix.get('diagnosis') or '')[:300]}\n"
                             f"最後の指摘: {json.dumps(last.get('review', last), ensure_ascii=False)[:600]}", ok=False)
            return 1

        diff = worktree_diff(wt)
        if diff.strip():
            branch = f"repair/{date}-{stage}"
            sh(["git", "add", "-A"], cwd=wt)
            msg = (f"oncall: {stage} {date} の停止を当番が修正(監査 approve)\n\n"
                   f"診断: {fix.get('diagnosis', '')}\n原因: {fix.get('root_cause', '')}\n"
                   f"検証: {fix.get('test_evidence', '')}\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>")
            sh(["git", "commit", "-q", "-m", msg], cwd=wt)
            sh(["git", "push", "-f", "origin", f"HEAD:refs/heads/{branch}"], cwd=wt, timeout=120)
            # main へ入れ、生きている edition ブランチへ取り込む
            cur = sh(["git", "branch", "--show-current"], cwd=ROOT).stdout.strip()
            sh(["git", "checkout", "main"], cwd=ROOT)
            sh(["git", "pull", "--ff-only", "origin", "main"], cwd=ROOT, timeout=120)
            m = sh(["git", "merge", "--no-edit", f"origin/{branch}"], cwd=ROOT)
            if m.returncode != 0:
                sh(["git", "merge", "--abort"], cwd=ROOT)
                sh(["git", "checkout", cur], cwd=ROOT)
                notify("oncall", f"{date} {stage}: 修正を main に入れられない(衝突)。ブランチ {branch} に置いた", ok=False)
                return 1
            sh(["git", "push", "origin", "main"], cwd=ROOT, timeout=120)
            if cur and cur != "main":
                sh(["git", "checkout", cur], cwd=ROOT)
                m2 = sh(["git", "merge", "--no-edit", "origin/main"], cwd=ROOT)
                if m2.returncode != 0:
                    sh(["git", "merge", "--abort"], cwd=ROOT)
                    notify("oncall", f"{date} {stage}: 修正は main に入ったが {cur} への取り込みが衝突。手で解くこと", ok=False)
                    return 1
                sh(["git", "push", "origin", cur], cwd=ROOT, timeout=120)
            # **報告は必須。**何を・なぜ・どう検証したか・監査の判定・差分の要点・戻し方を Discord へ
            commit = sh(["git", "rev-parse", f"origin/{branch}"], cwd=ROOT).stdout.strip()
            report_change(stage, date, fix, transcript, wt, commit, branch,
                          ["main"] + ([cur] if cur and cur != "main" else []))
        else:
            notify("oncall", f"{date} {stage}: コードの欠陥ではないと判断(no_fix_needed): {(fix.get('diagnosis') or '')[:300]}")
        if a.no_rerun:
            return 0
        code = rerun_stage(stage, date)
        notify("oncall", f"{date} {stage}: 再実行が終わった(exit {code})。"
                         f"{'成功' if code == 0 else '失敗。metrics/oncall-' + date + '-' + stage + '-rerun.log を見ること'}",
               ok=(code == 0))
        return code
    finally:
        sh(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT)
        sh(["git", "worktree", "prune"], cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
