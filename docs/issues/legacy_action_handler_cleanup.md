# Issue: ActionHandler / 旧会話フローの最終残骸を撤去する

**ステータス**: 🔲 再検証済み・最終撤去は未着手 (2026-07-10)
**優先度**: low（挙動影響なし・完全 dead code の整理）
**作成日**: 2026-05-09
**再検証日**: 2026-07-10
**関連**: `saiverse/action_handler.py`, `persona/core.py`, `persona/bootstrap.py`,
`manager/persona.py`, `manager/blueprints.py`,
[`building_auto_interval_setting_removal.md`](building_auto_interval_setting_removal.md)

## 結論（2026-07-10 再検証）

起票時に想定していた大部分は、2026-06-06 の commit `f915bf2`
（`refactor: pre-SEA 旧 LLM パスを撤去 (-1260 行)`）で既に撤去済み。

当時「唯一生きているため要設計」としていた inter-city ThinkingRequest も、現在は
`manager/background.py` から `persona.llm_client.generate(messages, tools=[])` を直接呼ぶ。
`ActionHandler`、旧 `_generate()`、callback 群を通らない。

したがって、残作業に inter-city 再設計や Building schema migration は不要。
**現在残っているのは完全 dead code だけ**であり、最終撤去は独立した低リスク cleanup として
実施できる。

## 既に完了している範囲（commit `f915bf2`）

### 旧 Persona 生成・会話入口

以下は現コードに存在しない:

- `PersonaCore._generate`
- `PersonaCore._build_messages`
- `PersonaCore._generate_stream`
- `PersonaCore.handle_user_input`
- `PersonaCore.handle_user_input_stream`
- `_process_generation_result`

Manager側の `handle_user_input` / `handle_user_input_stream` は同名の別実装で、現在も SEA Runtime
入口として現役。削除対象ではない。

### 旧移動・自律会話経路

以下も撤去済み:

- `run_auto_conversation`
- `run_scheduled_prompt` / `run_scheduled_prompts`
- `summon_to_user_room`
- `_handle_movement`
- `_handle_exploration`
- `_handle_creation`

`persona/mixins/movement.py` は、旧経路撤去済みであることを明記した空の
`PersonaMovementMixin` だけになっている。

### ThinkingRequest の ActionHandler 依存

起票時は `manager/background.py` の `persona._generate(...)` が唯一の生存経路だった。
現在は次の直呼びへ置換済み:

```python
response_text = persona.llm_client.generate(messages, tools=[])
```

この経路自体を将来 SEA Runtime 化するかは inter-city の別設計課題だが、
`ActionHandler` を残す理由にはならない。

## ActionHandler cleanup として確認済みの dead code

### 1. `ActionHandler` 本体

`saiverse/action_handler.py` の `ActionHandler` クラス。

- `::act ... ::end` JSON ブロックの抽出
- action priority 順の並べ替え
- `think` / `emotion_shift` / `move` の取り出し

リポジトリ内に import・インスタンス化・メソッド呼び出しは無い。ファイルごと削除可能。

### 2. action priority 設定一式

- `builtin_data/action_priority.json`
- `persona/bootstrap.py::load_action_priority`
- `PersonaCore.__init__(action_priority_path=...)`
- `PersonaCore.self.action_priority`
- PersonaCore 構築3箇所から渡す `action_priority_path`

`self.action_priority` は代入後にどこからも読まれない。`ActionHandler` 削除と同時に一式削除可能。

### 3. PersonaCore の未参照 callback 4本

Constructor 引数と同名属性:

- `move_callback`
- `dispatch_callback`
- `explore_callback`
- `create_persona_callback`

現在の参照は次の2種類だけ:

1. `manager/persona.py` / `manager/blueprints.py` から PersonaCore 構築時に渡す
2. `persona/core.py` で `self.*` に保存する

保存後に読むコードは0件。引数・属性・構築側の注入を同時に削除できる。

callback 4本の削除と、接続先になっていた Manager メソッド本体の削除は分けて判断する。

- `_move_persona` と `_create_persona` は現行の別経路から呼ばれているため残す
- `dispatch_persona` は直接の呼び出し元を発見できなかったが、`VisitingAI` の状態監視など
  inter-city dispatch の周辺機能は残っている。本 issue の callback 撤去だけを根拠に削除しない
- `_explore_city` / `RuntimeService.explore_city` は、下記のとおり追加の dead code 候補

### 4. 追加発見: 旧 city exploration 経路（要最終確認）

静的検索では、探索経路の参照は次の3種類だけだった。

1. PersonaCore の未参照 `explore_callback` への注入
2. `SAIVerseManager._explore_city` から `RuntimeService.explore_city` への wrapper
3. `AdminService.__init__` の `self._explore_city = runtime.explore_city` alias

`_explore_city(...)` / `explore_city(...)` の実呼び出しは見つからないため、旧 ActionHandler の
`explore_city` action とともに入口を失った残骸である可能性が高い。ただし Manager 公開面は
外部・動的参照の余地があるため、最終撤去時に API / addon / expansion の消費者が無いことを
もう一度確認してから、次を同一 cleanup に含めるか決める。

- `saiverse/saiverse_manager.py::_explore_city`
- `manager/runtime.py::RuntimeService.explore_city`
- `manager/admin.py` の `_explore_city` alias

## PersonaCore 構築箇所（全3箇所）

リポジトリ本体とローカル `expansion_data` を検索し、直接構築は次の3箇所だけと確認:

- `manager/persona.py` — 起動時ロード
- `manager/persona.py` — 新規ペルソナ作成
- `manager/blueprints.py` — Blueprint 経由作成

この3箇所を Constructor signature と同じ commit で更新すれば、呼び出し側の取りこぼしはない。

## 最終撤去の変更ファイル

### 削除

- `saiverse/action_handler.py`
- `builtin_data/action_priority.json`

### 編集

- `persona/bootstrap.py`
  - `load_action_priority` を削除
  - それ専用になった `Path` / `Dict` import を削除
- `persona/core.py`
  - `load_action_priority` import、`action_priority_path` 引数、代入を削除
  - callback 4引数と属性代入を削除
  - 不要になった `Tuple` import（および実使用の無い import）を整理
- `manager/persona.py`
  - PersonaCore 構築2箇所から action priority と callback 5引数を削除
- `manager/blueprints.py`
  - PersonaCore 構築1箇所から同じ引数を削除

上記「旧 city exploration 経路」は要最終確認の候補であり、この確定変更一覧にはまだ含めない。

## 明示的にスコープ外

### Building の旧自律設定 / ConversationManager

以下は ActionHandler とは独立して残っている:

- `Building.ENTRY_PROMPT` / `AUTO_PROMPT` / `AUTO_INTERVAL_SEC`
- `saiverse/buildings.py` の対応属性
- Building設定UI / WorldEditor
- no-op `ConversationManager` の生成・start/stop wrapper
- `global_auto_enabled` の旧UI/状態

これらは [`building_auto_interval_setting_removal.md`](building_auto_interval_setting_removal.md)
と ConversationManager cleanup で扱う。本 issue に混ぜない。

### inter-city ThinkingRequest の SEA Runtime 化

現在の LLM 直呼びは ActionHandler 非依存。SEA Runtime 化の是非は multi-city 復活時の別課題。
本 cleanup のブロッカーではない。

`dispatch_persona` 自体も静的な直接呼び出し元は見つからないが、dispatch 状態監視・訪問者処理を
含む inter-city 機能全体の生死を確認してから扱う。本 issue では callback 注入だけを外す。

### Emotion / Blueprint 概念そのもの

ActionHandler は `emotion_shift` / `create_persona` action を扱っていたが、クラスが未使用なので
撤去しても現行 Emotion / Blueprint の挙動は変わらない。両概念自体の整理は landscape §9 の
別 cleanup。

## リスク評価

**低**。実行経路の変更ではなく、未参照の定義・引数・代入だけを削る。

注意点は PersonaCore Constructor の引数を5本削るため、3つの構築箇所を同時に直すこと。
特に、現役の `_move_persona` / `_create_persona` 本体まで誤って消さないこと。
旧 city exploration 経路も同時に消す場合は、外部・動的消費者の確認を追加する。

## 検証

最低限:

1. `rg "ActionHandler|action_handler|action_priority|move_callback|dispatch_callback|explore_callback|create_persona_callback"`
   で意図しない残存がない（歴史docを除く）
2. exploration も撤去する場合は `rg "_explore_city|explore_city"` と API / addon / expansion の
   公開・動的参照を再確認
3. `ruff check` を変更Pythonファイルに実行
4. `python -m pytest tests/test_persona_mixins.py`
5. PersonaCore 構築を通る既存の persona/blueprint 系テストを実行

安全側の追加確認:

- テスト環境起動 → 既存Personaロード
- 新規Persona作成
- Blueprint経由作成（Blueprintを残している間）
- ThinkingRequest の既存テストがあれば実行（ActionHandler非依存の確認）

## ログ

- 2026-05-09: issue 起票。旧 pre-SEA 経路を段階削除する計画を記録。
- 2026-06-06: `f915bf2` で旧生成・移動・自律会話経路1260行を撤去。
  ThinkingRequest は LLM 直呼びへ変更。issue 本文は未追従のまま残った。
- 2026-07-10: 現コードを再検証。`ActionHandler` import/instance 0件、callback は注入と代入のみ、
  action priority は未参照属性のみと確認。最終撤去を独立low-risk cleanupとして再スコープ。
  あわせて `_explore_city` / `RuntimeService.explore_city` に静的な実呼び出しが無いことを確認し、
  外部・動的参照の最終確認が必要な追加候補として記録。`dispatch_persona` は inter-city 全体の
  監査なしには削除しない。
