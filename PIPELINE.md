# パイプライン運用設計(collect / compose / publish)

作成日: 2026-07-11 ／ REQUIREMENTS.md 4〜6章の実装設計。データ契約は REQUIREMENTS.md 3章と schema/ が正。

## 0. 実行環境と全体像

**実行主体はローカルマシン(WSL)のスケジューラ(systemd user timer)。** 理由: 下記3つの CLI がこのマシンでセッション認証済みであり、API キー運用なしで日々の実行が完結するため。GitHub 側の役割は **Pages 配信と、push 後の lint CI(事後検査)のみ**。マージ判定はローカルで完結し、main へ直接 push する(§5)。

- 執筆・探索 = Claude(`claude -p`)/ X 動向収集 = Grok(`grok -p`)/ **校閲 = Codex(`codex exec -m gpt-5.6-terra`、`.env` の REVIEW_MODEL)**。執筆と校閲が別ベンダー(Anthropic/OpenAI)となり、要件 4.5 を満たす。
- ヘッドレス実行の作法(実測済み): `codex exec` は stdin を閉じる(`< /dev/null`)・`--output-schema` は全プロパティを required に含める。`grok -p` は JSON 前に前置き文が混ざるため `--json-schema` で構造を強制する。
- ローカル秘匿値(Discord webhook 等)はリポジトリ直下の `.env` に置く(gitignore 済み。雛形は `.env.example`)。
- **自動ジョブは専用クローン `~/git/imas-ops` で動く**(systemd unit の WorkingDirectory)。ジョブはブランチ切替を伴うため、人間が閲覧・編集する `~/git/Imas_Daily_News` とは作業ツリーを完全分離する(共有すると serve 中の画面がジョブの checkout で化ける)。ops 側にも `.env` と `.venv` を配置する。
- PC が稼働していない日は収集も発行も止まる=事実上の休刊。異常はブランチ残存監視と Discord 通知で検知する(§7)。

```
[cron 07:30/12:30/18:30/23:30 JST]   [cron 03:30]      [cron 04:00]
scripts/collect.py                   collect.py        scripts/publish.py
 ├ A-1 定点観測(RSS/HTML差分)       (締切前スイープ)   ├ compose: claude -p(記事・号・社説生成)
 ├ A-2 探索: claude -p(Web検索)                       ├ ローカル lint(赤なら自己修正)
 ├ B   X動向: grok -p(構造化出力)                      ├ 校閲: codex exec(チェックリスト)
 ├ verify(一次ソース照合)                              ├ 指摘→修正コミット(最大2往復)
 └ commit & push → edition/YYYY-MM-DD                  └ lint green+approve → main へ squash merge & push
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

**X の調査は Grok を信頼するしかない。よって「Grok が観測した X 上の情報」をそういうソースとして扱う**: 公式アカウントの告知ポストは Grok の観測をもって confirmed(「公式」バッジ)、ファン発の話題は「ファン」バッジで現象として扱う。X はログイン必須のため lint の URL 生存確認・verify の機械照合は対象外(スキップ)とし、代わりに Grok への収集プロンプトで「実在のポストのみ・URL 必須・憶測除外」を強制する。

**Grok は万能クエリ1発ではなくクエリセットで回す**(実測: 1クエリでは6件止まり、ブランド別4クエリ並列で30件・所要約2分)。1回の collect で以下を並列実行する:

| # | クエリ | 狙い |
|---|--------|------|
| 1 | 765AS(ミリオンシアター外の765プロ本体・アケマス系譜含む) | 公式告知・ガシャ・グッズ・**締切**(続報キューの種) |
| 2 | シンデレラガールズ(デレステ・デレマス・ミュージカル) | 同上 |
| 3 | ミリオンライブ(ミリシタ・ミリアニ) | 同上 |
| 4 | シャイニーカラーズ(シャニマス・シャニソン) | 同上 |
| 5 | SideM(ゲーム・ライブ・315プロコンテンツ) | 同上 |
| 6 | 学園アイドルマスター | 同上 |
| 7 | 876プロ・ヴイアライヴ(ディアリースターズ・vα-liv) | 同上。露出が少ないブランドこそ拾い漏らさない |
| 8 | 合同・横断・その他(ツアーズ/20周年企画/合同ライブ/ポプマス・KR・961等) | 同上 |
| 9 | 全体トレンド(72時間) | 全国トレンド入り・バズ(「アイマス婚」級の現象) |
| 10 | ファン文化 | 高エンゲージのファンアート・コスプレ・ノスタルジア(声優逸話・周年ネタ)。ファン種別記事とランキングのトレンド分の種 |

- **ブランドは間引かない**。全ブランドを毎回のクエリセットに含める(並列実行なので所要時間は変わらない)。該当期間に動きのないブランドは空配列が返るだけでコストは小さい。
- 各クエリ最大8件・エンゲージメント(高/中/低)と投稿日時を必須項目にする。合計目安 40〜60件/回。
- 締切・受注期限つきの話題は、candidates 化と同時に `stock/upcoming.yml` へトリガーを予約する(§3)。

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

## 3. 続報の制度化と鮮度ポリシー

**続報はネタ切れ時の埋め草ではなく、毎日必ず回る制度である。** 初報を書いた時点で未来の続報が予約される。ニュースが薄い日ほど続報キューが紙面を支え、その内容(締切リマインド等)は薄い日の読者にこそ刺さる。これは新聞が本来やっていることそのものだ。

### 続報キュー(`stock/upcoming.yml`)

初報の compose 時に、話題の未来日程を**トリガー付きで登録**する。compose は毎朝、「本日トリガーされたエントリ」を機械的に受け取り、**必ず記事化候補として処理する**。

```yaml
- dedup_key: shiny-summer-best-pair-2026
  brand: shiny
  subject: シャニマスサマーベストペア2026 予選投票
  triggers:
    - { date: 2026-07-13, kind: 締切前, note: "予選締切(7/16)の3日前リマインド" }
    - { date: 2026-07-16, kind: 締切, note: "予選投票 23:59 締切" }
  pending: [結果発表(日付判明次第 trigger を追加)]
```

トリガー種別と意味:

| kind | 付与規則 | 記事の性格 |
|------|---------|-----------|
| 締切前 | 受付・投票等の締切3日前 | 読者に一番実利があるリマインド |
| 締切 | 締切当日 | 「本日まで」 |
| 開幕 | イベント・ツアー初日 | 「本日から」は立派な新報 |
| 千秋楽 | 最終日・終了日 | 節目の報 |
| 発売 | 発売日・提供開始日 | 「本日発売」 |
| 結果 | 投票結果・当落・達成の発表日 | 初報の回収 |

- 日付が未定の続報(結果発表など)は `pending` に控えを書き、判明した時点で triggers に昇格させる。
- トリガー由来の記事は「新事実なし」には当たらない(その日に起きること自体がニュース)。rank は原則 small〜medium、当日開幕・結果は内容次第で上位も可。

### ストーリー台帳 `stock/stories.yml`(重複防止)

話題単位の既報管理。collect が dedup_key 一致で束ね、compose が published_facts を追記する。

### 記事化の判定規則(compose プロンプトの規程)

| # | 状況 | 扱い |
|---|------|------|
| 1 | 新規ストーリー | 記事化してよい(同時に upcoming.yml へ未来トリガーを予約する) |
| 2 | **本日トリガーの続報キュー** | **必ず記事化候補として処理**(紙面あふれ時は small に吸収) |
| 3 | 既存ストーリー+新事実あり(published_facts に無い fact) | 続報として記事化OK。本文は新事実中心、経緯要約は1段落まで |
| 4 | 既存ストーリー+新事実なし・トリガーなし | 記事化NG。ダイジェスト「継続中」行のみ |

- 校閲チェックリストの**ブロック項目**に「前号までの既報と実質同内容(新事実なし・トリガーなし)の記事」を追加する。
- lint は形式面のみ担保: 続報記事が stories.yml の published_facts 追記を伴うこと。実質判定は校閲 AI。

## 3.5 記事本数規程

- **記事化基準を満たす話題は全部書く。「多いから落とす」という作業は存在しない**(紙面は無制限)。
- **10〜14本は最低限目指す目安であって上限ではない**。あふれた日は rank を small に寄せて全て掲載する(rank 調整は紙面バランスのためで、ドロップの代替ではない)。
- **lead 欠落はブロック**(一面のない新聞はあり得ない。lint エラー)。
- **下限8本割れは警告+Discord 通知の上で発行(GO)**。下限をブロックにすると発行不能=休刊に倒れ、日刊の信頼を損なう方が痛い。
- 下限維持の主たる手段は §3 の続報キュー。収集が薄い日は、キューの締切前リマインド・開幕・結果もので紙面を組み立てる。

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
- 発行 = ローカルで `git merge --squash` により main へ取り込み、**main へ直接 push**(コミット題: `第N号 YYYY-MM-DD 発行`)。PR・branch protection・gh CLI は使わない。main の履歴は「1号=1コミット」になり、candidates・stories の更新も同じコミットに同梱される。
- マージ前ゲートはローカルの lint green+校閲 approve。push 後の GitHub Actions lint は事後検査(すり抜け検知)として常時走らせる。
- マージ直後に当該ブランチを削除し、**すぐに翌日のブランチを main から作成**する(publish.py の最終ステップ)。
- **発行忘れ検知**: 朝の監視(09:00)で「発行日が今日以前の edition ブランチが残っている」= 未発行として Discord 通知。
- 発行後の訂正は `correction/<slug>` ブランチを main から切り、lint+校閲を経て main へ push(REQUIREMENTS 4.6)。

## 6. compose と校閲

- **compose**(04:00): `claude -p` を `.claude/commands/compose.md` で起動。入力は検証済み candidates+stock+stories+前号までの紙面。出力は `_posts`/`_editions`/`_editorials` の新規ファイル+stories.yml 追記+機械算出フィールドはスクリプト(`scripts/derive.py`)で埋める。生成後ただちにローカル lint を回し、赤なら compose 内で自己修正させる。
- **校閲**(04:30目安): `codex exec -m terra` に固定チェックリスト(`prompts/review-checklist.md`)+main との diff を渡し、判定を JSON(`--output-schema`)で受け取る。往復と判定結果は metrics に記録する。
  - **ブロック**: 出典にない事実/URL捏造/新事実なしの続報/個人攻撃・プライバシー/時制矛盾
  - **コメントのみ**: 表記ゆれ・字数・面白さ
- 指摘→Claude が修正コミット→再校閲、最大2往復。lint green+校閲 approve でローカル squash merge → main へ push。06:00 の Pages 配信に間に合わせる(実質締切 05:30)。

## 7. 異常時の扱い

以下は Discord webhook で通知し、人間判断(または休刊)とする:

- candidates が 0 件/verify 全滅
- 記事本数の下限(8本)割れ(発行は止めない=GO のまま通知のみ)
- 校閲 2 往復で未解決ブロックが残る
- 05:30 までにマージ不能(lint 赤継続・push 失敗等)
- 09:00 時点で edition ブランチ残存(発行忘れ・PC 停止)

休刊日は欠番とせず、号数は発行実績の連番を維持する(lint の連番検査は日付ではなく発行順)。

## 8. メトリクス

毎実行で `metrics/YYYY-MM-DD.json` に追記: 収集件数(系統別)/verify 内訳/lint 違反数/校閲往復数/各フェーズ所要時間/(取得可能なら)トークン使用量。閾値超過は Discord 通知。CLI はサブスクリプション課金のため API 従量コストは発生しない見込みだが、日次上限に収まるかは第0号期間に実測する(REQUIREMENTS 6章)。

## 9. スケジュール一覧(JST)

| 時刻 | ジョブ | 内容 |
|------|--------|------|
| 07:30 / 12:30 / 18:30 / 23:30 | collect | 定点観測+探索+Grok+verify → edition ブランチへ push |
| 03:30 | collect(締切前) | 最終スイープ |
| 04:00 | publish | compose → lint → 校閲 → main へ squash push → 翌日ブランチ作成 |
| 09:00 | watch | 発行忘れ・メトリクス閾値の監視 → Discord |

スケジューラは **systemd user timer** を採用する(この WSL2 で systemd 稼働を確認済み)。`Persistent=yes` により PC がスリープしていた場合も復帰後に追い付き実行される。ユニット定義は実装時に `ops/systemd/` に置き、`systemctl --user enable --now` で有効化する(手順は README に記載予定)。Windows 側のスリープ設定によっては深夜帯に PC が起きていない点に注意(その場合 04:00 の発行は復帰後に遅延実行される)。

## 9.5 命名規則と成長設計(ファイルは毎日増える前提)

| 対象 | 規則 | 衝突・成長への配慮 |
|------|------|--------------------|
| 記事 | `docs/_posts/YYYY-MM-DD-<slug>.md` → URL `/articles/YYYY-MM-DD-<slug>/` | **URL に日付を含める**ため slug の一意性は号内のみ。過去全記事との衝突を構造的に排除 |
| 号・社説 | `YYYY-MM-DD.{md}` | 日付キーで衝突なし |
| candidates / metrics | `YYYY-MM-DD.json`(収集日) | 日付キー。lint の出典突合・derive のトレンド集計は**発行日±1日の窓**のみ参照(全期間を読まない) |
| dedup_key / story_id | 英小文字ハイフン。**毎年ある定例企画は年を含める**(例: `shiny-summer-pair-2026`) | 年跨ぎの誤マージを防止 |
| upcoming.yml | compose が毎朝、過去日トリガーを掃除し、空エントリを削除 | 無限成長しない |
| stories.yml | 追記型 | **未解決**: 月次で `stock/archive/stories-YYYY-MM.yml` へ closed 分を退避するローテーションを創刊後に導入する |
| ブランチ | `edition/YYYY-MM-DD` / `correction/YYYY-MM-DD-<slug>` | 日付キー |
| **既知の負債** | Liquid テンプレが `site.posts` を全走査(号ページ・アーカイブ) | 記事 4,000 本規模(約1年)で Pages ビルドが分単位に劣化する見込み。ビルド3分超過を watch の監視項目にし、超えたら「号スナップショットに記事リストを持たせて参照を局所化」する改修を行う |

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

1. Discord webhook URL の払い出し → リポジトリ直下の `.env` に記入(雛形: `.env.example`。`.env` は gitignore 済み)
2. systemd user timer の有効化(実装時にユニット一式と1コマンドの手順を用意する)

gh CLI・branch protection・auto-merge 設定は**不要**(発行は main への直接 push)。
