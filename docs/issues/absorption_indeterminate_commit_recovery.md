# 吸収の付け替えで「commit は確定したのに例外が返る」不確定失敗の復元漏れ

**発見**: 2026-09-01 (Codex 最終確認一巡。まはー承認の止め線「high かつ実害の新種以外は issue 送り」の適用第 1 号 — 直前に根治した commit 失敗復元の族の最外周の角で、新種ではないと裁定)
**状態**: 🔲 未解決 — 修正の形は確定済み (下記)。発生条件が極めて稀 (commit がディスクへ確定した後に I/O エラー等で例外だけ返る) なため v0.3 は塞がない
**深刻度**: P3 — 実害の形は根治済みの Q2 と同じ (Fragment が撤去済みエントリを指す / バッチ帰属の NULL 落ち)。窓は Q2 よりさらに狭い

## 事象

`run_absorption` フェーズ 1 (sai_memory/arasuji/absorption.py) は、`_repoint_fragments` / `_repoint_batches` が **commit して正常復帰した後**に移動記録 (`moved_fragments` / `moved_batches`) へ積む。commit が更新を確定させた後に例外を返す不確定な失敗では、記録が積まれないため巻き戻し (先行 rollback は確定済みには効かない・条件付き復元は対象を知らない) が届かず、取り下げだけが走って旧帰属が失われる。

## 修正の形 (確定)

付け替え**対象の Fragment id / バッチ id を commit の前に「試行中」として記録**し、フェーズ 1 の失敗経路では (rollback の後に) その全対象を**条件付き UPDATE (現帰属 = 新 id のときだけ旧へ戻す)** で復元してから撤去する。条件付き復元は冪等なので、commit が実は確定していなかった場合にも安全。テストは「実 commit 後に例外を返す」代理 conn で Fragment・バッチ両方の旧帰属維持を固定する。regenerate_entry (storage.py) 側の同型も同時に。

## 関連

- [arasuji_tiny_run_absorption](arasuji_tiny_run_absorption.md) — 本体。Codex 十二巡 + ローカル 1 巡の消し込み記録と受容残余
- `sai_memory/arasuji/absorption.py` フェーズ 1 / `sai_memory/arasuji/storage.py` regenerate_entry
