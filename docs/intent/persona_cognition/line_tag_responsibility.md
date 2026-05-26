# Intent: Line と Memorize タグの責務分離

**親 Intent**: [README.md](README.md)
**ステータス**: v0.1 実装済 (4-A/B/C 達成) → **v0.2 実装済 (2026-05-26, §10)、実機検証待ち**
**位置付け**: Phase 3 残件 ([nested_subline_spell.md](nested_subline_spell.md) 実装の前提)

> **読む順序**: §1〜9 は v0.1 (ノード単位で line_role/scope を明示) の記録。**現行設計は §10 (v0.2: アスペクト `aspect` による呼び出し時指定)** を参照。§2 の「指定単位」と §4-C は §10 で置換される。

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

### 段階 4-A 完了基準 (✅ 達成 — v0.21, 2026-05-01)

- [x] `_prepare_context` が `line_role` / `scope` のみで context を組み立てる
  - `sea/runtime_context.py`: `required_line_roles=["main_line"]` + `required_scopes=["committed"]` に置換。`include_internal=True` のときは `sub_line` を許可するフォールバック (4-C で memorize.tags 整理時に廃止予定)
- [x] context 構築経路 (`_prepare_context`, `runtime.py:1559` metabolism anchor, `persona/mixins/generation.py:170` persona generation) は `required_tags` を渡さない
  - search/recall 経路 (api/recall, memopedia/generator, memory_search_brief, record_wait, recall_conversation_with) は `required_tags` を残置 (意味分類フィルタ、4-D で整理予定)
- [x] 既存テスト + 実機で context 構築の挙動が変わらないこと
  - tests: subplay_line 11 / meta_layer + track_manager + storage 70 / context + history 42 件パス、629 passed / 5 既存 failed (本変更前から)
  - 実機 air_city_a: `[sea][prepare-context] Fetching history: ... line_roles=['main_line'], scopes=['committed']` ログで置換動作確認、`Got 60 history messages` で legacy 互換 (line_role IS NULL → main_line) も確認

### 段階 4-B 完了基準 (✅ 達成 — v0.22, 2026-05-01)

- [x] `sub_play` の `report_to_parent` 渡しがタグハードコードを使っていない
  - `sea/runtime_nodes.py`: `_store_memory(line_role="main_line", scope="committed", ...)` に置換、`tags=["conversation"]` 廃止。`report_to_main` → `report_to_parent` 全面リネーム (state キー、ログ、コメント、サンプル Playbook ドキュメント)
- [x] サブラインから親への伝搬が line メタデータ経由で動作することを単体検証
  - `tests/test_subplay_line.py` の `test_subplay_line_sub_stores_report_to_saimemory_with_main_line_metadata` で `_store_memory` が `line_role="main_line"` + `scope="committed"` で呼ばれることを確認
  - `tests/test_sai_memory_storage.py` で `add_message → get_messages_paginated` の line metadata round-trip と legacy 互換を 4 件追加検証
  - `tests/test_payload_context_filter.py` 新規 28 件で `_payload_passes_context_filter` の網羅検証 (line_role / scope / pulse_id override / legacy 互換 / required_tags 互換 / 防御的処理)
  - 実機経路は `/run_playbook` Spell 実装後に再活性化するため、現状 `line='sub'` を使う Playbook 皆無 (`web_search_sub` は v0.19 で削除)。Phase 3 後段で end-to-end 検証する

### 段階 4-C 完了基準 (✅ 達成 — v0.23, 2026-05-01)

- [x] すべての builtin Playbook で `memorize.tags` に `internal` / `conversation` が含まれない (`event_message` は現状未使用)
  - `scripts/migrate_playbooks_to_lines.py` で 33 / 38 件を機械翻訳。残 5 件は対象タグなしで unchanged (basic_chat / meta_user / meta_user_manual / meta_simple_speak / meta_exec_speak)
  - `internal` 66 件 → `line_role: "sub_line"` + `scope: "volatile"` に置換 (LLM ノード 45 + memorize ノード 21)
  - `conversation` 5 件 → `line_role: "main_line"` + `scope: "committed"` に置換 (LLM ノード 5)
  - `context_profile` 75 ノードから完全削除 (4-A で無効化済みの記述を整理)
- [x] line_role / scope が memorize ノードで明示的に指定されている (or 妥当なデフォルト)
  - `MemorizeNodeDef` に `line_role` / `scope` フィールドを Pydantic に追加
  - `lg_memorize_node` (`sea/runtime_engine.py`) で `_store_memory` に渡す経路を追加
  - 未指定時は `_store_memory` 内で `pulse_context.current_line_metadata()` から自動解決 (= 現在のライン階層に従う)
- [x] Y 案で保留: `model_type=lightweight` (23 ノード) は触らず、`/run_playbook` Spell 実装と一体で 4-D で整理
- [x] DB 反映: `python scripts/import_all_playbooks.py --force` で 44 件 update 成功
- [x] 関連 7 ファイル合計: 134 件テスト pass / 0 新規回帰
  - 実機検証は次セッション以降 (まはー側で 3 シナリオ確認: ユーザー会話 / 自律 Pulse / メタ判断)

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

## 10. v0.2 改訂: アスペクト (aspect) による呼び出し時指定

**ステータス**: 設計合意 (2026-05-26, まはー)。実装前。

### 10.1 なぜ v0.1 を超える必要があるか

v0.1 (§2, §4-C) は「Playbook ノード単位で `line_role` / `scope` を明示する」方針だった。実機投入後、2026-05-26 に二重の脆さが露見した:

1. **サブ Playbook は `run_playbook` が frame を push しない** (pulse_ctx 共有、`runtime_graph.py:134` の push 条件 `parent_pulse_ctx is None` を満たさない)。よってサブ Playbook のノードは親 (Pulse-root = main_line) の frame を継承する。ノードが `line_role="sub_line"` を書き忘れると **main_line に漏れる**。
2. **`scope` は frame から一切継承されない** (`_store_memory` は line_role/line_id/origin_track_id のみ自動解決、scope は明示引数のみ)。未指定の scope は常に SQL デフォルト `committed`。

結果、`track_autonomous` の最終発言が volatile 固定だった件、`schedule_management` の memorize ノード5個が `line_role`/`scope` 未指定で main_line+committed に漏れる件が発生した。**`sub_line`/`volatile` の分類がノード作者の明示記述に依存し、書き忘れが即汚染になる**構造的脆さ。

加えて §4-C で保留した `model_type=lightweight` は、現状 `_select_llm_client` が `_force_lightweight_model` (run_playbook の line='sub') と `_pulse_type=="auto"` の場当たり判定で代用しており、ライン属性と分離している。

### 10.2 方針: 4分類を呼び出し時に1つ指定

ノードに `line_role` / `scope` / `model_type` を書く代わりに、**ライン起動時に4分類のいずれか1つ (`aspect`) を指定**する。`line_role` / `scope` / `model` は分類から内部導出され、Playbook JSON にもユーザー/ペルソナの意識にも出ない。

| `aspect` | line_role | scope | model | 代表的な起動元 |
|---|---|---|---|---|
| `CONVERSATION` (①) | `main_line` | `committed` | 標準 | `run_meta_user` (user / social / external track) |
| `WORKER` (②) | `sub_line` | `volatile` | 軽量 | `run_playbook` スペル |
| `AUTONOMOUS` (③) | `main_line` | `committed` | 軽量 | 自律 track の Pulse |
| `META` (④) | `meta_judgment` | `discardable` (確定分は `committed` に昇格) | 標準 | `meta_layer` のメタ判断 |

`line_role` だけでは ①③④ (全て `main_line`) を区別できないが、4分類なら区別できる。これが「scope を line_role に連動させる」案 (B) が成立しなかった理由 — **scope は line_role からではなく分類から導く**。

> **命名 (確定 2026-05-26)**: **`aspect`**。「全て同じペルソナの言動だが、状態の異なる一側面」の意 (不変条件 2 単一主体の記憶と整合)。`line_role` と語幹がかぶらず混同しない。AOP の aspect とは語が衝突するが本コードベース文脈では実害なし (既存の `aspect` 使用は画像の aspect ratio のみで別ドメイン)。フレーム属性 = `aspect`、enum = `Aspect`。

### 10.3 導出と継承の単一化

- `LineFrame` が `aspect` を保持。`push_line(aspect=...)` でセット。
- `current_line_metadata()` / `_store_memory` は frame の `aspect` から **`line_role` と `scope` を両方**導出する (v0.1 は line_role のみ継承、scope は未継承で committed 固定 = 10.1 のギャップの原因)。
- model 選択 (`_select_llm_client`) も frame の `aspect` から tier を導出 (まはー決定 2026-05-26: モデルも分類が決める)。現状の `_force_lightweight_model` / `_pulse_type=="auto"` 判定を置換。

### 10.4 `run_playbook` が WORKER frame を push (旧「A 案」の構造化)

`run_playbook` スペルがサブライン実行時に `aspect=WORKER` の frame を push する。これでサブ Playbook のノードは何も宣言しなくても `sub_line`/`volatile`/軽量になる。書き忘れによる main_line 汚染が原理的に起きない (10.1-1 の解消)。

**逃げ道 (override) は当面作らない** (まはー決定 2026-05-26)。アスペクトを唯一の供給源として固める。例外的に WORKER 以外で起動したい需要が出たら、後付けで `run_playbook` 引数による aspect 指定 / Playbook 側での aspect 強制を足す余地がある (YAGNI、今は実装しない)。

### 10.5 ④ メタ判断の scope 変動はクラス挙動に内包

メタ判断の「試行ターン `discardable` → 確定ターン `committed` 昇格」(不変条件 11) は **META クラスのランタイム挙動**として実装する (ノードに `scope=discardable` を書かない)。昇格ロジック (action=switch 時に discardable 行を committed へ UPDATE する既存経路, `runtime_llm.py` 周辺) を流用。

### 10.6 Playbook JSON の変更 (4-C の逆移行)

4-C でノードに入れた `line_role` / `scope` / `model_type` を**全ノードから削除**する。残すのは意味タグ (`creation`, `web_research` 等) と memorize の有無のみ。migration スクリプト (`migrate_playbooks_to_lines.py` の逆方向) で一括変換 → `import_all_playbooks.py --force` で DB 反映。

### 10.7 §2 / §4-C との関係

- §2「2軸独立」原則 (Line 軸 vs 意味タグ軸) は**維持**。変わるのは Line 軸の**指定単位**: ノード単位 → ライン起動単位 (分類)。
- §4-C「ノードに line_role/scope を明示」は本改訂で**置換** (ノードから抜いて分類に集約)。
- §7 不変条件への影響は**強化方向**: 分類が構造的に line/scope/model を保証するため、書き忘れによる不変条件破れ (不変条件 2 単一主体の記憶 / 11 メタ判断揮発) が消える。

### 10.8 実装状況 (2026-05-26 実装完了)

1. ✅ `Aspect` enum + 導出表 + `aspect_from_pulse_type` + `LineFrame.aspect` / `scope` / `model_tier` + `push_line(aspect=)` (`sea/pulse_context.py`)
2. ✅ `current_line_metadata` が `scope` も返し、`_store_memory` (`sea/runtime.py`) が未指定時に frame から `line_role` + `scope` を両方導出
3. ✅ `_select_llm_client` (`sea/runtime.py`) が active frame の `aspect.model_tier` から tier 判定 (aspect 無し legacy frame では従来フラグにフォールバック)
4. ✅ push 点配線: `run_meta_user` が `pulse_type` → `aspect` を導出して `pulse_line_aspect` を `_run_playbook` → `run_playbook` (runner) → `compile_with_langgraph` まで伝搬。`compile_with_langgraph` (`sea/runtime_graph.py`) が Pulse-root はアスペクト push、`line=="sub"` のサブラインは `Aspect.WORKER` を push (finally で pop)
5. ✅ Playbook JSON から `line_role` / `scope` / `model_type` を全削除 (`scripts/strip_playbook_line_fields.py`、20 Playbook) + DB 再 import
6. ✅ 単体テスト `tests/test_aspect_derivation.py` (9 件) + 既存 121 件パス / 🔲 実機検証 (① 会話 / ② サブ / ③ 自律 / ④ メタ の4経路) はサーバー再起動後

**実装メモ**:
- 後方互換: `aspect=None` の legacy frame では `role` 直指定 / `scope` 未導出 (committed 既定) で従来動作。明示 `scope`/`line_role` 引数は引き続きアスペクト導出より優先 (meta_judgment_finalize の committed/discardable 判定等)。
- サブライン WORKER frame の push で `run_playbook` の `_line_stack` 深さ制限 (`_MAX_LINE_STACK_DEPTH=4`) が実効化された (従来は frame が積まれず未機能だった)。
- `track_autonomous` / `schedule_management` の個別バグ修正は本移行に吸収 (両 Playbook も field 削除済み、アスペクトから導出)。
- **落とし穴 (修正済)**: field 剥がしで memorize が `{line_role, scope}` のみだったノード (tags なし) は `memorize: true` になる。`LLMNodeDef.memorize` の Pydantic 型が v0.1 では `Optional[Dict]` で bool 非対応だったため、`track_user_conversation` 等が DB ロードに失敗 → `basic_chat` フォールバック → 無発言のリグレッションを起こした。`memorize: Optional[Union[bool, Dict]]` に修正 (ランタイムは元々 True/dict 両対応)。回帰テスト `tests/test_aspect_derivation.py::TestMemorizeBoolAcceptance`。

---

## 関連ドキュメント

- [README.md](README.md) — 進捗表
- [01_concepts.md](01_concepts.md) — line / scope / 7 層ストレージモデルの概念定義
- [02_mechanics.md](02_mechanics.md) — Pulse 階層 / Playbook 起動とラインの関係
- [nested_subline_spell.md](nested_subline_spell.md) — `/run_playbook` Spell 機構 (本整理が前提)
- [phases/phase_1_base.md](phases/phase_1_base.md) — line_role / scope カラム追加の経緯
- [phases/phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) — Phase 3 タスク (本整理を含む)
- [revisions.md](revisions.md) — 改訂履歴
