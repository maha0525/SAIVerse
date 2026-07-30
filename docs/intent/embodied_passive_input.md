# Intent: 身体性の受動入力レイヤー (Embodied Passive Input)

**ステータス**: 歴史文書 (2026-07-27 まはー裁定)。本書の構想は Fixture 型知覚 ([physical_ear.md](physical_ear.md) 等) と知覚バッファ ([perception_buffer.md](perception_buffer.md)) に昇華済みで、この文書単体で実装されることはない。全体の位置づけは [realtime_foundation.md](realtime_foundation.md) を参照。(初版: v0.1 ドラフト 2026-05-21)

## これは何か

ペルソナの身体（Vessel: スタックチャン / 将来は Live2D / 3D モデル等）が持つセンサー入力を、**スペルを介さずに**ペルソナの認知へ届けるための機構。

現状、温湿度・照度のような環境センサーを「感じ取る」ためには毎回スペル経由で `get_environment_state()` 的なツールを呼ぶ必要がある。これは:

1. **認知モデルとの違和感**: Vessel Building = 身体 のメタファー (`stackchan_vessel.md`) の上で、自分の身体に張り付いてる温度計の値をいちいち「読みに行く」のは「自分の体温を測るために毎回体温計を意識的に取り出して計測する」のと等価で、自然な身体感覚と乖離する。
2. **LLM コールの無駄**: 単純な数値参照のためにスペル発火 → router → LLM 1コール、を消費するのは非効率。
3. **継続的条件発火が組めない**: 「気温40℃超えたら警戒」のような閾値ベースの自動反応が組めない (毎ターン スペルを呼ばないと取れないため)。

この doc は **(A) 環境センサー値の常時注入** と **(B) センサー条件発火** の2機構を扱う。

## 設計の骨子

### (A) 常時注入: `_build_realtime_context` の拡張

`sea/runtime.py:2380` にある `_build_realtime_context` は、既に「キャッシュを壊さない位置に動的情報を載せる」枠組みとして確立されている:

- 末尾近く (current prompt の手前) に挿入
- role="user" + `<system>...</system>` ラッピング (Gemini 互換)
- `metadata: {"__realtime_context__": True}` でマーク
- キャッシュを壊さない位置に置く設計が明示済み (関数 docstring 参照)

現在の section:
1. 現在時刻 (persona.timezone)
2. 前回 AI 発言からの経過
3. 空間情報 (unity_gateway 接続時)

ここに **環境センサー section** を追加する:

```
## リアルタイム情報
- 現在時刻: 2026年05月21日(木) 14:23
- 身体感覚: 室温 22.3°C / 湿度 48% / 明るさ 普通
- (将来) 振動 / 傾き / 接触 等
```

#### 値の取得経路

`unity_gateway` の `spatial_state` と同じパターンで、各 Vessel 種別ごとの gateway が `sensor_state` を保持する:

- **stackchan**: `stackchan-mcp` MCP server が定期的に温湿度・照度を読み、SAIVerse 側で最新値をキャッシュ
  - stackchan-mcp 側に sensor 読み出し endpoint があるか、なければ環境センサー専用の polling tool を SAIVerse 側で叩いてキャッシュする (実装時要調査)
- **将来の Live2D / 3D vessel**: 仮想センサー (時刻ベースで「眠い」「明るい部屋にいる」等を生成) もこの経路に乗せる

`_build_realtime_context` は **最新値を読むだけ** で、ポーリング頻度は gateway 側で吸収する。

#### Vessel 非依存にする抽象

センサー値は内部的に dict として保持し、フォーマットは section 描画関数が担当する:

```python
sensor_state = {
    "temperature_c": 22.3,
    "humidity_pct": 48,
    "illuminance_level": "normal",  # 段階値 (dark/dim/normal/bright)
    # ... 将来追加
}
```

LLM に渡す表現は「室温 22.3°C」のように人間語に変換するが、**ペルソナの口調は持たせない** (`feedback_tool_return_text_neutral`)。

### (B) 条件発火: 既存 PhenomenonManager に乗せる

`external_event_integration.md` で確立済みの経路をそのまま再利用する:

```
GatewayPoller (stackchan_gateway 内)
  ↓ 閾値判定 (例: temp > 40℃)
PhenomenonManager.emit(TriggerEvent(type="vessel_sensor_threshold", data={
    "vessel_id": ...,
    "sensor": "temperature_c",
    "value": 42.1,
    "threshold": 40.0,
    "direction": "above"
}))
  ↓ PhenomenonRule で条件マッチ
ペルソナへ通知 + メール送信ツール起動 等
```

#### 閾値設定の保存先

`PhenomenonRule` の `CONDITION_JSON` で表現できる。例えば:

```json
{
  "trigger_type": "vessel_sensor_threshold",
  "condition": {
    "sensor": "temperature_c",
    "value": {"$gt": 40.0}
  },
  "phenomenon_name": "high_temperature_alert",
  "argument_mapping": {
    "current_temp": "$trigger.value",
    "vessel_id": "$trigger.vessel_id"
  }
}
```

#### 閾値判定をどこでやるか

2案あり、実装時に決める:

- **案1 (gateway 側で判定)**: gateway poller が値を読みつつ閾値比較し、跨いだ瞬間だけ emit。利点: SAIVerse 側に値の連続流入を流さない。欠点: 閾値設定が gateway 側に複製される or 起動時に DB から配布する必要。
- **案2 (PhenomenonManager 側で判定)**: gateway は値変化を常に emit (`vessel_sensor_changed`)、PhenomenonRule の condition で閾値比較。利点: 設定が DB 一元化。欠点: emit 頻度が高いと負荷増。

**初期判断**: 案2 寄り。閾値設定の一元化メリットが大きく、頻度は gateway 側で「値が一定変化した時だけ emit」(hysteresis) で抑える。

### (C) スコープ外: まばたき等の常時 ON ローカル動作

まばたき・微小頭部揺動のような「LLM に見せる必要がない常時 ON 動作」は **gateway 内のローカルループ** で完結させる:

- stackchan-mcp の board 設定 or addon 側で「アイドル時まばたき」を常時実行
- SAIVerse 本体の認知に上げない (LLM コール無し)
- ペルソナがツールで「まばたきを止める」等の上書きはできる (= idle 動作の suppress 機構)

これは本 doc の機構には載せず、stackchan_vessel.md 側で扱う。

## 不変条件

1. **`_build_realtime_context` はキャッシュを壊さない位置を維持する**: 環境センサー section を追加しても、システムプロンプト / persona info / building info の位置を不変に保つ。注入位置を動かす場合は別 intent doc で再設計が必要。
2. **常時注入の section は短く保つ**: 1行/sensor を上限とし、複雑な分析テキストを入れない。読み手 (LLM) が「現在の体感」を一瞥するための情報密度に留める。
3. **gateway 経由でない sensor 値を `_build_realtime_context` に混ぜない**: persona の任意属性を head に書き込み始めると、何が動的注入で何が永続情報か区別がつかなくなる。Vessel sensor は必ず `vessel_id` を介して gateway から取る経路を通す。
4. **閾値発火は Phenomenon 経路を通す**: gateway が直接 ペルソナへメッセージを投げる経路を作らない。`external_event_integration.md` の不変条件 (条件 → アクション の一元管理) を継承する。
5. **物理 vessel 非依存の抽象を維持する**: sensor_state の dict 構造は vessel 種別に依存しない (= temperature_c は stackchan も 3D vessel も同じキー名)。3D vessel が仮想センサーで埋める場合も同じ構造で渡す。

## 段階実装プラン

- **Phase A (環境センサー基本注入)**: stackchan-mcp の温湿度を `_build_realtime_context` に流す経路を1本通す。`sensor_state` dict の構造を確定。Vessel building に居る時のみ section を描画する。
- **Phase B (照度・追加センサー)**: 段階値 (dark/dim/normal/bright) と追加 sensor key を増やす。
- **Phase C (条件発火)**: `vessel_sensor_threshold` TriggerType を `PhenomenonManager` に追加。`PhenomenonRule` の UI / 設定経路を整備。
- **Phase D (仮想 Vessel への横展開)**: Live2D / 3D vessel の仮想 sensor (時刻ベースの眠気・部屋の明るさ等) を同じ経路に乗せる。

## 関連 doc

- `docs/intent/stackchan_vessel.md` — Vessel Building = 身体 のメタファー定義。本 doc はその上の「身体感覚」の実装。
- `docs/intent/external_event_integration.md` — PhenomenonManager 経由の外部イベント処理。本 doc の条件発火はこの経路を継承。
- `docs/intent/embodied_expression.md` — ペアになる出力側 (ジェスチャー / 表情) の機構。
- `docs/intent/cached_head_architecture.md` — head pipeline 設計。`_build_realtime_context` がキャッシュ位置の外にあることの確認に使う。

## オープン課題

- stackchan-mcp に温湿度 sensor 読み出し endpoint が既にあるか、addon 側で別途実装が必要か → 実装着手時に kisaragi-mochi/stackchan-mcp を調査
- 仮想 vessel (Live2D / 3D) の sensor 値生成ロジックをどこに置くか (vessel-specific gateway? 共通の virtual_sensor_provider?) → Phase D 着手時に決める
- 閾値判定 案1 vs 案2 の最終確定 → Phase C 着手時にベンチを取る
