# Meta-Judgment（メタ判断）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §3](../overview/landscape.md)、**設計意図**は intent [`persona_cognitive_model.md`](../intent/persona_cognitive_model.md) を参照。

## 一言で

「どの [Track](track.md) を動かすか」を判断する上位視点（通称「メタレイヤー」）。

## 役割

複数の Track が並存する中で、次の [Pulse](pulse.md) がどの Track に対して思考するかを決める。「現 Track を続けるか / 別の関心事に切り替えるか / 新しい作業を始めるか」というペルソナの意思決定の中枢。

## 仕組み

MetaLayer が状況を判定して**状況別 Playbook**を選び（下表）、その Playbook 内の LLM ノードが**構造化出力（`response_schema`）で判断を返し、続く `tool` ノードがその判断（Track 操作）を機械的に適用する**（`llm` → `tool` の2段）。判断の形は状況ごとに違う（例: running なら `continue / pause / complete / abort` の action enum、idle+pending なら `activate`（保留 Track を選ぶ）/ `create`（新規作成）の decision）。

- **判断材料は [Session](session.md)（短期記憶）から得る** — 今見ているコンテキスト（長期記憶の末尾・[head](session.md)・進行中の [Beat](beat.md)・外界入力）を根拠に判断する
- 判断ログは `meta_judgment_log` に蓄積され、次の判断時に参考情報として注入される（過去の判断との一貫性）

### 状況別 Playbook（MetaLayer が決定論的に選ぶ）

メタ判断は状況ごとに Playbook が分かれ、**どれを走らせるかは MetaLayer が Track / persona 状態から決定論的に選ぶ**（`saiverse/meta_layer.py` の `_SITUATION_PLAYBOOK_MAP`）。**LLM ルーターは存在しない**（廃止された）:

| 状況キー | Playbook | 条件 |
|---|---|---|
| `alert_present` | `meta_judgment_alert` | alert（Track 状態の固有用語）発火。外部イベント即応のため `life_purpose_unset` より優先（2026-07-07 改訂） |
| `life_purpose_unset` | `meta_judgment_life_purpose` | LIFE_PURPOSE 未設定（alert が無い場合に最優先） |
| `running_active` | `meta_judgment_running` | 実行中 Track がある |
| `idle_with_pending` | `meta_judgment_idle_pending` | アイドル・保留 Track あり |
| `idle_no_pending` | `meta_judgment_idle_empty` | アイドル・保留なし |

表は判定の優先順（`_classify_situation`）。

> ⚠️ base の `meta_judgment.json` は `_SITUATION_PLAYBOOK_MAP` に**含まれない**別系統で、構造化出力を使わず内的独白 + `/spell` で完結する（そのファイル自身の設計メモは「重量級モデルのメインキャッシュに JSON を混ぜない」ためと述べる）。実際に dispatch されるのは上表の5つ。

> **設計の要点**: 状況判定はコード側（MetaLayer）の責務で、LLM は各 Playbook 内で「その状況での判断」だけを担う。判断材料の `situation_text`（Track 状態に応じた提示 + Track 一覧の整形済みテキスト）も MetaLayer が組み立てて渡す。

## 実装

- 状況→Playbook 選択: `saiverse/meta_layer.py`（`_SITUATION_PLAYBOOK_MAP`。Track/persona 状態から決定論的に選ぶ・LLM ルーターなし）
- Playbook: `builtin_data/playbooks/public/meta_judgment*.json`
- ランタイム: `sea/runtime.py` / `sea/runtime_llm.py`（Playbook 実行）
- 起動: `PulseController.submit_meta_judgment`（`sea/pulse_controller.py`）
- ログ: `meta_judgment_log` テーブル

## 関連概念

- [Track](track.md) — 選択の対象
- [Pulse](pulse.md) — メタ判断も1つの Pulse として走る
- [Session](session.md) — 判断材料の供給源
- [Playbook](playbook.md) — メタ判断の実装形式

## 参照

- intent: [`persona_cognitive_model.md`](../intent/persona_cognitive_model.md) / [`persona_cognition/`](../intent/persona_cognition/)
- 地図: [`landscape.md`](../overview/landscape.md) §3
