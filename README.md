# アイマスNEWS(α)

アイドルマスター関連ニュースを**毎日自動発行**する非公式ファン新聞。全記事を AI 編集部が執筆し、別ベンダーの AI が校閲する。人間は校閲しない——この体制を隠さないことが本紙のアイデンティティである。

- 配信: <https://imas-news.ofa.tokyo/>(自前オリジン + Cloudflare Tunnel。紙面はビルドせずアプリが描く。PIPELINE.md §9.6)
- 要件書: [REQUIREMENTS.md](REQUIREMENTS.md)(データ契約・パイプライン・編集規程の正)
- 確定モック: [base-design/](base-design/)(トップ/記事/社説/バックナンバー。デザインの正)

## 原則

1. **一次ソース至上主義** — 出典に書かれていない事実は書かない。全記事に出典必須。
2. **append-only** — 全号を発行当時の紙面のまま永久保存。誤りは削除ではなく「おわびと訂正」で残す。過去号のファイルには訂正以外で触れない(lint が強制)。
3. **30秒で掴める** — 「本日の紙面」ダイジェストが紙面の目次。SP では1画面・スクロールなし。

## パイプライン

```
collect(コード・毎日数回) → verify(コード) → compose(発行日 03:00)
 → lint(ローカルゲート) → review(校閲=Codex) → main へ squash push → 06:00 JST 発行
```

実行主体はローカルの systemd user timer(`claude`/`grok`/`codex` CLI のセッション認証がローカルにあるため)。GitHub 側は push 後の lint CI(事後検査)のみ。号ごとに `edition/YYYY-MM-DD` ブランチで収集・生成し、発行時にローカルで squash merge して main へ直接 push(main は1号=1コミット)。秘匿値は `.env`(gitignore 済み、雛形 `.env.example`)。運用設計の詳細は [PIPELINE.md](PIPELINE.md)。

現在の進捗: **Step 1 完了**(骨格・スキーマ・lint CI)+ 収集・発行の運用設計確定。次は Step 2(モックの Jekyll 移植)。全ステップは REQUIREMENTS.md 7章。

## ディレクトリ構成

```
├── REQUIREMENTS.md          # システム要件書(正)
├── base-design/             # 確定デザインモック(参照用・配信されない)
├── birthdays.json           # 誕生日マスタ(原本)。名鑑の源泉
├── docs/                    # 紙面のデータ(webapp が読んで描く。配信は scripts/deploy.py が版として写す)
│   ├── _posts/              # 記事: YYYY-MM-DD-<slug>.md → /articles/YYYY-MM-DD-<slug>/
│   ├── _editions/           # 号スナップショット(frontmatter のみ): YYYY-MM-DD.md → /editions/YYYY-MM-DD/
│   ├── _editorials/         # 社説: YYYY-MM-DD.md → /editorials/YYYY-MM-DD/
│   ├── _data/
│   │   ├── brands.yml       # ブランド定義(表示名・カラートークン)
│   │   └── idols.json       # 名鑑(生成物。手編集しない)
│   ├── assets/              # CSS・JS(Caddy がそのまま配る)
│   └── about/               # 編集方針と免責
├── webapp/                  # 配信のアプリ(紙面・タグ・検索のページを描く。テンプレートは templates/paper/)
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
- **社説(2026-09-06 号で終了。過去分のみ残す)** `docs/_editorials/YYYY-MM-DD.md` — frontmatter(title / excerpt)+本文([schema/editorial.schema.json](schema/editorial.schema.json))。
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

## 定時実行(systemd user timer)

collect(07:30/12:30/18:30/23:30/02:00)→ compose(03:00)→ release(06:00 発行)→ watch(09:00)。
**自動ジョブは専用クローン `~/git/imas-ops` で動く**(このリポジトリの作業ツリーには触れない)。導入は:

```sh
git clone git@github.com:subie-producer/Imas_Daily_News.git ~/git/imas-ops
cp .env ~/git/imas-ops/ && (cd ~/git/imas-ops && python3 -m venv .venv && .venv/bin/pip install selenium)
ops/systemd/install.sh   # ユニット配置+timer 有効化+linger 有効化
```

- 手動発行/検証: `python3 scripts/release.py [--date YYYY-MM-DD] [--dry-run]`
- 状態確認: `systemctl --user list-timers 'imas-*'` / ログ: `journalctl --user -u imas-release`
- 注意: Windows 再起動後は WSL を一度起動しないと timer も起きない(起動後は `Persistent=yes` が追い付き実行する)。実行時に作業ツリーが汚れていると安全のため発行を中止して通知する。

## 名鑑の更新

アイドルの追加・修正は原本 `birthdays.json` を編集し、`python3 scripts/build_idols.py` で `docs/_data/idols.json` を再生成してコミットする(CI が同期を検査)。ブランド対応表はスクリプト内 `BRAND_ID_MAP`。

## ローカルプレビュー

紙面はビルドしない。配信のアプリ(webapp)が docs をそのまま読んで描く(PIPELINE.md §9.6)。

```sh
python3 -m venv .venv-app && .venv-app/bin/pip install -r webapp/requirements.txt
SITE_ROOT=docs PAPER_DB=/tmp/paper.db .venv-app/bin/python scripts/indexer.py --db /tmp/paper.db   # タグ・検索を見るときだけ
SITE_ROOT=docs PAPER_DB=/tmp/paper.db BIND_PORT=8081 .venv-app/bin/python webapp/app.py
# → http://127.0.0.1:8081/(CSS は /assets/ を Caddy が配るので、プレビューでは素の HTML になる)
```

以前の静的版(Jekyll の出力)と全ページを比べるときは `webapp/compare.py --site docs --static <出力>`。

## 免責

本紙は非公式のファンメディアであり、株式会社バンダイナムコエンターテインメントおよび関連各社とは一切関係ありません。
