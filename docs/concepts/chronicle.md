# Chronicle（時系列圧縮 / Track 再開）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §5](../overview/landscape.md) を参照。

## 一言で

蓄積された [Message](saimemory.md) を LLM が「あらすじ」に圧縮した層。

## 役割

生ログは無限に増えるが、[Session](session.md)（短期記憶）に載せられる量は限られる。Chronicle は古い経験を「あらすじ」へ畳んで、ペルソナが過去の文脈を圧縮された形で参照できるようにする。加えて、中断された [Track](track.md) を再開する際にその目的に沿った文脈を呼び戻す。

## 仕組み

### 階層的な圧縮

- Message が一定数（`DEFAULT_BATCH_SIZE=20`）貯まるごとに LLM が **「あらすじ」（Lv1）** へ圧縮
- 古い Lv1 同士はさらに **「あらすじのあらすじ」（Lv2+）** へ統合
- 物理格納は `memopedia_pages`（trunk `root_chronicle` 配下の子ページ、Memory Atlas「時間の地図」。P3b, 2026-07-11）。`arasuji_entries` は生 SQL 消費者向けの読み取り専用 SQL VIEW として残る

### Track 専用 Chronicle

Track が中断・再開される際には **`origin_track_id` 付きの Track 専用 Chronicle** が生成され、その Track の目的に沿った情報が復帰時に呼び戻される。

> **`origin_track_id` の付与ルール**: Track 内のログには紐付けるが、世界横断のメタログ（`event_message` / host / メタ判断）は NULL。

### 生成タイミング

Chronicle は [Metabolism](metabolism.md) 発火時に `ArasujiGenerator` が生成する。同じバッチで [Memopedia](memopedia.md) の Fragment 生成も相乗りする（§5 と §6 の接続点）。

## 実装

- 生成: `sai_memory/arasuji/generator.py`（`ArasujiGenerator`）
- ストレージ: `sai_memory/arasuji/storage.py`（物理格納は `memopedia_pages`、`arasuji_entries` は互換 VIEW）
- コンテキスト構築: `sai_memory/arasuji/context.py`
- 読み出しツール: `builtin_data/tools/chronicle_*.py`（search / read_detail / context_up / context_down）

## 関連概念

- [SAIMemory](saimemory.md) — Chronicle を内包する容れ物
- [Memopedia](memopedia.md) — 同じ Metabolism バッチで連動生成
- [Metabolism](metabolism.md) — Chronicle 生成を発火する節目
- [Track](track.md) — Track 専用 Chronicle で再開文脈を供給

## 参照

- 地図: [`landscape.md`](../overview/landscape.md) §5
