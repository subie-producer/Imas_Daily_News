# パイプライン運用設計(collect / compose / publish)

作成日: 2026-07-11 ／ REQUIREMENTS.md 4〜6章の実装設計。データ契約は REQUIREMENTS.md 3章と schema/ が正。

## 0. 実行環境と全体像

**実行主体はローカルマシン(WSL)の cron。** 理由: 執筆・探索を担う `claude`(Claude Code)と、X動向収集・校閲を担う `grok`(Grok Build)の両 CLI がこのマシンでセッション認証済みであり、API キー運用なしで日々の実行が完結するため。GitHub 側の役割は **lint(required check)と Pages 配信のみ**。

- 執筆 AI = Claude(`claude -p` ヘッドレス)/ 校閲 AI = Grok(`grok -p` ヘッドレス)。**別ベンダー校閲の要件(4.5)をこの分担で満たす。**
- PC が稼働していない日は収集も発行も止まる=事実上の休刊。異常はブランチ残存監視と Discord 通知で検知する(§7)。
- クラウド Routine への移行は、grok の認証を非対話で持ち出せるようになった時点で再検討する。

```
[cron 07:30/12:30/18:30/23:30 JST]   [cron 03:30]      [cron 04:00]
scripts/collect.py                   collect.py        scripts/publish.py
 ├ A-1 定点観測(RSS/HTML差分)       (締切前スイープ)   ├ compose: claude -p(記事・号・社説生成)
 ├ A-2 探索: claude -p(Web検索)                       ├ ローカル lint → push → PR 作成
 ├ B   X動向: grok -p(構造化出力)                      ├ 校閲: grok -p(チェックリスト)
 ├ verify(一次ソース照合)                              ├ 指摘→修正コミット(最大2往復)
 └ commit & push → edition/YYYY-MM-DD                  └ lint green+approve → squash merge
                                                          → ブランチ削除 → 翌日ブランチ作成
```

## 1. 収集の2系統

### 系統A: Claude Code として収集できるもの

**A-1 定点観測(コード・決定論)** — `sources.yml` に宣言した URL 群を巡回し、RSS の新着とページの差分を検出する。LLM を使わない純 Python。取得結果はそのまま candidates に正規化する(`origin: watch`)。

**A-2 探索(Claude ヘッドレス)** — `claude -p` を `.claude/commands/collect-explore.md`(リポジトリ管理のプロンプト)で起動。ブランド別クエリセットで Web 検索し、定点観測が拾えない話題(まとめ記事、地方メディア、コラボ先企業の告知等)を発見する。出力は candidates スキーマの JSON に構造化。探索由来は `verify: unconfirmed` で登録(`origin: explore`)。

### 系統B: Grok 経由(X/Twitter 系)

`grok -p --json-schema <candidates準拠>` で X の動向を収集する(`origin: explore`)。担当領域:

1. **公式アカウントのポスト**(サイトに載らない告知・リマインド)
2. **トレンド・ファンの話題**(現象として扱えるもののみ。個人ポスト単体は記事化しない=編集規程4)
3. **ランキングのトレンド分**(名鑑アイドルの言及量。REQUIREMENTS 3.3)

X 由来の情報は原則「未確認」バッジ。verify が公式サイト等の一次ソースで裏取りできた場合のみ confirmed に昇格し「公式」等に変わる。**X のポスト URL 単独では出典にしない**(公式アカウントの一次告知ポストは例外的に可、その場合バッジは「公式」)。

### sources.yml 初期セット(案)

実装時(Step 3)に疎通・RSS 有無を確認して確定する。宣言形式: `{ id, brand, type: rss|html, url, selector? }`

| 区分 | 対象(案) |
|------|----------|
| 総合公式 | アイドルマスター公式ポータル NEWS、バンダイナムコエンタメ公式ニュース(アイマス絞り込み) |
| ブランド公式 | 765AS/シンデレラ(デレステ)/ミリオン(ミリシタ)/シャニ(シャニマス・シャニソン)/SideM/学マスの各公式サイト NEWS、ヴイアラ公式 |
| ストア・EC | アソビストア新着、ランティス/日本コロムビア リリース情報 |
| 報道 | 4Gamer・ファミ通・GAME Watch・インサイド・アニメイトタイムズ・コミックナタリー/音楽ナタリー(タグ/検索 RSS) |
| イベント | バンナムフェス等の特設、アニメイト・とらのあな等の店舗企画(必要に応じ追加) |

## 2. candidates のライフサイクル

1. collect 実行ごとに `candidates/YYYY-MM-DD.json`(**収集日**)へ追記。`dedup_key`(正規化: ブランド+主題+イベント日)で系統間の重複を排除し、同一候補の再検出は `facts` の増分マージにする。
2. verify: 候補ごとに一次ソース URL をフェッチし、日付・内容の一致を照合 → `verify: confirmed / unconfirmed / failed`。**一次ソースに書かれていない事実は facts に入れない**(絶対規則)。
3. 未来の予定(発売日・イベント日が先のもの)は `stock/upcoming.yml` へも登録し、ダイジェスト「明日/継続中」と発売日リマインド記事の種にする。
4. 締切: **発行日 04:00 時点の candidates**(前日分+当日未明分)。

## 3. 鮮度・続報ポリシー(「変わり映えしない内容」の排除)

### ストーリー台帳 `stock/stories.yml`

話題(ストーリー)単位の既報管理。collect が dedup_key 一致で束ね、compose が published_facts を追記する。

```yaml
- story_id: sidem-10th-live        # dedup_key と同系の正規化ID
  brand: sidem
  subject: "SideM 10周年ライブ"
  status: active                   # active | closed
  first_published: { edition: 2026-08-01, slug: sidem-10th-announce }
  published_facts:                 # 紙面に載せた事実(compose が追記)
    - "2026-10-10〜11 開催"
    - "会場は..."
```

### 記事化の判定規則(compose プロンプトの規程)

| # | 状況 | 扱い |
|---|------|------|
| 1 | 新規ストーリー | 記事化してよい |
| 2 | 既存ストーリー+**新事実あり**(published_facts に無い fact) | **続報として記事化OK**。本文は新事実を中心に、経緯の要約は1段落まで。rank は原則初報以下 |
| 3 | 既存ストーリー+新事実なし | **記事化NG**。ダイジェスト「継続中」行での言及のみ可 |
| 4 | 開催中・受付中の定常経過 | 「継続中」行が担当。記事は節目(開幕・締切前日・千秋楽・結果発表)のみ |

- 校閲チェックリストの**ブロック項目**に「前号までの既報と実質同内容(新事実なし)の記事」を追加する。
- lint は形式面のみ担保: 続報記事(既存 story_id 参照)が stories.yml の published_facts 追記を伴うこと。実質判定は校閲 AI。

## 4. 記事分量基準(本文のみ・見出し/lede除く)

| rank | 本文分量 | 位置づけ |
|------|---------|----------|
| lead | 800〜1200字 | 一面トップ。背景・経緯まで |
| large | 500〜800字 | 主要記事 |
| medium | 300〜500字 | 標準記事 |
| small | 150〜250字 | 短信 |

- 下限割れ・大幅超過は lint **警告**(ブロックしない)。
- **報道メディア由来のみ**をソースとする記事は、上限を1ランク下の値に抑える(要約が元記事の代替にならない分量。編集規程2)。
- 分量が下限に届かない話題は無理に膨らませず rank を下げる(water増し禁止)。

## 5. ブランチ運用(1号=1ブランチ)

- 号ごとに `edition/YYYY-MM-DD`(発行日)ブランチを **main から作成**。日中の collect コミットと早朝の compose コミットはすべてこのブランチに載る。
- 発行 = PR を **squash merge**(マージコミット題: `第N号 YYYY-MM-DD 発行`)。main の履歴は「1号=1コミット」になり、candidates・stories の更新も同じコミットに同梱される。
- マージ直後に当該ブランチを削除し、**すぐに翌日のブランチを main から作成**する(publish.py の最終ステップ)。
- **発行忘れ検知**: 朝の監視(09:00 cron)で「発行日が今日以前の edition ブランチが残っている」= 未発行として Discord 通知。
- 発行後の訂正は `correction/<slug>` ブランチを main から切り、通常の lint+校閲を経てマージ(REQUIREMENTS 4.6)。

## 6. compose と校閲

- **compose**(04:00): `claude -p` を `.claude/commands/compose.md` で起動。入力は検証済み candidates+stock+stories+前号までの紙面。出力は `_posts`/`_editions`/`_editorials` の新規ファイル+stories.yml 追記+機械算出フィールドはスクリプト(`scripts/derive.py`)で埋める。生成後ただちにローカル lint を回し、赤なら compose 内で自己修正させる。
- **校閲**(04:30目安): `grok -p` に固定チェックリスト(`prompts/review-checklist.md`)+PR diff を渡す。判定は JSON(`--json-schema`)で受け取る。
  - **ブロック**: 出典にない事実/URL捏造/新事実なしの続報/個人攻撃・プライバシー/時制矛盾
  - **コメントのみ**: 表記ゆれ・字数・面白さ
- 指摘→Claude が修正コミット→再校閲、最大2往復。lint green+校閲 approve で squash merge(gh CLI)。06:00 の Pages 配信に間に合わせる(実質締切 05:30)。

## 7. 異常時の扱い

以下は Discord webhook で通知し、人間判断(または休刊)とする:

- candidates が 0 件/verify 全滅
- 校閲 2 往復で未解決ブロックが残る
- 05:30 までにマージ不能(lint 赤継続・PR 作成失敗等)
- 09:00 時点で edition ブランチ残存(発行忘れ・PC 停止)

休刊日は欠番とせず、号数は発行実績の連番を維持する(lint の連番検査は日付ではなく発行順)。

## 8. メトリクス

毎実行で `metrics/YYYY-MM-DD.json` に追記: 収集件数(系統別)/verify 内訳/lint 違反数/校閲往復数/各フェーズ所要時間/(取得可能なら)トークン使用量。閾値超過は Discord 通知。CLI はサブスクリプション課金のため API 従量コストは発生しない見込みだが、日次上限に収まるかは第0号期間に実測する(REQUIREMENTS 6章)。

## 9. スケジュール一覧(JST)

| 時刻 | ジョブ | 内容 |
|------|--------|------|
| 07:30 / 12:30 / 18:30 / 23:30 | collect | 定点観測+探索+Grok+verify → edition ブランチへ push |
| 03:30 | collect(締切前) | 最終スイープ |
| 04:00 | publish | compose → lint → PR → 校閲 → squash merge → 翌日ブランチ作成 |
| 09:00 | watch | 発行忘れ・メトリクス閾値の監視 → Discord |

cron は WSL 上で動かす(WSL2 では systemd timer 推奨。Windows 側タスクスケジューラで WSL を起こす構成も可)。設定手順は実装時に README へ記載。

## 10. 実装ステップ(REQUIREMENTS 7章 Step 3〜4 の分解)

| # | 作業 | 成果物 |
|---|------|--------|
| 1 | sources.yml 確定(疎通確認)+定点観測・verify 実装 | `sources.yml` `scripts/collect.py` |
| 2 | 探索プロンプト+Grok 収集プロンプト | `.claude/commands/collect-explore.md` `prompts/grok-collect.md` |
| 3 | ストーリー台帳と続報判定の組み込み | `stock/stories.yml` 更新ロジック |
| 4 | compose プロンプト+機械算出スクリプト | `.claude/commands/compose.md` `scripts/derive.py` |
| 5 | 校閲チェックリスト+publish オーケストレータ | `prompts/review-checklist.md` `scripts/publish.py` |
| 6 | 監視・Discord 通知・cron 登録 | `scripts/watch.py`+README 手順 |

### 必要な手作業(ユーザー側)

1. `gh` CLI のインストールと subie-producer での認証(PR 作成・squash merge に使用)
2. Discord webhook URL の払い出し(ローカル `.env` に保存、gitignore)
3. branch protection(main: required check `lint`)+ リポジトリの auto-merge/ブランチ自動削除設定
4. cron(または systemd timer)の登録
