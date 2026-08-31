# Issue: SearXNG 検索で天気情報が上手く取れない → 天気取得用 Tool が必要

**ステータス**: 🔲 未着手
**優先度**: low
**作成日**: 2026-05-09
**関連**: `builtin_data/tools/`, SearXNG 連携

## 背景

ペルソナが「天気」を調べる用途で SearXNG 検索を使うと、結果が一般 Web 検索結果のリストになり、現在天気・気温などの構造化情報を取り出せない。SearXNG の天気カテゴリも安定しない。

天気は AI からの問い合わせ頻度が比較的高い情報なので、専用 Tool を用意したほうが UX が良い。

## 解決案候補

### 案 A: 無料天気 API の wrapper Tool

候補:
- Open-Meteo (https://open-meteo.com/) — 完全無料、API キー不要、商用 OK
- wttr.in — テキスト形式で簡素、シェルから curl でも取れる
- OpenWeatherMap — 無料枠あり、API キー必要

Open-Meteo が最も組み込みやすい。位置 (緯度経度 or 都市名 → ジオコーディング) と日時を渡し、現在天気・予報を返す。

### 案 B: SearXNG の特定 engine を絞る + パース

天気専用 engine だけを叩いてパースする実装。SearXNG 側の変更に追従するコストが高い。

→ 案 A 推奨。

## 関連リソース

- `builtin_data/tools/defs/web_search.py` (相当)
- メモリ: Tool 追加方法は CLAUDE.md の「Add new tool」項

## ログ

- 2026-05-09: issue 起票。
