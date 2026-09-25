# v0.4 自律運転: v2 実装と v3 設計の対応表 (調査スナップショット)

> **これは何**: develop-v0.4 で封印解除した直後 (HEAD ecd9385c) に、実装済みの v2 形の運転
> (時間割・判断点・watchdog・実行台帳の配送) と、v3 intent が確定させた運転の設計 (ティック等) を
> 突き合わせた調査の記録。**設計の正典は [autonomous_behavior_v3.md](../intent/autonomous_behavior_v3.md)** で、
> 本書は 2026-09-25 時点の現状のスナップショット。v0.4 の実装計画 (intent) を起草するための土台。
> 調査は読み取り専用のサブエージェント (Opus) が実施し、メティスが検収して収録した。
> 【事実】= intent またはコードで確認したこと、【推測】= 解釈。

**先に一番大事な点。** 【事実】止め具の撤去で、このブランチでは v2 の運転がそのまま生きている。起床・就寝の判断点の行 (`judgment_day_open` / `judgment_day_close`) を持つ自律 ON のペルソナがいれば、サーバーを立てるだけで、起床判断による時間割の編成・コマ・作業セッション・セッション終了判断・就寝判断が LLM を呼ぶ。v3 は時間割を「置き換え」と書いていて、共存させる移行期間の定義はない (§2 の C1)。

---

## 1. 対応表

### 1-A. v3 §11 が v0.4 に割り振った構成要素

**① ティックスケジューラ** — 分類: **未実装**
- 【事実】「最後の標準呼び出しから T 分」で Pulse を打つ機構はコードにない。コード内の tick は二つだけ: `AutonomyManager` (`saiverse/autonomy_manager.py`、ペルソナごとに EventScheduler へ次回を予約する周期ループ、既定 50 分、中身は `watchdog_tick` だけ) と、実行台帳の回復 tick (60 秒)。
- 【事実】流用できそうな部品: `AutonomyManager` は `AUTONOMY_ENABLED` と連動して起動・停止する (`saiverse_manager.py:1480-1515`)。`sea/session_lifecycle.py:1539 touch_anchor_after_llm_call` → `schedule_cache_ttl_pulse` は「LLM 呼び出しが成功したら、その時刻から予約し直す (key 上書き)」形で、「最後の標準呼び出しから T 分」の形そのもの。
- 【推測】ティックはこの「呼び出しのたびに予約し直す」形で作るのが最短。
- 【事実】既知の傷: `AutonomyManager` は実時刻 (`datetime.now`) で予約する ([issue](../issues/autonomy_manager_tick_uses_wall_clock.md))。`schedule_cache_ttl_pulse` も `datetime.now()` (`session_lifecycle.py:1758`)。

**② ティックの単発制御 (1 Beat で閉じる)** — 分類: **未実装** (前身の部品だけある)
- 【事実】`_run_spell_loop` (`sea/runtime_llm.py:2596`) はスペル実行後に LLM を呼び直すラウンドを回す (上限 `SAIVERSE_SPELL_MAX_ROUNDS=3`)。`/quick_spell` (`docs/intent/quick_spell.md`) は本人が「この発話で完了」と宣言すると呼び直しを省く機構で、失敗すると通常のラウンドへ戻る。
- 【推測】v3 が要るのは機構側が強制する単発。quick_spell はそのままでは代わりにならない。

**③ T 設定** — 分類: **未実装**
- 【事実】今あるのは予算の仕組み (day_open 行の `daily_budget_pulses` / `daily_budget_rounds` / `life_mode_override`、`lives[].budget_pulses`) で、v3 §8 では退役対象。
- 【事実】流用できるもの: `LIFE_EVEN_MAX_GAP_MINUTES=50` / 均等・自由モードの自動判定 (`day_plan.derive_default_life_mode`、`day_plan.py:1651`) / `META_JUDGMENT_CONFIG.periodic_interval_minutes` (AutonomyManager の間隔を DB に保存する口)。

**④ アラーム・ルーチンの実行** — 分類: **アラームは実装あり・改修が要る / ルーチンは未実装**
- 【事実】アラームは `PersonaSchedule` → `ScheduleManager` → 実行台帳の `schedule.dispatch` → `dispatch_schedule_fire` の流れで鳴る。Playbook の既定は `track_user_conversation` (CONVERSATION アスペクト = メインライン・committed・標準モデル)。
- 【事実】[issue slot_light_pulse ①](../issues/slot_light_pulse_runs_on_conversation_vehicle.md) のとおり、この会話用の器で走ると出力が建物への発話になる。
- 【事実】ルーチン (「ライフ始め」「ライフ終わり」という時間属性) は無い。`SCHEDULE_TYPE` は periodic / oneshot / interval の三つだけ。起床・就寝の行・アラーム・ルーチンは同じ `persona_schedule` テーブルに同居する前提 (v3 §4.1「器一枚」)。

**⑤ Playbook 投げっぱなし** — 分類: **未実装**
- 【事実】`builtin_data/tools/run_playbook.py` は同期。WORKER アスペクトのサブライン (軽量モデル) を走らせ、終わったら `report_to_parent` を返す。v3 §5 が手本に挙げる「身体移動スペルと同型の非同期」の実物は見つけられなかった (`move_persona` も同期) — 未確認。

**⑥ ライフビュー・できごと UI** — 分類: **v0.3 で撤去済みのため、実質は新しく作る**
- 【事実】コミット f2a36010 で削除済み: フロント `LifeView` / `EventsTimeline` / `LifeSettingsModal` / `TimetableTemplateModal` / `TasksModal` / `PersonaProfileModal`、API `activity.py` / `life_settings.py` / `timetable_template.py` / `autonomy.py` / `episodes.py` (api/routes/ 配下)。
- 【事実】残っているのは手帳・タスク帳の読み取り専用 API (`api/routes/people/pocketbook.py`、メモリタブ) と `episodes` テーブルの読み口だけ。

**⑦ 自律行動管理 UI** — 分類: **未実装**
- 【事実】ライフ設定の API と画面、自律 ON/OFF の API は撤去済み。`ScheduleModal` は `meta_playbook` を送らない (サーバー側で既定値が入る) ので、画面から起床・就寝の行は作れない。REST API (`POST /{persona_id}/schedules`) は `meta_playbook` を受け付ける。
- 【推測】既存の行を持つペルソナ以外は、API を直接叩かない限り運転を始められない。新しい環境での実機検証の前提として重要。

**⑧ Pulse 並列の「防ぐ / 耐える」検査** — 分類: **未実施** (排他の部品はある)
- 【事実】今ある排他: `sea/beat_gate.py` (ペルソナ単位の Beat ロック、RLock 再入可、記憶の関所) / MetaLayer のペルソナ単位判断ロック (順序は MetaLayer → Beat の一方向) / `user_conversation.conversation_lock` / EventScheduler は**単一の dispatch スレッドで callback をその場で実行** (`event_scheduler.py:367`)。スケジュールの発火は同期 (`dispatch_schedule_fire` は submit 後に `runtime_outcome` を読む)。
- 【事実】「別の活動中か」の判定 `_get_open_non_conversation_episode` (`user_conversation.py:321`) は常に None を返す仮置き。docstring に「v0.4 のティック設計が作り直す口」とある。

### 1-B. §5・§13 が前提にしている機構

| 機構 | 分類 | 根拠 |
|---|---|---|
| ライフ確定と境界処理 (`confirm_life_for_today` / `apply_life_boundary` / 均等モードの TTL 1h / keep-alive のライフ連動) | 実装済み・改修が要る | 【事実】`fire_judgment_point` の day_open 分岐の中にある (`autonomy_wiring.py:542`)。手前に `playbook_available` の検査があり、判断点 Playbook 未取り込みだとライフも確定しない。v3 §6「起床は機械の処理だけ」に合わせ、判断経路から切り出す必要 |
| ライフの保存先 | 実装済み・保存先の見直しが要る | 【事実】`persona_day_plan.meta_json` の `lives` (時間割の行と同じ行)。`resolve_business_day`・`get_life_status_now`・`is_keepalive_allowed` が依存 |
| 起床・就寝の LLM 判断 (`judgment_day_open` / `judgment_day_close`) | v3 で退役 (コードは現役) | 【事実】v3 §6・§8。day_open は時間割を編成、day_close はふりかえり・明日へのメモ・報告の種・編纂候補・命名候補の裁定を持つ (`judgment_points.py:479`) |
| 時間割、習慣テンプレート、コマ種別カタログ、コマの発火と繰り下げ、予算ゲート | v3 で退役 | 【事実】v3 §8。コード自身が「v0.4 のティック設計まで休眠」とコメント (`day_plan.py:3767`) |
| 作業セッション、コマの締め (帰属判定と経験値ノート)、セッション終了判断、ダイジェスト配送 | v3 で退役 | 【事実】v3 §8・§7.1。`sea/work_session.py`、`saiverse/slot_close.py`、`judgment_post_session`、`saimemory.append_digest` |
| watchdog | v3 に記載なし (退役か再定義か未決) | 【事実】見張るのは時間割の欠落とコマ予約の途絶だけ (`autonomy_wiring.py:1567`) |
| タスク帳の引き当て API (`list_open_with_due` / `list_open_system_tasks`) | 実装済み・そのまま使える | 【事実】`saiverse/task_book.py:561,586`。運転側からの呼び出し元はまだ無い |
| システムタスク | 器だけある・書き手は未実装 | 【事実】`ORIGIN_SYSTEM` は `task_book.py` 内でしか使われていない |
| 手帳の 2 テーブル、スルースの 7 欄、メモの範囲 ID の機械刻印 | 実装済み (v0.3) | `sea/sluice.py` / `sai_memory/memory/pocketbook.py` |
| スルースの「帳簿の重ね」、「ふと目に入る」差し込み、無条件配達、選び方の規則 | 未実装 | 【事実】grep で該当なし (v3 §13.4 が v0.4 に割り振り) |
| 手帳の訂正の口、本人がアクティビティを閉じる口 | 未実装 | 【事実】API は読み取り専用。`pocketbook_write` は want / did / promise のみ (v3 §13.2.1・§13.6) |
| tell | 実装済み・改修が要る | 【事実】今は二段構え (`builtin_data/tools/tell.py`)。モード別権限ゲート (`sea/mode_spell_permissions.py`) にも未掲載。v3 §9-4 は引数式・標準文脈限定を要求 |
| 事前実行スペル (センサー取得) | 流用候補あり | 【事実】`ExecutionRequest.pre_spells` (`sea/pulse_controller.py:104`、スケジュール経路で使用中) |
| 会話状態と沈黙タイマー (既定 30 分) | 実装済み・そのまま使える | 【事実】`is_in_user_conversation` が「空のティックは会話中は走らない」のゲートにそのまま使える |
| on_event 判断 | 実装済み・改修が要る | 【事実】反応の選択肢 `insert_slot` が時間割に依存 (`judgment_points.py:448`、`judgment_finalize._finalize_on_event`) |
| 実行台帳と Beat の関所 | 実装済み・そのまま使える | ティックを台帳に載せるかは未決 |
| keep-alive | 実装済み・発火タイミングの調整が要る | §2 の C6 |
| 書き込み時の目的参照 (割り付けのあるティック) | 未実装 | 【事実】前駆刻印 (`resolve_predecessor_message_id`) は v0.3 で導入済み。目的参照を刻む処理は無い |
| 目的の木の配線の撤去 (v3 §13.2.1 の「二段目」) | 未実施 | 【事実】`persona_task_manager` の参照は 9 モジュール (judgment_points / day_plan / timetable_template / curation / memory_atlas / recall_walk / day_report / day_scenario / judgment_finalize) |
| episodes の読み口 | 残骸 | 【事実】書き手は全部退役済み。day_close が「今日閉じた出来事」を今も読む (常に空) |
| 夜の日記用の漏れ検知プリセット (v3 §6) | 未実装 | 【事実】builtin の Playbook に無い |

---

## 2. 衝突点 (v3 と今のコードが食い違うところ)

**C1. 時間割のコマ駆動とティック駆動は、置き換えであって共存ではない** — 【事実】v3 §8 が時間割一式と起床・就寝の判断点を退役一覧に入れ、§5 はティックだけで一日を回す。コードは v2 のまま生きている。【推測】ティックを入れる前に時間割の発火を止めないと、二系統が同時に LLM を呼ぶ。

**C2. モデルの格とアスペクト** — 【事実】現状 `Aspect.AUTONOMOUS` = メインライン・committed・**軽量**、WORKER も軽量 (`sea/pulse_context.py`)。v3 §5 はティックを**標準モデル**・メインラインにしている。【推測】ティックに合うアスペクトが今は一つも無い。

**C3. 声の出口** — 【事実】v3 §8・§9-4 ではティックから声を出す唯一の経路が tell。今の標準の器 (会話用 Playbook) は出力を建物への発話として書く (issue slot_light_pulse ①)。アラームもこの器で鳴る。

**C4. tell** — 【事実】今は二段構えでアスペクト制限なし。v3 は引数式・標準文脈限定。【推測】ティックが標準モデルのまま今の tell を使うと呼び出しが二重になる。

**C5. 予算と T** — 【事実】`budget_pulses`・`used_pulses`・ラウンド台帳・κ が現役 (ライフ台帳と予算ゲートが使用)。v3 §5・§8 はこれを T (間隔) に置き換える。

**C6. keep-alive の発火タイミングが T より前に来る** — 【事実】keep-alive は「最後に触ってから TTL × (1 − 0.3)」後に予約 (`session_lifecycle.py:1757`)。均等モードは TTL 1h 上書き → 約 42 分後。【推測】T を 43〜50 分にすると毎回ティックより先に keep-alive が鳴り、「keep-alive は最後の保険」(v3 §1) が崩れて課金も余分にかかる。

**C7. 就寝判断が記憶整理の唯一の窓口** — 【事実】編纂候補と命名候補の裁定を適用する口は day_close の schema と finalize だけ。v3 §6 は「記憶整理を就寝判断から出す」、§9-5 はシステムタスク第二号を候補に挙げる。【推測】行き先を先に作らずに day_close の LLM 部分を退役させると記憶整理の裁定が止まる。

**C8. 判断点の数** — 【事実】v3 は判断点をイベント到着の一つだけにする (§6・§13.3)。コードには 4 種あり、on_event 自体も時間割に依存 (`insert_slot`)。

**C9. ルーチン (アラーム) を変えられるのはユーザーだけのはず** — 【事実】v3 §4.1「LLM の出力で直接変わらない」。ところがペルソナの `schedule_add` / `schedule_delete` スペルでアラームを追加・削除でき、`schedule_add` は判断点の Playbook 指定も拒否しない。【推測】ペルソナが自分で day_open の行を作ってライフの窓を動かせる穴 (`_find_day_schedules` は最も早い起床時刻を採る)。

**C10. ユーザー発話の仲裁** — 【事実】v3 §4 はユーザー発話を最強・常に最優先とする。`handle_user_utterance_conflict` は別の活動中なら on_event 判断に回す設計 (今は判定が常に None で常に直接応答)。【推測】ティック中・投げっぱなし中を「別の活動中」と定義し直すと v3 の要件と正面衝突する。

**C11. 並列性とスレッドの形** — 【事実】EventScheduler の callback は単一スレッドで同期実行、スケジュール発火も同期。【推測】ティックを同じ形で載せると全ペルソナのティック・アラーム・回復 tick・keep-alive が一本の列に並び、複数ペルソナの世界でティックが遅れる。投げっぱなしを別スレッドで走らせると Beat ロック (RLock) の再入の外側で動く別 Beat になり、ロック順序の規則にも関わる。

**C12. 目的の木と episodes の残骸** — 【事実】v3 は両方退役。時間割のコマの `ref` (`task:N`)・帰属判定・day_close の棚入れが今も読む。【推測】時間割の撤去と一緒に消える部分が大半。

---

## 3. 依存関係 (「A は B より先」)

1. **ティックの器 (アスペクト・Playbook・声を出さない記録先・単発制御) が、ティックスケジューラより先** — 単発制御が無いとティックが会話並みに連鎖し、1日15〜20回のコスト設計が崩れる (v3 §5)。
2. **tell の引数式化と標準文脈ゲートが、ティックの本稼働より先 (遅くとも同じ便)** — v3 §8・§9-4。ペルソナに見える語彙の改名は一度に束ねる (§9-7)。
3. **Pulse 並列の洗い出しが、スケジューラ設計より先** — v3 §9-3 明記。「別の活動中か」の作り直し (C10) と EventScheduler のスレッドの形 (C11) もここで決める。
4. **T 設定とモードが、スケジューラより先** — 予算の退役は T が入った後 (C5)。
5. **ライフ確定を判断経路から切り出すことが、day_open の LLM 退役より先**。
6. **ライフの保存先を決めることが、時間割の撤去より先** — `lives` は `persona_day_plan` の行にあり、表示・keep-alive・営業日判定が依存。
7. **記憶整理 (編纂・命名の裁定) の新しい受け皿が、day_close の LLM 退役より先** (C7) — 候補はシステムタスク (書き手の実装も要る)。
8. **on_event の選択肢の再設計 (`insert_slot` の代わり) が、時間割の撤去より先**。
9. **時間割の発火を止めることが、ティックの実機稼働より先** (C1、二系統の同時課金の防止)。
10. **自律行動管理 UI (少なくとも起床・就寝と T の設定の口) が、新しい環境での実機検証より先** — ライフ設定の UI と API は撤去済み (1-A ⑦)。
11. **目的参照の機械刻印が、スルースの「帳簿の重ね」・できごと UI の自由時間カード・手帳メモの範囲参照の精密化より先**。
12. **ティック本体が、「ふと目に入る」差し込み・無条件配達・締め切り通知より先** — いずれもティックのプロンプトに相乗り (v3 §5・§13.2)。
13. **投げっぱなしの報告を届ける経路が、投げっぱなしそのものより先** — 候補は `perception.push` の送信トレイ (実装済み)。「報告を受けて何をするか」の保存先も先に要る。
14. **keep-alive の発火タイミングの見直しが、T を 42 分超で運用することより先** (C6)。
15. 【推測】**AutonomyManager を土台にするなら実時刻の issue を直すのが先** — 一日シミュレータでティックを検証できなくなる。シミュレータ (`day_simulator.py` / `day_scenario.py`) は時間割前提なので作り直しも要る。

---

## 4. v3 に書かれていない未決事項 (実装すると決めが要る点)

1. **ティックのアスペクトと器**: 新アスペクトか AUTONOMOUS を標準へ変えるか / ペルソナに見せるモード名 / 出力の記録先 (SAIMemory メインラインだけか、建物ログ・チャット UI にも出すか — v3 §9-9 は「ティックの本人の発話へ降りる」とだけ)。
2. **単発制御の中身**: スペル失敗時の扱い / 結果が次のティックの頭にどう届くか / 事前実行スペルの対象の指定方法。
3. **「最後の標準呼び出し」の定義**: T を数え直すのはどれか (会話・アラーム・on_event・スルース・keep-alive・投げっぱなしの報告) / ペルソナ単位か (ペルソナ, モデル) の Session 単位か。
4. **T の設定**: 保存先・既定値・均等モードの上限 (50) の強制場所 / モード上書き / `AUTONOMY_ENABLED` との関係 / 自由モードで T を小さくしたときのコストの歯止め (予算退役後は何も無い)。
5. **ライフの保存先とライフ境界の台帳**: 時間割撤去後の置き場 / 機械だけの起床処理を実行台帳のどの kind で包むか (今は `judgment.day_open`)。
6. **ルーチンの形**: `PersonaSchedule` の拡張方法 / 中身 (指示書か Playbook か) の保存欄 / ライフ始めのティックを置き換えるか足すか / 複数ルーチンの順序 / アラームの器 (発話か独白か) / アラームが T を数え直すか。
7. **起床・就寝の行とルーチンの器の同居**: 自律 OFF だがアラームは欲しい場合 (v3 §11 自身が未決)。
8. **ペルソナのアラーム用スペル (`schedule_add` / `schedule_delete`) の扱い**: 残す / 提案だけ / 撤去 (C9)。判断点 Playbook を指定できる穴の閉じ方。
9. **on_event の選択肢**: `insert_slot` の代わり (タスク帳へ積む?) / `note_only` のメモの行き先 (今はライフ台帳の meta)。
10. **「別の活動中か」の定義と、ユーザー発話の仲裁を残すかどうか** (C10)。
11. **割り付けの具体**: 空きティックのプロンプトに手帳とレパートリーをどれだけ載せるか / 期限が「近い」の閾値 / システムタスク第一号を差し込む時機 / 解消の導出規則の種類ごとの定義。
12. **投げっぱなし**: 報告の形と届く場所 / 「報告を受けて何をするか」の保存先 / サブ Playbook のモデルとアスペクト / ライフ終了・自律 OFF 時の扱い / 失敗の報告 / v3 §5 の手本「身体移動スペル」がどれを指すか。
13. **ライフの端での打ち方**: 起床直後にすぐ打つか起床 + T 後か / 就寝直前のティック / ライフの外 (谷) で会話した後は打たない、でよいか。
14. **再起動からの回復**: T の予約はメモリ上だけ — 消えたとき誰が張り直すか / watchdog は退役かティック用に作り直すか / ティックの冪等キー。
15. **経験値ノートの席**: コマの締めが退役すると書く場所が無くなる (in_flight 台帳にも「宙に浮いている」)。
16. **「ふと目に入る」の頻度と量**: 数値・選び方の規則・無条件配達の量の上限 (§13.5-3)。
17. **スルースの「帳簿の重ね」の形式と、目的参照のメッセージ上の欄の形**。
18. **本人がアクティビティを閉じる口、手帳の訂正 API とタスク帳の楽観ロックの整合** (§13.2.1)。
19. **移行**: 既存の習慣テンプレートをルーチンへ写すか (§9-8 の移行一覧に無い) / 起床・就寝の行の `daily_budget_*` の扱い / `persona_day_plan` / `persona_timetable_template` / `persona_task*` を落とす時期 / 退役 kind の台帳の行 (`slot.fire` / `judgment.post_session` / `saimemory.append_digest`) の扱い。
20. **開発者モードを OFF にすると全ペルソナの自律が OFF になる件の裁定** ([issue](../issues/developer_mode_off_mass_disables_autonomy.md)、未決のまま)。
21. **夜の日記用の漏れ検知プリセットの中身** (v3 §6 は「用意する」とだけ)。

---

## 5. v0.4 の作業範囲に絡む既存 issue

`docs/issues/` の未解決分から: `autonomy_manager_tick_uses_wall_clock` / `conversation_timeout_eats_short_life_window` / `developer_mode_off_mass_disables_autonomy` / `on_event_judgment_has_no_idempotency_key` / `judgment_seat_contention_and_event_loss` / `work_session_gate_closed_consumes_slot` (作業セッションごと退役なら対象消滅) / `life_settings_template_ui_boundary_illegible` (管理 UI の設計で答えが出る) / `slot_light_pulse_runs_on_conversation_vehicle` (器の論点、C3 と同じ根)。

---

**調査で読んだ主なファイル** (すべて develop-v0.4): intent = autonomous_behavior_v3 (全文) / v2 / timetable_redesign / life / execution_ledger / issues/autonomous_v2_post_live_gaps。運転の本体 = autonomy_wiring / day_plan / judgment_points / slot_close / execution_ledger_wiring / schedule_manager / autonomy_manager / task_book / user_conversation / event_scheduler。SEA 側 = work_session / runtime_llm / pulse_context / session_lifecycle / beat_gate / sluice。ツール = tell / run_playbook / schedule_add。
