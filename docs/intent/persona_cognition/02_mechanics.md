# 認知モデル: 動的な仕組み

**親**: [README.md](README.md)
**関連**: [01_concepts.md](01_concepts.md) (用語) / [03_data_model.md](03_data_model.md) (永続化)

このファイルは認知モデルの**動的な振る舞い**を集約する。メタ判断の流れ、Pulse 階層、再開コンテキスト構築、ライン階層の管理。

---

## メタレイヤーの実行サイクル — A/B フロー

メタ判断は **Track 内メインラインからの一瞬の分岐** として動く。専用モデルや独立キャッシュは持たない。メインキャッシュも Track 横断 1 本のまま (不変条件 7)。

### 基本構造

```
[Track A のメインキャッシュ末尾]                          ← Track 横断 1 本のメインキャッシュ
  │
  │ メタ判断トリガ (定期 / alert / Pulse 完了等)
  ↓
[一瞬の分岐]
  - 末尾に「メタ判断用プロンプト」を追加して LLM 呼び出し
  - プロンプト構成:
    * 末尾メッセージ (Track A の直近状態)
    * pending / waiting / unstarted の Track 一覧
    * 開いている Note 一覧
    * 直近の外部イベント
    * 過去のメタ判断ログから参考情報を動的注入 ([1] メタ判断ログ領域から)
  ↓
[メタ判断ターン]
  - 重量級モデル (= メインモデル) で判断
  - response_schema: { thought, action: "continue"|"switch", switch_to_track_id?, new_track_spec?, current_disposition?, ... }
  ↓
判断:

  [A] 継続: 元の Track A 続行
      → 分岐ターンを Track A のメインキャッシュには **残さない**
      → 次のメインライン応答は元のキャッシュ末尾から続行 (キャッシュ完全ヒット)
      → ただし [1] メタ判断ログ領域には保存 (次のメタ判断時の参考情報として参照される)
      → 例外: response_schema に `notify_to_track` がある場合、その内容を event entry として Track A に追加

  [B] 移動: 別 Track Y に切り替え
      → 中断する Track A のサマリを生成 (履歴が一定以上長い場合、軽量モデルで作成)、A.pause_summary に保存
      → 分岐ターンを **そのまま残す** (Track 移動の来歴として、メインキャッシュに乗り続ける)
      → 移動先 Track Y の再開コンテキスト (Y.pause_summary + 末尾メッセージ + Note 差分) を Y のサブキャッシュ側で構築
      → 以降のメインライン応答は Track Y のものとして処理
      → 分岐ターンは [1] メタ判断ログ領域にも保存
```

### キャッシュ親和性の構造

LLM プロンプトキャッシュは「過去送ったプロンプト先頭部分との一致」でヒットする。これを利用する:

- **継続 [A] 時**: Track A の次のメインライン応答送信時、メタ判断分岐ターンは含めない → 分岐前のメインキャッシュ末尾までは過去送信と一致 → キャッシュヒット
- **移動 [B] 時**: Track Y の最初のメインライン応答送信時、メタ判断分岐ターンを含める → Track Y にとってこれは「冒頭の来歴ターン」になる → 次回 Y への送信でキャッシュヒット

**「保存しない」 = 「次のプロンプトに含めない」だけ**。メインキャッシュは Track 横断 1 本を維持しつつ、メタ判断分岐の commit/discard が両立する。実装上は `messages` テーブルの `scope` カラムで制御 (`discardable` → 続行時破棄、移動時 `committed` に昇格)。

### メタ判断ログ領域の役割

[A] 継続時もメタ判断ターンは **[1] メタ判断ログ領域には保存**される。理由: 別 Track からアラートが連続して来た時、毎回独立に判断すると「以前こう判断した」を踏まえられず判断が劣化する。

ログ領域からの参考情報注入はメタ判断時のプロンプト構築の中で動的に行う。古いログは適宜要約 (Metabolism 機構の活用)、最新のログは詳細に。

### 判断 LLM の response_schema

メタ判断は**現在の running Track を続けるか、別の状態に移動するか**の2択 (`continue` / `switch`)。`switch` のときに「現在の running をどう処遇するか」(`current_disposition`) と「次に何を活性化するか」(`switch_to_track_id` / `new_track_spec`) を別フィールドで指定する。

旧 v0.13 までの 4 値 enum (`continue`/`switch`/`wait`/`close`) は意味的に重複していたため (wait/close は switch のサブセット)、v0.14 で 2 値 + disposition 軸に再構成した。

- 旧 `wait` ≒ `switch` + target なし + `current_disposition="pause"` (アクティブ Track なし状態へ遷移)
- 旧 `close` ≒ `switch` + `current_disposition="complete"`

```json
{
  "type": "object",
  "properties": {
    "thought": { "type": "string", "description": "判断に至った内的独白" },
    "action": {
      "type": "string",
      "enum": ["continue", "switch"],
      "description": "現在の running を続けるか (continue)、別の Track に切り替えるか (switch)"
    },
    "switch_to_track_id": {
      "type": "string",
      "description": "switch の場合、切り替え先の既存 Track ID。省略時かつ new_track_spec も無い場合はアクティブ Track なし状態へ遷移 (旧 wait 相当)"
    },
    "new_track_spec": {
      "type": "object",
      "description": "switch で新規 Track を作成する場合の仕様 (track_type / title / intent / is_persistent 等)"
    },
    "current_disposition": {
      "type": "string",
      "enum": ["pause", "complete", "abort"],
      "description": "switch の場合、現在の running を pause (後で再開) / complete (完了) / abort (中止) のどれにするか。デフォルト pause。"
    },
    "close_reason": {
      "type": "string",
      "description": "current_disposition='complete' の場合の完了理由 (旧 close 用フィールドの後継)"
    },
    "notify_to_track": {
      "type": "string",
      "description": "continue の場合、現アクティブ Track に通知すべき内容"
    }
  },
  "required": ["thought", "action"]
}
```

---

## メタレイヤーの起動タイミング

判断間隔は **実時間ベース** で制御する (Pulse 数ではない)。重量級モデルのキャッシュ TTL に合わせる。

### 入口

| 入口 | トリガー |
|------|---------|
| `on_track_alert` | 外部イベント (ユーザー入力、ペルソナ間メッセージ、Kitchen 完了通知、占有変化等) で Track が alert 化したタイミング |
| `on_track_alert` | 内部 alert (各 Track のパラメータ閾値超過、スケジュール時刻到来等で Track が自発的に alert 化) — Phase 5 |
| `on_periodic_tick` | 重量級モデルのキャッシュ TTL を切らさない間隔 (Anthropic なら 1 時間以内、暫定 50 分) — Phase 4 |

両入口は **同じ判断ループ**を共有する。違いは context のみ:
- alert 入口: `context = {"trigger": "user_utterance", ...}` 等
- 定期入口: `context = {"trigger": "periodic_tick", "interval_seconds": ...}`

メタレイヤーのプロンプトは両ケースで「現状を見て判断する」共通形式。専用の判断ロジックを増やさない。

### Pulse 完了直後は起動しない

対ユーザー Track での発話完了直後をメタレイヤー判断のトリガにすると、ユーザーの返答を待たずに次の判断に走る不適切な挙動になる。「ユーザーの返答を待つ」のが基本姿勢。次に何をするかの判断はキャッシュ TTL 切れ直前まで待ち、これは結局**定期実行と同じタイミング**になるため、専用入口は設けない。

### アイドル状態の判断は定期実行に統合

「running Track が無い / 外部 alert が無い」状態でも、メタレイヤー定期実行が来た時に判断する。アイドル時の判断 (新規 Track 創設、pending Track 再開、何もしないで待つ) は専用入口を持たず、**通常の判断ロジックの中で「現状を見て決める」一部として扱う**。

理由:
- メタレイヤーの判断プロンプトには既に「現在 running / pending / waiting / unstarted の Track 一覧」が含まれる
- running が無いという状況も普通に判断材料の 1 つ
- 専用入口を増やすと責務分散が起き、状況に応じた重み付けが難しくなる

---

## Pulse の階層構造

Pulse は 2 種類: **メインライン Pulse** と **サブライン Pulse**。

| Pulse 種別 | 主体 | 単位 | 頻度の決まり方 |
|---|---|---|---|
| **メインライン Pulse** | Track 横断、ペルソナ単位 | 「判断・検収」1 回 | メインモデルのキャッシュ TTL / 外部イベント / サブからの区切りシグナル |
| **サブライン Pulse** | アクティブ Track 単位 | 「Playbook 実行」1 回 | サブモデルの特性 + メインラインから指示された連続回数上限 |

メインライン Pulse 1 回の中で「サブラインで N 回連続実行する」と方針を決めれば、サブラインはメインを呼び戻さずに進める。

**用語整理**: Pulse は「ライン 3 軸」の **「起点ライン」の起動**を指す。**入れ子ライン** (親から呼び出される子ライン) は Pulse ではなく Playbook 呼び出し階層の話で、頻度制御の対象外 (子は親の中で完結する)。

### Pulse の流れ (典型例: 自律 Track)

```
[Track が running になる]
  ↓
[メインライン Pulse]
  メインライン LLM (重量級モデル)
  - Track 情報 + 現状 + 使用可能 Playbook + Pulse 完了後挙動 を見て判断
  - 「サブラインで X 系作業を最大 N 回連続実行」と方針決定
  - またはスペルで直接 Track 操作 (track_pause / track_activate / track_create 等)
  ↓
[サブライン Pulse 1] (上で決めた方針に従って軽量モデルで実行)
[サブライン Pulse 2]
...
[サブライン Pulse N or 中断条件達成 or メインキャッシュ TTL 接近]
  ↓
[メインライン Pulse]
  サマリ + 検収 → 続行 / 切替 / 完了の判断
  ↓ ループ
```

### サブライン Pulse のみで進む期間

メインライン Pulse 間の期間は、サブライン Pulse が以下のいずれかまで連続実行される:

- メインラインが指定した連続回数上限に到達
- ペルソナ自身が `/track_pause` 等で意思表示
- メインモデルのキャッシュ TTL が切れる前 (TTL 接近で次のメインライン Pulse をトリガ)
- 外部イベント (alert) 到来

### Pulse サイクルの 7 つの制御点

サイクルの「型」は決まっているが、頻度・回数等の具体値は環境やペルソナ設定で変わる:

| # | 制御点 | 設定場所 | デフォルト想定 |
|---|--------|---------|--------------|
| (1) | Track 単位の Pulse 間隔 | `action_tracks.metadata.pulse_interval_seconds` | Handler の `default_pulse_interval` |
| (2) | Track 単位の連続実行回数上限 | `action_tracks.metadata.max_consecutive_pulses` | Handler の `default_max_consecutive_pulses` |
| (3) | メタレイヤー定期実行間隔 | `SAIVERSE_META_LAYER_INTERVAL_SECONDS` | 3000 (50 分、Anthropic TTL ベース) |
| (4) | モデル別キャッシュ TTL 同期 | `model_configs.py` の `cache_ttl_seconds` | Anthropic: 240 秒 / ローカル: 制限なし |
| (5) | メインライン Pulse のトリガ条件 | スケジューラのロジック | TTL 接近 + 外部イベント + サブからの区切りシグナル |
| (6) | サブライン Pulse のメインライン 1 呼び出しあたり最大回数 | メインライン LLM 出力 (方針指示) | メインが指定、上限なしなら -1 |
| (7) | サブライン Pulse の間隔 | Handler の `default_subline_pulse_interval` | ローカル: 0 秒 / Claude: 数秒 |

これらの設定可能性により、環境の違い (ローカル / クラウド / 混在) を仕様の変更なしに吸収できる。

### 環境別デフォルト値 (Phase 4 で導入)

#### Pattern A: Claude メイン + ローカルサブ (まはー想定)
```
SAIVERSE_META_LAYER_INTERVAL_SECONDS = 3000  # 50 分
Track.metadata.pulse_interval_seconds = 0    # サブライン連続実行
Track.metadata.max_consecutive_pulses = -1   # メインキャッシュ TTL まで無制限
default_subline_pulse_interval = 0           # 連続実行
```

#### Pattern B: 全 Claude (高コスト警戒)
```
SAIVERSE_META_LAYER_INTERVAL_SECONDS = 3000
Track.metadata.pulse_interval_seconds = 60   # サブも 1 分間隔
Track.metadata.max_consecutive_pulses = 10   # メイン 1 呼び出しあたり 10 回まで
default_subline_pulse_interval = 5           # 5 秒待機
```

#### Pattern C: 全ローカル
```
SAIVERSE_META_LAYER_INTERVAL_SECONDS = 1800  # 30 分等、自由
Track.metadata.pulse_interval_seconds = 0
Track.metadata.max_consecutive_pulses = -1
default_subline_pulse_interval = 0
```

これらはペルソナ作成時に DEFAULT_MODEL から自動推定して metadata に書き込む形が便利。手動で調整も可能。

---

## メインラインの Pulse 開始プロンプト構成

メインラインへの入力プロンプトは、**キャッシュ親和性のため固定情報と動的情報を明確に分離**する。

### 固定情報 (キャッシュ先頭、初回 Pulse でのみ追加)

軽量モデル側のキャッシュが新規構築されるタイミング (= Track 切り替え or キャッシュ TTL 切れ後の最初の Pulse) でのみコンテキスト先頭に追加される情報:

- **Track 識別**: id, title, type, intent
- **使用可能 Playbook 候補と各説明**: この Track 種別で許可される Playbook 群
- **Pulse 完了後挙動の通知**: この Track のリズム説明 (応答待ち / 連続実行)
- **Track 種別固有の振る舞い指針**: Handler が定める指針 (例: 対ユーザー Track なら「相手の発話は審判ではなく対話の一部」等)

これらは Track が変わらない限り再送しない。

### 動的情報 (Pulse ごと、コンテキスト末尾に追加)

毎 Pulse 末尾に追加する情報:

- 直近のサマリ (前 Pulse の結果、開かれている Note の差分等)
- 新着イベント (alert 通知、内部 alert、外部メッセージ)
- このターンでペルソナが受け取った発話 (あれば)

これらは履歴として自然に積み重なる。

### 「初回 Pulse」の判定

軽量モデル側キャッシュの初回構築タイミング:

1. Track が unstarted → running になった最初の Pulse
2. Track が pending/waiting → running に戻った Pulse (キャッシュが切れていた場合)
3. キャッシュ TTL 経過後の最初の Pulse

このタイミングでのみ固定情報をコンテキスト先頭に積む。それ以降は動的情報のみ末尾追加。Track の状態に「軽量キャッシュ最終構築時刻」(`action_tracks.metadata.cache_built_at`) を持たせる。

### プロンプト構築の流れ

```python
def build_main_line_prompt(persona, track):
    handler = get_handler_for_track_type(track.track_type)
    is_first_pulse = _is_first_pulse(track)
    
    parts = []
    
    if is_first_pulse:
        # 固定情報 (キャッシュ先頭)
        parts.append(format_track_identity(track))
        parts.append(format_available_playbooks(handler))
        parts.append(handler.pulse_completion_notice)
        parts.append(handler.track_specific_guidance)
        track.metadata.cache_built_at = now()
    
    # 動的情報 (毎 Pulse 末尾)
    parts.append(format_recent_summary(track))
    parts.append(format_new_events(track))
    parts.append(format_received_utterance(track))
    
    return "\n\n".join(parts)
```

固定情報は **Anthropic キャッシュ可能ブロック**としてマークすることでキャッシュヒットを最大化する (`cache_control` 等)。

---

## Pulse 完了後挙動と Track 種別の関係

Track 種別ごとに「Pulse 完了後どう振る舞うか」のデフォルトがある。Handler が `pulse_completion_notice` 文字列と `post_complete_behavior` 列挙を保持する。

### 軸 1: 完了後挙動

| 種別 | 完了後挙動 | プロンプトでの説明例 |
|------|-----------|-------------------|
| **応答待ち型** (`wait_response`) | 相手の応答を待つ。勝手に次の判断に進まない | 「Pulse 完了後はユーザーの返答を待つ。次のイベントが来るまで他のことを考えなくて良い」 |
| **連続実行型** (`meta_judge`) | 一段落 → メタレイヤーが続行 / 切り替え判断 | 「Pulse 完了後はメタレイヤーが次の判断をする。続行か別 Track 移行か任せて良い」 |

応答待ち型: 対ユーザー会話 / 交流 / 外部通信 / MCP Elicitation 待ち
連続実行型: 自律 / スケジュール起因 / 記憶整理

### 軸 2: 起動経路

| 起動経路 | 説明 |
|---------|------|
| 即時起動型 | 作成 = activate (or alert で即起動) |
| イベント駆動型 | 最初から waiting、外部イベントで起動 |

ターン単位で挙動を切り替えたい場面は、既存の `track_wait` スペルでペルソナが明示的に応答待ち状態へ移行できる。

メタレイヤー定期実行が来た時、現 running Track の Handler の `post_complete_behavior` を見て:

- `wait_response`: 抑止 (ユーザー応答待ちなので発火しない)
- `meta_judge`: 通常判断 (続行か切り替えか判断)

---

## Track の中断と再開

### 中断時サマリ作成

B フロー時の効率を確保するため、**中断する側で事前にサマリを作る**。

理由: 中断時はその Track のコンテキストがまだ温まっている。その場で軽量モデルにサマリを作らせるのが最も安い。再開時にゼロから組み立てるよりずっと効率的。

仕組み:

- Track を中断する直前、履歴の長さをチェック
- 一定以上長ければ (暫定: 7 メッセージ以上)、軽量モデルが「現在の状態 + 進行中の意図 + 重要な決定事項」のサマリを作成
- サマリは `action_tracks.pause_summary` に保存
- 履歴が短い場合 (暫定: 6 メッセージ以下) はサマリ不要、再開時は末尾メッセージのみで十分

サマリのフォーマット (自然言語要約 + 構造化メタ情報の混合):

```
## この Track の状況

[自然言語による要約 1〜3 段落]

### 進行中の意図
[この Track で達成しようとしていること]

### 重要な決定事項・進捗
- ...

### 関係エンティティ
- 人物: ...
- アイテム: ...
- 参照中の Memopedia: ...
```

### 再開時のコンテキスト構築

メタレイヤーが Track Y への切り替えを決めた時、軽量モデル側のコンテキストとして再開ビューを構築:

```
[システムプロンプト等の先頭部分 (変更しない、キャッシュ温存)]

...

[再開コンテキスト挿入領域]

## トラック「Y」の再開

### 前回までのサマリ
{Y.pause_summary}

### 直前のやりとり (末尾 N メッセージ)
- [user] ...
- [assistant] ...
- ...

### 開いている Note の差分 (中断時から変化があれば)
- Note「対エイド」: [追記された内容の要約 or 直近のメッセージ]
- Note「Project N.E.K.O.」: [追記された内容の要約]

### この Track を今再開する。
```

特性:

- **挿入位置**: コンテキスト末尾 (不変条件 7 のキャッシュ親和性に沿う)
- **構築者**: 軽量モデル (不変条件 8 の使い分け)
- **Note 差分の挿入**: Y を中断した時点の Note 状態と現在の Note 状態の差分を、event entry として整形して含める
- **追加情報取得**: ペルソナが「サマリだけでは足りない」と判断した場合、明示的にツール呼び出しで取得 (memory_recall / note_read 等)

### Note と再開フローの関係

Track を再開する時、起源 Track の認識回復が**主**であり、他 Track からの情報を素のメッセージとして混ぜることはしない。これは「家事 Track 中の SAIVerse 開発アイディア」のような場合に、家事 Track の作業履歴に SAIVerse 開発の話が混入するのを防ぐため。

その代わり:

- **Track 開始 / 再開時に開いた Note の最新状態**を読み込む
- **再開時は中断時の Note 状態との差分**を event entry (system タグ付き user メッセージ) として挿入
- 別 Track での発見・更新が、Note を開いている全 Track に自然に伝わる

これにより:

1. **会話の終了 vs 中断**が明確に分かれる:
   - 中断: pause_summary + 末尾メッセージで再開、起源 Track の続き
   - 終了: Track は close、Note は残る → 「前にこんな話した」を覚えた状態で新規 Track 開始
2. **複数 Track 間での情報共有**が Note 経由で自然に発生
3. **3 人会話問題の解決**: 3 人会話のメッセージは「対 A Note」「対 B Note」両方に書き込まれる。後で 1 対 1 で話す時はその Note を開けばよい

### 既存ペルソナ再会機能との対称性

ペルソナ再会機能は「**中断準備が存在しないケースの特殊形**」として位置づけられる:

| 状況 | 中断準備 | 再開時の動き |
|------|---------|-------------|
| 通常の Track 中断・再開 | あり (中断時に作る pause_summary) | サマリ + 末尾メッセージで再開 |
| ペルソナ再会 (既存実装) | なし (過去会話を持つだけで線として中断管理されてない) | 過去会話 + Memopedia から都度組み立て |

新基盤では:

- 過去にペルソナ X と話した記録があれば、X 専用の Track を暗黙的に存在するものとして扱う
- 再会時はその Track の再アクティブ化 (中断準備なし版) として、既存の Memopedia / 過去会話取得ロジックが走る
- 以降の会話は Track として管理され、次回中断時には pause_summary が作られる

Phase 5 でこの汎用化を実装する。

---

## ライン階層管理機構

### ランタイム上の階層表現

`PulseContext` を **階層化** して親子関係を持たせる:

```
PulseContext (Pulse 1)
├── Line: 起点メインライン (line_id=L0, role=main, parent=None)
│   ├── Line: 入れ子サブ (line_id=L1, role=sub, parent=L0)
│   │   └── (子ライン完了で消滅、report_to_parent を L0 に append)
│   └── Line: 入れ子メイン (line_id=L2, role=main, parent=L0) ← レア
└── ...

PulseContext (Pulse 2、別の自律 Track の Pulse)
└── Line: 起点サブライン (line_id=L3, role=sub, parent=None) ← サブライン Pulse スケジューラ起動
    ├── Line: 入れ子サブ (line_id=L4, role=sub, parent=L3)
    └── Line: 入れ子メイン (line_id=L5, role=main, parent=L3) ← レア
```

`PulseContext._line_stack` で LIFO 管理。実装は `sea/pulse_context.py:56-224` にあり (Phase 1 完了済み)。

### `line_id` の生成と付与

ライン起動時に新規 UUID を発行し、以下に伝播:

| 用途 | 場所 |
|---|---|
| メッセージ保存時のメタデータ | `messages.line_id` カラム |
| ライン階層の追跡 | `PulseContext._line_stack` |
| 起点ライン識別 | `meta_judgment_log` 等の参照経路 |

ノード実行時に「自分がどの line_id で動いているか」を `current_line()` で取得し、メッセージ保存時に `line_id` メタデータとして渡す。

### 親-子の寿命管理

不変条件 12 を実装で守る:

- 子ラインの起動時に親 `line_id` を記録
- 子ライン完了時に `report_to_parent` を親の `state["_messages"]` へ append、子の `LineFrame` を pop
- 親ラインが Track 切り替えで凍結された場合、子もその時点で凍結される (PulseContext ごと中断)
- Track 完全消滅 (`track_abort`) 時、その PulseContext 全体が破棄される (子ラインは自動的に消滅)

### 起点ライン複数並走

1 Track 内に起点サブラインが複数並走するケース (例: 自律 Track 内で記憶整理サブと web リサーチサブが同時稼働) は、それぞれが独立した `PulseContext` を持つ:

- SubLineScheduler が「同 Track の異なる起点サブライン」を別 Pulse として起動
- 各 PulseContext が独立した `_line_stack` を持つ
- メッセージ保存時の `track_id` は同じだが `line_id` が異なる → 7 層 [3] (Track 内サブキャッシュ群) では `line_id` で区別される

---

## `report_to_parent` 機構

子ライン完了時、結果を親ラインに伝える唯一の経路。

### output_schema での必須化

子 Playbook の `output_schema` には **`report_to_parent` を必須**で含める。Playbook ロード時 / `save_playbook` ツール経由 / `import_playbook.py` 経由でバリデーション:

```python
def validate_child_playbook(playbook: PlaybookSchema) -> None:
    """can_run_as_child=true の Playbook は report_to_parent を含む必要がある。"""
    if "report_to_parent" not in (playbook.output_schema or []):
        raise ValueError(
            f"Playbook '{playbook.name}' lacks 'report_to_parent' in output_schema. "
            f"Child playbooks must report back to their parent line."
        )
```

判定方針: Playbook 定義に `can_run_as_child: bool` メタ属性を追加 (デフォルト false)。これが true の Playbook のみ `report_to_parent` 必須チェック対象とする。

### サマリ生成ノードの推奨パターン

子 Playbook の最後にサマリ生成専用ノードを置く:

```json
{
  "id": "summarize_for_parent",
  "type": "llm",
  "action": "子ライン作業の結果を、親ライン側のあなた自身に伝える形で1〜3段落で要約してください。\n作業内容: {execution_log}\n結果: {final_result}",
  "output_key": "report_to_parent"
}
```

ペルソナにとっては「自分が一段下のレイヤーで考えた内容を、上のレイヤーに伝え直している」感覚 (不変条件 11)。

### ランタイムでの append 処理

子 Playbook 完了時:

```python
if final_state.get("report_to_parent"):
    report = final_state["report_to_parent"]
    formatted = f"<system>子 Playbook '{playbook.name}' の実行結果:\n{report}</system>"
    parent_state["_messages"].append({
        "role": "user",
        "content": formatted,
    })
```

system タグ付き user メッセージとする理由: 既存の `inject_persona_event` パターンと整合させるため。親モデル側からは「自分への通知」として認識される (不変条件 11)。

### 子ラインの messages コピー仕様

`line: "sub"` (および将来の `line: "main"` で別キャッシュ分岐パターン) で起動される子 Playbook の初期 messages 構築:

```python
parent_messages = parent_state["_messages"]  # = [..., A, B, C]

# 子ライン起動時、コピーで分岐
child_initial_state = {
    "_messages": list(parent_messages),  # ← コピー (参照共有しない)
    # ... その他の state は別途構築
}
```

これにより:
- 子ライン内では「親の会話履歴 + 自分の作業履歴」が見える (ペルソナの意識の連続性)
- 子ライン内での messages 変更は親に影響しない (コピーなので)

これは **親メイン → 子サブ** の典型例だけでなく、**親サブ → 子サブ** や **親サブ → 子メイン** (レア用途) でも同じ仕組みが適用される。

---

## メタレイヤーは Playbook 内 LLM ノードで実装する

メタレイヤーの判断ロジック (Track の選択・切り替え判断) は **Playbook 内の LLM ノードとして実装**する (Phase 1.2 で確定)。メインライン Playbook の最初に「メタ判断ノード」を組み込み、その後にメインライン応答ノードへ進む。

メタ判断ノードは **Track 内メインラインの一瞬の分岐**として動く:

- 結果が「継続」なら分岐ターンは Track のメインキャッシュには残さない (commit/discard 機構)
- 結果が「移動」なら分岐ターンはそのまま残し、新 Track の冒頭来歴になる
- ノード自身は **メタ判断ログ領域 [1]** への書き出しを行う (Track 続行/移動に関わらず保存)

`saiverse/meta_layer.py` の役割:

- **判断ロジック本体**: `meta_judgment.json` Playbook へ移管
- **alert ディスパッチ役**: TrackManager の alert observer として残し、適切な Playbook を起動するだけのディスパッチャに縮退

---

## Playbook 起動とラインの関係

### Pulse 開始時の Playbook は必ず起点メインライン

Pulse 開始時に起動される Playbook は**必ずメインライン**で動かす。これにより:

- Pulse 開始時の判断 (どの Playbook を使うか、どんな方針で動くか) が確実に重量級モデルで行われる (不変条件 8/9)
- メインキャッシュが Track 横断で連続的に積み重なる (不変条件 7)

ただし**サブライン Pulse** (起点サブラインの Pulse) も存在する。これは「サブライン Pulse スケジューラから起点サブラインを直接起動する」場合で、自律 Track の継続実行等が該当する。この時のサブライン Pulse は最初から軽量モデルで動く。

### Playbook 起動時のライン指定

Playbook 内から別の Playbook を呼ぶ時、ライン指定を明示する。指定は概念的に **2 つの軸**:

1. **継続 / 分岐**: 親と同じキャッシュを共有 (継続) するか、別建てで分岐するか
2. **モデル種別** (分岐時): 重量級 (= メイン) か軽量 (= サブ) か

`SubPlayNodeDef.line` フィールドが両方を兼ねる:

| 親ラインの種別 | 子の `line:` 指定 | 結果 |
|---|---|---|
| 親メイン | `main` | メインキャッシュ継続、同じ重量級モデルで処理 |
| 親メイン | `sub` | 別サブキャッシュへ分岐、軽量モデルで処理、完了時 `report_to_parent` |
| 親サブ | `main` | 別メインキャッシュへ分岐、重量級モデルで処理 (部分処理に重量級必要時、レア) |
| 親サブ | `sub` | 別サブキャッシュへ分岐 or 親サブキャッシュ継続 (実装段階で決定) |

---

## 多者会話と audience

複数のペルソナ・ユーザーが同じ Building にいる時の会話処理。

### output_target と audience の役割分担

[01_concepts.md の output_target と audience の分離](01_concepts.md#output_target-と-audience-の分離) を参照。

### audience による自動振り分け

Building 内に居る全ペルソナ + ユーザーは output_target=`building:current` の発話を受信する。各受信者は audience に応じて反応する:

| audience に含まれるか | 動作 |
|------------------|------|
| 含まれる | 該当 Track が `alert` 状態に → メインライン起動候補 |
| 含まれない | 関連 Person Note に記録するが反応しない |

### 多者会話のループ防止

audience を厳格に解釈することで自然にループを防げる:

- A が B に質問 (audience=[B]) → B のメインライン起動 → B が応答 (audience=[A]) → A のメインライン起動 → ...

この場合は正当な対話だが、**メタレイヤーが「会話を続けるか切り上げるか」を判断**して終わらせる。技術的なループストッパー:

- メタレイヤーが Track の発話数をカウント
- 一定数 (暫定 20) 超過で `track_pause` を強く推奨 (自動停止ではなく判断材料として)
- `SAIVERSE_TRACK_AUTO_PAUSE_HINT_TURNS` (暫定 20) で調整可能

### 別 Building のペルソナへの呼びかけ

`output_target=building:current` では別 Building には届かない。SAIVerse 内の別 Building / 外部 SAIVerse のペルソナへ発話するには:

- 一時的な `external:saiverse:<persona_id>` 通信 Track を作る
- 既存の SAIVerse 間ペルソナ通信機構を活用 (dispatch / visiting AI)

---

## 応答待ちの仕組み

応答待ち (`waiting` 状態) の Track は、外部応答 (ユーザー、他ペルソナ、Kitchen 完了通知、X リプライ等) を待っている状態。

### 監視方法

**SAIVerse 側で自動ポーリング**し、変化があった時にメタレイヤーへイベント通知する:

- ポーリングの責務: SAIVerseManager (または専用の WaitingMonitor) が `action_tracks` の `waiting` 状態の Track を定期的にチェック
- 通知経路: 既存の `inject_persona_event` を活用、`PersonaEventLog` 経由でメタレイヤーへ
- ペルソナ側にポーリングのコードは持たない

検知対象:

- `waiting_for` で指定された外部応答が到達したか
- `waiting_timeout_at` が過ぎていないか
- `waiting_for` の対象が「もう発生しない」と判明したか

### `waiting_for` フィールドの規約

JSON 構造化:

```json
{
  "type": "user_response" | "persona_response" | "kitchen_completion" | "external_event" | ...,
  "channel": "ui" | "discord" | "x" | "elyth" | ...,
  "target": "persona_id" | "user_id" | "cooking_id" | ...,
  "elicitation_request_id": "..."
}
```

応答到達検知のロジックは `type` ごとに別実装する (拡張ポイント)。

### 多重応答待ちの優先順位

複数の `waiting` Track があり、複数応答が同時に到達した場合:

- **新しい Track 優先**。理由: 細かいタスクから片付ける方がスムーズ
- 「新しい」の基準は `last_active_at` または Track 作成時刻

ただし優先順位はメタレイヤーが最終判断する。SAIVerse 側は「応答が来た」イベントを通知するのみ。

### タイムアウト

`track_wait(track_id, waiting_for, timeout=...)` で設定可能。`timeout=None` は無期限。

タイムアウト到達時:

- 自動で `abort` や `pending` に遷移**しない**
- メタレイヤーへタイムアウトイベントを通知し、判断を仰ぐ
- メタレイヤーが `track_resume_from_wait(track_id, "abort")` 等を選択する

### 応用範囲

| 応用 | `waiting_for.type` | 監視・検知 |
|------|-------------------|-----------|
| ユーザーへの返答待ち (通常会話) | `user_response` | UI からのメッセージ送信検知 |
| 他ペルソナへの応答待ち | `persona_response` | 相手ペルソナの発言検知 |
| MCP Elicitation | `mcp_elicitation` | MCP サーバーからの応答受信 |
| Kitchen 長時間処理完了 | `kitchen_completion` | Kitchen の cooking ステータス監視 |
| X / Mastodon リプライ待ち | `external_event` (channel=x) | 外部 API ポーリング |
| スケジュール時刻到来 | `scheduled_time` | 時刻監視 |

すべて同じ `waiting` 状態の Track として扱われ、メタレイヤーが統一的に管理する。

---

## 相手判定は「現在のコンテキストで判断する」

外部チャネル (X mention、Discord 等) からの会話では、そのツールで取れる情報しかペルソナに渡らない。

汎用機構としては「現在のコンテキストに見えている情報の中で判断する」を原則とする。それで足りない場合は、**個別チャネルの統合フロー側を組み直す話**になる (汎用機構の責任範囲外)。

これにより、SAIVerse 内ペルソナとの会話、X リプライ、Discord メッセージ、ユーザー入力すべてが同じ流れで処理される。

---

## 関連ドキュメント

- [01_concepts.md](01_concepts.md) — 用語と不変条件
- [03_data_model.md](03_data_model.md) — テーブルスキーマ
- [04_handlers.md](04_handlers.md) — Handler パターン
- [phases/phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) — Playbook 整備の進捗
- [phases/phase_4_pulse_scheduler.md](phases/phase_4_pulse_scheduler.md) — Scheduler 実装の進捗
