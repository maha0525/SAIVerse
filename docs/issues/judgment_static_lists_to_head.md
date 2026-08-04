# 判断プロンプトの静的一覧をシステムプロンプト行きにする（変動はシステム通知）

**発見**: 2026-07-29（判断プロンプトの中身の総洗い出し。起点はまはーの「本当にそこで渡すべき情報なのか」）
**状態**: 実装済・実機検証待ち（2026-07-30）
**関連**: `saiverse/judgment_points.py` の各ビルダー、`docs/issues/autonomous_v2_post_live_gaps.md`（自律行動v2の課題ハブ）

## 裁定（まはー、2026-07-29）

判断プロンプト（tail 注入）に毎回同じ一覧を貼るのをやめ、**システムプロンプト（head）に移す**。見せること自体は必要だが、判断のたびに再送する必要がない。

対象は2つ:

1. **[施設一覧]** — 起床判断に毎朝 約1,400字。実際の時間割は全コマ @own_room で、供給が使われ方に対して過剰。
2. **[Track/タスク/欲求の一覧]**（進行中のこと・やりたいこと・すでにあるもの） — 起床判断と会話終了判断に毎回注入。会話終了判断は一日に何度も走るので再送回数が多い。

## 最重要の設計要件: 凍結と変動通知はセット

head は prefix キャッシュ保護のため凍結される。**head に入れた後に施設や Track/タスクが増減すると、head が嘘をつく**。だから head 凍結の既存のやり方と同じく、**変動をシステム通知（イベント）でペルソナに届ける**機構が必須。「head に静的な全体像・通知に差分」の対にする。

- 施設: 建物の新設・撤去・改名を通知
- Track/タスク/欲求: 追加・完了・破棄などの状態変化を通知（既存のイベント通知にどこまで乗っているか要調査）

通知側が既に存在する変動はそれに乗せ、無い変動だけ通知を足す。head 側は (persona, model) 固定・用途による出し分け禁止の既存規約に従う。

## 期待効果

- 起床判断プロンプトが約1,400字＋一覧ぶん痩せる（[昨日のふりかえり]撤去と合わせると素の姿は1千字前後になる見込み）
- 会話終了判断の再送がなくなる
- 一覧の内容が「判断の瞬間の写し」でなく head の一貫した参照になる

## 実装時の注意

- `_format_facilities` / `_format_track_backlog` / `_format_task_backlog` / `desire_summary_for_prompt` / `_format_pickable_tracks` が対象部品。呼び出し元は build_day_open_situation_text / build_post_conversation_situation_text。
- head 側の置き場は head_pipeline のセクション設計に従う（capture は全部・render で絞る規約）。
- 判断の response_schema は施設 enum を参照している（`judgment_points.py` の facility enum）。一覧を head に移しても enum の選択肢供給は判断側に残る必要がある — 「読む情報」と「選べる選択肢」を分離すること。

---

## 実装 (2026-07-30)

**新設 Section 2 枚**（どちらも `refresh_on_events` 空 = Metabolism のみ再 capture、`(persona, model)` 固定・用途で出し分けない）:

| Section | order | head の見出し | 差分通知 |
|---|---|---|---|
| `FacilitiesSection` (`sea/head_pipeline/sections/facilities.py`) | 310（現在地 Building の直後） | `## 行ける場所` | 増減・改名・役割変更 (`facilities_changed`) |
| `PurposeBacklogSection` (`sea/head_pipeline/sections/purpose_backlog.py`) | 570（生きる目的の直後） | `## 進行中のことと、やりたいこと` | 増減・状態変化・改名 (`purpose_backlog_changed`) |

差分通知は既存経路にそのまま乗る（Pulse 冒頭の `inject_diff_notifications` → 知覚バッファ → 同 Pulse で flush）ので、**判断が走る前に「一覧が変わった」がその Pulse の窓に入る**。新たな通知機構は足していない。

### 実装で決めたこと（issue に無かった判断）

1. **本人が増やしたものも通知する**。DeskSection は「本人の開閉は通知しない」方針だが、こちらは逆にした。head が凍結されている以上、通知が無ければ head の台帳は「もう無いものを載せ、今あるものを隠す」状態になる。会話終了判断の重複作成の抑止はこの台帳の正確さに依存している。
2. **鮮度・再訪回数の変化では通知しない**。欲求の減衰は毎日ゆるやかに動くので、これで通知を出すと通知が一覧の再送に化けて移設の意味が消える。表示上の鮮度は次の節目まで stale-but-real。
3. **`LifePurposeSection` の「第一階層の短いメニュー」を `PurposeBacklogSection` へ統合**した。残すと同じ Track が head 内に二度並ぶうえ、旧メニューは差分通知を持たないので「通知される一覧」と「されない一覧」が同じ head で食い違う。Track が何であるかの説明文も一覧と同じ場所へ移した。
4. **一覧の抽出を enum と共用する**（`saiverse.facility_map.candidate_buildings` / `judgment_points.list_pickable_tracks` / `list_backlog_tasks` / `list_desire_tasks`）。head（読む情報）と enum（選べる選択肢）が別実装になると「head に無い場所が選べる / head にあるのに選べない」が起きる。
5. **capture は取得失敗を握らない**。例外は pipeline へ通し、既存 snapshot の据え置き（stale-but-real）と欠損の再取得に任せる。

### Codex レビューで消し込んだ 3 件（2026-07-30、初版の欠陥）

上の 4 と 5 は**初版では守れていなかった**。指摘を受けて直した内容:

- **high1 — 取得失敗を「全件削除」として保存・通知していた**。初版は Track だけ `tracks_unavailable` フラグで保護し、タスク・欲求・欲求テキストは例外を空リスト / 空文字へ変換していた。通常の差分では「全部消えた」という嘘の通知を出して既読基準（B）を空へ進め、復旧時に同じ項目を「増えた」と再通知する。Metabolism / TTL の `capture_all` ではその空値が成功した snapshot として永続化され、pipeline の stale-but-real と欠損再取得を迂回する。判断プロンプトから一覧を抜いた後なので、live enum に ref があっても head は「バックログなし」と説明する状態になり、重複作成と誤参照を誘発する。**直し = 例外処理を精巧にするのではなく撤去**。capture の握りつぶしを全部やめ、`tracks_unavailable` はフラグごと削除（pipeline が元から持つ機構に任せれば、この状態を Section が持つ必要がない）。
- **high2 — head の Track 一覧が判断で選べる Track と一致していなかった**。初版の head は `LIVE_STATUSES`（終端以外すべて）を出し、参照子未採番の行は UUID 先頭 8 桁を ref として表示していた。一方 `collect_pickable_track_refs` は running/alert/pending かつ採番済みのみ。`TrackManager.create` の既定は unstarted なので通常経路で発生する。**移設前の会話終了判断は選べる集合と同じものを見せていた**ので、これは移設で持ち込んだ退化 — LLM は head で見た track:N を `picked_tasks.track_ref` に書けず、構造化出力で別の Track か `new` に滑り、誤った関連付けと重複 Track が永続化する。**直し = `list_pickable_tracks` を公開して head と enum で共用**。
- **medium3 — 取得失敗から復旧しても既読基準が前進しない**。`tracks_unavailable` を撤去したので経路ごと消滅。

### 二巡目 4 件（2026-07-30）

- **high1 — 節目の取り直しで既読基準が古い head まで巻き戻る**（`sea/head_pipeline/pipeline.py`）。`capture_all` は B（last_notified）を新 A で丸ごと初期化する。通常は A = 今 capture した最新なので正しいが、**capture 失敗で古い A を再利用した Section** では、diff 通知で live state まで進んでいた B が古い A へ巻き戻り、復旧後に「もう届けた追加・削除」を再通知する。前巡で capture の握りつぶしを外した結果、この既存の穴を日常的に踏むようになった。**直し = stale 再利用した Section の B は据え置く**（A が stale なら B も stale のまま。A と B は対で意味を持つ）。
- **high2 — 「head を狭めて揃える」という一巡目の直し方そのものが誤りだった**。`judgment_finalize` が「新しい関心として立てる」で作る Track は `initial_status` 未指定 = unstarted。一方 `sanitize_timetable` は生きた Track なら unstarted でも受理する。つまり**狭かったのは選択肢の側**で、一巡目の修正は「立てたばかりの関心に翌朝コマを割り当てられない」という既存の欠陥を head から見えなくし、テストで正解として固定していた。**直し = `PICKABLE_TRACK_STATUSES` を廃し `LIVE_STATUSES` へ統合**。head の一覧・enum・検証の三者が一致する。**教訓は「歯止めの条件は目的から導く。種類で書くな」の再演** — 集合を一致させるときも、どちらに合わせるかは目的（判断が指し示せるもの）から決める。
- **high3 — Track 取得だけの障害でタスク・欲求まで head から消える**。1 Section に 3 系統をまとめたので、最初の取得が失敗すると Section ごと欠ける。一方 enum 側は Track だけ空へ degrade してタスク ref を供給し続けるため、**意味の分からない ref だけが判断へ渡る**。3 系統は同じ world DB を見ているので、部分障害を作り込むより揃えるのが正しい。**直し = `collect_pickable_track_refs` の握りつぶしを撤去**（タスク・欲求は元から握っていない）。「DB が読めないなら判断を走らせない」で 3 系統が揃い、head だけ欠けた状態で判断が走ることが無くなる。
- **medium4 — 成果物参照が付いても通知しない**。`append_artifact_ref` はタスクを完了させずにこの値だけ変えるので、凍結 head が「成果物参照: なし」と説明し続け、起床判断が出来上がっているものの作り直しを予定しうる。**直し = has_artifact の変化を差分に追加**。

回帰: 取得失敗が例外として pipeline へ届くこと（`CaptureFailureTest`）／stale 再利用時に既読基準が巻き戻らないこと（`test_stale_reuse_does_not_roll_back_last_notified`）／head の Track と選べる Track が集合として一致し、立てたばかりの関心も両方に載ること（`test_head_backlog_refs_are_all_selectable`）／会話から立てた Track に時間割コマを割り当てられること（`test_new_track_from_conversation_is_selectable_for_timetable`）／成果物参照の変化が通知されること。

### 三巡目 1 件（2026-07-30）

- **high — 引数の組み立ての失敗が、実行台帳と呼び出し元の失敗処理を迂回する**（`saiverse/judgment_points.py`）。二巡目で握りつぶしを外した結果、DB 障害は `build_judgment_args` から例外として上がるようになった。ところが `run_judgment_point` はこれを `mark_running` より前・例外処理の外で呼んでおり、例外がそのまま入口まで漏れる。`fire_judgment_point` は既に席を claim 済みなので、行が `prepared` のまま残り `submitted=False` も返らない。非 alert の `on_event` は `handle_external_event` の direct fallback に入れず、120 秒後の prepared 回収で再発火して呼び出し側の再試行と重複しうる。`post_conversation` は回収対象外なので 30 分後に期限切れとなり、会話からの収穫が失われる。**直し = LLM 開始前の失敗境界として包み、席を `failed` に落として `submitted=False` を返す**（この関数の契約は「起動できなければ理由つきの結果 dict」で、周囲の abort 経路もすべて `_safe_mark_failed` + `submitted=False` で揃っている）。
- 併せて **呼び出し側の契約違反（必須 context の欠落）は畳まずに上げる** ように分離した（`validate_judgment_context`）。環境の障害と一緒に結果へ畳むと、発火経路の配線ミスが `submitted=False` として静かに流れて誰も気づかない。**例外の種類で切り分けるのではなく、検査を guarded な収集の外に置くことで目的から分けている**。

回帰: `test_db_failure_while_building_args_fails_the_ledger_row`（DB 障害で席が終端化し、例外が入口へ漏れず、LLM も開始しないこと）。

### 四巡目 3 件（2026-07-30）

三巡目の直しが浅かった。**LLM 開始前の離脱を「席の放棄」として設計し直した**。

- **high1 — 席の終端に `mark_failed` を使っていた**。`mark_failed` は running からの遷移も許すので、同じ execution_id を共有した別の claimant が既に走らせている台帳まで failed に壊しうる。この用途には `ExecutionLedger.abandon_prepared`（status=prepared のときだけ failed に落とす条件付き遷移）が**元から用意されていた**。さらに、終端自体が失敗しても `submitted=False` を返していたため、呼び出し側の代替経路（on_event の direct dispatch）と回復 tick の再発火が両方走りうる。**直し = `abandon_prepared` の CAS で放棄し、放棄できなければ結末を `indeterminate` として返す**。`handle_external_event` は `indeterminate` では代替経路を走らせない（二重応対の方が害が大きい — 既存の unknown_reaction と同じ判断）。
- **high2 — claim 後の早期 return が席を prepared のまま残す**。ペルソナ未ロード / 現在地なし / pulse_controller なしの 3 経路。三巡目で「自分が作った退化ではない」として意図的に外したが、**閉じようとした穴と同型の穴が同じ関数の隣に残っている**状態だった。**直し = pre-dispatch の離脱を `_abort` 1 本に集約**し、どの経路でも席を放棄してから返す。
- **high3 — 契約検査が post_session を見ておらず、環境チェックの後に呼ばれていた**。`session_result` 無しでも `build_judgment_args` は成果物ゼロ・0 ラウンド・終了理由不明の args を作って LLM を開始でき、**起きていないセッションを前提に裁定と時間割変更が永続化**されうる（本番の発火側 `day_plan` は result が None なら撃たない——「偽前提の状況テキストは作話を誘発する」——ので、判断点側の穴だけが残っていた）。また検査位置が環境チェックの後だったため、ペルソナ未ロード時は `event_text` 欠落も raise されず「契約違反は必ず表面化する」という主張を満たしていなかった。**直し = 検査に `session_result` を追加し、kind 解決の直後（環境の状態を見るより前）へ移動**。

回帰: `test_seat_that_cannot_be_abandoned_is_reported_indeterminate` / `test_pre_dispatch_abort_releases_the_claimed_seat`（3 経路）/ `test_post_session_without_session_result_raises` / `test_contract_violation_raises_even_when_persona_is_missing` / `test_external_event_indeterminate_seat_avoids_double_handling`。

### 五巡目 4 件 → 1 件を閉じ、3 件を切り出し（2026-07-30）

指摘の中身が移設から離れ、**実行台帳の競合制御そのもの**へ移った。ここで続けると一覧の移設が台帳の設計監査に化けるので線を引いた。

- **閉じた（自分が作った穴）— 契約検査の例外が claim 済みの席を孤児化する**。検査を `run_judgment_point` の冒頭へ移したことで、`fire_judgment_point` が席を取った**後**に ValueError が出るようになった。呼び出し側に放棄の口が無いため席は prepared のまま残り、回復 tick が同じ不正な payload で再発火しては同じ例外を繰り返す。**直し = 検査を `fire_judgment_point` の claim 前へ移した**（配線ミスは台帳に触れる前に落とす）。
- **切り出した 3 件** → [judgment_seat_contention_and_event_loss.md](judgment_seat_contention_and_event_loss.md)。①席取りが CAS でない（`mark_running` → `try_mark_running`。同じ判断を二重に開始しうる）②発火側の早期離脱が勝者の running 台帳を `mark_failed` で壊す（`run_judgment_point` 側だけ `abandon_prepared` に寄せたので非対称が残った）③**代替経路を止めたことでイベントが永久に消えうる**（移設側の対処が新しく作った経路。二重応対と消失のどちらを取るかは裁定が要る）。①②は移設前から在る欠陥。

### 移設後の判断プロンプト

- 起床判断: 昨日の自分からのメモ / 今日の活動時間と現在時刻 / 予算 / 予定されたイベント
- 会話終了判断: 現在時刻 / 残りの時間割 / 中断中の作業メモ（＋「同じものを重ねて作るな」の一文）

### テスト

- `tests/test_head_purpose_backlog.py`（新規）— render・増減/状態変化の通知・鮮度ドリフトの沈黙・取得失敗の伝播・直列化
- `tests/test_facility_map.py` — head の一覧が enum と同じ候補集合を出すこと（旧 `_format_facilities` のテストを移設先へ向け直した）＋増減・改名の通知
- `tests/test_judgment_points.py` — 状況テキストに一覧が**再送されていない**こと。表示 ref と enum の整合検査（2026-07-05 の実 LLM シムの回帰）は、表示側が head へ移ったので移設先を跨いで継続

## 経緯: 判断プロンプトの静的一覧を head へ (2026-08-04 in_flight 台帳より移送)

> 台帳の器の再設計 (次アクション欄=前向きのみ) に伴い、それまで台帳セルに積もっていた経緯の全文をここへ移した。時系列の生の堆積であり、整理はしていない。

**実装完了 (2026-07-30)**: 起床判断・会話終了判断が毎回貼り直していた一覧 (行ける場所 / Track・タスク・やりたいこと候補) を head の新設 2 Section へ移し、凍結中の増減は同 Section の差分通知で届ける対にした。
既存の Pulse 冒頭 diff 経路にそのまま乗るので通知機構の新設はなし。
実装で決めたこと = **本人が増やしたものも通知する** (head 凍結下では通知が無いと台帳が嘘になる。
DeskSection とは逆方針) / **鮮度・再訪回数では通知しない** (通知が一覧の再送に化ける) / **`LifePurposeSection` の第一階層メニューを吸収** (同じ Track が head 内に二度、しかも旧メニューは差分通知を持たず食い違う) / 候補集合の決定を 1 箇所に集約 (head=読む情報 と enum=選べる選択肢 の別実装化を防ぐ)。
**Codex レビューで初版の欠陥 3 件を消し込み (2026-07-30)**: high1 = タスク・欲求の取得失敗を空リストへ変換しており「全件消えた」という嘘の snapshot が保存・通知され、Metabolism では pipeline の stale-but-real を迂回して永続化していた (直し = 例外の握りつぶしを撤去し `tracks_unavailable` フラグごと削除。
pipeline が元から持つ機構に任せる) / high2 = head の Track 一覧が「生きている Track 全部 + 参照子未採番は UUID 先頭 8 桁」で、判断で選べる集合 (running/alert/pending かつ採番済み) と食い違い、**移設前より退化**していた — LLM が head で見た track:N を書けず別 Track か 'new' に滑って重複 Track を作る経路 (直し = `list_pickable_tracks` を head と enum で共用) / medium3 = 復旧時に既読基準が前進しない (high1 の撤去で経路ごと消滅)。
教訓は自分の報告と実装の食い違い — 「候補集合を 1 箇所に寄せた」「取得失敗と空を区別した」と書きながら、どちらも Track だけの適用だった。
**二巡目でさらに 4 件 (2026-07-30)**: high1 = `capture_all` が B を新 A で丸ごと初期化するため、capture 失敗で古い A を再利用した Section の既読基準が巻き戻り、復旧後に届け済みの変化を再通知する (一巡目で握りつぶしを外した結果この既存の穴を日常的に踏むようになった。
直し = stale 再利用分は B も据え置く) / **high2 = 一巡目の直し方そのものが誤り** — `judgment_finalize` が「新しい関心として立てる」で作る Track は必ず unstarted、`sanitize_timetable` は unstarted を受理する、つまり狭かったのは選択肢の側で、head を狭めて揃えたことで「立てたばかりの関心に翌朝コマを割り当てられない」という既存の欠陥を隠しテストで固定していた (直し = `PICKABLE_TRACK_STATUSES` を `LIVE_STATUSES` へ統合。
**「歯止めの条件は目的から導く。
種類で書くな」の再演** — 集合を揃える先も目的から決める) / high3 = Track 取得だけの障害でタスク・欲求まで head から消え、enum だけが意味不明な ref を判断へ渡す (直し = enum 側の握りつぶしも撤去し 3 系統を fail-closed で揃える) / medium4 = 成果物参照が付いても通知されず head の「なし」が残る。
**三巡目 1 件**: 握りつぶしを外した副作用で、引数の組み立ての DB 障害が `run_judgment_point` の外へ例外として漏れ、claim 済みの席が `prepared` のまま残って呼び出し元の代替経路 (on_event の direct fallback) も回復 tick の再発火も両方動きうる状態だった (直し = LLM 開始前の失敗境界として包み `failed` + `submitted=False`。
併せて**呼び出し側の契約違反は畳まず上げる**よう `validate_judgment_context` へ分離 — 例外の種類でなく検査の置き場で目的から分ける)。
**四巡目 3 件**: 三巡目の直しが浅く、①席の終端に `mark_failed` を使っていた (running を上書きしうる。
この用途には `abandon_prepared` の prepared 限定 CAS が元からあった) うえ終端失敗でも `submitted=False` を返し代替経路と回復 tick が二重に走りえた → 放棄できなければ結末 `indeterminate` として代替経路を止める ②claim 後の早期 return 3 経路 (ペルソナ未ロード/現在地なし/pulse_controller なし) が席を prepared のまま残す — **閉じようとした穴と同型の穴を「自分の退化ではない」と外した判断が甘かった** → pre-dispatch の離脱を 1 本に集約 ③契約検査が post_session の `session_result` を見ておらず、**起きていないセッションを前提に裁定と時間割変更が永続化**されうる (本番の発火側は撃たないので判断点側だけ穴)、かつ検査位置が環境チェックの後で「契約違反は必ず表面化」を満たしていなかった → 検査を追加し kind 解決直後へ移動。
**五巡目 4 件 → 1 件を閉じ 3 件を切り出し**: 指摘が移設を離れ実行台帳の競合制御へ移ったので線を引いた。
閉じたのは自分が作った穴 (契約検査の例外が claim 済みの席を孤児化 → 検査を `fire_judgment_point` の claim 前へ)。
切り出しは [judgment_seat_contention_and_event_loss](judgment_seat_contention_and_event_loss.md) — ①席取りが CAS でない (`mark_running`→`try_mark_running`) ②発火側の離脱が勝者の running 台帳を壊す ③**代替経路を止めたことでイベントが永久に消えうる** (③は移設の対処が作った経路、①②は移設前から。
**二重応対と消失のどちらを取るかはまはー裁定待ち**)。
テスト新規 42 件。
**残 = 実機観察 + 切り出し先の裁定** (head に 2 節が出ること / 判断プロンプトが痩せたこと / 一覧を変えた次の Pulse に通知が届くこと / prefix キャッシュが節目まで持つこと)
