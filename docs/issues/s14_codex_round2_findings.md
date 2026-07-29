# §14 実装 (92a7b86) への Codex 2巡目指摘 — 消し込み待ち

**状態**: 未解決 (2026-07-29 起票)。§14 実装コミット `92a7b86` はテスト全緑 (3273) だが、Codex 2巡目 (job `review-ms65hqqb-qrtxv0`) が 3 件を指摘。セッション予算切れのため消し込みは次セッション。**1巡目 (`review-ms60fy6w-x22vqr`) の5指摘は並走 pytest による stale スナップショット由来と裏取り済み — 再燃させないこと。**

## 指摘と triage (このセッションでの検討結果)

### 1. [high・正しい・要修正] 冷えた anchor の前進が後続の圧縮区間を全消去する

`resolve_metabolism_anchor` の機構1 は汎用 `upsert_anchor_entry` で anchor を書くため、anchor 変更時の自動クリア (session_lifecycle.py の FOLDED_RANGES_JSON クリア分岐) が発動する。最前線 = 「最初の未編纂メッセージ」なので、**その後方にまだウィンドウ内の編纂済み fold が存在しうる** (未編纂の隙間を跨いで先の episode が畳まれた形)。クリアすると生ログが復活してウィンドウが再膨張し、head の Chronicle 枠との二重提示も起こる。

**修正方針**: 前進時に fold を仕分ける — 新 anchor (最前線) 以降に**全体が**収まる fold は保持、手前の fold は捨てる。位置判定は `compare_message_positions` (fold の先頭 message vs 最前線)。anchor と FOLDED_RANGES_JSON を同一書き込みで更新する専用経路を作る (汎用 upsert の自動クリアに任せない)。回帰テスト: 未編纂の隙間より後方に fold がある状態で snap → fold が生き残ること。

### 2. [high・裁定は「変更不要」寄り、ただし要まはー確認] 非常畳みの失敗を無視してモデル呼び出しへ進む

`run_meta_user` は `maybe_run_emergency_precompaction` の戻り値 (failed/deferred/nothing) を捨てて通常の playbook へ進む。Codex は fail-closed (呼び出し中止) を推奨。

**このセッションの検討**: 高水位超過 ≠ モデル実上限超過 (高水位12万字は多くのモデルの実上限よりずっと下)。畳みに失敗しても呼び出しは成功しうるので、**中止すると「成功したはずの応答」まで潰す**。実上限を超えている場合は進んでも中止でも応答は失敗し、結果は同じ。つまり「進む」が弱優越 — fail-open は見落としではなく選択。ただしこの裁定は intent に書いていないので、まはーの確認を取って §14-6 に明記する (失敗時のユーザー向け通知を足すかも合わせて)。

### 3. [medium・正しい・要修正] cold sweep の発火条件が Beat ロック取得前に陳腐化する

`run_cold_precompaction` の「全 anchor 冷え」判定はロック外。判定後〜`run_metabolism` のロック取得の間にユーザー Pulse が anchor を touch すると、**温まったばかりのキャッシュを畳みで壊す** (§14-4 の中心不変条件の破れ)。

**修正方針**: `run_cold_precompaction` 冒頭で `hold_beat` を自分で取り、**ロック内で** `cold_precompaction_status` を最終判定してから `run_metabolism` (RLock 再入で無害) を呼ぶ。競合テストを追加。

## 次セッションの手順

1. 指摘1と3を修正 (上の方針どおり)、指摘2は §14-6 への裁定明記 (まはー確認後)
2. 対象テスト + フルスイート → Codex 再レビュー (敵対・観点付き・**pytest と並走させない**) → コミット
3. 完了後この issue を archive へ、in_flight の該当行を更新
