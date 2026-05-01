# Phase 3 — ライン仕様 + Track 種別 Playbook

**親**: [../README.md](../README.md)
**ステータス**: 🟡 約 60%
**旧称**: Phase C-2 (line / context_profile DEPRECATED) + Phase 1.2 (meta_judgment 経路) + Phase 1.4 (DEPRECATED 宣言)

---

## 目的

旧 `context_profile` / `model_type` を `line: "main"|"sub"` 指定に集約。Track 種別ごとの専用 Playbook を整備し、メインライン Pulse の判断ロジックを Playbook で表現できるようにする。

---

## タスク

### ライン仕様の導入

| 項目 | 状態 | 実装場所 |
|------|------|---------|
| `SubPlayNodeDef.line` フィールド (`"main"|"sub"`) | ✅ | `sea/playbook_models.py:287-297` |
| ライン runtime (親 messages のコピー分岐 + report_to_parent append) | ✅ | `sea/runtime_nodes.py` |
| `LLMNodeDef.context_profile` DEPRECATED 化 | ✅ | `sea/playbook_models.py:48-55` |
| `LLMNodeDef.model_type` DEPRECATED 化 | ✅ | `sea/playbook_models.py:57-62` |
| `report_to_parent` 必須バリデーション (`can_run_as_child=true` 用) | 🟡 | runtime ルーティングは実装、厳密化は警告ログのみ |
| `exclude_pulse_id` 廃止 | 🔲 | 旧仕様コードは現存 |

### Track 種別 Playbook

| Playbook | 状態 | 場所 / 備考 |
|----------|------|------|
| `meta_judgment.json` | ✅ | `builtin_data/playbooks/public/` |
| `meta_judgment_dispatch.py` 経由パス | ✅ | (Phase 1.2 マージ済み) |
| `track_user_conversation.json` | ✅ | `builtin_data/playbooks/public/` |
| `track_autonomous.json` | ✅ | `builtin_data/playbooks/public/` |
| `track_social.json` | 🔲 | 未着手 |
| `track_external.json` | 🔲 | 未着手 |
| `track_waiting.json` | 🔲 | 未着手 |

### 旧 Playbook の翻訳

| 項目 | 状態 |
|------|------|
| 翻訳前段の Playbook 整理 (旧プロトタイプ削除 + Spell 化) | ✅ (v0.19, 2026-05-01) |
| 既存 Playbook の `context_profile` → `line` 翻訳 (`migrate_playbooks_to_lines.py`) | 🔲 |
| `context_profile` / `model_type` / `exclude_pulse_id` の完全削除 | 🔲 (全 Playbook 翻訳後) |

#### 整理結果 (2026-05-01)

翻訳作業に入る前に、対象 Playbook を圧縮するため以下を実施:

- **削除した Playbook**: 19 件
  - 旧自律稼働プロトタイプ: `meta_auto`, `meta_auto_full`, `sub_router_auto`, `sub_perceive`, `sub_reaction`, `sub_finalize_auto`, `sub_execute_phase`, `sub_detect_situation_change`, `sub_generate_want`, `wait`
  - テスト/残骸: `meta_websearch_demo`, `detail_recall_playbook`, `meta_agentic`, `agentic_chat_playbook`
  - Spell 代替済み: `memory_recall_playbook` (`memory_recall_unified` Spell), `web_search_step` (`source_web` Playbook)
  - 新規 Spell 化: `uri_view` (`resolve_uri` ツールに `spell=True`), `send_email_to_user_playbook` (`send_email_to_user` ツールに `spell=True`)
  - サンプル保存後削除: `web_search_sub` ([sub_line_playbook_sample.md](sub_line_playbook_sample.md) に内容を保存)

- **更新した Playbook**: `deep_research_playbook` の `exec_search` ノードを `web_search_step` → `source_web` に差し替え

- **コード側の整理**:
  - `sea/runtime.py`: `run_meta_auto` 関数削除、`_choose_playbook` の `meta_auto` fallback 削除
  - `sea/pulse_controller.py`: 旧 `auto-without-meta_playbook` 分岐削除、auto pulse は `meta_playbook` 必須化
  - `saiverse/conversation_manager.py`: `ConversationManager` クラスを no-op 化 (新認知モデルの `track_autonomous` + PulseScheduler 経路に統一)
  - `builtin_data/tools/detail_recall.py`: 削除

- **DB**: playbooks テーブル 67 → 48 件

整理に伴うコード経路の変更詳細は [revisions.md](../revisions.md) v0.19 を参照。

#### 残課題 (Phase 3 翻訳作業外で対応)

- `ConversationManager` クラスごと削除 (saiverse_manager.py / manager/runtime.py / manager/admin.py の参照整理を伴う)
- ~~DB 残骸の整理~~ → 起動時 prune を `playbook_sync.py` に追加して解決 (revisions v0.19 追補)

---

## 残タスクの詳細

### `track_social.json` Playbook

交流 Track 用。同 Building 内の他ペルソナ発話 (audience に自分が含まれる) で alert 化された時の処理を担う。

**設計の出発点**:

- メインライン (重量級) で起動
- 相手ペルソナの Person Note を自動開封
- 多者会話の場合、audience 解釈ロジックを Playbook 内で展開
- 応答完了後は `wait_response` 状態 (= 次の発話を待つ)

参考実装: `track_user_conversation.json` を雛形に、ユーザー固有処理を「相手ペルソナ固有処理」に置き換える形。

### `track_external.json` Playbook

外部 SAIVerse / Discord / X 等への通信 Track 用。`output_target=external:<channel>:<address>` で動作。

**設計の出発点**:

- 外部チャネルごとの送信ロジック (Discord webhook / X API / SAIVerse 間 dispatch 等) はツールに分離
- Playbook はメッセージ生成と送信タイミングの判断のみ担う
- `waiting` 状態への遷移トリガ (応答待ち) を含む

### `track_waiting.json` Playbook

待機 Track の起動時 (応答到達後の処理)。

**設計の出発点**:

- `waiting_for` の type に応じて応答内容を解釈
- メインライン Pulse として起動し、応答内容を踏まえた次のアクションを判断
- MCP Elicitation 等の構造化応答にも対応

### `report_to_parent` 厳密バリデーション

現状: runtime ルーティングは実装されているが、`output_schema` に `report_to_parent` が含まれていない子 Playbook も警告ログのみで通ってしまう。

**やること**:

1. `PlaybookSchema` に `can_run_as_child: bool` メタ属性を追加 (デフォルト false)
2. Playbook ロード時 (`save_playbook` ツール / `import_playbook.py`) でバリデーション
3. `can_run_as_child=true` かつ `report_to_parent` が `output_schema` にない → 例外 (警告ではなく)

```python
def validate_child_playbook(playbook: PlaybookSchema) -> None:
    if not playbook.can_run_as_child:
        return
    if "report_to_parent" not in (playbook.output_schema or []):
        raise ValueError(
            f"Playbook '{playbook.name}' lacks 'report_to_parent' in output_schema. "
            f"Child playbooks must report back to their parent line."
        )
```

### `migrate_playbooks_to_lines.py`

既存 Playbook の `context_profile` / `model_type` を `line: "main"|"sub"` に翻訳するスクリプト。

**翻訳ルール (案)**:

| 旧仕様 | 新仕様 |
|--------|-------|
| `context_profile: "default"` + `model_type: undefined` | `line: "main"` |
| `context_profile: "default"` + `model_type: "lightweight"` | `line: "main"` (継続) + 軽量モデル指定は別フィールドで継承 |
| `context_profile: "isolated"` 等 | `line: "sub"` (分岐) |
| `context_profile: "worker"` (完全独立) | Phase 6 で別途実装、現状は警告 |

機械的に翻訳できる部分は自動化、判断が必要な部分はユーザー確認を求める対話モード。

### `context_profile` / `model_type` / `exclude_pulse_id` の完全削除

すべての Playbook が新仕様に移行したことを確認後:

- `LLMNodeDef.context_profile` 削除
- `LLMNodeDef.model_type` 削除
- `CONTEXT_PROFILES` 定義削除
- `exclude_pulse_id` および関連の `PulseContext` 制御削除
- 関連ランタイムコード削除

---

## 段階移行計画

旧 Phase C-2a/b/c に対応:

| サブ Phase | 内容 | 状態 |
|-----------|------|------|
| 3-a (旧 C-2a) | 新仕様の追加 (旧仕様と共存) | ✅ 完了 |
| 3-b (旧 C-2b) | 既存 Playbook の改修 (`migrate_playbooks_to_lines.py`) | 🔲 未着手 |
| 3-c (旧 C-2c) | 旧仕様の削除 | 🔲 (3-b 完了後) |

---

## Playbook で表現できる範囲の確認

実装着手前の確認項目:

1. **モデル指定**: `line: "main"` (重量級指定) を確実に効かせられるか
2. **Track 情報をプロンプトに埋め込む**: 状態変数経由で Track 固定/動的情報を注入できるか
3. **スペル発火と応答生成の混在**: 1 ノードで「内的独白 + スペル + 発話」を表現できるか (Phase 2 で部分的に実証済み)
4. **Pulse 完了通知**: Playbook 完了時に呼び出し元 (PersonaCore or MetaLayer) に「次の挙動」を伝える機構が必要か

3, 4 が Playbook で表現できなければ (b) 路線 (メインライン Pulse 開始処理を Python で新規実装) に切り替える。

---

## 完了の判定基準

- [x] `SubPlayNodeDef.line` フィールドが受け入れられ、`line: "sub"` で子ラインが分岐実行される
- [x] 子ラインの `report_to_parent` が親メッセージに append される
- [ ] `can_run_as_child=true` Playbook が `report_to_parent` を欠いていたらロード時例外
- [ ] 全 Track 種別 (user_conversation / social / autonomous / external / waiting) の Playbook が揃う
- [ ] 既存 Playbook が `migrate_playbooks_to_lines.py` で全て翻訳済み
- [ ] `context_profile` / `model_type` / `exclude_pulse_id` 関連コードが削除された

---

## Phase 4 以降への前提条件

- Track 種別 Playbook が揃っていること → Phase 4 の MainLineScheduler が「どの Playbook を起動するか」を Handler から取れる
- ライン仕様が安定していること → Phase 4 の `on_periodic_tick` がメインライン Playbook を呼び出せる

---

## 関連ドキュメント

- [../02_mechanics.md](../02_mechanics.md) — Pulse 開始プロンプト構成 / ライン階層
- [../04_handlers.md](../04_handlers.md) — Handler / Playbook の関係
- [phase_4_pulse_scheduler.md](phase_4_pulse_scheduler.md) — Scheduler 実装
