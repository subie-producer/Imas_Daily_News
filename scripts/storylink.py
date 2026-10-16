#!/usr/bin/env python3
"""storylink: 今日の候補を、**同じ一次情報を扱った過去の記事**の話題(dedup_key)につなぎ直す。

同じ知らせが翌日以降にもう一度「新しい記事」として載る事故が多かった(実測 2026-09-15〜30: 発表から2日以上遅れて載った
新規記事 97本のうち 42本が、前の号で既に記事にした一次情報の再報道。例: 9/17 と 9/18 の「那覇市『でじます』×シンデレラコラボ決定」)。
原因は、定点観測と探索が同じ知らせを別々に拾うと dedup_key が別になり、既報の照合を「選定のモデルが既報台帳
(約1万行)を自分で読む」ことに任せていたこと。読み切れず、同じ主題を新規として通していた。

ここでは照合を**コード**がする:
1. 過去 LINK_DAYS 日の記事の出典・素材の URL を記事単位のキー(url_key)にし、一次情報 → 記事の索引を作る。
   多くの記事に出てくる URL(一覧ページ・ライブの特設ページなど。HUB_POSTS 本を超えるもの)は照合に使わない
2. 今日の候補の URL が過去の記事と一致すれば、その記事の dedup_key へつなぎ直す(台帳の同じ話題に積まれ、執筆は既報を受け取る)
3. 今日の候補どうしで URL が一致すれば、1つの dedup_key にまとめる(同じ日に定点観測と探索が拾った同じ知らせ)
選定と執筆には、主題ごとの過去の記事(prior: 号・見出し・読者に出した本文。同じ一次情報・同じ催しの別の話題の記事も添える。
同じ催しは公演ページ(EVENT_PAGE。hub になっていても)の共有で見分け、催しの記事は本数で切らずに全部渡す)、選定には面ごとの
直近の見出し(recent_titles)を渡す。記事にするか
(新しい事実・当日のトリガーがあるか)の判断はモデルがする(内容の判断はモデル。コードは照合だけ)。
"""
import collections
import datetime
import json
import re
import urllib.parse
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LINK_DAYS = 45          # 照合に使う過去の記事の日数
HUB_POSTS = 3           # これを超える本数の記事に出てくる URL は、一覧・特設ページとみなして照合に使わない
HUB_TOPICS = 3          # この数以上の別の話題(dedup_key)に使われた URL は、シリーズのページとみなして照合に使わない
RECENT_DAYS = 7         # 選定に渡す、面ごとの直近の見出しの日数
PRIOR_DAYS = 36500      # 既報として渡す過去の記事の期間(=全期間)
# 1つの催し(公演)だけを扱うページ: 公式の公演ページとその下(goods・ticket・streaming 等)。キーは催しの根(公演の ID まで)。
# 催しの発表が重なると、公演ページは多くの記事・話題に出て hub(話題の照合に使わない)になるが、同じ催しの記事であることは変わらない
EVENT_PAGE = re.compile(r"^(idolmaster-official\.jp/live_event/[^/?#]+|asobistage\.asobistore\.jp/event/[^/?#]+|livepocket\.jp/e/[^/?#]+)")
LIST_TAILS = {"", "news", "information", "topics", "top", "index", "info", "blog", "list", "article", "articles"}
BODY_LINK = re.compile(r"\]\((https?://[^)\s]+)\)")      # 本文の markdown のリンク先
FACT_URL = re.compile(r"https?://[^\s)」』\]>]+")            # 候補の facts に書かれた url(投稿に貼られたリンク・配信 URL 等)
# 記事を見分けない問い合わせ(追跡・共有・ページ送り)。これ以外の問い合わせ(?id= など)は記事を見分けるので残す
NOISE_QUERY = re.compile(r"^(utm_.*|s|t|ref|ref_src|fbclid|gclid|usp|si|feature|page|from|share|via)$")


def url_key(url) -> str:
    """記事単位の一次情報のキー。一覧・トップ・アカウントのページなど、記事を指さない URL は ""。
    ページ内の見出し(#以下)や問い合わせ(?id=)で項目を見分けるページは、それもキーに含める(一覧の URL で別の知らせを
    同じ記事とみなさない。監査指摘 r102: ランティスの発売情報の一覧で、別の商品が既報につながった)。"""
    s = (str(url or "").split() or [""])[0]
    u = urllib.parse.urlparse(s)
    host = (u.hostname or "").removeprefix("www.").removeprefix("m.")
    if not host:
        return ""
    if host in ("x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com"):
        m = re.search(r"/status(?:es)?/(\d+)", u.path)
        return f"x:{m.group(1)}" if m else ""
    if host in ("youtube.com", "youtu.be"):
        m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([A-Za-z0-9_-]{11})", s)
        return f"yt:{m.group(1)}" if m else ""
    path = re.sub(r"\.(html?|php)$", "", u.path).rstrip("/")
    segs = [p for p in path.split("/") if p]
    query = "&".join(f"{k}={v}" for k, v in sorted(urllib.parse.parse_qsl(u.query)) if not NOISE_QUERY.match(k))
    # 一覧(index・news 等)は、項目を見分ける見出し(#)か問い合わせがあるときだけ、その項目のキーにする。
    # 記事のページの見出し(#節)は同じ記事なので捨てる
    frag = ""
    if not segs or segs[-1].lower() in LIST_TAILS:
        frag = u.fragment.strip()
        if not segs or not (frag or query):
            return ""
    return host + path + (f"?{query}" if query else "") + (f"#{frag}" if frag else "")


def split_front(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    try:
        return (yaml.safe_load(m.group(1)) or {}) if m else {}
    except yaml.YAMLError:
        return {}


def published_text(text: str, fm: dict) -> str:
    """記事が**実際に読者へ出した内容**(リード+本文。事実 id の注記と空白を畳む)。
    既報台帳の published_facts は記事1本につき1〜4件の要約で、記事が書いた価格・店舗・特典の多くが抜ける。それを既報として
    渡したら、選定が「価格が新しい事実」と判断して同じ記事をもう一度載せた(実測 2026-10-01: 8番らーめんのコラボ。9/30 の記事は
    価格も店舗も書いていた)。既報の判断は、出した本文そのものと突き合わせる。"""
    body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    # 切り詰めない(後ろの段落に価格・特典・収録内容があり、切ると「新しい事実」に見える。監査指摘 r108)
    return re.sub(r"\s+", " ", f"{fm.get('lede') or ''} {body}").strip()


def past_articles(date: str, root: Path = ROOT, days: int = LINK_DAYS) -> list[dict]:
    """date より前の LINK_DAYS 日の記事: slug・号・見出し・面・dedup_key・URL キー。dedup_key はその号の計画から引く。"""
    d0 = datetime.date.fromisoformat(date)
    lo = (d0 - datetime.timedelta(days=days)).isoformat()
    plans: dict[str, dict[str, str]] = {}
    cand_keys: dict[str, dict[str, str]] = {}
    out = []
    for p in sorted((root / "docs" / "_posts").glob("*.md")):
        ed = p.name[:10]
        if not (lo <= ed < date):
            continue
        raw = p.read_text(encoding="utf-8")
        fm = split_front(raw)
        slug = str(fm.get("slug") or p.stem[11:])
        if ed not in plans:
            try:
                plans[ed] = {a.get("slug"): a.get("dedup_key") for a in
                             json.loads((root / "metrics" / f"plan-{ed}.json").read_text(encoding="utf-8")).get("articles") or []}
            except (OSError, ValueError):
                plans[ed] = {}
        dk = plans[ed].get(slug)
        ids = [str(i) for i in fm.get("candidate_ids") or []]
        urls = {url_key(s.get("url")) for s in fm.get("sources") or [] if isinstance(s, dict)}
        # 本文のリンク(読者に示した配信・販売のページ)も記事の一次情報。出典が告知ページでも、別の経路の候補(配信を告知する
        # X の投稿)は配信ページだけを共有する(2026-10-17号: 前日の記事が載せた配信ページの告知が、新規として選ばれた)
        links = {url_key(u) for u in BODY_LINK.findall(re.sub(r"\A---\n.*?\n---\n", "", raw, flags=re.S))}
        keys = {dk} if dk else set()
        if ids:
            if ed not in cand_keys:
                cand_keys[ed] = {}
                # 記事の素材はその号の候補と、その号の続報予約(sched-*)。候補だけを引くと、予約から書いた記事の話題キー・URL が
                # 照合から落ちる(素材の引き方は組版の load_materials と同じ: 候補が先。当番の指摘 f1a5050dcc と同じ型)
                for src in (root / "stock" / "scheduled" / f"{ed}.json", root / "candidates" / f"{ed}.json"):
                    try:
                        for c in json.loads(src.read_text(encoding="utf-8")):
                            cand_keys[ed][c.get("id")] = (c.get("dedup_key"), url_key(c.get("url")))
                    except (OSError, ValueError):
                        pass
            for i in ids:
                k, u = cand_keys[ed].get(i, (None, ""))
                urls.add(u)
                if k:
                    keys.add(k)
                dk = dk or k
        links -= urls                       # 本文のリンクだけに出る url(出典・素材の url でない)
        urls |= links
        urls.discard("")
        links.discard("")
        # keys = 計画の話題キーと、記事の候補が持っていた話題キー(別名)。既報はどのキーからも引ける(監査指摘 r108:
        # 計画が別の主題に統合した記事は、候補側のキーで再収集されると計画のキーだけでは既報が見えない)
        out.append({"slug": slug, "edition": ed, "title": str(fm.get("title") or ""), "brand": fm.get("brand"),
                    "dedup_key": dk, "keys": keys, "urls": urls, "links": links, "text": published_text(raw, fm)})
    return out


def hub_urls(past: list[dict]) -> set[str]:
    """話題を見分けない URL: 多くの記事に出る一覧・特設ページ(HUB_POSTS 本を超える)と、
    **3つ以上の別の話題**に使われたシリーズのページ(例: 特設 cg_mp が Memory Pict. の第6弾・第7弾…に共通。監査指摘 r104)。
    2つまでは話題を見分ける(同じ知らせが2つの話題キーで載った=二度載せの典型で、それを止めるのが照合の目的)。
    ただし本文のリンクとしてだけ出る url は、2つの話題に出れば見分けない"""
    count = collections.Counter(u for a in past for u in a["urls"])
    keys_of = collections.defaultdict(set)
    for a in past:
        for u in a["urls"]:
            keys_of[u].add(a["dedup_key"])
    # 本文のリンクとしてだけ出る url は、**2つ**の話題に出れば見分けない(記事が関連として示したページ。例: 9/4 のインタビュー
    # 記事が本文で示した催しのページに、別の催しの記事の話題がつながりかけた)
    links = set().union(*(a.get("links") or set() for a in past))
    return ({u for u, n in count.items() if n > HUB_POSTS} | {u for u, ks in keys_of.items() if len(ks - {None}) >= HUB_TOPICS}
            | {u for u in links if len(keys_of[u] - {None}) >= 2})


def link(cands: dict, date: str, root: Path = ROOT) -> list[tuple[str, str, str, str]]:
    """候補(id → 候補)の dedup_key を、同じ一次情報の過去の記事・今日の他の候補の dedup_key へつなぎ直す(候補を書き換える)。
    戻り値は (候補 id, 元の dedup_key, 新しい dedup_key, 理由) の一覧。続報予約(scheduled)は元から話題が決まっているので触らない。"""
    past = past_articles(date, root)
    hub = hub_urls(past)
    latest: dict[str, dict] = {}
    for a in sorted(past, key=lambda a: a["edition"]):
        if a["dedup_key"]:
            for u in a["urls"] - hub:
                latest[u] = a                       # 同じ URL なら、いちばん新しい記事の話題へ
    topics_of: dict[str, set[str]] = collections.defaultdict(set)
    for a in past:
        for u in a["urls"]:
            topics_of[u] |= {a["dedup_key"]} - {None}
    changed = []
    past_keys = {a["dedup_key"] for a in past if a["dedup_key"]}
    live = [c for c in cands.values() if c.get("dedup_key") and c.get("origin") != "scheduled"
            and not str(c.get("id") or "").startswith("sched-")]
    # 1. 過去の記事と同じ一次情報。**話題ごと**つなぎ直す(候補1件だけ寄せると、同じ話題の別の URL の候補が元の話題に
    #    残って割れ、そちらがまた新規として通る)。1つの話題が複数の過去記事に当たれば、いちばん新しい記事の話題へ
    #    候補の url に加え、facts に書かれた url(投稿に貼られたリンク等)でも引く(告知の投稿と告知ページは url が違い、
    #    共有するのは配信・販売のページだけのことがある。2026-10-17号: アイマスch のラジオ)
    target: dict[str, dict] = {}
    for c in live:
        own = url_key(c.get("url"))
        # facts の url は候補が触れたページで、候補そのものではない。2つ以上の話題の記事に出るなら見分けない
        # (例: 催しのページを出典にした催しの記事とインタビューの記事)
        hits = [a for u in cand_url_keys(c) if (a := latest.get(u)) and (u == own or len(topics_of[u]) < 2)]
        if any(a["dedup_key"] == c["dedup_key"] for a in hits):
            continue                                # 既に過去の記事の話題にいる
        for a in hits:
            cur = target.get(c["dedup_key"])
            if cur is None or a["edition"] > cur["edition"]:
                target[c["dedup_key"]] = a
    for c in live:
        a = target.get(c["dedup_key"])
        if a:
            changed.append((c["id"], c["dedup_key"], a["dedup_key"], f"{a['edition']}「{a['title']}」と同じ一次情報"))
            relink(c, a["dedup_key"])
    # 2. 今日の候補どうしで同じ一次情報(最初に拾った候補の dedup_key へ。過去につないだものが先なら、それへ)
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for c in live:
        k = url_key(c.get("url"))
        if k and k not in hub:
            groups[k].append(c)
    for k, cs in groups.items():
        keys = {c["dedup_key"] for c in cs}
        if len(keys) < 2:
            continue
        # 過去の記事の話題につないだ候補があれば、その話題へ(既報の続きとして扱う)。無ければ最初に拾った候補の話題へ
        cs = sorted(cs, key=lambda c: (str(c.get("found_at") or "9"), str(c.get("id"))))
        head = next((c for c in cs if c["dedup_key"] in past_keys), cs[0])
        # 1本の候補を寄せると、その候補の元の話題に属する他の候補も寄せる(話題を割らない)
        for c in live:
            if c["dedup_key"] in keys and c["dedup_key"] != head["dedup_key"]:
                changed.append((c["id"], c["dedup_key"], head["dedup_key"], "同じ日の別の候補と同じ一次情報"))
        for c in live:
            if c["dedup_key"] in keys:
                relink(c, head["dedup_key"])
    return changed


def cand_url_keys(c: dict) -> list[str]:
    """候補の一次情報のキー: url と、facts に書かれた url(重複・記事を指さないものを除く。url が先)。"""
    return [k for k in dict.fromkeys([url_key(c.get("url")),
                                      *(url_key(u) for f in c.get("facts") or [] for u in FACT_URL.findall(str(f)))]) if k]


def event_key(uk: str) -> str:
    """url_key → 催しのキー(公演ページの根)。公演ページでなければ ""。"""
    m = EVENT_PAGE.match(uk or "")
    return m.group(1) if m else ""


def relink(c: dict, key: str) -> None:
    """候補の話題を key へつなぎ直し、元の話題キーを `linked_from` に残す。元のキーの既報も引けるように(つなぎ先の話題の
    記事だけを既報にすると、元の話題で書いた記事の事実が新しく見える。2026-10-16号: 765×961 ライブの出演者)"""
    if c["dedup_key"] != key:
        c["linked_from"] = sorted(set(c.get("linked_from") or []) | {c["dedup_key"]})
        c["dedup_key"] = key


def prior_keys(cands) -> set[str]:
    """候補群の既報を引く話題キー: いまの dedup_key と、つなぎ直す前のキー(linked_from)。"""
    return {k for c in cands for k in [c.get("dedup_key"), *(c.get("linked_from") or [])] if k}


class PriorIndex:
    """既報を引く索引。`index(話題キー群, URL 群)` → 過去の記事(号・見出し・**読者に出した本文**)の一覧。**全期間**から取る
    (URL の照合は LINK_DAYS 日だが、既報は古くても既報。監査指摘 r107)。
    - 既報: キーのどれか(計画のキー・候補のキー=別名)を持つ記事、または URL(一覧・特設ページを除く)が同じ記事。
      **本数で切らない**(新しい順の本数で切ると、同じ話題の古い記事が書いた事実が新しく見える。実データで 130 の話題が3本以上、
      最大8本。hub でない URL は HUB_POSTS 本以下)
    - 同じ一次情報の別の話題(`related: true`): 候補・既報の記事と、話題を見分ける一次情報・特設ページ(hub_urls でないもの)を
      共有する別の記事。**本数で切らない**(hub でない URL は HUB_POSTS 本以下・3つ未満の話題にしか出ないので、それで有界)
    - 同じ催しの記事(`related: true`): 候補・既報の記事の URL にある公演ページ(EVENT_PAGE。hub になっていても)を出典に持つ記事の
      **全部**。`events=False` なら添えず、`events()` が催しのキーだけを返す(選定の索引は催しの記事を催しごとに1度だけ
      別のファイルに書き、主題には催しのキーを付ける。`event_rows()`)。
    出演者・ゲスト・チケットが別々に発表される催しは発表ごとに話題キーが分かれ、キーだけで引くと別の発表の記事が書いた事実(出演者など)
    が新しく見える(2026-10-16号: 765×961 ライブの 765側出演者は 9/30 号に載っていたが、ゲスト発表の 10/14 号の話題につながった候補には
    10/14 号の本文しか渡らず、新情報として選ばれた)。催しの記事を新しい順の本数で切ると、発表が重なるにつれて古い発表(出演者)の記事が
    押し出されて同じことが起きる(監査指摘 related-prior-evicts-known-cast)。hub は話題の照合(link)から外すための条件で、関連の提示には
    使わない(監査指摘 event-page-becomes-hub)"""

    def __init__(self, date: str, root: Path = ROOT):
        self.past = sorted(past_articles(date, root, days=PRIOR_DAYS), key=lambda a: (a["edition"], a["slug"]), reverse=True)
        self.hub = hub_urls(self.past)
        self.by_key: dict[str, list[dict]] = collections.defaultdict(list)
        self.by_url: dict[str, list[dict]] = collections.defaultdict(list)
        self.by_event: dict[str, list[dict]] = collections.defaultdict(list)
        for a in self.past:
            for k in a["keys"]:              # 計画のキーと、候補が持っていたキー(別名)のどちらからも引ける
                self.by_key[k].append(a)
            for u in a["urls"] - self.hub:
                self.by_url[u].append(a)
            for e in sorted({event_key(u) for u in a["urls"]} - {""}):
                self.by_event[e].append(a)

    def _direct(self, keys, urls) -> tuple[set[str], dict[int, dict]]:
        uks = {url_key(u) for u in urls} - {""}     # 催しは hub の公演ページからも見分けるので、hub を除く前のもの
        direct = {id(a): a for k in sorted({k for k in keys if k}) for a in self.by_key.get(k, [])}
        direct.update({id(a): a for u in sorted(uks - self.hub) for a in self.by_url.get(u, [])})
        return set().union(uks, *(a["urls"] for a in direct.values())), direct

    def events(self, keys, urls=()) -> list[str]:
        """候補・既報の記事の URL にある催しのキー(過去の記事があるものだけ)。"""
        shared, _ = self._direct(keys, urls)
        return sorted(e for e in {event_key(u) for u in shared} - {""} if self.by_event.get(e))

    def event_rows(self, ev: str) -> list[dict]:
        """催しの過去の記事の全部(新しい順)。"""
        return [{"edition": a["edition"], "title": a["title"], "text": a["text"]} for a in self.by_event.get(ev, [])]

    def __call__(self, keys, urls=(), events: bool = True) -> list[dict]:
        shared, direct = self._direct(keys, urls)
        evs = {event_key(u) for u in shared} - {""} if events else set()
        shared -= self.hub
        rest = [a for a in self.past if id(a) not in direct]
        related = ([a for a in rest if a["urls"] & shared]
                   + [a for a in rest if not a["urls"] & shared and evs & {event_key(u) for u in a["urls"]}])
        rows = [{"edition": a["edition"], "title": a["title"], "text": a["text"]} for a in self.past if id(a) in direct]
        return rows + [{"edition": a["edition"], "title": a["title"], "related": True, "text": a["text"]} for a in related]


def prior_lookup(date: str, root: Path = ROOT) -> PriorIndex:
    """既報の索引(PriorIndex)。"""
    return PriorIndex(date, root)


def recent_titles(date: str, root: Path = ROOT, days: int = RECENT_DAYS) -> dict[str, list[str]]:
    """面 → 直近 days 日の記事の見出し(「号 見出し」)。URL が違う同じ話題を、選定が見分けるための材料。"""
    out: dict[str, list[str]] = collections.defaultdict(list)
    for a in sorted(past_articles(date, root, days), key=lambda a: (a["edition"], a["slug"])):
        out[str(a["brand"] or "other")].append(f"{a['edition']} {a['title']}")
    return out
