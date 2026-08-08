# Beat 頭知覚消費 + フィード入室配送 — 実装ハンドオフ (2026-08-08 深夜)

**これは何**: Fable セッションで実装完走した「知覚消費点の Beat 頭化 + フィード入室配送ほか」の現況と、レビュー 1 巡の指摘・裁定の記録。**消し込み往復は Opus セッションの担当** (2026-08-04 まはー裁定の新運用)。読者 = 明日の Opus セッションと、実機検証を再開するまはー。

## 1. 何を作ったか (コミット 48edbf1、実装の正典は intent)

設計の経緯と裁定は [perception_buffer.md](../intent/perception_buffer.md) §4.2 の 2026-08-08 改訂節と [issue](../issues/feed_arrival_pulse_cannot_see_articles.md) に記録済み。骨子:

- **消費点の移設**: 知覚バッファの消費が「Pulse の頭で一回」→「**Beat の頭**」へ。実装は `sea/runtime_llm.py` の spell ループ、`_beat_gate.boundary` の直後 — 消費した合成メッセージを SAIMemory へ書き (既存経路)、**同じ内容を作業中の `messages` にも append** して続きの生成に見せる。消費するのは最外周の認知 Beat のみ (`beat_gate.held_depth == 1`、gate 無し環境は無条件)。失敗は WARN + 続行 (知覚は永続バッファに残る)。
- **adapter**: `flush_perception_buffer_payload()` (本体・メッセージ dict を返す) を切り出し、既存 `flush_perception_buffer()` は bool ラッパー化 (`saiverse_memory/adapter.py`)。
- **フィード入室配送**: `FeedManager.deliver_unread_on_entry(persona, building_id)` — 定期サイクルと配送本体 (`_deliver_subs_to_persona` に共通化) と既読カーソルを共有。配線は `saiverse/dynamic_state.py` `on_building_entered` の「移動先の様子」push の直後 (fail-open)。
- **看板描画**: `builtin_data/tools/get_visual_context.py` の設置物欄が STATE_JSON の `feed_stand` キー (購読タイトル+直近見出し 5 件) を描画。updated_at は載せない。
- **テンプレ整合**: `saiverse/timetable_template.py` が stay_home 系×自室以外の場所を 422 拒否。UI (`TimetableTemplateModal.tsx`) は種別選択で場所を「自室」固定+送信時正規化。

テスト: work_session +2 (Beat 頭注入 / ネスト時スキップ)、feed_intake +2 (入室配送とカーソル共有 / 施設なし noop)、core_memory_scene +1 (payload 契約)、test_visual_context_feed_stand 新設 3、timetable_template +2。対象+隣接 (beat_finalize / beat_gate / perception_buffer / pre_spells / run_playbook_spell / realtime_spells_media / move_entity_ledger) 全緑。ruff clean。フロント本番ビルド済み。

## 2. レビュー 1 巡の記録 (Opus セッションはここから)

### ローカルレビュー (llama.cpp) — 完了、実指摘ゼロ + 保留 1

観点 5 系統 (消費の不整合 / 深さ判定 / カーソル共有 / bool ラッパー互換 / テンプレ検証) すべて「設計と実装が合致・退行の懸念なし」。

**保留 1 件 (PLAUSIBLE、メティスの裏取りで補強)**: `flush_perception_buffer_payload` は append 成功 → delete_perceptions の順で、**delete が例外を投げると SAIMemory に書き込み済みのまま pending が残り、次の Beat 頭で同じ知覚がもう一度 event_message として書かれる** (重複)。これは**旧実装 (Pulse 頭消費・bool 版) から同じ形で存在した既存の性質**で今回の新設ではないが、消費点が Beat 単位に増えた分だけ再試行 (=重複機会) の頻度は上がる。発生条件は「append 成功と delete の間の DB 障害」で狭い。Opus への選択肢: (a) 現状受容 + コード側にコメント明記 (ローカルレビューの提案)、(b) delete 失敗時も payload を返して messages 整合だけ保ち、重複は次回 reduce の性質に委ねる — (b) は消費済み扱いの意味論が濁るため、私は (a) 推し。

### Codex adversarial-review — 1 巡完了 (needs-attention、high 3 件)。裁定つきで以下に記録

**#1 [high] executor スレッドでは Beat 頭消費が常にスキップされる (runtime_llm.py の held_depth 判定)**

- 指摘: `held_depth` はスレッドローカル。running event loop 経路では `_run_coro_sync` が別スレッドで実行するため深さ 0 → 消費が黙って飛ぶ。
- **メティスの裁定案: 受容 (既知縮退への追従)**。この経路は [beat_execution_context.md](../intent/beat_execution_context.md) §3.4 の実測帰結に記録済みのレガシー分岐で、**boundary 自体が同じ条件で no-op** になる。消費判定はその保守則 (所有が確認できない Beat では動かない) に揃えた意図的な形。実害は「その分岐では新機能が効かず旧挙動 (次 Pulse で消費) に縮退」で、正しさ・直列性は壊れない。主要経路は同一スレッド (同節の実測)。恒久解は既定路線の **§6-6 Beat ロックのトークン化 (ExecutionContext 所有)** と同じ配管であり、そこへ合流させる。Opus 対応: コードコメントに縮退の明記 + (可能なら) 縮退経路の観測ログ格上げ程度。トークン化を今回のスコープに入れるかはまはー裁定。

**#2 [high] SAIMemory 書き込み成功後の失敗で知覚が重複保存される (adapter.py)**

- 指摘: `_append_message` は行 commit 後の embedding 失敗を握って None を返す → payload None → pending 残存 → 次の Beat 頭で同じ知覚をもう一度 event_message として書く。Beat 単位化で再試行頻度が増幅。
- **メティスの裁定案: 妥当・要修正**。ローカルレビューの保留と同根 (あちらは delete 失敗経路、こちらは embedding 失敗経路まで特定)。形自体は旧実装 (Pulse 頭消費) から存在した既存の性質だが、消費点が増えた今は増幅が実害級。修正の方向: **消費した知覚 ID 束から作る冪等キーを message metadata に刻み、flush 前に「同キーの event_message が直近に書かれていないか」を確認してから append** (append 済みなら delete だけやり直す)。pending を残す S5 原則 (知覚を落とさない) は維持する。テスト: append 成功+delete 失敗 / commit 成功+embedding 失敗の注入で、再 Beat 後に message が 1 件のままであること。

**#3 [high] フィードカーソルの push 前 commit で欠落・重複・退室後配送 (feed_manager.py)**

三つに分解して裁定:

- **(i) カーソル先行 commit で push 失敗分が欠落** → **再裁定不要 (裁定済み設計)**。「重複より欠落に倒す」は intent §13 の確定で、回帰 `test_cursor_advances_even_when_push_fails` (F2) が意図として固定している。
- **(ii) 定期便と入室配送の並走で同じ記事が二重 push されうる** → **妥当・要修正 (今回の新規欠陥)**。配送の書き手が worker スレッド 1 人だった前提に、入室配送 (移動スレッド) を足したことで生まれた並走の窓 — カーソル読み〜commit が原子的でない。修正の方向: **FeedManager に persona 単位の配送ロック** (`_deliver_subs_to_persona` 全体を包む) が最小。durable outbox 化 (Codex 提案) は配送 1 回の重みに対して過剰と見る (安全装置は両方向から破れる — 重くするなら別途裁定)。テスト: 同一 persona への entry と cycle の並走で push が重複しないこと。
- **(iii) commit 後・push 前の退室で旧 Building の記事が届く** → **再裁定不要 (既存受容)**。`_deliver_subscription_to_persona` docstring の「この確認から commit までの間の移動はなお取りこぼしうる — 完全な同期は求めない (F10 裁定の延長)」に記録済みの残余。

**収束見立て (メティス)**: 修正必須は #2 と #3-(ii) の 2 件。#1 は既知縮退の明記のみ。ただし収束は観測であって予測ではない — Opus は修正後に再レビューを回すこと。

## 2-B. 消し込みの結果 (Opus セッション、2026-08-08)

§2 の裁定どおり #2 と #3-(ii) を修正し、#1 はコメント明記のみ。追加のレビュー 2 巡で出た指摘の裁定も以下に含む。

**#2 (知覚の重複保存) — 供給源を断つ + 最後の網の二段で修正**

- 供給源: `_append_message` が「行の commit 後に作る埋め込み」の失敗を**保存の失敗**として None で返していた。呼び出し側は保存済みの行を書き直しに来る — これが最も頻度の高い二度書きの元だった。埋め込みの失敗を分離し、行が入ったなら id を返す (失うのは検索用の索引だけ。再作成は reembed API)。
- 最後の網: 消費した知覚 id を event_message の `metadata.perception_ids` に刻み、次の消費が照合して「書き終えている分」は書き直さず削除だけやり直す。残る細い口 (削除失敗・二つの commit の間のプロセス死) 用。
- あわせて、削除段が失敗しても書き出した本体は呼び出し側へ返す (その Beat で読める。後始末は次の Beat 頭)。これで「書けたのに呼び出し側が受け取れない」経路が消え、照合が None を返すのは**内容が既に目に入っている並びだけ**になった。
- 正典: [perception_buffer.md](../intent/perception_buffer.md) §7 C6。

**#3-(ii) (配送の並走)** — `FeedManager` にペルソナ単位の配送ロック。正典: [rss_feed_intake.md](../intent/rss_feed_intake.md) §9-7。

**#1** — 縮退の明記のみ (`sea/runtime_llm.py`)。恒久解は §6-6b へ合流。

**追加レビューで出た指摘の裁定**

- **[却下] 並行 flush で二重書き込みが残る**: 消費は Beat の内側でしか呼ばれない (Pulse 全体が `hold_beat`、スペルループは保持深さ 1 のときだけ) ので、同じペルソナの消費同士は割り込まない。前提と「前提が崩れる条件」を adapter のコメントと C6 に明記した。
- **[受容・要観察] Beat 境界の割り込みで継続生成が知覚を見失う**: `boundary` の解放中に別 Beat (会話 Pulse 等) が同じバッファを消費すると、再取得後の消費は空振りし、元の周の継続コンテキストにその知覚は入らない (次の Pulse では見える)。**この改修の前の挙動と同じ**ところへ落ちるだけで欠落ではない。根治は「周の途中で提示コンテキストを追い足す」設計判断で、prefix キャッシュの前提に触るため単独では決められない — まはー裁定待ち。
- **[受容] 更新前から残っている刻印なしの中断跡**: 旧実装が作った「行はあるが pending も残る」組があれば、更新後の最初の消費でもう一度書かれる (それ以降は照合が効く)。**実測: 全ペルソナの `perception_buffer` は現在 0 件** (読み取りのみで確認) — 残っている中断跡はゼロなので、移行処理を用意する理由が無い。埋め込み障害が続けば旧コードでは Pulse ごとに重複が積み増しうる形ではあったが、その履歴は既に書かれたペルソナの記憶であり、内容一致で機械的に消すのは「同じ知覚が本当に二度届いた回」まで消しかねない (本人の記憶を推測で削らない)。
- **[却下・別案件] anchor の時刻逆行で event_message が読まれない**: 提示コンテキストは `(created_at, rowid)` の順序で anchor 以降を読むので、壁時計が巻き戻った瞬間に書かれた行は anchor より古くなり読まれなくなる。これは知覚に限らず**全ての行**に効く anchor 設計 (W8 の時刻正典順序) の性質で、この改修の範囲外。単調な挿入順序で境界を持つかはまはー裁定。
- **[修正] 作業セッションの頭に消費点が無い**: 周の切れ目にしか消費点がないため、スペルの無い応答や spell 無効のペルソナでは、開始時点で届いていた知覚がセッション中ずっと入らなかった。`run_work_session` の Beat ロック内・コンテキスト組成の**前**に消費を置いた (`run_meta_user` と同じ並び)。

## 3. Opus セッションへの依頼

1. §2 の指摘を裏取りして消し込み (妥当な指摘のみ。裁定済みの却下は繰り返さない)
2. 収束後フルスイート 1 回 → コミット
3. 終わったら in_flight 台帳の「知覚消費点の Beat 頭化」行を検証待ちへ、まはーへ実機検証再開の合図 (統合検証手順 = [2026-08-07 handoff](2026-08-07_timetable_live_verification_run.md) の Step 3 から。フィード配送項目の注記は解消済みとして読み替え)

## 4. 実機検証への影響

- 出かけるコマ・スペル移動でフィード施設に入った直後の Pulse/ラウンドで、移動先の様子+未読記事が知覚に入るはず (これが本改修の眼目 — 検証手順 Step 3 のフィード配送項目を「到着時に読める」で見直せる)
- 会話中にペルソナがスペルで移動した場合、移動先の様子が**同じ応答の続き**に反映される (旧: 次の Pulse まで盲目)
- 作業セッションの各ラウンド頭でも知覚が流れ込むようになった — 作業中に届いた通知が翌ラウンドで見える (挙動変化として観察対象)
