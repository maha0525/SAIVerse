# Intent: Line と Memorize タグの責務分離

**親 Intent**: [README.md](README.md)
**ステータス**: 起草中 (v0.1, 2026-05-01)
**位置付け**: Phase 3 残件 ([nested_subline_spell.md](nested_subline_spell.md) 実装の前提)

---

## 1. なぜこの整理が必要か

### 二重制御の問題

`messages` テーブルには Phase 1 で `line_role` / `line_id` / `scope` カラムが追加され、メッセージのライン階層と永続性が DB レベルで表現できるようになった。一方で context 構築 (= 次の Pulse のシステムプロンプトに何を載せるか) は依然として `metadata.tags` を主軸にフィルタしている (`sea/runtime_context.py:376-400` の `required_tags`)。

結果として、「あるメッセージが次の Pulse のプロンプトに載るかどうか」を決める判断軸が:

- `line_role` (階層属性)
- `scope` (永続性)
- `metadata.tags` (`conversation` / `internal` / `event_message` 等)
- `pulse_id` (Pulse 内除外)

の 4 つに散らばっている。

これが原因で:

- `sub_play` ノードが `report_to_main` を **タグベース** で `["conversation"]` を強制指定して親プロンプトに載せる (`sea/runtime_nodes.py:278`)
- autonomy 系 Playbook が `"memorize": {"tags": ["internal", "creation"]}` のように **タグでサブライン的な揮発を表現** している
- `include_internal` フラグが DEPRECATED 化されたはずなのに `runtime_context.py:377` で現役

「line で本来あるべき制御」を「タグで補う」状態が複数箇所に残っており、認知モデル v0.3.0 の中核設計と矛盾している。

入れ子サブライン Spell ([nested_subline_spell.md](nested_subline_spell.md)) を実装すると二重制御の上にさらに新しい制御を積むことになるため、先にこの整理を済ませる。

---

## 2. 責務分離の方針

### 2 軸を独立に運用する

| 軸 | 責務 | 主に参照される場面 |
|---|---|---|
| **Line** (`line_role` / `line_id` / `scope`) | メッセージの**階層属性**と**永続性** | context 構築 (= 次の Pulse のプロンプトに何を載せるか) |
| **Memorize タグ** (`metadata.tags`) | メッセージの**意味分類** | 検索・recall・Chronicle / Memopedia 連携・ユーザー向けラベリング |

両軸は独立に動く:

- **Line だけで**「親プロンプトに載るか・サブラインに閉じるか」「次の Pulse でも参照されるか・このターン限りか」が決まる
- **タグだけで**「何の意味のメッセージか」「Chronicle に上げるべきか」「memopedia の関連トピックか」が決まる

タグは context 構築には**関与しない**。タグを変えても次の Pulse のプロンプト構成は変わらない (= タグの追加・変更で副作用が出ない)。

### 結果として得られる性質

- **線引きが明示的**: 「このメッセージはサブラインに閉じる」と書きたいなら `line_role="sub_line"` を設定する。タグで間接的に表現しない。
- **タグ追加が安全**: ドメイン分類 (例: `creation`, `web_research`) を増やしても context 構築に影響しない。
- **設計時の判断が一意**: 新しい Playbook を書く時、「これは sub line か main line か」「scope は committed か discardable か volatile か」だけ決めれば良い。タグは別軸の意味分類として独立に決める。

---

## 3. 現状把握サマリー

詳細は調査メモに記録 (このセクションは要点のみ)。

### Line ベース制御の実装状況 (✅ 実装済み)

- カラム定義 `sai_memory/memory/storage.py:118-137`
  - `line_role`: `'main_line' | 'sub_line' | 'meta_judgment' | 'nested'`
  - `line_id`: 並列サブラインの区別
  - `scope`: `'committed' | 'discardable' | 'volatile'`
- 書き込み: `runtime._store_memory()` が `pulse_context.current_line_metadata()` から自動解決
- 読み出し (storage 層): `scope != 'discardable'` のフィルタは適用済み

### タグベース制御の現役箇所 (🟡 過渡期)

| 箇所 | コード位置 | 何をしているか |
|---|---|---|
| context 構築の主軸 | `sea/runtime_context.py:376-378` | `required_tags = ["conversation", "event_message"]`、`include_internal` で `"internal"` 追加 |
| `sub_play` の親伝搬 | `sea/runtime_nodes.py:265-281` | `report_to_main` を `tags=["conversation"]` ハードコードで SAIMemory に書く |
| autonomy 系 Playbook | `builtin_data/playbooks/public/autonomy_*.json` | `"memorize": {"tags": ["internal", "creation"]}` で揮発を表現 |
| `pulse_id` カラム + 旧タグ | `sai_memory/memory/storage.py:1292-1295` | `pulse_id` カラム化後も `"pulse:{uuid}"` タグ併行記録 |
| `include_internal` フラグ | `phase_1_base.md:51` で DEPRECATED 化済み | `runtime_context.py:377` で現役使用中 |

### 二重制御の現実例 5 件

これらは「line とタグの両方が同じ判断に関与」している箇所:

1. `sub_play` report_to_main: タグでフィルタ、line_role 渡さず
2. Context 構築: `required_tags` + storage の `scope != 'discardable'` で 2 軸同時適用
3. autonomy playbook の `internal` タグが context 除外を担う
4. `pulse_id` カラム化されたが `"pulse:{uuid}"` タグも併行記録
5. `include_internal` DEPRECATED 化済みだが context 構築で現役

---

## 4. 移行プラン (Phase 3 内で完結)

### 段階 4-A: context 構築を line ベースに切替

**目標**: `sea/runtime_context.py` の `required_tags` を line ベースに置換。タグはあくまで意味分類で、context 構築の判断軸から外す。

| 変更 | 内容 |
|------|------|
| `_prepare_context` のフィルタ | `required_tags` → `required_line_role` + `required_scope` |
| メインライン Pulse のデフォルト | `line_role IN ('main_line')` AND `scope = 'committed'` |
| サブライン Pulse のデフォルト | 自分の line_id 配下 + 親の committed メッセージ |
| `include_internal` フラグ | 廃止 (line ベースに統合済みのため不要) |
| storage 層 query | line_role / scope INDEX で検索、tags の json_each は意味分類のみに使用 |

### 段階 4-B: sub_play の report_to_main 経路を line ベースに統一

**目標**: `sea/runtime_nodes.py:265-281` のタグハードコードを line メタデータベースに切替。

| 変更 | 内容 |
|------|------|
| `_store_memory(tags=["conversation"], ...)` | `_store_memory(line_role="main_line", scope="committed", ...)` |
| `report_to_parent` リネームと統合 | (`nested_subline_spell.md §7` で定義済み) |

### 段階 4-C: 既存 Playbook の `memorize.tags` 整理

**目標**: 各 Playbook の `memorize` ノードで「context 制御のためのタグ」と「意味分類のためのタグ」を分離。

| 旧 | 新 |
|---|---|
| `"memorize": {"tags": ["internal", "creation"]}` | `"memorize": {"line_role": "sub_line", "scope": "volatile", "tags": ["creation"]}` |
| `"memorize": {"tags": ["conversation", "send_email_to_user"]}` | `"memorize": {"line_role": "main_line", "scope": "committed", "tags": ["send_email_to_user"]}` |

`internal` / `conversation` / `event_message` 等の **context 制御用タグは廃止**し、line_role / scope に置換。残るタグは純粋に意味分類 (`creation`, `web_research`, `send_email_to_user` 等)。

### 段階 4-D: 旧 DEPRECATED コードの削除

- `include_internal` パラメータの完全削除 (関数シグネチャから外す)
- `pulse:{uuid}` タグの併行記録廃止 (Phase 2.5 で pulse_id カラム化済み)
- `required_tags` パラメータの削除 (line ベースで必要十分なため)

---

## 5. 移行スコープと工数

### 影響範囲

| ファイル | 変更内容 | 規模 |
|---------|---------|------|
| `sea/runtime_context.py` | フィルタを line ベースに | 中 |
| `sai_memory/memory/storage.py` | query 経路の整理 + INDEX 活用 | 中 |
| `persona/history_manager.py` | `required_tags` 引数の置換 | 中 |
| `sea/runtime_nodes.py` | sub_play の report 渡し方修正 | 小 |
| `sea/runtime_llm.py` | spell loop / LLM ノードの memorize 経路整理 | 中 |
| `builtin_data/playbooks/public/*.json` | memorize.tags の整理 (autonomy_* / 各種実用 Playbook) | 大 (10〜15 ファイル) |

### Phase 3 翻訳作業との関係

- **Phase 3 翻訳作業 (`migrate_playbooks_to_lines.py`)**: `context_profile` / `model_type` → `line` の翻訳
- **本整理 (line vs タグ責務分離)**: `memorize.tags` の整理 (内部の memorize ノード単位)

両者は**同じ Playbook ファイルを触る**ので、**一括で両方やる**のが効率的。`migrate_playbooks_to_lines.py` を拡張して、両方の変換を 1 つのスクリプトでやる。

### 工数見積もり

- **手動作業**: 段階 4-A / 4-B / 4-D は runtime コード改修で 1〜2 セッション
- **半自動**: 段階 4-C の Playbook 整理は migration スクリプトで一括 (Phase 3 翻訳と一体化)
- **検証**: 既存テスト + 実機での動作確認で 1 セッション

総じて Phase 3 翻訳と一体で **2〜3 セッション**。完全 line ベース統一案 (案 A の 2000+ LOC) の半分以下。

---

## 6. nested_subline_spell との関係

[nested_subline_spell.md §8](nested_subline_spell.md) の「揮発設計」は本整理の前提に乗せて書き直す:

- サブライン内のメッセージは `line_role="sub_line"` + `scope="volatile"` で記録される
- 親プロンプトに自動で載らないのは **`line_role` と `scope` の組み合わせ**で決まる (タグは関与しない)
- `report_to_parent` は `line_role="main_line"` + `scope="committed"` で記録される (= 親メインラインの会話の一部)
- タグは意味分類のみ (Playbook 名や用途識別)

この前提が成立した上で `/run_playbook` Spell の実装に入る。タグレガシーを残したまま新機構を入れると二重制御が深まるので、**移行 → 入れ子サブライン実装**の順序を守る。

---

## 7. 不変条件への影響

ペルソナ認知モデルの不変条件 1〜12 (`README.md`) のうち、本整理で変わるもの:

- 不変条件 2「**単一主体の記憶**」: より厳密に保証される。タグ参照で意図せず conversation 化していたサブラインメッセージが、line ベースでは確実に揮発するため、ペルソナ人格への影響が予測可能になる。
- 不変条件 7「**キャッシュヒット継続を最優先**」: line ベースで context 構築が決まれば、タグ追加でプロンプトが変わるリスクが消えてキャッシュ予測性が上がる。
- 不変条件 11「**メタ判断はペルソナの自分の思考**」: meta_judgment line のメッセージが scope='discardable' で次の Pulse から消えることが、タグでなく line で保証される。

---

## 8. 段階別の完了基準

### 段階 4-A 完了基準

- [ ] `_prepare_context` が `line_role` / `scope` のみで context を組み立てる
- [ ] `required_tags` パラメータを受け取る関数が存在しない (内部で使われていない)
- [ ] 既存テスト + 実機で context 構築の挙動が変わらないこと

### 段階 4-B 完了基準

- [ ] `sub_play` の report_to_main 渡しがタグハードコードを使っていない
- [ ] サブラインから親への伝搬が line メタデータ経由で動作することを実機確認

### 段階 4-C 完了基準

- [ ] すべての builtin Playbook で `memorize.tags` に `internal` / `conversation` / `event_message` が含まれない
- [ ] line_role / scope が memorize ノードで明示的に指定されている (or 妥当なデフォルト)

### 段階 4-D 完了基準

- [ ] `include_internal` パラメータが関数シグネチャから消えている
- [ ] `pulse:{uuid}` タグの併行記録が廃止されている
- [ ] DEPRECATED コメントが削除されている

---

## 9. Phase 3 残作業との順序

```
Phase 3 残作業の依存グラフ:

[本整理: line vs タグの責務分離]
    ↓
[migrate_playbooks_to_lines.py 作成]
    ↓ (タグ整理 + context_profile/model_type 翻訳を一括で)
[既存 Playbook 一括翻訳]
    ↓
[/run_playbook Spell 実装 (nested_subline_spell.md)]
    ↓
[track_user_conversation を 1-LLM + Spell 構成に書き換え]
    ↓
[meta_user / sub_router_user 廃止]
    ↓
[実機検証]
```

本整理は依存グラフの**起点**。これを先に固めないと後段がすべて二重制御の影響を受ける。

---

## 関連ドキュメント

- [README.md](README.md) — 進捗表
- [01_concepts.md](01_concepts.md) — line / scope / 7 層ストレージモデルの概念定義
- [02_mechanics.md](02_mechanics.md) — Pulse 階層 / Playbook 起動とラインの関係
- [nested_subline_spell.md](nested_subline_spell.md) — `/run_playbook` Spell 機構 (本整理が前提)
- [phases/phase_1_base.md](phases/phase_1_base.md) — line_role / scope カラム追加の経緯
- [phases/phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) — Phase 3 タスク (本整理を含む)
- [revisions.md](revisions.md) — 改訂履歴
