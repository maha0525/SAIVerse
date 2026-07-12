# 概念再編（Memory Atlas）コードレビュー

**実施日**: 2026-07-12  
**対象**: `98aeb77` の次（intent 起草 `079808e`）から `HEAD` (`a4a2a83`) までのうち、`docs/intent/concept_consolidation.md` に対応する P1〜P4 実装  
**結論**: 要修正。編纂（P4-a）の実行経路に、実運用を止める契約不一致が1件、本文保存則に反する実装が1件、失敗時に部分変更を残す非原子性が1件ある。P1〜P3、P4-0/c/b/d については、このレビューで新たなブロッカーは確認できなかった。

## Findings

### [P1] 過小ページの `fold` は承認しても必ず失敗する

- 場所: `saiverse/curation.py:237`, `sai_memory/curation_ops.py:671`
- 事実: `_detect_undersized()` は `refs: [label]`（過小ページ1件だけ）を候補に入れる。一方、`run_pending_plans()` は `fold` を `merge` と同じ扱いにし、`refs` が2件未満なら `ValueError` にする。その後の実行契約も `refs[0] = survivor`, `refs[1] = absorbed` である。
- 影響: 就寝裁定で過小ページ整理を approve すると、プランは enqueue されるが睡眠中バッチで必ず failed になる。親への統合は一度も成功しない。
- 修正案: 検知時に実親の ref を含む `refs: [parent_label, label]` を生成する。あわせて「検知→enqueue→run_pending_plans」の統合テストを追加し、fold の survivor が親、absorbed が過小ページであることを固定する。

### [P1] 分割の「文字レベル本文保存則」が実装されていない

- 場所: `sai_memory/curation_ops.py:225`, `sai_memory/curation_ops.py:487`
- 事実: `_split_into_blocks()` は各ブロックへ `strip()` を適用し、2個以上の改行を `join("\n\n")` で再構成する。先頭・末尾空白、空白だけの行、3個以上の連続改行は失われる。保存検査は本文ではなくブロック番号の重複・欠落だけを検査している。
- 最小再現: `"  A  \n\n\nB  \n"` は分割・再結合後に `"A\n\nB"` となり、元本文と一致しない。
- 影響: intent が絶対条件としている「子ページ全部＋親の残り＝元の本文（文字レベル一致）」を満たさない。編纂承認により、意味の薄いように見える空白も含めて原文が不可逆に書き換わる。
- 修正案: 区切りを含む lossless block（本文スライスまたは `(text, separator)`）を作り、割当後の全出力から元本文を機械的に復元して完全一致を検査する。番号集合の検査は補助として残す。

### [P1] merge/split が原子的でなく、失敗報告と実状態が食い違う

- 場所: `sai_memory/curation_ops.py:344-359`, `sai_memory/curation_ops.py:503-532`, `sai_memory/memopedia/storage.py` の各 CRUD commit
- 事実: merge は「子の付け替え→survivor 更新→metadata 更新→absorbed soft-delete」、split は「子ページを複数作成→親更新」を順に行うが、呼び出すストレージ操作が各段階で `commit()` する。途中で例外が起きても `run_pending_plans()` は rollback せず plan を failed にするだけである。
- 影響: たとえば split の2枚目作成や最後の親更新が失敗すると、作成済み子ページだけが残る。merge でも子の付け替えや本文結合だけが確定しうる。それにもかかわらず翌朝報告は「ページは変更されていません」と述べるため、監督情報も事実と食い違う。
- 修正案: 編纂操作用に commit しない storage primitive を用意し、1プランを単一トランザクションで実行して成功時のみ commit、失敗時は rollback する。少なくとも各段階へ例外注入するテストを追加し、失敗後のページ・親子関係・履歴が実行前と同一であることを検証する。

## 検証記録

- 関連テスト10ファイル: 初回 **279 passed / 2 errors**。2 errors は `tmp_path` が既定の `%LOCALAPPDATA%\Temp\pytest-of-shuhe` を作れない `PermissionError` だった。workspace 内の一時ディレクトリを指定して該当2件を再実行し **2 passed**。したがって関連テストは実質 **281 passed**、assertion failure なし。
- `_split_into_blocks()` の空白喪失は独立した最小再現で確認済み。
- 作業ツリーに先在した別件変更（`docs/intent/stackchan_vessel.md`、モデルJSON群、`docs/handoff/2026-07-10_issue_audit.md`）はレビュー・編集対象外とした。

## 推奨修正順

1. fold の refs 契約を修正し統合テストを追加する。
2. merge/split を1プラン1トランザクションにする。
3. split を lossless block 化し、文字列完全一致テストを追加する。
4. 関連テストと全体 pytest、変更Pythonへの `ruff check` を通してから実機検証へ戻す。
