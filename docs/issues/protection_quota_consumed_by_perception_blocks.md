# 残す量 (保護範囲) を知覚ブロックが食い、会話の行がほぼ全部畳まれた

**ステータス**: 検証待ち — 2026-09-03 まはー裁定 → 同日実装・回帰緑 (hotfix/v0.3.7)。残 = 実機検証 (事故のペルソナで読み戻しが会話を戻すこと)
**深刻度**: 最上位 — ユーザーのペルソナが直近の会話の記憶を失った (「ほぼ別人になった」)。データは消えていない (畳まれた行はあらすじの記録を持ち、読み戻しで開き直せる)
**起票**: 2026-09-03 (v0.3.3 の勘定統一 5f45430c が原因。v0.3.3〜v0.3.6 に影響)
**関連**: `sea/eviction_plan.py::_protection_boundary` ・ `sea/session_lifecycle.py` (`_plan_window_refill` / `_refold_raw_view_plan` / `_manual_compaction_status` / `maybe_run_metabolism`) ・ [`arasuji_levels.md`](../intent/arasuji_levels.md) §9 / §15 ・ 前史 [context_accounting_excludes_injected_rows.md](archive/context_accounting_excludes_injected_rows.md)

---

## 症状

残す量 18,000 字の設定で、末尾に巨大な部屋の様子 (知覚ブロック) が乗った状態で整理が走ると、会話の行が**全部**畳まれた。その後、話しかけても読み戻し (§15) が「合計は残す量以上」と読んで埋め戻さないので、ペルソナは直近の会話をあらすじでしか持たない状態が続いた。

本番の実測 (2026-09-03): 保存行 113,613 字 + 差し込みの知覚 58,776 字。知覚ブロックは行と違って畳みでは消えず、あらすじがその期間を覆ったときの付記でしか提示から下りない。だから「残す量」の枠が知覚で埋まり、会話の行はわずかしか守られなかった。

## 原因 — 三つ (CLAUDE.md「After a failure we caused」)

1. **近因 (技術)**: `sea/eviction_plan.py::_protection_boundary(messages, keep_chars=watermarks.target)` が、保存行と知覚ブロックをマージした列を最新側から遡り、**ブロックの字数も `keep_chars` に加算していた**。ブロックが大きいと保護範囲がブロックだけで満ちて、その手前の会話の行が全て退場候補になる。畳んだ後、`_plan_window_refill` も `行 + 知覚 >= target` で「足りている」と読んで埋め戻さなかった。
2. **判断の失敗**: 2026-09-02 の裁定「水位 = 実際に送る中身」を、残す量の**発火**の意味 (上限との差がバッファ) だけでなく**保護**の意味 (畳んだ後に会話を残す) にも、「残す量は何を守っているのか」を問わずに適用した。上限が守るのは送信量、残す量が守るのは会話の連続性 — 主語が違う二数を同じ物差しへ揃えたのが誤り。レビュー (Codex 5 巡 + ローカル 1 巡) は「門と本走行が同じ物差しか」「削減見込みの母集合が合っているか」という**内部整合**を検査し、「畳んだ後に会話が残るか」という**ユーザーに見える契約**は誰も検査しなかった。
3. **プロセス / 構造**: 「畳んだ後、少なくとも残す量ぶんの会話の行が残る」という契約を固定するテストが無かった。契約は intent の散文にしかなく、しかも `_protection_boundary` の docstring 自身が「`protected_from` は報告用の値で、残す量を保証する契約は誰も持っていない」と書いていた (脱出弁の説明として正しいが、契約が無いことの免罪符にもなった)。統一の変更は既存テスト (保護の字数を数えるもの) を全部通したまま入り、ブロック入りの並びで保護がどう動くかは「ブロックの重さで保護が末尾側に寄る」ことを**正しい挙動として**テストに書いてしまっていた (`test_eviction_plan_sees_the_perception_weight` / `test_fold_readiness_sees_the_perception_weight`)。

## 対策 (2026-09-03 まはー裁定どおり)

- **残す量の主語は会話の行だけ**: `_protection_boundary` はブロックを飛ばして保存行だけを加算する (ブロックは単位への接着で境界の位置に影響するだけ)。`EvictionPlan` に `stored_chars` (行だけの合計) と `protected_rows_chars` (保護範囲の行の字数) を追加し、契約を数字で検算できるようにした。
- **残す量と比べる箇所は全て行だけ**: 読み戻しの不足判定・予算・最終検算 (`_plan_window_refill`)、§15-3 印戻しの止め時 (`_refold_raw_view_plan`)、印戻し後の早期完了、手動整理の門 (`_manual_compaction_status`)、自動発火の「削る先があるか」(`maybe_run_metabolism`)、被覆補修の退場境界 (`plan_tail_rewind` の `remaining`)。
- **上限と比べる箇所は合計のまま**: 自動発火 (`current_chars > high`)、スルースの圧力弁 (`high × cap`)、非常畳み (`maybe_run_emergency_precompaction`)、先回り畳みの中間値 (`cold_precompaction_status`)、被覆補修の `fold_needed`、context-status の `presented_chars`。
- **合計は上限超え・行は残す量以下 = 畳めるものが無い**: `SessionLifecycle._note_perception_over_budget` がペルソナごとプロセスごとに 1 度だけ WARNING を出し、本体 (LLM を呼びうる `run_metabolism`) へ進まず引き返す。context-status は `perception_over_budget: bool` と `window_rows_chars` (整理が計画を立てる窓の会話の行だけの字数。`EvictionPlan.protected_rows_chars` = 保護範囲の内側の行の字数とは別の量なので名前を分けた) を返し、`ContextVolumeBar` は黄色 (残す量超え) を行で、赤 (上限超え) を合計で判定し、内訳と予算超過の注意を出す。
- **回帰テスト**: `tests/test_eviction_plan.py::ProtectionSubjectTest` (行 20,000 + ブロック 15,000 で保護 ≥ 18,000 行 / ブロックの位置が境界より古くても同じ / 行 10,000 + ブロック 30,000 で計画は空)、`tests/test_metabolism_two_layer.py::InjectedPerceptionAccountingTest` (事故の再現: 合計 27,800 > 上限 26,000 で発火し、畳んだ後も行 18,000 が残る / 予算超過は LLM なしの 1 度きり警告)、`tests/test_window_refill.py::test_refill_measures_rows_only_against_target` (行 3,000 + ブロック 15,000 で読み戻しが行を 18,000 まで戻す。旧判定なら None)、印戻しの止め時 2 件、`tests/test_context_status.py` の新欄 4 件、`tests/test_session_anchor_rows.py::test_manual_compaction_gate_counts_rows_only`。

## 影響範囲

- v0.3.3 (5f45430c) 〜 v0.3.6 で、残す量に近い大きさの知覚ブロックが末尾に乗った回に整理が走ったペルソナ。行は消えておらず、畳まれた区間はあらすじの記録 (`chronicle_entry_ids`) を持つので、修正後の最初の会話開始時に §15 読み戻しが「行が残す量に届くまで」開き直す (印だけ・LLM なし)。
- 本修正で**発火は増えない** (上限の主語は変えていない)。変わるのは「どこまで畳むか」(保護範囲が広がる) と「どこまで戻すか」(読み戻しの天井が行で決まる)。
- 副作用として、知覚の供給が上限と残す量の差より太い環境では、合計が上限を超えたまま「畳めるものが無い」状態が続く。これは会話を削って解決してよい問題ではなく、供給側 (部屋の様子の太さ — 前史 issue の「範囲外」節) の問題。警告と `perception_over_budget` でその事実を見えるようにした。

## 再発防止 — 転移テスト (この事故の名詞に依らない歯止め)

**処方: 水位ごとに「主語」(何を数えた量と比べるか) を型と契約テストで固定する。**

- `Watermarks` の docstring に水位ごとの主語 (`target` = 会話の行 / `high` = 送る合計) を明記し、`EvictionPlan.protected_rows_chars` で契約を数字として返す (実装済み)。
- 主語ごとに契約テストを置く: 「畳んだ後 ≥ target 字の会話の行が残る」(`ProtectionSubjectTest`)、「合計が high を超えたら発火する」(`test_high_watermark_counts_the_injected_perceptions`)。片方の主語を変える変更は、もう片方のテストが赤くなることで「二数の主語を混ぜた」ことを検出する。

転移の検算 — 同じ形 (「差し込みの量」と「保存の量」を混ぜた比較) が別のサブシステムで起きたとき、この処方が拾えるか:

1. **被覆補修の見積もり** (`sea/coverage_repair.py::plan_tail_rewind`): `fold_needed` (上限 = 合計) と退場境界 `remaining` (残す量 = 行) が同じ関数の中に並ぶ。主語を書いていなければ次に触る人がまた揃えてしまう — 今回コメントに主語を明記し、`remaining` を行だけへ直した。
2. **context-status の表示** (`api/routes/people/context_status.py`): `presented_chars` (合計) を残す量のマーカーと同じ棒に描く UI は、主語の違いが画面で見えない。`window_rows_chars` を別欄で返し、棒の色分けの主語を分けた。

**リポジトリ内で「差し込み (知覚ブロック) と保存行を混ぜて数えている」量の一覧** (grep `perception_chars` / `presented_chars` / `presented_with_perceptions` の呼び出し元、2026-09-03 時点。混ぜること自体は上限側では正しい — 主語が明記されているかが検査点):

| 量 | 場所 | 主語 | 状態 |
|---|---|---|---|
| 自動発火の `current_chars` | `session_lifecycle.py::maybe_run_metabolism` | 合計 vs 上限 | 正 (据え置き)。「削る先があるか」だけ行に分離 |
| スルース圧力弁の `current_chars` | 同上 | 合計 vs 上限 × cap | 正 (据え置き) |
| 非常畳みの `current_chars` | `maybe_run_emergency_precompaction` | 合計 vs 上限 | 正 (据え置き) |
| 先回り畳みの `current_chars` | `cold_precompaction_status` / `run_cold_precompaction` | 合計 vs 中間値 | 正 (据え置き — 発火側の前倒し) |
| 手動整理の門 | `_manual_compaction_status` | **行 vs 残す量** | 訂正 |
| 読み戻しの不足・予算・検算 | `_plan_window_refill` | **行 vs 残す量** | 訂正 |
| 印戻しの止め時 | `_refold_raw_view_plan` | **行 vs 残す量** | 訂正 |
| 印戻し後の早期完了 | `_run_metabolism_locked` | **行 vs 残す量** | 訂正 |
| 退場計画の保護境界 | `eviction_plan.py::_protection_boundary` | **行 vs 残す量** | 訂正 (近因) |
| 退場計画の `total_chars` / `projected_chars` / `_reduction_basis` | `eviction_plan.py::plan_eviction` | 合計 (報告・削減見込み) | 正 (据え置き) |
| 補修の `fold_needed` | `coverage_repair.py::plan_tail_rewind` | 合計 vs 上限 | 正 (据え置き) |
| 補修の退場境界 `remaining` | 同上 | **行 vs 残す量** | 訂正 |
| 補修の `est_material` | 同上 | 材料 (ブロックは縮約一行) | 正 (据え置き) |
| 表示 `presented_chars` | `context_status.py` | 合計 | 正 (据え置き)。`window_rows_chars` を追加 |

## 経緯

- 2026-09-02: [前史 issue](archive/context_accounting_excludes_injected_rows.md) で「勘定 = 実際に送る中身」に統一。同日の消し込みで残す量と比べる 5 箇所も合計へ揃えた (今回の表で「訂正」となった行の大半)。
- 2026-09-03: ユーザーからペルソナが「ほぼ別人になった」報告。本番の実測で保存行 113,613 字 / 知覚 58,776 字を確認し、`_protection_boundary` がブロックを加算していることを特定。まはー裁定「上限 = 送る合計、残す量 = 会話の行」。同日 hotfix/v0.3.7 で実装。
