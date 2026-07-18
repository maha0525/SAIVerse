# W1 (実行台帳 Phase 1 — 判断点) セッション走行メモ (2026-07-19)

**用途**: このセッション (Fable/メティス) の確定設計と委譲・検収の再開点。工程の真実は [完了計画書](../overview/audit_remediation_plan.md)、finding の真実はレビュー台帳と各監査文書。ここはセッション固有の走行メモ。

**スコープ** (計画書 W1): A2 / A7 / A8 / A9 / A11 + intent §11 小物確定 + post_session×digest 統合 ((a') = 体験の構造 工程(1)) + episode 読み口スペル新設。

**進行**: 調査 (Explore 済み) → 設計 (本書 = 確定) → 実装 (Chunk A→B→C を general-purpose に順次委譲、並列なし) → 検収 (メイン) → コミット。

---

## 調査で確定した現状 (要点のみ、行番号は 2026-07-19 時点)

- 発火の単一ゲート = `autonomy_wiring.fire_judgment_point` (:202-303)。判断点 lock = MetaLayer per-persona Lock 共有。precondition 再判定は lock 取得後 (:264-280) だが **watchdog 側にしか無い** — `handle_scheduled_judgment:439` は precondition なしで発火 (= A2 の核)。
- ライフ境界副作用 (day_open/`_handle_life_end`) は lock 内・run_judgment_point の**前** (:282-287)。judgment_pulses 記帳は `result["submitted"]` 真のとき (:294-302)。
- `run_judgment_point` (judgment_points.py:1714-1825) は `submit_meta_judgment` が例外を投げた時だけ失敗、正常 return は無条件 `submitted:True` (:1824)。**`_submit_meta_lane` (pulse_controller.py:493-541) が LLMError 以外の例外を `[]` に変換するため例外が届かない** (= A7 の核)。submit は同期 (submit→_submit_meta_lane→_do_execute)。
- finalize (builtin_data/tools/judgment_finalize.py): 世界更新 (:1547-1571 で kind 分岐) → SAIMemory 保存 (:1576-1613、**try で握り潰し、committed は保存前 :1580 に確定**= A8) → judgment_applied callback (:1628-1643)。`_apply_task_verdict:610-623` が `update_task_status`→`append_artifact_ref` を**別々の独立 Session/commit** で呼ぶ (= A9)。`_fire_spell:85-114` は失敗も spells_record に積み、`_spell_to_text` が result 無視で正準形化・committed 化 (= A11)。
- post_session digest: `sea/work_session.py._generate_digest:623-660` が**専用 1 コール** (WORKER と同じ軽量クライアント) → main_line committed に `DIGEST_TAG` で保存 (:399-410) → `digest_ref=message:{id}` で episode close (:417-422)。セッション原本 = `RAW_LOG_TAG` の volatile 行として memory.db に実在、`origin_episode` は **metadata JSON のみ** (sea/runtime.py:1583-1606 で自動付与)。origin_episode で引く読み口は**存在しない** (書き込み専用)。
- 状況文の「ダイジェスト:」欄 = `build_post_session_situation_text:1103,1137-1138`。day_close の digest 収集 `_collect_today_session_digests:1281-1313` は adapter から `DIGEST_TAG` committed を読む (統合後も形が保たれれば無傷)。
- 台帳 Phase 0: `begin_execution` は冪等 INSERT のみ (「既に走った/走っているの判定は kind 別回収規則 = Phase 1 の仕事」と明記済み)。wiring の回復 tick に #2 (prepared 回収) の席が予約済み。`saimemory.append` / `perception.push` ハンドラ実装済み (冪等キー刻印は adapter 側 `append_ledger_message` / `push_ledger_perception`)。
- 営業日の非対称: day_open は `clock.now().date()` (`_confirm_life_at_day_open`:322)、day_close は `effective_plan_date` (:1620-1640)。
- 列昇格の前例: `sai_memory/memory/storage.py` の `origin_track_id` (`_ensure_column`:118 + index:139)。clips には `origin_episode_ref` 専用列が既にある。

## 確定設計 (私=Fable の裁定)

### D1. kind と冪等キー (A2)

| kind | idempotency_key | 備考 |
|---|---|---|
| `judgment.day_open` | `{persona}:{plan_date}` | plan_date = `_confirm_life_at_day_open` と同源 (`clock.now().date()`)。ヘルパー一本化 |
| `judgment.day_close` | `{persona}:{effective_plan_date}` | 営業日 |
| `judgment.post_session` | `{persona}:{episode_ref}` | セッション episode が自然な境界 |
| `judgment.post_conversation` | `{persona}:{episode_ref}` | episode_ref 無しは None (一意性なし) |
| `judgment.on_event` | None | 毎イベント新規行。**prepared 行が durable queue** (A7) |

深夜跨ぎ (起床 23:30 定刻 vs 日付変更後 watchdog) で day_open キーが割れる余地は既知の限界として引き受け、plan date 意味論は W2 (A1 全置換) の領分。

### D2. 台帳 Phase 1 API — `claim_execution`

`ExecutionLedger.claim_execution(kind, idempotency_key, persona_id, payload)` → `(execution_id | None, runnable, existing_status)`:

- 既存なし → prepared INSERT → runnable
- 既存 prepared → その行を再利用して runnable (payload は元の凍結値を維持)
- 既存 failed → キーを退避 (`{key}#failed-{id[:8]}` に UPDATE) して新規 prepared → runnable。failed は副作用ゼロ保証なので再実行安全 (intent §2.1)
- 既存 running / applied / completed → runnable=False (既に走った/走っている = A2 の重複抑止)
- 既存 unknown → runnable=False (**自動再実行禁止**。裁定まで当日の同種判断はブロック — 観測面は list_unknown。安全側に倒す設計判断)

debug 明示発火は `force=True` → `idempotency_key=None` で常に新規 (「ユーザーが明示した再編成」の口)。

### D3. fire_judgment_point の結線 (A2)

lock 内・**precondition と境界副作用より前**に claim。runnable=False → `{"submitted": False, "reason": "duplicate:<status>"}` で即 return (境界副作用・tail 通知も走らない — day_close の毎回再実行も同時に閉じる)。precondition 失敗 → `mark_failed("precondition")`。context は `_serialize_judgment_context` で JSON 化して payload に凍結 (WorkSessionResult → dict)。`resume_execution_id` 引数を追加 (回復 refire 用: claim をスキップして既存 prepared を使う)。

### D4. run_judgment_point の証跡ベース成功判定 (A7)

- `execution_id` を受け取り、`mark_running` **後**に submit (不変条件 1)。execution_id は judgment_context JSON に同乗させ finalize へ届ける (playbook JSON は不変)。
- `_submit_meta_lane` は例外を `[]` に変換するのを**やめる**: BeatGateClosedError / ExecutionCancelledException / 汎用 Exception とも、error event を event_callback に通知した上で**再送出** (LLMError は現状どおり素通し)。メタ判断レーン以外 (`_execute_unlocked`) は触らない。
- run_judgment_point の分類: BeatGateClosedError → `mark_failed` (LLM 未着手・副作用ゼロ、refire 安全) / LLMError → `mark_failed` (出力なし=適用前) / その他 Exception・Cancelled → `mark_unknown` (LLM が動いたか不明)。いずれも `submitted: False`。
- 正常 return 後: 台帳 status を読む — applied/completed → `submitted: True` / running のまま → **finalize 証跡なし** → `mark_unknown("no finalize evidence")` + `submitted: False` / failed → `submitted: False`。「成功 = finalize 完了の永続証跡」(A7 修正方針) の実装形。
- 戻り dict に `execution_id` を追加。台帳の無い環境 (旧テストスタブ) は WARN + 従来挙動に degrade。

### D5. on_event の durable queue と prepared 回収規則 (§11-4 確定)

`handle_external_event` は submitted=False で従来どおり直接応対へ fallback (A7 修正で初めて実際に発動する)。fallback 済みの行は failed/unknown 終端なので refire されない — **回復 #2 の refire 対象は prepared のみ** (プロセス死・scheduler drop で走り出せなかったもの。fallback も起きていないので二重応対なし)。refire は一度 running に入れば terminal に落ちるため、試行回数の管理は不要。

wiring `_recovery_tick` に #2 を実装 (kind 別規則、「行動を生む」ので手動モード persona はスキップ):

| kind | prepared 回収 | 期限 |
|---|---|---|
| judgment.on_event | 120 秒経過で refire (`fire_judgment_point(resume_execution_id=...)`) | refire 失敗は terminal に落ちて終わり |
| judgment.post_session | 120 秒経過で refire (digest が判断に依存するため回収価値が高い) | 同上 |
| judgment.day_open / day_close | refire なし (watchdog が自然再発火 → claim が failed キーを退避して回る) | 30 分で `mark_failed("expired")` |
| judgment.post_conversation | refire なし (会話の瞬間は過ぎている) | 30 分で `mark_failed("expired")` |

### D6. finalize の台帳化 (A8)

- 入口で台帳 status 検査: running でなければ「適用済み/終端」として世界更新せず return — **実行単位の冪等** (再 finalize の二重適用の口を閉じる。副作用個別の冪等化はここでは負わない)。
- 世界更新後、SAIMemory 直書きを廃止し `mark_applied(execution_id, result=RESULT_JSON, outbox_items=[判断行], deliver=True)` へ。判断行 payload は現行と同形 (role/line_role/scope/tags/judgment meta/paired_action_text/タイムスタンプ) を凍結。配送失敗は pending に残り関所/回復 tick が引き継ぐ (「適用済み・記録待ち」が正式な状態になる)。adapter/persona 不在も握り潰しから pending→dead (人裁定) の正規経路へ。
- **RESULT_JSON 標準 (§11-1 確定)**: `{"kind", "committed", "scope", "spells": {"attempted","succeeded","failed"}, "warnings": n}` + kind 固有 (`reaction` = on_event / `episode_ref` = post_session)。照合 (#5) と呼び出し側読み出しに足る最小。
- `handle_external_event` の reaction 読みは callback 優先 + 台帳 RESULT_JSON フォールバック (callback 消失でも二重応対せず読める)。

### D7. A9 — `complete_with_artifact`

`PersonaTaskManager.complete_with_artifact(task_ref, artifact_ref, execution_id)`: 完了 status + completed_at + artifact_refs 追記 + 両履歴行 (履歴に execution_id 記録) を**単一 Session/commit**。既に completed でも**同一 execution が artifact 未記帳なら補修だけ許可** (履歴の execution_id で判定)。`_apply_task_verdict` の 2 連呼びを置換。

### D8. A11 — SpellOutcome

- `_fire_spell` は成功/失敗を戻り値で表明 (`{"name","args","success","result"}`)。
- 本人記憶の判断行には**成功 spell だけ**正準 `/spell` 形で載せる。失敗はシステム名義の適用失敗通知として outbox `perception.push` (`kind="judgment_apply_failure"`, 客観+丁寧語) — 本人の行為に変換しない (不変条件 7 / [[feedback_no_assistant_impersonation_in_memory]])。
- `committed` / scope への寄与は成功 spell のみ。summary と callback は attempted/succeeded/failed を分離。

### D9. digest 統合 ((a') = 体験の構造 工程(1)、まはー裁定 2026-07-18 準拠)

1. `work_session._generate_digest` / `_build_digest_prompt` / digest 保存ブロックを**削除** (専用コール廃止、コール数 3→2)。`WorkSessionResult.digest` フィールドも削除。episode close は work_session に残るが `digest_ref=None` で閉じる。
2. 状況文: 「ダイジェスト:」欄 → 「セッションの記録 (原本):」。原本 = memory.db から `origin_episode == episode_ref` の行 (D10 の新読み口) を時系列レンダリング。**文字数上限なし** (セッションが走れた時点でサイズ実証済み)。
3. **コールローカル注入**: LLM に渡す situation_text だけが原本を含む。保存側 (paired_action_text) は原本を除き「episode 参照 + /spell episode_read で原本に到達できる」旨の一行を持つ別携帯 (`judgment_context.paired_situation_text`)。
4. post_session response_schema に `digest` (string, required) を追加。指示部に出典の規律 (実際に起きたことだけ) を継承。
5. finalize (post_session): digest を outbox の**第 1 項目**として積む — 新 target `saimemory.append_digest`。ハンドラ (wiring): 冪等 append (DIGEST_TAG / main_line / committed / ws_meta metadata / origin_episode) → 配送成功時に `episodes.set_digest_ref(episode_ref, "message:{id}")` (world DB、新設の小関数、冪等)。判断行は第 2 項目 (FIFO で digest が先)。配送までの間 episode は digest_ref NULL = 「適用済み・記録待ち」の観測可能状態。
6. day_close `_collect_today_session_digests` は無傷 (DIGEST_TAG committed の形を保存)。
7. 引き受ける歪み: post_session 判断が期限切れで死ぬと digest が生まれない (D5 の refire で最小化。原本はスペルで到達可能、digest_ref NULL が観測面)。

### D10. episode 読み口スペル (a')

- `origin_episode` を messages の**専用列に昇格** (`_ensure_column` + index、origin_track_id と同型)。追加時に一度だけ `json_extract(metadata,'$.origin_episode')` でバックフィル。書き込みは adapter が metadata から列へ転記 (書き手は不変)。
- adapter 新メソッド `get_messages_by_origin_episode(episode_ref)` (id 昇順、フィルタ厳密)。
- 新 native tool `builtin_data/tools/episode_read.py` (`spell=True`): 引数 `episode` (`episode:N`)。world DB の episode 行 (title/kind/期間) + 原本行を全文レンダリング (切り詰めなし — これ自体が「全文到達手段」)。戻りテキストは客観+丁寧語。

### 実装 Chunk (順次委譲、並列なし)

- **Chunk A (基盤+入口)**: D1〜D5。ledger claim_execution / pulse_controller 再送出 / fire・run_judgment_point 結線 / handle_external_event / wiring #2。回帰: A2 両順序 dedup、A7 各段階 (runtime 例外→submitted False + unknown 行 / on_event fallback 一度だけ / 偽成功 drop なし)、refire 規則。
- **Chunk B (finalize)**: D6〜D8。judgment_finalize 台帳化 / complete_with_artifact / SpellOutcome。回帰: A8 (保存失敗→pending 残存・applied 維持・再配送一回・再 finalize 無効)、A9 (全か無か・補修許可)、A11 (失敗 spell 不記載・システム通知・scope)。
- **Chunk C (digest+スペル)**: D9〜D10。回帰: 原本注入コールローカル (paired に原本なし)、digest 必須化、digest_ref 遅延確定、episode_read、origin_episode 列/バックフィル。
- **Docs (メイン)**: execution_ledger.md §11 消し込み+ステータス / judgment_points.md §6 改定反映 / experience_structure.md 工程(1) 済みマーク / 計画書 W1 状態 / レビュー台帳消し込み / in_flight。

### 検収チェックリスト (メイン)

- [ ] Chunk A: claim が境界副作用より前か / _submit_meta_lane の再送出がメタレーン限定か / degrade 経路 (台帳なし manager) で既存テストが割れないか
- [ ] Chunk B: finalize の running 検査が旧経路 (台帳なし) を殺していないか / outbox payload の判断行が現行と同形か (タグ・paired_action・tz)
- [ ] Chunk C: day_close の digest 収集が生きているか / RAW_LOG_TAG 以外 (締め発話) も原本に含まれるか / スペルの spell=True・visible
- [ ] 全体スイート (基準 2591 passed / avatar 除外) + ruff
