# Native ツールにアドオン名プレフィックスがない

## 状態: 未解決

## 問題

MCP 由来のスペルは `<qualified_name>__<tool_name>`（例: `stackchan__measure_temperature`）の形式で SPELL_TOOL_NAMES / TOOL_REGISTRY に登録されるが、`expansion_data/<addon>/tools/` 配下の native ツールはファイル名（= schema の `name` フィールド）がそのまま登録される。

別アドオンが同名の native ツールを持つ場合、後からロードされた方が先のものを上書きする（後勝ち）。ユーザーから見ると意図しないツールが呼ばれる可能性がある。

## 影響範囲

- `tools/__init__.py` の `_register_tool` / `_register_multiple_tools`
- TOOL_REGISTRY / SPELL_TOOL_NAMES / SPELL_TOOL_SCHEMAS のキー
- ペルソナが `/spell` で呼ぶときの名前解決
- リアルタイムスペル binding の `SPELL_NAME` カラム（名前で紐付けるため、rename 時に古い binding が壊れる）

## 対応策

native ツールも MCP と同様にアドオン名プレフィックスを付与する: `<addon_name>__<tool_name>`。

- `builtin_data/tools/` のものはプレフィックスなし（従来通り）
- `expansion_data/<addon>/tools/` のものは `<addon>__<name>` に変更
- `~/.saiverse/user_data/tools/` のものはプレフィックスなし（ユーザーカスタム = 最優先）

### 移行

- 既存の spell 設定（PersonaSchedule の pre_spells、realtime_spell_binding の SPELL_NAME）で旧名を使ってる場合のフォールバック or migration が必要
- ペルソナの発話中の `/spell name='xxx'` は新名に追従する必要がある（プレフィックスなし旧名でも解決できるフォールバック検索を残すか）

## 発見経緯

2026-06-21 リアルタイムスペル binding UI を実装した際、カタログ表示でアドオン native ツールがプレフィックスなしで並び、別アドオン間で名前が被ったら破綻するという指摘。
