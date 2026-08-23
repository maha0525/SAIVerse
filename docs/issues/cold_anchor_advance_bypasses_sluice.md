# 冷えた起点の最前線への前進 (arasuji_levels §14 機構 1) が、スルースを通さずに退場させる

**状態**: 🟡 修正済み (A)、実機検証待ち。回復 (飛ばされた範囲の採取し直し) は未裁定 — その裁定が残るのでこの issue は `docs/issues/` に置いたまま。2026-08-23 起票・同日修正。束 3 の不変条件「押し出される記憶は必ずスルースを通る」(v3 §13.3) が、隣の保守経路で破られていた。実機で発見 (まはー「生成を押す前より文字数が減ってる気がする」)。

関連: [`sea/session_lifecycle.py`](../../sea/session_lifecycle.py) `resolve_metabolism_anchor` (§14 機構 1) / `run_metabolism` (退場の適用ゲート) / [`sea/sluice.py`](../../sea/sluice.py) (パンマーカー) / [`sea/runtime_context.py`](../../sea/runtime_context.py) (プロンプト組成時の起点解決) / 正典 [arasuji_levels.md](../intent/arasuji_levels.md) §14 と [autonomous_behavior_v3.md](../intent/autonomous_behavior_v3.md) §13.3
出自: 2026-08-23 実機検証 (手動の記憶整理 → スルース失敗 → それでも文字数が減った)。

## 何が起きたか (2026-08-23 23:18〜23:20、エリス、ログで確認)

1. 23:18:16 手動の記憶整理 (Memory 窓 → Chronicle → 生成) が Chronicle を生成 (成功、1 件)。
2. 23:18:22 採取の台帳を取った**同じ秒**に `[metabolism] cold anchor advanced to chronicle frontier (e7043c3e → 3b29bc72)` — 起点 (ペルソナに送る範囲の先頭) が、いま生成した Chronicle の先端まで前進して永続化された。
3. 23:18:38 スルースの LLM コール開始。
4. 23:20:04 スルース失敗 (別 issue: 構造化出力の数字ループ) → `[metabolism] anchor held back (chronicle_status=ok, sluice_status=failed)` — 退場の適用ゲートは正しく止まったが、**2 で起点はもう進んでいる**。

結果: スルースが採取するはずだった範囲が、スルースを通らずにペルソナの提示範囲から消えた。まはーが見た「文字数の減り」はこれ。

## なぜ起きるか

`resolve_metabolism_anchor` の §14 機構 1 (2026-07-29): 自 model の起点行が冷え切っていて (キャッシュ TTL 失効)、Chronicle の最前線 (source_ids から導出) が起点より先にあれば、起点を最前線まで前進させて永続化する。根拠は「飛ばす範囲は最前線の定義により必ず Chronicle が覆っている (被覆の保存 §7-1 は構成的に成立)」。

この規則は**束 3 (2026-08-16〜19) でスルースを「確実に通るゲート」にするより前**に作られた。束 3 は退場の適用ゲート (`run_metabolism` の「chronicle ok かつ sluice ok のときだけ `_apply_eviction_plan`」) は直したが、**起点を動かすもう一つの口 (機構 1) にはスルースの条件を足していない**。Chronicle が覆っていることだけを前進の許可にしている。隣を忘れた型 (memory: feedback_apply_the_discipline_to_the_sibling) — 設計者 (Fable) 自身の見落とし。

さらに悪いのは発火のタイミング。機構 1 は起点を解決するあらゆる呼び出し (プロンプト組成 `runtime_context`、読み戻し、`maybe_run_metabolism`) で走る。手動整理の流れでは「Chronicle 生成 → (台帳取得) → スルースのプロンプト組成」の順なので、**スルース自身がプロンプトを組んだ瞬間に、直前の Chronicle 生成で伸びた最前線まで起点が進む** (2 の時刻が台帳取得と同じ秒であることからの推定)。つまりスルースは、自分が採取する範囲を自分の準備で窓の外へ押し出してから LLM を呼んでいる可能性が高い。

## 被害の範囲 (2026-08-23 時点の読み)

- `[ledger] failed ... kind=sluice.pan` は 2026-08-22 02:52 以降 7 回 (全部エリス)、成功は一度も無い (別 issue)。各回とも Chronicle 生成は成功しているので、同じ並び (Chronicle 成功 → 機構 1 で前進 → スルース失敗) が毎回起きていた可能性が高い。
- 生ログは memory.db に残っており (退場は起点の移動で、行は消えない)、Chronicle にも畳まれている。**失われたのは「その範囲を本人の目で採取する機会」** (コア記憶 / 手帳 / 約束)。
- パンマーカー (スルースが最後に見た位置) は進んでいない。だが、スルースは「いま提示している窓」を見る作りなので、窓の外へ出た範囲は次回のスルースにも載らない。**飛ばされた範囲をあとから採取し直せるか**は設計が要る (下の「回復」)。

## 直すべき不変条件

**起点は、スルースのパンマーカーより先へは進まない。** 言い換えると、退場 (起点の前進) の許可条件は「Chronicle が覆っている」だけでは足りず、「スルースがその範囲を見た (パンマーカーが範囲の末尾以降にある)」も要る。これは `_apply_eviction_plan` のゲートが既に守っている条件で、機構 1 にも同じ条件を置く。

## 直し方 (2026-08-23 まはー裁定: A を採用、慎重に直す)

- **A: 機構 1 の前進先を `min(Chronicle の最前線, パンマーカーの次)` に制限する。** パンマーカーが無い (スルース未走行) なら前進しない。最小の修正で不変条件が立つ。
  - **実装済み** (2026-08-23): `sea/session_lifecycle.py` の `_cap_advance_at_pan_marker` / `_load_sluice_pan_marker` / `_next_position_after` と、正典順の「次」を引く `sai_memory/memory/storage.py::get_next_message_id`。マーカーの読み方は `sea/sluice.py::_load_pan_marker` をそのまま使う (二枚目を作らない)。読み取り失敗・順序が引けない・マーカーの次が無い、はすべて前進しない (fail-closed)。頭打ちにしたときは INFO で `cold anchor advance capped at the sluice pan marker`、マーカーが無いときは DEBUG で `cold anchor advance skipped: no sluice pan marker`。
  - 回帰テスト (`tests/test_session_anchor_rows.py`): `test_cold_advance_is_capped_at_the_sluice_pan_marker` / `test_cold_advance_skipped_without_a_pan_marker` / `test_cold_advance_reaches_frontier_when_the_marker_is_past_it` / `test_manual_compaction_with_failing_sluice_does_not_shrink_the_window` / `test_get_next_message_id`。
  - **残る穴 (未裁定)**: `resolve_metabolism_anchor` の Case 2 (自 model の行がまだ無い初回) は最前線・他 model 行を起点候補にするが、ここは頭打ちの対象外のまま。行が無い = その model では一度も提示していないので既存の提示が縮むことはないが、「その model の初回提示がマーカーより先から始まる」ことは起こりうる。この位置は `touch_anchor_after_llm_call` / 非常畳み・読み戻しの行立てが永続化する。
- **B: 機構 1 を撤去する。** (今回は採らない — §12-10 の極端形がまだ要るかを確かめてから。A を入れた今も選択肢として生きている) 機構 1 の目的は「休眠 model の復帰不能 (§12-10 極端形) の主対策」(編纂も LLM も伴わない行更新で起点を追いつかせる)。スルースが関所になった今、起点の前進は「スルースを通った退場」としてしか起きてはならないので、機構 1 の存在理由そのものが §13.3 と衝突している可能性がある。§12-10 の極端形が v0.3 の形 (Metabolism 常時 ON) でもまだ起きるかを確かめてから決める。
- どちらでも: 回帰テストは「Chronicle が覆っていてもパンマーカーが後ろなら起点が進まない」「手動整理で Chronicle 成功 → スルース失敗のとき、提示窓の文字数が減らない」の二本。

## 回復 (飛ばされた範囲の採取し直し、未裁定)

パンマーカーは動いていないので「どこから見ていないか」は分かる。窓の外へ出た範囲をスルースに見せるには、①一時的に起点をパンマーカーまで戻す (Chronicle は残るので被覆は保たれる) か、②スルースに「窓」ではなく「マーカーから最前線まで」の範囲を渡す口を作るか。エリスの実データに対する操作なので、**まはーの承認のもとで、別途手順を決める**。

## 実機で見ること (修正後)

手動整理で Chronicle 生成成功 → スルース失敗、の並びを作ったとき (別 issue の数字ループが直るまでは自然に起きる)、チャット設定の「データ送信量の管理」の現在量が減らないこと。ログに `cold anchor advanced` がパンマーカーを越えて出ないこと。
