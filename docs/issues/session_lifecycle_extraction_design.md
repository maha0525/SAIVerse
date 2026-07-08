# 分割設計書: SEARuntime からの記憶ライフサイクル抽出 (SessionLifecycle)

**ステータス**: 🟡 Step 1 完了（抽出＋委譲シム、2026-07-08）・Step 2/3 未着手
**優先度**: high（`docs/intent/session.md` を実装に移すときの第一歩。記憶アーキテクチャ v2 Phase 0 で Chronicle 系に入る場合も先にこれ）
**作成日**: 2026-07-06
**関連**: `docs/overview/architecture_health.md` §3.2、`docs/intent/session.md`（起草中 v0.1）、`docs/overview/landscape.md` §6
**行番号の基準**: commit `b4ca78e`（branch `feature/autonomous-behavior-v2`）時点

## 目的

`SEARuntime`（`sea/runtime.py`、2,829 行 / 90 メソッド）には **Playbook 実行エンジン**と
**記憶ライフサイクル**（Anchor / Metabolism / Chronicle）という別概念層（landscape §4 と §6）が
同居している。後者を `SessionLifecycle` クラスとして抽出する。

これは掃除ではなく**受け皿づくり**: 「Session という統一制御単位はコードに存在しない」
（landscape §6）のは置き場所が SEARuntime に埋まっているからで、抽出したクラスが
session.md 実装時にそのまま Session 統一制御単位へ育つ。

## 1. 抽出対象（runtime.py L1511-2598、約 1,090 行 — ほぼ連続領域）

| メソッド | 行 | 役割 |
|---|---|---|
| `_get_high_watermark` / `_get_low_watermark` | 1511 / 1522 | Metabolism 発火閾値 |
| `_load_anchors` / `_save_anchors` | 1535 / 1554 | per-model anchor dict の永続化 |
| `_get_anchor_validity_seconds` | 1573 | TTL 設定解決 |
| `_resolve_metabolism_anchor` | 1593 | 3段フォールバックの anchor 解決 |
| `_update_anchor_for_model` | 1657 | anchor 書き込み |
| `_anchor_entry_ttl_seconds` | 1707 | entry 単位 TTL |
| `_touch_anchor_after_llm_call` | 1721 | LLM 成功後の touch（cache write 時刻） |
| `_check_token_threshold` | 1798 | token 閾値での Metabolism 予約 |
| `_schedule_cache_ttl_pulse` | 1817 | TTL 失効前 Pulse のスケジュール |
| `_maybe_run_metabolism` / `_run_metabolism` | 1896 / 1954 | Metabolism 判定と実行本体 |
| `_is_chronicle_enabled_for_persona` | 2023 | トグル（ライフサイクル専用） |
| `_is_autonomous_chronicle_enabled_for_persona` | 2036 | 同上 |
| `_is_memory_weave_context_enabled` | 2072 | 同上 |
| `_generate_chronicle` | 2111 | General Chronicle 生成（262 行） |
| `_ensure_recall_embeddings` | 2375 | 埋め込みバックログ処理 |
| `_generate_track_chronicle` | 2412 | Track Chronicle 生成 |

**移動しないもの**（ライフサイクル外で広く使われている）:

- `_is_spell_enabled_for_persona`（L2085）— runtime_graph / work_session / head_pipeline が使用
- `_is_realtime_info_enabled_for_persona`（L2098）— `_build_realtime_context` が使用
- `_is_auto_recall_enabled_for_persona`（L2054）— runtime_context の自動想起が使用。
  ライフサイクル隣接だが Session 概念確定まで SEARuntime 残置でよい
- `_prepare_context` / `preview_context` — 既に `runtime_context.py` へ委譲済み（触らない）

## 2. 設計

### 2.1 前例に従う

SEARuntime には既に同型の抽出前例がある（`__init__` L67-73）:
`RuntimeEmitters`（emit 系）と `RuntimeEngine`。`SessionLifecycle` も同じパターン:

```python
# sea/session_lifecycle.py（新規）
class SessionLifecycle:
    """Anchor / Metabolism / Chronicle — Session (短期記憶) の節目管理。
    docs/intent/session.md の「Session 統一制御単位」の実装先。"""
    def __init__(self, runtime: "SEARuntime", manager_ref: Any):
        self.runtime = runtime      # 過渡期の後方参照（§4 で削減）
        self.manager = manager_ref

# sea/runtime.py __init__ に追加
self.session_lifecycle = SessionLifecycle(runtime=self, manager_ref=manager_ref)
```

メソッド名は移動時に先頭 `_` を外して公開 API 化する
（例: `_touch_anchor_after_llm_call` → `touch_anchor_after_llm_call`）。

### 2.2 SEARuntime に委譲シムを残す

外部呼び出し元（§3）とテストを一切変えずに移すため、SEARuntime 側に同名メソッドを残して委譲:

```python
def _touch_anchor_after_llm_call(self, persona, usage) -> None:
    return self.session_lifecycle.touch_anchor_after_llm_call(persona, usage)
```

シムは §4 Step 2 で呼び出し元を直接参照に移行後、削除する。

## 3. 外部呼び出し元インベントリ（シムが守る範囲）

| 呼び出し元 | 使用メソッド |
|---|---|
| `sea/runtime_llm.py`（5箇所: L1646, 2435, 2587, 2996, 3367） | `_touch_anchor_after_llm_call` |
| `sea/work_session.py:569` | `_touch_anchor_after_llm_call` |
| `sea/runtime.py:194`（run_meta_user 末尾） | `_maybe_run_metabolism` |
| `sea/runtime_context.py`（L136, 170, 177, 203） | `_resolve_metabolism_anchor` / `_generate_chronicle` / `_generate_track_chronicle` / `_get_low_watermark` |
| `api/routes/people/config.py`（L231, 241） | `_generate_chronicle(force=True)` / `_ensure_recall_embeddings` |
| `api/routes/people/cache_status.py`（L144-145、getattr 経由） | `_load_anchors` / `_get_anchor_validity_seconds` |
| `tests/sea/test_runtime_regression.py` | `_maybe_run_metabolism` を Mock 差し替え |
| `tests/test_day_scenario.py` / `tests/test_head_pipeline_anchor_ttl.py` | stub 実装で上書き |

⚠️ **唯一シムで守れないもの**: `tests/test_cache_lifecycle.py`（L121, 133, 147）は
`SEARuntime._anchor_entry_ttl_seconds.__get__(rt)` のように**クラスから unbound メソッドを
直接束縛**している。シム化するとシム経由で stub の `rt.session_lifecycle` を探しに行って落ちる。
→ 移行と同時に `SessionLifecycle._anchor_entry_ttl_seconds.__get__(...)` 相当へ書き換える
（機械的置換で済む。テストの検証対象ロジック自体は不変）。

## 4. 段階手順

1. ✅ **Step 1（機械的移動）完了（2026-07-08）**: L1511-2598 の対象メソッドを `sea/session_lifecycle.py` へ移動し、
   SEARuntime に委譲シムを設置。`test_cache_lifecycle.py` の直束縛のみ書き換え。
   `self.` 参照のうち移動対象同士は `self.` のまま、残留メソッド
   （`_is_auto_recall_enabled_for_persona` 等）への参照は `self.runtime.` 経由にする
2. **Step 2（呼び出し元の直接参照化）**: §3 の呼び出し元を触るついでに
   `runtime.session_lifecycle.xxx()` へ移行。全箇所移行後にシム削除
3. **Step 3（session.md 実装時）**: SessionLifecycle に Session の識別
   （`(persona_id, model_key)` 粒度）と状態を持たせ、「anchor touch → 履歴取得 → head render の
   三部構成が個別に動く」現状（landscape §6 注記）を統一制御に置き換える。
   ここから先は intent doc（session.md）側の設計に従う

### 検証

- Step 1 は挙動不変: `python -m pytest tests/test_cache_lifecycle.py tests/sea/ tests/test_head_pipeline_anchor_ttl.py tests/test_auto_recall.py tests/test_day_scenario.py` + `ruff check sea/`
- 実機は Metabolism を1回踏むのが理想だが、既存テストが anchor / TTL / metabolism 判定を
  カバーしているので、通常会話1往復（anchor touch 経路）+ `cache_status` API 表示確認で足りる

## 5. 注意

- `_generate_chronicle` は Memopedia Fragment 生成の相乗り点（`batch_callback` に
  `entity_extractor`、landscape §5「二重パイプライン統合」）。移動で callback 配線を切らないこと
- `_run_metabolism` L2008 の `DynamicStateManager.on_metabolism` は lazy import — そのまま維持
- meta 判断ログの promote（`_promote_meta_judgment_in_pulse`、`saiverse/meta_layer.py` 側コメントが
  `sea/runtime.py:_generate_track_chronicle` を参照）— 移動後は meta_layer.py のコメントも追従更新

## ログ

- 2026-07-06: アーキテクチャ健診（`architecture_health.md` §3.2）を受けて本設計書を起草（エア / Fable 5）
- 2026-07-08: Step 1 完了。対象メソッド群を `sea/session_lifecycle.py`（SessionLifecycle）へ移動し、SEARuntime に委譲シムを設置（挙動不変）。`test_cache_lifecycle.py` の直束縛を `SessionLifecycle.xxx.__get__` へ書き換え。gold_panning の受け皿として本抽出の上に砂金採りを配線（gold_panning.md）。Step 2（呼び出し元の直接参照化＋シム削除）と Step 3（Session 統一制御化）は未着手（メティス）
