# アイマスNEWS(α)

アイドルマスター関連ニュースを**毎日自動発行**する非公式ファン新聞。全記事を AI 編集部が執筆し、別ベンダーの AI が校閲する。人間は校閲しない——この体制を隠さないことが本紙のアイデンティティである。

- 配信: <https://subie-producer.github.io/Imas_Daily_News/>(GitHub Pages / main ブランチ `/docs`)
- 要件書: [REQUIREMENTS.md](REQUIREMENTS.md)(データ契約・パイプライン・編集規程の正)
- 確定モック: [base-design/](base-design/)(トップ/記事/社説/バックナンバー。デザインの正)

## 原則

1. **一次ソース至上主義** — 出典に書かれていない事実は書かない。全記事に出典必須。
2. **append-only** — 全号を発行当時の紙面のまま永久保存。誤りは削除ではなく「おわびと訂正」で残す。過去号のファイルには訂正以外で触れない(lint が強制)。
3. **30秒で掴める** — 「本日の紙面」ダイジェストが紙面の目次。SP では1画面・スクロールなし。

## パイプライン

```
collect(コード・毎日数回) → verify(コード) → compose(Claude・発行日 04:00)
 → lint(ローカルゲート) → review(校閲=Codex) → main へ squash push → 06:00 JST 発行
```

実行主体はローカルの systemd user timer(`claude`/`grok`/`codex` CLI のセッション認証がローカルにあるため)。GitHub 側は Pages 配信と push 後の lint CI(事後検査)のみ。号ごとに `edition/YYYY-MM-DD` ブランチで収集・生成し、発行時にローカルで squash merge して main へ直接 push(main は1号=1コミット)。秘匿値は `.env`(gitignore 済み、雛形 `.env.example`)。運用設計の詳細は [PIPELINE.md](PIPELINE.md)。

現在の進捗: **Step 1 完了**(骨格・スキーマ・lint CI)+ 収集・発行の運用設計確定。次は Step 2(モックの Jekyll 移植)。全ステップは REQUIREMENTS.md 7章。

## ディレクトリ構成

```
├── REQUIREMENTS.md          # システム要件書(正)
├── base-design/             # 確定デザインモック(参照用・配信されない)
├── birthdays.json           # 誕生日マスタ(原本)。名鑑の源泉
├── docs/                    # Jekyll ソース = GitHub Pages 配信ルート
│   ├── _config.yml
│   ├── _posts/              # 記事: YYYY-MM-DD-<slug>.md → /articles/<slug>/
│   ├── _editions/           # 号スナップショット(frontmatter のみ): YYYY-MM-DD.md → /editions/YYYY-MM-DD/
│   ├── _editorials/         # 社説: YYYY-MM-DD.md → /editorials/YYYY-MM-DD/
│   ├── _data/
│   │   ├── brands.yml       # ブランド定義(表示名・カラートークン)
│   │   └── idols.json       # 名鑑(生成物。手編集しない)
│   ├── _layouts/ _includes/ assets/   # テンプレート(Step 2 で移植)
│   └── about/               # 編集方針と免責
├── schema/                  # JSON Schema(データ契約の機械可読形)
├── scripts/
│   ├── build_idols.py       # birthdays.json → docs/_data/idols.json
│   └── lint.py              # 紙面 lint(required check の実体)
├── candidates/              # collect 層の収集候補 YYYY-MM-DD.json(Step 3)
├── stock/                   # 未来の予定 upcoming.yml(Step 3)
├── metrics/                 # 実行メトリクス(Step 4)
└── .github/workflows/lint.yml
```

## データ契約

契約の定義は REQUIREMENTS.md 3章、機械可読形は [schema/](schema/) にある。要点:

- **記事** `docs/_posts/YYYY-MM-DD-<slug>.md` — frontmatter([schema/article.schema.json](schema/article.schema.json))+ Markdown 本文(中見出しは h2)。
- **号スナップショット** `docs/_editions/YYYY-MM-DD.md` — frontmatter のみの .md([schema/edition.schema.json](schema/edition.schema.json))。`pages`・`article_count`・`corrected_count` は機械算出で、lint が記事群から再計算して照合する。
- **社説** `docs/_editorials/YYYY-MM-DD.md` — frontmatter(title / excerpt)+本文([schema/editorial.schema.json](schema/editorial.schema.json))。
- **収集候補** `candidates/YYYY-MM-DD.json`([schema/candidates.schema.json](schema/candidates.schema.json))。記事の出典 URL は candidates に存在しなければならない(collect 稼働後に有効化)。

## lint(required check)

```sh
pip install pyyaml jsonschema
python3 scripts/lint.py            # ローカル(未コミット分を変更扱い)
python3 scripts/lint.py --no-net   # URL 生存確認なし
python3 scripts/lint.py --full     # 全記事の URL 監査
```

検査項目: スキーマ検証/slug・号数の一意性と連番/lead 号内1本/機械算出フィールドの照合/時制(本日・昨日・明日と絶対日付の共起)/出典の生存と candidates 突合/ダイジェストの4群構成・字数・SP 1画面制約(群4行・全体12行)/ランキング8件・名鑑照合・前号比 delta/誕生日欄と名鑑の一致/append-only 違反(過去紙面の削除・訂正なし変更)。

エラーは発行不可(publish のローカルゲート)、警告(見出し45字超など)は annotation のみ。push 後の GitHub Actions は事後検査として同じ lint を実行する。

**セットアップ(GitHub 側・手動)**:

1. Settings → Pages → Source: `Deploy from a branch`, Branch: `main` / `docs`

(branch protection・auto-merge は使わない — 発行は main への直接 push)

## 定時発行(systemd user timer)

毎朝 06:00 JST に `scripts/release.py` が edition ブランチを main へ squash merge して push する(=発行)。導入は:

```sh
ops/systemd/install.sh   # ユニット配置+timer 有効化+linger 有効化
```

- 手動発行/検証: `python3 scripts/release.py [--date YYYY-MM-DD] [--dry-run]`
- 状態確認: `systemctl --user list-timers 'imas-*'` / ログ: `journalctl --user -u imas-release`
- 注意: Windows 再起動後は WSL を一度起動しないと timer も起きない(起動後は `Persistent=yes` が追い付き実行する)。実行時に作業ツリーが汚れていると安全のため発行を中止して通知する。

## 名鑑の更新

アイドルの追加・修正は原本 `birthdays.json` を編集し、`python3 scripts/build_idols.py` で `docs/_data/idols.json` を再生成してコミットする(CI が同期を検査)。ブランド対応表はスクリプト内 `BRAND_ID_MAP`。

## ローカルプレビュー

初回セットアップ(Ubuntu / WSL):

```sh
sudo apt install -y ruby-full build-essential zlib1g-dev
gem install --user-install bundler   # ~/.local/share/gem/ruby/<ver>/bin に入る。PATH を通すこと
cd docs
bundle config set --local path vendor/bundle   # gem をプロジェクト内(git 管理外)に閉じ込める
bundle install
```

以後のプレビュー:

```sh
cd docs && bundle exec jekyll serve --baseurl /Imas_Daily_News
# → http://127.0.0.1:4000/Imas_Daily_News/
```

(本番ビルドは GitHub Pages 側で行われるため、ローカル Ruby は必須ではない)

## 免責

本紙は非公式のファンメディアであり、株式会社バンダイナムコエンターテインメントおよび関連各社とは一切関係ありません。
