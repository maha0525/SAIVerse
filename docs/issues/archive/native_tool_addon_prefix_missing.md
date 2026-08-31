# Native ツールにアドオン名プレフィックスがない

## 状態: ✅ 解決済み (2026-07-19)

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

## 解決 (2026-07-19)

対応策どおり実装。土台 (`_infer_addon_name` による addon 名解決と `meta.addon_name` 付与) は既に存在していたため、変更は局所的だった。

- **登録キーの名前空間化** (`tools/__init__.py`): `_registry_key(schema)` を新設。`schema.addon_name` があれば `<addon>__<tool>` を返す (builtin_data/user_data は addon_name=None → 素名維持)。`_register_tool` / `_register_multiple_tools` の登録キーと「既登録スキップ」判定 (user_data 優先) をこのキーに差し替え。ALIASES も addon 名前空間に載せる。これで別アドオンの同名 native ツールが別キーになり後勝ち上書きが消える。実機レジストリで `saiverse-stackchan-addon__see` / `saiverse-godot-vessel-addon__body_see` 等が名前空間化され、素の `see` が消えたことを確認。
- **旧名フォールバック** (`canonicalize_spell_name`, `tools/__init__.py`): 「プレフィックスなし旧名でも解決できるフォールバック検索を残すか」への回答 = **残す (一意なときだけ)**。完全一致 → 一意な `<addon>__<name>` 接尾一致 → それ以外 (未知 or 曖昧=複数アドオンが同名) は素名のまま返し呼び出し側が未知スペルとして扱う。呼び出し側の解決点 4 箇所 (`sea/runtime_llm.py`: メインスペルループ / pre_spells 2 / realtime binding) で `SPELL_TOOL_NAMES` ゲート前に canonicalize を通す。これで旧 `realtime_spell_binding.SPELL_NAME` / `PersonaSchedule.pre_spells` の素名エントリが自動で名前空間キーに解決し、データ移行なしで壊れない。新規 binding は UI カタログが名前空間キーを出すので最初から prefixed で保存される。
- **回帰**: `tests/test_native_tool_addon_prefix.py` 新設 (`_registry_key` / canonicalize の完全一致・一意接尾・曖昧・未知の 4 分岐)。`tests/test_realtime_spells_media.py` は `SPELL_TOOL_NAMES` をローカルパッチする一方 canonicalize が実グローバルを読む不整合を、canonicalize を identity パッチして分離 (本番では同一オブジェクトなので不整合は起きない)。関連スイート計 110+ 件緑。
- **残る persona-facing 影響 (別件)**: head のスペル一覧が全アドオンスペルを長い `<addon>__<tool>` 形式で表示するようになる (グループヘッダの addon 表示と冗長)。canonicalize により素名詠唱は一意なら通るので実害は小さいが、head 描画を「一意なら素名・衝突時のみ完全名」にする短縮案は別 issue 候補。
