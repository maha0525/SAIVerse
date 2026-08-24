# 退場の関所が「提示に載らない行」を数えて、退場が永久に保留になる

**状態**: 🔴 修正中 (2026-08-24 起票、まはー裁定済み)。エリスの実機でスルースの実機検証 (v0.3 チェックリスト 5 番) を止めている。

関連: [`sea/session_lifecycle.py`](../../sea/session_lifecycle.py) `_marker_advance_is_safe` / `_eviction_within_seen` / `run_metabolism` / [`api/routes/people/arasuji.py`](../../api/routes/people/arasuji.py) の deferred 写像 / [`frontend/src/components/memory/ArasujiViewer.tsx`](../../frontend/src/components/memory/ArasujiViewer.tsx) の案内表
出自: 2026-08-24 実機検証。まはーが Memory 窓の「生成」を押したら「別の整理が同じ範囲を処理中または処理済みです」+「予期しないエラーが発生しました」。

## 事実 (ログと DB の突き合わせ、読み取り専用)

- 21:46〜47 の手動生成で、Chronicle の畳み (7 通、entry f0417eac) と**スルース本体は成功した** (新 schema の本番初成功。数字ループなし・35 秒・コア記憶 1 追加・手帳メモ 1 追加)。
- 止まったのは最後の退場の関所。`finalize_ok=False evict_ok=False` で退場だけが保留になった (`[sluice] eviction deferred ... seen=98 ids`)。
- 実行台帳の記録 834f5f8a と エリスの memory.db の突き合わせ: **窓 (anchor 596d1f01 以降) の時間範囲に 102 行、スルースが見たのは 98 行**。差の 4 行の内訳は `(user, main_line, discardable)` × 2 と `(assistant/user, sub_line, volatile)` × 2。
- この 4 種類の行は**そもそも会話の提示に載らない設計** (履歴の読み出し側が `scope='discardable'` を SQL で除外し、sub_line の volatile 行も提示対象外)。スルースの入力は提示そのもの (`runtime._prepare_context` の `presented_message_ids`) なので、これらの行は永遠に「見ていない」。
- 一方、関所の検算 (`_marker_advance_is_safe` / `_eviction_within_seen`) は `window.presented` (生の窓) の**全行**の ID 包含を要求する。→ 提示に載らない行が窓にある限り、何度実行しても同じ保留になる。**押し直しで収束しない**。

## 芯

「見せる係」(提示 = `_prepare_context`) と「検算する係」(退場の関所) が、**別々の窓の定義**を使っている。関所の不変条件「全経験は退場前に一度本人の目を通る」は本人の経験 (main_line・committed の行) のためのもので、設計上一度も見せない裏方の行に適用すると定義上詰む。同じ理由の隣を忘れた型。

## まはー裁定 (2026-08-24)

**「経験」に提示に載らない裏方の行 (volatile / discardable) は含めない。** 関所の照合から除外する — 「まぁ定義通りだね」。

## 手当ての設計

1. **本丸**: 「提示に載る行か」の判定を一つの述語 (共有関数) にして、提示側の実際の除外規則と同じ定義を関所側 (`_marker_advance_is_safe` / `_eviction_within_seen`) が使う。規則を二箇所へ書き写さない — 写すと次にずれるのはまた here。
2. **従 (エラー表示の二段の嘘)**:
   - 手動生成の経路 (`arasuji.py`) が保留の理由を区別せず全部 `window_claimed` (「別の整理が同じ範囲を処理中または処理済みです」) に写している。`run_metabolism` は理由 (`unseen_tail` 等) を知っていて正しい文面も内部にあるのに、手動経路へ届いていない。理由を運んで、それぞれの文面を出す。
   - `ArasujiViewer.tsx` の案内表に `window_claimed` が無く、汎用の「予期しないエラー」に落ちる。理由コードぶんの案内を足す。

## 検証

- 関所の単体テスト両方向: 提示に載らない行 (volatile / discardable) だけが未見なら通る / **提示に載るはずの行** (main_line・committed) が未見なら今までどおり止まる。
- 実機: エリスで「生成」を再実行し、退場まで通ること (まはー)。
