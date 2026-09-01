#!/usr/bin/env python3
"""classify_sources: 判定表に無い出典を、**2つのモデルの合議**で振り分ける。

  python3 scripts/classify_sources.py [--date YYYY-MM-DD] [--apply]

`source_types.yml` に無いドメインは `未確認` になる。既定を未確認にしたのは
「多いから当事者」という推測で強い種別を名乗らせないためだが、そのままだと
表を人が育てるまで紙面のバッジが弱いまま残る(実測: 会場・チケット販売・自治体が
未確認のまま記事に載っていた)。

そこで、未知のドメインを**別ベンダーの2モデルに独立して分類させ、一致したものだけ**
表へ足す。片方でも違えば足さず、未確認のまま人へ回す。

**公式・準公式は自動で足さない。**この2つは「アイマス公式である」「公式の
グループ企業である」という強い主張で、外から見て確かめようがない。
過大表示はこの製品がいちばん避けたい事故なので、機械の合議では名乗らせない。

プラットフォーム(x.com / youtube.com / note.com など、多数の利用者が同居する場)は
ドメインで決まらないので対象外。1件ずつアカウントや ID で決める。
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import (ENV, ROOT, COLLECT_MODEL, EXPLORE_MODEL, classify_source,
                     edition_date, extract_json_array, html_to_text, notify, set_quiet)

# 合議で足してよい種別。公式・準公式は入れない(上の docstring を参照)
ALLOWED = ("当事者", "演者", "報道", "二次情報", "ファン")
# ドメインの一覧は種別ごと。演者はドメインを持たない(X アカウントで表す)ので入れない
LIST_OF = {"当事者": "party_domains", "報道": "press_domains",
           "二次情報": "secondary_domains", "ファン": "fan_domains"}
# ドメインでは決まらない場(アカウント・動画IDで決まる)。対象外
# **公式の主体がページを持ちうる場**だけを外す。そこはドメインでは決まらない。
# wiki ホスティング(atwiki 等)は外さない。公式がそこに告知を出すことはなく、
# どのページも利用者が書いた二次情報なので、ドメインで決められる(表に載せてある)
PLATFORMS = ("x.com", "twitter.com", "youtube.com", "youtu.be", "nicovideo.jp",
             "note.com", "docs.google.com", "forms.gle", "hatenablog.com",
             "ameblo.jp", "fanbox.cc", "booth.pm", "github.com", "rakuten.co.jp")
UA = "Mozilla/5.0 (compatible; ImasNews/1.0)"

RULES = """種別の定義(この新聞の編集規程2.5)。**この定義だけで判断すること。**

- 当事者: 主催者・販売元・会場・自治体・コラボ先など、催しや商品を**動かしている側**の一次発信
- 演者: **その作品を演じている人**とその所属事務所。声優本人の出演報告、事務所の公式アカウントなど
- 報道: 独立した報道メディア。自ら取材して記事を出している
- 二次情報: 攻略サイト・まとめサイト等、**他所の発表を写して伝えている**。法人運営でも個人運営でも同じ
- ファン: ファン・サークルが**自分の営みを自分で発信している**もの

判断に迷ったら `不明` と答える。**推測で埋めない。**

**アイマス公式そのもの、または公式レーベル・公式ストア・グループ企業に当たると思うものは、
`当事者` に寄せず `不明` と答えること。**この工程は「公式」「準公式」を扱わない。
弱い種別を無理に当てると、実態より弱いバッジが紙面に残る。人の判断へ回す。"""


def unknown_targets(date: str) -> tuple[dict[str, str], dict[str, tuple[str, list[str]]]]:
    """その号の候補から、判定表に無い**ドメイン**と**Xアカウント**を拾う。

    X はドメインでは決まらないが、アカウント単位なら決まる。
    大半はファンの投稿(イラスト・コスプレ・感想)で、1つずつ人が見るには多すぎる
    (実測: 178件が未分類のまま溜まっていた)。ここも合議に掛ける。
    """
    p = ROOT / "candidates" / f"{date}.json"
    if not p.exists():
        return {}, {}
    doms: dict[str, str] = {}
    accts: dict[str, tuple[str, list[str]]] = {}
    for c in json.loads(p.read_text(encoding="utf-8")):
        url = c.get("url") or ""
        if not url or classify_source(url) != "未確認":
            continue
        u = urllib.parse.urlparse(url)
        host = (u.hostname or "").removeprefix("www.")
        if host in ("x.com", "twitter.com"):
            seg = [s for s in u.path.split("/") if s]
            if not seg or seg[0].lower() == "i":
                continue
            a = seg[0]
            cur = accts.setdefault(a, (url, []))
            if c.get("title") and len(cur[1]) < 4:
                cur[1].append(c["title"][:70])
            continue
        if not host or any(host == q or host.endswith("." + q) for q in PLATFORMS):
            continue
        doms.setdefault(host, url)
    return doms, accts


def page_excerpt(url: str, chars: int = 1200) -> str:
    """判断材料としてページ本文の冒頭を取る。取れなくても続ける。"""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = html_to_text(r.read(200_000), r.headers.get_content_charset())
        return re.sub(r"\s+", " ", text).strip()[:chars]
    except Exception as e:
        return f"(取得できず: {type(e).__name__})"


def ask(cmd: list[str], prompt: str, timeout: int = 900) -> list[dict]:
    try:
        r = subprocess.run(cmd + [prompt], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL, cwd=ROOT)
        return extract_json_array(r.stdout) or []
    except Exception as e:
        print(f"分類の呼び出しに失敗({cmd[0]}): {e}", flush=True)
        return []


def build_prompt(items: list[tuple[str, str, str]]) -> str:
    body = "\n\n".join(f"### {h}\n代表URL: {u}\nページ冒頭: {x}" for h, u, x in items)
    return (f"""次のサイトを、この新聞の出典種別に分類してください。

{RULES}

## 対象
{body}

## 出力
**JSON 配列だけ**を出力してください。ほかの文字は書かないこと。
[{{"host": "ドメイン", "type": "当事者|報道|二次情報|ファン|不明", "why": "40字以内の根拠"}}]

- **ページ冒頭に書いてあることだけ**で判断する。知識で補わない
- 少しでも迷うなら `不明`。**推測で埋めない**
""")


def build_x_prompt(accts: dict[str, tuple[str, list[str]]]) -> str:
    body = "\n".join(f"- @{a}: " + " / ".join(t or ["(投稿の要約なし)"])
                     for a, (_, t) in sorted(accts.items()))
    return f"""次の X アカウントを、この新聞の出典種別に分類してください。

{RULES}

X のアカウントは次のどれかに当たることが多いので、目安にしてください。

- **演者**: 出演者(声優)本人、その所属事務所
- **当事者**: 店舗・会場・コラボ先・イベント主催・販売元の公式アカウント、
  作曲家やイラストレーターなど制作に関わった人
- **ファン**: ファンアート、コスプレ、感想、二次創作、応援の投稿をしている個人
- **報道**: ニュースメディアのアカウント
- **二次情報**: まとめ・引用中心のアカウント

## 対象(アカウントと、そこから拾った投稿の要約)
{body}

## 出力
**JSON 配列だけ**を出力してください。ほかの文字は書かないこと。
[{{"host": "@なしのアカウント名", "type": "当事者|演者|報道|二次情報|ファン|不明", "why": "40字以内の根拠"}}]

- **投稿の要約から読み取れることだけ**で判断する。知識で補わない
- 少しでも迷うなら `不明`。**推測で埋めない**
"""


def consensus(prompt: str, keys: list[str]) -> tuple[dict, list[str]]:
    """別ベンダーの2モデルに独立して答えさせ、一致したものだけ返す。

    同じベンダーだと同じ誤りを共有するので、Claude と Codex に分ける。
    """
    a = {str(d.get("host", "")).lstrip("@"): d for d in ask(
        ["claude", "-p", "--model", COLLECT_MODEL, "--dangerously-skip-permissions"], prompt)
        if isinstance(d, dict)}
    b = {str(d.get("host", "")).lstrip("@"): d for d in ask(
        ["codex", "exec", "-m", EXPLORE_MODEL, "-s", "read-only", "--skip-git-repo-check"], prompt)
        if isinstance(d, dict)}
    agreed, split = {}, []
    for k in keys:
        ta, tb = (a.get(k) or {}).get("type"), (b.get(k) or {}).get("type")
        if ta and ta == tb and ta in ALLOWED:
            agreed[k] = (ta, str((a.get(k) or {}).get("why", ""))[:40])
        else:
            split.append(f"{k}: {ta or '—'} / {tb or '—'}")
    return agreed, split


def add_domains(agreed: dict) -> None:
    p = ROOT / "source_types.yml"
    text = p.read_text(encoding="utf-8")
    for h, (t, why) in agreed.items():
        m = re.search(rf"^{LIST_OF[t]}:\n", text, re.M)
        if not m:
            print(f"  ★{LIST_OF[t]} が表に無いので {h} を足せない")
            continue
        text = text[:m.end()] + f"  - {h}{' ' * max(1, 22 - len(h))}# 合議で追加: {why}\n" + text[m.end():]
    p.write_text(text, encoding="utf-8")


def add_x_accounts(agreed: dict) -> None:
    """x_accounts の該当種別の下へ足す。無ければその種別の節を作る。"""
    p = ROOT / "source_types.yml"
    text = p.read_text(encoding="utf-8")
    for a, (t, why) in agreed.items():
        m = re.search(rf"^  {t}:\n", text, re.M)
        if m:
            text = text[:m.end()] + f"    - {a}{' ' * max(1, 20 - len(a))}# 合議で追加: {why}\n" + text[m.end():]
        else:  # その種別の節がまだ無い
            mx = re.search(r"^x_accounts:\n", text, re.M)
            if not mx:
                print(f"  ★x_accounts が表に無いので @{a} を足せない")
                continue
            text = (text[:mx.end()] + f"  {t}:\n"
                    f"    - {a}{' ' * max(1, 20 - len(a))}# 合議で追加: {why}\n" + text[mx.end():])
    p.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--apply", action="store_true", help="一致したものを source_types.yml に足す")
    ap.add_argument("--limit", type=int, default=60,
                    help="1回に掛ける X アカウントの数(多いと1回のプロンプトに載らない)")
    args = ap.parse_args()
    # 下見(--apply なし)では通知しない。試験実行が本物の警報と混ざる
    set_quiet(not args.apply)
    date = args.date or edition_date()

    doms, accts = unknown_targets(date)
    if not doms and not accts:
        print(f"{date}: 判定表に無い出典はありません")
        return 0

    split_all = []
    if doms:
        print(f"{date}: 判定表に無いドメイン {len(doms)}件 → {', '.join(sorted(doms))}", flush=True)
        items = [(h, u, page_excerpt(u)) for h, u in sorted(doms.items())]
        agreed, split = consensus(build_prompt(items), sorted(doms))
        for h, (t, why) in agreed.items():
            print(f"  一致 {t}\t{h}\t{why}")
        for s in split:
            print(f"  不一致・保留\t{s}")
        split_all += split
        if agreed and args.apply:
            add_domains(agreed)
            print(f"  → ドメイン {len(agreed)}件を表に追加")

    if accts:
        # 多いと1回のプロンプトに載らないので、出現数の多い順に区切って掛ける
        order = sorted(accts, key=lambda a: (-len(accts[a][1]), a))[:args.limit]
        sub = {a: accts[a] for a in order}
        print(f"\n{date}: 判定表に無い X アカウント {len(accts)}件"
              f"(今回 {len(sub)}件を処理)", flush=True)
        agreed, split = consensus(build_x_prompt(sub), sorted(sub))
        for a, (t, why) in agreed.items():
            print(f"  一致 {t}\t@{a}\t{why}")
        for s in split:
            print(f"  不一致・保留\t@{s}")
        split_all += split
        if agreed and args.apply:
            add_x_accounts(agreed)
            print(f"  → X アカウント {len(agreed)}件を表に追加")

    if split_all:
        notify("collect", f"{date}: 出典の種別が合議で決まらなかったものがあります"
                          f"(人の判断が要る):\n- " + "\n- ".join(split_all[:8]), ok=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
