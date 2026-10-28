# Issue: 起動時の Playbook 同期が、説明書きの約束と違って DB で編集した同名の Playbook をファイル版で上書きする

**ステータス**: 🔲 未着手（未解決 — どちらが正しい約束かが未裁定）
**優先度**: medium（ユーザーやペルソナが編集した Playbook が、気づかれないうちにファイル版へ戻っている可能性がある）
**作成日**: 2026-10-28
**関連**: `saiverse/playbook_sync.py`（`sync_playbooks_from_files`）、`builtin_data/tools/save_playbook.py`、`api/routes/world.py`（`update_playbook` / `/playbooks/import`）、`frontend/src/components/settings/WorldEditor.tsx`、[system_notice_delivery.md](../intent/system_notice_delivery.md)（発見の経緯）

## 背景

`saiverse/playbook_sync.py` のモジュールの説明書きは、次のように約束している。

> save_playbook ツール経由で DB を直接編集した場合は source_file / source_hash がクリアされるため、次回起動時にファイル版で上書きされない（設計上のユーザー優先）。

実装はこの約束どおりに動いていない。`sync_playbooks_from_files` は、ファイル版の Playbook ごとに同じ名前の DB の行を探し、次の順に判定する。

1. 行が無ければ、ファイル版を新しく取り込む。
2. `existing.source_hash != info["hash"]`（DB の行の `source_hash` とファイルの内容のハッシュが違う）なら、ファイル版の中身で行を上書きする。
3. 内容が同じで `source_file` だけが違えば、`source_file` を付け替える。
4. どれでもなければ何もしない。

`builtin_data/tools/save_playbook.py` は、既存の行を編集するときに `source_file` と `source_hash` を `None` にする。`None` はファイルのハッシュと一致しないので、判定 2 に当たる。つまり、ファイル版と同じ名前の Playbook を `save_playbook` で編集すると、次の起動でファイル版の中身に戻される。戻した後の行には `source_hash` が入るので、それ以降は判定 4 で何もしない（戻ったまま残る）。

画面の世界編集（`WorldEditor.tsx`）は `api/routes/world.py` の `PUT /api/world/playbooks/{playbook_id}`（`update_playbook`）と `POST /api/world/playbooks/import` で行を書き換える。この二つは `source_file` と `source_hash` に触らない。だから、画面で編集した同名の Playbook は、ファイル版の中身が変わらない間は判定 4 で残るが、SAIVerse の更新などでファイル版の中身が変わった最初の起動で、判定 2 によってファイル版で上書きされる。

ファイル版の無い名前の Playbook（`save_playbook` や画面で新しく作ったもの）は、どの判定にも当たらないので影響を受けない。孤児の削除（`_prune_orphan_playbooks`）も `source_file IS NULL` の行を対象にしないので、この件とは別。

## 影響

- ペルソナが `save_playbook` で直した組み込みの Playbook（同じ名前のファイル版がある Playbook）は、次の起動で黙ってファイル版に戻っている可能性がある。ログには `playbook_sync: updated '<名前>' from <パス>` が INFO で出るだけで、画面には何も出ない。
- ユーザーが画面で直した組み込みの Playbook は、SAIVerse の更新でその Playbook のファイルが変わった最初の起動で、黙ってファイル版に戻る。
- 実際にどの Playbook が戻されたかは、まだ調べていない。
- システム通知の一本化（[system_notice_delivery.md](../intent/system_notice_delivery.md)）で、組み込みの Playbook 7 本のファイル版が変わった（building_move・create_building・document_create・document_search・generate_image・novel_writing・schedule_management。MEMORIZE を role=system・タグ無しの本文にした）。このため、利用者が世界編集で変えていた同じ名前の Playbook は、この改修を含む版の最初の起動で、判定 2 によってファイル版で上書きされる。これがこの食い違いが実害になる最初の例になる。食い違いそのものは未裁定のまま残す。一方で、上位層（user_data / expansion_data）のファイルや DB だけにある Playbook が旧形式のシステム通知を作り続ける場合は自動で書き換えず、起動時に画面の警告（`saiverse/playbook_sync.py` の `playbook_compat_alerts`）で更新を促す。

## 未裁定のこと

次のどちらが正しい約束なのかが決まっていない。

- 説明書きの約束（DB で編集したらファイル版で上書きしない — ユーザー優先）が正しく、実装が間違っている。
- 実装の挙動（ファイル版が正 — 組み込みの Playbook の更新を確実に届ける）が正しく、説明書きが間違っている。

どちらに決めるかで、`save_playbook` と画面の編集の二つの経路の扱いを揃える必要がある（今は二つの経路で `source_hash` の扱いが違う）。

## ログ

- 2026-10-28: システム通知の一本化（[system_notice_delivery.md](../intent/system_notice_delivery.md)）の第 4 束の調査で、説明書きと実装の食い違いが分かった（この改修では Playbook の MEMORIZE を role=system に変えたファイル版を DB へ届ける必要がある）。この issue はその事実の記録で、解決は未着手。
