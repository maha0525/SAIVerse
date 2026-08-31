# 自律行動・判断点・schedule 一次監査

**開始日**: 2026-07-14

**状態**: 指摘あり・一次監査完了
**監査基準**: 開始 `2dda6ab`（2026-07-14 02:39 JST）、直近再確認 `113567e`
**監査軸**: 発火重複 / 日付・ライフ境界 / 予算 / 判断適用の原子性 / 失敗時再試行 / 本人裁定との一致

## 現行実装の再確認

監査開始後に life v0.5 と完全手動モードを含む改修が入ったため、旧監査メモを前提にせず、
`2dda6ab` の clean working tree から現行 Intent・実装・テストを読み直し、監査中の
改修後は各項目をその時点のHEADで再確認している。

確認済み:

- `docs/intent/autonomous_behavior_v2.md`
- `docs/intent/life.md` v0.5
- `docs/intent/persona_cognition/judgment_points.md`
- `saiverse/autonomy_wiring.py` の判断点直列化・watchdog
- `saiverse/autonomy_manager.py` の EventScheduler 駆動 watchdog
- `saiverse/day_plan.py` の保存・全置換・予約・発火・予算ゲート
- `builtin_data/tools/judgment_finalize.py` の5種判断適用（day_open / post_conversation / post_session / on_event / day_close）
- 直近改修 `96062ce`（時間割保存の丸め・部分救済、空plan watchdog回復）

## Coverage

一次監査で確認した実行境界:

- 起動入口: 定刻PersonaSchedule、day_open watchdog、会話終了/wait-response、作業session終了、外部event、debug manual発火。
- 直列化・重複: persona lock、watchdogと定刻の競合、重複schedule行、day_open/day_close境界副作用。
- 時間境界: City timezone、host clock復帰、通常日/深夜跨ぎの営業日、lifeの半開区間、overnight時間割順序。
- 費用と実行状態: life予算ゲート、旧日次台帳との二重書込み、slot pending/deferred/fired/done、EventScheduler予約、WorkSession結果、Episode open/closeとorigin継承。
- 判断適用: 5種finalize、時間割全置換、task完了＋artifact、desire/Track spell、event memo、SAIMemory判断行、`judgment_applied` callback。
- 制御・運用: Active gate、完全手動mode、life設定と一般schedule CRUD、register/unregister、oneshot/interval/periodicの発火後更新。
- 障害注入: DB保存・budget記帳・slot terminal化・artifact追記・SAIMemory保存・spell・runtime・scheduler同期・Pulse dispatchの各失敗、および同一入力の再試行。

この監査で直接の主対象にしなかったもの:

- SEA runtime内部のline/model/thread/cache session隔離とhead-tail組立は、次行「SEA runtime / Session / head-tail」で監査する。
- LLM provider固有のretry・外部API受付判定は「外部連携」で監査する。ここではPulseController/ScheduleManagerが受け取る例外・outcome境界までを対象にした。
- curation/namingのアルゴリズム本体とmemory.db内部整合は既存の記憶・Memory Atlas監査を正とし、本監査ではday_closeからの起動・部分適用境界だけを確認した。

## Findings

### [P1] 起床判断の時間割保存拒否が、保持された旧planの予約だけを先に消す

- 場所: `builtin_data/tools/judgment_finalize.py:230-240`, `saiverse/day_plan.py:393-425,1922-1950,1970-2001`, `saiverse/autonomy_wiring.py:815-877`
- 事実:
  - `day_open` の finalize は、sanitize 後にコマが残ると `cancel_scheduled_slots()` を呼び、その後で `save_day_plan()` を実行する。
  - life v0.5 の `save_day_plan()` は、全コマが「今〜就寝」の範囲外になると `ValueError` を投げる。この時DBの既存planは変更しない。
  - したがって保存拒否時は、DB上の旧planは残る一方、その pending/deferred コマの EventScheduler 予約だけが消える。
  - 同じ全置換を行う `replace_remaining_slots()` は、検証・life範囲正規化を済ませてから旧予約をcancelしており、失敗時に「planも予約も一切変更しない」と明記されている。起床判断だけがこの原子性を外している。
- 最小再現:
  1. 08:00〜12:00 のlife、09:00の既存コマと予約1件を作る。
  2. `day_open` 出力として13:00のコマだけを渡す。
  3. `save_day_plan()` は全除外で拒否する。
  4. 旧09:00コマはDBに残ったが、EventSchedulerの予約は `1 → 0` になった。
- 実行確認:
  - 監査用の一時回帰を `tests/test_judgment_points.py` に追加して実行し、上記状態を確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-repro tests/test_judgment_points.py::test_audit_repro_day_open_save_rejection_orphans_existing_reservations`
  - 結果: `1 passed`。
- 影響:
  - UI/DBには時間割が見えるのに、自動発火しない不整合を作る。
  - Activeならwatchdogがlost reservationを検出して再予約するが、既定では最大約50分遅れる。その間に開始時刻を過ぎたコマは本来の時刻に発火しない。
  - watchdog自体が停止中、または次tick前にプロセスが止まった場合は自動回復しない。
  - finalizeの適用文も「今日の時間割は編成されていません」と返すが、DBには旧時間割が残るため、報告と実状態も一致しない。
- 発生条件:
  - 既存の当日planと予約がある状態で `day_open` が再発火し、sanitizeは通るが、life範囲正規化後のコマが0件になる場合。
  - 直近改修で過去時刻は丸め・一部異常は部分救済されるため頻度は下がったが、就寝後だけの出力や谷での再発火では到達可能。
- 修正方針:
  - 起床判断の全置換を `day_plan` 内の単一APIへまとめ、検証・life範囲正規化成功後にだけ旧予約をcancelし、plan保存と再予約を行う。
  - 最低限でも `save_day_plan()` 成功後へcancelを移す。ただし保存成功後・cancel後・再予約前の例外も部分状態を作るため、理想は旧plan/旧予約の復元を含む置換APIである。
  - 保存拒否時の適用文は「既存時間割を維持した」と実状態に合わせる。
- 必要な回帰:
  - 既存plan＋予約ありで、新planが書式検証失敗、life範囲で全除外、DB保存失敗となる各ケースについて、旧planと旧予約がともに不変であること。
  - 保存成功時は旧index予約が残らず、新planのpendingコマだけが予約されること。
  - 再予約途中失敗時に旧plan/旧予約へ戻るか、明示的な回復状態へ収束すること。

### [P1] 判断点lockが実行を直列化するだけで、同一日のday_open/day_closeを重複抑止しない

- 場所: `saiverse/autonomy_wiring.py:185-298,381-439,837-864`, `saiverse/schedule_manager.py:139-176,423-475`, `api/routes/people/life_settings.py:58-68,142-158`, `database/models.py:400-425`, `api/routes/people/schedule.py:95-132`, `builtin_data/tools/schedule_add.py:90-145`, `saiverse/day_plan.py:1700-1732`
- 事実:
  - `fire_judgment_point()` は MetaLayer のpersona別lockを使うため同時実行は直列化されるが、`(persona, kind, plan_date)` の実行済み判定は持たない。lock待ちの後でも、各callerが持つprecondition以外は同じ判断を再度submitできる。
  - watchdogのday_openだけは「planがまだ無い」をlock取得後に再判定する。一方、定刻の `handle_scheduled_judgment()` はprecondition無しで `fire_judgment_point()` を呼ぶ。このため定刻day_openが先なら後続watchdogは止まるが、watchdogが先にplanを作った順序では後続の定刻day_openがもう一度走る。
  - `PersonaSchedule` はschedule IDだけが主キーで、persona＋判断点種別の一意制約がない。life設定APIも旧ScheduleModal等から複数行が存在し得ることを明記し、最小IDの1行だけを更新して他行は統合しない。
  - 汎用schedule APIと `schedule_add` toolは `judgment_day_open` / `judgment_day_close` を通常の新規行として作成できる。ScheduleManagerの予約keyはschedule ID単位なので、重複行は互いを上書きせず両方発火する。
  - day_openのライフ開始通知だけは「初回確定時のみ」で二重化を防ぐが、起床判断本体と時間割全置換は再実行される。day_closeは `_handle_life_end()` 自体にも終了済み判定がなく、境界処理・tail通知・判断本体が毎回走る。
- 最小再現A（watchdog先行）:
  1. Active、起床08:00、就寝22:00、当日plan無しで08:00のwatchdogを発火する。
  2. watchdogのday_openが09:00のplanを保存して完了する。
  3. 直後に同日の定刻 `judgment_day_open` を発火する。
  4. planが既に存在してもday_openが再度submitされ、呼出列は `day_open, day_open` になった。
- 最小再現B（重複schedule相当）:
  1. 当日のlifeを保存し、定刻 `judgment_day_close` を2回発火する。
  2. `judgment_day_close` は2回submitされ、`_handle_life_end()` も2回呼ばれた。
- 実行確認:
  - 上記2ケースを監査用の一時回帰として `tests/test_autonomy_wiring.py` に追加して実行し、確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-duplicates tests/test_autonomy_wiring.py::test_audit_repro_watchdog_then_scheduled_day_open_submits_twice tests/test_autonomy_wiring.py::test_audit_repro_duplicate_day_close_repeats_boundary_and_judgment`
  - 結果: `2 passed`。
- 影響:
  - day_openは本人が一度編成した当日時間割を、同じ起床イベント由来の二度目の判断が再生成・全置換する。二重のLLMコストだけでなく、最初の本人裁定を直後に別裁定で上書きする。
  - day_closeは活動終了tail通知、keep-alive停止、TTL解除予約、ふりかえり判断、判断点回数記帳が重複する。TTL予約keyの上書き等で最終状態が収束する副作用もあるが、本人の経験・通知・判断履歴は重複する。
  - 重複行はlife設定画面では最小IDの1件しか見えず、もう一方の発火源が設定変更後も残る。
- 修正方針:
  - 判断点入口へ `(persona_id, kind, effective_plan_date)` の実行台帳または同等の冪等性keyを持たせ、lock取得後に同一境界イベントの完了状態を再確認する。開始・成功・失敗を区別し、失敗再試行を永久に塞がない設計にする。
  - watchdogと定刻scheduleのどちらが先でも同じpreconditionへ収束させる。少なくともday_open定刻側にもplan有無の再判定が必要だが、「ユーザーが明示した再編成」との区別が要るため、単純なplan存在判定だけを全入口へ広げない。
  - DBへperiodicな `(persona_id, judgment_day_open)` / `(persona_id, judgment_day_close)` の一意性を導入し、migrationで既存重複を検出・統合する。汎用schedule API/toolから判断点Playbookを新規作成する口はlife設定APIへ誘導する。
  - day_close境界処理にも終了済みmarkerを持たせ、判断本体の再試行方針とは独立にtail通知とTTL終端副作用を一度だけにする。
- 必要な回帰:
  - watchdog→定刻、定刻→watchdogの両順序でday_openのsubmit・時間割全置換・判断点記帳が1回だけであること。
  - 同じpersona/kind/dateのschedule callbackを並行・直列に複数回呼んでも、成功済み判断と境界通知が1回だけであること。
  - 1回目がsubmit前、submit中、finalize中に失敗した各場合について、安全に再試行でき、成功済み効果は重複しないこと。
  - migrationが重複schedule行を可視化して統合し、life設定API・汎用schedule API・toolの全入口が一意制約を守ること。

### [P1] Cityタイムゾーンで発火した後にhost時計へ戻り、life・営業日・時間割予約が別の時刻になる

- 場所: `saiverse/schedule_manager.py:191-318,366-416`, `saiverse/clock.py:35-39`, `saiverse/autonomy_wiring.py:306-373,783-817`, `saiverse/judgment_points.py:994-1035,1511-1531,1619-1640`, `saiverse/day_plan.py:880-930,1243-1260,1304-1335,1758-1815`, `sea/runtime.py:1935-1946`, `api/routes/episodes.py:19-20,96-121`
- 事実:
  - ScheduleManagerは `City.TIMEZONE` を読み、PersonaScheduleの時刻をpersona-localで計算した後、UTC経由でhost-localのnaive datetimeへ変換してEventSchedulerへ積む。定刻の発火時刻自体はCity設定に従う。
  - しかしcallback後の自律行動系はpersona-localへ戻さず、`clock.now()`（実モードではhostの `datetime.now()`）をそのまま使う。
  - day_openのplan date・状況文の「現在時刻」、life確定日、day_closeのeffective plan date、watchdogの起床窓判定、life/budgetの現在営業日、時間割の範囲正規化がhost日時基準になる。
  - slotの `start` はpersona-localな時間割値だが、`_slot_fire_at()` はplan date＋HH:MMをhost-local naive datetimeとして組み、Cityタイムゾーン変換を行わない。
  - 同じproject内でもSEAのリアルタイムheadは実モードで `datetime.now(persona.timezone)`、episodes APIの日付境界は `City.TIMEZONE` を使っており、ペルソナに見える時刻・「今日」の既存規則とも一致しない。
- 最小再現（host=Asia/Tokyo、City=Pacific/Honolulu、life=08:00〜22:00）:
  1. Honolulu 2026-07-04 08:00のday_openは、ScheduleManagerの変換後はhost 2026-07-05 03:00に発火する。
  2. 発火後のlifeはpersona-localの `2026-07-04` ではなくhost暦日の `2026-07-05` に保存された。判断contextも `plan_date=2026-07-05`、状況文も「現在03:00」になった。
  3. 同時刻のwatchdogはhostの03:00をwake=08:00より前と比較し、現地では起床済みなのに `before wake` で停止した。
  4. personaが編成する09:00コマの現地時刻はhost 04:00だが、`_slot_fire_at()` はhost 09:00を返した。実発火はpersona-local 14:00となり、5時間遅れる。
  5. Honolulu 2026-07-04 22:00のday_closeはhost 2026-07-05 17:00に発火し、判断対象営業日は誤って `2026-07-05` になった。
- 実行確認:
  - 上記を監査用の一時回帰として `tests/test_autonomy_wiring.py` に追加し、life保存日・判断context・状況文・watchdog判定・slot予約時刻・day_close営業日を確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-timezone tests/test_autonomy_wiring.py::test_audit_repro_city_timezone_is_lost_after_schedule_dispatch`
  - 最終結果: `1 passed`。
- 影響:
  - hostとCityの時差によってはday_open直後に「まだ起床前」「すでに就寝後」と誤認し、watchdog回復が働かない。本人へも誤った現在時刻と日付を確定情報として渡す。
  - life、plan、budget、judgment count、day digestがpersona-localの一日ではなくhost暦日へ保存される。Cityの「今日のできごと」はCity日付境界なので、新聞・life・判断履歴で対象日が分裂する。
  - 時間割コマは時差ぶんずれて発火し、日付変更線を跨ぐ組合せでは前日／翌日へ移動する。深夜跨ぎの `plan_date+1日` 補正もhost日付に対して適用されるため、City営業日を回復できない。
  - hostとCityが同じタイムゾーンの通常構成では表面化しないため、既存のnaive仮想時刻テストは全て通る。
- 修正方針:
  - persona/City時刻を返す共通clock helperを設け、実モードでは `clock.now()` のepochを `persona.timezone` / `City.TIMEZONE` へ変換し、仮想モードではシナリオ時刻の意味を明示して一貫させる。
  - plan date・HH:MM・wake/closeの比較はpersona-localで行う。EventSchedulerへ渡す直前にだけpersona-local aware datetimeをhost-local/epochへ変換する。
  - `effective_plan_date` にnaive host datetimeを直接渡す口をなくし、timezone-awareなpersona nowまたは明示 `local_date/local_hhmm` を要求する。
  - 既存の誤日付plan/lifeをどう扱うかはmigrationで自動推測せず、City timezone・作成時刻・slot実績を用いた検出レポートを先に出す。
- 必要な回帰:
  - hostとCityが同一、UTC、正負の時差、日付変更線跨ぎ、DST開始/終了の各ケースで、day_open/day_close営業日・watchdog窓・状況文・life/budget記帳・slot発火epochがpersona-local設定と一致すること。
  - 通常日と深夜跨ぎlifeの双方で、slotのpersona-local HH:MMを期待するepochへ一度だけ変換すること。
  - SEA headの現在時刻、判断状況文、episodesの「今日」、life viewが同じCity-local日付と時刻を示すこと。

### [P1・回帰固定済み] 深夜跨ぎ時間割をHH:MMでsortした後、昼夜のコマを翌日深夜へ連続クランプする

- 場所: `saiverse/judgment_points.py:423-433,1836-1966`, `saiverse/day_plan.py:270-315,880-969,1760-1784`, `builtin_data/tools/judgment_finalize.py:189-257`, `docs/intent/autonomous_behavior_v2.md:313-322`
- 事実:
  - 起床判断のschemaとPlaybookは、コマを `start` の厳密昇順で返すよう要求する。`sanitize_timetable()`もLLM出力をHH:MM文字列で強制sortする。
  - 深夜跨ぎlife（例: 07:00〜01:00）の正しい時系列は `09:00 → 23:00 → 翌00:30` だが、日付を持たないHH:MM sortでは `00:30 → 09:00 → 23:00` になる。
  - life範囲正規化は07:00を起点とした拡張分で00:30を1050分後と正しく解釈する一方、配列順はsort後のまま処理する。最初の00:30を下限にした後、09:00と23:00を「直前以下の衝突」と判定し、1分ずつ後ろへクランプする。
  - Intentは当面「深夜帯コマは置けず、出力されても保存検証で弾かれる」と明記する。しかしsanitizeのsortとv0.5の丸めが組み合わさる現行実装では弾かれず、別時刻へ正常保存・予約される。
  - `_slot_fire_at()`は `start < wake` を営業日翌日のコマとして予約するため、書き換え後の全コマは翌日00:30台に実発火する。
- 最小再現:
  1. 同一タイムゾーン、起床07:00・就寝01:00のscheduleとlifeを設定する。
  2. 起床07:00に、本人の自然な時系列どおり `09:00, 23:00, 00:30` の休むコマをday_open出力としてfinalizeする。
  3. sanitize後は `00:30, 09:00, 23:00`、保存後は `00:30, 00:31, 00:32` になった。
  4. EventSchedulerの最初の予約は営業日翌日の00:30となり、本来当日09:00・23:00に行う二つの予定も翌日深夜へ移った。
- 実行確認:
  - 起床・就寝PersonaScheduleを含む本番相当条件の一時回帰を `tests/test_judgment_points.py` に追加して実行し、保存値と予約時刻を確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-overnight tests/test_judgment_points.py::test_audit_repro_overnight_timetable_collapses_day_slots_into_midnight`
  - 最終結果: `1 passed`。
- 影響:
  - 本人の時間割が検証エラーにならず、異なる時刻の予定として保存されるため、本人裁定の無断変形になる。
  - 日中・夜間の作業コマが十数時間遅れて就寝直前に連続発火する。セッション系なら予算ゲート、移動、エピソード、作業実行も誤時刻に集中する。
  - 適用エコーには調整メモが出るものの、「昇順維持の丸め」として表示され、営業日順序を壊したsystem側sortが原因とは伝わらない。
  - 2026-07-12実機で本人が「24:30」を出した需要が既に記録されており、深夜コマ出力は仮説ではない。
- 修正方針:
  - 時間割の時刻を営業日内の拡張分（wakeからの経過分）で比較・sort・重複判定する。`start < wake` は翌日として `+1440` し、`09:00 → 23:00 → 00:30` を正しい昇順として扱う。
  - schema/Playbookの「厳密昇順」もHH:MM順ではなく営業日順と明記する。可能なら内部正規形に `day_offset` または実datetimeを持たせ、表示時だけHH:MMへ落とす。
  - 深夜帯コマをまだ非対応として維持するなら、sanitize段階で `start < wake` を個別棄却し、丸めで別時刻へ変形しない。Intent記載どおり「弾く」挙動を構造的に保証する。
- 必要な回帰:
  - 07:00〜01:00で `09:00, 23:00, 00:30` が営業日順のまま保存され、それぞれ当日09:00・23:00・翌日00:30に予約されること（対応する場合）。
  - 非対応を維持する場合、00:30だけが明示理由付きで棄却され、09:00・23:00は時刻不変で残ること。
  - day_openとremaining_timetableの双方で、sanitize・保存・再予約・watchdog再接続後の順序とepochが一致すること。

- 修正確認（`e3c78cb`）:
  - `day_order_minutes()` がlife開始を一日の起点として営業日順を定義し、sanitize・保存・remaining置換が同じ順序を使うようになった。
  - `07:30, 09:00, 13:00, 00:30` が時刻不変・営業日順のまま保存される回帰を含む関連3テストを、直近HEAD `e076918` で再実行して `3 passed` を確認した。

### [P2] 定刻day_closeがlife終端の排他的境界から外れ、判断点回数に記帳されない

- 場所: `saiverse/autonomy_wiring.py:270-301,350-373`, `saiverse/day_plan.py:821-832,1280-1300,1343-1386`, `tests/test_life_confirmation.py:444-470`
- 事実:
  - `PersonaSchedule` のday_closeはlifeの終了時刻ちょうど（例: 07:00〜22:00なら22:00）に発火する。
  - `fire_judgment_point()` はday_closeのlife終了処理と判断submitを行った後、共通末尾で `record_judgment_pulse()` を呼ぶ。
  - `record_judgment_pulse()` は現在の営業日を正しく自己解決するが、発火時刻が属するlifeを `get_life_for_time()` の半開区間 `[start, end)` で探す。終端ちょうどはどのlifeにも属さないため `None` となり、day_closeだけが記帳されない。
  - 既存の別枠計数テストもこの挙動を認識しており、本番設定は22:00のまま、テスト内のday_close発火だけを21:59へずらして期待値3を成立させている。
  - life終了通知、keep-alive予約cancel、深夜01:00の前営業日解決は別経路で正常に動く。欠けるのは就寝判断の回数である。
- 最小再現:
  1. 07:00〜22:00のlifeをday_openで確定し、day_open 1回・post_conversation 1回を記帳する。
  2. 本番スケジュールと同じ22:00ちょうどにday_closeを発火する。
  3. 3判断すべてがsubmit済みだが、保存された `judgment_pulses` は期待3に対して2だった。
- 実行確認:
  - 上記を監査用の一時回帰として `tests/test_life_confirmation.py` に追加して実行し、`assert 2 == 3` の失敗を確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest tests/test_life_confirmation.py::test_audit_day_close_at_actual_boundary_is_counted -q`
  - 結果: `1 failed`（期待3、実値2）。
  - 境界副作用の切り分けとして、keep-alive停止と深夜day_closeの前営業日解決の既存2テストは `2 passed` を確認した。
- 影響:
  - 新聞・life view・ログ等の判断点回数が、定刻運用では原則として毎営業日1回ずつ過少になる。判断点LLMコストの観測値として使う台帳なので、実コストとの恒常的な差になる。
  - `_handle_life_end()` はday_close submit前に現在の `judgment_pulses` をログへ出すため、終端ログも就寝判断を含まない値で確定する。
  - 通常日と深夜跨ぎの双方で発生する。深夜跨ぎでは営業日自体は前日に正しく解決されるが、01:00がlife終端なので同様に記帳対象外となる。
- 修正方針:
  - day_open/day_closeの境界判断は時刻包含から対象lifeを推測せず、`effective_plan_date` と境界種別から対象lifeを明示して記帳する。終端を包含扱いに変えると隣接lifeとの境界が曖昧になるため、`get_life_for_time()` の半開区間規則は維持する。
  - day_closeが遅発した場合も、現在時刻ではなく「閉じる営業日・life」へ記帳する。汎用判断点（会話終了等）は従来どおり発火時刻が属するlifeへ記帳する。
  - life終了ログへ就寝判断を含めたい場合は、記帳と終端副作用の順序も明示的に定義する。ただしsubmit失敗・finalize失敗時に何を「1回」と数えるかは、判断点実行台帳の設計と合わせる。
- 必要な回帰:
  - 通常life 07:00〜22:00の22:00、深夜跨ぎlife 07:00〜01:00の翌日01:00で、day_closeが正しい営業日の正しいlifeへ1回記帳されること。
  - 終端が次lifeの開始と一致する場合にも、day_closeが閉じる側へ入り、通常パルスは開始側へ入ること。
  - 遅発day_close、重複callback、submit失敗・成功再試行で、定義した計数単位どおり過少・二重記帳にならないこと。

### [P1] 作業セッション後のlife消費記帳に失敗してもdoneへ進み、後続コマが予算を超過実行する

- 場所: `saiverse/day_plan.py:650-729,1389-1420,2260-2350,2345-2500`, `docs/intent/life.md:98-113,400-403`
- 事実:
  - lifeのある日の予算ゲートは `lives[].used_pulses / used_rounds` から残高を導出し、残高が0以下なら後続の作業コマをskipする。
  - `_fire_slot()` は作業ハンドラ終了後、実測 `used_rounds` を旧日次台帳の `consume_budget()` とlife台帳の `consume_life_rounds()`へ別々に書く。両方の例外をログだけで捕捉し、その後は無条件にslotを `done` へ更新する。
  - lifeのある日は `get_budget_state()` が旧日次台帳を参照せずlife台帳だけを正典にする。このため `consume_budget()` が成功しても、`consume_life_rounds()` だけ失敗すれば実予算残高は減らない。
  - 失敗したコマは `done` なのでwatchdog・再試行対象にもならず、実測ラウンドを後からlifeへ照合する永続情報もslotに残らない。後続コマは未消費の残高を見て通常実行される。
- 最小再現:
  1. 08:00〜12:00、予算1パルス（5ラウンド消費で係数0.2により枯渇）のlifeに、09:00と10:00の5ラウンド作業コマを置く。
  2. 作業セッションは各5ラウンド成功させ、`consume_life_rounds()` だけをDB障害相当の例外にする。
  3. 本来は09:00だけ実行後、10:00を予算枯渇でskipすべきところ、両方の作業セッションが実行された。
  4. 最終状態はhandler呼出し `[5, 5]`、slot status `[done, done]`、lifeの `used_rounds=0` だった。
- 実行確認:
  - 上記を監査用の一時回帰として `tests/test_life_phase2.py` に追加して実行し、壊れた最終状態を確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-budget-failure-state tests/test_life_phase2.py::test_audit_life_bookkeeping_failure_does_not_reopen_budget`
  - 結果: `1 passed`（handler 2回、両slot done、life消費0を確認）。
- 影響:
  - LLM作業の実コストが発生したのに残予算が減らず、同じ障害が続く限り当日予算を超えて後続の全作業コマを実行できる。予算をコストの物理法則にするlife設計の安全境界が開く。
  - UI・新聞ではコマが正常完了に見える一方、life表示は未消費のままになる。ログ以外に未記帳ラウンドを復元する材料がなく、障害復旧後の自動精算もできない。
  - 旧日次metaには `budget_used_rounds` が増え、正典のlifeは0のままという二重台帳の分裂も残る。lifeが存在する間、ゲートはこの旧値を無視する。
- 修正方針:
  - 不可逆なLLM実行前に、slotの `pending/deferred → fired` と予算予約を単一トランザクションで確定する。予約失敗時はhandlerを呼ばない。
  - 実行後は予約を実測ラウンドへ精算し、life消費とslot `done` を同一トランザクションで確定する。精算失敗時も予約額は残し、後続コマが未消費扱いで通らないようにする。
  - `consume_budget()` と `consume_life_rounds()` の二重書込みをやめ、lifeのある日は一つの正典へ集約する。既存の `fired` コマを、実行中・精算待ち・実行結果不明に区別できる永続状態または実行台帳を持たせる。
  - recoveryは実行ID/slot世代を用いて冪等に精算し、LLM作業そのものは自動再実行しない。結果不明時は予算を解放せず、明示的な照合対象にする。
- 必要な回帰:
  - 予算予約のDB失敗ではhandlerが0回、slotと予算が不変であること。
  - handler成功後のlife精算・done更新の各失敗で予約額が残り、後続コマが予算超過実行されないこと。
  - 精算再試行を複数回行ってもusedが一度だけ増え、slotが一度だけdoneになること。
  - プロセス停止を予約前・予約後/handler前・handler後/精算前・精算後の各点で再現し、LLM二重実行と予算消失の双方が起きないこと。

### [P1] handler成功後のdone保存失敗がslot Episodeを永久openにし、後続ログの出自を汚染する

- 場所: `saiverse/day_plan.py:1878-1960,2230-2285,2345-2500`, `saiverse/event_scheduler.py:275-304`, `saiverse/episodes.py:182-221,229-294`, `sea/runtime.py:1446-1468`, `docs/intent/life.md:61-66,164-172`
- 事実:
  - `_fire_slot()` はhandler実行前にslotを `fired` へ保存し、kind=`slot`（暮らし/休むはpresence）のEpisodeをopenする。handler成功後は予算記帳、slotの `done` 保存、Episode closeの順で処理する。
  - `done` の `_update_slot()` が例外を投げると、直後の `_close_slot_episode()`へ到達しない。例外はEventSchedulerが最外周で捕捉して予約keyをdropし、domain側の再試行状態は残さない。
  - watchdogの予約消失検査・再接続は `pending/deferred` だけを対象にし、二重実行防止のため `fired` を意図的に無視する。このためhandler成功済みのslotは `fired`、Episodeは `open` のまま自動回復対象から永久に外れる。
  - open Episodeはlife Intentで「いま何をしているか」の唯一の正典である。さらにSEAの全メッセージ保存は、最後に開いているEpisodeを `origin_episode` として自動継承する。open行はDB永続化され、プロセス再起動後も初回読出しで復元される。
  - 逆側の `fired` 保存失敗はhandler前に例外で止まり、slotがpendingのままなので、DB復旧後はwatchdogが再接続できる。破綻するのは不可逆処理後のdone/close境界である。
- 最小再現:
  1. 09:00の作業コマを予約し、作業handler自体は3ラウンド成功させる。
  2. 最初の `fired` 保存は成功させ、handler後の `status=done` 更新だけDB commit失敗相当の例外にする。
  3. EventScheduler callback終了後、slotは `fired`、対応するslot Episodeは `open` のまま残った。
  4. `find_lost_slot_reservations()` は空、`reschedule_pending_slots()` は0件で、watchdog・再接続のどちらも回復しなかった。
- 実行確認:
  - 上記を監査用の一時回帰として `tests/test_day_plan.py` に追加して実行し、壊れた収束状態を確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-done-failure-state tests/test_day_plan.py::test_audit_done_persistence_failure_leaves_open_episode_unrecoverable`
  - 結果: `1 passed`（slot fired、Episode open、lost予約なし、再接続0件を確認）。
- 影響:
  - 実際には終了した作業が「いま」の正典上では継続中になり、その後の発話・生ログが完了済みslotの `origin_episode` へ誤帰属する。新しいEpisodeが一時的に上へ積まれても、それを閉じると古いopen slotが再び最新openとして露出する。
  - 就寝判断・出来事タイムラインでは当該slotの完了区間が閉じず、終了時刻・digest参照を持つ正しい出来事として扱えない。単なる表示の遅れではなく、以後の記憶来歴を誤った出来事へ接続する。
  - slot側は「実行した（完了記録なし）」という保守的な表示に留まるが、Episode側のopen状態と分裂し、どちらを正典とするかというIntentの答え（Episode）を壊す。
- 修正方針:
  - handler実行を `execution_id` 付きの永続実行台帳にし、slot phase、対応Episode、予算予約、handler結果を同じ実行単位へ結びつける。`fired` 一語だけで「実行中」と「handler成功・精算待ち」を兼用しない。
  - handler終了後は、実測結果の保存・slot terminal化・Episode closeを一つのunit-of-workとして確定する。少なくともdone保存が失敗してもEpisode closeはfinallyで必ず試行し、どちらかが失敗した実行をreconciliation queueへ永続化する。
  - watchdog/startup recoveryはstale `fired` をLLM再実行せず、execution_idとWorkSessionのdigest/終了記録から精算・closeだけを冪等再試行する。結果不明時は自動再実行せず、open Episodeを無期限に正典化しない明示的な異常状態へ閉じる。
  - EventSchedulerの汎用的な「例外はdrop」は維持可能だが、不可逆callback側がdrop前にdomain retry/reconciliationを確立する契約を必須にする。
- 必要な回帰:
  - handler成功後、done保存・Episode closeの片方ずつ、および両方を失敗させても、再起動後のrecoveryで一度だけdone/closedへ収束すること。
  - recovery中に同じ実行を複数回処理しても、handler再実行・予算二重消費・Episode二重closeが起きないこと。
  - 異常なstale fired/open Episodeがある間と回復後で、後続メッセージの `origin_episode` が完了済みslotへ誤帰属しないこと。
  - fired保存失敗（handler未実行）とdone保存失敗（handler実行済み）を区別し、前者だけが安全に実行予約へ戻ること。

### [P1] メタ判断runtime例外を空結果へ変換し、判断成功扱いで外部イベントを破棄する

- 場所: `sea/pulse_controller.py:470-534`, `saiverse/judgment_points.py:1714-1825`, `saiverse/autonomy_wiring.py:210-301,600-685`, `saiverse/day_plan.py:1343-1386`
- 事実:
  - `PulseController._submit_meta_lane()` は `LLMError` 以外のruntime例外をLOGGERへ出した後、例外を再送出せず空リスト `[]` を返す。呼出元の `event_callback` へerror eventも通知しない。
  - `run_judgment_point()` が失敗と判定するのは、`submit_meta_judgment()` が例外を投げた場合か、callbackで `type=error` を受け取った場合だけである。空リストは検査せず、`submitted=True / errors=[] / applied_events=[]` を返す。
  - `fire_judgment_point()` は `submitted=True` だけを見て `judgment_pulses` を記帳するため、判断Playbookがruntime例外で完走していなくても判断済み回数が増える。
  - `handle_external_event()` は `submitted=False` のときだけ直接応対へfallbackする。偽の `submitted=True` ではreactionが無いので「判断は走ったが読めなかった」と解釈し、二重応対防止を理由に直接応対を起動しない。
  - generic MetaLayer側にはretryがあるが、判断点 (`run_judgment_point`) は単発submitであり、しかもPulseControllerが例外を隠すため呼出側でのretry判断もできない。
- 最小再現:
  1. 実 `PulseController` と、`run_meta_user()` が `RuntimeError` を投げるruntimeを接続する。
  2. `on_event` 判断を発火するとruntimeは落ちたが、戻り値は `submitted=True / errors=[] / applied_events=[]` になった。
  3. 同じ構成で外部イベント入口を通すと経路は `judged:unknown`、`dispatch_direct` は0回となり、イベントが応対されず終了した。
  4. 08:00〜12:00のlife内で直接発火と外部イベント発火を行うと、失敗した2回とも `judgment_pulses` に記帳された。
- 実行確認:
  - 上記を監査用の一時回帰として `tests/test_autonomy_wiring.py` に追加して実行し、壊れた戻り値・イベント経路・計数を確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-judgment-submit-count tests/test_autonomy_wiring.py::test_audit_meta_lane_runtime_exception_is_reported_as_submitted_and_drops_event`
  - 結果: `1 passed`（runtime例外2回、submitted true、error/applied event無し、direct dispatch 0回、judgment_pulses 2を確認）。
- 影響:
  - Xメンション、alert等の実イベントが、判断も直接応対もされないまま失われる。ログにはPulseControllerの例外が残るが、上位層は成功扱いなのでdomain retry・fallback・ユーザー通知のいずれも起動しない。
  - day_close等の判断点でもruntime失敗を判断済みとして計数し、その日のふりかえり・明日へのメモ・欲求レビュー等を欠落させたまま次の定刻まで進む。境界副作用だけ先に実行済みになる種類では、判断本体との状態分裂も生じる。
  - `submitted` が「受付」「実行完了」「finalize完了」の三状態を潰したboolになっており、呼出側が安全な再試行可否を判定できない。
- 修正方針:
  - PulseControllerはruntime例外を成功値へ変換せず、型付き `ExecutionOutcome`（accepted/completed/error/cancelled、execution_id）で返すか、少なくともerror event通知後に例外を再送出する。
  - 判断点側は「例外が無かった」ではなくfinalize完了の永続証跡を成功条件にする。best-effort callbackだけを唯一の証跡にせず、execution_idに紐づく完了台帳を持つ。
  - `submitted` を受付、runtime完了、finalize適用へ分解し、judgment回数・LLM利用量・適用回数も同じboolから派生させない。何を1回と数えるかを別々に定義する。
  - on_eventが完了証跡を得られなかった場合は、イベントをdurable queueへ戻して冪等再判断するか、明示的に直接応対へfallbackする。少なくとも偽成功によるdropを無くす。
- 必要な回帰:
  - runtime例外、Playbook不在、judge node失敗、finalize tool失敗、完了通知失敗の各段階で、呼出側がaccepted/completed/finalizedを正しく区別できること。
  - on_event失敗時に実イベントが一度だけretryまたはdirect dispatchされ、無処理drop・二重応対の双方が起きないこと。
  - 失敗した判断を再試行して成功した場合、判断適用とjudgment計数が定義どおり一度だけ記録されること。
  - day_open/day_closeの境界副作用と判断本体が、片方だけ成功した状態から再起動後に一貫して回復すること。

### [P1] 判断適用後のSAIMemory保存失敗を成功扱いにし、再試行で副作用を二重適用する

- 場所: `builtin_data/tools/judgment_finalize.py:987-1002,1005-1075,1513-1643`
- 事実:
  - `judgment_finalize()` は各kindの `_finalize_*()` を先に呼び、時間割・タスク・欲求・event memo等の世界状態を更新した後で、本人の判断独白と適用結果をSAIMemoryへ保存する。
  - `append_persona_message()` の例外はLOGGERへ記録するだけで握りつぶし、warning数・`committed`・ToolResultへ反映しない。そのまま `Judgment finalized (... applied=True, warnings=0 ...)` を返し、`judgment_applied` callbackも成功時と同じ内容で発火する。
  - 適用操作全体に判断実行IDや冪等性keyはない。`on_event.note_only` は既存配列へ無条件appendするため、同じ判断を再度finalizeすると同一memoがもう一件増える。他kindにもtask/desire作成、時間割置換、spell実行等の非冪等な副作用がある。
  - SAIMemoryの判断行は本人が何を考え、何を適用したかを結ぶ唯一の恒久履歴だが、その保存失敗と世界更新成功を原子的に扱わず、上位層へ部分成功を通知する経路もない。
- 最小再現:
  1. `on_event` の `note_only` 出力を用意し、SAIMemory adapterの `append_persona_message()` だけをDB障害相当の例外にする。
  2. 1回目のfinalizeでevent memoは1件保存された一方、判断行は保存されず、戻り値は `applied=True / warnings=0` だった。
  3. 同じ判断出力を再度finalizeすると、同一textのevent memoが2件へ増えた。2回目も成功扱いだった。
- 実行確認:
  - 上記を監査用の一時回帰として `tests/test_judgment_points.py` に追加して実行し、壊れた成功報告と二重適用を確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-finalize-memory tests/test_judgment_points.py::test_audit_on_event_memory_failure_reports_success_and_retry_duplicates_memo`
  - 結果: `1 passed`（1回目memo 1件、2回目memo 2件、両方 `applied=True`、warning 0を確認）。
- 影響:
  - 本人の裁定だけが記憶から欠け、世界には裁定結果だけが残る。「なぜこの状態になったか」を本人の発話として辿れず、自己著者性と監査可能性を同時に失う。
  - 上位層はfinalize完了と誤認するため、障害として再試行・保留できない。外部監視等から欠落を検出して再試行した場合は、今度はtask/desire/memo/spell等を二重適用し得る。
  - `judgment_applied` も発火するため、on_eventの `engage_now` では判断履歴が無いまま直接応対だけが開始される。履歴保存失敗と応対起動の境界も分裂する。
- 修正方針:
  - 判断ごとに永続 `judgment_execution_id` と状態（prepared/applied/recorded/completed/failed）を持ち、世界更新・本人判断行・完了通知を同一実行へ結びつける。各副作用はexecution IDを冪等性keyとして一度だけ適用する。
  - 単一DBトランザクションにできないSAIMemoryとworld DBの間はoutbox/sagaにし、判断行保存失敗時は「適用済み・記録待ち」としてdurable retryする。判断LLMや世界副作用を最初から再実行しない。
  - ToolResultとcallbackはpartial/failedを明示し、`judgment_applied` をcompletedの証拠にしない。保存失敗をwarning 0の成功として返さない。
  - 判断行を先に保存するだけでは、逆に未適用の裁定をcommittedとして記録するため不十分。prepared記録とcompleted記録を区別する。
- 必要な回帰:
  - 各kindについて、世界更新前・途中・更新後、判断行保存、callback発火の各点を失敗させ、再起動後に同一executionが一度だけcompletedへ収束すること。
  - task/desire/memo/time slot/spellがretry回数にかかわらず一度だけ適用され、判断行も一行だけ残ること。
  - 記録待ち状態では上位層がcompletedと数えず、on_eventを無処理drop・二重応対のどちらにもせず回復できること。
  - adapter不在・persona不在も黙って成功にせず、判断履歴を保持できない構成として明示的に扱うこと。

### [P1] post-session完了を先にcommitし、成果物参照失敗後は再試行でも接地を回復できない

- 場所: `builtin_data/tools/judgment_finalize.py:563-664`, `saiverse/persona_task_manager.py:573-624,807-846`
- 事実:
  - `task_verdict.status=done` は成果物refが当該セッションのartifact一覧に存在することを確認した後、`update_task_status(... completed)` と `append_artifact_ref()` を順に呼ぶ。
  - 両APIはそれぞれ独立したSessionを開き、履歴行を含めて個別にcommitする。二つを包むunit-of-workはない。
  - `append_artifact_ref()` が失敗すると例外は `_apply_task_verdict()` / `_finalize_post_session()` / `judgment_finalize()` を抜ける。この時点でcompleted遷移は既にcommit済みだが、成果物ref・本人判断行・適用完了通知は残らない。
  - 同じ判断を再試行しても、finalize冒頭の終了済みタスクガードがcompletedを検出し、再裁定全体を棄却する。このため本来「やったフリ」を防ぐための接地証跡を、通常経路では後から補修できない。
- 最小再現:
  1. pendingの `task:1` と、セッション成果物 `item-abc` を含む正当なdone裁定を用意する。
  2. `update_task_status()` は通常どおり成功させ、直後の `append_artifact_ref()` だけをDB障害相当の例外にする。
  3. finalizeは例外で終了し、タスクは `completed`、`artifact_refs=[]`、SAIMemory判断行0件になった。
  4. 障害を解除して同じ判断を再試行しても `applied=False` となり、`artifact_refs` は空のまま残った。
- 実行確認:
  - 上記を監査用の一時回帰として `tests/test_judgment_points.py` に追加して実行し、壊れた永続状態と再試行不能を確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-artifact-partial tests/test_judgment_points.py::test_audit_post_session_artifact_failure_strands_completed_task`
  - 結果: `1 passed`（初回は例外、completedかつartifact空・判断行0、再試行後もartifact空かつ `applied=False` を確認）。
- 影響:
  - 成果物実在の接地を完了条件として掲げながら、永続状態には証拠のないcompletedが作られる。翌朝のバックログ・Atlas/机・履歴から成果物へ辿れず、「完了したが何を作ったか分からない」状態が確定する。
  - completed_atと完了履歴だけは残る一方、本人の完了裁定そのものはSAIMemoryに無い。タスク台帳・成果物来歴・本人記憶の三者が分裂する。
  - PulseController経由では外側のgeneric例外握りつぶしと合成し、上位層からはsubmitted成功に見えるため、自動補修の契機も失う。
- 修正方針:
  - 完了status、artifact_refs、両履歴をPersonaTaskManager内の単一トランザクションで更新する `complete_with_artifact()` に集約する。呼出側で二つの公開APIを順番に組み立てない。
  - optimistic versionを同じ更新へ含め、同一judgment execution IDで冪等にする。既にcompletedでも、同じexecutionがartifact未記帳なら補修だけを許可する。
  - SAIMemoryとの原子性は前項のoutbox/sagaで扱い、task DB commit後に記録待ち状態を失わない。
- 必要な回帰:
  - status更新・artifact更新・各履歴insert・commitの各失敗で、全項目が未変更か全項目が一度だけ更新済みのどちらかになること。
  - commit応答喪失を含む再試行でcompleted_at、version、履歴、artifact refが二重化せず、一回で収束すること。
  - 旧バージョンで既に生じたcompleted＋artifact空を検出し、セッション実績から安全にbackfillできるmigration/repairを用意すること。

### [P1] 「完全手動モード」がscheduleと時間割予約を止めず、自動LLM発火を継続する

- 場所: `api/routes/people/debug.py:188-222`, `frontend/src/components/DebugPanel.tsx:69-78,146-155`, `saiverse/autonomy_manager.py:132-171`, `saiverse/schedule_manager.py:66-188,330-365,423-475`, `saiverse/autonomy_wiring.py:202-298,381-439`, `saiverse/day_plan.py:1922-2001`, `docs/intent/persona_cognition/debug_controller.md:11-16,40-52`
- 事実:
  - UIのボタンは「全タイマー停止して手動へ」、Intentは「完全手動モードONのとき自動発火はゼロ」と定義する。
  - 実装は `{autonomy:false, manual_mode:true}` を送り、backendはAutonomyManagerのwatchdog予約をstopし、`_debug_manual_mode_personas` を立ててrunning Trackのwait_response timeoutだけをcancelする。
  - PersonaScheduleを担うScheduleManager、EventScheduler上の `schedule:*` 予約、day planのslot予約は停止・cancelされない。scheduled judgmentのゲートもDBの `AI.AUTONOMY_ENABLED` 相当であり、AutonomyManagerのrunning stateやdebug manual flagを見ない。
  - 解除側はwait_response timeoutだけを再予約する。手動化で止めていないschedule/slotはON中も継続し、AutonomyManagerは自動再開しないため、状態名と実際に動くタイマーの組合せも非対称になる。
- 最小再現:
  1. `autonomy_enabled=True` のpersonaでdebug controllerへ `{autonomy:false, manual_mode:true}` を送る。
  2. responseは成功しmanual flagもONになる。
  3. 直後にPersonaSchedule由来の `judgment_day_open` 発火経路を通すと、`submitted=True` となりPulseControllerへ1回submitされた。
- 実行確認:
  - 上記を監査用の一時回帰として `tests/test_autonomy_wiring.py` に追加して実行し、完全手動ON後のscheduled day_open submitを確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-manual-mode tests/test_autonomy_wiring.py::test_audit_complete_manual_mode_still_allows_scheduled_day_open`
  - 結果: `1 passed`（manual flag ON、scheduled day_open `submitted=True`、PulseController呼出1回を確認）。
- 影響:
  - 決定論的な手動検証中に、起床・就寝判断、時間割の作業セッション、一般scheduleが割り込み、LLMコスト・タスク状態・記憶・時間割を裏で変更する。まさにこの機能が防ぐべき非決定論が残る。
  - UIとAPIは成功を返し「完全手動」と表示するため、利用者は自動発火が無いという誤った前提で観測結果を解釈する。検証結果の信頼性を壊すだけでなく、操作対象personaが本人の裁定なく活動する。
  - `autonomy_enabled` 自体をOFFにしていないので、schedule以外の判断入口もmanual flagを見なければ通過し得る。停止対象を個別列挙する現在方式では、新しいタイマー追加のたびに再発する。
- 修正方針:
  - per-personaの単一 `execution_gate` をEventScheduler callbackの実行直前に置き、manual modeではデバッグ明示発火以外の全persona起動を抑止する。timer種類ごとのcancelだけに依存しない。
  - manual ON時はAutonomyManager、wait-response、PersonaSchedule、day-plan slot、TTL/keepalive等の「personaの行動を生む予約」を分類してpauseする。単なる削除ではなく、解除時に元のfire_at/意味を安全に復元するpause契約が要る。
  - UI/APIは停止対象と残る世界共通タイマーを明示し、manual状態とAutonomyManagerの元状態を保存して解除時に対称復帰する。
- 必要な回帰:
  - manual ON中にwatchdog、day_open/day_close、一般schedule、day-plan slot、wait-response、external eventの各入口が自動Pulseを0回にすること。
  - debugの明示ボタンだけはforce設定どおり一回発火できること。
  - ON直前・callback dequeue直後・callback実行直前の各競合で、ON完了後に自動処理が開始されないこと。
  - OFF後はON前に稼働していたものだけが一度だけ復帰し、期限切れslot/scheduleを遅れて一斉発火しないこと。

### [P1] spell実行失敗をcommitted成功として、本人の記憶に未実行の行為を記録する

- 場所: `builtin_data/tools/judgment_finalize.py:80-115,443-475,1513-1643`
- 事実:
  - `_fire_spell()` はtool不在と実行例外を `spells_record[].result` に格納するだけで、成功/失敗を返さずwarningsにも追加しない。
  - 呼出側のpromotion/new_desire/track completeは `_fire_spell()` の結果を検査せず、常に `applied=True` にする。`spells_record` が1件でもあればscopeもcommittedになる。
  - SAIMemoryへ整形する `_spell_to_text()` はnameとargsだけを出力し、`result` を完全に捨てる。このため実行が失敗した場合も、本人の判断行には成功時と同じ `/spell ...` が残る。
  - summaryのspell数も「試行数」であって成功数ではないが、失敗warningは0になり得る。上位層・本人・後続文脈のどこからも、その行為が実在しないことを判別できない。
- 最小再現:
  1. post-session判断に正当な `new_desires` 1件だけを含め、`purpose_seed` toolを例外にする。
  2. 欲求作成は失敗したが、戻り値は `applied=True / warnings=0 / spells=1`、SAIMemory scopeはcommittedだった。
  3. 保存された本人のcontentには、例外や失敗表示のない `/spell name='purpose_seed' args=...` が記録された。
- 実行確認:
  - 上記を監査用の一時回帰として `tests/test_judgment_points.py` に追加して実行し、偽成功と記録内容を確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-spell-failure tests/test_judgment_points.py::test_audit_failed_desire_spell_is_recorded_as_committed_success`
  - 結果: `1 passed`（tool例外、summary `applied=True/warnings=0/spells=1`、scope committed、成功形spell行を確認）。
- 影響:
  - 本人は「やりたいことを作った」「関心へ昇格した」「Trackを完了した」と記憶する一方、実世界には存在しない。次の起床判断・机・タスク一覧がその対象を提示せず、本人の自己像と世界状態をシステムが分裂させる。
  - `track_complete` 等の操作では未完了状態が残るのに完了したという履歴が残り、後続判断が誤った前提で計画を組む。ログを見ない通常利用では原因を追えない。
  - spell tool自身が「エラー文字列を正常returnする」型でも、現契約は結果の意味を判定しないため同型の偽成功が広がる。
- 修正方針:
  - tool実行結果を型付き `SpellOutcome(success, changed, summary, error)` に統一し、`_fire_spell()` は呼出側へ返す。例外・tool不在・失敗ToolResultをすべてfailureとして扱う。
  - `applied` は成功かつ実変更があった操作だけで立て、warningsとcallbackへ失敗を構造化して渡す。attempted/succeeded/failedの件数を分離する。
  - 本人の記憶には、成功したspellだけを正準 `/spell` として載せる。失敗はシステム名義の適用失敗行として明示し、本人が実行した事実に変換しない。
- 必要な回帰:
  - registry不在、tool例外、失敗ToolResult、deferred適用失敗の各場合に `applied=False`、warningあり、成功形spell行なしとなること。
  - 複数spellの一部だけ成功した場合、実際に成功した行だけがcommittedとして記録され、失敗項目が個別に識別できること。
  - deferred Track操作はenqueue成功ではなくPulse終端の適用成功まで完了状態を分け、後段失敗を判断executionへ返せること。

### [P1] schedule設定のDB commit後に予約同期失敗を握りつぶし、有効表示のまま発火しなくなる

- 場所: `api/routes/people/life_settings.py:238-284`, `api/routes/people/schedule.py:83-131,133-168,170-245,247-276`, `saiverse/schedule_manager.py:66-188`
- 事実:
  - life設定と一般schedule CRUDは、PersonaScheduleのDB commitを先に完了してからScheduleManagerのregister/unregisterを呼ぶ。
  - 予約同期の例外はLOGGERへ出すだけで、DBをpending/retry状態にせず、API responseにも反映しない。create/update/life PUTは成功を返す。
  - ScheduleManagerがDB全件とEventSchedulerを照合するのは `start()` 時だけで、稼働中の定期reconciliationはない。register失敗後に自動再試行するdurable queueもない。
  - create/enable/life PUT失敗では有効なDB行だけが残り予約が無い。update失敗では旧時刻の予約が同じkeyで残り、旧時刻に一度発火してから次回registerで新設定へ追いつく。delete/disableのunregister失敗はcallback側のDB再読みによりLLM実行は防ぐが、不要callbackは残る。
- 最小再現:
  1. life設定PUTでwake 07:00 / close 22:00を保存し、ScheduleManagerの `register_schedule()` を2回とも例外にする。
  2. APIはHTTP 200で新設定を返し、DBにはenabledなday_open/day_closeが2行commitされた。
  3. EventSchedulerへの登録試行は両方失敗しており、稼働中にそれを再試行する経路は無い。
- 実行確認:
  - 上記を監査用の一時回帰として `tests/test_life_settings_api.py` に追加して実行し、成功responseとDB/予約同期の分裂を確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-schedule-sync tests/test_life_settings_api.py::LifeSettingsApiTest::test_audit_put_reports_success_when_both_scheduler_pushes_fail`
  - 結果: `1 passed`（HTTP 200、DB 2行、register試行2回とも例外を確認）。
- 影響:
  - UIには起床・就寝時刻が正常保存済みと見えるのにday_open/day_closeが発火しない。時間割編成、life確定、就寝ふりかえり、欲求レビュー等が丸ごと欠落する。
  - 一般oneshot/intervalも作成・有効化成功に見えたまま無期限に発火しない。ユーザーは設定値を再編集するまで障害を知る手段がログしかない。
  - update時は変更前時刻に一度発火するため、単純な欠落だけでなく「変更したはずの時間に旧判断が走る」誤実行になる。
- 修正方針:
  - DBをscheduleの正典とし、EventScheduler登録をdurable outbox/reconciliation対象にする。commitと同時に同期世代を増やし、scheduler側が世代一致まで再試行する。
  - APIは少なくとも `saved_but_not_scheduled` を明示し、完全成功と同じresponseにしない。同期完了を要求する操作ではregister失敗を5xxへ返し、DB側にretry状態を残す。
  - EventSchedulerのkeyへschedule IDとgenerationを結び、旧予約callbackは発火直前に世代不一致なら行為を起こさず最新予約へ置換する。
- 必要な回帰:
  - create/update/enable/life PUTのregister失敗後、プロセスを再起動せずreconciliationで最新時刻へ一度だけ登録されること。
  - update競合で旧generation callbackがLLMを起動せず、最新generationだけが発火すること。
  - delete/disableのunregister失敗でもcallbackが行為を起こさず、reconciliationでheapから除去されること。
  - API responseがDB保存とscheduler同期の状態を正確に区別すること。

### [P1] schedule dispatch失敗後も実行済みに更新し、oneshotを再試行不能にする

- 場所: `saiverse/schedule_manager.py:330-365,423-534`
- 事実:
  - `_handle_fire()` は `_execute_schedule()`、`_update_schedule_after_execution()`、次回registerを無条件に順番実行する。
  - `_execute_schedule()` は通常Playbookの `dispatch_schedule_fire()` 例外をLOGGERへ出して握りつぶし、成功可否を返さない。判断点専用経路の戻り値 `submitted=False` も検査せずreturnする。
  - その後oneshotは `COMPLETED=True`、intervalは `LAST_EXECUTED_AT=now` をcommitする。oneshotの次回fireは無いものとしてcancelされ、intervalは失敗時点から丸々1 interval後まで再試行されない。periodicも当該周期は実行済み同然に次周期へ進む。
  - callback自体が `_execute_schedule()` の外で例外を投げた場合はEventSchedulerの最外周が予約をdropするため、どちらの失敗形にもdurable retryはない。
- 最小再現:
  1. enabledなoneshot scheduleをDBへ作り、`pulse_dispatcher.dispatch_schedule_fire()` を例外にする。
  2. ScheduleManagerの実発火callbackを呼ぶと、Pulse dispatchは失敗した。
  3. DB行は `COMPLETED=True` になり、`schedule:{id}` の予約は存在しなくなった。
- 実行確認:
  - 上記を監査用の一時回帰として `tests/test_autonomy_wiring.py` に追加して実行し、失敗後のcompleted化と予約消失を確認後にテスト自体は削除した。
  - コマンド: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider --basetemp .\\temp\\audit-pytest-oneshot-failure tests/test_autonomy_wiring.py::test_audit_failed_oneshot_dispatch_is_marked_completed`
  - 結果: `1 passed`（dispatch例外、DB `COMPLETED=True`、EventScheduler予約なしを確認）。
- 影響:
  - 一度きりの予定がLLMへ届かないまま履行済みとして永久消失する。ユーザー・personaの双方からはenabled/completedな正常履歴に見え、retry操作の対象にもならない。
  - interval/periodicの定期処理も障害中の実行を欠落させる。day_open/day_close専用経路がpreconditionやPlaybook不在で `submitted=False` を返した場合も同じで、起床・就寝判断を一周期失う。
  - 外部dispatchが「受付後に例外」の場合、単純な再試行は二重実行の危険があるが、現状はexecution IDも受付証跡も無いため、安全側の照合すらできない。
- 修正方針:
  - `_execute_schedule()` は型付きoutcome（accepted/completed/failed/unknown、execution_id）を返し、成功時だけscheduleの実行済み状態を進める。
  - oneshot/intervalの発火を永続execution行でclaimし、dispatch前・受付後・完了後を区別する。失敗はbackoff付きretryへ残し、unknownは同一execution IDで照合・冪等再送する。
  - 判断点の `submitted=False` とfinalize未完了をschedule成功に変換しない。schedule起点とjudgment executionを同じIDで結ぶ。
- 必要な回帰:
  - dispatch前例外、受付拒否、受付後応答喪失、runtime/finalize失敗の各段階でoneshotがcompletedにならず、安全に一度だけ回復すること。
  - intervalの失敗時にLAST_EXECUTED_ATを成功時刻として更新せず、backoff retry後に次周期基準が定義どおりになること。
  - periodic day_open/day_closeが失敗時に当日中のretry対象へ残り、翌日まで欠落しないこと。

## 次の監査片

- `SEA runtime / Session / head-tail` のline/model/thread隔離、cache snapshot、Metabolism境界
