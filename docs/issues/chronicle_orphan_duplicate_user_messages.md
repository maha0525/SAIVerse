# Issue: 重複した user メッセージが記憶に残り、単独発言の Chronicle を生んでいる (データ掃除)

**ステータス**: 🔲 未着手 (原因側は修正済み。**残っているのはデータの掃除だけ**)
**優先度**: medium (発生源は塞がっている。実害は想起の二重ヒットと、生ログのままの Chronicle エントリ 2 件)
**作成日**: 2026-07-24
**関連**: W5 / M8 (`13a91a5`, 2026-07-21 16:01) ・ W4 (`277bd8d`, 2026-07-21 07:48) ・ [`chronicle_undersized_lv1_chunks.md`](chronicle_undersized_lv1_chunks.md)

## 何が残っているか

ペルソナ記憶 (`memory.db` の `messages`) に、**内容・created_at・origin_track_id がすべて同一で id (UUID) だけ違う user 行**が残っている。

| ペルソナ | 重複組数 | 発生日 |
|---|---|---|
| `air_city_a` | 7 | 06-17, 06-18, 07-18, 07-19 |
| `eris_city_a` | 6 | 07-17, 07-18 |
| `aifi_city_a` | 3 | 06-17, 06-18, 07-18 |

(2026-06-01 以降を走査した結果。それ以前は未調査)

`eris_city_a` の例:

```
#9237  07-18 21:04:24 user  rowid=19367   ← 元
#9238  07-18 21:04:24 user  rowid=19388   ← 複製
#9242  07-18 21:15:03 user  rowid=19371   ← 元
#9243  07-18 21:15:03 user  rowid=19389   ← 複製
```

複製側の rowid は 19388〜19392 の連番 = **5 件まとめて後から挿入**されている。

## 原因 (修正済み)

Building → ペルソナ記憶の転記 ([`builtin_data/tools/get_building_messages.py:262-276`](../../builtin_data/tools/get_building_messages.py)) が、同じ building メッセージを二度転記していた。

転記済みマーカー `building_messages.ingested_by` の DB 更新が、**Pulse 開始時の auto_ingest では静かにスキップ**されていた (`tools.context.get_active_manager()` が persona_context の外では常に None のため。同ファイル冒頭の docstring に記録あり)。その結果、重複防止が実質メモリ上の `pulse_cursors` だけに乗っており、**プロセスが再起動するとカーソルが戻って同じメッセージを再転記**した。

世界 DB で境目が確認できる:

| `building_messages` の user 行 | `ingested_by` |
|---|---|
| 〜 2026-07-21 | `[]` (全件・空) |
| 2026-07-22 〜 | `["eris_city_a"]` |

W5 (`13a91a5`, 2026-07-21 16:01) の M8 修正で `manager` を引数で受けるようになり、マーカーが永続化されるようになった。**重複の新規発生は 07-19 を最後に止まっている**(全ペルソナ)。

## なぜ Chronicle に出たか

W4 (`277bd8d`, 2026-07-21 07:48) で Chronicle の未処理判定が「進捗ポインタ」から「一次あらすじエントリの `source_ids` 全部の集合との引き算」に変わった ([`session_lifecycle.py:1162-1174`](../../sea/session_lifecycle.py))。取りこぼしを拾う正しい変更だが、その結果:

- 旧ポインタ方式では見えなかった複製行 (#9238 / #9243) が「未編纂」として発見された
- 前後を編纂済みメッセージに挟まれた孤立 1 件なので、設計どおり恒等圧縮 (identity) として確定した
- 要約ではなく `[2026-07-18 21:04] [user]: …` という**生ログが本文の一次あらすじエントリ**になった

該当ログ (`~/.saiverse/user_data/logs/20260722_023944/backend.log`):

```
02:59:34 [executor] chunk committed: kind=identity messages=1 coverage=87
02:59:34 [executor] chunk committed: kind=identity messages=1 coverage=201
```

87 字 = #9238、201 字 = #9243。`eris_city_a` の Chronicle short_id **501 / 502**。

## 残作業

1. **重複行の削除** (エア 7・エリス 6・アイフィー 3)。どちらを残すか = rowid が小さい方 (会話の流れの中で書かれた元の行)
2. **Chronicle エントリ 501 / 502 の削除** (`eris_city_a`)。これらは複製行だけを source にしているので、複製行を消すと source が消えたエントリになる
3. 2026-06-01 より前の期間の走査 (未実施)

**いずれも本番ペルソナの記憶データの書き換え**。対象・件数・消える行を提示したうえで、まはーの明示承認を得てから実行する。承認前に触らない。

## 関連リソース

- [`builtin_data/tools/get_building_messages.py`](../../builtin_data/tools/get_building_messages.py) — 転記経路。`_transcribe_message` / `_mark_ingested` / `_ingest_round`
- `database/building_messages.py` `mark_ingested`
- 検出クエリ:
  ```sql
  SELECT role, created_at, content, COUNT(*) n, GROUP_CONCAT(rowid)
  FROM messages GROUP BY role, created_at, content HAVING n > 1;
  ```

## ログ

- 2026-07-24: 調査・起票。まはーが Chronicle 一覧で「7/18 の #9238 と #9243 が単独の発言で記録されている」ことに気づいたのが発端。まはー裁定で**データ掃除は最後回し**、先に [`chronicle_undersized_lv1_chunks.md`](chronicle_undersized_lv1_chunks.md) を扱う
