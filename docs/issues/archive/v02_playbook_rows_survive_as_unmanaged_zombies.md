# v0.2 時代の Playbook 行が、アップグレード後も「ユーザー作」の顔で残り続ける

**発見**: 2026-08-29 (v0.2.29 → v0.3 実機アップグレード検証、隔離環境)
**状態**: ✅ 解決済み — まはー裁定 (2026-08-29 深夜):「選別せず、バックアップだけ取って完全に新 Playbook に置き換える」。対処候補 1 (世代ハッシュ照合) は却下され、より大胆な全置き換えを採用 (過去リリースの台帳を将来持ち続けなくて済み、規則が一文になる)。ブランチ `feature/playbook-wholesale-replacement` に実装 — UpgradeHandler (dev5→dev6) が全行を `<saiverse_home>/backups/playbooks/` へ JSON 退避 (読み戻し検算つき fail-closed) → 全削除 → 退役名の permission 行を掃除 → 起動時同期が現行一式を再取り込み。削除とバージョン刻印は同一 commit で原子的。ゾンビ世界 (v0.2.29 跳び済み・62 行) と 3 月世界 (二都市・64 行・許可 17 件) の両方で実機合格。Codex 一巡 (5 件、採用 2 / 却下 2 / 報告 1) 消し込み済み。2026-08-30 未明、まはーの判断で `feature/autonomous-behavior-v2` へマージ (fast-forward, `87a2d2ab`) — archive へ移送。
**深刻度**: P2 — 起動と会話は正常に動く。ただし退役した Playbook が全アップグレードユーザーの DB に残留し、ペルソナの選択肢に出続ける

## 実測 (2026-08-29 の隔離アップグレード検証)

v0.2.29 の世界 (builtin Playbook 51 本入り) を v0.3 で起動した結果:

- 名前が現行 `builtin_data/playbooks/public/` に存在する 25 本 → 起動時同期
  (`saiverse/playbook_sync.py`) が名前で照合して現行版へ更新し、出所も刻印した。**正常**。
- 現行にファイルが無い 37 本 (meta_user / meta_auto / sub_router 系 / basic_chat /
  source_* など v0.3 で退役した名前) → **6 月版のまま永久に残留**。

## なぜ残るか

v0.2 時代の取り込み (`scripts/import_all_playbooks.py` 旧版) は `source_file` /
`source_hash` を記録しなかった (列自体が無かった)。v0.3 の起動時同期は
`source_file IS NULL` を「save_playbook でユーザーが作った行 = 保護対象」と
解釈するので、旧 builtin 行がユーザー作と見分けられない。孤児掃除
(ファイルが消えた行の削除) も `source_file IS NOT NULL` の行しか対象にしないため、
どちらの機構にも拾われない。

## 実害

- ペルソナの Playbook 一覧 (`list_available_playbooks` の enum 等) に退役した名前が
  出続け、選択肢を汚染する。実測でも basic_chat が enum に載った。
- 退役 Playbook のグラフは現行の SEA でそのまま動く保証がない (今回のスモークでは
  会話は track_user_conversation を通ったため露見しなかった)。

## 対処の候補 (裁定待ち)

1. **世代ハッシュ照合での掃除** (推奨): 過去リリースタグ (v0.2.x) の builtin
   Playbook JSON の正規化ハッシュ一覧を持ち、`source_file IS NULL` かつ本文ハッシュが
   旧 builtin と一致する行だけを削除する upgrade handler
   (`saiverse/upgrade_handlers.py` の同族)。ユーザーが改変した行はハッシュが
   一致しないので生き残る — 誤爆なし。
2. 名前一覧だけで削除 (ハッシュなし) — ユーザーが同名で作った行を巻き添えにする
   リスクがあり非推奨。
3. 受容 — 残留させ、UI/enum 側で退役名を非表示にする。

## 関連

- [`saiverse/playbook_sync.py`](../../saiverse/playbook_sync.py) — 起動時同期と孤児掃除
- [`saiverse/upgrade_handlers.py`](../../saiverse/upgrade_handlers.py) — 退役名の参照張り替えの先例
- [`2026-08-29_v0229_upgrade_test.md`](../handoff/2026-08-29_v0229_upgrade_test.md) — 検証全体の記録
