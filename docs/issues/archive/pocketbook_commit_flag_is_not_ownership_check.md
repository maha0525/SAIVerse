# 手帳まわりの `commit` 引数が「トランザクションの所有」を検査していない

**状態**: ✅ 完了 (2026-08-22 実装・回帰テスト済み)。裁定どおり所有判定をヘルパへ集約した。

## 実装 (2026-08-22)

判定を `pocketbook.owns_transaction(conn, commit)` 一つに集約し、`pocketbook.py` の書き込み関数すべて (`add_activity` / `get_or_create_activity` / `rename_activity` / `close_activity` / `add_memo`) が**最初の execute より前**に一度だけ取った結果を、成功時の commit と失敗時の rollback の両方で使う形に揃えた。`recall_edges.add_chunk_page_edge` にも同じ形を当てた。`continuity.add_thread_edge` は既に所有判定を持つので触っていない。

裁定の範囲に無かったが同じ形だったので一緒に直したもの: `init_pocketbook_tables` と `recall_edges.init_chunk_page_edge_tables` の末尾の確定。前者は `commit` の旗すら持たず無条件に確定していた (断り方が無いぶん悪い)。

ヘルパの置き場を `pocketbook.py` にしたのは、同モジュールの `validate_epoch` を `recall_edges` / `continuity` が既に import している前例に合わせたため。

回帰テスト (`tests/test_pocketbook_and_edges.py::TestTransactionOwnership`): ①既定のまま呼んでも呼び出し元の未確定分を確定させない (rollback で全部消える)、②`IntegrityError` の収束経路でも呼び出し元の束を巻き戻さない、③辺の記帳も同じ規則。修正を外すと 4 件落ちることを確認した (④は既存テストの契約変更ぶん)。

**既存テストの契約を一つ書き換えた**: `test_add_memo_idem_hit_with_commit_true_commits_the_transaction` は「commit=True なら呼び手の未確定分も一緒に確定する」を仕様として固定していた (この issue の ⚠ 節が指摘した考え方そのもの)。所有判定の導入で契約が変わったため、`test_add_memo_idem_hit_does_not_confirm_callers_transaction` へ置き換え、ロック残りを見る側は `test_add_memo_idem_hit_leaves_no_lock_when_it_owns_the_transaction` として所有する場合に限定した。

**同じ形が残っている隣**: `sai_memory/arasuji/storage.py` (2 箇所) と `sai_memory/memopedia/storage.py` (4 箇所) が同じ「`commit=True` 既定 + 所有を検査しない `if commit:`」を持つ。裁定の範囲外なので今回は触らず、[別 issue](../storage_writers_commit_flag_is_not_ownership_check.md) へ切り出した (同日の Codex 横断走査も独立に同じ 6 箇所を指摘)。

## 裁定 (2026-08-22、まはー + Fable)

**直す。やり方は選択肢 B と D を一緒にやる形** — 「自分がトランザクションの持ち主か」の判定を一つの小さなヘルパに切り出し、`pocketbook.py` の書き込み関数すべてに同じ形で当てる。判定の中身は同じモジュールの `get_or_create_activity` が既に持っている形 (`commit and not conn.in_transaction`、**最初の execute より前**に一度だけ取る) をそのまま写す。commit 側と rollback 側の両方がその判定を使う。

**C (誤用を例外で落とす) と既定値の反転は採らない。** 持ち主の判定が正しくなれば、トランザクションの中から既定 (`commit=True`) のまま呼んでも「持ち主ではないので確定も巻き戻しもしない」となり、外側の持ち主が後で確定する。つまり誤用が誤用でなくなるので、落とす理由が消える。既定値の反転は呼び出し元を全部書き換える割に得るものがない。

**実装先**: 掃討フェーズの Opus セッション (作業指示は [裁定ハンドオフ](../handoff/2026-08-22_v3_shape_layer_rulings_handoff.md))。

関連: [`sai_memory/memory/pocketbook.py`](../../sai_memory/memory/pocketbook.py) / [`sai_memory/memory/recall_edges.py`](../../sai_memory/memory/recall_edges.py) / [`sai_memory/memory/continuity.py`](../../sai_memory/memory/continuity.py)
出自: v0.3 形の層の掃討フェーズ、ローカル LLM レビュー 束 1 の指摘 2 (2026-08-22)。同レビューの指摘 1 (辺の削除が `OperationalError` を種類で分けずに握る) は原因が一意だったため修正済み。

## 何が起きうるか

手帳まわりの書き込み関数は `commit=True` を既定にしていて、これは「**この関数がトランザクションを所有し、自分で確定させる**」の意味で使われている。ところが**呼び出し元が既にトランザクションを開いているかを検査していない**。

もし開いている状態で既定のまま呼ぶと、その `conn.commit()` は**呼び出し元の未確定の書き込みまで巻き込んで確定させる**。その後で呼び出し元が失敗して `rollback()` しても、巻き込まれた分は既に確定済みなので巻き戻せない。中途半端な状態が永続する。

## 現時点で踏む経路は無い (実際に確認した)

`add_activity` / `add_memo` / `add_chunk_page_edge` の呼び出し元を全部当たった結果、**トランザクションの中から呼ぶ 3 箇所は、いずれも明示的に `commit=False` を渡していた**。

- `sai_memory/memory/entity_extractor.py:701` — `BEGIN IMMEDIATE` の中で `commit=False`
- `sea/sluice.py:725` — 呼び出し元が `commit` / `rollback` を持ち `commit=False`
- `saiverse/v3_shape_migration.py:356` — 同上
- `sai_memory/memory/pocketbook.py:318` — `get_or_create_activity` が自分で束を管理し `commit=False`

つまり現状は正しく使われている。**故障ではなく、次に呼び出しを足す人が既定のまま呼ぶと踏む罠**である。

## 意図が「所有の旗」である根拠と、非対称

同じモジュールの二つの関数は、**所有を実際に検査している**。

- `pocketbook.py:305` (`get_or_create_activity`): `manage_txn = commit and not conn.in_transaction`
- `continuity.py:164` (`add_thread_edge`): `manage_txn = not conn.in_transaction`

さらに `add_activity` のコメント自身が `commit=True` を「(本関数所有)」と書いている。つまり**所有の概念は既にあり、検査する形も既にモジュール内に存在する**のに、一部の関数だけがそれを持っていない。

## ⚠ 素朴な一行修正は、直す前より悪くなる

レビューの提案は「`if commit:` を `if commit and not conn.in_transaction:` にする」だったが、**そのまま当ててはいけない**。

`add_activity` は `commit` を**三箇所**で所有の旗として使っている。

| 行 | 用途 |
|---|---|
| 233 | 成功時の `conn.commit()` |
| 241 | `IntegrityError` 時の `conn.rollback()` |
| 255 | その他の例外時の `conn.rollback()` |

commit 側だけに条件を足すと、**失敗時に呼び出し元のトランザクションを巻き戻す経路が残る**。巻き込んで確定させるより、巻き込んで捨てる方が実害が大きい。`add_memo` も同じ三点構造を持つ。

正しくは、所有の判定を**最初の `execute` より前**に一度だけ取り (実行すると暗黙にトランザクションが開き、以後 `in_transaction` は呼び出し元の有無に関わらず True になる)、commit と rollback の両方でその判定を使う。

## 直す範囲が広い

`pocketbook.py` だけで `if commit:` の形が 10 箇所以上あり、レビューが名指しした 2 関数以外にも同じ構造がある。2 関数だけ直すと、同一モジュール内に「検査する関数」と「しない関数」が混在したまま残る (CLAUDE.md「リファクタは完遂するか戻すか、中途半端な状態で残さない」に触れる)。

## 選択肢 (未裁定)

- **A: 何もしない。** 現に踏んでいる経路は無く、呼び出し規約 (トランザクション中は `commit=False`) は既に守られている。次に足す人が守ればよい
- **B: 所有の判定へ揃える。** モジュール全体の `if commit:` を `owns_txn` (最初の execute より前に確定) へ置き換える。既存の兄弟関数と同じ形になる。黙って何もしないので、誤用に気づく機会は無いまま
- **C: 誤用を例外で落とす。** 所有していないのに `commit=True` で呼ばれたら `ValueError`。誤用がその場で分かるが、既定が `commit=True` なので呼び出し規約の変更になる (既定を `False` へ倒す案とセットで検討する価値がある)
- **D: 所有の判定を 1 つのヘルパか context manager に切り出す。** 各関数が判定を書き写すのをやめる。B/C のどちらを採るにせよ、書き写しが 10 箇所あること自体が非対称の供給源なので、これが根に近い

## 判断の材料

- `commit=True` が既定である限り、「トランザクション中に既定で呼ぶ」誤用は書けてしまう。入口で塞ぐ (C や既定の反転) 方が、規約を文書で守らせるより強い
- 一方で、既定を変えると既存の呼び出し全部を見直すことになる
