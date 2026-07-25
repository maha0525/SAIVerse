# 編纂の run 境界が「fold の先頭 id」依存で、除外タグ 1 件で消える (偽の隣接)

**ステータス**: 未解決 (2026-07-25 発見。`chronicle_eviction` の実装レビューで、サブエージェントが `plan_alignment` を直接実行して確認)
**深刻度**: P1 — experience_structure §4-5 違反。**ペルソナの記憶に嘘の時系列が入る**
**⚠️ 本設計より前から在る**: 二段構えの退場計画 (2026-07-25) が入れた欠陥ではない。ただし今回の変更は 1 計画あたりの fold 数を増やす方向に働くので、露出は増える
**関連**: `sea/session_lifecycle.py` の `run_boundary_ids` ・ `sai_memory/arasuji/alignment.py` の `plan_alignment` / `_flush_pending` ・ [`../intent/experience_structure.md`](../intent/experience_structure.md) §4-5

---

## 何が起きるか

提示コンテキストの**途中**を畳めるようになったので、離れた位置に複数の fold が立つ。この二つを一つのあらすじに束ねてはいけない — 間に未畳みの生ログがあるのに、その前後が地続きに語られてしまうから (§4-5 偽の隣接 = 時系列の嘘)。

そのために編纂側へ「ここで切れ」という境界を渡している。渡し方が**各 fold の先頭メッセージの id** になっている。

```python
run_boundary_ids = {group[0] for group in compile_groups if group}
```

ところが編纂対象のメッセージ列は Chronicle 除外タグ (`handy_tool` / `spell` / `event_message` / `session_digest`) を落としたあとの集合。**fold の先頭が除外タグだと、その id は編纂対象に存在しないので、境界判定が一度も真にならない。**

境界が消えると、離れた二つの fold が一つの run に混ざり、末尾の端数吸収で**一つのあらすじに束ねられる**。

## 確認済みの実測

`plan_alignment` を直接実行した結果。fold1 = `[A0, A1]`、間に未畳みの生ログ、fold2 = `[E0(除外タグ), C0]`。

```
境界として渡された id: {'E0', 'A0'}
 → chunk batch ['A0', 'A1', 'C0']     ← A と C が同じあらすじ (間に生ログがあるのに)

対照 (fold2 の先頭が除外タグでない場合):
 → chunk batch ['A0', 'A1']
 → chunk identity ['C0']              ← 正しく分かれる
```

## テストがこの穴を通す理由

`tests/test_metabolism_two_layer.py` の `test_compile_groups_do_not_bundle_across_holes` は、**fold の先頭が編纂対象に残っているケースしか見ていない**。先頭が除外タグで消える場合を通さないので、緑のまま素通りする。

## 直す方向 (未検討)

境界を「先頭 id」で表すのをやめる。fold のどのメッセージに属するかで切れば、先頭が消えても境界は残る。

- 各 fold の**全 message id の集合**を渡し、「属する集合が変わったら切る」で判定する
- あるいは境界を id ではなく時刻範囲で渡す

どちらも `plan_alignment` の引数の形が変わるので、呼び出し側と回帰テストを揃えて直す必要がある。テストは「先頭が除外タグの fold」を必ず含める形に足す。
