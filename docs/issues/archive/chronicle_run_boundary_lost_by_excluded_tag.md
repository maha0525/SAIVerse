# 編纂の run 境界が「fold の先頭 id」依存で、除外タグ 1 件で消える (偽の隣接)

## 状態

✅ **解決 (2026-07-27)**。境界の表し方を「fold の先頭 id」から「所属 fold」へ変え、
先頭が編纂対象から落ちても境界が立つようにした。

- `sai_memory/arasuji/alignment.py`: `plan_alignment` の引数を
  `run_boundary_ids: Set[str]` (境界になる id) から
  `run_groups: Sequence[Sequence[str]]` (fold ごとの全 message id) へ交代。
  run 分割は各メッセージ**自身の所属 fold** が変わったところで切る。
- `sea/session_lifecycle.py`: `compile_groups` をそのまま `run_groups` に渡す
  (先頭 id を抜き出す加工が消えた)。
- 回帰: `tests/test_arasuji_alignment.py::TestRunSplitting`
  (`test_run_group_boundary_survives_missing_head` ほか 2 本 — 純関数の層で
  「群の先頭が `messages` に居ない」形を固定) と
  `tests/test_metabolism_two_layer.py::test_compile_groups_boundary_survives_excluded_head`
  (実 SAIMemory に `spell` タグのメッセージを置き、実フィルタが落とす経路を通す)。
  どちらも旧ロジックに戻すと赤くなることを確認済み。

**発見の記録 (2026-07-25、`chronicle_eviction` の実装レビューでサブエージェントが
`plan_alignment` を直接実行して確認)**
**深刻度**: P1 — experience_structure §4-5 違反。**ペルソナの記憶に嘘の時系列が入る**
**⚠️ 本設計より前から在る**: 二段構えの退場計画 (2026-07-25) が入れた欠陥ではない。ただし今回の変更は 1 計画あたりの fold 数を増やす方向に働くので、露出は増える
**関連**: `sea/session_lifecycle.py` の `run_groups` ・ `sai_memory/arasuji/alignment.py` の `plan_alignment` / `_flush_pending` ・ [`../../intent/experience_structure.md`](../../intent/experience_structure.md) §4-5

---

## 何が起きるか

提示コンテキストの**途中**を畳めるようになったので、離れた位置に複数の fold が立つ。この二つを一つのあらすじに束ねてはいけない — 間に未畳みの生ログがあるのに、その前後が地続きに語られてしまうから (§4-5 偽の隣接 = 時系列の嘘)。

そのために編纂側へ「ここで切れ」という境界を渡している。渡し方が**各 fold の先頭メッセージの id** になっていた。

```python
run_boundary_ids = {group[0] for group in compile_groups if group}
```

ところが編纂対象のメッセージ列は Chronicle 除外タグ (`handy_tool` / `spell` / `event_message` / `session_digest`) を落としたあとの集合。**fold の先頭が除外タグだと、その id は編纂対象に存在しないので、境界判定が一度も真にならない。**

境界が消えると、離れた二つの fold が一つの run に混ざり、末尾の端数吸収で**一つのあらすじに束ねられる**。

同じ穴は除外タグ以外でも開く。編纂対象から落ちる条件は `get_messages_for_chronicle`
が持っており、除外 `line_role` (`sub_line` / `meta_judgment` / `nested`) と Stelis
スレッドも同じように先頭を消しうる。**修正は「特定のタグ」ではなく「先頭が残って
いることに依存しない」形で入れた** (どのフィルタで落ちても境界は所属側に残る)。

## 確認済みの実測 (修正前)

`plan_alignment` を直接実行した結果。fold1 = `[A0, A1]`、間に未畳みの生ログ、fold2 = `[E0(除外タグ), C0]`。

```
境界として渡された id: {'E0', 'A0'}
 → chunk batch ['A0', 'A1', 'C0']     ← A と C が同じあらすじ (間に生ログがあるのに)

対照 (fold2 の先頭が除外タグでない場合):
 → chunk batch ['A0', 'A1']
 → chunk identity ['C0']              ← 正しく分かれる
```

## テストがこの穴を通していた理由

`tests/test_metabolism_two_layer.py` の `test_compile_groups_do_not_bundle_across_holes` は、**fold の先頭が編纂対象に残っているケースしか見ていなかった**。先頭が除外タグで消える場合を通さないので、緑のまま素通りしていた。上記の回帰 2 本はこの形を必ず含める。
