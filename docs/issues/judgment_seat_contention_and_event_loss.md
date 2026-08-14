# 判断点の席の競合制御と、イベントの取りこぼし

**発見**: 2026-07-30（[判断プロンプトの静的一覧を head へ](judgment_static_lists_to_head.md) の Codex レビュー五巡目。移設の範囲外として切り出し）
**状態**: 実装済・実機検証待ち（2026-07-31。③はまはー裁定で **B** に確定 — 下の「裁定の記録」。④⑤は 2026-08-14 追加・実装済）
**関連**: `docs/intent/execution_ledger.md`、`docs/overview/audit_remediation_plan.md`（実行台帳 W1〜）、`saiverse/judgment_points.py` / `saiverse/autonomy_wiring.py` / `saiverse/execution_ledger.py`

## なぜ切り出したか

判断プロンプトの一覧を head へ移す作業のレビューで出てきたが、**中身は実行台帳の競合制御と回復**で、移設とは別の機構。ここで続けると一覧の移設が台帳の設計監査に化けたまま終わらない。移設側で閉じたのは「自分が作った穴」までで、以下は移設前から在るもの（③だけは移設の対処が新しく作った経路）。

## ① 席取りが CAS でなく、同じ判断を二重に開始しうる

`ExecutionLedger.claim_execution` は既存の prepared 行を再利用するため、**ほぼ同時の二重 claim には同じ `execution_id` を両方へ runnable として返す**。勝者を一意にするための条件付き遷移が `try_mark_running`（status=prepared のときだけ running）で、docstring にも「勝者の一意化」と明記されている。

ところが `run_judgment_point` は `mark_running`（無条件遷移）を呼んでいる。両者が prepared を観測すると**両方が `submit_meta_judgment` へ進みうる** — 有料 LLM 呼び出しと finalize の適用が二重になる。

- 直し: `try_mark_running` へ置き換え、False の側は台帳を触らず離脱する。
- 要検討: 敗者の結末は `duplicate` か `indeterminate` か（呼び出し側が代替経路を走らせてよいかが変わる）。

## ② 発火側の早期離脱が、勝者の running 台帳を failed に壊す

`fire_judgment_point` は claim の後、precondition の失敗と day_open / day_close の境界失敗で `_safe_mark_failed`（= `mark_failed`）を呼ぶ。`mark_failed` は running からの遷移も許すため、**別の claimant が既に running へ進んだ後にこれが走ると、勝者の台帳を上書きする**。勝者はそのまま LLM を実行中で、finalize の applied 遷移が失敗して結果の証跡を失う。

`run_judgment_point` 側は 2026-07-30 に `abandon_prepared`（prepared 限定 CAS）へ寄せたが、**入口側は mark_failed のまま**で非対称。

- 直し: pre-dispatch の離脱を全部 `abandon_prepared` に統一する（precondition / ライフ境界を含む）。

## ③ 代替経路を止めた結果、イベントが永久に消えうる

移設側の対処で入れた経路。席を放棄できなかった判断（別 claimant 所有 / 台帳が応答しない）では、`handle_external_event` は direct dispatch を**行わない**（二重応対を避けるため）。したがって prepared 回収だけが唯一の処理経路になる。

ところが回収側は、refire が `submitted=False` を返すと残っている prepared 行を**その場で failed に終端化**し、direct dispatch はしない。台帳障害のあとの refire 時に「自律 OFF / Playbook 欠如 / persona・pulse_controller 不在 / 再度の args 構築失敗」のどれかが起きると、**元のイベントは判断にも応対にも届かず消える**。一時障害がユーザーに見えるイベントの欠落として確定する。

### 裁定が要る点

**二重応対とイベント消失のどちらを取るか。** 既存コードの明示的な答えは「二重応対の方が害が大きい」（`handle_external_event` の unknown_reaction 分岐のコメント）で、現状はそれに揃えてある。消失は台帳が応答しないときに限られるが、限られていても消える。

選択肢:

- **A. 現状維持** — 消失を受け入れ、台帳障害を運用で検知する（ログ・監視）。
- **B. 回収側に backoff を入れる** — 一度の非 submission で終端化せず保持し、条件が直れば処理される。消失は減るが、prepared が長く残る。
- **C. 元イベントから durable な代替応対を確定できる回復経路を作る** — 一番正しいが、イベント本文を台帳の payload として持ち回す設計が要る。

### 裁定の記録（2026-07-31、まはー）

**B を採用。** 根拠: 「台帳が応答しない」の実体は、本体 DB（saiverse.db）への `abandon_prepared` 書き込みが例外で失敗した瞬間で、現実的にはほぼ一瞬のロック競合（database is locked / Windows のファイルロック）。この分岐に来る時点で数秒前の claim は成功しているため、DB が恒久的に死んでいるケースはそもそもここに来ない。障害の正体が「一瞬」なら、保持して再試行すれば直っており、B がその形の障害への正しい処方箋。C の新構造（イベント本文の payload 持ち回し）は守備範囲の狭さに対して台帳の複雑さを一段増やすので見送り。

## ④ 判断が走った後の失敗を「起動できなかった」と読み、決定を上書きして応答する

**発見**: 2026-08-14（Track 撤廃 順序①の Codex レビュー high 指摘 F3。①より前から在る on_event 系の共通欠陥として、順序①の後始末ではなくここに記録する）

呼び出し側（`handle_external_event` / `handle_user_utterance_conflict`）は「`submitted=False` かつ `outcome` が indeterminate **でなければ** 起動できなかった」と読んで代替経路（direct dispatch / activate）を走らせていた。ところが `outcome` を載せていたのは LLM 開始前の離脱経路だけで、**メタレーンへ渡った後の失敗（実行時例外 → 台帳 unknown、finalize 証跡なし、failed）は `outcome` を持たない**。したがって「finalize が note_only を適用した後に例外で落ちた」ケースが「起動できなかった」に分類され、判断の決定を機構が上書きして応答していた（不変条件 b 違反）。

読み手が**キーの不在**から意味を推論していたのが根で、書き手が新しい失敗経路を足すたびに黙って代替経路が走る形だった。

- 直し: 結末の語彙を「呼び出し側が代替経路を走らせてよいか」を答え切る形に広げ（`aborted` / `no_effect` / `ran` / `indeterminate`）、判定を `judgment_points.direct_fallback_allowed` 1 箇所に集約。**結末の無い結果は拒否側に倒し WARNING を出す**（書き忘れが沈黙として観測できる）。
- 実行時例外の結末は「台帳へ何を書けたか」から導く（副作用ゼロ確定 → failed → `no_effect` = 代替経路 OK / それ以外 → unknown → `ran` = 代替経路 NG / 台帳遷移自体が失敗 → `indeterminate`）。例外型の読み分けは 1 関数に集約し、台帳の終端と呼び出し側の結末が食い違わないようにした。
- `submitted=False` を返す全経路に結末を付けた（自律 OFF・Playbook 未 import = `aborted` / duplicate:running = `indeterminate` / duplicate:applied・completed・unknown = `ran` / resume 系 = `indeterminate`）。
- schedule 側の精算（`_classify_judgment_outcome`）も同じ語彙に揃えた：`ran` は unknown（自動再実行禁止）。従来は「未知の reason は保守的に failed」で再試行に落ち、下流の duplicate:unknown 検出に救われていた。
- **テストが欠陥を守っていた**: `test_external_event_runtime_error_marks_unknown_and_falls_back_once` は「generic RuntimeError で fallback が 1 回起きる」を期待値に固定していた。期待値を「fallback しない（`none:judgment_ran`）」へ改め、副作用ゼロ確定（LLM エラー）の対比テストを足した。直結経路（`handle_user_utterance_conflict`）は直接テストが 1 件も無かったので新設（自律 OFF / Playbook 欠如 / unknown / 副作用ゼロ / engage_now / note_only の 6 系統）。

## ⑤ 回収の応対が種別を落とし、ユーザー発話を「外部イベント通知」として流し込む

**発見**: 2026-08-14（同レビュー high 指摘 F4）

回収の応対（`_dispatch_recovered_event_response`）は外部イベント用に作られており、判断 context の種別を見ない。別行動中のユーザー発話の仲裁（`utterance_conflict`）が席を残したまま落ち、回復 tick が engage_now を出すと、**ユーザーの発話が `<system>[外部イベント通知]` + `track_user_conversation` の形で流し込まれる**。応答は届くが、対ユーザー会話 Track は running にならず、会話の出来事も開かず、Track 切替通知も出ない — 応答と帳簿が食い違う。

初回の応対経路（`activate` → `on_track_activated` hook）が回収側に無いのが構造的な理由で、③④と同じ「初回と同じ入口を通せない」問題の別の顔。

- 直し: 仲裁の context に**種別と応対先（`conversation_track_id` / `conversation_user_id`）を凍結**し、回収側は `_dispatch_recovered_response` で入口を選ぶ — 仲裁なら会話 Track の `activate`（hook が切替通知・会話の出来事・main_line をまとめて担うので初回と一致）、それ以外は従来の応対 Pulse 再構成。
- 二重応対の境界: 既に会話が生きている（Track running **かつ**会話の出来事が開いている）なら activate しない。案 Y の残留 running を「応対済み」と読まないため、判定は開いている出来事で行う（running だけで判定すると、この発話への応答が失われる）。
- 応対先を決められない payload（この機構より前の prepared 行）は、外部イベント形へ縮退**させない** — それが欠陥そのものなので、`unroutable`（再試行しても直らない）として ERROR で残して打ち切る。
- `handle_user_utterance_conflict` の `track_id` / `user_id` は必須キーワード引数にした（唯一の呼び出し元が凍結を忘れたら TypeError で落ちる）。

## 対応の記録（2026-07-31 実装）

- **①** `run_judgment_point` の席取りを `_try_mark_running`（`try_mark_running` の prepared 限定 CAS、旧スタブは `mark_running` へ degrade）に変更。敗者は台帳に一切書かず `outcome=indeterminate` で離脱する（勝者が同じ判断を処理するため、呼び出し側は代替経路を走らせない）。
- **②** `fire_judgment_point` の pre-dispatch 離脱（precondition raised / rejected、day_open / day_close のライフ境界失敗）を `_release_claimed_seat`（= `_abandon_seat` の prepared 限定 CAS）に統一し、結果 dict に `outcome` を載せた。旧 `_safe_mark_failed` は撤去。
- **③（裁定 B）** 回復 tick の refire が非 submission でも、その場で終端化せず prepared を保持して毎 tick 再試行する。再試行窓は `PREPARED_REFIRE_EXPIRE_AFTER_SECONDS`（1800 秒）で、超過したら expired 終端（恒久条件の判定）。窓の起点は行齢でなく**このプロセスで最初に refire を試みた時刻**（in-memory）——行齢基準だとプロセス停止・手動モードの時間が窓を消費し、長時間停止後の再起動で一度も refire せず失効させてしまう（Codex 一巡目 high2）。放棄が台帳例外で失敗したときは初回時刻を保持し、窓を配り直さない（Codex 二巡目 medium）。あわせて回収側の期限切れ放棄（`PREPARED_EXPIRE_KINDS` 含む）も無条件 `mark_failed` から `abandon_prepared` の CAS へ変更。
- **schedule 側の精算分類**（`_classify_judgment_outcome`）: **再試行の claim を勝者の終端の照合に使う** — `duplicate:running` と indeterminate は新設の **waiting**（副作用ゼロだが実処理の失敗ではない = **attempt を消費しない** backoff 再試行）で勝者の終端を待ち、`duplicate:unknown` は unknown（自動再実行禁止）、applied / completed だけが settled_skip。待機ループの終端は台帳の running 期限監視（1 時間で unknown 化）が保証する。これで「勝者がまだ実行中なのに occurrence が前進」（一巡目 high1）「unknown 恒久ブロックで勝者 failed 後に当営業日分が回収不能」（二巡目 high）「待機が attempt 予算を食い潰して occurrence を放棄」（三巡目 high2）を全部塞ぐ。
- **回収 refire の応対復元**（三巡目 high1）: on_event の回収 refire は `refire_judgment_from_recovery` 経由になり、判断が engage_now を選んだときは **payload の凍結 `event_text` から応対 Pulse（`<system>[外部イベント通知]`）を再構成して起動する**。初回発火の応対 closure は回収側に無いため、これが無いと alert（engage_now しか選べない）で「回収成功に見えて応答が必ず欠落」していた。イベント本文を独立に持ち回す C 案の機構は作らず、既に payload が運んでいる情報だけで復元する。
- **schedule.dispatch の prepared 回収も CAS 化**（三巡目 medium）: payload 不備・設定世代不一致での放棄を無条件 `mark_failed` から `_abandon_prepared_row` へ（判断側と同じ規律）。
- **四巡目の追補**: ① waiting の failed 行には ERROR に `waiting:` 接頭辞を刻み、crash 後の failed 回収も attempt を据え置いて refire する（回収の +1 が待機を失敗として数えない）。② 応対の材料（組み立て済み `user_input` / `meta_playbook` / `args` / `event_type`）を `dispatch_envelope` として判断 context ごと台帳 payload に凍結し、回収の応対はそれを**そのまま**再送する（LLM に渡る judgment_context には入らない）。③ `dispatch_phenomenon_event` を型付き戻り値（bool）にし、応対復元は submit の成否を偽らず報告する。
- **五巡目の追補**: ① waiting 判定を attempt 上限の除外より先に行う（上限値のまま waiting で crash した行も据え置き回収）。② `dispatch_phenomenon_event` を `dispatch_schedule_fire`（型付き outcome）の薄いラッパーへ — Beat 関所閉鎖や PulseController 内部で畳まれた例外を「成功」と報告しない（completed / queued / cancelled のみ True）。③ 回収応対の明示失敗は EventScheduler の揮発予約で bounded 再試行（120 秒 × 3 回、上限で ERROR）。④ `inject_persona_event` も成否を観測し、Pulse が起動していないのに `ok` を返さない。
- **六巡目の追補**: ① 回収応対の成否を三値化（`dispatched` / `safe_failure` / `unknown`）— 分類は schedule 側で確立済みの `_classify_dispatch_outcome` を借用し、**再試行するのは副作用ゼロ確定（safe_failure）だけ**。実行中の例外（unknown = LLM が動いたか不明）は再試行せず ERROR（二重応対の方が害が大きい）。② `PulseController._queue_for_resumption` が復帰 request に `args` / `pre_spells` をコピーしていなかった既存バグを修正（中断復帰で元と異なる入力になっていた — 判断回収に限らない一般バグ）。③ レビューが掘り当てた**当該案件より前からあるプラットフォームの性質** 2 件（Pulse queue の上限超過での最古破棄 / PhenomenonManager 非同期 worker の戻り値破棄）は範囲外として [event_delivery_reachability_gaps.md](event_delivery_reachability_gaps.md) に切り出した。
- **七巡目の追補**: 復帰 request へのコピーを目的で分けた — `args`（純粋な入力）はコピー、`pre_spells`（副作用アクション、割り込み時点でほぼ実行済み）は**コピーしない**（六巡目の私の修正が復帰のたびに副作用 Spell を再実行する穴を作っていた）。残り 2 件（正常戻りに畳まれた実行拒否の completed 偽装 / cancelled の accepted 語義）は当該案件より前からの PulseController の性質として [event_delivery_reachability_gaps.md](event_delivery_reachability_gaps.md) ③④へ追記した。
- **九巡目の裁定（見送り）**: 「CAS より前にライフ境界処理が走るので敗者が境界を永続化できる」という指摘は、発火条件が現行アーキテクチャで成立しないため見送り。根拠 = 排他の層構造: ①本番の判断起動は全て `fire_judgment_point` の per-persona Lock 内で claim〜境界〜実行まで直列化（`run_judgment_point` の他の呼び出し元は sandbox シミュレータのみ）②runtime_marker が同一 DB の他プロセスを起動時排除 ③仮に interleave しても `confirm_life_for_today` は冪等（先勝ち）・節目は永続マーカーで一度きり・判断入力は claim 時凍結 payload が正典。台帳 CAS は Lock の代替ではなく契約レベルの砦（直接呼び出し・将来のマルチプロセス防御）。層構造は `fire_judgment_point` のコメントに明文化した。
- **十巡目の追補**: 九巡目の見送り裁定の層② (プロセス間排除) に Luna が実在の穴を示した — **CITYNAME 自動修復が稼働中の City を乗っ取れる** (`python main.py city_b` が単一 City DB の CITYID=1 を改名して同一ペルソナ群の 2 プロセス運転を作る。runtime marker は City 名でしか二重起動を弾かない)。修正 = `_init_city_config` の自動修復に関所を追加: 同じ DB を所有する稼働中の別プロセスがいる間は修復を拒否（`runtime_marker.another_running_process_owns_db`）。CLAUDE.md の「city_b はエラーになる」という stale 記述も現実に合わせた。これで層②が塞がり、同巡の残り 2 件（confirm_life の create-once 非原子性 / CAS 前の args 構築）は**プロセス並走の前提ごと消滅**（confirm_life の実行呼び出し元は判断ロック内の 1 箇所のみ、decay_desires は日付基準の冪等を確認済み）。受け入れた注記 = ①abandon 失敗→再 claim の系列で台帳 payload（claim 時凍結）と実引数（発火時実況）が乖離しうる（実行は一度きり・監査記録のズレのみ）②回収の再試行窓はプロセス内 in-memory 起点なので、30 分以内の再起動を繰り返すと窓が配り直される（意図した設計 — 停止を跨いだ prepared に復旧後の窓を与える方が、恒久条件の若干の延命より価値が高い。定数コメントに明文化済み）。
- **十一巡目の追補**: 修正 = **CITYNAME 自動修復を単一 City DB に限定**（複数 City の DB で未知名を渡すのは呼び出しの誤り — CITYID=1 の所属を黙って書き換えない）。切り出し = 「別 SAIVERSE_HOME + 明示 DB パスで同じ DB を二重運転できる（marker はホーム単位）」「起動前検査の順序（marker → 所有検査 → backup/migration）」「二重運転成立時の decay_desires 履歴 append の並走」は、オペレーターの明示的な多重設定を前提とするプラットフォーム排他の設計課題として [event_delivery_reachability_gaps.md](event_delivery_reachability_gaps.md) ⑥へ（DB 隣接の所有レコード + preflight 並べ替え。正規の複数 City 同居を許す必要があるため小さな intent を切って設計する）。
- **既知の限界（C 案スコープ、意図的に作らない）**: 判断の終端と応対 submit の間で**プロセスが落ちる**と応対は失われる（生存プロセス内の明示失敗は上記③で再試行される）。この窓は通常経路（`handle_external_event` の dispatch_direct）にも同じ形で存在し、埋めるには応対要求を finalize と同一トランザクションの outbox に永続化する C 案の機構が要る。裁定 B の範囲外として受け入れ、WARNING / ERROR で観察可能にした。
- 回帰: `tests/test_judgment_points.py` / `tests/test_autonomy_wiring.py` / `tests/test_execution_ledger_wiring.py` / `tests/test_schedule_manager_ledger.py` / `tests/test_schedule_reconciliation.py` に再現テストを追加、5 ファイル 237 件緑。
- 残: 実機検証。

## 経緯: 判断点の席の競合制御とイベントの取りこぼし (2026-08-04 in_flight 台帳より移送)

> 台帳の器の再設計 (次アクション欄=前向きのみ) に伴い、それまで台帳セルに積もっていた経緯の全文をここへ移した。時系列の生の堆積であり、整理はしていない。

**裁定 B + 実装完了・Codex 11 巡 (2026-07-31)**: ①席取りを prepared 限定 CAS 化 (敗者は台帳に書かず離脱) ②入口離脱も CAS 統一 ③回収は保持+再試行窓 (裁定 B — 一時障害が直ればイベントは処理される)。
巡回で拡張: schedule の waiting 精算 (勝者の終端を claim で照合・attempt 非消費)、回収応対の envelope 復元と三値成否、復帰 request の args コピー (pre_spells は副作用のため除外)、CITYNAME 自動修復の関所 (稼働中 DB の乗っ取り防止・単一 City 限定)。
範囲外は [issue](event_delivery_reachability_gaps.md) ①〜⑥へ切り出し。
フルスイート 3449 件緑。
残 = 収束判定 (十一巡目まで消化済) → コミット → 実機検証
**コミット済みを確認 (2026-08-07)**: 実装は 8d0ee00 / 15da635 として既にコミット済みだった (台帳の「コミット待ち」が stale)。残 = 実機検証のみ。観察項目は [統合検証手順](../handoff/2026-08-07_timetable_live_verification_run.md) Step 3 の横断観察に相乗り。
