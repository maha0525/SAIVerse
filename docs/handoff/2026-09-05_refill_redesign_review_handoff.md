# 引き継ぎ: 読み戻し再設計 — 実装済み・レビュー消し込みの途中 (2026-09-05)

**書き手**: Fable 5 セッション (週間クォータ残 2% のため途中で引き継ぐ)。**受け手**: Opus (または次の Fable)。
**ブランチ**: `hotfix/v0.3.8`。実装は完了しテストは緑、Codex レビュー三巡目まで消化済み。**未コミットの作業ツリー差分がこの案件の本体** — 検収の続きから始めること。

## 現在地 (一文で)

読み戻し (窓の会話文が目標量を下回ったとき、畳んだあらすじを開き直す機構) を 2026-09-05 のまはー裁定どおり全面再実装し、レビュー三巡で本物の指摘 3 件を消し込み、いま最後の 1 件 (覆い照会のチャンク分割) の実装をサブエージェントが書いている。

## 正本の場所

- **設計と裁定**: `docs/issues/refill_reads_by_budget_instead_of_arasuji_unit.md` — §裁定の確定 (9 点) が仕様のすべて。§レビューの残余が全指摘の処置記録 (採用 3 / 却下・受容 4、理由つき)。
- **intent**: `docs/intent/arasuji_levels.md` §15 (新設計へ書き換え済み・未コミット)。
- **7/30 棚卸し** (実装後の宿題): `docs/issues/audit_20260730_review_guards.md` — まはー裁定でスコープは「当時のセッションが触ったコード全部の再検証」。

## コミット状況

- コミット済み (未 push): `d3ae1c43` (裁定の確定 docs) / `f98a26f2` (棚卸しスコープ拡大)。その手前に 9/4 までの未 push 数件あり。push はまはーに言われてから。
- **未コミット (= 検収対象の本体)**: `sea/window_refill.py` (全面書き換え 541→187 行) / `sea/session_lifecycle.py` (`_plan_window_refill` 一本化 + `_read_segment_before` 新設) / `sea/runtime.py` (全 Pulse 化) / `sea/work_session.py` (読み戻し配線) / `sea/runtime_context.py` (コメントのみ) / `sai_memory/arasuji/storage.py` (照会ヘルパ 2 本 + チャンク分割修正が入る予定) / `tests/test_window_refill.py` / `tests/test_work_session.py` / `tests/test_window_floor.py` / `docs/intent/arasuji_levels.md` / `docs/concepts/metabolism.md` / `docs/issues/refill_reads_by_budget_instead_of_arasuji_unit.md` / `docs/overview/in_flight.md`。

## 残りの手順 (この順で)

1. **チャンク分割の検収**: サブエージェント (覆い照会 `get_entries_covering_messages` を 500 件ずつに分割) の完了通知を受けたら、差分を通読し `./.venv/Scripts/python.exe -m pytest tests/test_window_refill.py -n 0` と `ruff check` を自分の手で回す。
2. **Codex 四巡目**: `node C:/Users/shuhe/.claude/plugins/cache/openai-codex/codex/1.0.6/scripts/codex-companion.mjs adversarial-review --wait --scope working-tree "<観点>"` を Bash `run_in_background: true` で。観点には仕様の前提 (残す量=目標量で超過は正常 / 上限超過も WARNING のみ / 場所を問わず新しいあらすじから丸ごと / 予算廃止 / 読めない行は読める行だけで開く) と「§レビューの残余の裁定済み 6 件は再指摘不要」を明記する。
3. **収束判定はまはーに上げる**: 巡ごとの出来高は 一巡目 1 件 → 二巡目 1 件 → 三巡目 1 件 (いずれも本物)。四巡目が境界級のみなら「収束と見る」の見立てを根拠つきでまはーへ。
4. **収束後**: フルスイート (`./.venv/Scripts/python.exe -m pytest`) → コミット (実装 + テスト + docs を一つに。メッセージ末尾に `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`) → in_flight 台帳の行を「検証待ち (まはーの実機)」へ更新し `python scripts/check_in_flight.py` を通す。
5. 実機検証はエリスの本番窓 (壊れた状態が未変更のまま残っている — `rows=24029 target=40000` で読み戻しが `rung 1 is broken` で止まる状態) がそのまま試験台になる。

## まはーの裁定待ち (作業を止めない・聞くタイミングで)

- **最終防衛ライン (床) を作業セッションにも張るか**。今回は読み戻しだけ配線した。私の推しは「張らない」(発話しないセッションに発話見送りの経路は不要。床の unmet は実質 DB 障害の局面で、そこまで壊れていれば文脈の組み立て自体が失敗する)。まはーには質問済み・返答待ち。

## セッション運用の注意 (この案件に効くものだけ)

- **ローカル LLM レビューは回さない** — まはーが環境整備中 (2026-09-05 指示)。「整備が終わった」と言われるまで Codex 直行。
- 実装はサブエージェント委譲 (メインは設計・検収)。委譲プロンプトに「メインの作業ツリーで直接作業。worktree 隔離と再委譲は禁止。コミットはしない」と「着手前に前提を現物で検証」を必ず書く。
- 用語: rung / ladder の直訳「段」「梯子」は禁止 (2026-09-05 まはー裁定)。「あらすじ」の語彙で書く。
- 7/30 棚卸しは**この実装が実機検証まで済んでから**着手。
