#!/usr/bin/env python3
"""oncall: 実装の欠陥で工程が止まったとき、人へ投げる前に**当番が直す**。

  python3 scripts/oncall.py --stage compose|release --date YYYY-MM-DD --reason "…"

パイプラインが「人間判断が必要」で止まるのは、ほぼ毎回コードかプロンプトの欠陥である
(実測: 2026-09-06〜12、7日連続で毎朝止まった。原因は全部こちらの実装)。人が起きて
直すまで号が出ないのは実用ではない。そこで:

1. **当番(Opus)** が、ログと状態から診断し、origin/main から切った使い捨ての作業ツリーで最小の
   修正を書き、selfcheck と再現テストを通し、報告を JSON で返す(schema/oncall-fix.schema.json)
2. 変更はその場で **commit して内容を固定**し、**監査(Sol)** がその commit の差分(差分が無ければ
   診断そのもの)を敵対的にレビューし、判定を JSON で返す
3. 当番が指摘を**敵対的に取り込む**: 受け入れるものは直し、当たらないものは根拠で反論する。
   監査がもう一度見る。往復は最大2回
4. 合意(approve で must_fix が空)したものだけ、**監査した commit のハッシュそのもの**を main へ
   merge し、`edition/<日付>` へ取り込み、止まった工程を再実行する
5. 合意できなければ、両者の言い分を添えて人へ渡す
6. **修正を入れたら Discord に報告する(必須)**: 何を・なぜ・どう検証したか・監査の判定・
   差分の要点・戻し方(編集長の指示: 勝手に変な変更が入っていないかを人が見られるように)

守ること(監査の指摘で固めた):
- 当番の作業ツリーには **.env を置かない**。セッションの環境変数は最小の allowlist だけ渡す。
  触ってよいのは scripts/ prompts/ schema/ と設計文書だけ(判定表・収集対象は含めない)。
  セッションのあと**本体の作業ツリーが汚れていない・origin/main が動いていない**ことを確かめる
  (どちらかが破れていれば取り込まない)
- 当番は一度に1つ(flock)。compose/release/collect が走っているあいだは待つ(起動直後は親の
  compose が生きている)。取り込みの直前と再実行の直前にも確かめる。試行回数は待ち終えてから消費
- 本体の作業ツリーは**開始時に clean でなければ何もしない**。取り込み先は `edition/<日付>` を
  名前で解決する(「今いるブランチ」を信用しない)。`git add` は対象パスだけ
- Git 操作は全部 fail-closed。記録ブランチは一意な名前で force しない。main の push が失敗したら
  edition には触らない。当番の状態・記録(metrics/oncall-*)は Git 管理外
- 再実行は直した箇所を踏ませる。計画・執筆・組版の層を直したなら**その号を作り直す**:
  作り直す前に edition ブランチの控え(backup/…)を push し、stock からこの号の寄与を剥がし
  (assemble.rollback。組版前の控えは消さない)、成果物を外して compose を最初から。失敗したら
  控えへ戻す。それ以外は古い校閲記録を消して `--reuse-plan`
- 同じ日・同じ工程で2回までしか試みない(ループ防止)。予算と時間の上限
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
from pipelib import ENV, ROOT, JobLockTimeout, job_lock, notify

ONCALL_MODEL = ENV.get("ONCALL_MODEL", "opus")
AUDIT_MODEL = ENV.get("AUDIT_MODEL", "gpt-5.6-sol")
ONCALL_MAX_BUDGET_USD = ENV.get("ONCALL_MAX_BUDGET_USD", "15")
MAX_ATTEMPTS = 2
MAX_ROUNDS = 2
WAIT_IDLE_MIN = 90
DIFF_LIMIT = 60000
EDITABLE = ("scripts/", "prompts/", "schema/", "REQUIREMENTS.md", "PIPELINE.md", "AUDIT.md", "README.md")
# これを直したら、その号は作り直す(--reuse-plan では直した箇所を踏まない。監査指摘)
FULL_RERUN_IF = ("scripts/compose.py", "scripts/planlib.py", "scripts/renderlib.py", "scripts/assemble.py",
                 "prompts/", "schema/article-out", "schema/assemble")
# 当番・監査のセッションに渡す環境変数(資格情報は渡さない。監査指摘)
ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "USER", "LOGNAME", "TZ", "TMPDIR")
CO_AUTHOR = "\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"


def sh(args, cwd, timeout=600) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def must(r: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    """Git 操作は fail-closed。失敗を無視して先へ進むと、監査していない差分が main に入る(監査指摘)。"""
    if r.returncode != 0:
        raise RuntimeError(f"{what} に失敗: {(r.stderr or r.stdout)[-300:]}")
    return r


def undo_merge(before: str, what: str) -> None:
    """merge を無かったことにする。abort は補助で、**必ず** merge 前のハッシュへ reset する(監査指摘)。"""
    sh(["git", "merge", "--abort"], cwd=ROOT)
    must(sh(["git", "reset", "-q", "--hard", before], cwd=ROOT), f"{what} の巻き戻し")


def session_env() -> dict:
    return {k: v for k, v in os.environ.items() if k in ENV_ALLOW}


def save_state(path: Path, state: dict) -> None:
    """試行状態の原子的な保存(一時ファイル → fsync → os.replace)。途中で死んでも壊れた JSON を残さない(監査指摘)。"""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(state, ensure_ascii=False, indent=1))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def attempt_count(date: str, stage: str) -> int:
    """試行回数は state の JSON ではなく、試行ごとに O_EXCL で作る印ファイルの個数で数える
    (JSON が壊れても・書き換えられても、上限が緩まない単調な証跡。監査指摘)。"""
    return len(list((ROOT / "metrics").glob(f"oncall-{date}-{stage}-attempt-*")))


def consume_attempt(date: str, stage: str) -> int:
    """次の試行の印を作る(既にあれば失敗=競合)。戻り値は消費後の回数。"""
    n = attempt_count(date, stage) + 1
    fd = os.open(ROOT / "metrics" / f"oncall-{date}-{stage}-attempt-{n}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.write(fd, datetime.datetime.now().isoformat(timespec="seconds").encode())
    os.close(fd)
    return n


def load_state(path: Path) -> dict | None:
    """試行の記録(log)を読む。壊れていれば一意な名前で退避して通知し、新規として扱う。
    退避できなければ None(証拠を失ったまま「退避した」と言わない。監査指摘)。
    試行回数はここでは扱わない(attempt_count)。"""
    if not path.exists():
        return {"log": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("log"), list):
            raise ValueError("形が違う")
        return state
    except Exception as e:
        broken = path.with_name(f"{path.name}.corrupt-{time.time_ns()}-{os.getpid()}")
        try:
            os.link(path, broken)       # 既存を上書きしない(同名があれば失敗する)
            os.unlink(path)
        except OSError as e2:
            notify("oncall", f"当番の試行記録 {path.name} が壊れていて({type(e).__name__})、退避にも失敗({e2})。当番は起動しない", ok=False)
            return None
        notify("oncall", f"当番の試行記録 {path.name} が壊れていた({type(e).__name__})。{broken.name} に退避して新規として扱う", ok=False)
        return {"log": [{"corrupted": broken.name}]}


def env_fingerprint() -> str:
    """本体の .env(Git 管理外)の指紋。当番が触っていないことを事後に検める(監査指摘)。"""
    import hashlib
    p = ROOT / ".env"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def root_clean() -> bool:
    """判定不能(git status の失敗)は clean 扱いにしない(監査指摘)。"""
    return not must(sh(["git", "status", "--porcelain"], cwd=ROOT), "git status").stdout.strip()


def remote_main() -> str:
    must(sh(["git", "fetch", "-q", "origin", "main"], cwd=ROOT, timeout=120), "fetch")
    return sh(["git", "rev-parse", "origin/main"], cwd=ROOT).stdout.strip()


def gather_context(stage: str, date: str, reason: str) -> str:
    parts = [f"# 止まった工程: {stage} / 号: {date}\n\n## 呼び出し側の理由\n{reason}\n"]
    r = sh(["journalctl", "--user", "-u", f"imas-{stage}", "--since", f"{date} 00:00", "--no-pager", "-n", "250"],
           cwd=ROOT, timeout=60)
    if r.returncode == 0 and r.stdout.strip():
        lines = [re.sub(r"^.*?python3\[\d+\]: ", "", l) for l in r.stdout.splitlines()]
        parts.append("## journal(直近250行)\n" + "\n".join(lines[-250:]))
    parts.append("## ops の直近 commit\n" + sh(["git", "log", "--oneline", "-8"], cwd=ROOT).stdout)
    return "\n\n".join(parts)


def fix_prompt(stage: str, date: str, context: str, objections: list[dict] | None) -> str:
    obj = ""
    if objections:
        obj = ("\n\n## 監査からの指摘(敵対的に取り込む)\n"
               "受け入れるものは直し、当たらないものは**根拠を示して反論する**。馴れ合わない。\n"
               + json.dumps(objections, ensure_ascii=False, indent=1))
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の**当番エンジニア**です。自動発行の工程 `{stage}` が
{date}号で止まりました。人が起きるまで待たず、あなたが直します。この作業ツリーは origin/main から切った
使い捨てで、直したものは監査の敵対的レビューを通ってから main に入ります。

## 何が起きたか
{context}

## やること
1. 診断する。ログと状態から**事実**を押さえる(推測は推測と書く)。再現できるなら再現する
2. 原因が**コードかプロンプトの欠陥**なら、最小の修正を書く。触ってよいのは
   scripts/ prompts/ schema/ と設計文書(REQUIREMENTS.md, PIPELINE.md, AUDIT.md, README.md)だけ。
   **docs/ stock/ candidates/ metrics/ source_types.yml sources.yml .env は触らない。**
   **この作業ツリーの外(他のディレクトリ、git の設定、リモート)にも触らない。commit も push もしない**
   (commit はコードが行い、その差分がそのまま監査に掛かる)
3. `python3 scripts/selfcheck.py` を通し、直した箇所を**実際に踏ませるテスト**を書いて走らせる
   (壊れた入力を与えて、直す前は落ち、直した後は通ることを見せる)
4. 原因がデータや環境で、コードを直しても意味が無いなら status=no_fix_needed(診断に根拠を書き、
   recovery に「同じ入力でもう一度走らせて今度は通る根拠」を書く。同じ入力では同じ結果になるなら
   rerun_mode=none にして人へ渡す)。直せない・判断が要るなら status=cannot_fix(理由を書く)
5. rerun_mode を答える: 計画・執筆・組版の層(compose.py / planlib / renderlib / assemble / prompts /
   出力 schema)を直したなら rebuild(号を作り直す)。それ以外で続きから走れるなら resume。
   再実行しても意味が無いなら none

## 設計の原則(これに反する直し方をしない)
- 判断は小さな構造化セッション、写す・数える・整える・反映するはコード
- 「lint に叩かれて直す」「同じプロンプトでやり直す」「別セッションに修理させる」を新しく作らない
- 成果物は入力の純関数(冪等)。巻き戻しや後始末セッションを増やさない
- 通知して続行、で済ませない。落ちたものはその場で潰す

最後に、報告を JSON で返してください(schema で形が決まっています)。{obj}
"""


def review_prompt(stage: str, date: str, fix_report: dict, diff: str, integ: dict | None) -> str:
    extra = ""
    if integ:
        extra = ("\n\n## 前回の指摘に対する当番の対応(受け入れ/反論)\n"
                 + json.dumps(integ, ensure_ascii=False, indent=1)
                 + "\n反論に根拠があれば認め、無ければ却下してください。")
    body = (f"## 差分(監査対象の commit そのもの。`git log -1 -p` でも確認できます)\n```diff\n{diff}\n```"
            if diff.strip() else "## 差分\n(変更なし。当番は「コードの欠陥ではない」と判断しました。**その診断と recovery が正しいか**を疑ってください)")
    return f"""あなたは日刊AI新聞「アイマスNEWS(α)」の監査役です。自動発行の工程 `{stage}` が {date}号で止まり、
当番(別モデル)が対応しました。**敵対的に**レビューしてください。馴れ合いは不要です。
「本当にそれが原因か」「その直し方で明日また止まらないか」「新しく壊れるものは何か」
「rerun_mode は妥当か(直した層を再実行が踏むか)」を掘ってください。

## 当番の報告
{json.dumps(fix_report, ensure_ascii=False, indent=1)}

{body}

## 判定
- approve: これで工程が動き、データを壊さず、設計の原則(判断は小さな構造化セッション・
  事務処理はコード・冪等・通知して続行で済ませない)に反しない。**approve のとき must_fix は空**
  (残したい指摘があるなら reject にする)
- reject: must_fix に、根拠付きで問題を列挙する(推測は (推測) と明記)。severity は
  blocks_publish / corrupts_data / quality / style から
{extra}
判定を JSON で返してください(schema で形が決まっています)。
"""


def parse_json(text: str) -> dict | None:
    try:
        return json.loads(text.strip())
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def run_claude(prompt: str, schema_file: Path, cwd: Path, timeout: int = 1800) -> dict:
    r = subprocess.run(["claude", "-p", prompt, "--model", ONCALL_MODEL, "--dangerously-skip-permissions",
                        "--json-schema", schema_file.read_text(encoding="utf-8"),
                        "--max-budget-usd", ONCALL_MAX_BUDGET_USD],
                       cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                       env=session_env())
    ans = parse_json(r.stdout or "")
    if ans is None:
        raise RuntimeError(f"当番セッションの出力が読めない(exit {r.returncode}): {(r.stderr or r.stdout or '')[-300:]}")
    return ans


def run_codex(prompt: str, schema_file: Path, cwd: Path, timeout: int = 1800) -> dict:
    fd, out_name = tempfile.mkstemp(prefix="oncall-review-", suffix=".json")
    os.close(fd)
    out_path = Path(out_name)
    try:
        subprocess.run(["codex", "exec", "-m", AUDIT_MODEL, "-s", "read-only", "--skip-git-repo-check",
                        "--output-schema", str(schema_file), "-o", str(out_path), prompt],
                       cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                       env=session_env())
        text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    finally:
        out_path.unlink(missing_ok=True)
    ans = parse_json(text)
    if ans is None:
        raise RuntimeError(f"監査セッションの出力が読めない: {text[-300:]}")
    return ans


def freeze(wt: Path, base: str, rnd: int) -> tuple[str, str, list[str]]:
    """作業ツリーの変更を commit して内容を固定し、(HEAD, base からの差分, 変えたパス) を返す。
    監査した差分と取り込む差分を**同じ commit** にする(監査指摘)。"""
    must(sh(["git", "add", "-A"], cwd=wt), "add")
    if sh(["git", "status", "--porcelain"], cwd=wt).stdout.strip():
        must(sh(["git", "commit", "-q", "-m", f"oncall: 当番の修正({rnd}回目)" + CO_AUTHOR], cwd=wt), "commit")
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt).stdout.strip()
    diff = sh(["git", "diff", f"{base}..{head}"], cwd=wt).stdout
    changed = [l for l in sh(["git", "diff", "--name-only", f"{base}..{head}"], cwd=wt).stdout.splitlines() if l.strip()]
    return head, diff, changed


def out_of_bounds(paths: list[str]) -> list[str]:
    return [p for p in paths if not any(p.startswith(e) or p == e for e in EDITABLE)]


def split_chunks(text: str, limit: int) -> list[str]:
    """Discord の 2000 字上限に合わせて分割する。行単位で詰め、1行が上限を超えるなら文字で割る
    (つないだ結果が元の文字列と一致する。監査指摘)。"""
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    return chunks


def notify_long(job: str, text: str, ok: bool = True, limit: int = 1800) -> bool:
    """分割して**全部**送る。戻り値は全部届いたか(1つでも落ちれば False)。"""
    chunks = split_chunks(text, limit)
    delivered = True
    for i, c in enumerate(chunks):
        delivered &= notify(job, (f"({i + 1}/{len(chunks)}) " if len(chunks) > 1 else "") + c, ok=ok, require=True)
    return delivered


def report_change(stage: str, date: str, fix: dict, transcript: list[dict], base: str, head: str, merge_commit: str,
                  branch: str, targets: list[str], rerun_mode: str) -> bool:
    """当番が入れた修正の報告。**必須**。全文は metrics/oncall-<日付>-<工程>-report.md、要約を Discord へ。
    戻り値は Discord に届いたか(届かなければ当番は成功終了しない。監査指摘)。"""
    stat = sh(["git", "diff", "--stat", f"{base}..{head}"], cwd=ROOT).stdout.strip()
    diff = sh(["git", "diff", "--unified=2", f"{base}..{head}"], cwd=ROOT).stdout
    reviews = [t.get("review") for t in transcript if t.get("review")]
    integ = [t.get("integrate") for t in transcript if t.get("integrate")]
    last = reviews[-1] if reviews else {}
    verdict = f"{last.get('verdict', '?')}(往復 {len(reviews)}回)"
    if integ:
        verdict += (f" / 当番が受け入れた指摘 {sum(len(x.get('accepted') or []) for x in integ)}件"
                    f"・反論 {sum(len(x.get('refuted') or []) for x in integ)}件")
    mc = merge_commit[:10] if merge_commit else "(取り込み後に追送)"
    # 人に送る本文と、記録に残す本文は**同じもの**(切り詰めた要約を送らない。監査指摘)。
    # 往復の記録(JSON)だけはファイルに添える
    report = (f"🛠 当番の修正報告: {stage} {date}\n"
              f"修正 commit: {head[:10]} / main の merge commit: {mc}"
              f"(取り込み先: {', '.join(targets)})/ 記録ブランチ: {branch}\n"
              f"診断: {fix.get('diagnosis') or ''}\n"
              f"原因: {fix.get('root_cause') or ''}\n"
              f"検証: {fix.get('test_evidence') or ''}\n"
              f"監査(Sol): {verdict}\n"
              f"リスク: {fix.get('risk') or ''} / 確信度 {fix.get('confidence', '?')}\n"
              f"変更:\n{stat}\n"
              f"再実行: {rerun_mode}\n"
              f"戻し方: ops の main で `git revert -m 1 {mc}` → push → edition/{date} に main を merge → push\n"
              f"\n## 差分\n{diff}\n")
    (ROOT / "metrics" / f"oncall-{date}-{stage}-report.md").write_text(
        report + "\n## 往復の記録\n" + json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
    return notify_long("oncall", report)


def needs_full_rerun(changed: list[str]) -> bool:
    return any(p.startswith(pre) for p in changed for pre in FULL_RERUN_IF)


def apply_integrate(fix: dict, integ: dict) -> dict:
    """2巡目以降の当番の答え(監査の指摘の取り込み)を最終報告に反映する(監査指摘)。
    status(no_fix_needed → fixed、fixed → no_fix_needed の収束)と rerun_mode は 'unchanged' 以外なら更新。"""
    if integ.get("status") in ("fixed", "no_fix_needed", "cannot_fix"):
        fix["status"] = integ["status"]
    if integ.get("rerun_mode") in ("rebuild", "resume", "none"):
        fix["rerun_mode"] = integ["rerun_mode"]
    for k in ("diagnosis", "root_cause", "recovery"):
        if str(integ.get(k) or "").strip():
            fix[k] = integ[k]
    # 2巡目の変更・検証・リスクも最終報告に**累積**する(初稿の分を消さない。監査指摘)
    if integ.get("changed_files"):
        fix["changed_files"] = list(dict.fromkeys(list(fix.get("changed_files") or []) + list(integ["changed_files"])))
    if str(integ.get("test_evidence") or "").strip():
        fix["test_evidence"] = (str(fix.get("test_evidence") or "") + "\n[監査後の再検証] " + integ["test_evidence"]).strip()
    new_lines = [l.strip() for l in str(integ.get("risk") or "").splitlines() if l.strip()]
    if new_lines:
        # 項目(行)単位の完全一致で重複を除く。新しい側も行に割る(部分一致だと別の risk を捨て、
        # 複数行を塊で比べると同じ項目を二重に積む。監査指摘)
        items = [l.strip() for l in str(fix.get("risk") or "").splitlines() if l.strip()]
        for line in new_lines:
            if line not in items and "[監査後の変更で] " + line not in items:
                items.append("[監査後の変更で] " + line)
        fix["risk"] = "\n".join(items)
    return fix


def rerun_policy(stage: str, changed: list[str], rerun_mode: str) -> tuple[bool, str]:
    """再実行の仕方を決める(テストで固定するため関数にする。監査指摘)。戻りは (作り直すか, 表示名)。

    生成層(FULL_RERUN_IF)を直したか、当番が rebuild と答えたら、**stage を問わず**号を作り直す
    (release 起点でも生成済みの号をそのまま発行しない)。none なら再実行しない
    """
    if rerun_mode == "none":
        return False, "none"
    full = needs_full_rerun(changed) or rerun_mode == "rebuild"
    return full, ("作り直し(compose 全工程" + (" → release" if stage == "release" else "") + ")") if full \
        else ("続き(--reuse-plan)" if stage == "compose" else "続き(release)")


def commit_paths(paths: list[str], msg: str, branch: str) -> None:
    """対象パスだけを stage して commit・push(包括的な add -A は使わない。監査指摘)。"""
    must(sh(["git", "add", "-A", "--"] + paths, cwd=ROOT), "add")
    if must(sh(["git", "status", "--porcelain"], cwd=ROOT), "git status").stdout.strip():
        must(sh(["git", "commit", "-q", "-m", msg + CO_AUTHOR], cwd=ROOT), "commit")
        must(sh(["git", "push", "-q", "origin", branch], cwd=ROOT, timeout=120), "push")


def rollback_in_subprocess(date: str) -> list[str]:
    """stock からこの号の寄与を剥がす(assemble.rollback)。**取り込んだあとのコード**を別プロセスで
    走らせる。当番が assemble 自体を直した場合、この process に読み込み済みの古い実装で
    stock を触ってはいけない(監査指摘)。"""
    code = ("import sys; sys.path.insert(0, 'scripts'); import assemble; "
            "print('\\n'.join(assemble.rollback(sys.argv[1])))")
    r = must(sh([sys.executable, "-c", code, date], cwd=ROOT, timeout=600), "assemble.rollback")
    return [l for l in r.stdout.splitlines() if l.strip()]


def edition_artifacts(date: str) -> list[str]:
    """その号の成果物のパス(glob)。作り直しで外す・復元で掃除する対象。社説(EDITORIAL_UNTIL 以前の号)も含む(監査指摘)。"""
    return [f"docs/_posts/{date}-*.md", f"docs/_editions/{date}.md", f"docs/_editorials/{date}.md",
            f"metrics/plan-{date}*.json", f"metrics/review-{date}-*.json",
            f"metrics/stories-before-{date}.yml", f"metrics/pending-before-{date}.yml"]


def restore_edition(date: str, edition: str, backup: str) -> None:
    sh(["git", "merge", "--abort"], cwd=ROOT)
    must(sh(["git", "checkout", "-q", "-f", edition], cwd=ROOT), f"{edition} の checkout")
    must(sh(["git", "reset", "-q", "--hard", backup], cwd=ROOT), "控えへの reset")
    # reset は未追跡の生成物(作り直し中に compose が作った記事・号・計画・校閲記録)を消さない。
    # 残ると次の工程が clean 判定で拒否される。**この号のものだけ**を対象に消す(広い git clean は使わない。監査指摘)
    must(sh(["git", "clean", "-q", "-f", "--"] + edition_artifacts(date), cwd=ROOT), "生成物の掃除")
    # 判定不能(status の失敗)は clean 扱いにしない(監査指摘)
    if must(sh(["git", "status", "--porcelain"], cwd=ROOT), "git status").stdout.strip():
        raise RuntimeError(f"控えへ戻したが作業ツリーが clean でない: {sh(['git', 'status', '--short'], cwd=ROOT).stdout[:300]}")
    must(sh(["git", "push", "-q", "--force-with-lease", "origin", edition], cwd=ROOT, timeout=120), "控えへ戻す push")


def reset_edition(date: str, edition: str) -> str:
    """号を作り直せる状態にする。戻せるように控えのブランチを push してから、stock の寄与を剥がし、
    成果物を外す。**控えを作ったあとはどこで失敗しても控えへ戻す**(監査指摘)。戻り値は控えのブランチ名。"""
    backup = f"backup/{date}-{int(time.time())}"
    must(sh(["git", "branch", backup, edition], cwd=ROOT), "控えブランチの作成")
    must(sh(["git", "push", "-q", "origin", backup], cwd=ROOT, timeout=120), "控えブランチの push")
    try:
        for line in rollback_in_subprocess(date):
            print(f"  {line}", flush=True)
        # 組版前の控え(metrics/*-before-<日付>.yml)は**残す**(消すと組版後の状態を控え直す。監査指摘)
        for p in list((ROOT / "docs" / "_posts").glob(f"{date}-*.md")) \
                + [ROOT / "docs" / "_editions" / f"{date}.md", ROOT / "docs" / "_editorials" / f"{date}.md"] \
                + list((ROOT / "metrics").glob(f"plan-{date}*.json")) + list((ROOT / "metrics").glob(f"review-{date}-*.json")):
            if p.exists():
                p.unlink()
        commit_paths(["docs/_posts", "docs/_editions", "docs/_editorials", "metrics", "stock"],
                     f"oncall: {date} 号を作り直すため成果物と stock の寄与を外す(控え: {backup})", edition)
    except Exception as e:
        try:
            restore_edition(date, edition, backup)
            notify("oncall", f"{date}: 作り直しの準備に失敗({str(e)[:200]})。{edition} を控え {backup} に戻した", ok=False)
        except Exception as e2:
            notify("oncall", f"{date}: 作り直しの準備に失敗し、控え {backup} への復元も失敗: {e2}。**手で戻すこと**", ok=False)
        raise
    return backup


def run_stage(cmd: list[str], log: Path, timeout: int) -> int:
    with log.open("a", encoding="utf-8") as f:
        try:
            r = subprocess.run(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, timeout=timeout,
                               stdin=subprocess.DEVNULL,
                               env={**os.environ, "ONCALL": "off", "IMAS_JOB_LOCK": "held"})
            return r.returncode
        except subprocess.TimeoutExpired:
            return 124


def rerun_stage(stage: str, date: str, edition: str, full: bool) -> int:
    """止まった工程を再実行する。作り直し(full)なら、release 起点でも compose を先頭から走らせてから
    release する(生成層を直したのに生成済みの号をそのまま発行しない。監査指摘)。"""
    log = ROOT / "metrics" / f"oncall-{date}-{stage}-rerun.log"
    log.write_text("", encoding="utf-8")
    compose_py = str(ROOT / "scripts" / "compose.py")
    backup = ""
    code = 1
    done = False   # compose と(release 起点なら)release が**全部**終わったときだけ True(監査指摘)
    try:
        if full:
            backup = reset_edition(date, edition)
            code = run_stage([sys.executable, compose_py, "--date", date], log, 7200)
        elif stage == "compose":
            for p in (ROOT / "metrics").glob(f"review-{date}-*.json"):
                p.unlink()   # 古い校閲記録が残ると release が最大巡数の古い判定を読む(監査指摘)
            commit_paths(["metrics"], f"oncall: {date} の古い校閲記録を外す", edition)
            code = run_stage([sys.executable, compose_py, "--date", date, "--reuse-plan"], log, 7200)
        else:
            code = 0
        if code == 0 and stage == "release":
            code = 1
            code = run_stage([sys.executable, str(ROOT / "scripts" / "release.py"), "--date", date], log, 1800)
        done = code == 0
    finally:
        # 控えを作ったあとは、失敗の種類を問わず(exit 非0・例外。release 起動時の例外も)控えへ戻す(監査指摘)
        if not done and backup:
            try:
                restore_edition(date, edition, backup)
                notify("oncall", f"{date} {stage}: 作り直しが失敗(exit {code})したので {edition} を控え {backup} に戻した", ok=False)
            except Exception as e:
                notify("oncall", f"{date} {stage}: 作り直しが失敗し、控え {backup} への復元も失敗: {e}。**手で戻すこと**", ok=False)
    return code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["compose", "release"])
    ap.add_argument("--date", required=True)
    ap.add_argument("--reason", default="")
    ap.add_argument("--no-rerun", action="store_true")
    a = ap.parse_args()
    date, stage = a.date, a.stage
    edition = f"edition/{date}"

    # 工程の排他(collect/compose/release/当番で共通の flock)。起動直後は親の compose がまだ持って
    # いるので、ここで終わるまで待つ。以後、再実行が終わるまで持ち続ける(pgrep の隙間を作らない。監査指摘)
    try:
        lock_fd = job_lock("oncall", wait_min=WAIT_IDLE_MIN)
    except JobLockTimeout as e:
        notify("oncall", f"{date} {stage}: {e}。当番は諦める", ok=False)
        return 1
    # 試行回数はロックの中で数える・判定する・増やす(同時起動で上限をすり抜けない。監査指摘)。
    # 回数は印ファイルの個数(JSON の壊れ・書き換えで緩まない)、記録(log)は JSON
    state_p = ROOT / "metrics" / f"oncall-{date}-{stage}.json"
    state = load_state(state_p)
    if state is None:
        os.close(lock_fd)
        return 1
    if attempt_count(date, stage) >= MAX_ATTEMPTS:
        notify("oncall", f"{date} {stage}: 当番は既に{MAX_ATTEMPTS}回試みた。人の判断が要る:\n"
                         + "\n".join(str(x)[:200] for x in state["log"][-3:]), ok=False)
        os.close(lock_fd)
        return 1
    # 前提を全部確かめてから試行を消費する(dirty で何もしなかった回で回数を減らさない。監査指摘)
    try:
        if not root_clean():
            notify("oncall", f"{date} {stage}: 本体の作業ツリーが clean でない。当番は何もしない:\n"
                             + sh(["git", "status", "--short"], cwd=ROOT).stdout[:500], ok=False)
            return 1
        if sh(["git", "rev-parse", "--verify", "-q", edition], cwd=ROOT).returncode != 0:
            notify("oncall", f"{date} {stage}: {edition} が無い。当番は何もしない", ok=False)
            return 1
        must(sh(["git", "fetch", "-q", "origin", edition], cwd=ROOT, timeout=120), f"{edition} の fetch")
        if sh(["git", "rev-parse", edition], cwd=ROOT).stdout.strip() != sh(["git", "rev-parse", f"origin/{edition}"], cwd=ROOT).stdout.strip():
            notify("oncall", f"{date} {stage}: {edition} がリモートと食い違う。当番は何もしない", ok=False)
            return 1
    except RuntimeError as e:
        notify("oncall", f"{date} {stage}: 前提の確認に失敗: {e}", ok=False)
        return 1
    try:
        attempt_no = consume_attempt(date, stage)
    except FileExistsError:
        notify("oncall", f"{date} {stage}: 試行の印が競合した(同時起動)。当番は起動しない", ok=False)
        return 1
    save_state(state_p, state)
    notify("oncall", f"{date} {stage} が止まった。当番({ONCALL_MODEL})が診断・修正に入る({attempt_no}回目)")

    context = gather_context(stage, date, a.reason)
    (ROOT / "metrics" / f"oncall-{date}-{stage}-context.txt").write_text(context, encoding="utf-8")
    base = remote_main()
    env_before = env_fingerprint()
    wt = Path(tempfile.mkdtemp(prefix=f"oncall-{date}-{stage}-"))
    shutil.rmtree(wt)
    sh(["git", "worktree", "prune"], cwd=ROOT)
    must(sh(["git", "worktree", "add", "--detach", str(wt), base], cwd=ROOT, timeout=120), "worktree の作成")
    # .env は置かない(資格情報を当番に渡さない。監査指摘)
    try:
        schemas = ROOT / "schema"
        transcript: list[dict] = []
        objections: list[dict] | None = None
        integ: dict | None = None
        approved = False
        fix: dict = {}
        head, diff, changed = base, "", []
        for rnd in range(1, MAX_ROUNDS + 1):
            print(f"当番 {rnd}回目", flush=True)
            if rnd == 1:
                fix = run_claude(fix_prompt(stage, date, context, None), schemas / "oncall-fix.schema.json", wt)
                transcript.append({"round": rnd, "fix": fix})
            else:
                integ = run_claude(fix_prompt(stage, date, context, objections),
                                   schemas / "oncall-integrate.schema.json", wt)
                transcript.append({"round": rnd, "integrate": integ})
                # 監査の指摘で status(誤診の訂正)や再実行の仕方を改めたなら、それを最終判断にする(監査指摘)
                apply_integrate(fix, integ)
            # 作業ツリーの外への副作用は、答えが何であれ**毎回**検める(cannot_fix でも省かない。監査指摘):
            # 本体が汚れた / origin/main が動いた / 本体の .env(Git 管理外)が変わった
            if not root_clean():
                transcript.append({"round": rnd, "root_touched": sh(["git", "status", "--short"], cwd=ROOT).stdout[:500]})
                fix.update(status="cannot_fix", notes="当番が本体の作業ツリーに触った。取り込まない")
                break
            if remote_main() != base:
                transcript.append({"round": rnd, "remote_moved": True})
                fix.update(status="cannot_fix", notes="セッション中に origin/main が動いた(当番が push した疑い、または他の工程)。取り込まない")
                break
            if env_fingerprint() != env_before:
                transcript.append({"round": rnd, "env_touched": True})
                fix.update(status="cannot_fix", notes="当番が本体の .env に触った。取り込まない")
                break
            if fix.get("status") == "cannot_fix":
                break
            head, diff, changed = freeze(wt, base, rnd)
            bad = out_of_bounds(changed)
            if bad:
                transcript.append({"round": rnd, "out_of_bounds": bad})
                fix.update(status="cannot_fix", notes=f"触ってはいけないファイルを変えた: {bad}")
                break
            if len(diff) > DIFF_LIMIT:
                transcript.append({"round": rnd, "diff_too_large": len(diff)})
                fix.update(status="cannot_fix", notes=f"差分が大きすぎて監査できない({len(diff)} 字)")
                break
            # 報告と差分の整合(監査指摘): 直したと言うのに差分が無い / 欠陥ではないと言うのに差分がある
            if (fix.get("status") == "fixed") != bool(diff.strip()):
                transcript.append({"round": rnd, "status_mismatch": {"status": fix.get("status"), "diff": bool(diff.strip())}})
                fix.update(status="cannot_fix", notes="報告の status と差分が食い違う")
                break
            sc = sh([sys.executable, "scripts/selfcheck.py"], cwd=wt, timeout=300)
            if sc.returncode != 0:
                transcript.append({"round": rnd, "selfcheck": sc.stdout[-500:]})
                objections = [{"id": "selfcheck", "claim": "selfcheck が赤", "evidence": sc.stdout[-400:],
                               "severity": "blocks_publish"}]
                continue
            print(f"監査 {rnd}回目", flush=True)
            # 差分が無くても(no_fix_needed)監査は通す: 誤診を検める機会を残す(監査指摘)
            rev = run_codex(review_prompt(stage, date, fix, diff, integ), schemas / "oncall-review.schema.json", wt)
            transcript.append({"round": rnd, "review": rev})
            if rev.get("verdict") == "approve" and not (rev.get("must_fix") or []):
                approved = True
                break
            objections = rev.get("must_fix") or []
        (ROOT / "metrics" / f"oncall-{date}-{stage}-transcript.json").write_text(
            json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
        state["log"].append({"at": datetime.datetime.now().isoformat(timespec="minutes"), "approved": approved,
                             "status": fix.get("status"), "diagnosis": (fix.get("diagnosis") or "")[:300]})
        save_state(state_p, state)

        if not approved:
            last = transcript[-1]
            notify("oncall", f"{date} {stage}: 当番と監査が合意できなかった。人の判断が要る。\n"
                             f"診断: {(fix.get('diagnosis') or '')[:300]}\n"
                             f"最後の記録: {json.dumps(last.get('review', last), ensure_ascii=False)[:600]}", ok=False)
            return 1

        # release 起点でも、生成層を直したなら号を作り直す(生成済みの号をそのまま発行しない。監査指摘)
        full, rerun_mode = rerun_policy(stage, changed, str(fix.get("rerun_mode") or ""))
        targets: list[str] = []
        branch = ""
        reported = True
        if diff.strip():
            branch = f"repair/{date}-{stage}-{attempt_no}-{int(time.time())}"
            # 記録用ブランチ(force しない・一意な名前)。main に入れるのは**監査した head そのもの**
            must(sh(["git", "push", "-q", "origin", f"{head}:refs/heads/{branch}"], cwd=wt, timeout=120), "記録ブランチの push")
            # **取り込む前に**修正報告の全文(診断・原因・検証・監査判定・差分の要点)を人に届ける。
            # 届かなければ取り込まない。merge commit のハッシュだけは取り込み後に追送する(報告は必須。監査指摘)
            if not report_change(stage, date, fix, transcript, base, head, "", branch, ["main", edition], rerun_mode):
                notify("oncall", f"{date} {stage}: 修正報告が Discord に届かないので取り込まない(記録ブランチ {branch})", ok=False)
                return 1
            if not root_clean():
                notify("oncall", f"{date} {stage}: 取り込み前に本体の作業ツリーが汚れた。取り込まない(記録ブランチ {branch})", ok=False)
                return 1
            must(sh(["git", "checkout", "-q", "main"], cwd=ROOT), "main の checkout")
            must(sh(["git", "pull", "-q", "--ff-only", "origin", "main"], cwd=ROOT, timeout=120), "main の pull")
            # ローカル main に監査していない commit が乗っていたら、一緒に push してしまう(監査指摘)。
            # origin/main と完全一致のときだけ取り込む。push に失敗したら merge を巻き戻す
            main_before = sh(["git", "rev-parse", "main"], cwd=ROOT).stdout.strip()
            # 監査の基準(base)から origin/main が動いていないこと。動いていれば監査していない変更との
            # 組み合わせになるので取り込まない(監査指摘)
            if main_before != remote_main() or main_before != base:
                sh(["git", "checkout", "-q", edition], cwd=ROOT)
                notify("oncall", f"{date} {stage}: main が監査の基準から動いた(ローカル {main_before[:10]} / 基準 {base[:10]})。取り込まない(記録ブランチ {branch})", ok=False)
                return 1
            # --no-ff で必ず merge commit を作る。戻すときは `git revert -m 1 <merge commit>` 1つで済み、
            # 報告にそのハッシュを書ける(fast-forward だと -m 1 が使えない。監査指摘)
            m = sh(["git", "merge", "--no-ff", "--no-edit", head], cwd=ROOT)
            if m.returncode != 0:
                undo_merge(main_before, "main")
                must(sh(["git", "checkout", "-q", edition], cwd=ROOT), f"{edition} の checkout")
                notify("oncall", f"{date} {stage}: 修正を main に入れられない(衝突)。記録ブランチ {branch} に置いた", ok=False)
                return 1
            merge_commit = sh(["git", "rev-parse", "main"], cwd=ROOT).stdout.strip()
            # リモートの main が基準のままのときだけ push が通る(競合検出。監査指摘)
            pushed = sh(["git", "push", "-q", f"--force-with-lease=refs/heads/main:{base}", "origin", "main"], cwd=ROOT, timeout=120)
            if pushed.returncode != 0:
                undo_merge(main_before, "main")
                must(sh(["git", "checkout", "-q", edition], cwd=ROOT), f"{edition} の checkout")
                notify("oncall", f"{date} {stage}: main の push に失敗したので merge を巻き戻した: {(pushed.stderr or '')[-200:]}", ok=False)
                return 1
            targets.append("main")
            must(sh(["git", "checkout", "-q", edition], cwd=ROOT), f"{edition} の checkout")
            ed_before = sh(["git", "rev-parse", edition], cwd=ROOT).stdout.strip()
            m2 = sh(["git", "merge", "--no-edit", "main"], cwd=ROOT)
            if m2.returncode != 0:
                undo_merge(ed_before, edition)
                report_change(stage, date, fix, transcript, base, head, merge_commit, branch, targets, "取り込み衝突のため未実行")
                notify("oncall", f"{date} {stage}: 修正は main に入ったが {edition} への取り込みが衝突。手で解くこと", ok=False)
                return 1
            pushed = sh(["git", "push", "-q", "origin", edition], cwd=ROOT, timeout=120)
            if pushed.returncode != 0:
                # ローカルだけ進んだままにしない(次回の前提検査で「リモートと食い違う」になり、当番が
                # 二度と動けなくなる。監査指摘)。main には入っているので、次回は merge し直せば済む
                undo_merge(ed_before, edition)
                report_change(stage, date, fix, transcript, base, head, merge_commit, branch, targets, "edition の push 失敗のため未実行")
                notify("oncall", f"{date} {stage}: 修正は main に入ったが {edition} の push に失敗したので巻き戻した: {(pushed.stderr or '')[-200:]}", ok=False)
                return 1
            targets.append(edition)
            # 追送: 取り込んだ merge commit と戻し方(全文は取り込み前に届いている)
            (ROOT / "metrics" / f"oncall-{date}-{stage}-report.md").open("a", encoding="utf-8").write(
                f"\n\n## 取り込み結果\nmain の merge commit: {merge_commit}\n戻し方: git revert -m 1 {merge_commit}\n")
            reported = notify("oncall", f"{date} {stage}: 取り込み完了。main の merge commit {merge_commit[:10]}"
                                        f"(取り込み先: {', '.join(targets)})\n"
                                        f"戻し方: ops の main で `git revert -m 1 {merge_commit[:10]}` → push → {edition} に main を merge → push",
                              require=True)
        else:
            reported = notify("oncall", f"{date} {stage}: 当番と監査の合意: コードの欠陥ではない(no_fix_needed)。\n"
                                        f"診断: {(fix.get('diagnosis') or '')[:400]}\n再実行の根拠: {(fix.get('recovery') or '')[:300]}\n"
                                        f"再実行: {rerun_mode}")
        if not reported:
            # 報告が人に届いていないなら再実行(=発行)へ進まない(監査指摘)。修正は main に入っているので
            # 報告ファイル(metrics/oncall-*-report.md)を人が見て、手で再実行する
            notify("oncall", f"{date} {stage}: 修正報告が Discord に届かないので再実行しない。metrics/oncall-{date}-{stage}-report.md を見て手で再実行すること", ok=False)
            return 1
        if rerun_mode == "none":
            notify("oncall", f"{date} {stage}: 再実行しても通らない、と当番が判断。人の判断が要る", ok=False)
            return 1
        if a.no_rerun:
            return 0
        if not root_clean():
            notify("oncall", f"{date} {stage}: 再実行前に本体の作業ツリーが汚れた。再実行しない", ok=False)
            return 1
        must(sh(["git", "checkout", "-q", edition], cwd=ROOT), f"{edition} の checkout")
        code = rerun_stage(stage, date, edition, full)
        notify("oncall", f"{date} {stage}: 再実行({rerun_mode})が終わった(exit {code})。"
                         f"{'成功' if code == 0 else '失敗。metrics/oncall-' + date + '-' + stage + '-rerun.log を見ること'}",
               ok=(code == 0))
        return code
    except Exception as e:
        notify("oncall", f"{date} {stage}: 当番の処理で例外: {type(e).__name__}: {str(e)[:300]}", ok=False)
        return 1
    finally:
        sh(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT)
        sh(["git", "worktree", "prune"], cwd=ROOT)
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
