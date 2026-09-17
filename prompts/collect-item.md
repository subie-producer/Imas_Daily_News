1話題を1要素にした JSON 配列。各要素:
```
{"title": "短い見出し",
 "brand": "general|765|cg|million|shiny|sidem|gaku|dsva|joint|other",
 "kind": "official|semi|party|media|fan|trend",
 "url": "実在する URL",
 "event_date": "YYYY-MM-DD か空文字",
 "published_date": "情報の初出日(ページの掲載日・投稿日)YYYY-MM-DD か空文字",
 "deadline": "締切・終了日 YYYY-MM-DD か空文字",
 "facts": ["確認できた事実(1事実1要素)"],
 "dedup_key": "英小文字とハイフンの話題 ID(毎年ある定例企画は年を含める。例: shiny-summer-pair-2026)",
 "engagement": "高|中|低",
 "mentioned_idols": ["言及されたアイドル名"]}
```
kind: official = アイマス公式(公式ポータル・ブランド公式サイト・公式 X アカウント)/ semi = 公式レーベル・公式ストア(日本コロムビア・ランティス・アソビストアなど)/
party = アイマス公式に関係する主催者・販売元・自治体・コラボ先企業(ファン主催者は含まない)/ media = 報道 / fan = ファン発 / trend = 現象
