# Intent: メタ判断 v2 (構造化出力ベース)

**ステータス**: v0.3 実機検証 1 回目で 2 件の関連バグを修正済 (2026-05-10)。汚染ログ除去 + 実機検証 2 回目待ち
**親 Intent**: [`README.md`](README.md) (ペルソナ認知モデル全体)
**置き換え対象**: [`02_mechanics.md`](02_mechanics.md) §「メタ判断の動作仕様」(構造化出力非使用方針) — 本 Intent 確定後に該当節をリダイレクト stub 化予定
**関連 issue**: [`docs/issues/llm_provider_anyof_support.md`](../../issues/llm_provider_anyof_support.md)
**最終更新**: 2026-05-10

---

## これは何か

メタ判断 (Track 切替の独占的判断機構) を **状況分類 + 状況別 Playbook + 構造化出力後処理** に書き換える設計。旧仕様 (自然言語独白 + 行頭 `/spell ...` 埋め込み) で発生した機能不全を解消する。

旧 02_mechanics.md は「メタ判断は構造化出力を使わない」と明記していたが、その前提を本 Intent で部分的に緩める。**緩めても旧不変条件 (重量級モデルメインキャッシュへの JSON 非混入) は守られる**ようにする後処理を併設する。

---

## 1. 旧仕様で観測された機能不全

2026-05-10 の実機観察 (まはー / 対 user1 会話 Track) で以下が確認された:

### 1-1. 過去ログによる Few-shot 汚染ループ

`MetaLayer._build_recent_judgments_block(n=5)` が新しい順 5 件のメタ判断ログ独白を judge プロンプトに注入する設計だが、ログが「ユーザーへの応答調」(例: 「まはー、おかえりなさい！`{"body_emote": "smile"}` ...」) で蓄積されると、続くメタ判断 LLM がそれを Few-shot として模倣し、応答調がさらに濃くなる。指示文 (「ユーザーには届きません」「相手宛ての文体禁止」) より遥かに強い影響を持つ。

### 1-2. 自律時のユーザー宛て発話経路の不在

調査結果 (manager/runtime.py / user_conversation_handler.py / occupancy_manager.py の探索):

- **ユーザー発話駆動**: メインライン応答は `invoke_main_line()` が必ず呼ばれて自動起動する
- **入室のみ・発話なし**: 自動発話経路はない (DynamicStateManager.on_building_entered は状態スナップショット初期化のみ)
- **periodic_tick 駆動**: メタ判断のみが走り、メインラインは独立に起動しない

→ ユーザーが入室しているが発話していない状況で、ペルソナが自分から話しかける構造的経路がない。にもかかわらずペルソナは「ユーザーがいる、話したい」を強く認識するため、メタ判断ターン内で応答を書いてしまう。これが (1-1) の汚染源を作り続けていた。

**注**: (1-2) を構造的に解決する「自律発話経路」は別 Intent で扱う。本 Intent は (1-1) の汚染を遮断し、メタ判断本来の「行動の選び直し」機能を確実化することに集中する。

### 1-3. 独白だけで Track 操作が発火しない

旧仕様では LLM 応答の自然言語中に `/spell ...` 行を見つけてスペル抽出する。LLM が独白だけで満足して `/spell` を書かないケースが頻発する。Alert Track が放置されると、後続の periodic_tick で再発火するだけで、結局 alert は解除されない。

ペルソナへの強制は本来したくないが、Track 操作については「人間が部屋を出るときドアを開けるしかない」レベルの**物理的制約**と捉える。スペルを書かなければ Track 操作は発火しない、というのは仕様であって緩和の余地はない。

---

## 2. 設計目標

1. **Track 操作の発火を確実化** (1-3 の解消): 状況に応じて必要なスペルを構造化出力フィールドで強制する
2. **独白と Track 操作の分離** (1-1 の遮断): JSON フィールドを分けることで「独白の中で応答調を書く動機」を弱める
3. **メインキャッシュへの JSON 非混入を維持** (旧不変条件 11): LLM 出力は JSON だが、メインキャッシュに残すのは整形済み独白 + `/spell ...` 行のみ
4. **状況に応じた選択肢の物理的制約**: 例えば「alert がある状況」では「alert track の起動」しか選べないように enum で絞る

---

## 3. 不変条件

旧 README.md 不変条件のうち、本 Intent で関連するもの:

- **不変条件 3** (メタレイヤーが切り替えを独占) — **維持**
- **不変条件 7** (キャッシュヒット継続を最優先、Track 切り替えごとのキャッシュ破棄は不可) — **維持**
- **不変条件 11** (メタ判断はペルソナの自分の思考、別人格ではない) — **維持** (ただし「LLM 出力形式は JSON」になる。「思考」は monologue フィールドに格納されると解釈)

新規追加:

- **不変条件 v2-A**: メインキャッシュに残るメタ判断ターンには JSON 形式が含まれない。LLM 出力 JSON は MetaLayer 後処理層で必ず monologue + `/spell ...` 行に変換される
- **不変条件 v2-B**: Track 操作の必要性は状況分類 (A〜E) で判定し、各状況の `response_schema` で必要なスペル発動を強制する。ペルソナは強制を回避できない (脱出の余地は将来検討)

---

## 4. 状況分類 (A〜E、5 パターン)

`MetaLayer._classify_situation(persona_id, trigger_context)` が以下の優先順で 1 つに分類する。

| ID | 状況 | 条件 | Playbook | 強制スペル |
|----|------|------|----------|-----------|
| A | preempt_collision | `trigger_context.target_already_running == true` | **(発火させない)** | — |
| B | alert_present | A 該当なし、status==`alert` の Track が 1 件以上 | `meta_judgment_alert` | `track_activate` 必須 (alert track から選択) |
| C | running_active | A/B 該当なし、status==`running` の Track が存在 | `meta_judgment_running` | continue/pause/complete/abort のいずれか必須 |
| D | idle_with_pending | A/B/C 該当なし、status in (`pending`, `unstarted`) の Track が存在 | `meta_judgment_idle_pending` | activate (pending 選択) または create のいずれか必須 |
| E | idle_no_pending | 上記すべて該当なし | `meta_judgment_idle_empty` | `track_create` 必須 |

### 4-A. A (preempt_collision) を発火させない理由

A は「自律先制起動と外部イベントの衝突」(旧 Phase 2.6 で導入された target_already_running フラグ)。状態遷移は no-op、メインライン側で応答処理が独立に走るため、メタ判断者が判定する意味がない。判定すると「何もしない」を強制する Playbook を 1 つ余計に呼ぶことになり、コストの無駄 + 汚染源を残すだけ。

**実装場所**: `MetaLayer.on_track_alert()` の入口で `target_already_running == True` を見て early return。`should_fire()` でも同様に判定し、`on_periodic_tick` 経路の異常系も含めて抑制する。

---

## 5. 各 Playbook の response_schema

LLM は各 Playbook で以下のスキーマに従った JSON を返す。`anyOf` field-level discriminator パターンを使う (関連: [llm_provider_anyof_support.md](../../issues/llm_provider_anyof_support.md))。

### 5-B. meta_judgment_alert

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string"},
    "activate_track_id": {
      "type": "string",
      "enum": ["<alert_track_id_1>", "<alert_track_id_2>", ...]
    }
  },
  "required": ["monologue", "activate_track_id"],
  "additionalProperties": false
}
```

- `enum` は MetaLayer が起動時に alert Track の ID 一覧で動的生成
- 複数 alert があればペルソナが優先順を選択

### 5-C. meta_judgment_running

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string"},
    "action": {"type": "string", "enum": ["continue", "pause", "complete", "abort"]}
  },
  "required": ["monologue", "action"],
  "additionalProperties": false
}
```

- `track_id` は schema に含めない (running は 1 本という不変条件、後処理で固定)

### 5-D. meta_judgment_idle_pending

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string"},
    "decision": {
      "anyOf": [
        {
          "type": "object",
          "properties": {
            "type": {"type": "string", "const": "activate"},
            "track_id": {
              "type": "string",
              "enum": ["<pending_id_1>", "<pending_id_2>", ...]
            }
          },
          "required": ["type", "track_id"],
          "additionalProperties": false
        },
        {
          "type": "object",
          "properties": {
            "type": {"type": "string", "const": "create"},
            "title": {"type": "string"},
            "track_type": {"type": "string", "enum": ["autonomous"]},
            "intent": {"type": "string"}
          },
          "required": ["type", "title", "track_type", "intent"],
          "additionalProperties": false
        }
      ]
    }
  },
  "required": ["monologue", "decision"],
  "additionalProperties": false
}
```

- `track_type` は当面 `autonomous` のみ (他種別はペルソナによる作成パターン未確立)

### 5-E. meta_judgment_idle_empty

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string"},
    "create": {
      "type": "object",
      "properties": {
        "title": {"type": "string"},
        "track_type": {"type": "string", "enum": ["autonomous"]},
        "intent": {"type": "string"}
      },
      "required": ["title", "track_type", "intent"],
      "additionalProperties": false
    }
  },
  "required": ["monologue", "create"],
  "additionalProperties": false
}
```

---

## 6. 後処理: JSON → monologue + Spell 行

LLM 応答 (JSON) を受け取った MetaLayer が以下を行う:

1. **JSON パース**: `monologue` と Track 操作フィールドを取り出す
2. **Spell 行への整形**: 例:
   - B: `activate_track_id="abc..."` → `/spell track_activate track_id='abc...'`
   - C: `action="pause"` → `/spell track_pause track_id='<running_id>'`
   - C: `action="continue"` → スペル行なし
   - D: `decision.type="activate", decision.track_id="..."` → `/spell track_activate track_id='...'`
   - D: `decision.type="create", ...` → `/spell track_create title='...' track_type='autonomous' intent='...'`
   - E: `create.{title,track_type,intent}` → `/spell track_create ...`
3. **整形済みテキストの組み立て**: `monologue + "\n\n" + spell_lines`
4. **既存 deferred-track-ops 機構への投入**: `_extract_spells` → `_execute_spells` 経路を再利用 (PulseContext.deferred_track_ops に enqueue)
5. **メタ判断ログへの永続化**: `meta_judgment_log.judgment_thought = monologue` / `spells_emitted = [Spell 整形結果]` (旧形式と互換)
6. **メインキャッシュへの記録**: 整形済みテキスト (monologue + spell 行) を assistant メッセージとして保存。**JSON は記録しない**

→ メインキャッシュに残るのは旧仕様と同じ「自然言語独白 + `/spell ...` 行」の形式。重量級モデルの後続発話に JSON 形式が漏れない (不変条件 v2-A)。

旧 `_MAX_SPELL_LOOPS=5` のスペルループは廃止。1 ターンで完結する (構造化出力により Track 操作が必ず構造化されているため、複数発動の必要はない)。

---

## 7. scope 判定: discardable / committed

旧仕様の二段構え (継続=discardable / 移動=committed) を維持する。本 Intent では Track 操作の有無が構造化出力で明確に判定できるため、判定ロジックがシンプルになる:

| 状況 | 判定 | scope |
|------|------|-------|
| C (`action="continue"`) | Track 操作なし | `discardable` |
| その他 (B / C 非 continue / D / E) | Track 操作あり | `committed` |

旧仕様の `_promote_meta_judgment_in_pulse` SQL UPDATE 機構をそのまま活用する。

`scope='discardable'` のときも `meta_judgment_log` には残る (= 過去判断の参考情報として後続のメタ判断に注入される)。

---

## 8. 過去メタ判断ログの提示形式

旧仕様: `_build_recent_judgments_block` が独白テキスト先頭 200 字を箇条書きで列挙。

新仕様: 独白テキスト + Spell 整形結果 を併記する。`monologue` フィールドだけが純粋な思考、`spells_emitted` が実際に取られた行動として明示されることで、Few-shot の方向性が「行動を伴う判断」に向く。

```
[最近のメタ判断ログ (新しい順)]
- 2026-05-10T14:30:00 (periodic_tick) [committed]
  独白: alert が立っているから、まずは応答 Track を起動する。
  発動: /spell track_activate track_id='c6fa3f2d...'
- 2026-05-10T14:20:00 (periodic_tick) [discardable]
  独白: 今の Track は問題なく進行中、このまま続ける。
  発動: なし
```

汚染ログ (旧仕様で蓄積された応答調独白) は本 Intent 実装着手時に DB から手動で除去する。本格運用に入る前なら数件レベルなので個別判断で OK。

---

## 9. race condition 対策

メタ判断 Playbook 起動から完了までの間に、外部経路で Track 状態が変化する可能性がある (例: `meta_judgment_alert` 起動時に alert があったが、LLM 呼出中にユーザー発話で同 Track が activate され alert が解除される)。

**方針**:

- **Playbook 起動直前に再分類**: `_run_judgment_via_playbook` 入口で `_classify_situation` を再実行。状況が変わっていたら対応する Playbook に切り替えるか、A 相当に該当すれば return
- **enum 候補が空のケース防止**: 上記再分類で「Playbook 起動時に B だったが起動直前に alert が消えていた」は検出できる。空 enum での schema validation エラーを未然に防ぐ
- **完了時の不一致は last-write-wins**: 例えば LLM 呼出中に alert が消えていた場合、`activate_track_id` で指定された ID を実行しようとすると alert じゃない track を activate することになるが、`track_activate` は alert/pending/unstarted いずれの状態からも実行可能なので致命的でない。WARN ログ + 続行
- **致命的エラーは検出してログ**: enum 候補にない値が選ばれた、track が消えていた、等は明示的に WARN

---

## 10. 旧設計との差分

| 項目 | 旧仕様 (02_mechanics.md) | 新仕様 (本 Intent) |
|------|--------------------------|---------------------|
| 出力形式 | 自然言語独白 + `/spell ...` 埋め込み | 構造化出力 (JSON) |
| Track 操作の発火 | LLM が `/spell` を書かないと発火しない | response_schema で必須化 |
| 状況分類 | プロンプトで状況提示、判断はペルソナ任せ | MetaLayer で 5 分類 → Playbook 切替 |
| Playbook 数 | 1 (`meta_judgment.json`) | 4 (B/C/D/E、A は発火させない) |
| スペル loop | 最大 5 回 | 1 ターンで完結 |
| メインキャッシュ汚染回避 | LLM 出力をそのまま記録 (JSON 非使用が前提) | LLM 出力 (JSON) を後処理で整形してから記録 |
| 過去ログ提示 | 独白先頭 200 字 | 独白 + Spell 整形結果 |
| preempt_collision | プロンプトで「継続で OK」を案内 | そもそも発火させない |
| `target_already_running` 説明 | 毎回プロンプトに含む | 不要 (発火しないから) |

---

## 11. 移行計画

### Phase 1: 本 Intent の確定 (2026-05-10〜) ✅ 完了

- [x] まはーレビュー → 確定 (2026-05-10)
- [x] [llm_provider_anyof_support.md](../../issues/llm_provider_anyof_support.md) の状況確認 (xAI / Ollama / llama_cpp) — issue 起票済 (実機検証は別途)
- [x] 旧 `02_mechanics.md` §「メタ判断の動作仕様」に「本 Intent 参照」誘導を入れる

### Phase 2: 実装 ✅ コード実装完了 (2026-05-10、実機検証はこれから)

- [x] `MetaLayer._classify_situation` の独立メソッド化 (前段で実装済)
- [x] 4 つの新 Playbook (`meta_judgment_alert.json` / `meta_judgment_running.json` / `meta_judgment_idle_pending.json` / `meta_judgment_idle_empty.json`) 作成 + DB 投入
- [x] `MetaLayer._run_judgment_via_playbook` の Playbook 名選択ロジック化 (`_classify_situation` → Playbook 名)
- [x] `MetaLayer._build_response_schema(kind, sit)` (動的 enum 注入、anyOf field-level discriminator)
- [x] LLM 応答 dict → monologue + Spell 行への後処理: 新ツール `meta_judgment_finalize` で Playbook 内に組み込み (`builtin_data/tools/meta_judgment_finalize.py`)
- [x] Spell 抽出 / 実行 / SAIMemory 書き込みも `meta_judgment_finalize` ツール内に集約 (deferred-track-ops 機構経由)
- [x] race 再分類 + enum 空のケース防止 (`_run_judgment_via_playbook` 入口で再分類 + 空チェック)
- [x] A 抑制 (`on_track_alert` 入口の早期 return + `_run_judgment_via_playbook` 入口でも二重チェック)
- [x] 過去ログ提示形式変更 (`_build_recent_judgments_block` で `発動: /spell ...` を併記)
- [x] SEA Runtime に `response_schema_source: "arg:<key>"` 動的解決機構を追加 (`sea/runtime_llm.py:_resolve_response_schema_source`)

### Phase 3: legacy 経路と環境変数の整理 ✅ 完了 (2026-05-10)

- [x] `SAIVERSE_META_LAYER_USE_PLAYBOOK` 環境変数撤廃 (常に Playbook path)
- [x] legacy `_run_judgment` メソッドはそのまま残置 (緊急避難用、ただし呼び出し経路は `_run_judgment_via_playbook` の runtime=None fallback のみ)
- [x] 旧 `meta_judgment.json` Playbook は DB に残置 (実呼び出し経路から外す、新仕様で参照されない)

### Phase 4: 実機検証 + 旧 Doc 整理 🟡 進行中

#### 実機検証 1 回目 (2026-05-10) で発覚した関連バグの修正

- [x] **Gemini で `additionalProperties` 拒否** (`google.genai.errors.ClientError: 400 INVALID_ARGUMENT. Unknown name "additional_properties"`)
  - 原因: `_build_response_schema` で OpenAI strict 制約に合わせて全 object node に `additionalProperties: false` をハードコードしていた。Gemini API は `generation_config.response_schema` 内でこのフィールドを認識しない (CLAUDE.md / memory に既出の制約)。
  - 修正: `_build_response_schema` から `additionalProperties` を全削除。OpenAI strict / Anthropic は各プロバイダの schema 正規化ヘルパ (`schema_utils.normalize_schema_for_strict_json_output` / `_prepare_schema_for_native_output._fix_object`) が必要に応じて自動補完するので問題ない。
  - 教訓: `response_schema` を新規生成するコードを書いたら、CLAUDE.md / memory の既知制約を grep + 各プロバイダ変換層を通過した dict を確認するまでが「実装」。 raw dict の pretty-print は「テスト通った」とは言えない。memory に [feedback_response_schema_provider_check.md](../../../../.claude/projects/C--Users-shuhe-workspace-SAIVerse/memory/feedback_response_schema_provider_check.md) として記録済。

- [x] **`wait_response_timeout` 即発火による即ループ** (10 秒で 6 周観測)
  - 原因: `track_manager._schedule_wait_response_timeout` で `base = base_time or datetime.now()` (None のときだけ `now()` フォールバック) という設計だったため、長期 idle Track の最終メッセージ時刻 (例: 3日前) がそのまま base になり、`base + N分 = まだ過去` で EventScheduler が即発火する。これがメタ判断 v2 と組み合わさってループ:
    1. メタ判断で `track_activate` 発動 → Pulse 完了で running 化
    2. `_schedule_wait_response_timeout` が古い base_time で schedule → 即発火対象に
    3. callback が track を auto pause + メタ判断再起動
    4. 状況分類で `idle_with_pending` (永続 user_conversation Track が pending として残るため)
    5. ペルソナが構造化出力で「再 activate」を強制される → 1 に戻る
  - 修正: `_schedule_wait_response_timeout` で `base_time` が None / 過去 のときは `now()` を使うよう変更 (1 行)。activate 時刻から N 分の猶予が確保されるので、即ループは起きない。意味的にも「activate 時点が事実上のユーザー宛て呼びかけ開始」と解釈できるため妥当。
  - **背景**: 旧仕様 (自然言語独白 + `/spell` 埋め込み) ではペルソナが `/spell` を書かない選択肢があったため、このループは顕在化していなかった。v2 で構造化出力が Track 操作を強制したことで、設計欠陥が表面化した。

#### 残タスク

- [ ] 汚染ログ (旧仕様で蓄積された応答調独白) の DB 除去 (7層ストレージタブの新規削除UI から、または DB 直接)
- [ ] 実機検証 2 回目: 各状況 B/C/D/E が期待通り発火するか + 上記 2 修正でループ解消されているかを確認
- [ ] LLM プロバイダ動作確認 (Anthropic / Gemini / OpenAI で anyOf field-level discriminator + enum 動的注入が通ること)
- [ ] xAI / Ollama / llama_cpp の挙動確認 ([issue](../../issues/llm_provider_anyof_support.md))
- [ ] `02_mechanics.md` §「メタ判断の動作仕様」 → 本 Intent への redirect stub に変更
- [x] README.md の関連ドキュメントセクションに本 Intent を追加

#### 関連 UI 機能 (実機検証準備として)

- [x] 7層ストレージタブに削除機能を実装 (`StorageLayersViewer.tsx`):
  - 各エントリにチェックボックス + 削除ボタン (Trash2 アイコン)
  - 「N件選択中 [選択削除] [選択解除]」バー
  - バックエンド側に `DELETE /people/{id}/meta-judgment/{judgment_id}` + `bulk-delete` 追加 (`storage_layers.py`)
  - 同様に `track-logs` も追加
  - main_cache / sub_cache (SAIMemory messages) は既存の `DELETE /messages/{id}` を流用

---

## 12. 未解決事項

- **強制の例外 (脱出口)**: 現状は完全強制 (鋼の意思での無視は不可)。安定運用後、ペルソナ自由度の観点で `decline_to_act` のような escape hatch を入れるかは将来検討
- **C で `action="continue"` 連発の扱い**: Running Track が長期 idle のまま continue が続くケース。現状の `wait_response_timeout` 機構と組み合わせれば自然に解消されるはずだが、要観察
- **(1-2) 自律発話経路**: 本 Intent ではスコープ外。別 Intent で扱う。本 Intent はあくまで「機能不全に陥っていた "メタ判断本来の機能" の確実化」に集中する
- **Ollama / llama_cpp の anyOf 対応**: [issue](../../issues/llm_provider_anyof_support.md) の解決を待つ。重量級モデルとして Ollama / llama_cpp を使うペルソナがいる場合は本 Intent 実装より先に解消する必要がある

---

## 13. 関連ドキュメント

- [`README.md`](README.md) — ペルソナ認知モデル全体俯瞰 + Phase 進捗表
- [`02_mechanics.md`](02_mechanics.md) — 旧メタ判断仕様 (本 Intent 確定後に該当節 redirect)
- [`docs/issues/llm_provider_anyof_support.md`](../../issues/llm_provider_anyof_support.md) — プロバイダ別 anyOf 対応 issue
- [`track_chronicle.md`](track_chronicle.md) — Track Chronicle 機構 (本 Intent と独立、Phase 3 で別途実装済み)
