# 更新の入口 (update.bat / API detached / 起動時自己回復) にプロセス間ロックが無い

**発見**: 2026-09-01 (起動時自己回復の Codex 確認一巡。起動時の入口追加で既存クラスが一段広がったため起票 — 確認一巡の止め線裁定により issue 送り)
**状態**: 🔲 未解決 — 既存クラス (update.bat 同士の並走も従来から未防御)。設計が要るため v0.3 は塞がない
**深刻度**: P3 — 引き金は二重起動というユーザー操作で、git/pip/npm の各段は個別にはロバスト。同時に走ると理論上、片方の成功印がもう片方の中途状態を「完了」と誤認しうる

## 事象

更新エンジン (`scripts/update_engine.py`) には排他がなく、`update.bat` / API の detached 更新 / 起動時自己回復 (`start.bat` / `start.sh` の `--check-complete` → `--manual`) が同時に git pull・pip・npm ci・完了印の書き込みを実行しうる。

## 直し方の方向

全入口が共有するロックファイル (取得失敗 = 「別の更新が進行中です」で明確に停止。Windows の stale lock の回収込み) を update_engine 内に置き、check〜manual 完了を一つの所有者に限定する。ローカルレビューのラッパー (`local-claude.sh` の mkdir ロック + 先客待ち) が同型の先例。

## 関連

- `scripts/update_engine.py` / `start.bat` / `start.sh` / [v0229_update_bat_truncates_after_git_pull (自己回復の経緯)](v0229_update_bat_truncates_after_git_pull.md)
