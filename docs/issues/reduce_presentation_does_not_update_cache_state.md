# `reduce_presentation` が単発実行されたとき、`SessionAnchor` のキャッシュ状態が更新されない

**起票**: 2026-11-29 (PR #474 の `develop` 追従精査で確認)
**状態**: 未着手（全体的な造語・コメント整理のあとに対応予定）
**優先度**: low（発生条件は圧縮対象なし・レート制限クールダウン中などの限られた経路のみ。課金や送信には影響せず、チャットオプションのキャッシュタイマー表示や TTL 判定が実態より長めに残るだけ）
**関連**: PR #474、`sea/session_lifecycle.py` の `reduce_presentation` / `cache_broken_stamp`、`docs/intent/cache_lifecycle_control.md` §5.2

## 現象

PR #474 で「プロンプト内容を書き換える経路は、同じ書き込みで `SessionAnchor` の `(TTL_SECONDS, UPDATED_AT)` も更新する（キャッシュ無効化または新規設定）」というルールを導入した。

一方、PR #474 の分岐後に `develop` へ追加された `SessionLifecycle.reduce_presentation`（`sea/session_lifecycle.py`）は、`DynamicStateManager.on_metabolism` による `head` の再生成と、過去の部屋の様子・処理済み操作通知の短縮／除外（`_reduce_presented_perceptions`）を行う。

通常の会話圧縮とセットで動く経路では `save_folded_ranges` が先に `cache_broken_stamp()` を書いているため問題ないが、`require_pending=True` で `reduce_presentation` だけが単発実行される以下の 5 経路で実際に短縮・`head` 再生成が行われた場合は、プロンプトの先頭内容が変わって既存キャッシュが効かなくなるにもかかわらず、`SessionAnchor` の `(TTL_SECONDS, UPDATED_AT)` がそのまま残る。

1. `_note_perception_over_budget`（知覚データのみで上限超過したときの事前チェック）
2. `check_and_run_metabolism` のレート制限（HTTP 429）クールダウン中
3. `maybe_run_emergency_compaction` のレート制限（HTTP 429）クールダウン中
4. `_run_metabolism_core` の圧縮対象なし（`plan.is_empty`）分岐
5. `_run_metabolism_core` の圧縮スキップ分岐

## 修正時の注意点（なぜ PR #474 に混ぜなかったか）

上記の 3・4 の経路は、**返答直前に走る Pulse 内の処理**で、直前に履歴の復元（`maybe_run_window_refill`）が実行されている可能性がある。

ここで無条件に `cache_broken_stamp()`（`UPDATED_AT` を過去時刻にして期限切れ扱いにする）を書き込むと、直後のコンテキスト構築で `resolve_metabolism_anchor` が「キャッシュ期限切れ」と判定し、アンカーの自動前進（§14-2）によって復元したばかりの会話範囲を同じ Pulse の中で圧縮し直してしまうリスクがある（`_refold_raw_view_folds` に `preserve_active_window=True` が必要なのと同じ構造）。

## 対応方針の候補（未決）

- 実際に `head` 再生成または知覚短縮が発生した回（変更なし・スキップを除く）にだけ `SessionAnchor` を更新する。
- Pulse 外（返答後・手動実行・定期監視ワーカー）からの呼び出しは `cache_broken_stamp()` で失効扱いにし、返答直前（`maybe_run_emergency_compaction` 経由）は有効期限内の行なら `cache_fresh_stamp()` で現行設定の TTL と現在時刻へ更新する（`_refold_raw_view_folds` と同じ扱い）。
- 併せて、復元直後の緊急圧縮経路で自動前進と衝突しないことをテストで固定する。
