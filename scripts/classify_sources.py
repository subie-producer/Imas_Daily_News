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
                     source_type_table, write_source_table, prompt_part, render_prompt,
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
             "note.com", "docs.google.com", "drive.google.com", "forms.gle", "hatenablog.com",
             "ameblo.jp", "fanbox.cc", "booth.pm", "github.com", "rakuten.co.jp",
             "tiktok.com", "instagram.com", "threads.net", "bsky.app", "pixiv.net", "twitch.tv", "lit.link")
# 多数の主体が同居するプラットフォームでは、**持ち主が決まる単位**が3通りある。どれにも当たらない URL は
# 判定の単位を決められないので、黙って飛ばさず理由付きで記録する(unknown_targets の skipped)。
# 1. パス単位(アカウント・チャンネル・作品ページ): 先頭何区切りで主体か。ホスト → (区切り数, 先頭区切りに要る接頭辞)
PATH_KEYS = {"tiktok.com": (1, "@"), "ch.nicovideo.jp": (1, ""), "manga.nicovideo.jp": (2, ""),
             "seiga.nicovideo.jp": (2, ""), "note.com": (1, ""), "ameblo.jp": (1, ""), "github.com": (2, ""),
             "instagram.com": (1, ""), "threads.net": (1, "@"), "bsky.app": (2, ""), "pixiv.net": (2, ""),
             "twitch.tv": (1, ""), "lit.link": (1, ""), "item.rakuten.co.jp": (1, ""),
             "fanbox.cc": (1, "@"), "forms.gle": (1, "")}
# 2. 文書単位(フォーム・文書・表計算): 1つの文書が1つの主体のもの。告知から張られる応募フォームなどで紙面に出る
#    (実測 2026-09-20: 公式の特別配信のおたよりフォーム docs.google.com/forms/d/e/<id> が未確認のまま載った)。
#    キーは文書 ID まで(`/viewform` や `?usp=` は含めない)
DOC_HOSTS = ("docs.google.com", "drive.google.com")
# 3. サブドメイン単位(ブログ・ショップ): `<名前>.hatenablog.com` のようにホストがそのまま主体。ドメインとして判定する
SUBDOMAIN_PLATFORMS = ("hatenablog.com", "fanbox.cc", "booth.pm")


def path_key(url: str) -> str | None:
    """プラットフォーム上の主体(アカウント・チャンネル・作品ページ・1つの文書)を表すキー(host/seg…)。対象外なら None。"""
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").removeprefix("www.").removeprefix("m.")
    seg = [s for s in u.path.split("/") if s]
    if host in DOC_HOSTS:
        # /forms/d/e/<id>/viewform, /document/d/<id>/edit, /file/d/<id>/view, /drive/folders/<id> …
        # ID は `d`・`e`・`folders` の次の区切り。取れなければ None(ホスト全体を1つの主体にしない)
        for i, s in enumerate(seg):
            if i >= 1 and seg[i - 1] in ("d", "e", "folders") and len(s) >= 20:
                return host + "/" + "/".join(seg[:i + 1])
        return None
    for h, (n, prefix) in PATH_KEYS.items():
        if host == h and len(seg) >= n and seg[0].startswith(prefix):
            return host + "/" + "/".join(seg[:n])
    return None


def platform_unit(url: str) -> tuple[str, str]:
    """未知の URL を、何の単位で判定するか。("domain" | "path" | "skip", キーか理由)。"""
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").removeprefix("www.")
    if not host:
        return "skip", "ホストが読めない"
    if host in ("x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com"):    # classify_source と同じ集合
        seg = [s for s in u.path.split("/") if s]
        if seg and seg[0].lower() not in ("i", "search", "hashtag", "home", "explore"):
            return "x", seg[0]
        return "skip", "X のアカウントに紐づかない URL(トレンド・検索など)。出典にするなら個別の投稿の URL が要る"
    pk = path_key(url)
    if pk:
        return "path", pk
    for q in SUBDOMAIN_PLATFORMS:
        if host.endswith("." + q):
            return "domain", host
    for q in PLATFORMS:
        if host == q or host.endswith("." + q):
            return "skip", f"{q} の上で、持ち主が決まる単位(アカウント・文書 ID)を URL から取れない"
    return "domain", host
UA = "Mozilla/5.0 (compatible; ImasNews/1.0)"

# 種別の定義(編集規程2.5)。本文は prompts/classify-rules.md。サイト・X アカウントの依頼文が共通で使う
RULES = prompt_part("classify-rules")


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
    SKIPPED.clear()
    USED_IN.clear()
    rows = target_rows(date)
    if not rows:
        return {}, {}, {}
    doms: dict[str, str] = {}
    accts: dict[str, tuple[str, list[str]]] = {}
    paths: dict[str, str] = {}
    for c in rows:
        # 候補の URL に改行やゴミ(`…\n-`)が付いていると判定表に当たらず、登録済みの公式まで合議に回る
        # (実測 2026-09-15: 公式 X 9件が重複登録され compose が止まった)。空白で切る
        url = ((c.get("url") or "").split() or [""])[0]
        if not url or classify_source(url) != "未確認":
            continue
        u = urllib.parse.urlparse(url)
        host = (u.hostname or "").removeprefix("www.")
        if YT_ID.search(url) and host.removeprefix("m.") in ("youtube.com", "youtu.be"):
            continue                       # 動画は投稿者で決まる(resolve_videos が扱い、決まらなければそちらが報告する)
        unit, key = platform_unit(url)
        if unit == "skip":
            SKIPPED.setdefault(url, key)   # 黙って飛ばさない。main が理由ごと報告する
            continue
        if unit == "x":
            cur = accts.setdefault(key, (url, []))
            if c.get("title") and len(cur[1]) < 4:
                cur[1].append(c["title"][:70])
            continue
        (paths if unit == "path" else doms).setdefault(key, url)
        # どの記事・候補で、何として使われているか(判定の材料。フォームや文書は、張った側の文脈が主体の手掛かりになる)
        if c.get("title") and len(USED_IN.setdefault(key, [])) < 3:
            USED_IN[key].append(str(c["title"])[:120])
    return doms, accts, paths


SKIPPED: dict[str, str] = {}          # 判定の単位を決められなかった URL → 理由(unknown_targets が埋める)
USED_IN: dict[str, list[str]] = {}    # 判定のキー → 紙面・候補での使われ方(題名と出典の label)


def used_in(key: str) -> str:
    return ("\n紙面・候補での使われ方: " + " / ".join(USED_IN[key])) if USED_IN.get(key) else ""


POSTS_ONLY: str | None = None    # 号の日付。組版前の判定では、その号の記事の出典だけを対象にする(候補は見ない)


def target_rows(date: str) -> list[dict]:
    """判定の対象: その号の候補 + 紙面に載った未確認の出典。組版前(POSTS_ONLY)は、その号の記事の出典だけ。"""
    if POSTS_ONLY:
        return unresolved_post_sources(POSTS_ONLY)
    p = ROOT / "candidates" / f"{date}.json"
    rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    return rows + unresolved_post_sources()


def unresolved_post_sources(date: str | None = None) -> list[dict]:
    """紙面に載っている**未確認**の出典(全号。date を渡すとその号だけ)。執筆が自分で見つけた URL は
    候補に無いので、候補だけ見ていると紙面の未確認が残る。収集は次号の日付で走るので、既定では号で絞らない。"""
    rows = []
    for post in sorted((ROOT / "docs" / "_posts").glob(f"{date}-*.md" if date else "*.md")):
        text = post.read_text(encoding="utf-8")
        if "未確認" not in text:
            continue
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            continue
        for s in fm.get("sources") or []:
            if isinstance(s, dict) and s.get("url") and s.get("type") == "未確認":
                rows.append({"url": s["url"], "title": f"{fm.get('title') or ''}(記事の出典: {s.get('label') or ''})"})
    return rows


def page_meta(url: str) -> tuple[str, str, str]:
    """(title, meta description, 生 HTML)。JS で描画するページでも title/description は残っている。"""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ja"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read(400_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        d = re.search(r'<meta[^>]+(?:name|property)=["\'](?:description|og:description|og:site_name)["\'][^>]+content=["\']([^"\']*)', html, re.I)
        return (re.sub(r"\s+", " ", t.group(1)).strip() if t else "", (d.group(1).strip() if d else ""), html)
    except Exception:
        return "", "", ""


def rendered_excerpt(url: str, chars: int = 1200) -> str:
    """本文の冒頭。JS 描画のページは fetch_page.py が描画して読み直す。"""
    try:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch_page.py"), url, "--chars", str(chars)],
                           capture_output=True, text=True, timeout=150, stdin=subprocess.DEVNULL, cwd=ROOT)
        body = r.stdout.split("--- 本文(要約なし) ---", 1)[-1] if "--- 本文" in r.stdout else r.stdout
        return re.sub(r"\s+", " ", body).strip()[:chars]
    except Exception as e:
        return f"(取得できず: {type(e).__name__})"


def site_profile(host: str, url: str, top: str | None = None) -> str:
    """「このサイトは何の主体か」を掘るための材料: 当該ページ、サイトのトップ(プラットフォーム上の
    アカウントならそのアカウントのページ)、運営者情報(会社概要・About・特定商取引法)のページ。
    URL の字面だけで判定しない(編集長の指摘)。"""
    parts = []
    title, desc, _ = page_meta(url)
    parts.append(f"代表URL: {url}\ntitle: {title or '-'}\ndescription: {desc or '-'}\nページ冒頭: {rendered_excerpt(url)}")
    top = top or f"https://{host}/"
    ttitle, tdesc, thtml = page_meta(top)
    top_text = rendered_excerpt(top, 3000)
    parts.append(f"サイトのトップ {top}\ntitle: {ttitle or '-'}\ndescription: {tdesc or '-'}\n冒頭: {top_text[:800]}")
    # 会社概要・運営会社・特定商取引法のどれか(その順で優先)。ナビの文言はトップと共通なので、
    # 共通の前置きを除いた本文を渡す(メニューだけで枠が埋まって運営者名が届かないのを防ぐ)
    links = re.findall(r'href=["\']([^"\']+)["\'][^>]*>\s*(?:<[^>]+>\s*)*([^<]{0,40})', thtml, re.I)
    about = None
    for kw in ("会社概要", "運営会社", "運営者", "企業情報", "特定商取引", "Company", "Corporate", "About"):
        for href, label in links:
            if kw.lower() in label.lower():
                about, about_kw = urllib.parse.urljoin(top, href), kw
                break
        if about:
            break
    if about:
        text = rendered_excerpt(about, 4000)
        import os as _os
        common = len(_os.path.commonprefix([top_text, text]))
        text = text[common:] if common > 200 else text
        parts.append(f"運営者情報 {about}({about_kw}):\n{text[:1200]}")
    else:
        parts.append("運営者情報: トップから会社概要・About・特定商取引法のリンクを見つけられず(サイト内を fetch_page.py で探すこと)")
    return "\n".join(parts)


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
        import hashlib
        from pipelib import prompt_file
        short = prompt_file(edition_date(), f"classify-{cmd[0]}-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8], prompt)
        r = subprocess.run(cmd + [short], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL, cwd=ROOT)
        rows = extract_json_array(r.stdout) or []
        if not rows:    # 答えが空のまま「—」で議論が流れると、なぜ決まらなかったのか後から追えない
            tail = ((r.stdout or "").strip() or (r.stderr or "").strip())[-200:].replace("\n", " ")
            print(f"  分類の答えが読めない({cmd[0]} exit {r.returncode}): {tail or '(出力なし)'}", flush=True)
        return rows
    except Exception as e:
        print(f"分類の呼び出しに失敗({cmd[0]}): {e}", flush=True)
        return []


def build_prompt(items: list[tuple[str, str, str]]) -> str:
    return render_prompt("classify-site", RULES=RULES, KNOWN=known_official(),
                         TARGETS="\n\n".join(f"### {h}\n{x}" for h, u, x in items))


def build_x_prompt(accts: dict[str, tuple[str, list[str]]]) -> str:
    return render_prompt("classify-x", RULES=RULES, KNOWN=known_official(),
                         TARGETS="\n".join(f"- @{a}: " + " / ".join(t or ["(投稿の要約なし)"])
                                           for a, (_, t) in sorted(accts.items())))


CMD_A = ["claude", "-p", "--model", COLLECT_MODEL, "--dangerously-skip-permissions"]
CMD_B = ["codex", "exec", "-m", EXPLORE_MODEL, "-s", "read-only", "--skip-git-repo-check"]


def _key_forms(s: str) -> tuple[str, str]:
    """対象の書き方の揺れを吸収するための (全体, 末尾) の正規形。
    `https://www.youtube.com/@Foo/` も `youtube.com/@foo` も `@Foo` も、末尾は `foo`。"""
    full = re.sub(r"^[a-z]+://", "", str(s or "").strip().lower()).removeprefix("www.").rstrip("/")
    full = full.split("?")[0].removesuffix("/about")
    return full.lstrip("@"), full.rsplit("/", 1)[-1].lstrip("@")


def _by_host(rows: list, keys: list[str] | None = None) -> dict[str, dict]:
    """モデルの答えを、**依頼した対象(keys)に対応付けて**返す。

    答えの host は依頼どおりの文字列で返ってくるとは限らない(`youtube.com/@Foo` を頼んで `@Foo` や
    `https://www.youtube.com/@Foo` で返る)。完全一致だけで引くと、答えているのに「無回答(—)」扱いになり、
    議論しても決まらない(実測 2026-09-18: @TogawaNonoha)。全体の正規形で当て、無ければ末尾(ハンドル・
    最後の区切り)で当てる。末尾は、その末尾を持つ依頼が1つだけのときに限る。対応しない答えはログに出す。
    """
    rows = [d for d in rows if isinstance(d, dict)]
    if keys is None:
        return {str(d.get("host", "")).lstrip("@"): d for d in rows}
    by_full = {_key_forms(k)[0]: k for k in keys}
    tails: dict[str, list[str]] = {}
    heads: dict[str, list[str]] = {}     # `youtube.com/@foo` を `youtube.com` とだけ返す答え(実測 2026-09-18)
    for k in keys:
        tails.setdefault(_key_forms(k)[1], []).append(k)
        if "/" in _key_forms(k)[0]:
            heads.setdefault(_key_forms(k)[0].split("/", 1)[0], []).append(k)
    out: dict[str, dict] = {}
    for d in rows:
        full, tail = _key_forms(d.get("host", ""))
        k = (by_full.get(full) or (tails[tail][0] if len(tails.get(tail) or []) == 1 else None)
             or (heads[full][0] if len(heads.get(full) or []) == 1 else None))
        if k is None:
            print(f"  答えの対象が依頼と対応しない: {str(d.get('host'))[:80]!r}", flush=True)
        elif k not in out:
            out[k] = d
    return out


def debate_prompt(prompt: str, split_keys: list[str], mine: dict, theirs: dict) -> str:
    """2巡目: 割れた対象について、相手の答えと根拠を見せ、検証して賛成か反論かを答えさせる。"""
    rows = []
    for k in split_keys:
        m, t = mine.get(k) or {}, theirs.get(k) or {}
        rows.append(f"- {k}: あなた={m.get('type') or '—'}({m.get('why') or '根拠なし'}) / "
                    f"相手={t.get('type') or '—'}({t.get('why') or '根拠なし'})")
    return render_prompt("classify-debate", FIRST=prompt.rstrip("\n"), ROWS="\n".join(rows))


def consensus(prompt: str, keys: list[str]) -> tuple[dict, list[str]]:
    """別ベンダーの2モデルの合議。1巡目は独立に答え、割れたものは**議論**する(2巡目)。

    同じベンダーだと同じ誤りを共有するので、Claude と Codex に分ける。
    1巡目で割れた対象は、相手の答えと根拠を見せて検証させ、賛成か反論かを答えさせる。
    片方が「不明」と言い、もう片方が「これ」と言ったのに、不明側が検証もせず終わっていた
    (実測 2026-09-08〜12: @yuzu_yng 不明/ファン、@onkyodav 不明/当事者)。多数決や棄権扱いで
    誤魔化さず、答えた側の根拠に向き合わせる。2巡目でも割れたら両方の言い分を付けて人へ。
    """
    # prompt は「対象の一覧 → 依頼文」の関数(文字列も受ける)。2巡目は**割れた対象だけ**の依頼文を作り直す。
    # 全対象の依頼文のまま2巡目を掛けると、割れていない対象への省略形の答えを割れた対象に当てかねず、
    # 逆に全対象で対応付けると、一意になったはずの省略形の答えをまた捨てる(監査指摘)
    make = prompt if callable(prompt) else (lambda ks: prompt)
    a = _by_host(ask(CMD_A, make(keys)), keys)
    b = _by_host(ask(CMD_B, make(keys)), keys)

    def agree(k):
        ta, tb = (a.get(k) or {}).get("type"), (b.get(k) or {}).get("type")
        return ta if (ta and ta == tb and ta in ALLOWED) else None

    split_keys = [k for k in keys if not agree(k)]
    if split_keys:
        print(f"  1巡目で割れた {len(split_keys)}件を議論させる: "
              + ", ".join(f"{k}({(a.get(k) or {}).get('type') or '—'}/{(b.get(k) or {}).get('type') or '—'})" for k in split_keys),
              flush=True)
        a2 = _by_host(ask(CMD_A, debate_prompt(make(split_keys), split_keys, a, b)), split_keys)
        b2 = _by_host(ask(CMD_B, debate_prompt(make(split_keys), split_keys, b, a)), split_keys)
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


def _span(text: str, section: str) -> tuple[int, int] | None:
    """最上位の節 `section:` の中身の範囲(次の最上位キーの手前まで)。無ければ None。"""
    m = re.search(rf"^{re.escape(section)}:\n", text, re.M)
    if not m:
        return None
    nxt = re.compile(r"^[A-Za-z_]+:", re.M).search(text, m.end())
    return m.end(), (nxt.start() if nxt else len(text))


def _listed(text: str, section: str, item: str, ci: bool) -> bool:
    """`section` の**中に** `- item` が既にあるか(種別の節は問わない)。"""
    sp = _span(text, section)
    return bool(sp and re.search(rf"^\s+-\s+{re.escape(item)}(?=\s|#|$)", text[sp[0]:sp[1]],
                                 re.M | (re.I if ci else 0)))


def insert_labeled(text: str, section: str, label: str, item: str, note: str, ci: bool) -> tuple[str, str]:
    """種別ごとの節を持つ表(x_accounts / video_channels / video_ids)の `label` の下へ item を足す。

    戻り値は (新しい本文, "added" | "exists" | "nosection")。
    - **探すのも差し込むのも section の中だけ。**`  公式:` は3つの表のどれにもあるので、範囲を
      区切らないと隣の表へ差し込む(実測: X アカウントが動画チャンネルの節に入った。動画 ID は
      video_ids に無い種別だと後ろの x_accounts の節に入る形になっていた)。
    - 既に載っているものは足さない(二重に載ると表の検査が落ちて、付け直し・組版が止まる)。
    - その種別の節がまだ無ければ作る。
    """
    sp = _span(text, section)
    if not sp:
        return text, "nosection"
    if _listed(text, section, item, ci):
        return text, "exists"
    line = f"    - {item}{' ' * max(1, 20 - len(item))}# {note}\n"
    m = re.compile(rf"^  {re.escape(label)}:\n", re.M).search(text, sp[0], sp[1])
    if m:
        return text[:m.end()] + line + text[m.end():], "added"
    return text[:sp[0]] + f"  {label}:\n" + line + text[sp[0]:], "added"


def _add_labeled(section: str, agreed: dict, note: str, ci: bool, show=lambda k: k) -> None:
    p = ROOT / "source_types.yml"
    text = p.read_text(encoding="utf-8")
    for k, (t, why) in agreed.items():
        text, st = insert_labeled(text, section, t, k, f"{note}: {why}", ci)
        if st == "nosection":
            print(f"  ★{section} が表に無いので {show(k)} を足せない")
        elif st == "exists":
            print(f"  {show(k)} は既に {section} にある({t} と決まったが足さない)")
    write_source_table(text, p)


def add_domains(agreed: dict) -> None:
    p = ROOT / "source_types.yml"
    text = p.read_text(encoding="utf-8")
    for h, (t, why) in agreed.items():
        sp = _span(text, LIST_OF[t])
        if not sp:
            print(f"  ★{LIST_OF[t]} が表に無いので {h} を足せない")
            continue
        if _listed(text, LIST_OF[t], h, ci=True):
            print(f"  {h} は既に {LIST_OF[t]} にある(足さない)")
            continue
        text = text[:sp[0]] + f"  - {h}{' ' * max(1, 22 - len(h))}# 合議で追加: {why}\n" + text[sp[0]:]
    write_source_table(text, p)


def add_paths(agreed: dict) -> None:
    """プラットフォーム上のアカウント・チャンネル・作品ページを path_types(パス → 種別)へ足す。"""
    p = ROOT / "source_types.yml"
    text = p.read_text(encoding="utf-8")
    if not _span(text, "path_types"):
        text = text.rstrip("\n") + ("\n\n# --- プラットフォーム上のアカウント・チャンネル・作品ページ(パス単位で主体が決まる) ---\n"
                                   "# tiktok.com/@…、ch.nicovideo.jp/…、manga.nicovideo.jp/comic/… など。合議が足す\n"
                                   "path_types:\n")
    for k, (t, why) in agreed.items():
        a, b = _span(text, "path_types")
        if re.search(rf"^\s+{re.escape(k)}:", text[a:b], re.M | re.I):
            continue
        text = text[:a] + f"  {k}: {t}{' ' * max(1, 40 - len(k))}# 合議で追加: {why}\n" + text[a:]
    write_source_table(text, p)


def add_video_channels(agreed: dict) -> None:
    """YouTube のチャンネル(ハンドル)を video_channels の該当種別へ足す。"""
    _add_labeled("video_channels", agreed, "合議で追加", ci=True, show=lambda h: f"@{h}")


def add_x_accounts(agreed: dict) -> None:
    """x_accounts の該当種別の下へ足す。無ければその種別の節を作る。"""
    _add_labeled("x_accounts", agreed, "合議で追加", ci=True, show=lambda a: f"@{a}")


YT_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/live/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def unknown_videos(date: str) -> dict[str, str]:
    """判定表に無い YouTube 動画 ID → 代表 URL。"""
    out: dict[str, str] = {}
    for c in target_rows(date):
        url = ((c.get("url") or "").split() or [""])[0]
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
    """動画 ID を video_ids の該当種別へ足す。ID は大文字小文字を区別する。"""
    _add_labeled("video_ids", found, "機械で追加", ci=False)


def resolve_videos(date: str, apply: bool) -> tuple[list[str], dict]:
    """チャンネルが表(video_channels)にある動画の ID を、video_ids へ機械で足す。

    YouTube は投稿者で種別が決まるが、URL には ID しか無い。ヴイアラ(876プロ)は
    毎日配信があり、ID を1本ずつ人が見ていては追いつかない(実測: 1号で4件が
    未確認のまま紙面に載った)。チャンネルの公式・準公式は**人が表で決めてある**ので、
    ID の追加は合議に掛けず、oEmbed で投稿者を確かめて足す。
    戻り値は、チャンネルが表に無くて決まらなかったものの説明。
    """
    vids = unknown_videos(date)
    if not vids:
        return [], {}
    t = source_type_table()
    chans = {h.lower(): typ for typ, hs in (t.get("video_channels") or {}).items() for h in hs or []}
    print(f"\n{date}: 判定表に無い YouTube 動画 {len(vids)}件", flush=True)
    found: dict[str, tuple[str, str]] = {}
    left: list[str] = []
    unknown_chans: dict[str, tuple[str, str]] = {}   # handle → (動画 ID, 題名)。チャンネルが表に無い
    for vid in sorted(vids):
        handle, title = video_author(vid)
        typ = chans.get(handle.lower()) if handle else None
        if typ:
            found[vid] = (typ, f"@{handle}「{title[:30]}」")
            print(f"  {typ}\t{vid}\t@{handle} {title[:40]}")
        else:
            left.append(f"youtube:{vid}(@{handle or '?'})")
            if handle:
                unknown_chans.setdefault(handle, (vid, title))
    if found and apply:
        add_video_ids(found)
        print(f"  → 動画 ID {len(found)}件を表に追加")
    return left, unknown_chans


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--apply", action="store_true", help="一致したものを source_types.yml に足す")
    ap.add_argument("--limit", type=int, default=60,
                    help="1回に掛ける X アカウントの数(多いと1回のプロンプトに載らない)")
    ap.add_argument("--posts-only", action="store_true",
                    help="その号の記事に載った未確認の出典だけを対象にする(組版前。候補は見ない)")
    args = ap.parse_args()
    # 下見(--apply なし)では通知しない。試験実行が本物の警報と混ざる
    set_quiet(not args.apply)
    date = args.date or edition_date()
    if args.posts_only:
        global POSTS_ONLY
        POSTS_ONLY = date

    doms, accts, paths = unknown_targets(date)
    # 判定の単位を決められなかった URL は、黙って飛ばさず理由ごと残す(決まらなかったものとして下でまとめて出る)
    split_all = [f"{u}: 判定できない({why})" for u, why in sorted(SKIPPED.items())]
    for s in split_all:
        print(f"  判定の対象にできない\t{s}", flush=True)
    if not doms and not accts and not paths and not unknown_videos(date):
        if not split_all:
            print(f"{date}: 判定表に無い出典はありません")
            return 0

    if doms:
        print(f"{date}: 判定表に無いドメイン {len(doms)}件 → {', '.join(sorted(doms))}", flush=True)
        items = [(h, u, site_profile(h, u) + used_in(h)) for h, u in sorted(doms.items())]
        agreed, split = consensus(lambda ks: build_prompt([it for it in items if it[0] in ks]), sorted(doms))
        for h, (t, why) in agreed.items():
            print(f"  一致 {t}\t{h}\t{why}")
        for s in split:
            print(f"  不一致・保留\t{s}")
        split_all += split
        if agreed and args.apply:
            add_domains(agreed)
            print(f"  → ドメイン {len(agreed)}件を表に追加")

    if paths:
        # プラットフォーム上のアカウント・チャンネル・作品ページ。「トップ」はそのアカウントのページ
        print(f"\n{date}: 判定表に無いプラットフォーム上の主体 {len(paths)}件 → {', '.join(sorted(paths))}", flush=True)
        # 「トップ」はそのアカウントのページ。文書(フォーム等)にはトップが無いので、文書そのものを見せる
        items = [(k, u, site_profile(k, u, top=(u if k.split("/")[0] in DOC_HOSTS + ("forms.gle",) else f"https://{k}/")) + used_in(k))
                 for k, u in sorted(paths.items())]
        agreed, split = consensus(lambda ks: build_prompt([it for it in items if it[0] in ks]), sorted(paths))
        for k, (t, why) in agreed.items():
            print(f"  一致 {t}\t{k}\t{why}")
        for s in split:
            print(f"  不一致・保留\t{s}")
        split_all += split
        if agreed and args.apply:
            add_paths(agreed)
            print(f"  → パス {len(agreed)}件を表に追加")

    if accts:
        # 多いと1回のプロンプトに載らないので、出現数の多い順に区切って掛ける
        order = sorted(accts, key=lambda a: (-len(accts[a][1]), a))[:args.limit]
        sub = {a: accts[a] for a in order}
        print(f"\n{date}: 判定表に無い X アカウント {len(accts)}件"
              f"(今回 {len(sub)}件を処理)", flush=True)
        agreed, split = consensus(lambda ks: build_x_prompt({k: sub[k] for k in ks}), sorted(sub))
        for a, (t, why) in agreed.items():
            print(f"  一致 {t}\t@{a}\t{why}")
        for s in split:
            print(f"  不一致・保留\t@{s}")
        split_all += split
        if agreed and args.apply:
            add_x_accounts(agreed)
            print(f"  → X アカウント {len(agreed)}件を表に追加")

    # YouTube はチャンネルが表にあれば合議なしで決まる(人が決めた種別を写すだけ)。
    # チャンネルが表に無ければ、チャンネルのページ(概要)を材料に合議で種別を決めて表に足し、
    # そのうえで動画 ID を引き直す
    left, unknown_chans = resolve_videos(date, args.apply)
    if unknown_chans:
        print(f"\n{date}: 判定表に無い YouTube チャンネル {len(unknown_chans)}件 → "
              + ", ".join(f"@{h}" for h in sorted(unknown_chans)), flush=True)
        items = [(f"youtube.com/@{h}", f"https://www.youtube.com/@{h}/about",
                  site_profile(f"youtube.com/@{h}", f"https://www.youtube.com/@{h}/about", top=f"https://www.youtube.com/@{h}")
                  + f"\n表に載った動画: {vid}「{title[:60]}」")
                 for h, (vid, title) in sorted(unknown_chans.items())]
        agreed, split = consensus(lambda ks: build_prompt([it for it in items if it[0] in ks]),
                                  [f"youtube.com/@{h}" for h in sorted(unknown_chans)])
        chans = {k.split("@", 1)[1]: v for k, v in agreed.items() if "@" in k}
        for h, (t, why) in chans.items():
            print(f"  一致 {t}\t@{h}\t{why}")
        for s in split:
            print(f"  不一致・保留\t{s}")
        if chans and args.apply:
            add_video_channels(chans)
            print(f"  → チャンネル {len(chans)}件を表に追加")
            left, _ = resolve_videos(date, True)
        else:
            left = [x for x in left if not any(f"(@{h})" in x for h in chans)] + split
    split_all += left

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
