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
| 就寝 (day_close) | PersonaSchedule の就寝時刻 or 最終コマ終了 | 予定と実際のふりかえり＋明日の自分へのメモ |

> **用語（2026-07-05）**: 造語「机メモ」はユーザー／ペルソナに見える文言から全廃した。表示・プロンプトでは Track の状態メモ（`desk_memo`）を「作業メモ」、`tomorrow_memo` を「明日の自分へのメモ／昨日の自分からのメモ」と呼ぶ。内部フィールド名（`desk_memo` / `tomorrow_memo`）は変更しない。

**コマ開始は判断点ではない**（設計原理 6 の帰結）。LLM を呼ばず、コードのみで処理する：ユーザー会話中なら繰り下げ → 施設へ移動（OccupancyManager）→ コマ種別に応じてセッション起動 or 暮らし Pulse 実行。「動くか、休むか」を問う場面を作らない。

### v1 状況分類 (A〜E) との関係

v1 の periodic tick 駆動ディスパッチ（B〜E）のうち、**自律生活の駆動という役割は本書の判断点群が引き継ぐ**。alert (B) は on_event に吸収される。ユーザー会話 Track のライフサイクル管理（wait_response_timeout 等）における C/D の役割は当面存置し、完全統合は未決（§9）。

---

## 3. 共通要素

### 3.1 共通フィールド

- `monologue`（全判断点で必須・先頭）：判断に至る素直な思考。committed されるのはこれ（＋スペル整形行）のみ
- `new_desires`（post_conversation / post_session / on_event で任意）：欲求の型付き変換（v2 §5.2）の出力口
- `episode_purposes`（post_conversation / post_session / day_close で任意、2026-07-07 追加）：閉じた出来事への目的タグの棚入れ（層 2、`life_concept_map.md` §9.1）。enum は実在の Track＋採用済み task の参照（欲求候補は含めない——候補は木の外）。post_conversation / post_session は当該 episode への参照配列、day_close は `{episode, purpose}` ペア配列。finalize が `purpose_tags`（layer=2）へ永続化し、適用エコーが記録本文に乗る。スキーマはコード側（`saiverse/judgment_points.py`）で注入され playbook JSON は不変

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
- 六型のコマは `ref` に実在のタスク／欲求を指す（動的 enum なので実在しないものは指せない）。`暮らし`・`休む` は `ref: "none"`。**終了済み（completed / cancelled）タスクを指す ref は finalize の検証（sanitize_timetable）でコマごと棄却**——enum は生存タスクから構築されるが、「enum 構築後に完了したタスクの ref」が同じ判断の remaining_timetable や旧 plan の引き写しで滑り込む経路を塞ぐ（2026-07-05 実 LLM シム 3 回目 異常③）
- `facility` は型からのデフォルト対応（v2 §6.1）を deterministic に提示し、LLM は上書きのみ
- **欲求の提示書式は enum と同じ `desire:N`**（`desire_engine.to_desire_ref`）：状況テキストの「やりたいこと候補」一覧・就寝判断の「今日触れた欲求」・`desire_add` の戻りテキストは、ref を `task:N` の生表示ではなく `desire:N` で見せる。プロンプトに `task:N` と表示すると、ペルソナがそれを書こうとした構造化出力の制約デコードが enum 内の別 ref に滑り、無関係なタスクが選ばれる（2026-07-05 実 LLM シム: `task:2` 表示 → enum は `desire:2` → 制約デコードが `task:1` に滑落）。プロンプト表示と enum の整合は回帰テストで固定（`tests/test_judgment_points.py::test_day_open_desire_candidate_lines_match_ref_enum`）。なおメタ判断（idle_pending の promote）は enum・提示とも `task:N` で内部整合しており別語彙のまま
- finalize の検証：時刻昇順・就寝時刻内・ref と kind の整合・予算合計が日次予算内

### 3.3 時間割の編集形式

差分オペ（insert / drop / defer …）は採らない。**`remaining_timetable`：残りコマの全置換（配列）または null（変更なし）**の二択。スキーマはコマ定義の再利用、検証は起床判断と同一で済む。

- 厳密昇順の検証は**新コマの区間のみ**に適用する。消化済みコマ（fired / done / skipped）は帳簿として先頭に残るが、その境界を跨いだ比較はしない——「直前に消化したコマと同時刻・過去時刻から始まる組み替え」（直近コマのやり直し等）は正当な意志であり、過去時刻の新コマは EventScheduler が即時扱いする（2026-07-05 実 LLM シム 3 回目: 境界比較の全却下で正当な組み替えが黙って消えた不具合の修正）
- **却下は黙って捨てない**：置換が適用されなかった場合（全コマ無効・検証失敗）、finalize は「時間割の変更は適用されませんでした（理由）」をペルソナの文脈に乗る適用サマリ（lines）に必ず載せる。warnings（ログ）だけに落とすと、ペルソナは「組み替えた」つもりのまま一日を続け、就寝判断が実態とズレた総括をする（接地原則違反）。一部コマのみ棄却して適用した場合も除外件数を明示する
- **例外: 空配列 `[]` は null と同じ「変更なし」として黙って扱う**（2026-07-18 改定）。空の時間割は不変条件（最低 1 コマ）で保存できず、[] が有効な変更要求でありうる余地がない。実データでは [] は「残りコマが現実に無い時点の判断」で LLM が事実を記述したものとして出る（観測された却下 6 件は全件このケース）——変更の意図が無いものに却下エコーを返すと、ペルソナの記憶に無意味な失敗文が積もる。残りコマが無い時点でも欄自体は出し続ける（コマ追加という正当な組み替えの口を塞がないため——欄の条件挿入案は 2026-07-18 まはー裁定で見送り）

---

## 4. 起床判断 (day_open)

### 見るもの（tail 注入の状況テキスト）

1. 昨夜の `tomorrow_memo`（昨日の自分からのメモ）
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

**発火の前提（2026-07-05）**：この判断は「会話が実際にあった」ことが前提であり、**1 往復（ユーザー発話＋ペルソナ応答）も成立しなかった会話では発火させない**。応答生成が失敗した会話に対して「会話がひと区切りつきました。会話の内容はこの文脈にあります」という偽前提の状況テキストで判断を走らせると、ペルソナは直近文脈から「会話があったかのような」振り返りを紡ぐ（実 LLM シムで実証——作話の温床）。往復の成立はペルソナ応答が building_messages に実在するかで判定する（呼び出し側の責務。シムは `RealConversationUserEventDriver` が担う）。

### 見るもの

会話本文は main line のコンテキストにそのまま在る（この判断はペルソナ自身の直後の思考として走る）。tail 注入は：

1. 現在時刻と残りの時間割
2. 中断中セッションの作業メモ（あれば）
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

> **改定決定 (2026-07-18 まはー裁定・実装は W1 と同工区)**: digest 生成を post_session に統合する。
> 現行構造は「digest 専用コール（セッション文脈・軽量）→ post_session（メインライン文脈・標準）が
> **自己申告のダイジェストだけ**を根拠に裁定」で、①同じ事実が最終発話・digest・独白・作業メモの
> 4 回書かれる冗長、②採点者が答案の原本を見ない接地の弱さ、の二つの問題が実機で確認された
> (2026-07-18 観測: air の 10:00 セッションで 32 秒間に 4 回の同内容記述)。統合後:
> - post_session の位置・文脈・モデルは**変えない**（メインライン文脈・標準モデルのまま）
> - 状況文の「ダイジェスト:」欄を**セッション原本**（ラウンド発話+スペル+結果）に置き換える
> - response_schema に digest 欄を追加し、post_session 自身が要約を書く → それが確定済み
>   main_line 記録になる（digest 専用コールは廃止、コール数 3→2）
> - 原本の埋め込みに**文字数上限は設けない** — セッションが走れた時点でサイズは実証済み
>   （原本=セッションのモデルが実際にコンテキストに載せた内容）。
> - **原本の注入はコールローカル** (2026-07-18 追加裁定): paired_action_text (保存される状況文)
>   には原本を**含めない** — 状況文は adapter が以後の Pulse 文脈へ paired_action 展開するため、
>   素朴に埋めると原本が毎 Pulse のコンテキストに乗り続ける (実測: 2026-07-18 の air post_session
>   プロンプトに 07-16 の判断状況文が残存)。post_session の LLM コールにだけ見せ、保存側は
>   従来のメタ情報 + episode 参照に留める。統合工事 §6-5 の anchor コールローカル化と
>   同パターン。恒久記録は digest 一本 (生ログはセッションと共に死ぬ) のまま
> - **原本への読み口は現状存在しない — (a') 実装時に新設する** (2026-07-18 事実確認):
>   生ログ全行に層0タグ `metadata.origin_episode` が刻まれ (sea/runtime.py の保存時自動付与)、
>   volatile の物理削除経路も無いため原本は memory.db に残る — が、origin_episode を読む
>   コードはどこにも無く (書き込み専用)、episode の digest_ref が指すのも digest メッセージのみ。
>   「episode:N の原本を読む」操作 (スペル or saiverse:// URI、origin_episode で引くクエリ) を
>   (a') と同時に用意する — 保存側から原本を落とす以上、見る方法とセットが原則
> - セッション内の最終発話の無意味さは別件（終了スペル案 = quick_spell の終端宣言と同思想）として
>   保留 — LLM コール数が (a) と変わらないため今回は見送り
>
> 以下の「見るもの」1 は改定後は「セッションの記録（原本）」に読み替える。

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

- 対象タスクが**既に終了済み（completed / cancelled）**の場合は `task_verdict` 欄自体をスキーマから出さない——再 done 裁定（artifact_refs 多重追記）も、終了済みタスクへの desk_memo（偽の「中断中の作業」化）も構造的に不可能にする。finalize 側にも同じ棄却の二重ガードがある（2026-07-05 実 LLM シム 3 回目 異常③: 完了済みタスクへの再セッションで再 done が通った）
- `track_op: "complete"` は Track の全タスクが尽きたときのみ有効（finalize が検証）
- `blocked` の desk_memo は「何に詰まったか」を必須で書かせる——次の起床判断や、ユーザーへの相談（話す型欲求）の材料になる
- **出典の規律（まはー決定 2026-07-05）**：状況テキストの指示部で「独白・裁定・メモで挙げる出典は、このセッションで実際に参照・取得した情報源に限る」ことを明示する。2 回目の実 LLM シムで、Web 取得していない学会ガイドラインを desk_memo の根拠として語る出典作話が起きた——desk_memo は翌日の自分が読む記録なので、虚構の混入は接地原則違反（スキーマでは防げないためプロンプト明示で対処）

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

**実績の提示語彙も接地原則に従う（2026-07-05）**：システム都合のスキップ（実行手段未実装・日次予算切れ・会話優先の繰り下げ上限）は slot の `skip_reason` に記録され、対照表と一日新聞の両方で「実行できず（システム側の問題）」等と明示する。本人が選んでいないスキップを「見送り」（本人判断を示唆する語）で提示すると、ペルソナがふりかえりで**してもいない判断の理由を捏造する**（実 LLM シムで実証——「やったつもりバグ」と同型の「決めたつもりバグ」）。実装は `saiverse/day_plan.py` の `slot_result_label()`（`judgment_points.build_day_results_text` と `day_report` が共用）。

同じ理由で、**詳細な実行記録の無い done も「実行済み」と提示しない（まはー決定 2026-07-05）**：スタブ実装の「暮らし」「休む」コマは完了時に slot へ `record_level='presence_only'` が永続化され、対照表と一日新聞の両方で「時間を過ごした（詳細な記録なし）」と提示する。スタブの done を「実行済み」と見せると、ペルソナが就寝ふりかえりでしていない活動の内容（食事の選定等）を自分の成果として捏造する（soft-confabulation、実 LLM シム 異常 #4）。施設への実移動（presence）だけは本物なので、居た場所は事実として扱ってよい。マーカーの無い旧データ・セッション系の done は従来どおり「実行済み」（後方互換）。親 doc `../autonomous_behavior_v2.md` §4.1「暮らし/休む コマの現段階」参照。

出典の規律は就寝判断にも適用する（§6 と同趣旨、まはー決定 2026-07-05）：状況テキストの指示部で「ふりかえり・メモで挙げる出典は今日実際に参照・取得した情報源に限る」ことを明示する。`user_report_seeds` の「作り話禁止」と同じ接地の系譜で、対象を出典に広げたもの。

### response_schema

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string", "description": "一日のふりかえり。予定と実際のズレに触れる"},
    "tomorrow_memo": {"type": "string", "description": "明日の自分へのメモ"},
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
