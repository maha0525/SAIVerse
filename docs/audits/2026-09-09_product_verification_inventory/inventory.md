# 製品全体の検査対象 — 項目台帳 (inventory)

調査日: 2026-09-09 / 対象コミット: `7d7214be` (ブランチ `feature/chronicle-coverage-gaps`)
この文書の位置づけと全体の要約は [README.md](README.md) にある。接点は [connections.md](connections.md)、
棚卸し自体の網羅性の検算は [coverage.md](coverage.md)。

## これは何

SAIVerse が利用者とペルソナに提供している機能を、**個々の改修とは独立に**並べた台帳。
215 項目を 6 領域に分けて収めている。各項目は「誰がどこから何をするか」「何を受け取るか」
「その期待の根拠はどこにあるか」「コードを追って確認できた現在の挙動」「既存テストが何を確認しているか」
を分けて書いてある。

**現状の挙動を正解として書いてはいない。** 「今こう動く」と「こう動くべき」は別の欄にあり、
根拠が実装以外に見つからなかったものは `実装のみ (根拠なし)` と記してある。文書とコードが
食い違っていた箇所は、どちらが正しいかを断定せず両方を残した (各領域の「矛盾・疑義」節)。

## 読み方

### 項目 ID

領域ごとの接頭辞 + 連番。ID は安定させる約束で、後から項目を足すときは末尾に採番する
(既存の番号を詰め直さない)。

| 接頭辞 | 領域 | 件数 |
|---|---|---|
| `CHAT-nn` | A. 会話とコンテキスト | 32 |
| `MEM-nn` | B. 記憶 (Chronicle / Memopedia / スルース / 取り込み) | 41 |
| `WORLD-nn` | C. 世界 (City / Building / Item / 移動 / Observer / Phenomena) | 44 |
| `PERS-nn` | D. ペルソナと利用者の設定・導入導線 | 29 |
| `AUTO-nn` | E. 画面のない自動処理 | 34 |
| `OPS-nn` | F. 導入・運用・拡張・外部連携 | 35 |

### 「確認した」の 4 段階を潰さない

各項目末尾の **状態** 行は、次の 4 つを別々に記録している。ひとつの「確認済み」にまとめない。

| 欄 | 意味 |
|---|---|
| 機能の存在 | その入口が実在することを、コードまたは画面で確認した |
| 期待の根拠 | 「こうなるはず」の出所を特定した (下記の 4 種) |
| 挙動の静的確認 | コードを読んで、実際にどう動くかを追えた |
| テスト対応 | その結果を確認している既存テストを特定した |

`✓` = 確認できた / `△` = 部分的 (何が部分的かを一言添えている) / `✗` = できなかった。
**この調査ではテストを一度も実行していない。** テスト対応の `✓` は「そのテストが存在し、
何を通し何を差し替えているかを読んだ」という意味であって、「実行して合格した」ではない。

### 期待の根拠の 4 種

「こう動くべき」がどこから来ているかを、次の 4 つに分けて記している。混ぜると、実装の現状が
いつのまにか仕様に化ける。

1. **ユーザー原文** — まはー本人の言葉が残っているもの。最も強い根拠。
2. **既存の仕様文書** — `docs/intent/` などの設計文書。ただし「まはー承認済み」と書いてあるだけの場合は
   `設計書に承認の記載があるが原文未確認` と明記した。
3. **利用者向け説明** — README や `docs/user-guide/`。利用者に対する約束なので、実装がずれていれば
   どちらかが直る必要がある。
4. **実装のみ (根拠なし)** — コード以外に出所が無いもの。**この印が付いた数値や挙動は、
   まだ誰も「こうあるべき」と決めていない。** 特に閾値については、決め直す余地があるという意味になる。

## この台帳にまだ入っていない列 (次の一手)

調査の途中でまはーが示した方針 —「**確実に最初に隔離環境で検証を通しておいて、どうしても本番でしか
見れないものの数を減らす方が効く**」(2026-09-09、原文) — に対応する列が、まだ各項目に無い。
調査の指示を出した後に決まった方針なので、今回の 215 項目には反映されていない。

足すべき列は 2 つ:

- **隔離環境で確認できるか** (できる / 一部 / できない)
- **できないなら、何が隔離を妨げているか** — 実 API のレート制限 / データ規模 / 実機デバイス /
  課金 / 外部サービスの状態、など

この列が埋まると、台帳がそのまま「本番でしか見られないものを減らす」作業の対象リストになり、
減った証拠も同じ表で取れる。なお今回の事故 2 件は、**どちらも事後には隔離環境で再現できている**
(`tests/test_sluice_cold_isolation.py` は稟乃さんの形を 1/9 の縮尺で再現した)。だから問いは
「隔離できるか」ではなく「なぜ事前に隔離しなかったか」になる。

## 調査の条件と限界

- **静的な読み取りのみ。** pytest を含め、テストもスクリプトも一度も実行していない。
  「テストが存在する」と「実行して合格した」を分けてある。
- **本番ペルソナ・本番データに触れていない。** LLM・Pulse・Spell・スケジュールを起動していない。
  `~/.saiverse/` 配下は読み書きしていない。
- **UI はソースから調べた。** 画面を操作していないので、「画面にこう出る」はコードの読みであって
  実機の観察ではない。
- **`.worktrees/` 配下 (別セッションの作業ツリー) は対象外。**
- 作業ツリーは調査中も別セッションが動いている可能性がある。着手時と終了時の状態は README に記録した。

---

## 領域 A. 会話とコンテキスト (CHAT-01〜32)


### CHAT-01: メッセージを送って返事をもらう (ストリーミング会話の本線)

- **入口**: ユーザーがチャット欄に本文 (と添付) を入れて Ctrl+Enter または送信ボタン。フロントは `POST /api/chat/utter` を叩く (raw `/chat/send` はフロントからは使われない)。根拠: `frontend/src/app/page.tsx:2413` `handleSendMessage`、`frontend/src/app/page.tsx:2507` の fetch、`api/routes/chat.py:1367` `utter_message` → `api/routes/chat.py:962` `send_message`
- **結果**: 応答は `application/x-ndjson` のストリーミング。1 行 1 JSON イベント。実行経路は `manager.handle_user_input_stream` → 発言の durable insert → 在室ペルソナごとに `PulseDispatcher.dispatch_user_utterance` → `run_sea_user` → `PulseController` → `SEARuntime.run_meta_user`。永続化先は building_messages (DB) + 建物履歴 + 各ペルソナの SAIMemory。LLM 課金が発生する。根拠: `api/routes/chat.py:1136`、`manager/runtime.py:640`、`saiverse/saiverse_manager.py:698`、`sea/runtime.py:88`
- **NDJSON のイベント種別 (静的に列挙)**: `status` / `think` / `activity` / `auto_recall` / `streaming_thinking` / `streaming_chunk` / `streaming_discard` / `streaming_complete` / `say` / `error` / `warning` / `info` / `cancelled` / `duplicate_command` / `metabolism` / `user_message_id` / `permission_request` / `spell_confirmation` / `chronicle_confirm` / `speak_persisted` / `stelis_start` / `stelis_end` / `stelis_anchor` / `ping`。根拠: `manager/runtime.py:57-77` (`_OUTCOME_EVENT_TYPES`)、`sea/runtime_emitters.py:20`、`sea/runtime_llm.py:1779,1785,1914,1925,2071`、`sea/runtime_nodes.py:409,464`、`sea/session_lifecycle.py:5694`、`sea/coverage_repair.py:890`、`tools/confirmation.py:91`
  - サーバー側で「ユーザーに何かが届いた」と数える集合とフロントの鏡が**別ファイルに二重定義**されている。根拠: `manager/runtime.py:57` と `frontend/src/app/page.tsx:1621`。サーバー側のコメントが「鏡も同じコミットで揃えること」と明記している。
  - `streaming_complete` は「届いた」に数えない (画面に本文が出た証拠ではないため)。根拠: `manager/runtime.py:44-56`
- **期待の根拠**: `既存の仕様文書` — `docs/issues/user_utterance_path_failure_inventory.md` (出口 3/4/7 の設計) がコード内から繰り返し参照されている。加えて `docs/issues/archive/stream_completion_is_not_proof_of_persistence.md`。**原文未確認** (issue 本文は今回読んでいない)。UI 側の利用者向け説明は `docs/user-guide/chat-options.md` にストリーミングの記述なし。
- **現在の挙動 (静的確認)**:
  - 先頭で 2048 バイトのパディング付き `status` を流してヘッダを吐き出す。根拠: `api/routes/chat.py:1114`
  - キューが空でも 2 秒ごとに `{"type":"ping"}` を流す。根拠: `manager/runtime.py:929-931`
  - ジェネレータ本体が try/except で包まれ、ヘッダ送出後の例外も `error` イベントになる。根拠: `api/routes/chat.py:1112,1169`
  - 「発言は受け取ったのに何も画面に出ない」を **backend_worker の finally 一箇所**で検査し、応答者ゼロなら `no_responder`、それ以外は `no_response` を流す。根拠: `manager/runtime.py:878-914`
  - 在室ペルソナ**全員**が順に応答する (CHAT-32 参照)。
  - 発言本文の描画時、`<user_only>` タグは除去され中身は残る (フロント)。一方バックエンドは音声・他ペルソナ向けに中身ごと落とす経路を持つ。根拠: `frontend/src/lib/messageMarkdown.ts:24-26`、`saiverse/content_tags.py:30,47`
- **追跡できていない境界**: LLM クライアント (`llm_clients/`) の中でのストリーミング打ち切り・リトライの詳細。Playbook ノードの実行順 (LangGraph) の実挙動。Discord ゲートウェイ経由の会話。
- **既存テスト**: `tests/test_user_utterance_durability.py` — `RuntimeService` を実コードで通し、`manager` / `SessionLocal` / `pulse_dispatcher` / `personas` を MagicMock、`_persist_user_utterance` の DB は patch。イベント列を実際に組み立てて出口 3 の分岐を検証している。`tests/test_chat_boundary_w7.py` — `send_message` / `utter_message` を実関数で呼び、manager は SimpleNamespace + MagicMock。`tests/test_streaming_placeholder_salvage.py` — `sea/runtime_llm.py` の中断確定を、LLM クライアントを `_FakeStreamClient` に差し替えて検証。**フロントエンド側 (`consumeReplyStream` の全分岐) を通すテストは存在しない** (§5)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (バックエンドは厚い / フロントの読み手はゼロ)

---

### CHAT-02: 発言契機入室 (別の建物へ話しかけたら自動で移動する)

- **入口**: 画面上で別の建物を閲覧中に発言を送る。`POST /api/chat/utter` の `target_building_id` がサーバーの現在地と違えば、先に `move_user` を確定させてから発言処理へ入る。根拠: `api/routes/chat.py:1367-1435`、`frontend/src/app/page.tsx:2510`
- **結果**: 入室が台帳実行として確定 (`OccupancyManager.move_entity`)。世界状態が変わる (在室者・移動イベント・phenomena トリガー)。根拠: `manager/runtime.py:283-315`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/building_memory_unified.md` §C-2 / §B-1 / §B-2 (コード内から参照)。**原文未確認**。
- **現在の挙動 (静的確認)**:
  - クライアントが知っている現在地 (`expected_from_building_id`) がサーバーと食い違えば 409 `cas_conflict`。根拠: `api/routes/chat.py:1374-1390`
  - サーバー側 CAS 競合も 409 に畳む。根拠: `api/routes/chat.py:1401-1413`
  - 409 のときフロントは吹き出しを引っ込め、本文を入力欄に戻し、添付も戻す (ただし「入力欄に既に添付があるときは戻さない」)。根拠: `frontend/src/app/page.tsx:2536-2557`
  - raw `/chat/send` は現在地以外への発言を 409 で拒否する (単一位置モデルの迂回を塞ぐ)。根拠: `api/routes/chat.py:976-987`
  - 位置照合は route / 添付処理後 / ストリーム開始時 / runtime 層 / 永続化 tx 内 の**5 段**ある。根拠: `api/routes/chat.py:976, 1084, 1120`、`manager/runtime.py:673-692`、`manager/runtime.py:748-765`
- **追跡できていない境界**: `OccupancyManager.move_entity` の条件付き UPDATE の実挙動 (別領域)。
- **既存テスト**: `tests/test_chat_boundary_w7.py::test_send_rejects_non_current_building_with_409`、`::test_send_recheck_after_attachments_cleans_up_and_409s`、`::test_send_stream_start_recheck_cleans_up_late_conflict`、`::test_stream_refuses_non_current_building` ほか。`move_user` は MagicMock。**フロント側の 409 復旧 (本文と添付の戻し) を通すテストは無い**。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (サーバーのみ)

---

### CHAT-03: 二重送信の抑止 (client_message_id による冪等)

- **入口**: 送信操作 1 回につきフロントが UUID を 1 つ生成して送る。ネットワーク再送やユーザーの二度押しで同じ値が届いても、サーバーは 1 行しか insert しない。根拠: `frontend/src/app/page.tsx:2489-2502`、`api/routes/chat.py:430-434`
- **結果**: 重複と判定された回は `duplicate_command` イベントを流して**認知を起こさない** (LLM を呼ばない)。根拠: `manager/runtime.py:786-796`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/building_memory_unified.md` §B-2 (コード内参照)。**原文未確認**。
- **現在の挙動 (静的確認)**: `crypto.randomUUID()` が Secure Context 限定なので、非 secure context (Tailscale 越しの `http://100.x.x.x:3000` など) では `getRandomValues` で UUID v4 を手組みするフォールバックに落ちる。根拠: `frontend/src/app/page.tsx:2489-2502`
- **追跡できていない境界**: DB の UNIQUE 制約の定義そのもの (`database/building_messages.py`)。
- **既存テスト**: `tests/test_user_utterance_durability.py::test_duplicate_command_returns_canonical_id_without_redispatch` / `::test_new_durable_command_dispatches_once`。`_persist_user_utterance` は patch。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---

### CHAT-04: 生成の停止 (停止ボタン)

- **入口**: 生成中に停止ボタン。`POST /api/chat/stop`。根拠: `frontend/src/app/page.tsx:2782`、`api/routes/chat.py:908`
- **結果**: **ユーザーの現在建物にいる全ペルソナ**の走行中リクエストを `cancellation_token.cancel(interrupted_by="user_stop")` で取り消し、建物の `stop_event` を立てる。根拠: `saiverse/saiverse_manager.py:1307-1339`
  - 途中まで喋った発言は「途中で終わった」印 (`metadata["_interrupted"]`) 付きで確定し、続きの生成ボタンが出る (CHAT-05)。根拠: `sea/runtime_llm.py` `INTERRUPTED_METADATA_KEY`、`api/routes/chat.py:66`
- **期待の根拠**: `ユーザー原文` — コード内に「2026-08-27 まはー裁定」「2026-08-28 まはー裁定」の引用付き記載がある (本文が一字一句の引用ではないため `設計書に承認の記載があるが原文未確認`)。根拠: `frontend/src/app/page.tsx:2255-2261`、`frontend/src/app/page.tsx:2244-2249`
- **現在の挙動 (静的確認)**:
  - 本文が一文字も出ていない状態で止めた回は、フロントが吹き出しをその場で畳む (サーバー履歴に残らないため)。根拠: `frontend/src/app/page.tsx:2244-2249`
  - 本文が出る前に止めた回も「返事が来なかった」扱いで `needsRetry` を立てる。根拠: `frontend/src/app/page.tsx:2262-2264`
  - fetch は abort しない (バックエンドの取り消しフローに `streaming_complete` / `cancelled` を自然に流させるため)。根拠: `frontend/src/app/page.tsx:2783-2786`
  - **保険的処理**: 停止は建物単位なので、同じ建物で走っている**無関係な自律 Pulse も巻き添えで止まる**。これはコード内で自覚的に記述されている (「画面を閉じただけで無関係な自律行動まで巻き添えで止まる。しかも記録には『ユーザーが止めた』と残る」)。根拠: `manager/runtime.py:1114-1119`。**この巻き添えは画面に説明されない。**
- **追跡できていない境界**: `PulseController._current` と `CancellationToken` の伝播先 (LLM クライアントのストリーム切断まで)。
- **既存テスト**: `tests/test_streaming_placeholder_salvage.py::test_a_user_stop_writes_the_user_notice` / `::test_a_late_stop_still_reads_as_a_user_interruption` — `runtime_llm` の確定処理を実コードで通し、LLM クライアントと `_finalize_beat` を差し替え。`cancel_active_generation` そのものを通すテストは見つけていない。
- **状態**: `機能の存在=✓` `期待の根拠=△` (裁定の引用はあるが原文未確認) `挙動の静的確認=✓` `テスト対応=△`

---

### CHAT-05: 続きの生成 (途中で終わった発言の続き)

- **入口**: 「途中で終わった」印の付いたペルソナ発言に出る「続きの生成」ボタン。`POST /api/chat/continue`。根拠: `frontend/src/app/page.tsx:3468-3478`、`api/routes/chat.py:1192`
- **結果**: 発言を送り直さず、**応答だけ**を起こす。入力欄の席に `<system>` で包んだ機構の指示文を載せる (永続化されない)。LLM 課金が発生する。根拠: `manager/runtime.py:1126-1195`、`manager/runtime.py:1002-1012` (`CONTINUE_INSTRUCTION`)
- **期待の根拠**: `既存の仕様文書` + `ユーザー原文への言及` — 「追加の推論はすべてボタンの後ろに置く (2026-08-25 まはー裁定)」。`docs/issues/user_utterance_path_failure_inventory.md`。**原文未確認**。
- **現在の挙動 (静的確認)**:
  - 「印を降ろす権威」は保存成功が確定したワーカースレッド側にあり、読み手 (ブラウザ) が切断しても正しく降りる。根拠: `manager/runtime.py:1069-1084`、`manager/runtime.py:1168-1180`
  - 保存の信号 (`speak_persisted`) が来なかった回は印が残る = ボタンが出続ける側に倒す。根拠: `manager/runtime.py:1189-1195`
  - エラーの札は 5 種: `no_current_building` / `history_unavailable` / `message_not_found` / `not_interrupted` / `persona_not_found`。根拠: `manager/runtime.py:1128-1166`
  - 「記録に無い」と「履歴を読めなかった」を別の札にしている。根拠: `manager/runtime.py:1021-1046`
- **追跡できていない境界**: `history_manager.update_building_message` の実挙動。
- **既存テスト**: `tests/test_user_utterance_durability.py::test_continue_keeps_the_mark_when_nothing_was_spoken` / `::test_continue_clears_the_mark_once_the_persona_actually_spoke` / `::test_screen_events_alone_do_not_clear_the_interrupted_mark` / `::test_the_persistence_signal_clears_the_interrupted_mark` / `::test_continue_separates_an_unreadable_history_from_a_missing_message`。`history_manager` は MagicMock、`run_sea_user` は差し替え。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---

### CHAT-06: 応答のやり直し (再送)

- **入口**: 返事が来なかった自分の発言に出る「再送」ボタン。`POST /api/chat/retry`。根拠: `frontend/src/app/page.tsx:3481-3492`、`api/routes/chat.py:1212`
- **結果**: 発言は送り直さず、**最初の 1 体だけ**に応答をやり直させる。LLM 課金が発生する。根拠: `manager/runtime.py:1285-1290`
- **期待の根拠**: `既存の仕様文書` + 裁定への言及 — 「2026-08-29 まはー裁定」。`docs/issues/archive/retry_api_has_no_server_side_eligibility_check.md`。**原文未確認**。
- **現在の挙動 (静的確認)**:
  - **二重の門番**: (1) API route で「その発言より後にペルソナの発言があるか」を検査し、あれば 409 `already_replied`。(2) 生成直前の Beat ロックの内側で同じ共有判定をもう一度引く。根拠: `api/routes/chat.py:1227-1256`、`manager/runtime.py:1257-1283`、`sea/runtime.py:127-142`
  - 判定不能 (DB を読めない) は **fail-closed** で 503 を返す。根拠: `api/routes/chat.py:1242-1256`
  - `building_id` が無い回はここでは判定できず、直後のストリームが `no_current_building` で拒む。根拠: `api/routes/chat.py:1257-1259`
  - フロントは 409 `already_replied` のときボタンを戻さず、履歴を読み直す。根拠: `frontend/src/app/page.tsx:2694-2706`
  - 「やり直しても結果が変わらない」札 (`no_responder` / `already_replied`) はフロントで再送ボタンを落とす。根拠: `frontend/src/app/page.tsx:1601`
- **追跡できていない境界**: `database/building_messages.assistant_reply_exists_after` の SQL。
- **既存テスト**: `tests/test_user_utterance_durability.py::test_retry_does_not_ask_for_a_resend_when_the_history_cannot_be_read` / `::test_retry_still_reports_a_genuinely_missing_message` / `::test_retry_uses_the_same_label_for_the_same_fact`。`SessionLocal` は MagicMock。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---

### CHAT-07: 発言の取り消し (まだ読まれていない自分の発言)

- **入口**: 返事が来なかった自分の発言に出る「取り消す」ボタン。`POST /api/chat/withdraw`。根拠: `frontend/src/app/page.tsx:3495-3506`、`api/routes/chat.py:1287`
- **結果**: DB の行を消し、メモリ内の建物履歴からも落とし、本文を入力欄へ返す。LLM は呼ばない。根拠: `manager/runtime.py:1292-1341`
- **期待の根拠**: `ユーザー原文への言及` — 「読まれた後は取り消せない (2026-08-25 まはー裁定)」。**原文未確認**。
- **現在の挙動 (静的確認)**:
  - 取り消せるかは**ペルソナがもう読んだか**で決まる。理由は「聞いた覚えがあるのに記録が無い」状態を作らないため。根拠: `manager/runtime.py:1292-1299`
  - 断られる理由は 4 種 (`already_heard` / `not_found` / `wrong_role` / `unavailable`) で、各々に日本語の説明文がある。根拠: `manager/runtime.py:1327-1341`
  - 取り消せなかったときは `withdrawBlocked` を立てて「取り消す」だけを引っ込め、「再送」は残す。根拠: `frontend/src/app/page.tsx:2752-2760`
  - 二度押しは `withdrawingRef` で塞ぐ。根拠: `frontend/src/app/page.tsx:2734-2737`
- **追跡できていない境界**: `database/building_messages.withdraw_building_message_in_db` の判定 (「読んだ」の定義)。
- **既存テスト**: `manager/runtime.py` の `withdraw_user_message` を直接通すテストは特定できていない (`tests/test_building_messages_db.py` に DB 側があるかは未確認)。
- **状態**: `機能の存在=✓` `期待の根拠=△` (裁定の言及のみ) `挙動の静的確認=✓` `テスト対応=✗` (この経路のテストを見つけられていない)

---

### CHAT-08: 顛末不明の送信の復旧

- **入口**: 送信のストリームが「届いたか分からない」形で切れたとき、フロントが自動で `GET /api/chat/message-outcome?client_message_id=...` を叩く。根拠: `frontend/src/app/page.tsx:2296-2385`、`api/routes/chat.py:1313`
- **結果**: 三値 (`found` / `not_found` / `unknown`) が返る。読み取りのみで LLM は呼ばない。根拠: `api/routes/chat.py:1298-1330`
- **期待の根拠**: `既存の仕様文書` — `docs/issues/archive/unknown_send_outcome_has_no_recovery_path.md` (コード内参照)。**原文未確認**。
- **現在の挙動 (静的確認)**:
  - `found` かつ `has_reply` → 履歴を読み直して「通信は切れたが発言は届いていて応答も付いている」と案内。
  - `found` かつ応答なし → 出口 3 と同じ「再送」導線へ合流。
  - `not_found` → 吹き出しを引っ込めて本文と添付を入力欄へ返す。**添付は下書きと戻り分の両方を残し、id で重複を弾く**。根拠: `frontend/src/app/page.tsx:2354-2364`
  - `unknown` / 問い合わせ自体の失敗 → 「発言が届いたかどうかは分かりません」と正直に出す。根拠: `frontend/src/app/page.tsx:2379-2392`
  - 「読めなかった行が 1 行でもあれば」異常として catch へ落とす。根拠: `frontend/src/app/page.tsx:2274-2280`
  - 「結果イベントゼロの正常終了」も切断と断定する。根拠: `frontend/src/app/page.tsx:2294`
- **追跡できていない境界**: `database/building_messages.lookup_client_message_outcome`。
- **既存テスト**: **見つからない**。API 側 (`get_message_outcome`) もフロント側の復旧分岐も、通すテストを特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

---

### CHAT-09: 会話履歴の読み込み・遡り・追従ポーリング

- **入口**: 画面を開く / 上へスクロール / 5 秒ごとの自動ポーリング。`GET /api/chat/history`。根拠: `frontend/src/app/page.tsx:699` `fetchHistory`、`:947` `handleScroll`、`:1263` ポーリング、`api/routes/chat.py:271`
- **結果**: 建物のメッセージ 20 件ずつ (ポーリングは 50 件)。読み取りのみ。根拠: `frontend/src/app/page.tsx:712`、`:1279`
- **期待の根拠**: `実装のみ (根拠なし)` — `docs/user-guide/` に履歴表示の仕様記述は見当たらない。`docs/reference/api-endpoints.md:83` は説明文が空。
- **現在の挙動 (静的確認)**:
  - **本文が空のメッセージは履歴から落とされる**。根拠: `api/routes/chat.py:299-302`
  - message_id の無い旧メッセージには `md5(building:timestamp:role:content[:100])` の仮 ID を振る。根拠: `api/routes/chat.py:316-325`
  - `before` / `after` の ID が見つからなければ空を返す (クライアントは「これ以上ない」と解釈する)。根拠: `api/routes/chat.py:346-373`
  - 隔離された建物 (`quarantined_buildings`) は空履歴 + `quarantined: true` を返す。根拠: `api/routes/chat.py:289-291`
  - **`quarantined` フラグはフロントで使われていない** (`HistoryResponse` 型に無い)。根拠: `frontend/src/app/page.tsx:660-664`。→ §3 の矛盾に記載。
  - 500 以上のとき `backendConnected=false` にして再接続ポーリングへ委ねる。根拠: `frontend/src/app/page.tsx:798-801`
  - `syncAfterResponse` は **role + 本文の先頭 120 文字**でサーバー行とローカル行を突き合わせる。根拠: `frontend/src/app/page.tsx:891`
- **追跡できていない境界**: `manager.get_building_history` の実体 (in-memory と DB と log.json の関係)。
- **既存テスト**: `tests/test_game_session_log.py` が `serialize_history_message` を共用しているはず (未読)。`get_chat_history` のページングを通すテストは特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

---

### CHAT-10: 添付ファイル (画像・文書・音声・動画)

- **入口**: 「+」メニューからのファイル選択、またはドラッグ&ドロップ。根拠: `frontend/src/app/page.tsx:2862` `handleFileUpload`、`:2976` `handleDrop`
- **結果**: `~/.saiverse/{image,documents,audio,video}/` にファイルを保存し、対応する Item を建物に作る (文書・音声・動画は `is_open=True` で視界に入る)。LLM への概要生成が走る場合がある (課金)。根拠: `api/routes/chat.py:483-906`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/multimodal_input_pipeline.md` (未読)。利用者向けは `docs/user-guide/items-and-files.md` (未読)。
- **現在の挙動 (静的確認)**:
  - **保険的処理 / 見えている**: 動画は 90 秒、音声は 300 秒で ffmpeg 正規化時に打ち切られる (超過は `RuntimeError` → 400 で「詳細つき」のエラーになる)。根拠: `api/routes/chat.py:671, 747`。ただし**上限値は UI に表示されない** (エラーが出て初めて分かる)。
  - 大きい動画は base64 を避けて `POST /api/media/upload-video` へ multipart で先に上げ、`saiverse://video/...` の URI で参照する。根拠: `api/routes/chat.py:790-846`、`frontend/src/app/page.tsx:2808`
  - PDF はテキスト抽出 (先頭 5 ページ)。失敗しても「(PDF text extraction failed)」で続行する。根拠: `api/routes/chat.py:585-595`
  - 文書の要約は先頭 200 文字を切り出すだけ (LLM 不使用)。根拠: `api/routes/chat.py:602-605`
  - 画像・音声・動画の**内容の概要生成**は既定でバックグラウンド。グローバル設定「添付したメディアの内容を自動想起に使う」が ON のときだけ同期実行され、送信が数秒待たされる。根拠: `api/routes/chat.py:1008-1011, 529-558`
  - 位置競合で発言が拒否されたとき、作成済み Item を削除し、建物履歴に**撤去の補記**を追記する (元の「User uploaded ...」の記録は消さない)。保存済みファイル自体は消さない。根拠: `api/routes/chat.py:915-959`
  - `delete_item` は失敗を例外でなく `"Error: ..."` 文字列で返す契約なので、戻り値検査をしている。根拠: `api/routes/chat.py:939-944`
  - 添付処理の例外は `RuntimeError` 以外だと `None` を返して**黙って落ちる** (その添付は無かったことになり、ユーザーに通知されない)。根拠: `api/routes/chat.py:904-906`
- **追跡できていない境界**: `saiverse/ffmpeg_runner.py` の実挙動、`saiverse/media_summary.py` の LLM 呼び出し、`api/routes/media.py`。
- **既存テスト**: `tests/test_attachment_paths.py` (未読)。`tests/test_chat_boundary_w7.py` の cleanup 系 3 本が `_cleanup_attachment_items` を実コードで通し `delete_item` を MagicMock。
- **状態**: `機能の存在=✓` `期待の根拠=△` `挙動の静的確認=✓` `テスト対応=△`

---

### CHAT-11: コンテキストプレビュー (送る前に中身と費用を見る)

- **入口**: 「+」メニューのコンテキストプレビュー。`POST /api/chat/preview`。根拠: `frontend/src/app/page.tsx:2907`、`api/routes/chat.py:1448`
- **結果**: 応答可能な各ペルソナについて、節ごとのトークン内訳・推定入力トークン・推定費用 (キャッシュ全ヒット時〜全書き込み時の幅)・各メッセージ本文を返す。LLM は呼ばず、履歴にも書かない。根拠: `sea/runtime_context.py:1914-1927`
- **期待の根拠**: `既存の仕様文書` — 「プレビューが嘘にならないの原則」が `api/routes/people/context_status.py:5-8` に明記されている。利用者向け説明 (`docs/user-guide/chat-options.md`) にはプレビューの記述なし。
- **現在の挙動 (静的確認)**:
  - 節は `system_prompt` / `memory_weave_chronicle` / `memory_weave_memopedia` / `memory_weave` / `visual_context` / `history` / `realtime_context` / `perception_buffer` / `user_message` / `attachments`。根拠: `sea/runtime_context.py:1958-1974`
  - プレビューでも §15 読み戻しを反映する (素の窓のままだと「話しかけた時に実際に見える窓」より薄い嘘になるため)。読み戻しを反映するときは head のあらすじ枠も組み直し、組み直しに失敗したら読み戻しごと見送って素の窓に落とす。根拠: `sea/runtime_context.py:460-495`
  - 知覚の下ろし境界は進めない (`advance_cutoff=not preview_only`)。根拠: `sea/runtime_context.py:595-598`
  - 自動想起の注入はプレビューでは走らない。根拠: `sea/runtime_context.py:690-699`
  - 提示済みの知覚 / 部屋の様子にはバッジが付く。根拠: `frontend/src/components/ContextPreviewModal.tsx:22-25, 114-119`
  - **プレビューは `persona.model` を使う**が、実際の Pulse は `resolve_execution_context(...).model_key` を使う。根拠: `sea/runtime_context.py:1955` vs `sea/runtime.py:238-245`
  - **プレビューは非常畳み (`maybe_run_emergency_precompaction`) を通らない**。実送信は LLM 呼び出しの直前にそれを走らせる。根拠: `sea/runtime.py:396-403` (実送信側) / `sea/runtime_context.py:1943-1947` (プレビュー側に相当処理なし)。→ §3 の疑義に記載。
  - **保険的処理 / 見えていない**: バックエンドはペルソナごとのプレビュー失敗を握って `logging.exception` するだけで、そのペルソナは結果から黙って消える。根拠: `manager/runtime.py:1367-1370`
  - **保険的処理 / 誤誘導**: フロントは fetch 失敗時に `{personas: []}` を入れるため、画面には「このビルディングに応答可能なペルソナがいません。」と出る (実際は通信/サーバー失敗)。根拠: `frontend/src/app/page.tsx:2940-2943` と `frontend/src/components/ContextPreviewModal.tsx:238-240`。→ §3 に記載。
  - タブ番号は開くたびに先頭へ戻す (前回の部屋のタブ番号が残って真っ黒になる不具合の対処、2026-09-07 実機で発覚と注記)。根拠: `frontend/src/components/ContextPreviewModal.tsx:210-215`
- **追跡できていない境界**: `saiverse/token_estimator.py` の推定精度、費用計算の pricing 解決。
- **既存テスト**: `tests/test_preview_perception_badges.py` (バッジの分類)。`tests/test_payload_context_filter.py` (未読)。**プレビューと実送信の一致を突き合わせるテストは見つけていない**。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

---

### CHAT-12: データ送信量の管理 (水位・現在量・畳み可否) ★数値の契約

- **入口**: チャットオプション →「データ送信量の管理」を開く。ペルソナを選ぶと `GET /api/people/{id}/context-status` を 1 回だけ叩く (ポーリングしない)。根拠: `frontend/src/components/ChatOptions.tsx:172-193`
- **結果**: 読み取り専用。行は一切書かない (`persist_advance=False` / `advance_cutoff=False`)。根拠: `api/routes/people/context_status.py:60-66`
- **期待の根拠**: `既存の仕様文書` — `docs/issues/chat_options_metabolism_section_redesign.md`、`docs/issues/context_accounting_excludes_injected_rows.md`、`docs/issues/watermarks_unsatisfiable_when_perception_is_large.md`、`docs/intent/chronicle_eviction.md` §4。裁定日は 2026-09-02 / 09-03 / 09-04 / 09-05。**原文未確認** (コード内に裁定番号つきで引用されている)。
- **数値の契約 (何の量か / 誰に何を約束しているか)**:

  | 名前 | UI 表示 | 主語 (何の量か) | 約束 | 組み込み既定 |
  |---|---|---|---|---|
  | `high_chars` | 「上限」 | **送る合計** (会話の行 + 機構名義の行 + 送信直前に差し込む知覚) | これを超えたら整理 (Metabolism) が走る | 120,000 字 |
  | `target_chars` | 「残す量」 | **会話の行だけ** (機構名義の行も知覚も数えない) | 整理後にここへ揃える到達点 = 保護範囲。会話の起点が無いときの初期読み込み量も兼ねる | 40,000 字 |
  | `perception_high_chars` | UI 非表示 | 提示中の知覚ブロックの合計 | 超えたら下ろしが起きる | 60,000 字 |
  | `perception_target_chars` | UI 非表示 | 同上 | 一度の下ろしでここまでまとめて下ろす | 20,000 字 (定数名は TARGET だがコメントは「4 万へ広げた」と書いてある → §3 に記載) |
  | `WATERMARK_HEADROOM_CHARS` | UI 非表示 | 保存時検査の余裕 | 「上限 − 残す量 > 知覚の上限 + 余裕」を保存時に検査する | 10,000 字 |
  | `fold_unit_chars` (U) | 説明文中に字数で表示 | 一次あらすじの標準被覆 = **材料字数** (機構の長い行を圧縮した後の字数) | 整理は残す量より古い側を U ずつ刻んで畳む | env `SAIVERSE_CHRONICLE_BAND_BUDGET` 由来 |

  根拠: `saiverse/model_configs.py:203-262`、`api/routes/people/context_status.py:37-100`、`frontend/src/components/common/ContextVolumeBar.tsx:8-40`
- **現在の挙動 (静的確認)**:
  - `presented_chars` は**合計**で、内訳は `stored_chars` (会話) / `mechanism_chars` (スペル結果などの機構名義の行、生) / `injected_perception_chars` (部屋の様子) の三分割。根拠: `api/routes/people/context_status.py:206-256`
  - 「残す量」と比べる量は `window_rows_chars` (計画窓の会話の行だけ) で、`stored_chars` とは別物。UI は色分けにこの二つを使い分ける。根拠: `api/routes/people/context_status.py:299-301`、`frontend/src/components/common/ContextVolumeBar.tsx:76-79, 107-113`
  - 内訳の表示には `stored_chars` を使い、`window_rows_chars` は使わない (「うち」の足し算が合計と合わなくなるため)。根拠: `frontend/src/components/common/ContextVolumeBar.tsx:85-99`
  - `fold_ready` / `fold_shortfall_chars` は実行時と同じ純関数 `plan_eviction` を dry に呼んで出す (画面側で算数を再実装しない)。根拠: `api/routes/people/context_status.py:259-303`
  - `perception_over_budget` (会話は残す量以下なのに合計が上限超え = 畳めるものが無い) を旗として返し、UI に一文で出す。根拠: `api/routes/people/context_status.py:305-310`、`frontend/src/components/common/ContextVolumeBar.tsx:130-136`
  - 水位の解決に失敗したら 500 (「水位を持たないモデル」に偽装しない)。計測の失敗は `measurement_failed` で区別する (「起点なし」に潰さない)。根拠: `api/routes/people/context_status.py:127-141, 190-199`
  - 水位の逆転 (残す量 > 上限) は警告ログを出して `high = target` に丸める。根拠: `sea/session_lifecycle.py:336-356`。**この丸めは画面に出ない。**
  - **UI 表示と実装の単位は一致している** (どちらも文字数、上限=合計・残す量=会話のみ)。UI の説明文が主語まで書いている。根拠: `frontend/src/components/ChatOptions.tsx:647-655`
  - 水位の設定はモデル編集画面へ誘導する (チャットオプションからは変えられない。2026-07-30 にグローバル上書きを廃止)。根拠: `frontend/src/components/ChatOptions.tsx:654`、`frontend/src/components/ChatOptions.tsx:81-82`
- **追跡できていない境界**: `sea/eviction_plan.py:plan_eviction` の内部 (832 行、目次のみ確認)。`preview_planning_window` / `presented_with_perceptions` の実装。
- **既存テスト**: `tests/test_context_status.py` — 27 本。`get_context_status` を実関数で呼び、`lifecycle` を SimpleNamespace で差し替えて `plan_eviction` は実物を通す。内訳の三分割・`perception_over_budget` が同じ窓から取られること・計測失敗と起点なしの区別・500 を固定している。`tests/test_metabolism_global_defaults.py` (41 本) が三層解決を、`tests/test_watermark_headroom_validation.py` (16 本) が保存時検査を、`tests/test_eviction_plan.py` が計画の純関数を実コードで通す。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓` (バックエンド。UI 表示の一致は目視のみ)

---

### CHAT-13: キャッシュタイマーとペルソナ単位のキャッシュ設定

- **入口**: チャットオプション →「キャッシュ（このペルソナ）」。`GET /api/people/{id}/cache-status` を **2 秒ごとにポーリング**し、セレクタ変更で `POST /api/people/{id}/cache-config`。根拠: `frontend/src/components/ChatOptions.tsx:133-158, 487-500`
- **結果**: 残り時間の横棒と「残り mm:ss」。設定は `off` / `5m` / `1h` の 3 択で、**in-memory・非永続** (再起動で消える)。根拠: `api/routes/people/cache_status.py:110-124`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/cache_lifecycle_control.md` §4.6 / §7 Phase 1 (コード内参照)。**原文未確認**。利用者向けは `docs/user-guide/chat-options.md` に「プロンプトキャッシュの設定。キャッシュ TTL の上書きなど」の 1 行のみ。
- **現在の挙動 (静的確認)**:
  - `supported=true` は **Anthropic の explicit cache のみ**。Gemini/OpenAI の implicit は Phase 3 未対応で `supported=false`。根拠: `api/routes/people/cache_status.py:76-82`
  - 起点は `session_anchor` 行の `updated_at` (LLM 呼び出し成功時にのみ touch される)。根拠: `api/routes/people/cache_status.py:127-160`
  - 書き込み時に記録した `ttl_seconds` を優先する (設定を変えるたび既存キャッシュの残り時間表示が遡及的に変わるバグの対処、「まはー報告のバグ」と注記)。根拠: `api/routes/people/cache_status.py:152-158`
  - 累計ヒット/節約額は載せない (Usage ページの領分)。根拠: `api/routes/people/cache_status.py:5-7`
- **追跡できていない境界**: `SessionLifecycle.load_anchors` / `get_anchor_validity_seconds`。実際のキャッシュ書き込みが Anthropic API 側で成立しているか。
- **既存テスト**: `tests/test_cache_lifecycle.py` / `tests/test_cache_keepalive.py` / `tests/test_session_anchor_rows.py` (いずれも未読)。`get_cache_status` そのものを通すテストは特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (endpoint 単位の確認ができていない)

---

### CHAT-14: モデル選択とモデルパラメータ

- **入口**: チャットオプションの「モデル」欄。`POST /api/config/model`。パラメータは「設定を適用」で `POST /api/config/parameters`。根拠: `frontend/src/components/ChatOptions.tsx:365-461, 502-513`
- **結果**: **その場にいるペルソナだけでなく、メモリ上の全ペルソナのモデルが一時的に切り替わる** (非永続、DB には書かない)。空を選ぶと各ペルソナの DB の DEFAULT_MODEL に戻る。根拠: `saiverse/saiverse_manager.py:1418-1448`、`api/routes/config.py:399-402`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/chat-options.md`「送信のたびに使用モデルを選べる」。**スコープ (全ペルソナ / 非永続) は説明されていない**。`CLAUDE.md` の「Persona model priority: chat UI override > persona DEFAULT_MODEL (DB) > env > built-in default」が実装と一致。
- **現在の挙動 (静的確認)**:
  - 素早い A→B 切替の追い越し対策として `client_id` + `seq` の世代ガードがある。古い要求が遅れて届いたら 409。根拠: `api/routes/config.py:408-429`、`frontend/src/components/ChatOptions.tsx:353-461`
  - POST は 10 秒でアボートし、失敗時はサーバーの実状態を取り直す。タイムアウト時は 3 秒後に resync を予約する。根拠: `frontend/src/components/ChatOptions.tsx:386-457`
  - モデル変更時に `max_image_embeds_override` はリセットされる。根拠: `api/routes/config.py:479-481`
  - 「別名で保存」「上書き保存」は現在のパラメータ + キャッシュ設定 + 画像上限をモデル JSON へ書く (builtin は user_data に上書きコピー)。根拠: `frontend/src/components/ChatOptions.tsx:515-579`
  - **UI にスコープの表示がない** (「このペルソナ」と書かれているのはキャッシュ欄だけで、モデル欄には何も書かれていない)。根拠: `frontend/src/components/ChatOptions.tsx:682-728`
- **追跡できていない境界**: `persona.set_model` の内部、モデル切替後の Session 粒度 `(persona_id, model_key)` の扱い (別 Session になるため anchor も別)。
- **既存テスト**: `tests/audit_20260730_model_change.mjs` (JS のプローブ、pytest の対象外)、`tests/audit_20260730_probes.py`。世代ガードの回帰は `tests/` 内で特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=△` (スコープが説明にない) `挙動の静的確認=✓` `テスト対応=△`

---

### CHAT-15: 画像埋め込み上限

- **入口**: チャットオプション →「データ送信量の管理」→「画像埋め込み上限」。`POST /api/config/max-image-embeds` (onBlur でコミット)。根拠: `frontend/src/components/ChatOptions.tsx:474-484, 799-819`
- **結果**: LLM に送る画像の最大枚数。超過分は**テキスト要約に置換される**。0 で全画像テキスト化、空欄でモデル既定 (組み込み既定 4 枚)。根拠: `frontend/src/components/ChatOptions.tsx:816-818`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/chat-options.md`「送信する画像の上限」。UI のヒント文の方が詳しい。
- **現在の挙動 (静的確認)**: UI の入力は 0〜50 に制限。モデル既定は placeholder に出る。設定はモデル変更でリセットされる (CHAT-14)。根拠: `frontend/src/components/ChatOptions.tsx:806-815`、`api/routes/config.py:479-481`
- **追跡できていない境界**: 実際の置換処理 (どこでテキスト要約に差し替わるか)。`saiverse/model_configs.get_max_image_embeds` の三層解決。
- **既存テスト**: 特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` (置換の実処理を追えていない) `テスト対応=✗`

---

### CHAT-16: ツールの使い方の指定 (ツールモード + pre_spells)

- **入口**: チャット入力欄の隣のツールモードセレクタ。「自動」/「ツール指定」の 2 択で、後者では Playbook 1 つ + Spell 複数を選ぶ。根拠: `frontend/src/components/ToolModeSelector.tsx:58-70`
- **結果**: 選択は `POST /api/config/playbook` にその場で保存され、送信時に `pre_spells` エントリ列へ変換されて `/chat/utter` の body に載る。**最初の LLM 呼び出しの前に必ず実行される** (課金あり)。根拠: `frontend/src/lib/preSpells.ts:60-80`、`frontend/src/app/page.tsx:2461-2481`、`api/routes/chat.py:425-429`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/persona_cognition/nested_subline_spell.md` §13 (コード内参照)。**原文未確認**。利用者向けは `docs/user-guide/chat-options.md`「『ツールを使わず話す』『使うツールを指定する』『ペルソナに委ねる』といったツールの使い方は、別途ツールモード選択で切り替える」— **3 択と書いてあるが実装は 2 択**。→ §3 に記載。
- **現在の挙動 (静的確認)**:
  - `TOOL_MODE_SELECTED = 'tool_selected'` は**サーバーに存在しない Playbook 名のセンチネル**で、送信時には `meta_playbook` として送らない (送ると "playbook not found" になる)。根拠: `frontend/src/components/ToolModeSelector.tsx:44-49`、`frontend/src/app/page.tsx:2462-2478`
  - 旧値 (`meta_user` / `meta_user_manual` / `meta_simple_speak`) がサーバーに残っていても「自動」に黙って写す。根拠: `frontend/src/components/ToolModeSelector.tsx:110-114`
  - Playbook 一覧は `/api/config/playbooks?router_callable=true`、Spell 一覧は `/api/people/spells`。根拠: `frontend/src/components/ToolModeSelector.tsx:147-190`
  - **`/api/people/spells` を `persona_id` なしで呼んでいる**ため、ペルソナ単位の可用性フィルタ (MCP のペルソナ別ゲート、`availability_check`) が適用されない。根拠: `frontend/src/components/ToolModeSelector.tsx:174` vs `api/routes/people/summon.py:41-70`。→ §3 に記載。
  - ユーザーが名指しした起動は Playbook 権限の確認をスキップする (`user_configured` = True で `user_only` も通る)。根拠: `sea/runtime.py:1052-1057`
  - `sea/mode_spell_permissions.py` の aspect 別ゲート表は**現在空**で、実効なし。根拠: `sea/mode_spell_permissions.py:36-41`
- **追跡できていない境界**: `_execute_pre_spells` / `spell_args_decider` Playbook による引数の動的決定 (`sea/runtime_llm.py:2920-`)。
- **既存テスト**: `tests/test_pre_spells_dynamic_args.py`、`tests/test_spell_args_parsing.py`、`tests/test_config_set_playbook.py`、`tests/test_mode_spell_permissions.py`、`tests/test_quick_spell.py`。フロントの `buildPreSpellsFromUI` / `parsePreSpellsForUI` を通すテストは無い (§5)。
- **状態**: `機能の存在=✓` `期待の根拠=△` (説明が実装と食い違う) `挙動の静的確認=✓` `テスト対応=△`

---

### CHAT-17: Playbook 実行の確認ダイアログ

- **入口**: 会話中にペルソナが `ask_every_time` 権限の Playbook を起こそうとすると `permission_request` が流れ、モーダルが出る。根拠: `sea/runtime.py:1105-1175`、`frontend/src/app/page.tsx:2167-2174`、`frontend/src/components/PlaybookPermissionDialog.tsx`
- **結果**: 「許可」「拒否」「常に許可する」「ペルソナには使わせない」。後ろ 2 つは **City 単位の恒久設定**として書き込まれる (`auto_allow` / `user_only`)。応答は `POST /api/chat/permission-response`。根拠: `sea/runtime.py:1080-1092`、`api/routes/chat.py:1483-1496`
- **期待の根拠**: `ユーザー原文への言及` — 「まはー裁定 2026-08-17」がボタン文言のコメントに引用されている (「この」は付けない、設定は City 単位)。根拠: `frontend/src/components/PlaybookPermissionDialog.tsx:80-84`。**原文未確認**。
- **現在の挙動 (静的確認)**:
  - **保険的処理**: 60 秒で自動拒否 (`timeout`) し、toast の警告を出してスキップする。ペルソナには「ユーザーが拒否した / タイムアウトした」旨が state・履歴・SAIMemory に記録される。根拠: `sea/runtime.py:1156-1174`、`sea/runtime.py:1176-1198`、`frontend/src/components/PlaybookPermissionDialog.tsx:22`
  - **保険的処理 / 見えない**: `event_callback` が無い (確認を出す先が無い) 回は**黙って拒否**。根拠: `sea/runtime.py:1118-1120`, `1069-1073`
  - **保険的処理 / 見えない**: 自律 Pulse (`auto_mode`) は確認を出さず拒否。根拠: `sea/runtime.py:1063-1067`
  - `blocked` は常に拒否 (ユーザー指定でも通さない)。根拠: `sea/runtime.py:1049-1050`
  - 判定は 1 箇所に集約されている (EXEC ノードと `/run_playbook` スペルの二重定義を 2026-08-17 レビューで統合した経緯がコメントにある)。根拠: `sea/runtime.py:1000-1007`
- **追跡できていない境界**: `_get_playbook_permission` / `_set_playbook_permission` の永続化先 (DB の city 設定)。
- **既存テスト**: `tests/test_run_playbook_spell.py`、`tests/test_spell_auto_mode_w10.py`、`tests/test_upgrade_handlers_spell_enabled_default.py` (いずれも未読)。ダイアログの往復 (`respond_to_permission`) を通すテストは特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=△` `挙動の静的確認=✓` `テスト対応=△`

---

### CHAT-18: スペル実行の確認ダイアログ (副作用のあるツール)

- **入口**: ネイティブツール (X 投稿、SwitchBot 等) が `request_spell_confirmation` を呼ぶと `spell_confirmation` が流れる。根拠: `tools/confirmation.py:50-124`、`frontend/src/app/page.tsx:2175-2185`
- **結果**: 「実行する」「キャンセル」。編集可能な確認では本文を書き換えて `edit:` 付きで返せる。応答は `POST /api/chat/spell-confirmation-response`。根拠: `api/routes/chat.py:1509-1526`
- **期待の根拠**: `実装のみ (根拠なし)` — 利用者向けの説明を `docs/user-guide/` に見つけられなかった。docstring に設計意図がある。
- **現在の挙動 (静的確認)**:
  - **保険的処理**: 120 秒で自動キャンセル (`timeout` → `approved=False`)。根拠: `tools/confirmation.py:33, 111-117`、`frontend/src/components/SpellConfirmDialog.tsx:23`
  - **⚠️ 保険的処理 / 逆向き**: `auto_mode` のときと、**イベントチャネル / manager が無いとき**は確認を**自動承認**する (`reason="auto"` / `"no_channel"`)。docstring は「UI が無いので永遠にブロックするのを避ける」と説明している。根拠: `tools/confirmation.py:75, 80-85`
    - CHAT-17 (Playbook 権限) は同じ条件で**自動拒否**する。**同種の状況で片方は承認、片方は拒否**という非対称がある。→ §3 に記載。
  - 文字数上限 (`max_chars`) を超える編集は「実行する」を無効化する。空文字も無効。根拠: `frontend/src/components/SpellConfirmDialog.tsx:35-37, 96-101`
- **追跡できていない境界**: どのツールが実際にこれを呼ぶか (X / SwitchBot などの addon 側)。
- **既存テスト**: `tests/test_life_confirmation.py`、`tests/test_spell_misfire_feedback.py` (未読)。`request_spell_confirmation` の自動承認分岐を通すテストは特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

---

### CHAT-19: Chronicle 生成の確認ダイアログ (記憶の整理の費用同意)

- **入口**: 会話の応答後 Metabolism で編纂対象があると、user Pulse のときだけ `chronicle_confirm` が流れる。根拠: `sea/session_lifecycle.py:5686-5710`、`frontend/src/app/page.tsx:2186-2194`
- **結果**: 「未処理メッセージ N 件」「推定 LLM 呼び出し M 回」「モデル名」を見せて「生成する」/「スキップ」。応答は `POST /api/chat/permission-response` を共用 (`_pending_permission_requests` を共有)。根拠: `frontend/src/components/ChronicleConfirmDialog.tsx:56-80`、`frontend/src/app/page.tsx:1560`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/memory_architecture_v2.md` §6.3 (Phase 0, 2026-07-04)。**原文未確認**。
- **現在の挙動 (静的確認)**:
  - **確認を出さず直行する条件が 3 つある**:
    1. 推定 LLM 呼び出しが 0 回 (全チャンクが恒等転写/圧縮 = コストゼロ)。根拠: `sea/session_lifecycle.py:5664-5673`
    2. `force=True` (明示的な要求)。根拠: `sea/session_lifecycle.py:5674-5679`
    3. 非 user Pulse かつペルソナの `AUTONOMOUS_CHRONICLE_ENABLED` が True。根拠: `sea/session_lifecycle.py:5680-5685`
  - **保険的処理**: 60 秒で自動スキップ (`deferred`) し、「Chronicle生成をスキップしました。」の toast を出す。根拠: `sea/session_lifecycle.py:5708-5723`
  - 対話経路が無く自律編纂も OFF なら**確認せず `disabled` で戻る** (押し出された生ログは SAIMemory に残り、後続の user Pulse で編纂される)。根拠: `sea/session_lifecycle.py:5724-5737`
  - 承認後、LLM 実行前に実行台帳へ冪等 claim する (二重編纂 = 二重課金の抑止)。台帳が無い環境では claim なしで走る (degrade)。根拠: `sea/session_lifecycle.py:5739-5760`
  - **費用は「回数」でしか出ない** (推定金額は出ない)。CHAT-11 のプレビューは金額を出すので、同じ画面群の中で粒度が違う。
- **追跡できていない境界**: `plan` の組み立て (`sea/coverage_repair.py` / arasuji 側)、`estimated_llm_calls` の正確さ。
- **既存テスト**: `tests/test_arasuji_*.py` 群 (10 本以上)、`tests/test_execution_ledger.py` / `test_execution_ledger_wiring.py`。**ダイアログの往復と 3 つの直行条件を通すテストは特定できていない。**§5 の事故 1 (レベル2あらすじが一つしか生成されない) はこの下流。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

---

### CHAT-20: 応答前の窓の手当て (読み戻し / 最終防衛ライン / 非常畳み)

- **入口**: **自動処理**。ユーザーが話しかけた (= どの種類の Pulse でも) 直後、Beat ロックの内側・いかなる永続化よりも前に走る。根拠: `sea/runtime.py:180-270`
- **結果**: 3 段。ユーザーには何も出ない (失敗時のみエラーイベント)。
  1. **読み戻し** (`maybe_run_window_refill`): 窓の会話文が「残す量」を下回っていたら、畳んだあらすじを新しい順に開き直す。LLM なし・帳簿のみ。根拠: `sea/runtime.py:246-252`
  2. **最終防衛ライン** (`ensure_window_floor`): 窓の会話の行が残す量を下回ったまま発話させない。埋め切れなければ **Pulse そのものを見送る** (`window_floor_unmet`)。根拠: `sea/runtime.py:262-270`
  3. **非常畳み** (`maybe_run_emergency_precompaction`): 知覚の消費の後・LLM 呼び出しの前に、提示が上限を既に超えていたら応答より先に畳む。根拠: `sea/runtime.py:389-403`
- **期待の根拠**: `既存の仕様文書` — `docs/issues/window_floor_and_refill_redesign.md`、`docs/intent/arasuji_levels.md` §14-3 / §15 / §15-5、`docs/intent/sluice_coverage_gaps.md`。「2026-09-05 まはー裁定」への言及あり。**原文未確認**。
- **現在の挙動 (静的確認)**:
  - 見送りは user Pulse のときだけ画面に出る: **「記憶の窓を用意できなかったため、この応答を見送りました。次に話しかけると再試行します。」**。自律 Pulse はログのみ。根拠: `sea/runtime.py:195-210`
  - 実行 model の解決に失敗しても見送る (検証した窓と喋る窓が食い違うのを防ぐ)。根拠: `sea/runtime.py:225-245`
  - 3 段はいずれも**確認を求めない** (LLM を呼ばない帳簿操作 + 非常畳みは編纂を伴いうる)。非常畳みは `event_callback` を受け取るので `metabolism` イベントで進捗は出る。根拠: `sea/runtime.py:398-400`、`sea/session_lifecycle.py:4884-4916`
  - **保険的処理**: 「合計は上限超えだが会話の行は残す量以下」= 畳めるものが無い回は、LLM を呼ばずに引き返す。根拠: `sea/session_lifecycle.py:249-283`、`api/routes/people/context_status.py:305-310` (画面側の同じ判定)
  - 冷えた起点の前進がスルースのパンマーカーを越えるとき、越えた範囲を `sluice_skipped_spans` へ**記録してから**前進する (fail-closed: 記録できなければ前進しない)。根拠: `sea/session_lifecycle.py:947-1000`
- **追跡できていない境界**: `preview_refilled_history` / `ensure_window_floor` / `maybe_run_emergency_precompaction` の内部 (`sea/session_lifecycle.py` 全 6679 行のうち該当節のみ読了)。`sea/window_refill.py` (177 行) は未読。
- **既存テスト**: `tests/test_window_refill.py` (48 本)、`tests/test_window_floor.py` (47 本)、`tests/test_sluice_cold_isolation.py` (12 本、稟乃さんの形の縮尺再現)。いずれも `session_factory` fixture で SQLite を実際に作って通す (LLM は差し替え)。`tests/test_session_window_folds.py`、`tests/test_metabolism_two_layer.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` (3 段の入口と見送り経路は追えたが、各段の内部計算は未読) `テスト対応=✓`

---

### CHAT-21: 応答後の記憶の整理 (Metabolism)

- **入口**: **自動処理**。Playbook の実行が終わった後、同じ Beat ロックの内側で走る。根拠: `sea/runtime.py:405-435`
- **結果**: 提示が上限を超えていたら古い側を畳んで Chronicle を生成し、窓の起点を進める。LLM 課金が発生しうる (CHAT-19 の確認ダイアログを経る)。進捗は `metabolism` イベントで画面のスピナー文言に出る。根拠: `sea/session_lifecycle.py:1658-1760`、`frontend/src/app/page.tsx:2132-2148`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/chronicle_eviction.md`、`docs/overview/landscape.md` §6「Metabolism（節目：短期リフレッシュ + 長期結晶化）」。ステータスは landscape §6 で「**実装済・実機検証待ち**」。
- **現在の挙動 (静的確認)**:
  - 発火条件は「提示の**合計**が上限 (`high_chars`) を超えた」。根拠: `sea/session_lifecycle.py:1689-1694`
  - 会話の行が残す量以下なら畳まずに引き返す。根拠: `sea/session_lifecycle.py:1704-1716`
  - Metabolism の前後で `building_messages` の max_seq が動いたら WARNING を出す (整理中に発言が入った検知)。根拠: `sea/runtime.py:429-435`
  - フロントは `metabolism` の `completed` を 2 秒表示してから `Thinking...` に戻す。ストリームが閉じた後にこのタイマーが発火するとスピナーが復活して消えなくなるので、`finishReplyCycle` で必ず潰す。根拠: `frontend/src/app/page.tsx:2303-2319, 1669-1673`
  - **保険的処理**: 進捗表示のデフォルト文言は「記憶を整理しています...」。何が畳まれたかは画面に出ない。根拠: `frontend/src/app/page.tsx:2318`
- **追跡できていない境界**: `run_metabolism` の本体 (数千行)。畳んだ結果が実際に記憶の連続性を保っているか。
- **既存テスト**: `tests/test_metabolism_two_layer.py`、`tests/test_metabolism_rate_limit_cooldown.py`、`tests/test_session_window_folds.py`、`tests/test_coverage_repair.py`、`tests/test_arasuji_*.py` 群。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` `テスト対応=△`

---

### CHAT-22: 自動想起 (ふと浮かんだ記憶) の表示

- **入口**: **自動処理**。CONVERSATION アスペクト (user/schedule Pulse) のとき、ローカル埋め込み検索で関連記憶を履歴の末尾に一時注入する。LLM は呼ばない。根拠: `sea/runtime_context.py:685-699`、`sea/runtime_context.py:1679`
- **結果**: `auto_recall` イベントが流れ、画面ではスペル結果と同じ折りたたみで表示される。`<system>` タグは剥がされる。永続化は assistant 発言の `metadata["auto_recall"]` に入り、履歴 API から復元される。根拠: `frontend/src/app/page.tsx:1897-1919`、`api/routes/chat.py:54-58, 242-244`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/memory_architecture_v2.md` §4 / §4.5 / §10-1 / §10-2 / §10-7 (コード内参照)。**原文未確認**。
- **現在の挙動 (静的確認)**: head には混入させず SAIMemory にも永続化しない (提示だけ)。プレビューでは走らない (CHAT-11)。根拠: `sea/runtime_context.py:685-689`
- **追跡できていない境界**: `sea/auto_recall.py` の `build_query` と検索の実装。
- **既存テスト**: `tests/test_auto_recall.py` (未読)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` `テスト対応=△`

---

### CHAT-23: アドオンのバブルボタン (発言ごとの操作)

- **入口**: assistant の発言バブルに、有効なアドオンが宣言したボタンが並ぶ。根拠: `frontend/src/app/page.tsx:3507-3515`、`frontend/src/components/AddonBubbleButtons.tsx`
- **結果**: 2 種類。(a) `play_audio` — メタデータの URL を `new Audio()` で再生。(b) `tool` — `POST /api/addon/{addon}/{tool}` に `{message_id, text, persona_id}` を送り、メタデータ値の変化で完了を検知する。根拠: `frontend/src/components/AddonBubbleButtons.tsx:99-162, 209-240`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/addon_extension_points.md` / `voice_tts_pipeline_streaming.md` (未読)。
- **現在の挙動 (静的確認)**:
  - アイコンは `ICON_MAP` のホワイトリストで解決し、未知の値は `Play` にフォールバック (addon.json から任意のコンポーネントを描かせない)。根拠: `frontend/src/components/AddonBubbleButtons.tsx:37-73`
  - **保険的処理**: 再生成の完了検知は 5 分 (300 秒) のタイムアウト保険で強制解除し、エラー表示にする。根拠: `frontend/src/components/AddonBubbleButtons.tsx:196-208`
  - 完了は SSE (`/api/addon/events`) 経由のメタデータ変化で検知する。SSE は 5 秒で自動再接続。根拠: `frontend/src/hooks/useAddonEvents.ts:50-`
  - 発言本文 (`messageText`) が addon の endpoint へ POST される。根拠: `frontend/src/components/AddonBubbleButtons.tsx:229-235`
- **追跡できていない境界**: addon 側の endpoint 実装 (expansion_data、gitignore 対象)。
- **既存テスト**: `tests/test_addon_hooks.py`、`tests/test_addon_routes_params_merge.py`、`tests/test_addon_events*` (未読)。フロント側のボタン描画・タイムアウトはテストなし (§5)。
- **状態**: `機能の存在=✓` `期待の根拠=△` `挙動の静的確認=✓` `テスト対応=△`

---

### CHAT-24: saiverse:// リンクの解決と内容ビューア

- **入口**: 発言本文の Markdown 内の `saiverse://` リンクをクリック。根拠: `frontend/src/components/SaiverseLink.tsx:31-42`
- **結果**: `saiverse://item/{id}/...` は ItemModal を開き、それ以外は ContentViewerModal が `GET /api/uri/resolve?uri=...&persona_id=...` を叩いて内容を表示する。読み取りのみ。根拠: `api/routes/uri.py:11-54`
- **期待の根拠**: `既存の仕様文書` — `docs/reference/` の `saiverse://` URI リファレンス (手動保守、未読)。`docs/intent/reference_addressing.md` (未読)。
- **現在の挙動 (静的確認)**:
  - Markdown の sanitize schema に `saiverse` プロトコルを href/src の両方で許可している。根拠: `frontend/src/app/page.tsx:53-57`
  - `persona_id` が渡されず URI に `//self/` が含まれるとき、**現在建物の在室ペルソナのうち ID 順で最初の 1 体**にフォールバックして解決する。根拠: `api/routes/uri.py:26-35`
  - 解決結果が `error` のとき `access_denied` なら 403、それ以外は 404。根拠: `api/routes/uri.py:41-45`
  - メッセージログは `[role] YYYY-MM-DD HH:MM: ` のヘッダ正規表現でパースして構造化表示し、末尾 `<<<` をハイライト印として扱う。根拠: `frontend/src/components/ContentViewerModal.tsx:41-70`
- **追跡できていない境界**: `saiverse/uri_resolver.py` のアクセス制御 (どの URI が誰に見えるか)。
- **既存テスト**: `tests/test_document_item_ref.py`、`tests/test_clips.py` (未読)。`resolve_uri` の `//self/` フォールバックを通すテストは特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=△` `挙動の静的確認=✓` `テスト対応=✗`

---

### CHAT-25: システム警告バナー (隔離された履歴ファイルなど)

- **入口**: 画面表示時に `GET /api/system/alerts` を 1 回だけ叩く (ポーリングなし)。根拠: `frontend/src/components/SystemAlertBanner.tsx:25-41`、`frontend/src/app/page.tsx:2996`
- **結果**: critical / warning / info の 3 段。critical は自動展開。`quarantine_` で始まる ID は QuarantineModal を開けるボタンを出し、`details.kind === "unreadable"` は「脇へ移す」ボタン (`POST /api/system/legacy-log/{building}/archive`) を出す。根拠: `frontend/src/components/SystemAlertBanner.tsx:44-71, 95-125`
- **期待の根拠**: `実装のみ (根拠なし)` — 利用者向けの説明を `docs/user-guide/` に見つけられなかった。
- **現在の挙動 (静的確認)**:
  - 退避は「ファイルを消しません」と明記した `window.confirm` を出す。確認済みフラグを持たず、ファイルが移ったこと自体が記録。根拠: `frontend/src/components/SystemAlertBanner.tsx:44-56`
  - fetch 失敗は黙って無視する (「backend may not be ready yet」)。**バックエンドが起動していないときはバナーが出ない**。根拠: `frontend/src/components/SystemAlertBanner.tsx:34-37`
- **追跡できていない境界**: `api/routes/system.py` のアラート生成条件。
- **既存テスト**: 特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

---

### CHAT-26: アクティブクライアントタブの表示

- **入口**: **自動処理**。同一ブラウザ内の複数タブが BroadcastChannel で最終操作時刻を共有し、最新のタブだけがアイコンを出す。根拠: `frontend/src/hooks/useActiveClientTab.ts`、`frontend/src/components/ActiveClientIndicator.tsx`
- **結果**: 表示のみ (クリック不可)。アドオンの client_actions が「どのタブで実行するか」の判定に使う想定。根拠: `frontend/src/components/ActiveClientIndicator.tsx:9-16`
- **期待の根拠**: `実装のみ (根拠なし)` — docstring に設計意図がある。
- **現在の挙動 (静的確認)**: 異なるブラウザ / 端末は BroadcastChannel で同期できないため、**両方がアクティブになりうる (意図的な仕様と明記)**。根拠: `frontend/src/hooks/useActiveClientTab.ts:12-16`
- **追跡できていない境界**: `useClientActions` がこれをどう使うか。
- **既存テスト**: 無し (フロントのみ、§5)。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

---

### CHAT-27: ワーキングメモリ (recalled_ids) の閲覧・操作 API

- **入口**: `GET/POST/DELETE /api/people/{id}/working-memory[/recall[/{source_id}]]`。根拠: `api/routes/people/working_memory.py`
- **結果**: 想起中の ID リストと上限 (`adapter.RECALLED_IDS_MAX`) を返す / 追加 / 個別削除 / 全削除。根拠: `api/routes/people/working_memory.py:38-97`
- **期待の根拠**: `実装のみ (根拠なし)` — `docs/overview/landscape.md` §6 は「旧 `working_memory` テーブルによるワーキングメモリ実装は**死んでおり**、短期記憶は Session 概念へ統合される方向」と書いている。→ §4 に記載。
- **現在の挙動 (静的確認)**: `SAIMemoryAdapter` の `get_recalled_ids` / `add_recalled_id` / `remove_recalled_id` / `clear_recalled_ids` を素通しする薄い層。根拠: `api/routes/people/working_memory.py:44-97`
- **追跡できていない境界**: フロントのどの画面がこの API を呼ぶか (`frontend/src/` の grep では会話画面からの呼び出しを見つけられなかった)。Adapter 側の実装。
- **既存テスト**: 特定できていない。
- **状態**: `機能の存在=✓ (API は在る)` `期待の根拠=✗` `挙動の静的確認=△` `テスト対応=✗`

---

### CHAT-28: リアルタイムスペルの設定

- **入口**: `GET /api/people/realtime-spell-catalog` と `GET/POST/DELETE /api/people/{id}/realtime-spell`。根拠: `api/routes/people/realtime_spell.py`
- **結果**: ペルソナ単位で「毎回実行するスペル」の binding を DB (`RealtimeSpellBinding`) に登録する。根拠: `api/routes/people/realtime_spell.py:82-130`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/realtime_foundation.md` (未読)。
- **現在の挙動 (静的確認)**: カタログは `spell_visible=False` のスペルを除外する (非表示にした判断が UI から素通しにならないように)。ただし `/api/people/spells` と違い**ペルソナ単位の可用性フィルタは掛けていない**。根拠: `api/routes/people/realtime_spell.py:14-45`
- **追跡できていない境界**: binding が実際にどこで消費されるか (Pulse の頭か Beat か)。フロントのどの画面から設定するか。
- **既存テスト**: `tests/test_realtime_spells_media.py` (未読)。
- **状態**: `機能の存在=✓` `期待の根拠=△` `挙動の静的確認=△` `テスト対応=△`

---

### CHAT-29: 右サイドバー (在室者・アイテム・アイテムの開閉)

- **入口**: 会話画面の右サイドバー。`GET /api/info/details?building_id=...` を開いている間 **10 秒ごと**にポーリング。根拠: `frontend/src/components/RightSidebar.tsx:116-182`
- **結果**: 在室ペルソナ・在室ユーザー・アイテム一覧。アイテムの「開く/閉じる」トグル (`POST /api/info/item/{id}/toggle-open`) は**視界コンテキストに内容を含めるか**を切り替える = 次の送信内容が変わる。根拠: `frontend/src/components/RightSidebar.tsx:56, 134-148`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-view.md` / `items-and-files.md` (未読)。
- **現在の挙動 (静的確認)**:
  - **暮らし系の表示 (話しかけやすさ / いま何をしているか / 自律 OFF) は v0.3 で隠されている** (`autonomous_behavior_v3.md` §11「運転 UI は隠す」)。根拠: `frontend/src/components/RightSidebar.tsx:46-48`
  - `currentBuildingId` が未指定の間は fetch しない (server-global の現在地に汚染されるのを防ぐ)。根拠: `frontend/src/components/RightSidebar.tsx:116-122`
  - 建物が変わったら開いているモーダル・メニューを**全部強制クローズ**する (2026-04-30 の「エリス上書き事故」の再発防止と明記)。根拠: `frontend/src/components/RightSidebar.tsx:102-113, 154-172, 221-256`
- **追跡できていない境界**: `api/routes/info.py` の details 生成。
- **既存テスト**: 特定できていない (フロント、§5)。
- **状態**: `機能の存在=✓` `期待の根拠=△` `挙動の静的確認=✓` `テスト対応=✗`

---

### CHAT-30: アドオンの複合アクション設定パネル (ActionsPanel)

- **入口**: アドオン設定画面内のパネル。`GET/POST/PUT/DELETE /api/addon/{addon}/actions`、`/actions/tool-schemas`、`/actions/test-targets`。根拠: `frontend/src/components/ActionsPanel.tsx:95-215`
- **結果**: ユーザー定義の複合アクション (複数ツールの束) の CRUD とテスト実行。会話中にペルソナがスペルとして呼べるようになる。根拠: `frontend/src/components/ActionsPanel.tsx`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/user_defined_composite_actions.md` (未読)。
- **現在の挙動 (静的確認)**: 会話画面本体には出ない (アドオン設定内)。今回は endpoint の呼び出し一覧と編集フォームの構造だけ確認した。
- **追跡できていない境界**: `api/routes/addon_actions.py` の実装、テスト実行が実デバイスを動かすかどうか。
- **既存テスト**: `tests/test_addon_routes_params_merge.py` ほか (未読)。
- **状態**: `機能の存在=✓` `期待の根拠=△` `挙動の静的確認=✗` (会話領域の周辺として存在確認のみ) `テスト対応=△`

---

### CHAT-31: ストリームの心拍監視と切断時の案内

- **入口**: **自動処理**。ストリーム読み取り中、15 秒間データが来なければ接続を死んだものとして扱う。根拠: `frontend/src/app/page.tsx:1602-1612, 1766-1790`
- **結果**: 「通信が途中で切れました。」のシステムメッセージ (`stream_broken`)、または顛末不明なら CHAT-08 の復旧フローへ。根拠: `frontend/src/app/page.tsx:2385-2404`
- **期待の根拠**: `実装のみ + 実測の記録` — 「2026-08-28 実測: バックエンド直結の読み手は即座にエラーを受けるが、中継経由の読み手は done もエラーも受けず永遠に待つ」とコメントに書かれている。**この実測の原典は未確認**。
- **現在の挙動 (静的確認)**:
  - サーバーは 2 秒ごとに `ping` を流すので 15 秒は 7 倍の余裕、という根拠がコメントに書かれている。根拠: `frontend/src/app/page.tsx:1602-1611`、`manager/runtime.py:929-931`
  - この締切が必要なのは Next の中継 (:3000 → :8000) がバックエンドの死をブラウザへ伝えないため。根拠: 同上
  - race に負けた `readPromise` は unhandled rejection にしないよう catch する。根拠: `frontend/src/app/page.tsx:1788-1789`
  - **`_stream_persona_pulse` / `handle_user_input_stream` の finally は「読み手が去っても生成は止めない」**。画面を閉じた程度で認知を打ち切らない設計 (2026-08-26 に一度入れて撤回した経緯がコメントにある)。根拠: `manager/runtime.py:1104-1119`、`manager/runtime.py:935-950`
- **追跡できていない境界**: Next の rewrite/proxy 設定 (`frontend/next.config.ts`) の挙動。
- **既存テスト**: 無し (フロントのみ、§5)。
- **状態**: `機能の存在=✓` `期待の根拠=△` `挙動の静的確認=✓` `テスト対応=✗`

---

### CHAT-32: 応答者の決定 (在室者全員が順に応答)

- **入口**: **自動処理**。発言のたびに `_build_responding_personas(building_id)` が応答者リストを作る。根拠: `manager/runtime.py:475-505`
- **結果**: 建物の在室者 (派遣中を除く) **全員**が、リスト順に**逐次** Pulse を起こす。人数分の LLM 課金が発生する。根拠: `manager/runtime.py:780-830`
- **期待の根拠**: `実装のみ (根拠なし)` — 利用者向け説明を見つけられなかった。ゲーム Region の Ruler 先頭注入だけ `temp/region_rpg_intent.md` §B / §E-2 を参照している (temp/ 配下、正式な intent ではない)。
- **現在の挙動 (静的確認)**:
  - ゲーム Region 内の建物では Ruler が**先頭**に注入される (控室に常駐したまま Region 内全建物の発話を受ける)。根拠: `manager/runtime.py:496-504`
  - Region 解決に失敗したら Ruler 注入をスキップしてログを出す。根拠: `manager/runtime.py:488-495`
  - 各ペルソナの後に `stop_event` を確認し、立っていればループを抜けて `cancelled` を流す。根拠: `manager/runtime.py:781-786, 826-830`
  - **人数分の課金が事前に画面に出ない**。コンテキストプレビューがペルソナごとのタブで費用を出すのが唯一の手がかり (CHAT-11)。
  - retry (CHAT-06) だけは「最初の 1 体」に絞る。根拠: `manager/runtime.py:1285-1290`
- **追跡できていない境界**: `PulseDispatcher.dispatch_user_utterance` の中の「会話が開いているか」判定と `on_event` 判断点の仲裁 (`saiverse/user_conversation.py`)。
- **既存テスト**: `tests/test_user_utterance_durability.py` が `_build_responding_personas` の結果に応じた出口 3 の分岐を固定 (`no_responder` vs `no_response`)。Ruler 先頭注入は `tests/test_facility_map.py` 等に有るかもしれないが特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=△`

---


### 矛盾・疑義

以下は「どちらが正しいか」を断定しない。両方を並べる。

1. **`docs/user-guide/chat-options.md` の「データ送信量の管理」が旧仕様**
   - 文書: 「**メッセージ数上限**」「**履歴の新陳代謝** — ON にすると…OFF は従来のスライディングウィンドウ」「**代謝後の保持件数**」の 3 項目を説明。根拠: `docs/user-guide/chat-options.md` §データ送信量の管理
   - 実装: この 3 項目はどれも UI に無い。あるのは読み取り専用の「会話コンテキストの現在量」(文字数ベース) と「画像埋め込み上限」だけ。ON/OFF トグルは 2026-07-30 に撤去され、オプトアウトはモデル定義で水位を null にする方法のみ。根拠: `frontend/src/components/ChatOptions.tsx:779-821`、`sea/runtime_context.py:346-350`、`saiverse/model_configs.py:220-226`
   - 単位も違う: 文書は「件数」、実装は「文字数」。

2. **`docs/user-guide/chat-options.md` のツールモードが 3 択と書いてある**
   - 文書: 「『ツールを使わず話す』『使うツールを指定する』『ペルソナに委ねる』」の 3 つ。根拠: `docs/user-guide/chat-options.md` §ツールの使い方の指定
   - 実装: 「自動」(ペルソナが判断) と「ツール指定」の **2 択**。「ツールを使わず話す」に相当する選択肢は無い。根拠: `frontend/src/components/ToolModeSelector.tsx:58-70`

3. **`/api/chat/history` の `quarantined` フラグが使われていない**
   - バックエンド: 「Return empty history but **signal the UI so it can show the appropriate state instead of pretending the building is empty**」と書いて `quarantined: true` を返す。根拠: `api/routes/chat.py:286-291`
   - フロント: `HistoryResponse` 型に `quarantined` が無く、値は読まれない。画面は「メッセージが 0 件の建物」と同じ見た目になる。根拠: `frontend/src/app/page.tsx:660-664, 737-758`
   - ただし `SystemAlertBanner` が別経路 (`/api/system/alerts`) で `quarantine_*` の警告を出すので、ユーザーが全く気づかないとは限らない。根拠: `frontend/src/components/SystemAlertBanner.tsx:76`

4. **プレビューが「実際に送られるもの」と食い違いうる 2 点**
   - 原則: 「コンテキストプレビューと同じ値を出すため (プレビューが嘘にならないの原則)」。根拠: `api/routes/people/context_status.py:5-8`
   - (a) プレビューは `persona.model` を使い、実送信は `resolve_execution_context(...).model_key` を使う。根拠: `sea/runtime_context.py:1955` vs `sea/runtime.py:238-245`
   - (b) プレビューは非常畳み (`maybe_run_emergency_precompaction`) を通らないが、実送信は LLM 呼び出しの直前に必ず通す。根拠: `sea/runtime.py:389-403`。窓が上限超えの状態でプレビューを見ると、実際より大きい提示が表示される可能性がある (静的読み取りからの推定であり、実行での確認はしていない)。

5. **確認ダイアログの「出せないとき」の既定が機構ごとに逆**
   - Playbook 権限: 確認先が無い / auto_mode → **拒否**。根拠: `sea/runtime.py:1063-1073, 1118-1120`
   - スペル確認 (副作用のあるツール): 確認先が無い / auto_mode → **自動承認**。根拠: `tools/confirmation.py:75, 80-85`
   - Chronicle 確認: 対話経路が無く自律編纂 OFF → **確認せずスキップ (`disabled`)**。根拠: `sea/session_lifecycle.py:5724-5737`
   - 3 つとも「UI が無いときにブロックしない」という同じ動機を docstring に書いているが、既定の向きが揃っていない。

6. **知覚の水位の定数名とコメントが食い違う**
   - `BUILTIN_PERCEPTION_TARGET_CHARS = 20_000` の行末コメントが「**幅 2 万は…4 万へ広げた**」と書いている。根拠: `saiverse/model_configs.py:249`
   - 直上のブロックコメントは「既定の 6万 / 4万は、Metabolism 側の既定 (残す量 4万 / 上限 12万) と組んで…」と書いている。根拠: `saiverse/model_configs.py:245-248`
   - 実際の値は 20,000 と 60,000。コメント (4万) と定数 (2万) が一致していない。**どちらが意図かは断定しない。**

7. **チャットオプションの「モデル」欄にスコープの表示が無い**
   - 実装: `manager.set_model` は**メモリ上の全ペルソナ**のモデルを一時的に切り替える (非永続)。根拠: `saiverse/saiverse_manager.py:1418-1448`
   - UI: モデル欄には範囲の説明がない。すぐ下の「キャッシュ（このペルソナ）」だけが範囲を明示している。根拠: `frontend/src/components/ChatOptions.tsx:682-766`
   - 利用者向け説明も「送信のたびに使用モデルを選べる」だけ。根拠: `docs/user-guide/chat-options.md`

8. **ツール指定モードのスペル一覧がペルソナ別のフィルタを通っていない**
   - `/api/people/spells` は `persona_id` を受け取れば MCP のペルソナ別ゲートと `availability_check` を適用する。根拠: `api/routes/people/summon.py:41-70`
   - `ToolModeSelector` は `persona_id` を付けずに呼ぶ。根拠: `frontend/src/components/ToolModeSelector.tsx:174`
   - 結果として `availability_check(None)` が呼ばれ、その場のペルソナが使えないスペルも一覧に出うる (静的読み取りからの推定)。

9. **コンテキストプレビューの失敗が「ペルソナがいません」と表示される**
   - フロント: fetch 失敗時に `setContextPreviewData({ personas: [] })`。根拠: `frontend/src/app/page.tsx:2940-2943`
   - モーダル: `personas.length === 0` で「このビルディングに応答可能なペルソナがいません。」。根拠: `frontend/src/components/ContextPreviewModal.tsx:238-240`
   - 加えてバックエンドもペルソナ単位のプレビュー失敗を握って、そのペルソナを黙って一覧から落とす。根拠: `manager/runtime.py:1367-1370`

10. **「返事が来なかった」印が再読み込みで消える**
    - `interrupted` (続きの生成) はサーバーの `metadata["_interrupted"]` に永続化され、履歴 API が返す。根拠: `api/routes/chat.py:66, 267`
    - `needsRetry` / `retryUseless` / `withdrawBlocked` は**クライアント状態のみ**で、`ChatMessage` にも `HistoryResponse` にも無い。根拠: `frontend/src/app/page.tsx:139-148`、`api/routes/chat.py:40-66`
    - つまりブラウザを再読み込みすると「再送」「取り消す」のボタンが消える。サーバー側には `/chat/message-outcome` の `has_reply` という同じ判定材料があるが、履歴取得では使われていない。

11. **landscape §6 の Session の記述とコードの現在地**
    - 文書: 「**コード上にはまだ『Session』という統一制御単位は存在しない**（現状は anchor touch → 履歴取得 → head render の三部構成で個別に動く）」。根拠: `docs/overview/landscape.md` §6
    - コード: `sea/session_lifecycle.py` (6679 行) というクラスが存在し、`SessionLifecycle` として anchor 解決・水位・Metabolism・読み戻し・床を束ねている。根拠: `api/routes/people/context_status.py:311-317` (`runtime.session_lifecycle`)
    - 「統一制御単位」の定義次第で、この記述が古いのか正確なのかは判断しない。

12. **landscape §6 の 机 (desk) の記述と会話画面**
    - 文書: 机は「head の一角」で「有限の作業面（文字数予算、既定8000字）」「溢れると LRU で棚に戻る」「机から下ろした通知は理由別（溢れ／実体消失）にシステムが出し、本人の開閉は通知しない」。根拠: `docs/overview/landscape.md` §6
    - 会話画面には机の残量・中身を見る導線が見つからなかった (`ChatOptions` にも `ContextPreviewModal` の節一覧にも `desk` 相当が無い)。`tests/test_desk.py` / `test_head_pipeline_desk.py` は存在する。**実装が無いのか、会話画面から見えないだけなのかは断定しない。**

---

### 凍結・開発者専用・到達不能・文書のみ

- **`/chat/send` (raw)**: 実装はあり動くが、フロントは常に `/chat/utter` を使う。`/chat/send` は現在地以外への発言を 409 で拒否する専用口。根拠: `api/routes/chat.py:962-987`、`frontend/src/app/page.tsx:2507`
- **旧単一添付 (`attachment` フィールド)**: 後方互換のため残っている。フロントは常に `attachments` を使う。根拠: `api/routes/chat.py:1074-1079`
- **`sea/mode_spell_permissions.py` の aspect 別ゲート表**: `TASK_CONTROL_SPELLS = frozenset()` で**空**。判定関数は素通し。「機構ごと畳むかは v0.4 の運転設計で問う」と注記。根拠: `sea/mode_spell_permissions.py:36-49`
- **暮らし系の表示 (話しかけやすさ / いま何をしているか / 自律 OFF)**: v0.3 で意図的に隠されている (`autonomous_behavior_v3.md` §11「運転 UI は隠す」)。根拠: `frontend/src/components/RightSidebar.tsx:46-48`
- **スルース被覆の UI (第二段)**: API (`api/routes/people/sluice.py`) は在るが、フロントの画面が無い。intent は「入口 UI は第二段」と明記。根拠: `docs/intent/sluice_coverage_gaps.md`、`docs/handoff/2026-09-09_sluice_cold_skip_and_v0311_handoff.md` ②
- **ワーキングメモリ API (CHAT-27)**: endpoint は生きているが、`docs/overview/landscape.md` §6 は「旧 `working_memory` テーブルによるワーキングメモリ実装は死んでおり、短期記憶は Session 概念へ統合される方向」と書いている。フロントからの呼び出しを見つけられなかった。
- **implicit キャッシュ (Gemini / OpenAI) のタイマー表示**: Phase 3 未対応で常に `supported=false`。UI は「このモデルは明示的キャッシュに非対応です（タイマー対象外）。」と出す。根拠: `api/routes/people/cache_status.py:76-82`、`frontend/src/components/ChatOptions.tsx:585-587`
- **旧ツールモードの値 (`meta_user` / `meta_user_manual` / `meta_simple_speak`)**: サーバーに残っていても UI は「自動」に写して表示する。根拠: `frontend/src/components/ToolModeSelector.tsx:110-114`
- **`/api/chat/persona/{id}/avatar`**: 実装内で `Path` を関数定義より前に使っている (`api/routes/chat.py:83` の `Path` は `:399` の import より前) — モジュールレベルの import 順で解決されるので実行時には問題ないが、ファイル内の宣言順が入り組んでいる。**動作の可否は確認していない。**
- **音声入力の注入経路 (stackchan アドオン)**: `expansion_data/` 配下 (gitignore 対象の user pack) から `handle_user_input_stream` を直接叩く。標準配布に含まれるかは未確認。

---

### この領域で「検査が無い」と判断した重要な結果

**最大の穴: フロントエンドにテスト基盤が存在しない。**
`frontend/package.json` に jest / vitest / testing-library / playwright のいずれの依存もスクリプトも無く、`frontend/src` 配下に `*.test.*` / `*.spec.*` が 1 本も無い。根拠: `frontend/package.json`。
このため以下の**利用者に見える結果**は、既存テストがまったく触れていない:

1. **NDJSON ストリームの読み手 (`consumeReplyStream`, `frontend/src/app/page.tsx:1709-2408`, 約 700 行)** — 21 種のイベント分岐、`streaming_discard` による撤回の追跡、「結果ゼロの正常終了を切断と断定する」判定、15 秒の心拍締切、`malformedLines` の扱い。サーバー側 `_OUTCOME_EVENT_TYPES` の鏡がずれても機械的には検出されない (コメントで「同じコミットで揃えること」と依頼しているだけ)。
2. **顛末不明の送信の復旧 (CHAT-08)** — `found` / `not_found` / `unknown` の三分岐、本文と添付の入力欄への戻し、id での重複弾き。サーバー側 `get_message_outcome` を通すテストも見つからない。
3. **CAS 競合 (409) の復旧 (CHAT-02)** — 吹き出しの引っ込め、本文の戻し、添付の条件付き復元 (「手が入っていないときだけ戻す」)。
4. **「再送」「取り消す」「続きの生成」ボタンの出し入れ** — `markRetryable` / `restoreAffordance` / `replyBehind` による印の降ろし。バックエンドの印の管理には厚いテストがあるが、画面側の対応する規則には無い。
5. **送信量の横棒 (`ContextVolumeBar`)** — 「上限は合計と比べ、残す量は会話の行と比べる」という色分けの規則、内訳の足し算、`perception_over_budget` の文言。バックエンドの数値 (`test_context_status.py` 27 本) は固定されているが、それを**画面がどう読み替えるか**は検査されていない。§3 の「数値の契約は主語がある」という失敗の再発面がここ。
6. **`pre_spells` の組み立て/読み戻し (`frontend/src/lib/preSpells.ts`)** — バックエンドの `_SPELL_PATTERN` / `_SPELL_PATTERN_NO_ARGS` と互換であることが**コメントでしか担保されていない**。片方だけ変えると無音で壊れる。
7. **確認ダイアログ 3 種のタイムアウト表示** (60 / 120 / 60 秒) と、タイムアウト後にサーバーが実際に取る挙動 (拒否 / キャンセル / スキップ) の対応。

**バックエンド側で検査が見つからなかった、利用者に見える結果:**

8. **`/chat/withdraw` (CHAT-07)** — `withdraw_user_message` を通すテストを特定できていない。取り消せる/取り消せないの判定は「ペルソナがもう読んだか」という記憶の整合性に直結する。
9. **`/chat/history` のページング (CHAT-09)** — `before` / `after` の ID が見つからないときの空返し、旧メッセージへの md5 仮 ID の安定性。仮 ID は本文の先頭 100 文字を含むので、**本文が編集されると ID が変わる**。
10. **`/uri/resolve` の `//self/` フォールバック (CHAT-24)** — 在室ペルソナの ID 順で最初の 1 体に解決する。誰の視点で解決されたかがユーザーに見えない。
11. **`request_spell_confirmation` の自動承認分岐 (CHAT-18)** — 副作用のあるツールが確認なしで実行される条件。
12. **`chronicle_confirm` の 3 つの直行条件 (CHAT-19)** — 特に「推定 LLM 呼び出しが 0 回なら確認しない」。この推定が外れると無確認で課金が起きる。

**事故二件が、この領域のどこで検査対象になるはずだったか:**

- **事故 1 (レベル2あらすじが一つしか生成されない)** — 会話側の入口は **CHAT-19 (Chronicle 生成の確認ダイアログ)**。ダイアログは「未処理メッセージ N 件 / 推定 LLM 呼び出し M 回」を見せて同意を取るので、**同意した M 回と実際に生まれた成果物の数を突き合わせる検査**がここに要る。現状のテスト (`test_context_status.py` の `fold_ready`) は「畳みが起きるか」までは実際の計画関数で確認するが、**承認した回数ぶんの成果物が生まれたか**は誰も見ていない。handoff にある「安全弁 3 件で頭打ち → 承認 5 件でまとめ 3 件で完了顔」はこの形。根拠: `docs/handoff/2026-09-09_sluice_cold_skip_and_v0311_handoff.md` ①
- **事故 2 (大量履歴をコンテキストに読み込んだ状態でスルースにも全量を読ませた)** — 会話側の入口は **CHAT-20 (応答前の窓の手当て)** と **CHAT-12 (送信量の表示)**。読み戻しが 212 万字を窓に開いたのに、`context-status` は「現在量」を出せる立場にありながら、**その量が異常であることをユーザーにも機構にも知らせる契約を持っていなかった** (`measurement_failed` と `perception_over_budget` はあるが「大きすぎる」の旗は無い)。加えて 429 の文面がコンテキスト超過判定の文字列に一致して誤分類された経路は、**「エラーの分類」に検査が無かった**ことの現れ。現在は後退方式ごと撤去され (`sea/sluice.py:1526-1531`)、`tests/test_sluice_cold_isolation.py::test_context_overflow_marker_no_longer_reclassifies_the_429` が回帰として置かれている。根拠: `docs/handoff/2026-09-09_sluice_cold_skip_and_v0311_handoff.md` ②
- 両方に共通するのは **「機構が自分で決めた量 (安全弁 3 件 / 窓に開く量) が、ユーザーに提示した数字や同意した数字と一致しているか」を突き合わせる検査が無い**こと。CHAT-12 / CHAT-19 / CHAT-11 は「数字をユーザーに見せる」役割を持つ 3 つの面であり、そこが検査の置き場所になる。

---

## 領域 B. 記憶 — Chronicle / Memopedia / スルース / 取り込み (MEM-01〜41)


### MEM-01: チャットログの閲覧・編集 (スレッド単位)

- **入口**: ペルソナメニュー →「記憶」→「チャットログ」タブ。`frontend/src/components/MemoryModal.tsx:131` → `memory/MemoryBrowser.tsx`。API は `api/routes/people/memory.py:17`(スレッド一覧), `:45`(メッセージ一覧), `:106`(追加), `:158`(編集), `:177`(削除), `:190`(スレッド削除), `:203`(有効化)。
- **結果**: `~/.saiverse/personas/<id>/memory.db` の `threads` / `messages` を直接変更する。追加・編集時は `replace_message_embeddings` で埋め込みも張り直す (`memory.py:141`)。LLM 課金なし。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/memory-view.md §タブ`「生の会話ログをスレッド単位で閲覧…メッセージの追加も可能」。
- **現在の挙動 (静的確認)**: 追加は `get_or_create_thread` + `add_message` を `adapter._db_lock` の内側で実行し、内容が空でなければチャンク分割して埋め込みを張る (`memory.py:120-148`)。削除は `adapter.delete_message` / `delete_thread` に委譲。
- **追跡できていない境界**: メッセージ削除が Chronicle の `source_ids` に残す孤児参照の後始末。`sai_memory/arasuji/absorption.py` の `_sweep_dead_message_sources` が補修経路で掃くことは読んだが、削除 API 自体は何も通知していない (削除時点では Chronicle は触られない)。
- **既存テスト**: `tests/test_open_persona_memory.py`, `tests/test_sai_memory_storage.py`, `tests/test_memory_db_connection_leak.py` — storage 層の実 DB を通す。API ルート関数を直叩きする網は見つけられなかった。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (storage 層はあるが、この API の入口を通す試験を確認できていない)

### MEM-02: Stelis スレッドの救出

- **入口**: 「デバッグ」タブ (`MemoryRecall.tsx`) のボタン → `POST /{persona_id}/rescue-stelis-thread` (`api/routes/people/memory.py:217`)。
- **結果**: 現在アクティブな Stelis スレッドを通常スレッドへ変換する (memory.db の書き換え)。LLM 課金なし。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/memory-view.md`「Stelis スレッド救出などの調査用」。設計は `docs/intent/stelis_thread.md` (未読)。
- **現在の挙動 (静的確認)**: ルートの存在と frontend からの呼び出しは確認した。変換処理の中身は未読。
- **追跡できていない境界**: 変換の実処理と、変換後に Chronicle / 提示窓がどう変わるか。
- **既存テスト**: 該当テストを特定できていない (`tests/` に `rescue` を含む名前なし)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` `テスト対応=✗`

### MEM-03: コア記憶の一覧・確認・訂正・削除・復元

- **入口**: 「コア記憶」タブ (`memory/CoreMemoryScene.tsx`)。API は `core_memory.py:472`(一覧), `:507`(ごみ箱), `:582`(confirm), `:606`(edit), `:651`(delete), `:690`(restore)。
- **結果**: memory.db のコア記憶ページを変更する。**ユーザーの edit/delete/restore は「仮想センサー」でペルソナへ `event_message` 通知する** (`core_memory.py:1-27` の docstring、`_notify_persona_correction`)。confirm は内容不変なので通知しない。LLM 課金なし。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/memory_architecture_v2.md §5.1` (docstring に引用あり。原文は未確認)。
- **現在の挙動 (静的確認)**: `confirmed` は 0/1 のフラグで、スルースの自動採取が `confirmed=0`、ユーザーの編集で 1 になる (`sai_memory/core_memory.py:281-392`)。`count_unconfirmed_core_memories` がチャットの「N件更新」バッジ用 (`:509`)。削除は soft-delete。
- **追跡できていない境界**: 未確認バッジの置き場所 (in_flight 台帳に「残 = 未確認バッジの置き場所の確定」とある — `docs/overview/in_flight.md:66`)。
- **既存テスト**: `tests/test_core_memory_storage.py`, `tests/test_core_memory_section.py`, `tests/test_core_memory_scene_api.py`(route 関数を直叩き、temp DB + Embedder patch)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### MEM-04: コア記憶 scene の作成 (会話を探して刻む)

- **入口**: 「コア記憶」タブの検索 → 窓プレビュー →「刻む」。API は `core_memory.py:171`(検索), `:293`(窓), `:377`(scene 作成)。
- **結果**: `sai_memory.core_memory.create_scene_core_memory` で会話の逐語転写をコア記憶へ書く。スペル `memory_clip mode='transcribe' paste_to='core'` と同じ関数 (`core_memory.py:383`)。LLM 課金なし。目安字数超過は `over_budget` で返すだけで止めない。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/memory_architecture_v2.md §5「UI 導線」`(docstring 引用あり、原文未確認)。
- **現在の挙動 (静的確認)**: 実会話でないメッセージ (ツール実行ログ等) は 404 で拒否 (`core_memory.py:412`)。
- **追跡できていない境界**: `_resolve_budget` が参照するコア記憶の字数予算の解決順。
- **既存テスト**: `tests/test_core_memory_scene_api.py::TestCreateScene` 系 (クラス名は未確認だが、docstring に create_scene が対象と明記)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### MEM-05: 手帳 (アクティビティとメモ) の閲覧

- **入口**: 「手帳」タブ (`memory/PocketbookViewer.tsx`) → `GET /{persona_id}/pocketbook` (`api/routes/people/pocketbook.py:96`)。
- **結果**: 読み取りのみ。**v0.3 は訂正の口を置かない** (`pocketbook.py:16` の明記)。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/autonomous_behavior_v3.md §13.6` (「まはー承認の正典」と `sluice_coverage_gaps.md` B-2 が書く。原文は未確認)。
- **現在の挙動 (静的確認)**: メモは二つの日付を持つ — `date`(書かれた日) と `event_date`(できごとの日、機械刻印)。**提示・ソートの軸はできごとの日** (`pocketbook.py:50-70`, `sai_memory/memory/pocketbook.py:175 effective_date`)。`origin` は `live`/`readback`/`mechanism` の閉語彙 (旧行は NULL = live 相当)。「テーブルがまだ無い」だけを空配列に倒し、他の読み取り失敗は 500 のまま上げる (`pocketbook.py:19-21`)。
- **追跡できていない境界**: `PocketbookViewer.tsx` の表示が `origin` を「ペルソナが書いた」等へどう写しているか (`PocketbookViewer.tsx:55` に `sluice: 'ペルソナが書いた'` の辞書があることまでは確認)。
- **既存テスト**: `tests/test_pocketbook_api.py`(route 関数直叩き、書き込みは fixture のみ), `tests/test_pocketbook_and_edges.py`, `tests/test_pocketbook_spells.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### MEM-06: タスク帳 (約束) の閲覧

- **入口**: 「手帳」タブ内 → `GET /{persona_id}/task-book` (`pocketbook.py:204`)。
- **結果**: 中央 DB の open なタスク行を読み取りのみで返す。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/autonomous_behavior_v3.md §13` (docstring 引用、原文未確認)。
- **現在の挙動 (静的確認)**: 閉じた行は除外、テーブル不在は空配列、未知ペルソナは 404。
- **追跡できていない境界**: 書き手 (スルースの promises 適用、`sea/sluice.py:1094 _apply_promises`) の CAS (revision 照合) の実装細部は読んだが、タスク帳側の更新経路は未追跡。
- **既存テスト**: `tests/test_pocketbook_api.py`(get_task_book)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### MEM-07: Chronicle (あらすじ) の閲覧

- **入口**: 「Chronicle」タブ (`memory/ArasujiViewer.tsx`)。API は `arasuji.py:231`(stats), `:645`(一覧), `:732`(単体), `:894`(source メッセージ), `:939`(fragments), `:978`(ID 指定のメッセージ取得)。
- **結果**: 読み取りのみ。レベル別の件数と本文、被覆元メッセージ、そこから生まれた Fragment を見せる。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/memory-view.md`「あらすじ（Chronicle）をレベル別に閲覧」。設計は `docs/intent/arasuji_levels.md`。
- **現在の挙動 (静的確認)**: `_get_arasuji_db` はテーブル用意で倒れたら接続を閉じてから送出する (`arasuji.py:57-84` — 閉じ忘れが Memopedia を待たせた 2026-09-02 の事故対応)。
- **追跡できていない境界**: 被覆の「歯抜け」表示 (未着手の issue `docs/issues/chronicle_coverage_range_hides_gaps.md`)。
- **既存テスト**: `tests/test_arasuji_generator.py`, `tests/test_arasuji_diagnosis_api.py`(診断側)。一覧・単体の route を通す試験は特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

### MEM-08: Chronicle エントリの編集・削除・全削除

- **入口**: Chronicle タブ → `PATCH /{persona_id}/arasuji/{entry_id}` (`arasuji.py:832`), `DELETE .../{entry_id}` (`:783`), `DELETE .../arasuji` (`:866` 全削除)。
- **結果**: あらすじ本文の書き換え / エントリの削除。削除は被覆を「未被覆」に戻すので、次の見積もり (MEM-12) の件数が増える。LLM 課金なし。
- **期待の根拠**: `実装のみ (根拠なし)` — `docs/user-guide/memory-view.md` は閲覧しか書いていない。intent 側にも編集・全削除の UI 操作の記述を見つけられなかった。
- **現在の挙動 (静的確認)**: 削除で該当範囲が未被覆に戻ることは `tests/test_coverage_repair.py::TestDeleteRestoresUncovered` が固定している。全削除の後の再編纂が実行台帳の completed 行に塞がれる欠陥は 2026-09-08 に発見され、`supersede_completed` で修正された (`docs/issues/chronicle_delete_all_then_recompile_blocked_by_ledger.md`、handoff ①)。
- **追跡できていない境界**: 編集 (PATCH) が上位あらすじの stale 連鎖を起こすかどうか。読んだ範囲では確認できていない。
- **既存テスト**: `tests/test_coverage_repair.py::TestDeleteRestoresUncovered`(削除), `tests/test_metabolism_two_layer.py::RemoveFoldsReferencingEntryTest`(削除に伴う fold の掃除)。編集 (PATCH) のテストは特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△` `テスト対応=△`

### MEM-09: Chronicle エントリ単体の再生成

- **入口**: Chronicle タブ → `POST /{persona_id}/arasuji/{entry_id}/regenerate` (`arasuji.py:1013`)。
- **結果**: そのエントリを LLM で作り直し、親子関係を保つ。**LLM 課金あり** (1 コール)。Beat ロック (`hold_beat`) で補修ジョブ・Metabolism・削除/編集と直列化する (`arasuji.py:1035-1040`)。
- **期待の根拠**: `実装のみ (根拠なし)` — docstring が処理手順を書くだけ。
- **現在の挙動 (静的確認)**: 実処理は `sai_memory/arasuji/storage.regenerate_entry` に委譲。API は 400/500 の写像だけ持つ。
- **追跡できていない境界**: `regenerate_entry` の中身 (親の付け替えと source_ids の扱い)。
- **既存テスト**: `tests/test_arasuji_regenerate.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△` `テスト対応=✓`

### MEM-10: 手動の畳み — 「溜まった会話をあらすじにまとめる」(mode=compaction)

- **入口**: 2 つ。①Chronicle タブの生成ボタン (`ArasujiViewer.tsx:1000`) ②ペルソナメニュー (`PersonaMenu.tsx:163`、確認 `confirm()` 付き)。どちらも `POST /{persona_id}/arasuji/generate` の `mode="compaction"` (`arasuji.py:1460`)。
- **結果**: 背景ジョブが `SessionLifecycle.run_manual_compaction_checked` を呼ぶ (`arasuji.py:1160`)。残す量より古い側の会話をあらすじへ畳み、提示窓を前進させる。**LLM 課金あり**。完了後に `ensure_recall_embeddings` (ローカル・無料) を必ず走らせる。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/arasuji_levels.md §13 裁定4`「手動生成も範囲規則を自動と揃える。『発火を待たずに今すぐ畳む』だけ」。docstring (`arasuji.py:1093-1113`) が引用している。
- **現在の挙動 (静的確認)**: 旧「全量編纂 + 進捗バー + max_messages / model / with_memopedia 指定」は撤去され、リクエストの旧フィールドは受理して無視する (`arasuji.py:1085`)。status → 文面の写像は 7 分岐 (`ok`/`noop`/`deferred`(中止)/`deferred`(claim 競合)/`deferred_sluice_unseen`/`disabled`/`failed`)。head 再構築の失敗は completed のまま添え書きにする (`_HEAD_REBUILD_WARNING`)。
- **追跡できていない境界**: `run_manual_compaction_checked` の内部で `run_metabolism` に渡す窓の撮り方 (anchor 解決の分岐) までは読んだが、全経路の追跡はしていない。
- **既存テスト**: `tests/test_arasuji_generation_status_mapping.py`(status → error_code/文面の写像を固定。lifecycle は `SimpleNamespace` の偽物で、実際の畳みは通さない)。`tests/test_metabolism_two_layer.py::ChronicleClaimTest`(claim と束ね予算。executor は偽物)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (文面の写像は固定されているが、実際の畳みを通す試験ではない)

### MEM-11: 被覆補修 — 「あらすじになっていない過去の会話を編纂」(mode=repair)

- **入口**: Chronicle タブの補修バナー →確認モーダル →実行 (`ArasujiViewer.tsx:930-960`, `:557-565`)。同じ `POST .../arasuji/generate` の `mode="repair"` + `confirmed_unprocessed_messages`。
- **結果**: `SessionLifecycle.run_coverage_repair_checked` へ委譲 (`arasuji.py:1325`)。止め線 (`resolve_compile_ceiling`) より古い未被覆の編纂対象を一次あらすじにする。**提示窓は動かさない (退場なし)**。**LLM 課金あり**。完了文に内訳 (あらすじにした N / 隣のあらすじに合流 M / まとめ K / 残り) を出す (`arasuji.py:1348-1390`)。
- **期待の根拠**: `既存の仕様文書` + `ユーザー原文の引用あり` — `docs/intent/arasuji_levels.md §16` (被覆補修) と `docs/intent/chronicle_coverage_gaps.md` 機構 A〜G。後者の「原則」節と機構 A にまはーの原文が引用されている (「何も保証できてないのに、ユーザーさんの API 使用量を食いつぶす会話量を送ることを強制することって正当化できるか？」)。
- **現在の挙動 (静的確認)**:
  - 実行直前に `_count_repair_targets` で対象を数え直し、承認時より**増えていたら** `estimate_stale` で止める (`arasuji.py:1315-1323`)。減る方向は走る。
  - 内訳は `lifecycle.pop_last_chronicle_breakdown` から取り、`compiled`/`absorbed`/`folds`/`silent`/`skipped`/`deferred` を出し分ける。編纂ゼロで束ねだけなら文面を「あらすじを大きな流れにまとめました」に差し替える (`arasuji.py:1362-1366`)。
  - 止め線の解決失敗 (`CeilingResolutionError`) は fail-closed で `ceiling_unresolved` として止める (`arasuji.py:1428-1440`)。
- **追跡できていない境界**: `run_absorption` の内部 (1610 行) は計画・実行の骨子だけ読み、generate-then-swap の失敗復元までは追えていない (`docs/issues/absorption_indeterminate_commit_recovery.md` が未解決として起票済み)。
- **既存テスト**: `tests/test_coverage_repair.py`(`TestRunCoverageRepair` / `TestRepairCompletionBreakdown` / `TestSilentOnlyRunEndToEnd` / `TestEstimateStaleGuard` ほか。**`run_band_overflow` を `lambda *a, **k: 0` に差し替えている箇所が 3 つある** — `:492`, `:804`, `:1031`)。`tests/test_arasuji_absorption.py`(3283 行。ここでも `run_band_overflow` を 0 に差し替える箇所が 5 つ)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (補修と束ねを同時に通す試験は無い。束ねは差し替えられている)

### MEM-12: 補修の事前見積もりとバナー・確認モーダル

- **入口**: Chronicle タブを開くと `GET /{persona_id}/arasuji/cost-estimate` (`arasuji.py:106`)。
- **結果**: 「あらすじになっていない過去の会話が N 件」「まとめる作業が M 回分」「前回の処理が完了していません」の 3 種のバナーと、実行前の費用見積もり (対象件数 / まとめ回数 / AI 呼び出し / 概算費用)。読み取りのみ (`persist_advance=False`)。
- **期待の根拠**: `既存の仕様文書` — `chronicle_coverage_gaps.md` 機構 G + 「2026-09-08 の実機検証での追記」(「補修の入口は、未編纂がゼロでも束ねの残りが 1 回分以上あれば開く」)。
- **現在の挙動 (静的確認)**:
  - 止め線の解決失敗は **500 で止める** (`arasuji.py:141-146` — 「読めなかった」を「上端なし」へ潰さない)。
  - `is_repair_incomplete` の読み取り失敗は **True に倒す** (バナーを消さない、`:196-206`)。
  - `consolidation_calls` は `plan_band_overflow` の dry 予測 (`sai_memory/arasuji/estimate.py:214-233`)。fold 照会失敗時は 0 (生成経路が束ねを見送るのと同形)。
  - 見積もりの `estimated_llm_calls` は `level1_calls + consolidation_calls + upper_regen_calls`。`upper_regen_calls` は `consolidation_calls` に**混ぜない** (CLI が `consolidation_calls` を `max_folds` にそのまま渡すため、`estimate.py:31-37`)。
- **追跡できていない境界**: 見積もりが出す `consolidation_calls` と、実行時に `generate_chronicle` が計算し直す `band_plan_count` (`sea/session_lifecycle.py:5606`) は**別々の計算**である。両者がずれた場合に、承認画面の「まとめ M 回分」と完了文の「まとめ K 件」がどれだけ食い違うかは追えていない。`confirmed_unprocessed_messages` の時点ずれの歯止めは**メッセージ件数にしか効かない** (`arasuji.py:1315`)。
- **既存テスト**: `tests/test_coverage_repair.py::TestEstimateGenerationParity`(見積もりと生成が同じ止め線・同じ数を言うこと), `::TestEstimateStaleGuard`。バナーの表示条件 (フロント側) を通す試験は見つけられなかった。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (件数の一致は固定済み。束ね回数の一致とフロントの表示条件は未検査)

### MEM-13: 上位あらすじの束ね (統合) — レベル階層の生成 ← **事故1 の発生地点**

- **入口**: 利用者の直接操作ではない。**3 つの呼び出し元**がある:
  1. `generate_chronicle` の `after_chunk` — チャンク確定のたび (`sea/session_lifecycle.py:6296`)
  2. `generate_chronicle` の走行末尾 — 予算が残り進んでいる限り**呼び直すループ** (`sea/session_lifecycle.py:6331-6345`、2026-09-09 追加)
  3. CLI `scripts/arasuji/build_arasuji_core.py:915-928` — `execute_plan` の後に呼び直しループ (**`after_chunk` は渡していない**)
- **結果**: レベル N の並びの合計字数が上限 (`BAND_CHAR_LIMIT=5,000`) を超えたら、古い側を 1 個の親にまとめてレベル N+1 へ置く。**畳み 1 回 = LLM 1 コールで課金あり**。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/arasuji_levels.md §3-2` と `§9`。両者が「べき」を定めている:
  - §9: レベル1 以上は**上限 5,000 字 / 残す量 2,500 字**。レベル0 は上限 20 万字 / 残す量 10 万字。
  - §3-2: 畳み 1 回の材料の合計は **U/2 = 5,000 字**まで (`FOLD_MATERIAL_CHAR_LIMIT`、2026-09-08 まはー裁定)。「子 1 本 300〜800 字の実態で親 1 本あたり子 7〜15 本になり、旧設計の『Lv1 を 10 本で Lv2 一本』と同じ粒感に戻る」。
  - §3-2: 安全弁 `SAIVERSE_CHRONICLE_MAX_BAND_CONSOLIDATIONS_PER_RUN` (既定 3) は **`run_band_overflow` 1 回の呼び出しあたり**であって走行全体ではない (2026-09-03 まはー裁定)。
  - **⚠️ 件数の主語**: 正典に「N 通のログからレベル2 が何本できるべきか」を直接言う契約は無い。**「べき」は字数の予算 (レベルごとの並びの上限/残す量) と材料上限 U/2 から導かれる量**であって、件数として書かれていない。実装上の「実際に何本作るか」は `plan_band_overflow` の dry 予測 (= 承認済み予算 `max_folds`) が決めており、**dry が少なく数えれば実行はそれ以上作らない** (`bands.py:1234`, `session_lifecycle.py:6252`)。
- **現在の挙動 (静的確認)**:
  - 計画は `_plan_folds` (`bands.py:692`) — dry (`plan_band_overflow`) と実行 (`run_band_overflow`) が同じ関数を共有する。
  - 1 回の呼び出しの上限は `min(_max_consolidations_per_run(), max_folds)` (`bands.py:1259-1262`)。
  - 予算は**試行回数**で消費する (成功数ではない、`session_lifecycle.py:6180-6195`) — 失敗する畳みが同じ予算で無限に再試行されないため。
  - 走行末尾のループは「進んだ間だけ呼び直す」形 (`session_lifecycle.py:6331-6345`)。
- **事故1 がここでどう現れるはずだったか**: handoff (`docs/handoff/2026-09-09_sluice_cold_skip_and_v0311_handoff.md` ①) が記録する二つの欠陥が、どちらもこの項目の「結果」に直接出る。
  1. **材料上限の欠落**: 2026-07-21 の W4 移行が旧 `consolidation_size=10` の「一回の畳み量の上限」の役割を引き継ぎ損ねていた。全量編纂では境界の無い巨大区間が丸ごと 1 個の親に握られ、**dry も「巨大区間 = 畳み 1 回」と数える**。その結果 `band_plan_count=1` → 実行も 1 本 → **レベル2 が 1 つしか生成されない**。修正は `FOLD_MATERIAL_CHAR_LIMIT` (2026-09-08)。
  2. **束ねだけの走行の頭打ち**: チャンクが無い走行では `after_chunk` が一度も走らないので、末尾の 1 回きりの呼び出しが安全弁 3 件で止まる (承認 5 件 → まとめ 3 件で完了顔)。修正は末尾の呼び直しループ (2026-09-09、コミット `7d7214be`)。
- **なぜ既存の検査で捕まらなかったか (静的に読める範囲の事実)**:
  - 束ねの計画の試験 (`tests/test_arasuji_bands.py`) は**純関数と小さい DB**を使い、「畳みが起きること」「上限を跨がないこと」を見る。事故前の時点で「大量の材料から**何本の**上位あらすじが出るべきか」を言う assert は無かった (材料上限の試験 `TestFoldMaterialCap` は 2026-09-08 の修正と同時に追加されたもの)。
  - 補修経路の試験 (`tests/test_coverage_repair.py`) と吸収の試験 (`tests/test_arasuji_absorption.py`) は、**`run_band_overflow` を `lambda *a, **k: 0` に差し替えている** (計 8 箇所)。束ねは補修の結果の一部なのに、補修の試験からは束ねが消えている。
  - 走行側の試験 (`tests/test_metabolism_two_layer.py::ChronicleClaimTest`) は逆に `plan_band_overflow` / `run_band_overflow` の**両方を偽物に差し替え**、呼び出しの `max_folds` の並びだけを見る。dry の数え方そのものは検査対象外。
  - 実物を繋ぐ唯一の試験 `tests/test_arasuji_interleaved_consolidation.py` は 20 チャンクで **`folded == 1` / Lv2 が 1 件**を正解として固定している (`:186-192`)。規模が小さいので事故の形にならず、かつ末尾の呼び直しループを写していない (`_consolidate()` を 1 回呼ぶだけ、`:181`)。
  - つまり、**「編纂した結果としてレベル階層が何段・何本できたか」を利用者に見える結果として検査する網が、どの層にも無かった**。
- **追跡できていない境界**: CLI 経路 (`build_arasuji_core.py`) の束ねが `after_chunk` を渡していないこと (下の §3 矛盾 ③) の実際の影響。
- **既存テスト**: `tests/test_arasuji_bands.py`(実 DB + 偽 LLM。`TestFoldMaterialCap::test_dry_count_matches_execution_count_on_bulk_backlog` が 30 本の backlog で dry と実行の総数一致を固定)。`tests/test_arasuji_interleaved_consolidation.py`(executor + bands + context を実物で繋ぐ)。`tests/test_metabolism_two_layer.py::ChronicleClaimTest::test_final_consolidation_loops_until_the_approved_budget_is_done`(2026-09-09 の呼び直しループ。ただし `run_band_overflow` は偽物)。
- **状態**: `機能の存在=✓` `期待の根拠=✓`(字数の契約として) `挙動の静的確認=✓` `テスト対応=△` (計画層は厚い。走行と結果を結ぶ層と CLI 経路は薄い)

### MEM-14: 会話中の自動編纂 (Metabolism) と確認ダイアログ

- **入口**: 応答後の Metabolism (`SessionLifecycle.maybe_run_metabolism` → `run_metabolism` → `generate_chronicle`)。user Pulse では `chronicle_confirm` イベントで `ChronicleConfirmDialog` を出す (`session_lifecycle.py:5680-5700`)。
- **結果**: 未処理メッセージ数・推定 LLM 呼び出し・モデル名を見せ、「生成する / スキップ」。**60 秒で自動スキップ** (`ChronicleConfirmDialog.tsx:22`)。承認すると LLM 課金あり。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/memory_architecture_v2.md §6.3 (Phase 0, 2026-07-04)`。非 user Pulse は `AUTONOMOUS_CHRONICLE_ENABLED` が True なら確認なしで実行 (docstring 引用あり、原文未確認)。
- **現在の挙動 (静的確認)**: LLM コール見込みが 0 の回は確認せず直行する (`session_lifecycle.py:5657-5665`)。`force=True` (手動入口) も確認を経ない。
- **追跡できていない境界**: `_pending_permission_requests` の応答の受け口 (manager 側)。
- **既存テスト**: `tests/test_metabolism_two_layer.py`(claim・退役ゲート・退場計画)。確認ダイアログの timeout/deny の写像は `deferred` として扱われることを docstring で読んだが、対応するテストは特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

### MEM-15: 非常畳み (会話の応答より前の回復措置)

- **入口**: 話しかけた時点で提示窓が上限を超えていたら発火 (`session_lifecycle.py:2760 maybe_run_emergency_precompaction`)。利用者へは status イベントで**通知**するだけ (同意ダイアログではない)。
- **結果**: `run_metabolism(..., chronicle_force=True, close_undersized_tail=True)` を呼ぶ。**LLM 課金あり** (編纂 + スルース)。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/arasuji_levels.md §14-3`「原因不問の回復措置」。「畳まない選択肢は無い (まはー裁定 2026-07-29)」と docstring が引用。
- **現在の挙動 (静的確認)**:
  - 判定は**実際に送る中身** (知覚ブロック込み) で測る (`presented_chars`、2026-09-02 まはー裁定)。
  - 合計は上限超えでも会話の行が残す量以下なら「畳めるものが無い」として通知も行の立ち上げもせず引き返す (`:2825-2831`)。
  - **レート制限の小休止中は見送る** (`:2792-2800`、sluice_coverage_gaps 第一段 C-1)。
- **追跡できていない境界**: `close_undersized_tail=True` が材料 U 未満の端数を閉じる経路 (回復措置に限る例外) の下流。
- **既存テスト**: `tests/test_sluice_cold_isolation.py::ColdMetabolismTest`(巨大な未提示履歴を持つ実 DB で非常畳みを回し、二回目は畳むものが無くなって引き返すことを固定)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### MEM-16: 読み戻し (窓の充填) ← **事故2 の上流**

- **入口**: Pulse の応答より前に、窓の会話文が目標量を下回っていたら発火 (`session_lifecycle.py:2888 maybe_run_window_refill`)。全種の Pulse で走る (2026-09-05 まはー裁定)。
- **結果**: 畳んだあらすじを新しい順に開き直し、目標量まで会話を戻す。**LLM ゼロ** (帳簿のみ)。
- **期待の根拠**: `既存の仕様文書 + ユーザー原文の引用あり` — `docs/intent/arasuji_levels.md §15` / `§15-5`、および `sluice_coverage_gaps.md` 追加の決定 2 (原文「未編纂の物に開くとかそういう概念ないんだから後ろから見てってほしい分までとったら終わりでいいでしょ」)。
- **現在の挙動 (静的確認)**: `_plan_window_refill` の docstring が段取りを持つ (`session_lifecycle.py:3413-3450`):
  - 不足判定は**会話文だけ** vs 目標量 (知覚も機構名義の行も残す量を消費しない)。
  - **丸ごと開く単位があるのはあらすじに覆われた圧縮区間だけ**。生の未編纂の行は後ろから残す量まで読んだら終わり。
  - 一つ開くたびに提示を組み直して測る。目標量の超過は問題ない。
  - 仕上げに一度だけ、知覚込みの合計を上限と比べ、超えていたら WARNING だけ出す (開いたものは戻さない)。
- **事故2 との関係**: 稟乃さんの実機では**起動直後の読み戻しが v0.2 履歴 212 万字を窓に開いた** (`sluice_coverage_gaps.md` 出自節)。当時これは §15-5 の決定どおりの動き (「記憶を開き直さないくらいなら超過を受け入れる」) で、「次の非常畳みが整理する」前提が下 (スルース) で潰れた。追加の決定 2 で「生の未編纂は塊として一括で開かない」に改めた。
- **追跡できていない境界**: `_read_uncovered_tail_before` / `_history_back_to_folds` / `_write_refill` の実装細部までは読んでいない。`docs/issues/window_refill_rarely_plans_for_starved_personas.md` (旧設計の診断記録) との現況の突き合わせも未了。
- **既存テスト**: `tests/test_sluice_cold_isolation.py::ColdRefillTest`(212 万字の回帰 — 巨大な塊を開かないこと)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (冷たい形の回帰は固定。温かい定常での開き方の網は未確認)

### MEM-17: スルース (押し出される会話からの採取) — 定常運転

- **入口**: 利用者の直接操作ではない。Metabolism の中で、Chronicle 生成後・退場前に走る (`session_lifecycle.py:4777`)。**呼び出し元はこの 1 箇所だけ**。
- **結果**: 押し出される会話を本人の目で読み返し、コア記憶 (`confirmed=0` の自動採取)・手帳のメモ (want/did)・約束 (タスク帳) を書く。**LLM 課金あり** (構造化出力 1 コール)。失敗すると**退場が止まる** (次の Metabolism で再試行)。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/autonomous_behavior_v3.md §13` (正典) と `docs/intent/gold_panning.md` (旧名。設計理由 = キャッシュ経済・pan マーカー・defer-to-hot はそのまま生きている)。
- **現在の挙動 (静的確認)**:
  - 担当範囲はパンマーカーの次から窓の末尾まで。マーカーが窓に無ければ窓全体 (`sluice.py:_compute_span`, `session_lifecycle.py:1168`)。
  - **LLM 入力は担当範囲ではなく提示窓全体**: `_call_sluice_llm` はメインラインと同じ context を組み (`_prepare_context`)、末尾に注入プロンプトを 1 つ足す。`seen_ids = list(presented_ids)` (`sluice.py:2017`)。担当範囲は「対象範囲の通数」としてプロンプトの一文になるだけ (`_scope_sentence`)。
  - 実行台帳で冪等 claim (identity = `persona:span_start_id`)。記録済み結果があれば LLM を呼ばず再適用する (`sluice.py:2178-2215`)。
  - 確定 (マーカー前進 + 台帳 completed) と退場は二段のゲートで検算する — マーカーの新位置以前の窓のメッセージが全件 seen に含まれるときだけ確定、退場計画の対象 ID 全件が seen に含まれるときだけ退場 (`session_lifecycle.py:4810-4840`)。
  - **書き込みの名義**: 機構がコア記憶へ書いた分は `confirmed=0` (ユーザー確認待ち) で、`metadata` に `{"source": "sluice"}` が入る (`sai_memory/core_memory.py:96`)。手帳のメモは `origin` で由来を分ける。
- **追跡できていない境界**: `_apply_core_ops` / `_apply_memos` / `_apply_promises` の CAS (照合値) の全経路。
- **既存テスト**: `tests/test_sluice.py` (3367 行。実 SAIMemory の temp DB + 偽 LLM。コア記憶・手帳・約束の適用、再適用の冪等、退場ゲート、429 とコンテキスト超過の一発送出、defer-to-hot を固定)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### MEM-18: 冷たいときのスルース飛ばしと「通っていない範囲」の記録 ← **事故2 の修正地点**

- **入口**: 自動。Metabolism の中で、担当範囲の保存行の字数が `SAIVERSE_SLUICE_MAX_SPAN_CHARS` (既定 **10 万字**、`sea/sluice.py:106-125`) を超えていたら走らせない (`session_lifecycle.py:4738-4770`)。
- **結果**: スルースを走らせず (LLM 課金なし)、**退場はそのまま進める**。窓から出て行く未見の範囲を `sluice_skipped_spans` に 1 行書く。記録が書けない回は**退場を見送る (fail-closed)** (`session_lifecycle.py:4844-4858`)。読み口は `GET /{persona_id}/sluice/skipped-spans` (`api/routes/people/sluice.py:119`)。
- **期待の根拠**: `ユーザー原文の引用あり` — `docs/intent/sluice_coverage_gaps.md`「決定」節に 5 件の原文。特に「もう思い切ってキャッシュ冷えてる時のスルースはやらないをデフォルトにして、スルース通ってない範囲に関しては後から通せる仕組みを作る」。追加の決定 1 の「守る対象が『越えさせない』から『見える・直せる』へ移る」も原文ほぼそのまま。
- **現在の挙動 (静的確認)**:
  - 判定は**位置ではなく量** (整合性レビュー c-1 — 位置で引くと一度飛ばした後にマーカーが永久に窓の外になる)。
  - 起点前進 (機構1) の側でも、マーカーを越える前進は範囲を記録してから進める (`session_lifecycle.py:947-980`)。パンマーカーが無いペルソナは旧起点〜新起点の全部を未見として記録する。
  - `_CONTEXT_OVERFLOW_MARKERS` (429 の "Request too large" を誤分類していた文字列一覧) は**機構ごと撤去**された (intent の経緯節、差分 (2))。
  - **UI の入口はまだ無い** — `sluice` を参照する frontend ファイルは 0 件 (`ArasujiViewer.tsx` の `sluice_unseen` は別概念のエラー文言)。第二段の期間選択 UI が未実装。
  - **context-status への一行も見送られている** (intent の経緯節 差分 (3)、`api/routes/people/context_status.py` に sluice の語なし)。
- **事故2 がここでどう現れるはずだったか**: 事故は「大量の未整理履歴をコンテキストに読み込んだ状態で、スルースにも全量を読ませてしまう」。この項目は**その入口を量で塞ぐ**もの。事故当時はこの判定が存在せず、代わりに旧 §13.5-1 の後退方式 (直近 1〜2 通を外して再試行) が 212 万字に対して空転していた。
- **なぜ既存の検査で捕まらなかったか (静的に読める範囲の事実)**:
  - 事故前の `tests/test_sluice.py` は**窓が普通の大きさである形しか作っていない**。担当範囲や窓の巨大さは変数になっていなかった (現在の「冷たいときの飛ばし」の項は 2026-09-08 の修正と同時に追加されたもの)。
  - 「読み戻し (MEM-16)」と「スルース (MEM-17)」は別ファイル・別テストで、**両者を同じ走行で繋ぐ試験が無かった**。事故は「読み戻しが開いた量が、そのままスルースの入力になる」という接点で起きている。
  - 429 の本文がコンテキスト超過判定の文字列一覧に一致するという誤分類は、**プロバイダの実エラー文面を材料にした試験が無い**限り静的にも動的にも出てこない (現在は `RateLimitMisclassificationTest` が回帰として立っている)。
- **追跡できていない境界 / 疑義**: **量の判定の主語と、実際に送る量の主語が違う**。判定するのは「担当範囲 (未見の窓メッセージ) の保存行の字数」だが、実際に LLM へ送るのは**提示窓全体 + head** (MEM-17 の「LLM 入力は担当範囲ではなく提示窓全体」)。マーカーが窓の途中にあり未見部分だけが小さい場合、判定は通って巨大な窓が送られうる。intent 自身も判定の主語を「担当範囲」と書いている (第一段 A) ので、**設計とコードは一致している**。ここで指摘するのは「この数値が守っている対象は送信量ではない」という事実であって、実害の有無は静的には確定できていない。
- **既存テスト**: `tests/test_sluice.py`(飛ばしと記録の単体), `tests/test_sluice_cold_isolation.py::ColdMetabolismTest`(v0.2 形の実 DB で、スルースの LLM が一発も飛ばず・退場が進み・範囲が記録されること。**修正を外すと全量が開く実証つき**), `tests/test_session_anchor_rows.py`(起点前進側の記録、`:1219-1400`)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓` (ただし判定の主語と送信量の関係は未検査)

### MEM-19: 後から通す採取ジョブ (機構モード / 本人モード)

- **入口**: **API のみ。UI の入口は無い** (`api/routes/people/sluice.py:6`「第一段では UI の入口を作らない — 第二段 (期間選択 UI) がこの読み口を使う」)。`POST /{persona_id}/sluice/capture` (`:367`、`dry=true` で見積もりのみ)、cancel (`:417`)、status (`:436`)。
- **結果**:
  - **機構モード (既定)**: 誰でもない機構が範囲を読み、手帳のメモ**候補**を `sluice_candidate_memos` へ置く。**本人の器 (コア記憶・手帳・約束・会話ログ) には何も書かない**。採用・却下は第二段の UI。
  - **本人モード**: いまの本人に「読み返し」と明示して渡す。過程は discardable、本線には**ダイジェスト一行だけ**。
  - どちらも**LLM 課金あり** (チャンクごと 1 コール)。
- **期待の根拠**: `ユーザー原文の引用あり` — `sluice_coverage_gaps.md` B 節。最初の実装 (`3bfd0919`) が「第三の主体をでっち上げて『本人の目』と呼んでいた」としてまはーに棄却された経緯と、三候補 (機構 / 過去の本人 / 現在の本人) の裁定が原文つきで記録されている。
- **現在の挙動 (静的確認)**:
  - チャンクは A と同じ閾値 (`get_max_span_chars`) 以下に刻む (`sluice.py:3498`)。チャンクを終えるたびに `sluice_skipped_spans` の行を縮める (中断しても続きから)。**パンマーカーは動かさない**。
  - **名義の規律 (点 4 の該当箇所)**: 本人モードのダイジェスト一行は `role="user"` + `<system>` 包み + タグ `[DIGEST_TAG, "sluice"]` で書かれる。コメントに理由が明記されている — 「role は作業セッションの digest (assistant = 本人の言葉) と違って user + `<system>` 包み — この一行は件数から機械が組んだ文で、機構の代筆を本人名義 (assistant) にしない (発話の尊厳の規律)」(`sea/sluice.py:3456-3484`)。
  - 機構モードのプロンプトは**本人のシステムプロンプトを着せない**。「あなたはこの会話の当事者ではありません」と明示し、日付は LLM に申告させず範囲のメッセージから機械が刻印する (`sluice.py:3079-3126`)。
  - 拾われたメモは `origin='readback'` (本人モード) / `'mechanism'` (機構モードからの採用) と `event_date` (機械刻印) を持つ。
- **追跡できていない境界**: 候補の採用・却下の操作 (第二段の UI) が無いので、`sluice_candidate_memos` に置かれた候補が実際に手帳へ入る道は**現時点で存在しない**。読み口 (`GET .../sluice/candidate-memos`) は API にあるが frontend から呼ばれていない。
- **既存テスト**: `tests/test_sluice_capture.py`(`CapturePlanTest` / `CaptureRunTest` / `CaptureApplyTest` / `MechanismModeTest` / `CaptureApiTest`。実 DB + 偽 LLM。閾値の遵守・機構名義の行を読ませない・パンマーカー不動・origin と event_date の刻印・discardable・中断からの再開・完了で行が消えることを固定)。`tests/test_sluice_cold_isolation.py::ColdCaptureTest`(記録された範囲を両モードで完走)。
- **状態**: `機能の存在=✓`(API のみ) `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### MEM-20: レート制限 (429) の小休止

- **入口**: 自動。スルース / 編纂の LLM 呼び出しがレート制限で失敗したら発火 (`session_lifecycle.py:5120 _note_metabolism_rate_limit`, `:43 _rate_limit_cooldown_seconds`)。
- **結果**: そのペルソナの Metabolism の LLM 呼び出しを一定時間 (既定 600 秒) 見送る。非常畳みも小休止中は `skip` で引き返す (`:2792`)。
- **期待の根拠**: `既存の仕様文書` — `sluice_coverage_gaps.md` 第一段 C-1「走行単位で止めるだけだと、自律の Pulse ごとに 3 回ずつの 429 が続く (整合性レビュー c-4)。これで夜通しの連打は『10 分に 3 回』まで落ちる」。
- **現在の挙動 (静的確認)**: `_is_rate_limit_error` で判定し、`_metabolism_rate_limit_active` が入口を止める。intent は「ゼロにする最後の受け皿は既存構想の送信量の安全弁 (ideas.md) で、本設計の範囲外」と明記。
- **追跡できていない境界**: 画面への表出。スルースの失敗は画面に出ない (事故の記録: 「朝の発話が受けた 429 だけが『APIの利用制限に達しました』として画面に出た」)。小休止中であることを利用者が知る道は静的には見つけられなかった。
- **既存テスト**: `tests/test_sluice_cold_isolation.py::RateLimitMisclassificationTest`(429 が一発で走行を閉じ、小休止が次の入口を止めること), `tests/test_sluice.py`(429 の一発送出), `tests/test_sluice_capture.py`(小休止中は走行を閉じる)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### MEM-21: Chronicle 診断レポート

- **入口**: 「デバッグ」タブ (`MemoryRecall.tsx`) → `GET /{persona_id}/arasuji/diagnosis` (`arasuji.py:387`)。
- **結果**: 読み取り専用・LLM ゼロ。レベル別の件数・未統合数・進捗・帯のシミュレーション (予算・実寸・レベル別内訳・可視エントリ全件)・孤児参照の内訳。
- **期待の根拠**: `既存の仕様文書` — `chronicle_coverage_gaps.md` 機構 G「診断ツールには孤児参照のエントリ単位の内訳を追加する」。
- **現在の挙動 (静的確認)**: 帯のシミュレーションは `exclude_entry_ids` を渡さない (サーバーの実行時状態に依存させないため)。応答自身が `excludes_presented_digests: False` でそれを申告する (`arasuji.py:322-347`)。予算の解決元 (`persona_column`/`env`/`env_budget_disabled`/`builtin_default`) をラベルで返す。
- **追跡できていない境界**: 診断結果を frontend がどう描いているか。
- **既存テスト**: `tests/test_arasuji_diagnosis_api.py`(「材料が出ること」と「数が互いに整合すること」だけを固定。値の良し悪しは判断しない)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### MEM-22: Memopedia ページの閲覧・編集・履歴・ロールバック・削除

- **入口**: 「Memopedia」タブ (`memory/MemopediaViewer.tsx`)。API は `memopedia.py:67`(tree), `:91`(page), `:116`(history), `:142`(rollback), `:170`(put), `:233`(delete), `:308`(新規), `:346`(trunks), `:375`(trunk 変更), `:412`(important), `:443`(desk), `:486`(move), `:504`(unorganized)。
- **結果**: memory.db の Memopedia ページを変更する。LLM 課金なし。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/memopedia.md`。
- **現在の挙動 (静的確認)**: 編集は履歴 (`edit_source`) を残し、ロールバックできる。
- **追跡できていない境界**: 1086 行のうち tree / unorganized の組み立てロジックは読んでいない。
- **既存テスト**: `tests/test_memopedia_rollback.py`, `test_memopedia_atomic_writes.py`, `test_memopedia_root_trunk.py`, `test_memopedia_category_registry.py`, `test_memopedia_index_toggle.py`, `test_uri_resolver_memopedia.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` `テスト対応=✓`

### MEM-23: Memopedia ページの生成 (Deep Research 風)

- **入口**: Memopedia タブ / デバッグタブ → `POST /{persona_id}/memopedia/generate` (`memopedia.py:687`)、status は `:741`。
- **結果**: キーワードから想起 → 文脈拡張 → LLM で知識抽出 → 十分性判定 → 繰り返し → ページ保存。**LLM 課金あり** (`max_loops` 回まで)。
- **期待の根拠**: `実装のみ (根拠なし)` — docstring が 6 段の手順を書くだけ。intent 側の対応文書を特定できていない。
- **現在の挙動 (静的確認)**: 背景ジョブ、プロセス内メモリの台帳。`db_lock` を渡してアダプタの錠前で書く。
- **追跡できていない境界**: `_run_memopedia_generation` の中身。
- **既存テスト**: `tests/test_build_memopedia_core.py`(CLI 側)。この API を通す試験は特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△` `テスト対応=△`

### MEM-24: Memopedia をログから一括構築 (entity 抽出)

- **入口**: 「デバッグ」タブ → `POST /{persona_id}/memopedia/build-from-logs` (`memopedia.py:1046`)。
- **結果**: メッセージをバッチ処理して entity を抽出し、Memopedia ページへ反映 (新規作成 or 追記)。**LLM 課金あり**。
- **期待の根拠**: `実装のみ (根拠なし)`。
- **現在の挙動 (静的確認)**: `start_after` / `start_after_rowid` で途中から再開できる (`tests/test_memopedia_rebuild_cursor.py` が対応)。
- **追跡できていない境界**: 抽出の失敗と付箋 (backlog) の関係。
- **既存テスト**: `tests/test_memopedia_rebuild_cursor.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△` `テスト対応=△`

### MEM-25: Memopedia のエクスポート / インポート / 全ページ削除

- **入口**: `GET .../memopedia/export` (`:258`), `POST .../memopedia/import` (`:270`), `DELETE .../memopedia/pages` (`:292` 全削除)。
- **結果**: ページ一括の入出力。全削除は破壊的。
- **期待の根拠**: `実装のみ (根拠なし)` — user-guide に記述を見つけられなかった。
- **現在の挙動 (静的確認)**: ルートの存在を確認。中身は未読。
- **追跡できていない境界**: 全削除が Fragment・クリップ・机の参照に何を残すか。
- **既存テスト**: 該当を特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✗` `テスト対応=✗`

### MEM-26: Memopedia 本文 → Fragment 変換 (v0.2.x → v0.3.x)

- **入口**: 「デバッグ」タブ → `MemopediaConversion.tsx` (`MemoryRecall.tsx:1111` からマウント)。
- **結果**: 機構が書いた行を Fragment へ移し、本文には人が書いたものだけを残す。**判定は三段で、保留行は 1 行ずつユーザーが決める**。取り消し機構が保険として存続。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/memopedia_body_to_fragment.md` (ステータス: **完了**、2026-08-06 本番 aifi_city_a へ適用済み。294 ページ / Fragment 1893 件 / dedup 0 の検算記録あり)。
- **現在の挙動 (静的確認)**: 「自動マイグレーションにしない。ユーザーが自分で押す機能として作る」という設計どおり、デバッグタブに置かれている。
- **追跡できていない境界**: 変換エンジン (`sai_memory/memopedia/body_to_fragment.py` 1535 行) の中身。
- **既存テスト**: 名前で特定できるものは見つけられなかった (`test_p4d_memopedia_index_section.py` は別件)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` `テスト対応=✗`

### MEM-27: Fragment の自動生成 (Metabolism 相乗り)

- **入口**: 自動。Chronicle 生成のチャンク確定ごとに `entity_extractor` の `batch_callback` が発火する (`session_lifecycle.py:5993-6006`)。束ね側でも「恒等圧縮の子が初めて要約に変わる束ね」で発火する (`bands.py` の `batch_callback`)。
- **結果**: 会話に出た固有の対象を Memopedia ページ / Fragment として整理する。**LLM 課金あり** (抽出コール)。
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md §5`「Fragment の生成タイミング（検証済）: Metabolism 発火時に Chronicle 生成チャンクへ entity_extractor が batch_callback として相乗りする」。
- **現在の挙動 (静的確認)**: 抽出の失敗は Chronicle の成否に畳み込まない。失敗した entry id は「付箋 (backlog)」に記録して次回の Metabolism の頭で拾い直す。**付箋にも残せなかった分は自動では拾い直されず**、その旨を別文面で言い分ける (`session_lifecycle.py:6363-6382`, `:6413-6426`)。
- **追跡できていない境界**: `entity_extractor.py` (1374 行) の抽出の中身と、`docs/issues/entity_extraction_empty_result_variance_is_undetectable.md` (未解決) の現況。
- **既存テスト**: `tests/test_metabolism_two_layer.py::ExtractionBacklogRecoveryPointTest`(拾い直しの発火点)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

### MEM-28: Memopedia の編纂 (分割・統合) — **v0.3 では発火しない**

- **入口**: 判断点 `judgment_day_close` の finalize が approve した op を `curation_ops.enqueue_plan` で積み、背景スレッドが `run_pending_plans` で実行する (`builtin_data/tools/judgment_finalize.py:858`, `:1186`)。
- **結果**: ページの分割 (LLM でブロック割当ラベルのみ、保存則の機械検証あり) と統合 (完全決定論・LLM ゼロの逐語連結)。1 プラン = 1 トランザクション。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/concept_consolidation.md` が正典 (landscape §5 が参照)。
- **現在の挙動 (静的確認)**: **`saiverse/autonomy_wiring.py:91` の `AUTONOMOUS_DRIVING_SHIPPED = False` により、v0.3 では判断点が発火しない**。したがってこの経路は稼働していない。手動 CLI だった `scripts/maintain_memopedia.py` は 2026-08-05 に削除済み (landscape §5)。
- **追跡できていない境界**: judgment 経路以外に `run_pending_plans` を呼ぶ道があるか (grep では見つけられなかった)。
- **既存テスト**: `docs/issues/curation_duplicate_pages_loop.md` / `curation_plan_double_execution.md` が未解決として残る。テストファイル名で特定できるものは見つけられなかった。
- **状態**: `機能の存在=✓`(コードは在る) `期待の根拠=✓` `挙動の静的確認=✓`(発火しないことを確認) `テスト対応=✗`

### MEM-29: 想起 (セマンティック検索) のデバッグ

- **入口**: 「デバッグ」タブ (`MemoryRecall.tsx`) → `POST /{persona_id}/unified-recall` (`recall.py:497`)。`POST .../recall` (`:15`) と `.../recall-debug` (`:66`) も存在するが、**`recall-debug` は frontend から呼ばれていない**。
- **結果**: 検索クエリを投げて、何が想起されるかを見る。埋め込みはローカル (課金なし)。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/memory-view.md`「セマンティック想起のテスト（検索クエリを投げて確認）」。
- **現在の挙動 (静的確認)**: `sai_memory/unified_recall.py` (1179 行) が Chronicle / Memopedia / Fragment / 生ログを横断する。
- **追跡できていない境界**: 経路ごとの機構名義タグの除外一覧が揃っていない (未解決 issue `docs/issues/semantic_recall_mechanism_tag_exclusion_inconsistent.md`)。
- **既存テスト**: `tests/test_unified_recall.py`, `test_recall_walk.py`, `test_auto_recall.py`, `test_recall_on_enter_gate.py`, `test_recall_on_enter_user.py`, `test_copresence_recall.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` `テスト対応=✓`

### MEM-30: ワーキングメモリ (想起した ID) の閲覧・操作

- **入口**: API は 4 本 (`working_memory.py:41`/`:55`/`:77`/`:89`)。`MemoryRecall.tsx` が `working-memory` を参照している。専用の `WorkingMemoryViewer.tsx` は存在するが**どこからも import されていない**。
- **結果**: 想起して机に載っている ID の一覧・追加・削除・全消去。
- **期待の根拠**: `実装のみ (根拠なし)`。
- **現在の挙動 (静的確認)**: ルートと `get_adapter` 経由の読み書きを確認。
- **追跡できていない境界**: `MemoryRecall.tsx` がどの操作まで露出しているか。
- **既存テスト**: `tests/test_working_memory_recalled.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△` `テスト対応=✓`

### MEM-31: メモリーノート (旧・知識メモ) の一覧・解決

- **入口**: API は `memory_notes.py:41`/`:73`。**`MemoryNotesViewer.tsx` はどこからも import されていない** → 画面に出る道が無い。
- **結果**: 未解決の知識メモの一覧と、解決済みへの一括更新。
- **期待の根拠**: `実装のみ (根拠なし)`。
- **現在の挙動 (静的確認)**: **書き手が退役している**。`sai_memory/memory/entity_extractor.py:3` が「Replaces the old note_extractor + note_organizer pipeline」と明記し、`note_extractor` / `note_organizer` / `note_executor` を呼ぶのは `scripts/extract_memory_notes.py` / `scripts/organize_memory_notes.py` の CLI だけ (grep で確認)。
- **追跡できていない境界**: 既存ペルソナの DB に残っている旧ノート行の扱い。
- **既存テスト**: `tests/test_memory_notes.py`。
- **状態**: `機能の存在=△`(API とテーブルは在るが UI から到達不能・書き手は退役) `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✓`

### MEM-32: 埋め込みの再生成 (reembed)

- **入口**: 「インポート」タブ (`memory/MemoryImport.tsx`) と `frontend/src/app/page.tsx` → `POST /{persona_id}/reembed` (`reembed.py:120`)、status (`:150`)。
- **結果**: 未埋め込み (または `force` で全件) のメッセージに埋め込みを張り直し、`embed_metadata.embed_model` に現在のモデル名を記録する。**ローカル埋め込みのみで API 課金なし**。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/memory-view.md`「エンベディング管理（未作成メッセージへの埋め込み生成）」/ `memory-migration.md`。
- **現在の挙動 (静的確認)**: 背景タスク、10 件ごとに進捗を更新。別に `SessionLifecycle.ensure_recall_embeddings` が Chronicle / ページ / Fragment の未埋め込みを Metabolism のたびに**無条件で**埋める (`session_lifecycle.py:6445-6452`)。
- **追跡できていない境界**: 埋め込みモデルが変わったときの起動時の扱い。
- **既存テスト**: `tests/test_saiverse_memory_adapter.py`, `test_sai_memory_chunking.py`。reembed の route を通す試験は特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

### MEM-33: ChatGPT 公式エクスポートの取り込み

- **入口**: 「インポート」タブ → ファイルを上げて `POST .../import/official/preview` (`import_chatlog.py:239`) → 会話を選んで `POST .../import/official` (`:301`) → `GET .../import/official/status` (`:372`)。
- **結果**: 選んだ会話を memory.db の `threads`/`messages` へ挿入し、埋め込みを張る (`skip_embedding` で省略可)。**LLM 課金なし**。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/memory-migration.md`「取り込みの際に呼ばれるのは『文章をデータベースに保存する』処理と『あとで検索できるように索引を作る』処理だけで、費用のかかる AI 呼び出しは一切行われない」。
- **現在の挙動 (静的確認)**: preview はパース結果をプロセス内キャッシュ (`_chatgpt_export_cache`) に置き、`cache_key` で本実行へ渡す。上限 2GB (`CHATLOG_IMPORT_MAX_BYTES`)。`ensure_persona_exists` で未知 ID を弾く (孤児 memory.db を作らないため)。プレースホルダ題名 (`(untitled)`/`Untitled`) はスレッド名として保存しない (`:29-40`)。
- **追跡できていない境界**: 取り込み完了後に Chronicle 化 (MEM-11) へ誘導する導線が**無い** — 未解決 issue `docs/issues/import_flow_lacks_chronicle_cta.md` (2026-09-01、まはーが「Chronicle を作る場面が無いまま完了した」と気づいた)。
- **既存テスト**: `tests/test_chatgpt_importer.py`, `test_import_chatlog_titles.py`, `test_chatlog_markdown_import.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### MEM-34: 拡張機能エクスポートの取り込み

- **入口**: 「インポート」タブ → `POST .../import/extension` (`import_chatlog.py:379`)、status (`:432`)。
- **結果**: MEM-33 と同じ器へ挿入。
- **期待の根拠**: `利用者向け説明` — `memory-migration.md` (ただし本文は CLI スクリプトの手順を主に書いている)。
- **現在の挙動 (静的確認)**: 背景タスクでパース → 挿入 → 埋め込み。
- **追跡できていない境界**: パーサ (`chatlog_exporter_importer`) の対応範囲。
- **既存テスト**: `tests/test_chatlog_markdown_import.py`, `test_legacy_*` 系 (`docs/issues/legacy_cursor_import_failure_is_silent.md` ほか 3 件が未解決)。
- **状態**: `機能の存在=✓` `期待の根拠=△`(CLI 手順との対応が曖昧) `挙動の静的確認=△` `テスト対応=△`

### MEM-35: ネイティブ (SAIMemory) 形式のエクスポート / インポート

- **入口**: `GET .../threads/{thread_id}/export-native` (`native_export_import.py:36`), `POST .../import/native` (`:139`), `.../preview` (`:230`), status (`:214`)。
- **結果**: メタデータを保ったままスレッドを丸ごと書き出し / 取り込む。上限 256MB。
- **期待の根拠**: `実装のみ (根拠なし)` — user-guide には CLI (`scripts/export_saimemory_native.py` / `import_saimemory_native.py`) の記述しか見つけられなかった。
- **現在の挙動 (静的確認)**: `saiverse_memory/native_export.py` が書き出しを持つ。
- **追跡できていない境界**: 取り込み時の ID 衝突の扱い。
- **既存テスト**: `tests/test_native_import_separation.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△` `テスト対応=✓`

### MEM-36: 経験の台帳 — **画面から隠されている**

- **入口**: `MemoryModal.tsx` の `HIDDEN_TABS` に入っている (`:35`)。API (`experience_ledger.py:99`, `:132`) と `ExperienceLedgerViewer.tsx` は生きている。
- **結果**: 目的ノード・テーマページ・実体ページの索引と、動的合成ページ (読み取り専用)。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/experience_ledger.md §3`。隠す理由もコードのコメントに書かれている: 「台帳に『経験値ノート』を書くのはコマ締め (`saiverse/slot_close.py`) で、自律行動の止め具 (`AUTONOMOUS_DRIVING_SHIPPED = False`) があるあいだコマは発火しない。書き手が動かないので、台帳は Memopedia の焼き直ししか映さない。復帰条件: 自律行動 (時間割のコマ) の出荷時」(`MemoryModal.tsx:26-33`、2026-09-01 まはー裁定)。
- **現在の挙動 (静的確認)**: 隠す判断と理由・復帰条件がコードに残されている。
- **追跡できていない境界**: なし (隠されている理由まで明示されている)。
- **既存テスト**: 特定できていない。
- **状態**: `機能の存在=✓`(隠し) `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### MEM-37: 7 層ストレージの読み口 — **UI から到達不能**

- **入口**: API のみ (`storage_layers.py:246` 一覧, `:426`/`:462`/`:494`/`:531` 削除系)。**frontend に `storage-layers` を呼ぶコードは 0 件**。
- **結果**: 層別のメッセージ / イベント / 判断ログの正規化ビューと、一括削除。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/memory_modal_legacy_tabs_retirement.md` (ステータス: **完了 2026-07-16**)。「7層ストレージ」タブは意図的に退役させた。
- **現在の挙動 (静的確認)**: ルーターは今も登録されている (`api/routes/people/__init__.py:65`)。docstring は層 [4] が揮発・[7] が未実装であることを書く。
- **追跡できていない境界**: 削除系エンドポイント (`_BulkDeleteIdsRequest`) の呼び出し元。
- **既存テスト**: 特定できていない。
- **状態**: `機能の存在=△`(API のみ・UI 退役済み) `期待の根拠=✓`(退役の intent) `挙動の静的確認=△` `テスト対応=✗`

### MEM-38: 送信量の管理 (context-status)

- **入口**: チャットオプションの「データ送信量の管理」→ `GET /{persona_id}/context-status` (`context_status.py:25`)。
- **結果**: 水位 (上限 / 残す量)、次に送られる提示量の三分割 (会話 / 機構名義の行 / 知覚)、`window_rows_chars`、`perception_over_budget`、`fold_unit_chars`、`fold_ready`。読み取りのみ。
- **期待の根拠**: `既存の仕様文書 + ユーザー裁定の記載` — `docs/intent/arasuji_levels.md §9`「二数の主語 (2026-09-03 まはー裁定)」。上限 = 実際に送る合計 / 残す量 = 会話の行の量。
- **現在の挙動 (静的確認)**: §15 読み戻しの読み取り専用計画を再利用して「次に話しかけた時に実際に送られる量」を測る (プレビューと同じ値にするため)。
- **追跡できていない境界**: **スルースを飛ばしたことはここに出ない** (intent の経緯節 差分 (3) が「見送った — 第二段 UI の領分」と明記。コードにも sluice の語なし)。
- **既存テスト**: 特定できていない (`docs/issues/watermarks_unsatisfiable_when_perception_is_large.md` に関連)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### MEM-39: 建物ログの隔離と復元 (QuarantineModal)

- **入口**: `SystemAlertBanner.tsx` の警告 → `QuarantineModal.tsx` → `GET/POST /api/system/quarantine*` (`api/routes/system.py:198`, `:226`)。
- **結果**: 破損した建物の `log.json` を隔離し、バックアップから復元する。**対象は建物履歴であってペルソナの memory.db ではない**。
- **期待の根拠**: `実装のみ (根拠なし)` — `docs/issues/quarantine_path_dead_code_removal.md` が未解決として起票されている。
- **現在の挙動 (静的確認)**: 隔離中の建物のチャット履歴は空 + `quarantined: true` フラグで返る (`api/routes/chat.py:289`)。
- **追跡できていない境界**: 復元の実処理。
- **既存テスト**: 特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△` `テスト対応=✗`

### MEM-40: 記憶 DB の起動時バックアップ

- **入口**: 起動時。`SAIMEMORY_BACKUP_ON_START` / `SAIVERSE_DB_BACKUP_ON_START` / `SAIVERSE_DB_BACKUP_KEEP` (CLAUDE.md「Other setups」)。実体は `sai_memory/backup.py` (476 行)。
- **結果**: `~/.saiverse/backups/saimemory_simple/<persona_id>/` 等へコピーを取る (handoff ① に実例: 「事前バックアップは自動バックアップ (`~/.saiverse/backups/saimemory_simple/persona_3_city_a/` の 09-07 19:16) に有る」)。
- **期待の根拠**: `既存の仕様文書` — `docs/reference/scripts.md` (未読)。
- **現在の挙動 (静的確認)**: ファイルの存在と env 名を確認した。世代管理・検証の実装は未読。
- **追跡できていない境界**: バックアップの検証 (取れたことの確認) がどこで行われるか。
- **既存テスト**: 特定できていない。
- **状態**: `機能の存在=✓` `期待の根拠=△` `挙動の静的確認=✗` `テスト対応=✗`

### MEM-41: 実行台帳による編纂・採取の冪等 claim

- **入口**: 自動。`generate_chronicle` は `kind="metabolism.run"` で claim する (`session_lifecycle.py:5272-5276`)。スルースは `(persona, span_start_id)` を identity にする (`sluice.py:2178`)。
- **結果**: 同じ範囲を二重に編纂・採取しない。失敗した実行は再試行できる (failed 行のキーを退避して新規 prepared を作る)。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/execution_ledger.md` / `beat_execution_context.md §3.2`「編纂は persona に一度」。
- **現在の挙動 (静的確認)**: **2026-09-08 に、全削除後の再編纂が completed 行に永久に塞がれる欠陥が実機で見つかり、claim 時に鍵を退避する `supersede_completed` が入った** (`docs/issues/chronicle_delete_all_then_recompile_blocked_by_ledger.md`、intent `execution_ledger §11.2`)。キャンセルは `completed` で封印せず `failed` 終端にする (`session_lifecycle.py:6255-6268`)。
- **追跡できていない境界**: スルースの台帳が `unknown` (結果不明) になったときの利用者の捌き口が**無い** (未解決 issue `docs/issues/sluice_ledger_unknown_needs_user_facing_resolution.md`、まはー「それユーザーどうやって捌くんすか」)。
- **既存テスト**: `tests/test_metabolism_two_layer.py::ChronicleClaimTest`。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---


### 矛盾・疑義

**① `docs/user-guide/memory-view.md` のタブ表が実装と合わない (依頼元の予備調査の確認)**
- 文書は「7層ストレージ」「Tracks」を現役タブとして書く (`memory-view.md §タブ`)。
- 実装 (`MemoryModal.tsx:22`) のタブは browser / core_memory / pocketbook / arasuji / memopedia / experience / pulse_timeline / import / debug で、**コア記憶と手帳が文書に無く、7層ストレージと Tracks は実装に無い**。
- `docs/intent/memory_modal_legacy_tabs_retirement.md` は 2026-07-16 に完了として「7層ストレージ」「Tracks」の退役を記録している。文書 (user-guide) だけが取り残されている。
- さらに文書は「Pulse タイムライン」を現役として書くが、実装では `HIDDEN_TABS` に入っている。

**② `docs/overview/landscape.md §5` の Chronicle 段落が旧世代の説明のまま**
- landscape は「digest 確定済み episode は恒等転写（LLM なし）、digest の無い範囲は…1000 字未満の豆粒は恒等圧縮（生のまま）」「次数 k ≒ 10^k 万字」と書く。
- `docs/intent/arasuji_levels.md §5 消える概念` は「恒等転写 / 転写 (CHUNK_EPISODE_DIGEST)」を消える概念に挙げ、§9 はレベル1 以上の予算を「上限 5 千字・残す量 2.5 千字」と定める。
- コードは `sai_memory/arasuji/alignment.py:38` に `CHUNK_LLM_BATCH = "batch"` の 1 種しか持たず、`:107` に「kind: 常に CHUNK_LLM_BATCH」と書かれ、`:20` に「**小さくても要約する** (生ログを生のまま一次あらすじの席に置く恒等圧縮は…)」とある。
- どちらが「今の正」かはここでは断定しない。3 者 (landscape / intent / コード) のうち intent とコードが一致し、landscape が離れている、という事実だけを残す。

**③ 全量再編纂 CLI が「束ねをチャンクごとに挟む」規則を満たしていない**
- `arasuji_levels.md §3-2` (2026-09-03 まはー裁定): 「束ねはチャンク確定のたびに挟む。…束ねを走行の最後に一度だけ行うと、**大量編纂 (数万通のインポート修復) の後半チャンクは直前 20 件のレベル1 しか見えず**、それより前の流れを失う」。
- `api/routes/people/arasuji.py:1088` は「全量再編纂は `scripts/arasuji/build_arasuji_core.py` の領分」と書く。つまり大量編纂の担当はこの CLI。
- ところが `scripts/arasuji/build_arasuji_core.py:906-911` の `execute_plan` 呼び出しは **`after_chunk` を渡していない** (`execute_plan` の既定は `None`、`sai_memory/arasuji/executor.py:321`)。束ねは `:915-928` の末尾ループでのみ走る。
- これが実害を生んでいるかは静的には確定できない (呼び直しループは 2026-09-09 に入っている)。ただし裁定が名指しした状況 = 大量編纂 の担当経路で、裁定が要求する挟み込みが行われていない、という食い違いは事実として残る。

**④ 「レベル2 が何本できるべきか」の契約が、量 (字数) でしか書かれていない**
- `arasuji_levels.md §9` はレベルごとの上限と残す量を字数で定める。§3-2 は畳み 1 回の材料上限を字数で定める。**件数の契約はどこにも無い**。
- 実装では、実際に作る本数は `plan_band_overflow` の dry 予測 = 承認済み予算 `max_folds` が上限として決める (`bands.py:1259`)。dry が少なく数えれば実行はそれ以上作らない。
- したがって「dry の数え方の欠陥」は**そのまま「あらすじの本数の欠陥」になり、かつどこの契約にも違反しない**。事故1 はこの形で起きた。
- 「これは仕様どおりか」を断定はしない。事実として、字数の契約と実際の生成本数の間に、dry 予測という検査されていない橋が 1 本だけある。

**⑤ スルースの量の判定の主語と、実際の送信量の主語が違う**
- 判定: 「担当範囲 (パンマーカーから窓の末尾まで) の保存行の字数」(`session_lifecycle.py:4741-4745`、intent 第一段 A も同じ主語)。
- 実際の LLM 入力: 提示窓全体 + head (`sluice.py:_call_sluice_llm` が `runtime._prepare_context` を通し、`seen_ids = list(presented_ids)`)。
- 設計書とコードは一致している。指摘するのは「10 万字という数が守っている対象は送信量ではない」という事実で、実害の有無は静的には確定していない。

**⑥ 見積もりの「まとめる作業 M 回分」と、実行が使う予算が別計算**
- バナー・確認モーダルの `consolidation_calls` は `estimate.py:214` の `plan_band_overflow`。
- 実行時の `band_plan_count` は `session_lifecycle.py:5606` の `plan_band_overflow` (別の時点・別の `excluded_entry_ids` 解決)。
- 時点ずれの歯止め (`confirmed_unprocessed_messages`) は**メッセージ件数にしか効かない** (`arasuji.py:1315-1323`)。束ね回数がずれた場合の扱いは追えていない。

**⑦ `run_sluice` の docstring に、もう存在しない呼び出し元が書かれている**
- docstring: 「`finalize=True` (既定) は本体内で即確定する (**Memory 窓からの手動生成など直接呼びの経路**)」(`sluice.py:2072-2075`)。
- grep の結果、製品コードの `run_sluice` 呼び出しは `sea/session_lifecycle.py:4777` の 1 箇所のみ (すべて `finalize=False`)。残りはテスト。

**⑧ `generate_chronicle` に、廃止された概念を前提にした分岐コメントが残っている**
- 「全チャンクが恒等転写 / 恒等圧縮 (LLM コストゼロ)。確認ダイアログは LLM コストへの同意なので、コストが無ければ確認なしで直行する」(`session_lifecycle.py:5657-5665`)。
- ②のとおり恒等転写・恒等圧縮は廃止されている。分岐自体 (`estimated_llm_calls == 0` なら確認なし) は今も到達しうる (チャンクが無く束ねも吸収も 0 の回など) が、コメントの根拠は現存しない概念を指している。

---

### 凍結・開発者専用・到達不能・文書のみ

| 対象 | 状態 | 根拠 |
|---|---|---|
| 「経験」タブ (MEM-36) | **画面から隠されている**。コードは生きている。復帰条件 = 自律行動 (時間割のコマ) の出荷時 | `MemoryModal.tsx:26-35` (2026-09-01 まはー裁定) |
| 「Pulse タイムライン」タブ | **画面から隠されている**。復帰条件 = 開発者向け表示の出し分けの器ができたとき | `MemoryModal.tsx:29-33` |
| 「7層ストレージ」「Tracks」タブ (MEM-37) | **退役済み** (2026-07-16)。`storage_layers` の API とルーター登録は残存、frontend からの呼び出しは 0 件 | `docs/intent/memory_modal_legacy_tabs_retirement.md`(ステータス: 完了), `api/routes/people/__init__.py:65` |
| `MemoryNotesViewer.tsx` / `WorkingMemoryViewer.tsx` / `PulseLogsViewer.tsx` | **どこからも import されていない** (画面に出る道が無い)。API は生きている | grep 確認 (frontend/src 全体) |
| メモリーノート (MEM-31) の書き手 | **退役済み**。`entity_extractor` が置き換えた。`note_extractor`/`note_organizer`/`note_executor` を呼ぶのは CLI スクリプトのみ | `sai_memory/memory/entity_extractor.py:3`、grep 確認 |
| `POST .../recall-debug` | frontend からの呼び出しなし (開発者専用) | grep 確認 |
| Memopedia の編纂 (分割・統合、MEM-28) | **v0.3 では発火しない** (`AUTONOMOUS_DRIVING_SHIPPED = False`)。手動 CLI `scripts/maintain_memopedia.py` は 2026-08-05 に削除済み | `saiverse/autonomy_wiring.py:91`, `landscape.md §5` |
| スルースの UI (MEM-18/19) | **第一段では UI の入口を作らない**。読み口と採取ジョブは API だけ。第二段 (期間選択 UI) は intent に設計済み・未実装 | `api/routes/people/sluice.py:6`, `sluice_coverage_gaps.md 第二段` |
| 機構モードの候補 (`sluice_candidate_memos`) | 置かれるが、**採用・却下する道が現時点で無い** (第二段の UI 待ち) | `api/routes/people/sluice.py:150-160` |
| 目的の木 (`persona_task` / `task:N`) | **退役** (2026-08-23)。行は読み取り専用の残置。`memory_read task:N` だけ通る | `landscape.md §5` |
| vividness (鮮度減衰) | **廃止確定** (landscape §9)。`sai_memory/memopedia/vivid_to_desk_migration.py` が移行として残る | `landscape.md §5 実装状況メモ` |
| 恒等転写 / 恒等圧縮 / エピソードの畳み拒否権 | **消える概念** (arasuji_levels §5)。コード上も `CHUNK_LLM_BATCH` 1 種のみ | `arasuji_levels.md §5`, `alignment.py:38` |
| 旧世代の編纂入口 (会話前の起点失効編纂 / セッションクローズの前倒し編纂 / 手動入口の全量編纂) | **撤去済み** (arasuji_levels §13、2026-07-29 裁定)。手動生成のリクエストの旧フィールドは受理して無視 | `arasuji.py:1085`, `arasuji_levels.md §5` |
| 旧 §13.5-1 の後退方式 (コンテキスト超過での再試行) と `_CONTEXT_OVERFLOW_MARKERS` | **機構ごと撤去** (2026-09-08) | `sluice_coverage_gaps.md 経緯節 差分 (2)` |
| スルースのセッションクローズ採取 | **撤去済み** (2026-08-24)。自動実行は Metabolism の一本 | `docs/intent/gold_panning.md` 冒頭注記 |

---

### この領域で「検査が無い」と判断した重要な結果

利用者・ペルソナに見える結果のうち、既存テストが触れていないもの。

1. **「編纂した結果、あらすじの階層が何段・何本できたか」** — 事故1 が現れた場所。計画層 (`test_arasuji_bands.py`) と走行層 (`test_metabolism_two_layer.py`) の両方に試験はあるが、前者は小さい DB での純関数、後者は `plan_band_overflow`/`run_band_overflow` を偽物に差し替えている。**補修 (`test_coverage_repair.py`) と吸収 (`test_arasuji_absorption.py`) は `run_band_overflow` を `lambda *a, **k: 0` に差し替えており (計 8 箇所)、束ねが結果から消えている**。「大量の未編纂ログを補修した結果として階層が育つ」ことを見る試験は無い。

2. **全量再編纂 CLI (`scripts/arasuji/build_arasuji_core.py`) の走行** — `arasuji.py` 自身が「全量再編纂はこの CLI の領分」と書く経路。`tests/` にこの CLI を通す試験を見つけられなかった。2026-09-09 の呼び直しループの修正はこの CLI にも入っているが、対応する回帰試験は `tests/test_metabolism_two_layer.py` 側 (偽物の `run_band_overflow`) にしかない。

3. **「取り込み → 編纂 → 読み戻し → 会話」の一本の旅** — 領域内のどの試験もこの旅を通していない。`test_sluice_cold_isolation.py` が最も近いが、**Chronicle を `CHRONICLE_ENABLED=False` で無効化し** (ファイル冒頭に明記)、**チャット送信〜返答受信を通していない** (非常畳みの入口関数を直接呼ぶ)。インポート API から入る形も無い。

4. **補修の完了文の内訳が、実際に起きた仕事と一致すること** — `test_coverage_repair.py::TestRepairCompletionBreakdown` は内訳の写像を固定するが、束ねが差し替えられているため「まとめ N 件」の N が実際の畳み数と一致するかは検査されていない。バナーの「まとめる作業が M 回分」と完了文の「まとめ N 件」の対応も未検査 (§3 ⑥)。

5. **スルースの失敗・飛ばしが利用者に見えること** — 事故2 の記録に「スルースの失敗は画面に出ない」とある。第一段の実装後も、飛ばしたことは `sluice_skipped_spans` に書かれるだけで、**UI にも context-status にも出ない** (intent が意図的に第二段へ送った)。「見える・直せる」という代替の制約 (追加の決定 1) のうち「見える」側が、現時点ではコード上に実現していない。テスト以前に機能が無い。

6. **メッセージ削除が Chronicle に残す傷** — MEM-01 の削除 API は Chronicle を触らず、孤児参照の掃除は補修経路まで遅延する。削除の時点で利用者に何も伝わらない。「削除したのにあらすじにその話が残る」という利用者から見える結果を検査する試験は見つけられなかった (機構 F の試験は補修経路の内側のみ)。

7. **Memopedia の生成・ログからの構築・エクスポート/インポート・全削除 (MEM-23/24/25)** — LLM 課金を伴う操作を含むのに、API を通す試験を特定できなかった。`test_memopedia_rebuild_cursor.py` が再開カーソルを見るのみ。

8. **`context-status` が返す数字 (MEM-38)** — チャットオプションの「データ送信量の管理」に出る値そのもの。二数の主語 (まはー裁定) を固定する試験を特定できなかった。

9. **起動時バックアップ (MEM-40)** — 事故対応の最後の受け皿 (handoff ① で実際にこれが救いになった)。取れたことの検証を含む試験を特定できなかった。

---

### 付記: 事故二件が「どの項目のどの結果として現れるはずだったか」

依頼の要点なので、項目の中に書いた内容をここで一箇所にまとめる。

**事故1 (レベル2 あらすじが一つしか生成されない)**
- 現れる場所: **MEM-13 の「結果」** (レベル N+1 の並びに何本置かれるか)。利用者から見えるのは MEM-07 (Chronicle タブのレベル別件数) と MEM-11 の完了文 (「まとめ N 件」)。
- 検査対象になるはずだった形: 「大量の未編纂ログを補修 (MEM-11) した結果として、MEM-13 が作る上位あらすじが §9 の予算に収まるまで階層を育てること」。
- 捕まらなかった理由 (静的に読める事実): ①MEM-11 の試験が MEM-13 を偽物に差し替えていた ②MEM-13 の試験は小規模の純関数で、本数の期待値を持たなかった ③実物を繋ぐ唯一の試験は 20 チャンクで「Lv2 = 1 件」を正解として固定していた ④「本数」の契約が正典に無く、dry 予測が実質の唯一の権威だった (§3 ④)。

**事故2 (未整理履歴の全量をスルースに読ませる)**
- 現れる場所: **MEM-16 (読み戻し) の結果が MEM-17 (スルース) の入力になる接点**。利用者から見えるのは「話しかけても返事が来ない」(MEM-14/15 の走行が終わらない) と「APIの利用制限に達しました」。
- 検査対象になるはずだった形: 「巨大な未提示履歴を持つ DB で話しかけたとき、返事が返ること」— つまり**領域内の二機構をまたぐ結果**。
- 捕まらなかった理由 (静的に読める事実): ①MEM-16 と MEM-17 は別ファイルの試験で、同じ走行で繋ぐ試験が無かった ②MEM-17 の試験は窓が普通の大きさの形しか作っていなかった ③429 の実文面を材料にした試験が無く、文字列一覧による誤分類は静的にも動的にも出なかった ④スルースの失敗が画面に出ないため、実機でも「返事が来ない」としか観測できなかった (MEM-20 の「追跡できていない境界」)。
- 現在: ①②③には `tests/test_sluice_cold_isolation.py` (4 クラス 12 本) が回帰として立った。ただしこのテストは **Chronicle を無効化し、チャット送信〜返答受信を通していない** (§5 の 3 番)。④は未解決 (§5 の 5 番)。

---

## 領域 C. 世界 — City / Building / Item / 移動 / Observer / Phenomena (WORLD-01〜44)

> **まはーの一次情報 (2026-09-09、この調査の中間報告に対する応答。原文)**
>
> 「regionとゲーム、FixtureとObserver、この辺は0.4.0以降のリリースにするために意図的に配線を繋いでない。
> ただ、何も書かれてないのは今みたいに混乱を招くから問題だね。」
>
> **該当項目**: `WORLD-18` (Region の作成・編集・削除) / `WORLD-19` (Region スコープの表示) /
> `WORLD-20` (game Region の Ruler 生成) / `WORLD-21` (ゲームの開始・終了) /
> `WORLD-34` (Fixture の作成) / `WORLD-35` (Observer の作成・実行)。
>
> これらが「API はあるのに画面から到達できない」状態にあることを、担当は
> 「凍結とも開発者専用とも書かれていない、ただ到達できない状態」としか記述できなかった
> (コードを読んでも、未完成なのか壊れたのか意図的なのかを区別する材料が無いため)。
> この発言により、**v0.4.0 以降へ回すための意図的な未配線**であることが確定した
> (期待の根拠 = ユーザー原文)。以下の各項目の「期待の根拠」欄はこの注記で上書きされる。
>
> あわせて、**その意図がどこにも書かれていないこと自体が課題として立っている**
> (まはー本人の判断)。今回の調査で担当が判定できなかったことが、その実例になっている。



### WORLD-01: City を作る

- **入口**: ワールドエディタ → City タブ → 新規作成。`POST /api/world/cities` (`frontend/src/components/settings/WorldEditor.tsx:499`, `api/routes/world.py:185`)
- **結果**: `city` 行が 1 件増える。応答文言は「Please restart the application to use it.」で、実際に作った City へ切り替えるには `python main.py <slug>` の起動引数を変える必要がある。slug の文字種検査・大文字小文字を畳んだ重複検査・ポート重複検査・IANA タイムゾーン検査がある。根拠: `manager/admin.py:229-295`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/city_identity.md` §3/§4 (slug=識別子・作成時のみ決定可、name=表示名)。`利用者向け説明` — `docs/user-guide/world-editor.md` §Cities タブ
- **現在の挙動 (静的確認)**: slug は作成時にしか決められない (`CityUpdate` に slug 欄が無い、`api/routes/world.py:40-49`)。作成後の hot reload は `_load_cities_from_db()` のみで、Building も personas も新 City 側には無い。
- **追跡できていない境界**: 2 つ目の City を作った状態で `main.py` を起動し直したときに何が起きるかは静的には追えていない。multi-city は凍結されている (WORLD-44) ので、「作れるが行き来はできない」の実際の見え方が未確認。
- **既存テスト**: `tests/test_city_identity.py` (22 件) — 実 sqlite + TestClient で slug 不変性・表示名編集経路・移行を通す。`tests/test_audit_second_batch_world.py::test_cityname_auto_repair_refused_for_multi_city_db` — 複数 City DB での slug 自動修復拒否。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (slug/表示名は厚い。ポート重複・タイムゾーン検査・作成後の再起動の挙動は未検査)

### WORLD-02: City の設定を編集する (全項目)

- **入口**: ワールドエディタ → City タブ → 保存。`PUT /api/world/cities/{city_id}` (`api/routes/world.py:189`)
- **結果**: 表示名・説明・オンラインモード・UI/API ポート・タイムゾーン・ホストアバター・マップ背景を書き換える。根拠: `manager/admin.py:133-228`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-editor.md` §Cities タブ (「名前・ポート設定を変更」「オンラインモードで起動」)
- **現在の挙動 (静的確認)**: 全フィールド必須の PUT。オンラインモードは SDS 登録の可否につながるが、SDS は landscape §8 で「実質冬眠中」とされている。
- **追跡できていない境界**: ポートを変えたときに稼働中プロセスへ何が起きるかは追えていない。
- **既存テスト**: `tests/test_city_identity.py` に表示名まわりの検査あり。ポート・タイムゾーン更新の検査は見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (表示名のみ)

### WORLD-03: 街マップ画面から街の表示名だけを変える

- **入口**: 街マップ → 見出しをクリックして編集。`PATCH /api/world/cities/{city_id}/name` (`frontend/src/components/CityMap.tsx:596`〜, `api/routes/world.py:193-221`)
- **結果**: `CITYNAME` だけを更新し、前後空白を落として返す。空なら UI は `CITY_SLUG` を代わりに見せる。根拠: `api/routes/world.py:206-213`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/city_identity.md` §4 不変条件 2/3
- **現在の挙動 (静的確認)**: 識別子は受け取らない口なので、この経路から slug は変えられない。
- **追跡できていない境界**: なし (単一の DB 更新)
- **既存テスト**: `tests/test_city_identity.py` に表示名編集経路の検査あり (TestClient 経由)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`
- **注記**: `docs/user-guide/city-map.md` の「編集モード」節はドラッグ配置と背景画像しか書いておらず、この表示名編集に触れていない (§3 に矛盾として記載)。

### WORLD-04: マップの背景画像を設定・削除する (City スコープ / Region スコープ)

- **入口**: 街マップ → 編集モード → 背景画像。`POST /api/media/upload-hires` で保存 → `PATCH /api/world/cities/{id}/map-background` または `PATCH /api/world/regions/{id}/map-background`。根拠: `frontend/src/components/CityMap.tsx:528-590`
- **まとめた根拠**: City 用と Region 用の 2 ルートは、リクエスト型 (`CityMapBackgroundUpdate`)・処理・UI の押しボタンがすべて同一で、フロントは表示中スコープで宛先だけ切り替えている (`CityMap.tsx:529-532`)。利用目的が同じなので 1 項目にまとめた。
- **結果**: `CITY.MAP_BACKGROUND_IMAGE` / `REGION.MAP_BACKGROUND_IMAGE` に URL 文字列を保存。Region 側は保存後に `manager._reload_regions()` を呼ぶが、City 側は呼ばない (次回 city-map fetch が DB から読む)。根拠: `api/routes/world.py:222-246` / `:337-362`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/city-map.md` §編集モード
- **現在の挙動 (静的確認)**: 画像は `upload-hires` (WebP 変換のみ、解像度維持) に載る。削除は `null` を PATCH。
- **追跡できていない境界**: 差し替え前の画像ファイルが消されるかは追えていない (削除処理を見つけられなかった = 残り続ける可能性がある)。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### WORLD-05: City を削除する

- **入口**: ワールドエディタ → City タブ → 削除。`DELETE /api/world/cities/{city_id}` (`api/routes/world.py:247`)
- **結果**: 4 つの拒否条件をすべて通ったときだけ削除。①最後の 1 件は消せない ②稼働中の City は消せない ③Building が残っていたら消せない ④占有ログが残っていたら消せない。根拠: `manager/admin.py:352-383`
- **期待の根拠**: `実装のみ (根拠なし)` — `docs/user-guide/world-editor.md` は削除の制約に触れていない
- **現在の挙動 (静的確認)**: 事前検査で守られており、削除で会話が失われる経路は塞がっている。
- **追跡できていない境界**: `~/.saiverse/cities/<slug>/` のファイル群が削除されるかは追えていない (削除コードを見つけられなかった)。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

### WORLD-06: Building を作る

- **入口 (3 つ)**: ①ワールドエディタ → Building タブ ②左サイドバー「場所」の ＋ ボタン (`frontend/src/components/Sidebar.tsx:257-280`、capacity=10 固定・説明とシステムプロンプトは空) ③ペルソナが `create_building_playbook` を `run_playbook` で呼ぶ (`builtin_data/playbooks/public/create_building_playbook.json`、`router_callable: true`)。いずれも `POST /api/world/buildings` → `manager.create_building`
- **結果**: `building` 行が 1 件増え、`_reload_buildings()` で in-memory の buildings / building_map / capacities / occupants が即時更新される。根拠: `saiverse/saiverse_manager.py:1821-1882`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-editor.md` §Buildings タブ
- **現在の挙動 (静的確認)**: ID は ASCII 文字種を強制し、日本語名は `building_<連番>_<city>` にフォールバックする。大文字小文字を畳んだ重複検査あり (Windows のフォルダ名衝突対策)。根拠: `manager/admin.py:388-455`
  - **文言と挙動のずれ**: 戻り文字列は「A restart is required for it to be usable.」だが、実際には `_reload_buildings()` で即時反映される (`saiverse/saiverse_manager.py:1826-1828`)。サイドバーの＋ボタンはこのメッセージを表示せず `refreshData()` するだけ。
- **追跡できていない境界**: 起動後に作られた部屋は `startup_seq_watermark` に載らない (`manager/initialization.py:228` は起動時の buildings だけを埋める)。転記側はこれを「作られた時点で空だから 0 でよい」と扱う (`builtin_data/tools/get_building_messages.py:353-364`)。この前提が `_reload_buildings` の全経路で成り立つかは追えていない。
- **既存テスト**: `tests/test_building_admin_id.py` — ID 文字種契約を AdminService 直叩きで検査。`tests/test_buildings.py` — Building データクラスのみ。API 経路・サイドバー経路・playbook 経路の検査は見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (ID 文字種のみ。3 つの入口はどれも通していない)

### WORLD-07: Building の設定を編集する

- **入口 (2 つ)**: ①ワールドエディタ → Building タブ ②右サイドバー → 現在地 → Building 設定 (`BuildingSettingsModal.tsx`)。どちらも `PUT /api/world/buildings/{building_id}`
- **結果**: 名前・定員・説明・システムプロンプト・所属 City・ツール紐付け・自動インターバル・内装画像・追加プロンプトファイルを更新。DB 更新後、in-memory の Building オブジェクトも書き換える。根拠: `manager/admin.py:509-580`, `saiverse/saiverse_manager.py:2122-2160`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-editor.md` §Buildings タブ (フィールド表)
- **現在の挙動 (静的確認)**:
  - 「利用可能なツール」欄は `BuildingToolLink` テーブルに書くが、CLAUDE.md と `docs/user-guide/world-editor.md` の注記のとおり**この紐付けでペルソナにツールは届かない**。UI は両画面に残っている (`BuildingSettingsModal.tsx:373-386`)。
  - 更新時に `building.system_instruction = system_instruction` を直接代入する (`saiverse/saiverse_manager.py:2156`)。この属性は `refresh_building_system_instruction` がアイテム一覧を差し込む先だが、**`system_instruction` を読む箇所を repo 内に見つけられなかった** (書き手は `manager/items.py:211-241` と `saiverse_manager.py:2156` の 2 つだけ)。ペルソナに届く Building プロンプトは `base_system_instruction` 側 (`builtin_data/tools/get_visual_context.py:561`, `sea/head_pipeline/sections/building.py:34`, `saiverse/occupancy_manager.py:954`)。→ アイテム一覧の system_instruction 差し込みは現在**読み手のいない書き込み**に見える。
- **追跡できていない境界**: `system_instruction` を動的属性アクセス (`getattr`) で読む経路が無いかは全文検索していない。上の判断は `.system_instruction` の直接参照の検索結果に基づく。
- **既存テスト**: 該当テストなし (`update_building` を通すテストは見つからなかった)
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` (system_instruction の読み手不在が未確定) `テスト対応=✗`

### WORLD-08: Building を削除する

- **入口**: ワールドエディタ → Building タブ → 削除。`DELETE /api/world/buildings/{building_id}` (`api/routes/world.py:288`)
- **結果**: seed 由来の Building は拒否。AI が在室 (`EXIT_TIMESTAMP IS NULL` の占有行) なら拒否。ユーザーが在室 (`User.CURRENT_BUILDINGID`) なら拒否。通れば `building_occupancy_log` の該当行を全削除し、`building` 行を削除。根拠: `manager/admin.py:461-508`
- **期待の根拠**: `実装のみ (根拠なし)` — `docs/user-guide/world-editor.md` は削除で何が失われるかを書いていない
- **現在の挙動 (静的確認)**: **削除しないもの** — `building_messages` (その部屋の会話ログ全件)、`fixture` 行、その部屋にあった `item_location` 行、`realtime_spell_binding` 行、`~/.saiverse/cities/<city>/buildings/<id>/` のファイル。いずれも `BUILDING_ID` の FK 宣言はあるが (`database/models.py:870, 907`)、`PRAGMA foreign_keys=ON` を repo 内に見つけられなかった (SQLite の既定は OFF) ので、削除は成功して孤児行が残る形になる。**確認: 実行はしていない。**
- **追跡できていない境界**: 孤児になった `building_messages` を後から掃除する経路があるかは追えていない。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

### WORLD-09: 街マップ上の Building 配置を編集する

- **入口**: 街マップ → 編集ボタン → ドラッグ → 保存。`PUT /api/world/buildings/positions` (`api/routes/world.py:253-277`)
- **結果**: 複数 Building の `MAP_X` / `MAP_Y` を一括更新。in-memory は触らず DB のみ更新し、次の city-map 取得で反映。存在しない building_id は黙って読み飛ばして `updated` 件数だけ返す。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/city-map.md` §編集モード
- **現在の挙動 (静的確認)**: 座標が未設定の Building は擬似配置 (`pseudoBuildingPosition`) で描かれる (`frontend/src/components/CityMap.tsx:500`)。
- **追跡できていない境界**: なし
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### WORLD-10: Building にリアルタイムスペルを結ぶ

- **入口**: Building 設定モーダル → 事前実行スペル。`GET/POST/DELETE /api/world/buildings/{id}/realtime-spell` (`api/routes/world.py:836-917`, `frontend/src/components/BuildingSettingsModal.tsx:141, 405, 473`)
- **まとめた根拠**: 3 ルートは同一の binding 表に対する一覧・追加・削除で、UI も 1 つのパネル。
- **結果**: `RealtimeSpellBinding` (OWNER_KIND='building') 行の増減。この Building で発言するペルソナの発話前にスペルが走る。
- **期待の根拠**: `実装のみ (根拠なし)` — `docs/user-guide/world-editor.md` にも `world-view.md` にも記載を見つけられなかった。UI ラベルは「事前実行スペル」
- **現在の挙動 (静的確認)**: 作成 API は `spell_name` の存在検証をしていない (存在しないスペル名でも 200 で binding が作れる)。`api/routes/world.py:867-893`
- **追跡できていない境界**: binding が実際にいつ消費されるか (Spell 実行側) は Spell 領域なので追っていない。
- **既存テスト**: `tests/test_realtime_spells_media.py` があるが、Building binding の CRUD ルートを通しているかは未確認 (ファイル名から media 系と読める)。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△` `テスト対応=△`

### WORLD-11: 街マップを見る

- **入口**: チャット領域ヘッダの「街マップ」ボタン。`GET /api/info/city-map[?region_id=...]` を約 7 秒間隔でポーリング (`frontend/src/components/CityMap.tsx`, `api/routes/info.py:259-357`)
- **結果**: そのスコープの Building 一覧 + 各 Building の在室ペルソナ + 座標 + 内装画像 + 背景画像 + 街の表示名 + 「子スコープの入口かどうか」を 1 回で返す。アイテムは含まない。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/city-map.md` 全体。`既存の仕様文書` — `docs/intent/region.md` §2.2 (スコープ規則)
- **現在の挙動 (静的確認)**: スコープの絞り込みは `building.region_id == scope_id` の単純比較。入口 Building は親スコープに属する構造なので、この比較だけで「自スコープ直属 + 子スコープの入口」が成立する。ユーザー自身は occupants に載らない (`manager.personas` に居る ID だけを拾う、`api/routes/info.py:328-336`)。
- **追跡できていない境界**: 7 秒ポーリングの負荷と、多数 Building 時の挙動は静的には測れない。
- **既存テスト**: `tests/test_city_identity.py:408, 420` — city-map の表示名フォールバック (CITYNAME 空なら CITY_SLUG) を TestClient で通す。Region スコープ・occupants・座標の検査は見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (表示名のみ)

### WORLD-12: 建物の中を見る (在室者・アイテム・設置物)

- **入口**: 右サイドバー (Info ボタン)。`GET /api/info/details?building_id=...` (`api/routes/info.py:77-227`, `frontend/src/components/RightSidebar.tsx`)
- **結果**: 建物名・説明・内装画像・在室ペルソナ (AI) ・在室ユーザー・アイテム (bag は中身つき) ・設置物 (Fixture、observer の有無つき) を返す。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-view.md` §右サイドバー、`docs/user-guide/items-and-files.md` §建物のアイテム
- **現在の挙動 (静的確認)**:
  - `building_id` を省略するとサーバ全体で共有の `user_current_building_id` にフォールバックし、WARN を出す。docstring に「2026-04-30 のエリス上書き事故の遠因」と明記されている (`api/routes/info.py:79-92`)。**この省略呼び出しはまだ 400 で拒否されていない。**
  - 在室者の「話しかけやすさ」欄 (`life_state` / `life_until` / `activity_label`) は応答モデルには残っているが、供給側は撤去済みで常に None (`api/routes/info.py:112-127` のコメント)。
  - bag の中身は再帰的に組み立てる (`_build_item_info`)。
- **追跡できていない境界**: `manager.item_registry` と `manager.items` の二重参照 (`:159-165`) がどちらの経路で食い違うかは追えていない。
- **既存テスト**: `tests/test_info_life_state.py` — occupants[] に暮らし系の欄が出ないことを固定。アイテム・設置物・users 配列の検査は見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (life_state の不在のみ)

### WORLD-13: 建物を「閲覧」に切り替える (移動とは別)

- **入口**: 左サイドバーの場所一覧をクリック / 街マップの Building をクリック。`frontend/src/components/Sidebar.tsx:239-254`
- **結果**: **サーバ上の現在地は変わらない。** UI 上の履歴・在室者・チャット表示だけが切り替わる。実際の入室は発言時に `/api/chat/utter` が原子的に行う。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-view.md` §「Building の選択は「閲覧」（移動ではない）」。`既存の仕様文書` — `docs/intent/building_memory_unified.md` §C (コード内参照)
- **現在の挙動 (静的確認)**: サイドバーの `handleMove` は `/api/user/move` を呼ばない。`PeopleModal` / `RightSidebar` / `PersonaMenu` はいずれも「閲覧中の building_id」を明示的に渡す設計になっている (`PeopleModal.tsx:46-52` は未指定なら即 return する安全策つき)。
- **追跡できていない境界**: 街マップからのクリック移動後にサイドバーの現在地表示が更新されない既知不具合が `docs/issues/map_click_move_sidebar_not_updated.md` (未着手) にある。実際の再現条件は追えていない。
- **既存テスト**: `tests/test_chat_boundary_w7.py` — utter 側の境界照合を扱う。フロントの閲覧モードそのものの検査は無い (フロントに自動テストが見当たらない)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (バックエンド側のみ)

### WORLD-14: ユーザーが移動する

- **入口 (2 つ)**: ①`POST /api/user/move` (街マップからの明示移動、ペルソナ作成ウィザード、チュートリアル)。②`POST /api/chat/utter` の発言契機入室 (閲覧中の別建物で発言すると自動入室)。根拠: `api/routes/user.py:94-150`, `api/routes/chat.py:1333-1400`
- **結果**: `User.CURRENT_BUILDINGID` の更新 + 退室/入室の host 行 2 本 (元の建物と先の建物) + `manager.state.user_current_building_id` の同期 + `user_move` フェノメノントリガーの発火。根拠: `saiverse/occupancy_manager.py:199-496`, `manager/runtime.py:315-330`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-view.md` §Building の選択は「閲覧」
- **現在の挙動 (静的確認)**:
  - クライアント CAS (`expected_from_building_id`) とサーバ CAS (条件付き UPDATE の rowcount) の 2 段。どちらも競合すると 409 + サーバの確定現在地を返す。
  - 位置遷移 + host 行 2 本 + 台帳 applied + 後処理 outbox が**単一トランザクション**。commit 後は False を返さない規律。
  - `expected_from_building_id` は Optional で、未指定ならクライアント CAS を飛ばす (後方互換)。
- **追跡できていない境界**: フロントが `expected_from_building_id` を常に送っているかは全呼び出し箇所を確認していない。
- **既存テスト**: `tests/test_move_entity_ledger.py::test_user_move_updates_location_and_uses_none_queue`、`tests/test_location_occupancy_w7.py::test_cas_update_user_location_arbitration` / `::test_user_stale_from_is_refused` / `::test_user_move_syncs_manager_state` — 実 sqlite (StaticPool) + fake manager。LLM も HTTP も使わない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### WORLD-15: ペルソナが移動する / 呼ぶ / 帰す

- **入口 (5 つ、すべて `OccupancyManager.move_entity` に集約)**:
  1. People 管理モーダルの「召喚」 → `POST /api/people/summon/{persona_id}?building_id=` → `manager.summon_persona` (`api/routes/people/summon.py:110`, `manager/runtime.py:397`)
  2. ペルソナメニューの「自室に戻す」/ People モーダルの「解散」 → `POST /api/people/dismiss/{persona_id}` → `manager.end_conversation` (`manager/runtime.py:439`)
  3. ワールドエディタ → ペルソナタブ → 移動 → `POST /api/world/ais/{ai_id}/move` → `move_ai_from_editor` (`manager/admin.py:1484`)
  4. ペルソナ自身が `move_persona` ツール (playbook `building_move_playbook` 経由、`router_callable: true`)
  5. ゲームのパーティー追従・帰還 (`saiverse/game_lifecycle.py`、`topology_bypass` つき)
- **まとめた根拠**: 5 経路とも最終的に `move_entity(entity_id, 'ai', from, to)` の同じ 1 本を通り、位置属性の同期・イベント・後処理も同じ (「呼び出し側は位置属性を書き換えてはならない」と `move_entity` の docstring が明記、`saiverse/occupancy_manager.py:213-236`)。
- **結果**: `building_occupancy_log` の active 行の付け替え + 退室/入室の host 行 2 本 + `persona.current_building_id` / `_mark_entry` / `_save_session_metadata` の同期 + 後処理 outbox 3 種 (dynamic state / addon hooks / game lifecycle) + `persona_move` トリガー (経路 1〜4 のうち `_move_persona` を通るものだけ)。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-view.md` §ペルソナメニュー (「自室に戻す」)、`docs/user-guide/world-view.md` §チャット領域 (People 管理)。`既存の仕様文書` — CLAUDE.md 「Move a persona: OccupancyManager.move_entity」
- **現在の挙動 (静的確認)**:
  - **`persona_move` トリガーは `RuntimeService._move_persona` からしか出ない** (`manager/runtime.py:332-350`)。`move_entity` を直接呼ぶ経路 (game lifecycle 等) では出ない。
  - `POST /api/world/ais/{id}/move` は `target_building_name` を受け、名前一致で ID を探し、見つからなければ**受け取った文字列をそのまま building_id として使う** (`api/routes/world.py:469-489`)。同じ関数内にどう設計すべきか迷ったままのコメントが 8 行残っている。
  - `move_ai_from_editor` はユーザーの部屋 (`user_room_id`) を特別扱いし、そこへの移動は `summon_persona` に、そこからの移動は「end conversation を実行してください」の拒否に振り替える (`manager/admin.py:1484-1520`)。
- **追跡できていない境界**: 経路 5 (game lifecycle) の `topology_bypass` が、どの場面でどのくらい使われるかは追えていない。
- **既存テスト**: `tests/test_move_entity_ledger.py` (11 件) — 実 sqlite + fake manager で単一 commit・ロールバック・後処理失敗時の再配送・台帳なし縮退経路を通す。`tests/test_location_occupancy_w7.py` (28 件) — CAS 仲裁・重複 active 行の修復・属性同期・event_key の一意性。**API ルート (`/people/summon`, `/people/dismiss`, `/world/ais/{id}/move`) を通すテストは見つからなかった。**
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (中核は厚い / 5 つの入口はどれも通していない)

### WORLD-16: 移動が記録され、ペルソナに届く

- **入口**: WORLD-14 / WORLD-15 の移動が確定した瞬間 (自動処理)
- **結果 (3 系統に分かれる)**:
  1. **建物ログの host 行 2 本** — 退室元と入室先に「〜が〜へ移動しました」「〜が〜から入室しました」を挿入。`heard_by` は移動後の在室者 (退室元は移動者を除く、入室先は移動者を含む)。`metadata.event.type = "occupancy"`、`event_key` は移動ごとの採番 ID。根拠: `saiverse/occupancy_manager.py:498-583`
  2. **ペルソナ記憶への転記は行われない** — 建物ログの host 行はペルソナの記憶へ自動転記される仕組みだが (`builtin_data/tools/get_building_messages.py:252-283`)、`event.type == "occupancy"` の行と `entity_type == "user"` の行は**明示的に転記対象から外され、既読マークだけ付けられる** (`:255-260`)。つまり移動そのものは記憶に文として残らない。理由はコード内コメント (`:242-251`) に「head_pipeline の BuildingSection / BuildingOccupantsSection が diff 通知を出す」とある。
  3. **知覚バッファへの投入 (3 段)** — `move.post_dynamic_state` outbox → `DynamicStateManager.on_building_entered` (`saiverse/dynamic_state.py:86-260`) が、①移動した本人へ移動の事実の差分通知 (`only_sections={"building","building_occupants"}`) ②同じ部屋に既に居る他ペルソナ全員へ「この入室」の差分通知 ③本人へ**部屋の様子の束** (他ペルソナの外見・内装画像・アイテム・設置物) を `perception.room_state` outbox で配送。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/room_state_packages.md` (ステータス: 完了 2026-09-07)、`docs/intent/perception_buffer.md`
- **現在の挙動 (静的確認)**: 「世界側の記録がペルソナの記憶に流れ込む経路」は、移動については**建物ログ経由ではなく知覚バッファ経由**。建物ログの occupancy 行はユーザーの画面表示専用。
  - 対して、**それ以外の host 行 (アイテムの拾得/設置/使用、World Event、Blueprint spawn、Observer 通知) は転記対象**で、`<system>[建物名] 本文</system>` の形で `role: "user"` として記憶へ入る (`builtin_data/tools/get_building_messages.py:264-283`)。
  - ただし転記は `heard_by` に自分が入っている行だけが対象 (`:373-380`)。`add_building_event` は `heard_by` の既定が空リストなので (`manager/history.py:112-114`)、**heard_by を渡さない呼び出し元の host 行は誰の記憶にも届かない**。渡していない呼び出し元: `_append_building_history_note` (アイテム系すべて、`saiverse/saiverse_manager.py:1020-1025`)、`ObserverManager._notify_building` (`saiverse/observer_manager.py:707-728`)。渡している呼び出し元: World Event (`manager/admin.py:1552`)、Blueprint spawn (`manager/blueprints.py:350`)、game lifecycle (`saiverse/game_lifecycle.py:508`)、City Transfer (`manager/visitors.py:285, 348`)。
  - アイテム系は代わりに `broadcast_item_event` → `record_persona_event` で当事者以外の在室ペルソナへ直接届けている (`manager/items.py:307-311, 351-355`)。**Observer 通知にはこの代替経路が見当たらない** (WORLD-38 参照)。
- **追跡できていない境界**: `record_persona_event` から先 (ペルソナのイベントログ → head/知覚) は記憶領域の担当なので追っていない。
- **既存テスト**: `tests/test_building_ingest_m8.py` — 転記の停止規律と冪等性。`tests/test_move_entity_ledger.py::test_ai_move_single_commit_and_post_processing_delivered` — host 行 2 本の生成と outbox 配送。`tests/test_location_occupancy_w7.py::test_event_keys_unique_for_same_second_round_trip` — event_key の衝突回避。`tests/test_room_state_diff.py` — 部屋の様子の差分。**「heard_by を渡さない host 行が誰にも届かない」ことを固定するテストは見つからなかった。**
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (転記規律と移動の中核はある / heard_by 空の host 行の帰結は未検査)

### WORLD-17: 移動が拒否される (5 つの門)

- **入口**: WORLD-14 / WORLD-15 のあらゆる移動要求
- **結果**: 拒否理由の日本語文字列。CAS 競合だけは `MoveDenialMessage` (code=`cas_conflict`) 型で返り、route 層が 409 に変換する。
- **門の一覧 (`saiverse/occupancy_manager.py`、判定順)**:
  1. 行き先の Building が存在しない (`:235`)
  2. 行き先が隔離中 (`:242-256`) — **WORLD-43 のとおり、隔離を立てる検出器が現在は呼ばれていないので実質発動しない**
  3. Region の入口トポロジー (`:257-263` → `_check_entrance_topology` `:110-146`)。境界越えは入口 Building からのみ。同時にその境界点で entry policy (`open` / `locked` / `whitelist`) を執行 (`_check_entry_policy` `:148-164`)
  4. game Region の入場ゲート (`:265-272` → `_check_game_region_gate` `:166-197`) — 進行中は参加者と Ruler 以外を拒否
  5. 定員 (`:273-287`) — AI のみ。既に在室なら超過でも通す。ユーザーは定員の対象外
  6. (実質 6 つ目) DB の CAS — active 行の重複、stale from、条件付き UPDATE / guarded INSERT の仲裁負け
- **期待の根拠**: `既存の仕様文書` — `docs/intent/region.md` §2.4/§3 (入口トポロジー・entry policy)。定員は `docs/user-guide/world-editor.md` §Buildings タブ (「Capacity 定員（0=無制限）」)
- **現在の挙動 (静的確認)**: **定員の 0 は無制限にならない。** `capacity_limit = self.capacities.get(to_id, 1)` で `current_ai >= capacity_limit` を比較するため、CAPACITY=0 の Building は在室者 0 でも `0 >= 0` が成立して常に拒否される (`saiverse/occupancy_manager.py:274-287`)。`docs/user-guide/world-editor.md` は「0=無制限」と説明している。**確認: 実行はしていない。コードの読みのみ。**
- **追跡できていない境界**: 既定の CAPACITY 値と、実際に 0 を設定できる UI 経路 (ワールドエディタの数値入力) の下限があるかは追えていない。
- **既存テスト**: `tests/test_entrance_topology.py` (21 件) — 入口トポロジーと entry policy を fake で網羅。`tests/test_game_region_gate.py` (11 件) — game ゲート。`tests/test_location_occupancy_w7.py` に CAS 群。**定員判定 (`capacity_limit`) を直接通すテストは `tests/test_location_occupancy_w7.py::test_capacity_overflow_is_classified_without_eviction` (起動時の分類) だけで、`move_entity` の定員拒否そのものの検査は見つからなかった。**
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (トポロジー・ゲート・CAS は厚い / 定員は薄い)

### WORLD-18: Region を作る・編集する・削除する・Building を所属させる

- **入口**: `POST/PUT/DELETE /api/world/regions`、`PUT /api/world/buildings/{id}/region` (`api/routes/world.py:316-336`)
- **まとめた根拠**: 4 ルートはすべて Region の構造編集で、`manager/admin.py:581-927` の同じ service 群に落ちる。UI から到達できない点も共通。
- **結果**: `region` 行の増減と `building.REGION_ID` の付け替え。作成時、入口 Building を指定しなければ「(名): 入口」を自動作成する。削除時、自動生成された入口 (`entrance_<region_id>`) は Region と運命を共にし、ユーザー指定の Building は残す。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/region.md` (Status: v0.2 確定)
- **現在の挙動 (静的確認)**:
  - **これら 4 ルートを呼ぶフロントエンドのコードが見つからなかった。** `frontend/src/` 内の `/api/world/regions` の参照は `game/log`・`game/rejoin`・`map-background` の 3 つだけ (`app/page.tsx:726, 838, 1279`, `components/CityMap.tsx:530`)。ワールドエディタのサブタブは City / Building / ペルソナ / Blueprint / ツール / アイテム / Playbook の 7 つで、**Region タブは存在しない** (`frontend/src/components/settings/WorldEditor.tsx:498-506`)。
  - 削除には 3 つの拒否条件がある (SubRegion が残っている / Ruler がいる / Building が所属している)。`manager/admin.py:832-877`
  - `set_building_region` は入口 Building の所属変更を拒否する (入口の所属は Region service が持つ不変条件)。`manager/admin.py:880-903`
- **追跡できていない境界**: 実際に Region を作った世界がどう作られたのか (seed / スクリプト / 手動 SQL) は追えていない。
- **既存テスト**: `tests/test_region_admin.py` (45 件) — AdminService を `__new__` + SessionLocal 差し込みで直叩き。実 sqlite。**API ルート層は通していない。**
- **状態**: `機能の存在=✓ (API のみ)` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (service は厚い / ルートと UI は無い)

### WORLD-19: Region スコープの表示 (サイドバー折り畳み・マップの潜り)

- **入口**: 左サイドバーの「場所」(`GET /api/user/buildings` が `regions` を返す、`api/routes/user.py:46-57`) と `GET /api/world/regions` (`api/routes/world.py:295-315`)、街マップの入口 Building クリック (`GET /api/info/city-map?region_id=`)
- **結果**: Region ごとの折り畳み表示、および Region 内部マップへの遷移と「上の階層へ戻る」。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/city-map.md` (「Region の入口 Building → その Region 内部のマップへ潜る」)、`docs/user-guide/world-view.md` (「Region があれば折り畳み表示」)。`既存の仕様文書` — `docs/intent/region.md` §2.2
- **現在の挙動 (静的確認)**: 読み取り側は実装され UI からも使われている (WORLD-18 の書き込み側と非対称)。
- **追跡できていない境界**: Region がゼロ件の世界での見え方は追えていない。
- **既存テスト**: 該当テストなし (`GET /api/world/regions` / `city-map?region_id` を通すテストを見つけられなかった)
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### WORLD-20: game Region に Ruler (GM ペルソナ) を作る

- **入口**: `POST /api/world/regions/{region_id}/ruler` (`api/routes/world.py:363-366`)
- **結果**: GM ペルソナと控室 Building を生成して Region に紐づける。
- **期待の根拠**: `実装のみ (根拠なし)` — `docs/intent/region.md` は「RPG 固有の設計は `temp/region_rpg_intent.md` (リポジトリ外管理)」としており、リポジトリ内に仕様がない
- **現在の挙動 (静的確認)**: フロントエンドにこのルートを呼ぶコードが見つからなかった。
- **追跡できていない境界**: `manager.create_ruler` の中身は読んでいない (`manager/admin.py` の Region 節の外にある可能性)。仕様文書がリポジトリ外なので期待値を確認できない。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓ (API のみ)` `期待の根拠=✗` `挙動の静的確認=△` `テスト対応=✗`

### WORLD-21: ゲームを始める・止める・再開する・終える・復帰する / セッションログを読む

- **入口**: `GET /api/world/regions/{id}/game`、`POST .../game/{start,pause,resume,end,rejoin}`、`GET .../game/log` (`api/routes/world.py:371-443`)。ペルソナ側は Ruler 専用ツール 4 本 (`game_create_subregion` / `game_create_building` / `game_set_scene` / `game_move_party`)
- **まとめた根拠**: 7 ルートは 1 つの `GameLifecycleService` の状態機械 (phase: playing / paused / …) に対する操作と参照。
- **結果**: Region の `state` (phase / participants / started_at) の遷移、パーティーの移動、Region 内全 Building の建物ログを時系列 merge した読み出しビュー。
- **期待の根拠**: `実装のみ (根拠なし)` — 仕様は `temp/region_rpg_intent.md` (リポジトリ外)。`docs/user-guide/` にゲームの記述は見つからなかった
- **現在の挙動 (静的確認)**: **フロントが呼ぶのは `game/log` と `game/rejoin` の 2 つだけ** (`frontend/src/app/page.tsx:726, 838, 1279`)。start / pause / resume / end を呼ぶ UI が見つからなかった。ログビューは `heard_by` に閲覧者 (ユーザー) を含むメッセージだけを返す。
- **追跡できていない境界**: ゲームを開始する実際の手段 (Ruler ペルソナのツールから開始する設計なのか、UI 未実装なのか) は追えていない。
- **既存テスト**: `tests/test_game_lifecycle.py` — phase 遷移・自動ポーズ/再開・アーカイブを fake manager + 実 sqlite で。`tests/test_game_session_log.py` — merge とカーソル絞り込みを実 sqlite で。`tests/test_game_world_tools.py` — Ruler 専用ツール 4 本を fake manager で。`tests/test_game_region_gate.py` — 入場ゲート。**API ルート層は通していない。**
- **状態**: `機能の存在=✓` `期待の根拠=✗ (仕様がリポジトリ外)` `挙動の静的確認=✓` `テスト対応=△` (service は厚い / ルートと UI 導線は無い)

### WORLD-22: アイテムを作る

- **入口**: ワールドエディタ → アイテムタブ → 作成。`POST /api/world/items` (`api/routes/world.py:524-565`)
- **結果**: `item` 行 1 件 + (owner_kind が world 以外なら) `item_location` 行 1 件。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-editor.md` §Items タブ (Name / Type / Description「自動生成可」/ Location)
- **現在の挙動 (静的確認)**:
  - **説明を空にして file_path を渡すと LLM を呼ぶ (課金が発生する)**。picture なら `ensure_image_summary`、document なら `ensure_document_summary` → `_generate_image_summary` が実際に LLM クライアントへリクエストする (`api/routes/world.py:539-563`, `saiverse/media_summary.py:109-190`)。UI にこの費用の告知は見つからなかった。
  - **owner_kind の既定が `world`** で、ワールドエディタのアイテムタブも既定値を `owner_kind: 'world'` にしている (`frontend/src/components/settings/WorldEditor.tsx:504`)。world 所有のアイテムは `ItemLocation` 行を持たず `world_items` に入るだけで (`manager/items.py:170-179`)、右サイドバーにも街マップにも出ない。ワールドエディタのアイテム表からしか見えない。
  - file_path は `resolve_allowed_path` で許可ルート内に強制される (`api/routes/world.py:528-531`)。
- **追跡できていない境界**: `world_items` を読む場所が本当に無いかは全文検索していない (`world_items` の参照を数か所しか見ていない)。
- **既存テスト**: 該当テストなし (`POST /api/world/items` を通すテストを見つけられなかった)
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### WORLD-23: アイテムを編集する・削除する

- **入口 (2 つ)**: ①ワールドエディタ → アイテムタブ (`PUT/DELETE /api/world/items/{item_id}`) ②アイテムモーダル → 名前/説明の編集 (`ItemModal.tsx:265`)
- **結果**: `item` 行の更新 / 削除 (削除時は `item_location` 行も一緒に消す)。削除後 `_load_items_from_db()` で in-memory 再構築。根拠: `manager/admin.py:1027-1136`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/items-and-files.md` §アイテムの閲覧・編集
- **現在の挙動 (静的確認)**: 削除しても実ファイル (`~/.saiverse/image/...` 等) は消えない (`manager/admin.py:1117-1136` にファイル削除が無い)。削除の確認ダイアログはワールドエディタ側にあるが、そのアイテムがどのペルソナの記憶から参照されているかの警告は見当たらない。
- **追跡できていない境界**: 削除したアイテムを指す `saiverse://item/N` がペルソナの記憶に残っている場合の見え方 (「死んだ参照」) は記憶領域の担当なので追っていない。`docs/overview/release_history.md:34` に「死んだ message 参照の sweep」の記述があり、item 参照についても同種の問題が起きうる。
- **既存テスト**: `tests/test_document_item_ref.py` — document アイテムの参照解決。削除経路の検査は見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

### WORLD-24: アイテムの中身を見る (本文・画像・音声・動画・bag)

- **入口**: 右サイドバーのアイテムをクリック → アイテムモーダル。`GET /api/info/item/{item_id}[?thumb=1]`、`GET /api/info/item/{item_id}/bag-contents` (`api/routes/info.py:388-563`)
- **結果**: document は本文テキスト、picture は画像ファイル (thumb=1 なら WebP サムネイル)、audio は ogg、video は mp4 を返す。bag は中身の一覧。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/items-and-files.md` §アイテムの閲覧・編集
- **現在の挙動 (静的確認)**:
  - `item_id` はペルソナ可視の short_id (`item:N` の N) でも UUID でも受ける (`_resolve_item_uuid`, `api/routes/info.py:373-386`)。
  - **旧 WSL 絶対パスや相対パスからの復旧戦略が 8 通り積まれている** (`:439-505`)。ファイル名だけを頼りに `~/.saiverse/{documents,image,audio,video}/` を探す戦略まである。復旧後に `ensure_allowed_path` を通す (`:511`)。
  - `thumb` はサムネイル生成に失敗したらオリジナルへフォールバックする。
- **追跡できていない境界**: 復旧戦略 2a/2b (ファイル名だけの一致) が、同名の別ファイルを引く可能性は追えていない。
- **既存テスト**: `tests/test_attachment_paths.py` — 添付の保存先と URI 再解決。`GET /api/info/item/{id}` のパス復旧戦略を通すテストは見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

### WORLD-25: ドキュメントアイテムの本文を編集する

- **入口**: アイテムモーダル → 本文編集 → 保存。`PUT /api/info/item/{item_id}/content` (`api/routes/info.py:595-655`)
- **結果**: 実ファイルを `write_text` で上書き。document 型以外は 400 で拒否。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/items-and-files.md` (「本文の表示…と編集・保存」)
- **現在の挙動 (静的確認)**: 書き込み前に `ensure_allowed_path` を通す (`:652`)。パス復旧は読み取り側より簡略 (documents ディレクトリのみ)。バックアップも版管理も無い (上書き)。
- **追跡できていない境界**: そのアイテムを既にコンテキストへ載せているペルソナ側の表示がいつ更新されるかは追えていない。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### WORLD-26: アイテムを AI のコンテキストに出し入れする

- **入口**: 右サイドバーのアイテム行の目のアイコン。`POST /api/info/item/{item_id}/toggle-open` (`frontend/src/components/RightSidebar.tsx:136`, `api/routes/info.py:573-593`)
- **結果**: `item.STATE_JSON.is_open` を反転。含めるとその建物にいるペルソナのコンテキストにアイテム内容が載る。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/items-and-files.md` §AI コンテキストへの出し入れ
- **現在の挙動 (静的確認)**: UI は picture / document / bag / audio / video のときだけトグルを出す (`RightSidebar.tsx:422`)。実際にコンテキストへ載る経路は `get_open_items_in_building` (`manager/items.py:760`) と部屋の様子の束。
- **追跡できていない境界**: `is_open` が実際に head / 知覚のどこへ効くか (`build_room_bundle` の内部) は記憶・head 領域の担当なので追っていない。
- **既存テスト**: 該当テストなし (`toggle-open` ルートを通すテストを見つけられなかった)
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` `テスト対応=✗`

### WORLD-27: ペルソナの持ち物を見る

- **入口**: ペルソナメニュー → 持ち物。`GET /api/people/{persona_id}/items` (`api/routes/people/inventory.py:10-41`, `frontend/src/components/InventoryModal.tsx:34`)
- **結果**: `ItemLocation.OWNER_KIND == 'persona'` のアイテムを名前順で返す。読み取り専用。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/items-and-files.md` §ペルソナの持ち物（インベントリ）
- **現在の挙動 (静的確認)**:
  - モーダルの見出しが `インベントリ: {personaId}` で、**表示名ではなく内部 ID を出す** (`InventoryModal.tsx:56`)。
  - アイテムをクリックしても何も開かない (ItemModal に繋がっていない)。bag の中身も出ない (`OWNER_KIND == 'bag'` は拾わない)。
  - ユーザーがここからアイテムを取り上げたり置いたりする経路は無い。
- **追跡できていない境界**: なし
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### WORLD-28: ペルソナがアイテムを拾う・置く・使う・見る・書き込む

- **入口**: ペルソナの Spell — `item_move` / `item_view` / `item_annotate` (いずれも `spell=True`)。内部は `ItemService.pickup_item` / `place_item` / `use_item` / `view_item` / `move_item` (`manager/items.py:313-475, 689, 1450`)
- **結果**: `item_location` 行の付け替え + 本人へ `record_persona_event` + 同室の他ペルソナへ `broadcast_item_event` + 建物ログへ note 行 (heard_by 空)。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/items-and-files.md` (「ペルソナは pickup / place 相当の操作でアイテムを拾ったり置いたりする」)。`既存の仕様文書` — `docs/concepts/item.md` (未読)
- **現在の挙動 (静的確認)**: 建物ログの note 行は heard_by 空なので誰の記憶にも転記されない (WORLD-16 参照)。ペルソナ側の知覚は `record_persona_event` / `broadcast_item_event` が担う二重系。
  - `pickup_item` は「その Building にある」ことだけを検査し、固定物 (Fixture) の概念は Item 側に無い (`docs/intent/observer.md` §なぜ必要か-2 が指摘している欠落)。
- **追跡できていない境界**: bag の入れ子・slot 割り当ての境界条件は追えていない。
- **既存テスト**: `tests/test_document_item_ref.py` — document 参照。pickup/place/use を通すテストは見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### WORLD-29: ファイルをアップロードする

- **入口**: チャットの添付ボタン / ドラッグ&ドロップ、各種画像アップロード欄、マップ背景。`POST /api/media/{upload, upload-hires, upload-document, upload-audio, upload-video, upload-file}` (`api/routes/media.py:47-333`)
- **まとめた根拠**: 6 ルートは同じ「利用者のファイルを `~/.saiverse/` 配下へ保存して URL を返す」目的で、`upload-file` は content-type で他 4 本へ振り分けるだけのディスパッチャ。
- **結果**: `~/.saiverse/{image,documents,audio,video}/{YYYYMMDD_HHMMSS}_{uuid4}.{ext}` に保存し、`{"url": "/api/media/...", "relative_path": "..."}` を返す。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/items-and-files.md` §チャットへのファイル添付、`docs/user-guide/world-view.md` §メッセージ入力
- **現在の挙動 (静的確認)**:
  - 上限: 画像/文書 25MB、音声 100MB (5 分)、動画 500MB (90 秒)。`read_upload_bounded` は上限+1 バイトしか読まず 413 を返す。`write_upload_bounded` は超過時に書きかけを消す (`api/file_safety.py:44-77`)。
  - 画像は LLM 向けに長辺 768px へ縮小 + 500KB 以下へ再圧縮 (`upload`)、背景用は WebP 変換のみで解像度維持 (`upload-hires`)。
  - 音声/動画は ffmpeg で正規化する。**ffmpeg が無い環境では 503 を返して機能自体が使えない** (`:195-199, 259-263`)。
  - 保存ファイル名はサーバが生成するので、アップロード側のファイル名は拡張子の判定にしか使われない (`safe_upload_suffix` が `\.[a-z0-9]{1,10}` に限定)。
- **追跡できていない境界**: content-type はクライアント申告値をそのまま信じている (中身の検査をしていない)。実際に何が起きるかは追えていない。
- **既存テスト**: `tests/test_api_file_boundaries.py` (5 件) — 上限超過で 413、部分ファイルの削除、画像ルートの 413 伝播、ファイル名・拡張子の検査、許可ルート外の拒否。実ファイル + patch。LLM も ffmpeg も使わない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### WORLD-30: アップロードしたファイルを配信する

- **入口**: `GET /api/media/{images,documents,audio,video}/{filename}` (`api/routes/media.py:334-378`)
- **まとめた根拠**: 4 ルートは同じ形 (`safe_filename` → ディレクトリ結合 → 存在確認 → FileResponse)。
- **結果**: ファイルの中身をそのまま返す。認証は無い。
- **期待の根拠**: `実装のみ (根拠なし)`
- **現在の挙動 (静的確認)**: `safe_filename` がパス構文 (`.` `..` `/` `\` `:` NUL、`Path(v).name != v`) を 400 で弾く (`api/file_safety.py:32-42`)。ディレクトリ外への脱出はここで塞がれている。
  - **認可は無い**: URL を知っていれば誰でも読める。SAIVerse は 127.0.0.1 バインドが既定 (CLAUDE.md「backend on 127.0.0.1:8000」) なので、ローカル前提の設計と読める。
- **追跡できていない境界**: 実運用でバインドアドレスを変えた場合 (Tailscale 経由での公開など、`docs/intent/observer.md` の push 節がそれを想定している) の露出は追えていない。
- **既存テスト**: `tests/test_api_file_boundaries.py::test_filename_and_suffix_reject_path_syntax` — `safe_filename` の直接検査。配信ルート自体は通していない。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=△`

### WORLD-31: チャットの添付が建物のアイテムになる

- **入口**: チャットでファイルを添付して送信。`api/routes/chat.py:483-880` の `_store_*_attachment` 群
- **結果**: `create_picture_item_for_user` / `create_document_item_for_user` / `create_audio_item_for_user` / `create_video_item_for_user` が、**現在の Building 所有のアイテムを `is_open=True` (= AI コンテキストに含める) で作る** (`manager/items.py:998-1293`)。建物ログに「User Upload」の note 行も残る (heard_by 空)。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/items-and-files.md` §チャットへのファイル添付 (「添付は `saiverse://` URI 経由でペルソナから参照される」)
- **現在の挙動 (静的確認)**:
  - 添付は**一時的な入力ではなく、その部屋に置かれ続ける永続アイテム**になる。`docs/user-guide/items-and-files.md` はこの点 (部屋に残る / 既定で開いている / 後から手で消す必要がある) を書いていない。
  - 説明の自動生成が同期実行されるモード (`sync_summary`) があり、その場合 LLM を呼ぶ (`tests/test_attachment_paths.py::test_sync_summary_on_generates_synchronously`)。
  - 部屋のアイテムが溜まり続ける問題は `docs/issues/room_items_uncapped.md` (設計待ち、v0.3.9 スコープ外) に起票済み。アイフィの部屋 58 個で部屋の様子が 35,000 字超と記録されている。
- **追跡できていない境界**: 添付を消す UI 経路 (ワールドエディタのアイテム削除以外) は見つけられなかった。
- **既存テスト**: `tests/test_attachment_paths.py` (10 件超) — 保存先が `SAIVERSE_HOME` 配下になること、URI が同じファイルへ再解決すること、sync_summary の有無。実ファイル + patch。
- **状態**: `機能の存在=✓` `期待の根拠=△ (URI 参照だけ書かれ、部屋に残ることは書かれていない)` `挙動の静的確認=✓` `テスト対応=✓`

### WORLD-32: 利用者のファイルがどこに保存され、誰が読めるか (ファイル境界)

- **入口**: 自動処理。`api/file_safety.py` と `saiverse/file_policy.py` が世界系ルートから呼ばれる
- **結果**: 許可ルート外のパスは 403、パス構文を含むファイル名は 400、上限超過は 413。
- **期待の根拠**: `実装のみ (根拠なし)` — 利用者向け文書に「ファイルはどこに置かれるか」の記述を見つけられなかった
- **現在の挙動 (静的確認)**:
  - 許可ルート = `~/.saiverse/` (または渡された home)、`USER_DATA_DIR`、リポジトリの `assets` / `builtin_data` / `expansion_data` / `user_data`、そして環境変数 `SAIVERSE_EXTERNAL_FILE_ROOTS` で追加されたパス。`saiverse/file_policy.py:9-25`
  - 検査は `resolve()` 後の `is_relative_to` なので、`..` もシンボリックリンクも解決後に判定される。
  - **`enforce_allowed_file_path` を通していない経路がある**: `saiverse/media_utils.py:64-88` の `resolve_media_uri` は URI から取り出したファイル名をディレクトリに素で結合するだけで、`safe_filename` も許可ルート検査も通さない。`parse_sai_uri` にも `..` の除去は無い (`saiverse/uri_resolver.py:103-184`)。この経路を使う `UriResolver._resolve_document` (`:862-878`) はファイルを `read_text` して中身を返す。→ `saiverse://document/../<何か>` の形が `~/.saiverse/documents/../<何か>` を読む形になる。**確認: コードの読みのみ。実行して再現はしていない (禁止事項に従い試していない)。** 到達経路は `GET /api/uri/resolve` と、ペルソナの `resolve_uri` スペル。
- **追跡できていない境界**: `resolve_extended_media_uri` (`saiverse/media_utils.py:91`〜) 側にも同じ形があるかは読んでいない。
- **既存テスト**: `tests/test_api_file_boundaries.py::test_file_policy_rejects_outside_managed_roots` — 許可ルート外の拒否。**`resolve_media_uri` にパス構文を渡す検査は見つからなかった** (`tests/test_attachment_paths.py` は正常系の再解決のみ)。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△ (未検査の穴を静的に確認)` `テスト対応=△`

### WORLD-33: `saiverse://` URI を解決する

- **入口**: `GET /api/uri/resolve?uri=&persona_id=` (`api/routes/uri.py`)、ペルソナの `resolve_uri` スペル (`builtin_data/tools/resolve_uri.py`、`spell=True`)、フロントの `ContentViewerModal` (`frontend/src/components/ContentViewerModal.tsx:130`)
- **結果**: URI の指す中身 (本文・パス・一覧) を返す。ペルソナスコープ (message / memopedia / chronicle) は本人の記憶のみで、他人は 403。
- **期待の根拠**: `既存の仕様文書` — `docs/reference/saiverse-uri.md` (グローバルスキーム 6 種の表)
- **現在の挙動 (静的確認)**: **世界系の 2 形式が動かない読みになっている。**
  - `saiverse://building/{id}/items` → `self.manager.item_service.list_items_in_building(building_id)` を呼ぶ (`saiverse/uri_resolver.py:771`)。`list_items_in_building` は**リポジトリ内でこの 1 箇所からしか参照されておらず、定義が見つからない** (`ItemService` の実在メソッドは `get_all_items_in_building`、`manager/items.py:784`)。呼べば `AttributeError` になり、直後の `except Exception` が `"Failed to list building items: ..."` のエラー結果に変える。
  - `saiverse://building/{id}/history` → `self.manager.buildings.get(building_id)` を呼ぶ (`saiverse/uri_resolver.py:797`)。`manager.buildings` は `List[Building]` (`manager/initialization.py:142`) で `.get()` を持たない。加えて `Building` クラスに `history` 属性は無い (`saiverse/buildings.py:22-42`)。同じく `except Exception` に落ちる。
  - **確認: コードの読みのみ。実行して確かめてはいない。** どちらも `docs/reference/saiverse-uri.md` の表に載っている。
  - `image` / `document` / `item` / `persona` / `web` の 5 種は素直に実装されている。
- **追跡できていない境界**: `ContentViewerModal` が実際にどの scheme を渡すかは追えていない。
- **既存テスト**: `tests/test_uri_resolver_memopedia.py` — memopedia スキームのみ (`UriResolver(manager=None)` で構築)。**building スキームを通すテストは見つからなかった** (manager=None では到達できない)。
- **状態**: `機能の存在=△ (building 系 2 形式は静的に不成立)` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

### WORLD-34: Fixture (持ち運べない設置物) を作る・見る

- **入口**: `POST /api/observer/fixture`、`GET /api/observer/fixture/{id}`、`GET /api/observer/building/{id}/fixtures` (`api/routes/observer.py:120-175`)。RSS フィード機能が同じ Fixture を作る (`tests/test_feeds_api.py`)
- **結果**: `fixture` 行の upsert と参照。右サイドバーの「設置物」欄に出る (`GET /api/info/details` の `fixtures` 経由)。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/observer.md` (ステータス: **ドラフト v0.1, 2026-05-28**)。`利用者向け説明` — `docs/user-guide/world-view.md` §右サイドバー (「設置物 — 建物内の Fixture」)
- **現在の挙動 (静的確認)**:
  - **API に更新も削除も無い** (`POST /fixture` の upsert と 2 つの GET だけ)。UI からも作成・削除できない。RSS フィード機能 (`api/routes/feeds.py`) が別途 Fixture を作る経路を持つ。
  - `docs/overview/landscape.md` §2 は Fixture を「`observer.md` v0.1、**設計のみ・未実装**」と書いているが、テーブル・manager・API・UI 表示はすべて存在する (§3 に矛盾として記載)。
- **追跡できていない境界**: `api/routes/feeds.py` の Fixture 作成経路は領域 C の担当外と判断して詳細を読んでいない。
- **既存テスト**: `tests/test_feeds_api.py` — 実物の `ObserverManager` を使い、feeds ルート経由で Fixture 作成 (プリセット / カスタム / 404 / 422 / 上限) を検査。**`/api/observer/fixture` ルート自体を通すテストは見つからなかった。**
- **状態**: `機能の存在=✓` `期待の根拠=△ (intent がドラフト)` `挙動の静的確認=✓` `テスト対応=△` (feeds 経由のみ)

### WORLD-35: Observer (定期観測) を作る・回す

- **入口**: `POST /api/observer/config` (`api/routes/observer.py:182-200`)。実行は起動時に `observer_manager.start_pull_observers()` (`saiverse/saiverse_manager.py:530`)
- **結果**: `observer_config` 行の upsert。`EXEC_KIND` が `tool` / `playbook` の Observer は EventScheduler に相乗りして `INTERVAL_SEC` ごとに実行され、結果を `observer_metrics` へ蓄積する。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/observer.md` §実行・蓄積・通知のフロー、§守るべき不変条件 3 (EventScheduler を塞がない)
- **現在の挙動 (静的確認)**:
  - **API は作成 (upsert) だけ。取得も一覧も更新も削除も無い。** 作った Observer を後から止める HTTP 経路が見つからなかった (`ENABLED` を落とすには DB を直接触るしかない読みになる)。
  - UI からは Observer を一切作れない。
  - 起動時の `start_pull_observers()` は manager 構築とは別に `start()` で呼ばれており、機構としては動いている。
- **追跡できていない境界**: `_execute_tool` / `_execute_pull` が EventScheduler をどう塞がないようにしているかは実行しないと測れない。
- **既存テスト**: `tests/test_feeds_api.py` が実物の ObserverManager を構築する。**pull 実行 (`_schedule_pull` / `_execute_pull`) を通すテストは見つからなかった。**
- **状態**: `機能の存在=✓ (API のみ、片方向)` `期待の根拠=△ (intent がドラフト)` `挙動の静的確認=△` `テスト対応=✗`

### WORLD-36: 外部アプリから観測値を push する

- **入口**: `POST /api/observer/{observer_id}/push` (`api/routes/observer.py:83-113`)。Bearer token 認証 (`OBSERVER_PUSH_TOKEN` 環境変数)
- **結果**: `observer_metrics` への記録 + `fixture.STATE_JSON` の最新値キャッシュ更新 + 閾値通知の評価 (WORLD-38)。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/observer.md` §Push モード (2026-06-21 合意)。想定利用者はウェアラブルコンパニオンアプリ
- **現在の挙動 (静的確認)**:
  - トークン未設定なら 503、ヘッダ無しなら 401、不一致なら 403。トークン比較は素の `!=` (定数時間比較ではない)。
  - `EXEC_KIND != "push"` の Observer への push は 400。
  - **冪等性が無い** ことは `docs/issues/observer_push_resend_idempotency.md` (未解決、2026-08-03 起票) に起票済み。intent の記述 (「(OBSERVER_ID, METRIC_NAME, RECORDED_AT) の自然キーで upsert」) と実装が一致しているかは `record_metrics` を読んだが upsert の形までは確認できていない。
- **追跡できていない境界**: `record_metrics` (`saiverse/observer_manager.py:337-484`) の `_write()` 内部の衝突処理は読み切っていない。
- **既存テスト**: 該当テストなし (`/api/observer/{id}/push` を通すテストを見つけられなかった)
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` `テスト対応=✗`

### WORLD-37: 観測値を見る (ユーザー / ペルソナ)

- **入口 (3 つ)**: ①右サイドバー → 設置物をクリック → 設置物モーダル ②`GET /api/observer/{id}/latest` ③`GET /api/observer/{id}/history/{metric_name}?limit=` ④ペルソナの `observer_read` スペル
- **結果**: 最新値 (名前 / 値 / 記録時刻) または履歴。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-view.md` §右サイドバー (「Observer 系はクリックで観測値（**時系列データ**）を表示」)、`docs/user-guide/items-and-files.md` §設置物 (「クリックで観測値（名前 / 値 / 記録時刻）を表示」)
- **現在の挙動 (静的確認)**:
  - **設置物モーダルは時系列を出さない。** `GET /api/info/details` が返した `state_json` (= 最新値のスナップショット) を表で描くだけで、`/api/observer/{id}/latest` も `/history/` も呼んでいない (`frontend/src/components/RightSidebar.tsx:555-604`)。→ `world-view.md` の「時系列データ」は実装と食い違う。`items-and-files.md` の書き方 (名前/値/記録時刻) は実装と一致する。
  - **`/api/observer/{id}/latest` と `/history/` を呼ぶフロントエンドのコードが見つからなかった。**
  - `observer_read` スペルは `spell=True` だが `spell_visible=False` で、UI と head のスペル一覧から隠されている。コメントに「オブザーバー機能自体がまだ利用できないため…復帰条件: オブザーバーの出荷時に spell_visible=True へ戻す (2026-09-01 裁定)」とある (`builtin_data/tools/observer_read.py:85-91`)。→ **ペルソナからの実行は可能だが、ペルソナはそのスペルの存在を知らされない。**
- **追跡できていない境界**: `spell_visible=False` のスペルをペルソナが実際にどう呼べるのか (名前を知っていれば呼べるのか) は Spell 領域の担当。
- **既存テスト**: `tests/test_visual_context_feed_stand.py` — 観測値形式のキーが部屋の様子に描かれること (observer_manager は SimpleNamespace の fake)。`tests/test_room_state_diff.py` — 同上。**API 2 ルートと設置物モーダルの検査は見つからなかった。**
- **状態**: `機能の存在=✓` `期待の根拠=△ (文書 2 件が食い違う)` `挙動の静的確認=✓` `テスト対応=△`

### WORLD-38: 観測値の閾値超過を建物に通知する

- **入口**: 自動処理。`record_metrics` (pull / push どちらも) の後に `_evaluate_notify_rules` (`saiverse/observer_manager.py:656-705`)
- **結果**: `NOTIFY_RULES_JSON` の `above` / `below` を満たしたら、Fixture の設置 Building へ host 行を挿入する (`_notify_building`, `:707-728`)。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/observer.md` §守るべき不変条件 5 (「通知は既存パイプラインに乗せる」)、§決定事項記録 (「初版スコープは通知まで」)
- **現在の挙動 (静的確認)**: `_notify_building` は `add_building_event(building_id, {...})` を **`heard_by` を渡さずに**呼ぶ。`add_building_event` の既定は空リスト (`manager/history.py:112-114`) で、転記側は `heard_by` に自分が入っている行しか拾わない (`builtin_data/tools/get_building_messages.py:373-380`)。→ **この通知はどのペルソナの記憶にも届かない読みになる。** アイテム系が持っている代替経路 (`broadcast_item_event` → `record_persona_event`) に相当するものも `_notify_building` には無い。ユーザーの画面には建物ログとして出る。**確認: コードの読みのみ。実行はしていない。**
- **追跡できていない境害**: `event_type: "observer_alert"` を拾う別の消費者がいないかは全文検索していない (`observer_alert` の grep はしていない)。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△ (届き先が静的に不成立の読み)` `テスト対応=✗`

### WORLD-39: フェノメノンのルールを管理する

- **入口**: 左サイドバー → システム → フェノメノン (**開発者モードのときだけ導線が出る**、`frontend/src/components/Sidebar.tsx:485-497`) → `/phenomena` 画面。`GET/POST/PUT/DELETE /api/phenomena/rules[/{id}]`、`GET /api/phenomena/{available,triggers}`
- **まとめた根拠**: 7 ルートはすべて 1 画面 (`frontend/src/app/phenomena/page.tsx`) のためのルール CRUD + 選択肢供給。
- **結果**: `phenomenon_rule` 行の増減。トリガー種別とフェノメノン名は登録済みのものだけを受け付ける (400 で拒否)。JSON 欄は構文検査つき。
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md` §4 Phenomena。`利用者向け説明` — `docs/user-guide/world-view.md` §左サイドバー (「開発者モード時はフェノメノンも」)
- **現在の挙動 (静的確認)**: 利用可能なフェノメノンは `builtin_data/phenomena/` の 2 本 (`example_log`, `inject_persona_event`) + 三層読み込みで拡張されたもの。
- **追跡できていない境界**: `expansion_data/` のアドオンが供給するフェノメノンは gitignore 対象なので、リポジトリだけからは数えられない。
- **既存テスト**: `tests/test_pulse_dispatcher_phenomenon.py` — `dispatch_phenomenon_event` の成否判定。**ルール CRUD ルートの検査は見つからなかった。**
- **状態**: `機能の存在=✓ (開発者モード)` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

### WORLD-40: 世界の出来事がフェノメノンを発火させる

- **入口**: 自動処理。`SAIVerseManager._emit_trigger(TriggerType, data)` → `PhenomenonManager.emit(TriggerEvent)` → ルール評価 → フェノメノン実行 (`phenomena/manager.py:76-201`)
- **結果**: ルールに合致すれば `inject_persona_event` などが走り、新しい Pulse が起動する (LLM 課金が発生しうる)。
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md` §4 Phenomena、`docs/reference/api-endpoints.md` §phenomena
- **現在の挙動 (静的確認)**: **`TriggerType` は 11 種類宣言されているが、リポジトリ内で実際に `_emit_trigger` されるのは 4 種類だけ**:
  - 発火する: `SERVER_START` (`saiverse/saiverse_manager.py:565`)、`SERVER_STOP` (`:1154`)、`USER_MOVE` (`manager/runtime.py:327`)、`PERSONA_MOVE` (`manager/runtime.py:346`)
  - 発火箇所を見つけられなかった: `USER_SPEECH` / `PERSONA_SPEECH` / `USER_LOGIN` / `USER_LOGOUT` / `SCHEDULE_FIRED` / `EXTERNAL_WEBHOOK` (`phenomena/triggers.py` の `TRIGGER_SCHEMAS` にしか現れない)
  - `X_POLL_DETECTED` はリポジトリ外のアドオン (`expansion_data/saiverse-x-addon/integrations/polling.py:401`、gitignore 対象) が発火する
  - `/phenomena` 画面のトリガー選択肢は `TRIGGER_SCHEMAS` 全 11 種を出すので (`api/routes/phenomena.py:231-240`)、**利用者は決して発火しないトリガーでルールを作れる**。
  - `PERSONA_MOVE` は `RuntimeService._move_persona` を通る移動でしか出ない (WORLD-15 参照)。
- **追跡できていない境界**: アドオンが供給するトリガーの全体像はリポジトリからは数えられない。
- **既存テスト**: `tests/test_pulse_dispatcher_phenomenon.py` — 実行顛末の型付き成否。**トリガー発火からルール評価までを通すテストは見つからなかった。**
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

### WORLD-41: Blueprint (ペルソナ生成テンプレート) を管理する / spawn する

- **入口**: ワールドエディタ → Blueprint タブ。`POST/PUT/DELETE /api/world/blueprints[/{id}]`、`POST /api/world/blueprints/{id}/spawn` (`api/routes/world.py:491-509`)
- **まとめた根拠**: 4 ルートは 1 タブの CRUD + 実行。
- **結果**: `blueprint` 行の増減。spawn は指定 Building にペルソナを生成し、その建物へ「Blueprint Spawn」の host 行を **heard_by つきで**挿入する (`manager/blueprints.py:340-355`)。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-editor.md` §タブ構成 (「Blueprint = ペルソナ生成テンプレート」)。それ以上の説明は無い
- **現在の挙動 (静的確認)**: spawn は `building_name` を受け取る (ID ではない)。生成したペルソナは共通初期化フックを通り、occupants に加わる。
- **追跡できていない境界**: `spawn_entity_from_blueprint` の中身 (ペルソナ生成の全体) はペルソナ領域の担当なので追っていない。
- **既存テスト**: `tests/test_persona_creation_wiring.py` に一部関連 (`svc.add_building_event` の fake 化)。Blueprint ルートの検査は見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=△ (一行のみ)` `挙動の静的確認=△` `テスト対応=✗`

### WORLD-42: Tool テーブルを管理する (レガシー・実効性なし)

- **入口**: ワールドエディタ → ツールタブ。`POST/PUT/DELETE /api/world/tools[/{id}]` (`api/routes/world.py:511-521`)
- **結果**: `tool` テーブルの行の増減。
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-editor.md` の注記 (「ツールタブと Building へのツール紐付け UI（`BuildingToolLink`）が**残っているが、現在は実効性がない**」)。CLAUDE.md も同じ (「Tool + BuildingToolLink is a legacy table and currently unused」)
- **現在の挙動 (静的確認)**: UI は 2 画面 (ワールドエディタのツールタブ、Building 設定モーダルの「利用可能なツール」) に残っている。ペルソナへツールが届くのは Spell (`spell=True`) と Playbook の TOOL ノードだけ。
- **追跡できていない境界**: なし
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓ (だが効果なし)` `期待の根拠=✓ (文書が実効性なしと明記)` `挙動の静的確認=✓` `テスト対応=✗`

### WORLD-43: 壊れた建物ログを隔離して復旧する

- **入口**: 起動時アラートバナー → 「対応する」→ 隔離モーダル。`GET /api/system/quarantine`、`POST /api/system/quarantine/{id}/{restore,reset}` (`api/routes/system.py:198-330`, `frontend/src/components/QuarantineModal.tsx`)
- **結果**: 隔離中の Building 一覧の表示、バックアップからの復元 (`log.json` を差し替え)、リセット。復元時に全ペルソナの pulse_cursors を clamp する。
- **期待の根拠**: `実装のみ (根拠なし)` — `docs/user-guide/` に隔離の説明を見つけられなかった。コード内 docstring (`manager/initialization.py:404-411`) が唯一の仕様
- **現在の挙動 (静的確認)**: **隔離を立てる検出器が呼ばれていない。**
  - `_quarantine_building` (`manager/initialization.py:394-480`) は `quarantined_buildings` を埋める唯一の書き込み口だが、**リポジトリ内でこの関数を呼ぶコードが見つからなかった** (grep の結果は定義行 1 件のみ)。
  - `_init_building_histories` (`manager/initialization.py:191-202`) には「Phase 2+3 以降は DB が source of truth。**旧 log.json 5 状態判定 / quarantine 起動時バックアップは廃止**し、in-memory `building_histories` dict は legacy caller 互換のため空 dict で初期化するのみ」と書かれている。
  - つまり `quarantined_buildings` は常に空のままで、それを消費する 6 箇所 — 移動の門 (`saiverse/occupancy_manager.py:246`)、`add_building_event` の拒否 (`manager/history.py:103`)、チャットの拒否 (`api/routes/chat.py:289`)、ゲートウェイ (`manager/gateway.py:393`)、ペルソナの履歴書き込み (`persona/history_manager.py:220, 251`)、隔離 API/UI — がすべて発動しない読みになる。
  - 復旧側は `log.json` を読み書きするが、`log.json` はもう書かれていない (WORLD 接点表 / §3 参照)。
  - **確認: コードの読みのみ。実行して確かめてはいない。**
- **追跡できていない境界**: DB (`building_messages`) が壊れたときの検出・隔離に相当する仕組みが別にあるかは追えていない (`saiverse/legacy_log_import.py` の起動時検算は「旧 log.json が DB へ取り込めているか」を見るもので、DB 自体の破損検出ではない)。
- **既存テスト**: 該当テストなし (`quarantined_buildings` を扱うテストは `tests/` 内に見つからなかった)
- **状態**: `機能の存在=△ (消費側だけ残り、検出側が不在)` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

### WORLD-44: City 間の移動 (凍結)

- **入口**: `/api/inter-city/*`、`/api/persona-proxy/{id}/think` (`database/api_server.py:90-140`)、`manager/visitors.py` の `dispatch_persona` / `return_visiting_persona`
- **結果**: **API は 503 + 凍結メッセージ。** manager 側のメソッドは冒頭で `return False, MULTI_CITY_FREEZE_MESSAGE` して以降のコードに入らない (`manager/visitors.py:42, 151`)。`VisitingAI` / `ThinkingRequest` テーブルのポーリングは登録されない。
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md` §8 (2026-07-16 まはー裁定、入口の明示封鎖)。CLAUDE.md も同じ
- **現在の挙動 (静的確認)**: 凍結は「黙って動かない」ではなく 3 重の明示封鎖。再有効化フラグは意図的に無い。
  - ただし **City を作る口 (WORLD-01) と City を編集する口 (WORLD-02、オンラインモード含む) は開いている**。「2 つ目の City は作れるが、そこへ行く手段は封鎖されている」状態。
  - `manager/visitors.py:285, 348` の「City Transfer」host 行 (heard_by つき) は凍結された経路の中にあるので、現在は書かれない。
- **追跡できていない境界**: SDS (`sds_server.py`) は landscape §8 で「実質冬眠中」とされているが、コードを読んでいない。
- **既存テスト**: `tests/test_multi_city_freeze.py` (6 件) — ①API が 503 + 凍結メッセージ ②DB polling が登録されないこと ③manager メソッドが封鎖メッセージで即 return すること。TestClient + `inspect` によるソース検査。
- **状態**: `機能の存在=✗ (凍結)` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---


### 矛盾・疑義

1. **建物履歴の保存先** — CLAUDE.md §Memory and History は「Building chat history is kept in memory and logged to `~/.saiverse/cities/<city>/buildings/<building>/log.json`」と書く。実装では `_save_modified_buildings` が no-op (`manager/history.py:85-88`)、`_init_building_histories` は「Phase 2+3 以降は DB が source of truth」(`manager/initialization.py:191-202`)、`log.json` を書くコードを見つけられなかった (読むのは `saiverse/legacy_log_import.py` の起動時取り込みだけ)。どちらが正しいかは断定しない。

2. **Fixture / Observer の実装状態** — `docs/overview/landscape.md` §2 は「`observer.md` v0.1、**設計のみ・未実装**」。`docs/overview/in_flight.md:103` も「構想止まり(当分動かない)…intent draft で管理: observer/Fixture」。一方、`fixture` / `observer_config` / `observer_metrics` テーブル、`saiverse/observer_manager.py` (728 行)、`api/routes/observer.py` (6 ルート)、右サイドバーの「設置物」欄と設置物モーダル、`observer_read` スペル、起動時の `start_pull_observers()` (`saiverse/saiverse_manager.py:530`) はすべて存在する。両方を残す。

3. **ACTIVITY_STATE** — `docs/user-guide/world-editor.md` §AIs タブ (「アクティビティ状態 … `ACTIVITY_STATE`: Stop / Sleep / Idle / Active」) と `docs/overview/landscape.md` §2 Persona (同上) が今も書いている。CLAUDE.md は「2026-07-14 に解体・カラム削除」。API の `AIUpdate` にも該当欄は無い (`api/routes/world.py:118-133`)。既知ドリフト (依頼元の予備調査にも記載) だが、landscape 側にも同じ記述があることを追加で記録する。

4. **設置物モーダルが「時系列データ」を出すか** — `docs/user-guide/world-view.md` §右サイドバー は「Observer 系はクリックで観測値（**時系列データ**）を表示」。`docs/user-guide/items-and-files.md` §設置物 は「クリックで観測値（名前 / 値 / 記録時刻）を表示」。実装 (`frontend/src/components/RightSidebar.tsx:555-604`) は `state_json` のスナップショットを表で出すだけで履歴 API を呼ばない。文書 2 件どうしも食い違っている。

5. **`saiverse://building/...` の 2 形式** — `docs/reference/saiverse-uri.md` の表に `building/{id}/items` と `building/{id}/history` が載っているが、実装は存在しないメソッド (`list_items_in_building`) と存在しない属性 (`manager.buildings.get` / `Building.history`) を呼ぶ (`saiverse/uri_resolver.py:771, 797`)。実行して確かめてはいないが、静的には `except Exception` に落ちてエラー文字列になる読みになる。

6. **定員の 0** — `docs/user-guide/world-editor.md` §Buildings タブ は「Capacity 定員（0=無制限）」。実装は `current_ai >= capacity_limit` で拒否するので (`saiverse/occupancy_manager.py:274-287`)、0 は無制限ではなく「常に拒否」になる読み。断定はしない (実行していない)。

7. **ワールドエディタに Region タブが無い** — `docs/user-guide/world-editor.md` §タブ構成 は 7 タブを列挙し Region に触れない (文書としては正しい)。一方 `docs/intent/region.md` は Status: v0.2 確定で、Region CRUD API も `manager/admin.py` の実装も `tests/test_region_admin.py` の 45 件もある。**「確定した設計だが利用者が触る口が無い」**という状態そのものが、どちらの文書からも読み取れない。

8. **Building 作成の戻り文言** — `manager/admin.py:449-452` は「A restart is required for it to be usable.」を返すが、`saiverse/saiverse_manager.py:1826-1828` が `_reload_buildings()` を呼んで即時反映する。削除も同様 (`:1888`)。

9. **`docs/user-guide/city-map.md` の編集モード** — ドラッグ配置と背景画像しか書いていないが、実装には街の表示名のインライン編集がある (`frontend/src/components/CityMap.tsx:594-605`、`PATCH /api/world/cities/{id}/name`)。

10. **`api/routes/world.py` に Playbook 管理が同居している** — `/api/world/playbooks*` の 7 ルートは Playbook の CRUD + import で、City/Building/Item のような「世界」とは別概念。`docs/reference/api-endpoints.md` も world 節に並べている。領域分けの上では疑義として残す。

11. **`docs/reference/api-endpoints.md` の world 節は実装と一致していた** (48 ルートすべて記載)。矛盾ではないが、自動生成が効いている証拠として記録する。

12. **`api/routes/world.py:1-4` の未使用 import** — `UploadFile` / `File` / `Form` / `shutil` がインポートされているが使用箇所が無い。かつてこのファイルにアップロード処理があった痕跡と読めるが、断定はしない。

---

### 凍結・開発者専用・到達不能・文書のみ

**凍結 (明示的に止めてある)**

- **inter-city travel / multi-city** (WORLD-44)。`/api/inter-city/*` と `/api/persona-proxy/{id}/think` は 503 + 凍結メッセージ。`dispatch_persona` / `return_visiting_persona` は冒頭で return。`VisitingAI` / `ThinkingRequest` のポーリングは登録されない。`tests/test_multi_city_freeze.py` が封鎖を固定している。**ただし City の作成・編集・削除の口は開いたまま。**
- **SDS (SAIVerse Directory Service)** — landscape §8 で「実質冬眠中」。City の `START_IN_ONLINE_MODE` は既定 off。コードは読んでいない。

**開発者モードでのみ到達できる**

- **フェノメノン管理画面** (`/phenomena`)。左サイドバー → システムの導線が `developerMode` で囲まれている (`frontend/src/components/Sidebar.tsx:485`)。URL 直打ちなら到達できる。

**API はあるが UI から到達できない**

- **Region の作成・編集・削除・Building の所属変更** (WORLD-18) — 4 ルート。ワールドエディタに Region タブが無い。
- **Ruler の生成** (WORLD-20) — `POST /api/world/regions/{id}/ruler`。呼び出し元が見つからない。
- **ゲームの開始・一時停止・再開・終了** (WORLD-21) — 4 ルート。フロントが呼ぶのは `game/log` と `game/rejoin` だけ。
- **Fixture の作成・単体取得・建物一覧** (WORLD-34) — `/api/observer/fixture*` 3 ルート。UI からは作成できない (表示だけ `/api/info/details` 経由)。
- **Observer の作成** (WORLD-35) — `POST /api/observer/config`。
- **観測値の最新値・履歴 API** (WORLD-37) — `/api/observer/{id}/latest`、`/api/observer/{id}/history/{metric}`。設置物モーダルはこれらを呼ばない。
- **Observer への push** (WORLD-36) — 外部アプリ向けなので UI が無いのは設計どおり。`OBSERVER_PUSH_TOKEN` 未設定なら 503。

**API にあるが片方向 (対になる操作が無い)**

- Fixture: 作成 (upsert) と取得はあるが**更新・削除が無い**。
- Observer: 作成 (upsert) はあるが**取得・一覧・更新・削除が無い**。
- ペルソナの持ち物: 一覧はあるが**移動・削除の口が無い** (ユーザー側から)。

**ペルソナからは呼べるが隠されている**

- **`observer_read` スペル** — `spell=True` かつ `spell_visible=False`。UI と head のスペル一覧に出さない (2026-09-01 裁定、`builtin_data/tools/observer_read.py:85-91`)。復帰条件は「オブザーバーの出荷時」。

**残っているが効果がない**

- **Tool テーブル / BuildingToolLink** (WORLD-42) — ワールドエディタのツールタブと Building 設定モーダルの「利用可能なツール」の 2 画面。CLAUDE.md と `docs/user-guide/world-editor.md` が実効性なしと明記している。
- **`building.system_instruction` へのアイテム一覧の差し込み** (WORLD-07) — 書き手 2 箇所、読み手を見つけられなかった。
- **建物ログの隔離機構** (WORLD-43) — 消費側 6 箇所と復旧 API / UI が残り、隔離を立てる `_quarantine_building` の呼び出し元が無い。
- **決して発火しないトリガー種別 6 つ** (WORLD-40) — `USER_SPEECH` / `PERSONA_SPEECH` / `USER_LOGIN` / `USER_LOGOUT` / `SCHEDULE_FIRED` / `EXTERNAL_WEBHOOK`。`/phenomena` 画面の選択肢には出る。
- **在室者の「話しかけやすさ」欄** — `OccupantInfo` の `life_state` / `life_until` / `activity_label` は応答モデルに残るが常に None (`api/routes/info.py:112-127`)。

**文書にしかない / 静的に不成立**

- `saiverse://building/{id}/items` と `saiverse://building/{id}/history` (WORLD-33)。`docs/reference/saiverse-uri.md` の表に載っている。
- 定員 0 = 無制限 (WORLD-17)。`docs/user-guide/world-editor.md` に書かれている。
- 設置物モーダルの「時系列データ」表示 (WORLD-37)。`docs/user-guide/world-view.md` に書かれている。

---

### この領域で「検査が無い」と判断した重要な結果

利用者またはペルソナに見える結果のうち、既存テストがまったく触れていないもの。

1. **世界編集の API ルート層が丸ごと未検査**。`api/routes/world.py` の 48 ルートを 1 本でも通すテストを見つけられなかった (`tests/test_city_identity.py` が City の表示名 PATCH を TestClient で通すのが唯一の例外)。service 層 (`manager/admin.py`) には `tests/test_region_admin.py` (45 件) と `tests/test_building_admin_id.py` があるが、ルート → service の配線・`_check_result` のエラー変換・Pydantic の型は検査されていない。

2. **Building を削除したとき何が残るか**。会話ログ (`building_messages`)、Fixture、その部屋のアイテムの location 行、realtime spell binding はどれも消えず、FK も強制されていない読み。削除の事前検査 (在室者チェック) にもテストが無い。**利用者から見て「部屋を消したら会話はどうなるのか」に答えるものが、コードにも文書にもテストにも無い。**

3. **`heard_by` を空で書いた建物ログ行は誰の記憶にも届かない**、という契約がどこにも固定されていない。`add_building_event` の既定が空リストであることと、転記側が `heard_by` で絞ることは別々のファイルにある。Observer の閾値通知 (WORLD-38) がこれに当たる読みで、テストは無い。

4. **移動の定員拒否**。`move_entity` の定員判定 (`saiverse/occupancy_manager.py:274-287`) を通すテストが見つからなかった。入口トポロジー (21 件) と game ゲート (11 件) と CAS (多数) は厚いのに、いちばん素朴な「満室で入れない」だけが薄い。文書が「0=無制限」と言っているところでもある。

5. **`saiverse://` の building スキーム 2 形式**。`docs/reference/saiverse-uri.md` に載っている機能が、静的には存在しないメソッド/属性を呼んでいる。`tests/test_uri_resolver_memopedia.py` は `manager=None` で構築するので building スキームには到達できない。

6. **メディア URI のパス構文**。`resolve_media_uri` に `..` を含むファイル名を渡す検査が無い (WORLD-32)。アップロード側の `safe_filename` / `enforce_allowed_file_path` は `tests/test_api_file_boundaries.py` で検査されているのに、URI 解決側だけが素通しになっている。**同じ危険に対する手当てが片側にしか入っていない形。**

7. **アイテム作成・編集・削除・トグルの全経路**。`POST/PUT/DELETE /api/world/items`、`PUT /api/info/item/{id}/content`、`POST /api/info/item/{id}/toggle-open`、`GET /api/info/item/{id}` のパス復旧戦略 8 通り。いずれもテストを見つけられなかった。ペルソナの `pickup_item` / `place_item` / `use_item` も同様。

8. **アイテム作成が LLM を呼ぶこと**。説明を空にすると課金が発生する (WORLD-22)。チャット添付側には `tests/test_attachment_paths.py::test_sync_summary_*` があるが、ワールドエディタからの作成経路には無い。

9. **Observer の pull 実行・push 受信・閾値通知**。`saiverse/observer_manager.py` の中核 3 機能 (`_execute_pull` / `record_metrics` / `_evaluate_notify_rules`) を通すテストが `tests/` に見つからない (`tests/test_feeds_api.py` は feeds ルート経由で `create_fixture` を通すのみ)。push の冪等性欠如は `docs/issues/observer_push_resend_idempotency.md` として起票済み。

10. **フェノメノンのトリガー発火からルール評価まで**。`_emit_trigger` → `PhenomenonManager.emit` → ルール照合 → フェノメノン実行の鎖を通すテストが無い。`tests/test_pulse_dispatcher_phenomenon.py` は鎖の末端 (`dispatch_phenomenon_event`) だけを扱う。**「決して発火しないトリガー 6 種」も、この鎖に検査があれば見つかる形の欠陥。**

11. **隔離機構が起動しないこと**。`quarantined_buildings` を扱うテストが 1 件も無いので、検出器が呼ばれなくなったことは検査に引っかからない。移動・チャット・履歴書き込みの 4 箇所が「隔離中なら止める」と書いてあるのに、それが一度も発動しない状態がテストから見えない。

12. **街マップの配置保存・背景画像・Region スコープ表示**。`PUT /api/world/buildings/positions`、`PATCH .../map-background`、`GET /api/info/city-map?region_id=` にテストが無い。`tests/test_city_identity.py` が city-map の表示名フォールバックだけを通す。

---

## 領域 D. ペルソナと利用者の設定・導入導線 (PERS-01〜29)


### PERS-01: ペルソナを作る (ペルソナ作成ウィザード)

- **入口**: サイドバー「+」→ `PersonaWizard`。名前・ID・City・システムプロンプトを入れて `POST /api/world/ais`。根拠: `frontend/src/components/PersonaWizard.tsx:173-187`, `api/routes/world.py:454-460`
- **結果**: `ai` 行 + 私室 Building 行 + 入室ログ (`building_occupancy_log`) が 1 トランザクションで作られ、ユーザーが 1 人しかいなければ `user_ai_link` も自動で張られる。commit 後にインメモリの Building / PersonaCore / 各種 map へ登録し、`_on_persona_registered` を呼ぶ。LLM 課金は発生しない。根拠: `manager/persona.py:444-695`
- **期待の根拠**: `利用者向け説明` — `docs/overview/roadmap_status.md` §6 「チュートリアル … 最低限のみ実装。拡充が課題」。`CLAUDE.md` Quick Reference「Create a persona: frontend UI, or ask Genesis in 創造の祭壇」
- **現在の挙動 (静的確認)**:
  - 新規ペルソナの `DEFAULT_MODEL` には `self.model` (= グローバル上書き。既定 None) が入るだけで、ウィザードはモデルを一切設定しない。根拠: `manager/persona.py:553`, `frontend/src/components/PersonaWizard.tsx:176-186`
  - `LIGHTWEIGHT_MODEL` は渡されないので NULL。実行時は `sea/runtime.py:700-720` が「ペルソナの軽量クライアントが無ければ既定の軽量モデルで一時クライアントを作る」ので、無設定でも router ノードは動く経路がある。根拠: `persona/core.py:255-278`, `sea/runtime.py:700-720`
  - ID は名前の slug、日本語名なら `persona_<連番>`。フロントは初期値を出すだけで、ユーザーが ID 欄を触っていなければ `ai_id: null` を送って採番をサーバーへ任せる。根拠: `PersonaWizard.tsx:118-138, 180-186`
  - 大文字小文字を畳んだ ID 重複検査あり (フォルダ名になるため)。根拠: `manager/persona.py:506-521`
  - `AdminService.create_ai` は成功メッセージに "A restart is required for the AI to become active." を含めるが、`_create_persona` は同じ呼び出しの中で PersonaCore を `self.personas` へ登録している。ウィザードはこのメッセージを表示していない。根拠: `manager/admin.py:1190-1197`, `manager/persona.py:679-685`, `PersonaWizard.tsx:196-209`
  - ステップ 2 は ChatGPT ログのインポート (`MemoryImport`)。スキップ可。根拠: `PersonaWizard.tsx:312-328`
- **追跡できていない境界**: `_on_persona_registered` の中身 (どの機構が初期化されるか) は未読。`MemoryImport` の実処理 (`api/routes/people/import_chatlog.py`) は他領域と重なるため未読。
- **既存テスト**: `tests/test_persona_creation_wiring.py` — `PersonaMixin._create_persona` を実 DB (一時 sqlite) で呼び、ID 採番・私室 Building ID の一意化・commit 順序・`_on_persona_registered` の呼び出しを検証する。**PersonaCore とモデル設定解決はスタブに差し替え**ている (docstring 冒頭に明記)。HTTP 経由 (`POST /api/world/ais`) のテストは無い。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (配線は固定されているが、API 層とウィザードの挙動は未検査)

### PERS-02: ペルソナを作る / 編集する (ワールドエディタ AIs タブ)

- **入口**: グローバル設定 → ワールドエディタ → AIs タブ。「+ 新規作成」→ `POST /api/world/ais`、既存選択 → `PUT /api/world/ais/{id}`。根拠: `frontend/src/components/settings/WorldEditor.tsx:390-392`
- **結果**: 作成は PERS-01 と同じ経路。編集は `AIUpdate` → `SAIVerseManager.update_ai` → `AdminService.update_ai`。根拠: `api/routes/world.py:462-464`, `manager/admin.py:1200-1430`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-editor.md` §AIs タブ
- **現在の挙動 (静的確認)**:
  - フォームが読み書きするのは 名前 / ホーム都市 / デフォルトモデル / 軽量モデル / **自律行動** / アバター / 外見画像 / システムプロンプト / 説明 の 9 項目。根拠: `WorldEditor.tsx:384-389, 640-679`
  - `AIUpdate` の残りのフィールド (chronicle_enabled 等) は `Optional[...] = None` で、`update_ai` 側が None を「変更なし」として扱うため、ここで保存しても SettingsModal 専用の設定は消えない。根拠: `api/routes/world.py:126-140`, `manager/admin.py:1276-1323`
  - ただし `default_model` / `lightweight_model` は None 判定を経ず `ai.DEFAULT_MODEL = default_model or None` で必ず上書きされる。フォームは選択中の値を載せるので通常は同値だが、`AIUpdate` は両方 `Optional[str]` (既定値なし=必須) なので、これらを含まないリクエストは 422 になる。根拠: `manager/admin.py:1265-1266`, `api/routes/world.py:131-132`
  - `create` はフォームの name / system_prompt / home_city_id だけを送る (ID 指定なし)。根拠: `WorldEditor.tsx:390`
- **追跡できていない境界**: `apiCall` の共通エラー表示 (`WorldEditor.tsx` 内) は未読。
- **既存テスト**: `tests/test_admin_ai_edit_contract.py` — `AdminService.__new__` で属性だけ注入したインスタンスに対し `update_ai` / `get_ai_details` を実 DB で往復させる。**LLM クライアント再生成とアバター変換は `personas={}` と `_set_persona_avatar` の差し替えで通らない**。HTTP (`PUT /api/world/ais/{id}`) は未検査。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (manager 層のみ)

### PERS-03: ペルソナ一覧 (`GET /api/people/`)

- **入口**: アドオン管理 UI の「ペルソナ別設定」。根拠: `api/routes/people/__init__.py:24-41` (docstring に用途を明記)
- **結果**: `ai` テーブル全行の `{id, name}`。ページングなし。根拠: 同上
- **期待の根拠**: `実装のみ (根拠なし)` — docstring に「AddonManagerModal が叩いていたが 404 だったので足した」とあるだけで、仕様文書は見つからなかった。
- **現在の挙動 (静的確認)**: `PERSONA_ROLE`(ruler) も dispatched も除外せず全件返す。根拠: `api/routes/people/__init__.py:38-39`
- **追跡できていない境界**: `AddonManagerModal` 側の消費のしかたは未読 (アドオン領域)。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

### PERS-04: ペルソナを呼ぶ / 自室に戻す (召喚・退室)

- **入口**: 右サイドバーの「ペルソナ管理」(`PeopleModal`) の「呼び出し」タブ、またはペルソナメニューの「Return to Room」。`POST /api/people/summon/{id}?building_id=...` / `POST /api/people/dismiss/{id}?building_id=...`。根拠: `frontend/src/components/PeopleModal.tsx:84-87, 113-116`, `frontend/src/components/PersonaMenu.tsx:38-41`
- **結果**: `manager.summon_persona` → OccupancyManager 経由で在室が変わる。建物履歴に入退室が残る (未確認)。LLM 課金は召喚自体では発生しない。根拠: `api/routes/people/summon.py:110-140`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/world-view.md` (ペルソナメニューの開き方として persona-settings.md から参照されている)
- **現在の挙動 (静的確認)**:
  - `GET /api/people/summonable` は `manager.personas` を自前で走査し、`is_dispatched` と「既にここにいる」だけを除外する。**`persona_role`(ruler) の除外が無い**。根拠: `api/routes/people/summon.py:20-27`
  - 同じ意味の関数 `RuntimeService.get_summonable_personas` は ruler を除外する。API はこちらを呼んでいない。根拠: `manager/runtime.py:354-366`
  - `PeopleModal` は `currentBuildingId` が渡されないと何もしない (server-global へのフォールバックを禁止。2026-04-30 の上書き事故対策)。根拠: `PeopleModal.tsx:47-54`
  - API 側は `building_id` 省略時に `manager.user_current_building_id` へフォールバックする。根拠: `summon.py:122`
- **追跡できていない境界**: `manager.summon_persona` / `dismiss` の内部 (OccupancyManager) は領域 (世界) 側。
- **既存テスト**: `tests/test_game_lifecycle.py` が `summon` 系に触れる (Region RPG 文脈)。`GET /api/people/summonable` の ruler 除外を見るテストは見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=△` (world-view.md は未読) `挙動の静的確認=✓` `テスト対応=✗`

### PERS-05: ペルソナ設定の読み出しと保存 (ペルソナ設定モーダル)

- **入口**: ペルソナメニュー →「Settings」。`GET /api/people/{id}/config` で読み、「保存」で `PATCH /api/people/{id}/config`。根拠: `frontend/src/components/SettingsModal.tsx:193, 317-384`
- **結果**: `ai` 行の 20 列前後を更新し、ロード済み PersonaCore の属性 (名前・プロンプト・自律・各モデル) を即時反映、必要なら LLM クライアントを作り直す。失敗すると応答に `warning` が付き、フロントが alert で見せる。根拠: `manager/admin.py:1324-1424`, `api/routes/people/config.py:140-179`, `SettingsModal.tsx:386-395`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/persona-settings.md` (ただし内容が古い。§3 参照)
- **現在の挙動 (静的確認)**:
  - 画面に出る項目: デフォルト/軽量/Memory Weave/画像/音声/動画モデル、デバッグコントローラー、キャッシュ維持 2 項目、応答待ち Track 自動 pause 閾値、Chronicle 自動生成、Chronicle 読み込み文字数、自律行動中の Chronicle、自動想起、Memory Weave コンテキスト、Memopedia 索引常時表示、コア記憶文字数、スペル、リアルタイム情報、事前実行スペル、リンクユーザー、アバター、外見画像、説明、システムプロンプト。名前は読み取り専用。根拠: `SettingsModal.tsx:421-973`
  - **自律行動 ON/OFF は画面に出ない**が、state はロード→保存で往復する (送らないと PATCH が既定値で塗り潰すため)。根拠: `SettingsModal.tsx:99-102, 217, 329`
  - 別ペルソナ上書き事故 (2026-04-30) の対策として、ロード元 persona_id と保存先 persona_id の一致を保存前に検査し、不一致なら拒否する。根拠: `SettingsModal.tsx:294-313, 984-990`
  - `META_JUDGMENT_CONFIG` は丸ごと置換なので、このモーダルが編集しないキーはロード値からマージして送り返す。根拠: `SettingsModal.tsx:361-374`
  - フロントの `AIConfig` インターフェースには `spell_enabled` と `chronicle_char_budget` の宣言が無いが、`res.json()` の戻りを any として読むため実行時には効いている。根拠: `SettingsModal.tsx:27-50` と `:223-233` の対比
  - 反映タイミング: モデル・プロンプト・自律は保存時に PersonaCore へ即時反映。Chronicle 文字数の説明文は「反映は次の記憶の整理から」と明示。Memopedia 索引の説明文は「次に記憶の整理が起きたときに反映」。根拠: `SettingsModal.tsx:668, 736`, `manager/admin.py:1327-1414`
  - 旧 `POST /{id}/organize-memory` は 2026-09-01 に撤去され、手動の畳みは `POST /{id}/arasuji/generate` へ一本化された。根拠: `api/routes/people/config.py:182-186`, `PersonaMenu.tsx:59-87`
- **追跡できていない境界**: `avatar_path` が GET では URL 化 (`avatar_path_to_url`) されて返り、PATCH ではその URL がそのまま `AVATAR_IMAGE` へ書かれる往復が値を保存し続けられるかは追えていない。
- **既存テスト**: `tests/test_admin_ai_edit_contract.py` (manager 層の往復契約)。`/api/people/{id}/config` を HTTP で叩くテストは見つからなかった。フロントの一致検査 (上書き事故対策) のテストも無い。
- **状態**: `機能の存在=✓` `期待の根拠=✓` (ただし文書が古い) `挙動の静的確認=✓` `テスト対応=△`

### PERS-06: 自律行動の ON/OFF (`AUTONOMY_ENABLED`)

- **入口**: ワールドエディタ AIs タブの「自律行動」チェックボックスのみ。ペルソナ設定モーダルからは v0.3 で非表示。根拠: `WorldEditor.tsx:655-664`, `SettingsModal.tsx:99-102`
- **結果**: `ai.AUTONOMY_ENABLED` を書き換え、ロード済みペルソナの `autonomy_enabled` を更新し、`ensure_autonomy_for` を呼ぶ。根拠: `manager/admin.py:1255-1259, 1331, 1342-1353`
- **期待の根拠**: `既存の仕様文書` — `docs/concepts/persona.md:17`「自律行動の ON/OFF は AUTONOMY_ENABLED の 1 本だけ。OFF にすると時間割も判断点も発火しない (会話への返答は止まらない)」。`docs/intent/autonomous_behavior_v3.md` §11.1 (止め具の裁定)
- **現在の挙動 (静的確認)**:
  - `saiverse/autonomy_wiring.py:91` に `AUTONOMOUS_DRIVING_SHIPPED = False` があり、`is_autonomy_on()` は**この定数が False の間ペルソナの設定値に関わらず常に False を返す**。根拠: `saiverse/autonomy_wiring.py:199-224`
  - `is_autonomy_on` を通るのは判断点 (`:455`, `:1013`, `:1138`)・watchdog (`:1647`)・起動時の再確立 (`saiverse_manager.py:1226-1228`)・`ensure_autonomy_for` (`saiverse_manager.py:1532-1547`)。
  - したがって **v0.3 ではワールドエディタのチェックボックスを ON/OFF しても駆動側の挙動は変わらない**。intent §11.1 は「v0.3 では切り替え UI を隠した」と書いているが、ワールドエディタ側は隠されていない。
  - 会話への返答は自律ゲートを通らない (intent §11.1「止めないもの」)。実装上も PulseController のユーザー経路は `is_autonomy_on` を呼んでいない (grep 結果に無い)。
- **追跡できていない境界**: `ensure_autonomy_for` の中で AutonomyManager が実際に何を止めるかは `saiverse/autonomy_manager.py` を通読していない。
- **既存テスト**: `tests/test_v03_autonomy_gate.py` — 固定具を外して `AUTONOMOUS_DRIVING_SHIPPED=False` を復元し、`is_autonomy_on` が常に False になること、`is_autonomy_on` を経由する呼び出し点が存在すること (`:167` はソース文字列の検査) を見る。**`tests/conftest.py` は autouse fixture で全テスト中この定数を True に差し替える** (`tests/conftest.py:17-33`) ので、それ以外のテストは全部「出荷後 (v0.4)」の世界を検証している。
- **状態**: `機能の存在=△` (設定は保存されるが v0.3 では効かない) `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (止め具の回帰 1 本のみ。UI からの経路は未検査)

### PERS-07: ペルソナを削除する

- **入口**: グローバル設定 → ワールドエディタ → AIs タブ → 「削除」。`confirm("このペルソナを削除しますか？")` のあと `DELETE /api/world/ais/{id}`。根拠: `WorldEditor.tsx:393`, `api/routes/world.py:466-467`
- **結果**: `ai` 行の削除、開いている `building_occupancy_log` の EXIT 打刻、`task_book` の当該行の物理削除、インメモリの `personas` / `persona_map` / `id_to_name_map` / `avatar_map` / `occupants` からの除去。根拠: `manager/admin.py:1431-1482`
- **期待の根拠**: `実装のみ (根拠なし)` — 「削除で何が消えるか」を書いた利用者向け説明は見つからなかった。`docs/user-guide/world-editor.md` は AIs タブの説明に削除の項目を持たない。
- **現在の挙動 (静的確認)**:
  - **消えないもの (静的に確認)**: `~/.saiverse/personas/<id>/` (memory.db、log.json、conscious_log.json 等) — `manager/admin.py` の削除経路に `shutil.rmtree` も persona ディレクトリ操作も無い。`persona_schedule` / `persona_event_log` / `persona_task` (+step/history) / `session_anchor` / `user_ai_link` / `llm_usage_log` の当該行 — 削除文が無い。`database/models.py` で `ForeignKey("ai.AIID")` を宣言している列は 21 本あるが、削除経路が触るのは `building_occupancy_log` と `task_book` の 2 つだけ。根拠: `manager/admin.py:1453-1461`, `saiverse/task_book.py:519-534`, `database/models.py` の FK 宣言
  - シードされたペルソナ (`_is_seeded_entity`) と dispatched なペルソナは削除できない。根拠: `manager/admin.py:1439-1451`
  - **同じ `delete_ai` の実装が `manager/persona.py:707-` にも重複している**。`manager/persona.py:715-717` の docstring 自身が「AdminService 側の同名定義と行単位の複製関係にある — 片方を変えたらもう片方も揃えること」と書いている。実際に呼ばれるのは `SAIVerseManager.delete_ai` → `self.admin.delete_ai` の側。根拠: `saiverse/saiverse_manager.py:2114-2116`
  - 残った `persona_schedule` 行は ENABLED のままなら次回起動で `ScheduleManager.start()` が再登録し、発火時に `all_personas.get(persona_id)` が None になって `"persona not found"` で failed になる。根拠: `saiverse/schedule_manager.py:156-183, 1161-1164`
  - 復旧手段: DB のバックアップ (`SAIVERSE_DB_BACKUP_ON_START`) 以外に、UI 上の undo は無い。
- **追跡できていない境界**: `_is_seeded_entity` の判定基準は未読。`llm_usage_log.PERSONA_ID` は FK 宣言が無い (`database/models.py:545`) ので孤児の扱いが別問題になりうるが、追えていない。
- **既存テスト**: `tests/test_task_book.py:548-563` — `purge_persona_entries` が当該ペルソナの行だけを消すことを検証する。**commit は「呼び手 (delete_ai) の責任」としてテスト側が代行しており、`delete_ai` 本体は呼んでいない**。`delete_ai` 自体のテスト、および「何が消えないか」を固定するテストは見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=△`

### PERS-08: アラーム (スケジュール) の登録・編集・トグル・削除

- **入口**: ペルソナメニュー →「Alarm / アラーム管理」。`GET/POST /api/people/{id}/schedules`、`PUT/DELETE /{schedule_id}`、`POST /{schedule_id}/toggle`。根拠: `frontend/src/components/ScheduleModal.tsx:283-333`, `api/routes/people/schedule.py`
- **結果**: `persona_schedule` 行の作成・更新・削除と、EventScheduler への予約の同期。発火時には LLM 課金が発生する (PERS-09)。根拠: `api/routes/people/schedule.py:153-164, 293-302, 325-333`
- **期待の根拠**: `既存の仕様文書` — `docs/handoff/2026-07-20_w3_schedule_ledger_handoff.md` D2/D7 (テストの docstring が引用)。利用者向けの説明文書は見つからなかった。
- **現在の挙動 (静的確認)**:
  - 種別は periodic / oneshot / interval の 3 種。oneshot の日時は City のタイムゾーンで解釈して UTC 保存、読み出し時に `+00:00` を付けて返す。根拠: `schedule.py:19-49, 130-139`
  - 発火に影響する変更 (create/update/toggle) は同一 commit で `SYNC_GENERATION` を SQL 式でインクリメントする (lost update 対策)。根拠: `schedule.py:118-122, 187, 283`
  - EventScheduler への同期失敗は HTTP 200 のまま `scheduler_synced: false` で返す。**フロントはこのフィールドを読んでいない** — `ScheduleModal.handleSave` は `res.ok` だけを見て `loadSchedules()` する。根拠: `schedule.py:158-164`, `ScheduleModal.tsx:285-294`
  - **`meta_playbook` はフロントから送らない**。新規はサーバー既定 `DEFAULT_META_PLAYBOOK = "track_user_conversation"`、編集は「空なら既存を保つ」。したがって **UI からは起床 (`judgment_day_open`) / 就寝 (`judgment_day_close`) のアラームを作れない**。根拠: `ScheduleModal.tsx:264-270`, `schedule.py:110-113, 232-234`, `saiverse/schedule_manager.py:51`
  - `create_schedule` / `delete_schedule` は **persona_id の存在を検査しない** (`ai` 行の照会が無い)。存在しない ID を渡すと孤児行が作られる。根拠: `schedule.py:105-146, 311-321`
  - ペルソナ側は `schedule_add` スペルで `meta_playbook` を明示できる。根拠: `builtin_data/tools/schedule_add.py:29, 85, 103`
- **追跡できていない境界**: `PLAYBOOK_PARAMS` の `pre_spells` がどう実行されるかは `submit_schedule` 側で未読。`INSTANCE_TOKEN` / 実行台帳の精算は未読。
- **既存テスト**: `tests/test_schedule_api_sync.py` (TestClient で世代 bump と `scheduler_synced` を HTTP 検証、DB は一時 file sqlite)、`tests/test_schedule_default_playbook.py`、`tests/test_schedule_dispatch_outcome.py`、`tests/test_schedule_manager_ledger.py`、`tests/test_schedule_reconciliation.py`。ScheduleModal 側 (`scheduler_synced` を無視していること) の検査は無い。
- **状態**: `機能の存在=✓` `期待の根拠=△` (開発者向け handoff のみ) `挙動の静的確認=✓` `テスト対応=△` (API は厚い / UI は無い)

### PERS-09: アラームが実際に発火する (ScheduleManager)

- **入口**: 自動処理。起動時に `ScheduleManager.start()` が ENABLED 全件を EventScheduler へ登録し、時刻到達で `_handle_fire` → `_execute_schedule`。根拠: `saiverse/schedule_manager.py:156-183`
- **結果**: 判断点 Playbook なら `handle_scheduled_judgment`、それ以外は `pulse_dispatcher.dispatch_schedule_fire` で Pulse を起こす。**LLM 課金が発生する**。根拠: `saiverse/schedule_manager.py:1129-1189`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/autonomous_behavior_v3.md` §11.1 (止め具が「止めるもの/止めないもの」を列挙)
- **現在の挙動 (静的確認)**:
  - 判断点経路 (`JUDGMENT_PLAYBOOK_NAMES` に載る Playbook) は `handle_scheduled_judgment` → `is_autonomy_on` を通るので、v0.3 では必ず止まる (`settled_skip` の理由に `"persona autonomy disabled"` がある)。根拠: `saiverse/schedule_manager.py:1129-1152, 985-991`, `autonomy_wiring.py:1138`
  - **非判断点の Playbook (既定の `track_user_conversation` を含む) は自律ゲートを通らない**。`_execute_schedule` の後半は `all_personas` からペルソナを引いて `dispatch_schedule_fire` を直接呼ぶ。根拠: `saiverse/schedule_manager.py:1153-1189` (このブロックに `is_autonomy_on` の呼び出しが無く、grep でも `schedule_manager.py` に `is_autonomy_on` は出てこない)
  - つまり UI から作れる唯一の種類のアラームは、v0.3 でも `AUTONOMY_ENABLED=False` でも発火して課金する。
  - `META_PLAYBOOK` が空の行は既定 Playbook にフォールバックし WARNING を出す (鳴らないアラームを避けるため)。根拠: `saiverse/schedule_manager.py:1097-1109`
- **追跡できていない境界**: `dispatch_schedule_fire` から先 (PulseDispatcher / SEARuntime) は領域外。`_handle_fire` の claim / occurrence 精算は未読。
- **既存テスト**: `tests/test_schedule_dispatch_outcome.py` / `tests/test_schedule_manager_ledger.py` / `tests/test_schedule_reconciliation.py` / `tests/test_event_scheduler.py`。**全テストは `conftest.py` の autouse fixture により `AUTONOMOUS_DRIVING_SHIPPED=True` の世界で走る**ので、「v0.3 で非判断点アラームだけが発火する」という現行出荷の非対称を固定しているテストは見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=△` (intent の「止めるもの」一覧はスケジュール駆動の非判断点発火に触れていない) `挙動の静的確認=✓` `テスト対応=△`

### PERS-10: ライフ (活動時間帯 = 起床〜就寝の区間)

- **入口**: **現在、利用者から設定する入口が無い**。
- **結果**: —
- **期待の根拠**: `既存の仕様文書` — `docs/intent/life.md`。冒頭ステータスは 💤 **凍結 (2026-08-23、まはー裁定)**「実装は入ったまま (撤去していない)」。§9.2 に「ライフ設定画面 (2026-07-14 実装完了)」の記載がある。
- **現在の挙動 (静的確認)**:
  - `api/routes/people/life_settings.py` と `tests/test_life_settings_api.py` は commit `f2a36010` (束 6c、運転 UI 撤去) で削除済み。根拠: `git log --diff-filter=D --name-only -- "*life*"`
  - 残っているのは `api/routes/people/life.py` の `/clips` 1 本のみ。同ファイル冒頭が profile-tree (2026-08-21) と day-plan (2026-08-22) の退役を明記。根拠: `api/routes/people/life.py:1-15`
  - フロントに「ライフ」「起床」「就寝」を扱う画面は無い (grep で当たるのはコメントのみ)。`Sidebar.tsx:69` に「『できごと』とライフビューへの導線は v0.3 で隠した」と注記。
  - `saiverse/day_plan.confirm_life_for_today` と `autonomy_wiring` の day_open/day_close 経路は実装として残っているが、`AUTONOMOUS_DRIVING_SHIPPED=False` で発火しない。根拠: `autonomy_wiring.py:595, 654-695`
  - 一方 `api/routes/people/schedule.py:229-231` のコメントは「ライフ設定が登録した起床・就寝のアラームを編集しても Playbook が化けないように」と、ライフ設定が現存する前提で書かれている。
- **追跡できていない境界**: 既存ユーザーの DB に残る起床・就寝の `persona_schedule` 行 (過去のライフ設定 UI が作ったもの) がどう扱われるかは追えていない。
- **既存テスト**: `tests/test_life_confirmation.py` / `test_life_phase2.py` / `test_life_phase3.py` — `confirm_life_for_today` と `fire_judgment_point` の day_open/day_close を検証する。ただし `conftest.py` が止め具を外した世界で走るため、**出荷時 (v0.3) の「発火しない」状態は検証していない**。`test_life_settings_api.py` は削除済みで、`test_feeds_api.py:10` と `test_schedule_api_sync.py:10` の docstring に参照が残っている。
- **状態**: `機能の存在=✗` (v0.3 では利用者に提供されていない。実装は残存) `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

### PERS-11: 会話バブルの観測点ハイライト (点クリップ)

- **入口**: 記憶ブラウザ (`MemoryBrowser`) がメッセージ ID をまとめて `GET /api/people/{id}/clips?message_ids=...` に投げる。根拠: `api/routes/people/life.py:67-117`, `frontend/src/components/memory/MemoryBrowser.tsx:47`
- **結果**: 引用アンカーを持つ点クリップだけを created_at 昇順で返す。読み取り専用・LLM 呼び出しなし。根拠: `life.py:1-4, 100-117`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/persona_cognition/life_concept_map.md` (life.py の docstring が「画面 C」として参照)
- **現在の挙動 (静的確認)**: `message_ids` は最大 100 件 (`CLIPS_BATCH_LIMIT`)、空なら 400、超過なら 400、未知ペルソナは 404。**建物履歴の message_id ではなく memory.db の messages.id** を要求する。根拠: `life.py:30-31, 84-98`
- **追跡できていない境界**: `MemoryBrowser` がどのタイミングでこれを呼び、どう描画するかは記憶領域と重なるため未読。
- **既存テスト**: `tests/test_life_view_api.py` — TestClient で `/clips` を検証し、**退役した profile-tree / day-plan / episodes が 404 であることも一緒に固定**する (消したルートが別経路で生き残っていないことの回帰)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### PERS-12: ユーザープロフィールの編集

- **入口**: サイドバーのフッタのユーザー欄をクリック → `UserProfileModal` → `PATCH /api/user/me`。根拠: `Sidebar.tsx:565-572`, `UserProfileModal.tsx:43-51`
- **結果**: `user` 行 (USERID=1 固定) の USERNAME / AVATAR_IMAGE / MAILADDRESS を更新し、**全 City の `user_room_<slug>` Building の表示名を「<新しい表示名>の部屋」に書き換える**。インメモリの `manager.state` と Building オブジェクトにも反映。根拠: `api/routes/user.py:194-273`
- **期待の根拠**: `実装のみ (根拠なし)` — 利用者向け説明は見つからなかった (`docs/user-guide/global-settings.md` はプロフィールに触れていない)。
- **現在の挙動 (静的確認)**:
  - ユーザーは USERID=1 にハードコードされている (コード内コメントに明記)。根拠: `user.py:196, 202`
  - `avatar` / `email` は `_Unset` センチネルで「未指定」と「null で消す」を区別する。フロントは常に両方送る。根拠: `user.py:183-192`, `UserProfileModal.tsx:46-50`
  - アバターが `/api/media/images/...` の URL なら実ファイルを解決して `_process_avatar_upload` に通す。ファイルが無ければ WARN を出して URL のまま保存する。根拠: `user.py:212-224`
  - 部屋名の書き換えはユーザーが部屋名を自分で変えていた場合でも上書きする (条件分岐なし)。根拠: `user.py:229-243`
- **追跡できていない境界**: `_process_avatar_upload` の保存先と、`UserStateMixin._resolve_avatar_to_path` の解決規則は未読。
- **既存テスト**: 該当テストなし (`tests/` に `user/me` や `update_user_profile` の grep ヒット無し)
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

### PERS-13: ユーザーの在席状態と現在地

- **入口**: フロントが定期的に `POST /api/user/heartbeat` と、タブの可視性変化で `POST /api/user/visibility` を送る。移動は `POST /api/user/move`。根拠: `api/routes/user.py:281-304, 94-149`
- **結果**: `manager.state.user_presence_status` (online/away/offline) と `user_last_activity_time` を更新し、`_refresh_user_state_cache()` で SEA ランタイムが見るキャッシュへ同期する。移動は OccupancyManager 経由で CAS 付き。根拠: `user.py:284-288, 122-149`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/building_memory_unified.md` §B-1 (CAS の根拠として user.py:30 が参照)
- **現在の挙動 (静的確認)**:
  - 移動はクライアント CAS (`expected_from_building_id`) とサーバー CAS (move_entity の条件付き UPDATE) の両方が 409 を返す。根拠: `user.py:102-143`
  - `visibility: false` で即 offline。復帰で online。中間の "away" を書く経路はこの 2 エンドポイントには無い (別の場所にある可能性は追えていない)。根拠: `user.py:294-304`
  - 在席は DB に永続化されず `manager.state` のメモリ上のみ。再起動で失われる。根拠: `user.py:73, 87`
- **追跡できていない境界**: "away" を設定する箇所、`_refresh_user_state_cache` の中身、フロントの heartbeat 間隔 (`page.tsx` 未読)。
- **既存テスト**: 該当テストなし (user API の直接テストは見つからなかった)
- **状態**: `機能の存在=✓` `期待の根拠=△` `挙動の静的確認=△` (away の書き手を追えていない) `テスト対応=✗`

### PERS-14: 初回セットアップ (チュートリアルの自動起動)

- **入口**: 起動時にフロントが `GET /api/tutorial/status` を叩き、`tutorial_completed == false` または `needs_initial_setup == true` なら `TutorialWizard` を開く。根拠: `frontend/src/app/page.tsx:513-535, 3774-3788`
- **結果**: 8 ステップのウィザード。完了で `POST /api/tutorial/complete` を打ち、作成した部屋へ移動する。根拠: `TutorialWizard.tsx:235-265`
- **期待の根拠**: `既存の仕様文書` — `docs/overview/roadmap_status.md` §6「🟡 チュートリアル … 最低限のみ実装。拡充が課題」
- **現在の挙動 (静的確認)**:
  - `needs_initial_setup` は「City が 0 件 または AI が 0 件」。根拠: `api/routes/tutorial.py:167-170`
  - 完了フラグは `user_settings` テーブル (USERID=1) の `TUTORIAL_COMPLETED`。行が無ければ未完了扱い。根拠: `tutorial.py:165-183`
  - ステップは Welcome / ユーザー名 / City名 / ペルソナ / APIキー / モデル設定 / Chronicle / 完了 の 8 段。スキップできるのは 2,3,5,6,7。根拠: `TutorialWizard.tsx:67-76, 456`
  - **ペルソナ作成 (ステップ 4) は APIキー入力 (ステップ 5) とモデル自動設定 (ステップ 5 の完了時) より前に起きる**。根拠: `TutorialWizard.tsx:186-221, 412-421`
  - ステップ 5 をスキップすると `saveApiKeys()` も `autoConfigureModels()` も走らないため、ステップ 6 の割り当て一覧が空のまま表示される。`editMode` は `startAtStep === 6` のときだけ有効なので、初回通し実行でスキップした場合はここで手動割り当てもできない。根拠: `TutorialWizard.tsx:186-210, 431-441`
  - `handleSkip` は `step < 9` を条件にしているが最大は 8。根拠: `TutorialWizard.tsx:229-233`
- **追跡できていない境界**: `page.tsx` のチュートリアル表示条件が他のモーダル (地図等) とどう競合するかは `:3694-3698` のコメントを読んだだけで挙動は未確認。
- **既存テスト**: 該当テストなし (`tests/` に tutorial の grep ヒット無し)
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### PERS-15: チュートリアルの API キー保存とモデル自動設定

- **入口**: ステップ 5 で API キーを入力 →「次へ」で `POST /api/admin/env` に書き、`POST /api/tutorial/auto-configure-models` を呼ぶ。根拠: `TutorialWizard.tsx:315-338, 353-371`
- **結果**: `.env` と `os.environ` に 6 つのモデルロール env var を書き込み、`manager.update_default_model()` で基準モデルを差し替える。根拠: `api/routes/tutorial.py:489-575`
- **期待の根拠**: `実装のみ (根拠なし)` — `docs/user-guide/global-settings.md` は「モデルロール」タブに触れるが、チュートリアルの自動設定は説明していない。
- **現在の挙動 (静的確認)**:
  - プロバイダは `PROVIDER_PRIORITY` (gemini_paid → gemini_free → anthropic → openai → grok → openrouter → openrouter_free → nvidia → ollama) の順に、キーが設定されている最初のものを選ぶ。ollama は「常に利用可能だが最下位」なので `_detect_best_provider` のループでは continue され、どれも無ければ最後に `"ollama"` を返す。根拠: `tutorial.py:410-457`
  - 画像/音声/動画の要約ロールは Gemini 系のみ対応なので、他プロバイダのプリセットでは None → Gemini キーがあれば Gemini 既定へフォールバック、無ければ warning を積んで割り当てを飛ばす。根拠: `tutorial.py:519-531`
  - `PROVIDER_ENV_KEYS` は「LLM クライアントが実際に読む env var だけを書く」と明記され、anthropic は `CLAUDE_API_KEY` のみ (2026-08-22 に `ANTHROPIC_API_KEY` を外した)。根拠: `tutorial.py:106-120`
  - `.env.example` のプレースホルダ (`sk-...` 等) は「未設定」と判定する (末尾 `...` かつ 20 文字未満)。根拠: `tutorial.py:123-135`
  - プリセットのモデル ID (`gemini-3-flash-preview-paid`, `claude-sonnet-4-5`, `gpt-4o-2024-11-20` …) が実際に `builtin_data/models/` に存在するかは、このコードの中では検証していない。
- **追跡できていない境界**: `write_env_updates` (`api/routes/admin.py`) の実装、`manager.update_default_model` の副作用、プリセットのモデル ID と `builtin_data/models/` の実在の突き合わせ。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△` (プリセットのモデル ID の実在を確認していない) `テスト対応=✗`

### PERS-16: チュートリアルを後から実行する

- **入口**: サイドバー →「システム」→「チュートリアル」→ `TutorialSelectModal` の 4 コース。根拠: `Sidebar.tsx:518-527`, `tutorial/TutorialSelectModal.tsx:22-48`
- **結果**: 選んだステップから `TutorialWizard` を開く。完了時に `window.location.reload()`。根拠: `TutorialSelectModal.tsx:110-121`
- **期待の根拠**: `利用者向け説明` — チュートリアル完了画面の文言「サイドバーの『システム』を開いて『チュートリアル』を選択してください」。根拠: `tutorial/steps/StepComplete.tsx`
- **現在の挙動 (静的確認)**:
  - コースは 最初からセットアップ(1) / ペルソナ作成(4) / APIキー設定(5) / モデル設定(6)。根拠: `TutorialSelectModal.tsx:22-48`
  - `startAtStep` で途中から開くと `TutorialWizard` は state をリセットするので、`createdPersonaId` が空のまま進む。「APIキー設定」(5) から入って「次へ」を押すと、ステップ 7 の Chronicle 保存 (`saveChronicleSettings`) は `createdPersonaId` が null なので何もせず素通りする。根拠: `TutorialWizard.tsx:117-136, 340-351`
  - `POST /api/tutorial/reset` は実装されているが、フロントから呼んでいる箇所は見つからなかった。根拠: `tutorial.py:209-218` と frontend の grep 結果
- **追跡できていない境界**: なし (画面と API を両方読んだ)
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### PERS-17: API 使用状況ページ (費用の可視化)

- **入口**: サイドバー →「システム」→「API使用状況」→ `/usage`。根拠: `Sidebar.tsx:505-516`, `frontend/src/app/usage/page.tsx`
- **結果**: 期間 / ペルソナ / カテゴリで絞った合計コスト・トークン・呼び出し回数、モデル別日次コストのグラフ、カテゴリ別使用状況。根拠: `usage/page.tsx:104-108, 211-395`
- **期待の根拠**: `実装のみ (根拠なし)` — 利用者向けの説明文書は見つからなかった。
- **現在の挙動 (静的確認)**:
  - 数えているのは `llm_usage_log` テーブルの行。列は入力/出力トークン、キャッシュ読み出しトークン、コスト、通貨、ノード種別、Playbook 名、カテゴリ。根拠: `database/models.py:537-556`
  - 通貨は混在しうるので `costs_by_currency` で通貨ごとに分けて返す。`total_cost_usd` は USD 行のみの合計 (後方互換)。根拠: `api/routes/usage.py:82-103`
  - **`cache_write_tokens` は DB 列が無く、コスト計算にだけ使われて保存されない**。根拠: `saiverse/usage_tracker.py:101, 159-172` (LLMUsageLog に該当列を渡していない)
  - ページが叩くのは summary / daily / personas / categories / by-category の 5 本。`GET /api/usage/by-persona` と `GET /api/usage/models` はフロントのどこからも呼ばれていない (grep 結果)。
- **追跡できていない境界**: `formatCost` の通貨表示規則、グラフの積み上げ規則は未読。
- **既存テスト**: 該当テストなし (`/api/usage/*` を叩くテストは見つからなかった)
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

### PERS-18: 無料枠の残量表示 (RPD)

- **入口**: チャット画面のモデル選択に連動して `GET /api/usage/rpd?model_id=...`。根拠: `frontend/src/app/page.tsx:1189, 1700`
- **結果**: モデル JSON の `rate_limit.rpd` が設定されたモデルについて、リセットタイムゾーンの当日 0 時からの呼び出し回数と上限を返す。根拠: `api/routes/usage.py:399-446`
- **期待の根拠**: `実装のみ (根拠なし)`
- **現在の挙動 (静的確認)**: 既定のリセットタイムゾーンは `America/Los_Angeles` (Gemini 無料枠の想定)。数えているのは `llm_usage_log` の行数 (= 呼び出し回数)。根拠: `usage.py:370-387, 428-435`
- **追跡できていない境界**: 記帳漏れ (PERS-19) がそのまま残量の過小表示になるが、その影響範囲は追えていない。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

### PERS-19: 使用量の記帳 (UsageTracker)

- **入口**: 自動処理。LLM 呼び出しの後に `get_usage_tracker().record_usage(...)`。根拠: `sea/runtime_llm.py:262, 2749`, `sea/runtime.py:2165`, `sea/work_session.py:738`, `sea/sluice.py:1987, 2748, 3258`, `sai_memory/arasuji/generator.py:46`, `sai_memory/memopedia/generator.py:330`
- **結果**: `llm_usage_log` に 1 行 (batch_size=1 で即 flush)。根拠: `saiverse/usage_tracker.py:40, 109-112, 152-180`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/model_provider_management.md` の不変条件「使用量の帰属」(`docs/issues/llm_usage_accounting_gaps.md` が引用)
- **現在の挙動 (静的確認)**:
  - `configure()` が呼ばれていない (= session_factory が無い) プロセスではレコードを捨てて WARNING を出す。本番では `manager/initialization.py:52` で設定される。根拠: `usage_tracker.py:131-147`
  - `record_cache_storage` が積むレコードには `"currency"` キーが無く、`_flush_to_db` の `record.get("currency", "USD")` により **JPY 建てモデルでもキャッシュ保管コストが USD として記録される**。`record_usage` 側は `:103` で通貨を入れている。根拠: `usage_tracker.py:205-218` と `:93-107` の対比、`:168`
  - 既知の欠落が 2 件、issue として起票済み: `docs/issues/llm_usage_accounting_gaps.md` (未解決) — 例外で終わると記帳されない / 空応答 retry が使用量を捨てる / runtime を通らない直接呼び出しが無記録 (media_summary、curation_ops、entity_extractor、scripts 群など)。`docs/issues/usage_tracking_gaps.md` (未着手) — 画像生成コストが一切計上されない。
- **追跡できていない境界**: `calculate_cost` の cache 割引・書き込み割増の正確さは未検証。
- **既存テスト**: `tests/test_usage_tracker.py` — 未設定時の drop、記録の形。**DB へ書く経路は session_factory を差し替えて検証しており、実際の LLM 呼び出し経路 (runtime_llm / sluice / arasuji) からの記帳は通していない**。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

### PERS-20: お知らせ

- **入口**: サイドバー →「システム」→「お知らせ」→ `/announcements`。またはメイン画面が未読判定のため `GET /api/config/announcements-monitor` → `GET /api/system/announcements` を叩く。根拠: `Sidebar.tsx:494-504`, `frontend/src/app/announcements/page.tsx:51`, `frontend/src/app/page.tsx:1145-1160`
- **結果**: 外部 Gist の JSON をそのまま表示。既読判定はコンテンツのハッシュを `localStorage` に保存する方式。根拠: `announcements/page.tsx:16-25, 61-63`
- **期待の根拠**: `実装のみ (根拠なし)`
- **現在の挙動 (静的確認)**:
  - 取得先は `SAIVERSE_ANNOUNCEMENTS_URL` (既定は maha0525 の Gist raw URL)。30 分キャッシュ。取得失敗時は古いキャッシュを返す。根拠: `api/routes/system.py:31-38, 127-150`
  - `GET /api/system/announcements` は `manager.state.announcements_enabled` を**見ていない**。トグルは `GET/POST /api/config/announcements-monitor` で読み書きでき、グローバル設定にも UI があるが、`announcements_enabled` を読む消費側は `api/routes/config.py:572` (自分自身の GET) しかない。根拠: `api/routes/system.py:153-158`, `api/routes/config.py:569-578`, `manager/state.py:90`, および grep 結果
  - ただしメイン画面はトグルの値を見てから取得しているので、未読バッジの抑止としては効いている。根拠: `frontend/src/app/page.tsx:1145-1160`
- **追跡できていない境界**: `page.tsx:1145-1170` の未読バッジ描画の詳細は未読。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

### PERS-21: デバッグコントローラー (Embedding 一括生成)

- **入口**: ペルソナ設定モーダルの中の `DebugPanel` →「Embedding 一括生成」→ `POST /api/people/{id}/debug/generate-embeddings`。根拠: `SettingsModal.tsx:536`, `DebugPanel.tsx:81-83`
- **結果**: Chronicle エントリ / Memopedia ページ / Fragment の未生成 embedding をバッチ生成し、件数をメッセージで返す。埋め込みモデルはローカル (SAIMemory の embedder)。根拠: `api/routes/people/debug.py:42-88`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/persona_cognition/debug_controller.md` (debug.py と DebugPanel.tsx の両方が冒頭で参照)
- **現在の挙動 (静的確認)**:
  - ペルソナがロードされていなければ 404、SAIMemory が未準備なら 503、埋め込み不可なら 503、排他ロックが取れなければ 503。根拠: `debug.py:45-63`
  - SAIMemory の `_db_lock` の下で実行する (他の書き込みと混ざるとトランザクションが確定してしまうため)。根拠: `debug.py:55-63, 73-78`
  - パネルに残っているボタンはこの 1 本だけ。旧「自律 Pulse を 1 回」「メタ判断を 1 回」「会話を切り上げ」「Autonomy 切替」「完全手動モード」は 2026-07〜08 に順次削除された (DebugPanel.tsx 冒頭のコメントに経緯)。
- **追跡できていない境界**: `embed_*` 関数の中身は記憶領域。
- **既存テスト**: 該当テストなし (`debug/generate-embeddings` を叩くテストは見つからなかった)
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### PERS-22: Memopedia 本文 → Fragment 変換 (下見・実行・取消)

- **入口**: 記憶モーダルの `MemopediaConversion` 画面。`GET/POST /api/people/{id}/debug/memopedia-conversion/preview`、`/apply`、`/runs`、`/revert`。根拠: `frontend/src/components/memory/MemopediaConversion.tsx:116`, `api/routes/people/debug.py:186-344`
- **結果**: 本文の行を Fragment へ移す。逐語の検算に落ちたら何も書かずに 409。取消は run_id 単位。根拠: `debug.py:248-297, 309-344`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/memopedia_body_to_fragment.md` (debug.py:94 が §6「自動マイグレーションにしない」を参照)
- **現在の挙動 (静的確認)**:
  - SAIMemory の共有接続ではなく**専用の sqlite 接続**を開き、かつ `_db_lock` も取る (部分適用の防止 + SAIMemory 側の追記が黙って落ちるのを防ぐ)。根拠: `debug.py:120-183`
  - `apply` は下見が返した `fingerprint` を必須にし、下見の後で本文が変わっていたら実行しない。根拠: `debug.py:259-268`
  - `revert` は変換より後の編集があると 409 で拒否し、`force: true` のときだけその編集ごと巻き戻して警告メッセージに載せる。根拠: `debug.py:318-344`
  - 下見は read_only 接続で、Memopedia 未初期化なら 409 (root ページの seed で書き込みが起きるのを避ける)。根拠: `debug.py:163-177`
- **追跡できていない境界**: `sai_memory/memopedia/body_to_fragment.py` の実装 (保存の実体・逐語検算) は未読。`MemopediaConversion.tsx` の画面遷移も未読。
- **既存テスト**: 未確認 (`tests/` に `body_to_fragment` の grep をかけていない)。記憶領域の担当と重なるため、そちらの結果と突き合わせが要る。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` (API 層のみ) `テスト対応=不明`

### PERS-23: Pulse ログの閲覧 (`/pulse-logs`)

- **入口**: `GET /api/people/{id}/pulse-logs` と `/pulse-logs/{pulse_id}`。根拠: `api/routes/people/pulse_logs.py:10-54`
- **結果**: pulse_id 単位のサマリ一覧 (新しい順・ページング) と、1 pulse の全ログ行。読み取り専用。根拠: 同上
- **期待の根拠**: `既存の仕様文書` — `docs/intent/persona_cognition/debug_controller.md` (可視化セクション。pulse_timeline.py の docstring が参照)
- **現在の挙動 (静的確認)**:
  - `get_adapter` を通るので、ロード済みでない場合も `ai` 行があれば一時アダプタで読む。未知 ID は 404。根拠: `pulse_logs.py:18`, `api/routes/people/utils.py:89-129`
  - **これを呼ぶ画面 `frontend/src/components/memory/PulseLogsViewer.tsx` は、どこからも import されていない** (frontend/src 全体の grep でヒットするのは自身の 3 行のみ)。つまり UI から到達できない。
- **追跡できていない境界**: `adapter.count_pulses` / `list_pulse_summaries` / `get_pulse_logs` の実装は未読。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=△` (API は生きているが UI から到達不能) `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

### PERS-24: Pulse タイムラインの閲覧 (`/pulse-timeline`)

- **入口**: 記憶モーダルの「Pulse タイムライン」タブ (`PulseTimelineViewer`)。根拠: `frontend/src/components/memory/PulseTimelineViewer.tsx:113, 179`, `MemoryModal.tsx:106-138`
- **結果**: SAIMemory の messages を pulse_id で束ねた一覧と、1 pulse の詳細 (送信プロンプト、line_role、spell 由来 ID、隙間メッセージ)。読み取り専用。根拠: `api/routes/people/pulse_timeline.py:1-9, 26-60`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/persona_cognition/debug_controller.md` (可視化セクション)
- **現在の挙動 (静的確認)**: **タブは `HIDDEN_TABS` に入っていて表示されない**。根拠: `frontend/src/components/MemoryModal.tsx:32-36`「pulse_timeline (Pulse タイムライン): line_role や spell 由来 ID といった…」というコメント付きで `experience` と一緒に隠されている。
- **追跡できていない境界**: `pulse_timeline.py` の 60 行目以降 (gap 計算・プロンプト取得) は未読。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=△` (API とコンポーネントはあるがタブが隠されている) `期待の根拠=✓` `挙動の静的確認=△` `テスト対応=✗`

### PERS-25: グローバル設定の入口

- **入口**: サイドバーのフッタの歯車 → `GlobalSettingsModal`。根拠: `Sidebar.tsx:556-563`
- **結果**: 8 タブ (環境 / ワールドエディタ / フィード / モデルロール / モデル管理 / Playbook権限 / 情報 / 便利機能)。根拠: `GlobalSettingsModal.tsx:51, 722-771`
- **期待の根拠**: `利用者向け説明` — `docs/user-guide/global-settings.md`
- **現在の挙動 (静的確認)**:
  - 「環境」タブにテーマ切り替え、API キー / 環境変数、ペルソナに送る量の水位 (metabolism / perception の既定値) がある。根拠: `GlobalSettingsModal.tsx:84-108, 402-490, 776-...`
  - 文書が挙げる「データベース管理」タブは存在しない (`GlobalSettingsModal.tsx` に「データベース」「バックアップ」の文字列が無い)。文書に無い「フィード」タブが存在する。
  - DB テーブルの汎用閲覧 API (`/api/db/tables/{table}`) はワールドエディタ・ペルソナウィザード・チュートリアルが使っている。根拠: `frontend/src/lib/dbTable.ts:57`
- **追跡できていない境界**: 各タブの中身 (モデル管理・Playbook 権限・便利機能) は他領域。
- **既存テスト**: `tests/test_metabolism_global_defaults.py` が水位の既定値 API に触れる (中身は未読)。タブ構成の検査は無い。
- **状態**: `機能の存在=✓` `期待の根拠=✓` (ただし文書が古い) `挙動の静的確認=△` `テスト対応=✗`

### PERS-26: リンクユーザーの設定

- **入口**: ペルソナ設定モーダルの「リンクユーザー」欄。`PATCH /api/people/{id}/config` の `linked_user_id`。根拠: `SettingsModal.tsx:912-927, 355`
- **結果**: `user_ai_link` の当該ペルソナ行を全削除してから、0 より大きい ID なら新しい行を追加。ロード済みペルソナの `linked_user_name` も更新する (システムプロンプトに名前が出る)。根拠: `api/routes/people/config.py:147-174`
- **期待の根拠**: `実装のみ (根拠なし)` — 画面内の説明文「このペルソナがリンクするユーザー。システムプロンプトに名前が表示されます」だけ。
- **現在の挙動 (静的確認)**:
  - 0 = クリア、None = 変更なし。フロントは未選択時に 0 を送る (= クリア)。根拠: `SettingsModal.tsx:355`, `config.py:148-157`
  - ペルソナ新規作成時、ユーザーが 1 人だけなら自動でリンクされる。根拠: `manager/persona.py:602-610`
  - リンク更新の例外は 500 だが、その時点で `update_ai` は既に commit 済みなので、モデル等の更新は残ってリンクだけ失敗する状態になりうる。根拠: `config.py:108-135` と `:148-174` の順序
- **追跡できていない境界**: `linked_user_name` がプロンプトのどこに出るかは領域外。
- **既存テスト**: 該当テストなし
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

### PERS-27: 事前実行スペルの設定

- **入口**: ペルソナ設定モーダルの「事前実行スペル」欄。`GET/POST /api/people/{id}/realtime-spell`、`DELETE /{binding_id}`、カタログは `GET /api/people/realtime-spell-catalog`。根拠: `SettingsModal.tsx:236-243, 796-910`, `api/routes/people/realtime_spell.py:12, 49, 80, 108`
- **結果**: 会話のたびに自動実行され、結果がリアルタイム情報に足される。実行時に LLM/ツールのコストが発生しうる。根拠: 画面の説明文 `SettingsModal.tsx:798-800`
- **期待の根拠**: `実装のみ (根拠なし)` — 画面内の説明文のみ。`docs/user-guide/persona-settings.md` は触れていない。
- **現在の挙動 (静的確認)**: 追加・削除は保存ボタンを経由せず即時に API を叩く (他の設定項目と挙動が違う)。根拠: `SettingsModal.tsx:812-815, 881-889`
- **追跡できていない境界**: 実行時の注入経路 (`_build_realtime_context` 周辺) は会話領域。
- **既存テスト**: 未確認
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△` `テスト対応=不明`

### PERS-28: 部屋にいる人の表示 (`/api/info/details` の occupants)

- **入口**: 右サイドバーや `PeopleModal` の「帰ってもらう」タブが `GET /api/info/details?building_id=...` を叩く。根拠: `PeopleModal.tsx:59`
- **結果**: AI ペルソナ (occupants) とユーザー (users) を分けて返す。根拠: `api/routes/info.py:42-51, 104-140`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/life.md` §9.1 (「話しかけやすさ」)、`docs/intent/persona_activity_view.md` §4.2 (常在インジケータ)
- **現在の挙動 (静的確認)**:
  - `OccupantInfo` は `activity_label` / `life_state` / `life_until` を**宣言しているが、どの経路でも値を入れていない** (束 6c で撤去)。実際に入るのは `autonomy_enabled` だけ。根拠: `api/routes/info.py:17-24` と `:118-127` の対比
  - フロントで `autonomy_enabled` を読んでいるのは `SettingsModal` と `WorldEditor` だけで、在室表示では使っていない (grep 結果)。
  - `building_id` を省略するとサーバー全体の現在地にフォールバックし、WARN を出す (2026-04-30 の事故の遠因として移行中)。根拠: `info.py:77-92`
- **追跡できていない境界**: 右サイドバーの描画 (`RightSidebar.tsx`) は未読。
- **既存テスト**: `tests/test_info_life_state.py` — 「暮らし系の欄が消えていること」を回帰として固定する (欄が黙って復活しないための歯止め)。一時 DB を使い本番に触れない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

### PERS-29: ペルソナのタスク (旧 tasks.db)

- **入口**: ペルソナ側のスペル / Playbook。UI からの直接の入口はこの調査では見つからなかった。
- **結果**: `persona_task` / `persona_task_step` / `persona_task_history` (メイン DB) に保存される。根拠: `database/models.py:1064-1180`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/unified_task_model.md` §5 step 6 (`persona/tasks/storage.py` の docstring が参照)
- **現在の挙動 (静的確認)**: `persona/tasks/storage.py` は値型 (dataclass / 例外) だけを持ち、per-persona `tasks.db` の `TaskStorage` クラスは廃止済みと冒頭に明記されている (2026-06-28 の統合)。根拠: `persona/tasks/storage.py:1-10`
- **追跡できていない境界**: `persona/tasks/store.py` / `PersonaTaskManager` は未読。タスクを閲覧する UI があるかは確認できていない。
- **既存テスト**: `tests/test_task_book.py` は `task_book` (別の台帳) のテスト。`persona_task` のテストは未確認。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` `テスト対応=不明`

---


### 矛盾・疑義

1. **`ACTIVITY_STATE` — 文書 vs コード**
   - `docs/user-guide/persona-settings.md:23-31` は「アクティビティ状態 (`ACTIVITY_STATE`): Active / Idle / Sleep / Stop」を現役の設定として表で説明し、`:46` で `features/autonomous-mode.md` を「自律行動と ACTIVITY_STATE」として参照する。
   - `docs/user-guide/world-editor.md:52` も AIs タブのフィールドとして「アクティビティ状態 (`ACTIVITY_STATE`: Stop / Sleep / Idle / Active)」を挙げる。
   - `docs/overview/landscape.md:73` は「自律性は `ACTIVITY_STATE` (4段階) で外部に宣言される」と書く (同じ文書の §9 では解体済みと書かれている)。
   - コード側: `database/models.py:133-137` は列自体を `AUTONOMY_ENABLED` へ一本化したと記録し、`tests/test_cognitive_model_schema.py:70` が `assert "ACTIVITY_STATE" not in cols` で列の不在を固定している。UI にも該当のフィールドは無い (`SettingsModal.tsx` / `WorldEditor.tsx` のどちらにも `ACTIVITY_STATE` は無い)。`database/migrate.py:553-603` は旧 DB からの変換だけを残している。
   - どちらが正しいかは断定しないが、**利用者向け文書 3 本が実装に存在しない設定を説明している**状態。

2. **`AUTONOMY_ENABLED` — 「効く」と書いた文書 vs 「効かない」出荷**
   - `docs/concepts/persona.md:17` と `docs/features/autonomous-mode.md` §AUTONOMY_ENABLED は「ON なら時間割・判断点が発火する」「OFF なら自律行動が一切起きない」と書く。
   - `saiverse/autonomy_wiring.py:91, 199-224` は v0.3 の出荷でこの設定を無効化する (`AUTONOMOUS_DRIVING_SHIPPED = False` の間 `is_autonomy_on` は常に False)。
   - `docs/features/autonomous-mode.md` §グローバル制御は「サイドバー / ライフビューから自律行動の再生・停止をトグルできる」と書くが、その UI は存在しない (`Sidebar.tsx:69` に「ライフビューへの導線は v0.3 で隠した」)。
   - `docs/overview/landscape.md:162` と `docs/intent/autonomous_behavior_v3.md` §11.1 は止め具を正しく書いている。**利用者向け文書と概念文書だけが古い**。

3. **自律トグルの UI — 隠した側と隠していない側**
   - `SettingsModal.tsx:99-102` は「自律行動の ON/OFF は v0.3 で UI から隠した (autonomous_behavior_v3.md §11「運転 UI は隠す」)」として非表示にしている。
   - `WorldEditor.tsx:655-664` は同じ設定をチェックボックスとして出したままにしている。
   - `docs/intent/autonomous_behavior_v3.md` §11.1 は「v0.3 ではその切り替え UI を隠した — つまり新しいユーザーの世界では『ペルソナは既定で自律 ON、OFF にする手段が無い』」と書いており、**ワールドエディタが残っていることを前提にしていない**。どちらが意図かは断定できない。

4. **ライフ設定 — 「実装完了」と書いた intent vs 削除済みのコード**
   - `docs/intent/life.md` は §9.2 の「ライフ設定画面新設」を「実装完了 (2026-07-14)、まはー実機検証待ち」と書き、ステータス行は 💤 凍結・「実装は入ったまま (撤去していない)」とする。
   - 実際には `api/routes/people/life_settings.py` と `tests/test_life_settings_api.py` は commit `f2a36010` で**削除**されている。フロントにもライフ設定画面は無い。
   - 「実装は入ったまま」は `saiverse/day_plan.py` / `autonomy_wiring.py` の確定ロジックについては正しいが、**設定 API と画面については正しくない**。

5. **アラーム UI と起床・就寝の関係**
   - `api/routes/people/schedule.py:229-231` のコメントは「ライフ設定が登録した起床・就寝のアラームを編集しても Playbook が化けないための担保」と書く。
   - しかしライフ設定は削除済み (§3-4) で、`api/routes/people/summon.py:92-94` は「一日のリズムはライフ設定が所有し … 2026-09-01 裁定でアラーム管理の Playbook 選択欄も撤去した」と書く。
   - 結果として **起床・就寝の `persona_schedule` 行を作る利用者向けの入口が一つも無い**。ペルソナ自身の `schedule_add` スペル (`builtin_data/tools/schedule_add.py:29`) だけが `meta_playbook` を指定できる。

6. **止め具の適用範囲 — intent の記述 vs スケジュール発火**
   - `docs/intent/autonomous_behavior_v3.md` §11.1「止めるもの」は「判断点 / 見張り / 起動時のコマ予約の再確立 / 実イベントの判断経由の応対」を挙げ、「止めないもの」に会話・Metabolism・手帳・沈黙タイマー・実イベントの直接応答を挙げる。
   - `saiverse/schedule_manager.py:1153-1189` の非判断点発火はどちらにも書かれていないが、実装は自律ゲートを通らずに `dispatch_schedule_fire` を呼ぶ。UI から作れるアラームはすべてこの経路 (§PERS-08)。
   - 「意図してそうしている」のか「棚卸しから漏れた」のかは、この調査では判定できない。

7. **`GET /api/people/summonable` と `RuntimeService.get_summonable_personas`**
   - 同じ「呼べる人の一覧」を 2 箇所が別々に実装しており、`manager/runtime.py:354-366` は `persona_role is None`(= ruler 除外) を持つが、`api/routes/people/summon.py:20-27` は持たない。
   - `manager/persona.py:465` のコメントは「'ruler'=Region RPG GM (召喚一覧から除外され、Region に常駐する)」と書いており、**API 側の実装がこの記述を満たしていない**。

8. **`delete_ai` の二重定義**
   - `manager/admin.py:1431` と `manager/persona.py:707` にほぼ同一の実装がある。後者の docstring 自身が「行単位の複製関係にある — 片方を変えたらもう片方も揃えること」と書いている。
   - 呼ばれるのは `AdminService` 側 (`saiverse/saiverse_manager.py:2116`)。`docs/issues/archive/persona_mixin_ai_edit_dead_duplicate.md` は `get_ai_details`/`update_ai` の複製を 2026-08-12 に撤去した記録だが、`delete_ai` は残っている。

9. **`CLAUDE.md` の Task 記述**
   - `CLAUDE.md` Memory Stack「Task storage (`persona/tasks/storage.py`) — per-persona `tasks.db`」。
   - `persona/tasks/storage.py:1-10` は「Task は統合 `persona_task` テーブル (main DB) へ一本化された (2026-06-28)。旧 per-persona tasks.db の `TaskStorage` クラスは廃止」と書く。

10. **`docs/user-guide/global-settings.md` のタブ一覧**
    - 文書は「環境 / ワールドエディタ / データベース管理 / モデルロール / モデル管理 / Playbook権限 / 情報 / 便利機能」の 8 つを挙げる。
    - 実装は「環境 / ワールドエディタ / **フィード** / モデルロール / モデル管理 / Playbook権限 / 情報 / 便利機能」(`GlobalSettingsModal.tsx:51, 722-771`)。「データベース管理」タブは存在しない。

11. **「再起動が必要」というメッセージ**
    - `manager/admin.py:1193-1196` と `manager/persona.py:701-704` は作成成功時に "A restart is required for the AI to become active." を返す。
    - 同じ呼び出しの中で `_create_persona` は PersonaCore を `self.personas` に登録している (`manager/persona.py:679`)。
    - `PersonaWizard` はこのメッセージを表示していないので利用者には見えないが、API を直接叩く利用者には見える。

12. **`Chronicle タブ` という呼称**
    - `tutorial/steps/StepChronicle.tsx` と `SettingsModal.tsx:623` は「メモリー画面の『Chronicle』タブ」と案内する。
    - `PersonaMenu.tsx:59-64` と `SettingsModal.tsx:736` は同じ場所を「あらすじタブ」と呼ぶ。
    - `MemoryModal.tsx:22` のタブ ID は `arasuji`。表示名は未確認 (`ArasujiViewer` を読んでいない)。

13. **キャッシュ保管コストの通貨**
    - `saiverse/usage_tracker.py:93-107` (`record_usage`) はモデルの pricing から通貨を引いてレコードに入れる。
    - `:205-218` (`record_cache_storage`) は `"currency"` キーを入れない。`_flush_to_db:168` が `record.get("currency", "USD")` なので、**JPY 建てモデルのキャッシュ保管コストが USD 行として記録される**。使用状況ページは通貨ごとに合計するので、その合計に紛れ込む。

---

### 凍結・開発者専用・到達不能・文書のみ

- **ライフ (活動区間)** — 💤 凍結 (2026-08-23、まはー裁定、`docs/intent/life.md` ステータス行)。確定ロジック (`saiverse/day_plan.confirm_life_for_today`) と判断点経路は残存。設定 API (`life_settings.py`) と画面は削除済み。
- **自律の駆動全体** — `saiverse/autonomy_wiring.py:91` の `AUTONOMOUS_DRIVING_SHIPPED = False` により、判断点・watchdog・起動時のコマ再予約・実イベントの判断経由が全部止まる。設定値 (`AUTONOMY_ENABLED`) は DB に残り、v0.4 で定数ごと削除する方針 (intent §11.1)。
- **ライフビュー / できごと UI** — v0.3 で導線を隠した (`Sidebar.tsx:69`)。`docs/intent/persona_activity_view.md` は「実装完了、実機検証待ち」のまま。§7 の間隔設定 UI と `PUT /activity/intervals` は退役済み (同 intent の Status 行)。
- **`OccupantInfo.activity_label` / `life_state` / `life_until`** — 応答モデルに宣言は残るが値を入れる経路が無い (`api/routes/info.py:17-24` vs `:118-127`)。`tests/test_info_life_state.py` がこの空欄を回帰として固定している。
- **`GET /api/people/{id}/pulse-logs`** — API は動くが、唯一の画面 `PulseLogsViewer.tsx` がどこからも import されておらず UI から到達不能。
- **Pulse タイムラインタブ** — `MemoryModal.tsx:36` の `HIDDEN_TABS` に入っていて表示されない (`experience` タブも同様)。
- **`POST /api/tutorial/reset`** — 実装はあるがフロントから呼ぶ箇所が見つからない。
- **`GET /api/usage/by-persona` / `GET /api/usage/models`** — フロントから呼ばれていない。
- **`manager.state.announcements_enabled`** — バックエンドで読む消費側が無い (フロントの未読バッジ抑止としてだけ機能する)。
- **`AI.MEMOPEDIA_INDEX_LIMIT`** — 死に列と明記 (`database/models.py`「Unused as of 2026-07-14 (dead column)」)。
- **`AI.LIFE_PURPOSE`** — 退役済み。読み手も書き手も無い。v0.3 の移行 (機械写し) の入力としてだけ残す、と明記。
- **`AI.METABOLISM_ANCHORS`** — legacy。`backfill_session_anchors` の変換元としてのみ残存 (変換後は常に NULL)。
- **`AI.LIGHTWEIGHT_VISION_MODEL`** — 列はあるが `get_ai_details` / `update_ai` / 設定 UI のいずれにも現れない (この調査の範囲では読み書きの経路を見つけられなかった)。
- **`manager/persona.py:707` の `delete_ai`** — 到達しない複製 (呼ばれるのは `AdminService` 側)。
- **`docs/user-guide/persona-settings.md` の「自律行動マネージャー」「メタ判断 Pulse 設定」** — 前者の UI は削除済み (`DebugPanel.tsx` の冒頭コメント)、後者は「キャッシュ維持の設定」として一部だけ残る。文書のみの機能。

---

### この領域で「検査が無い」と判断した重要な結果

以下は**利用者が受け取る結果**でありながら、`tests/` の中に触れているテストを見つけられなかったもの。

1. **ペルソナ削除で何が消えて何が残るか。** memory.db・tasks・スケジュール・イベントログ・リンクが残る事実を固定するテストが無い (`tests/test_task_book.py` は task_book の行だけを見て、`delete_ai` 本体を呼んでいない)。孤児スケジュールが起動時に再登録されて毎回失敗する挙動も未検査。
2. **チュートリアル (導入導線) の全経路。** `/api/tutorial/*` を叩くテストが 1 本も無い。初回起動判定 (`needs_initial_setup`)、API キー保存、モデル自動設定、途中コースからの再実行 (persona が null のまま進む) がすべて未検査。**新規ユーザーが最初に通る一本道が丸ごと無検査**。
3. **ペルソナ設定 API (`/api/people/{id}/config`) の HTTP 往復。** manager 層 (`test_admin_ai_edit_contract.py`) は厚いが、`config.py` の PATCH がどのフィールドを manager に渡すか、`linked_user_id` の 0/None の意味論、`meta_judgment_config` のマージが検査されていない。フロントの「ロード元と保存先の一致検査」(2026-04-30 の上書き事故の再発防止) も未検査。
4. **ユーザープロフィール更新の副作用。** 表示名の変更が全 City の `user_room_*` の Building 名を書き換えることを固定するテストが無い。
5. **使用量の記帳が実際の LLM 呼び出し経路から届くこと。** `test_usage_tracker.py` は tracker を直接呼ぶだけで、`sea/runtime_llm.py` などの実経路からの記帳は通していない。既知の欠落 2 件 (`docs/issues/llm_usage_accounting_gaps.md` / `usage_tracking_gaps.md`) はどちらも未解決で、**費用表示が実費より少なく出る可能性**が検査で押さえられていない。RPD 残量も同じ台帳を数えるので同じ影響を受ける。
6. **v0.3 の出荷状態そのもの。** `tests/conftest.py:17-33` の autouse fixture が全テストで `AUTONOMOUS_DRIVING_SHIPPED=True` に差し替えるため、テストスイートは一貫して「v0.4 の世界」を検証している。出荷状態を見るのは `tests/test_v03_autonomy_gate.py` 1 本だけで、そこが見るのは `is_autonomy_on` の戻り値と呼び出し点の存在。**「UI から作れるアラームは止め具を通らずに発火して課金する」という出荷時の非対称は、どのテストも触れていない。**
7. **`GET /api/people/summonable` の ruler 除外。** API 側に除外が無いことを咎めるテストが無く、`manager/runtime.py` 側の除外を見るテストも見つからなかった。
8. **観測系 UI の到達性。** `PulseLogsViewer` が import されていないこと、`pulse_timeline` タブが隠されていることは、どちらもテストではなくコメントでしか担保されていない。`tests/test_life_view_api.py` が「退役したルートが 404 であること」を固定しているのと対照的に、**「生きている API に画面が繋がっていること」を見る検査は無い**。
9. **スケジュール API の `scheduler_synced` がフロントで無視されていること。** API 側の応答は `tests/test_schedule_api_sync.py` が固定しているが、**フロントがそれを読まずに成功として扱う**ため、予約が作れなかったアラームが「登録できた」ように見える。この UI 側の欠落は未検査。
10. **キャッシュ保管コストの通貨。** `record_cache_storage` が通貨を落とすことを見るテストが無い。

---

## 領域 E. 画面のない自動処理 (AUTO-01〜34)


### AUTO-01: v0.3 の自律の止め具 (自律行動を全部止めている定数)

- **入口**: 自動。プロセス起動時に `SAIVerseManager.start()` が一度だけ INFO を出す。判定は定数 `AUTONOMOUS_DRIVING_SHIPPED = False` を読む `is_autonomy_on()` 一本。根拠: `saiverse/autonomy_wiring.py:91`、`:199-222`、`:225-242`、`saiverse/saiverse_manager.py:546-556`
- **結果**: 利用者には**何も見えない** (ログ行 1 本だけ)。ペルソナ側は判断点・見張り・コマの再予約・実イベントの判断経由が一切走らない。DB の `AI.AUTONOMY_ENABLED` は書き換えない (値は残る)。LLM 課金は発生しない (むしろ止める側)。根拠: `saiverse/autonomy_wiring.py:217-222`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/autonomous_behavior_v3.md` §11.1 (「2026-08-23 まはー裁定」と本文に明記。裁定の原文引用はなく、**設計書に承認の記載があるが原文未確認**)。`docs/overview/roadmap_status.md` §1 と `docs/overview/landscape.md` §3 も同じ記述を持つ。
- **現在の挙動 (静的確認)**: `is_autonomy_on` の呼び出し元は 4 箇所 (`autonomy_wiring.py:455` 判断点の共通入口 / `:1013` 実イベント / `:1138` 仲裁 / `:1647` watchdog) と `saiverse_manager.py:1228` (起動時のコマ再予約) / `:1547` (`ensure_autonomy_for`)。全部が定数 False で閉じる。根拠: repo 全体の grep で他に読み手なし
- **追跡できていない境界**: この止め具が「自律 ON/OFF の差が出るのは実イベント 1 箇所だけ」と intent が言う主張の検算 — **AUTO-13 (保温) が `persona.autonomy_enabled` を直接読んでおり、ゲートを通っていない**。intent の主張とコードが食い違う (→ §3-1)
- **既存テスト**: `tests/test_v03_autonomy_gate.py` (12 本)。`gate_off` fixture で `conftest` の autouse 固定具を外し、止め具が効く状態を検証する。実コードで通すのは `is_autonomy_on` / `handle_external_event` / `handle_user_utterance_conflict` / `watchdog_tick` / `fire_judgment_point` / `ensure_autonomy_for`。差し替えているのは manager (`SimpleNamespace` スタブ) と `fire_judgment_point` (呼ばれないことの証拠用)。2 本 (`test_startup_slot_rescheduling_goes_through_the_gate` / `test_manager_start_announces_the_gate`) は **`inspect.getsource` の文字列一致**で配線を見ており、実行はしていない。
- **⚠️ 検査の穴**: `tests/conftest.py:18-33` の autouse fixture が**全テストで定数を True に上書きする**。つまり pytest スイート 296 ファイルのほぼ全部は「v0.4 の運転が配線された世界」を検証しており、**利用者が今日受け取る v0.3 の挙動を検証しているのは `test_v03_autonomy_gate.py` の 12 本だけ**。この設計判断は conftest の docstring に理由が書かれている (v0.4 の設計資産を殺さないため) が、「出荷している姿の検査」がその 12 本に全部乗っている事実は変わらない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (止め具そのものは 12 本で見ているが、止め具が掛かった状態の他機能への波及は本スイートで検査されない)

---

### AUTO-02: 判断点 (起床・就寝・セッション終了・イベント到着)

- **入口**: 自動。①アラーム (`PersonaSchedule.META_PLAYBOOK` が `judgment_day_open` / `judgment_day_close`) の発火 → `handle_scheduled_judgment` ②作業セッション終了 → `post_session` ③実イベント到着 → `on_event` ④watchdog の火入れ直し。**v0.3 では 4 経路すべて `fire_judgment_point` の入口ゲートで止まる**。根拠: `saiverse/autonomy_wiring.py:393-455`、`:785`、`saiverse/judgment_points.py:81-92`
- **結果 (止め具を外した場合)**: 標準モデル (META アスペクト) の LLM を 1 回呼び、構造化出力を `builtin_data/tools/judgment_finalize.py` が適用する。メインキャッシュには整形済み独白＋要約行だけが残り JSON は混入しない。時間割の編成・タスクの裁定・手帳への棚入れが起きる。**LLM 課金あり**。根拠: `saiverse/judgment_points.py:1-45`
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md` §3「判断点」、`docs/intent/autonomous_behavior_v3.md` §6、`docs/intent/persona_cognition/` (judgment_points.md は intent ディレクトリ直下には無く、参照だけが残る)
- **現在の挙動 (静的確認)**: Playbook 4 本は `builtin_data/playbooks/public/judgment_{day_open,day_close,on_event,post_session}.json` に実在する。`playbook_available()` が DB 未 import を WARNING + スキップに落とす (判定不能時は True に倒し、実行側のエラー処理に委ねる)。根拠: `saiverse/autonomy_wiring.py:245-276`
- **追跡できていない境界**: `judgment_finalize` ツールが実際に何を書くか (手帳・時間割・実行台帳) は読んでいない。退役した目的の木への「残置の配線」が今も書き込むかどうかも未確認 (landscape §3 は「残置」と書く)。
- **既存テスト**: `tests/test_judgment_points.py` (69 本)、`tests/test_autonomy_wiring.py` (88 本)、`tests/test_judgment_playbook_prompt_contract.py`、`tests/test_judgment_layer2_tags.py`。いずれも `RecordingPulseController` 等のスタブで PulseController を差し替えており、**実 LLM は呼ばない**。かつ conftest が止め具を外しているので、これらは v0.4 の設計の検査。
- **状態**: `機能の存在=✓ (ただし v0.3 では発火しない)` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (v0.4 の姿としては厚い。v0.3 として「一つも発火しない」の検査は AUTO-01 の 12 本のみ)

---

### AUTO-03: 見張り (AutonomyManager の watchdog tick)

- **入口**: 自動。`ensure_autonomy_for()` が自律ゲート True のペルソナに `AutonomyManager` を立て、EventScheduler に `autonomy:<persona_id>` の周期予約を積む。既定間隔 50 分 (`META_JUDGMENT_CONFIG.periodic_interval_minutes` > env `SAIVERSE_META_LAYER_INTERVAL_SECONDS` > 既定の優先順)。根拠: `saiverse/autonomy_manager.py:47`、`:88-110`、`saiverse/saiverse_manager.py:1522-1567`
- **結果**: 正常時は何もしない (LLM を呼ばない)。異常時のみ ①今日の時間割が無い/コマ 0 件 → `day_open` を撃ち直す ②コマ予約が消えている → 再 push。発火時は必ず INFO ログ。根拠: `saiverse/autonomy_wiring.py:1619-1765`
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md` §3「AutonomyManager: 定期 tick は watchdog に縮退」
- **現在の挙動 (静的確認)**: **v0.3 では `AutonomyManager` が 1 本も立たない** (`ensure_autonomy_for` の `is_autonomy_on` が False)。`watchdog_tick` 自身も入口で `{"action": "skip", "reason": "autonomy disabled"}` を返す。二重の閉じ。根拠: `saiverse_manager.py:1547`、`autonomy_wiring.py:1647`
- **追跡できていない境界**: `day_plan.resolve_business_day` / `find_lost_slot_reservations` / `reschedule_pending_slots` の中身 (`saiverse/day_plan.py` 5618 行) は未読。
- **既存テスト**: `tests/test_autonomy_manager.py` (15 本)。manager を `MagicMock` にし `pulse_dispatcher.dispatch_autonomy_tick` の呼び出し回数を見る。**EventScheduler は本物**を使う。`tests/test_stop_autonomy.py` (1 本)。
- **状態**: `機能の存在=✓ (v0.3 では立たない)` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓ (v0.4 の姿として)`

---

### AUTO-04: 時間割のコマ発火 (day plan slot → 予算付き作業セッション)

- **入口**: 自動。起床判断が編成した各コマの開始時刻に EventScheduler が発火する (`_push_slot` → `_fire_slot_by_id` → `_fire_slot`)。再起動時は `_on_persona_registered` の手順 4 が pending/deferred コマを再 push する (自律ゲート越し)。根拠: `saiverse/day_plan.py:3028-3055`、`saiverse/saiverse_manager.py:1218-1240`
- **結果 (止め具を外した場合)**: WORKER アスペクトの予算付き作業セッション (`sea/work_session.py`) が走り、終了時に締めの一手 (`saiverse/slot_close.py`) が構造化出力コール 1 発で帰属判定と経験値ノートを書く。**LLM 課金あり (ラウンド予算ぶん)**。実行台帳の kind `slot.fire` に記帳される。根拠: `sea/work_session.py:1-40`、`saiverse/slot_close.py:1-28`、`saiverse/execution_ledger_wiring.py:52`
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md` §3「時間割 (day plan)」、`docs/intent/timetable_redesign.md` (参照のみ、本文未読)
- **現在の挙動 (静的確認)**: v0.3 では起床判断が発火しないので時間割が編成されず、コマ予約も生まれない。起動時の再予約もゲートで閉じる。**ただし過去に編成された day_plan 行が DB に残っている世界でも、再予約はゲートで走らない** ので発火しない。
- **追跡できていない境界**: `_fire_slot` 本体、`reschedule_pending_slots` の `downtime_recovery` 意味論、締めの一手の書き込み先。
- **既存テスト**: `tests/test_day_plan.py`。ほかに `saiverse/day_scenario.py` / `day_simulator.py` による一日シミュレーション (仮想クロック、開発者専用スクリプト `scripts/run_day_sim.py`)。
- **状態**: `機能の存在=✓ (v0.3 では発火しない)` `期待の根拠=✓` `挙動の静的確認=△ (day_plan 本体は未読)` `テスト対応=△`

---

### AUTO-05: アラーム (PersonaSchedule / ScheduleManager) — **v0.3 で出荷されており、LLM 課金が起きる**

- **入口**: 利用者が「アラーム管理」画面 (`ScheduleModal`、CityMap と右サイドバーから開く) で作る。ペルソナ自身も `schedule_add` スペルで作れる。登録すると `ScheduleManager.register_schedule` が EventScheduler に次回発火を積む。起動時に有効な全スケジュールを再登録。根拠: `frontend/src/components/ScheduleModal.tsx:375`、`api/routes/people/schedule.py:98-165`、`saiverse/schedule_manager.py:156-207`
- **結果**: 発火時に `<system>` プロンプト付きの Pulse を `PulseDispatcher.dispatch_schedule_fire` へ submit する。既定 Playbook は `track_user_conversation` (`DEFAULT_META_PLAYBOOK`)。**ペルソナが発言し、記憶に残り、LLM 課金が発生する**。利用者が画面を開いていなくても起きる。根拠: `saiverse/schedule_manager.py:53`、`:1155-1200`
- **期待の根拠**: `利用者向け説明` — 画面に「アラーム管理」「登録済みアラーム」「新規アラーム追加」の文言がある (`ScheduleModal.tsx:375-453`)。`ユーザー原文` としては `docs/intent/autonomous_behavior_v3.md` §11 に「自律行動はさせたくないがアラームだけは設定したい」の扱いが**未決**と書かれている。
- **現在の挙動 (静的確認)**: **`ScheduleManager` の発火経路に `is_autonomy_on` のゲートは無い** (grep で `schedule_manager.py` 内に `is_autonomy_on` の呼び出しゼロ)。`META_PLAYBOOK` が判断点名 (`judgment_day_open`/`judgment_day_close`) のときだけ `handle_scheduled_judgment` へ迂回し、そこでゲートに当たって止まる。それ以外の全アラームは v0.3 でも通常どおり発火する。根拠: `saiverse/schedule_manager.py:1130-1150`
- **安全弁**: 失敗時 backoff 120 秒 × 最大 3 回 (`SCHEDULE_DISPATCH_RETRY_BACKOFF_SECONDS` / `SCHEDULE_DISPATCH_MAX_ATTEMPTS`)。`unknown` (LLM が動いたか不明) は自動再実行しない。`META_PLAYBOOK` が空なら既定へ倒して WARNING。根拠: `saiverse/schedule_manager.py:63-64`、`:1102-1110`
- **追跡できていない境界**: `PulseDispatcher.dispatch_schedule_fire` の先 (Pulse 本体) は他領域。`schedule_add` スペルの実装は未読。
- **既存テスト**: `tests/test_schedule_manager_ledger.py` / `test_schedule_dispatch_outcome.py` / `test_schedule_reconciliation.py` / `test_schedule_default_playbook.py` / `test_schedule_api_sync.py`。台帳・冪等キー・世代照合・outcome 分類を実コードで通す。PulseController/Dispatcher は差し替え。**「自律 OFF のペルソナのアラームも鳴る」という利用者から見た事実を固定するテストは見つからなかった**。
- **状態**: `機能の存在=✓` `期待の根拠=△ (画面はあるが、v0.3 でアラームが自律の止め具の外にあることを説明した文書は見つからない)` `挙動の静的確認=✓` `テスト対応=△`

---

### AUTO-06: EventScheduler (全時刻予約のディスパッチャ)

- **入口**: 自動。`SAIVerseManager.start()` の 5 番目に dispatch スレッドが起動する。以降、heap に積まれた予約が時刻到来で発火する。根拠: `saiverse/saiverse_manager.py:527`、`saiverse/event_scheduler.py:94-104`
- **結果**: 現在このスケジューラに積まれる予約 (v0.3 で実際に立つもの): 台帳の掃除 tick (60 秒)、アラーム (件数ぶん)、フィード取得 (30 分)、pull observer (設定ごと)、冷えたウィンドウ見張り (10 分)、キャッシュ保温 (TTL 接近)、会話の沈黙タイマー (30 分)、SDS heartbeat (オンラインモード時のみ)。**予約はすべてインメモリで、再起動で消える** (再確立は個別の経路が担う)。根拠: `saiverse/saiverse_manager.py:1181-1215` (沈黙タイマー再確立)、`:405-409` (SDS)
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md` §3、モジュール docstring (`event_scheduler.py:1-29`)
- **現在の挙動 (静的確認)**: 単一スレッド。callback はその dispatch スレッドで**同期実行**される (docstring が「重い処理は別 thread へ」と明記)。`schedule_periodic` は **callback が例外を投げると周期を永久に止める** (再 schedule しない)。同じ key の再 schedule は古い予約を cancel する。仮想クロック中は背景スレッドが発火しない。根拠: `saiverse/event_scheduler.py:272-313`、`:405-415`
- **追跡できていない境界**: dispatch ループの残り (`:415` 以降) の詳細は読んでいない。
- **既存テスト**: `tests/test_event_scheduler.py` (18 本)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---

### AUTO-07: 会話の沈黙タイマー (既定 30 分) と「いま会話中か」の在処

- **入口**: 自動。会話が開くと `arm_conversation_timeout` が EventScheduler に `conversation_timeout:<persona_id>` を積む。時間は `AI.USER_CONV_TIMEOUT_MINUTES` (NULL なら 30 分、0 以下で無効化)。起動時は「開いている会話の出来事があるペルソナだけ」再確立する。根拠: `saiverse/user_conversation.py:50`、`:343-383`、`:386-490`、`saiverse/saiverse_manager.py:1203-1215`
- **結果**: 発火すると `handle_conversation_end` が会話状態を落とすだけ。**LLM は呼ばない**。会話の終わりは記録に残さない (2026-08-23 裁定)。watchdog の次回 tick を押し戻す。根拠: `saiverse/autonomy_wiring.py:852-935`、`saiverse/user_conversation.py:556-596`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/autonomous_behavior_v3.md` §8/§13.3 (会話終了判断の退役)、`docs/overview/landscape.md` §9 (「再起動で状態が消える = 『会話していない』に一貫して倒れるのは設計」)
- **現在の挙動 (静的確認)**: 会話状態は**プロセス内メモリ** (`_state_map`)。予約も EventScheduler のインメモリ heap。したがって再起動で両方消える。条件付き解除 (`expected_conversation_id`) で「別の会話を閉じる」窓を塞いでいる。止め具の対象外 (v0.3 でも動く)。
- **追跡できていない境界**: `rearm_conversation_timeout_on_load` が「開いている会話の出来事」をどこから読むか (メモリ内状態は再起動で消えるはずなので、DB 側の何かを読んでいる可能性がある) — 未確認。
- **既存テスト**: `tests/test_autonomy_wiring.py` の会話終了節 (`test_conversation_end_*` 4 本)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△ (再確立の読み元が未確認)` `テスト対応=✓`

---

### AUTO-08: 実イベントの受け口 (`inject_persona_event`) — **v0.3 でも LLM を呼ぶ**

- **入口**: 自動。Phenomenon ルールに合致したトリガー (webhook / X / SwitchBot / アドオン) が `inject_persona_event` フェノメノンを起動する。根拠: `builtin_data/phenomena/inject_persona_event.py:22-45`
- **結果**: ①`persona_event_log` にイベントを記録 ②`<system>` タグ付きプロンプトで Pulse を submit → **ペルソナが応答し、LLM 課金が発生する**。既定 Playbook は `track_user_conversation`。根拠: 同 `:150-170`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/autonomous_behavior_v3.md` §11.1「止めないもの: … **実イベントと仲裁の直接応答**。イベントは落ちない」
- **現在の挙動 (静的確認)**: `meta_playbook` 未指定の呼び出しは `handle_external_event` を通る。v0.3 では `is_autonomy_on` が False なので `ROUTE_DIRECT_AUTONOMY_DISABLED` になり、判断点を経由せず `_dispatch_direct()` を直接呼ぶ (v0.2 と同じ経路)。`meta_playbook` を明示した呼び出し (アドオン専用経路) は最初から直接 dispatch。根拠: `saiverse/autonomy_wiring.py:1013-1015`、`:175`、`builtin_data/phenomena/inject_persona_event.py:172-186`
- **追跡できていない境界**: 実際に `inject_persona_event` を撃つ Phenomenon ルールが標準構成に存在するか (DB の `phenomenon_rule` テーブル依存なので静的には決まらない)。同梱の統合実装はゼロ (AUTO-29) なので、既定インストールでは撃つ者がいない可能性が高い — **未確認**。
- **既存テスト**: `tests/test_autonomy_wiring.py` の実イベント節 (13 本超)、`tests/test_pulse_dispatcher_phenomenon.py`、`tests/test_v03_autonomy_gate.py::test_external_event_skips_the_judgment_and_dispatches_directly`。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---

### AUTO-09: Phenomena のトリガー発火 (server_start / server_stop ほか)

- **入口**: 自動。世界の出来事が `_emit_trigger` を呼ぶ。トリガー種別は 11 種 (`SERVER_START` / `SERVER_STOP` / `USER_SPEECH` / `PERSONA_SPEECH` / `PERSONA_MOVE` / `USER_MOVE` / `USER_LOGIN` / `USER_LOGOUT` / `SCHEDULE_FIRED` / `X_POLL_DETECTED` / `EXTERNAL_WEBHOOK`)。根拠: `phenomena/triggers.py:13-26`、`saiverse/saiverse_manager.py:565-569` (server_start)、`:1153-1157` (server_stop)
- **結果**: 条件に合致する `PhenomenonRule` があれば、非同期ワーカースレッドがフェノメノンを実行する。何が起きるかはルール次第 (`inject_persona_event` なら AUTO-08)。根拠: `phenomena/manager.py:74-93`
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md` §4「Phenomena (世界側からのイベント入口)」、`docs/reference/phenomena.md` (未読)
- **現在の挙動 (静的確認)**: `PhenomenonManager.start()` は `SAIVerseManager.start()` の 3 番目。ルール検索は `ENABLED == True` の全件を PRIORITY 降順で読む。ワーカーは daemon スレッド 1 本。ルールが 0 件なら実質何も起きない。
- **追跡できていない境界**: `_matches_condition` / `_resolve_arguments` の詳細、既定 DB にルールが seed されるか。
- **既存テスト**: 見つけた範囲では `tests/test_pulse_dispatcher_phenomenon.py` のみが近い。`PhenomenonManager` 自身のテストは名前からは見つからなかった (`該当テストなし` に近い)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` `テスト対応=△`

---

### AUTO-10: Observer の定期実行 (pull) と閾値通知

- **入口**: 自動。`SAIVerseManager.start()` の 6 番目に `start_pull_observers()` が走り、`ENABLED` かつ `EXEC_KIND in ("tool","playbook")` の設定を `observer:<id>` の周期予約で登録する (間隔は `INTERVAL_SEC`)。根拠: `saiverse/observer_manager.py:538-575`
- **結果**: ツールを実行し結果を `observer_metrics` に時系列蓄積、`fixture.STATE_JSON` に最新値をキャッシュ。`NOTIFY_RULES_JSON` の閾値 (`above` / `below`) を超えたら **Building へ `host` ロールのメッセージを注入する** (`event_type: observer_alert`)。建物ログに入るので、その部屋のペルソナの記憶に届きうる。LLM は呼ばない (Pulse は起こさない)。根拠: `saiverse/observer_manager.py:656-728`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/observer.md` §128/§159/§235
- **現在の挙動 (静的確認)**: `EXEC_KIND == "playbook"` は **実装されておらず debug ログを出して終わる** (`observer_manager.py:590-591`)。intent §128/§235 は playbook 実行を `PulseDispatcher` 経由で動くものとして書いている (§200 に「初版に含めるか」の未決も併記)。また intent §129/§159 は「重い実行は別 executor へ」と書くが、`_execute_tool` は EventScheduler の dispatch スレッドで**同期実行**している。
- **追跡できていない境界**: push 型 observer の HTTP 受け口 (`api/routes/observer.py`) は読んでいない。
- **⚠️ 静的に見えた壊れ方**: `_execute_pull` は**ツール実行の例外だけ**を捕まえる (`:611-615`)。`SessionLocal()` や DB クエリで例外が出るとそのまま `schedule_periodic` の wrapper に届き、**その observer の周期予約が永久に止まる** (`event_scheduler.py:292-299`)。ログは 1 行出るが、利用者から見ると「観測が黙って止まる」。
- **既存テスト**: 名前で該当するものは見つからなかった (`tests/test_visual_context_feed_stand.py` は feed 側)。`該当テストなし` と判断した。
- **状態**: `機能の存在=✓ (tool のみ)` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

---

### AUTO-11: フィードの定期取得と配送

- **入口**: 自動。`SAIVerseManager.start()` の 7 番目。**起動直後にまず 1 回取得**し、以後 `SAIVERSE_FEED_FETCH_INTERVAL_SEC` (既定 1800 秒) 周期。根拠: `saiverse/feed_manager.py:49`、`:283-317`
- **結果**: 記事を取得・保存し、フィード施設のある Building にいるペルソナの**知覚バッファ (kind="feed")** に最新 N 件 (既定 3) を積む。カーソルは候補全体の末尾へ進めるので、古い候補は正直にスキップされる。Pulse は起こさず LLM も呼ばない — 次にそのペルソナが喋るときの知覚として現れる。根拠: `saiverse/feed_manager.py:55`、`:822-840`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/rss_feed_intake.md` (§10-6 / §13 が本文から参照されている。本文は未読)
- **現在の挙動 (静的確認)**: サイクル全体に壁時計予算 (`SAIVERSE_FEED_CYCLE_BUDGET_SEC`、既定 300 秒)。超過したら残りの購読を打ち切り、**何本残したかを WARNING で表明する**。打ち切りが常に後方に偏らないよう開始位置をサイクルごとに回転させる。列挙は現 City の `feed_stand` 施設に属する購読だけ。`stop()` 後は再 start 不可 (単回使用)。根拠: `saiverse/feed_manager.py:443-535`
- **追跡できていない境界**: `_fetch_one` のネットワーク処理、剪定 (`SAIVERSE_FEED_ITEM_KEEP`)、`SAIVERSE_FEED_MAX_PENDING`。
- **既存テスト**: `tests/test_feed_intake.py`、`tests/test_feeds_api.py`、`tests/test_visual_context_feed_stand.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---

### AUTO-12: 実行台帳の起動時回復と 60 秒の掃除 tick

- **入口**: 自動。`SAIVerseManager.start()` の**最初** (背景ループが 1 本も動く前) に `run_startup_recovery()` を同期で行い、そのあと `schedule_recovery_tick()` が 60 秒周期を積む。根拠: `saiverse/saiverse_manager.py:504-514`、`saiverse/execution_ledger_wiring.py:41-52`、`:226-236`
- **結果**: 起動時 — 前世代の `running` を **unknown 化** (自動再実行はしない)、前世代の `slot.fire` を settle-close、`applied` 残留を掃除、滞留 outbox を配送。周期 tick — 同じ掃除に加え `prepared` の回収、schedule の reconciliation、pending 配送。**ペルソナの記憶への書き込み (outbox 配送) が起きうる**。LLM は呼ばない (`unknown` の自動再実行は禁止)。根拠: `saiverse/execution_ledger_wiring.py:190-300`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/execution_ledger.md` §2.4 #1/#3/#4/#6、§2.5 (unknown の自動再実行禁止)
- **現在の挙動 (静的確認)**: 安全弁 — `running` の期限 3600 秒で unknown 化 / `slot.fire` の settle 期限 900 秒 / `prepared` の refire 120 秒後・失効 1800 秒。`_recovery_tick` は各ステップを個別に try/except で包むので、一つの DB エラーで周期が止まることはない (`schedule_periodic` の「例外で永久停止」契約への対処が明記されている)。根拠: `execution_ledger_wiring.py:44-120`、`:239-300`
- **追跡できていない境界**: `execution_ledger.py` 本体の状態機械、配送ハンドラ (`saimemory.append` ほか) の中身。
- **既存テスト**: `tests/test_execution_ledger_wiring.py` (49 本)、`tests/test_execution_ledger.py`、`tests/test_move_entity_ledger.py`。`run_startup_recovery` と `schedule_recovery_tick` を実コードで呼ぶテストがある。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---

### AUTO-13: キャッシュの保温 (keep-alive) — **v0.3 で自動的に LLM を呼ぶ。止め具の外**

- **入口**: 自動。LLM 呼び出し成功のたびに `touch_anchor_after_llm_call` が anchor を touch し、`schedule_cache_ttl_pulse` が `ttl:<persona>:<model>` の予約を積む。発火時刻は `TTL × (1 - cache_threshold_ratio)` (既定 ratio 0.3)。根拠: `sea/session_lifecycle.py:1524-1613`
- **結果**: `SEARuntime.run_cache_keepalive` が**メインラインと同じ context (head + 履歴) を組み、末尾に不活性な 1 文を足して LLM を 1 回呼ぶ**。応答は破棄、記憶には一切書かない。成功すると再度 touch → 次の保温が予約され、**連鎖が続く**。根拠: `sea/runtime.py:1962-1986`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/persona_cognition/life_concept_map.md` §14 A2 (「まはー決定 2026-07-07」と記載。**設計書に承認の記載があるが原文未確認**)、`docs/intent/autonomous_behavior_v3.md` §1 (「キャッシュの保温はティックの営みそのものが行い、keep-alive 専用の温め直しは最後の保険に退く」= v0.4 の姿)
- **現在の挙動 (静的確認)**: 予約は `cache_type == "explicit"` (Anthropic 等) のときだけ。`META_JUDGMENT_CONFIG.keep_cache_alive == False` なら予約しない。発火側のゲートは **`persona.autonomy_enabled` の直読み** (`sea/runtime.py:1994`) — `autonomy_wiring.is_autonomy_on` を**通らない**。`AI.AUTONOMY_ENABLED` は既定 True で、v0.3 はその切り替え UI を隠している。したがって **explicit cache モデルを使うペルソナは、利用者が画面を閉じていても TTL の 7 割ごとに LLM を呼び続ける** (ライフ未宣言なら `is_keepalive_allowed` は常に許可)。連鎖が止まるのは、呼び出し失敗・anchor 失効・非 explicit への設定変更・自律 OFF のいずれか。
- **追跡できていない境界**: `get_anchor_validity_seconds` の実効値 (既定 1200 秒と本文にあるが per-persona override あり)。実際にどのモデルが `explicit` になるかは `saiverse/model_configs.py` 依存で未確認。**「何回・いくら課金されるか」は静的には出せない**。
- **既存テスト**: `tests/test_cache_keepalive.py` (9 本)。`FakeScheduler` / `FakeMetaLayer` / `FakeLLMClient` に差し替え、`get_cache_config` を `patch` で `explicit` 固定。「記憶に書かない」「失効時は撃たない」「anchor が動いたら中止」は実コードで通す。**conftest が止め具を外しているため、「v0.3 の止め具が掛かった状態でも保温は走る」という実際の出荷挙動を固定したテストは無い**。
- **状態**: `機能の存在=✓` `期待の根拠=△ (v0.4 の姿は書かれているが、v0.3 で保温が止め具の外にある事実の記述は見つからない)` `挙動の静的確認=✓` `テスト対応=△`

---

### AUTO-14: 冷えたウィンドウの先回り畳み (10 分ごとの見張り) — **v0.3 で自動的に LLM を呼ぶ**

- **入口**: 自動。`SAIVerseManager.start()` の 8 番目に `schedule_cold_window_sweep()` が積まれ、以後 **600 秒 (10 分) ごと**に全ペルソナを巡回する。tick は自分で次回を積み直す (`finally` の中なので例外でも継続)。根拠: `sea/session_lifecycle.py:94-95`、`:4302-4338`、`saiverse/saiverse_manager.py:533-537`
- **結果**: 検査は読みだけ (LLM なし)。条件成立 (`due`) のペルソナだけを **daemon スレッドへ逃がして `run_cold_precompaction` を走らせる** = Chronicle の編纂とスルース、つまり **LLM 課金が発生する**。ペルソナごと同時 1 本。根拠: `sea/session_lifecycle.py:4317-4338`、`:4396-4422`
- **発火条件**: ①Chronicle 生成が有効 ②`AUTONOMOUS_CHRONICLE_ENABLED` が True ③全 anchor が冷え切っている (生きたキャッシュが 1 つでもあれば `hot` で見送り) ④提示ウィンドウの実送信文字数が `(target + high) / 2` を超えている。根拠: `sea/session_lifecycle.py:4340-4394`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/arasuji_levels.md` §14-4 (「まはー裁定 2026-07-29」と本文に記載。**設計書に承認の記載があるが原文未確認**)。「先回りはコスト最適化であって回復措置ではない」ので、`AUTONOMOUS_CHRONICLE_ENABLED=False` のペルソナは対象外にする、と docstring が理由まで書いている。
- **現在の挙動 (静的確認)**: 止め具 (`AUTONOMOUS_DRIVING_SHIPPED`) とは無関係に走る。`AUTONOMOUS_CHRONICLE_ENABLED` (ペルソナ設定) が唯一の利用者側の口。
- **追跡できていない境界**: `run_cold_precompaction` → `_run_metabolism_locked` の全経路 (記憶領域)。`is_autonomous_chronicle_enabled_for_persona` の設定が UI のどこに出るか。
- **既存テスト**: `tests/test_perception_call_contracts.py` と `tests/test_session_anchor_rows.py` が `cold_window_sweep` / `cold_precompaction` に触れる。**10 分周期の見張りそのもの (積み直し・inflight 排他・due 判定) を主題にしたテストは見つからなかった**。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

---

### AUTO-15: Metabolism の自動発火 (記憶の整理) と発火側の安全弁

- **入口**: 半自動。①ペルソナの応答後 (`maybe_run_metabolism`) ②冷えたウィンドウ見張り (AUTO-14) ③手動の「記憶の整理」ボタン。根拠: `sea/session_lifecycle.py:1613-1755`
- **結果**: あらすじ (Chronicle) 生成 → 埋め込み補充 → スルース → 退場 (窓の起点前進)。**LLM 課金が発生する**。完了は `event_callback` で画面へ「記憶の整理が完了しました（N 件…）」と出る。根拠: `sea/session_lifecycle.py:4880-4895`
- **発火条件 (3 択の OR)**: ①トークン閾値超過フラグ ②前回 defer した `pending` フラグ ③提示コンテキストの実送信文字数 > 上限水位 (`high`)。ただし「会話の行だけ」で測った量が `target` 以下なら**走らせない** (削る先が無い)。根拠: `sea/session_lifecycle.py:1677-1712`
- **安全弁 (この関数の中だけで 3 つ)**:
  1. **レート制限の小休止** — `RateLimitError` の後は既定 600 秒 (`SAIVERSE_METABOLISM_RATE_LIMIT_COOLDOWN_S`) Metabolism を見送る。守るのは利用者の API 枠。閾値の根拠は `docs/intent/sluice_coverage_gaps.md` 第一段 C-1。利用者に**見えない・選べない (env のみ)**。根拠: `sea/session_lifecycle.py:43-56`、`:1633-1642`
  2. **defer-to-hot** — キャッシュが冷たければ整理を繰り延べる (スルースを温かい prefix に載せるため)。根拠: `:1723-1741`
  3. **圧力弁** — 実送信が `high × 1.5` (`SAIVERSE_SLUICE_PENDING_CAP`) を超えたら、冷たくても走らせる。「繰り延べ続けて毎ターン肥大ウィンドウを読むより一回のコールド代の方が安い」。WARNING を残す。利用者に**見えない・選べない (env のみ)**。根拠: `sea/sluice.py:101-103`、`sea/session_lifecycle.py:1725-1735`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/chronicle_eviction.md` §4、`docs/concepts/metabolism.md`、`docs/intent/gold_panning.md` §3.7 (defer-to-hot)
- **追跡できていない境界**: `run_metabolism` の全経路、水位の解決 (`get_metabolism_watermarks`) は記憶領域。
- **既存テスト**: `tests/test_metabolism_two_layer.py`、`tests/test_metabolism_rate_limit_cooldown.py`、`tests/test_metabolism_global_defaults.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---

### AUTO-16: スルースの「冷たいときの飛ばし」と飛ばした範囲の記録 (安全弁)

- **入口**: 自動。Metabolism の中で、スルースの担当範囲 (パンマーカーから窓の末尾まで) の保存行の字数が `SAIVERSE_SLUICE_MAX_SPAN_CHARS` (**既定 100,000 字**) を超えていたら走らせない。根拠: `sea/sluice.py:106-124`、`sea/session_lifecycle.py:4726-4770`
- **守っているもの**: 利用者の会話。「入りきらない事態を復旧する」のではなく「入りきる回だけ走らせる」。飛ばした回は退場を進め、窓から出ていく未見の範囲を `sluice_skipped_spans` に記録する。**記録が書けなければ退場を止める (fail-closed)**。根拠: `sea/session_lifecycle.py:4835-4860`
- **閾値の根拠**: `既存の仕様文書` + `ユーザー原文の引用あり` — `docs/intent/sluice_coverage_gaps.md` の「決定」節に 2026-09-08 のまはーの発言が引用されている旨が本文に書かれている (「スルースを通っていない記憶が一部あることで人格は崩れない…普通の会話を妨げてまで守るのは本末転倒」)。**100,000 字という数値そのものの出所は実装のみ** (intent 側に数値の根拠は見当たらなかった)。
- **利用者に見えるか・選べるか**: **見えない・選べない** (env `SAIVERSE_SLUICE_MAX_SPAN_CHARS` のみ、UI なし)。飛ばしたことは WARNING ログと `sluice_skipped_spans` テーブルに残る。intent は「通っていない範囲はユーザーに分かる状態で明示する」を**第二段** (未起草) の宿題としている。
- **既存テスト**: `tests/test_sluice_cold_isolation.py` (`test_skipped_span_is_recorded_before_the_window_moves` ほか 12 本)、`tests/test_sluice.py`、`tests/test_sluice_capture.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---

### AUTO-17: Chronicle の束ねの安全弁 (1 呼び出し 3 件) と 2026-09-09 の修正

- **入口**: 自動。Metabolism / 手動編纂の中で `run_band_overflow` が呼ばれる。1 回の呼び出しで実行する束ね (LLM コール) は既定 **3 件**まで (`SAIVERSE_CHRONICLE_MAX_BAND_CONSOLIDATIONS_PER_RUN`)。根拠: `sai_memory/arasuji/bands.py:58`、`:104-111`、`:1184-1189`
- **守っているもの**: 「承認・課金見込みを実行が超えてはいけない」(`max_folds` の説明、Codex レビュー 2026-07-28 high2)。承認済み予算は**試行回数**で消費する (成功数で数えるとプロバイダ障害中に課金が縛られない)。根拠: `sai_memory/arasuji/bands.py:1196-1215`
- **現在の挙動 (静的確認)**: 設計は「チャンクごとに呼び直す累計で承認件数まで届く」。編纂ゼロ・束ねだけの走行は `after_chunk` が一度も走らないので最後の 1 回きりになり、**承認 5 件が 3 件で頭打ちになる欠陥があった**。`7d7214be` (2026-09-09) が最後の束ねを「予算が残っていて前回が進んだ間は呼び直す」ループに変えた。根拠: `sea/session_lifecycle.py:6329-6346` (`generate_chronicle` は `:5239`)、`scripts/arasuji/build_arasuji_core.py` にも同型の修正
- **期待の根拠**: `ユーザー原文` — コミットメッセージに「承認 5 件の補修が『まとめ 3 件』で完了顔になった (2026-09-09 実機)」とある (まはーの実機観察)。設計側は `docs/intent/chronicle_coverage_gaps.md`。
- **利用者に見えるか・選べるか**: 承認ダイアログには dry 予測件数が出る (= 承認する件数は見える) が、**「1 呼び出し 3 件」という内部の安全弁は見えない**。env のみ。したがって「承認した件数が実行されなかった」ことは、完了報告の内訳を読まないと分からない。
- **追跡できていない境界**: 完了報告の内訳がどこまで出るか (`chronicle_coverage_gaps.md` G が「完了報告は内訳を言う」を新設項目として挙げている = 実装中)。
- **既存テスト**: `tests/test_metabolism_two_layer.py` (`7d7214be` で 32 行変更)、`tests/test_arasuji_bands.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓ (直近の修正と同便で追加)`

---

### AUTO-18: 起動時の主 DB バックアップ (2 系統)

- **入口**: 自動。`python main.py` の起動シーケンス。①**pre_upgrade バックアップ** — DB が存在すれば無条件・同期で取る (env で無効化できない)。②**startup バックアップ** — `SAIVERSE_DB_BACKUP_ON_START` (既定 true) が有効なら daemon スレッドで取る。根拠: `main.py:352-356`、`:474`、`database/backup.py:21-24`、`:188-205`
- **結果**: `<db>_backup_<kind>_<timestamp>_<uuid>.bak` + `.json` マニフェストを DB と同じディレクトリに作り、`integrity_check` を通し、古い世代を剪定する (既定 10 世代、`SAIVERSE_DB_BACKUP_KEEP` / `SAIVERSE_DB_PREUPGRADE_BACKUP_KEEP`)。根拠: `database/backup.py:18`、`:27-37`、`:80-119`
- **失敗したら何が起きるか**:
  - **pre_upgrade**: `backup_saiverse_db` が `RuntimeError` を投げ、`main.py` は捕まえない → **起動が落ちる**。コメントも「Failure is startup-fatal」と明記。部分バックアップは削除される。根拠: `main.py:352-356`、`database/backup.py:121-133`
  - **startup**: 別スレッドで `LOGGER.exception("Startup backup failed (non-fatal)")` だけ。起動は続く。根拠: `database/backup.py:204-205`
- **期待の根拠**: `既存の仕様文書` — `docs/reference/scripts.md` (未読)、`docs/reference/environment-vars.md` (`SAIVERSE_DB_BACKUP_ON_START` は記載あり。**`SAIVERSE_DB_PREUPGRADE_BACKUP_KEEP` は記載なし**)
- **既存テスト**: `tests/test_audit_batch_one_safety.py` / `tests/test_audit_second_batch_world.py` が `backup_saiverse_db` / `run_startup_backup` に触れる。**`main.py` の起動シーケンス全体を通すテストは無い**。
- **状態**: `機能の存在=✓` `期待の根拠=△ (env 1 本が未記載)` `挙動の静的確認=✓` `テスト対応=△`

---

### AUTO-19: 起動時のスキーマ移行と 12 本のデータ backfill

- **入口**: 自動。`main.py` の起動シーケンス。`needs_migration()` が真ならスキーマ移行 (追加系は ALTER/CREATE の軽量パス、破壊的差分はファイル全書換にフォールバック)。その後 **移行の有無に関係なく毎起動で 12 本の冪等ステップ**が走る。根拠: `main.py:362-450`
- **走るもの (毎起動・無条件)**: `backfill_day_plan_refs` / `backfill_schedule_instance_tokens` / `backfill_city_display_names` / `backfill_desire_stage_normalization` / `backfill_session_anchors` / `backfill_session_head_snapshots` / `ensure_active_occupancy_unique` / `ensure_region_entrance_unique` / `ensure_feed_tables` / `ensure_episode_inheritance_table` / `ensure_task_book_table` / `migrate_deadline_tasks_to_task_book` / `drop_empty_legacy_note_tables` / `ensure_building_memory_tables`。うち **`drop_empty_legacy_note_tables` はテーブルを DROP する** (空になっていれば)。`ensure_active_occupancy_unique` は重複行を**修復してから**部分一意 index を張る。`ensure_region_entrance_unique` は共有入口があれば WARN のみで自動修復しない (「所有の選択は人間の判断」)。根拠: `main.py:378-450`
- **失敗したら何が起きるか**: `ensure_building_memory_tables` だけが try/except で WARNING に落ちる。**他の 13 本は例外を捕まえていない** — 失敗すると起動が落ちる。破壊的移行のフォールバック (`migrate_database_in_place`) は Windows でファイルが開かれていると `WinError 32` になりうる旨がコメントに書かれている。
- **期待の根拠**: `既存の仕様文書` — `CLAUDE.md`「Schema changes go in `database/migrate.py`」、各 backfill のコメントに intent への参照あり
- **追跡できていない境界**: 各 backfill の中身 (`database/migrate.py`) は読んでいない。「冪等」の主張はコメントに拠っており、コードでは検算していない。
- **既存テスト**: `tests/test_p1_migration.py` / `test_desire_stage_migration.py` / `test_note_theme_migration.py` / `test_v3_shape_migration.py` / `test_migrate_building_logs_to_db.py` など個別移行のテストはある。**「起動時にこの 14 本がこの順で走る」という配線を検査するテストは無い**。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△ (各 backfill 本体は未読)` `テスト対応=△`

---

### AUTO-20: 版差分アップグレード (run_startup_upgrade) — 失敗で起動中止

- **入口**: 自動。ペルソナがロードされる前に、各 City と AI の `LAST_KNOWN_VERSION` を現バージョンと比較して必要な upgrade handler を走らせる。NULL は pre-v0.3.0 扱い。根拠: `main.py:452-471`
- **失敗したら何が起きるか**: `run_startup_upgrade` が False を返すと **`sys.exit(1)` で起動を中止**する。ログは「失敗した handler を調べて直して再試行」「`SAIVERSE_SKIP_VERSION_CHECK=1` で回避 (危険)」。根拠: `main.py:463-468`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/version_aware_world_and_persona.md`
- **既存テスト**: `tests/test_upgrade_handlers_legacy_schedule.py` / `tests/test_upgrade_handlers_retired_autonomy.py`。個別 handler の検査で、`main.py` の中止判定は未検査。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

---

### AUTO-21: 二重起動の見張り (runtime marker)

- **入口**: 自動。起動の最初期に `acquire_runtime_marker(city_name, db_path, argv)` を取り、`atexit` で解放する。失敗すると `parser.error()` で起動を止める。根拠: `main.py:339-349`
- **結果**: `~/.saiverse/.runtime/<sha256(city_name)>.json` に pid・token・プロセス生成時刻を書く。マーカーは **City 単位**なので、同じ DB を別 City 名で起動する二重起動はここでは弾けない (その穴を塞ぐのが AUTO-22 の関所)。破壊的な保守コマンド (DB 復元など) はマーカー全体を見て「生きた SAIVerse がいれば拒否」する。根拠: `saiverse/runtime_marker.py:1-30`、`database/backup.py:243-247`
- **期待の根拠**: `既存の仕様文書` — `saiverse/runtime_marker.py` の module docstring、`docs/intent/city_identity.md` §6 (参照)
- **現在の挙動 (静的確認)**: pid が読めても `psutil` が無ければ `os.kill(pid, 0)` にフォールバックし、`PermissionError` は "stopped" に倒さない (fail-closed)。
- **既存テスト**: `tests/test_runtime_marker_failclosed.py`、`tests/test_audit_second_batch_world.py`。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---

### AUTO-22: City 識別子 (CITY_SLUG) の自動修復 — 起動時の破壊的書き換え

- **入口**: 自動。`_init_city_config` が `CITY_SLUG == city_name` の行を見つけられなかったときのフォールバック。根拠: `manager/initialization.py:54-116`
- **結果**: `CITYID=1` の行の `CITY_SLUG` を、コマンドラインで渡された名前に**書き換えて commit する** (WARNING ログ付き)。これは旧チュートリアルが識別子を表示名で上書きした世界の救済。根拠: `manager/initialization.py:94-102`
- **2 つの関所**: ①同じ DB を所有する**別の稼働中プロセスがいたら拒否**する (`another_running_process_owns_db`) — 「稼働中の City を別名で起動すると同じペルソナ群を 2 プロセスが同時運転する」ため ②**City が 1 行だけの DB に限る** — 複数 City の DB で未知名が来たのは改名事故でなく呼び出しの誤り。どちらも `ValueError` で起動を止める。根拠: `manager/initialization.py:74-112`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/city_identity.md` §4 不変条件 2 / §6。`CLAUDE.md` にも記載あり (2026-07-31 の関所追加を含む)。
- **既存テスト**: `tests/test_audit_second_batch_world.py::test_cityname_auto_repair_refused_while_db_is_owned` / `::test_cityname_auto_repair_refused_for_multi_city_db`。**関所 2 本は検査されているが、「実際に修復が成功する」正常系のテストは見つからなかった**。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△ (拒否側のみ)`

---

### AUTO-23: legacy `user_data/` の移送とアドオンデータの移送

- **入口**: 自動。`main.py` の **import 直後、ログ設定より前** に `migrate_legacy_user_data()` と `migrate_addon_data_dirs()` が走る。根拠: `main.py:26-35`
- **結果**: repo 内 `user_data/` が空でなければ `~/.saiverse/user_data/` へ**移動**する (ディレクトリは再帰マージ、**legacy 側が勝つ** = 起動コードが自動生成した宛先ファイルは上書きされる)。Windows でロックされて move できないファイルは copy にフォールバックし、元を残す。copy も失敗したら WARNING で先へ進む。根拠: `saiverse/data_paths.py:359-425`
- **失敗したら何が起きるか**: 個別ファイルの失敗は WARNING のみ (起動は続く)。結果として **一部だけ移った中途半端な状態**になりうるが、次回起動でまた試みる (legacy 側が残っている限り)。
- **期待の根拠**: `既存の仕様文書` — `CLAUDE.md`「`main.py` migrates legacy in-repo `user_data/` on startup」
- **既存テスト**: 直接のテストは見つからなかった (`該当テストなし`)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

---

### AUTO-24: ペルソナ登録時に自動で走る 5 つの処理

- **入口**: 自動。起動時のペルソナロード / 動的作成 / Blueprint spawn のいずれでも `_on_persona_registered` が走る。各ステップは独立 (1 つ失敗しても残りは走る)。根拠: `saiverse/saiverse_manager.py:1181-1268`
- **走るもの**: ①`ensure_autonomy_for` (v0.3 では no-op) ②会話の沈黙タイマーの再確立 (開いている会話があるペルソナだけ) ③当日 day_plan のコマ再予約 (自律ゲート越し = v0.3 では走らない) ④Note → テーマノードページ移行 (main DB → per-persona memory.db、扇形の冪等移行) ⑤v3 形への機械写し (LIFE_PURPOSE / Track の関心 / desire 候補 → コア記憶と手帳)。**④⑤ はペルソナの記憶 DB に書き込む**。LLM は呼ばない。
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md` §9 (Note 移行)、`docs/intent/autonomous_behavior_v3.md` §9-8 (機械写し)
- **現在の挙動 (静的確認)**: ②の「開いている会話」の判定元は未追跡 (→ AUTO-07 の未追跡境界)。③の `downtime_recovery=True` は「落ちていた間に開始時刻を過ぎたコマは遅延実行せず流れたことにする」意味論。
- **既存テスト**: `tests/test_note_theme_migration.py`、`tests/test_v3_shape_migration.py`。`_on_persona_registered` の配線そのものは `tests/test_v03_autonomy_gate.py::test_startup_slot_rescheduling_goes_through_the_gate` が**ソース文字列の一致**で見ているだけ。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

---

### AUTO-25: SAIMemory (ペルソナごとの記憶 DB) の起動時バックアップ

- **入口**: 自動。ペルソナ登録経路 (`persona/bootstrap.py` の `initialise_memory_adapter`) が `startup_backup=True` で adapter を作ったときだけ、かつ `SAIMEMORY_BACKUP_ON_START` (既定 true) が有効なとき、**daemon スレッド**で走る。ツール・API・スクリプトの使い捨て adapter は `False` を渡すので走らない (memory 系スペルの DB ロック玉突きを防ぐ門)。根拠: `saiverse_memory/adapter.py:71-73`、`:114-131`、`:291-292`
- **結果**: `rdiff-backup` があれば差分バックアップ、無ければ単純バックアップ (変更が無ければスキップ)。根拠: `sai_memory/backup.py:430-475`
- **期待の根拠**: `既存の仕様文書` — `CLAUDE.md`「Automatic backups on startup are controlled by … `SAIMEMORY_BACKUP_ON_START`」、`docs/reference/environment-vars.md` に記載あり
- **既存テスト**: `tests/test_open_persona_memory.py` (env の ON/OFF でスレッドが立つかを見る)、`tests/test_people_get_adapter.py` (使い捨て adapter で走らないことを OFF で担保)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---

### AUTO-26: 想起用埋め込みの自動補充

- **入口**: 自動。Metabolism の中で **Chronicle 生成の成否・トグルとは独立に毎回**、未埋め込みの Chronicle / ページ / Fragment を埋める (`ensure_recall_embeddings`)。手動の口は `POST /api/people/{id}/debug/generate-embeddings`。根拠: `sea/session_lifecycle.py:4708-4712`、`api/routes/people/debug.py:42-60`
- **結果**: ローカル埋め込みモデルで処理するので**課金なし**。自動想起 (ゾーン C) の再現率を支える。Chronicle 生成に相乗りさせるとバックログが溜まる (2026-07-04 実測) ので独立させた、とコメントが理由を書いている。
- **期待の根拠**: `既存の仕様文書` — `docs/intent/persona_cognition/debug_controller.md` (参照のみ)、`docs/getting-started/gpu-setup.md`
- **追跡できていない境界**: `ensure_recall_embeddings` 本体 (記憶領域)。GPU 不在時の挙動。
- **既存テスト**: 名前で該当するものは見つからなかった (`tests/test_reembed*` は無い。`api/routes/people/reembed.py` はある)。
- **状態**: `機能の存在=✓` `期待の根拠=△` `挙動の静的確認=△` `テスト対応=✗`

---

### AUTO-27: 終了処理の順序と締め切り

- **入口**: 自動。`atexit` と `SIGTERM` の両方から `shutdown_everything()` (二重実行ガードあり)。FastAPI の lifespan には終了処理を**置かない** (「二本目を作ると順序が絡む」とコメント)。根拠: `main.py:558-625`
- **順序**: ①走行中の発言生成を締める (`stop_all_active_generations`、既定 8 秒) ②MCP 停止 ③Unity Gateway 停止 ④llama-server 群の停止 ⑤API Server サブプロセス停止 ⑥`manager.shutdown()`。①を最初に置く理由がコメントに書かれている — Beat の後始末 (途中本文の確定・記憶書き込み) は生成スレッドの中で走るので、MCP や llama-server を先に壊すと壊れた道具の上で後始末が走り、**下書き行が未確定のまま残って発言が丸ごと消える**。根拠: `main.py:566-586`
- **締め切りを過ぎたら**: WARNING「some active generations did not settle before teardown」を出して先へ進む。つまり **8 秒で締まらなかった発言は失われうる**ことが設計として受容されている。根拠: `main.py:576-582`
- **`manager.shutdown()` の中身**: gateway 停止 → ユーザーを offline に → SDS/db_polling の予約 cancel → FeedManager 停止 (worker join) → ConversationManager 停止 (no-op) → IntegrationManager 停止 → ScheduleManager 停止 → `server_stop` トリガ発火 → **EventScheduler 停止 (pending callback は破棄)** → PhenomenonManager 停止 → 全ペルソナのセッションメタ保存 + 建物保存。根拠: `saiverse/saiverse_manager.py:1100-1175`
- **静的に見えた点**: `AutonomyManager` と pull observer と冷えたウィンドウ見張りは個別に停止されない — すべて EventScheduler の予約なので `event_scheduler.stop()` で一括して消える。ただし**冷えたウィンドウ見張りが既に daemon スレッドへ逃がした畳み処理は、この停止では待たれない** (`_spawn_cold_precompaction` は daemon Thread、join なし)。根拠: `sea/session_lifecycle.py:4417-4421`
- **期待の根拠**: `実装のみ (根拠なし)` — 8 秒という数値と「締まらなければ諦める」の裁定を書いた文書は見つからなかった。コメントは理由を書いているが出典を持たない。
- **既存テスト**: `tests/test_pulse_controller_shutdown.py`。`shutdown_everything` 全体の順序を検査するテストは無い。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=△`

---

### AUTO-28: SDS heartbeat (既定オフ)

- **入口**: 自動。City の `START_IN_ONLINE_MODE` が真のときだけ、`__init__` で `sds_heartbeat` を 30 秒後に積む (`_SDS_BASE_INTERVAL = 30`)。失敗が続くと backoff で最大 300 秒まで伸びる。根拠: `saiverse/saiverse_manager.py:398-409`、`manager/sds.py:65-70`
- **結果**: SDS (`sds_server.py`、port 8080) へ生存通知し、他 City の一覧を取得する。既定は Offline Mode なので走らない。根拠: `saiverse/saiverse_manager.py:411-414`
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md` §8「SDS … 現状はデフォルト無効 … 実質冬眠中」
- **状態**: `機能の存在=✓ (既定オフ)` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

---

### AUTO-29: 外部統合のポーリング (IntegrationManager)

- **入口**: 自動。`SAIVerseManager.start()` の 4 番目。ただし **登録された統合が 0 件ならスレッドを起こさない**。30 秒の基準 tick で、各統合の `poll_interval_seconds` (既定 300 秒) を満たしたものだけ `poll()` する。根拠: `saiverse/integration_manager.py:29-44`、`:87-104`、`:119-142`
- **現在の挙動 (静的確認)**: `saiverse/integrations/` には `base.py` と `__init__.py` しか無い。**同梱の統合実装はゼロ**で、登録経路はアドオン (`expansion_data/<addon>/integrations/*.py`) だけ。つまり既定構成ではスレッドが立たない。根拠: `saiverse/saiverse_manager.py:586-600`
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md` §7 (Addon)、`docs/intent/x_integration.md` (未読)
- **状態**: `機能の存在=✓ (既定では空)` `期待の根拠=△` `挙動の静的確認=✓` `テスト対応=✗`

---

### AUTO-30: llama-server の待機停止 (idle checker)

- **入口**: 自動。llama.cpp サーバーを自動起動したときに daemon スレッド `llama-idle-checker` が立ち、一定間隔で `idle_timeout` を超えたサーバーを探す。根拠: `llm_clients/llama_server.py:388-408`
- **結果**: `/slots` に問い合わせて **"idle" と証明できたときだけ**停止する。"busy" は `busy_deadline` 超過でのみ強制停止、**"unknown" (証明不能) は決して止めない** (fail-safe)。候補選定と停止の間にロックを解放するので、最終ロック内で世代と条件を再確認する (「処理中の射殺」防止)。根拠: `llm_clients/llama_server.py:410-432`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/llama_server_auto_launch.md` (未読)、`CLAUDE.md`「llama.cpp server processes are auto-launched by `llama_server.py`」
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△ (未確認)`

---

### AUTO-31: Pulse の優先度・割り込み・キュー上限 (安全弁)

- **入口**: 4 つの起動源が `PulseController` に集約される (`submit_user` / `submit_schedule` / `submit_auto` / `submit_meta_judgment`)。優先度 USER(1) > SCHEDULE(2) > AUTO(3)。メタ判断だけは priority 体系外の別レーン。根拠: `sea/pulse_controller.py:31-88`
- **安全弁**: **`QUEUE_LIMIT = 10`**。ペルソナごとの待ちキューが 10 件を超えると `LOGGER.error` を出して**最も古い要求を捨てる**。守っているのはプロセス (無制限のキュー成長)。閾値の根拠は**実装のみ** (`# Queue limit - log error if exceeded` というコメント 1 行だけ)。**利用者に見えない・選べない** (ERROR ログのみ、通知なし)。根拠: `sea/pulse_controller.py:28`、`:418-431`
- **中断復帰の規律**: 割り込みで中断された要求を再キューするとき、`args` はコピーするが **`pre_spells` はコピーしない** — 実行前アクション (メール送信・画像生成等の副作用) が割り込みのたびに再実行されるのを防ぐため。「未実行のまま中断された pre_spells が失われる窓は残るが、副作用の二重実行より害が小さい」と明記。根拠: `sea/pulse_controller.py:432-457`
- **期待の根拠**: `既存の仕様文書` — `docs/overview/landscape.md` §3「PulseController」、`docs/intent/` 配下の pulse_dispatch.md (参照のみ、intent ディレクトリには見当たらない)
- **既存テスト**: `tests/test_pulse_controller_shutdown.py`。**キュー上限で古い要求が捨てられる挙動のテストは見つからなかった**。
- **状態**: `機能の存在=✓` `期待の根拠=△` `挙動の静的確認=✓` `テスト対応=△`

---

### AUTO-32: 「クライアントが切れたら止める」型の処理は見つからなかった

- **調べたこと**: `api/` / `saiverse/` / `sea/` を `disconnect` / `is_disconnected` / `ClientDisconnect` / `client_gone` で横断検索した。ヒットしたのは `api/routes/addon_events.py:49` の SSE クライアント切断 debug ログと OAuth の `disconnect` (別概念) だけ。根拠: repo 全体の grep
- **事実として書けること**: ペルソナの Pulse・記憶整理・スケジュール発火・保温を、**HTTP クライアントの接続状態で止める仕組みは実装に存在しない**。生成を止める口は ①利用者が明示的に押す停止 (`cancel_active_generation`) ②プロセス終了時の一括締め (`stop_all_active_generations`) ③高優先度の割り込み (`PulseController`) の 3 つで、いずれも「見られていないから止める」ではない。
- **良し悪しは断定しない**: この事実は `docs/overview/landscape.md` の「住人は見られていなくても生きている」という前提とは矛盾しない、という以上のことは静的には言えない。
- **追跡できていない境界**: `api/routes/chat.py` の NDJSON ストリーミング (`:1178` ほか) で、クライアントが切れた後に生成スレッドが走り続けるかどうか (領域 A の担当だと思われる)。
- **状態**: `機能の存在=✗ (そういう処理は無い)` `期待の根拠=✓ (landscape の前提と整合)` `挙動の静的確認=✓` `テスト対応=—`

---

### AUTO-33: 起動時の Playbook 同期と孤児の削除 (破壊的)

- **入口**: 自動。`main.py` が `sync_playbooks_from_files(manager.SessionLocal)` を毎起動呼ぶ。根拠: `main.py:497`、`saiverse/playbook_sync.py:189-280`
- **結果**: `builtin_data/` / `expansion_data/` / `~/.saiverse/user_data/` の Playbook ファイルを DB に差分同期する (新規 import / 変更 update / 同一 skip)。そのあと **`_prune_orphan_playbooks` が「`scope='public'` かつ `source_file` が非 NULL かつファイルが disk に無い」Playbook を DB から削除し、対応する `PlaybookPermission` 行も削除する**。根拠: `saiverse/playbook_sync.py:138-186`、`:271-278`
- **保護されるもの**: `save_playbook` ツールで作った Playbook (`source_file IS NULL`) と `personal` / `building` scope。
- **利用者から見た意味**: アドオンをアンインストールしたり `expansion_data/` を消したりすると、**次回起動でその Playbook と権限設定が黙って消える** (INFO ログは出る)。復元はファイルを戻して再起動。
- **期待の根拠**: `実装のみ (根拠なし)` — この prune の裁定を書いた文書は見つからなかった。`CLAUDE.md` は import スクリプトの話しかしていない。
- **既存テスト**: 名前で該当するものは見つからなかった (`該当テストなし`)。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

---

### AUTO-34: `main.py` の到達不能なコード

- **`_sync_builtin_playbook_flags(session_factory)`** (`main.py:244-294`) は **repo 全体で呼び出し元がゼロ**。定義だけが残っている。根拠: repo 全体の grep (`main.py:244` の 1 件のみ)
- **状態**: `機能の存在=✗ (到達不能)` — 掃除候補。

---


### 矛盾・疑義

**3-1. 保温 (keep-alive) が自律ゲートを通っていない — intent の主張とコードの食い違い**
- `docs/intent/autonomous_behavior_v3.md` §11.1: 「ライフの設定が無い世界で ON/OFF の差が実際に出るのは**一箇所だけ** (実イベントへの応対)」。
- `saiverse/autonomy_wiring.py:213-215`: 「設定値を読んで**表示・保存するだけの場所**はこの関数を通さないこと — 止め具は『駆動するか』の判定にだけ効かせる」。
- `sea/runtime.py:1994`: `if not bool(getattr(persona, "autonomy_enabled", False)): return False` — **駆動 (LLM コール) の判定でありながら `is_autonomy_on` を通っていない**。
- どちらが正しいかは断定しない。事実として並べる: ①AUTONOMY_ENABLED の ON/OFF は保温の発火にも効く (= 差が出るのは 2 箇所) ②止め具の「止めるもの/止めないもの」の一覧に保温は**どちらにも書かれていない**。

**3-2. 判断点は 4 種か 5 種か (landscape 内の自己矛盾)**
- `docs/overview/landscape.md` §3「判断点」: 「判断点**5 種**に置換された: 起床・就寝・セッション終了・**会話終了**・イベント到着」。
- 同 §9: 「`post_conversation` (会話終了判断) は**裁定ごと退役** (2026-08-16)」。
- コード `saiverse/judgment_points.py:81-92`: kind は 4 種、Playbook も 4 本。`saiverse/autonomy_wiring.py:19-24` も 4 種。
- 同じ文書の §3 と §9 が食い違っており、コードは §9 側と一致する。

**3-3. Observer の playbook 実行 — 文書は動くものとして書き、実装は debug ログの no-op**
- `docs/intent/observer.md:128`「callback が EXEC_KIND/EXEC_TARGET を実行 (… playbook は `PulseDispatcher` 経由)」、`:235`「`"playbook"` — EventScheduler が PulseDispatcher 経由で Playbook 実行 (pull 型)」。
- `saiverse/observer_manager.py:590-591`: `LOGGER.debug("[observer] playbook execution not yet implemented for %s", observer_id)`。
- 同じ文書の `:200` には「playbook 実行を初版に含めるか」が未決として残っており、文書内でも整合していない。DB の `EXEC_KIND` には `"playbook"` が入れられる (`start_pull_observers` の filter が受け付ける) ので、設定できるが何も起きない状態。

**3-4. Observer の「重い処理は別 executor へ」という不変条件が守られていない**
- `docs/intent/observer.md:129` / `:159`: 「重い実行 (I2C 往復・外部 API・playbook) は別 executor に投げる。EventScheduler は単一スレッドなので callback 内で塞がない」。
- `saiverse/observer_manager.py:577-615`: `_execute_pull` → `_execute_tool` → `tool_func(**args)` は EventScheduler の dispatch スレッドで同期実行される。
- 対照的に、冷えたウィンドウ見張り (`sea/session_lifecycle.py:4317-4321`) は同じ理由を挙げて daemon スレッドへ逃がしている。同じ規律が Observer には適用されていない。

**3-5. アラームは「自律行動」なのか — 線引きが文書にない**
- `docs/intent/autonomous_behavior_v3.md` §11 は「アラーム・ルーチンの実行」を **v0.4 (運転の層)** に割り当てている。
- 一方 §11.1 の「止めるもの」の一覧にアラームは無く、実装 (`saiverse/schedule_manager.py`) にもゲートは無い。UI (`ScheduleModal`) は v0.3 で出荷されている。
- §11 は同じ節に「未決 = 『自律行動はさせたくないがアラームだけは設定したい』の扱い」と書いており、この未決が実装上は「アラームは常に鳴る」に倒れている。裁定の記録は見つからなかった。

**3-6. 環境変数 2 本がリファレンスに無い**
- `SAIVERSE_META_LAYER_INTERVAL_SECONDS` (AutonomyManager の tick 間隔、`saiverse/autonomy_manager.py:89-105`)
- `SAIVERSE_DB_PREUPGRADE_BACKUP_KEEP` (pre_upgrade バックアップの保持世代、`database/backup.py:28-31`)
- どちらも `docs/reference/environment-vars.md` に記載が無い (grep で 0 件)。

**3-7. テストスイート全体が「出荷していない世界」を検査している**
- `tests/conftest.py:18-33` の autouse fixture が全テストで `AUTONOMOUS_DRIVING_SHIPPED = True` にする。理由は docstring に書かれており (v0.4 の設計資産を殺さないため)、意図的な判断。
- 結果として、**利用者が今日受け取る v0.3 の挙動を検査しているのは `tests/test_v03_autonomy_gate.py` の 12 本だけ**。そのうち 2 本は `inspect.getsource` の文字列一致で、コードを実行していない。
- これは「テストが間違っている」という主張ではなく、「テストが緑であることが v0.3 の挙動の証拠にならない範囲がある」という事実。

**3-8. `main.py` の起動シーケンスに統合テストが無い**
- `SAIVerseManager.start()` を呼ぶテストは、止め具テストのソース文字列検査 1 本を除いて存在しない (`tests/llm_clients/test_openai_codex_auth.py` の `manager.start()` は無関係な別クラス)。
- `main.py` の 14 本の backfill・2 系統のバックアップ・upgrade の中止判定・playbook の孤児削除・shutdown の順序は、どれも起動を通した検査を持たない。

---

### 凍結・開発者専用・到達不能・文書のみ

| 区分 | 実体 | 状態の具体 | 根拠 |
|---|---|---|---|
| **凍結 (入口封鎖済み)** | inter-city travel の DB polling (`VisitingAI` / `ThinkingRequest`) | `SAIVerseManager.__init__` が **EventScheduler への登録を行わない**。ポーリング関数本体 (`manager/background.py`) は残っている。`self.db_polling_stop_event` は shutdown 経路の互換のため残置。再有効化の env は**意図的に作られていない** | `saiverse/saiverse_manager.py:456-470` |
| **凍結 (入口封鎖済み)** | `RemotePersonaProxy` | クラスは残るが `is_proxy` 属性と初期化だけの器。`/inter-city/*` と `/persona-proxy/{id}/think` は 503 を返す (`database/api_server.py`、未読) | `saiverse/remote_persona_proxy.py` 全文 |
| **no-op (クラス削除待ち)** | `ConversationManager` | `start()` / `stop()` / `trigger_next_turn()` がすべて `return` のみ。`SAIVerseManager.start()` の 3 番目でループを回すが実質何もしない | `saiverse/conversation_manager.py:26-34`、`saiverse/saiverse_manager.py:560-562` |
| **削除済み** | `SubLineScheduler` (`saiverse/pulse_scheduler.py`) | モジュールごと削除。`SAIVerseManager.start()` に「旧 SubLineScheduler の起動は廃止」の NOTE だけが残る | `saiverse/saiverse_manager.py:521-524`、landscape §9 |
| **削除済み** | v1 メタ判断・`InternalAlertPoller`・Track Handler 三種・Tracks API | landscape §9 に退役の記録。`MetaLayer` は判断 Pulse の共有基盤 (per-persona Lock・判断設定・判断ログ) としてのみ存続 | `docs/overview/landscape.md` §9、`saiverse/meta_layer.py` (251 行) |
| **出荷していない (定数で停止)** | 判断点 4 種 / watchdog / 時間割のコマ / 実イベントの判断経由 | `AUTONOMOUS_DRIVING_SHIPPED = False`。コードも Playbook も DB スキーマも全部揃っているが 1 回も発火しない | AUTO-01〜04 |
| **既定オフ** | SDS heartbeat | City の `START_IN_ONLINE_MODE` が既定 false。SDS サーバー (`sds_server.py`) も別プロセスで手動起動が要る | `saiverse/saiverse_manager.py:398-414`、landscape §8 |
| **既定で空** | IntegrationManager のポーリング | 同梱の統合実装がゼロ (`saiverse/integrations/` は `base.py` のみ)。登録 0 件ならスレッドを起こさない | `saiverse/integration_manager.py:93-96` |
| **実装されていない (設定はできる)** | Observer の `EXEC_KIND == "playbook"` | debug ログを出して終わる。設計文書は動くものとして書いている (→ §3-3) | `saiverse/observer_manager.py:590-591` |
| **到達不能** | `main.py:_sync_builtin_playbook_flags` | repo 全体で呼び出し元ゼロ | `main.py:244` |
| **開発者専用** | 一日シミュレーション (`saiverse/day_simulator.py` / `day_scenario.py`、`scripts/run_day_sim.py`) | 仮想クロック中は EventScheduler の背景スレッドが発火せず、同期駆動 API (`next_fire_time` / `run_due`) だけが動く | `saiverse/event_scheduler.py:21-28`、`:415-420` |
| **開発者専用** | `api/routes/people/debug.py` の 6 ルート | 埋め込み一括生成と Memopedia 変換のみ。自律系の口 (fire-meta-judgment / fire-subline-pulse / scheduler / wrap-up-conversation) は 2026-08-14 と 2026-08-23 に削除済み | `api/routes/people/debug.py:1-21` |
| **UI を隠している** | 自律行動管理 / ライフビュー / できごと / タスク管理 / 時間割テンプレート | v0.3 で画面とルートごと削除 (landscape §9)。**データと記憶の形は v0.3 に入っている** | `docs/overview/landscape.md` §9 最終行 |

---

### この領域で「検査が無い」と判断した重要な結果

利用者またはペルソナが受け取る結果のうち、既存テストがまったく触れていないもの。

1. **アラームが自律 OFF のペルソナでも鳴り、LLM 課金が発生する** (AUTO-05)。`ScheduleManager` に自律ゲートが無いという事実を固定するテストは無い。台帳・冪等・世代照合のテストは厚いが、「v0.3 でこれが動く」という利用者から見た結果を押さえていない。
2. **キャッシュ保温が画面を閉じていても LLM を呼び続ける** (AUTO-13)。`test_cache_keepalive.py` は v0.4 前提 (conftest が止め具を外す) で動いており、v0.3 の出荷状態での保温の発火を検査していない。課金が発生する自動処理としては、この経路がいちばん検査が薄い。
3. **冷えたウィンドウ見張りが 10 分ごとに全ペルソナを巡回し、条件成立で編纂 (課金) を始める** (AUTO-14)。周期の積み直し・inflight 排他・`due` 判定を主題にしたテストが見つからなかった。
4. **Observer の閾値通知が Building へ `host` メッセージを注入する** (AUTO-10)。`_evaluate_notify_rules` / `_notify_building` を通すテストが見つからなかった。ペルソナの記憶に入りうる書き込みなので、内容の検査が無いこと自体が結果に効く。
5. **Observer の周期予約が DB エラーで永久に止まる** (AUTO-10)。`_execute_pull` がツール例外しか捕まえないため、`schedule_periodic` の「例外で永久停止」契約に直結する。利用者から見ると観測が黙って止まる。テストなし。
6. **起動時に Playbook の孤児が削除され、権限行も消える** (AUTO-33)。アドオンを外すと次回起動で消える、という利用者に見える結果。テストも設計文書も見つからなかった。
7. **`main.py` の起動シーケンスそのもの** (AUTO-18〜23, 33)。pre_upgrade バックアップの失敗が起動を落とすこと、14 本の backfill がこの順で走ること、upgrade 失敗で `sys.exit(1)` すること、legacy `user_data` の移送 — どれも起動を通した検査を持たない。DB を触る破壊的処理が集中している場所なので、影響は最大。
8. **終了時に 8 秒で締まらなかった発言が失われうる** (AUTO-27)。`shutdown_everything` の順序と締め切りに関するテストは無く、8 秒という数値の裁定も文書に見つからなかった。
9. **Pulse キューが 10 件を超えると最も古い要求が捨てられる** (AUTO-31)。ERROR ログだけで利用者に通知は無い。テストなし。
10. **legacy `user_data/` の移送で「legacy 側が勝つ」上書きが起きる** (AUTO-23)。起動の最初期に走り、失敗は WARNING のみ。テストなし。

---

### 付記: この領域から見た事故 2 件の位置

依頼の物差しとして挙がっている 2 件は、どちらも記憶領域の欠陥だが、**この領域が発火の引き金を握っている**。

- **事故 1 (レベル 2 あらすじが 1 つしか生成されない)** — 束ねの安全弁 (1 呼び出し 3 件、AUTO-17) が「呼び直しの累計で承認件数に届く」前提で作られており、その呼び直しを回す側 (`generate_chronicle` の最後の束ね) が 1 回きりだった。**検査対象になるはずだった場所は「安全弁とその呼び出し側の契約」** — 安全弁の側は「1 呼び出し 3 件」を正しく守っており、単体で見れば正しい。壊れていたのは呼び出し側の累計の作り方で、これは AUTO-17 の「安全弁の閾値の根拠と、それが誰の呼び直しを前提にしているか」を台帳に書いていれば拾えた性質のもの。
- **事故 2 (大量の未整理履歴をスルースにも全量読ませた)** — 発火の引き金は非常畳み (Metabolism の一形態、AUTO-15) で、量の見張りが無かった。**検査対象になるはずだった場所は「自動発火する処理の入力量の上限」** — AUTO-15 の 3 つの安全弁 (レート制限の小休止・defer-to-hot・圧力弁) はどれも「いつ走らせるか」を決めるもので、「一回にどれだけ入れるか」を守るものが無かった。その穴を埋めたのが AUTO-16 (冷たいときの飛ばし、既定 10 万字) で、これは第一段。第二段 (飛ばした範囲を利用者に見せる) は未起草。
- 共通するのは、**安全弁が「誰のために何を守るか」と「その閾値の根拠」を台帳に持っていなかった**こと。この報告では AUTO-05 / 14 / 15 / 16 / 17 / 31 に安全弁を列挙し、それぞれ「守る対象・閾値の出所・利用者に見えるか」を書いた。閾値の出所が `実装のみ` になっているのは AUTO-16 の 10 万字・AUTO-27 の 8 秒・AUTO-31 の 10 件の 3 つ。

---

## 領域 F. 導入・運用・拡張・外部連携 (OPS-01〜35)

> **まはーの一次情報 (2026-09-09、この調査の中間報告に対する応答。原文)**
>
> 「Unity用のポートヤバいな……。今Unity連携完全に凍結してる状態だし、対処しておいた方が良いな。」
>
> **該当項目**: `OPS-28` (Unity ゲートウェイ)。
>
> Unity 連携が現在**完全に凍結**していること、および既定で待ち受けている状態に**対処が要る**という
> 判断が示された (期待の根拠 = ユーザー原文)。具体的な対処の内容 (既定値を `false` にする /
> 待ち受け先をループバックに限る / 機構ごと外す / 認証を足す) は未決。
>
> 調査で確認した事実は次の 3 点: ①`main.py:501` の既定値が `"true"` で、明示的に切らない限り起動する
> ②`unity_gateway/server.py:85` の待ち受け既定が `0.0.0.0:8765` (全インターフェース)
> ③`server.py:160-172` が接続時の認証を確かめないまま全ペルソナの ID と表示名を返す。
> `UNITY_GATEWAY_ENABLED` は `.env.example` にも `docs/reference/environment-vars.md` にも記載が無く、
> 存在を知らなければ切ることができない。



### OPS-01: 初回セットアップ (setup.bat / setup.sh)

- **入口**: 利用者が展開したフォルダで `setup.bat` をダブルクリック (Windows) / `./setup.sh` を実行 (Mac・Linux)。根拠: `README.md:170`, `README.md:243`
- **結果**: Python venv 作成 → `pip install -r requirements.lock` → `npm install` → DB 初期化 (条件付き) → Playbook 取り込み → `expansion_data/` 作成 → Git 導入と `git init` → `.env` 作成 → SearXNG 導入 → 埋め込みモデル (ONNX) ダウンロード。根拠: `setup.bat:1-320`, `setup.sh:1-180`
- **期待の根拠**: `利用者向け説明` (`README.md:129-250` クイックスタート) + `既存の仕様文書` (`docs/getting-started/installation.md`, `docs/intent/dependency_management.md` §2-2)
- **現在の挙動 (静的確認)**:
  - 両者ともインストール元は `requirements.lock` で、`requirements.txt` は読まない。根拠: `setup.bat:146`, `setup.sh:45`
  - DB 初期化は「`~/.saiverse/user_data/database/saiverse.db` または旧 `database/data/saiverse.db` に対して `SELECT 1 FROM city` が通るか」だけで判定する。通らなければ **確認なしで `seed.py --force`** を実行する。根拠: `setup.bat:170-196`, `setup.sh:57-84`
  - Windows は Node.js と Git を winget → portable の順で自動導入する。Mac/Linux は導入されていなければエラー (Node) / 警告 (Git) で終わる。根拠: `setup.bat:44-96`, `setup.bat:207-266`, `setup.sh:20-27`, `setup.sh:95-130`
  - `git init` 経路は `git init` → `remote add origin` → `fetch` → `branch -M main` → **`git reset origin/main`** (mixed) → `set-upstream-to`。作業ツリーのファイルはそのまま残る。根拠: `setup.bat:256-263`, `setup.sh:104-111`
  - 埋め込みモデルは `ignore_patterns=['*.bin','*.safetensors','*.h5','openvino/*','*.ot']` で ONNX 版だけを落とす。読み込み側 (`sai_memory/config.py:22-28`) が `onnx/model.onnx` を探すので、これは意図どおり。根拠: `setup.bat:295`, `setup.sh:159`, `sai_memory/config.py:99-140`
  - 画面表示の言語が非対称: `setup.bat` は全文英語、`setup.sh` は全文日本語。根拠: `setup.bat:6-320` vs `setup.sh:6-180`
  - `.venv` が壊れているときの自己修復は Windows のみ。`setup.sh` は `.venv` ディレクトリの存在だけを見て「既に存在します」と言い、直後の `source .venv/bin/activate` が `set -euo pipefail` の下で失敗して終わる。根拠: `setup.bat:100-135` vs `setup.sh:29-38`
- **追跡できていない境界**:
  - ZIP 展開 → `git reset origin/main` の後、作業ツリーが「変更あり」と判定されるかどうか。リリース ZIP は `git archive` 生成 (OPS-31) なので origin/main と同一コミットなら一致するはずだが、`.gitattributes` の改行変換 (`*.bat eol=crlf` 等) が `git archive` の出力にどう効くかを実行して確かめていない。ここが一致しないと OPS-03 の「作業ツリーが汚れていたら更新拒否」に直行する。
  - winget / portable インストーラ (`scripts/install_node_portable.ps1`, `install_git_portable.ps1`) の中身は未読。
- **既存テスト**: `tests/test_requirements_lock_contract.py::test_install_paths_read_the_lock_not_the_intent_file` / `::test_install_paths_mention_the_lock` が `setup.bat` / `setup.sh` の本文を文字列検査している (`-r requirements.txt` が無いこと・`requirements.lock` の語があること)。**スクリプトを実行するテストは無い**。両プラットフォームの手順の対応関係を検査するテストも無い。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (lock を読むかの文字列検査のみ。分岐・破壊的動作・両プラットフォームの同期は未検査)

---

### OPS-02: 起動 (start.bat / start.sh / start-dev.*) と中断された更新の自己回復

- **入口**: `start.bat` をダブルクリック / `./start.sh [city_name]`。根拠: `README.md:174`, `start.sh:6-7`
- **結果**: (1) `update_engine.py --check-complete` を回して、終了コード 10 なら `--manual` で更新を仕上げてから起動、失敗したら **起動しない**。(2) SearXNG 起動 (条件付き)。(3) `python main.py <city>` をバックエンドとして起動。(4) `npm run build` → `npm start` でフロントエンド。(5) ブラウザを `http://localhost:3000` で開く。根拠: `start.bat:29-53`, `start.bat:59-88`, `start.sh:38-80`
- **期待の根拠**: `利用者向け説明` (`README.md:174-180`) + `既存の仕様文書` (`docs/reference/scripts.md` 「開発 / 運用」節の `--check-complete` 行、`docs/issues/v0229_update_bat_truncates_after_git_pull.md`)
- **現在の挙動 (静的確認)**:
  - 終了コードの契約は `update_engine.py` 側に定数として置かれている (`CHECK_READY=0` / `CHECK_NEEDS_FINISH=10` / `CHECK_INCONCLUSIVE=11`)。根拠: `scripts/update_engine.py:56-59`
  - `start.bat` は City 名を `city_a` に決め打ちする。`start.sh` は第 1 引数で受ける。根拠: `start.bat:70` vs `start.sh:12,64`
  - SearXNG の起動条件が非対称: Windows は「導入されていれば必ず起動」、Mac/Linux は「`SAIVERSE_SEARXNG=1` のときだけ」。根拠: `start.bat:60-66` vs `start.sh:56-60`
  - `start-dev.bat` / `start-dev.sh` は更新自己回復ブロックを持たない (dev モードは中断更新の検査を通らずに起動する)。根拠: `start-dev.bat:1-30`, `start-dev.sh:29-45`
  - `start.bat` は毎回 `npm run build` を実行してから `npm start` する (本番モード)。根拠: `start.bat:78-81`
- **追跡できていない境界**: `--check-complete` が「判定できない (11)」を返す条件のうち、実環境で最も起きうるもの (パッケージ照会の失敗) がどれくらいの頻度で起きるかは実行していないので不明。
- **既存テスト**: `tests/test_update_completion_marker.py` が `check_update_complete()` を **Python 関数として** 直接呼び、3 つの終了コードを網羅的に検証している (40 本超)。`git` / `pip` / `npm` は tmp_path 上のダミーで置き換える。**`start.bat` / `start.sh` のシェル分岐そのものを実行するテストは無い** — 終了コードを bat の `if errorlevel` が正しく捌くか (errorlevel の降順判定を含む) は未検査。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (Python 側は厚い。シェル側の分岐と両プラットフォームの意味の一致は未検査)

---

### OPS-03: 手動更新 (update.bat / update.sh / update_from_github.ps1 / self_update.py)

- **入口**: SAIVerse を停止した状態で `update.bat` / `./update.sh` を実行。根拠: `update.bat:1-9`, `update.sh:1-8`
- **結果**: `scripts/update_engine.py --manual` が (1) portable git を PATH へ、(2) 作業ツリーが汚れていないか検査、(3) 世界のスナップショット作成、(4) `git fetch` + `git merge --ff-only --no-overwrite-ignore @{upstream}`、(5) 完了印の削除、(6) `pip install -r requirements.lock`、(7) `pip check` で衝突を警告のみ、(8) `npm ci`、(9) 完了印の書き込み。失敗すると `git reset --hard <旧 revision>` + 依存の入れ直しへ戻す。根拠: `scripts/update_engine.py:1070-1103`, `:726-1000`
- **期待の根拠**: `既存の仕様文書` (`docs/reference/scripts.md` 「開発 / 運用」節、`docs/intent/dependency_management.md` §2-5) + `利用者向け説明` (`README.md:139` 「ZIP で導入しても、その後の自動更新 (update.bat) まで手動 Git なしで動きます」)
- **現在の挙動 (静的確認)**:
  - **4 入口すべてが同一エンジンに集約されている**。`update.bat:8` と `update.sh:8` と `scripts/update_from_github.ps1:7` は `scripts/update_engine.py --manual` を呼ぶだけ。`scripts/self_update.py` は 8 行の互換 shim。根拠: `scripts/self_update.py:1-9`
  - Git チェックアウトでないと更新できない。ZIP overlay 経路は明示的に無効化されている。根拠: `scripts/update_engine.py:740-745`
  - 追跡ファイルに変更があると更新しない。stash も reset もせず、変更ファイル名と 2 つの出口 (捨てる / commit する) を提示して止まる。根拠: `scripts/update_engine.py:753-767`
  - `--no-overwrite-ignore` により、新版が追跡し始めるパスに ignored ファイルがあると merge が拒否して旧版のまま止まる。根拠: `scripts/update_engine.py:842-858`
  - スナップショットの制限時間は固定 3600 秒。コメント自身が「暫定値であって解ではない」と明記している。根拠: `scripts/update_engine.py:800-810` (関連: `docs/issues/snapshot_timeout_is_fixed_while_world_grows.md`)
  - アドオンが本体と同じ venv を共有するため、lock の更新がアドオン側の依存を壊しうる。`pip check` の結果を **警告として記録するだけ** で、更新は止めないし戻さない。根拠: `scripts/update_engine.py:873-925`
- **追跡できていない境界**:
  - 複数の更新入口 (update.bat / API detached / 起動時自己回復) が同時に走ったときの挙動。プロセス間ロックが無いことは `docs/issues/update_entrances_lack_process_lock.md` に既知課題として起票されている (未解決)。
  - README の「ZIP でも update.bat が動く」という約束が、実際の ZIP 展開環境で成立するか (OPS-01 と同じ境界)。
- **既存テスト**: `tests/test_update_engine_safety.py` (16 本) — 汚れたツリーの拒否、ignored ファイル上書きの拒否、psutil 欠落時の fail-closed、依存失敗時の rollback。`git` は tmp_path 上の実リポジトリを作って **実際に呼ぶ**。`pip` / `npm` はモンキーパッチで置き換える。`tests/test_update_completion_marker.py` (40 本超) — 完了印のライフサイクル。`tests/test_snapshot_exclusions.py::test_pre_update_snapshot_*` (3 本) — スナップショット段の時間枠と後始末。**更新の全段を通しで実行するテストは無い** (pip / npm を実際に走らせる検査は存在しない)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓` (領域 F の中では最も検査が厚い。ただし ZIP 由来チェックアウトの端は未検査)

---

### OPS-04: 画面からのセルフアップデートと更新通知

- **入口**: 新しい版があるとき画面にバナーが出て、押すと `window.confirm` の後 `POST /api/system/update`。通知の元は `GET /api/system/version`。根拠: `frontend/src/app/page.tsx:1452-1487`, `frontend/src/app/page.tsx:1126`
- **結果**: バックエンドが (1) venv の interpreter で psutil の実在を実行確認、(2) 作業ツリーの清潔さを事前確認、(3) 再起動契約 (`.update_config.json`) を atomic に書く、(4) デタッチした updater を起動、(5) 3 秒後に `manager.shutdown()` → 子プロセス終了 → `os._exit(0)`。updater 側は本体の終了を待ってから OPS-03 と同じ全段を回し、最後に同じ引数で再起動して health を確認する。根拠: `api/routes/system.py:415-588`, `scripts/update_engine.py:994-1068`
- **期待の根拠**: `既存の仕様文書` (`docs/issues/self_update_unsafe_without_psutil.md`, `docs/overview/release_history.md` の v0.3.11 範囲)
- **現在の挙動 (静的確認)**:
  - psutil の検査は API プロセス自身の import ではなく **updater が実際に使う venv の interpreter** をサブプロセスで叩き、`create_time()` の呼び出しまで確認する。失敗したら 409 で断り、本体は落とさない。根拠: `api/routes/system.py:441-473`
  - 版の比較は GitHub Releases API の `tag_name` と `VERSION` のタプル比較。1 時間キャッシュ。根拠: `api/routes/system.py:40-86`
  - 409 のとき、サーバーが返した `detail` (変更ファイル一覧を含む) をそのままトーストに出す。根拠: `frontend/src/app/page.tsx:1466-1479`
  - 確認ダイアログの文言だけ英語 (`Update to v...?`)。根拠: `frontend/src/app/page.tsx:1454-1456`
- **追跡できていない境界**: デタッチ後の再起動 (`restart_application` → `wait_for_healthy_restart`) が Windows の実環境で確実に親から切り離されるか。`CREATE_BREAKAWAY_FROM_JOB` の失敗時フォールバックは書かれているが実測していない。
- **既存テスト**: `tests/test_system_update_api.py::UpdatePsutilPrecheckTest` (2 本) — psutil プローブの成否だけを検査。`subprocess.run` をモックし、**spawn と shutdown は実行しない** (プローブ失敗時に spawn/shutdown が呼ばれないことを assert する形)。`tests/test_update_completion_marker.py::test_detached_update_records_only_after_a_healthy_restart` / `::test_failed_restart_leaves_no_marker` が detached 経路の完了印を検査するが、`restart_application` は差し替えている。**`.update_config.json` の内容 (main_args / db_identity 等) が再起動で正しく使われるかの通しテストは無い**。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (psutil の門と完了印は検査済み。spawn→shutdown→restart の実経路は未検査)

---

### OPS-05: 世界のスナップショット (更新前の自動 + CLI からの手動)

- **入口**: (a) 自動 — OPS-03/04 の更新が pull の前に必ず 1 つ作る。(b) 手動 — `snapshot.bat {save|list|restore|inspect|delete}` / `./snapshot.sh ...`。根拠: `scripts/update_engine.py:1079-1080`, `snapshot.bat:5-6`, `snapshot.sh:2-3`
- **結果**: `~/.saiverse/` 配下 (`backups` / `snapshots` / `llama_cache` を除く) を sha256 マニフェスト付き ZIP にまとめて `~/.saiverse/snapshots/<name>.zip` へ。restore はステージングへ展開 → 検証 → 現状の自動スナップショット → 一括入れ替え。根拠: `scripts/snapshot.py:63-130`, `:505-602`, `:663-737`
- **期待の根拠**: `既存の仕様文書` (`docs/reference/scripts.md` 「開発 / 運用」節、`docs/intent/version_aware_world_and_persona.md:205-215`)
- **現在の挙動 (静的確認)**:
  - restore は SAIVerse が動いていると拒否し、この検査は迂回できない。根拠: `scripts/snapshot.py:669-673`
  - restore は `RESTORE` のタイプ入力を求める (`--yes` で省略可)。復元前に現状を自動スナップショットする (`--no-auto-snapshot` で省略可)。根拠: `scripts/snapshot.py:688-726`
  - アーカイブ内のパスは `PROTECTED_FROM_ARCHIVE` (= 除外集合から `llama_cache` を引いたもの) への書き込みを拒否する。根拠: `scripts/snapshot.py:88`, `:357-372`
  - 更新側でタイムアウトした場合の書きかけ `.zip.tmp` は、子を殺した側 (update_engine) が消す。根拠: `scripts/update_engine.py:787-838`
- **追跡できていない境界**: 実際の大きな世界 (数十 GB) で 3600 秒に収まるかは実行していない。既知課題として `docs/issues/snapshot_timeout_is_fixed_while_world_grows.md` に「恒久策は未着手」と記録されている。
- **既存テスト**: `tests/test_snapshot_exclusions.py` (13 本) — 除外集合、アーカイブメンバーの脱走防止、入れ替えの原子性、更新前スナップショットの後始末と時間枠。`tests/test_snapshot_script_standalone.py::test_snapshot_save_runs_without_editable_install` (1 本) — save を **サブプロセスで実際に実行** する。**restore の通し (稼働中拒否・自動スナップショット・入れ替え) をエンドツーエンドで実行するテストは無い** (入れ替え関数 `swap_staged_world` 単体は検査されている)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△` (save と入れ替え部品は検査済み。restore コマンドの通しは未検査)

---

### OPS-06: データベースの自動バックアップと復元

- **入口**: (a) 自動 — 起動のたびに背景スレッドで 1 世代。加えて起動時の書き換え (schema / backfill / version handler) の直前に同期の `pre_upgrade` 世代。(b) 手動復元 — `python database/backup.py --db <path> restore <backup>`。根拠: `main.py:352-356`, `main.py:473-474`, `database/backup.py:315-346`
- **結果**: 検証済み (integrity_check 済み) の世代が `~/.saiverse/` 配下に溜まり、既定 10 世代で剪定される。復元は staged コピーの integrity_check を通してから差し替え、差し替え前の安全バックアップを返す。根拠: `database/backup.py:18-133`, `:150-186`, `:237-305`
- **期待の根拠**: `利用者向け説明` (`README.md:35` 「自動バックアップ機能を搭載しており、起動するたびに会話データ等がコピー・保存されます」) + `既存の仕様文書` (`docs/reference/environment-vars.md` の `SAIVERSE_DB_BACKUP_ON_START`)
- **現在の挙動 (静的確認)**:
  - 世代数は `SAIVERSE_DB_BACKUP_KEEP` / `SAIVERSE_DB_PREUPGRADE_BACKUP_KEEP` で別々に制御される。既定 10。根拠: `database/backup.py:18-38`
  - `SAIVERSE_DB_BACKUP_ON_START=false` で起動時バックアップを止められる。`pre_upgrade` 側はこのフラグに関係なく走り、失敗すると起動を止める。根拠: `database/backup.py:21-25`, `main.py:352-356`
  - **復元の入口は CLI のみで、UI からは押せない**。`restore_saiverse_db_backup` の呼び出し元はこの CLI とテストだけ。根拠: `database/backup.py:337`, `tests/test_audit_second_batch_world.py:297`
- **追跡できていない境界**: Windows の WAL モードでバックアップ中のファイルロックがどう振る舞うか (`docs/handoff/2026-07-13_migration_upgrade_backup_audit.md:104-106` に過去の WinError 32 の記録がある)。実行していないので現在も起きるかは不明。
- **既存テスト**: `tests/test_audit_second_batch_world.py` の一部 (`backup_saiverse_db` / `restore_saiverse_db_backup` を **実 SQLite ファイルに対して実行**、安全バックアップの生成と壊れた入力の拒否を検査)。**世代剪定 (`_prune_old_backups`) と `SAIVERSE_DB_BACKUP_ON_START=false` の経路を検査するテストは見つからなかった**。
- **状態**: `機能の存在=✓` `期待の根拠=✓ (README に「バックアップされる」だけ。復元手順は利用者向け文書に無い — §3 参照)` `挙動の静的確認=✓` `テスト対応=△`

---

### OPS-07: データベースのスキーマ移行 (起動時自動 + 手動 CLI)

- **入口**: (a) 自動 — `python main.py` の起動シーケンスで `needs_migration()` が真なら実行。(b) 手動 — `python database/migrate.py [--db <path>] [--force]`。根拠: `main.py:362-370`, `database/migrate.py:2461-2484`
- **結果**: 追加系 (新規テーブル / 新規列) は生きた DB に ALTER/CREATE で当てる。破壊的差分のときだけ「DB を `.bak` へ move → 新スキーマで作り直し → データ移送」を行い、失敗したら `.bak` を戻す。続いて 15 本前後の backfill / ensure が無条件で走る。根拠: `main.py:358-445`, `database/migrate.py:210-495`
- **期待の根拠**: `既存の仕様文書` (`CLAUDE.md` 「Common Pitfalls」の migrate 節、`docs/reference/database-schema.md`)
- **現在の挙動 (静的確認)**:
  - `--db` を明示したのにファイルが無ければエラーで止まる。省略時は `default_db_path()`。根拠: `database/migrate.py:2477-2480`
  - 全書換経路はバックアップを **move** (コピーではない) で作る。復旧は `.bak` を戻す形。根拠: `database/migrate.py:230-255`, `:474-486`
  - 起動時の backfill 群はすべて「冪等なので毎回呼んでよい」というコメント付きで無条件に実行される。根拠: `main.py:372-445`
  - バージョン対応のアップグレードハンドラが失敗すると `sys.exit(1)` で起動を止める。`SAIVERSE_SKIP_VERSION_CHECK=1` で迂回できる旨がログに書かれている。根拠: `main.py:456-470`
- **追跡できていない境界**: backfill / ensure 関数 60 本超の個々の冪等性は読んでいない。特に `migrate_deadline_tasks_to_task_book` (`database/migrate.py:1316-1463`) のような「写す」系は、二周目に 0 件になる根拠がコメントに書かれているだけで、実データで確かめていない。
- **既存テスト**: `tests/test_migrate_building_logs_to_db.py` ほか、個々の移行に対応するテストが tests/ に点在する。**`main.py` の起動時 migration シーケンス (順序依存を含む) を通しで実行するテストは見つからなかった** — 特に「`ensure_task_book_table` の後に `migrate_deadline_tasks_to_task_book` を置くこと」という順序の不変条件 (`main.py:436` のコメント) を守らせる機械検査は無い。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△ (個々の backfill は未追跡)` `テスト対応=△`

---

### OPS-08: データベース初期化 (`database/seed.py`) — 破壊的

- **入口**: (a) 利用者が直接 `python database/seed.py` を実行。(b) **セットアップが条件付きで `--force` 付きで自動実行する** (OPS-01)。根拠: `database/seed.py:390-408`, `setup.bat:189`, `setup.sh:77`
- **結果**: 既存 DB を消して seed データから作り直す。ペルソナ・会話履歴・Playbook・アイテム・入退室ログがすべて消える。根拠: `database/seed.py:259-330`
- **期待の根拠**: `既存の仕様文書` (`CLAUDE.md` 「Database — read this before running anything destructive」、`seed.py` 自身の argparse epilog)
- **現在の挙動 (静的確認)**:
  - 対話実行では `DELETE` の全大文字入力を求める。`--force` は無条件。根拠: `database/seed.py:284-302`
  - どちらの経路でも削除前に `<db>_backup_<timestamp>.bak` を同じディレクトリに作る。根拠: `database/seed.py:304-308`
  - **消えないもの**: `~/.saiverse/personas/<id>/memory.db` (SAIMemory)、`tasks.db`、建物ログ `log.json`。seed.py はこれらに触れない。結果として、seed 後は persona ディレクトリが残ったまま DB 側の AI 行が新規 ID で作り直される。根拠: `database/seed.py:1-410` (persona ディレクトリへの言及が無いこと)
  - セットアップ経由の `--force` は「DB ファイルはあるが `city` テーブルの SELECT が失敗する」ときに発火する。破損 DB や移行途中の DB がこの条件に当たりうる。根拠: `setup.bat:170-196`
- **追跡できていない境界**: 「`city` テーブルが読めない DB」が実際にどういう状態で発生するか (途中で止まった全書換 migration、ファイル破損など) を確かめていない。
- **既存テスト**: **該当テストなし**。`seed_database()` を呼ぶテストは見つからなかった (`tests/` に `seed.py` を import するファイルなし)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

---

### OPS-09: 汎用テーブル閲覧・編集・削除 API (`/api/db/tables`)

- **入口**: (a) 画面 — ワールドエディタと初回チュートリアルが **GET だけ** 使う。(b) API 直叩き — POST (upsert) / DELETE (行削除) はどのフロントエンドからも呼ばれていない。根拠: `frontend/src/lib/dbTable.ts:57`, `frontend/src/app/page.tsx:1095`, `frontend/src/components/tutorial/TutorialWizard.tsx:144,282`, `frontend/src/components/settings/WorldEditor.tsx:137`
- **結果**: GET は 1 ページ最大 1000 行 + 総件数を `X-Total-Count` ヘッダで返す。POST は SQLAlchemy の `merge` で任意テーブルに upsert。DELETE は主キー完全一致で 1 行削除。根拠: `api/routes/db_manager.py:51-160`
- **期待の根拠**: `実装のみ (根拠なし)` — `docs/user-guide/global-settings.md` は「データベース管理」タブがあると書いているが、実装にそのタブは無い (§3)。`docs/reference/api-endpoints.md` には自動生成でルートが載っている。
- **現在の挙動 (静的確認)**:
  - 対象テーブルは `database/models.py` の全モデルを動的に列挙して決まる。除外リストは無い。根拠: `api/routes/db_manager.py:17-25`
  - DELETE は主キーの過不足を 400 で弾く。バックアップも確認も無い。根拠: `api/routes/db_manager.py:133-160`
  - POST は空文字列を None に置換するだけで、型検証も外部キー検証もしない。根拠: `api/routes/db_manager.py:99-131`
  - ループバック起動 (既定) では認証が一切かからない (OPS-30)。
- **追跡できていない境界**: 「POST/DELETE を実際に叩いた人がいるか」は静的には分からない。UI からの経路は無い。
- **既存テスト**: `tests/test_db_manager_api.py::DbManagerPaginationTest` (9 本) — **GET のページ送りのみ**。FastAPI の TestClient で実 SQLite に対して実行する。**POST / DELETE を検査するテストは無い**。
- **状態**: `機能の存在=✓` `期待の根拠=✗ (POST/DELETE は文書にも UI にも根拠が無い)` `挙動の静的確認=✓` `テスト対応=△ (GET のみ)`

---

### OPS-10: 環境変数 (.env) の閲覧と編集

- **入口**: グローバル設定の「環境」タブ。`GET /api/admin/env` → `POST /api/admin/env`。初回チュートリアルの API キー設定も同じ関数を通る。根拠: `api/routes/admin.py:54-158`, `api/routes/tutorial.py:500`
- **結果**: `.env` が書き換わり、`os.environ` も即時更新され、キー系が変わったときは全ペルソナの LLM クライアントと router クライアントが破棄される。根拠: `api/routes/admin.py:72-141`
- **期待の根拠**: `利用者向け説明` (`docs/user-guide/global-settings.md` 「環境」タブ行) + `既存の仕様文書` (`docs/reference/environment-vars.md`)
- **現在の挙動 (静的確認)**:
  - 一覧では `KEY` / `TOKEN` / `SECRET` / `PASSWORD` を含むキーの値を `********` にマスクする。根拠: `api/routes/admin.py:24-26`, `:54-70`
  - 書き込みは `open(ENV_FILE_PATH, "w")` で **その場で truncate** してから書く。一時ファイル + `os.replace` は使っていない。根拠: `api/routes/admin.py:100-104`
  - `ENV_FILE_PATH = Path(".env")` はプロセスの作業ディレクトリ相対。根拠: `api/routes/admin.py:14`
  - 非 Windows では書き込み後に `chmod 600`。根拠: `api/routes/admin.py:106-108`
  - 同じ「壊れかけの書き込み」の危険に対して、プロバイダ設定側は一時ファイル + `os.replace` を採用しており、その理由まで書かれている (`saiverse/provider_configs.py:169-177`)。`.env` とモデル JSON (OPS-15) はその形になっていない。
- **追跡できていない境界**: `.env` の書き込みが失敗したときにフロントエンドがどう見せるか (500 のトーストのみか) は未確認。
- **既存テスト**: **該当テストなし**。`write_env_updates` / `read_env_file` を検査するテストは見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

---

### OPS-11: サーバー再起動 (`POST /api/admin/restart`)

- **入口**: グローバル設定の画面から。根拠: `frontend/src/components/GlobalSettingsModal.tsx:631`
- **結果**: 1 秒後に `manager.shutdown()` を明示的に走らせてから `os.execv(python, [python] + sys.argv)` でプロセスを置き換える。根拠: `api/routes/admin.py:161-181`
- **期待の根拠**: `実装のみ (根拠なし)` — `docs/user-guide/global-settings.md` に再起動ボタンの記載は無い。
- **現在の挙動 (静的確認)**: `os.execv` は Python のクリーンアップを一切走らせないので、`shutdown()` の明示呼び出しが唯一の保存機会になっている。子プロセス (inter-city API server) の終了処理はこの経路には無い。根拠: `api/routes/admin.py:163-179` (比較: `api/routes/system.py:568-582` の update 経路は子プロセスを terminate している)
- **追跡できていない境界**: `os.execv` 後に旧プロセスの子 (api_server, SearXNG) がどうなるか。Windows での `execv` の挙動は実行していないので不明。
- **既存テスト**: **該当テストなし**。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△ (子プロセスの行方が未追跡)` `テスト対応=✗`

---

### OPS-12: 壊れた建物ログの隔離・復元・リセット・退避

- **入口**: 起動時にログが読めないと隔離され、画面上部にバナーが出る。`QuarantineModal` から復元 / リセットを選ぶ。旧形式ログが読めない場合は「脇へ移す」。根拠: `api/routes/system.py:198-412`, `frontend/src/components/QuarantineModal.tsx`, `frontend/src/components/SystemAlertBanner.tsx`
- **結果**: 復元 = 選んだバックアップを `log.json` へコピーし、メモリへ読み直し、ペルソナの読み位置を切り詰める。リセット = 空履歴で再開し、壊れたファイルは `.corrupted_*` に残る。退避 = ファイル名を変えて毎起動の検算から外す。根拠: `api/routes/system.py:226-292`, `:295-343`, `:346-412`
- **期待の根拠**: `既存の仕様文書` (`docs/intent/building_memory_unified.md` 「過去ログ取り込みの自動化と検算」)
- **現在の挙動 (静的確認)**:
  - 復元元は「起動時に列挙した候補リスト」に含まれるものだけ受け付ける。JSON としてパースでき、かつ list であることを確認してからコピーする。根拠: `api/routes/system.py:240-262`
  - リセットは一時ファイル + `os.replace` で空 `[]` を書き、ペルソナの読み位置と採番カウンタも戻す。根拠: `api/routes/system.py:309-330`
  - これら 3 つの状態変更ルートは、`docs/issues/api_state_changing_routes_have_no_origin_check.md` が「どこから来た操作か」の確認が無い例として名指ししている (未着手)。
- **追跡できていない境界**: 隔離が起きる条件 (起動時のログ読み込み失敗の判定) は `saiverse/` 側にあり、そこまでは追っていない。
- **既存テスト**: 復元・リセットの API を検査するテストは `tests/` に見つからなかった (`quarantine` で grep して該当なし)。**該当テストなし**。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

---

### OPS-13: お知らせ配信と監視トグル

- **入口**: 自動 — フロントエンドが `GET /api/system/announcements` を読む。トグルは `GET/POST /api/config/announcements-monitor` と `GET/POST /api/config/update-check`。根拠: `api/routes/system.py:127-160`, `api/routes/config.py:555-582`
- **結果**: 開発者の Gist から JSON を取得して画面に出す。30 分キャッシュ。取得失敗時は古いキャッシュを返す。根拠: `api/routes/system.py:127-152`
- **期待の根拠**: `実装のみ (根拠なし)` — 利用者向け文書にお知らせ機能の説明は見つからなかった。
- **現在の挙動 (静的確認)**:
  - 取得先は `SAIVERSE_ANNOUNCEMENTS_URL` で差し替えられるが、この変数は `.env.example` にも `docs/reference/environment-vars.md` にも載っていない。根拠: `api/routes/system.py:31-37`
  - CDN キャッシュ回避のためクエリにタイムスタンプを付ける。根拠: `api/routes/system.py:140-141`
  - トグルは `manager.state` 上のフラグだけを変える。永続化されているかはここからは読み取れない。
- **追跡できていない境界**: トグルの値が再起動をまたぐか (DB / 設定ファイルに保存されるか) は `manager/state.py` を読み切っていないので未確認。
- **既存テスト**: **該当テストなし**。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=△` `テスト対応=✗`

---

### OPS-14: LLM プロバイダの管理

- **入口**: グローバル設定 → 「モデル管理」タブ → 「プロバイダ」サブタブ。一覧 / 追加 / 編集 / 削除 / 接続テスト / 再読込。根拠: `frontend/src/components/GlobalSettingsModal.tsx:51-52,748`, `frontend/src/components/settings/ProviderManagementPanel.tsx`, `api/routes/providers.py:129-420`
- **結果**: `~/.saiverse/user_data/providers/<id>.json` が作られ・書き換わり・消える。保存のたびにプロバイダとモデルの両方が再読込される。根拠: `saiverse/provider_configs.py:145-215`
- **期待の根拠**: `既存の仕様文書` (`docs/intent/model_provider_management.md` ステータス「実装完了」) + `利用者向け説明` (`docs/reference/providers.md`, `docs/custom_providers.md`, `docs/user-guide/global-settings.md` 「モデル管理タブ」節)
- **現在の挙動 (静的確認)**:
  - UI から作れるのは `openai_compat` / `ollama_compat` のみ。他は 400。根拠: `api/routes/providers.py:29-30,146-160`
  - builtin の編集は必ず user_data に上書き用ファイルを作る。builtin 本体は変更されない。builtin だけのプロバイダの削除は 403。根拠: `api/routes/providers.py:180-227`, `saiverse/provider_configs.py:190-215`
  - どの層から読んだかは **ローダーが実際に歩いたルート** から刻む。ファイル内の自己申告 (`builtin`) は捨てる。根拠: `saiverse/provider_configs.py:85-96`
  - 保存は一時ファイル + `os.replace`。理由 (壊れた JSON はスキップされてプロバイダが一覧から消える) がコメントに書かれている。根拠: `saiverse/provider_configs.py:169-186`
  - プロバイダを編集するとモデル側も必ず再解決する。理由 (モデルは読み込み時に base_url / api_key_env を取り込むため) がコメントにある。根拠: `saiverse/provider_configs.py:131-143`
  - builtin は 12 件で `docs/reference/providers.md` の表と一致する (`ls builtin_data/providers/` = 12)。
- **追跡できていない境界**: 接続テスト (`POST /api/providers/test`) が各プロトコルの実エンドポイントに何を投げるか (`api/routes/providers.py:260-420`) は読んだが、実際のプロバイダで正しく判定するかは実行していない。
- **既存テスト**: `tests/test_provider_configs.py` (25 本超) — 保存/削除/上書き/`provider_ref` の継承/OpenRouter の帰属ヘッダ。**ただし `TestLoadProviders` は `USER_DATA_DIR` を隔離せずに実環境の `~/.saiverse/user_data/providers/` を読む** ため、開発者の手元に `openai` などの上書きがあると `source == builtin` の assert が落ちる。またこのテストは 7 個の ID の存在しか見ておらず、`openrouter` / `plamo` / `sakana` / `lmstudio` / `llama_cpp_server` の 5 件は数えていない (テスト名の「seven」がそれ)。API ルート (`api/routes/providers.py`) を通す検査は見つからなかった。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△ (設定層は厚い。API 層は未検査、環境隔離が無い)`

---

### OPS-15: モデル定義ファイルの管理

- **入口**: グローバル設定 →「モデル管理」→「モデル」サブタブ (追加 / 編集 / 複製 / 削除)。加えてチャット画面の設定から「別名で保存 / 上書き保存」(`POST /api/config/models/save-from-chat`)。根拠: `frontend/src/components/settings/ModelManagementPanel.tsx`, `ModelEditorModal.tsx`, `api/routes/config.py:1518-1700`
- **結果**: `~/.saiverse/user_data/models/<key>.json` が作られ・書き換わり・消える。毎回 `reload_configs()` が走る。根拠: `api/routes/config.py:1518-1607`
- **期待の根拠**: `既存の仕様文書` (`docs/intent/model_provider_management.md` §2-3) + `利用者向け説明` (`docs/user-guide/global-settings.md` 「モデル管理タブ」節)
- **現在の挙動 (静的確認)**:
  - builtin / expansion のモデルを編集すると自動で user_data 側にコピーが作られ、応答に `created_override` が入る。builtin だけのモデルの削除は 403。根拠: `api/routes/config.py:1545-1601`
  - 作成・更新・複製・チャットからの保存の 4 経路すべてが水位検査 (`_validate_watermarks`) を通る。複製にも通す理由がコメントに書かれている。根拠: `api/routes/config.py:1536,1565,1628`
  - **保存が `user_path.write_text(...)` の直接上書き**。OPS-14 のプロバイダ側が一時ファイル経由にした理由 (壊れた JSON は一覧から消える) は、モデル側にもそのまま当てはまる — `load_configs()` はパース失敗を warning にしてそのモデルを飛ばし、しかも user_data の名前が先に「見た」扱いになるので builtin へのフォールバックも起きない。根拠: `api/routes/config.py:1537-1542`, `saiverse/model_configs.py:104-127`, `saiverse/data_paths.py:139-166`
  - モデルの探索順のうち「ファイル名で直接探す」段だけが `get_data_paths(MODELS_DIR)` を使っており、これは `expansion_data/models/` (アドオン直下ではなく expansion_data の直下) を見る。読み込み本体 (`iter_files_with_layer`) は `expansion_data/<addon>/models/` を見る。**この 2 つは同じ「expansion 層」を別の場所として扱っている**。根拠: `saiverse/model_configs.py:632-644` vs `saiverse/data_paths.py:149-159`
- **追跡できていない境界**: 上の食い違いが実害になるのは「MODEL_CONFIGS に載っていないモデル ID をファイル名で引く」場面だけで、通常は段 1/2 で見つかるため到達しない可能性が高い。到達する呼び出し元を特定していない。
- **既存テスト**: `tests/test_model_configs.py` (25 本超) — 価格計算、コンテキスト長、画像対応、`find_model_config`。`tests/test_model_watermark_validation.py` — 水位検査。**API ルートを通した作成/更新/削除/複製の検査は見つからなかった**。壊れた JSON がモデルを一覧から落とすことを確かめるテストも無い。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

---

### OPS-16: モデルロールと全体設定 (`/api/config/*` のトグル群)

- **入口**: グローバル設定の「モデルロール」タブと「環境」タブ。根拠: `frontend/src/components/GlobalSettingsModal.tsx:742-767`
- **結果**: モデル一覧・スロット種別・Playbook 一覧・現在のモデルとパラメータ・キャッシュ設定・画像既定品質・メディア再生・Gemini 自動キャッシュ・画像埋め込み上限・お気に入りモデル・水位の全体既定 (`metabolism-defaults`) などが読み書きされる。根拠: `api/routes/config.py:93-1516`
- **期待の根拠**: `利用者向け説明` (`docs/user-guide/global-settings.md`) — ただし網羅していない。`既存の仕様文書` として `docs/intent/model_provider_management.md`。
- **現在の挙動 (静的確認)**: 40 ルートの内訳は、(a) 読み取り専用の一覧 8 本、(b) 現在のモデル / パラメータの読み書き 4 本、(c) 個別トグル・数値設定の GET/POST 対 11 組、(d) モデルファイル CRUD 6 本 (OPS-15)、(e) 開発者モード 2 本 (OPS-17)。保存先は `manager.state` (メモリ) と DB とファイルが混在している。根拠: `api/routes/config.py` の各 `@router` 行 (93, 127, 148, 170, 243, 318, 399, 493, 504, 510, 555, 561, 569, 575, 617, 626, 705, 733, 755, 761, 776, 782, 801, 813, 848, 864, 896, 903, 931, 967, 1013, 1032, 1168, 1432, 1438, 1518, 1546, 1581, 1607, 1643)
- **追跡できていない境界**: 各トグルの永続化先 (`manager.state` の値が再起動をまたぐか) を 1 本ずつ確かめていない。
- **既存テスト**: `tests/test_config_set_playbook.py`、`tests/test_model_change_seq.py` (モデル切り替えの世代トークン)、`tests/test_model_watermark_validation.py`。**大半のトグルには対応するテストが無い**。
- **状態**: `機能の存在=✓` `期待の根拠=△ (画面の説明は主要タブのみ)` `挙動の静的確認=△` `テスト対応=△`

---

### OPS-17: 開発者モードのトグル

- **入口**: `GET/POST /api/config/developer-mode`。根拠: `api/routes/config.py:504-547`
- **結果**: OFF にすると **全ペルソナの `AUTONOMY_ENABLED` が DB ごと一括で False に更新され**、メモリ上のペルソナと AutonomyManager も同期される。ON に戻しても復元されない。根拠: `api/routes/config.py:518-546`
- **期待の根拠**: `既存の仕様文書` — `docs/issues/developer_mode_off_mass_disables_autonomy.md` がこの連動を「ConversationManager 時代の名残に見える。まはーの裁定を得てから撤去か維持を決める」として **未解決** で起票している。
- **現在の挙動 (静的確認)**: 上記のとおり。副作用は API のレスポンスには現れず、`{"success": true, "enabled": false}` だけが返る。根拠: `api/routes/config.py:547`
- **追跡できていない境界**: 画面側でこの副作用を利用者に告知しているかは `GlobalSettingsModal.tsx` / `Sidebar.tsx` の該当箇所を読み切っていない。
- **既存テスト**: **該当テストなし** (`developer-mode` / `set_developer_mode` で grep して tests/ に該当なし)。
- **状態**: `機能の存在=✓` `期待の根拠=✓ (issue として明文化されている)` `挙動の静的確認=✓` `テスト対応=✗`

---

### OPS-18: Codex サブスク認証 (SAIVerse 自身での ChatGPT ログイン)

- **入口**: プロバイダ管理画面の `CodexLoginModal`。`POST /api/codex-auth/login/start` → 表示された user_code をユーザーがブラウザで入力 → フロントが `GET /login/status` をポーリング。根拠: `frontend/src/components/settings/CodexLoginModal.tsx`, `api/routes/codex_auth.py:48-75`
- **結果**: SAIVerse 自前のトークンストアにトークンが保存され、`openai_codex` プロバイダのモデルが使えるようになる。ログアウトは自前ストアだけを消し、`~/.codex/auth.json` には触らない。根拠: `api/routes/codex_auth.py:77-95`
- **期待の根拠**: `既存の仕様文書` (`docs/intent/codex_subscription_auth.md` — ステータス「完了 (v0.2, 2026-08-16)」、実機 end-to-end 確認済みの記載あり) + `利用者向け説明` (`docs/reference/providers.md` の `openai_codex` 行)
- **現在の挙動 (静的確認)**:
  - トークンの値はどのレスポンスにも載せない設計がモジュール docstring に明記されている。根拠: `api/routes/codex_auth.py:1-14`
  - cancel は `attempt_id` + `lease_id` の両方を必須にして、閉じたモーダルの遅延 cancel が新しい試行を殺さないようにしている。省略経路は意図的に作られていない。根拠: `api/routes/codex_auth.py:35-46`
  - intent doc が **未検証の境界を 3 件自己申告している** (ログアウトが `~/.codex` を消さないことの実機未確認、同一 client_id での共存、lease 返却不達の受容)。根拠: `docs/intent/codex_subscription_auth.md` ステータス行
- **追跡できていない境界**: `llm_clients/openai_codex_auth.py` の `LOGIN_MANAGER` 内部 (トークンストアの場所・更新・失効処理) は読んでいない。
- **既存テスト**: intent doc が「テスト 44 本」と書いている。tests/ 内で `codex` を含むファイル名は見つからなかったので、どのファイルに入っているかは特定できていない (**未確認**)。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△ (API 層のみ)` `テスト対応=△ (存在すると文書にあるが、場所を特定できていない)`

---

### OPS-19: アドオンカタログ (registry 取得・導入・更新・削除)

- **入口**: 左サイドバー → アドオン管理 → 「カタログ」タブ。導入 / 更新 / 削除は確認ダイアログ (`AddonActionConfirmDialog`) を挟んで進捗ダイアログ (SSE) へ。根拠: `frontend/src/components/Sidebar.tsx:596`, `AddonManagerModal.tsx:1071-1230`, `AddonCatalogPanel.tsx:173-190`, `AddonInstallProgressDialog.tsx:56-120`
- **結果**: `expansion_data/<addon_id>/` に shallow clone → 指定 commit へ checkout → `addon.json` を検証 → `setup.steps` を実行。削除は `uninstall.steps` → ディレクトリ削除 → (チェックが入っていれば) `~/.saiverse/user_data/addon_data/<id>/` も削除。根拠: `saiverse/addon_installer.py:460-694`, `api/routes/addon_catalog.py:343-435`
- **期待の根拠**: `既存の仕様文書` (`docs/intent/addon_catalog_management.md` — ステータス「Phase 4 完了 (voice-tts 除く、2026-05-23)」)
- **現在の挙動 (静的確認)**:
  - registry は既定で `raw.githubusercontent.com/maha0525/saiverse-addon-registry/main/registry.json`。`SAIVERSE_ADDON_REGISTRY_URL` で差し替え可能。根拠: `saiverse/addon_registry.py:33-36,200-201`
  - **公式 URL からの registry は Ed25519 署名の検証を必須にする**。署名が無い / 壊れているものは拒否。公式以外の URL で未署名を通すには `SAIVERSE_ALLOW_UNSIGNED_REGISTRY` の明示が要る。HTTPS 以外は拒否し、リダイレクト後も HTTPS を確認する。根拠: `saiverse/addon_registry.py:228-292`
  - `repo_url` は `https://` 始まりのみ、`commit` と `id` は形式検査あり。根拠: `saiverse/addon_registry.py:94,122,132-135`
  - 導入失敗時は clone したディレクトリを削除して戻すが、**永続データディレクトリには触れない**。根拠: `saiverse/addon_installer.py:481,549-560`
  - `setup.steps` の `pip_install` は本体の `requirements.lock` を constraints (`-c`) として必ず渡す。lock が無ければ実行を拒否する。根拠: `saiverse/addon_installer.py:233-264`
  - 更新は `setup_version` が上がったときだけ `setup.steps` を再実行する。根拠: `saiverse/addon_installer.py:616-639`
  - 削除の確認ダイアログには「永続データも削除する (保存された参照音声・OAuth トークン等が消えます)」というチェックボックスがあり、既定 OFF。根拠: `frontend/src/components/AddonActionConfirmDialog.tsx:57,146-158,174`
  - 同じアドオンに対する並行操作はプロセス内ロックで 409 に落とす。**プロセスをまたぐロックは無い**。根拠: `api/routes/addon_catalog.py:50-60,258-263`
  - 完了イベントの `restart_required` は `api_routes.py` の有無だけで決まる。根拠: `api/routes/addon_catalog.py:143-144,311`
- **追跡できていない境界**:
  - **アドオン各々の内部実装 (tools / playbooks / speak_hook / api_routes の中身) は担当外**。ここで扱ったのは本体側の「取ってくる・置く・登録する・消す」までで、`expansion_data/saiverse-voice-tts/` 等の中身は別調査が要る。
  - `_execute_steps` の各ステップ種別 (`platform_script` / `python_script` / `git_clone` / `download_file` / `remove_dir`) がアドオン作者の任意コードを実行する。パス検査 (`_check_addon_dir_path` / `_check_data_dir_path`) はあるが、実行されるスクリプト本体の安全性は本体側では検査していない。
- **既存テスト**: `tests/test_addon_registry_trust.py` (3 本) — 署名検証の 3 分岐 (正しい署名 / 改竄 / 未署名の opt-in)。`tests/test_addon_installer_constraints.py` (3 本) — pip に lock が constraints で渡ること・lock が無ければ拒否すること (`_run_subprocess` を差し替えてコマンド列だけを検査)。**`install_addon` / `update_addon` / `uninstall_addon` を通しで実行するテストは無い**。`scripts/test_addon_catalog_api.py` は **起動中の SAIVerse に対して手動で叩く E2E スクリプト** であって自動テストではない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△ (信頼境界と pip の縛りだけ。導入・更新・削除の本体は未検査)`

---

### OPS-20: アドオンの有効/無効と設定

- **入口**: アドオン管理モーダルの「導入済み」タブ。トグルとパラメータ入力欄 (グローバル / ペルソナ別 / ファイル添付)。根拠: `frontend/src/components/AddonManagerModal.tsx:1194-1226`, `api/routes/addon.py:345-1100`
- **結果**: `AddonConfig` / `AddonPersonaConfig` (DB) が更新され、有効化/無効化に連動して MCP サーバー・Integration・server hook・Composite action の登録が **再起動なしで** 切り替わる。根拠: `api/routes/addon.py:427-540`
- **期待の根拠**: `既存の仕様文書` (`docs/intent/mcp_addon_integration.md` §3, `docs/intent/addon_extension_points.md` §C, `docs/intent/addon_speak_hooks.md` §D)
- **現在の挙動 (静的確認)**:
  - 無効化のときだけ MCP の後片付けの完了を最大 5 秒待つ。有効化は待たない (subprocess 起動が数十秒かかるため)。待ち切れたかどうかを `mcp_settled` として返し、画面が取り直しの判断に使う。根拠: `api/routes/addon.py:451-465,532-540`
  - 4 つの通知 (MCP / Integration / server hook / composite action) はそれぞれ `try/except` で囲まれ、失敗しても warning のみで進む。根拠: `api/routes/addon.py:456-520`
  - ファイル型パラメータのアップロード / 取得 / 削除が persona 別と global の両方にある (6 ルート)。根拠: `api/routes/addon.py:872-1100`
- **追跡できていない境界**: 有効化に失敗した 4 経路のいずれかが warning で握り潰されたとき、画面に何も出ない。利用者から見て「有効にしたのに動かない」がどう見えるかは未確認。
- **既存テスト**: `tests/test_addon_toggle_propagation.py`、`tests/test_addon_config_mcp_reconnect.py`、`tests/test_addon_routes_params_merge.py`、`tests/test_addon_secret_param_deletion.py`、`tests/test_addon_hooks.py`、`tests/test_addon_loader_integrations.py`、`tests/test_addon_external_loader.py`、`tests/test_addon_paths.py`。トグル伝播と設定マージは実コードを通す形で検査されている。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✓`

---

### OPS-21: アドオンのアクション定義 (composite action)

- **入口**: アドオン管理モーダル内の `ActionsPanel`。定義の CRUD と「テスト実行」。根拠: `api/routes/addon_actions.py:32-160`, `frontend/src/components/ActionsPanel.tsx`
- **結果**: 複数の MCP ツール呼び出しをまとめた「アクション」が定義され、Spell として登録される。根拠: `api/routes/addon_actions.py:76-140`, `saiverse/composite_actions.py`
- **期待の根拠**: `既存の仕様文書` — `docs/intent/` 配下に composite action 単独の intent は見つからず、`addon_extension_points.md` にも記載が無い。実装が先行している可能性がある。
- **現在の挙動 (静的確認)**: 9 ルート。`available-tools` / `tool-schemas` / `test-targets` が UI の入力補助を返し、`POST /actions/test` が実行する。根拠: `api/routes/addon_actions.py:39-52,141-160`
- **追跡できていない境界**: `POST /actions/test` が実際に MCP ツールを呼ぶ (= 物理デバイスを動かしうる) かどうか。`saiverse/composite_actions.py` の実行部は読んでいない。
- **既存テスト**: `tests/test_native_tool_addon_prefix.py` が composite action に触れるが、ルート層の検査は見つからなかった。**ルート層は該当テストなし**。
- **状態**: `機能の存在=✓` `期待の根拠=✗ (intent doc が見つからない)` `挙動の静的確認=△` `テスト対応=△`

---

### OPS-22: アドオンイベントの常設 SSE

- **入口**: フロントエンドが `EventSource('/api/addon/events')` で接続し続ける。根拠: `api/routes/addon_events.py:22-30`
- **結果**: アドオンからの非同期イベント (音声再生完了、アドオンのトグル変更など) が画面に届く。根拠: `saiverse/addon_events.py`, `api/routes/addon.py:521-530`
- **期待の根拠**: `実装のみ (根拠なし)` — 利用者向け説明は不要な内部機構。
- **現在の挙動 (静的確認)**: 25 秒ごとに keep-alive コメントを送る。ルート登録順が `addon_events` → `addon_actions` → `addon` の順で、`/{addon_name}` のキャッチオールに飲まれないよう明示的にコメントされている。根拠: `api/main.py:31-37`, `api/routes/addon_events.py:19,44-49`
- **追跡できていない境界**: `saiverse/addon_events.py` の `set_event_loop` / `emit_addon_event` がバックグラウンドスレッドから安全に呼べるかの検証。
- **既存テスト**: **該当テストなし**。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

---

### OPS-23: アドオン UI パネルの取り込み

- **入口**: 自動 — `npm run dev` / `npm run build` の前に `predev` / `prebuild` hook が `frontend/scripts/sync-addon-panels.mjs` を走らせる。根拠: `frontend/package.json:11-12`
- **結果**: `expansion_data/<addon>/ui/Panel.tsx` が `frontend/src/addon-panels/<addon>/` にコピーされ、`frontend/src/addon-panels.generated.ts` に lazy import のレジストリが生成される。`AddonManagerModal` の各カードがこれを引いて描画する。根拠: `frontend/scripts/sync-addon-panels.mjs:1-60`
- **期待の根拠**: `実装のみ (根拠なし)` — このスクリプトの docstring 以外に文書が見つからなかった。
- **現在の挙動 (静的確認)**:
  - コピー先は `.gitignore` されている。根拠: `.gitignore:110-113`
  - 実際に UI パネルを持つアドオンは stackchan の 1 件。根拠: `ls frontend/src/addon-panels/`
  - **カタログ導入の `restart_required` は `api_routes.py` の有無だけで決まる** (OPS-19) ため、UI パネルだけを持つアドオンを導入しても「再起動不要」と表示される。しかしパネルの反映にはフロントエンドの再ビルドが要る。根拠: `api/routes/addon_catalog.py:143-144` と `frontend/package.json:11-12` の突き合わせ
- **追跡できていない境界**: `start.bat` は毎回 `npm run build` を通るので Windows の通常起動では次回起動時に反映される。`start-dev` / `npm start` 単独の場合の挙動は実行して確かめていない。
- **既存テスト**: **該当テストなし**。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

---

### OPS-24: MCP サーバーの状態確認・再接続・停止・再試行・直接呼び出し

- **入口**: アドオン管理モーダルの `MCPSection` (アドオン別 / 全体)。根拠: `frontend/src/components/AddonManagerModal.tsx:1035,1197`, `api/routes/mcp.py:20-165`
- **結果**: サーバーインスタンスの一覧・ツール一覧・失敗一覧の表示、再接続、強制停止、再試行。加えて `POST /api/mcp/tool-call` が **ペルソナを経由せず MCP ツールを直接実行する** (管理・デバッグ用途)。根拠: `api/routes/mcp.py:58-230`
- **期待の根拠**: `既存の仕様文書` (`docs/intent/mcp_addon_integration.md`) + `docs/reference/api-endpoints.md` (自動生成)
- **現在の挙動 (静的確認)**:
  - 「繋ぎ直す接続がまだ無い」は失敗ではなく `message` として返し、画面が失敗と区別できるようにしている。根拠: `api/routes/mcp.py:58-103`
  - `per_persona` の手動停止は、そのペルソナの次の Pulse head で復活する。docstring に明記され、`docs/issues/mcp_per_persona_manual_stop_revives.md` に紐づいている。根拠: `api/routes/mcp.py:105-120`
  - `POST /api/mcp/tool-call` は `visible: false` の管理系ツール (`gpio_test`, `self.i2c.scan` 等) を含めて任意のツールを叩ける。**確認も権限検査も無く、ループバック起動では認証もかからない**。根拠: `api/routes/mcp.py:163-230`
  - MCP 設定の探索は 4 段 (`user_data/mcp_servers.json` → `user_data/<project>/mcp_servers.json` → `expansion_data/<pack>/mcp_servers.json` → `builtin_data/mcp_servers.json`)。`expansion_data` 由来のサーバー名は `<addon_name>__` を自動で前置して分離する。根拠: `tools/mcp_config.py:293-390`
  - `enabled` は厳密な真偽値判定。文字列 `"false"` は「無効にしようとした意図」として無効扱いにする。根拠: `tools/mcp_config.py:243-268`
- **追跡できていない境界**: `tools/mcp_client.py` の接続ライフサイクル (refcount、backoff、per_persona インスタンスの生成) は読んでいない。
- **既存テスト**: `tests/test_mcp_config.py` (25 本超) — 4 層の優先順位、addon プレフィックス、プレースホルダ解決。`tests/test_mcp_reconnect_outcome.py`、`test_mcp_error_classification.py`、`test_mcp_tool_refresh.py`、`test_mcp_connection_owner_task.py`、`test_mcp_subprocess_errlog.py`。**`POST /api/mcp/tool-call` を検査するテストは見つからなかった**。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△ (設定と再接続は厚い。直接呼び出しは未検査)`

---

### OPS-25: 汎用 OAuth フロー (アドオン宣言)

- **入口**: アドオン管理モーダルの `OAuthFlowSection`。`GET /api/oauth/start/{addon}/{flow}?persona_id=...` で認可 URL を得てポップアップを開き、`GET /api/oauth/callback/...` に戻る。根拠: `frontend/src/components/OAuthFlowSection.tsx`, `api/routes/oauth.py:52-115`
- **結果**: トークンが `AddonPersonaConfig` に保存され、以後アドオンは `get_valid_token()` で取り出す (期限切れは自動リフレッシュ)。切断でトークンを削除。根拠: `saiverse/oauth/handler.py:324-460,514`
- **期待の根拠**: `既存の仕様文書` (`docs/intent/addon_extension_points.md` セクション B — **ステータスは「ドラフト (まはーレビュー待ち)」**)
- **現在の挙動 (静的確認)**:
  - PKCE (S256) と one-shot な state を使う。state は in-memory で TTL 付き、サーバー再起動で揮発する。根拠: `saiverse/oauth/handler.py:16-19,43,294-322,346-356`
  - `client_secret` があるときは HTTP Basic Auth ヘッダで送る (X 等が body の secret を受けないため)。根拠: `saiverse/oauth/handler.py:367-390`
  - callback の base URL は `X-Forwarded-Proto` / `X-Forwarded-Host` を優先する。根拠: `api/routes/oauth.py:40-50`
  - **`/api/oauth/callback/` は LAN 公開時の owner 認証を素通しする例外パス**になっている (認可サーバーからのリダイレクトを受けるため)。根拠: `api/owner_auth.py:59-61`
- **追跡できていない境界**: `_invoke_post_authorize_handler` がアドオン側の任意関数を呼ぶ (`saiverse/oauth/handler.py:465-512`)。その先はアドオンの実装で担当外。
- **既存テスト**: `tests/test_oauth_handler.py` (16 本) — PKCE の形、必須パラメータ、state の one-shot 性と不正拒否、トークン交換の失敗、リフレッシュ、status がトークンを含まないこと、切断でトークンが消えること。HTTP は差し替える。**API ルート層 (`api/routes/oauth.py`) の検査は無い** — 特に owner 認証の例外パスの扱いは未検査。
- **状態**: `機能の存在=✓` `期待の根拠=△ (intent doc がドラフト状態のまま実装が先行)` `挙動の静的確認=✓` `テスト対応=△`

---

### OPS-26: 3 層リソース解決 (user_data > expansion_data > builtin_data)

- **入口**: 自動 — 起動時および reload 時に各サブシステムが読む。根拠: `saiverse/data_paths.py:67-304`
- **結果**: 同名リソースがあれば上位層が勝つ。どの層から読んだかは trust 判定 (プロバイダの api_key_env 制限) に使われる。根拠: `saiverse/provider_configs.py:37-49`, `saiverse/provider_security.py`
- **期待の根拠**: `既存の仕様文書` (`CLAUDE.md` 「Directory Structure」節「resource loading is 3-layer」、`docs/reference/providers.md` §「API キー名の縛りは『誰が宣言したか』で変わる」)
- **現在の挙動 (静的確認)** — **ヘルパーが 3 つの異なる形を持っている**:
  1. `iter_files_with_layer` / `iter_files` — expansion は **`expansion_data/<addon>/<subdir>/`**。層のラベルを歩いたルートから返す。使い手: モデル (`saiverse/model_configs.py:106`)、プロバイダ (`saiverse/provider_configs.py:61`)、フィードプリセット (`saiverse/feed_presets.py:129`)、プロンプト (`api/routes/world.py:449`)
  2. `iter_project_subdirs` / `get_project_data_paths` — user_data も **プロジェクト単位** (`user_data/<project>/<subdir>/`) で走査する。使い手: ツール (`tools/__init__.py:418`)、phenomena (`phenomena/__init__.py:133`)、Playbook (`saiverse/playbook_sync.py:73`)
  3. `get_data_paths` / `get_all_data_paths` — expansion は **`expansion_data/<subdir>/`** (アドオンの下ではなく直下)。使い手: `saiverse/model_configs.py:634` (`find_model_config` の第 3 段)、`manager/admin.py:1709`
  - `find_file` はさらに別で、user_data は直下、expansion はプロジェクト単位を見る。根拠: `saiverse/data_paths.py:96-127`
  - MCP 設定はこれらを使わず、独自に 4 段を組む (OPS-24)。根拠: `tools/mcp_config.py:293-317`
  - `expansion_data/` の実体は 7 アドオン。`models/` や `providers/` を置いているアドオンは無い (`ls` で確認) ので、上の食い違いは現時点で表に出ていない可能性が高い。
- **追跡できていない境界**: 3 つの形のどれが「正しい 3 層」なのかを決める文書が見つからない。`CLAUDE.md` は一つの規則として書いているが、実装は上記のとおり分かれている。
- **既存テスト**: `tests/test_provider_configs.py::test_user_data_overrides_builtin` (プロバイダの上書き)、`tests/test_mcp_config.py::test_load_mcp_configs_respects_priority_and_env` (MCP の 4 層)。**`data_paths.py` のヘルパー自体を横断的に検査するテストは見つからなかった** — 3 つの形の食い違いを検出する仕掛けは無い。
- **状態**: `機能の存在=✓` `期待の根拠=✓ (ただし文書は一つの規則としてしか書いていない)` `挙動の静的確認=✓` `テスト対応=△`

---

### OPS-27: Discord ゲートウェイ

- **入口**: `.env` に `SAIVERSE_GATEWAY_ENABLED=1` と `SAIVERSE_GATEWAY_WS_URL` / `SAIVERSE_GATEWAY_TOKEN` を書いて起動する。UI からの入口は無い。根拠: `discord_gateway/integration.py:44-46,79-99`, `main.py:482-483`, `discord_gateway/config.py:11-20`
- **結果**: 背景スレッドで独立したイベントループが走り、別途動かす relay bot (`discord_gateway/bot/`) と WebSocket で繋がる。Discord のチャンネルと SAIVerse の建物をマッピングして双方向に中継する。根拠: `discord_gateway/runtime.py:11-56`, `discord_gateway/orchestrator.py`, `discord_gateway/mapping.py`
- **期待の根拠**: `既存の仕様文書` (`discord_gateway/docs/discord_gateway_manual.md`, `implementation_discord.md`, `relay_server_design.md`) + `docs/overview/roadmap_status.md` §7 は Discord を 🔲 (未着手)「見守り機能の軽量アドオン化構想」と書いている
- **現在の挙動 (静的確認)**:
  - 既定は無効 (`SAIVERSE_GATEWAY_ENABLED` の既定 `"0"`)。根拠: `discord_gateway/integration.py:45`
  - 設定が足りない状態で有効化すると `GatewaySettings` の生成時に例外になる (`bot_ws_url` / `handshake_token` は必須)。根拠: `discord_gateway/config.py:13-14`
  - `ws://` または `wss://` 以外は拒否する。根拠: `discord_gateway/config.py:40-44`
  - relay bot は SAIVerse 本体とは別のプロセス / 別のホストに置く前提で、本体の setup / start スクリプトはこれを一切起動しない。
- **追跡できていない境界**: `discord_gateway/bot/` (13 ファイル) の実装と、そこに必要な Discord bot トークンの用意手順。利用者向けの導入手順が本体の README / docs にあるかは確認したが見つからなかった (gateway の docs は `discord_gateway/docs/` にしかない)。
- **既存テスト**: `discord_gateway/tests/` に 15 ファイル。**これは唯一 CI で自動実行されているテスト群** (`.github/workflows/discord_gateway.yml`)。ただし `discord_gateway/**` 配下が変わったときだけ走る。
- **状態**: `機能の存在=✓ (開発者向け・オプトイン)` `期待の根拠=△ (gateway 内部に設計書はあるが、roadmap は未着手と書いていて食い違う — §3)` `挙動の静的確認=✓ (入口のみ)` `テスト対応=✓ (CI で回る唯一の群)`

---

### OPS-28: Unity ゲートウェイ

- **入口**: **自動 — 既定で有効**。`UNITY_GATEWAY_ENABLED` の既定値が `"true"` で、`websockets` パッケージが入っていれば起動する。根拠: `main.py:500-511`
- **結果**: WebSocket サーバーが **`ws://0.0.0.0:8765`** で待ち受ける。接続してきたクライアントは handshake を送るだけで登録され、応答として **全ペルソナの id と表示名の一覧を受け取る**。根拠: `unity_gateway/server.py:85-98,131-172`
- **期待の根拠**: `既存の仕様文書` — `docs/features/unity-gateway.md:5` が「これは設計書 (feature の完成状態の説明ではない)。Phase 1 の一部は実装済みで main.py に統合されている」と自己申告している。`docs/overview/roadmap_status.md` に Unity の行は無い。
- **現在の挙動 (静的確認)**:
  - bind 先はハードコードの `0.0.0.0`。`main.py` 側は `port` しか渡さない。根拠: `unity_gateway/server.py:85`, `main.py:508`
  - handshake に認証は無い。`client_id` と `user_id` はクライアントの自己申告をそのまま受け入れる。根拠: `unity_gateway/server.py:131-148`
  - handshake ack で全ペルソナの `id` / `name` を返す。根拠: `unity_gateway/server.py:160-172`
  - **`user_speak` の処理は現行の manager と噛み合っていない**: `self.manager.get_user(client.user_id)` を呼ぶが、`SAIVerseManager` に `get_user` は存在しない (あるのは `get_user_profile`)。さらに `handle_user_input` を `user_id=` / `building_id=` / `target_persona_id=` / `source=` のキーワードで await しているが、実際の署名は `handle_user_input(message, metadata=None)` の同期関数。根拠: `unity_gateway/server.py:200-232` vs `saiverse/saiverse_manager.py:1279-1280`, `manager/runtime.py:560-561`。呼び出しは `except Exception` に飲まれて `logger.error` だけが残る (`unity_gateway/server.py:231-232`)。
  - `UNITY_GATEWAY_ENABLED` / `UNITY_GATEWAY_PORT` は `.env.example` にも `docs/reference/environment-vars.md` にも載っていない。
  - `unity_client/` は `.gitignore:131` で除外されており、配布物 (git archive) には含まれない。
  - ペルソナ側からの経路は生きている: `builtin_data/tools/control_body.py:44-107` が `manager.unity_gateway` を見て emote / behavior を送る。
- **追跡できていない境界**: `websockets` が本体の依存に入っているかどうか (入っていれば既定でポートが開く)。`requirements.lock` を全部は読んでいない。
- **既存テスト**: **該当テストなし** (`tests/` に unity を含むファイルは無い)。
- **状態**: `機能の存在=△ (受け口は生きているが user_speak は壊れている)` `期待の根拠=△ (設計書のみ・完成状態の説明ではないと自己申告)` `挙動の静的確認=✓` `テスト対応=✗`

---

### OPS-29: SDS 登録と inter-city (凍結)

- **入口**: City の設定 `START_IN_ONLINE_MODE` が真のとき、起動時に自動で SDS へ登録する。この値は **ワールドエディタの City 編集画面から切り替えられる**。根拠: `saiverse/saiverse_manager.py:396-412`, `frontend/src/components/settings/WorldEditor.tsx:26,348`, `manager/admin.py:169`
- **結果**: `SDS_URL` (既定 `http://127.0.0.1:8080`) へ登録リクエストを投げ、以後 30 秒間隔 (失敗時は指数バックオフで最大 300 秒) で heartbeat を送り続ける。根拠: `manager/sds.py:64-100`, `main.py:305-306`
- **期待の根拠**: `既存の仕様文書` (`docs/overview/roadmap_status.md` §8 「SDS / multi-city は 2026-07-16 まはー裁定で凍結・入口封鎖済み」)
- **現在の挙動 (静的確認)**:
  - 凍結されているのは **inter-city の DB polling (VisitingAI / ThinkingRequest) と HTTP 入口 (503) だけ**。SDS への登録と heartbeat のスケジュール登録は凍結対象に入っていない。根拠: `saiverse/saiverse_manager.py:455-469` (polling の凍結コメント) と `:396-409` (SDS 登録は無条件に実行) の対比
  - `database/api_server.py` の `/inter-city/*` と `/persona-proxy/{id}/think` は最初の 1 行で 503 を投げる。凍結前の実装は到達しないコードとして残されている。根拠: `database/api_server.py:89-176`
  - `sds_server.py` は単独起動するスタンドアロンの FastAPI (`python sds_server.py` で 127.0.0.1:8080)。setup / start スクリプトからは一切起動されない。根拠: `sds_server.py:75-78`
- **追跡できていない境界**: `START_IN_ONLINE_MODE` を真にしたとき、SDS が動いていない環境で heartbeat の失敗ログがどれくらい出るか (バックオフで最大 300 秒間隔) は実行していない。
- **既存テスト**: `tests/test_multi_city_freeze.py` — inter-city / persona-proxy API が 503 + 凍結メッセージを返すこと、polling が登録されないことを検査する。**SDS 登録・heartbeat が凍結対象外であることは、意図なのか漏れなのかを示す検査も文書も見つからなかった**。
- **状態**: `機能の存在=△ (凍結だが SDS 側だけ生きている)` `期待の根拠=✓ (凍結の裁定は明文化されている)` `挙動の静的確認=✓` `テスト対応=△ (封鎖側のみ)`

---

### OPS-30: LAN 公開と持ち主認証

- **入口**: `python main.py <city> --listen-host <非ループバック>` または `SAIVERSE_API_HOST` の設定。根拠: `main.py:307-329`
- **結果**: 非ループバックを指定すると `SAIVERSE_OWNER_TOKEN` と `SAIVERSE_ALLOWED_ORIGINS` の両方が必須になり、`OwnerAuthMiddleware` が全 API に差し込まれる。根拠: `main.py:317-328,674-677`, `api/owner_auth.py:54-84`
- **期待の根拠**: `利用者向け説明` (`README.md:255-273` 「スマホで使いたいんだが？」、`docs/getting-started/tailscale-runbook.md`)
- **現在の挙動 (静的確認)**:
  - 認証は Bearer トークンか、HMAC 由来の session cookie。状態を変えるメソッド (GET/HEAD/OPTIONS 以外) では、cookie 認証のときだけ Origin の一致も要求する。根拠: `api/owner_auth.py:69-83`
  - 例外パスは `/api/auth/login` と `/api/oauth/callback/`。根拠: `api/owner_auth.py:58-61`
  - **README とランブックが案内する遠隔アクセス手順は、この門を通らない**: 利用者に案内されるのはフロントエンド (`:3000`) で、Next.js が `/api/*` を `http://127.0.0.1:8000` へ rewrite する。バックエンドはループバックのままなので `lan_mode` は偽になり、`OwnerAuthMiddleware` は差し込まれない。根拠: `README.md:255-273`, `frontend/next.config.ts:22-40`, `main.py:674-677`。ランブックには `--listen-host` / `SAIVERSE_OWNER_TOKEN` / `SAIVERSE_ALLOWED_ORIGINS` の記載が一切無い (grep で 0 件)。
  - `next dev` は明示的に `-H 0.0.0.0` で待ち受ける。根拠: `frontend/package.json:7`
  - CORS の許可 origin は `localhost:3000` / `127.0.0.1:3000` + `SAIVERSE_ALLOWED_ORIGINS`。根拠: `main.py:651-671`
  - ループバック起動 (= 既定、かつ上記の遠隔アクセス手順) では、`/api/db/tables` の DELETE (OPS-09) も `/api/mcp/tool-call` (OPS-24) も `/api/admin/env` (OPS-10) も無認証で通る。
- **追跡できていない境界**: Tailscale が張る仮想 NIC のアドレスに対して Next.js の rewrite 先 `127.0.0.1:8000` が届くこと自体は自明だが、Tailscale の ACL がどこまで守るかは環境依存で、この調査では判断できない。
- **既存テスト**: **該当テストなし** (`OwnerAuthMiddleware` / `owner_auth` を検査するテストは見つからなかった)。関連する既知課題は `docs/issues/api_state_changing_routes_have_no_origin_check.md` (未着手)。
- **状態**: `機能の存在=✓` `期待の根拠=✓ (README とランブック)` `挙動の静的確認=✓` `テスト対応=✗`

---

### OPS-31: リリース配布と CI

- **入口**: `v*` タグを push すると GitHub Actions が走る。根拠: `.github/workflows/release.yml:3-6`
- **結果**: `git archive --format=zip --prefix=SAIVerse/ HEAD` で作った `SAIVerse.zip` を、自動生成ノート付きの GitHub Release に添付する。README の「最新版をダウンロード (ZIP)」リンクがこれを指す。根拠: `.github/workflows/release.yml:14-25`, `README.md:154,222`
- **期待の根拠**: `既存の仕様文書` (`docs/overview/release_history.md` 冒頭「発行の手続きの型」)
- **現在の挙動 (静的確認)**:
  - **ワークフローは 2 本しか無い**: `release.yml` と `discord_gateway.yml`。根拠: `find .github -type f`
  - `release.yml` にテスト工程は無い。`checkout` → `git archive` → `gh release create` の 3 ステップだけ。根拠: `.github/workflows/release.yml:12-25`
  - `discord_gateway.yml` は `discord_gateway/**` / `requirements.txt` / `requirements.lock` / `discord_gateway/pyproject.toml` が変わった push と PR でのみ走り、`ruff check discord_gateway` と `pytest discord_gateway/tests` を実行する。**本体 (`tests/` の 5000 本超) を回す工程はどこにも無い**。根拠: `.github/workflows/discord_gateway.yml:3-40`
  - `.gitattributes` に `export-ignore` は無いので、ZIP には `tests/` `docs/` `.github/` も含まれる。根拠: `cat .gitattributes` (export-ignore 0 件)
  - ローカルの git hook は Git LFS の 4 本だけで、テストを走らせるものは無い。`.pre-commit-config.yaml` も無い。根拠: `ls .git/hooks/`
  - `requirements.lock` の全プラットフォーム検査 (`scripts/check_lock_platforms.py`) は **手動で回す前提** と文書に明記されている。CI からの呼び出しは無い。根拠: `docs/developer-guide/contributing.md:73`, `docs/intent/dependency_management.md:144`
  - `VERSION` は `0.3.10`。`CHANGELOG.md` の最上部は `## [Unreleased] / v0.3.0 (development)` のままで、v0.3.1〜v0.3.10 の記載が無い (§3)。
- **追跡できていない境界**: `git archive` が `.gitattributes` の `eol=crlf` / `eol=lf` をどう適用するか (OPS-01 の ZIP → git init 経路の成否に効く)。実行していないので不明。
- **既存テスト**: `tests/test_requirements_lock_contract.py` が lock の契約を検査する (ローカル実行のみ)。**ワークフロー自体を検査するものは無い**。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗ (本体を回す CI が存在しない)`

---

### OPS-32: SearXNG の導入と起動

- **入口**: セットアップの第 11 段で自動導入。起動は `start.bat` が自動 / `start.sh` は `SAIVERSE_SEARXNG=1` のときだけ。根拠: `setup.bat:283-290`, `setup.sh:148-155`, `start.bat:60-66`, `start.sh:56-60`
- **結果**: `scripts/.searxng-venv/` と `scripts/.searxng-src/` が作られ、`scripts/searxng_settings.yml` が生成される。以後、検索ツールがこのローカルサーバーを使う。根拠: `scripts/setup_searxng.ps1`, `scripts/setup_searxng.sh`, `scripts/run_searxng_server.{ps1,sh}`
- **期待の根拠**: `利用者向け説明` (`README.md` の主な機能節) + `既存の仕様文書` (`docs/overview/roadmap_status.md` §7 の SearXNG 行)
- **現在の挙動 (静的確認)**:
  - 導入に失敗しても警告のみで、セットアップは続く。根拠: `setup.bat:286-290`, `setup.sh:150-155`
  - 設定は 3 層マージ (SearXNG 本体 → `searxng_engine_defaults.yml` → `searxng_user_engines.yml`)。根拠: `scripts/merge_searxng_settings.py`, `docs/overview/roadmap_status.md` §7
  - roadmap §7 が「**⚠️ 次回バージョンアップ時に既存ユーザーの settings.yml リセット検証が必要**」という未検証の宿題を自己申告している。作業ツリーには `scripts/searxng_settings.yml` (2026-09-09 更新) と `.bak` (2026-06-03) が両方ある。
- **追跡できていない境界**: 更新経路 (OPS-03) が `scripts/searxng_settings.yml` をどう扱うか。このファイルは gitignore されているか追跡対象かを確かめていない。
- **既存テスト**: **該当テストなし**。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=△` `テスト対応=✗`

---

### OPS-33: 埋め込みモデルの事前ダウンロード

- **入口**: セットアップの第 12 段。根拠: `setup.bat:293-300`, `setup.sh:157-163`
- **結果**: `sbert/multilingual-e5-small/` に ONNX 版の重みとトークナイザが置かれる。失敗しても警告のみで、初回起動時に再試行される。根拠: `setup.bat:296-300`
- **期待の根拠**: `既存の仕様文書` (`docs/getting-started/gpu-setup.md`, `docs/reference/environment-vars.md` の `SAIMEMORY_EMBED_MODEL`)
- **現在の挙動 (静的確認)**: `ignore_patterns` が `*.bin` / `*.safetensors` / `*.h5` / `openvino/*` / `*.ot` を除外する。読み込み側は `onnx/model.onnx` 等を探し、ONNX が無ければ別のディレクトリへフォールバックする。**除外は意図どおりで、重みが落ちないバグではない**。根拠: `setup.bat:295`, `sai_memory/config.py:22-28,99-140`
- **追跡できていない境界**: `SAIMEMORY_EMBED_MODEL` を別モデルに変えた利用者が、そのモデルの ONNX をどうやって用意するのか。
- **既存テスト**: **該当テストなし** (setup の該当段を検査するテストは無い)。`sai_memory/config.py` のフォールバック順を検査するテストは探していない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=✗`

---

### OPS-34: 二重起動の防止 (runtime marker) と CITY_SLUG の自動修復

- **入口**: 自動 — `python main.py <city>` の最初期。根拠: `main.py:340-352`
- **結果**: City 名ごとのマーカーファイルを取得する。既に生きたプロセスが同じ City を持っていれば起動を断る。DB に指定 City が無い場合、条件を満たせば `CITYID=1` の `CITY_SLUG` を書き換えて救済する。根拠: `saiverse/runtime_marker.py:185-`, `manager/initialization.py:54-115`
- **期待の根拠**: `既存の仕様文書` (`CLAUDE.md` 「Running, testing, linting」節の slug 自動修復の説明、`docs/intent/city_identity.md` §4 不変条件 2 / §6)
- **現在の挙動 (静的確認)**:
  - 自動修復は 2 つの関所を通る: (a) 同じ DB を所有する稼働中プロセスがいないこと、(b) City が 1 行だけの DB であること。両方ともコメントに理由が書かれている。根拠: `manager/initialization.py:74-110`
  - マーカーは pid の生存だけでなく `create_time` まで照合して pid 再利用を弾く。判定できない場合は fail-closed 側 (`unknown`) に倒す。根拠: `saiverse/runtime_marker.py:66-127`
- **追跡できていない境界**: `acquire_runtime_marker` の中身 (185 行以降) は読んでいない。
- **既存テスト**: `tests/test_city_identity.py` (自動修復の条件を実 SQLite に対して検査)。マーカーの pid 判定を検査するテストは探していない。
- **状態**: `機能の存在=✓` `期待の根拠=✓` `挙動の静的確認=✓` `テスト対応=△`

---

### OPS-35: inter-city API サーバー子プロセスの起動とポートの強制解放

- **入口**: 自動 — 毎回の起動で必ず走る。根拠: `main.py:542-548`
- **結果**: `database/api_server.py` を `manager.api_port` (city_a の既定は 8001) で子プロセスとして起動する。**起動前に、そのポートを使っているプロセスを探して `taskkill /F` (Windows) / `SIGTERM` → `SIGKILL` (POSIX) で殺す**。根拠: `main.py:151-178`, `main.py:205-220`
- **期待の根拠**: `実装のみ (根拠なし)` — この子プロセスとポート強制解放を説明する文書は見つからなかった。
- **現在の挙動 (静的確認)**:
  - この子プロセスが提供する 3 ルートはすべて 503 で封鎖済み (OPS-29)。つまり **凍結された機能のために、毎回ポートを占有し、そこにいた無関係のプロセスを殺す**。根拠: `database/api_server.py:89-176` と `main.py:542-548` の対比
  - 殺す対象は `netstat` / `ss` 系コマンドの出力から拾った PID で、それが SAIVerse のものかどうかの確認は無い。根拠: `main.py:120-151`
  - 終了時は `shutdown_subprocess` で terminate → kill。根拠: `main.py:606`
- **追跡できていない境界**: `find_pid_for_port` が実際にどのコマンドを使うか (`main.py:120-140` の冒頭を読み切っていない)。
- **既存テスト**: **該当テストなし**。
- **状態**: `機能の存在=✓` `期待の根拠=✗` `挙動の静的確認=✓` `テスト対応=✗`

---


### 矛盾・疑義

すべて「どちらが正しいかは断定せず、両方を並べる」形で書く。

1. **CHANGELOG と VERSION** — `VERSION` は `0.3.10`。`CHANGELOG.md:5` の最上部見出しは `## [Unreleased] / v0.3.0 (development)` のままで、v0.3.1〜v0.3.10 の記載が無い。一方 `docs/overview/release_history.md` には v0.3.4 / v0.3.8 / v0.3.11 の記録がある。リリース履歴の正本がどちらかは文書上決まっていない (`release_history.md` は自らを「発行済みリリースの記録と次の版に入れる範囲の正典」と名乗る)。

2. **グローバル設定のタブ一覧** — `docs/user-guide/global-settings.md` の表は「**データベース管理**」タブを載せ、「フィード」タブを載せていない。実装 (`frontend/src/components/GlobalSettingsModal.tsx:51,724-772`) のタブは `環境 / ワールドエディタ / フィード / モデルロール / モデル管理 / Playbook権限 / 情報 / 便利機能` の 8 個で、「データベース管理」は無い。

3. **更新の三点セット** — `CLAUDE.md` 「Setup/Update Script Parity」は「`update.bat` / `update.sh` / `scripts/self_update.py` が同期していなければならない」と書く。実装ではこの 3 つ (＋`scripts/update_from_github.ps1`) すべてが `scripts/update_engine.py` を呼ぶだけの薄い入口になっており、`scripts/self_update.py` は 8 行の互換 shim (`scripts/self_update.py:1-9`)。同期すべき実体はもう無い。`docs/reference/scripts.md` は `self_update.py` を「互換 wrapper」と正しく書いている。

4. **アドオン基盤の進捗** — `docs/overview/roadmap_status.md:68` は「🔵 **Phase 2**: registry + API（着手前）」と書く。実装には `saiverse/addon_registry.py` (Ed25519 署名検証つき)、`api/routes/addon_catalog.py` (registry / install / update / uninstall)、`AddonCatalogPanel.tsx` が揃っている。`docs/intent/addon_catalog_management.md` のステータスは「Phase 4 完了 (2026-05-23)」。roadmap と intent が食い違っている。

5. **Discord の位置づけ** — `docs/overview/roadmap_status.md:113` は Discord を「🔲 見守り機能の軽量アドオン化構想」と書く。実装には `discord_gateway/` 一式 (21 ファイル + bot 13 ファイル + テスト 15 ファイル) があり、`main.py:482-483` から起動され、CI も存在する。構想なのか実装済みなのかが読めない。

6. **Unity ゲートウェイの提供状態** — `docs/features/unity-gateway.md:5` は自らを「設計書 (feature の完成状態の説明ではない)」と断り、Phase 1 の一部が実装済みと書く。実装は既定 ON で `0.0.0.0:8765` を開く (`main.py:501`, `unity_gateway/server.py:85`)。roadmap には Unity の行が無い。「開発者専用」とも「凍結」とも書かれていないのに、既定で有効なポートが開く。

7. **`user_speak` の署名不一致 (Unity)** — `unity_gateway/server.py:215,223-230` は `manager.get_user(...)` と `handle_user_input(user_id=..., message=..., building_id=..., target_persona_id=..., source=...)` を await する。実際には `get_user` は存在せず (`saiverse/saiverse_manager.py` / `manager/admin.py` には `get_user_profile` のみ)、`handle_user_input` は `(message, metadata=None)` の同期関数 (`saiverse/saiverse_manager.py:1279`)。呼び出しは `except Exception` に飲まれる。実装が現行 API から取り残されている。

8. **CLAUDE.md の LLM 記述** — `CLAUDE.md` 「LLM integration」は「`get_llm_client(model_name, config)`」「Ollama は届かなければ Gemini 2.0 Flash にフォールバック」「`llm_router.py` は Gemini 2.0 Flash でツール呼び出しを決める」と書く。実装の署名は `get_llm_client(model, provider, context_length, config=None)` (`llm_clients/factory.py:160`)、Ollama のプローブ失敗は warning のログのみでフォールバック先は無い (`llm_clients/ollama.py:170-214`)、router のモデルは `BUILTIN_DEFAULT_LITE_MODEL` = `gemini-3.1-flash-lite-preview` (`saiverse/llm_router.py:16-18`, `saiverse/model_defaults.py:11`)。

9. **3 層解決の形が 3 つある** — `CLAUDE.md` は「resource loading is 3-layer — `~/.saiverse/user_data/` > `expansion_data/` > `builtin_data/`」と一つの規則として書く。実装では expansion 層の位置が `expansion_data/<addon>/<subdir>/` (`iter_files_with_layer`) と `expansion_data/<subdir>/` (`get_data_paths`) に分かれ、user_data 層も直下 (`iter_files_with_layer`) とプロジェクト単位 (`iter_project_subdirs`) に分かれる。MCP はさらに別の 4 段 (`tools/mcp_config.py:293-317`)。詳細は OPS-26。

10. **遠隔アクセスの手順と LAN 公開の門** — `main.py:317-328` は非ループバック公開に `SAIVERSE_OWNER_TOKEN` と `SAIVERSE_ALLOWED_ORIGINS` を必須にする。一方 `README.md:255-273` と `docs/getting-started/tailscale-runbook.md` が案内する遠隔アクセスは、フロントエンド (`:3000`) 経由で `/api` を `127.0.0.1:8000` に rewrite する形 (`frontend/next.config.ts:22-40`) なので、この門を一度も通らない。ランブックには 3 つの変数への言及が 0 件。どちらが意図された運用かは文書上で決着していない。

11. **`docs/reference/providers.md` と `tests/test_provider_configs.py`** — 文書は builtin を 12 件と書き、実体も 12 件。テストは「seven_builtin_providers_loaded」という名前で 7 件の存在だけを確認する。数え上げが文書と一致していない (テストが 5 件を数えていない)。

12. **`tests/test_requirements_lock_contract.py:117` のコメント** — 「§2-2 の**七つ**の経路」と書いているが、直後の `_INSTALL_PATHS` は 6 要素。

13. **`database/backup.py` の復元手順が利用者向け文書に無い** — `README.md:35` は「自動バックアップ機能を搭載しており、起動するたびに会話データ等がコピー・保存されます」と約束し、`README.md:404` は「定期的なバックアップを推奨します」と書く。しかし復元の入口 (`python database/backup.py --db <path> restore <backup>` と `snapshot.bat restore <name>`) は `docs/reference/scripts.md` と `docs/intent/version_aware_world_and_persona.md` にしか無く、README にも `docs/user-guide/` にも `docs/getting-started/` にも記載が無い (grep で 0 件)。バックアップされることは約束されているのに、戻し方が利用者に届いていない。

14. **`start_dev.sh` / `start_dev.ps1` が旧世代のまま** — `start_dev.sh:14-24` は「FastAPI + Gradio」「port 7860」「`python3 main.py`」(City 引数なし)。`start_dev.ps1:14` は `conda activate SAIVerse`。現行は venv + FastAPI のみで Gradio は無く、`start-dev.sh` / `start-dev.bat` が現役。ハイフン版とアンダースコア版が両方リポジトリに残っており、どちらが正かの記載が無い。

15. **SearXNG 起動条件のプラットフォーム差** — `start.bat:60-66` は導入済みなら必ず起動。`start.sh:56-60` は `SAIVERSE_SEARXNG=1` のときだけ。どちらが意図かの記載が無い。同様に `start.bat` は City を `city_a` に決め打ちし (`start.bat:70`)、`start.sh` は引数で受ける (`start.sh:12`)。

16. **凍結された inter-city のために毎回ポートを奪う** — `database/api_server.py` の全ルートは 503 で封鎖済み (`:96,107,131`) だが、`main.py:542-548` はこの子プロセスを毎回起動し、その前に `manager.api_port` を使っている **任意のプロセスを強制終了する** (`main.py:151-178`)。凍結の裁定 (`docs/overview/roadmap_status.md` §8) は入口の封鎖までで、子プロセスの起動停止には触れていない。

17. **SDS 登録が凍結対象に入っていない** — 同 §8 は「SDS / multi-city は凍結・入口封鎖済み」と書く。`saiverse/saiverse_manager.py:455-469` が凍結したのは DB polling だけで、`:396-409` の SDS 登録 + heartbeat は無条件に走る。しかも `START_IN_ONLINE_MODE` はワールドエディタから切り替えられる (`frontend/src/components/settings/WorldEditor.tsx:348`)。凍結された機能のスイッチが UI に残っている。

18. **モデル JSON の保存だけ atomic でない** — `saiverse/provider_configs.py:169-186` は「壊れた JSON はプロバイダを一覧から消すので一時ファイル + `os.replace` にする」と理由つきで書かれている。同じ失敗形が当てはまるモデル JSON (`api/routes/config.py:1537,1567,1626,1712 付近`) と `.env` (`api/routes/admin.py:100-104`) は直接上書きのまま。関連 issue は `docs/issues/malformed_provider_json_breaks_provider_list.md` (プロバイダ側のみ)。

---

### 凍結・開発者専用・到達不能・文書のみ

| 対象 | 状態 | 根拠 |
|---|---|---|
| `/inter-city/request-move-in` / `/inter-city/buildings` / `/persona-proxy/{id}/think` | **凍結・封鎖済み**。全ルートが最初の行で 503。凍結前の実装は到達しないコードとして残置 | `database/api_server.py:89-176` |
| VisitingAI / ThinkingRequest の DB polling | **凍結**。EventScheduler へ登録しない。関数本体は残る | `saiverse/saiverse_manager.py:455-469` |
| `RemotePersonaProxy` | **凍結**。`CLAUDE.md` にも記載 | `saiverse/remote_persona_proxy.py` |
| SDS 登録 + heartbeat | **凍結対象外で今も動く** (§3-17)。`START_IN_ONLINE_MODE` が真のときのみ | `saiverse/saiverse_manager.py:396-409` |
| `sds_server.py` | **開発者専用のスタンドアロン**。setup / start からは起動されない | `sds_server.py:75-78` |
| `database/api_server.py` 子プロセス | **凍結機能のために毎回起動される** (§3-16) | `main.py:542-548` |
| Unity ゲートウェイ | **既定 ON だが `user_speak` は現行 API と噛み合わず到達不能** (OPS-28)。emote / behavior の送出側は生きている | `unity_gateway/server.py:200-232`, `builtin_data/tools/control_body.py:44-107` |
| `unity_client/SAIVerse3D/` | **gitignore 済み**。配布物に含まれない | `.gitignore:131` |
| Discord ゲートウェイ | **開発者向けオプトイン**。`SAIVERSE_GATEWAY_ENABLED=1` + 別途 relay bot が要る。UI 入口なし | `discord_gateway/integration.py:44-46` |
| `start_dev.sh` / `start_dev.ps1` | **旧世代の遺物**。Gradio / port 7860 / conda 前提 | `start_dev.sh:14-24`, `start_dev.ps1:14` |
| `POST /api/db/tables/{table}` / `DELETE /api/db/tables/{table}` | **UI からの呼び出し元なし**。API 直叩きのみ到達可能 | `frontend/src/**` の grep 結果 (GET のみ) |
| `POST /api/mcp/tool-call` | **管理・デバッグ専用**。docstring に「ペルソナのスペルパイプラインを迂回する」と明記 | `api/routes/mcp.py:163-190` |
| `scripts/test_addon_catalog_api.py` | **手動 E2E スクリプト**。起動中の SAIVerse に対して叩く。自動テストではない | `scripts/test_addon_catalog_api.py:1-20` |
| `docs/features/unity-gateway.md` | **設計書であって完成状態の説明ではない** と自己申告。コード例は旧ツール API | `docs/features/unity-gateway.md:5` |
| `docs/intent/addon_extension_points.md` | **ドラフト (まはーレビュー待ち)** のまま、OAuth 実装が先行 | 同 doc ステータス行 |
| `LEGACY_MODELS_DIR` (`models/` 直下) へのフォールバック | **後方互換の遺物**。3 層から 1 件も読めなかったときだけ使う | `saiverse/model_configs.py:130-148` |
| `Tool` + `BuildingToolLink` テーブル | **未使用** (`CLAUDE.md` に記載)。`seed.py` は今も `Tool` を import する | `database/seed.py:17` |
| `scripts/check_lock_platforms.py` | **手動実行のみ**。CI からの呼び出しは無い | `docs/developer-guide/contributing.md:73` |

---

### この領域で「検査が無い」と判断した重要な結果

利用者に見える結果のうち、既存テストがまったく触れていないもの。

1. **本体のテストを回す CI が存在しない。** ワークフローは 2 本だけで、`release.yml` はタグ push で ZIP を作るだけ (テスト工程なし)、`discord_gateway.yml` は `discord_gateway/**` が変わったときだけ `discord_gateway/tests` を回す。`tests/` 配下の 5000 本超は **PR でも push でもタグでも一度も自動実行されない**。`.pre-commit-config.yaml` も無く、ローカルの git hook は Git LFS の 4 本だけ。つまり「リリース ZIP に入るコードが緑だったか」を機械が保証する場所がどこにも無い。根拠: `.github/workflows/` の全 2 ファイル、`ls .git/hooks/`

2. **`setup.bat` / `setup.sh` を実行するテストが無い。** 検査されているのは「本文に `-r requirements.txt` が無いこと」と「`requirements.lock` の語があること」の文字列検査 2 本だけ (`tests/test_requirements_lock_contract.py:132-149`)。DB 初期化の分岐 (破損 DB → `seed.py --force`)、Node/Git の自動導入、`git init` + `git reset origin/main`、`.venv` 破損時の扱いは、どれも未検査。両プラットフォームの手順が同じ意味を持つことを確かめる仕掛けも無い。

3. **`database/seed.py` に一切テストが無い。** リポジトリで最も破壊的なコマンドで、しかもセットアップから `--force` 付きで自動実行されうる (`setup.bat:189`)。確認プロンプト、`.bak` バックアップ、消える範囲 (persona の `memory.db` は残る) のどれも検査されていない。

4. **`/api/db/tables` の POST / DELETE に検査が無い。** `tests/test_db_manager_api.py` は GET のページ送りだけ (9 本)。任意テーブルへの upsert と主キー削除は、ループバック起動では無認証で通り、UI からの呼び出し元も無く、テストも無い。

5. **`.env` の書き換えに検査が無い。** `write_env_updates` は API キーを含む `.env` を `open(..., "w")` で truncate してから書き直す (`api/routes/admin.py:100-104`)。同じ失敗形に対してプロバイダ設定は一時ファイル + `os.replace` を採用しているのに (`saiverse/provider_configs.py:169-186`)、`.env` とモデル JSON はそのまま。テストは 0 本。

6. **アドオンの導入・更新・削除の本体に検査が無い。** 検査されているのは registry の署名 (3 本) と pip の constraints (3 本) だけ。`install_addon` / `update_addon` / `uninstall_addon` を通しで実行するテストは無く、特に **`delete_data=true` で `~/.saiverse/user_data/addon_data/<id>/` を消す経路** (OAuth トークンと参照音声が入る) は一度も検査されていない。代替は起動中サーバーに対する手動スクリプト (`scripts/test_addon_catalog_api.py`)。

7. **持ち主認証 (`OwnerAuthMiddleware`) に検査が無い。** LAN 公開時の唯一の防壁で、Bearer / cookie / Origin の 3 経路と 2 つの例外パスを持つが、テストは 0 本。加えて README とランブックが案内する遠隔アクセス手順はこの防壁を通らない (§3-10)。

8. **壊れた建物ログの復元・リセットに検査が無い。** `POST /api/system/quarantine/{id}/restore` は選んだバックアップで `log.json` を置き換え、ペルソナの読み位置を切り詰める。`.../reset` は履歴を空にして採番を 1 に戻す。どちらも利用者の記憶に直接効くのに、テストが無い。

9. **開発者モード OFF の一括副作用に検査が無い。** 全ペルソナの `AUTONOMY_ENABLED` を DB ごと False にし、ON に戻しても復元しない (`api/routes/config.py:518-546`)。issue (`developer_mode_off_mass_disables_autonomy.md`) として未解決のまま、機械的な歯止めも検査も無い。

10. **Unity ゲートウェイに検査が無い。** 既定 ON で `0.0.0.0:8765` を開き、無認証の handshake に対して全ペルソナの id と表示名を返す。テストは 0 本で、`user_speak` の署名不一致 (§3-7) も検査で捕まらなかった。

11. **3 層リソース解決の一貫性に検査が無い。** ヘルパーが 3 つの異なる形を持ち (OPS-26)、どれが正しいかを決める文書も、食い違いを検出する検査も無い。プロバイダの上書き 1 本 (`test_user_data_overrides_builtin`) と MCP の 4 層 1 本 (`test_load_mcp_configs_respects_priority_and_env`) が個別に存在するだけ。

12. **起動時の移行シーケンスの順序に検査が無い。** `main.py:358-445` の 15 本超の backfill / ensure には順序依存があり (`ensure_task_book_table` → `migrate_deadline_tasks_to_task_book` はコメントで ⚠ 付きの明示)、その依存を守らせる機械検査は無い。

13. **プロバイダ設定のテストが実環境を読む。** `tests/test_provider_configs.py::TestLoadProviders` は `USER_DATA_DIR` を隔離せずに `load_configs()` を呼ぶ。`conftest.py` にも `SAIVERSE_HOME` の隔離は無い (アドオンテストの opt-in 設定だけ)。開発者の手元に builtin の上書きがあると結果が変わる。

### 事故二件との関係 (この領域から見た説明)

依頼の 2 件はどちらも記憶・Chronicle 側の欠陥だが、**この領域の観点から言えば「入口の一覧と、その入口が触る永続化先の対応表」が台帳として存在しないこと**が共通の土台になっている。

- 領域 F では、利用者が押せる入口 (画面のボタン / API / スクリプト) に対して「何が永続化されるか」「戻せるか」「検査があるか」を並べた表がどこにも無い。この調査で初めて §1 の形に並べた。事故 1 (編纂で レベル2 が 1 つしか出ない) は「大量を投入したときに出力の**個数**が想定と違う」という結果の検査で、事故 2 (スルースに全量を読ませる) は「入口が下流へ渡す**量**が想定と違う」という接点の検査で捕まる型。どちらも §1 の「結果」欄と §2 の「受け渡す情報・状態」欄に対応する。
- 領域 F で同じ型が潜んでいる場所を挙げると: (a) 更新前スナップショットの制限時間 (世界が育つと固定値を追い越す — 既知課題として起票済み)、(b) `fetchAllTableRows` の 100 ページ打ち切り (`frontend/src/lib/dbTable.ts:79-93`。打ち切りを console.error にはするが、呼び出し側は「全件」を前提にしている)、(c) `pip check` の衝突報告 (件数を数えて警告するが、更新は止めない)。いずれも「大量を入れたときに出力の量が変わる」形。

---

### 付記: この調査で実行しなかったこと

- `pytest` を一切実行していない。「テストが存在する」ことしか確認していない。
- `main.py` / `setup.bat` / `update.bat` / `snapshot.py` などを一度も実行していない。
- 本番ペルソナ・本番 DB (`~/.saiverse/`) に読み書きしていない。`.env` の値も読んでいない。
- `.worktrees/` 配下は一切参照していない。
- `expansion_data/` は `ls` でディレクトリ構成だけを確認し、各アドオンの実装は読んでいない (担当範囲の指示どおり)。
