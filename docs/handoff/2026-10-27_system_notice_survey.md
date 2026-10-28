# システム通知の全数調査 (2026-10-27)

[system_notice_delivery.md](../intent/system_notice_delivery.md) の前提調査。LLM へ渡る messages 列 (送るメッセージの配列) に入る「ユーザーの実発言・ペルソナ自身の発話**ではない**テキスト」を、供給側 (通知を作って積む側のコード)・保存・読み出しの三面から洗った。調査は Opus のサブエージェント (読み取り専用) が行い、設計の生死に関わる要所 (読み出しの変換が 1 箇所にまとまっていること、文字列での会話の選別、スケジュール入力と子ラインの保存、死んだコード) はメティスがコードで裏取りした。基点: develop (a8fbb855 時点の worktree)。

**2026-10-28 の訂正**: この調査の #13 (添付アイテム作成の注記) は「ユーザー発言の末尾に埋め込んで保存」と書いたが、実装時の確認で、保存はされておらず、文脈を組み立てるたびに `sea/runtime.py` の `_enrich_history_with_attachments` が発言の metadata からその場で足す形だった。だから #13 は移行の対象ではない (経緯は [intent](../intent/system_notice_delivery.md) の経緯の節)。それ以外の記述は 2026-10-27 時点の記録として変えていない。

凡例 — **a**: role=user で本文に `<system>` タグ / **b**: role=user でタグ無しのシステムメッセージ / **c**: role=system を会話の途中に挿入 / **d**: システムメッセージを role=assistant で保存。

## 結論 (要点)

1. messages テーブルの role=system の行を読む経路は `saiverse_memory/adapter.py` の `_payload_from_message_locked` の 1 箇所で、そこで必ず user+`<system>` へ変換される (包む関数は `sai_memory/memory/storage.py` の `wrap_system_record_content` ただ一つ)。【裏取り済み】ただし**送信のときに同じ形をその場で組む箇所が別にある**: スペルの結果 (`sea/runtime_llm.py:4358`)【裏取り済み】・事前スペルの結果 (同 `:5187`)・TOOL ノードの結果 (`sea/runtime_engine.py:253`) が `presented_system_record_content` で、知覚のまとまり・リアルタイム情報・自動想起が `sea/runtime_context.py` で包む。読み出しだけを変えると送った列と復元が食い違う (persist_realtime_and_recall の不変条件 1 が割れる) ので、両方を対で変える。
2. 本文に `<system>` を埋め込んで role=user で保存する供給側が 13 系統ある (下の別枠)。移行の本体。
3. 実会話だけを選ぶ処理では、本文の文字列で判定している: `storage.py` の 2697 行と 2953 行の `content NOT LIKE '<system>%'`。【裏取り済み】
4. 会話の途中に role=system を入れる生きた経路は無い。見つかった 3 箇所は全部呼び出し元が無い。【`_expand_recalled_ids` は裏取り済み】
5. システム通知を見分ける metadata・タグは統一されていない。`event_message` を持つ系統と持たない系統があり、スケジュール入力は `with:["system"]` だけ、子ライン (裏で走る Playbook) の結果は `playbook:` タグだけ。
6. paired_action_text (発話と対の指示文の控え) は、読む経路によって展開されたりされなかったりする。展開する `_expand_paired_action_payloads` が呼ばれるのは `recent_persona_messages` 系の 3 箇所だけで、通常の履歴取得経路 (`persona_messages_from_anchor`) では展開されない。既知の非対称として記録。

## 保存形式は三種類

| 形式 | 代表 | 扱い |
|---|---|---|
| role=system・タグ無し本文 (読み出しのときに包まれる) | スペル・ツールの結果、事前スペル、Handy Tool、権限拒否 | 新設計の正しい形式。変更不要 |
| role=user・本文にタグ埋め込み | 下の別枠 12 系統 | 移行の本体 |
| role=user・タグも無し | 他ペルソナの発話 `名前: 本文` (get_building_messages.py:221) / Playbook MEMORIZE の生ツール結果 4 本 | 発話は正しい形式のまま。MEMORIZE 4 本は role=system 化の対象 |

## 別枠: 本文に `<system>` を埋め込んで保存・送信する 13 系統

| # | 系統 | 組み立て・保存の箇所 | 識別用の metadata・タグ |
|---|---|---|---|
| 1 | スケジュール・外部イベントの Pulse 入力 | `sea/runtime.py:416-423`【裏取り済み】(組み立て: `saiverse/schedule_manager.py:972`、`builtin_data/phenomena/inject_persona_event.py:123,128`、`saiverse/autonomy_wiring.py:1357`) | `with:["system"]` のみ |
| 2 | 建物の出来事 (host 名義の行) の取り込み | `builtin_data/tools/get_building_messages.py:276-280` | `[internal,event_message]` |
| 3 | 子ラインの結果報告 | `sea/runtime_nodes.py:300-345`【裏取り済み】 | `playbook:{name}` タグのみ |
| 4 | スルース (sluice.py — 会話から記憶を採取する機構) の採取記録 (本文と paired_action_text の両方) | `sea/sluice.py:3518, 4335, 2122-2138` | `[internal,event_message,sluice]` |
| 5 | スルースの読み返しダイジェスト | `sea/sluice.py:4874-4876` | `[DIGEST_TAG,sluice]`。event_message 無し |
| 6 | ライフ (1 日の活動時間帯) の境界の通知 | `saiverse/day_plan.py:1990-1993` | `[internal,event_message,day_plan]` |
| 7 | 夜間の記憶整理 (curation) の報告 | `sai_memory/curation_ops.py:1372-1378` | `[internal,event_message,curation]` |
| 8 | 音声通話の痕跡 | `saiverse/voice_call.py:1255, 743` | `[internal,event_message]` |
| 9 | Playbook MEMORIZE のタグ付き action | `create_building_playbook.json:117`、`novel_writing.json:215`、`schedule_management_playbook.json:30,161,216` | `playbook_result` 系タグ |
| 10 | paired_action_text 列 | `sea/runtime_llm.py:1286` (Playbook の action)、`:4191` (スペル初回周・作業セッションの指示書)、`sea/sluice.py:2137` | 列そのものが識別になる |
| 11 | リアルタイム情報・自動想起のまとまり (履歴に残す設定 persist_realtime_and_recall のモデルのみ) | `perception_batches.rendered_text` (kind=`realtime_and_recall`。`sea/runtime_context.py:513` → `sai_memory/perception_buffer.py:940` 付近) | 別テーブルの kind が識別になる |
| 12 | (呼び出し元なし) 状況変化の通知 | `builtin_data/tools/get_situation_snapshot.py:276` | `[internal,situation_change]` |
| 13 | ユーザー発言の末尾に埋め込む「添付アイテム作成」の注記 (**2026-10-28 訂正**: 保存はされておらず、文脈を組むたびに `_enrich_history_with_attachments` が足す形だった。移行の対象ではない) | `sea/runtime.py:2604` 付近 (元の本文は `SAID_CONTENT_KEY` に控え済み) | `SAID_CONTENT_KEY` |

MEMORIZE でタグ無し・role=user のまま保存している Playbook (role=system 化の対象): `building_move_playbook.json:65`、`document_create_playbook.json:68`、`document_search_playbook.json:88,112`、`generate_image_playbook.json:141`。

## タグで包むのが送る直前の組 (messages テーブルにはタグ込みで保存されない)

- 知覚のまとまり・部屋の様子: `saiverse_memory/adapter.py:942`、`sea/runtime_context.py:1511,1908` (`perception_batches` テーブルにはタグ無しで保存し、タグで包むのは送る直前)
- リアルタイム情報: `sea/runtime.py:2781`、`sea/runtime_context.py:1397` (履歴に残す設定のモデルでは #11 として保存)
- 自動想起: `sea/auto_recall.py:1851`、`sea/runtime_context.py:2349-2371`
- 履歴の加工 (時間の区切り、自律行動の読み替え、圧縮区間のあらすじ、keep-alive): `sea/runtime_context.py:47,1186`、`sea/session_window.py:196,300`、`sea/runtime.py:2177,2331`
- head (プロンプト先頭部) の自分の姿・長期記憶の節: `sea/head_pipeline/integration.py:1320,1338`
- 作業セッションの指示書・tell・スケジュール枠 (コマ) の締めの指示: `sea/work_session.py:415`、`builtin_data/tools/tell.py:177`、`saiverse/slot_close.py:314`
- 音声通話の整形 (host 名義の行と他ペルソナ発話): `saiverse/voice_call.py:344,338`

## 読み出しの経路 (裏取り済みの要点)

- `_payload_from_message_locked` (`saiverse_memory/adapter.py:2374`): `model`→`assistant`、**`system`→`user` + `wrap_system_record_content` で包む**。system のまま返る経路は無い。metadata・line_role・scope・paired_action_text は素通し。
- `wrap_system_record_content` (`storage.py:1287`) の説明書きに「送った本文と保存から復元した本文が一字でも違うと、プロンプトキャッシュの先頭一致が壊れる」(persist_realtime_and_recall 不変条件 2) とある。文字列を一字も変えずに復元する責任はこの関数に置かれている。
- 各 LLM への送信処理: Anthropic 用 (`anthropic_request_builder.py:47-72`)・Gemini 用 (`gemini.py:1604`)・Codex 用 (`openai_codex.py:535` 付近、instructions へ) では、どの位置の role=system も先頭の system 欄へまとめられる。OpenAI 互換用 (`openai_message_preparer.py:103`) では、`convert_system_to_user` が真のときだけ、途中の role=system が role=user + `<system>` タグへ変換される。xAI 用 (`xai.py:193`) は途中の role=system をその場に置く (受け付けるかは未実測)。
- `persona/history_manager.py:205` の `_sync_to_memory` は role=system の行を保存せず `("skipped", None)` を返す【裏取り済み】。スケジュール発火文 (#1) と建物の出来事 (#2) はこの経路 (`add_to_persona_only`) を通る。

## 呼び出し元の無いコード (掃除対象の候補)

| 箇所 | 中身 |
|---|---|
| `sea/runtime_context.py:2470` `_expand_recalled_ids` | 「## 想起した記憶」を途中の role=system で挿入【呼び出し元ゼロを裏取り済み】 |
| `builtin_data/tools/get_situation_snapshot.py:276` | 状況変化の通知 (保存あり) |
| `builtin_data/tools/get_since_last_user_conversation.py:216` | role=system の要約 |
| `builtin_data/tools/get_history.py` | 呼び出し元なし |
| `builtin_data/tools/record_wait.py:85,107` | 待機の開始を role=assistant で保存 |
| `sea/pulse_context.py:660` `get_protocol_messages` / `sai_memory/memory/recall.py:390` `build_context_payload` | role=system を素通しする読み出し (利用者不明) |

## 調査で確認しきれなかった点

- `sea/pulse_controller.py:596` の再開プロンプトが、schedule 以外の Pulse でも LLM へ渡される経路があるか。
- `builtin_data/tools/thread_switch.py` を実際に呼んでいる箇所。
- spell_args_decider の paired_action_text が、どの ライン・どの scope で保存されるか。
- 知覚の個々の供給側 (head_pipeline の差分、feed など) の文面と、archive 側の Playbook。

実装のとき、各系統に触れる前にその箇所のコードを読み直すこと (この一覧の行番号は 2026-10-27 時点のもので、今後の変更でずれていく)。
