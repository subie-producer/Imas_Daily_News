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
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipelib import (ENV, ROOT, COLLECT_MODEL, EXPLORE_MODEL, ID_CASE_HOSTS, QUERY_DOC_PAGES, SHORTENERS, classify_source,
                     source_type_table, write_source_table, prompt_part, render_prompt, query_doc_key, path_type_norm,
                     edition_date, extract_json_array, html_to_text, notify, render_failure, set_quiet,
                     X_ANON_POST, x_post_author, CLI_BLOCKED_EXIT, CliBlocked, cli_check, link_key, src_rank,
                     browser_only, render_page)
from pipelib import PAGE_READ_BYTES, run_group   # モデル・道具は自前のグループで起動し、時間切れでは子孫まで止める
from pipelib import polite_urlopen      # ページの取得は同じホストへの間隔をあけ、429 は Retry-After を守って取り直す

# 合議で足してよい種別。公式・準公式も答えさせる(作品・ブランドの公式アカウントを「不明」で人へ回して
# いたら、@idolmaster_en もジムシャニ公式も未確認のまま紙面に載った。編集長の指摘)。
# どの種別も2モデルの一致が要る。割れたら議論(consensus の2巡目)
ALLOWED = ("公式", "準公式", "当事者", "演者", "報道", "二次情報", "ファン")
# ドメインの一覧は種別ごと。ALLOWED のどれとも対応させる(依頼文は所属事務所のサイトも演者と答えさせる。演者の保存先が無く、
# 2モデルが一致した答えが KeyError で取引ごと落ちた。監査指摘 2026-10-14)
LIST_OF = {"公式": "official_domains", "準公式": "semi_official_domains",
           "当事者": "party_domains", "演者": "performer_domains", "報道": "press_domains",
           "二次情報": "secondary_domains", "ファン": "fan_domains"}
# ドメインでは決まらない場(アカウント・動画IDで決まる)。対象外
# **公式の主体がページを持ちうる場**だけを外す。そこはドメインでは決まらない。
# wiki ホスティング(atwiki 等)は外さない。公式がそこに告知を出すことはなく、
# どのページも利用者が書いた二次情報なので、ドメインで決められる(表に載せてある)
PLATFORMS = ("x.com", "twitter.com", "youtube.com", "youtu.be", "nicovideo.jp",
             "note.com", "docs.google.com", "drive.google.com", "forms.gle", "hatenablog.com",
             "ameblo.jp", "fanbox.cc", "booth.pm", "github.com", "rakuten.co.jp",
             "tiktok.com", "instagram.com", "threads.net", "bsky.app", "pixiv.net", "twitch.tv", "lit.link",
             "krs.bz", "line.me")
# 多数の主体が同居するプラットフォームでは、**持ち主が決まる単位**が3通りある。どれにも当たらない URL は
# 判定の単位を決められないので、黙って飛ばさず理由付きで記録する(unknown_targets の skipped)。
# 1. パス単位(アカウント・チャンネル・作品ページ): 先頭何区切りで主体か。ホスト → (区切り数, 先頭区切りに要る接頭辞)
PATH_KEYS = {"tiktok.com": (1, "@"), "ch.nicovideo.jp": (1, ""), "manga.nicovideo.jp": (2, ""),
             "seiga.nicovideo.jp": (2, ""), "note.com": (1, ""), "ameblo.jp": (1, ""), "github.com": (2, ""),
             "instagram.com": (1, ""), "threads.net": (1, "@"), "bsky.app": (2, ""), "pixiv.net": (2, ""),
             "twitch.tv": (1, ""), "lit.link": (1, ""), "item.rakuten.co.jp": (1, ""),
             "fanbox.cc": (1, "@"), "forms.gle": (1, ""), "krs.bz": (1, "")}
# 先頭の区切りが持ち主(アカウント)ではないもの。投稿・動画の ID しか無く、持ち主はパーサで引く(先頭1区切りで切ると、
# 別の人の投稿がみな同じ主体になる。2026-10-14: instagram.com/p が1つの主体として表に載り、どの投稿も同じ種別になっていた)
NOT_OWNER_HEADS = {"instagram.com": ("p", "reel", "reels", "tv", "stories"), "twitch.tv": ("videos",)}
# 2. 文書単位(フォーム・文書・表計算): 1つの文書が1つの主体のもの。告知から張られる応募フォームなどで紙面に出る
#    (実測 2026-09-20: 公式の特別配信のおたよりフォーム docs.google.com/forms/d/e/<id> が未確認のまま載った)。
#    キーは文書 ID まで(`/viewform` や `?usp=` は含めない)。Microsoft Forms は ID がクエリにあり、キーは
#    `<host>/pages/responsepage.aspx?id=<ID>`(pipelib.query_doc_key)か短縮の `<host>/r/<ID>`
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
    if host in QUERY_DOC_PAGES:
        # 文書 ID はクエリの id か、短縮の /r/<ID>。取れなければ None(フォームのサービス全体を1つの主体にしない。監査指摘 2026-10-14)
        return query_doc_key(url) or (f"{host}/r/{seg[1]}" if len(seg) >= 2 and seg[0].lower() == "r" else None)
    if host == "youtube.com" and seg == ["playlist"]:
        # 再生リストは list= の ID までが1つの単位(種別は持ち主のチャンネルを写す。resolve_playlists)。ID が無ければ None
        return query_doc_key(url)
    if host == "youtube.com" and seg:
        # チャンネルのページ(`/@<ハンドル>/streams`・`/channel/<ID>/videos` など)は、チャンネルまでが1つの単位。
        # 枝(streams・videos・community…)は同じ持ち主(2026-10-28: `@KamizuruCosmo/streams` にパーサが無かった)
        if seg[0].startswith("@") and len(seg[0]) > 1:
            return f"{host}/{seg[0]}"
        if seg[0] in ("channel", "c", "user") and len(seg) >= 2:
            return f"{host}/{seg[0]}/{seg[1]}"
    if host == "ch.nicovideo.jp" and seg and seg[0] == "channel":
        # `/channel/<ch ID>` は ID までが1つのチャンネル。先頭1区切りで切ると、別のチャンネルが同じ `ch.nicovideo.jp/channel` になり、
        # 1つを公式にすると他のチャンネル(とその番組)まで公式になった(監査指摘 2026-10-14)。別名の形(`/sidem`)は先頭1区切り
        return f"{host}/channel/{seg[1]}" if len(seg) >= 2 and re.fullmatch(r"ch\d+", seg[1]) else None
    for h, (n, prefix) in PATH_KEYS.items():
        if host == h and len(seg) >= n and seg[0].startswith(prefix) and seg[0] not in NOT_OWNER_HEADS.get(h, ()):
            return host + "/" + "/".join(seg[:n])
    return None


def display_base(url_or_key: str) -> str:
    """人に見せる・記録に使う「判定表に書く単位」の表記。URL でも判定のキー(`youtube.com/@h`、X のハンドル、
    ドメイン)でも、同じ表記に揃える。watch の表示と、決まらなかった記録の照合キーを一致させる(監査指摘)。"""
    s = str(url_or_key or "").strip()
    if "://" not in s and "/" not in s and "." not in s:      # X のハンドル
        return f"https://x.com/{s.lstrip('@')}"
    if "://" not in s:                                          # 判定のキー(host/path か host)
        return "https://" + s.rstrip("/")
    u = urllib.parse.urlparse(s)
    host = (u.hostname or "").removeprefix("www.").removeprefix("m.")
    seg = [x for x in u.path.split("/") if x]
    if host in ("youtube.com", "youtu.be") and seg and seg[0].startswith("@"):
        return f"https://youtube.com/{seg[0]}"
    m = NICO_ID.search(s)
    if m:
        # ニコニコの生放送・動画もチャンネルで決まる。チャンネルを引けなければ番組の URL
        key, _ch, _t = nico_channel(m.group(1))
        return f"https://{key}" if key else nico_watch_url(m.group(1))
    pl = yt_playlist_key(s)
    if pl:
        # 再生リストも持ち主(チャンネル)で決まる。引けなければ再生リストの URL
        handle, _ = playlist_owner(pl)
        return f"https://youtube.com/@{handle}" if handle else f"https://{pl}"
    m = YT_ID.search(s)
    if m and host in ("youtube.com", "youtu.be"):
        # 動画は投稿者(チャンネル)で決まる。判定表に書く単位はチャンネルなので、投稿者を引いて出す。
        # 引けなければ動画の URL(決まらなかった記録も動画 ID で引ける)
        handle, _ = video_author(m.group(1))
        return f"https://youtube.com/@{handle}" if handle else f"https://youtube.com/watch?v={m.group(1)}"
    pid, handle = anon_x_author(s)
    if handle:
        # 投稿者の無い X の投稿も、判定表に書く単位は投稿者のアカウント
        return f"https://x.com/{handle}"
    unit, key = platform_unit(s)
    if unit == "x":
        return f"https://x.com/{key}"
    if unit in ("path", "domain", "short"):
        return f"https://{key}"
    if unit == "parser":
        return f"(持ち主を取るパーサが無い: {key})"
    return f"(判定の単位なし: {key})"


def platform_unit(url: str) -> tuple[str, str]:
    """未知の URL を、何の単位で判定するか。("domain" | "path" | "x" | "short" | "skip" | "parser", キーか理由)。

    skip は「持ち主が無いと分かっている URL」(検索・トレンド・ID の無い文書ホスト)。
    parser は「持ち主はいるはずだが、取る手段(パーサ)をこのコードが持っていない」。後者は人に「決まらない」と
    申告する事象ではなく、パーサを書かせる依頼を当番に出す(`request_parsers`。編集長 2026-09-26:
    「この手のやつが来たら『パースしろ』という判定をしろ」「申告前に診断しろ」)。
    short は短縮 URL(`t.co/<ID>`)。ホストでは決まらず、展開先の種別を短縮1つずつへ写す(resolve_short_links)。
    """
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").removeprefix("www.")
    if not host:
        return "skip", "ホストが読めない"
    if host in SHORTENERS:
        seg = [s for s in u.path.split("/") if s]
        return ("short", f"{host}/{seg[0]}") if seg else ("skip", f"{host} の短縮 ID の無い URL")
    if host in ("x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com"):    # classify_source と同じ集合
        seg = [s for s in u.path.split("/") if s]
        if seg and seg[0].lower() not in ("i", "search", "hashtag", "home", "explore"):
            return "x", seg[0]
        return "skip", "X のアカウントに紐づかない URL(トレンド・検索など)。出典にするなら個別の投稿の URL が要る"
    pk = path_key(url)
    if pk:
        return "path", pk
    if host in DOC_HOSTS or host in QUERY_DOC_PAGES:
        return "skip", f"{host} の文書 ID の無い URL(文書でなければ持ち主は無い)"
    if host.removeprefix("m.") == "youtube.com" and u.path.rstrip("/") == "/playlist":
        return "skip", "YouTube の再生リスト ID(list=)の無い URL"
    if host.removeprefix("m.") == "youtube.com" and (u.path.strip("/") == "" or u.path.strip("/").split("/")[0] in ("results", "feed", "hashtag")):
        return "skip", "YouTube のチャンネルにも動画にも紐づかない URL(トップ・検索・トレンド・ハッシュタグ)。出典にするならチャンネルか動画の URL が要る"
    for q in SUBDOMAIN_PLATFORMS:
        if host.endswith("." + q):
            return "domain", host
    if host == "booth.pm":
        # `booth.pm/<言語>/items/<ID>` は商品1つ。持ち主はショップ(`<名前>.booth.pm`)で、ドメインとして判定する。
        # ショップは商品の JSON の shop.subdomain から引く(booth_item_shop)。商品 ID の無い URL(トップ・検索・カテゴリ・イベント)に持ち主は無い
        item = booth_item_key(url)
        if not item:
            return "skip", "BOOTH の商品 ID の無い URL(トップ・検索・カテゴリなど。持ち主は無い)"
        shop, _ = booth_shop(item)
        return ("domain", shop) if shop else ("skip", "BOOTH の商品からショップを引けない(商品の JSON が返らない。削除・非公開の商品か)")
    for q in PLATFORMS:
        if host == q or host.endswith("." + q):
            return "parser", host
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
    NEED_PARSER.clear()
    USED_IN.clear()
    X_POSTS.clear()
    SHORT_LINKS.clear()
    ALL_SHORTS.clear()
    BOOTH_ITEMS.clear()
    YT_CHANNELS.clear()
    rows = target_rows(date)
    # 判定済みの短縮も、展開先が上位になっていないかを見る(resolve_short_links)。組版前はその号の記事の出典の全部から集める
    # (target_rows は未確認の出典だけ。監査指摘 2026-10-21)
    for c in rows + (unresolved_post_sources(POSTS_ONLY, every=True) if POSTS_ONLY else []):
        url = ((c.get("url") or "").split() or [""])[0]
        if url and (urllib.parse.urlparse(url).hostname or "").removeprefix("www.") in SHORTENERS and link_key(url):
            ALL_SHORTS.setdefault(link_key(url), url)
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
        if (YT_ID.search(url) and host.removeprefix("m.") in ("youtube.com", "youtu.be")) or NICO_ID.search(url):
            continue                       # 動画は投稿者で決まる(resolve_videos / resolve_nico が扱い、決まらなければそちらが報告する)
        if yt_playlist_key(url):
            continue                       # 再生リストも持ち主のチャンネルで決まる(resolve_playlists が扱う)
        pid, handle = anon_x_author(url)
        if pid:
            # 投稿者の無い X の投稿(`x.com/i/status/<ID>`)。URL は記事に載ったまま変えられないので、
            # 投稿者を引いてアカウントを判定し、決まった種別を投稿 ID で表に残す(resolve_x_posts)
            if not handle:
                SKIPPED.setdefault(url, "X の投稿者を引けない(oEmbed が返さない。削除・非公開の投稿か)")
                continue
            X_POSTS.setdefault(pid, handle)
            if classify_source(f"https://x.com/{handle}") != "未確認":
                continue                   # アカウントは表にある。投稿 ID を足すだけ
            cur = accts.setdefault(handle, (f"https://x.com/{handle}/status/{pid}", []))
            if c.get("title") and len(cur[1]) < 4:
                cur[1].append(c["title"][:70])
            continue
        unit, key = platform_unit(url)
        if unit == "path" and key.startswith("youtube.com/@"):
            # チャンネルのページ(`/@<ハンドル>/streams` など)はチャンネルで決まる。動画の持ち主と同じ表(video_channels)で
            # 決めるので、パスの表ではなくチャンネルの合議に掛ける(main)
            h = urllib.parse.unquote(key.split("@", 1)[1])
            # ハンドルは大文字小文字を区別しない(同じチャンネルを2つの対象として合議に掛けると、答えの対応付けで1つに重なり無回答になる。
            # Sol 監査 2026-10-27)。最初に見た表記で1つにする
            if not any(x.lower() == h.lower() for x in YT_CHANNELS):
                YT_CHANNELS[h] = (url, str(c.get("title") or ""))
            continue
        if unit == "skip":
            SKIPPED.setdefault(url, key)   # 黙って飛ばさない。main が理由ごと報告する
            continue
        if unit == "short":
            # 短縮 URL そのものは判定しない。展開先は target_rows が対象に足してあり、決まった種別を resolve_short_links が写す
            SHORT_LINKS.setdefault(key, url)
            continue
        if unit == "parser":
            NEED_PARSER.setdefault(url, key)   # パーサが無い種類。main が当番に書かせる依頼を出す(人には申告しない)
            if c.get("title") and len(USED_IN.setdefault(url, [])) < 3:
                USED_IN[url].append(str(c["title"])[:120])
            continue
        if unit == "x":
            cur = accts.setdefault(key, (url, []))
            if c.get("title") and len(cur[1]) < 4:
                cur[1].append(c["title"][:70])
            continue
        item = booth_item_key(url)
        if item:
            # BOOTH の商品はショップで決まる。商品の URL は記事に載ったまま変えられないので、ショップの種別を商品へ写す
            # (resolve_booth_items)。ショップが表にあれば合議に掛けない
            BOOTH_ITEMS.setdefault(item, (key, url))
            if classify_source(f"https://{key}/") != "未確認":
                continue
        (paths if unit == "path" else doms).setdefault(key, url)
        # どの記事・候補で、何として使われているか(判定の材料。フォームや文書は、張った側の文脈が主体の手掛かりになる)
        if c.get("title") and len(USED_IN.setdefault(key, [])) < 3:
            USED_IN[key].append(str(c["title"])[:120])
    return doms, accts, paths


PRIMARY = ("公式", "準公式", "当事者", "演者")    # 自分の告知として文書を張る側(一次発信)。並びは強い順


def is_document(key: str) -> bool:
    """フォーム・文書など、**それ自身は身元を持たない**単位か(持ち主は、それを張った告知で決まる)。"""
    return key.split("/")[0] in DOC_HOSTS + ("forms.gle",) + tuple(QUERY_DOC_PAGES)


URL_START_OK = "（「『【(:："     # URL の直前に来てよい ASCII 以外の区切り・開き括弧・コロン(空白・文の始まり・和文の文字のほか)


def fact_urls(text: str) -> list[str]:
    """facts の文から切り出した URL。空白で区切った語ごとに見て、URL の範囲が曖昧なものは入れない(削って・途中から切り出して
    別の URL を作らない。監査指摘 2026-10-21: `/event(1)` から `/event`、`/event'B`・`/event（B）`・`/event、B` から `/event`、
    `…'https://other…` から後半を作り、張られていない先を公式にした)。
    入れるのは: 語の中の最初の `http(s)://` の直前が語の始まり・和文の文字・開き括弧・コロンで、そこから語の終わりまでが URL の文字
    だけのもの、または URL の後ろが和文の閉じ括弧・句読点だけのもの。末尾が句読点とも読める・括弧の対が合わないものは入れない。"""
    out = []
    for word in text.split():
        # 語の中の `://` は1つだけ(別の URL の中から切り出さない。`ftp://…:https://…`・`HTTPS://…:https://…` を入れない)
        if word.count("://") != 1:
            continue
        m = re.search(r"https?://", word)
        i = m.start() if m else -1
        # 前置きは全部が和文(かな・漢字・全角)か開き括弧・コロン(`blob:`・`data:…,`・`//…/:` など別の URL に包まれたものを入れない)
        if i < 0 or not all(ch in URL_START_OK or re.match(r"[぀-ヿ㐀-鿿！-｠]", ch) for ch in word[:i]):
            continue
        m = re.match(r"https?://[A-Za-z0-9._~/?#=&%+@!$*,;:()'\[\]-]+", word[i:])
        if not m:
            continue        # `https://` だけ・直後が URL の文字でないものは入れない(過去の素材にあっても判定を止めない)
        u, rest = m.group(0), word[i + len(m.group(0)):]
        if ((rest and not re.fullmatch(r"[）」』】、。]+", rest)) or u[-1] in ".,;:!?…" or u.count("(") != u.count(")")
                or u.count("://") != 1):
            continue
        out.append(u)
    return out
_LINK_INDEX: list = [None, {}, {}]    # [素材ファイルの同一性, リンク先のキー → リンク元の URL の並び, リンク元の URL → (題名, facts)]


def link_index() -> tuple[dict[str, list[str]], dict[str, tuple[str, list[str]]]]:
    """素材(候補 candidates/*.json と続報予約 stock/scheduled/*.json の全部)から引いた、リンク先 → リンク元。
    リンク元は2通り: 候補の link_from(url をリンクとして載せていた投稿・ページ)と、facts に url を載せている候補自身
    (Grok の投稿の「リンク: <url>」、告知ページの申込先など)。キーは pipelib.link_key(書き方の揺れを同じ先にまとめる)。
    以前は直近7号の候補の facts だけを文書 ID で探していたので、9/26 の公式の投稿が張ったフォームは 10/21 号の判定で
    リンク元が「無い」とされ、合議が割れて未確認のまま載った(2026-10-21)。素材ファイルが変わっていなければ前回の結果を使う。"""
    files = sorted((ROOT / "candidates").glob("????-??-??.json")) + sorted((ROOT / "stock" / "scheduled").glob("*.json"))
    ident = (str(ROOT), tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files))
    if _LINK_INDEX[0] == ident:
        return _LINK_INDEX[1], _LINK_INDEX[2]
    idx: dict[str, list[str]] = {}
    rows_of: dict[str, tuple[str, list[str]]] = {}   # 素材の url → (題名, facts)。リンク元の告知の中身として合議に見せる

    def add(k: str, src: str) -> None:
        if src not in idx.setdefault(k, []):
            idx[k].append(src)
    for p in files:
        try:
            rows = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        for c in rows if isinstance(rows, list) else []:
            if not isinstance(c, dict):
                continue
            url = ((c.get("url") or "").split() or [""])[0]
            own = link_key(url) if url else None
            lf = c.get("link_from")
            if own and isinstance(lf, str) and lf.startswith(("http://", "https://")) and link_key(lf) not in (None, own):
                add(own, lf)
            if not own:
                continue
            if url not in rows_of or not rows_of[url][1]:
                rows_of[url] = (str(c.get("title") or ""), [str(f) for f in c.get("facts") or []][:40])
            for f in c.get("facts") or []:
                for u in fact_urls(str(f)):
                    k = link_key(u)
                    if k and k != own:
                        add(k, url)
    about = {u: rows_of.get(u, ("", [])) for srcs in idx.values() for u in srcs}
    _LINK_INDEX[:] = [ident, idx, about]
    return idx, about


def link_sources(key: str) -> list[tuple[str, str]]:
    """そのリンク先(URL か判定の単位のキー)を**張っている**素材(リンク元)の (URL, 種別)。リンク元が分かれば、
    モデルに推測させるまでもない(編集長の指摘 2026-09-20:「どこに書いてあって、どこにリンクされているかで決まる」。
    実測: 公式 X の投稿の facts に「配信へのお便り募集: https://docs.google.com/forms/…」とあるのに、フォームを未確認の
    まま合議に回していた)。"""
    k = link_key(key if "://" in key else "https://" + key)
    return [(u, classify_source(u)) for u in link_index()[0].get(k, [])] if k else []


def by_link_source(key: str) -> tuple[str, str] | None:
    """リンク元だけで機械的に決められる文書の (種別, 根拠)。決められなければ None(リンク元を材料に付けて合議へ)。

    - リンク元が全部ファンなら、ファン(個人の文書。過大表示にならない)
    - 一次発信(公式・準公式・当事者・演者)が張っている文書は、**機械では決めない**。リンク元の告知と関係しているかは
      リンク元だけでは分からない(監査指摘 r69)。関係を合議に確かめさせ(inherit_from_links)、確かめたものだけリンク元の
      種別を引き継ぐ(編集長 2026-10-21)。決まらなければリンク元を材料に付けて文書の合議へ(`link_hint`)
    - リンク元が無い・報道や二次情報だけ、も合議へ。報道が紹介したフォームは報道のものではない
    """
    srcs = link_sources(key)
    if srcs and all(t == "ファン" for _, t in srcs):
        return "ファン", f"リンク元 {srcs[0][0]}(ファン)が張っている"
    return None


def link_hint(key: str) -> str:
    """合議に渡す材料: この文書を張っている候補(リンク元)と、その種別。どう使うかの規則は依頼文(classify-site)にある。"""
    srcs = link_sources(key)
    if not srcs:
        return "\nリンク元: 手元の素材には、この文書を張っているものが無い"
    return "\nリンク元(この文書を張っている素材): " + " / ".join(f"{u}({t})" for u, t in srcs[:4])


SKIPPED: dict[str, str] = {}          # 持ち主の無い URL → 理由(unknown_targets が埋める)
NEED_PARSER: dict[str, str] = {}      # 持ち主を取るパーサが無い URL → ホスト(unknown_targets が埋める)
USED_IN: dict[str, list[str]] = {}    # 判定のキー → 紙面・候補での使われ方(題名と出典の label)
X_POSTS: dict[str, str] = {}          # 投稿者の無い X の投稿 ID → 投稿者(unknown_targets が埋める。resolve_x_posts が表へ)
_X_AUTHOR: dict[str, str] = {}        # 投稿 ID → 投稿者(引けなければ "")。同じ実行で何度も問い合わせない
YT_CHANNELS: dict[str, tuple[str, str]] = {}   # 表に無い YouTube チャンネルのページのハンドル → (URL, 題名)(unknown_targets が埋める。main がチャンネルの合議へ)
BOOTH_ITEMS: dict[str, tuple[str, str]] = {}   # BOOTH の商品のキー → (ショップのホスト, URL)(unknown_targets が埋める。resolve_booth_items が表へ)
SHORT_LINKS: dict[str, str] = {}      # 判定表に無い短縮 URL のキー(`t.co/<ID>`)→ URL(unknown_targets が埋める。resolve_short_links が表へ)
ALL_SHORTS: dict[str, str] = {}       # 判定の対象の短縮 URL の全部(判定済みも)。キー → URL(unknown_targets が埋める)
_SHORT: dict[str, str] = {}           # 短縮 URL → 展開先(引けなければ "")。同じ実行で何度も問い合わせない


def expand_short(url: str) -> str:
    """短縮 URL の展開先。引けなければ ""。"""
    if url not in _SHORT:
        _SHORT[url] = expand_short_fetch(url)
    return _SHORT[url]


def expand_short_fetch(url: str, hops: int = 5) -> str:
    """短縮 URL を、短縮サービスの外へ出るまで辿る(展開先のページそのものは取りに行かない)。辿れなければ ""。
    リダイレクト(Location)と、ページで飛ばす形(meta refresh / location.replace)の両方を読む。"""
    import html as _html
    import urllib.error
    import urllib.request

    class _Stay(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(_Stay)
    cur = url
    for _ in range(hops):
        if (urllib.parse.urlparse(cur).hostname or "").removeprefix("www.") not in SHORTENERS:
            return cur
        nxt = ""
        try:
            with polite_urlopen(urllib.request.Request(cur, headers={"User-Agent": UA}), timeout=20, opener=opener) as r:
                body = r.read(100_000).decode("utf-8", "replace")
            m = (re.search(r'http-equiv=["\']?refresh["\']?[^>]*content=["\'][^"\']*?url=([^"\'>]+)', body, re.I)
                 or re.search(r'location\.replace\(["\']([^"\']+)', body))
            nxt = _html.unescape(m.group(1)).replace("\\/", "/").strip() if m else ""
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                nxt = e.headers.get("Location") or ""
        except Exception as e:      # noqa: BLE001  引けなければ未確認のまま(理由は resolve_short_links が残す)
            print(f"  短縮 URL を展開できず {cur}: {type(e).__name__}", flush=True)
            return ""
        if not nxt:
            return ""
        cur = urllib.parse.urljoin(cur, nxt)
    return ""


def resolve_short_links(apply: bool, date: str | None = None) -> list[str]:
    """判定表に無い短縮 URL へ、展開先の種別を機械で写す(path_types に短縮1つずつ。合議には掛けない。x_posts と同じ)。
    展開先が表に無ければ、展開先は target_rows で合議・機械の判定に掛かっている(この関数は main の最後に呼ぶ)。
    戻り値は決まらなかったものの説明(`<短縮のキー>: 理由`)。date を渡すと、号 date より前の公開済みの記事が出典にしている
    短縮は、足すのも書き換えるのもしない(published_citations。訂正の手順へ回す記録に残す)。"""
    found: dict[str, tuple[str, str]] = {}
    left: list[str] = []
    pub = published_citations(date) if date else {}
    for key, url in sorted(SHORT_LINKS.items()):
        if classify_source(url) != "未確認":
            continue
        if pub.get(link_key(url)):
            # 公開済みの記事が出典にしている未確認の短縮も自動では変えない(監査 r12 の later: 変えると公開済みの記事が変わる)
            left.append(held_for_correction(key, pub[link_key(url)]))
            continue
        dest = expand_short(url)
        typ = classify_source(dest) if dest else "未確認"
        if not dest:
            left.append(f"{key}: 短縮 URL の展開先を引けない(リダイレクトもページ内の転送も読めなかった)")
        elif typ == "未確認":
            left.append(f"{key}: 短縮 URL。展開先 {dest[:160]} の種別が決まらない(展開先の判定を参照)")
        else:
            found[key] = (typ, f"展開先 {dest[:160]}")
            print(f"  {typ}\t{key}\t→ {dest[:80]}", flush=True)
    # 判定済みの短縮でも、展開先そのものの種別が今の種別(リンク元から引き継いだ種別を含む)より上位なら、上位で書き換える
    # (編集長 2026-10-21: リンク先そのものの種別が上位なら上位)
    for key, url in sorted(ALL_SHORTS.items()):
        cur = classify_source(url)
        if key in found or cur == "未確認":
            continue
        dest = expand_short(url)
        typ = classify_source(dest) if dest else "未確認"
        if typ != "未確認" and src_rank(typ) < src_rank(cur) and pub.get(key):
            left.append(held_for_correction(key, pub[key]) + f"。展開先 {dest[:160]}({typ})")
        elif typ != "未確認" and src_rank(typ) < src_rank(cur):
            found[key] = (typ, f"展開先 {dest[:160]}")
            print(f"  {cur} → {typ}\t{key}\t→ {dest[:80]}(展開先そのものが上位)", flush=True)
    if found and apply:
        add_paths(found, how="機械で追加", upgrade=True)
        print(f"  → 短縮 URL {len(found)}件を表に追加・更新(展開先の種別を写した)", flush=True)
    return left


# --- リンク元からの種別の引き継ぎ(編集長 2026-10-21) ------------------------------------------------------
# 公式の投稿・ページからリンクで行った先は公式、準公式からなら準公式。ただしリンク先がその告知の内容と関係していると
# 確かめたときだけ。種別 = max(リンク元の種別(関係を確かめたときだけ), リンク先そのものの種別)。
# 関係の判断は合議(prompts/classify-link.md)、コードは強いほうを採って判定表 link_types に1件ずつ残すだけ。
# 2026-10-21 号: アイマスch 公式の配信告知が張ったお便りフォームが、合議で「準公式 / 不明」に割れて未確認のまま載った
ANSWER_LINK = ("host", "related", "why")           # prompts/classify-link.md の答えの欄
RELATED = ("関係する", "関係しない")


def link_checked_path() -> Path:
    """合議が「関係しない」で一致したリンク先とリンク元の組(Git 管理外)。同じ組を判定のたびに問い直さない。"""
    return ROOT / "metrics" / "link-checked.json"


def load_link_checked() -> dict[str, str]:
    try:
        d = json.loads(link_checked_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


_PUBLISHED: dict = {}     # (ROOT, 号) → published_citations の結果(同じ実行で何度も読まない)


def published_citations(date: str) -> dict[str, list[str]]:
    """号 date より前の、追記だけの号(試験発行 number: 0 の号は除く)の記事が出典にしているリンク先: キー(link_key)→ 記事。
    ここにあるリンク先の種別を変えると、付け直し(retag_sources)が公開済みの記事を書き換え、append-only の lint で取引ごと
    戻る(組版前なら発行前の lint で止まる)。公開済みの記事の種別は訂正の手順で直すので、自動では変えない。"""
    if (str(ROOT), date) in _PUBLISHED:
        return _PUBLISHED[(str(ROOT), date)]
    live: dict[str, bool] = {}
    out: dict[str, list[str]] = {}
    for post in sorted((ROOT / "docs" / "_posts").glob("????-??-??-*.md")):
        d = post.name[:10]
        if d >= date:
            continue
        if d not in live:
            ed = ROOT / "docs" / "_editions" / f"{d}.md"
            m = re.search(r"(?m)^number:\s*(\d+)", ed.read_text(encoding="utf-8")) if ed.exists() else None
            live[d] = not (m and int(m.group(1)) == 0)
        if not live[d]:
            continue
        m = re.match(r"^---\n(.*?)\n---\n", post.read_text(encoding="utf-8"), re.S)
        try:
            fm = (yaml.safe_load(m.group(1)) or {}) if m else {}
        except yaml.YAMLError:
            continue
        for s in fm.get("sources") or [] if isinstance(fm, dict) else []:
            k = link_key(s.get("url")) if isinstance(s, dict) and s.get("url") else None
            if k and post.stem not in out.setdefault(k, []):
                out[k].append(post.stem)
    _PUBLISHED.clear()
    _PUBLISHED[(str(ROOT), date)] = out
    return out


def held_for_correction(k: str, posts: list[str]) -> str:
    """公開済みの記事が引用しているので自動では種別を変えないリンク先の記録(決まらなかったものの説明の形)。"""
    return (f"{k}: 公開済みの記事 {', '.join(posts[:3])}{' ほか' if len(posts) > 3 else ''} が出典にしている。種別を変えると公開済みの"
            "記事が変わるので、自動では変えない(訂正の手順で直す)")


def own_link_type(url: str) -> str:
    """リンク先そのものの種別。短縮 URL は展開先の種別とも比べ、強いほう(短縮そのものは持ち主を持たない)。"""
    t = classify_source(url)
    if (urllib.parse.urlparse(url).hostname or "").removeprefix("www.") in SHORTENERS:
        dest = expand_short(url)
        if dest:
            t = min((t, classify_source(dest)), key=src_rank)
    return t


def link_candidates(date: str) -> tuple[dict[str, tuple[str, str, list[tuple[str, str]]]], dict[str, str]]:
    """リンク元の種別を引き継ぐかを確かめる対象: リンク先のキー → (URL, リンク先そのものの種別, [(リンク元, 種別)…])。
    リンク元はリンク先そのものより強いものだけ、強い順。合議が「関係しない」と一致した組(リンク先とリンク元)は除く
    (除いたあとに残る弱いリンク元は対象のまま)。対象は判定の対象(target_rows)と、組版前(POSTS_ONLY)はその号の記事の出典の
    全部(未確認でないものも)。公開済みの記事が出典にしているリンク先は対象にせず、2つ目の戻り値(キー → 説明)に分ける
    (published_citations)。"""
    idx, _ = link_index()
    checked = load_link_checked()
    pub = published_citations(date)
    rows = target_rows(date) + (unresolved_post_sources(POSTS_ONLY, every=True) if POSTS_ONLY else [])
    out: dict[str, tuple[str, str, list[tuple[str, str]]]] = {}
    held: dict[str, str] = {}
    for r in rows:
        url = ((r.get("url") or "").split() or [""])[0]
        k = link_key(url) if url else None
        if not k or k in out or k in held or not idx.get(k):
            continue
        own = own_link_type(url)
        srcs = sorted(((s, t) for s in idx[k] for t in [classify_source(s)]
                       if t != "未確認" and src_rank(t) < src_rank(own) and f"{k} <- {s}" not in checked),
                      key=lambda st: src_rank(st[1]))
        if not srcs:
            continue
        if pub.get(k):
            held[k] = held_for_correction(k, pub[k]) + f"。リンク元 {srcs[0][0]}({srcs[0][1]})"
            continue
        out[k] = (url, own, srcs)
    return out, held


def link_page(url: str) -> str:
    """合議の材料: リンク先のページ(短縮は展開先)の題名・説明・冒頭。"""
    dest = expand_short(url) if (urllib.parse.urlparse(url).hostname or "").removeprefix("www.") in SHORTENERS else ""
    page = dest or url
    title, desc, _ = page_meta(page)
    return "\n".join([f"リンク先: {url}" + (f"(展開先 {dest})" if dest else ""), f"title: {title or '-'}",
                      f"description: {desc or '-'}", f"ページ冒頭: {page_excerpt(page, 800)}"])


def link_source_text(s: str) -> str:
    """合議の材料: リンク元の告知(素材の題名と facts)。"""
    t, facts = link_index()[1].get(s, ("", []))
    return f"リンク元: {s}\n  題名: {t or '-'}\n  内容: " + (" / ".join(facts)[:1500] or "(素材に内容なし)")


def add_links(found: dict[str, tuple[str, str]]) -> int:
    """リンク元から引き継いだ種別を link_types(リンク先1つ → 種別)へ足す。既にあれば、強くなるときだけ書き換える。
    行の根拠に、どのリンク元から引き継いだかを残す(後から確かめられるように)。戻り値は実際に書いた行の数。"""
    n = 0
    p = ROOT / "source_types.yml"
    text = p.read_text(encoding="utf-8")
    if not _span(text, "link_types"):
        text = text.rstrip("\n") + ("\n\n# --- リンク元から引き継いだ種別(リンク先1つずつ。キーは pipelib.link_key)。合議がリンク元の告知と関係して"
                                   "いると確かめたものだけ(classify_sources.inherit_from_links)。リンク先そのものの種別より強いときだけ効く ---\n"
                                   "link_types:\n")
    for k, (t, why) in found.items():
        if link_key("https://" + k) != k:      # link_key が判定表に書ける形しか返さないので、ここに来るのはコードの欠陥
            print(f"  ★{k} はリンク先のキーの形でないので足さない", flush=True)
            continue
        a, b = _span(text, "link_types")
        why = re.sub(r"\s+", " ", str(why))     # 根拠に改行があると表が壊れる
        line = f"  {k}: {t}{' ' * max(1, 40 - len(k))}# {why}\n"
        old = re.compile(rf"^  {re.escape(k)}:\s*(\S+).*\n?", re.M).search(text, a, b)
        if old is None:
            text, n = text[:a] + line + text[a:], n + 1
        elif src_rank(t) < src_rank(old.group(1)):
            text, n = text[:old.start()] + line + text[old.end():], n + 1
    write_source_table(text, p)
    return n


def inherit_from_links(date: str, apply: bool, limit: int = 60) -> list[str]:
    """リンク元のほうが強いリンク先について、リンク先とリンク元の**組ごと**に、告知と関係しているかを合議に確かめさせる。
    関係すると一致した組のうち最も強いリンク元の種別を link_types に残す(リンク先そのものと強いほうを採るのは
    pipelib.classify_source)。「関係しない」と一致した組だけを記録し、見せていない組・割れた組は次の判定でまた問う。
    戻り値は決まらなかったものの説明。"""
    todo, held = link_candidates(date)
    for s in held.values():
        print(f"  引き継がない(公開済み)\t{s}", flush=True)
    pairs = [(k, s, t) for k in sorted(todo) for s, t in todo[k][2]]
    if not pairs:
        return list(held.values())
    ids = {f"L{i + 1}": p for i, p in enumerate(pairs[:limit])}
    left = list(held.values()) + [f"{k}: リンク元 {s}({t})の種別を引き継ぐかの確かめが上限を超え、次の判定に回した"
                                  for k, s, t in pairs[limit:]]
    print(f"\n{date}: リンク元のほうが強いリンク先 {len(todo)}件・組 {len(pairs)}件(今回 {len(ids)}組)→ "
          + ", ".join(f"{k}({todo[k][1]}←{t})" for k, _, t in ids.values()), flush=True)
    from concurrent.futures import ThreadPoolExecutor
    targets = sorted({k for k, _, _ in ids.values()})
    with ThreadPoolExecutor(max_workers=8) as ex:
        pages = dict(zip(targets, ex.map(lambda k: link_page(todo[k][0]), targets)))
    mats = {i: f"### {i}\n{pages[k]}\n{link_source_text(s)}" for i, (k, s, _) in ids.items()}
    agreed, split = consensus(lambda ks: render_prompt("classify-link", TARGETS="\n\n".join(mats[i] for i in ks)),
                              list(ids), need=ANSWER_LINK, field="related", accept=RELATED)
    best: dict[str, tuple[str, str, str]] = {}     # リンク先 → (種別, リンク元, 根拠)。関係すると一致した組のうち最も強いもの
    checked = load_link_checked()
    for i, (verdict, why) in agreed.items():
        k, s, t = ids[i]
        print(f"  {verdict}\t{k}\tリンク元 {s}({t})\t{why}", flush=True)
        if verdict == "関係する":
            if k not in best or src_rank(t) < src_rank(best[k][0]):
                best[k] = (t, s, why)
        else:
            checked[f"{k} <- {s}"] = f"{date} 関係しない: {why}"
    found = {k: (t, f"リンク元から引き継ぎ(合議で関係を確認): {s}({t})。{why}") for k, (t, s, why) in best.items()}
    for row in split:
        i, _, rest = row.partition(": ")
        k, s, t = ids.get(i, (i, "", ""))
        print(f"  不一致・保留\t{k}\tリンク元 {s}({t})\t{rest}", flush=True)
        if k not in best:
            left.append(f"{k}: リンク元 {s}({t})の種別を引き継ぐかで合議が割れた: {rest}")
    if apply:
        if found:
            print(f"  → リンク元から引き継いだ種別 {add_links(found)}件を表に追加", flush=True)
        try:
            link_checked_path().parent.mkdir(parents=True, exist_ok=True)
            link_checked_path().write_text(json.dumps(checked, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            print(f"  関係しないと決まった組を保存できない({e})", flush=True)
    return left


BOOTH_ITEM = re.compile(r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?items/(\d+)/?", re.I)
_BOOTH_SHOP: dict[str, tuple[str, str]] = {}   # 商品 ID → (ショップのホスト, ショップ名)。同じ実行で何度も問い合わせない


def booth_item_key(url: str) -> str | None:
    """`booth.pm/<言語>/items/<ID>` の商品の URL なら、判定表(path_types)に書くキー(`booth.pm/ja/items/<ID>`。URL のパスのまま)。"""
    u = urllib.parse.urlparse(url)
    if (u.hostname or "").removeprefix("www.") != "booth.pm":
        return None
    m = BOOTH_ITEM.fullmatch(u.path)
    return "booth.pm" + u.path.rstrip("/") if m else None


def booth_item_shop(item_id: str, fetch=None) -> tuple[str, str]:
    """BOOTH の商品の持ち主のショップ。(ショップのホスト `<subdomain>.booth.pm`, ショップ名)。取れなければ ("", "")。
    商品の JSON(`booth.pm/ja/items/<ID>.json`)の shop.subdomain(出品者の構造化フィールド)だけを見る。説明文からは推定しない。
    fetch(url) -> 本文 を渡せば取得しない(テスト用)。
    実測 2026-10-19: items/7563732.json は shop {"subdomain": "nagarestar", "name": "流れ星"} を返す。"""
    url = f"https://booth.pm/ja/items/{item_id}.json"
    try:
        if fetch is None:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with polite_urlopen(req, timeout=20) as r:
                body = r.read(PAGE_READ_BYTES).decode("utf-8")      # 読み取りは上限まで(終わらない応答を溜め込まない。2026-10-27)
        else:
            body = fetch(url)
        shop = json.loads(body).get("shop") or {}
    except Exception as e:
        print(f"  BOOTH の商品の JSON を取得できず {item_id}: {type(e).__name__}", flush=True)
        return "", ""
    sub = str(shop.get("subdomain") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", sub):
        return "", ""
    return f"{sub}.booth.pm", str(shop.get("name") or "")


def booth_shop(item_key: str) -> tuple[str, str]:
    """`booth_item_shop` を商品ごとに1回だけ呼ぶ(display_base は出典行ごとに引く)。item_key は booth_item_key の形。"""
    iid = item_key.rsplit("/", 1)[1]
    if iid not in _BOOTH_SHOP:
        _BOOTH_SHOP[iid] = booth_item_shop(iid)
    return _BOOTH_SHOP[iid]


def resolve_booth_items(apply: bool) -> list[str]:
    """判定表に無い BOOTH の商品へ、ショップの種別を機械で写す(path_types に商品1つずつ。x_posts・短縮 URL と同じ)。
    ショップの合議の後に呼ぶ。戻り値は決まらなかったものの説明(`<URL>: 理由`)。"""
    found: dict[str, tuple[str, str]] = {}
    left: list[str] = []
    for item, (shop, url) in sorted(BOOTH_ITEMS.items()):
        if classify_source(url) != "未確認":
            continue
        typ = classify_source(f"https://{shop}/")
        if typ == "未確認":
            left.append(f"{url}: BOOTH の商品。ショップ {shop} の種別が決まらない(ショップの合議を参照)")
        else:
            found[item] = (typ, f"ショップ {shop}")
            print(f"  {typ}\t{item}\t{shop}", flush=True)
    if found and apply:
        add_paths(found, how="機械で追加")
        print(f"  → BOOTH の商品 {len(found)}件を表に追加(ショップの種別を写した)", flush=True)
    return left


def anon_x_author(url: str) -> tuple[str, str]:
    """投稿者の無い X の投稿 URL なら (投稿 ID, 投稿者)。投稿者を引けなければ投稿者は ""。それ以外は ("", "")。"""
    m = X_ANON_POST.fullmatch(url)
    if not m:
        return "", ""
    pid = m.group(1)
    if pid not in _X_AUTHOR:
        _X_AUTHOR[pid] = x_post_author(pid)
    return pid, _X_AUTHOR[pid]
PARSER_REQUESTS = ROOT / "metrics" / "parser-requests.json"   # Git 管理外。ホスト → 当番に依頼した日(同じ依頼を1日5回出さない)
PARSER_RETRY_DAYS = 3                 # 依頼してもパーサが入らないまま(当番が直せなかった等)なら、この日数のあとにもう一度出す


def request_parsers(date: str, need: dict[str, str], escalate_fn=None) -> list[str]:
    """パーサの無い URL について、**人に「決まらない」と言う前に、当番(Opus 修正 → Sol 査読)にパーサを書かせる依頼を出す**。

    編集長 2026-09-26:「この手のやつが来たら『パースしろ』という判定をしろ」「なんでその設計判断なしにこっちに
    決まりませんなんて言ってんだ」「大体のエラー、申告前に診断しろ」。判定の単位を URL から取れないのは
    このコードの欠陥(パーサの不足)で、欠陥は当番が直す。依頼はホストごとに1つ、同じホストは PARSER_RETRY_DAYS の
    あいだ出し直さない(収集は1日5回走る)。戻り値は決まらなかった記録の行(URL: 理由)。
    """
    from pipelib import escalate
    escalate_fn = escalate_fn or escalate
    record = _load_parser_requests()
    by_host: dict[str, list[str]] = {}
    for url, host in sorted(need.items()):
        by_host.setdefault(host, []).append(url)
    fresh = {h: urls for h, urls in by_host.items()
             if not (record.get(h) or {}).get("date") or (record[h]["date"] < _days_before(date, PARSER_RETRY_DAYS))}
    status: dict[str, str] = {}
    if fresh:
        hosts_text = "\n".join(
            f"- {h}\n" + "\n".join(f"  - {u}" + (f"(使われ方: {' / '.join(USED_IN.get(u) or [])})" if USED_IN.get(u) else "")
                                   for u in urls)
            for h, urls in sorted(fresh.items()))
        reason = render_prompt("oncall-parser", HOSTS=hosts_text)
        sent = escalate_fn("classify", date, reason)
        for h in fresh:
            if sent:
                record[h] = {"date": date, "urls": fresh[h][:10]}
                status[h] = f"持ち主を取るパーサが無い。当番(Opus 修正 → Sol 査読)にパーサを書かせる依頼を {date} に出した"
            else:
                status[h] = "持ち主を取るパーサが無い。当番を呼べなかった(ONCALL=off か当番が無い)。人がパーサを書くこと"
        _save_parser_requests(record)
    for h in by_host:
        if h not in status:      # 依頼済み(PARSER_RETRY_DAYS 以内)
            status[h] = f"持ち主を取るパーサが無い。当番に依頼済み({record[h]['date']})。直らなければ {PARSER_RETRY_DAYS} 日後に出し直す"
    return [f"{u}: {status[h]}" for u, h in sorted(need.items())]


def _load_parser_requests() -> dict:
    try:
        record = json.loads(PARSER_REQUESTS.read_text(encoding="utf-8"))
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_parser_requests(record: dict) -> None:
    """原子的に書く(一時ファイル → fsync → replace)。途中で死んで記録を失うと、次の収集が全ホストを出し直す(監査指摘 r85)。"""
    import os
    try:
        PARSER_REQUESTS.parent.mkdir(parents=True, exist_ok=True)
        tmp = PARSER_REQUESTS.with_name(PARSER_REQUESTS.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, indent=1))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PARSER_REQUESTS)
    except OSError as e:
        print(f"  パーサの依頼の記録を保存できない({e})", flush=True)


def forget_parser_requests(hosts: set[str]) -> None:
    """依頼の記録から、そのホストを消す(当番の修正で直らなかったとき。次の収集が出し直す)。"""
    record = _load_parser_requests()
    left = {h: v for h, v in record.items() if h not in hosts}
    if left != record:
        _save_parser_requests(left)


def _days_before(date: str, days: int) -> str:
    import datetime
    return (datetime.date.fromisoformat(date) - datetime.timedelta(days=days)).isoformat()


def used_in(key: str) -> str:
    return ("\n紙面・候補での使われ方: " + " / ".join(USED_IN[key])) if USED_IN.get(key) else ""


POSTS_ONLY: str | None = None    # 号の日付。組版前の判定では、その号の記事の出典だけを対象にする(候補は見ない)


def target_rows(date: str) -> list[dict]:
    """判定の対象: その号の候補 + 紙面に載った未確認の出典。組版前(POSTS_ONLY)は、その号の記事の出典だけ。
    判定表に無い短縮 URL は、その**展開先**も対象に足す(短縮そのものは持ち主を持たない。展開先がドメイン・アカウント・動画の
    どれでも、同じ規則で決まる。決まった種別は resolve_short_links が短縮へ写す)。"""
    if POSTS_ONLY:
        rows = unresolved_post_sources(POSTS_ONLY)
    else:
        p = ROOT / "candidates" / f"{date}.json"
        rows = (json.loads(p.read_text(encoding="utf-8")) if p.exists() else []) + unresolved_post_sources()
    dests = []
    for c in rows:
        url = ((c.get("url") or "").split() or [""])[0]
        if url and platform_unit(url)[0] == "short" and classify_source(url) == "未確認":
            dest = expand_short(url)
            if dest:
                dests.append({"url": dest, "title": f"{c.get('title') or ''}(短縮 URL {url} の展開先)"})
    return rows + dests


def unresolved_post_sources(date: str | None = None, only: set[str] | None = None, every: bool = False) -> list[dict]:
    """紙面に載っている**未確認**の出典(全号。date を渡すとその号だけ。only を渡すとそのファイル名の記事だけ。every なら
    未確認でない出典も)。執筆が自分で見つけた URL は候補に無いので、候補だけ見ていると紙面の未確認が残る。収集は次号の日付で
    走るので、既定では号で絞らない。"""
    rows = []
    for post in sorted((ROOT / "docs" / "_posts").glob(f"{date}-*.md" if date else "*.md")):
        if only is not None and post.name not in only:
            continue
        text = post.read_text(encoding="utf-8")
        if not every and "未確認" not in text:
            continue
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            continue
        for s in fm.get("sources") or []:
            if isinstance(s, dict) and s.get("url") and (every or s.get("type") == "未確認"):
                rows.append({"url": s["url"], "title": f"{fm.get('title') or ''}(記事の出典: {s.get('label') or ''})"})
    return rows


def page_meta(url: str) -> tuple[str, str, str]:
    """(title, meta description, 生 HTML)。JS で描画するページでも title/description は残っている。"""
    try:
        if browser_only(url):
            # 素の取得に 429 を返すサイトは描画して読む(2026-10-26 編集長「429を出すようなアクセスはするな。Chromeを使え。」)
            html = render_page(url, 30)
        else:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ja"})
            with polite_urlopen(req, timeout=20) as r:
                html = r.read(400_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        d = re.search(r'<meta[^>]+(?:name|property)=["\'](?:description|og:description|og:site_name)["\'][^>]+content=["\']([^"\']*)', html, re.I)
        return (re.sub(r"\s+", " ", t.group(1)).strip() if t else "", (d.group(1).strip() if d else ""), html)
    except Exception:
        return "", "", ""


def rendered_excerpt(url: str, chars: int = 1200) -> tuple[str, str]:
    """(本文の冒頭, 取得状態)。JS 描画のページは fetch_page.py が描画して読み直す。取得状態は、取れなかった・描画できずに
    描画前の素の本文(メニューだけ等)で続けた、のときに原因(終了コードと stderr の要点)を書く。正常なら空。
    以前は fetch_page.py の stdout だけを読み、描画の失敗(stderr に出て終了コード0で続く)を捨てていた。
    分類の材料がメニューだけになっても、それが描画の時間切れのせいだと分類側にも当番にも見えなかった(監査指摘
    render-diagnostic-lost-in-classification)"""
    try:
        r = run_group([sys.executable, str(ROOT / "scripts" / "fetch_page.py"), url, "--chars", str(chars)],
                      timeout=150, cwd=ROOT)
    except Exception as e:
        return "", f"取得できず: {type(e).__name__}: {e}"[:300]
    if r.returncode != 0 or "--- 本文(要約なし) ---" not in r.stdout:
        head = next((l.strip() for l in r.stdout.splitlines() if l.strip()), "")
        return "", (f"取得できず: {head[:200]} / " if head else "取得できず: ") + render_failure(r.returncode, r.stderr)
    body = re.sub(r"\s+", " ", r.stdout.split("--- 本文(要約なし) ---", 1)[1]).strip()[:chars]
    if "RENDER_FAILED:" in (r.stderr or ""):
        return body, "描画できず、描画前の素の本文で代用(メニューだけのことがある): " + render_failure(r.returncode, r.stderr)
    return body, ""


def _with_status(text: str, status: str) -> str:
    return text + (f"\n取得状態: {status}" if status else "")


def site_profile(host: str, url: str, top: str | None = None) -> str:
    """「このサイトは何の主体か」を掘るための材料: 当該ページ、サイトのトップ(プラットフォーム上の
    アカウントならそのアカウントのページ)、運営者情報(会社概要・About・特定商取引法)のページ。
    URL の字面だけで判定しない(編集長の指摘)。"""
    parts = []
    title, desc, _ = page_meta(url)
    body, status = rendered_excerpt(url)
    parts.append(_with_status(f"代表URL: {url}\ntitle: {title or '-'}\ndescription: {desc or '-'}\nページ冒頭: {body}", status))
    top = top or f"https://{host}/"
    ttitle, tdesc, thtml = page_meta(top)
    top_text, top_status = rendered_excerpt(top, 3000)
    parts.append(_with_status(f"サイトのトップ {top}\ntitle: {ttitle or '-'}\ndescription: {tdesc or '-'}\n冒頭: {top_text[:800]}", top_status))
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
        text, about_status = rendered_excerpt(about, 4000)
        import os as _os
        common = len(_os.path.commonprefix([top_text, text]))
        text = text[common:] if common > 200 else text
        parts.append(_with_status(f"運営者情報 {about}({about_kw}):\n{text[:1200]}", about_status))
    else:
        parts.append("運営者情報: トップから会社概要・About・特定商取引法のリンクを見つけられず(サイト内を fetch_page.py で探すこと)")
    return "\n".join(parts)


def page_excerpt(url: str, chars: int = 1200) -> str:
    """判断材料としてページ本文の冒頭を取る。取れなくても続ける。"""
    try:
        if browser_only(url):
            text = html_to_text(render_page(url, 30).encode("utf-8", "replace"))     # 素の取得に 429 を返すサイトは描画して読む(page_meta)
        else:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with polite_urlopen(req, timeout=20) as r:
                text = html_to_text(r.read(PAGE_READ_BYTES), r.headers.get_content_charset())
        return re.sub(r"\s+", " ", text).strip()[:chars]
    except Exception as e:
        return f"(取得できず: {type(e).__name__}: {e})"[:300]


def ask(cmd: list[str], prompt: str, timeout: int = 900) -> list[dict]:
    try:
        import hashlib
        from pipelib import partial_output, prompt_file, save_raw
        name = f"classify-{cmd[0]}-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
        short = prompt_file(edition_date(), name, prompt)
        # codex は通信を開いた workspace-write で、リポジトリ外の作業ディレクトリ(fetch_page.py の入口つき)で走らせる。
        # read-only では名前解決が通らず、依頼文どおり fetch_page.py で運営者情報を読もうとして毎回失敗していた
        # (2026-10-18 classify-codex-c9936bed: Temporary failure in name resolution)
        wd = None
        try:
            if cmd[0] == "codex":
                from collect import explore_workdir
                wd = explore_workdir("classify", entry="scripts/fetch_page.py")
                # 依頼文を書く失敗(容量不足など)でも作業ディレクトリを残さない(監査指摘 2026-10-21)
                short = prompt_file(edition_date(), name, prompt, base=wd)
            # モデル・道具は自前のグループで起動し、時間切れでは子孫まで止める(組 P)
            r = run_group(cmd + [short], timeout=timeout, cwd=wd or ROOT)
        except subprocess.TimeoutExpired as e:
            save_raw(edition_date(), name, *partial_output(e))     # 時間切れの回も、それまでの出力を残す
            raise
        finally:
            if wd:
                shutil.rmtree(wd, ignore_errors=True)
        # 答えが読めた・読めないに関係なく生の出力を残す(決まらなかった分類の理由を後から確かめるため)
        save_raw(edition_date(), name, r.stdout, r.stderr, r.returncode)
        # 認証切れ・利用上限で答えなかったのは「決まらなかった」ではない(無回答として議論・人へ回さない)。判定を丸ごとやめて、
        # 次の判定でやり直す(main が CLI_BLOCKED_EXIT で終え、取引が判定表を開始時へ戻す)
        blocked = cli_check(cmd[0], "compose" if POSTS_ONLY else "collect", "出典の判定(合議)", r.returncode, r.stdout, r.stderr)
        if blocked:
            raise CliBlocked(blocked)
        # 異常終了の間際の出力は答えにしない(無回答として議論・人へ。監査指摘)
        rows = (extract_json_array(r.stdout) or []) if r.returncode == 0 else []
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


# ページは依頼文どおり fetch_page.py で読ませる。WebFetch は JS を描画せず、公式ポータルが見出しだけに見える(2026-10-21)
# 依頼文は ask が末尾に足す。--disallowedTools は値を幾つでも取るので、末尾に置くと依頼文まで道具の名前として呑み、
# 依頼文の無い起動になって毎回 exit 1 で答えなかった(2026-10-22: claude 側の判定が全部「答えが読めない」)。値を1つ取る旗の後ろに置かない
CMD_A = ["claude", "-p", "--disallowedTools", "WebFetch", "--model", COLLECT_MODEL, "--dangerously-skip-permissions"]
CMD_B = ["codex", "exec", "-m", EXPLORE_MODEL, "-s", "workspace-write", "--skip-git-repo-check",
         "-c", "sandbox_workspace_write.network_access=true"]


def _key_forms(s: str) -> tuple[str, str]:
    """対象の書き方の揺れを吸収するための (全体, 末尾) の正規形。
    `https://www.youtube.com/@Foo/` も `youtube.com/@foo` も `@Foo` も、末尾は `foo`。"""
    raw = re.sub(r"^[a-z]+://", "", str(s or "").strip(), flags=re.I).rstrip("/")
    head, sep, rest = raw.partition("/")
    head = head.lower().removeprefix("www.")
    path, _, query = rest.partition("?")
    # 文書の ID(Google の文書・フォームのパス、Microsoft Forms のクエリの id)は大小文字を区別する。ほかは区別しない。
    # クエリは落とすが、Microsoft Forms の id は残す(落とす・同一視すると、別のフォームどうしが同じ対象になり、別の文書に判定が付く。
    # 監査指摘 2026-10-14)
    if head in QUERY_DOC_PAGES:
        # Microsoft Forms: 固定のページ(/pages/responsepage.aspx)は小文字にそろえ、短縮の /r/<ID> の ID はそのまま
        segs = path.split("/")
        path = path.lower() if path.lower() == QUERY_DOC_PAGES[head].strip("/") else "/".join([segs[0].lower()] + segs[1:])
    elif head not in ID_CASE_HOSTS:
        path = path.lower()
    qid = query.split("&")[0] if head in QUERY_DOC_PAGES and query[:3].lower() == "id=" else ""
    full = (head + sep + path).rstrip("/").removesuffix("/about") + (f"?id={qid[3:]}" if qid else "")
    return full.lstrip("@"), full.rsplit("/", 1)[-1].lstrip("@")


ANSWER_X = ("host", "type", "why")                 # prompts/classify-x.md の答えの欄
ANSWER_SITE = ("host", "operator", "type", "why")  # prompts/classify-site.md の答えの欄


def _by_host(rows: list, keys: list[str] | None = None, need: tuple = ANSWER_X) -> dict[str, dict]:
    """モデルの答えを、**依頼した対象(keys)に対応付けて**返す。

    答えの host は依頼どおりの文字列で返ってくるとは限らない(`youtube.com/@Foo` を頼んで `@Foo` や
    `https://www.youtube.com/@Foo` で返る)。完全一致だけで引くと、答えているのに「無回答(—)」扱いになり、
    議論しても決まらない(実測 2026-09-18: @TogawaNonoha)。全体の正規形で当て、無ければ末尾(ハンドル・
    最後の区切り)で当てる。末尾は、その末尾を持つ依頼が1つだけのときに限る。対応しない答えはログに出す。
    """
    # 依頼した形(need の欄が空でない文字列。サイトは運営主体 operator も)の答えだけを答えとする。運営主体や根拠の無い
    # 答えを一致として判定表へ確定させない(形が崩れた答えは無回答。議論へ回る。監査指摘)。
    # ただし**形の検査は、対象へ対応付けて数えた後**(崩れた2つ目の答えを先に捨てると、矛盾が見えなくなる。監査指摘)
    shaped = lambda d: all(isinstance(d.get(k), str) and d[k].strip() for k in need)
    rows = [d for d in rows if isinstance(d, dict) and isinstance(d.get("host"), str)]
    if keys is None:
        return {d["host"].lstrip("@"): d for d in rows if shaped(d)}
    by_full = {_key_forms(k)[0]: k for k in keys}
    tails: dict[str, list[str]] = {}
    heads: dict[str, list[str]] = {}     # `youtube.com/@foo` を `youtube.com` とだけ返す答え(実測 2026-09-18)
    for k in keys:
        tails.setdefault(_key_forms(k)[1], []).append(k)
        if "/" in _key_forms(k)[0]:
            heads.setdefault(_key_forms(k)[0].split("/", 1)[0], []).append(k)
    out: dict[str, dict] = {}
    twice: set[str] = set()
    for d in rows:
        full, tail = _key_forms(d.get("host", ""))
        k = (by_full.get(full) or (tails[tail][0] if len(tails.get(tail) or []) == 1 else None)
             or (heads[full][0] if len(heads.get(full) or []) == 1 else None))
        if k is None:
            print(f"  答えの対象が依頼と対応しない: {str(d.get('host'))[:80]!r}", flush=True)
        elif k in out:
            twice.add(k)
        else:
            out[k] = d
    # 同じ対象に2つの答え(矛盾しうる)があれば、先頭を採らず無回答にする(議論へ回る。監査指摘)
    for k in twice:
        print(f"  同じ対象に答えが2つある(無回答として議論へ): {k}", flush=True)
        out.pop(k, None)
    return {k: d for k, d in out.items() if shaped(d)}


def debate_prompt(prompt: str, split_keys: list[str], mine: dict, theirs: dict, field: str = "type") -> str:
    """2巡目: 割れた対象について、相手の答えと根拠を見せ、検証して賛成か反論かを答えさせる。"""
    rows = []
    for k in split_keys:
        m, t = mine.get(k) or {}, theirs.get(k) or {}
        rows.append(f"- {k}: あなた={m.get(field) or '—'}({m.get('why') or '根拠なし'}) / "
                    f"相手={t.get(field) or '—'}({t.get('why') or '根拠なし'})")
    return render_prompt("classify-debate", FIRST=prompt.rstrip("\n"), ROWS="\n".join(rows))


def consensus(prompt: str, keys: list[str], need: tuple = ANSWER_SITE, field: str = "type",
              accept: tuple = ALLOWED) -> tuple[dict, list[str]]:
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
    a = _by_host(ask(CMD_A, make(keys)), keys, need)
    b = _by_host(ask(CMD_B, make(keys)), keys, need)

    # field は答えの欄(種別なら type、リンク先の関係なら related)、accept は一致として採る値
    def agree(k):
        ta, tb = (a.get(k) or {}).get(field), (b.get(k) or {}).get(field)
        return ta if (ta and ta == tb and ta in accept) else None

    split_keys = [k for k in keys if not agree(k)]
    if split_keys:
        print(f"  1巡目で割れた {len(split_keys)}件を議論させる: "
              + ", ".join(f"{k}({(a.get(k) or {}).get(field) or '—'}/{(b.get(k) or {}).get(field) or '—'})" for k in split_keys),
              flush=True)
        a2 = _by_host(ask(CMD_A, debate_prompt(make(split_keys), split_keys, a, b, field)), split_keys, need)
        b2 = _by_host(ask(CMD_B, debate_prompt(make(split_keys), split_keys, b, a, field)), split_keys, need)
        # 議論した対象は2巡目の答えで置き換える。2巡目に形の揃った答えが無ければ無回答にする(1巡目の答えを残すと、
        # 崩れた・矛盾した2巡目を出した側の古い答えで一致が成立してしまう。監査指摘)
        for k in split_keys:
            a[k] = a2.get(k) or {}
            b[k] = b2.get(k) or {}
    agreed, split = {}, []
    for k in keys:
        t = agree(k)
        if t:
            agreed[k] = (t, str((a.get(k) or {}).get("why", ""))[:40])
        else:
            da, db = a.get(k) or {}, b.get(k) or {}
            split.append(f"{k}: {da.get(field) or '—'}「{str(da.get('why') or '')[:60]}」 / "
                         f"{db.get(field) or '—'}「{str(db.get('why') or '')[:60]}」")
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
    """ドメインを種別ごとの一覧へ足す。その種別の一覧がまだ表に無ければ作る。
    ドメイン単位で決まらないホスト(短縮 URL・フォーム・プラットフォーム)は足さない(ドメイン全体の登録で、別の主体の
    短縮・フォームまで同じ種別になった。監査指摘 2026-10-14)。"""
    p = ROOT / "source_types.yml"
    text = p.read_text(encoding="utf-8")
    for h, (t, why) in agreed.items():
        if platform_unit(f"https://{h}/") != ("domain", h.removeprefix("www.")):
            print(f"  ★{h} はドメイン単位で決まらない({platform_unit(f'https://{h}/')[0]})ので、ドメインとしては足さない")
            continue
        if not _span(text, LIST_OF[t]):
            text = text.rstrip("\n") + f"\n\n{LIST_OF[t]}:\n"
        sp = _span(text, LIST_OF[t])
        if _listed(text, LIST_OF[t], h, ci=True):
            print(f"  {h} は既に {LIST_OF[t]} にある(足さない)")
            continue
        text = text[:sp[0]] + f"  - {h}{' ' * max(1, 22 - len(h))}# 合議で追加: {why}\n" + text[sp[0]:]
    write_source_table(text, p)


def add_paths(agreed: dict, how: str = "合議で追加", upgrade: bool = False) -> None:
    """プラットフォーム上のアカウント・チャンネル・作品ページ・文書を path_types(パス → 種別)へ足す。
    how は**どうやって決めたか**(合議で追加 / 機械で追加)。決め方を偽って記録しない。
    upgrade なら、既にある行も新しい種別のほうが上位のときだけ書き換える(短縮の展開先が上位になったとき)。"""
    p = ROOT / "source_types.yml"
    text = p.read_text(encoding="utf-8")
    if not _span(text, "path_types"):
        text = text.rstrip("\n") + ("\n\n# --- プラットフォーム上のアカウント・チャンネル・作品ページ(パス単位で主体が決まる) ---\n"
                                   "# tiktok.com/@…、ch.nicovideo.jp/…、manga.nicovideo.jp/comic/… など。合議が足す\n"
                                   "path_types:\n")
    for k, (t, why) in agreed.items():
        a, b = _span(text, "path_types")
        why = re.sub(r"\s+", " ", str(why))     # 根拠に改行があると表が壊れる
        line = f"  {k}: {t}{' ' * max(1, 40 - len(k))}# {how}: {why}\n"
        # 既にあるかは、表の検査と同じ比べ方で見る(文書・短縮の ID は大小文字を区別する。pipelib.path_type_norm)
        old = next((m for m in re.finditer(r"^  (\S+?):\s*(\S+).*\n?", text[a:b], re.M)
                    if path_type_norm(m.group(1)) == path_type_norm(k)), None)
        if old is None:
            text = text[:a] + line + text[a:]
        elif upgrade and src_rank(t) < src_rank(old.group(2)):
            text = text[:a + old.start()] + line + text[a + old.end():]
    write_source_table(text, p)


def add_video_channels(agreed: dict) -> None:
    """YouTube のチャンネル(ハンドル)を video_channels の該当種別へ足す。"""
    _add_labeled("video_channels", agreed, "合議で追加", ci=True, show=lambda h: f"@{h}")


def add_x_accounts(agreed: dict) -> None:
    """x_accounts の該当種別の下へ足す。無ければその種別の節を作る。"""
    _add_labeled("x_accounts", agreed, "合議で追加", ci=True, show=lambda a: f"@{a}")


def resolve_x_posts(apply: bool) -> None:
    """投稿者の無い X の投稿 ID を、投稿者のアカウントの種別で x_posts へ機械で足す(合議には掛けない)。

    記事に載った `x.com/i/status/<ID>` は URL を書き換えられない(過去紙面は種別しか変えられない)ので、
    投稿 ID で引ける表に写す。アカウントが表に無い(合議で決まらなかった)ものは足さない。
    その理由はアカウントの合議の記録に残り、watch はアカウントの単位で引く。
    """
    found = {}
    for pid, handle in sorted(X_POSTS.items()):
        typ = classify_source(f"https://x.com/{handle}")
        if typ != "未確認" and classify_source(f"https://x.com/i/status/{pid}") == "未確認":
            found[pid] = (typ, f"@{handle}")
            print(f"  {typ}\tx.com/i/status/{pid}\t@{handle}", flush=True)
    if found and apply:
        p = ROOT / "source_types.yml"
        text = p.read_text(encoding="utf-8")
        if not _span(text, "x_posts"):
            write_source_table(text.rstrip("\n") + "\n\n# --- 投稿者の無い X の投稿(x.com/i/status/<ID>)。投稿者のアカウントの種別を機械で写す ---\nx_posts:\n", p)
        _add_labeled("x_posts", found, "機械で追加", ci=False)
        print(f"  → X の投稿 ID {len(found)}件を表に追加", flush=True)


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


_NICO_HOSTS = ("nicovideo.jp", "www.nicovideo.jp", "live.nicovideo.jp", "live2.nicovideo.jp", "sp.nicovideo.jp", "sp.live.nicovideo.jp",
               "embed.nicovideo.jp", "nico.ms")


class _NicoId:
    """ニコニコの番組・動画 URL の ID。ホストとパスの形を検める(部分一致で拾わない。監査指摘 r82)。
    `NICO_ID.search(url)` で YT_ID と同じ使い方ができる(group(1) が ID)。"""

    def search(self, url: str):
        u = urllib.parse.urlparse(str(url or "").split()[0] if url else "")
        host = (u.hostname or "").lower()
        if u.scheme not in ("http", "https", "") or host not in _NICO_HOSTS:
            return None
        seg = [s for s in u.path.split("/") if s]
        if host == "nico.ms":
            vid = seg[0] if len(seg) == 1 else ""
        else:
            vid = seg[1] if len(seg) == 2 and seg[0] == "watch" else ""
        m = re.fullmatch(r"(lv|sm|so|nm)\d+", vid)
        return re.match(r"(.*)", vid) if m else None


NICO_ID = _NicoId()


def nico_watch_url(vid: str) -> str:
    return f"https://live.nicovideo.jp/watch/{vid}" if vid.startswith("lv") else f"https://www.nicovideo.jp/watch/{vid}"


def unknown_nico(date: str) -> dict[str, str]:
    """判定表に無いニコニコの生放送・動画 ID(lv…/sm…/so…)→ 代表 URL。"""
    out: dict[str, str] = {}
    for c in target_rows(date):
        url = ((c.get("url") or "").split() or [""])[0]
        m = NICO_ID.search(url)
        if m and classify_source(url) == "未確認":
            out.setdefault(m.group(1), url)
    return out


_NICO_CACHE: dict[str, tuple[str, str, str]] = {}


def nico_channel(vid: str) -> tuple[str, str, str]:
    """`nico_channel_fetch` を番組 ID ごとに1回だけ呼ぶ(watch は同じ URL を出典行ごとに引く。監査指摘 r82)。"""
    if vid not in _NICO_CACHE:
        _NICO_CACHE[vid] = nico_channel_fetch(vid)
    return _NICO_CACHE[vid]


def nico_channel_alias(ch_id: str, ch_url: str = "") -> str:
    """チャンネルの別名(ch.nicovideo.jp/sidem のような)を、チャンネルページの canonical から取る。取れなければ ""。"""
    try:
        import urllib.request
        req = urllib.request.Request(ch_url or f"https://ch.nicovideo.jp/channel/{ch_id}", headers={"User-Agent": "Mozilla/5.0"})
        with polite_urlopen(req, timeout=20) as r:
            final = r.geturl()
            cpage = r.read(200_000).decode("utf-8", "replace")
        c = re.search(r'<link rel="canonical" href="https://ch\.nicovideo\.jp/([^"/?#]+)', cpage)
        seg = (c.group(1) if c else urllib.parse.urlparse(final).path.strip("/").split("/")[0])
        return seg if seg and seg != "channel" else ""
    except Exception:
        return ""


def nico_channel_fetch(vid: str, page: str | None = None, fetch_alias: bool = True) -> tuple[str, str, str]:
    """ニコニコの生放送・動画ページから、チャンネル(ch.nicovideo.jp の ID と別名)と題名を取る。page を渡せば取得しない(テスト用)。
    (channel_key, channel_id, title)。channel_key は判定表の path_types に書く単位(`ch.nicovideo.jp/<別名>` か
    `ch.nicovideo.jp/channel/<ch id>`)。ユーザー投稿は投稿者のユーザーページ `nicovideo.jp/user/<id>` を単位にする
    (channel_id は "")。取れないときは ("", "", title)。
    実測 2026-09-26: live.nicovideo.jp/watch/lv… の埋め込みデータに socialGroup {type: channel, id: ch2606757,
    socialGroupPageUrl: https://ch.nicovideo.jp/channel/ch2606757} がある。
    実測 2026-10-09: www.nicovideo.jp/watch/sm… は data-api-data を持たず、<meta name="server-response"> の
    data.response に channel / owner {id: 17835786} がある。"""
    import html as _html
    if page is None:
        try:
            import urllib.request
            req = urllib.request.Request(nico_watch_url(vid), headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ja"})
            with polite_urlopen(req, timeout=20) as r:
                page = r.read(600_000).decode("utf-8", "replace")
        except Exception as e:
            print(f"  ニコニコのページを取得できず {vid}: {type(e).__name__}", flush=True)
            return "", "", ""
    t = re.search(r"<title>([^<]*)</title>", page)
    title = _html.unescape(t.group(1)).replace(" - ニコニコ生放送", "").replace(" - ニコニコ動画", "").strip() if t else ""
    props = None
    m = (re.search(r'id="embedded-data"[^>]*data-props="([^"]*)"', page) or re.search(r'data-api-data="([^"]*)"', page)
         or re.search(r'<meta name="server-response" content="([^"]*)"', page))
    if m:
        try:
            props = json.loads(_html.unescape(m.group(1)))
        except ValueError:
            props = None
    if isinstance(props, dict) and isinstance(props.get("data"), dict) and isinstance(props["data"].get("response"), dict):
        props = props["data"]["response"]      # server-response の包み(meta / data.response)
        if not title and isinstance(props.get("video"), dict):
            title = str(props["video"].get("title") or "")      # この形のページには <title> が無い
    # 投稿主体を示す構造化フィールドだけを見る(生放送: socialGroup{type: channel} / 動画: channel)。HTML 全体から
    # `ch.nicovideo.jp/channel/ch…` を拾うと、説明文に貼られた別チャンネルのリンクを投稿主体と取り違える(監査指摘 r82)
    ch_id = ch_url = ""
    if isinstance(props, dict):
        sg = props.get("socialGroup") if isinstance(props.get("socialGroup"), dict) else None
        if sg is not None:
            if sg.get("type") == "channel" and re.fullmatch(r"ch\d+", str(sg.get("id") or "")):
                ch_id, ch_url = str(sg["id"]), str(sg.get("socialGroupPageUrl") or "")
        elif isinstance(props.get("channel"), dict):
            ch = props["channel"]
            cid = str(ch.get("id") or "")
            if cid.isdigit():
                cid = "ch" + cid
            if re.fullmatch(r"ch\d+", cid):
                ch_id, ch_url = cid, str(ch.get("url") or "")
    if not ch_id:
        # ユーザー投稿・ユーザー生放送は投稿者(個人のユーザーページ)が持ち主。単位が無いと合議に掛からず未確認のまま残る
        uid = ""
        if isinstance(props, dict):
            sg = props.get("socialGroup") if isinstance(props.get("socialGroup"), dict) else None
            sup = (props.get("program") or {}).get("supplier") if isinstance(props.get("program"), dict) else None
            if sg is None and isinstance(props.get("owner"), dict):
                uid = str(props["owner"].get("id") or "")
            elif isinstance(sup, dict) and sup.get("supplierType") == "user":
                uid = str(sup.get("programProviderId") or "")
        if uid.isdigit():
            return f"nicovideo.jp/user/{uid}", "", title
        return "", "", title
    alias = nico_channel_alias(ch_id, ch_url) if fetch_alias else ""
    key = f"ch.nicovideo.jp/{alias}" if alias else f"ch.nicovideo.jp/channel/{ch_id}"
    return key, ch_id, title


def resolve_nico(date: str, apply: bool) -> tuple[list[str], dict[str, tuple[str, str, str]]]:
    """チャンネルが判定表(path_types の ch.nicovideo.jp/…)にあるニコニコの生放送・動画の ID を、video_ids へ機械で足す。
    YouTube(resolve_videos)と同じ: 投稿者で種別が決まり、URL には ID しか無い。
    戻り値は (決まらなかったものの説明, チャンネルが表に無いもの: key → (ID, 題名, 代表 URL))。"""
    vids = unknown_nico(date)
    if not vids:
        return [], {}
    print(f"\n{date}: 判定表に無いニコニコの生放送・動画 {len(vids)}件", flush=True)
    found: dict[str, tuple[str, str]] = {}
    left: list[str] = []
    unknown_chans: dict[str, tuple[str, str, str]] = {}
    for vid, url in sorted(vids.items()):
        key, ch_id, title = nico_channel(vid)
        typ = classify_source(f"https://{key}/") if key else "未確認"
        if key and typ != "未確認":
            found[vid] = (typ, f"{key}「{title[:30]}」")
            print(f"  {typ}\t{vid}\t{key} {title[:40]}")
        else:
            left.append(f"nicovideo:{vid}({key or '?'})")
            if key:
                unknown_chans.setdefault(key, (vid, title, url))
    if found and apply:
        add_video_ids(found)
        print(f"  → 動画 ID {len(found)}件を表に追加")
    return left, unknown_chans


def video_author(vid: str) -> tuple[str, str]:
    """oEmbed で投稿者のハンドルと題名を取る。(handle, title)。取れなければ ("", "")。"""
    try:
        import urllib.request
        q = urllib.parse.urlencode({"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"})
        req = urllib.request.Request(f"https://www.youtube.com/oembed?{q}", headers={"User-Agent": UA})
        with polite_urlopen(req, timeout=20) as r:
            d = json.loads(r.read(PAGE_READ_BYTES).decode("utf-8"))
        handle = urllib.parse.unquote((d.get("author_url") or "").rstrip("/").rsplit("/", 1)[-1]).removeprefix("@")
        if handle:
            return handle, str(d.get("title") or "")
        print(f"  oEmbed に投稿者が無い {vid}。視聴ページから読む", flush=True)
    except Exception as e:
        print(f"  oEmbed 取得できず {vid}: {type(e).__name__}。視聴ページから読む", flush=True)
    # oEmbed が断続的に失敗する(実測 2026-09-12: Lantis の動画が HTTPError で未確認のまま紙面に載った)。
    # 配信前の枠は 200 で author_url も title も無い応答を返す(実測 2026-10-31: iVOtp0VZh8g が未確認のまま紙面に載った)。
    # 視聴ページの埋め込み情報(ownerProfileUrl / <link itemprop=name>)から投稿者を取る
    try:
        import urllib.request
        req = urllib.request.Request(f"https://www.youtube.com/watch?v={vid}",
                                     headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ja"})
        with polite_urlopen(req, timeout=20) as r:
            html = r.read(PAGE_READ_BYTES).decode("utf-8", "replace")
        m = re.search(r'"ownerProfileUrl":"[^"]*?/@([^"/]+)"', html) or re.search(r'"canonicalBaseUrl":"/@([^"/]+)"', html)
        t = re.search(r'<meta name="title" content="([^"]*)"', html) or re.search(r'<title>([^<]*)</title>', html)
        if m:
            return m.group(1), (t.group(1) if t else "").replace(" - YouTube", "")
        print(f"  視聴ページからも投稿者が取れず {vid}", flush=True)
    except Exception as e:
        print(f"  視聴ページ取得できず {vid}: {type(e).__name__}", flush=True)
    return "", ""


def yt_playlist_key(url: str) -> str | None:
    """YouTube の再生リストの URL なら、判定表(path_types)に書くキー `youtube.com/playlist?list=<ID>`。それ以外は None。"""
    k = query_doc_key(url)
    return k if k and k.startswith("youtube.com/playlist?") else None


def playlist_author(list_id: str, fetch=None) -> tuple[str, str]:
    """再生リストの持ち主のハンドルと題名を oEmbed で取る。(handle, title)。取れなければ ("", "")。
    oEmbed の author_url(投稿主体の構造化フィールド)だけを見る。説明文や収録動画の投稿者からは推定しない
    (他人の動画を集めた再生リストでは、収録動画の投稿者は持ち主ではない)。fetch(url) -> 本文 を渡せば取得しない(テスト用)。
    実測 2026-10-17: list=PLVWdhK_WANzc の oEmbed は author_url "/@imas-official" を返す。"""
    import urllib.request
    q = urllib.parse.urlencode({"url": f"https://www.youtube.com/playlist?list={list_id}", "format": "json"})
    try:
        if fetch is None:
            req = urllib.request.Request(f"https://www.youtube.com/oembed?{q}", headers={"User-Agent": UA})
            with polite_urlopen(req, timeout=20) as r:
                body = r.read(PAGE_READ_BYTES).decode("utf-8")
        else:
            body = fetch(f"https://www.youtube.com/oembed?{q}")
        d = json.loads(body)
    except Exception as e:
        print(f"  再生リストの oEmbed 取得できず {list_id}: {type(e).__name__}", flush=True)
        return "", ""
    last = urllib.parse.unquote(str(d.get("author_url") or "").rstrip("/").rsplit("/", 1)[-1])
    # 判定表のチャンネルはハンドルで持つ。`/channel/UC…` の形はハンドルではないので取れなかったことにする
    return (last[1:] if last.startswith("@") and len(last) > 1 else ""), str(d.get("title") or "")


_PLAYLIST_CACHE: dict[str, tuple[str, str]] = {}


def playlist_owner(key: str) -> tuple[str, str]:
    """`playlist_author` を再生リストごとに1回だけ呼ぶ(display_base は出典行ごとに引く)。key は yt_playlist_key の形。"""
    lid = key.split("list=", 1)[1]
    if lid not in _PLAYLIST_CACHE:
        _PLAYLIST_CACHE[lid] = playlist_author(lid)
    return _PLAYLIST_CACHE[lid]


def unknown_playlists(date: str) -> dict[str, str]:
    """判定表に無い YouTube の再生リスト(キー → 代表 URL)。"""
    out: dict[str, str] = {}
    for c in target_rows(date):
        url = ((c.get("url") or "").split() or [""])[0]
        k = yt_playlist_key(url)
        if k and classify_source(url) == "未確認":
            out.setdefault(k, url)
    return out


def resolve_playlists(date: str, apply: bool) -> tuple[list[str], dict[str, tuple[str, str]]]:
    """持ち主のチャンネルが表(video_channels)にある再生リストへ、チャンネルの種別を path_types に機械で写す(動画と同じ。
    合議には掛けない)。戻り値は (決まらなかったものの説明, チャンネルが表に無いもの: handle → (再生リストのキー, 題名))。"""
    lists = unknown_playlists(date)
    if not lists:
        return [], {}
    t = source_type_table()
    chans = {h.lower(): typ for typ, hs in (t.get("video_channels") or {}).items() for h in hs or []}
    print(f"\n{date}: 判定表に無い YouTube 再生リスト {len(lists)}件", flush=True)
    found: dict[str, tuple[str, str]] = {}
    left: list[str] = []
    unknown_chans: dict[str, tuple[str, str]] = {}
    for key, url in sorted(lists.items()):
        handle, title = playlist_owner(key)
        typ = chans.get(handle.lower()) if handle else None
        if typ:
            found[key] = (typ, f"持ち主 @{handle}「{title[:30]}」")
            print(f"  {typ}\t{key}\t@{handle} {title[:40]}")
        elif handle:
            left.append(f"https://www.{key}: 持ち主のチャンネル(@{handle})が判定表に無い(チャンネルの合議でも決まらなかった)")
            unknown_chans.setdefault(handle, (key, title))
        else:
            left.append(f"https://www.{key}: 再生リストの持ち主を引けない(oEmbed が返さない。削除・非公開か)")
    if found and apply:
        add_paths(found, how="機械で追加")
        print(f"  → 再生リスト {len(found)}件を表に追加(持ち主のチャンネルの種別を写した)")
    return left, unknown_chans


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
    from pipelib import exit_on_term
    exit_on_term()      # SIGTERM でも、起動した分類のセッションを子孫ごと止めてから終わる
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--apply", action="store_true", help="一致したものを source_types.yml に足す")
    ap.add_argument("--limit", type=int, default=60,
                    help="1回に掛ける X アカウントの数(多いと1回のプロンプトに載らない)")
    ap.add_argument("--posts-only", action="store_true",
                    help="その号の記事に載った未確認の出典だけを対象にする(組版前。候補は見ない)")
    ap.add_argument("--require-parsers", action="store_true",
                    help="当番がパーサを足したあとの再実行用。パーサの無い URL が残っていたら exit 2(成功扱いにしない)")
    args = ap.parse_args()
    # 下見(--apply なし)では通知しない。試験実行が本物の警報と混ざる
    set_quiet(not args.apply)
    date = args.date or edition_date()
    if args.posts_only:
        global POSTS_ONLY
        POSTS_ONLY = date

    # リンク元のほうが強いリンク先は、先にリンク元の告知との関係を確かめて引き継ぐ(引き継いだものは下の合議に掛からない)
    split_links = inherit_from_links(date, args.apply, args.limit)
    doms, accts, paths = unknown_targets(date)
    # 持ち主の無い URL は、黙って飛ばさず理由ごと残す(決まらなかったものとして下でまとめて出る)
    split_all = [f"{u}: 判定できない({why})" for u, why in sorted(SKIPPED.items())]
    for s in split_all:
        print(f"  判定の対象にできない\t{s}", flush=True)
    split_all += split_links
    # 持ち主を取るパーサが無い URL は、人に「決まらない」と申告する前に、当番にパーサを書かせる(下見では依頼しない)
    if NEED_PARSER and args.require_parsers:
        # 当番の再実行: パーサを足したのに同じ種類が残っている(ホスト内の別の形の URL を取り漏らした等)なら失敗にする
        # (成功扱いにすると当番が「再実行成功」と報告し、依頼の記録が残るあいだ自動修正が止まる。監査指摘 r85)。
        # 残ったホストの依頼の記録を消して、次の収集が出し直せるようにする(回数の上限は当番側の attempt)
        forget_parser_requests(set(NEED_PARSER.values()))
        for u, h in sorted(NEED_PARSER.items()):
            print(f"  パーサがまだ無い\t{u}({h})", flush=True)
        print(f"パーサの無い URL が {len(NEED_PARSER)}件 残っている。再実行は失敗", flush=True)
        return 2
    if NEED_PARSER:
        rows = request_parsers(date, NEED_PARSER) if args.apply else \
            [f"{u}: 持ち主を取るパーサが無い({h})。下見なので当番は呼ばない" for u, h in sorted(NEED_PARSER.items())]
        for s in rows:
            print(f"  パーサが無い\t{s}", flush=True)
        split_all += rows
    if (not doms and not accts and not paths and not X_POSTS and not SHORT_LINKS and not ALL_SHORTS and not BOOTH_ITEMS and not YT_CHANNELS
            and not unknown_videos(date) and not unknown_nico(date) and not unknown_playlists(date)):
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

    # 公開済みの記事が出典にしている文書は、自動では種別を決めない(決めると付け直しが公開済みの記事を書き換え、append-only の
    # lint で取引ごと戻る。リンク元の索引を広げたので、公開済みの未確認のフォームにもリンク元が付いて合議が一致しうる。2026-10-21)
    pub = published_citations(date)
    for k in sorted(paths):
        lk = link_key("https://" + k)
        if is_document(k) and lk and pub.get(lk):
            split_all.append(held_for_correction(k, pub[lk]))
            print(f"  決めない(公開済み)\t{split_all[-1]}", flush=True)
            paths.pop(k)
    # 文書(フォーム・ドキュメント)は、**リンク元**で決める。リンク元が手元のデータで分かるものは合議に掛けない
    decided = {}
    for k in sorted(paths):
        if is_document(k):
            got = by_link_source(k)
            if got:
                decided[k] = got
                print(f"  リンク元で決定 {got[0]}\t{k}\t{got[1]}", flush=True)
    if decided:
        if args.apply:
            add_paths(decided, how="機械で追加")
            print(f"  → パス {len(decided)}件を表に追加(リンク元で決定。合議には掛けていない)")
        paths = {k: u for k, u in paths.items() if k not in decided}

    if paths:
        # プラットフォーム上のアカウント・チャンネル・作品ページ。「トップ」はそのアカウントのページ
        print(f"\n{date}: 判定表に無いプラットフォーム上の主体 {len(paths)}件 → {', '.join(sorted(paths))}", flush=True)
        # 「トップ」はそのアカウントのページ。文書(フォーム等)にはトップが無いので、文書そのものを見せる。
        # 合議に回る文書(リンク元で決められなかったもの)には、分かっているリンク元を材料として付ける
        items = [(k, u, site_profile(k, u, top=(u if is_document(k) else f"https://{k}/")) + used_in(k)
                  + (link_hint(k) if is_document(k) else ""))
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
        agreed, split = consensus(lambda ks: build_x_prompt({k: sub[k] for k in ks}), sorted(sub), need=ANSWER_X)
        for a, (t, why) in agreed.items():
            print(f"  一致 {t}\t@{a}\t{why}")
        for s in split:
            print(f"  不一致・保留\t@{s}")
        split_all += split
        if agreed and args.apply:
            add_x_accounts(agreed)
            print(f"  → X アカウント {len(agreed)}件を表に追加")
    # 投稿者の無い X の投稿は、投稿者のアカウントの種別を投稿 ID へ写す(アカウントの合議の後)
    resolve_x_posts(args.apply)

    # YouTube はチャンネルが表にあれば合議なしで決まる(人が決めた種別を写すだけ)。
    # チャンネルが表に無ければ、チャンネルのページ(概要)を材料に合議で種別を決めて表に足し、
    # そのうえで動画 ID を引き直す
    left, unknown_chans = resolve_videos(date, args.apply)
    # 再生リストも持ち主のチャンネルで決まる。チャンネルが表に無ければ、動画のチャンネルと一緒に合議に掛ける
    pl_left, pl_chans = resolve_playlists(date, args.apply)
    left += pl_left
    # 同じチャンネルは大文字小文字の違いを1つにしてから合議に掛ける(2つの対象にすると、答えの対応付けで重なって無回答になる。Sol 監査 2026-10-27)
    merged: dict = {}
    for h, v in list(unknown_chans.items()) + list(pl_chans.items()) + list(YT_CHANNELS.items()):
        if not any(x.lower() == h.lower() for x in merged):
            merged[h] = v
    unknown_chans = merged
    if unknown_chans:
        print(f"\n{date}: 判定表に無い YouTube チャンネル {len(unknown_chans)}件 → "
              + ", ".join(f"@{h}" for h in sorted(unknown_chans)), flush=True)
        items = [(f"youtube.com/@{h}", f"https://www.youtube.com/@{h}/about",
                  site_profile(f"youtube.com/@{h}", f"https://www.youtube.com/@{h}/about", top=f"https://www.youtube.com/@{h}")
                  + f"\n表に載った動画・再生リスト: {vid}「{title[:60]}」")
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
            left += resolve_playlists(date, True)[0]
            # チャンネルのページは動画・再生リストの引き直しに出てこない。決まらなかったものは合議の不一致のまま残す
            left += [x for x in split if any(x.lower().startswith(f"youtube.com/@{h.lower()}:") for h in YT_CHANNELS)]
        else:
            left = [x for x in left if not any(f"(@{h})" in x for h in chans)] + split
    split_all += left

    # ニコニコの生放送・動画も投稿者(チャンネル)で決まる。チャンネルが表に無ければ、そのチャンネルのページを材料に
    # 合議で決めて path_types に足し、動画 ID を引き直す(編集長 2026-09-26「これくらい curl して取れよ」)
    left, unknown_nico_chans = resolve_nico(date, args.apply)
    if unknown_nico_chans:
        print(f"\n{date}: 判定表に無いニコニコのチャンネル {len(unknown_nico_chans)}件 → {', '.join(sorted(unknown_nico_chans))}", flush=True)
        items = [(k, f"https://{k}/", site_profile(k, f"https://{k}/", top=f"https://{k}/") + f"\n表に載った番組: {vid}「{title[:60]}」")
                 for k, (vid, title, _u) in sorted(unknown_nico_chans.items())]
        agreed, split = consensus(lambda ks: build_prompt([it for it in items if it[0] in ks]), sorted(unknown_nico_chans))
        for k, (t, why) in agreed.items():
            print(f"  一致 {t}\t{k}\t{why}")
        for s in split:
            print(f"  不一致・保留\t{s}")
        if agreed and args.apply:
            add_paths(agreed)
            print(f"  → チャンネル {len(agreed)}件を表に追加")
            left, _ = resolve_nico(date, True)
        else:
            left = [x for x in left if not any(f"({k})" in x for k in agreed)] + split
    split_all += left

    # BOOTH の商品は、ショップの種別が上で決まってから、その種別を商品1つずつへ写す
    split_all += resolve_booth_items(args.apply)

    # 短縮 URL は、展開先の種別が上で決まってから、その種別を短縮1つずつへ写す(展開先が決まらなければ理由を残す)
    split_all += resolve_short_links(args.apply, date)

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
    if args.apply:
        save_unresolved(split_all)
    return 0


UNRESOLVED = ROOT / "metrics" / "classify-unresolved.json"    # Git 管理外。watch が「なぜ決まらなかったか」を引く


def save_unresolved(rows: list[str]) -> None:
    """決まらなかった対象と、両モデルの言い分を残す(キー → 説明)。次の判定で上書き。
    翌朝の watch が「未確認の出典」を出すとき、どこで割れたかを併記する(編集長: 何の記事のためのリンクか、
    なぜ決まらないのかが無いと人は判断できない)。
    別の行が同じキーに落ちる(BOOTH の商品 → ショップ、動画 → チャンネル、投稿 → アカウント)。後の行で先の理由を
    上書きせず、違う理由は先の理由の後ろへ足す(合議の言い分を、後の「合議を参照」の行で消さない)。"""
    out: dict[str, str] = {}

    def put(k: str, why: str) -> None:
        why = why.strip()
        old = out.get(k, "")
        if not old:
            out[k] = why[:400]
        elif why and why not in old:
            out[k] = f"{old} / {why[:400]}"[:800]

    for s in rows:
        key, _, rest = s.partition(": ")
        key = key.strip()
        m = re.fullmatch(r"youtube:([A-Za-z0-9_-]{11})\(@([^)]*)\)", key)
        if m:      # 動画: チャンネルが表に無くて決まらなかった。表に書く単位はチャンネル。動画 ID でも引けるように両方残す
            rest = rest or "投稿者のチャンネルが判定表に無い(チャンネルの合議でも決まらなかった)"
            put(f"https://youtube.com/watch?v={m.group(1)}", rest)
            if m.group(2) != "?":
                put(f"https://youtube.com/@{m.group(2)}", rest)
            continue
        m = re.fullmatch(r"nicovideo:((?:lv|sm|so|nm)\d+)\(([^)]*)\)", key)
        if m:      # ニコニコも同じ: 番組 ID とチャンネルの両方で引ける
            rest = rest or ("投稿者のチャンネルが判定表に無い(チャンネルの合議でも決まらなかった)" if m.group(2) != "?"
                            else "番組ページから投稿者を引けなかった(合議に掛かっていない)")
            put(nico_watch_url(m.group(1)), rest)
            if m.group(2) != "?":
                put(f"https://{m.group(2)}", rest)
            continue
        put(display_base(key), rest)
    try:
        UNRESOLVED.parent.mkdir(parents=True, exist_ok=True)
        UNRESOLVED.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        print(f"  決まらなかった理由を保存できない({e})", flush=True)


def load_unresolved() -> dict[str, str]:
    try:
        d = json.loads(UNRESOLVED.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CliBlocked as e:
        # 投稿は見つけたときに済んでいる。決まらなかったものの記録(classify-unresolved.json)は書き換えない
        print(f"出典の判定をやめた(次の判定でやり直す): {e}", flush=True)
        sys.exit(CLI_BLOCKED_EXIT)
