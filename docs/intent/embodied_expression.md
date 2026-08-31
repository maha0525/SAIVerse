# Intent: 身体性の表現出力レイヤー (Embodied Expression)

**ステータス**: v0.3 ドラフト (2026-07-17) — v0.2の発声同期ツール実行に、長時間Body behaviourとの責務境界を追記。 `docs/intent/screen_avatar.md` / `docs/intent/virtual_embodiment_godot.md` と連携。

## これは何か

ペルソナの「表情変更 / サーボジェスチャー / LED 発光」等の**修飾的な身体表現**を、本文中インラインマーカーで**発話 (TTS) と同期して発火**する機構。stackchan に限らず Live2D / 3D モデル / 画面内アバター等に横展開できる vessel 非依存の抽象を持つ。

**駆動の実体 = 発声同期ツール実行** (v0.2 一般化, 2026-07-02): マーカーは「emote 実行可能」として絞られたツール (= 修飾的な意味合いで、発声に同期して実行したい短時間の行動あるいは状態変化) を、TTS 再生のその文字位置で実行する。プリセット (後述 A) はその上に乗る「複数 vessel action を束ねた高レベルエモート表現」であり、preset 適用も emote 実行の一形態。対象ツールの絞りは **spell が spell_tools で実行可能ツールを絞るのと同型**に、emote 実行可能ツールを登録で限定する (emote ≒ 発声同期版 spell)。

### Body behaviourとの境界（v0.3）

`/emote`は「発話のこの位置で短い身体表現を添える」修飾層であり、目的地・継続状態・中断・非同期完了を持つ行動は扱わない。`ユーザーの前まで行く`、`嬉しそうに駆け寄る`のような行動はBody SpellからEmbodiment Gatewayへ送り、`command_id`付きbehaviourとして `accepted → generating? → ready? → playing → completed/failed/cancelled` を追跡する。

両者は同じvessel executorとモーション資産を再利用できるが、behaviourはemoteの上位互換ではない。behaviourには目標と中断可能性があり、emoteには発話時刻への同期という固有の意味がある。詳細契約とGodot/ARDY実装は [`virtual_embodiment_godot.md`](virtual_embodiment_godot.md) を正典とする。

## 動機

### スペル方式の限界

現状のジェスチャー発動は「ペルソナがツール (スペル) を呼び、サーボ角度や LED 色を引数指定して動かす」モデル:

1. **発話との同期が困難**: 「うれしい！って言いながら手を上げる」をやろうとすると、speak スペルとジェスチャースペルを順次呼ぶ必要があり、TTS 再生中のどのタイミングで動きを発火するか制御できない。
2. **LLM コール / プロンプト消費が重い**: 単純な「喜び」の表現でもサーボ角度を毎回 LLM に決めさせる必要があり、router → 引数生成 LLM コール → 実行、と段数が深い。
3. **再利用性が低い**: 「happy-dance」のような複合動作 (表情 + 頭の動き + LED + 持続) を毎回ゼロから組み立てる必要がある。
4. **身体非依存にならない**: 「サーボ角度 30°」は stackchan 固有の表現で、Live2D / 3D vessel に同じ意図を持っていけない。

### インライン + プリセット方式の利点

本文中に `/emote happy-dance` のようなマーカーを書くだけで、TTS のその位置で同期的にプリセットが起動する:

```
こんにちは！ /emote wave_happy 今日も会えてうれしい〜
```

- **発話と完全同期**: TTS 再生のその文字位置で発火 → 「言葉と動きが揃う」自然な表現
- **LLM コストゼロ**: マーカー1個を本文に書くだけ。引数生成 LLM コール不要
- **再利用**: プリセット (JSON) を一度定義すれば何度でも使える
- **身体非依存**: プリセット名 `happy-dance` は意図、各 vessel のディスパッチ層が「stackchan ならサーボ + LED」「Live2D なら表情モーション」「3D ならアニメーションクリップ」に変換

## 設計の骨子

### (0) emote 実行可能ツールの絞り (v0.2 追加)

インライン発声同期で実行できるツールは無制限ではなく、**spell の spell_tools と同型に「emote 実行可能」を登録で絞る**。

- **選定基準**: 「修飾的な意味合いで、発声に同期して実行したい短時間の行動あるいは状態変化」。表情変更・LED・短いサーボジェスチャー・画面アバターの expression 等が該当。長時間処理・世界状態を変える重い操作・引数生成に LLM 推論を要する操作は対象外。
- **絞りの軸は引数の有無ではない**: 「表情を変える」ツールは表情名が引数。軸はあくまで上記の意味論的カテゴリ + 明示登録。
- **preset との関係**: preset 適用 (複合エモート) も emote 実行の一形態。単発の表情変更ツールを直接同期実行することも、preset で複数 action を束ねて 1 マーカーで撃つこともできる。
- **spell との違い**: spell はラウンド内で逐次実行 (`project_spell_loop_sequential`)、emote は TTS 再生タイミングで subscriber 側同期実行。絞り機構は同型だが発火タイミングが異なる。構文・機構をどこまで共通化するかはオープン課題 (下記)。

### (A) プリセット = JSON ファイル

プリセットは vessel 非依存の意図表現として JSON に書く。配置は priority 3層構造に準拠 (`builtin_data/emotes/` < `expansion_data/<addon>/emotes/` < `~/.saiverse/user_data/emotes/`):

```json
{
  "name": "happy_dance",
  "display_name": "うれしい踊り",
  "duration_ms": 1500,
  "tags": ["positive", "energetic"],
  "actions": {
    "stackchan": {
      "expression": "happy",
      "servo_sequence": [
        {"t_ms": 0,    "pan": 0,   "tilt": -10},
        {"t_ms": 300,  "pan": 15,  "tilt": 0},
        {"t_ms": 600,  "pan": -15, "tilt": 0},
        {"t_ms": 1200, "pan": 0,   "tilt": 0}
      ],
      "led": {"pattern": "pulse", "color": "#ffd34a", "duration_ms": 1500}
    },
    "live2d": {
      "motion_id": "happy_dance_01",
      "expression_id": "happy"
    },
    "fallback_text": "(うれしそうに体を揺らす)"
  }
}
```

#### 設計判断

- **意図名は身体非依存**: `happy_dance` のような意味ベース。サーボ角度を直接プリセット名にしない (`pan_30_tilt_neg10` のような命名は不可)
- **vessel 別の `actions` ブロック**: 各 vessel の表現方法はその vessel のキーに書く。新しい vessel 種別を増やすときは既存プリセットに `actions.<new_vessel>` を追加すれば対応可能
- **`fallback_text`**: その vessel に対応する action が無い場合の代替テキスト (= テキストチャットだけ見てるユーザーに「うれしそうに体を揺らす」と伝える)
- **`duration_ms`**: プリセット全体の総時間。TTS スケジューラが「次のマーカーまでに収まるか」判定に使う
- **`tags`**: 後述の動的プリセット選択 (タグからランダム選択) で使う

### (B) インラインマーカーの構文

```
本文 /emote <preset_name> 本文続き
```

- 構文: `/emote <name>` (空白 or 改行で終端)
- `<name>` は preset の `name` フィールドと一致するスラッグ
- 本文の任意位置に挿入可能。発話の文字位置で発火タイミングを表す

#### 構文選定理由

- 既存の `/slash` コマンド類との命名一貫性
- `<emote name="..." />` のような XML 風タグはトークン消費が多く、また LLM が閉じタグを忘れがち
- `[emote: happy]` 風の括弧記法は本文中の括弧書きと衝突しうる
- スラッシュコマンドは1単語で書け、TTS が誤読する可能性も低い

### (C) パーサ + ディスパッチ層

LLM が出力した本文を TTS にかける前に **emote パーサ** が走る:

```
入力本文: "こんにちは！ /emote wave_happy 今日も会えてうれしい〜"
  ↓ パース
{
  "tts_text": "こんにちは！ 今日も会えてうれしい〜",  // /emote マーカーを除去
  "emote_events": [
    {"char_offset": 7, "preset": "wave_happy"}
  ]
}
```

- `char_offset` は **マーカー除去後の本文** における文字位置
- TTS スケジューラがこの offset の文字位置の再生タイミングで preset を発火

#### 同期方式の選択肢

実装時に確定する課題:

- **案1: TTS engine が word/char timing を返す**: GPT-SoVITS が文字単位 timing を返せるなら、それを基準に preset 発火タイミングを決める (= 最も正確)
- **案2: 文字数 × 合成速度の推定**: TTS engine が timing を返さない場合、本文長 + 合成総時間から線形補間で各 char_offset の再生時刻を推定 (= 経験則だが実装容易)

`voice_tts_playback_queue.md` の教訓「server だけで subscriber 側の再生タイミングを正確に推定するのは原理的に不可能」を踏まえ、**preset 発火は subscriber 側 (= 再生端末) で行う** のが筋。subscriber は再生中の audio element の `currentTime` を見ながら、自分のローカルクロックで preset 発火を制御する:

```
subscriber 側:
  audio_ready 受信時に emote_events リストも受け取る
  audio.play() 開始
  while playing:
    if audio.currentTime >= emote_events[i].fire_time_sec:
      vessel.dispatch_emote(emote_events[i].preset)
      i++
```

#### ディスパッチ層

`vessel.dispatch_emote(preset_name)` の実装は vessel 種別ごとに分岐:

- **stackchan**: preset の `actions.stackchan` を読み、`set_expression` / `servo_move_sequence` / `led_set` MCP tool を順次叩く
- **Live2D**: `actions.live2d.motion_id` で `<live2d>.startMotion()` を呼ぶ
- **画面内アバター (2D PNGTuber / VRM)**: `actions.screen_2d` / `actions.vrm` を読む。2D=表情差分 PNG 切替、VRM=expression preset weight + アニメーションクリップ (`docs/intent/screen_avatar.md`)
- **テキストチャット (vessel 無し)**: `actions.fallback_text` をチャット欄に小さく表示 (= "(うれしそうに体を揺らす)" のようなト書き表示)

### (D) ペルソナによるプリセット追加

ペルソナ自身がプリセットを増やせる経路:

- **保存ツール**: `save_emote(preset_json)` native tool。`~/.saiverse/user_data/emotes/<name>.json` に書き込む
- **書いてその場で使う**: 同じプルス内でプリセット定義 + 本文中で利用も可能 (= ファイル保存後に reload)
- **編集ツール**: 既存プリセットの一部 (例: servo_sequence) を更新する `edit_emote(name, patch)`
- **検索 / 一覧**: ペルソナが現在使えるプリセット一覧を `_build_realtime_context` 経由 or 専用 section で head に注入 (`available_emotes` section)

プリセット作成 UI もペルソナ用に開放 (= 創作行為として楽しい)。バリデーション:

- `name` がスラッグ形式 (英数 + アンダースコア)
- `duration_ms` が常識的範囲 (0 < x < 10000)
- vessel 別 actions のスキーマが各 vessel の dispatch 層と整合

## 不変条件

1. **プリセット名は身体非依存の意味表現を維持する**: `pan_30_tilt_neg10` のような物理パラメータ直書きの名前は許容しない。各 vessel 固有のパラメータは `actions.<vessel>` ブロック内に留める
2. **`/emote` マーカーは TTS テキストに混入しない**: パーサが必ず除去する。fallback として「ト書き表示」する場合はテキストチャット側の描画層で挿入し、TTS には絶対渡さない
3. **発火タイミングは subscriber 側で決定する**: server (SAIVerse 本体) は preset 名 + char_offset のメタデータを渡すだけ。実時刻計算は subscriber が `audio.currentTime` を見て行う (voice_tts_playback_queue の教訓継承)
4. **プリセットは priority 3層構造に従う**: builtin < expansion < user_data。user_data で上書き可能、builtin は git tracked
5. **vessel 種別未対応のプリセットは `fallback_text` でグレースフルに表現する**: actions に該当 vessel のキーが無い場合でも何かしらユーザーに見える形で代替 (= 動作の意図が失われない)

## オープン課題

- **TTS engine の timing API**: GPT-SoVITS が文字/単語単位 timing を返せるか調査。返せない場合は線形推定 + 経験則調整
- **既存スペル方式との共存**: 既に存在する individual な expression / servo / led スペルは廃止するか、低レベル API として残すか (= プリセットで表現できない一回限りの動作のため)
  - 暫定方針: 残す。プリセット = 高頻度・再利用、個別スペル = 一回限り・即興、で住み分け
- **マーカー誤検出対策**: 本文中に偶然「/emote 〜」という文字列が出てきた場合 (例: コードブロック内、ユーザー発言の引用) の扱い。コードブロック / 引用部はパースから除外する規則が必要
- **動的プリセット選択**: `/emote-random positive` のように tag 指定でランダム選択する構文を入れるか (= ペルソナの表現バリエーション増やすため) → Phase B 以降
- **複合プリセット連結**: `/emote wave /emote bow` のように短時間に複数発火した場合の挙動 (= queue / preempt) → 実装時に subscriber 側の queue 設計とすり合わせ
- **spell 機構との統合度**: emote を「発声同期版 spell」として構文・絞り機構 (spell_tools 相当) をどこまで共通化するか。共通化すれば実装・認知の重複を避けられるが、発火タイミング (ラウンド内逐次 vs TTS 同期) と対象カテゴリの違いをどう表現するか → 実装時に詰める

## 段階実装プラン

- **Phase A (パーサ + stackchan ディスパッチ + 基本プリセット 5-10 個)**: builtin に happy / sad / surprised / wave / nod 程度を用意。stackchan で動作確認。timing 推定は線形補間でスタート
- **Phase B (ペルソナによる save_emote tool)**: ペルソナがプリセットを作れる経路を解放。available_emotes section を head に追加
- **Phase C (Live2D / 3D vessel への横展開)**: vessel-specific ディスパッチ層を増やす。プリセット側に `actions.live2d` 等を追記する作業
- **Phase D (TTS timing API 連携)**: TTS engine が char timing を返せるなら、線形推定から正確な timing に切り替え

## 関連 doc

- `docs/intent/embodied_passive_input.md` — ペアになる入力側 (環境センサー) の機構
- `docs/intent/stackchan_vessel.md` — stackchan vessel 全体。本 doc の表現出力はこの上の発火層
- `docs/intent/voice_tts_playback_queue.md` — TTS 再生制御。subscriber 側で preset を発火する設計理由はこの doc の教訓に依拠
- `docs/intent/cached_head_architecture.md` — `available_emotes` section を追加する場合の section 設計参照
