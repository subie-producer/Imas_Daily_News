"""planlib: 面別選定の**出力契約**と、答えから計画を組み立てるコード。

以前の選定は、モデルに JSON ファイルを書かせ、その中に候補の長い id を写させ、
slug を考えさせ、「全主題を articles か dropped に必ず書け」と**文で**頼んでいた。
写し間違いは別セッションで修理し(repair_invalid_ids)、漏れは別セッションで
拾い直していた(replan_missing)。写す・数える・一意にするは機械の仕事である(監査指摘 P1-1/P1-2)。

この設計では:

- 主題ごとに**必ず1つ**の判定を返す schema を、その面の主題キーから生成する
  (`required` に全キー、`additionalProperties: false`)。漏れは構造的に起きない
- モデルが返すのは判断だけ: article / roundup / merge / drop / cross_brand と、切り口・rank・lead_score・理由
- candidate_ids は主題の ids からコードが展開し、slug はコードが付ける(一意)
- roundup は面で1本にまとめ、素材が足りなければ small の通常記事にする(規程13)
"""
import re

ROUNDUP_MIN_ITEMS = 3
ACTIONS = ("article", "roundup", "merge", "drop", "cross_brand")
RANKS = ("large", "medium", "small")
REASONS = ("既報", "過年度", "同人・ファン主催", "個人の話題", "重複", "出典不足", "面違い", "その他")


def plan_schema(keys: list[str]) -> dict:
    """その面の主題キー全部を required にした判定 schema。"""
    decision = {
        "type": "object",
        "required": ["action", "angle", "rank", "lead_score", "merge_into", "claimed_slug", "reason", "note"],
        "additionalProperties": False,
        "properties": {
            "action": {"enum": list(ACTIONS)},
            "angle": {"type": "string", "maxLength": 120,
                      "description": "記事の切り口(article/roundup のとき。roundup なら束ねる観点)"},
            "rank": {"enum": list(RANKS) + [""], "description": "article のとき。他は空"},
            "lead_score": {"type": "integer", "minimum": 0, "maximum": 100,
                           "description": "号の一面に値する度合い。面で最大の1本にだけ高く、他は0〜30"},
            "merge_into": {"type": "string", "description": "merge のとき: この面の別の主題キー(そこへ素材を統合)。他は空"},
            "claimed_slug": {"type": "string", "description": "cross_brand のとき: 他の面が既に立てた記事の slug。他は空"},
            "reason": {"enum": list(REASONS) + [""], "description": "drop のとき。他は空"},
            "note": {"type": "string", "maxLength": 160},
        },
    }
    return {
        "type": "object",
        "required": ["decisions"],
        "additionalProperties": False,
        "properties": {
            "decisions": {
                "type": "object",
                "required": list(keys),
                "additionalProperties": False,
                "properties": {k: decision for k in keys},
            }
        },
    }


def slugify(brand: str, key: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", f"{brand}-{key}".lower()).strip("-")[:70] or f"{brand}-article"
    s, n = base, 2
    while s in taken:
        s = f"{base[:66]}-{n}"
        n += 1
    taken.add(s)
    return s


def decisions_to_plan(brand: str, rows: list[dict], decisions: dict, taken: set[str]) -> dict:
    """判定 → {articles, dropped, cross_brand}(従来の面別計画と同じ形)。

    rows は plan-index の主題行(dedup_key・ids・title・...)。
    """
    by_key = {r["dedup_key"]: r for r in rows}
    arts: dict[str, dict] = {}
    dropped: list[dict] = []
    cross: list[dict] = []
    roundup_keys: list[str] = []
    merges: list[tuple[str, str]] = []
    for key, r in by_key.items():
        d = decisions.get(key) or {"action": "drop", "reason": "その他", "note": "判定なし"}
        act = d.get("action")
        if act == "article":
            arts[key] = {"slug": slugify(brand, key, taken), "brand": brand,
                         "rank": d.get("rank") if d.get("rank") in RANKS else "small",
                         "angle": d.get("angle") or r.get("title") or key,
                         "lead_score": int(d.get("lead_score") or 0), "dedup_key": key,
                         "candidate_ids": list(r.get("ids") or [])}
        elif act == "roundup":
            roundup_keys.append(key)
        elif act == "merge":
            merges.append((key, d.get("merge_into") or ""))
        elif act == "cross_brand":
            cross.append({"slug": d.get("claimed_slug") or "", "dedup_key": key, "note": d.get("note") or ""})
        else:
            dropped.append({"dedup_key": key, "reason": d.get("reason") or "その他", "note": d.get("note") or ""})

    # roundup: 面で1本。素材が足りなければ small の通常記事に戻す(規程13)
    if len(roundup_keys) >= ROUNDUP_MIN_ITEMS:
        ids = [i for k in roundup_keys for i in (by_key[k].get("ids") or [])]
        first = by_key[roundup_keys[0]]
        angles = [decisions.get(k, {}).get("angle") for k in roundup_keys if decisions.get(k, {}).get("angle")]
        arts["__roundup__"] = {"slug": slugify(brand, "ops-roundup", taken), "brand": brand, "rank": "roundup",
                               "angle": (angles[0] if angles else "定常運営まとめ"), "lead_score": 0,
                               "dedup_key": first["dedup_key"], "candidate_ids": ids,
                               "_members": list(roundup_keys)}
    else:
        for k in roundup_keys:
            r = by_key[k]
            arts[k] = {"slug": slugify(brand, k, taken), "brand": brand, "rank": "small",
                       "angle": decisions.get(k, {}).get("angle") or r.get("title") or k,
                       "lead_score": 0, "dedup_key": k, "candidate_ids": list(r.get("ids") or [])}

    # merge: 素材を相手の記事へ。相手が記事でなければ自分の記事にする(黙って消さない)
    for key, target in merges:
        tgt = arts.get(target)
        if tgt is None and target in roundup_keys and "__roundup__" in arts:
            tgt = arts["__roundup__"]
        if tgt is None:
            r = by_key[key]
            arts[key] = {"slug": slugify(brand, key, taken), "brand": brand, "rank": "small",
                         "angle": decisions.get(key, {}).get("angle") or r.get("title") or key,
                         "lead_score": 0, "dedup_key": key, "candidate_ids": list(r.get("ids") or [])}
            continue
        for i in by_key[key].get("ids") or []:
            if i not in tgt["candidate_ids"]:
                tgt["candidate_ids"].append(i)
        tgt.setdefault("_merged", []).append(key)

    out_arts = []
    for a in arts.values():
        a.pop("_members", None)
        a.pop("_merged", None)
        out_arts.append(a)
    return {"articles": out_arts, "dropped": dropped, "cross_brand": cross}
