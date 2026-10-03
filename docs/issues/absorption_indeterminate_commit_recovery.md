# 吸収の付け替えで「commit は確定したのに例外が返る」不確定失敗の復元漏れ

**発見**: 2026-09-01 (Codex 最終確認一巡。まはー承認の止め線「high かつ実害の新種以外は issue 送り」の適用第 1 号 — 直前に根治した commit 失敗復元の族の最外周の角で、新種ではないと裁定)
**状態**: 🟣 検証待ち — 確定済み設計の修正と隔離 SQLite の回帰テストを実装。レビュー待ち。本番履歴の操作・補修は行っていない
**深刻度**: P3 — 実害の形は根治済みの Q2 と同じ (Fragment が撤去済みエントリを指す / バッチ帰属の NULL 落ち)。窓は Q2 よりさらに狭い

## 事象

`run_absorption` フェーズ 1 (sai_memory/arasuji/absorption.py) は、`_repoint_fragments` / `_repoint_batches` が **commit して正常復帰した後**に移動記録 (`moved_fragments` / `moved_batches`) へ積む。commit が更新を確定させた後に例外を返す不確定な失敗では、記録が積まれないため巻き戻し (先行 rollback は確定済みには効かない・条件付き復元は対象を知らない) が届かず、取り下げだけが走って旧帰属が失われる。

## 修正の形 (確定)

付け替え**対象の Fragment id / バッチ id を commit の前に「試行中」として記録**し、フェーズ 1 の失敗経路では (rollback の後に) その全対象を**条件付き UPDATE (現帰属 = 新 id のときだけ旧へ戻す)** で復元してから撤去する。条件付き復元は冪等なので、commit が実は確定していなかった場合にも安全。テストは「実 commit 後に例外を返す」代理 conn で Fragment・バッチ両方の旧帰属維持を固定する。regenerate_entry (storage.py) 側の同型も同時に。

## 守る全体と責任分界

生ログと知覚バッチから新しい Chronicle を生成し、既存の Fragment / バッチを付け替えてから旧 Chronicle を消す、という旅全体が対象。**この差し替えが試行した対象**について、失敗時も利用者が既存の知識を辿れ、消費済み知覚が未使用へ戻らないことを守る。帰属の正典は `memory.db` の各参照列で、復元は差し替えを所有する吸収・再生成の可逆フェーズが受け持つ。表示側で壊れた参照を隠したり、起動時の履歴修復へ押し出したりしない。

対象 ID と旧帰属を先に控え、前向きの UPDATE もその ID に限定する。失敗時は pending な書き込みを rollback し、現在も新 id を指す対象だけを旧へ戻した後、新 entry を撤去する。未確定・復元済みの対象への再適用は no-op になり、他の書き手が別 entry へ動かした帰属は取り戻さない。

本件は 1 操作 1 commit の既存構造を維持する。試験は付け替え commit が一度だけ例外を返し、その後の rollback / 条件付き復元 / 撤去が実行できる場合を固定する。**復元そのものの UPDATE / commit も失敗し続ける場合は未解決**で、既存の best-effort 処理は撤去へ進むため対象の参照を失い得る。旧 entry 削除後の確定フェーズの失敗も変更しない。schema migration や既存ユーザー履歴への遡及処理は不要。

### 隣接する既存制約: 試行対象外の参照が新 entry に加わる競合

新 entry は生成時点で保存済みなので「未公開だから他の書き手には見えない」とは扱えない。吸収の `_withdraw` は親・統合済み・本文変更だけを採用の形跡として見るが、Fragment / バッチの追加参照は調べない。再生成の `_withdraw_replacement` にはその採用ガード自体がない。吸収の `db_lock` は共有する writer を直列化するだけで、CLI 等の別接続・別プロセスの書き込みをこの窓全体から排除しない。

そのため、復元対象ではない行が並行操作で新 entry に付いた場合、`delete_entry_and_update_parent` の通常撤去が持つ制約は残る:

- 新 entry を指す**対象外バッチ**は、`unmark_batches_annexed` が全件を NULL に戻す。旧 entry へ誤って奪わないことは本件で守るが、その新帰属自体を維持するわけではない。
- 新 entry を指す**対象外 Fragment**は撤去が触らないため、消えた新 entry への参照が残り得る (`chronicle_entry_id` に削除連動の外部キーもない)。

ここは削除・公開・採用判定の既存の競合制約として別途扱い、本件では作り直さない。「撤去前後の参照整合」「参照残留ゼロ」は、復元が実行できる場合の**記録済み試行対象**に限る。

## 経緯・検証 (2026-10-02)

- 旧状態は「未解決・形は確定・稀なため v0.3 は塞がない」。独立バックログとして着手し、レビュー待ちへ進めた。
- 修正前の実 SQLite 再現で、commit 確定後例外による吸収側の Fragment 参照残留・バッチ NULL 落ち、および吸収/再生成側の無条件 Fragment 復元による並行帰属の上書きを確認。
- 吸収は全旧 entry の試行対象を commit 前に控え、再生成は Fragment と知覚バッチの復元を ID + 現帰属の条件付き UPDATE に統一。`reassign_batches_annexed` は後方互換の `batch_ids` 指定を追加し、前向きの対象と復元対象を一致させた。
- 新規 `tests/test_arasuji_commit_recovery.py` は一時ディレクトリの実 SQLite を使用。Fragment / バッチそれぞれの commit 前失敗と「実 commit 後に例外」を、吸収・再生成の両方で再現。吸収は旧 entry が 2 個ある途中失敗も対象とした。
- 別 SQLite 接続で復元直前に帰属を変更する競合、条件付き復元の冪等性と ID の限定、対象外バッチを旧 entry へ奪わないこと、撤去に入る前の試行対象の帰属回復と撤去後の**試行対象の**参照残留ゼロを固定。生成部分だけを fake に置き換え、DB 更新・commit・rollback・entry 削除は本物を通した。
- 対象外バッチの試験は「帰属を旧 entry へ奪わず、通常撤去で NULL に戻る」までを確認する。追加の隔離 DB 確認では、復元直前に別接続で対象外 Fragment を新 entry へ追加すると、吸収・再生成とも新 entry が撤去され、その Fragment の参照が残ることを確認した。これは上記の残存制約の確認であり、修正済みとは扱わない。
- 新規回帰 25 件を含め、`test_arasuji*.py` / `test_perception*.py` / Chronicle 予算 / SAIMemory storage / eviction plan の関連回帰 **477 件が通過**。対象 ID を限定しても、生成中に材料バッチが増えた場合は既存の中止動作を維持する。変更 Python の `ruff check`、`git diff --check`、`scripts/check_in_flight.py` も通過 (台帳の既存経過措置警告はそのまま)。
- 本番 DB・ペルソナ・LLM/API は使用せず、既存ユーザー履歴の修復も行っていない。実ディスク故障の誘発や本番運転の確認は未実施で、検証済みなのは commit 結果が不明な場合の永続化境界から撤去までの旅。

## 関連

- [arasuji_tiny_run_absorption](archive/arasuji_tiny_run_absorption.md) — 本体。Codex 十二巡 + ローカル 1 巡の消し込み記録と受容残余
- [arasuji_levels.md §7-9](../intent/arasuji_levels.md#7-9-差し替え失敗でも知識と知覚の帰属を保つ) — 帰属維持の不変条件
- `sai_memory/arasuji/absorption.py` フェーズ 1 / `sai_memory/arasuji/storage.py` regenerate_entry


## 追加レビュー: 前向き付け替えの取り残し (2026-10-03)

- 技術原因: 前向き UPDATE を記録済み ID へ絞った後、SELECT と UPDATE の間に別接続が旧 entry に追加した Fragment を成功経路が見落とした。判断の誤り: 復元対象の厳密さだけを優先し、旧削除へ進める条件を再検証しなかった。検出漏れの条件: 並行 writer のテストが失敗後の復元だけを対象にしていた。
- 吸収・再生成とも、UPDATE が持つ書き込みトランザクション内で commit 前に旧 entry の参照残留を検査する。残っていれば例外にして既存の rollback → 条件付き復元 → replacement 取り下げへ戻し、旧 entry は残す。対象外の行を勝手に移さない。
- 別 SQLite 接続が SELECT 完了直後に Fragment を追加する再現を、吸収の一つ目・二つ目の旧 entry と再生成の計 3 件で修正前に確認。既存の commit 確定後例外復元も維持する。
- 成功側の完全性と失敗側の復元範囲を対で検査する型は、ファイル置換と接続・ツール登録の入れ替えにも適用できる。これらの別領域を本 PR で変更・検証したとは扱わない。
- 元からの「最初の SELECT が空の経路」や付け替え commit 後から旧削除までの並行 writer、継続的な復元失敗、replacement への対象外参照の制約は再設計しない。今回防ぐのは、非空の試行対象 SELECT → ID 限定 UPDATE の窓で新たに生じた取り残し。

- 追加修正後の関連回帰 **480 passed** (arasuji / perception / storage 427 件、予算解決 / eviction 53 件)。ruff・台帳検査・diff check 合格。生成のみ fake、SQLite 更新・commit・復元・撤去は実処理。Windows / 本番データは未検証。
