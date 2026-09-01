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
ALLOWED = ("当事者", "報道", "二次情報", "ファン")
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

- 当事者: 主催者・販売元・会場・自治体・コラボ先など、その催しや商品の**当事者による一次発信**
- 報道: 独立した報道メディア。自ら取材して記事を出している
- 二次情報: 攻略サイト・まとめサイト等、**他所の発表を写して伝えている**。法人運営でも個人運営でも同じ
- ファン: ファン・サークルが**自分の営みを自分で発信している**もの

判断に迷ったら `不明` と答える。**推測で埋めない。**

**アイマス公式そのもの、または公式レーベル・公式ストア・グループ企業に当たると思うものは、
`当事者` に寄せず `不明` と答えること。**この工程は「公式」「準公式」を扱わない。
弱い種別を無理に当てると、実態より弱いバッジが紙面に残る。人の判断へ回す。"""


def unknown_hosts(date: str) -> dict[str, str]:
    """その号の候補から、判定表に無いドメインを拾う。{host: 代表URL}"""
    p = ROOT / "candidates" / f"{date}.json"
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for c in json.loads(p.read_text(encoding="utf-8")):
        url = c.get("url") or ""
        if not url or classify_source(url) != "未確認":
            continue
        host = (urllib.parse.urlparse(url).hostname or "").removeprefix("www.")
        if not host or any(host == q or host.endswith("." + q) for q in PLATFORMS):
            continue
        out.setdefault(host, url)
    return out


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--apply", action="store_true", help="一致したものを source_types.yml に足す")
    args = ap.parse_args()
    # 下見(--apply なし)では通知しない。試験実行が本物の警報と混ざる
    set_quiet(not args.apply)
    date = args.date or edition_date()

    hosts = unknown_hosts(date)
    if not hosts:
        print(f"{date}: 判定表に無い出典はありません")
        return 0
    print(f"{date}: 判定表に無い出典 {len(hosts)}件 → {', '.join(sorted(hosts))}", flush=True)

    items = [(h, u, page_excerpt(u)) for h, u in sorted(hosts.items())]
    prompt = build_prompt(items)

    # **別ベンダーの2モデルに独立して答えさせる。**同じベンダーだと同じ誤りを共有する
    a = {d.get("host"): d for d in ask(
        ["claude", "-p", "--model", COLLECT_MODEL, "--dangerously-skip-permissions"], prompt)
        if isinstance(d, dict)}
    b = {d.get("host"): d for d in ask(
        ["codex", "exec", "-m", EXPLORE_MODEL, "-s", "read-only", "--skip-git-repo-check"], prompt)
        if isinstance(d, dict)}

    agreed, split = {}, []
    for h in sorted(hosts):
        ta, tb = (a.get(h) or {}).get("type"), (b.get(h) or {}).get("type")
        if ta and ta == tb and ta in ALLOWED:
            agreed[h] = (ta, (a.get(h) or {}).get("why", "")[:40])
        else:
            split.append(f"{h}: {ta or '—'} / {tb or '—'}")

    for h, (t, why) in agreed.items():
        print(f"  一致 {t}\t{h}\t{why}")
    for s in split:
        print(f"  不一致・保留\t{s}")

    if agreed and args.apply:
        p = ROOT / "source_types.yml"
        text = p.read_text(encoding="utf-8")
        for h, (t, why) in agreed.items():
            key = LIST_OF[t]
            m = re.search(rf"^{key}:\n", text, re.M)
            if not m:
                print(f"  ★{key} が表に無いので {h} を足せない")
                continue
            text = text[:m.end()] + f"  - {h}{' ' * max(1, 22 - len(h))}# 合議で追加: {why}\n" + text[m.end():]
        p.write_text(text, encoding="utf-8")
        print(f"\nsource_types.yml に {len(agreed)}件を追加しました")

    if split:
        notify("collect", f"{date}: 出典の種別が合議で決まらなかったものがあります"
                          f"(人の判断が要る):\n- " + "\n- ".join(split[:8]), ok=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
