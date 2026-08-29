# 手動整理の保留が「別の整理が処理中」と誤表示され、案内表からも漏れて「予期しないエラー」に落ちる

**状態**: ✅ 解決済み (2026-08-24 起票・同日修正 → 2026-08-29 実機合格)。起点の凍結の修正後、まはーが Memory 窓の「生成」を一押しで再実行し、採取から畳みまで保留なしで通って一次あらすじ 1 本 (材料 10,685 字の被覆) が確定した。

関連: [`sea/session_lifecycle.py`](../../sea/session_lifecycle.py) `run_metabolism` の戻り値 / [`api/routes/people/arasuji.py`](../../api/routes/people/arasuji.py) の deferred 写像 / [`frontend/src/components/memory/ArasujiViewer.tsx`](../../frontend/src/components/memory/ArasujiViewer.tsx) の案内表
出自: 2026-08-24 実機検証 (v0.3 チェックリスト 5 番)。まはーが Memory 窓の「生成」を押したら「別の整理が同じ範囲を処理中または処理済みです」+「予期しないエラーが発生しました」。

## 実際に起きていたこと (訂正済みの診断)

手動生成は**ほぼ全部成功していた**。Chronicle の畳み (7 通、entry f0417eac) が確定し、スルースも新 schema の本番初成功 (数字ループなし・35 秒・コア記憶 1 追加・手帳メモ 1 追加)。保留されたのは最後の退場の適用だけで、その機構は:

1. 整理の実行頭で、関所の照合に使う窓を撮る (このとき起点は古い 3b29bc72、提示規則通過で 106 行)。
2. 実行中に Chronicle の畳みが確定し、最前線が進む。
3. スルースのプロンプト組成 (`_prepare_context`) の中で、冷えた起点の保守経路 (機構 1) が起点を新しい最前線 596d1f01 まで前進させる (パンマーカー頭打ちの範囲内 — 前進で窓から出た行は Chronicle 被覆済み + 過去スルース通過済みで、不変条件は保たれる)。
4. スルースは前進後の起点から 98 行を見る。関所は実行頭の窓 (106 行) と突き合わせるので、頭の 8 行 (いま畳まれた分) が「未見」になり、退場を安全側で見送る。

これは「起点がスルースの組成中に前進して、退場計画の土台とスルース入力がズレる」既知の型 (Codex 第三巡で名指し済み) で、`tests/test_sluice.py::test_cold_run_anchor_advance_blocks_eviction` がこの見送りを仕様として、`test_cold_run_head_gap_recovers_on_next_metabolism` が次回の整理で続きから進むことを固定している。**放置後の手動生成 (冷えた起点 + 新しい畳みが出る回) では一度だけ起き、詰みはしない。** しかも実害はほぼ無い — 起点の前進自体が当該 8 行の退役を果たしており、採取も編纂も確定済み。

欠陥だったのは表示の二段だけ:

- 手動経路 (`arasuji.py`) が保留の理由を区別せず、全部「別の整理が同じ範囲を処理中または処理済みです」(claim 競合用の文面) に写していた。
- その理由コードが `ArasujiViewer.tsx` の案内表に無く、「予期しないエラー」の汎用文に落ちていた。

## 誤診の記録 (2026-08-24、メティス)

起票時の診断「関所が提示に載らない行 (discardable / sub_line volatile) を数えて永久保留になる」は**誤りだった**。関所の窓 (`get_presented_window`) は最初から提示と同じ規則 (`main_line` + `committed`、実体は `_payload_passes_context_filter` の一本) で濾されており、私が数えた「窓 102 行」は生の DB 行を、しかも前進後の起点から数えた誤照合。まはーはこの誤診の上で「提示に載らない行は経験に含めない」と裁定したが、その内容は現行コードが既に満たしていた (裁定の実質は維持されており、覆すものはない)。

- 近因: 二つの集合 (窓と seen) の差分を欠陥と断定する前に、**両辺の組成コードを読んでいなかった** (関所の判定関数は読んだが、窓を作る側を読まずに DB の生行数で代用した)。
- 判断の失敗: 差分 4 行の「種類」がもっともらしい物語 (提示に載らない行) を作れたため、そこで照合を閉じた。ログにあった `cold anchor advanced` の行 (答えそのもの) を見落とした。
- 通ってしまった条件: 診断のままま裁定を仰ぎ、実装フェーズの検分 (委譲文の「規則は実装から導く」) が最初の反証点になった。裁定の前に反証を試みる段が無かった。

実装エージェントに指示した「提示の除外規則を推測で書かず実装から導く」が誤診を検出した。教訓は memory `feedback_read_both_sides_before_calling_a_diff_a_defect` に一般化して記録。

## 直したもの (2026-08-24)

- `run_metabolism` の戻り値で保留理由を運ぶ: スルースが読めていない範囲による退場見送りは `"deferred_sluice_unseen"` (従来の `"deferred"` はキャンセル・claim 競合のまま)。呼び出し元・docstring・既存テスト 3 スイートを追従。
- `arasuji.py` の生成ジョブ写像に `deferred_sluice_unseen` → `error_code="sluice_unseen"` + 正直な文面「今回の採取で読めていない範囲があったため、畳みは次回の整理で続きから進みます」。読めていない範囲は末尾の新着とは限らない (今回の実機は窓の頭側) ので「新しい会話」と断定しない (Codex レビュー 2026-08-24 で当初文面の嘘を指摘され訂正)。文面は「もう一度実行すると整理できます」と**約束しない** — 起点前進で超過が畳み単位を割った回は、再実行が「畳めません」と正しく断るため ([[feedback_promises_in_messages_are_contracts]] の適用)。
- `ArasujiViewer.tsx` の案内表に `window_claimed` / `sluice_unseen` を追加 (「予期しないエラー」への落下を解消)。`PersonaMenu.tsx` の organize-memory 側も追従。
- 回帰: `tests/test_arasuji_generation_status_mapping.py` (写像 3 件・新規、文面が「新しい会話」と断定しないことも固定) + 既存 unseen 系の戻り値更新。歯止めを外すと対応テストが落ちることを実測済み。

## 根本修理済み (2026-08-24) — 起点の凍結

まはー裁定: **「一回の整理 (Metabolism) は一つの一貫した窓で最後まで走る」** — 見送り + 次回続行という動き自体を欠陥と認定し、根本修理した。表示修正 (上の節、別コミット) が「見送りを正直に報告する」だったのに対し、こちらは「その見送り自体を起こさなくする」。

- `run_metabolism` は実行頭に撮った窓の起点 (`window.anchor_id`) を `run_sluice` → `_prepare_context` の新引数 `pinned_anchor_id` として渡す。凍結された組成は `resolve_metabolism_anchor` を呼ばない — §14-2 (機構 1) の起点前進は**判定ごと**走らないので、実行中に Chronicle が確定して最前線が動いても、退場計画の土台とスルース入力は同じ窓のまま。既存の `persist_anchor_advance=False` (前進を計算するが永続化しない、keepalive 用) とは別の意味論。
- **fail-closed**: 凍結起点で履歴が組めないときは `PinnedAnchorUnavailableError` (sea/runtime_context.py) を送出し、通常解決へフォールバックしない — フォールバックはこの競合を静かに再導入する穴になる。送出は退場停止に写像され、次回の Metabolism が再試行する。
- キャッシュにも順方向: 前回の会話 prefix は凍結する起点で組まれているので、実行中の前進 (prefix を変えてキャッシュを壊す方向だった) を止めることはキャッシュヒットを守る。
- **末尾の新着への安全弁は残る**: 関所 (`_marker_advance_is_safe` / `_eviction_within_seen`) は従来どおりで、記録済み結果の再適用時に窓が新着で伸びていれば退場は見送られる (`test_reapply_does_not_evict_unseen_tail`)。凍結したのは起点 (頭側) だけ。
- 仕様テストの入れ替え: 旧「見送りが正しい」を固定していた `test_cold_run_anchor_advance_blocks_eviction` / `test_cold_run_head_gap_recovers_on_next_metabolism` は、`test_pinned_window_evicts_in_one_run` (一発退場 + seen が実行頭の窓の全行を覆う) / `test_pinned_anchor_unavailable_fails_closed` に置き換え。凍結組成の fail-closed 契約は `PinnedHistoryCompositionTest` が直接固定する。

## 検証

- 回帰テスト: 上記 + `tests/test_sluice.py` / `tests/test_session_anchor_rows.py` / `tests/test_metabolism_two_layer.py` で 201 passed。
- 実機 (まはー): 「生成」を再度使ったとき、見送りの回は新しい文面が出ること。採取分 (コア記憶・手帳メモ) がタブに見えていること。
