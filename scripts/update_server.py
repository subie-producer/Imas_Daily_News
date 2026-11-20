#!/usr/bin/env python3
"""update_server: 配信サーバー(Caddy と webapp の Python の依存)の新しい版と脆弱性を毎日見て、重大なものは合議で上げる。
毎日 10:00(imas-server.timer)。

  python3 scripts/update_server.py [--check]

編集長 2026-11-20(cloudflared は月1で上げていたが、Caddy は 6 月に手で置いた 2.11.4、webapp の依存は requirements.txt の固定のままで、
どの定期にも入っていなかった。調べると Caddy 本体と Werkzeug に GHSA が出ていた):
- 毎日1回、新しい版・リリースノート・GHSA・CVE・CVSS を確かめる。脆弱性の一覧は OSV(GHSA・Go の脆弱性DB・PyPA を束ねる)から取り、
  CVSS は GHSA(GitHub)か CVE(NVD)から引く。Caddy は実行ファイルの build-info から、組み込んだ Go の標準ライブラリと依存のモジュールまで見る
- 通常の更新(脆弱性の無い新しい版)は Discord に知らせるだけ。同じ版は1回だけ知らせる
- 今の版に当たる脆弱性があれば、CVSS の有無や数値だけで決めず、Sol(codex)と Opus(claude)がそれぞれ評価し、割れたら議論させる。
  両者が「重大」かつ「上げる」で、上げる先まで揃ったときだけ自動で上げる。同じ報告と候補の組は判定し直さない
- 上げたら HTTP 200(手元の Caddy 経由の数ページと、外から見た公開ページ)を確かめ、だめならすぐ戻す
- 戻した後、版の固定・依存の制約・設定の非互換などの障壁が原因なら、Sol が外し方を出し Opus が査読する。合意したら外して上げ直し、
  外した requirements.txt / Caddyfile は今の号の枝に commit する(発行のマージで main に入る)
- 判断と根拠・更新の中身・障壁の解除・動作の確認・戻したかを、すべて Discord に知らせる
- 再起動で数秒落ちる。発行の時間帯(02:30〜07:00)には再起動しない(update_cloudflared と同じ待ち方)
- --check は調べて表示するだけ(通知・合議・更新をしない)
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_cloudflared as ucf
from pipelib import (ENV, ROOT, CliBlocked, checkout_edition_branch, claude_traced, cli_check, commit_and_push, edition_date,
                     job_lock, notify, notify_crash, now_jst, partial_output, prompt_file, read_answer, render_prompt, run_group,
                     save_raw, schema_ok, write_atomic)

JOB = "server"
HOME = Path.home()
SRV = HOME / "srv" / "imas-news"
CADDY_BIN = HOME / ".local" / "bin" / "caddy"
CADDYFILE_LIVE = SRV / "etc" / "Caddyfile"
CADDYFILE_REPO = Path("ops") / "caddy" / "Caddyfile"          # ROOT からの相対
REQUIREMENTS_REPO = Path("webapp") / "requirements.txt"       # ROOT からの相対(deploy.ensure_deps が読むもの)
APP_PY = Path(ENV.get("APP_PYTHON", str(SRV / "venv" / "bin" / "python")))
STATE = SRV / "state" / "server-updates.json"
WORK_ROOT = HOME / ".cache" / "imas-news" / "server"
WEB_UNIT, APP_UNIT = "imas-web.service", "imas-app.service"
LOCAL_WEB = "http://127.0.0.1:8080"
# Caddy 経由で、アプリが描くページ(トップ・検索・フィード)を取る
CHECK_PATHS = ("/", "/search?q=" + urllib.parse.quote("アイマス"), "/feed.xml")
CHECK_WAIT = 90               # 再起動から 200 が返るまで待つ秒
CADDY_RELEASE = "https://api.github.com/repos/caddyserver/caddy/releases/latest"
CADDY_MODULE = "github.com/caddyserver/caddy/v2"
PYPI_JSON = "https://pypi.org/pypi/{}/json"
OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{}"
GH_ADVISORY = "https://api.github.com/advisories/{}"
NVD_CVE = "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId={}"
NVD_INTERVAL = 6.5            # NVD は鍵なしで 30 秒に 5 回まで
UA = {"User-Agent": "ImasNewsBot/1.0"}
SOL_MODEL = ENV.get("SERVER_SOL_MODEL", "gpt-6.1-sol")
OPUS_MODEL = ENV.get("SERVER_OPUS_MODEL", "claude-opus-5-5")
SESSION_TIMEOUT = 1800
BARRIER_ROUNDS = 2            # 障壁の外し方の「案→査読」の往復の上限
LOCK_WAIT_MIN = 120           # 枝に commit するとき、収集などが作業ツリーを使い終わるのを待つ分
PIP_TIMEOUT = 600
# 入れ替えを始めてから、失敗して戻し・再起動・確認を終えるまでに見込む秒(入れ替えと戻しの pip の上限+確認の待ち×2+余裕)。
# これだけ先までに発行の時間帯に掛かるなら始めない(戻しの分を含める。Sol 監査 r5)
RESTART_TIMEOUT = 120         # systemctl restart の上限
EXTERNAL_TIMEOUT = 30         # 外からの公開ページの確認の上限
CHECK_LIMIT = CHECK_WAIT + EXTERNAL_TIMEOUT    # check_http 全体の上限
JOURNAL_TIMEOUT = 60          # 失敗の記録に添える journal の読み取りの上限
# 入れ替え1回(または戻し1回)の各段の上限の合計: pip+再起動+確認+journal。Caddy の差し替えは pip より短いので同じ見込みで足りる
SWAP_STEP = PIP_TIMEOUT + RESTART_TIMEOUT + CHECK_LIMIT + JOURNAL_TIMEOUT
# 障壁を外した上げ直しで、ロックを持ったまま行う git の通信。1段(checkout の fetch と再試行 / commit の push と pull --rebase と再試行)
# を子プロセスで走らせ、GIT_STEP_TIMEOUT 秒で子孫ごと打ち切る。ssh にも接続の上限と死活の確認を付け、切れた通信は早く諦める(Sol 監査 r8・r9)
GIT_SSH_BOUND = "ssh -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o BatchMode=yes"
GIT_STEP_TIMEOUT = 7 * 60
GIT_LOCK_MARGIN = 2 * GIT_STEP_TIMEOUT + 60      # checkout と commit の2段+余裕
QUIET_MARGIN = 2 * SWAP_STEP + GIT_LOCK_MARGIN + 300     # 入れ替え+戻し+ロック中の git+余裕(約 51 分。Sol 監査 r5・r6・r8: 各段の上限から積む)
# この回の持ち時間(unit の TimeoutStartSec 12h より短く)。入れ替えは、待ち・入れ替え・戻しがこの内に収まるときだけ始める(Sol 監査 r2)
RUN_BUDGET = 11 * 3600
RUN_DEADLINE: float | None = None      # main が起動の時刻+RUN_BUDGET を入れる(テスト・--check では無し)
SCHEMA_ASSESS = "server-assess.schema.json"
SCHEMA_BARRIER = "server-barrier.schema.json"
SCHEMA_REVIEW = "server-barrier-review.schema.json"


def say(msg: str) -> None:
    ucf.say(msg)


# ---------------------------------------------------------------- 取得

def http_json(url: str, data: dict | None = None, timeout: float = 60, attempts: int = 3, headers: dict | None = None):
    """JSON を取る。通信の失敗(WSL の名前解決の断続的な失敗を含む)は間を空けて attempts 回まで。404 は None。"""
    body = json.dumps(data).encode() if data is not None else None
    hdr = {**UA, **(headers or {}), **({"Content-Type": "application/json"} if body else {})}
    last: Exception | None = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=hdr), timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
            if e.code < 500 and e.code != 429:
                break
        except Exception as e:
            last = e
        if i < attempts - 1:
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"{url} を取れない({type(last).__name__}: {last})")


def vkey(v: str) -> tuple:
    """版の比較の鍵。数字の並び(v は外す)と、プレリリース(a/b/rc/dev/alpha/beta・Go の -pre)かどうか。"""
    m = re.match(r"v?(\d+(?:\.\d+)*)", v or "")
    nums = tuple(int(x) for x in m.group(1).split(".")) if m else ()
    rest = (v or "")[m.end():] if m else (v or "")
    pre = bool(re.search(r"(a|b|rc|dev|alpha|beta|pre)\d*", rest, re.I) or rest.startswith("-"))
    return nums + (0,) * (8 - len(nums)), 0 if pre else 1


def is_pre(v: str) -> bool:
    return vkey(v)[1] == 0


def parse_build_info(text: str) -> dict:
    """`caddy build-info` から {version, go, modules}。置き換え(=>)の行は直前のモジュールの版を置き換える。"""
    go, mods, last = "", {}, ""
    for ln in (text or "").splitlines():
        f = ln.split("\t")
        if f[0] == "go" and len(f) > 1:
            go = f[1].removeprefix("go")
        elif f[0] == "dep" and len(f) > 2:
            mods[f[1]] = f[2]
            last = f[1]
        elif f[0] == "=>" and len(f) > 2 and last:
            mods[last] = f[2]
    return {"version": mods.get(CADDY_MODULE, "").removeprefix("v"), "go": go, "modules": mods}


def caddy_inventory(binary: Path = CADDY_BIN) -> dict:
    r = subprocess.run([str(binary), "build-info"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"{binary} build-info が失敗(exit {r.returncode}): {(r.stderr or '').strip()[-200:]}")
    inv = parse_build_info(r.stdout)
    if not inv["version"] or not inv["go"]:
        raise RuntimeError(f"{binary} build-info から Caddy の版・Go の版を読めない")
    return inv


def python_inventory() -> dict[str, str]:
    """webapp の venv に入っているパッケージ {名前: 版}(依存の依存も含む)。"""
    r = subprocess.run([str(APP_PY), "-m", "pip", "list", "--format=json", "--disable-pip-version-check"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"pip list が失敗(exit {r.returncode}): {(r.stderr or '').strip()[-200:]}")
    return {p["name"]: p["version"] for p in json.loads(r.stdout)}


def caddy_queries(inv: dict) -> list[tuple[str, str, str]]:
    return [("Go", "stdlib", inv["go"])] + [("Go", m, v.removeprefix("v")) for m, v in sorted(inv["modules"].items())]


def osv_ids(queries: list[tuple[str, str, str]]) -> dict[tuple[str, str, str], list[str]]:
    """OSV に (生態系, 名前, 版) ごとの当たる報告の ID を聞く。"""
    out: dict = {}
    for i in range(0, len(queries), 500):
        chunk = queries[i:i + 500]
        res = http_json(OSV_BATCH, {"queries": [{"package": {"ecosystem": e, "name": n}, "version": v} for e, n, v in chunk]})
        results = (res or {}).get("results") or []
        if len(results) != len(chunk):
            raise RuntimeError(f"OSV の答えの数が問い合わせと合わない({len(results)}/{len(chunk)})")
        for q, r in zip(chunk, results):
            ids = [v["id"] for v in r.get("vulns") or []]
            if r.get("next_page_token"):
                ids += _osv_more(q, r["next_page_token"])
            if ids:
                out[q] = ids
    return out


def _osv_more(q: tuple[str, str, str], token: str) -> list[str]:
    ids = []
    while token:
        r = http_json("https://api.osv.dev/v1/query",
                      {"package": {"ecosystem": q[0], "name": q[1]}, "version": q[2], "page_token": token}) or {}
        ids += [v["id"] for v in r.get("vulns") or []]
        token = r.get("next_page_token") or ""
    return ids


def fixed_versions(vuln: dict, eco: str, name: str) -> list[str]:
    """OSV の報告から、その パッケージの「直った版」の一覧。"""
    out = []
    for a in vuln.get("affected") or []:
        p = a.get("package") or {}
        if p.get("ecosystem") != eco or p.get("name", "").lower() != name.lower():
            continue
        for rg in a.get("ranges") or []:
            out += [ev["fixed"].removeprefix("v") for ev in rg.get("events") or [] if ev.get("fixed")]
    return sorted(set(out), key=vkey)


def go_symbols(vuln: dict, name: str) -> list[str]:
    """Go の報告の、影響を受けるパッケージ・関数(ecosystem_specific.imports)。"""
    out = []
    for a in vuln.get("affected") or []:
        if (a.get("package") or {}).get("name") != name:
            continue
        for imp in ((a.get("ecosystem_specific") or {}).get("imports") or []):
            syms = imp.get("symbols") or []
            out.append(f"{imp.get('path')}: {', '.join(syms[:12])}{' ほか' if len(syms) > 12 else ''}" if syms else str(imp.get("path")))
    return out


def cvss_of(vid: str, aliases: list[str], cache: dict, nvd_last: list) -> dict:
    """CVSS の点と出どころ。GHSA があれば GitHub、無ければ CVE を NVD で引く。引いたものは state に残し、同じ報告を引き直さない。"""
    if vid in cache:
        return cache[vid]
    ids = [vid] + list(aliases)
    got = {"score": None, "vector": "", "source": "", "severity": ""}
    ghsa = next((i for i in ids if i.startswith("GHSA-")), "")
    cve = next((i for i in ids if i.startswith("CVE-")), "")
    try:
        if ghsa:
            d = http_json(GH_ADVISORY.format(ghsa), headers={"Accept": "application/vnd.github+json"}) or {}
            sev = d.get("cvss_severities") or {}
            best = next((sev[k] for k in ("cvss_v4", "cvss_v3") if (sev.get(k) or {}).get("score")), None) or d.get("cvss") or {}
            got = {"score": best.get("score"), "vector": best.get("vector_string") or "", "source": ghsa,
                   "severity": d.get("severity") or ""}
        if got["score"] is None and cve:
            wait = NVD_INTERVAL - (time.time() - (nvd_last[0] if nvd_last else 0))
            if wait > 0:
                time.sleep(wait)
            nvd_last[:] = [time.time()]
            d = http_json(NVD_CVE.format(cve)) or {}
            vulns = d.get("vulnerabilities") or []
            metrics = ((vulns[0].get("cve") or {}).get("metrics") or {}) if vulns else {}
            for k in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30"):
                if metrics.get(k):
                    data = metrics[k][0].get("cvssData") or {}
                    got = {"score": data.get("baseScore"), "vector": data.get("vectorString") or "", "source": cve,
                           "severity": data.get("baseSeverity") or got["severity"]}
                    break
    except Exception as e:
        say(f"({vid} の CVSS を引けない: {e})")
        return {**got, "error": str(e)[:120]}      # 引けなかった回は残さない(次の回に引き直す)
    cache[vid] = got
    return got


def advisories(hits: dict[tuple[str, str, str], list[str]], component: str, state: dict) -> dict[str, dict]:
    """当たった報告の中身を OSV から引いて、報告ごとに1つにまとめる(同じ報告が標準ライブラリと x/net の両方に当たる)。"""
    out: dict[str, dict] = {}
    cache = state.setdefault("cvss", {})
    nvd_last: list = []
    for (eco, name, ver), ids in hits.items():
        for vid in ids:
            if vid not in out:
                v = http_json(OSV_VULN.format(vid)) or {"id": vid}
                aliases = [a for a in v.get("aliases") or [] if a != vid]
                out[vid] = {"id": vid, "aliases": aliases, "summary": v.get("summary") or "",
                            "details": (v.get("details") or "")[:2000], "published": v.get("published") or "",
                            "modified": v.get("modified") or "", "component": component, "packages": [],
                            "refs": [r.get("url") for r in v.get("references") or [] if r.get("url")][:6],
                            "cvss": cvss_of(vid, aliases, cache, nvd_last), "_raw": v}
            a = out[vid]
            a["packages"].append({"ecosystem": eco, "name": name, "installed": ver,
                                  "fixed": fixed_versions(a["_raw"], eco, name),
                                  "symbols": go_symbols(a["_raw"], name) if eco == "Go" else []})
    for a in out.values():
        a.pop("_raw", None)
    return merge_aliases(out)


def merge_aliases(advs: dict[str, dict]) -> dict[str, dict]:
    """同じ脆弱性の別名の報告(GHSA と GO- と PYSEC など、互いの aliases に載るもの)を1つに束ねる。
    代表は ID の若いもの。当たるパッケージは合わせ、CVSS は点のあるほうを採る。"""
    parent = {k: k for k in advs}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    for k, a in advs.items():
        for al in a["aliases"]:
            if al in advs:
                ra, rb = root(k), root(al)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    out: dict[str, dict] = {}
    for k in sorted(advs):
        r = root(k)
        a = advs[k]
        if r not in out:
            out[r] = {**a, "aliases": list(a["aliases"]), "packages": list(a["packages"])}
            continue
        m = out[r]
        m["aliases"] = sorted((set(m["aliases"]) | set(a["aliases"]) | {k}) - {r})
        have = {(p["ecosystem"], p["name"]) for p in m["packages"]}
        m["packages"] += [p for p in a["packages"] if (p["ecosystem"], p["name"]) not in have]
        if m["cvss"].get("score") is None and a["cvss"].get("score") is not None:
            m["cvss"] = a["cvss"]
        m["details"] = m["details"] or a["details"]
        m["refs"] = list(dict.fromkeys(m["refs"] + a["refs"]))[:8]
    return out


def caddy_latest() -> dict:
    rel = http_json(CADDY_RELEASE)
    if not rel or not rel.get("tag_name"):
        raise RuntimeError("Caddy の最新の release を読めない")
    return {"version": rel["tag_name"].removeprefix("v"), "notes": rel.get("body") or "", "url": rel.get("html_url") or "",
            "published": rel.get("published_at") or "", "assets": rel.get("assets") or []}


def pypi_latest(name: str) -> dict:
    d = http_json(PYPI_JSON.format(urllib.parse.quote(name)))
    if not d:
        return {}
    rels = [v for v, files in (d.get("releases") or {}).items()
            if files and not all(f.get("yanked") for f in files) and not is_pre(v)]
    urls = (d.get("info") or {}).get("project_urls") or {}
    notes = next((u for k, u in urls.items() if re.search(r"change|release|history|news", k, re.I)), "")
    return {"latest": max(rels, key=vkey) if rels else "", "releases": sorted(rels, key=vkey), "notes_url": notes}


# ---------------------------------------------------------------- Caddy の新しい版を取る

def caddy_asset(rel: dict) -> tuple[dict | None, dict | None, str]:
    ver = rel["version"]
    name = f"caddy_{ver}_linux_amd64.tar.gz"
    by = {a.get("name"): a for a in rel["assets"]}
    return by.get(name), by.get(f"caddy_{ver}_checksums.txt"), name


def checksum_of(text: str, name: str) -> str:
    """checksums.txt(「<sha512>  <ファイル名>」の行)から、そのファイルの sha512。"""
    m = re.search(rf"(?m)^([0-9a-f]{{128}})\s+\*?{re.escape(name)}\s*$", text or "")
    return m.group(1) if m else ""


def fetch_caddy(rel: dict, work: Path) -> Path:
    """最新の Caddy を work に取り、checksums.txt の sha512 と照らし、展開した実行ファイルが版を名乗ることまで確かめる。"""
    asset, sums, name = caddy_asset(rel)
    if not asset or not sums:
        raise RuntimeError(f"release {rel['version']} に {name} か checksums.txt が無い")
    tgz, sums_p = work / name, work / "checksums.txt"
    ucf.download(sums["browser_download_url"], sums_p)
    want = checksum_of(sums_p.read_text(encoding="utf-8", errors="replace"), name)
    if not want:
        raise RuntimeError(f"checksums.txt に {name} の sha512 が無い")
    ucf.download(asset["browser_download_url"], tgz)
    got = hashlib.sha512(tgz.read_bytes()).hexdigest()
    if got != want:
        raise RuntimeError(f"{name} の sha512 が checksums.txt と違う(記載 {want[:16]}… / 取得 {got[:16]}…)")
    out = work / "caddy-new"
    with tarfile.open(tgz) as tf:
        m = tf.getmember("caddy")
        if not m.isfile():
            raise RuntimeError(f"{name} の caddy が通常のファイルでない")
        with tf.extractfile(m) as src, out.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    out.chmod(0o755)
    v = caddy_inventory(out)["version"]
    if v != rel["version"]:
        raise RuntimeError(f"取った Caddy が {rel['version']} を名乗らない({v})")
    return out


# ---------------------------------------------------------------- 状態

def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise RuntimeError(f"{STATE} が JSON として読めない({e})")


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(STATE, json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


def judge_key(ids: list[str], candidates: dict[str, list[str]]) -> str:
    """判定の鍵: 当たっている報告と、上げる先の候補の組。同じ組は判定し直さない(候補が増えた・報告が増えた・直ったら変わる)。"""
    blob = json.dumps({"ids": sorted(set(ids)), "candidates": {k: sorted(v, key=vkey) for k, v in sorted(candidates.items())}},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 合議

def sol_session(prompt: str, schema_name: str, name: str, work: Path, date: str) -> dict | None:
    """Sol(codex)の構造化セッション1本。正常に終わり、渡した schema に合う答えだけを返す。"""
    schema_p = ROOT / "schema" / schema_name
    short = prompt_file(date, name, prompt, base=work)
    out_p = work / f"{name}.answer.json"
    cmd = ["codex", "exec", "-m", SOL_MODEL, "-s", "read-only", "--skip-git-repo-check", "-C", str(work),
           "--output-schema", str(schema_p), "--output-last-message", str(out_p), short]
    try:
        r = run_group(cmd, timeout=SESSION_TIMEOUT, cwd=work)
        so, se, code = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        (so, se), code = partial_output(e), None
    text = out_p.read_text(encoding="utf-8") if out_p.exists() else ""
    save_raw(date, f"server-{name}", so, se, code, files={"answer": text})
    blocked = cli_check("codex", JOB, "配信サーバーの評価", code, so or "", se or "")
    if blocked:
        raise CliBlocked(blocked)
    if code != 0:
        say(f"  Sol {name}: {'時間切れ' if code is None else f'exit {code}'}: {((se or so or '').strip())[-200:]}")
        return None
    ans, why = read_answer(text)
    if not (isinstance(ans, dict) and schema_ok(ans, json.loads(schema_p.read_text(encoding="utf-8")))):
        say(f"  Sol {name}: 答えが schema のとおりでない({why or '形が違う'})")
        return None
    return ans


def opus_session(prompt: str, schema_name: str, name: str, work: Path, date: str) -> dict | None:
    """Opus(claude)の構造化セッション1本。道具は Read だけ。"""
    schema = (ROOT / "schema" / schema_name).read_text(encoding="utf-8")
    short = prompt_file(date, name, prompt, base=work)
    r = claude_traced([short, "--model", OPUS_MODEL, "--json-schema", schema, "--tools", "Read", "--dangerously-skip-permissions"],
                      work / f"{name}-trace.jsonl", timeout=SESSION_TIMEOUT, cwd=work, job=JOB, where="配信サーバーの評価")
    say(f"  Opus {name}: {r['summary'][:300]}")
    out = r["structured"] if r["ok"] else None
    if r["ok"] and not isinstance(out, dict):
        out, _ = read_answer(r["result"] or "")
    return out if isinstance(out, dict) and schema_ok(out, json.loads(schema)) else None


def shaped_assess(ans: dict | None, candidates: dict[str, list[str]]) -> dict | None:
    """評価の答えを候補と照らす。apply なら上げる先が候補の中から1パッケージ1つ。hold なら上げる先は空。崩れていれば None。"""
    if not isinstance(ans, dict):
        return None
    targets = ans.get("targets") or []
    if ans.get("verdict") == "hold":
        return {**ans, "targets": []}
    seen = set()
    for t in targets:
        name, _, ver = str(t).partition("==")
        if name not in candidates or ver not in candidates[name] or name in seen:
            return None
        seen.add(name)
    return ans if targets else None


def agreed(a: dict | None, b: dict | None) -> str:
    """2人の評価が揃ったか: "apply"(両者が重大・上げる・同じ上げる先)/ "hold"(両者が上げない)/ ""(割れた・答えが無い)。"""
    if not a or not b:
        return ""
    if a["verdict"] == b["verdict"] == "apply" and a.get("serious") is True and b.get("serious") is True \
            and sorted(a["targets"]) == sorted(b["targets"]):
        return "apply"
    if a["verdict"] == b["verdict"] == "hold":
        return "hold"
    return ""


def council(component: str, materials: Path, candidates: dict[str, list[str]], work: Path, date: str) -> dict:
    """Sol と Opus が独立に評価し、割れたら相手の答えを見せて2巡目。戻りは {"result": apply/hold/split, "sol", "opus", "rounds"}。"""
    cand = "\n".join(f"- {n}: " + " / ".join(f"{n}=={v}" for v in vs) for n, vs in sorted(candidates.items()))
    first = render_prompt("server-assess", COMPONENT=component, CANDIDATES=cand, MATERIALS=str(materials))
    a = shaped_assess(sol_session(first, SCHEMA_ASSESS, f"{component}-assess-sol", work, date), candidates)
    b = shaped_assess(opus_session(first, SCHEMA_ASSESS, f"{component}-assess-opus", work, date), candidates)
    rounds = 1
    if not agreed(a, b) and a and b:
        rounds = 2
        dump = lambda x: json.dumps(x, ensure_ascii=False, indent=1)
        a2 = shaped_assess(sol_session(render_prompt("server-assess-debate", FIRST=first, MINE=dump(a), THEIRS=dump(b)),
                                       SCHEMA_ASSESS, f"{component}-debate-sol", work, date), candidates)
        b2 = shaped_assess(opus_session(render_prompt("server-assess-debate", FIRST=first, MINE=dump(b), THEIRS=dump(a)),
                                        SCHEMA_ASSESS, f"{component}-debate-opus", work, date), candidates)
        a, b = a2, b2
    if not a or not b:
        return {"result": "noanswer", "sol": a, "opus": b, "rounds": rounds}
    return {"result": agreed(a, b) or "split", "sol": a, "opus": b, "rounds": rounds}


def barrier_council(component: str, targets: list[str], failure: str, materials: Path, work: Path, date: str) -> dict:
    """障壁の外し方: Sol が案を出し、Opus が査読する。承認されるまで BARRIER_ROUNDS 往復。
    戻りは {"agreed": bool, "proposal", "review", "rounds"}。agreed は Opus が案を承認したこと(外す・外さないのどちらでも)。"""
    objections = "(なし)"
    proposal = review = None
    for n in range(1, BARRIER_ROUNDS + 1):
        p = render_prompt("server-barrier", COMPONENT=component, TARGETS=", ".join(targets), OBJECTIONS=objections,
                          FAILURE=failure, MATERIALS=str(materials))
        proposal = shaped_barrier(sol_session(p, SCHEMA_BARRIER, f"{component}-barrier-sol-{n}", work, date), component)
        if not proposal:
            return {"agreed": False, "proposal": None, "review": review, "rounds": n}
        rv = render_prompt("server-barrier-review", COMPONENT=component, PROPOSAL=json.dumps(proposal, ensure_ascii=False, indent=1),
                           FAILURE=failure, MATERIALS=str(materials))
        review = opus_session(rv, SCHEMA_REVIEW, f"{component}-barrier-opus-{n}", work, date)
        if not review:
            return {"agreed": False, "proposal": proposal, "review": None, "rounds": n}
        if review.get("approve") is True and not review.get("objections"):
            return {"agreed": True, "proposal": proposal, "review": review, "rounds": n}
        objections = "\n".join(f"- {o}" for o in review.get("objections") or []) or f"- {review.get('why')}"
    return {"agreed": False, "proposal": proposal, "review": review, "rounds": BARRIER_ROUNDS}


REQ_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
REQ_SPEC = re.compile(r"(?:(?:==|>=|<=|~=|!=|<|>)\s*[0-9][0-9A-Za-z.*+-]*)(?:\s*,\s*(?:==|>=|<=|~=|!=|<|>)\s*[0-9][0-9A-Za-z.*+-]*)*")


def shaped_barrier(ans: dict | None, component: str) -> dict | None:
    """障壁の案の形: 外すなら、webapp は requirements の書き換え(名前と版の指定)だけ、caddy は Caddyfile の全文だけ。"""
    if not isinstance(ans, dict):
        return None
    reqs, cf = ans.get("requirements") or [], ans.get("caddyfile") or ""
    if not ans.get("lift"):
        return {**ans, "requirements": [], "caddyfile": ""}
    if component == "webapp":
        ok = reqs and not cf and all(REQ_NAME.fullmatch(str(r.get("name") or "")) and REQ_SPEC.fullmatch(str(r.get("spec") or ""))
                                     for r in reqs)
    else:
        ok = cf.strip() and not reqs
    return ans if ok else None


def edit_requirements(text: str, edits: list[dict]) -> str:
    """requirements.txt の、名前の一致する行の指定を差し替える。無い名前は末尾に足す。注記・ほかの行はそのまま。"""
    lines = text.splitlines()
    norm = lambda s: re.sub(r"[-_.]+", "-", s).lower()
    for e in edits:
        new = f"{e['name']}{e['spec'].replace(' ', '')}"
        for i, ln in enumerate(lines):
            m = REQ_NAME.match(ln.strip())
            if m and not ln.lstrip().startswith("#") and norm(m.group(0)) == norm(e["name"]):
                lines[i] = new
                break
        else:
            lines.append(new)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 動作の確認

def systemctl_restart(unit: str) -> str:
    """再起動。失敗の理由を返す(成功なら空)。例外は投げない。上限は RESTART_TIMEOUT 秒。"""
    try:
        r = subprocess.run(["systemctl", "--user", "restart", unit], capture_output=True, text=True, timeout=RESTART_TIMEOUT)
    except Exception as e:
        return f"{unit} の再起動で例外({type(e).__name__}: {e})"
    return "" if r.returncode == 0 else f"{unit} の再起動に失敗(exit {r.returncode}): {(r.stderr or '').strip()[-200:]}"


def get_status(url: str, headers: dict | None = None, timeout: float = 30) -> int:
    """url の HTTP の状態コード(取れなければ 0)。curl の --max-time で、名前解決・接続・少しずつ届く応答まで含めた要求全体を
    timeout 秒で打ち切る(urlopen の timeout は1回の読みの上限で、全体を縛らない。Sol 監査 r7)。subprocess の timeout は予備。"""
    t = max(1.0, timeout)
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", f"{t:.1f}", "-A", UA["User-Agent"]]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    try:
        r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=t + 5)
    except Exception:
        return 0
    if r.returncode != 0:
        # 上限で打ち切った(exit 28)・つながらない回は、状態行だけ受け取っていても取れなかったとする(-w は 200 を出す)
        return 0
    code = (r.stdout or "").strip()
    return int(code) if code.isdigit() else 0


def check_http() -> tuple[bool, str]:
    """Caddy 経由の数ページが 200 か、外から公開ページが 200 か。再起動の直後は 200 が揃うまで待つ。例外は投げない。
    全体で CHECK_LIMIT 秒を越えない(1回の要求の上限は残りの秒に縮める。待ちの見込み QUIET_MARGIN の前提。Sol 監査 r6)。
    手元のページの待ちに CHECK_WAIT 秒、外からの確認に残り(EXTERNAL_TIMEOUT 秒)を取っておく。"""
    try:
        start = time.monotonic()
        local_end = start + CHECK_WAIT
        end = start + CHECK_LIMIT
        left = lambda until: until - time.monotonic()
        while True:
            codes = {}
            for p in CHECK_PATHS:
                codes[p] = get_status(LOCAL_WEB + p, timeout=min(30, left(local_end))) if left(local_end) > 0 else 0
            if all(c == 200 for c in codes.values()) or left(local_end) <= 0:
                break
            time.sleep(min(3, max(0, left(local_end))))
        site = ENV.get("SITE_URL", "").rstrip("/")
        ext = (get_status(f"{site}/?server-check={int(time.time())}", {"Cache-Control": "no-cache"}, timeout=min(30, left(end)))
               if site and left(end) > 0 else (0 if site else None))
        parts = [f"{p} {c}" for p, c in codes.items()] + ([f"外から {site} {ext}"] if site else [])
        ok = all(c == 200 for c in codes.values()) and ext in (200, None)
        return ok, "・".join(parts)
    except Exception as e:
        return False, f"確認の途中で例外({type(e).__name__}: {e})"


def unit_log(unit: str, since: str, lines: int = 40) -> str:
    try:
        r = subprocess.run(["journalctl", "--user", "-u", unit, "--since", since, "-o", "cat", "--no-pager", "-n", str(lines)],
                           capture_output=True, text=True, timeout=JOURNAL_TIMEOUT)
        return (r.stdout or "").strip()
    except Exception as e:
        return f"(journal を読めない: {e})"


# ---------------------------------------------------------------- 上げる

def pip(*args: str, timeout: float = PIP_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run([str(APP_PY), "-m", "pip", *args, "--disable-pip-version-check"],
                          capture_output=True, text=True, timeout=timeout)


def apply_webapp(targets: list[str], req_text: str, work: Path) -> dict:
    """webapp の依存を上げる。requirements(req_text)を守ったまま入れ、アプリを再起動して確かめ、だめなら入れる前の版に戻す。
    戻りは {"ok", "steps": [...], "failure": 失敗の記録(成功なら空), "rolled_back": bool}。"""
    steps: list[str] = []
    since = now_jst().strftime("%Y-%m-%d %H:%M:%S")
    freeze = pip("freeze", "--all")
    if freeze.returncode != 0:
        return {"ok": False, "steps": steps, "failure": f"pip freeze が失敗: {freeze.stderr[-300:]}", "rolled_back": False}
    before = work / "freeze-before.txt"
    before.write_text(freeze.stdout, encoding="utf-8")
    req = work / "requirements.txt"
    req.write_text(req_text, encoding="utf-8")
    dry = pip("install", "--dry-run", "-q", "-r", str(req), *targets)
    if dry.returncode != 0:
        steps.append("入れる前の解決で失敗(何も入れていない)")
        return {"ok": False, "steps": steps, "rolled_back": False,
                "failure": f"pip install --dry-run -r requirements.txt {' '.join(targets)} が失敗:\n{(dry.stderr or dry.stdout)[-1500:]}"}
    # 入れ替えの直前に見直す。ここでは待たない(障壁を外した上げ直しではロックを持っている。待ちは入口の guarded がロックの外で行う。
    # Sol 監査 r5)。発行の時間帯に掛かるなら何も入れずに返し、guarded が待ってからやり直す
    if (why := swap_blocker()):
        steps.append(why)
        return {**not_started(why), "steps": steps}
    # ここから先は入れ替えが始まっている。例外(pip の時間切れなど)でも必ず下の戻しへ進む(Sol 監査 r1)。停止の要求(SIGTERM)は
    # この区間では例外にせず保留し、戻しまで済ませる(Sol 監査 r3)
    with critical():
        try:
            r = pip("install", "-q", "-r", str(req), *targets)
            if r.returncode != 0:
                fail = f"pip install が失敗:\n{(r.stderr or r.stdout)[-1500:]}"
            elif TERM_PENDING:
                fail = "入れているあいだに停止の要求(SIGTERM)を受けたので、再起動せずに戻す"
            elif ucf.publishing_wait(now_jst()) > 0:
                fail = "入れ終わったときに発行の時間帯に入っていたので、再起動せずに戻す"
            else:
                steps.append(f"{' '.join(targets)} を入れた")
                why = systemctl_restart(APP_UNIT)
                ok, state = check_http() if not why else (False, why)
                steps.append(f"アプリを再起動して確認: {state}")
                if ok:
                    return {"ok": True, "steps": steps, "failure": "", "rolled_back": False}
                fail = f"上げた後の確認で失敗: {state}\n--- imas-app の journal ---\n{unit_log(APP_UNIT, since)}"
        except Exception as e:
            fail = f"入れ替えの途中で例外({type(e).__name__}: {e})"
        # 戻す: 入れる前の freeze のとおりに入れ直し、再起動して確かめる。戻しの例外も捕まえて、戻せなかったと返す
        try:
            rb = pip("install", "-q", "-r", str(before))
            pip_state = "pip 成功" if rb.returncode == 0 else "pip 失敗: " + (rb.stderr or "")[-200:]
            pip_ok = rb.returncode == 0
        except Exception as e:
            pip_state, pip_ok = f"pip で例外({type(e).__name__}: {e})", False
        rs = systemctl_restart(APP_UNIT)
        ok2, state2 = check_http() if not rs else (False, rs)
        steps.append(f"入れる前の版に戻した({pip_state})・再起動して確認: {state2}")
        return {"ok": False, "steps": steps, "failure": fail, "rolled_back": True, "restored": pip_ok and ok2}


def caddy_validate(binary: Path, caddyfile: Path) -> str:
    """新しい Caddy で Caddyfile が通るか。通らなければ理由。"""
    r = subprocess.run([str(binary), "validate", "--config", str(caddyfile), "--adapter", "caddyfile"],
                       capture_output=True, text=True, timeout=120)
    return "" if r.returncode == 0 else f"caddy validate が失敗(exit {r.returncode}):\n{(r.stderr or r.stdout)[-1500:]}"


def apply_caddy(new_bin: Path, version: str, cur: str, caddyfile_text: str | None = None) -> dict:
    """Caddy を差し替える(caddyfile_text を渡せば Caddyfile も)。validate → 差し替え → 再起動 → 確認、だめなら両方戻して再起動・確認。"""
    steps: list[str] = []
    since = now_jst().strftime("%Y-%m-%d %H:%M:%S")
    cf = CADDYFILE_LIVE
    cand = new_bin.with_name("Caddyfile.candidate")
    bak = CADDY_BIN.with_name(f"caddy.{cur or 'unknown'}.bak")
    cf_bak = cf.with_name("Caddyfile.bak")
    # 差し替えの前の段(validate・旧版の写し)の例外は、何も入れ替えていないので戻しは要らない
    try:
        cand.write_text(caddyfile_text if caddyfile_text is not None else cf.read_text(encoding="utf-8"), encoding="utf-8")
        bad = caddy_validate(new_bin, cand)
        if bad:
            steps.append("新しい版で Caddyfile が通らない(差し替えていない)")
            return {"ok": False, "steps": steps, "failure": bad, "rolled_back": False}
        shutil.copy2(CADDY_BIN, bak)
        shutil.copy2(cf, cf_bak)
    except Exception as e:
        steps.append("差し替えの前の準備で例外(差し替えていない)")
        return {"ok": False, "steps": steps, "failure": f"準備で例外({type(e).__name__}: {e})", "rolled_back": False}
    # 差し替えの直前に見直す。ここでは待たない(待ちは入口の guarded がロックの外で行う。Sol 監査 r5)
    if (why := swap_blocker()):
        steps.append(why)
        return {**not_started(why), "steps": steps}
    # 差し替えから戻しまでは、停止の要求(SIGTERM)を例外にせず保留する(確認の失敗から戻しまでのあいだで止められると、
    # 新しい実行ファイル・設定が残り、戻しも止めた印の記録もしない。Sol 監査 r3)
    with critical():
        staged = CADDY_BIN.with_name("caddy.new")
        swap_err = ""
        try:
            shutil.copy2(new_bin, staged)
            os.replace(staged, CADDY_BIN)
            if caddyfile_text is not None:
                write_atomic(cf, caddyfile_text)
        except Exception as e:
            # 片方だけ入れ替わった状態を残さない: 下の戻しへ進む
            swap_err = f"差し替えの途中で例外({type(e).__name__}: {e})"
        finally:
            ucf.quiet_unlink(staged)
        if swap_err:
            ok, state = False, swap_err
        elif TERM_PENDING:
            ok, state = False, "差し替えのあいだに停止の要求(SIGTERM)を受けたので、再起動せずに戻す"
        else:
            steps.append(f"Caddy {cur} → {version} に差し替えた" + ("(Caddyfile も書き換えた)" if caddyfile_text is not None else ""))
            why = systemctl_restart(WEB_UNIT)
            ok, state = check_http() if not why else (False, why)
        steps.append(f"再起動して確認: {state}")
        if ok:
            try:
                prune_caddy_backups()
            except Exception as e:
                say(f"(古い旧版の片付けに失敗: {e})")
            return {"ok": True, "steps": steps, "failure": "", "rolled_back": False}
        try:
            log = unit_log(WEB_UNIT, since)
        except Exception as e:
            log = f"(journal を読めない: {e})"
        fail = f"差し替えた後の確認で失敗: {state}\n--- imas-web の journal ---\n{log}"
        restore = ""
        try:
            ucf_replace(bak, CADDY_BIN)
            shutil.copy2(cf_bak, cf)
        except Exception as e:
            restore = f"{type(e).__name__}: {e}"
        rs = systemctl_restart(WEB_UNIT)
        ok2, state2 = check_http() if not rs else (False, rs)
        steps.append(("旧版に戻した" if not restore else f"旧版に戻せない({restore}。{bak} と {cf_bak} を手で戻す)")
                     + f"・再起動して確認: {state2}")
        return {"ok": False, "steps": steps, "failure": fail, "rolled_back": True, "restored": not restore and ok2}


def ucf_replace(src: Path, dest: Path) -> None:
    staged = dest.with_name(dest.name + ".new")
    try:
        shutil.copy2(src, staged)
        os.replace(staged, dest)
    finally:
        ucf.quiet_unlink(staged)


def quiet_wait_secs(now: datetime.datetime, margin: float = 0) -> float:
    """now から margin 秒のあいだに発行の時間帯(02:30〜07:00)に掛かるなら、その終わりまでの秒。掛からなければ 0。"""
    later = ucf.publishing_wait(now + datetime.timedelta(seconds=margin))
    if later > 0:
        return later + margin
    return ucf.publishing_wait(now)


def wait_quiet(margin: float = 0) -> None:
    """入れ替えて再起動し終えるまで(margin 秒)のあいだに発行の時間帯に掛かるなら、その終わりまで待つ(再起動で数秒落ちるため)。
    待ちから戻るたびに時刻を見直す。停止の要求を受けたら待ちをやめて戻る(入れ替えるかは呼び手が swap_blocker で決める)。"""
    while not TERM_PENDING and (w := quiet_wait_secs(now_jst(), margin)) > 0:
        say(f"発行の時間帯に掛かるので {ucf.QUIET_UNTIL:%H:%M} まで待ってから入れ替える({int(w)}秒)")
        time.sleep(min(w, 60))


def swap_blocker() -> str:
    """いま入れ替えを始めてはいけない理由(無ければ空)。入れ替えの直前(待ち・ロック待ちの後)に必ず見直す(Sol 監査 r4):
    停止の要求を受けている・入れ替えと戻しのあいだに発行の時間帯に掛かる・この回の残り時間に収まらない。"""
    if TERM_PENDING:
        return "停止の要求(SIGTERM)を受けているので入れ替えを始めない"
    if quiet_wait_secs(now_jst(), QUIET_MARGIN) > 0:
        return QUIET_BLOCK
    ok, why = swap_budget_ok()
    return "" if ok else f"入れ替えを始めない: {why}"


QUIET_BLOCK = "入れ替えと戻しのあいだに発行の時間帯に掛かるので入れ替えを始めない"


def prune_caddy_backups(keep: int = ucf.BACKUP_KEEP) -> None:
    baks = sorted(CADDY_BIN.parent.glob("caddy.*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in baks[keep:]:
        ucf.quiet_unlink(p)


def not_started(msg: str) -> dict:
    """何も入れ替えずに返す。発行の時間帯に掛かるのが理由なら retry(入口の guarded がロックの外で待ってからやり直す)。"""
    return {"ok": False, "steps": [msg], "failure": msg, "rolled_back": False, "retry": msg == QUIET_BLOCK}


def change_on_branch(rel: Path, expect: str, text: str, message: str, apply) -> dict:
    """障壁を外した上げ直し: 作業ツリーのロックを取り、今の号の枝を checkout し、rel が評価に使った原本(expect)のままかを確かめてから
    apply()(入れ替え・確認・だめなら戻し)を走らせ、通ったら rel を text にして commit・push する。ロックはこの間ずっと持つ
    (合議のあいだに枝に入った変更を、評価の時の古い全文で消さない。Sol 監査 r3)。戻りは apply と同じ形。
    - 原本と違えば入れ替えずに失敗にする(次の回に今の中身で評価し直す)
    - commit できずに終わったら、index と作業ツリーの rel を HEAD に戻す(stage したまま残すと、次の収集・組版の checkout が
      未コミットの変更で止まる。Sol 監査 r1)。commit できて push だけ失敗したときは戻さない(次の工程の checkout が送る)
    - 自分が rel を書き換える前の失敗(ロック・checkout)では何も戻さない(他の工程の変更を消す。Sol 監査 r2)
    - text が expect と同じ(書き換えない通常の上げ方)なら checkout も commit もせず、ロックの中で作業ツリーの原本を確かめて apply する。
      発行の deploy もこのロックを持って venv に依存を入れるので、重ならず、待つあいだに deploy が入れた依存を古い全文で戻さない(Sol 監査 r9)
    - git の通信(checkout の fetch・commit の push)は子プロセス(git_step)で走らせ、全体の期限で子孫ごと打ち切る(Sol 監査 r8・r9)"""
    fd = None
    head = ""
    wrote = False
    r: dict | None = None
    try:
        # 発行の時間帯の待ちは、ロックの外の入口(guarded)で済ませる。ロックの中では待たない(ロックを持ったまま待つと、03:00 の組版・
        # 06:00 の発行がロックを取れない。Sol 監査 r4・r5)。ロック待ちは停止の要求を受けたらやめる(待ち終えてから停止の直前に入れ替えを始めない。Sol 監査 r4)
        fd = job_lock(JOB, wait_min=0, wait_fn=lambda t0: 0 if TERM_PENDING else LOCK_WAIT_MIN)
        date = edition_date()
        if text != expect:
            ok, why = git_step("checkout", date)
            if not ok:
                return not_started(f"edition/{date} を checkout できない(入れ替えていない): {why}")
        path = ROOT / rel
        cur = path.read_text(encoding="utf-8")
        if cur != expect:
            return not_started(f"{rel} が評価の後に枝の上で変わっていた(評価に使った原本と違う)。入れ替えずに次の回に今の中身で評価し直す")
        # ロック待ちのあいだに時刻・停止の要求・残り時間が変わる。入れ替えの直前に見直す
        if (why := swap_blocker()):
            return not_started(why)
        r = apply()
        if not r["ok"] or cur == text:
            return r
        head = _head()
        wrote = True
        path.write_text(text, encoding="utf-8")
        ok, why = git_step("commit", date, str(rel), message)
        if not ok:
            if _committed(head, rel, text):
                r["steps"].append(f"{rel} を edition/{date} に commit したが push できない(次の工程の checkout が送る)")
                return r
            _restore(rel)
            msg = f"🚨 {rel} の edition/{date} への commit に失敗(index と作業ツリーは元に戻した。動いている版は上げたまま): {why}"
            r["steps"].append(msg)
            return {**r, "ok": False, "failure": msg}
        r["steps"].append(f"{rel} を edition/{date} に commit した")
        return r
    except Exception as e:
        if wrote and not (head and _committed(head, rel, text)):
            _restore(rel)
        msg = f"枝への反映の途中で例外({type(e).__name__}: {e})"
        if r is None:
            return not_started(msg)
        r["steps"].append(f"🚨 {msg}")
        return {**r, "ok": False, "failure": msg}
    finally:
        if fd is not None:
            os.close(fd)


def git_step(kind: str, *args: str) -> tuple[bool, str]:
    """git の通信を伴う1段を子プロセス(この scripts の --git-step)で走らせ、GIT_STEP_TIMEOUT 秒で子孫ごと打ち切る(ssh の死活の確認に
    応えながら git だけが止まる場合も切る。Sol 監査 r9)。親が持つ作業ツリーのロックは子に引き継ぐ(IMAS_JOB_LOCK=held)。
    打ち切ったら、殺した git が残した .git/index.lock を消す(ロックを持っているので、ほかの工程の git ではない)。戻りは (成功か, 説明)。"""
    env = {**os.environ, "IMAS_JOB_LOCK": "held", "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND") or GIT_SSH_BOUND}
    cmd = [sys.executable, str(Path(__file__).resolve()), "--git-step", kind, *args]
    try:
        r = run_group(cmd, timeout=GIT_STEP_TIMEOUT, cwd=ROOT, env=env)
    except subprocess.TimeoutExpired:
        lock = ROOT / ".git" / "index.lock"
        try:
            lock.unlink(missing_ok=True)
        except OSError as e:
            say(f"({lock} を消せない: {e})")
        return False, f"git の {kind} が {GIT_STEP_TIMEOUT} 秒で終わらず、子孫ごと打ち切った"
    tail = ((r.stdout or "") + (r.stderr or "")).strip()[-300:]
    return r.returncode == 0, tail


def git_step_main(kind: str, args: list[str]) -> int:
    """--git-step の子プロセスの本体(git_step から呼ばれる)。"""
    if kind == "checkout" and len(args) == 1:
        return 0 if checkout_edition_branch(args[0], JOB) else 1
    if kind == "commit" and len(args) == 3:
        date, rel, message = args
        return 0 if commit_and_push(f"edition/{date}", message, JOB, paths=[rel]) else 1
    say(f"--git-step の引数が違う: {kind} {args}")
    return 2


def _head() -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _committed(head: str, rel: Path, text: str) -> bool:
    """head の後に、rel を text にした commit が積まれたか。"""
    now = _head()
    if not now or now == head:
        return False
    r = subprocess.run(["git", "show", f"HEAD:{rel.as_posix()}"], cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0 and r.stdout == text


def _restore(rel: Path) -> None:
    """rel の index と作業ツリーを HEAD に戻す。"""
    subprocess.run(["git", "reset", "-q", "--", str(rel)], cwd=ROOT, capture_output=True)
    subprocess.run(["git", "checkout", "--", str(rel)], cwd=ROOT, capture_output=True)


# ---------------------------------------------------------------- 素材

def fmt_adv(a: dict) -> str:
    c = a["cvss"]
    score = f"{c['score']}({c.get('severity') or ''} {c.get('vector') or ''} / {c.get('source')})" if c.get("score") is not None else "不明"
    lines = [f"### {a['id']}" + (f"({', '.join(a['aliases'])})" if a["aliases"] else ""),
             f"- 要約: {a['summary'] or '(なし)'}", f"- CVSS: {score}", f"- 公開: {a['published'][:10]} / 更新: {a['modified'][:10]}"]
    for p in a["packages"]:
        lines.append(f"- 当たる: {p['ecosystem']} {p['name']} {p['installed']}(直った版: {', '.join(p['fixed']) or 'なし'})")
        lines += [f"  - 影響を受ける関数: {s}" for s in p["symbols"]]
    if "fixed_by_latest" in a:
        lines.append(f"- 最新の版で: {'直る' if a['fixed_by_latest'] else '直らない'}")
    lines += [f"- 参照: {u}" for u in a["refs"]]
    lines += ["- 詳細:", "", a["details"].strip() or "(なし)", ""]
    return "\n".join(lines)


def write_materials(work: Path, component: str, head: list[str], advs: list[dict]) -> Path:
    p = work / f"{component}-materials.md"
    p.write_text("\n".join(head) + "\n\n## 報告\n\n" + "\n".join(fmt_adv(a) for a in advs), encoding="utf-8")
    return p


def caddy_head(inv: dict, rel: dict, caddyfile: str) -> list[str]:
    return [f"# Caddy の評価の素材", "",
            f"- 今の版: Caddy {inv['version']}(Go {inv['go']})。実行ファイル {CADDY_BIN}",
            f"- 最新の版: {rel['version']}({rel['published'][:10]} 公開。{rel['url']})",
            f"- 設定: {CADDYFILE_LIVE}(全文は下)", f"- 起動: ~/.config/systemd/user/{WEB_UNIT}",
            f"- 前段: Cloudflare Tunnel(cloudflared)から 127.0.0.1:8080 へ。転送先のアプリは {SRV / 'app' / 'app.py'}",
            "", "## Caddyfile", "", "```", caddyfile.rstrip(), "```",
            "", f"## 最新の版のリリースノート({rel['version']})", "", rel["notes"].strip()[:12000]]


def webapp_head(pkgs: dict[str, str], req_text: str, latest: dict[str, dict]) -> list[str]:
    rows = [f"- {n} {v}(最新 {latest.get(n, {}).get('latest') or '不明'}"
            + (f"・変更履歴 {latest[n]['notes_url']}" if latest.get(n, {}).get("notes_url") else "") + ")" for n, v in sorted(pkgs.items())]
    return ["# webapp の Python の依存の評価の素材", "",
            f"- 動かし方: {APP_PY} で {SRV / 'app' / 'app.py'}(Flask を waitress で配る。~/.config/systemd/user/{APP_UNIT})",
            "- 前段: Cloudflare Tunnel → Caddy(127.0.0.1:8080)→ アプリ(127.0.0.1:8081)",
            f"- 依存の指定: {ROOT / REQUIREMENTS_REPO}(全文は下)。配信の deploy はこれを入れる",
            "", "## requirements.txt", "", "```", req_text.rstrip(), "```", "", "## 入っているパッケージ", "", *rows]


# ---------------------------------------------------------------- 本体

def new_versions(state: dict, items: list[tuple[str, str, str, str]]) -> list[str]:
    """(鍵, 表示名, 今の版, 最新の版, …) のうち、まだ知らせていない新しい版の行。知らせた版を state に残す。"""
    out = []
    notified = state.setdefault("notified", {})
    for key, label, cur, latest, notes in items:
        if latest and cur and vkey(latest) > vkey(cur) and notified.get(key) != latest:
            out.append(f"- {label} {cur} → {latest}" + (f"({notes})" if notes else ""))
            notified[key] = latest
    return out


def report(component: str, verdict: dict, lines: list[str], ok: bool) -> None:
    def who(name, a):
        if not a:
            return f"{name}: 答えなし"
        return (f"{name}: {'重大' if a.get('serious') else '重大でない'}・{'上げる ' + ', '.join(a['targets']) if a['verdict'] == 'apply' else '上げない'}"
                f"\n  根拠: {a.get('why')}\n  攻撃の成否: {str(a.get('exposure'))[:400]}"
                f"\n  上げる危険: {str(a.get('update_risk'))[:200]} / 置く危険: {str(a.get('leave_risk'))[:200]}")
    head = {"apply": "合意: 上げる", "hold": "合意: 上げない", "split": "割れた(合意がないので上げない)",
            "noanswer": "答えが揃わない(上げない)"}[verdict["result"]]
    text = (f"{component} の脆弱性の判断 — {head}({verdict['rounds']}巡)\n" + who("Sol", verdict["sol"]) + "\n" + who("Opus", verdict["opus"])
            + ("\n" + "\n".join(lines) if lines else ""))
    notify(JOB, text, ok=ok)


def handle(component: str, advs: list[dict], candidates: dict[str, list[str]], materials: Path, work: Path, date: str,
           state: dict, apply_fn, barrier_fn) -> None:
    """1つの部品(caddy / webapp)の合議 → 上げる → 確認 → 障壁 → 上げ直し → 知らせる。"""
    key = judge_key([a["id"] for a in advs], candidates)
    prev = (state.get("judged") or {}).get(component) or {}
    if stopped(state, component):
        return
    if prev.get("key") == key and prev.get("result") in ("applied", "hold", "split"):
        say(f"{component}: 同じ報告と候補の組は判定済み({prev.get('at')} {prev.get('result')})")
        return
    say(f"{component}: 報告 {len(advs)} 件・候補 {candidates} を合議にかける")
    v = council(component, materials, candidates, work, date)
    lines: list[str] = []
    result = v["result"]

    def record(res: str) -> None:
        state.setdefault("judged", {})[component] = {"key": key, "result": res, "at": now_jst().isoformat(timespec="seconds"),
                                                     "ids": sorted(a["id"] for a in advs)}
        save_state(state)

    def swap(fn, proposal) -> dict:
        # 入れ替えと、戻しきれなかったときの止めた印の記録までを、停止の要求を保留する区間にする(記録の前に止まると、
        # 次の回が止めた印を知らずに上げ直す。Sol 監査 r3)
        with critical():
            r = guarded(fn, targets, proposal)
            if broken(r):
                record("broken")
            return r
    if result == "apply":
        targets = v["sol"]["targets"]
        lines.append(f"更新: {', '.join(targets)}")
        r = swap(apply_fn, None)
        lines += [f"- {s}" for s in r["steps"]]
        if broken(r):
            # 戻しきれていない: ここで止めて人に渡す。障壁を外して上げ直すと、壊れた今の状態を旧版の写しに上書きしかねない(Sol 監査 r1)
            lines.append("🚨 戻した後も確認が通らない。障壁の検討・上げ直しはせずに止めた(手で戻す)")
        elif not r["ok"] and not TERM_PENDING:
            b = barrier_council(component, targets, r["failure"], materials, work, date)
            lines.append(barrier_text(b))
            if b["agreed"] and b["proposal"]["lift"]:
                r = swap(barrier_fn, b["proposal"])
                lines += [f"- {s}" for s in r["steps"]]
                if broken(r):
                    lines.append("🚨 上げ直しの後に戻しきれない。止めた(手で戻す)")
        result = "applied" if r["ok"] else ("broken" if broken(r) else "failed")
        if not r["ok"]:
            lines.append(f"失敗の記録: {r['failure'][:600]}")
    with critical():
        record(result)
        report(component, {**v, "result": v["result"]}, lines, ok=result in ("applied", "hold", "split"))
    if TERM_PENDING:
        raise Terminated("停止の要求(SIGTERM)を受けていた。結果を記録して知らせたので止める")


class Terminated(Exception):
    """systemd の停止(時間制限の SIGTERM)。Exception にして、入れ替えの途中なら戻しの経路へ進ませる(Sol 監査 r2)。"""


TERM_PENDING = False      # 停止の要求を受けたか(入れ替えの区間では保留し、記録と通知を済ませてから止まる)
_CRITICAL = 0             # 入れ替え〜戻しの区間の深さ


def on_sigterm(signum, frame):
    """停止の要求: 入れ替えの区間の外なら Terminated で止める。区間の中なら保留し、区間が戻しまで済ませた後、
    handle が止めた印・結果を記録して知らせてから止める(Sol 監査 r3)。"""
    global TERM_PENDING
    TERM_PENDING = True
    if _CRITICAL == 0:
        raise Terminated("SIGTERM を受けた(unit の時間制限か停止)")


@contextlib.contextmanager
def critical():
    global _CRITICAL
    _CRITICAL += 1
    try:
        yield
    finally:
        _CRITICAL -= 1


def swap_budget_ok(now: float | None = None) -> tuple[bool, str]:
    """入れ替えを始めてよいか: 発行の時間帯の待ち+入れ替え・確認+戻しの分が、この回の残り時間(RUN_DEADLINE まで)に収まるか。"""
    if RUN_DEADLINE is None:
        return True, ""
    left = RUN_DEADLINE - (now if now is not None else time.time())
    need = quiet_wait_secs(now_jst(), QUIET_MARGIN) + 2 * QUIET_MARGIN
    if left < need:
        return False, f"この回の残り {int(left)} 秒では、入れ替えと戻し(見込み {int(need)} 秒)が終わらない"
    return True, ""


GUARD_TRIES = 3


def guarded(fn, targets, proposal) -> dict:
    """入れ替え(apply_fn / barrier_fn)の入口。発行の時間帯の待ちはここだけで、ロックの外で行う(Sol 監査 r5)。待った後、停止の要求と
    残り時間を見て、足りるときだけ fn を呼ぶ。fn が準備(pip の解決・ロック待ち)のうちに時間帯に近づいて何もせずに返したら(retry)、
    ここで待ってからやり直す(GUARD_TRIES 回まで)。足りなければ何も入れ替えずに失敗として返す(次の回にやり直す)。"""
    r: dict = {}
    for _ in range(GUARD_TRIES):
        # 待つ前に、待ち・入れ替え・戻しが残り時間に収まるかを見る(収まらないのに何時間も待たない)
        ok, why = swap_budget_ok()
        if not ok:
            return not_started(f"入れ替えを始めない: {why}")
        wait_quiet(margin=QUIET_MARGIN)
        if TERM_PENDING:
            return not_started("停止の要求(SIGTERM)を受けているので入れ替えを始めない")
        ok, why = swap_budget_ok()
        if not ok:
            return not_started(f"入れ替えを始めない: {why}")
        r = fn(targets, proposal)
        if not r.get("retry"):
            return r
    return r


def stopped(state: dict, component: str) -> bool:
    """前の回に戻しきれずに止めた部品か。止めていれば知らせて True。人が手で戻して state の judged.<部品> を消すまで、
    候補選び・判定・更新・「直す版なし」の記録のどれもしない(止めた印を上書きしない。Sol 監査 r2)。"""
    prev = (state.get("judged") or {}).get(component) or {}
    if prev.get("result") != "broken":
        return False
    notify(JOB, f"{component}: {prev.get('at')} の更新で戻しきれずに止めたまま。手で戻したら {STATE} の judged.{component} を消す"
                "(消すまで判定も更新もしない)", ok=False)
    return True


def broken(r: dict) -> bool:
    """上げるのに失敗し、戻しても確認が通らない(または戻せない)状態か。"""
    return not r["ok"] and bool(r.get("rolled_back")) and not r.get("restored", True)


def barrier_text(b: dict) -> str:
    p, rv = b.get("proposal"), b.get("review")
    if not p:
        return f"障壁の検討: Sol の案が出ない({b['rounds']}往復)。外さない"
    head = ("合意: 外す" if p["lift"] else "合意: 外さない") if b["agreed"] else "合意できず: 外さない"
    edits = (", ".join(f"{e['name']}{e['spec']}" for e in p["requirements"]) if p["requirements"]
             else ("Caddyfile を書き換える" if p["caddyfile"] else "なし"))
    return (f"障壁の検討 — {head}({b['rounds']}往復)\n  障壁: {p['barrier']}\n  外した影響: {p['impact']}\n  外し方: {edits}\n"
            f"  Sol: {p['why']}\n  Opus: {'承認' if rv and rv.get('approve') else '差し戻し'} {rv.get('why') if rv else '(答えなし)'}"
            + (f"\n  指摘: {'; '.join(rv.get('objections') or [])}" if rv and rv.get("objections") else ""))


def run(check: bool) -> int:
    date = now_jst().strftime("%Y-%m-%d")
    work = WORK_ROOT / now_jst().strftime("%Y%m%d-%H%M%S")
    work.mkdir(parents=True, exist_ok=True)
    state = load_state() if not check else {}
    # ---- 今の版と最新の版
    cinv = caddy_inventory()
    rel = caddy_latest()
    pkgs = python_inventory()
    latest = {n: pypi_latest(n) for n in pkgs}
    req_text = (ROOT / REQUIREMENTS_REPO).read_text(encoding="utf-8")
    # ---- 脆弱性
    caddy_hits = osv_ids(caddy_queries(cinv))
    py_hits = osv_ids([("PyPI", n, v) for n, v in sorted(pkgs.items())])
    cadv = advisories(caddy_hits, "caddy", state)
    padv = advisories(py_hits, "webapp", state)
    say(f"Caddy: 今 {cinv['version']}(Go {cinv['go']})/ 最新 {rel['version']} / 当たる報告 {len(cadv)} 件")
    for n, v in sorted(pkgs.items()):
        lt = latest[n].get("latest") or "?"
        if lt != v:
            say(f"  {n}: 今 {v} / 最新 {lt}")
    say(f"webapp: 当たる報告 {len(padv)} 件" + (f"({', '.join(sorted(padv))})" if padv else ""))
    if check:
        for a in list(cadv.values()) + list(padv.values()):
            c = a["cvss"]
            say(f"  {a['id']} {c.get('score') or '-'} {a['summary'][:80]} ← "
                + ", ".join(f"{p['name']} {p['installed']}" for p in a["packages"]))
        return 0
    # ---- 通常の更新(新しい版)を知らせる
    news = new_versions(state, [("caddy", "Caddy", cinv["version"], rel["version"], rel["url"])]
                        + [(f"pypi:{n.lower()}", n, v, latest[n].get("latest") or "", latest[n].get("notes_url") or
                            f"https://pypi.org/project/{n}/{latest[n].get('latest')}/") for n, v in sorted(pkgs.items())])
    if news:
        notify(JOB, "配信サーバーの新しい版(脆弱性の判断は別に知らせる):\n" + "\n".join(news))
    save_state(state)
    # ---- 前の回に戻しきれずに止めた部品は、候補選び・判定・更新・「直す版なし」の記録のどれもしない(止めた印を残す。Sol 監査 r2)
    halted = {c for c in ("caddy", "webapp") if stopped(state, c)}
    # ---- Caddy の脆弱性
    if cadv and "caddy" not in halted:
        handle_caddy(cinv, rel, cadv, work, date, state)
    # ---- webapp の脆弱性
    if padv and "webapp" not in halted:
        handle_webapp(pkgs, latest, req_text, padv, work, date, state)
    return 0


def handle_caddy(cinv: dict, rel: dict, cadv: dict, work: Path, date: str, state: dict) -> None:
    candidates: dict[str, list[str]] = {}
    new_bin = None
    if vkey(rel["version"]) > vkey(cinv["version"]):
        try:
            new_bin = fetch_caddy(rel, work)
            still = osv_ids(caddy_queries(caddy_inventory(new_bin)))
            left = {i for ids in still.values() for i in ids}
            for a in cadv.values():
                a["fixed_by_latest"] = not ({a["id"], *a["aliases"]} & left)
            if any(a["fixed_by_latest"] for a in cadv.values()):
                candidates["caddy"] = [rel["version"]]
        except Exception as e:
            notify(JOB, f"Caddy {rel['version']} を取って確かめられない({type(e).__name__}: {e})。脆弱性 {len(cadv)} 件の判断は次の回に",
                   ok=False)
            return
    advs = sorted(cadv.values(), key=lambda a: a["id"])
    if not candidates:
        nofix(state, "caddy", advs, f"Caddy {cinv['version']} に当たる報告 {len(advs)} 件に、直す版がまだ無い(最新 {rel['version']})")
        return
    # 評価に使う Caddyfile の原本。障壁を外して上げ直すとき、枝の上の ops/caddy/Caddyfile がこれと同じかを確かめる
    caddyfile_seen = CADDYFILE_LIVE.read_text(encoding="utf-8")
    materials = write_materials(work, "caddy", caddy_head(cinv, rel, caddyfile_seen), advs)

    def apply_fn(targets, _):
        return apply_caddy(new_bin, rel["version"], cinv["version"])

    def barrier_fn(targets, proposal):
        return change_on_branch(CADDYFILE_REPO, caddyfile_seen, proposal["caddyfile"],
                                f"Caddyfile: Caddy {rel['version']} で通るように書き換える(配信サーバーの更新の合議)",
                                lambda: apply_caddy(new_bin, rel["version"], cinv["version"], caddyfile_text=proposal["caddyfile"]))

    handle("caddy", advs, candidates, materials, work, date, state, apply_fn, barrier_fn)


def handle_webapp(pkgs: dict, latest: dict, req_text: str, padv: dict, work: Path, date: str, state: dict) -> None:
    candidates: dict[str, list[str]] = {}
    by_pkg: dict[str, list[dict]] = {}
    for a in padv.values():
        for p in a["packages"]:
            by_pkg.setdefault(p["name"], []).append(p)
    for name, ps in by_pkg.items():
        need = []
        for p in ps:
            fixes = [f for f in p["fixed"] if vkey(f) > vkey(p["installed"])]
            if fixes:
                need.append(min(fixes, key=vkey))
        lt = latest.get(name, {}).get("latest") or ""
        cands = set()
        if need:
            n = max(need, key=vkey)
            rels = latest.get(name, {}).get("releases") or []
            cands.add(n if n in rels or not rels else next((r for r in rels if vkey(r) >= vkey(n)), n))
            if lt and vkey(lt) >= vkey(n):
                cands.add(lt)
        if cands:
            candidates[name] = sorted(cands, key=vkey)
    advs = sorted(padv.values(), key=lambda a: a["id"])
    if not candidates:
        nofix(state, "webapp", advs, f"webapp の依存に当たる報告 {len(advs)} 件に、直す版がまだ無い")
        return
    materials = write_materials(work, "webapp", webapp_head(pkgs, req_text, latest), advs)

    def apply_fn(targets, _):
        # 書き換えない通常の上げ方も、作業ツリーのロックの中で requirements.txt が評価のときのままかを確かめてから入れる
        # (発行の deploy と重ならず、待つあいだに deploy が入れた依存を古い全文で戻さない。Sol 監査 r9)
        return change_on_branch(REQUIREMENTS_REPO, req_text, req_text, "", lambda: apply_webapp(targets, req_text, work))

    def barrier_fn(targets, proposal):
        new_req = edit_requirements(req_text, proposal["requirements"])
        return change_on_branch(REQUIREMENTS_REPO, req_text, new_req,
                                "webapp の依存: " + ", ".join(f"{e['name']}{e['spec']}" for e in proposal["requirements"])
                                + "(セキュリティの更新を妨げる固定を外す。配信サーバーの更新の合議)",
                                lambda: apply_webapp(targets, new_req, work))

    handle("webapp", advs, candidates, materials, work, date, state, apply_fn, barrier_fn)


def nofix(state: dict, component: str, advs: list[dict], head: str) -> None:
    """直す版が無い報告は合議にかけず、同じ組を1回だけ知らせる。"""
    key = judge_key([a["id"] for a in advs], {})
    prev = (state.get("judged") or {}).get(component) or {}
    if prev.get("key") == key:
        return
    rows = [f"- {a['id']} CVSS {a['cvss'].get('score') or '不明'}: {a['summary'][:100]}" for a in advs[:30]]
    notify(JOB, head + "\n" + "\n".join(rows) + (f"\n- ほか {len(advs) - 30} 件" if len(advs) > 30 else ""))
    state.setdefault("judged", {})[component] = {"key": key, "result": "nofix", "at": now_jst().isoformat(timespec="seconds"),
                                                 "ids": sorted(a["id"] for a in advs)}
    save_state(state)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="調べて表示するだけ(通知・合議・更新をしない)")
    ap.add_argument("--git-step", nargs="+", help=argparse.SUPPRESS)      # git_step が子プロセスとして呼ぶ
    args = ap.parse_args()
    if args.git_step:
        return git_step_main(args.git_step[0], args.git_step[1:])
    global RUN_DEADLINE
    if not args.check:
        RUN_DEADLINE = time.time() + RUN_BUDGET
        signal.signal(signal.SIGTERM, on_sigterm)
    try:
        return run(args.check)
    except CliBlocked as e:
        notify(JOB, f"モデルの CLI が使えず、配信サーバーの判断をやめた({e})。次の回にやり直す", ok=False, ledger=False)
        return 1
    except Exception as e:
        if args.check:
            raise
        notify_crash(JOB, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
