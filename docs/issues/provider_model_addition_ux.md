# Issue: マイナープロバイダ (Kimi, Z.AI 等) とモデルをユーザーが追加しやすくする

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-09
**関連**: `models/`, `llm_clients/`, モデル設定 UI

## 背景

新しい LLM プロバイダ (Kimi, Z.AI 等の OpenAI 互換 API を出す事業者) や個別モデルをユーザーが追加するには現状:

- `models/<model>.json` を手動で作成 (`provider`, `base_url`, `api_key_env`, `parameters` 等を記述)
- 場合によっては `llm_clients/` に provider 実装を足す

これは敷居が高く、ユーザーから「追加しづらい」フィードバックが出ている。とくにモデルファイル作成・編集自体が UI からできない (CLAUDE.md でも JSON 直書き手順)。

## 解決案候補

### 案 A: モデル追加 UI

設定画面から「新規モデル追加」フォームを提供:
- 表示名、API model id、provider (ドロップダウン)、base_url、api_key_env
- 既存モデルからのコピー新規作成
- 編集・削除も UI から

OpenAI 互換 API の事業者なら provider="openai" + base_url 指定で動くことが多いので、provider を選んで base_url を変えるだけのシンプル UI でもかなり対応可能。

### 案 B: プロバイダプリセット

「Kimi」「Z.AI」「DeepSeek」などをプリセットとして同梱 (base_url や API キー環境変数名を埋めた状態で一覧から選べる)。OpenAI 互換のものはほぼ流用できる。

### 案 C: 一括インポート

provider/model のテンプレ JSON を Gist や URL から取り込める機能。コミュニティで共有しやすい。

→ 案 A + 案 B 併用が現実的。案 C は後続で。

## 関連リソース

- `models/` ディレクトリ
- `saiverse/model_configs.py`
- `llm_clients/openai.py` 等
- `frontend/` の設定画面

## ログ

- 2026-05-09: issue 起票。
