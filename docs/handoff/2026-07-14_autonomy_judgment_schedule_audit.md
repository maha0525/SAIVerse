# 自律行動・判断点・schedule 一次監査

**開始日**: 2026-07-14

**状態**: 指摘あり・一次監査中
**監査基準**: 開始 `2dda6ab`（2026-07-14 02:39 JST）、直近再確認 `e076918`
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
- `builtin_data/tools/judgment_finalize.py` の起床判断適用
- 直近改修 `96062ce`（時間割保存の丸め・部分救済、空plan watchdog回復）

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

## 次の監査片

- `fired` / `done` 永続化失敗と、watchdog・手動回復時のslot再試行方針
- 判断submit後のfinalize失敗・永続化失敗と、成功/再試行の境界
