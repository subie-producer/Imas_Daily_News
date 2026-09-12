#!/usr/bin/env python3
"""classify_sources: 判定表に無い出典を、**2つのモデルの合議**で振り分ける。

  python3 scripts/classify_sources.py [--date YYYY-MM-DD] [--apply]

`source_types.yml` に無いドメインは `未確認` になる。既定を未確認にしたのは
「多いから当事者」という推測で強い種別を名乗らせないためだが、そのままだと
表を人が育てるまで紙面のバッジが弱いまま残る(実測: 会場・チケット販売・自治体が
未確認のまま記事に載っていた)。

そこで、未知のドメインを**別ベンダーの2モデルに独立して分類させ、一致したものだけ**
表へ足す。割れたら2巡目で相手の答えと根拠を見せて検証させ、賛成か反論かを答えさせる(議論)。
2巡目でも割れたものだけ、両方の言い分を付けて未確認のまま人へ回す。

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
                     source_type_table,
                     edition_date, extract_json_array, html_to_text, notify, set_quiet)

# 合議で足してよい種別。公式・準公式も答えさせる(作品・ブランドの公式アカウントを「不明」で人へ回して
# いたら、@idolmaster_en もジムシャニ公式も未確認のまま紙面に載った。編集長の指摘)。
# どの種別も2モデルの一致が要る。割れたら議論(consensus の2巡目)
ALLOWED = ("公式", "準公式", "当事者", "演者", "報道", "二次情報", "ファン")
# ドメインの一覧は種別ごと。演者はドメインを持たない(X アカウントで表す)ので入れない
LIST_OF = {"公式": "official_domains", "準公式": "semi_official_domains",
           "当事者": "party_domains", "報道": "press_domains",
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
- 報道: 独立した報道メディア。**自ら取材して**記事を出している
  (現地に行った、関係者に聞いた、独自に入手した、のいずれかの形跡があること)
- 二次情報: **媒体化したもの**。攻略サイト・まとめサイト・ファンコミュニティのサイト等、個人ではなく
  媒体として**他所の発表を写して伝えている**
- ファン: **その個人に紐づくものすべて**。個人のアカウント・ブログ・サークルの発信は、内容(創作・感想・
  目撃・公式情報の紹介)を問わずファン。**X の個人アカウントを二次情報にしない**(公式のキャンペーンを
  紹介しているだけの個人投稿もファン。記事のほうが紹介先の公式を出典にすればよい)

**報道と二次情報を分ける線は「自ら取材したか」だけである。**
体裁が整っていること、見出しや FAQ が並んでいること、考察が付いていることは、
取材した証拠にはならない。次に当たるものは、見た目が報道でも二次情報とする。

- 署名が「編集部」だけで、書き手も取材相手も分からない
- 中身が公式発表・プレスリリースの言い換えで、現地取材も関係者の言葉も無い
- 自動生成・AI 執筆をうかがわせる定型の構成が全記事に共通している

判断できないときだけ `不明` と答える。**根拠なく埋めない。**

## 「公式」「準公式」も答える

- 公式: **アイドルマスターの作品・ブランドそのものの本拠**。公式ポータル、作品公式サイト、
  ブランド・作品の公式アカウント(海外向け・言語別の公式アカウントも公式)。
  作品名・ブランド名を名乗り、告知を一次発信しているなら公式である
- 準公式: **バンダイナムコのグループ会社**(社名に Bandai Namco / バンダイナムコ)、
  **公式レーベル・公式ストア**(そのブランドの音源・商品を出す本体)、連載誌・連載サイト
  (作品を掲載している出版社の本体)

下に登録済みの一覧を渡す。一覧に無くても、上に当たるなら公式・準公式と答えること
(新しい作品公式サイト・新しい公式アカウントは必ず一覧の外から現れる)。
**アイマスの作品・ブランド・レーベルについて知っていることは使ってよい**
(例: @idolmaster_en はアイドルマスターの英語公式、「事務所のシャイニーカラーズ(ジムシャニ)」は
シャイニーカラーズの作品)。根拠に書くこと。

ライセンスを受けて商品を作る・売る会社は、公式にも準公式にも当たらない(下記)。

とくに間違えやすい点:

- **ライセンスを受けてグッズを作る・売る会社は「当事者」である。**準公式ではない。
  フィギュアメーカー、くじの運営、コラボカフェ、雑貨店、カラオケ事業者などは、
  公式の許諾を得ていても、自社サイトで自社の商品・催しを告知している当事者である
- 会場・チケット販売・自治体・イベント主催も同じく当事者
- 「公式ライセンス商品」「公式グッズ」という言葉がページにあっても、
  **その会社が公式になるわけではない**"""


def known_official() -> str:
    """公式・準公式として既に判定表に載っている相手の一覧。

    モデルに「公式かどうか」を推測させると、関係がありそうな会社を全部
    `不明` に倒す(実測: コトブキヤの自社イベント告知に対して
    「公式やグループ企業かどうか明確でない」と答えて保留になった)。
    公式・準公式は数えられる少数なので、**照合できる形で渡す**。
    """
    t = source_type_table()
    doms = sorted(set((t.get("official_domains") or []) + (t.get("semi_official_domains") or [])))
    sufs = sorted(t.get("official_suffixes") or [])
    paths = sorted(t.get("official_paths") or [])
    x = t.get("x_accounts") or {}
    accts = sorted(set((x.get("公式") or []) + (x.get("準公式") or [])))
    vids = sorted(set((t.get("video_ids") or {}).get("公式", [])
                      + (t.get("video_ids") or {}).get("準公式", [])))
    return ("### 公式・準公式として登録済み(照合用。同じ主体の別アカウント・別サイトは同じ種別)\n"
            + "ドメイン: " + ", ".join(doms) + "\n"
            + "配下も含むドメイン: " + ", ".join(sufs) + "\n"
            + "パス指定: " + ", ".join(paths) + "\n"
            + "X アカウント: " + ", ".join("@" + a for a in accts) + "\n"
            + "動画ID: " + ", ".join(vids))


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
    known = known_official()
    body = "\n\n".join(f"### {h}\n代表URL: {u}\nページ冒頭: {x}" for h, u, x in items)
    return (f"""次のサイトを、この新聞の出典種別に分類してください。

{RULES}

{known}

## 対象
{body}

## 出力
**JSON 配列だけ**を出力してください。ほかの文字は書かないこと。
[{{"host": "ドメイン", "type": "公式|準公式|当事者|報道|二次情報|ファン|不明", "why": "40字以内の根拠"}}]

- ページ冒頭に書いてあることと、**アイマスの作品・ブランド・レーベル・連載先について知っていること**で判断する
- 根拠を挙げられないときだけ `不明`
""")


def build_x_prompt(accts: dict[str, tuple[str, list[str]]]) -> str:
    known = known_official()
    body = "\n".join(f"- @{a}: " + " / ".join(t or ["(投稿の要約なし)"])
                     for a, (_, t) in sorted(accts.items()))
    return f"""次の X アカウントを、この新聞の出典種別に分類してください。

{RULES}

X のアカウントは次のどれかに当たることが多いので、目安にしてください。

- **公式**: アイドルマスターのブランド・作品の公式アカウント(海外向け・言語別も含む。
  例: @idolmaster_en。作品名を名乗り告知を一次発信している @jimushiny_oa のような作品公式も)
- **準公式**: 公式レーベル・公式ストア・バンダイナムコのグループ会社の公式アカウント
- **演者**: 出演者(声優)本人、その所属事務所
- **当事者**: 店舗・会場・コラボ先・イベント主催・販売元の公式アカウント、
  作曲家やイラストレーターなど制作に関わった人
- **ファン**: 個人のアカウント(ファンアート、コスプレ、感想、目撃、公式情報の紹介など内容を問わない)。
  **個人は二次情報にしない**
- **報道**: ニュースメディアのアカウント
- **二次情報**: 媒体化したもの(まとめサイト・ファンコミュニティ・攻略サイトのアカウント)

{known}

## 対象(アカウントと、そこから拾った投稿の要約)
{body}

## 出力
**JSON 配列だけ**を出力してください。ほかの文字は書かないこと。
[{{"host": "@なしのアカウント名", "type": "公式|準公式|当事者|演者|報道|二次情報|ファン|不明", "why": "40字以内の根拠"}}]

- アカウント名と投稿の要約、**アイマスの作品・ブランド・レーベル・出演者について知っていること**で判断する
  (X のプロフィールは機械から開けないので、要約と知識が材料の全てである)
- 根拠を挙げられないときだけ `不明`
"""


CMD_A = ["claude", "-p", "--model", COLLECT_MODEL, "--dangerously-skip-permissions"]
CMD_B = ["codex", "exec", "-m", EXPLORE_MODEL, "-s", "read-only", "--skip-git-repo-check"]


def _by_host(rows: list) -> dict[str, dict]:
    return {str(d.get("host", "")).lstrip("@"): d for d in rows if isinstance(d, dict)}


def debate_prompt(prompt: str, split_keys: list[str], mine: dict, theirs: dict) -> str:
    """2巡目: 割れた対象について、相手の答えと根拠を見せ、検証して賛成か反論かを答えさせる。"""
    rows = []
    for k in split_keys:
        m, t = mine.get(k) or {}, theirs.get(k) or {}
        rows.append(f"- {k}: あなた={m.get('type') or '—'}({m.get('why') or '根拠なし'}) / "
                    f"相手={t.get('type') or '—'}({t.get('why') or '根拠なし'})")
    return (prompt + "\n\n## 2巡目: 相手の答えと食い違った対象\n"
            "別のモデルが同じ材料で独立に答え、次の対象で食い違いました。**相手の根拠を読み、材料(ページ冒頭・"
            "投稿の要約)と知識で検証してから**答え直してください。\n"
            + "\n".join(rows) + "\n\n"
            "- 相手が正しいと分かったら相手の種別を答える(自分の1巡目に固執しない)\n"
            "- 相手が間違っていると分かったら自分の種別を答え、**相手の根拠のどこが違うか**を why に書く\n"
            "- 「不明」で済ませない。1巡目に不明と答えたなら、相手の根拠を材料に照らして賛成か反論かを決める。"
            "どうしても決められないなら why に「何が分かれば決められるか」を書く\n"
            "- 出力は1巡目と同じ JSON 配列(上の対象だけ)\n")


def consensus(prompt: str, keys: list[str]) -> tuple[dict, list[str]]:
    """別ベンダーの2モデルの合議。1巡目は独立に答え、割れたものは**議論**する(2巡目)。

    同じベンダーだと同じ誤りを共有するので、Claude と Codex に分ける。
    1巡目で割れた対象は、相手の答えと根拠を見せて検証させ、賛成か反論かを答えさせる。
    片方が「不明」と言い、もう片方が「これ」と言ったのに、不明側が検証もせず終わっていた
    (実測 2026-09-08〜12: @yuzu_yng 不明/ファン、@onkyodav 不明/当事者)。多数決や棄権扱いで
    誤魔化さず、答えた側の根拠に向き合わせる。2巡目でも割れたら両方の言い分を付けて人へ。
    """
    a = _by_host(ask(CMD_A, prompt))
    b = _by_host(ask(CMD_B, prompt))

    def agree(k):
        ta, tb = (a.get(k) or {}).get("type"), (b.get(k) or {}).get("type")
        return ta if (ta and ta == tb and ta in ALLOWED) else None

    split_keys = [k for k in keys if not agree(k)]
    if split_keys:
        print(f"  1巡目で割れた {len(split_keys)}件を議論させる: "
              + ", ".join(f"{k}({(a.get(k) or {}).get('type') or '—'}/{(b.get(k) or {}).get('type') or '—'})" for k in split_keys),
              flush=True)
        a2 = _by_host(ask(CMD_A, debate_prompt(prompt, split_keys, a, b)))
        b2 = _by_host(ask(CMD_B, debate_prompt(prompt, split_keys, b, a)))
        for k in split_keys:
            if k in a2:
                a[k] = a2[k]
            if k in b2:
                b[k] = b2[k]
    agreed, split = {}, []
    for k in keys:
        t = agree(k)
        if t:
            agreed[k] = (t, str((a.get(k) or {}).get("why", ""))[:40])
        else:
            da, db = a.get(k) or {}, b.get(k) or {}
            split.append(f"{k}: {da.get('type') or '—'}「{str(da.get('why') or '')[:60]}」 / "
                         f"{db.get('type') or '—'}「{str(db.get('why') or '')[:60]}」")
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
        mx = re.search(r"^x_accounts:\n", text, re.M)
        if not mx:
            print(f"  ★x_accounts が表に無いので @{a} を足せない")
            continue
        # 種別の節は **x_accounts の中**で探す(video_channels にも「公式:」があり、そちらに
        # 差し込むと X アカウントが動画チャンネル扱いになる)
        m = re.compile(rf"^  {t}:\n", re.M).search(text, mx.end())
        nxt = re.compile(r"^[a-z_]+:", re.M).search(text, mx.end())
        if m and (nxt is None or m.start() < nxt.start()):
            text = text[:m.end()] + f"    - {a}{' ' * max(1, 20 - len(a))}# 合議で追加: {why}\n" + text[m.end():]
        else:  # その種別の節がまだ無い
            text = (text[:mx.end()] + f"  {t}:\n"
                    f"    - {a}{' ' * max(1, 20 - len(a))}# 合議で追加: {why}\n" + text[mx.end():])
    p.write_text(text, encoding="utf-8")


YT_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/live/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def unknown_videos(date: str) -> dict[str, str]:
    """判定表に無い YouTube 動画 ID → 代表 URL。"""
    p = ROOT / "candidates" / f"{date}.json"
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for c in json.loads(p.read_text(encoding="utf-8")):
        url = c.get("url") or ""
        host = (urllib.parse.urlparse(url).hostname or "").removeprefix("www.")
        if host not in ("youtube.com", "m.youtube.com", "youtu.be"):
            continue
        m = YT_ID.search(url)
        if m and classify_source(url) == "未確認":
            out.setdefault(m.group(1), url)
    return out


def video_author(vid: str) -> tuple[str, str]:
    """oEmbed で投稿者のハンドルと題名を取る。(handle, title)。取れなければ ("", "")。"""
    try:
        import urllib.request
        q = urllib.parse.urlencode({"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"})
        req = urllib.request.Request(f"https://www.youtube.com/oembed?{q}", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        handle = urllib.parse.unquote((d.get("author_url") or "").rstrip("/").rsplit("/", 1)[-1]).removeprefix("@")
        return handle, str(d.get("title") or "")
    except Exception as e:
        print(f"  oEmbed 取得できず {vid}: {type(e).__name__}。視聴ページから読む", flush=True)
    # oEmbed が断続的に失敗する(実測 2026-09-12: Lantis の動画が HTTPError で未確認のまま紙面に載った)。
    # 視聴ページの埋め込み情報(ownerProfileUrl / <link itemprop=name>)から投稿者を取る
    try:
        import urllib.request
        req = urllib.request.Request(f"https://www.youtube.com/watch?v={vid}",
                                     headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ja"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "replace")
        m = re.search(r'"ownerProfileUrl":"[^"]*?/@([^"/]+)"', html) or re.search(r'"canonicalBaseUrl":"/@([^"/]+)"', html)
        t = re.search(r'<meta name="title" content="([^"]*)"', html) or re.search(r'<title>([^<]*)</title>', html)
        if m:
            return m.group(1), (t.group(1) if t else "").replace(" - YouTube", "")
        print(f"  視聴ページからも投稿者が取れず {vid}", flush=True)
    except Exception as e:
        print(f"  視聴ページ取得できず {vid}: {type(e).__name__}", flush=True)
    return "", ""


def add_video_ids(found: dict[str, tuple[str, str]]) -> None:
    p = ROOT / "source_types.yml"
    text = p.read_text(encoding="utf-8")
    mv = re.search(r"^video_ids:\n", text, re.M)
    if not mv:
        print("  ★video_ids が表に無いので足せない")
        return
    for vid, (typ, why) in found.items():
        # `  公式:` は x_accounts にもあるので、video_ids: より後ろだけを探す
        m = re.compile(rf"^  {typ}:\n", re.M).search(text, mv.end())
        if not m:
            print(f"  ★video_ids に {typ} の節が無いので {vid} を足せない")
            continue
        text = text[:m.end()] + f"    - {vid}   # 機械で追加: {why}\n" + text[m.end():]
    p.write_text(text, encoding="utf-8")


def resolve_videos(date: str, apply: bool) -> list[str]:
    """チャンネルが表(video_channels)にある動画の ID を、video_ids へ機械で足す。

    YouTube は投稿者で種別が決まるが、URL には ID しか無い。ヴイアラ(876プロ)は
    毎日配信があり、ID を1本ずつ人が見ていては追いつかない(実測: 1号で4件が
    未確認のまま紙面に載った)。チャンネルの公式・準公式は**人が表で決めてある**ので、
    ID の追加は合議に掛けず、oEmbed で投稿者を確かめて足す。
    戻り値は、チャンネルが表に無くて決まらなかったものの説明。
    """
    vids = unknown_videos(date)
    if not vids:
        return []
    t = source_type_table()
    chans = {h.lower(): typ for typ, hs in (t.get("video_channels") or {}).items() for h in hs or []}
    print(f"\n{date}: 判定表に無い YouTube 動画 {len(vids)}件", flush=True)
    found: dict[str, tuple[str, str]] = {}
    left: list[str] = []
    for vid in sorted(vids):
        handle, title = video_author(vid)
        typ = chans.get(handle.lower()) if handle else None
        if typ:
            found[vid] = (typ, f"@{handle}「{title[:30]}」")
            print(f"  {typ}\t{vid}\t@{handle} {title[:40]}")
        else:
            left.append(f"youtube:{vid}(@{handle or '?'})")
    if found and apply:
        add_video_ids(found)
        print(f"  → 動画 ID {len(found)}件を表に追加")
    return left


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
    if not doms and not accts and not unknown_videos(date):
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

    # YouTube はチャンネルが表にあれば合議なしで決まる(人が決めた種別を写すだけ)
    split_all += resolve_videos(date, args.apply)

    # **決まらなかったものを、その場で人へ上げない。**
    #
    # 候補の大半は記事にならずに消える。決まらなかった1件ずつを毎回通知すると、
    # 紙面に出ないものまで人の判断待ちになり、通知が意味を失う。
    # 未確認のまま紙面に載ったものだけが本当に判断の要るもので、
    # それは watch(毎朝)がまとめて出す。ここではログに残すだけにする。
    if split_all:
        print(f"\n決まらなかったもの({len(split_all)}件・未確認のまま): "
              + " / ".join(split_all), flush=True)
        print("(紙面に載ったものだけ watch が毎朝まとめて報告する)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
