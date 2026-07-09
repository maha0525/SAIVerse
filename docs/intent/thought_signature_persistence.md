# Intent Document: Thought Signature Persistence

**ステータス**: 起草中 (v0.1, 2026-05-20)
**位置付け**: Gemini 3.5 Flash 投入に伴う追加実装。マルチターン会話で Gemini が生成する `thoughtSignature` を欠落させないため、SAIMemory の `messages` テーブルに専用カラムを追加してターン跨ぎ永続化を実現する。
**前提**: `unified_memory_architecture.md` (SAIMemory schema)、`cached_head_architecture.md` (head/tail 分離)

---

## 1. これは何か / 何でないか

### これは何か

Gemini 3.x 系 LLM が生成する `thoughtSignature` (opaque な思考連続性トークン) を、SAIMemory の `messages` テーブルに専用カラムで永続化し、ターンを跨いだ LLM 呼び出しでも復元・echo できる仕組み。

### これは何でないか

- **ネイティブ function calling (`tool_calls`) の永続化機構ではない。** SAIVerse は memory `feedback_no_function_calling_for_cache.md` の方針で「playbook 内 structured output + tool ノード固定実行」に倒しており、`tool_calls` のターン跨ぎ永続化は当面不要。将来必要になった場合は本実装を参考に別カラムを追加する形で拡張する
- **Anthropic thinking blocks の永続化機構ではない。** Gemini の signature とは別概念・別シリアライゼーションなので、将来 Provider 別カラムとして個別に追加する
- **LLM Provider 非依存の "thinking trace" 永続化ではない。** Gemini の signature 専用のシンプルな実装に留める。複数 Provider にまたがる thinking 永続化が要求された時点で再設計の対象とする

### なぜ必要か

Gemini 3.5 Flash 公式仕様 ([whats-new-gemini-3.5](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.5)) によれば:

- テキストパートにも `thoughtSignature` が乗ることがある
- マルチターンでシグネチャを落とすと **"推論と回答の品質が低下する"** (Text/Chat の場合)
- Function Calling (Strict) では 400 エラーになるが、SAIVerse は native function calling 非採用なのでこの経路は無関係
- 公式 SDK の自動処理は `types.Content` を直接保持する場合のみ有効。SAIVerse は messages dict 経由でやり取りしているため、永続化と復元を自前で実装する必要がある

現状の `llm_clients/gemini.py:_separate_parts` (line 1370-1386) と stream のテキスト抽出は thought_signature を捨てており、1 ターン目で signature が生成されても 2 ターン目で送り返せない。Function Calling 経路の signature echo は既に実装済み (`gemini.py:1527-1529` / `sea/runtime_llm.py:2724-2727`) なので、本実装は **テキスト経路の signature 永続化** を埋める。

---

## 2. 設計原則

### Provider 固有の値を metadata に押し込まない

`metadata` JSON カラムは role / scope / tags / audience 等の Provider 非依存メタデータの集約場所。Gemini 固有の `thought_signature` を metadata に同居させると、検索クエリや migration の対象範囲が曖昧になり、metadata の意味論 (= ペルソナ的に意味のあるラベル) を汚す。専用カラム化することで:

- SQL レベルで `WHERE thought_signature IS NOT NULL` 等のクエリが書ける
- 将来別 Provider の同種フィールドが追加された時に同じパターンで横展開できる (= memory `feedback_design_boldly_not_conservatively.md` の「部分再設計」)
- metadata の意味論を汚さない

### 欠落許容で設計する

SAIVerse は 1 ターン = 1 playbook 実行 単位で動いており、signature 欠落の影響は「次ターンの思考品質低下」のみで動作不能にはならない。よって:

- 既存メッセージ (signature 無し) はそのまま動作する (NULL 許容)
- 一部 Provider クライアントが signature を生成しなくても全体は動く
- 一部のメッセージで signature が壊れていても、その箇所だけ品質低下するだけで他は影響なし

### Gemini クライアントの責務範囲を明確化する

レスポンス解析時に thought_signature を抽出 → message dict に格納 → 次ターン送信時に Part に復元、という閉ループは `GeminiClient` 内で完結させる。SEA runtime や SAIMemory adapter は「不透明な値 (Optional[str]) を bypass する」役割のみを担い、signature の解釈や生成には関与しない。

### opaque な値として扱う

thought_signature は Google が定義する opaque token。中身を解釈・分解・部分使用しない (公式 SDK が opaque を要求している)。SAIVerse 側では文字列としてそのまま保存・読み出し・送信する。

---

## 3. アーキテクチャ

### 3.1. データモデル

`messages` テーブルに `thought_signature TEXT` カラムを追加 (NULL 許容)。

```sql
ALTER TABLE messages ADD COLUMN thought_signature TEXT;
```

Migration は `database/migrate.py` ではなく、`sai_memory/memory/storage.py:init_db` 内の `_ensure_column` パターンを踏襲 (idempotent な「あれば追加」方式)。理由: SAIMemory は per-persona の独立 DB であり、`migrate.py` は `saiverse.db` 側を対象としているため。

### 3.2. データフロー

**保存経路 (Gemini 応答 → DB):**

1. `GeminiClient._separate_parts` / stream のテキスト抽出で、最初の text Part の `thought_signature` を拾う
2. `_store_thought_signature(value)` (新規 LLMClient API) で client インスタンスに保持
3. SEA runtime の LLM ノード処理が response 完了時に `client.get_response_thought_signature()` を取得し `state["_last_thought_signature"]` に保存 (既存の Function Calling 経路と同じパターン)
4. `_assistant_msg` 構築時に `message["thought_signature"] = state["_last_thought_signature"]` をトップレベルに入れる
5. `history_manager._sync_to_memory` → `append_persona_message(message)` → adapter `_append_message` が `thought_signature` キーを読み取って `add_message(..., thought_signature=...)` に渡す
6. `add_message` (storage.py) が `thought_signature` 列に INSERT

**復元経路 (DB → Gemini リクエスト):**

1. `Message` dataclass に `thought_signature: Optional[str] = None` を追加
2. `_payload_from_message_locked` (adapter.py:1051-1089) が `msg.thought_signature` が非 None なら payload dict に含める
3. `GeminiClient._convert_messages` が assistant role message から `thought_signature` を取り出して、対応する `types.Part` に乗せる
   - text-only assistant: 最初の text Part に乗せる (Gemini 公式仕様: 最初のパートまたは function_call パートに付与)
   - tool_calls 付き assistant: 既存実装が echo 済み (gemini.py:682-684)

### 3.3. インターフェース変更

**LLMClient base クラス (`llm_clients/base.py`):**

- 新規: `_store_thought_signature(self, value: Optional[str])` / `get_response_thought_signature(self) -> Optional[str]`
- Gemini 以外は default 実装で常に None

**Gemini Client (`llm_clients/gemini.py`):**

- `_separate_parts`: 最初の text Part (thought ではない) の `thought_signature` を抽出して返す
- `generate` / `generate_stream`: 取得した text signature を `_store_thought_signature` で保持
- `_convert_messages`: assistant role の message から `message.get("thought_signature")` を読み取り、生成する `types.Part` (テキスト) に乗せる

**SEA runtime (`sea/runtime_llm.py`):**

- LLM ノード処理: text-only レスポンスでも `state["_last_thought_signature"]` をセット (現状は Function Calling 経路のみ)
- `_assistant_msg` 構築 (line 2728-): `_thought_sig` が text 経路で来た場合は message dict のトップレベル `thought_signature` に入れる (`tool_calls[].thought_signature` ではなく)

**SAIMemoryAdapter (`saiverse_memory/adapter.py`):**

- `_append_message`: `thought_signature = message.get("thought_signature")` を追加、`add_message` に渡す
- `_payload_from_message_locked`: `msg.thought_signature` が非 None なら payload に含める

**sai_memory/memory/storage.py:**

- `Message` dataclass に `thought_signature: Optional[str] = None`
- `add_message`: `thought_signature` キーワード引数追加
- INSERT 文と SELECT 文の列リストに `thought_signature` を追加
- `init_db` で `_ensure_column(conn, "messages", "thought_signature", "TEXT")` を呼ぶ

### 3.4. 不変条件

- 保存される `thought_signature` は **Gemini 由来** のみ。他 Provider が同名フィールドを使う場合は別カラムを追加するか、Provider 識別タグを別途持つ (将来検討)
- thought_signature は **opaque な値** として扱う。中身を解釈・分解・部分使用しない
- ターン跨ぎで signature が消失した場合: warning ログを出し、リクエストは続行 (品質低下を許容)
- 1 メッセージにつき 0 個または 1 個。複数 signature の concat は行わない。ストリーミング応答では通常 1 つだけ送られてくる想定だが、Gemini 公式 doc が「最終チャンクの空 text part に signature だけ乗ることがある」と明記している以上、複数 chunk に signature が現れた場合は **最後に受信した非 None 値を採用する**

### 3.5. UI パラメータの命名とモデル世代での出し分け (2026-07-09 追記)

ユーザーがモデル JSON の `parameters` から操作できるスイッチは実体としては **thought_signature を次ターンに echo するか否か**の一択であり、モデル側の「マルチターン推論そのもの」を on/off する API パラメータは存在しない。しかし当初この UI 項目を全 Gemini モデルで `multi_turn_thinking` (ラベル "Multi-turn Thinking") と名付けていたため、名前が実態と食い違うモデルが生じていた。

食い違いの原因は、公式仕様上「マルチターン推論 = 全ターンの推論文脈を引き継ぐ」挙動が **GenerateContent API では Gemini 3.5 Flash から**である点 ([whats-new-gemini-3.5](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.5))。SAIVerse は `generate_content` / `generate_content_stream` 経路のみ使用するため、3.5 未満 (Gemini 3 / 3.1 / 2.5 系) では signature を echo しても「全ターン推論の引き継ぎ」は起きず、echo が効くのは主に function calling の連続性維持のみ。よって:

- **3.5 系**: UI キーは `multi_turn_thinking` のまま (名前が実態に合う)。**default は `off`** — 全ターン推論の引き継ぎはトークン/コストへの悪影響が大きいため
- **3.5 未満 (3 / 3.1 / 2.5 系)**: UI キーを `thought_signature_echo` (ラベル「思考署名の引き継ぎ」) にリネーム。**default は `on`**
- コード側 `configure_parameters` (`gemini.py`) は両キーを受けて同一の `_multi_turn_thinking` フラグに落とす (後方互換)

**スイッチの非対称性** (要注意): `_multi_turn_thinking` が off でも、Gemini 3.x 系は function_call パートの signature を常に echo する (`gemini.py` の `if self._multi_turn_thinking or self._is_gemini_3x:`)。off で止まるのは text パートの signature echo (`gemini.py` の `if g_role == "model" and self._multi_turn_thinking:`) のみ。これは「3.x は function calling で signature を返さないと 400 になる」公式仕様に対する安全側の設計。UI の description もこの非対称を明記している。

---

## 4. 既存実装との整合

- **`pulse_logs` テーブル**: 影響なし。pulse_logs は別経路で動いており、thought_signature の保存対象外
- **Building log JSON**: 当面影響なし。messages テーブル経路のみで signature を保持し、building log への波及は将来検討 (= Building Memory DB 化案 `project_building_memory_db_proposal.md` と並走時に再検討)
- **他 Provider クライアント (OpenAI / Anthropic / xAI / Ollama / llama_cpp / Codex)**: 影響なし。base クラスの default 実装が None を返すため
- **Function Calling 既存経路** (`gemini.py:1527-1529`, `runtime_llm.py:2724-2727`): そのまま維持。今回は text 経路のみを追加する

---

## 5. SDK バージョン要件

`google-genai>=1.75.0,<2.0` を要求する。

**判断軸:**

- 必須機能 (`Part.thought_signature`, `FunctionResponse.id`, `ThinkingLevel.{MINIMAL,LOW,MEDIUM,HIGH}`) は 1.56.0 で既に揃う。よって**機能要件上は 1.56.0 でも実装可能**
- ただし 1.56.0 リリースは 2026-01 で、Gemini 3.5 Flash 公式リリース (2026-05) まで半年分の bug fix を取り込めていない (1.57〜1.75 で 19 マイナー版分)
- 1.x 系最終版 1.75.0 (2026-05-04) は破壊的変更なしで Gemini 3.x 系の安定性向上を取り込める
- 2.x 系メジャー更新 (2.0.0〜2.4.0) は **Interactions API の破壊的変更** を含む。SAIVerse は `generate_content` / `generate_content_stream` 経路のみ使用しており Interactions API は無関係だが、検証コストは本タスクのスコープ外なので別タスクで対応する

---

## 6. マイグレーション

- **既存 DB**: `_ensure_column` で `thought_signature TEXT` を idempotent に追加 (NULL 許容なので既存行は影響なし)
- **既存メッセージ**: thought_signature = NULL のまま。次ターンで品質低下が 1 回起きる可能性はあるが許容範囲
- **ロールバック**: カラムを残したまま読み書き経路 (Gemini client / adapter / runtime) を無効化することで実質ロールバック可能。カラム自体の DROP は不要 (NULL 列が残るだけ)

---

## 7. 開発・検証

### 検証シナリオ

1. **保存検証**: Gemini 3.5 Flash で text-only 応答を生成 → DB に signature が保存されているか SQL で確認 (`SELECT id, thought_signature FROM messages WHERE thought_signature IS NOT NULL LIMIT 5;`)
2. **復元検証**: ターン跨ぎで会話 → 2 ターン目のリクエストペイロードに signature が乗っているか `llm_io.log` で確認
3. **品質確認**: Gemini 3.5 Flash で長期セッション → signature 保持有無での思考品質を比較 (主観評価でも可)
4. **互換性検証**: Gemini 2.5 系 / 他 Provider が signature 不使用でも従来通り動作することを確認

### 検証外 (現時点では対象外)

- パフォーマンス測定 (signature 文字列長による DB サイズ影響)
- migration の大規模 DB での実行時間 (`_ensure_column` は ALTER 文 1 本のみなので軽量と判断)

---

## 8. 将来拡張の余地

本実装が安定化した後、以下の方向に拡張する余地を残す:

- **`tool_calls` 専用カラム追加**: SAIVerse が将来 native function calling を採用した場合、本実装と同じ `_ensure_column` パターンで `tool_calls TEXT` を追加する
- **Anthropic thinking blocks 永続化**: 別カラム (`anthropic_thinking_blocks TEXT` 等) として独立追加
- **Provider 統一抽象化**: 3 種類以上の Provider 固有 thinking 値が溜まった時点で、`provider_artifacts JSON` 形式への移行を検討
