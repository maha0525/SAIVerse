# 追跡ファイルに手元の変更があると更新が拒否され、ユーザーに出口が無い

**発見**: 2026-09-03 (macOS ユーザーの不具合報告。直接の引き金は未追跡の `.DS_Store` と診断スクリプトで、これは v0.3.6 で検査対象から外した。追跡ファイルの変更で拒否される側は残る)
**状態**: 🔲 未解決 — 設計が要る (強制更新の経路を別案件として設計する)
**深刻度**: P2 — 該当ユーザーは update.sh / update.bat / アプリ内ボタンのどれからも更新できず、git の知識なしには進めない

## 事象

2026-07 のセキュリティ監査第二陣 (`cc2712e8`) 以降、更新エンジン (`scripts/update_engine.py` の `assert_git_update_ready`) は stash も reset も一切行わない。**追跡ファイル**が手元で変更されていると「Working tree has local changes」で更新を止め、update.sh / update.bat とアプリ内の更新ボタン (`POST /api/system/update`、HTTP 409) の全部が同じ場所で止まる。

追跡ファイルが変わる原因はユーザーの手編集だけではない:

- 設定ファイルなどを手で書き換えた
- 改行コードの揺れ (Windows の `core.autocrlf` などで、中身を触っていなくても `git status` に出る)
- SAIVerse 自身がリポジトリ内に書き込むもの

止まったあとの案内は「commit or otherwise resolve them explicitly」だけで、git を知らないユーザーには何をすればよいか分からない。v0.2 系の更新スクリプトは `git stash push --include-untracked` してから再試行していたので、当時は少なくとも先へ進めた (その stash 案内が壊れていた経緯は [v0229_update_bat_truncates_after_git_pull](v0229_update_bat_truncates_after_git_pull.md))。

2026-09-03 のまはー裁定: 未追跡ファイルは早送り (fast-forward) を邪魔しないので、v0.3.6 で検査対象から外した (`--untracked-files=no`)。新リビジョンが追加するパスと衝突する未追跡ファイルは merge 自身が拒否する。無視対象 (`.gitignore` に載っているもの) は git が既定で黙って上書きするため、同時に `git pull` を `git fetch` + `git merge --ff-only --no-overwrite-ignore` に変えて、こちらも拒否させるようにした (Codex レビューの指摘。この穴は v0.3.5 以前から開いていた)。追跡ファイルの変更は引き続き拒否し、拒否メッセージに該当パスを最大 20 件まで列挙するようにした。出口の設計は本 issue で扱う。

## 直し方の方向

強制更新の経路を、通常の更新とは別の入口として設計する:

1. 手元の変更をラベル付きの名前で stash する (`git stash push -m "saiverse-update-<日時>"` のような、後から見つけられる名前)
2. pull 以降の通常の更新手順を実行する
3. 完了時に「手元の変更は stash `<名前>` に退避した」と明示し、復元の手順か復元ボタンを提示する

黙って stash する旧挙動には戻さない (監査で退けた理由 = ユーザーの作業を本人の知らないところで動かすこと)。強制更新はユーザーが明示的に選ぶ操作であり、何が退避されたかを本人が読める形で残すことが条件。

## 関連

- `scripts/update_engine.py` (`assert_git_update_ready`) / `api/routes/system.py` (409 の返却) / `frontend/src/app/page.tsx` (`handleTriggerUpdate` のトースト表示)
- [update_entrances_lack_process_lock](update_entrances_lack_process_lock.md) (同じ更新エンジンの別の未解決)
- [v0229_update_bat_truncates_after_git_pull](v0229_update_bat_truncates_after_git_pull.md) (v0.2 系の stash 案内の経緯)
