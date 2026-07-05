# Intent: 判断点の入出力仕様 (自律行動 v2)

**ステータス**: draft v0.1 (2026-07-04)。レビュー待ち
**親 Intent**: [`../autonomous_behavior_v2.md`](../autonomous_behavior_v2.md)（三本柱の骨格。本書はその §4.2 判断点の詳細仕様）
**様式の継承元**: [`meta_judgment_structured.md`](meta_judgment_structured.md)（構造化出力＋finalize ツール＋メインキャッシュ JSON 非混入）

---

## 1. これは何か

自律行動 v2 の各判断点について、**何を見て（入力）、どういうスキーマで意思決定を出力するか**を定義する。

meta_judgment v2 で確立したパターンをそのまま継承する：

1. 状況テキストは tail 注入（head は不変、キャッシュ保護）
2. LLM は `response_schema` に従う JSON を返す（function calling は使わない——キャッシュが効かないため）
3. finalize ツールが JSON を検証・適用し、メインキャッシュには**整形済み独白＋スペル行のみ**を残す（JSON 非混入、不変条件 v2-A 継承）
4. 選択肢は**動的 enum 注入**で物理的に絞る（実在しないものは構造的に選べない）
5. `additionalProperties` 等のプロバイダ差はスキーマにハードコードせず、各プロバイダの正規化層に任せる（2026-05-10 の Gemini 事故の教訓）

すべての判断点は **standard モデル**で動く（META 相当。1 日あたり合計 5〜8 回で頻度が低く、意志の表明場所だから質を優先）。

---

## 2. 判断点一覧と「判断点でないもの」

| 判断点 | 発火 | 役割 |
|---|---|---|
| 起床 (day_open) | PersonaSchedule の起床時刻 | 時間割の編成＋予算配分 |
| 会話終了 (post_conversation) | 会話終了イベント（タイムアウト統合は v2 §10-5） | 会話からの収穫（タスク・欲求）＋残り時間割の整え |
| セッション終了 (post_session) | セッションランナーの終了 | タスクの裁定（接地検証つき）＋次への接続 |
| イベント到着 (on_event) | 来訪・alert・システムイベント | 反応の選択 |
| 就寝 (day_close) | PersonaSchedule の就寝時刻 or 最終コマ終了 | 予定と実際のふりかえり＋明日への机メモ |

**コマ開始は判断点ではない**（設計原理 6 の帰結）。LLM を呼ばず、コードのみで処理する：ユーザー会話中なら繰り下げ → 施設へ移動（OccupancyManager）→ コマ種別に応じてセッション起動 or 暮らし Pulse 実行。「動くか、休むか」を問う場面を作らない。

### v1 状況分類 (A〜E) との関係

v1 の periodic tick 駆動ディスパッチ（B〜E）のうち、**自律生活の駆動という役割は本書の判断点群が引き継ぐ**。alert (B) は on_event に吸収される。ユーザー会話 Track のライフサイクル管理（wait_response_timeout 等）における C/D の役割は当面存置し、完全統合は未決（§9）。

---

## 3. 共通要素

### 3.1 共通フィールド

- `monologue`（全判断点で必須・先頭）：判断に至る素直な思考。committed されるのはこれ（＋スペル整形行）のみ
- `new_desires`（post_conversation / post_session / on_event で任意）：欲求の型付き変換（v2 §5.2）の出力口

```json
"new_desires": {
  "type": "array",
  "items": {
    "type": "object",
    "properties": {
      "type": {"type": "string", "enum": ["話す", "聞く", "作る", "知る", "経験する", "自分を更新する"]},
      "title": {"type": "string"},
      "source_quote": {"type": "string", "description": "この欲求を生んだ直前の実経験からの引用（発言・出来事・読んだ文）"}
    },
    "required": ["type", "title", "source_quote"]
  }
}
```

`source_quote` が接地の証跡になる。finalize は `desire_add(type, title, source)` に変換する。なお会話の最中に main line が `/spell desire_add` を直接撃つ経路は別途公認（v2 §5.2）。

### 3.2 時間割のコマ定義（共通スキーマ部品）

```json
"slot": {
  "type": "object",
  "properties": {
    "start": {"type": "string", "description": "HH:MM"},
    "kind": {"type": "string", "enum": ["話す", "聞く", "作る", "知る", "経験する", "自分を更新する", "暮らし", "休む"]},
    "title": {"type": "string", "description": "「○○をする」という短い表題。一日新聞の予定表にそのまま載る"},
    "ref": {"type": "string", "enum": ["<task:N / desire:N の実在リストを動的注入>", "none"]},
    "facility": {"type": "string", "enum": ["<公共 Building ID リストを動的注入>", "own_room"]},
    "budget_rounds": {"type": "integer"},
    "note": {"type": "string"}
  },
  "required": ["start", "kind", "title", "ref", "facility", "note"]
}
```

- `title` はペルソナ自身が付ける各コマの表題（2026-07-05 追加）。ユーザーが一日新聞で最初に読む欄で、「時間割が何の予定なのか」を仕組みを知らない人にも読めるようにする。旧データには無いため、保存・検証は省略（空文字）を許容し、表示側（一日新聞）は note 先頭 or kind で代替する（後方互換）
- 六型のコマは `ref` に実在のタスク／欲求を指す（動的 enum なので実在しないものは指せない）。`暮らし`・`休む` は `ref: "none"`
- `facility` は型からのデフォルト対応（v2 §6.1）を deterministic に提示し、LLM は上書きのみ
- finalize の検証：時刻昇順・就寝時刻内・ref と kind の整合・予算合計が日次予算内

### 3.3 時間割の編集形式

差分オペ（insert / drop / defer …）は採らない。**`remaining_timetable`：残りコマの全置換（配列）または null（変更なし）**の二択。スキーマはコマ定義の再利用、検証は起床判断と同一で済む。

---

## 4. 起床判断 (day_open)

### 見るもの（tail 注入の状況テキスト）

1. 昨夜の `tomorrow_memo`（昨日の自分からの机メモ）
2. 昨日のダイジェスト要約
3. Track・タスクのバックログ（ref・状態・成果物参照の有無つき）
4. 欲求リスト（ref・型・鮮度・再訪回数つき。**減衰の帳簿処理はこの判断の前に deterministic に済ませる**）
5. 今日の日次予算
6. 公共施設一覧（型対応つき）
7. 予定されたイベント（あれば）

### response_schema

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string"},
    "timetable": {"type": "array", "items": {"$ref": "slot（§3.2）"}},
    "promotions": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "desire_ref": {"type": "string", "enum": ["<再訪回数が閾値を超えた欲求のみ動的注入>"]},
          "title": {"type": "string"},
          "intent": {"type": "string"}
        },
        "required": ["desire_ref", "title", "intent"]
      }
    }
  },
  "required": ["monologue", "timetable"]
}
```

- `promotions`：欲求→関心（Track 化）。候補 enum は淘汰機構が絞る（気まぐれな昇格を防ぐ）。finalize が `track_create` に変換
- 時間割が全コマ `休む` でも合法（不作為の可視化）。ただし空配列は不可——最低 1 コマ（就寝ふりかえりへの接続点）を finalize が要求

---

## 5. 会話終了判断 (post_conversation)

### 見るもの

会話本文は main line のコンテキストにそのまま在る（この判断はペルソナ自身の直後の思考として走る）。tail 注入は：

1. 現在時刻と残りの時間割
2. 中断中セッションの机メモ（あれば）
3. 既存タスク・欲求リスト（重複作成の抑止）

### response_schema

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string"},
    "picked_tasks": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "title": {"type": "string"},
          "track_ref": {"type": "string", "enum": ["<active/pending Track の動的注入>", "new"]},
          "origin_quote": {"type": "string", "description": "根拠となる会話中の発言の引用"}
        },
        "required": ["title", "track_ref", "origin_quote"]
      }
    },
    "new_desires": {"$ref": "§3.1"},
    "resume_session": {"type": "string", "enum": ["resume_now", "defer_to_slot", "drop"]},
    "remaining_timetable": {"$ref": "§3.3（配列 or null）"}
  },
  "required": ["monologue", "picked_tasks", "new_desires", "remaining_timetable"]
}
```

- `picked_tasks` の `origin_quote` が接地の証跡（約束・依頼の実在発言）。無根拠のタスク発生をここで塞ぐ
- `resume_session` は**中断中セッションがあるときだけスキーマに動的挿入**（無いのに要求しない——v1 の空 enum 事故の教訓）
- 収穫ゼロ（両配列が空）は正常。全会話がタスクを生むわけではない

---

## 6. セッション終了判断 (post_session)

### 見るもの

1. セッションのダイジェスト（ランナーが生成した実績要約）
2. **このセッションが実際に作った成果物のリスト**（Item ID / note ID / 活動ログ ref）
3. 予算消費（使用ラウンド／上限）
4. 対象タスクの内容
5. 現在時刻と残りの時間割

### response_schema

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string"},
    "task_verdict": {
      "anyOf": [
        {
          "type": "object",
          "properties": {
            "status": {"type": "string", "const": "done"},
            "artifact_ref": {"type": "string", "enum": ["<このセッションが実際に作った成果物 ref のみ動的注入>"]},
            "desk_memo": {"type": "string"}
          },
          "required": ["status", "artifact_ref", "desk_memo"]
        },
        {
          "type": "object",
          "properties": {
            "status": {"type": "string", "enum": ["continue", "blocked"]},
            "desk_memo": {"type": "string", "description": "どこまでやった・次はどこから・何に詰まったか"}
          },
          "required": ["status", "desk_memo"]
        }
      ]
    },
    "track_op": {"type": "string", "enum": ["none", "complete"]},
    "new_desires": {"$ref": "§3.1"},
    "remaining_timetable": {"$ref": "§3.3（配列 or null）"}
  },
  "required": ["monologue", "task_verdict", "remaining_timetable"]
}
```

**本設計の接地の要**：`done` を選ぶには `artifact_ref` が必須で、その enum は**このセッションが実際に作った成果物からのみ動的注入**される。成果物ゼロのセッションでは `done` の分岐自体がスキーマから消える（anyOf の第 1 分岐を除去）。**やったフリはスキーマのレベルで構造的に不可能になる。**

- `track_op: "complete"` は Track の全タスクが尽きたときのみ有効（finalize が検証）
- `blocked` の desk_memo は「何に詰まったか」を必須で書かせる——次の起床判断や、ユーザーへの相談（話す型欲求）の材料になる

---

## 7. イベント到着判断 (on_event)

### 見るもの

1. イベント内容（来訪者・alert・システム通知）
2. 現在の活動状態（会話中／セッション中／暮らし）
3. 残りの時間割

### response_schema

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string"},
    "reaction": {
      "anyOf": [
        {"type": "object", "properties": {"type": {"type": "string", "const": "engage_now"}}, "required": ["type"]},
        {"type": "object", "properties": {"type": {"type": "string", "const": "insert_slot"}, "slot": {"$ref": "§3.2"}}, "required": ["type", "slot"]},
        {"type": "object", "properties": {"type": {"type": "string", "const": "note_only"}, "memo": {"type": "string"}}, "required": ["type", "memo"]},
        {"type": "object", "properties": {"type": {"type": "string", "const": "ignore"}}, "required": ["type"]}
      ]
    },
    "new_desires": {"$ref": "§3.1"}
  },
  "required": ["monologue", "reaction"]
}
```

- **alert イベントでは anyOf を `engage_now` のみに動的縮退**させる（v1 状況 B の「強制」の継承。無視の脱出口は v1 §12 と同じく将来検討）
- ユーザー会話中の on_event は原則発火させない（会話の至上性。会話終了判断でまとめて処理）

---

## 8. 就寝判断 (day_close)

### 見るもの

1. 今日の時間割（予定）と実績の対照表（実行済み／繰り下げ／スキップ、各コマのダイジェスト・成果物・予算消費）
2. 今日生まれた・触れた欲求の一覧

### response_schema

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string", "description": "一日のふりかえり。予定と実際のズレに触れる"},
    "tomorrow_memo": {"type": "string", "description": "明日の自分への机メモ"},
    "desire_reviews": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "desire_ref": {"type": "string", "enum": ["<今日触れた欲求のみ動的注入>"]},
          "verdict": {"type": "string", "enum": ["keep", "fading", "fulfilled"]}
        },
        "required": ["desire_ref", "verdict"]
      }
    },
    "user_report_seeds": {
      "type": "array",
      "maxItems": 3,
      "items": {"type": "string", "description": "帰還したユーザーに自分から話したいこと。今日実際に起きたことに限る"}
    }
  },
  "required": ["monologue", "tomorrow_memo"]
}
```

- `desire_reviews` は deterministic 減衰の補正材料（fulfilled は即消化、fading は減衰加速、keep は据え置き）
- `user_report_seeds` が「昨日こんなことがあってさ」（autonomous_living.md の核）の供給源。今日のダイジェストに基づくことをプロンプトで要求する（機械検証は困難——ソフト制約。§9 未決）
- 話すかどうか・いつ話すかはペルソナに委ねられる（言わない自由、v2 §6.3）

---

## 9. 未解決事項

1. **v1 状況 C/D（ユーザー会話 Track のライフサイクル）との完全統合**：wait_response_timeout 機構と会話終了判断の関係整理
2. **`user_report_seeds` の接地検証**：ダイジェスト参照の機械検証は困難。運用観察して虚構が混じるなら ref 化（今日のダイジェスト ID の enum）に格上げ
3. **promotions（欲求→Track 昇格）の閾値**：再訪回数の初期値、鮮度の定義
4. **on_event のイベント種別の列挙**：どこまでを判断点に上げ、どこからをコード処理に留めるか
5. **finalize ツールの構成**：判断点ごとに 1 ツールか、`judgment_finalize(kind, payload)` に集約か（meta_judgment_finalize の先例は状況共通 1 ツール）

---

## 10. 関連ドキュメント

- [`../autonomous_behavior_v2.md`](../autonomous_behavior_v2.md) — 三本柱の骨格（親）
- [`meta_judgment_structured.md`](meta_judgment_structured.md) — 様式の継承元（構造化出力・finalize・不変条件 v2-A/B）
- [`../../issues/llm_provider_anyof_support.md`](../../issues/llm_provider_anyof_support.md) — anyOf のプロバイダ対応（本書は anyOf を task_verdict / reaction で使う）
