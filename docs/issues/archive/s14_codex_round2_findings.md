# §14 実装 (92a7b86) への Codex 2巡目指摘 — 消し込み待ち

**状態**: 解決済み (2026-07-29 起票 / 2026-07-30 全指摘消し込み完了)。2巡目3件 + 4巡目3件 + 5巡目2件 + 6巡目2件を修正・裁定・分離起票で決着し、7巡目で Codex 承認 (approve)。関所閉鎖の slot 消費のみ別 issue (`work_session_gate_closed_consumes_slot.md`) へ分離。指摘1 = `_advance_anchor_preserving_folds` 専用経路 (仕分け基準は読み側 prune_folds と同じ「一部でも提示に残る範囲は残す」= fold 末尾 vs 新 anchor)。指摘3 = `run_cold_precompaction` が Beat ロックを取ってから `cold_precompaction_status` を最終判定 (関所閉鎖は "deferred")。指摘2 = fail-open で確定 (まはー裁定 2026-07-30、§14-6 の 9)。回帰テスト2本追加、intent §14-6 に 7〜9 として明記。残 = フルスイート → Codex 再レビュー → コミット → archive。**1巡目 (`review-ms60fy6w-x22vqr`) の5指摘は並走 pytest による stale スナップショット由来と裏取り済み — 再燃させないこと。**

## 指摘と triage (このセッションでの検討結果)

### 1. [high・正しい・要修正] 冷えた anchor の前進が後続の圧縮区間を全消去する

`resolve_metabolism_anchor` の機構1 は汎用 `upsert_anchor_entry` で anchor を書くため、anchor 変更時の自動クリア (session_lifecycle.py の FOLDED_RANGES_JSON クリア分岐) が発動する。最前線 = 「最初の未編纂メッセージ」なので、**その後方にまだウィンドウ内の編纂済み fold が存在しうる** (未編纂の隙間を跨いで先の episode が畳まれた形)。クリアすると生ログが復活してウィンドウが再膨張し、head の Chronicle 枠との二重提示も起こる。

**修正方針**: 前進時に fold を仕分ける — 新 anchor (最前線) 以降に**全体が**収まる fold は保持、手前の fold は捨てる。位置判定は `compare_message_positions` (fold の先頭 message vs 最前線)。anchor と FOLDED_RANGES_JSON を同一書き込みで更新する専用経路を作る (汎用 upsert の自動クリアに任せない)。回帰テスト: 未編纂の隙間より後方に fold がある状態で snap → fold が生き残ること。

### 2. [high・裁定済み: 変更不要] 非常畳みの失敗を無視してモデル呼び出しへ進む

`run_meta_user` は `maybe_run_emergency_precompaction` の戻り値 (failed/deferred/nothing) を捨てて通常の playbook へ進む。Codex は fail-closed (呼び出し中止) を推奨。

**裁定 (まはー 2026-07-30)**: 「失敗しても進む」で確定。高水位超過 ≠ モデル実上限超過 (高水位12万字は多くのモデルの実上限よりずっと下)。畳みに失敗しても呼び出しは成功しうるので、中止すると「成功したはずの応答」まで潰す。実上限を超えている場合は進んでも中止でも応答は失敗し、結果は同じ — 止める理由が弱い。§14-6 の 9 に明記済み。コード変更なし。

### 3. [medium・正しい・要修正] cold sweep の発火条件が Beat ロック取得前に陳腐化する

`run_cold_precompaction` の「全 anchor 冷え」判定はロック外。判定後〜`run_metabolism` のロック取得の間にユーザー Pulse が anchor を touch すると、**温まったばかりのキャッシュを畳みで壊す** (§14-4 の中心不変条件の破れ)。

**修正方針**: `run_cold_precompaction` 冒頭で `hold_beat` を自分で取り、**ロック内で** `cold_precompaction_status` を最終判定してから `run_metabolism` (RLock 再入で無害) を呼ぶ。競合テストを追加。

## 3巡目 (2026-07-30, 消し込みレビュー) の追加指摘

### 4. [high・正しい・修正済み] Beat 外の keepalive touch が anchor 前進を巻き戻す

keepalive (`run_cache_keepalive`) は設計上 Beat ロックの外を走る (記憶に書かない軽量呼び出し)。開始時に読んだ anchor を LLM 完了後に `touch_anchor_after_llm_call` へ渡すため、呼び出し中に anchor 前進 (§14-2 / 退場) が起きると、古い anchor の touch が後から届いて (a) anchor の巻き戻し (b) 汎用 upsert の列クリアで fold 消去 (c) updated_at=now で冷えた行の温かい偽装、の三つを一度に起こす。指摘1の修正 (fold 保持) も後勝ちで無効化される。

**修正**: touch を CAS に (`upsert_anchor_entry` の `require_current_anchor_id` — 行の現在の anchor が一致するときだけ書く条件付き UPDATE 1 文)。ズレた touch = 捨てられた提示ウィンドウのキャッシュの主張なので棄却が常に正しい。Codex 推奨の「共有ロックで直列化」は LLM 呼び出しを跨ぐ新ロックになるため不採用 (§14-6 の 10)。受容する残余 = 先回り畳みと keepalive 飛行中の交差で温め直しキャッシュ 1 回分が無駄になる (状態は無矛盾)。

## 4巡目 (2026-07-30, CAS 実装レビュー) の指摘と決着

1. **[high・正しい・修正済み] CAS 棄却後も見張り予約を上書きする** — `upsert_anchor_entry` / `update_anchor_for_model` が書き込み成否 (bool) を返すようにし、touch は棄却時に見張り予約 (schedule_cache_ttl_pulse) と token 判定へ進まない。stale 完了時刻での予約上書きは正当な予約を後ろへずらし、現行キャッシュの失効を招くため。
2. **[high・正しい・修正済み] 実行 model 切替の正当な touch を CAS が誤棄却** — 根は「CAS を全 touch に付けた」3巡目の私の設計が広すぎたこと。Beat ロックの外を走る touch は keepalive だけで、Beat 内の touch (会話 / fallback / sub-line) は前進と直列化済み。CAS を `only_if_anchor_unchanged=True` (keepalive 呼び出し面のみ) に絞り、Beat 内の cross-model anchor 確立 (usage.model 記帳、S1) は従来どおり通す。
3. **[medium・既存挙動・受容] 同一 anchor への並行 touch の TTL 延命計算の後勝ち** — read→計算→write の構造は §14 以前からの既存実装で、今回の変更で広がっていない。壊れるのは TTL の記録精度のみ (提示・記憶は無傷)、次の touch で自己回復。延命規則を SQL へ複製する修正は規則の二重実装になるため見送り (§14-6 の 10 に受容として明記)。

## 5巡目 (2026-07-30) の指摘と決着

1. **[high・正しい・修正済み] work_session の初回 LLM 呼び出しが Beat ロック外で touch も CAS なし** — `run_work_session` の hold_beat は act→observe ループだけを包んでいて、context 組成と初回 generate + touch がロックの前にあった。しかもコード内の「prefix は履歴なしだから anchor は None」というコメントは 2026-07-23 の倫理修正 (履歴なしで本人名義の行動を走らせない) 以前の陳腐化した記述で、実際は実 anchor が載る。§14-2 以降は組成自体が anchor 前進という書き込みを持つため、**hold_beat を組成の前へ広げて「組成〜初回〜ループ」を一つの直列域にした** (関所が初回 LLM 課金より先に来る利点も)。ロック順序テスト追加。
2. **[medium・正しい・修正済み] fold 保持前進の失敗が伝播されず「前進成功」として振る舞う** — `_advance_anchor_preserving_folds` が bool を返し、resolve は失敗時に旧 anchor へ留まる ("self" を返す = 次回再試行)。失敗を frontier として返すと、後続の touch が通常 upsert で frontier を書き、anchor 変更の列クリアで行に残った fold が全消えするため。失敗注入テスト追加。

## 6巡目 (2026-07-30) の指摘と決着

1. **[high・正しい・修正済み] keepalive の context 組成が Beat 外で anchor 前進 (行書き込み) を発火しうる** — 生存確認と組成の間に TTL 境界を跨ぐと、組成内の resolve が §14-2 前進を永続化する。ロック外の行書き込みは並走 Beat の前進・fold 更新と競合する。**keepalive の組成を「読みだけ」にした**: prepare_context に `persist_anchor_advance` を新設 (preview_only と違い自動想起の注入は通常どおり = 温める prefix は本物と一致)、keepalive は False で組み、組成に採用された anchor が生存確認の値とズレていたら LLM を呼ばず連鎖停止 (逸脱ガード)。keepalive は touch の CAS まで一貫して読みだけで進む。回帰テスト追加。
2. **[high・別 issue へ分離] 関所閉鎖 (BeatGateClosedError) が未実行の作業コマを消費する** — 「error → コマ消費」の精算は §14 以前からの挙動で、修正はデイプラン側の再試行設計 (slot 再武装・回数上限) を伴う。`docs/issues/work_session_gate_closed_consumes_slot.md` に起票し、まはーの裁定待ち。

## 消し込みの結末 (2026-07-30)

全巡の決着は上の各節のとおり。2巡目指摘1の仕分け基準は issue 起票時の「fold 先頭 vs 最前線」から「fold **末尾** vs 新 anchor」へ変更した — 読み側 `prune_folds` の「一部でも提示に残る範囲は残す」と揃えるため (先頭基準だと新 anchor を跨ぐ fold を捨てて生ログが復活する)。7巡目で Codex 承認 (approve・material findings なし)。回帰テストは test_session_anchor_rows (+7本) / test_work_session (+1本) / test_cache_keepalive (+1本)。
