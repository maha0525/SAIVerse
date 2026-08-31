# Intent Document: スレッドベースのメッセージルーティング（構想）

## ステータス

**構想段階 (v0.1)**。実装計画は未策定。実装着手は早くても v0.4.0 以降を想定。

このドキュメントは、現行のタグベース可視性管理の限界を起点に、より素直な代替設計の方向性を残しておくためのもの。詳細は別セッションで詰める。

---

## なぜ必要か（解決する問題）

### きっかけ

2026-04-27 の v0.3.0 開発で、`v0_3_0_dynamic_state_reset` ハンドラが SAIMemory に挿入する通知メッセージのタグを `["internal", "system_event", "version_upgrade"]` と設定したところ、**ペルソナの会話コンテキストに取り込まれない**ことが判明。

原因は `sea/runtime_context.py` の `required_tags = ["conversation", "event_message"]` というハードコードされたフィルタ。`event_message` タグが含まれていないと、コンテキスト構築時に拾われない。

### 構造的な問題

現状の設計は **「ペルソナに見えるかどうか」をタグで管理している**。しかしタグは本来「メッセージを横断的に分類する」属性であり、可視性制御に向いていない:

- 新しい種類のメッセージを追加するたびに、既存のフィルタロジック（`runtime_context.py` の `required_tags`、想起対象の判定、Chronicle 化対象の判定など）を更新しなければならない
- 「タグの組み合わせ」が暗黙の契約になっており、`event_message` を1つ忘れるだけで「ペルソナから見えない通知」になる（今回の事故）
- どのタグの組み合わせがどのフィルタに引っかかるか、コードを横断しないと把握できない
- メッセージ挿入側とコンテキスト構築側が**結合**している

タグの数が増えるほど、この負債は増える。`v0.3.0` のロードマップでは認知モデル（Track / Note / ライン）や統一記憶探索など、新種のメッセージが多く増える見込みであり、現行方式は早晩限界を迎える。

---

## 提案する方向性

**「どこに溜めるか」を決めれば「どこから読むか」が決まる**、というスレッドベースの設計。

### 基本構造

各メッセージは「適切なスレッド」に格納される:

| スレッド種別 | 例 | 内容 |
|---|---|---|
| 会話本体 | `<persona_id>:<building_id>` | ペルソナとユーザー / 他ペルソナの対話 |
| 永続パーソナルスレッド | `<persona_id>:__persona__` | ペルソナ単位で建物を跨いで残るもの |
| イベント通知 | `<persona_id>:__events__` | 外界からのシステム通知（入退室、Memopedia 変化、アップデート検知など） |
| 内部推論 | `<persona_id>:__internal__` | pulse の思考、router 判断、tool decisions |
| 想起・要約 | `<persona_id>:__summary__` | 要約、Chronicle ソース |

タグは「カテゴリラベル」として残るが、可視性の制御から切り離す。

### コンテキスト構築の合成プロファイル

「どのスレッドをどう混ぜるか」のレシピを `context_profile` として定義する:

```yaml
# 例: 通常の会話用プロファイル
profile: conversation
threads:
  - source: conversation_thread
    weight: 1.0
  - source: events_thread
    merge: chronological  # 時系列順にマージ
```

```yaml
# 例: pulse 内部思考用
profile: pulse_internal
threads:
  - source: conversation_thread
  - source: events_thread
  - source: internal_thread
    merge: chronological
```

これによって、メッセージ挿入時は「正しいスレッドに送る」だけで済み、コンテキスト構築は宣言的なレシピで制御できる。

---

## 主な論点（別セッションで詰める）

1. **既存スキーマからの移行戦略**
   - `messages.thread_id` 列は既にあるが、運用上は `:__persona__` と building thread しか使われていない
   - 既存メッセージの再分類が必要か（タグから推測してスレッドに振り分け）、それともスナップショット時点を境に新方式へ移行か
   - 大規模なマイグレーションになる場合、バージョン認識基盤の Phase 2 ハンドラの形で実装する

2. **タグの位置づけの再定義**
   - タグは「カテゴリラベル」として残す（検索・分類用）
   - 可視性制御から切り離す
   - 既存の `event_message` `internal` `summary` 等は意味的に役割を変えるか、廃止か

3. **メッセージ挿入側の API**
   - 現状: `adapter.append_persona_message(message, thread_suffix=...)`
   - 新方式: `adapter.append_to_thread(thread_kind, message)` のような明示API、もしくは `append_event(message)` `append_internal(message)` のような種別別関数
   - 既存呼び出し箇所（`dynamic_state.py`, `upgrade_handlers.py`, pulse 内部、playbook の memorize ノードなど）の改修範囲

4. **想起・Chronicle 化の対象決定**
   - 現状はタグでフィルタ（例: `summary` タグを Chronicle ソースに、`internal` を想起対象外に）
   - 新方式では「どのスレッドを対象にするか」をプロファイルで定義
   - 認知モデル（Track / Note）やワーキングメモリとの整合

5. **`context_profile` の定義場所**
   - playbook ごとに指定するのか、グローバル設定にするのか
   - `context_requirements` を廃止して `context_profile` に移行する流れ（unified_memory_architecture.md 関連）と統合できるか

6. **既存スレッドとの互換性**
   - building_id を thread に使う現行方式は維持するか、別の整理にするか
   - persona thread (`:__persona__`) の役割を再定義するか

---

## 不変条件（実装時に守るべきこと）

- メッセージ挿入時に「どのスレッドに行くか」が一意に決まる（曖昧な振り分けはしない）
- コンテキスト構築時に「どのスレッドから読むか」が宣言的に定義される（コードに分散させない）
- スレッド構造の変更は不可逆操作 → version_aware の upgrade ハンドラで管理する
- タグは検索・分類用途に限定し、可視性制御に流用しない

---

## 関連ドキュメント

- `dynamic_state_sync.md` — 現行のイベント通知挿入は dynamic_state 経由
- `unified_memory_architecture.md` — Phase 2 の統一記憶探索とこの再設計を整合させる必要あり
- `context_profile_and_subagent.md` — プロファイル定義の仕組みは既にある程度整っている
- `version_aware_world_and_persona.md` — スキーマ移行はこの基盤に乗せる
- `persona_cognitive_model.md` — Track / Note との接続点

---

## 当面のしのぎ

本格的な再設計まで時間が空く間、新しい種類のシステムメッセージを SAIMemory に挿入する場合は **必ず `event_message` タグを含める**ことを徹底する。タグ追加箇所のチェックリスト:

- [ ] `event_message`: ペルソナのコンテキストに常時取り込みたいなら必須
- [ ] `internal`: pulse 内部の思考（`include_internal=True` のときのみ取り込まれる）
- [ ] `conversation`: 通常の会話メッセージ（ペルソナとユーザー間）
- [ ] その他カテゴリタグ（`system_event`, `version_upgrade`, `task` など）: 検索・分類用

挿入箇所: `dynamic_state.py`, `upgrade_handlers.py`, `runtime.py` の memorize 処理、各 playbook の memorize ノード。
