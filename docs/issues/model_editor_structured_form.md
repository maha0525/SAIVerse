# Issue: モデル編集 UI の追加設定セクションを構造化フォーム化

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-11
**関連**: `docs/intent/model_provider_management.md`, `frontend/src/components/settings/ModelEditorModal.tsx`

## 背景

モデル編集 UI (`ModelEditorModal.tsx`) は基本フィールド (`model` / `display_name` / `provider_ref` / `context_length`) を専用入力欄にしたが、それ以外は依然として JSON エディタで生編集する形。

SAIVerse 側で固有に定義しているフィールドは、ユーザーが手書きするには敷居が高い：
- スキーマ全体を覚える必要がある
- フィールド名のタイポでサイレントに無効化される
- バリデーションがない（型を間違えてもサーバーまで届く）
- builtin の例 JSON を読み解いてコピペする運用になりがち

「色んな追加パラメータは JSON で書く」と割り切って Phase 1 では JSON エディタ実装したが、よく使うフィールドは構造化 UI にすべき。

## 構造化 UI 化の候補フィールド

| フィールド | 型 | UI 候補 |
|---|---|---|
| `supports_images` | bool | チェックボックス |
| `convert_system_to_user` | bool | チェックボックス |
| `supports_structured_output` | bool | チェックボックス |
| `structured_output_backend` | enum | ドロップダウン (`xgrammar` / `outlines` / なし) |
| `structured_output_mode` | enum | ドロップダウン (`native` / `json_object` / なし) |
| `metabolism_target_chars` | number | 数値入力（整理後の目標文字数） |
| `metabolism_high_chars` | number | 数値入力（発火する文字数。null = token 閾値のみ） |
| `max_image_embeds` | number | 数値入力 |
| `max_image_bytes` | number | 数値入力 |
| `cache.default_enabled` | bool | チェックボックス（プロバイダがキャッシュ対応のときのみ表示） |
| `cache.default_ttl` | enum | ドロップダウン (`5m` / `1h`) |
| `cache.min_tokens` | number | 数値入力 |
| `pricing.input_per_1m_tokens` | number | 数値入力 (USD) |
| `pricing.output_per_1m_tokens` | number | 数値入力 (USD) |
| `pricing.cached_input_per_1m_tokens` | number | 数値入力 |
| `pricing.cache_write_per_1m_tokens` | number | 数値入力 |
| `rate_limit.rpd` | number | 数値入力 |
| `rate_limit.reset_timezone` | text | 入力欄 |
| `parameters` | dict (slider/dropdown 仕様) | **構造化編集が難しい — 別 issue 候補** |

`parameters` は spec の仕様自体（type=slider/dropdown/number、min/max/step/options/default）を編集する必要があり、フォーム化が一段難しい。Phase 1 では JSON 編集のままで残し、Phase 2 で取り組む方が現実的。

## 解決案候補

### 案 A: タブ分割（推奨）

ModelEditorModal を「基本」「機能フラグ」「Cache」「Pricing」「パラメータ (JSON)」の縦タブまたは折りたたみセクションに分割。
- 利点：頻出フィールドは UI で完結、`parameters` の複雑さは JSON で逃げる
- 欠点：実装量が増える。スキーマが拡張されるたびに UI も追従が必要

### 案 B: アコーディオン式の "詳細" 折りたたみ

基本フィールドの下に「機能フラグ」「Cache 設定」「Pricing」など複数のアコーディオンを並べる。各アコーディオンが個別フォーム + その他は依然 JSON。
- 利点：実装が単純、よく使うフィールドだけ最初から見せる
- 欠点：JSON エディタの責務が中途半端に残る（基本でも詳細フォームでもないフィールドは JSON）

### 案 C: スキーマ駆動

`builtin_data/models/_schema.json` のような形でフィールド定義を持ち、フロントが汎用フォームレンダラーで自動生成。
- 利点：スキーマ追加だけで UI 拡張、保守コスト低
- 欠点：初期実装が重い。React Hook Form や JSON Schema Form 系ライブラリの導入を検討

## 推奨

短期的には **案 A**（タブ or 折りたたみ + 残余 JSON）。`parameters` は引き続き JSON で残し、将来 **案 C** にリファクタする余地を残す。

## 関連リソース

- 現状の編集 UI: `frontend/src/components/settings/ModelEditorModal.tsx`
- フィールド定義の参考: `saiverse/model_configs.py`、`builtin_data/models/*.json`
- Intent: `docs/intent/model_provider_management.md` §G「フロントエンド UI」では Phase 1 として「parameters は JSON エディタ。構造化フォームは将来検討」と明記済み

## ログ

- 2026-05-11: 起票。Phase 1 の JSON エディタ実装後、まはーから「特に supports_images とかの SAIVerse 固有フィールドは専用 UI が欲しい」とフィードバック。基本フィールドの構造化までは Phase 1 で完了済み。残りは後日対応。
