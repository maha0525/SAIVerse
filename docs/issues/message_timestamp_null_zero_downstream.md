# 下流に残る created_at の NULL→0 写像

**発見**: 2026-10-03 ([PR #346 のレビュー](https://github.com/maha0525/SAIVerse/pull/346#issuecomment-5964245268)、同日実コードで再確認)
**状態**: 未着手 — 記録のみ。PR #346 では修正せず、台帳にも追加しない。
**深刻度**: P3 — 本番データでは発生条件 0 件 (2026-10-03 時点、下記レビュー報告)。要約・吸収の順序を変えるため、今は実行経路に手を入れない。

## 全体の規則と今回の境界

生ログの正典順は `created_at` の NULL 群が全実時刻より前、NULL 同士・同秒同士は
`rowid` 順 (W8 S7)。保存行 → Python の材料 → 要約・吸収 → 提示・想起を通して、
インポートされた時刻欠落行もこの歴史位置を保つ必要がある。

[比較器の修正](compare_message_positions_null_zero_mapping.md) が揃えたのは
`compare_message_positions` と `session_lifecycle` の呼び手 5 か所の位置判断。
下記の変換・並べ替えは別経路であり、この PR で解消したものには数えない。

## 実コードで確認した箇所

行番号は PR #346 の `19d3cc01` 時点。関数名も検索の手掛かりとして残す。

| 箇所 | 現在の処理と影響 |
|---|---|
| `sai_memory/memory/storage.py:552` `_row_to_message` | DB の NULL を `Message.created_at = 0` に変換する。元の NULL と epoch 0 の区別がこの境界で失われる。直前の「全ての実時刻より前と整合」というコメントも、負の epoch を含む入力では成立しない。 |
| `sai_memory/arasuji/absorption.py:180` `merge_standalone_chunks` | チャンクの先頭の `(created_at or 0, 元の材料列での位置)` で並べ替える。負の epoch が、NULL 由来の 0 より前に出る。 |
| 同 `:476` / `:596` `plan_absorption` | 相乗り時の更新と新規 item 作成で、材料の `created_at or 0` の最小値を `start_at` にする。`:607` の item の処理順にも使う。 |
| 同 `:1376` `run_absorption` | 読み出した `Message` を `created_at or 0` で再ソートしてから一次あらすじ生成へ渡す。 |
| `sai_memory/arasuji/generator.py:354` `_format_messages_for_prompt` | `Message` を `(created_at or 0, 0, seq)` にして、知覚等の追加材料と時刻順に再ソートする。SQL から正典順で届いても負の epoch と NULL の先後が入れ替わる。 |
| `sea/auto_recall.py:926` / `:976` `_vivid_source_messages` / `_vivid_fallback_messages` | `Message` 経由ではなく SQL の行から抜粋タプルを作るときに `created_at or 0` へ写す。ここ自体は再ソートではない。現在の表示側 `_format_vivid_lines` では 0 と NULL 由来の 0 の日付がともに `?` になる。 |
| `sai_memory/perception_buffer.py:1101` `count_batch_records` | メッセージとは別の `perception_buffer` 行を、`int(created_at or 0)` で件数計算用 `PerceptionItem` に写す。ただし SQL の `created_at ASC, id ASC` 順を保ち、`reduce_perceptions` も時刻で再ソートしない。この箇所に同じ順序逆転が起きるとは確認していない。 |

要約・吸収では、NULL と負の epoch が材料に混ざると順序が正典からずれる。
NULL と epoch 0 も同じキーになるため、両者の先後は入力順・副キーに依存する。
一方、想起と知覚の上記箇所で確認したのは NULL/0 の区別を失う変換であり、
要約・吸収と同じ再ソートの不具合とは分けて扱う。

隔離したインメモリ SQLite で、SQL 順が `[NULL, -1, 0]` の 3 行を
`_row_to_message` → `_format_messages_for_prompt` に通すと、材料順が
`[-1, NULL 由来の 0, epoch 0]` になることを確認した (2026-10-03、LLM 呼び出しなし)。
吸収全体・想起・知覚の統合試験を行ったという意味ではない。

## 実データの観測と先送り理由

2026-10-03 のレビュー報告では、本番全ペルソナの `memory.db` 28 個・メッセージ
37,348 行を読み取り専用で数え、`created_at IS NULL` と `created_at <= 0` は
どちらも 0 件だった。これは上記レビューを出典とする観測で、この文書追補では
本番 DB を再照会していない。知覚テーブルの件数を検査したという意味でもない。

確認された本番メッセージには発生条件がない一方、修正は要約・吸収・Memopedia
想起等、ペルソナの記憶を作り提示する経路に及ぶ。比較器の修正へ混ぜず、
実害のない条件のために今その回帰リスクを取らない。移行・再編纂・本番への入力は行わない。

## 将来着手するときの確認

- `Message` 化の時点で失われる NULL の区別をどこが保持するかを決める。下流だけで `canonical_position_key` を呼んでも、既に 0 に写した後では元の NULL は復元できない。
- NULL・負の epoch・0・同秒を混ぜた合成 DB で、保存行から材料の組立て、吸収計画・実行、要約プロンプトまでの順序を確認する。LLM は fake を使い、本番の記憶は使わない。
- 正の epoch の通常データと、同秒の `rowid` 順・知覚材料との合流規則を維持する。想起の表示と知覚の件数計算は、実際の責務に沿った別の確認を行う。

## 関連

- [W8 の正典順序と NULL の裁定記録](../handoff/2026-07-22_w8_time_order_handoff.md)
- [あらすじのレベル制](../intent/arasuji_levels.md) §14 / §16
