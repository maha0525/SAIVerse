# line / aspect（処理レーンとその導出）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §3](../overview/landscape.md)、**設計意図**は intent [`cache_lifecycle_control.md`](../intent/cache_lifecycle_control.md) / [`persona_action_tracks.md`](../intent/persona_action_tracks.md) を参照。

## 一言で

[Track](track.md) 内の処理を規定する軸が **line**、呼び出し時に1つ指定して line の各軸をまとめて導出する値が **aspect**。

## 役割

同じペルソナの思考でも「重量級モデルで Track 横断のメインキャッシュに載せる会話」と「軽量モデルでこのターン限りの下調べ」では、モデル・キャッシュ寿命・記録の残り方が違う。line/aspect は、この違いを毎回の呼び出しで宣言し、**プロンプトキャッシュの効き方を制御する**ための仕組み。

## 仕組み

### line の3軸

Track 内の処理は複数の **line** に分かれ、3つの独立した軸で規定される:

- **モデル/キャッシュ**: メイン（重量級モデル + Track 横断の単一メインキャッシュ）/ サブ（軽量モデル + Track ごとのサブキャッシュ）
- **呼び出し関係**: 親ライン（Pulse スケジューラが直接起動）/ 子ライン（親から分岐、完了で `report_to_parent` を返す）
- **Pulse 階層内位置**: 起点ライン / 入れ子ライン

### aspect（4分類）

呼び出し時に1つ指定する値で、`(line_role, scope, model_tier)` をまとめて導出する:

| aspect | line_role | scope | model_tier | 用途 |
|---|---|---|---|---|
| **CONVERSATION** | main_line | committed | standard | 対ユーザー会話（メインキャッシュに残る） |
| **WORKER** | sub_line | volatile | lightweight | サブラインの下調べ（このターン限り） |
| **AUTONOMOUS** | main_line | committed | lightweight | 自律稼働（メインに残る・軽量） |
| **META** | meta_judgment | discardable | standard | メタ判断（使い捨て・重量級） |

各呼び出しで aspect を指定すると、そのメッセージがメインキャッシュに残るか・このターン限りかが自動的に決まる。**v0.2 実装済・実機検証待ち**。

## 実装

- aspect 定義と導出マップ: `sea/pulse_context.py`（`Aspect` enum → `(line_role, scope, model_tier)`）
- line 階層の追跡: `sea/pulse_context.py`（`PulseContext._line_stack`。`run_playbook` の入れ子は最大4段、5段目は拒否）
- キャッシュ制御との接続: `sea/head_pipeline/`（[head](session.md) / [Metabolism](metabolism.md)）

## 関連概念

- [Track](track.md) — line はこの中で分岐する
- [Pulse](pulse.md) — line は Pulse 階層を表現する
- [Session](session.md) / [Metabolism](metabolism.md) — scope はキャッシュ寿命に効く
- [Spell](spell.md) — `run_playbook` Spell がサブライン（WORKER）を起動する

## 参照

- intent: [`cache_lifecycle_control.md`](../intent/cache_lifecycle_control.md)
- 地図: [`landscape.md`](../overview/landscape.md) §3
