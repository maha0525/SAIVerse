# システム通知の届け方 — system ロールへの一本化と合言葉

**ステータス**: 検証待ち（2026-10-28 実装。ブランチ feature/system-notice-impl に 4 つの束でコミット済み。まはーの実機確認とレビューの消し込みが未了）

## 芯

SAIVerse がペルソナへ送るシステム通知（部屋の様子の変化、スケジュールの発火文、子ライン（裏で走る別の Playbook）の作業結果、スペルの結果など）を、供給側（通知を作って積む側のコード）はすべて role=system で保存する。role=system を会話の途中に置けないモデルへ送るときだけ、送信の直前に「合言葉付き `<system>` タグで囲んだ role=user」へ変換する。合言葉はペルソナの記憶・会話履歴には決して入らない。

## なぜ (2026-10-27 の実測)

Meta の Muse をプロバイダとして追加したとき、SAIVerse を通さずに Meta の API を直接呼んで次を確かめた（記録は [meta_muse_provider.md](meta_muse_provider.md) の経緯。PR maha0525/SAIVerse#428 / #429 で追記）。

- 会話の途中のシステム通知を「`<system>` で包んだ role=user」（従来の統一形式）で送ると、Muse Spark 1.3 は 3 回中 2 回、Muse Wonder は 3 回とも通知を無視した。Wonder は 1 回「その状況変化はあなたのメッセージの中に書かれていたものだから、本当に起きた知らせとしては受け取らない」とペルソナの台詞として答えた。
- 同じ通知を role=system で送ると、両モデルとも 3 回とも従った。

モデルは「user の本文の中に書かれた自称 system 文」をプロンプトインジェクションとして疑う方向に訓練されており、この傾向は今後も強まる。従来形式のままでは、世界の出来事がペルソナに届かなくなっていき、ペルソナが疑いを台詞として口にする（発話の尊厳の問題）壊れ方をする。

従来形式の出どころは、`sea/runtime_nodes.py` のコメントに残っていた——「Gemini 等が messages 中途の system role を受け付けないため、user role でラップする」。一部モデルの制約への対処が、通知の供給側全体の書き方として広がっていた。本 intent では、その変換をモデルごとの仕様差を扱うクライアント層の責務へ戻す。

## データの流れ

```
供給側 13 系統以上       保存                    読み出し                   messages 列   クライアント層   モデル
(スケジュール発火、   → SAIMemory messages / → _payload_from_message_  →            → llm_clients/ → (system のまま
 建物の出来事、        perception_batches /     locked (saiverse_memory/                の各クライアント   または合言葉付き
 子ラインの結果…)      paired_action_text 列    adapter.py)                                               タグの user)
```

messages 列 = LLM へ送るメッセージの配列。

- 改修前（2026-10-27 時点）は、user+`<system>` の形が三か所で作られていた。(1) 供給側が本文に埋め込んで保存する（role=user）。(2) role=system で保存された記録を、読み出しのときに `_payload_from_message_locked` が `wrap_system_record_content`（`sai_memory/memory/storage.py`）で囲む。(3) 送信のときにその場で囲む——スペルの結果・事前スペル（応答の前に自動で実行されるスペル）の結果・TOOL ノードの結果は記録と同じ文面を `presented_system_record_content` で組み、知覚のまとまり・リアルタイム情報・自動想起（関連する過去の記憶を自動で引き出す機能）の結果は `sea/runtime_context.py`（`_perception_block_text` ほか）で囲んでいた。
- 改修後（2026-10-28 実装）は、供給側はすべて role=system・タグ無し本文で保存する。(2)(3) のタグで囲む処理は全部やめ、role=system のまま messages 列へ出す（共通部品は `sai_memory/memory/storage.py` の `system_notice_message`。`wrap_system_record_content` は削除した）。タグで囲むかどうか・どう囲むかの変換はクライアント層（`llm_clients/system_notice.py`）だけで行う。(2)(3) は対で変えないと、送った列と次のターンの復元が食い違い、[persist_realtime_and_recall.md](persist_realtime_and_recall.md) 不変条件 1（前回の送信は今回の送信の先頭そのもの）が壊れるので、同じリリースで両方を変えた。

調査の全件一覧（供給側の一覧、保存形式の分類、各系統の識別用 metadata）: [2026-10-27_system_notice_survey.md](../handoff/2026-10-27_system_notice_survey.md)。

## 不変条件

1. **システム通知の保存形式は role=system、本文はタグ無し。** 本文に `<system>` を埋め込む供給側を新設しない。識別用の metadata の鍵は一つに定義して全系統で揃える。鍵は `__system_notice__`（`sai_memory/memory/storage.py` の `SYSTEM_NOTICE_KEY`）で、値はその通知を作った系統の ASCII snake_case 名（`schedule`、`building_event`、`subline_report` など）にした。`docs/concepts/saimemory.md` の「システム通知には `event_message` タグ必須」の規則はここで合流させ、識別の正は `__system_notice__` とした（既存の tags は残っていて、あらすじの機構名義の判定などは今も tags を読む）。
2. **role=user を role=system へ変換する仕組みを持たない。** 「本文が `<system>` で始まるか」を意味の判定に使う常設コードを置かない（移行の一回だけが例外）。ユーザーや外部の文章がシステム通知に化ける経路は、移行後の世界のどこにも無い。例外は運用者自身の手: 記憶ブラウザの編集 API とインポートスクリプトでは role=system の行を作れるが、それは所有者が自分の世界に行う管理行為として認める。
3. **合言葉はペルソナの記憶・会話履歴・ログ・画面に出さない。** 保存してよいのは head（プロンプト先頭部）の実体 `SessionHeadSnapshot`（(persona_id, model_key) に一つ — [beat_execution_context.md](beat_execution_context.md) §3.1）の `PASSPHRASE` 列だけ。head の作り直しのたびにランダムに生成し直す。head の本文には仮置きの記号（`{{SYSTEM_NOTICE_PASSPHRASE}}`）を書き、実際の値はクライアント層の最終変換でだけ差し込む——SAIVerse 層で messages を写す口（`llm_io.log` を書く `_dump_llm_io`、画面へ返す `/api/chat/preview`）に値が乗らないようにするため。
4. **識別用の metadata はサーバーだけが書ける。** 送り手から届く metadata を選別する処理（`api/routes/chat.py` の `_client_metadata`）は禁止リスト方式で、改修前は `tags` と `with` が素通りしていた（= 既存の識別用 metadata は送り手が偽造できた）。システム通知の識別に使う鍵は「送り手の入力から必ず取り除かれる領域（`__` 始まり）」に置き、あわせて `tags`・`with` も送り手から書けないよう閉じた。X・Discord など外部由来のテキストは本文としてしか入らないことを保つ。
5. **システム通知の時系列上の位置は保たれる。** 変換は元の位置で行う。先頭へ寄せてよいのは head 由来の system（人格指示）だけで、その判定は「先頭に連続しているか」という位置ではなく、head 由来であることを示す metadata（`__head__` = `HEAD_MESSAGE_KEY`。記録には保存しない）で行う（履歴が空のターンでは、冒頭の通知が人格指示と区別できなくなるため）。
6. **同じ head を使い続けている間、同じ記録から組み立てる送信本文は一字も変わらない**（persist_realtime_and_recall.md 不変条件 1 の継承）。一致を取る対象が「保存した本文そのまま」から「クライアント層の変換後の本文」へ移る。合言葉の更新は head の作り直しと同時にだけ行う。
7. **他ペルソナ・ユーザーの発話は role=user のまま。** 世界の中の発話はシステム通知ではないので、role=system 化の対象にしない。

## 設計

### 保存 — 供給側の正規化

全件一覧の 13 系統（本文に `<system>` を埋め込んで保存していた供給側）を、role=system・タグ無し本文 + 識別用の metadata `__system_notice__` での保存に変えた（#13 の添付アイテム作成の注記は保存されていなかったので、提示側で扱った — 下の「移行」の節）。Playbook の MEMORIZE でツールの生の結果をタグ無しの role=user で保存していた Playbook も、`MemorizeNodeDef` の既存の `role` フィールドで role=system に変えた（DB への再 import がリリース手順に要る）。他ペルソナの発話（`名前: 本文`）は role=user のまま変えない。

保存経路の注意: `persona/history_manager.py` の `_sync_to_memory` は、改修前は role=system の行を警告を出さずに読み飛ばしていた（`("skipped", None)` が返る）。スケジュール発火文と建物の出来事はこの経路（`add_to_persona_only`）を通るので、role を変えるだけでは記録が消える。実装では、`__system_notice__` を metadata に持つ role=system の行だけを記憶へ書き、印の無い system 行（入室案内など、機構がその場の状況として積むだけの行）は従来どおり書かない形にした。

### 読み出しと messages 列の組み立て

`_payload_from_message_locked` は、role=system を role=user + タグへ変換するのをやめ、role=system のまま metadata 付きで返す。送信のときにその場で包んでいた箇所（データの流れの (3)。スペル・事前スペル・TOOL・子ラインの報告、知覚のまとまり・部屋の様子、リアルタイム情報・自動想起、時刻の目安・自律行動の記録・あらすじの畳み・keep-alive、作業セッションの指示書・tell・コマの締め、音声通話の host 行など）も role=system の通知として出す。paired_action_text（発話の記録に対で付く指示文の控え）も、保存形式をタグ無しにし、展開のときに role=system の通知として出す。

添付アイテム作成の注記は、ユーザー発言の本文へ連結するのをやめ、発言の直後に独立した role=system の通知（`__system_notice__` = `attachment_note`）として置く（`sea/runtime.py` の `_enrich_history_with_attachments`）。発言の本文はユーザーが言った言葉そのままに残る。

head の自分の姿（self_view）は、タグだけ外して role=user のまま置いた（理由は経緯の 2026-10-28 の項）。

### クライアント層の変換と合言葉

- 判定と変換は `llm_clients/system_notice.py` の一枚にまとめた。会話の途中の role=system を受け取れるかの判定（`accepts_mid_conversation_system`）は、`xai_native` は受け取れる側、`openai_compat` と `nvidia_nim` はモデル設定の `convert_system_to_user` が真なら受け取れない側・それ以外は受け取れる側、それ以外の protocol（`anthropic_native`・`gemini_native`・`openai_codex`・`ollama_compat`・未知の protocol）は受け取れない側にした。
- 会話の途中の role=system をそのまま送れるモデルへは、そのまま送る。Muse は直接呼び出しで実測済み。ただし Meta のプロバイダ定義（`builtin_data/providers/meta.json`）の `default_convert_system_to_user: true` を外す PR #429 は 2026-10-28 時点で未マージで、このブランチでは Muse はタグで囲む側に落ちる。OpenAI と xAI（`llm_clients/xai.py` は途中の system をその場に置く）は仕様上受け付けるが、SAIVerse の messages 列に途中の role=system が現れるのは本 intent が初めてなので、実サービスでの実測は検証計画に残っている。
- 置けないモデルへは、送信の直前に `<system pass="合言葉">本文</system>` の role=user へ元の位置で変換する（`present_system_notices`）:
  - OpenAI 互換（`llm_clients/openai_message_preparer.py`）: head の印を持つ列では `convert_system_to_user` を「途中の system を合言葉付きタグで囲む」の意味で読む（true を宣言している既存モデル定義は、タグで囲む側 = 安全側に落ちる）。head の印を持たない列（要約・判定など、head を組まない呼び出し）は従来の扱いのまま。
  - Gemini（`llm_clients/gemini.py`）・Anthropic（`llm_clients/anthropic_request_builder.py`）・Codex（`llm_clients/openai_codex.py` — instructions へ寄せる）: 先頭の system 欄へまとめるのは head の印を持つ system だけにし、途中の system は合言葉付きタグの role=user へ元の位置で変換する（不変条件 5）。
  - Ollama（`llm_clients/ollama.py`）: チャットテンプレート次第で途中の system が落ちうるので、タグで囲む側に倒した。
- 画像を持つ通知の扱い: タグで囲む側では、metadata の media を引き継いで role=user のまま画像を埋め込む。そのまま送る側では、system メッセージは文字しか持てないので、画像は説明の文（`llm_clients/system_notice.py` の `system_notice_image_note`）に置き換わり、通知の本文の後ろに足される（OpenAI 互換・xAI とも。本文が空で画像だけの通知も、空のメッセージとして落とさずに説明の文で残す）。説明の文は、記録の metadata の画像の項目に保存済みの要約（`summary`）があれば `[画像: <uri>] <要約>`、無ければ `[画像: <uri>]` だけで、その場で要約を生成しない — 生成する `image_summary_note` を使うと、生成の成否で同じ記録から組む送信本文が変わり、不変条件 6 が割れる。
- 変換のとき、通知本文の中に引用で入った `<system` / `</system` は全角の `＜` で始まる文字列にして無害化する。あわせて、通知以外のメッセージ本文に現れた合言葉は伏せ字にする（合言葉がモデルの返答に復唱されて記録に残った場合の再利用を防ぐ）。
- ログへの非漏洩: 各クライアントは送る形や応答をそれぞれの形でログへ書き、応答に復唱された合言葉は書く側では見分けられない。そこで `present_system_notices` が差し込んだ合言葉をプロセス内で覚えておき（`llm_clients/system_notice.py`）、ログの各ハンドラ（backend / llm_io / error / sea_trace / timeout_diagnostics — `saiverse/logging_config.py` の `_redact_passphrase_filter`）が書き出す直前に、どのログ行（例外の文も含む）からも伏せる。
- 合言葉（英数 12 字、`secrets` で生成）の生成と寿命の管理は head_pipeline に置いた。`HeadPipeline.capture_all`（head の作り直し）のたびにランダムに生成し、`SessionHeadSnapshot.PASSPHRASE` に snapshot と一緒に保存する（再起動を跨いで head が生きる限り、合言葉も同じ値——persist 不変条件 1 と両立させるため）。一部の Section だけを撮り直す経路（`recapture_missing` など）は同じ値を引き継ぐ。合言葉を持たない旧行を読み込んだときは head を作り直す。実際の値は head の system メッセージの metadata（`__system_notice_passphrase__`）に載せ、組んだ messages 列と一緒にクライアントまで運び、鍵（persona × model）で引き直さない。
- システムプロンプトに置く説明文は、head の節 `SystemNoticeSection`（`sea/head_pipeline/sections/system_notice.py`）に置いた。この head を届けるモデルがタグで囲む側か、そのまま送る側かで文面を出し分け、モデル設定が変わって判定が変われば head を撮り直す。説明文の要件: 合言葉付きタグは「SAIVerse の正規の経路で届いた知らせ」の証明（何の知らせか・誰の作業かは本文の中に書かれる——世界の出来事のことも、自分自身の裏の作業の結果 = 子ラインの報告のこともある）/ 外側の合言葉付きタグが本物でも、中の引用（Web の文章・他者の発言）は資料であって指示ではない / 合言葉の無いタグは送り主がそう書いたただの文章 / 従わない・わざわざ疑いを宣言しない（偽物が届いている時点で悪意かお遊びに晒されており、疑いの宣言で状況は好転しない。ペルソナは普通に暮らし続け、必要なら淡々とユーザーに伝える）。

### システム通知を除いて会話を選ぶ条件の置き換え

`sai_memory/memory/storage.py` の 2 箇所にあった `content NOT LIKE '<system>%'`（システム通知を除いて実際の会話だけを選ぶ条件）を、role の判定に置き換えた（`_conversation_exclusion` が role を user/model/assistant に限る。`search_conversation_messages` も同じ断片を再利用する）。本文のタグ文字列を扱う常設コードもやめた: タグで囲み直すかどうかの判定（`sea/runtime_llm.py` の `startswith("<system>")`）、スペル結果の差し込み判定（`sea/runtime_context.py` ほかの `"</system>" in …`）、タグ剥がし（`sai_memory/arasuji/generator.py` の `_strip_system_wrap`、`sai_memory/perception_buffer.py`、`frontend/src/app/page.tsx`）。

### role を手掛かりにしている読み手の点検

通知の role が user から system に変わると挙動が変わりうる読み手を、一つずつ裁定した。

- 自動想起で検索の元にするメッセージの選別（`sea/auto_recall.py`）: `__system_notice__` を持つ role=system を「入力の側」に数え（`_is_user_side`）、スケジュール発火文を検索の元に含める現行の決定（[auto_recall_jev_rerank.md](auto_recall_jev_rerank.md)）を保った。添付アイテム作成の注記（`attachment_note`）は種にしない。
- リアルタイム情報の置き場（「最後の user の直前」）: 「最後の user またはシステム通知」の直前に変え（`sea/runtime_context.py` の `last_input_index`）、改修前と同じ位置を指すようにした。
- あらすじや退場の量の勘定で「システムが自動で入れた行」を見分ける判定: 従来の判定（tags）のまま据え置いた（経緯の 2026-10-28 の項）。

### 移行 (database/system_notice_migration.py)

- **置き場と起動**: 移行は `database/system_notice_migration.py` に置いた（当初の案の `database/migrate.py` ではない）。`main.py` が起動のたびに、本体 DB のスキーマ同期の後、バージョンアップグレードと SAIVerseManager の初期化（ペルソナの記憶 DB を開く処理）の前に `run_startup_system_notice_migration` を呼ぶ。
- **一回限りにする仕組み**: ペルソナの記憶 DB は、その DB の `embed_metadata` に完了の印（`system_notice_migration_done`、値は完了時刻）を書き、印があれば何もしない。本体 DB は印を持たない。変換の条件（タグで始まる・未配送・prepared など）を満たす行は変換すると条件から外れるので、毎起動呼んでも二度目以降は何も書かない。
- **対象（全数）**:
  1. ペルソナの記憶 DB の `messages` のうち、本文が `<system>` で始まり、**かつ**その系統の識別用 metadata を持つ行。タグを剥がし、role=system + `__system_notice__`（値は系統名）にする（`classify_tagged_message`）。拾う系統は、スルースの採取記録・ライフの境界・記憶整理・知覚（知覚のレンダリングより前の世代）・バージョンアップの知らせ・音声通話・建物の出来事・その他の `event_message` を持つ行・スルースのダイジェスト・Track 切替通知（`track_context`）・Track の Chronicle 挿入（`track_chronicle_insert`）・状況変化（`situation_change`）・スケジュール入力（`with:["system"]`）・子ラインの報告と MEMORIZE（`playbook:` タグ）、それに中断の記録（role=assistant + `interrupted` タグ）。
  2. 同じ表の、印の無い role=system 行（スペル・事前スペル・TOOL・Handy Tool・権限拒否・実行エラー・MEMORIZE の結果）。tags から系統を推定して `__system_notice__` を付ける（`classify_unmarked_system_message`）。推定できない行には印を付けず、件数をレポートに出す。
  3. `messages.paired_action_text` のうち `<system>` で始まる値。タグを剥がす。
  4. `perception_batches` の kind=`realtime_and_recall` の `rendered_text`。`<system>…</system>` の塊が二つ連結された本文なので、全部の塊を剥がし、塊どうしを `\n\n` で繋ぐ。これは提示の最終形（`compose_turn_realtime_and_recall_content`）と一字一致する。kind が違うタグ始まりの行は変換せず、レポートに出す。
  5. 本体 DB の `building_messages` のうち、音声通話の痕跡（role=user + タグ + `event_message` + `voice_call`）。今の形（role=host・タグ無し）にする。
  6. 本体 DB の `execution_ledger` のうち、判断点 on_event の prepared 行の `dispatch_envelope.user_input`（回収で応対の入力として送り直される）と、スルースの台帳（kind=`sluice.pan`）の applied / completed 行の `RESULT_JSON.prompt`（再適用のときに記録の paired_action_text になる）。
  7. 本体 DB の `execution_outbox` のうち、記憶への追記（`saimemory.append` / `saimemory.append_digest`）の未配送（pending / dead）行の `message`。送り終えた行の凍結は崩さない。
- **剥がし方**: 一番外側の包みだけを外す（`strip_system_tags`）。剥がすのは、本文全体が完全な `<system>…</system>` の組を空白だけを挟んで並べた形の行だけ。組の中で開きと閉じが同数そろって入れ子になっているタグ（再開文が元の発火文を引用している世代など）は本文の一部としてそのまま残し、送るときにクライアント層が無害化する。対の無い閉じタグ・閉じられていない開きタグ・組の外の地の文がある行は、外部由来の本文に混ざったタグとどこまでが包みかを機械的に決められないので、系統が判定できても変換せず、理由（タグの対応が曖昧）つきでレポートへ出す。
- **残すもの**: 識別用 metadata が無いのにタグで始まる行は**触らずに残し**、その変換しなかった行もレポートへ全件出す。外部由来の偽物はそのまま残す（role=user のままなら、合言葉の規則のもとで自然に「ただの文章」扱いになる）。
- **偽造への備え**: 既存の識別用 metadata（`with:["system"]` など）は送り手が偽造できた期間がある（不変条件 4）。移行は印と本文の AND で判定し、**系統ごとの件数と本文のサンプルをレポートに出して人の目を通す**形にした。当初の案にあった「系統ごとの出どころの裏付け（pulse の種類、建物ログとの突き合わせなど）」は実装していない。
- **埋め込みベクトル**: タグを剥がした行の埋め込みは作り直さない（差はタグの文字列だけ）。
- **バックアップ**: 本体 DB は起動冒頭の pre_upgrade バックアップに加えて、書き換える行があるときだけ `<SAIVERSE_HOME>/backups/system_notice_migration/` に写し（`saiverse.db.pre_migration_<時刻>.bak`）を取る。ペルソナの記憶 DB も、書き換える行がある DB だけ同じフォルダの `<persona_id>/memory.db.pre_migration_<時刻>.bak` に写しを取る。記憶 DB の印の確認と変換対象の走査は読み取り専用で開いて行い、写しは `init_db`（スキーマの用意と既存行の埋め直しをコミットする）より前に取る — 写しは「何も触る前」の状態になる。写しが取れなければ、その DB は書き換えない。通常の起動時バックアップは数が上限を超えると古いものから消えるので、写しは別の置き場に置いた。
- **レポート**: 変換した行・変換しなかったタグ始まりの行・系統を推定できなかった role=system の行・エラーのどれかがあるとき、同じフォルダに `report_<時刻>.md` を書く。中身は、DB ごとの移行前の写しの場所、表と系統ごとの件数、系統ごとに 3 件までの前後のサンプル（本文は冒頭 160 字）、変換しなかったタグ始まりの行の全件、推定できなかった role=system の行の件数と先頭 50 件、旧 MEMORIZE の結果の候補の件数と先頭 50 件。ペルソナの記憶 DB で「変換しなかった行だけ」がある場合は、印の無い初回だけレポートを書く。
- **旧 MEMORIZE の結果の候補（変換しない）**: 旧世界で role=user・タグ無しの本文で保存された MEMORIZE の結果（building_move / document_create / document_search / generate_image など）は、発話でないことの来歴を機械的に確定できないので変換しない。代わりに「role=user・タグ無しの本文・`playbook:` タグあり・`conversation` タグ無し」の行を候補として、件数と先頭 50 件（id と本文の冒頭）をレポートの独立した節に出し、人の目で確かめる。
- **知らせ**: 結果は `manager.startup_alerts` に積まれ、画面のバナーに出る。変換があれば「新しい形式へ移行しました」（写しとレポートの場所つき）、移行できなかった DB があれば警告を出す。失敗した DB は印が立たないので次の起動で再試行され、起動は止めない。
- **コピーで試す**: `python -m database.system_notice_migration --home <SAIVERSE_HOME の写し> --dry-run` は読み取り専用で開いてレポートだけを書く。`--dry-run` を外すと写しに当てる。`--db` で本体 DB の場所を、`--report` でレポートの書き出し先を変えられる（`docs/reference/scripts.md`）。
- **後から取り込んだ記憶**: 完了の印は記憶 DB の `embed_metadata` にあり、ペルソナまるごとパックは `embed_metadata` を運ばない（`saiverse_memory/persona_pack/export.py` の `NOT_CARRIED_MEMORY_TABLES`）。だから、移行の後に新しく作られた記憶 DB へ旧エクスポートを取り込んだ場合は、その DB に印が無いので次の起動で移行される。一方、すでに印の立った記憶 DB へ旧形式の行を取り込んだ場合（既存のペルソナへの合流 `mode="merge"` など）は移行が走らず、旧形式の行は role=user のまま残る。これは既知の制約として未解決。
- **添付アイテム作成の注記（全件一覧の #13）は移行の対象ではない**: 全数調査では「ユーザー発言の末尾に埋め込んで保存」と書いたが、実装時の確認で、保存はされておらず、文脈を組むたびに `_enrich_history_with_attachments`（`sea/runtime.py`）が発言の metadata からその場で足す形だった。記録に旧形式が残っていないので、提示側の変更（上の「読み出しと messages 列の組み立て」）だけで済んだ。

### 掃除

呼び出し元の無いコードを削除した: `sea/runtime_context.py` の `_expand_recalled_ids`、`sea/pulse_context.py` の `get_protocol_messages`、`sai_memory/memory/recall.py` の `build_context_payload`、ツール 4 本（`get_situation_snapshot`・`get_since_last_user_conversation`・`get_history`・`record_wait` — スペルと Playbook のどちらからも到達しないことを確認した）。`builtin_data/tools/get_visual_context.py` の `_render_head_view` は、タグだけ外して残した。

静的検査として `tests/test_system_tag_literals.py` を足した。許可した 4 ファイル（`llm_clients/system_notice.py`、`llm_clients/openai_message_preparer.py`、`sea/head_pipeline/sections/system_notice.py`、`database/system_notice_migration.py`）以外の Python・Playbook の JSON・フロントエンドのコードに `<system` / `</system` の文字列が無いことを固定する。

## キャッシュへの影響

messages 列の中身が変わるので、適用した瞬間に全ペルソナ・全モデルのキャッシュが一度 head から作り直しになる。一回限りの計画されたコスト。だから供給側の正規化・読み出しの変更・クライアント層の変換・移行は**一つのリリースに束ね**、小出しにしない（小出しにするたびにキャッシュが作り直しになる）。

## 影響する既存文書

2026-10-28 の文書の改訂（第 5 束）で、下の各文書の旧形式を定めた記述を本 intent の形に改めた（`docs/handoff/` と `docs/audits/` の当時の記録、退役した機構の経緯は、指針として読まれる箇所だけを直した）。

- [persist_realtime_and_recall.md](persist_realtime_and_recall.md): 不変条件 1（一字一致）は維持し、一致を取る対象がクライアント層の変換後へ移る。不変条件 2（OFF のモデルは何も変わらない——今回は送る列の role が一度変わった）は経緯に記録した。不変条件 3（保存は `<system>` タグ付き user）・§4.1・§5（TOOL の結果を、記録から読み出したときと同じ形 = 復元形で足す規則）は、role=system・タグ無し・`__system_notice__` の形へ書き換えた。
- [beat_execution_context.md](beat_execution_context.md) §3.1 / [cached_head_architecture.md](cached_head_architecture.md): 合言葉は `SessionHeadSnapshot` の作り直しと同時に再生成し、snapshot と一緒に `PASSPHRASE` 列へ保存する。
- [meta_muse_provider.md](meta_muse_provider.md): 「会話の途中の system は user へ変換する」の決定は PR #429 で撤回する（2026-10-28 時点で未マージ）。本 intent はそれを全プロバイダへ一般化する。
- `persona_cognition/02_mechanics.md`（子ラインの報告の形、Track 切替時の Chronicle 挿入）と `persona_action_tracks.md`、`persona_cognitive_model.md`（report_to_parent の形）: 「user role + `<system>` タグ統一形式」の定めを本 intent の形式（role=system）へ改めた。「通知がペルソナに自分への通知として認識される」という目的は変わらない。
- そのほか user+`<system>` の形を前提にしていた文書: `concepts/saimemory.md`、`auto_recall_jev_rerank.md`、`gold_panning.md` §3.8、`perception_buffer.md` §10.3、`chronicle_coverage_gaps.md`、`memory_architecture_v2.md`、`embodied_passive_input.md`、`codex_window_addon.md`、`nested_subline_spell.md`、`audits/2026-09-12_normal_behavior_consolidation/normal_behavior.md`、それに grep で追加で拾った文書（`persona_cognition/track_chronicle.md` §7、`ultramemory.md`、`x_integration.md`、issue の `runtime_llm_node_split_design.md`・`spell_loop_continuation_contract.md`・`external_text_prompt_injection_boundary.md`・`in_pulse_message_forms_differ_from_restored_history.md`・`llm_call_entry_point_standardization.md`・`judgment_seat_contention_and_event_loss.md`）。
- **stackchan アドオン（リポジトリ外）**: `saiverse-stackchan-addon` の `audio_input_relay.py` は、機体からの音声を届けるとき、`<system>ｽﾀｯｸﾁｬﾝから音声入力を受信しました。…</system>` という本文をユーザー発言として送っている。新しい規則では合言葉が無いので、この文はただの文章として扱われる。アドオン側の追従修正が要る。アドオンの新しい版には、アドオンカタログ（`saiverse-addon-registry`）の版の項目の `min_saiverse_version`（`saiverse/addon_registry.py`。導入と更新の前に `saiverse/addon_installer.py` の `check_min_saiverse_version` が確かめる）を付けて、本体のリリースと足並みを揃える。

## 検証計画

- **静的検査（済み）**: クライアント層と移行スクリプトなど許可した 4 ファイル以外のコードに `<system` の文字列が残っていないことを `tests/test_system_tag_literals.py` で固定した（供給側の付け忘れと、従来の書き方が再び混ざるのを防ぐ）。
- **単体テスト（済み）**: 供給側の保存形式（`tests/test_system_notice_supply.py`）、提示の切り替え（`tests/test_system_notice_presentation.py`）、head の合言葉と説明文、ログ・プレビューへの非漏洩（`tests/test_system_notice_head.py`）、各クライアントの変換・偽タグの無害化・画像の引き継ぎ（`tests/llm_clients/test_system_notice.py`）、移行の全系統の最終形・偽物と通常行の無傷・冪等・dry-run・送信済みの台帳と配送済みの送信トレイの無傷（`tests/test_system_notice_migration.py`）。2026-10-28 にこの 6 ファイルを通した（59 件成功）。
- **隔離環境での通し（未実施）**: 隔離環境（`SAIVERSE_HOME` 隔離 + テスト用に作った仮のペルソナ）で、保存 → 読み出し → 各クライアントの送信本文（外部通信なしのモック HTTP）までを通す。偽タグ（合言葉なし・引用の中のタグ）の無害化、画像を持つ通知、persist の機械検査（`find_unrecorded_messages`）が WARN を出さないこともここで見る。
- **移行レポートの目視（未実施）**: 本番 DB のコピーに対して `--dry-run` と実行を行い、移行レポートの系統別件数・サンプル・変換しなかった行を目で確かめる。
- **実サービスでの実測（未実施）**: Muse（role=system 直送。PR #429 のマージ後）と Gemini / Claude（合言葉付きタグ）で、会話の途中のシステム通知に従うことを実測する。費用見積もりを出して承認を得てから行う。
- **画面の確認（未実施）**: 記憶ブラウザには role=system の表示スタイルが既にある（`MemoryBrowser.module.css`）。履歴表示がどう描かれるかを、プレビューを開いて確かめる。

## 経緯

- 2026-10-27: Muse の実サービス確認で、従来形式のシステム通知が無視されることが判明（[meta_muse_provider.md](meta_muse_provider.md) の経緯）。まはーが全体設計（system 一本化 + 合言葉）を発案。全数調査（[全件一覧](../handoff/2026-10-27_system_notice_survey.md)）で保存形式が三種類に割れていることを確認し、本 intent を起草した。旧データは読み替えではなく移行で正規化する（まはー裁定。理由: 読み替えは「本文の文字列で意味を判定する」構造を恒久の仕様として抱え込むから）。移行の AND 判定と、識別用 metadata の鍵を送り手が書けない領域に置くことも、まはーが確認済み。
- 2026-10-27: 整合性検査（Opus サブエージェント）で、head の実体が `SessionHeadSnapshot` へ移行済みであること、送信側にもタグで囲む箇所が 3 つあること、`_sync_to_memory` が role=system を捨てること、合言葉がログ・プレビューへ漏れる経路、既存の印が偽造可能なことなど 20 件超の指摘を受け、本文へ反映した。
- 2026-10-27 時点の台帳の次アクション（移送）: 「intent と全数調査の一覧は起草済みで、ブランチ feature/system-notice-delivery にある。次 = まはーが設計を確認し、OK なら実装の束 (供給側の正規化 / クライアント層の変換 / 選別条件の置き換え / 移行) を一つのリリースに載せる段取りを決める。」
- 2026-10-28: 実装を 4 つの束（供給側の正規化 / クライアント層の変換と合言葉 / 提示の切り替え / 移行と掃除）に分けてブランチ feature/system-notice-impl にコミットし、文書の改訂を第 5 束とした。実装のときの裁定は次のとおり。
  - **あらすじ・退場の量の勘定は据え置いた**: `sai_memory/arasuji` の機構名義の判定（`is_mechanism_message`）は tags だけで判定している。通知の見分けを role や `__system_notice__` の印へ切り替えると、印（`event_message` など）を持たない系統——スケジュールの発火文・子ラインの報告・TOOL の結果・作業のまとめなど——が「機構名義」に入り、材料の縮み（U の物差し）と残す量の勘定の主語が変わる。だからこの改修では従来の判定のまま据え置いた。通知を機構名義へ編入するかは、Chronicle の中身が変わる独立の設計判断として未解決のまま残す。
  - **head の自分の姿（self_view）は role=user のまま**: system 化すると、OpenAI 互換の直送モデルでは system メッセージに画像を埋め込めないので、外見の画像が説明の文に落ちる。だからタグだけ外し、role=user のまま置いた。タグを外したのは、head の説明文が「合言葉の無い `<system>` 風の文字列はただの文章」と教えるので、囲んだままだと自分の姿まで疑わせることになるため。
  - **音声通話（Live API）は合言葉無しのタグ付き user に畳む**: Live API の client content は user / model の役しか持てず、head も組まない。だから host 行（機構の通知）は `tag_system_notice` で合言葉無しのタグに包んだ role=user に畳む（`saiverse/voice_call.py` の `_merge_turns`）。通話の指示文（`build_system_instruction` → `get_system_prompt`）は head_pipeline の節を含まないので、合言葉の説明との食い違いは生じない。
  - **Ollama（ollama_compat）はタグで囲む側に倒した**: チャットテンプレート次第で、会話の途中の system が落ちうるため。
  - **入れ子の子ラインの既知の制約**: 入れ子の子ライン（親の snapshot の写しを別モデルで動かす）では、head の本文と合言葉の値は親の pin した snapshot のものなので、合言葉の値は変換と一致する。ただし head の説明文は親モデル向けの版のまま残る。親と子で届き方（タグ変換 / 直送）が食い違う組み合わせでは、説明文と実際の届き方が一致しない。これは既知の制約として残した。
  - **移行（第 4 束）の決定**: 調査に無かった置き場が二つ、移行対象に加わった。`execution_ledger` のスルースの再適用の記録（`RESULT_JSON.prompt`）と、`execution_outbox` の未配送のライフ境界の通知（記憶への追記）である。on_event の `dispatch_envelope` は、回収するときに剥がすのではなく台帳側を移行した。回収時に剥がすと、本文の文字列で判定する常設コードを置くことになるため（不変条件 2）。`event_message` の無い旧世代は、Track 切替通知（`track_context`）・Track の Chronicle 挿入（`track_chronicle_insert`）・状況変化（`situation_change`）・中断の記録（role=assistant + `interrupted`）の 4 系統を git 履歴から特定して拾った。タグを剥がした行の埋め込みベクトルは作り直さない（差はタグの文字列だけ）。移行の後に旧エクスポートからペルソナを新しく取り込んだ場合は、その記憶 DB に完了の印が無いので次の起動で移行される（印の立った記憶 DB へ合流した場合は移行されない — 「移行」の節）。
  - **添付アイテム作成の注記（全件一覧の #13）の訂正**: 調査では保存されていると書いたが、実際は文脈を組むたびに足す形だったので、移行対象ではなかった。実装では、ユーザー発言への連結をやめ、直後の独立したシステム通知（`attachment_note`）として出す形にした。
