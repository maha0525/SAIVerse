# W7 走行メモ — 柱5: 位置・占有 (2026-07-21)

> セッション固有の走行メモ。工程の真実は [完了計画書](../overview/audit_remediation_plan.md) W7。
> 対象 finding ([分離監査](2026-07-15_persona_city_building_separation_audit.md) の非凍結残):
> **P1-2** (active occupancy 一意性 + CAS) / **P1-3** (chat 境界) / **P1-6** (Region 入口不変条件) /
> **P1-7** (Building City 変更) / **P2-1** (event key 秒衝突) / **P2-2** (startup 不正 occupancy) /
> **W5 委譲: P1-1 残片** (persona/user 属性更新の移動 service への集約)。
> P1-4 / P1-5 (dispatch 確定・来訪 profile 署名) は multi-city 凍結スコープで対象外。

## 患部の事実 (調査済み)

| # | 患部 | 事実 |
|---|---|---|
| P1-1残 | 5 呼び出し箇所 | `runtime.summon_persona` (`current_building_id`+`_mark_entry`+`_save_session_metadata`) / `runtime.end_conversation` (attr+save のみ、`_mark_entry` なし) / `admin.move_ai_from_editor` (attr+`register_entry`) / `tools/move_persona` (attr+`_mark_entry`) / `day_plan._move_persona_for_slot` (attr+mark+save)。儀式がバラバラで、漏れ (end_conversation の mark なし) も既にある。user は `runtime.move_user` が `state.user_current_building_id` を成功時更新 |
| P1-2 | `building_occupancy_log` | AIID の「EXIT_TIMESTAMP IS NULL は高々 1 行」制約なし。`move_entity` は `(AIID, BUILDINGID=from_id)` の active 行だけ close するため、stale from では旧 active 行が残ったまま新行が積まれる。user 側も DB CURRENT_BUILDINGID と from_id の照合なし |
| P1-3 | `api/routes/chat.py /send` | `req.building_id` をそのまま採用し現在地照合なし。`_persist_user_utterance` は user_id を無条件 heard_by に含める → 不在の部屋に「居た」履歴。frontend の通常経路は `/chat/utter` のみ (`/send` 直叩きは test fixture だけ)。utter の docstring「1 トランザクション」は実装と乖離 |
| P1-6 | `admin.update_region` / `set_building_region` | parent 変更時に入口 Building の REGION_ID を同期しない (入口が旧スコープに取り残される)。`set_building_region` は対象が入口かを確認せず任意 Region (自分自身含む) へ再所属/detach できる |
| P1-7 | `admin.update_building` | City 変更の拒否条件が「active AI occupancy がある場合」だけ。User.CURRENT_BUILDINGID / REGION_ID / private room / target City 存在を検査しない |
| P2-1 | `occupancy_manager._build_occupancy_events` | `event_key = occupancy:{id}:{from}:{to}:{int(now.timestamp())}` — 同一秒の同経路移動が同一 key に衝突。key の消費者は recalled_by マーキング (`mark_event_recalled`) と pending entrant 重複排除 |
| P2-2 | `manager/persona._load_occupancy_from_db` | active 全行を読み、同一 persona の複数行をそのまま複数 building の occupants へ append。`current_building_id` は読み順最後の行で確定 (任意選択)。分類も修復記録もなし |

補助事実: 移動は全て `occupancy_manager.move_entity` に漏斗化済み (AI = `runtime._move_persona` 経由 + day_plan 直呼び、user = `runtime.move_user` 経由)。W5 で move は `move.entity` 台帳実行 (単一 tx + commit 後 False なし)。即時 flush 中のハンドラ (`on_building_entered` / `on_entity_moved`) は明示 building_id 引数で動き、属性が旧のままでも成立 (W5 検証済み)。in-memory 同期は `saiverse_manager` の `_reload_regions` / `_reload_buildings` が既設。

## 設計 (Fable 裁定)

### D1. P1-2 — active occupancy の部分一意 index + 移動の CAS 化

- **DB 制約**: `CREATE UNIQUE INDEX IF NOT EXISTS uq_occupancy_active_ai ON building_occupancy_log (AIID) WHERE EXIT_TIMESTAMP IS NULL`。
  - 新設 `ensure_active_occupancy_unique(db_path)` (migrate.py) — **修復が先、index が後**: AIID ごとに active 行 2+ を検出し、canonical (ENTRY_TIMESTAMP 最新、同時刻は ID 最大) 以外を EXIT_TIMESTAMP=修復時刻で close。修復は 1 tx + 監査ログ (どの行を canonical にしたか行単位で INFO)。main.py の他バックフィルと同列で毎起動呼ぶ (冪等)。
  - **モデル metadata には載せない** (裁定): `migrate_database_in_place` (全書換) は模型スキーマで再作成→データコピーするため、修復前の重複を含む旧 DB のコピーが unique 違反で起動不能になる。index は ensure 関数だけが作る (models.py にコメントで参照を残す)。全書換で index が消えても次回 ensure が再作成。
- **move_entity の CAS** (台帳経路・legacy 経路とも):
  - AI: active 行を **AIID だけで** 引く。2+ 行 = 破損 → 修復要求メッセージで失敗 / 1 行で BUILDINGID ≠ from_id → CAS conflict で失敗 (無変異、現在地を返す) / 0 行 → WARN + 新行 insert で自己回復 (起動時生成漏れに寛容)。
  - user: User.CURRENT_BUILDINGID が非 NULL かつ ≠ from_id → CAS conflict で失敗。
  - 同時移動の insert 競合は unique index が拒否 → 既存の except 経路 (rollback + mark_failed + False)。

### D2. P1-1 残片 — 属性更新を移動 service へ集約

- `move_entity` の **commit 後・flush_pending 後** (= 現行の呼び出し側更新と同一タイミング) に canonical sync を実施:
  - AI: `manager.personas[entity_id]` に `current_building_id = to_id` → `_mark_entry(to_id)` → `_save_session_metadata()` (各段 guard、失敗は WARN — commit 済みなので False にしない)。
  - user: `manager.state.user_current_building_id = to_id`。
  - legacy 経路も同一 sync を末尾に。
- 呼び出し側 6 箇所 (summon / end_conversation / editor / tool / day_plan / move_user) から重複更新を撤去。
- **意図した挙動差** (儀式の統一): end_conversation にも `_mark_entry` が付く (cursor 会計の是正) / editor・tool 移動にも `_save_session_metadata` が付く。
- flush 中ハンドラの「属性は旧 Building」前提は不変 (sync は flush の後)。dynamic_state / day_plan の該当コメントを現況に更新。

### D3. P1-3 — chat 境界

- `/chat/send` (route): `req.building_id` が指定され、かつ `manager.user_current_building_id` と不一致なら **409** `{"code": "not_in_building"}` (utter への誘導メッセージ)。`/chat/utter` は移動後に send へ委譲するため常に一致。
- 多層防御: `runtime.handle_user_input_stream` にも同じ照合 (不一致 → error イベントで打ち切り、永続化なし)。HTTP を通らない呼び出し元 (gateway 等) も塞ぐ。
- heard_by の幽霊在室は境界強制で消滅 (send が現在地専用になるため)。
- utter の意味論を docstring で正直化 + 回帰固定: 入室 = `move.entity` 台帳実行として原子的 / 発言 = durable insert が認知の前提条件 / 「入室成功→insert 失敗」は retryable エラーで入室は残る (発言契機の入室は物理事実) / 再送は current==target で move スキップ + `client_message_id` 冪等で二重発言なし / 並行デバイスは CAS 409。**「move+message を単一 tx」へは進めない** (裁定): chat を移動 service の tx に結合する複雑化に対し、残る窓は「入室だけ成立し再送で発言が載る」自己回復型のみで、失敗は正直に報告される。

### D4. P1-6 — Region 入口の不変条件

- `update_region`: PARENT_REGION_ID 変更時、ENTRANCE_BUILDING_ID の Building の REGION_ID を新しい親スコープ (top 化なら None) へ**同一 tx** で同期。manager wrapper は `_reload_buildings()` も追加実行。
- `set_building_region`: 対象 Building がいずれかの Region の入口なら拒否 (入口の所属は Region service だけが変更する)。

### D5. P1-7 — Building の City 変更を immutable 化

- `update_building`: `building.CITYID != city_id` なら拒否。City 間移送は専用 migration command の領分とし、multi-city 凍結中は実装しない (凍結解除時に監査の修正方針を正典として設計)。

### D6. P2-1 — event key を採番 ID ベースへ

- 台帳経路: `event_key = occupancy:{entity_id}:{from}:{to}:{execution_id}` (台帳採番 = DB 採番の移動 ID)。legacy 経路: uuid4 hex。
- tx が原子的なので「再試行時の同一 key 再利用」は不要になった (失敗 tx はイベントを残さない)。

### D7. P2-2 — startup consistency checker

- `_load_occupancy_from_db` を分類型に:
  - **重複 active 行** → 起動時に明示 tx で修復 (canonical = ENTRY_TIMESTAMP 最新を残し他を close、migrate と同じ規則を共有ヘルパで) + startup_warnings + 修復内容の監査ログ。任意選択で稼働継続しない。
  - **IS_DISPATCHED なのに active 行** → warning 分類 (multi-city 凍結中は発生しない想定の異常として記録)。
  - **capacity 超過** → warning 分類 (自動退去はさせない — 世界への変更は可視の操作で)。
  - **AI/Building 不明** → 既存 warning を分類ラベル付きで維持。

## 実装チャンク (全チャンク実装済み 2026-07-21)

- A ✔ D1+D7 の DB 層 — `database/occupancy_repair.py` 新設 (`repair_duplicate_active_occupancy` + `ensure_active_occupancy_unique_index`、修復規則 = ENTRY_TIMESTAMP 最新・同時刻 ID 最大) / migrate.py `ensure_active_occupancy_unique` (修復→CREATE INDEX、失敗は WARN で起動継続) / main.py 毎起動結線 / models.py に不変条件コメント
- B ✔ D1+D2+D6 の move_entity 改修 — 台帳・legacy 両経路に CAS (AI = active 行を AIID だけで引く: 2+ 行 = 破損拒否 / 不一致 = stale from 拒否 / 0 行 = WARN + 自己回復 insert。user = CURRENT_BUILDINGID 照合) / `_sync_canonical_location` 新設 (commit + flush 後に属性 + `_mark_entry` + `_save_session_metadata` / user state を一元更新、失敗は WARN で移動成功のまま) / event_key を `occupancy:{id}:{from}:{to}:{execution_id}` (legacy = uuid4) に。呼び出し側 6 箇所 (summon / end_conversation / editor / tool / day_plan / move_user) の重複更新を撤去
- C ✔ D3 chat 境界 — `/chat/send` route の別室 409 (`not_in_building`) + `handle_user_input_stream` の多層防御 + utter docstring のコマンド意味論正直化
- D ✔ D4+D5 admin — `update_region` parent 変更時の入口 REGION_ID 同一 tx 同期 (入口行方不明は拒否) / `set_building_region` の入口拒否 / `update_building` の CITYID immutable 化 (旧「occupancy がある時だけ拒否」条件を置換) / manager wrapper の `_reload_buildings()` 追加 / frontend WorldEditor の都市セレクタを編集時 disabled
- E ✔ D7 startup checker — `_load_occupancy_from_db` を分類型に (重複 = 共有ヘルパで明示 tx 修復 + `occupancy_repair` warning / dispatched active 行 = 分類 warning / capacity 超過 = 分類 warning・自動退去なし)
- F: 回帰テスト (test_location_occupancy_w7.py 15 件 + test_chat_boundary_w7.py 8 件 + test_region_admin.py に 7 件追加) + スタブ 6 箇所 (test_day_plan / test_day_scenario / test_budget_gate / test_life_phase2 / test_track_slot_ref / run_day_sim) を新契約 (stub も成功時に属性 sync) へ更新 + docs 同期 (region.md / building_memory_unified.md / concepts/building-city.md)

## Codex レビュー 1 巡 2 件消し込み (受諾 2 / 却下 0、2026-07-21)

| # | 指摘 | 裏取り | 修正 |
|---|---|---|---|
| P1 | user 位置の CAS が read-then-write (並行 2 移動が両方旧値を読んで両方 commit できる) | 確認 — pysqlite は SELECT を autocommit で実行するため、事前検査はトランザクション外。書き込み時点の仲裁が無かった | `_cas_update_user_location` (条件付き UPDATE `WHERE CURRENT_BUILDINGID = from OR IS NULL` の rowcount で勝敗確定)。**同型の防御を AI 行の close にも展開** (`_close_active_row_cas` = `WHERE EXIT_TIMESTAMP IS NULL` 条件付き UPDATE — unique index 不在の縮退環境でも二重 presence を塞ぐ)。台帳・legacy 両経路 |
| P2 | CAS 競合が普通の `False`+文字列で返り、route 層が 409 (クライアント再同期) に変換できない | 確認 — `/chat/utter` は 400 move_failed、`/user/move` は 200 payload に落ちていた | `MoveDenialMessage` (str 派生 + `code` 属性、呼び出し側の `ok, msg` 契約は不変) を新設し CAS 競合に `code="cas_conflict"` を付与。`/user/move`・`/chat/utter` がクライアント CAS と同一の 409 形式へ変換 |

回帰追加: 仲裁ヘルパ 2 件 (close 済み行で負ける / user 位置不一致で負ける) + code 伝播 1 件 + route 変換 3 件。

## Codex レビュー 2 巡目 3 件消し込み (受諾 3 / 却下 0、2026-07-21)

| # | 指摘 | 裏取り | 修正 |
|---|---|---|---|
| P1 | canonical sync が outbox 配送の**後** — 配送が pending/遅いハンドラで止まる間、並行スレッド (chat 境界照合・スケジューラ) が旧所在地を見る | 確認 — 「現行の呼び出し側更新と同一タイミング」の保守的選択が、W7 で境界照合が state に依存するようになった後は公開遅延の欠陥になる | `_sync_canonical_location` を occupants 更新直後 (配送前) へ移動 (台帳・legacy 両経路)。配送ハンドラは payload の to_id で動くため公開順に依存しない (W5 検証済み)。dynamic_state の「属性はまだ旧」docstring を現況に更新 |
| P2 | index 作成失敗を握って続行 + 「active 行ゼロ → 素の INSERT」の自己回復経路に仲裁がない → index 不在で並行 2 移動が二重 active 行を作れる | 確認 — 自己回復経路は close の条件付き UPDATE を通らない | 新 active 行の INSERT を guarded INSERT (`WHERE NOT EXISTS (active 行)`) に (`_insert_active_row_cas`、全経路)。書き込み時仲裁が index 非依存になり、index は防御の二重化 + 外部書き込みへの最終防衛に位置づけ直し (migrate コメント同期)。起動 fail-closed 化は不採用 (仲裁が成立した今、起動不能の実害の方が大きい) |
| P2 | `create_region` が既に他 Region の入口である Building を入口に流用でき、親変更時に他方の「入口は親スコープ」不変条件を壊す | 確認 — 所有一意性の検査なし | create_region で共有入口を拒否 + update_region の親変更もレガシー共有を検出して拒否 (解消してから変更) |

回帰追加: guarded INSERT 2 面 + 公開順 (配送時点で state/属性が新所在地) 2 面 + 入口共有拒否 2 件。

## Codex レビュー 3 巡目 3 件消し込み (受諾 3 / 却下 0、2026-07-21)

| # | 指摘 | 裏取り | 修正 |
|---|---|---|---|
| P2 | `/chat/send` の境界照合が `manager.user_current_building_id` (遅延 mirror) を読む — 移動確定〜wrapper 戻りの間に来た発言を誤って 409 にする / 旧 Building へ添付を保存しうる | 確認 — mirror は `_refresh_user_state_cache` 更新で、canonical は `manager.state` | 境界照合を `manager.state.user_current_building_id` に変更 |
| P2 | CAS 409 の `current_building_id` が in-memory 由来で「勝者 commit 後・sync 前」の窓に stale — クライアントが誤った同期先を受け取り再衝突 | 確認 | `MoveDenialMessage` に `current_building_id` (拒否時点の DB 確定値、仲裁負けは rollback 後再読) を追加し、`/user/move`・`/chat/utter` の 409 がそれを優先 |
| P2 | 入口所有一意性が read-before-write 検査のみ — 並行 create_region が同じ入口で両方 commit できる | 確認 | 部分一意 index `uq_region_entrance_building` (`WHERE ENTRANCE_BUILDING_ID IS NOT NULL`) を起動時 ensure で作成。レガシー共有は**自動修復せず** WARN 可視化 (所有の選択は人間の判断)・index なしで続行 (admin 検査が防ぐ)。metadata 外の理由は occupancy index と同じ |

回帰追加: mirror 不使用 2 面 / 409 の確定値優先 2 件 / 入口 index 2 件。

## Codex レビュー 4 巡目 3 件消し込み (受諾 3 / 却下 0、2026-07-21)

| # | 指摘 | 裏取り | 修正 |
|---|---|---|---|
| P2 | 参照先不明の active 行 (AI/Building 削除済み・別 City) を警告だけで残すと、CAS が常に stale 判定 + 一意 index が自己回復 INSERT も塞ぎ、当該ペルソナが**移動不能**になる | 確認 — CAS 強化の副作用として新たに成立するロック状態 | startup checker が実体不在の行を close (分類 4)。AI 行が実在する「ロード失敗」は位置を壊さないよう据え置き警告 (破壊的修復は実体不在に限定) |
| P2 | main.py の起動前修復が後段 checker より先に重複を直すため、「行を自動選択して閉じた」事実が startup_warnings (UI) に現れない | 確認 | `occupancy_repair` に同一プロセス内ステージング (`record/consume_startup_repairs`) を追加し、checker が pre-start 修復明細を監査記録へ引き継ぐ |
| P2 | 添付 (Item 配置 = building へ永続化) が境界照合の後・runtime 最終照合の前に処理されるため、処理中 (概要生成の同期実行で数秒) の別デバイス移動で「旧 Building に孤立した添付」だけが残る | 確認 | 添付処理後に現在地を**再照合** — 競合していたら作成済み Item を片付けて (`delete_item` best-effort) 409。runtime 層の最終照合は defense として存置 |

回帰追加: 無効行 close + ロード失敗据え置き + pre-start 引き継ぎ + 添付競合の片付け = 4 件。

## Codex レビュー 5 巡目 3 件消し込み (受諾 3 / 却下 0、2026-07-21)

| # | 指摘 | 裏取り | 修正 |
|---|---|---|---|
| P2 | runtime の境界拒否が NDJSON ストリームへ生 HTML を返す — フロントの JSON.parse で破棄され、ユーザーにエラーが見えない | 確認 (既存の空メッセージ経路の様式を踏襲したのが誤り — そちらは utter からは実質到達しない縮退経路) | JSON エラーイベント (`error_code: not_in_building` + `current_building_id`) に変更 |
| P2 | route 照合通過 → 遅延 generator 開始の間にも競合窓があり、runtime は発言を拒否するが Item は片付けられない | 確認 | generator 内 (runtime 呼び出し直前) にも最終照合 + cleanup + JSON エラーを追加。残る窓は「generator 照合通過 → runtime 照合」の間のみ (発言は拒否される・Item のみ残る、極小) |
| P2 | cleanup 分岐の `logging.warning` が UnboundLocalError — 同関数の後続 `import logging` により `logging` が関数全体でローカル扱いになり、409 が 500 に化ける | 確認 (Python のスコープ規則による実バグ) | send_message 内の関数ローカル `import logging` / `import json` / `StreamingResponse` を撤去し module import に一本化 (コメントで再発防止を明記) |

回帰追加: JSON イベント化 1 件 (既存を強化) / generator 最終照合 + cleanup 1 件 / cleanup 例外でも 409 維持 1 件。

## Codex レビュー 6 巡目 3 件消し込み (受諾 3 / 却下 0、2026-07-21)

| # | 指摘 | 裏取り | 修正 |
|---|---|---|---|
| P1 | 発言境界の照合 (in-memory) と永続化が原子的でない — 照合後・`_persist_user_utterance` 前に別デバイスの移動が確定すると、旧 Building へ発言が永続化されその部屋の Pulse まで起動する | 確認 — backend_worker は別スレッドで、照合は state 読みのみ | `insert_building_message_with_location_guard` 新設 (database/building_messages.py): **無変化 UPDATE で write ロックを先取り** → User.CURRENT_BUILDINGID を検証 → INSERT → commit を単一 tx に (SQLite 単一書き手直列化で検証〜commit に移動が割り込めない)。競合は `_location_conflict` dict (確定現在地つき・何も書かない) で返り、stream は `not_in_building` エラーイベント / 非 stream は拒否文を返す。user 行が引けない環境 (テストスタブ) は fail-open |
| P2 | runtime 拒否経路では route が Item を片付けない — generator 照合通過後の競合で拒否発言の添付 Item が旧 Building に残る | 確認 | route の chunk 転送ループが `error_code == "not_in_building"` イベント (JSON parse で確認、部分一致誤爆なし) を検知して created_item_ids を cleanup |
| P2 | ストリーム競合エラー (HTTP 200 + NDJSON error) をフロントが現在地同期に使わない — UI が移動前の現在地を保持し次回発言が余分な CAS 競合に | 確認 (page.tsx の error ハンドラは content 表示のみ) | error イベントの `current_building_id` で `updateServerBuildingId` を呼ぶ |

回帰追加: guard の実 DB 検証 (一致=保存 / 移動済み=無書き込み+競合 dict) / runtime 競合イベント化 / route cleanup = 4 件。既存の永続化テスト 5+2 件は patch 対象を guard 関数へ更新。**作業事故 1 件**: PowerShell 一括置換で test ファイルの日本語がエンコーディング化け → git 復元 + Edit ツールで再適用 (教訓: 日本語を含むファイルの一括置換に PS の Get-Content/-replace を使わない)。

## Codex レビュー 7 巡目 2 件消し込み (受諾 2 / 却下 0、2026-07-21)

| # | 指摘 | 裏取り | 修正 |
|---|---|---|---|
| P1 | 重複修復の canonical 選択が「最新」のみで参照整合性を見ない — 最新行が削除済み/別 City の Building を指すと有効な旧行を close し、後段の checker (分類 4) が無効行も close して**所在地が全喪失**する | 確認 — 第四巡の分類 4 追加によって新たに成立する複合ケース | canonical 選択を「参照整合な行 (Building 実在 + City 一致) 優先 → その中で最新」に (`ORDER BY ref_valid DESC, ENTRY_TIMESTAMP DESC`)。全行無効なら従来どおり最新 (checker が close して自己回復 INSERT に委ねる) |
| P2 | `delete_item` は失敗を例外でなく "Error: ..." 文字列で返す契約なのに、cleanup 3 分岐が戻り値未検査 — cleanup 失敗が成功として扱われる | 確認 (AdminService.delete_item の契約) | `_cleanup_attachment_items` 共通ヘルパ (例外 + Error 文字列の両方を検査して WARNING 記録) に 3 分岐を集約 |

回帰追加: 参照整合優先の修復 1 件 + Error 文字列検査 1 件。

## Codex レビュー 8 巡目 1 件消し込み (受諾 1 / 却下 0、2026-07-21)

| # | 指摘 | 裏取り | 修正 |
|---|---|---|---|
| P1 | 拒否した添付の cleanup が Item/ItemLocation 止まり — `create_*_item_for_user` が書いた「User uploaded ...」host 履歴が削除済み Item を指したまま残り、保存ファイルも孤児化 | 確認 (items.py L1067-1072 の `_append_building_history_note` / delete_item は DB 行のみ) | **履歴 = 追記で補償** (削除口を新設せず、同じ host note 機構で「Upload of X was withdrawn」の補記を追加 — 旧 Building の occupants は「アップロード→撤去」を一貫した出来事として知覚)。cleanup を `(item_id, filename)` ベースに拡張。**ファイルは削除しない** (裁定: URI モードの動画は再送が同じ保存ファイルを参照するため、消すと正当な再送導線が壊れる。data モードの残置ファイルは稀な競合時の小さな残差として引き受ける — 下記残差欄) |

回帰追加: 撤去補記 note 1 件 + 失敗 Item は補記対象外 1 件。

## Codex レビュー 9 巡目 = 指摘なし (収束、2026-07-21)

計 9 巡 18 件消し込み (受諾 18 / 却下 0)。指摘の重心は P1 主要欠陥 (read-then-write CAS / 発言永続化の原子性) → 回復経路の複合ケース → 残差の縁 (cleanup の完全性) へと単調に縮小して収束した。

**教訓 (次 wave へ)**: (1) 「CAS」と呼ぶ検査は**書き込み時の仲裁** (条件付き UPDATE の rowcount / guarded INSERT) になっているかを最初から問う — pysqlite は SELECT を autocommit で実行するため、事前検査は常にトランザクション外にある。W2 の教訓「CAS の比較列と書き込み列のズレ」の変種で、今回は「比較の時点と書き込みの時点のズレ」だった。 (2) 副作用が複数層 (DB 行 + host 履歴 + ファイル + in-memory) にまたがる操作の拒否経路は、**全層の棚卸し表**を書いてから補償を設計する (Item だけ消して履歴が dangling、を 2 巡分けて指摘された)。 (3) 関数内の `import logging` は名前を関数全体でローカル化する — 早い分岐から参照する module はローカル import 禁止。

## 引き受ける残差 (意図的・記録)

- **utter の move+message 単一 tx 化はしない** (D3 裁定): 残る窓は「入室成功 → 発言 insert 失敗」のみで、入室は物理事実として残り、エラーは retryable で正直に返り、再送が `client_message_id` 冪等で一度だけ載る自己回復型。chat を移動 service の tx へ結合する複雑化に見合わない。
- **部分一意 index はモデル metadata 外** (D1 裁定): 全書換 migration の再構成コピーが修復前の重複データで詰まないように。index の真実は `database/occupancy_repair.py` + 起動時 ensure。
- **City 間の Building 移送コマンドは作らない**: multi-city 凍結スコープ。凍結解除時に分離監査の修正方針を正典として設計する。
- **capacity 超過・dispatched active 行の startup 自動修復はしない**: 世界への変更は可視の操作で行う (分類 warning のみ)。
- **`_load_occupancy_from_db` の user 復元は従来どおり** (User.CURRENT_BUILDINGID からの occupants 復元)。
- **位置競合で拒否された添付の保存ファイルは残る** (第八巡裁定): URI モード動画は再送が同じファイルを参照するため削除できない。data モードの残置は「複数デバイス + 添付処理中の移動」という稀な競合時のみ発生する小さなディスク残差で、Item・履歴の整合は補償済み (Item 削除 + 撤去補記)。
- **「generator 最終照合の通過 → runtime 永続化 tx」の間の移動**は runtime の tx 内検証が発言を拒否し (世界の整合は保たれる)、添付 Item は route の chunk 検知 cleanup が拾う。runtime 検証と添付作成を単一の受理境界に統合する再設計 (添付パイプラインの作成タイミング変更) は W7 の血管を越えるため行わない。

## 検収基準 (監査「必要な回帰」)

- P1-2: 同時移動・stale from・既存 duplicate 行・再起動のいずれでも二重 presence が成立しない。
- P1-3: raw send の別室拒否 / move 後 message insert 失敗で入室が残り再送が一度だけ載る / 同一 utter retry は duplicate_command / 並行デバイスは 409。
- P1-6: top↔sub 変換で入口が親スコープに追従 / 入口の detach・self・別 Region 割当の拒否。
- P1-7: user 在室・Region 所属・private room 参照を持つ Building の City 変更拒否 / 通常 field 更新は無影響。
- P1-1残: 全移動経路で persona 属性・cursor 儀式・user state が move_entity 内で一元更新。
- P2-1: 同一秒の A→B→A→B で 4 イベントの key が全て異なる。
- P2-2: 重複 active 行がある DB での起動が修復 + 監査ログ + 警告分類で立ち上がり、二重 presence にならない。
