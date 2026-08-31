# 改訂履歴

**親**: [README.md](README.md)

旧 `persona_cognitive_model.md` (Intent A) と `persona_action_tracks.md` (Intent B) の v0.1〜v0.14 改訂差分を集約する。確定文書 (`01_concepts.md` / `02_mechanics.md` / `03_data_model.md` / `04_handlers.md`) からは「v0.X で確定」「v0.Y で改訂」等の差分情報を取り除き、ここに集約する。

設計判断の経緯 (なぜそう変えたか) を追跡する目的。

---

## Intent A: persona_cognitive_model.md の改訂

### v0.35 (2026-05-09) — メタ判断 Pulse の tool 結果到達バグ修正 + Phase 3 段階 4-D 完了

v0.34 の動作観察で、メタ判断 Pulse の `judge` ノードが `fetch_tracks` (= track_list ツール) の結果を一切受け取れていない構造的バグが発覚。tool ノードが `state["_messages"]` を更新せず PulseContext のみ更新していたため、`context_profile` を持たない LLM ノードでは tool 結果が見えなかった。同種バグは subplay 経路で 2026-04-28 に既に修正済み (tool ノードは取り残されていた)。

並行して、Phase 3 段階 4-D で先送りされていた旧 DEPRECATED コード削除を完了。

#### 1. tool 結果到達バグの修正

- `sea/runtime_engine.py:lg_tool_node` の tool 実行成功 / 失敗ブロックの両方で、`state["_messages"]` に `<system>tool '{tool_name}' result:\n{result_str}</system>` 形式の user メッセージを append する経路を追加。subplay の `report_to_parent` 修正 (`sea/runtime_nodes.py:229-263`) と同じパターン。
- これにより、`meta_judgment.json` の `judge` ノードが `last_message_relative` 等を含む Track 一覧 JSON を実際に読めるようになる。

#### 2. Phase 3 段階 4-D 完了

「`context_profile` は旧仕様」とまはー指摘 → 段階 4-D の完遂に移行。実装範囲は [`docs/issues/archive/phase3_4d_dead_code_removal.md`](../../issues/archive/phase3_4d_dead_code_removal.md) のログ参照。要点:

- **削除**: `LLMNodeDef.context_profile` / `LLMNodeDef.model_type` / `CONTEXT_PROFILES` / `ContextRequirements.include_internal` / `exclude_pulse_id` 全層 (4 関数) / `pulse:{uuid}` タグ併行記録 / `_warn_once_legacy_field`。
- **集約**: `runtime_llm.py:lg_llm_node` の base_msgs は `state["_messages"]` のみを source of truth に。`runtime.py:_select_llm_client` は `_force_lightweight_model` フラグのみで判断。
- **残置**: `_FULL_CONTEXT_REQUIREMENTS` は Playbook 既定値として `runtime_runner.py` で実用継続。`get_history` ツールの `include_internal` 引数は別物 (= ツール仕様、search/recall 互換) のため残置。
- **Playbook 整理**: ノード単位混在/部分指定の 8 件 (Pattern A: source_web/pdf/messagelog/memopedia/document/chronicle、Pattern C: deep_research_playbook/memopedia_write_playbook) + research オーケストレーター 2 件 (research_task / memory_research) を `builtin_data/playbooks/archive/` に移動。**必要時に作り直す方針** (Spell で代替できる + 自律行動として最適化したい意図)。
- **残置 Playbook**: `autonomy_creation` / `autonomy_memory_organization` / `autonomy_web_research` の 3 件 (全 LLM ノードが lightweight)。Pydantic extra='ignore' で `model_type` フィールドは無視されるため触らず、将来の自律行動再構築時に整理する。
- **テスト**: 785 件全 pass。ruff clean。

#### 設計判断

- **ノード単位の軽量モデル指定は新仕様に組み込まない**: 旧 `model_type` 相当のフィールドを `use_lightweight: bool` 等で復活させる選択肢もあったが、Pattern A/C の混在は Playbook 設計として「キャッシュを考えない非効率」だった。再構築時には `SubPlayNodeDef.line` の一括指定で代替するか、Spell ベースで分解する方が自然。
- **メタ判断 prompt の改修は依然必要**: 本改訂は tool 結果が判断側に「物理的に届く」基盤の修正。`last_message_relative` の客観情報があっても判断側が読まない症状 (= 過去独白を真実と誤認) への対症療法は別途必要 (handoff_2026-05-09 §5 注意点参照)。
- **「context_profile は旧版」を半年放置していた負債**: まはー指摘で「古い話を引きずる」リスクが顕在化。今後同種の旧仕様コードは残さない方針。

#### 関連リソース

- [`handoff_2026-05-09.md`](handoff_2026-05-09.md) — 元起点の handoff
- [`docs/issues/archive/phase3_4d_dead_code_removal.md`](../../issues/archive/phase3_4d_dead_code_removal.md) — 4-D 削除内訳のログ
- v0.34 (本ファイル) — wait_response timeout / Track 最終メッセージ時間可視化

---

### v0.34 (2026-05-09) — wait_response Track 自動 pause タイマー + Track 最終メッセージ時間の可視化

[`handoff_2026-05-09.md`](handoff_2026-05-09.md) を起点にした 2 件の運用改善。Track Chronicle (v0.32) + ユーザー会話 Track 親保持機構 (v0.33) の commit 後に動作確認していて発覚した「自律稼働中に何らかの拍子でユーザー会話 Track が active 化したまま長期 idle に陥り、メタ判断 Pulse が `post_complete_behavior=='wait_response'` 抑止で止まり続ける」症状への対処。

#### 背景

`MetaLayer.should_fire` / `on_periodic_tick` は対話 UX 保護 (= ユーザー応答中にメタ判断で別 Track に勝手に切り替える事故を防ぐ) のため、running Track の Handler が `wait_response` ならメタ判断を skip する。設計判断としては正しいが、長期 idle 時の脱出経路が無いため、ペルソナが自分で `track_activate` した user_conversation Track が応答無しのまま放置されると、自律稼働がそのまま停止する。

加えて副次的に、過去のメタ判断ログ独白 ("Track は running 状態で安定している" 等) を「現在の事実」として誤認する症状も観察された。Track ごとの最終メッセージ時間の客観情報がメタ判断 prompt に出ていないことが要因の 1 つ。

#### 1. wait_response Track の自動 pause タイマー

「`post_complete_behavior=='wait_response'` 全般」を対象に、N 分 idle で自動 pending 落とし + メタ判断 Pulse 発火する機構を追加。

- ペルソナ別設定 `AI.USER_CONV_TIMEOUT_MINUTES` (Integer, NULL=既定値 30 分)。軽量モデルと重量級モデルで「自然な対話の間」が違うため環境変数ではなくペルソナ別。
- TrackManager に `wait_response_timeout_provider` (= `track -> (minutes, last_msg_time)?`) と `wait_response_timeout_callback` (= `(persona_id, track_id) -> None`) を注入し、SAIVerseManager が両者を実装。Handler 解決 / AI 行参照 / SAIMemory 参照は SAIVerseManager 側のみ、TrackManager は EventScheduler への push と再評価ロジックだけを持つ。
- タイマー基準時刻 = SAIMemory の `MAX(messages.created_at) WHERE origin_track_id=...` (= Track 紐付きメッセージの最新)。メッセージ無しの新規 Track は `datetime.now()` にフォールバック (= activate 直後の即時タイムアウト事故を防ぐ)。
- 発火時に再評価 (Track 状態 + idle 時間) → 条件未達なら残り時間で再 push、条件成立なら `pause` → callback。callback は `MetaLayer.on_periodic_tick(trigger='wait_response_timeout')` を発火 + AutonomyManager の次回 tick を `now + interval` に押し戻す (二重発火回避)。
- 状態遷移時のキャンセル: pause / wait / complete / abort / activate 経由の自動 pending 押し出し でタイマー解除。set_alert は running → no-op なので解除不要。

#### 2. Track 最終メッセージ時間の可視化

メタ判断 prompt 内の Track 一覧 + UI Tracks Viewer に「最終メッセージから N 時間経過」の客観情報を注入。過去独白を真実と誤認する症状の対症療法。

- `SAIMemoryAdapter.get_track_last_message_time(track_id)` / `get_track_last_message_times([track_ids])` を追加 (origin_track_id でフィルタ + MAX(created_at))。
- API: `/api/people/{id}/tracks` レスポンスの `TrackItem` に `last_message_at` フィールド追加。N+1 回避のため bulk 取得経路で実装。
- UI: TracksViewer のヘッダ時刻表示を `last_active_at` から `last_message_at` の相対表記に変更 (絶対時刻は title 属性で hover 表示)。詳細展開時は両方を絶対 + 相対併記。
- メタ判断 prompt: `track_list` ツール出力 (= `meta_judgment.json` の `fetch_tracks` ノード) に `last_message_at` (ISO) + `last_message_relative` (例: "3時間前") を追加。判断側の prompt 改善 (「過去ログより現在の Track 状態を信頼せよ」明示) は本改訂のスコープ外。

#### 設計判断

- ペルソナがメタ判断 spell で user_conversation Track を `track_activate` する経路は塞がない (= 「自分から話しかける」未来拡張のため)。あくまで **activate された後の脱出経路** を作る方針。
- タイムアウト発火時の挙動は **pending 落とし + メタ判断発火**。単に pending に落とすだけだと自律稼働再開のトリガが無い。タイムアウト = 「今この Track が終わったので次を判断すべき」という能動的状態変化、と解釈。
- 対象 Track は wait_response 全般 (Track 種別ハードコードでなく属性ベース)。将来の他応答待ち型 Track も自動カバー。

#### 実装

- 新規/拡張:
  - `database/models.py:AI` に `USER_CONV_TIMEOUT_MINUTES` カラム追加 (migrate.py の auto-detect 経路で migration 自動)
  - `saiverse_memory/adapter.py` に `get_track_last_message_time` / `get_track_last_message_times` 追加
  - `saiverse/track_manager.py` に `_wait_response_timeout_*` 一連 + provider/callback 注入経路追加
  - `saiverse/saiverse_manager.py` に `_wait_response_timeout_provider` / `_wait_response_timeout_callback` を実装し TrackManager に注入
  - `saiverse/autonomy_manager.py` に `defer_next_tick` (out-of-band trigger 後の重複発火回避) 追加
- 変更:
  - `api/routes/people/tracks.py` / `models.py` で `last_message_at` を bulk 取得経路で配信
  - `frontend/src/components/memory/TracksViewer.tsx` で表示行追加 + 相対時刻ヘルパ
  - `builtin_data/tools/track_list.py` で出力 JSON に `last_message_at` / `last_message_relative` 追加
  - `frontend/src/components/SettingsModal.tsx` に「応答待ち Track 自動 pause 閾値」入力 UI 追加
  - `manager/admin.py` / `saiverse/saiverse_manager.py` の `update_ai` シグネチャに `user_conv_timeout_minutes` 追加 (0/負値 → NULL = 既定値運用に倒す)
- テスト: `tests/test_track_manager.py` に wait_response timeout 用ケース 6 件追加 (schedule / no-op / cancel-on-pause / cancel-on-other-activate / fire+callback / re-schedule-when-not-idle-enough)

---

### v0.33 (2026-05-09) — ユーザー会話 Track の親スレッド保持機構を追加

Track Chronicle の実装着手後、運用観点でまはー (ユーザー) が指摘した重大課題への対応。Track Chronicle 全 Track 一律適用だと、ユーザー会話 Track の「対話の温度感」が作業遂行型サマリで失われる + 自律稼働で長く動いた後にユーザー会話に戻った時に**生メッセージが 1 件もコンテキストに無い**状況が発生し得る → SAIVerse の中心需要 (人格の安定性) に対して致命的。

#### 本質の整理

Stelis 親子スレッドのメタファーを流用:

- **ユーザー会話 Track = 親スレッド** (常時保持、生メッセージで温度感を伝える)
- **その他 Track = 子 Stelis スレッド** (メインラインで入れ替わる)

「Track Chronicle で補う」「Track 全 Chronicle を head に列挙」「アクティブ以外スキップ」のいずれの案でも届かない問題で、**生メッセージを常に一定数保持し続ける独立機構**が必要と確認。

#### 不足分補完方式 (重複回避)

「常に N 件保持」ではなく **「メインラインに既に居る数を数えて不足分だけ補完」** 方式を採用:

- Metabolism 後の history 内のオーナー Track メッセージ数 = existing_count
- 不足 (target_count - existing_count) > 0 のときだけ、history 最古より過去のメッセージを needed 件取得して上部補完
- アクティブが user_conversation のときは history 内に十分あるので補完省略 (= 重複回避)
- 自律稼働中はオーナー Track メッセージが history に居ないので、最低 target_count 件補完される

#### オーナーユーザー会話 Track の特定

リンクユーザー (UserAiLink) で persona に紐付くユーザーの user_conversation Track を採用。リンク未設定なら最古の user_conversation Track を自動的にオーナー扱い。

#### コンテキスト構成 (改訂)

```
[head]
  system prompt
  Memory Weave (General Chronicle / Memopedia / Track Chronicle (アクティブ ≠ user_conv のとき))
  visual context

[親スレッド保持セクション (アクティブが user_conv 以外のとき)]
  時刻アンカー①  <system>以下、{ts} 以降のユーザーとの会話です</system>
  オーナー Track の不足分補完メッセージ

[メインライン履歴]
  時刻アンカー②  <system>以下、{ts} 以降のやり取りです</system>
  history (line_role=main_line, scope=committed)

[末尾]
  realtime info
```

#### ユーザー会話 Track Chronicle 化のスキップ

親保持機構があるため、ユーザー会話 Track は Track Chronicle 対象外。3 箇所の判定追加:

- `_generate_track_chronicle` ループ内で `track_type='user_conversation'` をスキップ
- `_get_track_chronicle_context` で active が user_conversation なら "" 返す
- `_insert_track_chronicle_on_switch` で切り替え先が user_conversation なら早期 return

#### パラメータ

- `SAIVERSE_USER_CONV_PRESERVE_COUNT` (環境変数、デフォルト 20、ペルソナ別設定なし)

#### 実装

- 新規: `saiverse/user_conversation_preserver.py` — オーナー Track 特定 + 不足分補完取得
- 既存: `sea/runtime_context.py` の `prepare_context` 内で history 取得後に補完 + 時刻アンカー①/② 挿入
- 関連: messages テーブルの payload に `origin_track_id` を含めるよう `Message` dataclass + `_LINE_METADATA_COLUMNS` + `_payload_from_message_locked` を更新

#### Intent doc 反映

[`track_chronicle.md`](track_chronicle.md) §11 (新規) に「ユーザー会話 Track の親スレッド保持機構」セクションを追加。実装ガイドとしての完結性を担保。

---

### v0.32 (2026-05-09) — Track Chronicle Intent doc 起草 (pause_summary 完全廃止 + 中断・再開機構の本体化)

v0.31 で「pause_summary 書き込み側を Phase 3 に乗せる」とした書き込み機構について、設計を詰める対話の中で本質が大きく拡張された。結論として **pause_summary は完全廃止**し、**Track Chronicle** (Track 内必要情報の維持機構) として再設計する。Intent doc を独立ファイル [`track_chronicle.md`](track_chronicle.md) として起草。

#### 拡張の発端

書き込みの本質を「中断 → 再開時のサマリ書き込み」に閉じて捉えていたが、まはー (ユーザー) との対話で次のように再整理された:

- 書き込みの本質: Metabolism で押し出された内容から必要情報を圧縮保存する
- 読み込みの本質: 現コンテキストに含まれていない必要情報を洗い出してコンテキストに含める
- Track の役割: 「Track 内に居る間は Track 内の過去全情報にアクセスできる」必要情報の基準線

「中断 → 再開」はこのカバー範囲の 1 ケースに過ぎない。同 Track 内で長時間作業して Metabolism が起きた場合、別 Track にしばらく行って戻ってきた場合、すべて同じ機構で必要情報が呼び戻される。

#### 既存実装の現状確認 (重大発見)

設計議論中の精査で、以下が dead code として現役ランタイムに統合されていないと判明:

- `sea/pulse_root_context.py` の `prepare_pulse_root_context` / `build_fixed_section` / `build_dynamic_section` は Phase 1.1 で実装されたが**呼び出し元が無い**
- 結果、`build_dynamic_section` 内で参照される `pause_summary` は**そもそも読み出されていなかった**
- 書き手も無く、読み手も無く、宙ぶらりんで残っていた

これらの dead code は撤去対象。Track Chronicle の実装は legacy `prepare_context` (`runtime_context.py`) 側に統合する。

#### 新設される設計の核

[`track_chronicle.md`](track_chronicle.md) の §3 全体像参照。要約:

1. **書き込み**: Metabolism 連動。押し出し対象を `origin_track_id` で Track ごとに分けて Chronicle DB (`arasuji_entries` に origin_track_id カラム新設) に entry 追加。バッチサイズ未満は `incomplete: true` フラグ付きで保存し、後で 20 件揃ったら正規 Lv1 に再生成。1000 字未満ならスキップ (読み込み時に SAIMemory から生メッセージ直接取得)。生成は新規関数 `_generate_track_chronicle` として独立経路 (既存 `_generate_chronicle` の制約 = user pulse 限定 / バッチ未満スキップ等を受けない)
2. **読み込み (head)**: アクティブ Track の Chronicle 一式を `get_episode_context` (origin_track_id フィルタ版) で取得し、Memory Weave context として head 配置。Metabolism のたびに head が新アクティブ Track のものに入れ替わる
3. **読み込み (history 末尾近く)**: Track 切り替え時、`_promote_meta_judgment_in_pulse` (`saiverse/saiverse_manager.py:1168`) の延長で、メタ判断独白の committed 昇格直後に切り替え先 Track の Chronicle を独立メッセージ (role='user' + `<system>` ラップ) として INSERT
4. **時刻アンカー**: Metabolism 時、最古残存メッセージ直前に揮発挿入。書式: `<system>以下、YYYY-MM-DD HH:MM:SS 以降のやり取りです</system>`
5. **キャッシュ挙動**: head は Metabolism 時のみ入れ替え、Track 切り替え時は head 不変。詳細は [`track_chronicle.md`](track_chronicle.md) §6 の t1〜t3.5 具体例参照

#### General Chronicle との関係

Track Chronicle と General Chronicle は**独立に走る**。両者は対象範囲も抽出視点も異なるため、同じメッセージから両方が生成されても問題ない (重複 OK)。General Chronicle 側に残る課題:

- 生成 trigger を Metabolism 押し出し対象判定に変更 → [`docs/issues/general_chronicle_metabolism_trigger.md`](../../issues/general_chronicle_metabolism_trigger.md)
- 自律稼働中に Chronicle が生成されない問題 → [`docs/issues/general_chronicle_user_pulse_only.md`](../../issues/general_chronicle_user_pulse_only.md)

これらは Track Chronicle で結果的に Track 単位の必要情報維持は救えるが、General 側の網羅性は別問題として issue 化済み。

#### v0.31 との差分

v0.31 で「Phase 3 の Track 中断・再開機構: `pause_summary` 書き込み側実装」「Phase 5 の時間差ツール基盤」と切り分けたが、書き込み側の本質拡張に伴い前者は **Track Chronicle 本体実装** に置き換わる。後者 (Phase 5 時間差ツール) は変更なし。

#### 廃止 / 撤去対象 (v0.32 で確定)

- `action_tracks.pause_summary` / `pause_summary_updated_at` カラム
- `prepare_pulse_root_context` / `build_fixed_section` / `build_dynamic_section` / `is_first_pulse` / `mark_cache_built` / `reset_cache_built` (dead code)
- `pause_summary` の API 露出 (`api/routes/people/tracks.py:57-58`, `models.py`)
- `pause_summary` の Frontend 表示 (`frontend/src/components/memory/TracksViewer.tsx`)
- `meta_layer.py:28` の「責務外」コメント (v0.31 で残されていたもの、書き込み責務確定により不要)

#### Phase 配置への反映

- Phase 3 進捗表に Track Chronicle 実装の項目を新設 (中断・再開機構の本体)
- README ドキュメント構造に [`track_chronicle.md`](track_chronicle.md) を追加
- v0.31 で Phase 3 に乗せた「Track 中断・再開機構」の項目は Track Chronicle 本体化に書き換え

---

### v0.31 (2026-05-09) — 「待ち」を行動の性質として再整理 + pause_summary を Phase 3 に乗せる

Phase 3 の積み残し (`track_waiting.json` 等) の着手を起点に、まはー (ユーザー)
との対話で「待ち」の本質を整理し直した結果、認知モデル上の位置づけが大きく
変わった。本改訂はその再整理を確定させる。

#### 整理の発端

`track_waiting.json` の着手検討時、「誰がどうやってこの Playbook を使うと決め
るのか」「なぜ `track_autonomous` では駄目なのか」という根本疑問が出た。深掘り
した結果、「待ち」は Track 種別でも特殊状態でもなく、**結果が時間差で返って
くる行動の性質**であることが明確になった。

具体的にいうと:

- ペルソナは「これは時間差で結果が返る行動だ」と予定調和的に認識して呼ぶ
  (途中で突然待ちが入るわけではない)
- 行動 = ツール / Spell / Playbook ノード。これらに「結果が時間差で返る」性質
  がある
- Track の中断は「待ち発生」と独立した別問題。3 つ同時に重い仕事を投げて Track
  を続けることもある。中断するかどうかはメタ判断者の領域
- 結果到達は Track 内のイベントメッセージ。Track が active なら次 Pulse で
  通常の messages として参照され、inactive なら Alert として通知される
- timeout も「結果が来なかった」事象としてイベントメッセージ化される

つまり認知モデル上「待ち」を独立した概念として扱う必要がない。既存の Track /
Alert / メタ判断の枠組みで全部成立する。

#### 廃止される旧仕様 (Phase 3)

| 項目 | 廃止理由 |
|------|---------|
| `track_waiting.json` Playbook | Track 種別ではなく「待ち」を独立 Playbook 化していた誤設計 |
| `STATUS_WAITING` (`saiverse/track_manager.py`) | 状態として独立する根拠がなくなる (pending と区別不要) |
| `track.waiting_for` カラム + 関連 API | 「何を待っているか」はツール / Spell の引数 + 結果イベントで自己記述する |
| `track.waiting_timeout_at` カラム + EventScheduler 予約 | timeout もツール側責務 (= 結果不到達イベント) |
| `TrackManager.wait()` / `resume_from_wait()` メソッド | 状態廃止に伴う |
| Phase 4-e (v0.30) で実装した `_schedule_waiting_timeout` / `_handle_waiting_timeout` | 時間差ツール基盤に移行。本改訂で相殺 |
| `04_handlers.md` の `post_complete_behavior` 表で「waiting」を Track 種別として記述していた箇所 | 概念的に誤りだった |
| `track_type='waiting'` の選択肢 | 同上 |

#### 新設される作業 (Phase 3)

「Track 中断・再開機構」を Phase 3 タスクに昇格。コードベース調査の結果、
読み込み側 (`sea/pulse_root_context.py:271-273` の `build_dynamic_section`) と
DB スキーマ (`action_tracks.pause_summary` / `pause_summary_updated_at`)、
API 露出 (`api/routes/people/tracks.py:57-58`) は実装済みだが、**書き込み側
(中断時に軽量モデルでサマリ生成) が未実装**で、Phase 計画上も浮いていた。

`saiverse/meta_layer.py:28` には「中断時 pause_summary 作成 / 再開コンテキスト
構築 (Phase 1.3 後段 / Phase 2)」と責務外として書かれていたが、Phase 1 / 2 の
ドキュメントには着手記録がない。Phase 3 のスコープ (Track 種別 Playbook 起動
時の Pulse 開始プロンプト構成) にきれいに収まるため、Phase 3 で改めて明示
タスク化した。

| 項目 | 担当 Phase | 状態 |
|------|-----------|------|
| 中断時 pause_summary 生成 (軽量モデル) | Phase 3 | 🔲 未実装 |
| Note 差分の挿入経路 (`build_dynamic_section` 拡張) | Phase 3 | 🔲 未実装 |

#### 新設される作業 (Phase 5)

「時間差ツール基盤」を Phase 5 タスクに新設。`waiting` 機構の代わりとして
時間差で結果が返ってくるツールの汎用基盤を整備する。

| 項目 | 担当 Phase | 状態 |
|------|-----------|------|
| 起動時の識別子発行 (call_id 等) | Phase 5 | 🔲 |
| 完了時に Track にイベントメッセージとして配送 (Track 不在なら Alert) | Phase 5 | 🔲 |
| timeout 自体もイベントメッセージ化 | Phase 5 | 🔲 |
| 並列起動 (3 つ同時投げ等) サポート | Phase 5 | 🔲 |
| 個別ツール (Kitchen / MCP / dispatch / X 等) の汎用基盤への移植 | 別タスク | 🔲 |

#### 文書側の改訂

- `phase_3_lines_playbooks.md`: ステータス 95% → 85%。Track 種別 Playbook 表
  から `track_waiting.json` を打ち消し線で削除。新セクション「Track 中断・
  再開機構」「待ち機構の整理」を追加。完了判定基準にも反映
- `phase_5_autonomy.md`: 「時間差ツール基盤」セクション追加。完了判定基準にも反映
- `02_mechanics.md`:
  - 「軸 2: 起動経路」表の「最初から waiting、外部イベントで起動」を「既存だが
    非アクティブで待機、外部イベントで alert 化」に書き換え
  - `track_wait` スペル言及を削除し Phase 3 廃止予定の注記を追加
  - 「応答待ちの仕組み」章を「応答待ち (時間差ツール基盤)」に改題し全面改稿
    (旧 `waiting_for` 規約 / 多重応答待ち優先順位 / タイムアウト節は廃止して
    新モデルに統一)
- `04_handlers.md`:
  - `post_complete_behavior` 表の `wait_response` 行から「waiting」を削除し
    注記を追加
  - Track 種別 Playbook 一覧から `track_waiting.json` を打ち消し線で削除
- `03_data_model.md`:
  - `action_tracks` スキーマで `waiting_for` / `waiting_timeout_at` を
    Phase 3 削除予定としてコメントアウト + 注記
  - `idx_action_tracks_waiting_timeout` インデックスを削除予定マーク
  - `track_type` の列挙から `waiting` を除外 + 注記
  - 状態遷移図から `waiting` 経路を削除 + 注記
  - 主要遷移トリガー表から `track_wait` / `track_resume_from_wait` 行を削除
  - メタレイヤーのトラック管理ツール群表で同 2 ツールを打ち消し線
  - alert への遷移トリガーから「関連 waiting Track」表現を「関連 Track
    (時間差ツール基盤経由、Phase 5)」に修正、「時間差ツールの結果到達 /
    timeout (Phase 5)」行を新規追加
- `01_concepts.md`:
  - Track 状態リストから `waiting` を削除 + Phase 3 削除予定注記
  - alert 解消ルール表から `waiting` (`wait`) 遷移先を削除 + 注記

#### Phase 4-e との関係

Phase 4-e (v0.30、2026-05-08) で waiting timeout の EventScheduler 化を実装した
直後に本廃止が決まった形。Phase 4-e の実装は時間差ツール基盤に再構成される
方向性なので、丸ごと無駄になるわけではない (push 駆動の予約機構 / fire 後の
alert 通知パターンは活用できる)。

ただし `_schedule_waiting_timeout` / `_handle_waiting_timeout` のような
「Track の `waiting_timeout_at` カラムに紐付く」コードは廃止対象。Phase 3 廃止
作業の中で取り扱う。

### v0.30 (2026-05-08) — Phase 4-e: anchor touch 修正 + EventScheduler 集約 + メタ判断 Pulse 失敗時挙動

Phase 4-e (`phases/phase_4_pulse_scheduler.md` の 4-e サブフェーズ) を完了させる
大規模リファクタ。当初は「メタ判断 Pulse の失敗時リカバリ」だけのつもりだったが、
キャッシュ管理・スケジューラ全体・ペルソナ別パラメータ化まで一気に再構成した。

#### 1. anchor touch を LLM 呼び出し成功後に移動 (Metabolism バグ修正)

**旧**: `sea/runtime_context.py:432-435` (P1) と `:579-581` (P2) で、context 組成
時点で `_update_anchor_for_model` を呼んで `METABOLISM_ANCHORS.updated_at` を
touch していた。

**問題**: 「context 組成は走ったが LLM 呼び出しが失敗した」ケースで updated_at
が前進してしまい、次回 `_resolve_metabolism_anchor` が「TTL 内」と誤判定して、
**実際には切れているキャッシュに対して長大コンテキストを送り直す不整合**を招い
ていた。

**新**: P1/P2 の `_update_anchor_for_model` 呼び出しを削除。代わりに
`sea/runtime.py:_touch_anchor_after_llm_call(persona, usage)` を新設し、
`runtime_llm.py` の各 `usage = llm_client.consume_usage()` 直後 (5 箇所) で
呼ぶ。explicit cache モデル (Anthropic 等) は `cache_read > 0 OR cache_write > 0`
の時だけ touch、両方 0 なら WARN ログを出して touch しない (cache breakpoint
設定ミスや TTL 切れの兆候として観測可能に)。

副作用として、Case 3 fallback 経路で新規 anchor 立てた直後に LLM 失敗した場合、
DB に anchor が永続化されず次回も Case 3 を踏むことになるが、minimal load の
挙動は同じなので致命的でない。

#### 2. `META_JUDGMENT_CONFIG` カラム新設 (ペルソナ別 Pulse パラメータ)

`AI.META_JUDGMENT_CONFIG` (Text/JSON, nullable) カラムを追加。ペルソナごとに
メタ判断 Pulse の挙動を細かく制御できるようにする:

```json
{
  "cache_threshold_ratio": 0.3,        // TTL 残り割合の閾値
  "max_retries": 1,                    // Pulse 失敗時の即時リトライ回数
  "retry_backoff_seconds": 5,          // リトライ間の待機秒数
  "periodic_interval_minutes": 50,     // メタ判断自動発話間隔
  "keep_cache_alive": true             // TTL 接近で前倒し fire するか (低頻度ペルソナ向けに OFF 可能)
}
```

DB 側は NULL のまま運用 (= built-in default を使う)、UI で明示的に指定された
キーだけ JSON に書き込む。デフォルトは `MetaLayer._DEFAULT_JUDGMENT_CONFIG` で
管理し、不正 JSON / 型不一致は WARN ログ + デフォルト fallback で吸収する。

UI: `SettingsModal` の自律行動マネージャー直下に「メタ判断 Pulse 設定」セクション
を新設。各数値項目は空欄=既定値で、`(既定: X)` の副表示を入力欄の右に配置して
意図を明示。`keep_cache_alive` は tri-state select (既定 / ON / OFF) で表現し、
OFF の時はキャッシュ閾値 input を `disabled` 化する。

#### 3. EventScheduler 新設 + コア側ポーリング全廃

`saiverse/event_scheduler.py` を新設。min-heap + `Condition.wait_until` による
時刻指定ディスパッチャ。同 key 上書きは lazy deletion (cancelled フラグ) で実現。
`schedule(fire_at, callback, key)` / `cancel(key)` / `schedule_periodic(...)` API。

**集約対象 (旧: 専用 thread + sleep ループ → 新: EventScheduler に push)**:

| 旧コンポーネント | 新形態 |
|---|---|
| `ScheduleManager._schedule_loop` (60s ポーリング) | API 経由の作成/更新/削除/トグルから直接 push、`_handle_fire` で実行 + 次回再 push |
| `AutonomyManager` per-persona sleep ループ | `_handle_tick` を `_schedule_next_tick` (EventScheduler) で繋ぐ。API シグネチャ維持 |
| `InternalAlertPoller` 専用 thread | `schedule_periodic` 経由 (例外で停止しないよう `_safe_tick` で吸収) |
| `_db_polling_loop` (3s ポーリング) | `schedule_periodic(interval_seconds=3, ...)` |
| `_sds_background_loop` (動的 backoff) | `_sds_tick_and_reschedule` で callback 内手動再 schedule |

これでコア側 background thread は EventScheduler の dispatch thread 1 本に集約。
ペルソナ別メタ判断は anchor touch 直後に `_schedule_cache_ttl_pulse` で TTL 接近
時刻を予約 (key=`ttl:<persona_id>`)、ユーザー対話のたびに再 touch で予約上書き
されるので「対話継続中は前倒し fire しない、TTL 残り少なくなったら自動的に
メタ判断が走る」挙動が得られる。

**残ったポーリング (性質上必須)**:
- `db_polling` (inter-city DB 確認): 別プロセスが DB に書く瞬間を検知できない
- `internal_alert_poll` (時間ドリフト型パラメータ): 時間経過で増える値の閾値
- `sds_heartbeat`: outbound、push 不可

これらは EventScheduler に乗せて集約された (= dispatcher が 1 本) が、本質的な
ポーリングはまだ残っている。完全 push 化は将来の別タスク (`docs/issues/`)。

addon (X 監視等) のポーリング統合は Phase 4-e のスコープ外:
[`docs/issues/addon_event_scheduler_integration.md`](../../issues/addon_event_scheduler_integration.md)
として切り出した。

#### 4. waiting Track timeout を EventScheduler に push

`TrackManager.wait()` で `waiting_timeout_at` をセットするタイミングで
EventScheduler に予約 push (key=`wait_timeout:<track_id>`)。timeout 到達時は
`_handle_waiting_timeout` で再 fetch → `waiting` 状態のままなら
`_notify_alert(persona_id, track_id, context={"trigger": "waiting_timeout", ...})`
を発火。intent 通り **自動遷移しない** (メタ判断に委ねる)。

waiting 解除/abort/pause 経路で予約を cancel (`_cancel_waiting_timeout`)。
TrackManager は `event_scheduler` を Optional な `__init__` 引数で受け取り、
None の場合 (tools 等の独立インスタンス) は timeout 通知が機能しない (旧仕様
互換)。tools から `wait()` を呼ぶ需要は現状ない (`track_wait` ツール自体未実装)。

#### 5. メタ判断 Pulse 失敗時の retry ループ

`MetaLayer._run_judgment_via_playbook` に `for attempt in range(max_retries + 1):`
ループを追加。`META_JUDGMENT_CONFIG.max_retries` + `retry_backoff_seconds` から
取得した値で:

- 各試行で `runtime.run_meta_user(...)` を呼ぶ
- 例外 → リトライ対象 (WARN ログ + `last_failure_reason` 記録)
- `event_callback` が `error` event を捕捉 → リトライ対象
- 成功 → 即 return

リトライ前は `time.sleep(retry_backoff_seconds)` で per-persona Lock を保持した
まま wait (並行 alert/tick は既存の Lock 機構で wait される)。全試行枯渇 → WARN
ログ "exhausted retries" + 諦める。次の判断は EventScheduler が cache TTL 接近 /
interval 経過で自動的に push する前提。

#### 6. 自動発話間隔の二重管理を解消

旧実装では:
- 自律行動マネージャー UI の `間隔` (`AutonomyManager.interval_minutes`、実行時値)
- メタ判断 Pulse 設定 の `自動発話間隔` (`META_JUDGMENT_CONFIG.periodic_interval_minutes`、永続値)

の 2 つが独立していて、しかも前者は **DB 永続化されておらず再起動で 50 分に
リセット**されるバグがあった。

統合: `META_JUDGMENT_CONFIG.periodic_interval_minutes` を真実とし、
- `AutonomyManager.__init__` の引数優先順を「引数 > META_JUDGMENT_CONFIG > env > module default」に変更
- `/api/people/{id}/autonomy/start` と `update_autonomy_config` で interval が来た時、`AutonomyManager.set_interval()` を呼ぶと同時に `META_JUDGMENT_CONFIG.periodic_interval_minutes` を DB 永続化
- メタ判断 Pulse 設定 UI から「自動発話間隔」を削除

これで自律行動マネージャー UI の `間隔` が真実の入口になり、再起動後も値が保持
される (旧バグ修正)。`set_interval` の `should_reschedule` 判定も `state in
(RUNNING, WAITING)` に修正済 (旧 `RUNNING` のみで判定すると WAITING 中の即時
反映が効かなかったバグ)。

#### 関連 issue / 副産物

- [`docs/issues/uvicorn_traceback_not_in_logs.md`](../../issues/uvicorn_traceback_not_in_logs.md)
  500 エラーのデバッグ中に発覚: uvicorn の `Exception in ASGI application` Traceback
  が `backend.log` に載らずターミナル stderr 直行してた。Phase 4-e 中の
  `update_ai` フォワードメソッド漏れ事故を契機に起票。
- [`docs/issues/addon_event_scheduler_integration.md`](../../issues/addon_event_scheduler_integration.md)
  X 監視等の addon ポーリング統合は別タスクに切り出し。

---

### v0.29 (2026-05-08) — スケジュール / チャット UI で Spell + Playbook 両方選択可能に

v0.28 で完成した pre_spells 引数あり対応 (バックエンド側) を、UI 経路でも操作できる
ようにする UI 改修。スケジュールでもチャット UI でも、`/spell name='run_playbook'
args={"name": "X"}` (Playbook 起動) と `/spell name='Y'` (Spell 起動) を混ぜて
pre_spells に流せる UX を整備した。

**バグ修正**:

- `frontend/src/app/page.tsx:1304-1306` で「ツール指定」モードの送信時に
  `/run_playbook(name="X")` 形式 (関数呼び出し風) を pre_spells として送って
  いたが、バックエンドの `_SPELL_PATTERN` (`^/spell\s+name='([^']+)'\s+args=...`) /
  `_SPELL_PATTERN_NO_ARGS` (`^/spell\s+name='([^']+)'\s*$`) のどちらでも
  パースできず実行されない状態だった (作業 1 で形式統一した時に壊れた)。
- `/spell name='run_playbook' args={"name": "X"}` 形式に修正。

**新機能 (両 UI 共通の機構)**:

- `/api/people/spells` エンドポイント新規追加 (`api/routes/people/summon.py`):
  利用可能な Spell 一覧 (`name` / `display_name` / `description`) を返す。
  `persona_id` クエリパラメータ指定で `availability_check` (ToolSchema 属性) +
  MCP per-persona フィルタを適用。`spell_visible=False` の Spell は除外。
- 共通ユーティリティ `frontend/src/lib/preSpells.ts` 新規:
  - `parsePreSpellsForUI(entries)`: pre_spells エントリ列を
    `{playbookName: string | null, spellNames: string[]}` に分解
  - `buildPreSpellsFromUI(playbookName, spellNames)`: 逆方向の組み立て
  - `run_playbook` (確定値 args) と他 Spell (引数省略形) を区別して扱い、
    UI 状態 ↔ pre_spells エントリ列の双方向変換を一元化

**ScheduleModal 拡張**:

- 「実行する Playbook」セクション新規追加 (ドロップダウン形式)
  - `/api/config/playbooks?router_callable=true` から候補取得
  - 「（指定しない）」を含めて単一選択、未選択 OK
- 「使用するスペル」セクション (v0.28 で追加済) と独立して扱える
  - `run_playbook` は Spell リストから除外 (Playbook ドロップダウンが担当)
- スケジュール一覧の「パラメータ」列で `Playbook: X / Spells: Y, Z` の形で表示

**ToolModeSelector 拡張**:

- 「ツール指定」モード時、Playbook ドロップダウンの隣に Spell 複数選択ボタンを
  新規追加
- ボタン表示: `スペル併用なし` / `スペル: N 個`
- ドロップダウンはチェックボックス形式で複数選択
  - `run_playbook` は除外
  - description が title 属性に入って tooltip 表示
- 選択値は `playbookArgs.selected_spells: string[]` に保存、サーバー同期は
  既存 `syncToServer` 経由

**page.tsx 拡張**:

- 送信時に `currentPlaybookArgs.selected_playbook` (Playbook 1 つ) +
  `currentPlaybookArgs.selected_spells` (Spell 複数) の両方を
  `buildPreSpellsFromUI` で pre_spells エントリ列に変換

**データフロー (新仕様、両 UI 共通)**:

```
UI: Playbook 選択 (1 つ or 未選択) + Spell 選択 (複数 or 0 件)
  ↓ buildPreSpellsFromUI
pre_spells エントリ列:
  - "/spell name='run_playbook' args={\"name\": \"<playbook>\"}" (Playbook 選択時のみ)
  - "/spell name='<spell_name>'" * N (Spell 選択分)
  ↓ POST /api/chat/send (or schedule.PLAYBOOK_PARAMS.pre_spells)
バックエンド _execute_pre_spells:
  - run_playbook entry: 確定値 args で run_playbook Spell 実行 → サブライン Playbook 起動
  - args 省略形 entry: spell_args_decider Playbook で動的引数生成 → Spell 実行
  ↓
すべての結果が state["_messages"] に注入 → メインライン LLM が踏まえて発話
```

**実装ファイル**:

- `api/routes/people/summon.py`: `/api/people/spells` エンドポイント追加
- `frontend/src/lib/preSpells.ts`: 共通ユーティリティ新規
- `frontend/src/components/ScheduleModal.tsx`: 「実行する Playbook」追加 + 共通
  ユーティリティへ置換 + 表示改善
- `frontend/src/components/ToolModeSelector.tsx`: Spell 複数選択 UI 追加
- `frontend/src/app/page.tsx`: 送信ロジックを `buildPreSpellsFromUI` 経由に統一 +
  バグ修正

**検証**:

- ruff: 通過
- frontend TypeScript: 通過 (`npx tsc --noEmit` エラーなし)
- 実機検証 (まはー報告 2026-05-08):
  - スケジュール UI で Spell 単独実行 (メール送信) → OK
  - チャット UI のツール指定モードで Playbook 起動 → 復活確認
  - 両 UI で Playbook + Spell 併用ケース → OK

**残課題**:

- なし (本セッションの当初目標は完遂)

### v0.28 (2026-05-08) — Phase 3 A 残件 + B 完了 (meta_user 系削除 + pre_spells 引数あり対応)

handoff_2026-05-08.md で計画した作業 1 / 1.5 / 2 を一括実装。Phase 3 の主要構造刷新が完遂し、旧 meta_user 系 Playbook が完全に消えた。

**作業 1 — `meta_user` / `sub_router_user` / `meta_user_manual` / `basic_chat` の削除**:

- `builtin_data/playbooks/public/` から 4 Playbook ファイル削除
- DB 自動 prune (`scripts/import_all_playbooks.py --force` で起動時に実施)
- コード残骸整理:
  - `sea/runtime_state.py`: `update_router_selection` 関数を完全削除 (旧 `sub_router_user` 専用)
  - `sea/runtime.py`: 上記の import / `_update_router_selection` メソッド / `_choose_playbook` の docstring から旧言及削除
  - `sea/runtime_llm.py:812`: `if playbook.name == 'sub_router_user'` のデバッグログ条件削除、docstring 整理
  - `sea/runtime_engine.py`: `lg_exec_node` から `meta_user_manual` 特別処理 + `basic_chat` フォールバック + permission check ガードを全削除
  - `api/routes/config.py`: `("tool_selected", "meta_user_manual")` を `tool_selected` 一本化 (2 箇所)
  - `builtin_data/phenomena/inject_persona_event.py`: デフォルト `track_user_conversation` 化、`selected_playbook` 経路は WARNING ログ + 通常経路で処理
  - `builtin_data/playbooks/public/schedule_management_playbook.json`: LLM プロンプト内の `meta_user`/`meta_user_manual` 言及を `track_user_conversation` に書き換え
  - `frontend/src/app/page.tsx` / `frontend/src/components/ToolModeSelector.tsx`: legacy 値コメントを「pre-Phase 3」と明示
- テスト修正:
  - `tests/sea/test_runtime_state.py`: `update_router_selection` テスト + import 削除
  - `tests/sea/test_runtime_engine.py`: `meta_user_manual` 関連 4 テストを `tool_selected` ベースに整理 (warning emit テストは削除)
  - `tests/test_config_set_playbook.py`: `meta_user_manual` → `tool_selected` 全置換 + 関数名リネーム
  - `test_fixtures/test_api.py` / `test_fixtures/definitions/test_data.json`: `EXPECTED_PLAYBOOKS` を実態反映 (`track_user_conversation`, `sub_speak`, `meta_exec_speak`)
- マイグレーション: `v0_3_0_dev1_legacy_schedule_playbook_names` (AI scope) を `saiverse/upgrade_handlers.py` に追加。`persona_schedule.META_PLAYBOOK` から削除済み Playbook 名を `track_user_conversation` に書き換え
- VERSION: `0.3.0.dev0` → `0.3.0.dev1`
- テスト追加: `tests/test_upgrade_handlers_legacy_schedule.py` 7 件

**作業 1.5 — `/api/people/meta_playbooks` UI フィルタ修正**:

- 原因: `api/routes/people/summon.py:49` の `PlaybookModel.name.like("meta_%")` フィルタが旧 `meta_user`/`meta_user_manual` 時代の遺物で、`track_user_conversation` (新時代の主流) を除外していた
- 修正: `name.like("meta_%")` を削除し、`user_selectable=true` フラグのみで判定する形に統一
- 結果: スケジュール編集 UI の Meta Playbook 選択肢が `meta_simple_speak` のみ → `meta_simple_speak`, `track_autonomous`, `track_user_conversation` の 3 件に拡大

**作業 2 — pre_spells を引数あり Spell 対応 + スケジュール経路適用**:

設計上の重要判断 (本セッション中盤の対話で確定):

- 旧 `meta_user_manual` 経路 (`PLAYBOOK_PARAMS.selected_playbook=X`) を新仕様で復活させる方法を検討
- 5 案 (`spell_call` 新ノード) は **2 LLM 構造への先祖返り + 新経路追加で Phase 3 の哲学から外れる** ため不採用
- 代わりに **pre_spells を完全な姿 (どんな Spell でも実行可) に拡張**:
  - pre_spells の責務は「Spell 実行」のまま、関数内 LLM コールはやらない
  - 引数決定は外部 Playbook (`spell_args_decider`) に委譲
- 認知モデル整合: `spell_args_decider` は `{context}` 文字列を受け取らず、親ライン (メインライン) の messages を v0.25 snapshot 経路で継承。ペルソナは自分の認知から自然に引数決定 (Spell loop と同じ流れ)
- 経路は `pre_spells` 1 本に統一 (Spell loop / pre_spells / メインライン LLM ノードの三位一体構造を維持)

実装:

- `LLMNodeDef.response_schema_source` フィールド追加 (`sea/playbook_models.py`)
- `_resolve_response_schema_source` ヘルパ (`sea/runtime_llm.py`): `spell:<name>` → `SPELL_TOOL_SCHEMAS[name].parameters` 解決
- `lg_llm_node` で `response_schema_source` を template 展開 (`{state_var}` 解決) → schema 動的注入
- `_decide_spell_args_via_playbook` ヘルパ: `spell_args_decider` Playbook を sub_line で起動 → `parent_state["args"]` から取得
- `_SPELL_PATTERN_NO_ARGS` 追加: `^/spell\s+name='([^']+)'\s*$` (引数省略形)
- `_execute_pre_spells` 拡張: 引数あり / 引数なし両対応、引数なしは `_decide_spell_args_via_playbook` 経由で動的生成
- `builtin_data/playbooks/public/spell_args_decider.json` 新規: 1 LLM ノード (`response_schema_source: "spell:{spell_name}"`, `output_key: "args"`, `output_schema: ["args"]`)
- `submit_schedule` に `pre_spells: Optional[List[str]]` 引数追加 (`sea/pulse_controller.py`)
- `_execute_schedule` で `PLAYBOOK_PARAMS.pre_spells` を抽出して `submit_schedule(pre_spells=...)` に渡す。`pre_spells` キーは Playbook input_schema には流さない (runtime hook として分離)
- マイグレーションハンドラ `v0_3_0_dev2_legacy_schedule_selected_playbook` (AI scope, `0.3.0.dev1` → `0.3.0.dev2`) で旧 `PLAYBOOK_PARAMS.selected_playbook=X` を `pre_spells=["/spell name='X'"]` に変換 (既存 `pre_spells` があれば末尾追加、空文字は削除のみ)
- VERSION: `0.3.0.dev1` → `0.3.0.dev2`

**テスト追加**:

- `tests/test_response_schema_source.py` 6 件 (spell:解決、未知/空、不正形式)
- `tests/test_pre_spells_dynamic_args.py` 8 件 (no_args パターン、decider 呼出、混在エントリ)
- `tests/test_upgrade_handlers_legacy_selected_playbook.py` 9 件 (変換、追加、空、null、冪等、他ペルソナ非干渉、HANDLERS 登録)
- 計 23 件追加。全パス
- tests/ 全体: 759 件パス (既知 flaky `test_gemini_client_generate_stream` 1 件は単独実行で確認 OK)

**起動時の動作 (既存ユーザー視点)**:

`update.bat` で `0.3.0.dev2` に更新 → `main.py` 起動時:

1. `v0_3_0_dynamic_state_reset` (dev0): 既走 → no-op
2. `v0_3_0_dev1_legacy_schedule_playbook_names` (dev1): 旧 `META_PLAYBOOK` を `track_user_conversation` に
3. `v0_3_0_dev2_legacy_schedule_selected_playbook` (dev2): 旧 `selected_playbook` を `pre_spells` 形式に変換

スケジュール起動時:

1. `META_PLAYBOOK="track_user_conversation"`
2. `PLAYBOOK_PARAMS.pre_spells=["/spell name='send_email_to_user'"]` を抽出
3. `_execute_pre_spells` が `spell_args_decider` Playbook で引数を動的生成 → Spell 実行
4. 結果が `state["_messages"]` に注入
5. メインライン LLM が結果を踏まえて発話

旧 `meta_user_manual` 経路と同等以上の挙動を、`pre_spells` 1 本の経路で実現。

**実機検証 (2026-05-08 まはー報告)**:

- 起動時マイグレーション動作確認
- track_user_conversation での通常会話 OK
- スケジュール経路の動作確認は次セッション以降

**残課題 (本実装スコープ外)**:

- スケジュール作成 UI で `pre_spells` を指定できる UX 改修 (現状はバックエンド経路のみ整備、手動 SQL or PLAYBOOK_PARAMS 直接編集が必要)
- 段階 4-D: 旧 DEPRECATED コード完全削除 (`include_internal` / `pulse:{uuid}` タグ併行記録 / `LLMNodeDef.context_profile` / `LLMNodeDef.model_type` / `exclude_pulse_id`)

### v0.27 (2026-05-08) — Phase 3 A 残件の実態確認 + handoff 整理 (実装変更なし)

セッション開始時のキャッチアップで、Phase 3 A (`/run_playbook` Spell 段階移行) の進捗を一次ソース (コード) と突き合わせて確認した結果、handoff_phase3_impl.md / 旧 README の進捗表に記載されていた「未着手」項目が実は既に完了済だったため、ドキュメントを実態に合わせて更新した。コード変更なし、ドキュメント整理のみ。

**コード調査で確定した実装状況**:

| 項目 | 旧 README 記載 | 実際の状態 | 確認場所 |
|---|---|---|---|
| システムプロンプト Playbook 一覧注入 | 🔲 未着手 | ✅ 完了 | `sea/runtime_context.py:118-152` で `## 利用可能な能力` セクション、`router_callable=true` を bullet list 化、`ContextRequirements.available_playbooks` フラグで活性化 |
| `track_user_conversation` を 1-LLM + Spell 構成に | 🔲 未着手 | ✅ 完了 | `track_user_conversation.json`: `main_line_response` (LLM 1) + `process_body` (control_body ツール) 構成。Spell 実行ループは LLM ノード内で runtime が回す |
| UI からの Playbook 起動 (pre_spells) | 🔲 未着手 | 🟡 コア完成 / Schedule 経路未対応 | `api/routes/chat.py:322` (ChatRequest)、`sea/runtime_llm.py:661-776` (`_execute_pre_spells`)、`pulse_controller.py` で `ExecutionRequest.pre_spells` 伝播、`api/routes/config.py:155-157` で `router_callable=true` 一覧を UI に返却 |
| `meta_user` / `sub_router_user` の本番経路使用 | 既存 | 既に未使用 (削除可能) | `_choose_playbook` は `track_user_conversation` のみ候補 (`runtime.py:2130`)。`run_meta_user` メソッドは `meta_user` Playbook に依存しない汎用 Pulse エントリ。`meta_layer.run_meta_user(meta_playbook="meta_judgment")` のように Playbook 名を引数で渡す経路 |

**`router_callable` 分布** (38 件中):

- `router_callable: true` (12 件): `building_move`, `create_building`, `deep_research`, `document_create`, `document_search`, `generate_image`, `generate_image_local`, `item_action`, `memopedia_note`, `memory_research`, `novel_writing`, `schedule_management`
- `router_callable: false` (18 件): `autonomy_creation`, `autonomy_memory_organization`, `autonomy_web_research`, `basic_chat` (deprecated), `meta_autonomy_decision`, `meta_exec_speak`, `meta_judgment`, `meta_simple_speak`, `meta_user` (deprecated), `meta_user_manual` (deprecated), `research_task`, `source_chronicle`, `source_document`, `source_memopedia`, `source_messagelog`, `source_pdf`, `source_web`, `sub_router_user` (deprecated), `sub_speak`, `sub_think_meta`, `track_*` (5 件)

**残件の整理 (handoff_2026-05-08.md に集約)**:

1. **作業 1**: `meta_user` / `sub_router_user` / `meta_user_manual` / `basic_chat` の削除 — 4 つの Playbook ファイル削除 + DB prune + コード残骸整理 (`sea/runtime_llm.py:812` のデバッグ条件、test_data.json、docstring)
2. **作業 2**: スケジュール起動経路への `pre_spells` 適用 — `saiverse/schedule_manager.py` の `_execute_schedule` で `PLAYBOOK_PARAMS` から `pre_spells` を抽出 → `submit_schedule` → `ExecutionRequest.pre_spells` に流す。frontend のスケジュール作成 UI 拡張も
3. **作業 3**: 段階 4-D 旧 DEPRECATED コード削除 (作業 1, 2 完了後) — `include_internal` / `pulse:{uuid}` タグ併行記録 / `LLMNodeDef.context_profile` / `LLMNodeDef.model_type` (+ `model_type=lightweight` 23 ノード) / `exclude_pulse_id`

**設計判断 (本セッションで確定)**:

- **`run_meta_user()` メソッド名はリネームしない**: 紛らわしい名称だが、`meta_user` Playbook 削除と同時にメソッドリネームすると影響範囲が大きい (`pulse_controller`, `meta_layer`, `runtime_context` 全部から呼ばれる)。リネーム (例: `run_pulse` / `run_meta_pulse`) は別タスクで実施。`meta_user` 削除と同時には docstring 更新のみで済ませる
- **`_basic_chat_playbook()` (in-memory) は残す**: `basic_chat.json` ファイルを削除しても、コード上の最終フォールバック (`runtime.py:2137`) は残置。絶対に到達しない保険として機能

**ドキュメント変更**:

- [README.md](README.md) — Phase 3 進捗 60% → 80% に更新、進捗表のステップ別状態を実態反映、関連ドキュメントに新 handoff 追加
- [phases/phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) — Spell 機構タスク表 (システムプロンプト注入 / `track_user_conversation` 構成 / pre_spells コア) を ✅ に更新、残件を `meta_user` 系削除 + スケジュール経路適用 + end-to-end 検証に分割
- [handoff_phase3_impl.md](handoff_phase3_impl.md) — ヘッダに完了済ステータス追記、後継 handoff へのポインタ追加
- [handoff_2026-05-08.md](handoff_2026-05-08.md) — 新規作成、A 残件の実装着手用 handoff

**次セッション**: handoff_2026-05-08.md の作業 1 から着手。

### v0.26 (2026-05-01) — Spell 結果に media attachment を載せる経路 + UI リマインド

`/run_playbook` Spell 経由で起動したサブ Playbook (主に `generate_image`) の生成物 (画像等) が、親メインラインの「次の LLM ラウンド」で attachment として届かない問題への対応。v0.25 の実機検証で「ペルソナが画像を見るには item_view スペルを別途呼ぶ必要がある」という UX 制約が見えたため独立改修。

**確定事項**:

- Spell の戻り値型を `str` から `Tuple[str, Optional[Dict[str, Any]]]` (text, metadata) に拡張 — 既存の str 戻り値 spell は `(str, None)` に正規化されて互換維持
- spell loop が全 spell の `metadata.media` を集約し、次の LLM ラウンドの user message の `metadata.media` に lift。LLM client の `iter_image_media()` 経路で attachment 化される
- `run_playbook` Spell が `parent_state["metadata"].media` を取り出して上記の媒体経路に流す。サブ Playbook の `output_schema` に "metadata" が含まれていれば自動で伝わる (v0.24 で実装済の output_schema 伝播経路を流用)
- `generate_image_playbook.json` の `report_template` に Markdown リンク (`[タイトル](saiverse://item/<id>/content)`) リマインドを追記。ペルソナが発言中に URI を含めればチャット UI が画像を直接表示する仕組みを思い出させる

**実装スコープ**:

1. **媒体経路 (multimodal forwarding)**
   - `sea/runtime_llm.py` `_run_spell_tool_async` を `Tuple[str, Optional[Dict]]` 戻り値に拡張
     - 既存 spell の str 戻り値 → `(str(result), None)`
     - 新仕様 spell の `(str, dict)` tuple → そのまま
     - その他 → `(str(x), None)` (legacy 互換)
   - `_run_spell_loop` で全 spell の `metadata.media` (list) を `aggregated_media` に合算
   - `messages.append({"role": "user", "content": ..., "metadata": {"media": [...]}})` の形で attachment を載せる
   - 複数 spell × 複数 media (例: 1 spell が 4 枚返す将来のローカル画像生成) も合算で対応
   - `builtin_data/tools/run_playbook.py` の戻り値を `(report_text, metadata)` に変更。`parent_state["metadata"].media` を `metadata` に転送
   - 既存 spell tools (track_*, note_*, memory_recall_unified 等) は無修正で互換 (str 戻り値のまま)

2. **UI リマインド**
   - `generate_image_playbook.json` の `report_template` 末尾に追記:
     `💡 発言の中に [画像タイトル](saiverse://item/<アイテムID>/content) の形で Markdown リンクを書くと、チャット UI で画像が直接表示されます (アイテム閲覧スペル不要)。<アイテムID> は上記の「アイテムID: ...」をそのまま使ってください。`
   - `<アイテムID>` placeholder は意図的にリテラル (現在の `image_generator` が item_id を独立フィールドで返さず text 文字列内のみ持つため。template 側からの dot-notation 展開で解決するには image_generator 戻り値拡張が別途必要、本 commit ではスコープ外)

**設計判断**:

- **spell 戻り値の Tuple 拡張 vs PulseContext buffer**: 後者 (PulseContext に媒体 buffer を持たせる) も検討したが、PulseContext のロギング目的責務を肥大化させる。spell tool の戻り値型を素直に拡張する方が責務分離がきれい
- **既存 spell の互換**: str OR Tuple の両対応にしたので、既存 spell tools は触らずに済む。新 spell が media を返したい時だけ Tuple にすればよい
- **複数 media 合算**: 並列 spell 実行 (spell loop が同ラウンドで複数 spell を `asyncio.gather` で並列実行) でも、各 spell が media を返せば合算される。将来「ローカル画像生成で 4 枚一気に」のようなケースも自動対応
- **リマインドの位置**: メインラインに伝わる report に書く方針 (template の末尾)。Playbook 内のシステムプロンプトに恒久指示を入れる方法もあるが、毎回 system 領域で消費されるのは無駄。「画像生成した直後だけ思い出す」がリマインドの本来の機能

**実機検証**:

- (まはー報告) `/run_playbook(name="generate_image")` 経由で生成 → 親メインラインの次ラウンドで画像が attachment として LLM に届くことを確認
- ペルソナが報告メッセージの内容を踏まえて発話に Markdown リンクを含めるかは、リマインド文言で導線が引けたかどうか継続観察

**残課題 (本 commit スコープ外)**:

- `image_generator` tool の戻り値を 5要素 tuple に拡張して `item_id` を独立フィールド化 → `report_template` で `{item_id}` を直接展開可能にする。リマインド文言から `<アイテムID>` placeholder を消せて UX 向上。次回 image_generator を触るときに同梱
- 他の媒体生成系 spell (今後のローカル画像 4枚生成等) で同じ media 戻し方パターンを採用するための contributor doc 起草

### v0.25 (2026-05-01) — `/run_playbook` Spell の親履歴流入 + `report_template` 機構

v0.24 で起動した `/run_playbook` Spell の MVP 制約 2 つを解消する補完実装。実機 (air_city_a で `generate_image` 起動) で問題が表面化したため独立改修。

**確定事項**:

- 親メインライン (or 親サブライン) の LLM messages を、`/run_playbook` で起動するサブラインに **snapshot コピーで引き継ぐ**経路を新設
- 子 Playbook トップレベルに `report_template: Optional[str]` フィールドを新設し、**LLM コール無しで機械的に `report_to_parent` を組み立てる**経路を追加

**実装スコープ**:

1. **親 LLM messages の流入 (snapshot 経路)**
   - `tools/context.py` に `_LLM_MESSAGES: ContextVar` 追加
   - `persona_context()` に `llm_messages: Optional[List[Dict]] = None` 引数追加
   - `get_active_llm_messages()` getter 追加
   - `sea/runtime_llm.py` の spell loop が `_run_spell_tool_async(... messages=messages)` を渡す。handy tool 経路 + spell 同期/async 3 箇所で `persona_context(... llm_messages=messages)` に
   - `builtin_data/tools/run_playbook.py` で `parent_state["_messages"] = list(get_active_llm_messages() or [])`
   - 入れ子は context manager の入れ子 reset で自動的に正しく動く (孫サブラインは親サブラインの messages を、ひ孫は孫の messages を、それぞれ正しく snapshot)

2. **`report_template` フィールド**
   - `sea/playbook_models.py` の `PlaybookSchema` に `report_template: Optional[str]` 追加
   - `sea/runtime_graph.py` の `compile_with_langgraph` 完了処理で、`report_template` 指定時に `{key}` / `{key.subkey}` プレースホルダを最終 state で展開し、`parent_state["report_to_parent"]` に書き込み
   - dict 値は dot-notation で展開 (例: `gen_params.title` で gen_params dict 内の title を参照)
   - 内部 `_` プレフィックス state キーは展開対象外
   - 既存の output_schema 経路 (state 内に `report_to_parent` を書いて伝播) も維持。template と並走する場合は template の値で上書き
   - `generate_image_playbook.json` で実例追加 (`"report_template": "画像「{gen_params.title}」の生成が完了しました。\n\n{text}"`)

**設計判断**:

- **contextvar を選んだ理由**: PulseContext に「親 LLM messages」を持たせるとロギング目的の同オブジェクトの責務が肥大化する。spell 実行時の context をまとめる `persona_context` (= contextvar 群) と同じ仕組みに乗せる方が自然
- **snapshot コピー**: 参照ではなく `list(...)` で copy して渡す。サブライン処理中に親 messages が変動しても影響を受けない (race-free)
- **template は output_schema 不要**: `parent_state["report_to_parent"]` に runtime が直接書き込む。output_schema 経由の従来経路と並存可能 (動的サマリが要る Playbook は LLM/memorize ノードで書く経路、機械的でよい Playbook は template 経路)
- **template の優先度**: state["report_to_parent"] と template 両方ある場合、template が後勝ちで上書き。Playbook 作者が「これが正規の report 形」と宣言したものとして扱う

**実機検証**:

- `generate_image_playbook.json` を `report_template` 付きで保存し、`/run_playbook(name="generate_image")` 経由で起動
- 結果: 親メインライン履歴 (エアの「新緑の未来」会話) がサブライン `decide_prompt` ノードに流入し、文脈に即した英語プロンプトが組み立てられた (旧: 親 messages 空 → サンプルプロンプトに引っ張られてサイバーパンク出力)
- `report_to_parent` も template 通り「画像「Air and Maha's Future Horizon」の生成が完了しました。\n\n（生成詳細）」が親に返る (旧: "completed but produced no report_to_parent" 警告で空返り)

**残課題 (本 commit スコープ外)**:

- `report_to_parent` 厳密バリデーション: `can_run_as_child=true` の Playbook で `report_to_parent` が出ないと例外化 (現状は警告ログ)。`report_template` で機械的に保証できるようになったため、今後は「template 指定 OR LLM ノードで明示的に書く」のどちらかを必須にする方向で整理可能
- 他の機械的 sub Playbook (例: 各種 source_* Playbook) への `report_template` 適用 — Playbook ごとに作者が判断して導入

### v0.24 (2026-05-01) — `/run_playbook` Spell 新設: 入れ子サブライン機構の中核

**確定事項**:

- `builtin_data/tools/run_playbook.py` 新規 — Spell として登録 (spell=True)
- メインライン (or 親サブライン) LLM が通常発話の中で `/run_playbook(name="...")` と書くと、指定された Playbook がサブラインとして起動され、完了時に `report_to_parent` (string) が親に返る
- 詳細仕様: `nested_subline_spell.md` v0.1 (2026-05-01) を実装

**実装スコープ (このコミットでカバー)**:

- 引数: `name` のみ (Playbook 名)。引数値は呼ばれた側の最初の LLM ノードが構造化出力で決める (旧 router 方式の踏襲)
- 戻り値: `report_to_parent` を string で返却。`output_schema` に `report_to_parent` が含まれていない場合は警告メッセージを返す (例外は出さず Spell loop 継続を保証)
- 起動経路: `sea_runtime._run_playbook(line="sub", isolate_pulse_context=False)` 経由
  - `line="sub"` → 親 `_messages` のコピーをベースに軽量モデルで実行
  - `isolate_pulse_context=False` → 親 PulseContext を共有 (line_stack 管理のため)
- `router_callable=true` チェック: false の Playbook は外部から呼べない (内部 sub_play 専用)。エラー文字列を返す
- 深さ制限: 4 階層。`PulseContext._line_stack` の長さで判定 (メインライン Pulse 起動時 = 1 frame、各 `/run_playbook` で +1 frame、stack length 5 まで許容、6+ は拒否)
- エラーパスは全て文字列で返す (Spell loop の継続を保証):
  - persona_id / manager / pulse_context が無い: 各エラーメッセージ
  - 深さ超過: `"Subline depth limit (4) exceeded; cannot run playbook 'X'."`
  - Playbook not found: 利用可能な router_callable Playbook 一覧を表示
  - router_callable=false: `"Playbook 'X' is not callable from spell."`
  - sub-line 実行例外: `"Sub-line failed for 'X': <error>"`

**設計判断**:

- **Spell として実装し、Spell loop の特別分岐は追加しない**: Spell ツール内で `sea_runtime._run_playbook` を直接呼ぶ MVP 設計。Spell loop は通常通り並列実行する (複数の `/run_playbook` を 1 ターンで呼ぶことも可能)
- **メインライン会話の引き継ぎは MVP でしない**: spell ツール内では state 引数にアクセスできないため、`parent_state["_messages"] = []` の minimal state を渡す。Playbook 内で必要な情報は input_schema 経由 / SAIMemory recall / state.input から取得できる前提。end-to-end で不足が出たら spell loop に messages contextvar を追加する経路を検討
- **PulseContext は共有 (`isolate_pulse_context=False`)**: line_stack 管理のため。sub_play ノード経由 (line='sub') では isolate_pulse_context=True がデフォルトだが、`/run_playbook` Spell では line_stack の親子関係が必要なので共有する

**テスト追加**:

- `tests/test_run_playbook_spell.py` 新規 +10 件:
  - エラーパス 3 件: persona_id / manager / pulse_context 欠落
  - 深さ制限 2 件: stack length 上限到達 / 上限未達
  - router_callable チェック 1 件: false の Playbook を拒否
  - Playbook 名不正 1 件: "not found" エラー
  - 正常系 1 件: line="sub", parent_state, pulse_context 共有を確認
  - report_to_parent 欠落時の警告 1 件
  - sub-line 実行例外時のエラーメッセージ 1 件
- importlib で動的ロード (Phase 3 の他の builtin_data ツールテストと同じパターン)

**残作業 (Spell 実機定着の段階移行)**:

- ステップ 3: メインライン LLM のシステムプロンプトに「Playbook 一覧」セクション注入 (`router_callable=true` Playbook を列挙)
- ステップ 4: `router_callable` の運用整理 (現状 18 件 true / 25 件 false、見直し必要)
- ステップ 5: `track_user_conversation` を 1-LLM + Spell 構成に書き換え (旧 `meta_user` / `sub_router_user` 統合廃止と一体)
- ステップ 6: `meta_user` / `sub_router_user` の deprecated 化 → 削除
- ステップ 7: end-to-end 動作検証 (軽い Spell のみ / 重い `/run_playbook` 1 段 / 入れ子)

これらは実機 air_city_a での挙動確認が必須なので、本 commit ではコア機構のみ提供。

**旧 `call_playbook` ツールとの関係**:

- 旧 `call_playbook` (meta_exec_speak 経由の間接実行) は **2026-08-17 に `meta_exec_speak` ごと撤去** (どこからも起動されない残置物であることを全経路の走査で確認 — Playbook のツールノード / `available_tools` / spell / 旧ツール割り当てテーブル / realtime binding / Python 直接呼び出しのいずれにも参照なし。まはー裁定)
- `/run_playbook` が実機定着して既存 Playbook の Spell 利用が広まったら廃止検討

### v0.23 (2026-05-01) — 段階 4-C 完了: 既存 Playbook を line メタデータベースに一括翻訳

**確定事項**:

- 既存 builtin Playbook 33 件 (38 件中、5 件は対象タグなしで unchanged) を `scripts/migrate_playbooks_to_lines.py` で半自動翻訳
- `MemorizeNodeDef` に `line_role` / `scope` フィールドを追加 (Pydantic Optional[str])
- `lg_memorize_node` (`sea/runtime_engine.py`) で `node_def.line_role` / `node_def.scope` を `_store_memory` に渡す経路を追加。未指定時は `pulse_context.current_line_metadata()` から自動解決
- Y 案により `model_type=lightweight` (23 ノード) は **保留** (4-D で `/run_playbook` Spell 実装と一体で整理予定)

**変換ルール (確定版)**:

| 旧 | 新 | 件数 |
|---|---|---|
| LLM ノードの `context_profile` (4 種値: `conversation` / `worker_light` / `worker` / `router`) | 削除 (4-A 後は無効化済み、記述として残ってるだけ) | 75 ノード |
| `memorize.tags` の `internal` | `line_role: "sub_line"` + `scope: "volatile"` に置換 | 66 件 (LLM 45 + memorize ノード 21) |
| `memorize.tags` の `conversation` | `line_role: "main_line"` + `scope: "committed"` に置換 | 5 件 (LLM 5 + memorize ノード 0) |
| `memorize.tags` の `event_message` | 意味分類として残置 + `line_role: "main_line"` + `scope: "committed"` を併記 (Chronicle 連携のため) | 0 件 (現状未使用) |
| 残りの意味分類タグ (`creation` / `memory_research` / `web_research` / `playbook_result` / `tool_result` / `novel_writing` / `schedule_management` 等) | そのまま保持 | — |
| `model_type=lightweight` | **保留** (Y 案、4-D で整理) | 23 ノード残存 |

**設計判断**:

- **複数タグ混在時の優先順位**: `internal` が含まれる場合は sub_line/volatile を採用 (より制約が強い)。次に `conversation`、最後に `event_message`
- **意味分類タグの保持**: handoff §段階 4-C の「ハマりどころ」に従い、`creation` / `web_research` 等は純粋な意味分類タグなのでそのまま残す。Chronicle / Memopedia / recall 経路で参照される
- **Y 案で `model_type=lightweight` を保留**: `/run_playbook` Spell 実装で「メインライン判断 + 発話統合」が完成すると model_type 機能は不要 (= 自然に廃止できる)。それまでに削除すると router 系が重量級モデルで動いてコスト増 + 応答速度低下 → 段階的安全性のため保留
- **machine-translation の妥当性**: dry-run で全 33 件の差分を目視確認、機械翻訳できないイレギュラーパターン無し
- **互換性**: `_store_memory` は既に `line_role` / `scope` 引数対応済 (Phase 1.3 メタ判断 scope='discardable' 対応の流用)、Pydantic 側のフィールド追加で完成

**実装ファイル**:

- `scripts/migrate_playbooks_to_lines.py` 新規 — 半自動翻訳スクリプト (`--dry-run` / `--apply` / `--filter` / `--no-diff`)
- `sea/playbook_models.py` — `MemorizeNodeDef.line_role` / `scope` フィールド追加 + `tags` の description 更新
- `sea/runtime_engine.py` — `lg_memorize_node` で line_role / scope を `_store_memory` に渡す経路 + sea_trace ログ強化
- `builtin_data/playbooks/public/*.json` — 33 ファイルの一括翻訳結果

**DB 反映**:

- `python scripts/import_all_playbooks.py --force`: Updated 44 / Imported 0 / Pruned 0
- DB 内の Playbook 数: 43 件、`router_callable=true` 18 件 (`/run_playbook` Spell の対象)

**検証**:

- ruff check: All checks passed
- 関連 7 ファイル合計: 134 件 pass / 0 新規回帰
- 実機検証は次セッション以降 (まはー側で 3 シナリオ: ユーザー会話 / 自律 Pulse / メタ判断 の context 維持確認)

**次の段階**: `/run_playbook` Spell 実装 (`nested_subline_spell.md` v0.1 の段階移行 7 ステップ)。Spell 実装後に 4-D で旧仕様コード完全削除 (`include_internal` / `pulse:{uuid}` タグ併行記録 / `model_type` / `LLMNodeDef.context_profile` Pydantic フィールド)。

### v0.22 (2026-05-01) — 段階 4-B 完了: sub_play の親伝搬を line ベースに統一 + report_to_parent リネーム

**確定事項**:

- `sea/runtime_nodes.py` の sub_play ノード完了処理で、サブラインからの結果を親ラインに記録する `_store_memory` 呼び出しを line メタデータベースに移行
  - 旧: `runtime._store_memory(tags=["conversation"], ...)` (context 包含目的でメインライン用タグを付けていた、4-A 前のフィルタ仕様の名残)
  - 新: `runtime._store_memory(line_role="main_line", scope="committed", ...)` + 意味分類タグなし
- `report_to_main` → `report_to_parent` への全面リネーム
  - state キー (`state["report_to_main"]` → `state["report_to_parent"]`)
  - ログ・コメント
  - `sea/playbook_models.py` の SubPlayNodeDef.line docstring
  - `tests/test_subplay_line.py` の関数名・assertion 11 件
  - `docs/intent/persona_cognition/phases/sub_line_playbook_sample.md`
- 旧名 `report_to_main` はコード側から完全消去 (経緯記録のドキュメントのみ残置)

**設計判断**:

- **3 経路の親伝搬は維持**: (1) `state["_messages"]` (2) 親 PulseContext (3) SAIMemory。目的・順序ともに 4-B 前と同じ
- **意味分類タグは渡さず**: `_store_memory` 内で自動付与される `playbook:{name}` のみ metadata.tags に残る。recall/search 用途には Playbook 名で絞れる
- **記録先は親ライン側のメタデータ**: サブラインからの report_to_parent は「親ラインの会話の一部として」SAIMemory に記録するので `line_role="main_line"` が正しい (4-B スコープ確認時にまはー指摘あり、私の初期説明が雑だった点を修正)
- **リネーム理由**: 入れ子サブライン (深さ 2 以上) では親が必ずメインラインとは限らない。「親ラインに上がる」という挙動を正確に表す `report_to_parent` に改名

**実装ファイル**:

- `sea/runtime_nodes.py` — sub_play ノード完了処理の line ベース化 + リネーム
- `sea/playbook_models.py` — docstring 更新
- `docs/intent/persona_cognition/phases/sub_line_playbook_sample.md` — サンプル更新

**テスト追加 (4-B 検証 + 4-A 後追い)**:

- `tests/test_subplay_line.py`: 11 件の assertion を line メタデータ仕様に更新
- `tests/test_sai_memory_storage.py` +4 件:
  - `add_message → get_messages_paginated` の line metadata round-trip
  - `scope='discardable'` 除外維持 (Phase 1.3 挙動)
  - legacy 行 (line metadata 未指定) の DB 上の挙動
  - `get_messages_from_id` (anchor 経路) の line metadata 取り回し
- `tests/test_payload_context_filter.py` 新規 +28 件:
  - 4-A で adapter.py に新設した `_payload_passes_context_filter` ヘルパの網羅単体テスト
  - line_role / scope / pulse_id override / legacy 互換 (NULL→main_line, NULL→committed, "pulse:{uuid}" タグ経由) / required_tags 互換 / 防御的処理を全カバー

**検証**:

- ruff check: All checks passed
- tests/test_subplay_line.py: 11 passed
- tests/test_payload_context_filter.py: 28 passed
- 関連 6 ファイル合計: 126 passed / 0 新規 failed

**実機検証は省略**: 現状 `line='sub'` で sub_play する Playbook は皆無 (`web_search_sub` は v0.19 で削除済み)。`/run_playbook` Spell 実装でサブラインが再活性化したときに end-to-end が走る。今回は単体テスト 28+11+4 件で line メタデータ経路を網羅検証する判断。

**次の段階**: 4-C (`memorize.tags` 整理 + `migrate_playbooks_to_lines.py`)、4-D (`include_internal` / `pulse:{uuid}` タグ併行記録 など DEPRECATED コードの完全削除) を継続実施。

### v0.21 (2026-05-01) — 段階 4-A 完了: context 構築を line_role/scope ベースに切替

**確定事項**:

- `_prepare_context` (sea/runtime_context.py) の `required_tags = ["conversation", "event_message"]` ハードコードを廃止
- 代わりに `required_line_roles=["main_line"]` + `required_scopes=["committed"]` で context を組み立てるよう統一
- adapter / history_manager / storage 層に `required_line_roles` / `required_scopes` 引数を追加し、context 経路から渡す
- `sea/runtime.py:1559` (metabolism anchor) と `persona/mixins/generation.py:170` (persona generation) の `required_tags=["conversation"]` も同様に line ベース化
- `Message` データクラスに `line_role` / `line_id` / `scope` / `pulse_id` を追加、`_row_to_message` を可変列対応に。context 経路の SELECT (`get_messages_paginated`, `get_messages_from_id`) を 11 列拡張

**設計判断**:

- **legacy 互換**: line_role IS NULL → 'main_line' 扱い、scope IS NULL → 'committed' 扱い。Phase 1 以前の大量データが context から消えるのを防ぐ
- **Pulse-scoped overrides**: `exclude_pulse_id` 一致は line/scope 関係なく除外、`pulse_id` 一致は line/scope 関係なく強制包含 (従来挙動維持)
- **`include_internal` フォールバック**: 4-C で Playbook 側の memorize.tags が整理されるまでの暫定措置として、`include_internal=True` のときは `sub_line` を許可。完全廃止は 4-D
- **search/recall 経路の `required_tags` は残置**: api/recall, memopedia/generator, memory_search_brief, record_wait, recall_conversation_with は意味分類フィルタとしてタグを使い続ける (4-D で整理予定)
- **Python 側フィルタ統一**: adapter.py に `_payload_passes_context_filter` ヘルパを新設、4 関数 (`recent_persona_messages` / `_by_count` / `_balanced` / `persona_messages_from_anchor`) の Python 側フィルタロジックを共通化

**実装ファイル**:

- `sea/runtime_context.py` — `_prepare_context` の line ベース化
- `saiverse_memory/adapter.py` — `_payload_passes_context_filter` ヘルパ + 4 関数のシグネチャ拡張
- `persona/history_manager.py` — 4 関数のシグネチャ拡張 (`required_line_roles` / `required_scopes` 追加)
- `sai_memory/memory/storage.py` — `Message` データクラス拡張 + 2 関数の SELECT 拡張
- `sea/runtime.py:1559` — metabolism anchor の `required_tags` 廃止
- `persona/mixins/generation.py:170` — persona generation の `required_tags` 廃止

**検証**:

- ruff check: All checks passed
- tests: 629 passed / 5 failed (failed 5 件は本変更前から既存で失敗、stash で確認済)
- 実機 air_city_a: ログに `line_roles=['main_line'], scopes=['committed']` が出力、`Got 60 history messages` で legacy 互換動作も確認

**次の段階**: 4-B (`sub_play` の `report_to_main` を line ベースに統一 + `report_to_parent` リネーム)、4-C (`memorize.tags` 整理 + `migrate_playbooks_to_lines.py`)、4-D (DEPRECATED コード削除) を継続実施。

### v0.20 (2026-05-01) — line と memorize タグの責務分離 + 入れ子サブライン Spell の Intent 起草

**確定事項**:

- `line_role` / `line_id` / `scope` カラム (Phase 1 実装済) と `metadata.tags` の責務を明確に分離
  - **Line**: メッセージの階層属性と永続性 (= context 構築の主軸)
  - **タグ**: 意味分類のみ (= 検索・recall・連携用、context 構築には関与しない)
- 二重制御 5 件を特定し、移行プラン (段階 4-A〜4-D) を策定
- `/run_playbook` Spell 機構の Intent を起草 (`nested_subline_spell.md` v0.1)
- 揮発設計を line ベースに乗せ直し (旧 `internal` タグでの揮発表現を廃止前提)
- Phase 3 残作業の依存グラフを確定:
  ```
  [line vs タグ整理] → [migrate_playbooks_to_lines.py] → [/run_playbook 実装]
  → [track_user_conversation 書き換え] → [meta_user 廃止] → [実機検証]
  ```

**追加 Intent doc**:

- `nested_subline_spell.md` v0.1 — `/run_playbook` Spell 機構の設計
- `line_tag_responsibility.md` v0.1 — line と memorize タグの責務分離

**改訂理由**:

入れ子サブライン Spell (`/run_playbook`) を実装する前に、まはー指摘で「`line` と `memorize` タグの両方が context 制御に関与している二重制御の問題」が判明。Phase 1 で line_role / scope カラムを追加した時点で「タグ参照を捨てて line 制御に統一する」つもりだったが、移行が中途半端で残っていた。

このまま入れ子サブライン Spell を実装すると「二重制御の上に新機構を積む」ことになり、設計上の負債が増える。先に整理を済ませる判断。

工数見積:
- 完全 line ベース統一案 (タグ全廃): 4 ファイル + 5+ Playbook、2000+ LOC、Phase 3 全翻訳と同規模 → 重すぎる
- 責務分離案 (採用): タグは search / recall 用に残す、context 構築だけ line ベースに統一 → Phase 3 翻訳と一体化で 2〜3 セッション

不変条件 2 (単一主体の記憶), 7 (キャッシュヒット継続), 11 (メタ判断はペルソナ自身の思考) の保証がより厳密になる副作用あり。

### v0.19 (2026-05-01) — Phase 3 翻訳前段の Playbook 整理

**確定事項**:

- 旧自律稼働プロトタイプ用 Playbook 群を一括削除 (`meta_auto`, `meta_auto_full`, `sub_router_auto`, `sub_perceive`, `sub_reaction`, `sub_finalize_auto`, `sub_execute_phase`, `sub_detect_situation_change`, `sub_generate_want`, `wait`)
- テスト用 / 残骸 Playbook を削除 (`meta_websearch_demo`, `detail_recall_playbook`, `meta_agentic`, `agentic_chat_playbook`)
- Spell 階層に置き換え可能な Playbook を削除し、対応するツールに `spell=True` を付与:
  - `memory_recall_playbook` → `memory_recall_unified` Spell (既存)
  - `web_search_step` → `source_web` Playbook (依存していた `deep_research` は `source_web` 呼び出しに切り替え)
  - `uri_view` → `resolve_uri` Spell (新規 Spell 化)
  - `send_email_to_user_playbook` → `send_email_to_user` Spell (新規 Spell 化)
- `web_search_sub` (Phase C-2b 動作確認サンプル) は `phases/sub_line_playbook_sample.md` に内容を保存して本体 Playbook 削除
- `run_meta_auto` 関数 (sea/runtime.py) と関連分岐 (sea/pulse_controller.py の auto-without-meta_playbook 分岐、`_choose_playbook` の `meta_auto` fallback) を削除。auto pulse は `meta_playbook` 必須化
- `ConversationManager` (saiverse/conversation_manager.py) を no-op 化。Building 内 AI 自律会話は PulseScheduler + `track_autonomous` 経由に統一済みのため、旧プロトタイプの周回駆動は不要
- 削除を反映してテスト類を整理 (`tests/sea/test_runtime_regression.py` の `run_meta_auto` テスト、`tests/test_subplay_line.py` の `web_search_sub` テスト、`test_fixtures/test_api.py` の `EXPECTED_PLAYBOOKS`)
- `builtin_data/tools/detail_recall.py` を削除 (`detail_recall_playbook` 専用ツールだったため)

**改訂理由**:

Phase 3 残件「既存 Playbook の `context_profile` / `model_type` → `line: "main"|"sub"` 翻訳」に着手する前に、翻訳対象の総数を減らして作業を圧縮するため。Spell 階層 (`memory_recall_unified` / `resolve_uri` / `searxng_search` / `read_url_*` 等) が充実してきており、旧 Playbook で表現していたパターンの大半は Spell 単発呼び出しで賄えるようになっていた。

加えて、新認知モデル (Track + メタ判断 Playbook) への完全移行に伴い、旧自律稼働プロトタイプ (`meta_auto` 経路 + `ConversationManager` の周回駆動) は呼ばれなくなっていた。コード側で残骸を抱え続けると Phase 3 翻訳作業時に「これは現役か旧版か」の判定が増えてミスが起きやすくなるため、翻訳前に旧経路を完全に断つ判断。

DB 上の Playbook は 67 → 48 件。翻訳対象 (`context_profile` / `model_type` を使う Playbook) も同時に減る (旧プロトタイプ系が消えたため)。

不変条件としての変更はなし。あくまで Phase 3 翻訳前のクリーンアップ。

**追補 (同日)**:

整理直後の動作確認で「Disk から消した Playbook が DB に残り、`router_callable=1` のものがシステムプロンプトに乗ってペルソナが Spell として呼ぼうとして警告 (`Unknown spell 'read_url_content'` 等) が出る」事象が発覚。

原因は起動時の `sync_playbooks_from_files` (`saiverse/playbook_sync.py`) が import 専用で orphan prune を行わない設計だったため。`scripts/import_all_playbooks.py` 側には `prune_orphan_playbooks` が実装済みだったが、これは手動実行 / バージョンアップフロー経由でしか走らない。

修正:
- `sync_playbooks_from_files` 内に `_prune_orphan_playbooks` を実装し、毎起動時に Disk と DB の整合性を取る
- 対象は scope='public' AND source_file IS NOT NULL かつソースファイルが disk に無い Playbook
- `save_playbook` ツール経由 (source_file IS NULL) は保護される
- addon 関連は expansion ファイルが存在する限り保護され、addon を一時的に外すと対応 Playbook も削除されるが、addon 再追加で復元される
- DB 残骸の即時クリーンアップとして `read_url_content` / `searxng_search` / `x_reply` (旧 builtin → expansion 移行残骸) は新 prune で削除、`sub_speak_meta` / `sub_speak_simple` (source_file IS NULL の旧 meta layer 残骸) は手動削除

DB 上の Playbook は 48 → 43 件。

### v0.18 (2026-05-01) — 自律先制と外部 alert のレース解消 (Phase 2.6)

**確定事項**:

- `set_alert` の状態遷移と observer 通知を分離。既 running の Track への set_alert は状態 no-op のまま、observer には `target_already_running=True` フラグ付きで通知する
- context に `target_track_title` / `target_track_type` も常に乗せて、メタ判断者が UUID でなく自然言語で対象を識別できるようにする
- `meta_judgment.json` judge prompt に「target_already_running=true は自律先制と外部イベントの衝突 → 通常は継続判断で OK」のガイダンスと独白例を追加
- `MetaLayer._build_state_message` (legacy path) もフラグを自然言語化

**改訂理由**:

実機検証 (2026-05-01) で Pulse A (自律メタ判断で対 user1 を pending→running に先制起動) の直後に Pulse B (ユーザー発話起因のメタ判断) が起動したが、context に alert 情報が乗っていないことを発見。Pulse A のエアは「自分が pending→running にした」と認識し、Pulse B のエアは「特に理由なくメタ判断が走った」と認識する不整合が起きていた。

原因は `set_alert` が既 running の Track に対して状態遷移と observer 通知を**両方** no-op にしていたため。仕様としては状態遷移 no-op は正しいが、外部イベント (ユーザー発話) の事実そのものはメタ判断者に届けるべきだった。状態遷移と通知の責務を分離することで、ペルソナの自律先制と外部イベントが時間的に衝突しても、メタ判断者がきちんと認識できるようになる。

不変条件 11 ("メタ判断 = ペルソナ自身の思考の流れ") の延長として、「思考の連続性」が外部イベントとの衝突で断絶しないための基盤整備。

### v0.17 (2026-05-01) — SAIMemory `messages.pulse_id` カラム化 (Phase 2.5)

**確定事項**:

- per-persona memory.db の messages テーブルに `pulse_id TEXT` 専用カラム + INDEX (`idx_messages_pulse_id`) を追加
- `_store_memory` (sea/runtime.py) は当面、列とタグ (`metadata.tags` の `"pulse:{uuid}"`) の両方に書き込む (互換維持)。読み出し経路が全部カラム参照に移行したらタグ書き込みは廃止予定
- `add_message` (sai_memory/memory/storage.py) と `_append_message` (saiverse_memory/adapter.py) に `pulse_id` 引数を追加
- `_promote_meta_judgment_in_pulse` の SQL を `pulse_id = ?` の INDEX 付き直接 WHERE に書き換え (旧 json_each 線形スキャンから O(log N) へ)
- `_backfill_messages_pulse_id` で既存行の pulse_id をタグから抽出して埋める (起動時 1 回、べき等)

**改訂理由**:

Phase 2 実装直後の実機検証で `_promote_meta_judgment_in_pulse` が `OperationalError: no such column: pulse_id` で落ちることが発覚。前セッション (0cfe61c) の SQL 設計が SAIMemory の保存実装 (タグ経由) と整合していなかった。

応急処置として一旦 json_each(metadata, '$.tags') 経由に書き換えたが、本質的にはタグ照会は (1) INDEX が効かず将来スケールに対応できない (2) `pulse:` プレフィックス命名規則が暗黙の前提で脆い (3) `pulse_logs` テーブルとの JOIN や Pulse 単位集計を素直に書けない、という 3 つの不満があった。Phase 2 で pulse_id ベースの参照経路を入れたばかりで関連箇所の記憶も新しいうちに、専用カラム化を済ませた。

メタ判断ログ機構 (Phase 2) が動き始める前に基盤を固める判断。`pulse_logs` テーブルが既に pulse_id カラムを持っているため、整合性も同時に取れた。

### v0.16 (2026-04-30) — メタ判断 Pulse の per-persona 直列化 + メタ判断ログ機構の運用

**確定事項 (Part 1: 直列化)**:

- 同一ペルソナのメタ判断 Pulse は同時 1 本に制限する。`MetaLayer` が persona_id ごとの `threading.Lock` を保持し、`on_track_alert` / `on_periodic_tick` の両入口で取得待ちする
- 競合時は **wait** で確定 (skip しない)。理由: alert を skip すると即応イベントを取りこぼし、定期 tick を skip するとメインキャッシュ TTL 切れを誘発する
- 別ペルソナ同士は Lock が独立しているため並列実行可能 (per-persona 粒度)
- chat thread のブロックは一時的に許容。将来「安全な中断機構」を作る意思は持つ
- `02_mechanics.md` §"メタ判断 Pulse は同時 1 本 (per-persona 直列化)" を追加

**確定事項 (Part 2: ログ機構の運用)**:

- `meta_judgment_log` スキーマを v0.15 (独白 + /spell 方式) に整合化。旧 4 値 enum (`judgment_action`) と関連カラム (`switch_to_track_id` / `new_track_spec` / `notify_to_track` / `raw_response`) を廃止し、`spells_emitted` (JSON 配列) を新設
- 書き込み機構を実装:
  - **Playbook path**: `_run_spell_loop` が `pulse_type == 'meta_judgment'` のとき `PulseContext.meta_judgment_buffer` に独白 + spell + 結果を蓄積。Pulse 完了時 (`runtime_graph.py`) に `MetaLayer._record_judgment_log` を呼んで永続化
  - **legacy path**: `MetaLayer._run_judgment` が判断ループ中に直接バッファし、`finally` で `_record_judgment_log` を呼ぶ
- 動的注入: `MetaLayer._build_recent_judgments_block(persona_id, n=5)` で過去 5 件を箇条書きにし、Playbook path は `meta_judgment.json` の `recent_judgments` 入力経由で `{recent_judgments}` 展開、legacy path は状態メッセージ末尾に追記
- これで: (a) 過去のメタ判断を踏まえた連続性 (Intent A v0.14 メタ判断ログ領域の本来意図)、(b) 古い snapshot 問題への対処 (前回操作の結果が今回の判断材料になる) を達成

**改訂理由**:

Part 1: Phase C-2 のテスト中に「pending と思って pause したら裏で alert になっていた」現象を観測。原因: alert observer (chat thread 経由) と AutonomyManager 定期 tick (background thread 経由) が別 thread で同じ persona に対するメタ判断 Playbook を起動し、それぞれが独立した snapshot を見て Track 操作を発動していた。不変条件 11 ("メタ判断 = ペルソナ自身の思考の流れ 1 本") を構造で守るには、入口での直列化が必要だった。

Part 2: `meta_judgment_log` テーブルは Phase 1 で新設したが、書き込み・読み込み・動的注入は Phase 2 以降で運用予定 (`phase_1_base.md`) と明記されていた。Part 1 で並列実行を抑止しても、過去の判断結果を参照できないと: (1) 別 Track からアラートが連続して来た時に毎回独立判断になり判断が劣化、(2) 古い snapshot 問題 (前回 pause したのに「pending と思って pause」をまた書く等) が残る。Part 2 でログ機構を実運用に乗せることで両者を解消。

**実機検証で発覚した追加修正 (2026-05-01)**:

Phase 2 + Phase 2.5 の動作確認中に SQL レベルで以下の不整合が見つかり、同セッション内で順次修正した:

1. **scope 昇格 SQL のカラム不在 (cfb68b4 で応急処置 → 1728dbf で本修正)**: `_promote_meta_judgment_in_pulse` が `messages.pulse_id` カラムを直接叩いていたが、SAIMemory の messages テーブルには pulse_id 専用カラムが無く `metadata.tags` の `"pulse:{uuid}"` で保存されていた。応急処置として json_each 経由のタグ照会に書き換え、その後 Phase 2.5 で専用カラム化して INDEX 利用に戻した。

2. **fuzzy spell parser のノイズ WARNING (cfb68b4)**: `_parse_fuzzy_spell_args` が strict parser をフォールバック前置で呼ぶため、fuzzy 形式 (`/spell name key='value'`) でも常に WARNING が出ていた。silent モードを追加して fuzzy 経由は DEBUG に降格。canonical 形式での失敗のみ WARNING を維持。

3. **`committed_to_main_cache` が常に False (b2c0c81)**: `runtime_graph.py` の Pulse 完了処理で、`_apply_deferred_track_ops` が `deferred_track_ops` を clear した**後**で `any(op.op_type == 'activate')` を評価していたため、Track 切替が発動した判断ターンでも `committed_to_main_cache=False` と記録されていた。判定を apply の**前**に移動。CRITICAL ORDER のコメントで再発防止。
   - 実機影響: messages 本体は scope='committed' で正しく残っていたため Track 切替動作自体は問題なし。次回メタ判断時の judge prompt 注入で `[switch]` マーカーが付かず、過去の重要決断を全て「継続」として読んでしまう不整合があった (実害は Phase 2 の効果が半減する程度)。

### v0.15 (2026-04-30) — メタ判断を独白 + /spell 方式に回帰

**確定事項**:

- メタ判断 LLM は **構造化出力 (response_schema) を使わない**。自然言語の独白の中に `/spell <name> ...` を埋め込んで Track 操作を発動する形式に統一
- 旧 4 値 enum (`continue`/`switch`/`wait`/`close`) と `meta_judgment_dispatch` ツールを廃止
- scope 昇格 (`'discardable'` → `'committed'`) は Track 切替系スペル発動時に `_track_common._maybe_promote_meta_judgment` が実施。ツール経由ではなく「Track 切替スペル発動 = 判断確定」と解釈
- 「アクティブ Track なし状態に遷移」は `/spell track_pause` のみ発動 (新規 activate しない) で表現
- `02_mechanics.md` から response_schema セクションを削除し、独白 + スペル方式の応答形式 + scope 昇格機構の説明に差し替え

**改訂理由**:

v0.12〜v0.14 で構造化出力ベースに走った結果、以下 3 つの問題が顕在化:

1. **JSON 混入によるメインキャッシュ汚染** (不変条件 11 違反): メタ判断ターンは [B] 移動時にメインキャッシュに乗る。一度 JSON が混入したキャッシュ末尾を持つ会話は、以降のメインライン発話も JSON 化する副作用が出る。intent A の「メタ判断 = ペルソナ自身の思考の流れ」と矛盾していた
2. **マルチプロバイダ互換性の制約**: Gemini SDK は `any_of` のみ、OpenAI strict は anyOf 非対応、Anthropic は anyOf 16 個制限と、プロバイダ毎の差が大きい。構造化出力依存だとペルソナのモデル選択が制約される
3. **wait/close enum の冗長**: 4 値構造は wait/close が switch のサブセットでしかなく、「アクティブなし状態へ遷移」の選択肢が enum で表現されていなかった

intent docs 自身に矛盾が含まれていた (本文は「独白 + スペル」原則、response_schema セクションは構造化出力) ことを 2026-04-30 のデバッグセッションで発見し、本来の設計に戻すと同時に response_schema 関連の記述を撤去した。実装上、SEA runtime のスペルループ機構 (`_run_spell_loop`) が既に Playbook の自然言語 LLM ノードに対して動くため、Playbook 側の変更だけで切り替え可能だった。

### v0.14 (2026-04-29) — ライン 3 軸独立化 + 7 層ストレージモデル

**確定事項**:

- **ライン (Line) の 3 軸独立化**: モデル/キャッシュ種別 (メイン/サブ) × 呼び出し関係 (親/子) × Pulse 階層位置 (起点/入れ子) の 3 軸に整理。旧 v0.8〜v0.13 で混在していた語義 (「メインライン = 重量級 + Track 横断」等) を分離
- **7 層ストレージモデル**: メッセージ・思考・ログを 7 つの層 (メタ判断ログ / メインキャッシュ / Track 内サブキャッシュ群 / 入れ子一時 / Track ローカルログ / SAIMemory / アーカイブ) で整理。タグベース管理の限界を解消
- **「呼んだラインの記録レイヤーに従う」原則**: Spell loop 等の記録は呼び出し元のラインに従う (固定タグ排除)
- **メタ判断フロー再定義**: メタ判断は Track 内メインラインからの一瞬の分岐として動く。継続時は分岐ターン破棄 + メタ判断ログ領域に保存、移動時は分岐ターンが新 Track の冒頭来歴に。メインキャッシュは Track 横断 1 本を維持
- **メタ判断ログ独立領域**: 全メタ判断結果を独立保存し、次のメタ判断時に参考情報として動的注入。判断の連続性を確保
- **`report_to_main` → `report_to_parent` 改名**: 親が必ずメインラインとは限らないため
- **不変条件 12 新規**: 親-子ラインの寿命関係 (子は親の中で完結)

**改訂理由**:

旧 v0.13 までの「メインライン = 重量級モデル + Track 横断混合キャッシュ」「サブライン = 軽量モデル + Track 内連続キャッシュ」は**役割と一体**で語っていた。3 つの軸が混ざっていたため、「親サブから子メインを呼ぶ」「親サブから子サブを呼ぶ」のような組み合わせを論理的に表現できなかった。v0.14 で 3 軸を分離。

旧 v0.12〜v0.13 の「軽量で要約 → 重量級で独立判断」の 2 段階フローはコスト効率が悪かった。Track ごとに重量級キャッシュを別建てするとコスト破産する。v0.14 で「メインキャッシュ Track 横断 1 本 + メタ判断ログ独立領域 + commit/discard 機構」に再設計。

### v0.10〜v0.13 (2026-04-28) — Pulse 階層と Track 特性の整備

**確定事項**:

- **v0.10**: Track 特性 / Track パラメータ / 内部 alert / スケジュール統合方針の導入
- **v0.11〜v0.13**: メインラインの Pulse 開始プロンプト構成 (固定/動的分離) / Pulse 階層 (メインライン Pulse / サブライン Pulse) / 7 制御点

**改訂理由**:

「Pulse」が単一概念で扱われていたが、実際にはモデル種別とキャッシュ管理の単位が違うため 2 階層に分離する必要があった。これにより「Claude メイン + ローカルサブ」のような環境別最適化が可能に。

### v0.9 (2026-04-28) — 永続 Track / alert 状態 / ACTIVITY_STATE / ライン分岐仕様

**確定事項**:

- **永続 Track の導入**: `is_persistent=true` で完了/中止しない Track。対ユーザー会話 Track (ユーザーごと 1 個) + 交流 Track (ペルソナにつき 1 個)
- **alert 状態の導入**: pending と running/waiting の中間、可及的速やかに対応が必要
- **ACTIVITY_STATE 4 段階**: Stop / Sleep / Idle / Active で旧 INTERACTION_MODE を置き換え
- **`SLEEP_ON_CACHE_EXPIRE` フラグ**: API 料金保護
- **`SubPlayNodeDef.line` フィールド**: "main"|"sub"
- **サブライン分岐 = 親 messages のコピー** (完全独立ではない)
- **`output_schema` の `report_to_main` 必須化** (`can_run_in_sub_line=true` の Playbook で)

**改訂理由**:

ユーザーとの長期的関係性を「永続 Track」として明示することで、再会時の文脈復元が自然になる。

`alert` 状態の導入により、メタレイヤーが「すぐ対応すべき / 後回しでいい」を判断できる粒度になった。旧モデルでは pending と running の二択しかなく、ユーザー発話のような即応すべきイベントの優先度を表現しづらかった。

### v0.8 (2026-04-28) — Note / 行動 Track / Line / ペルソナ認知モデル基盤

**確定事項**:

- **Note 概念の導入**: 関心の固まりを表す単位。Memopedia ページ + メッセージ群を束ねる
- **Note の type は 3 種類のみ**: person / project / vocation
- **行動 Track と SAIMemory thread の分離**: 3 人会話問題の解決
- **Line (ライン) の導入**: メインライン / サブライン / モニタリングライン
- **メタレイヤーは Playbook 内 LLM ノードで実装** (Phase C-1 別系統メッセージは廃止)
- **Track 種別ごとに専用 Playbook を新規作成** ((a) 路線)

**改訂理由**:

3 人会話で「対 A」「対 B」両方の Track に同じメッセージを書き込む必要があるという問題から、初期案の「track_id = thread_id」を撤回。Note 概念で多対多を実現。

### v0.6〜v0.7 (2026-04-28) — Track 種別整理と Handler パターン

**確定事項**:

- Track 種別を `track_type` で表現
- Handler パターンを Track 種別ごとに繰り返し適用
- 種別ごとの追加情報は `action_tracks.metadata` JSON に格納 (早すぎる正規化を避ける)
- Track 特性レイヤーは TrackManager 変更なしで実装可能に

**改訂理由**:

新しい Track 種別を追加するたびに TrackManager に手を入れるのは責務肥大化を招く。Phase C-1 で確立した Handler パターン (UserConversation / Social) を繰り返し適用する形で拡張可能にした。

### v0.5 (2026-04-25) — メインサイクルと Track 内動作パターン

**確定事項**:

- メインライン = メタレイヤー + Track 内重量級判断 (同じキャッシュ連続)
- サブライン = アクティブ Track の Playbook 実行
- Track 内動作 3 パターン: 他者会話 / タスク遂行ループ / 待機
- モニタリングラインは Track ではない、独立した並列ラインとして将来追加

### v0.2〜v0.4 (2026-04-25) — メタレイヤー / 状態モデル / 応答待ち統合

**確定事項**:

- AutonomyManager は「責務再配置と拡張」 (取り壊しではない)
- メタレイヤーから ExecutionRequest 投下で線切り替えが成立
- 状態モデル: running / pending / waiting / unstarted / completed / aborted の 6 状態 + `is_forgotten` 直交フラグ (v0.4)
- 「実行中は 1 本」は `track_activate` の実装で自動保証
- 応答待ちは SAIVerse 側自動ポーリング → イベント通知でメタレイヤー判断
- 多重応答時は新しい Track 優先

---

## Intent B: persona_action_tracks.md の改訂

### v0.11 (2026-04-29) — 7 層ストレージのテーブル化 + handoff 解消

**確定事項**:

- **7 層ストレージのテーブル対応**: 各層を本ドキュメントのテーブル設計にマッピング
- **`meta_judgment_log` テーブル新設**: メタ判断の全履歴を独立保存
- **`track_local_logs` テーブル新設**: Track 内のイベント・モニタログ・起点サブの中間ステップトレース
- **`messages` メタデータ拡張**: `line_role` / `line_id` / `scope` / `paired_action_text` カラム追加
- **`report_to_main` → `report_to_parent` 改名**: 親が必ずメインラインとは限らないため
- **ライン階層管理機構の最小実装**: `PulseContext._line_stack` で親子関係を追跡
- **Spell loop 保存方針**: 「呼んだラインの記録レイヤーに従う」原則。`tags=["conversation"]` 固定を廃止
- **action 文ペア保存方針**: action 文を user role 単独保存せず、応答メッセージの `paired_action_text` に紐付け
- **Pulse Logs の役割縮退**: 実行トレース専用へ
- **handoff 3 経路問題の解決**: 経路 A (Spell loop) / B (`_emit_say` で `speak: false` を skip) / C (action 文ペア保存) を Phase 0 タスクとして明文化

**改訂理由**:

handoff 観察記録 (`handoff_track_context_management.md`) で報告された多重記録問題が、Spell loop / `_emit_say` / action 文の保存先がバラバラだったことに起因していた。Intent A v0.14 の 7 層ストレージモデルを実装側に展開する形で、テーブル設計と保存方針を整理。

### v0.10 (2026-04-28) — Pulse スケジューラの責務分離

**確定事項**:

- Pulse スケジューラを 2 系統 (MainLineScheduler / SubLineScheduler) に分離
- Handler に v0.10 拡張属性追加 (`default_pulse_interval` / `default_max_consecutive_pulses` / `default_subline_pulse_interval`)
- 7 制御点の実装場所明確化 (action_tracks.metadata + 環境変数 + Handler 属性 + モデル設定)
- AutonomyManager は MainLineScheduler に再配置
- 環境別デフォルト値 (Pattern A/B/C) を明示
- Phase C-3 を C-3a/b/c/d に分割

**改訂理由**:

メインライン Pulse とサブライン Pulse の頻度制御を 1 つのスケジューラで管理するのは無理があった。責務を分離して各 Scheduler を独立実装することで、環境差 (Claude / ローカル / 混在) を仕様変更なしで吸収できる構造に。

### v0.9 (2026-04-28) — Playbook ノードの line フィールド + 段階廃止計画

**確定事項**:

- `SubPlayNodeDef.line: "main"|"sub"` フィールド追加 (デフォルト "main")
- 最初に呼ばれる Playbook はメインライン強制
- サブライン分岐 = 親 messages のコピー、軽量モデル実行
- サブライン完了時に `report_to_main` がメインラインに system タグ付き user メッセージとして append
- `output_schema` の `report_to_main` を `can_run_in_sub_line=true` の Playbook で必須化
- 旧 `context_profile` / `model_type` / `exclude_pulse_id` を段階的に廃止 (C-2a → C-2b → C-2c)
- Phase C-1 MetaLayer は alert ディスパッチ役へ縮退、判断ロジックは Playbook へ移植
- 完全独立コンテキスト (worker 系) は本ライン仕様の上で将来別途実装

### v0.7〜v0.8 (2026-04-28) — Track 特性レイヤー + Pulse プロンプト構造

**確定事項**:

- Track 特性レイヤーは Handler パターンの繰り返し適用で実装する (TrackManager は変更しない)
- 種別ごとの追加情報は `action_tracks.metadata` JSON に格納 (早すぎる正規化を避ける)
- Track パラメータは `metadata.parameters` に連続値として持つ
- 内部 alert は Handler の `tick()` メソッド内で判定 + 既存 `set_alert` 発火
- Handler tick は SAIVerseManager の background loop に統合 (`SAIVERSE_HANDLER_TICK_INTERVAL_SECONDS`)
- メタレイヤーには `on_periodic_tick` 入口を追加、`on_track_alert` と同じ判断ループを共有
- 「Pulse 完了直後にメタレイヤー起動」は **採用しない** (ユーザー応答待ち優先)
- ScheduleManager は段階的に Track 特性に吸収、v0.4.0 で完全移行
- Track 種別ごとに専用 Playbook を新規作成する方針 ((a) 路線)
- Handler に `pulse_completion_notice` 文字列 + `post_complete_behavior` 列挙
- Pulse プロンプト = 固定情報 (初回のみ先頭) + 動的情報 (毎 Pulse 末尾)

### v0.4〜v0.6 (2026-04-25〜2026-04-28) — 状態モデル / 永続 Track / 多者会話

**確定事項**:

- 状態モデル: `running` / `pending` / `waiting` / `unstarted` / `completed` / `aborted` の 6 状態 + `is_forgotten` 直交フラグ (v0.4)
- メタレイヤーのトラック管理は 10 個のツール群 (`track_*`) (v0.4)
- 応答待ちは SAIVerse 側自動ポーリング → イベント通知でメタレイヤー判断 (v0.4)
- 多重応答時は新しい Track 優先 (v0.4)
- 永続 Track (`is_persistent=true`) の導入: 対ユーザー会話 + 交流 Track (v0.6)
- 状態モデルに `alert` 追加 (v0.6)
- `output_target` フィールド追加 (v0.6)
- 「対ペルソナ会話 Track」は持たない (Person Note + 交流 Track の組み合わせ) (v0.6)
- `ACTIVITY_STATE` 4 段階 (v0.6)
- 多者会話のループ防止: audience 厳格 + メタレイヤー判断 + 環境変数によるヒント (v0.6)

### v0.3 (2026-04-25) — Track と thread の分離 + Note 概念の導入

**確定事項**:

- track_id は独立した UUID
- Track と thread は別概念、メッセージは thread に物理保存
- Note を介してメッセージのメンバーシップを多対多管理
- Note の type は person / project / vocation の 3 種類のみ
- audience による自動 Note メンバーシップ生成
- メンバーシップ付与は Metabolism 時に後付け
- 再開時は起源 Track の認識回復が主、他 Track 由来の情報は Note 差分として event entry で挿入

**改訂理由**:

3 人会話で「対 A」「対 B」両方の Track に同じメッセージを書き込む必要があるという問題から、v0.2 で確定した「track_id = thread_id」を撤回。Note 概念導入で多対多を解決。

### v0.2 (2026-04-25) — 既存資産の責務再配置

**確定事項**:

- AutonomyManager は「責務再配置と拡張」
- メタレイヤーから ExecutionRequest 投下で線切り替えが成立
- 既存 thread metadata と range_before/range_after は参照可能な仕組みとして残る

---

## Phase 番号の変遷

旧ドキュメントには複数系統の Phase 番号が混在していた。新ディレクトリでは Phase 1〜6 の線的順序に集約。

### 旧 Intent B 由来: Phase 0 / C-1 / C-2 / C-3

`persona_action_tracks.md` で使われていた Phase 番号。C は Cognitive の C と推測されるが、明示的な定義はなかった。

| 旧称 | 内容 | 新 Phase |
|------|------|---------|
| Phase 0 (P0-1〜P0-7) | handoff 3 経路問題の解消 | Phase 1 |
| Phase C-1 | MetaLayer / Track 基盤 | Phase 2 |
| Phase B-X | social_track_handler 雛形 | Phase 2 |
| Phase C-2a | line / context_profile DEPRECATED 仕様の追加 | Phase 3 |
| Phase C-2b | 既存 Playbook の改修 | Phase 3 残件 |
| Phase C-2c | 旧仕様の削除 | Phase 3 残件 |
| Phase C-3a | Handler v0.10 拡張属性追加 | Phase 4 |
| Phase C-3b | SubLineScheduler 新設 | Phase 4 |
| Phase C-3c | AutonomyManager → MainLineScheduler 再配置 | Phase 4 残件 |
| Phase C-3d | ConversationManager 関係整理 | Phase 4 残件 |

### 旧 unified_memory_architecture.md 由来: Phase 1〜4

`unified_memory_architecture.md` v3 で使われていた Phase 番号。認知モデルとは別系統だが命名衝突していた。

| 旧称 | 内容 | 新 Phase での扱い |
|------|------|----------------|
| Phase 1 (実装済み) | pulse_logs / Important フラグ / 自動タグ付け / サブエージェント隔離 | unified_memory_architecture 側で管理 |
| Phase 2 (次) | 統一記憶探索 + 記憶基盤強化 | unified_memory_architecture 側で管理 |
| Phase 3 (自律稼働バイオリズム) | 1 時間サイクル | unified_memory_architecture 側で管理、認知モデル Phase 4 と連携 |
| Phase 4 (構想) | 恒常入力処理サブモジュール (カメラ、X 等) | 認知モデル Phase 6「モニタリングライン」に吸収 |

### 直近 (2026-04-29 マージ): Phase 1.1〜1.4

`f6d555b` コミットで導入された Phase 番号 (「認知モデル Phase 1.1〜1.4」)。

| 旧称 | 内容 | 新 Phase |
|------|------|---------|
| Phase 1.1 | Pulse-root context 構築機構 + Handler.track_specific_guidance | Phase 2 |
| Phase 1.2 | meta_judgment.json + meta_judgment_dispatch.py、Playbook 経由パス | Phase 3 |
| Phase 1.3 | scope='discardable'/'committed' 機構、scope 昇格 SQL UPDATE | Phase 1 |
| Phase 1.4 | context_profile / model_type DEPRECATED 宣言 | Phase 3 |

---

## 命名衝突の経緯

旧 Phase C-1〜C-3 と Phase 1.1〜1.4 が並存した時期 (2026-04 後半) があり、ドキュメント参照が複雑化した。本再構造化 (2026-04-30) で Phase 1〜6 に統一し、すべての旧称を「旧称マッピング」で参照可能にした。

---

## 関連ドキュメント

- [README.md](README.md) — 全体俯瞰 (旧称マッピング含む)
- (旧) `persona_cognitive_model.md` v0.14 — 整理完了まで残置
- (旧) `persona_action_tracks.md` v0.11 — 整理完了まで残置
- `handoff_track_context_management.md` — Phase 1 (旧 Phase 0) のもとになった観察記録
