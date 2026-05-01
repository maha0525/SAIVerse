# Handoff: Phase 3 残作業の実装 (line vs タグ責務分離 + `/run_playbook` Spell)

**親**: [README.md](README.md)
**前回 handoff (ロードマップ)**: [handoff_2026-05-01.md](handoff_2026-05-01.md)

このドキュメントは **次セッションで実装に着手する人** 向け。設計判断の根拠は intent doc 群に集約済みなので、本 handoff は「最初の 30 分で何をするか」「触るファイル」「検証方法」「ハマりどころ」だけを書く。

---

## このセッションを開く前に読むべき docs (順番)

1. [README.md](README.md) — 進捗表で全体像を掴む
2. [line_tag_responsibility.md](line_tag_responsibility.md) — **本セッションの中核**。段階 4-A〜4-D の設計
3. [nested_subline_spell.md](nested_subline_spell.md) — 後段 (`/run_playbook` Spell) の設計
4. [phases/phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) — Phase 3 全体タスク

ざっと目を通すだけで設計は把握できる。実装のスコープは段階 4-A → 4-B → 4-C → 4-D の順で進める。**全部を一気にやらない**: 4-A だけで 1 セッション、4-B + 4-C で 1 セッション、4-D で 1 セッションくらいの粒度を想定。

---

## 段階 4-A: context 構築を line ベースに切替

### 目的

`sea/runtime_context.py` の `_prepare_context` (および周辺) が `required_tags` 主軸でフィルタしているのを、`line_role` / `scope` 主軸に置換する。タグはあくまで意味分類で、context 構築の判断軸から外す。

### 触るファイル

| ファイル | 何をするか |
|---|---|
| `sea/runtime_context.py:376-400` 周辺 | `required_tags` フィルタを `line_role` / `scope` フィルタに書き換え |
| `sai_memory/memory/storage.py:488-556` 周辺 | query 経路で line_role / scope の INDEX を活用するよう整理 |
| `persona/history_manager.py:317+` | `required_tags` 引数を受け取っている箇所を整理 |
| 既存テスト (`tests/test_*context*`, `tests/test_*history*`) | 期待値を line ベースに更新 |

### 実装方針

1. **メインライン Pulse の context 構築**: `line_role IN ('main_line') AND scope = 'committed'` で絞る
2. **サブライン Pulse の context 構築**: 自分の `line_id` 配下 + 親の `committed` メッセージ
3. **メタ判断 Pulse**: `line_role = 'meta_judgment'` の `discardable` メッセージを動的注入 (Phase 2 で実装済み機構をそのまま使う)
4. **タグ参照を完全廃止**: `required_tags` 引数を関数シグネチャから削除。意味分類タグは search/recall でのみ使う

### 起点 (最初の作業)

```
1. sea/runtime_context.py を開いて _prepare_context の `required_tags` を grep
2. 各箇所で「タグでフィルタしているのは line_role/scope のどれに対応するか」をコメントで併記
3. 1 箇所ずつ line_role/scope ベースに置換、テストを通す
4. すべて置換できたら required_tags 引数を削除
```

### 検証方法

- 既存の `tests/test_subplay_line.py` (11 件パス確認済み) が壊れないこと
- 既存の `tests/sea/test_runtime_regression.py` が壊れないこと (1 件は line 整理前から落ちてるので除外)
- 実機で `air_city_a` を起動し、ユーザー会話のメインライン context が変わらないことを確認
- `~/.saiverse/user_data/logs/{session}/sea_trace.log` で `_prepare_context` の出力メッセージ数 + line_role 内訳を確認

### ハマりどころ

- **`include_internal` フラグ**: `runtime_context.py:377` で現役。これを引き剥がすと一部 Playbook (autonomy 系) が壊れる。**段階 4-C で Playbook 側の `memorize.tags` を整理するまでは、暫定的に `include_internal` の挙動を line_role='sub_line' のフォールバックで吸収する**のが安全。完全削除は段階 4-D。
- **`pulse_id` カラム + 旧タグ併行記録** (`storage.py:1292-1295`): Phase 2.5 で pulse_id カラム化済みだが、`"pulse:{uuid}"` タグも併行記録している。読み出しは pulse_id カラム経由に統一して OK。タグ書き込みは段階 4-D で削除。
- **メタ判断 Pulse の context**: `meta_judgment_log` の動的注入 (Phase 2 で実装済) は line_role='meta_judgment' に依存している。ここの挙動は変えない。

---

## 段階 4-B: `sub_play` の `report_to_main` を line ベースに統一

### 目的

`sea/runtime_nodes.py:265-281` の `report_to_main` 渡し方が `tags=["conversation"]` ハードコードを使っているのを、line メタデータベースに切替。同時に `report_to_main` → `report_to_parent` リネーム ([nested_subline_spell.md §7](nested_subline_spell.md))。

### 触るファイル

| ファイル | 何をするか |
|---|---|
| `sea/runtime_nodes.py:265-281` | `_store_memory(tags=["conversation"], ...)` → `_store_memory(line_role="main_line", scope="committed", ...)` |
| `sea/runtime_nodes.py:238-296` 全体 | `report_to_main` → `report_to_parent` にリネーム |
| `02_mechanics.md` の関連記述 | リネーム反映 |
| `phases/sub_line_playbook_sample.md` | サンプル内のフィールド名修正 |
| 既存 Playbook で `output_schema` に `report_to_main` を含むもの | 該当なし (`web_search_sub` は削除済み、`source_*` は使ってない) ので心配無用 |

### 実装方針

1. リネーム: `report_to_main` を grep してすべて `report_to_parent` に
2. `_store_memory` 呼び出しでタグハードコードを line ベースに置換
3. (1)(2) はサブラインの output_schema 検証も連動して変更が必要かも (要確認)

### 検証方法

- `tests/test_subplay_line.py` が壊れないこと
- 実機で `deep_research_playbook` を起動 → サブライン (source_web) からの結果がメインラインに伝わることを確認

### ハマりどころ

- **既存サブライン Playbook の output_schema**: 旧 `web_search_sub.json` は削除済みだが、サンプルとしてまだ docs に残っている。サンプル側もリネーム反映する。
- **後方互換**: 旧名 `report_to_main` は内部のみで使われているフィールド名なので後方互換不要。一括リネームで OK。

---

## 段階 4-C: 既存 Playbook の `memorize.tags` 整理

### 目的

各 Playbook の `memorize` ノードで「context 制御のためのタグ」と「意味分類のためのタグ」を分離。`internal` / `conversation` / `event_message` 等の context 制御用タグを廃止し、`line_role` / `scope` に置換。

### 実装方針: `migrate_playbooks_to_lines.py` で一括変換

これは **Phase 3 翻訳 (`context_profile` / `model_type` → `line` の翻訳) と一体で実施**。既に handoff_2026-05-01.md で言及した migration スクリプトを拡張する形:

```python
# scripts/migrate_playbooks_to_lines.py の変換対象 (拡張版)

def migrate_playbook(data: dict) -> dict:
    for node in data.get("nodes", []):
        # 旧変換 (context_profile / model_type → line)
        if "context_profile" in node:
            ...
        if "model_type" in node:
            ...
        # 新規: memorize.tags の整理
        memorize = node.get("memorize")
        if isinstance(memorize, dict):
            tags = memorize.get("tags", [])
            # context 制御用タグを除去 + line_role / scope を抽出
            line_role, scope, remaining_tags = split_control_tags(tags)
            if line_role:
                memorize["line_role"] = line_role
            if scope:
                memorize["scope"] = scope
            memorize["tags"] = remaining_tags  # 意味分類のみ残す
    return data


def split_control_tags(tags: list[str]) -> tuple[str | None, str | None, list[str]]:
    """context 制御用タグを line_role / scope に翻訳して、残りを返す"""
    line_role = None
    scope = None
    remaining = []
    for tag in tags:
        if tag == "internal":
            line_role = "sub_line"
            scope = "volatile"
        elif tag == "conversation":
            line_role = "main_line"
            scope = "committed"
        elif tag == "event_message":
            line_role = "main_line"
            scope = "committed"
            # event_message は意味分類としても残したい場合があるので要相談
        else:
            remaining.append(tag)
    return line_role, scope, remaining
```

### 触るファイル

- `scripts/migrate_playbooks_to_lines.py` (新規作成)
- `builtin_data/playbooks/public/*.json` (一括変換、10〜15 件影響予定)
- `sea/playbook_models.py` の `MemorizeNodeDef` (or 同等) に `line_role` / `scope` フィールド追加

### 起点

```
1. playbook_models.py の memorize 関連の定義を読む
2. line_role / scope フィールドを追加 (省略可、デフォルト値は None)
3. migrate スクリプトを書く (--dry-run と --apply オプション付き)
4. dry-run で全 Playbook の差分を確認
5. apply して、import_all_playbooks.py で DB に反映
```

### 検証方法

- 全 Playbook が新形式で import できること
- 既存の動作 (autonomy_creation の Pulse 等) が変わらないこと
- 実機で各種 Playbook を一通り走らせて挙動確認

### ハマりどころ

- **`event_message` タグ**: これは「動的状態変化を常時表示する」専用タグで、Chronicle 対象外という意味分類も持つ。単純に削除すると Chronicle 連携が壊れる可能性。**意味分類として残す + line_role/scope を併記**が無難。
- **`playbook_result` / `creation` / `web_research` 等**: 純粋な意味分類なのでそのまま残す。
- **半自動翻訳**: 機械的に変換するだけだと誤訳リスクがある (特に複数タグが混在しているケース)。dry-run の出力を一通り目視確認してから apply する。

---

## 段階 4-D: 旧 DEPRECATED コードの削除

### 目的

過渡期の二重制御を完全に消す。

### 削除対象

| 対象 | 削除箇所 |
|---|---|
| `include_internal` パラメータ | `sea/runtime_context.py:377`、`persona/history_manager.py:317+`、関連関数シグネチャ |
| `pulse:{uuid}` タグの併行記録 | `sai_memory/memory/storage.py:1292-1295` |
| `required_tags` パラメータ | 4-A で線形に廃止していれば残骸はないはず、念のため grep |
| `LLMNodeDef.context_profile` フィールド | Phase 3 翻訳完了後に削除 (これは Phase 3 翻訳タスク本体に含まれる) |
| `LLMNodeDef.model_type` フィールド | 同上 |
| `exclude_pulse_id` 関連 | `sea/pulse_context.py` 周辺、関数の引数からも削除 |

### 検証方法

- ruff チェックで未使用コードがないこと
- 全テストパス
- 実機で 1 通りシナリオを回す (ユーザー会話 / 自律 Pulse / メタ判断 / サブライン)

### ハマりどころ

- **削除前に grep**: それぞれの削除対象が**本当に使われていない**ことを grep で確認。コードの残骸を拾い忘れると runtime エラーになる。
- **段階 4-A〜4-C を全部終えてから**: 順序を守る。4-A の途中で 4-D に手をつけると broken な状態が長く続く。

---

## `/run_playbook` Spell 実装 (段階 4 完了後)

[nested_subline_spell.md](nested_subline_spell.md) §12 の段階移行 7 ステップに従う。段階 4-A〜4-D が完了していれば、line ベースの土台が整った状態なので Spell 機構は素直に乗る。

実装の最初の一歩:

1. **`builtin_data/tools/run_playbook.py`** を新規作成 (Spell 定義)
2. **`sea/runtime_llm.py`** の Spell loop に `/run_playbook` 検出 → `runtime._run_playbook()` 呼び出しの分岐を追加
3. **`sea/pulse_context.py`** の `_line_stack` 深さチェック (4 階層上限) を入れる
4. **`router_callable` フラグのチェック** を Spell 実行時に追加

詳細は nested_subline_spell.md を見て、必要なら別途実装 handoff を切る。

---

## 触らない方が良い領域

- **メタ判断 Pulse (Phase 2 完了済み)**: `meta_judgment_log` 機構、per-persona Lock、自律先制と外部 alert のレース解消は実機検証済み。安易に変えない。
- **AutonomyManager (純粋タイマー)**: Phase C-2 で再構成済み、廃止しない。
- **Phase 1 の line_role / line_id / scope カラム**: スキーマ自体は変えない。書き込み経路だけ整理する。
- **不変条件 1〜12** (`README.md` 後半): どの整理でもこれらが破られないか毎回確認する。

---

## 判断が必要な論点 (実装中に出てくる可能性)

実装中に「これどうする？」となる細部。本 handoff 起草時点で未確定:

1. **`event_message` タグ** の扱い: 意味分類として残すか、line_role/scope に完全置換するか
2. **Migration スクリプトの dry-run 出力フォーマット**: diff 表示か JSON 比較か (人間が一通り確認しやすい形式が望ましい)
3. **段階 4-A の段階的リリース**: 一気に置換するか、`required_tags` を残しつつ line_role/scope を OR 条件で追加 → 後で `required_tags` を削除、の 2 段階か (後者は安全だが工数増)
4. **サブライン内の自由発話ノードの位置付け**: `nested_subline_spell.md §10` で「将来検討」とした「ノード単位 Spell オフ設定」を実装するか
5. **`router_callable` フラグの名前変更**: するなら本実装と一体で。後方互換不要 (内部フラグ)

これらは実装着手時にまはーに確認するか、実装の感触で判断する。

---

## 検証用チェックリスト (実装完了時に確認)

- [ ] `_prepare_context` のフィルタが line_role / scope のみで動作
- [ ] `required_tags` パラメータが関数シグネチャから消えている
- [ ] `sub_play` の `report_to_main` (リネーム後 `report_to_parent`) が line メタデータベースで親に伝搬
- [ ] すべての builtin Playbook で `memorize.tags` に context 制御用タグ (`internal` / `conversation` / `event_message`) が含まれない
- [ ] `include_internal` / `pulse:{uuid}` タグ併行記録 / `exclude_pulse_id` の DEPRECATED コードが削除済み
- [ ] 既存テスト全パス (`tests/test_subplay_line.py`, `tests/sea/test_*`, `tests/test_meta_layer.py`, `tests/test_track_manager.py` 等)
- [ ] 実機 `air_city_a` で以下シナリオが動く:
  - ユーザー会話の往復
  - 自律 Pulse (track_autonomous)
  - メタ判断 Pulse (alert / 定期 tick 両方)
  - サブライン (deep_research / memory_research)
  - `/run_playbook` 経由のサブライン起動 (実装後)

---

## 関連ドキュメント

- [README.md](README.md) — 進捗表
- [line_tag_responsibility.md](line_tag_responsibility.md) — 本実装の設計
- [nested_subline_spell.md](nested_subline_spell.md) — 後段 (`/run_playbook` Spell) の設計
- [phases/phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) — Phase 3 全体タスク
- [revisions.md](revisions.md) — 改訂履歴 (v0.20 で本整理を記録)
- [handoff_2026-05-01.md](handoff_2026-05-01.md) — Phase 3 全体ロードマップ handoff
- [handoff_2026-04-30.md](handoff_2026-04-30.md) — Phase 2 完了時 handoff (前提)
