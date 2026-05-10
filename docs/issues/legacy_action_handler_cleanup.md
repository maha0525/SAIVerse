# Issue: ActionHandler / 旧会話フロー周辺の旧仕様コード一括整理

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-09
**関連**: `saiverse/action_handler.py`, `persona/mixins/generation.py`, `persona/mixins/movement.py`, `manager/background.py`, [`building_auto_interval_setting_removal.md`](building_auto_interval_setting_removal.md)

## 背景

ユーザー会話フローが SEA Runtime に移行した結果、`::act ... ::end` ブロックを解釈する `ActionHandler` ベースの旧フロー周辺が大量の dead/no-op コードとして残存している。

memory の `Architecture Notes` には長らく「`action_handler.py` is legacy — SEA runtime replaces conversation flow」と書かれていたが、実調査の結果:

- **完全 dead code が 4 メソッド以上ある** (どこからも呼ばれていない)
- **実質 dead な経路がさらに広い** (経路はあるが Building 設定が空のため即 return)
- **唯一生きているのは inter-city ThinkingRequest 経路だけ** (`manager/background.py:59` の `persona._generate(...)`)
- DB スキーマからすでに削除済みのカラムに対応する Python 属性が残骸として残っている (`run_auto_llm` / `run_entry_llm`)

inter-city 経路は SEA Runtime ベースに作り直すか、当面残すかの設計判断が要るので、整理は段階的に進めるのが安全。

## 完全 dead code (即削除候補)

呼び出し元なしを確認済み:

- `PersonaCore.handle_user_input` (`persona/mixins/generation.py:694`)
- `PersonaCore.handle_user_input_stream` (`persona/mixins/generation.py:717`)
- `PersonaCore._generate_stream` (`persona/mixins/generation.py:472`) — `handle_user_input_stream` からのみ
- `PersonaCore.summon_to_user_room` (`persona/mixins/movement.py:186`)

> 注: `manager.handle_user_input` / `manager.handle_user_input_stream` は **Manager 側の同名別実装** (`manager/runtime.py:370, 453`) で、`run_sea_user` を呼んで SEA Runtime に流している。PersonaCore の同名メソッドには到達しない。

## 実質 dead (経路はあるが no-op)

ガード条件で空返却される:

- `run_auto_conversation` (`movement.py:135`) — `building.entry_prompt` / `building.auto_prompt` が空なら全分岐スキップ
  - 呼び出し元: `manager/runtime.py:327` `summon_persona` 経由 (UI から呼ばれる API ルート `api/routes/people/summon.py:104` あり、ただし召喚先 building の prompt が空なので無動作)
- `run_scheduled_prompt` (`movement.py:169`) — `auto_prompt` が空または `auto_interval_sec <= 0` なら return []
  - 呼び出し元: `_db_polling_tick` (`manager/background.py:22`) → `run_scheduled_prompts` (`manager/runtime.py:663`)

裏付け:
- `Building.AUTO_PROMPT` / `Building.ENTRY_PROMPT` は DB スキーマに残るが `default=""`
- `builtin_data/` 配下に `auto_prompt` / `entry_prompt` を seed する記述なし
- `frontend/src/components/BuildingSettingsModal.tsx` でこれらフィールドの編集 UI なし
- `RUN_AUTO_LLM` / `RUN_ENTRY_LLM` カラムは既に DB スキーマから削除済み (`saiverse/buildings.py:14-15` の Python デフォルト True で動的に True 扱いされているだけ)

連鎖して以下も実質 dead:

- `_generate` (`generation.py:347`) — 上記から呼ばれる + 後述の inter-city 経路のみ
- `_process_generation_result` (`generation.py:263`)
- `_handle_movement` / `_handle_exploration` / `_handle_creation` (`movement.py:26, 91, 106`)
- `ActionHandler` クラス全体 (`saiverse/action_handler.py`)
- callback 群: `move_callback`, `dispatch_callback`, `explore_callback`, `create_persona_callback`
  - 初期化箇所: `manager/persona.py:199-202, 442-445`, `manager/blueprints.py:275-278`
  - PersonaCore 受け口: `persona/core.py:63-66, 157-160`

## 唯一生きている経路

`manager/background.py:59` の `persona._generate(...)` — リモート都市からの ThinkingRequest 処理 (`/persona-proxy/{id}/think` 経由で DB に積まれた要求を `_db_polling_tick` で消化)。

ここだけが ActionHandler を実際に通す。マルチ City 運用していなければ発火しない。

## 解決案候補

### 案 A: 完全 dead code のみ削除 (低リスク・最小着手)

呼び出し元なしを確認済みの 4 メソッドだけ削除:
- `PersonaCore.handle_user_input` / `handle_user_input_stream` / `_generate_stream` / `summon_to_user_room`

これだけでも generation.py / movement.py から数百行落ちる。inter-city 経路と Building.entry_prompt 経路は触らないので影響なし。

### 案 B: 案 A + 実質 dead の整理

ガード条件で no-op になっている経路も削除:
- `run_auto_conversation` / `run_scheduled_prompt` 廃止
- `manager/runtime.py:327` の `persona.run_auto_conversation(initial=True)` 削除 (summon_persona 内)
- `manager/background.py:22` の `self.run_scheduled_prompts()` 呼び出し削除
- `manager/runtime.py:663-672` の `run_scheduled_prompts` 削除
- `saiverse/saiverse_manager.py:1272-1280` の同名 wrapper 削除

DB カラム / Python 属性の残骸も整理:
- migration で `Building.AUTO_PROMPT` / `Building.ENTRY_PROMPT` / `Building.AUTO_INTERVAL_SEC` 削除 ([`building_auto_interval_setting_removal.md`](building_auto_interval_setting_removal.md) と統合)
- `saiverse/buildings.py` から `run_auto_llm` / `run_entry_llm` / `auto_interval_sec` / `entry_prompt` / `auto_prompt` パラメータ削除
- `tests/test_buildings.py` / `tests/test_persona_mixins.py` の該当部分整理

inter-city ThinkingRequest 経路は当面残す (案 C で別途対応)。

### 案 C: 案 B + ActionHandler 自体の廃止 (要設計)

`manager/background.py:59` の `persona._generate(...)` を SEA Runtime ベースに置き換え:
- リモート都市から来た ThinkingRequest を SEA Runtime の入口に流す形に再設計
- 完了後 `_generate` / `_process_generation_result` / `_handle_*` / `ActionHandler` / callback 群を全廃
- `persona/core.py` の callback 引数も削除

inter-city 関連の整備全体 (memory にあるとおり「だいぶ整備していない」状態) と一緒にやるのが筋。`v0.4` 以降向け。

### 推奨進行

1. **まず案 A** — リスクなしで明らかな dead code を削る
2. **次に案 B** — `building_auto_interval_setting_removal` と統合実施 (Building 設定 UI / migration / Python オブジェクト整合を一括で)
3. **案 C は別 issue 化** — inter-city 整備全体の中で取り扱う

## 関連リソース

- `persona/mixins/generation.py` (action_handler を呼ぶ層)
- `persona/mixins/movement.py` (`run_auto_conversation` / `run_scheduled_prompt` / `_handle_*`)
- `saiverse/action_handler.py` (本体)
- `manager/background.py` (`_db_polling_tick`, `_process_thinking_requests`)
- `manager/runtime.py` (`summon_persona`, `run_scheduled_prompts`, Manager 側 `handle_user_input_stream`)
- `saiverse/buildings.py` (Python 属性の残骸)
- `database/models.py` (Building テーブル: AUTO_PROMPT/ENTRY_PROMPT/AUTO_INTERVAL_SEC は残骸)
- `frontend/src/components/BuildingSettingsModal.tsx` (これらフィールドの UI なし)
- 関連 issue: [`building_auto_interval_setting_removal.md`](building_auto_interval_setting_removal.md)

## ログ

- 2026-05-09: issue 起票。memory の「action_handler is legacy」記述が実態と乖離していた件をきっかけに、コールグラフを遡って旧仕様コード群の全体像を整理。
