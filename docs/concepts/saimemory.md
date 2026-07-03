# SAIMemory（長期記憶の容れ物）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §5](../overview/landscape.md)、**設計意図**は intent [`unified_memory_architecture.md`](../intent/unified_memory_architecture.md) を参照。旧版は [`legacy/saimemory.md`](legacy/saimemory.md)。

## 一言で

ペルソナの長期記憶をすべて格納する per-persona の SQLite DB（`memory.db`）。

## 役割

> ⚠️ SAIMemory は **DB（容れ物）の名前**であって、生ログそのものではない。生ログ・[Chronicle](chronicle.md)・[Memopedia](memopedia.md)・pulse_logs・memory_notes などはすべて SAIMemory の中身。

短期記憶（[Session](session.md)）とは階層が異なり、蓄積された経験の全体をなす。必要に応じて短期記憶へ引き出される。

## 仕組み

### 生ログ（Thread / Message）

ペルソナが経験したメッセージ・ツール結果・思考の時系列の連なり。

- 個々の発言が **Message**（`messages` テーブル）
- それを束ねる会話単位が **Thread**（`thread_id` / `get_or_create_thread`）
- タグ（`conversation` / `internal` / `task` / `summary` / `event_message` 等）で分類・検索される
- [Pulse](pulse.md) 内の詳細は `pulse_logs` テーブルに記録され、重要なノード出力は両方に書く「二重書き込み」で確実に残す

> **タグ注意**: 新種のシステム通知を挿入する時は `event_message` タグ必須（タグ漏れでペルソナのコンテキストに乗らない事故）。

### 中身の3層

| 中身 | 何を | リファレンス |
|---|---|---|
| 生ログ（Thread ⊃ Message） | 経験の時系列そのもの | 本ページ |
| Chronicle | 時系列を圧縮した「あらすじ」 | [chronicle.md](chronicle.md) |
| Memopedia | 固有対象の知識グラフ | [memopedia.md](memopedia.md) |

### 外部ログのインポート

生ログへの入力は Pulse 記録だけではない。ChatGPT 公式エクスポートや Chrome 拡張のエクスポートを SAIMemory に取り込める（新規ユーザーが過去の対話履歴を持ち込む導線 → [`roadmap_status.md`](../overview/roadmap_status.md) §6）。

## 実装

- 実装本体: `sai_memory/`（`memory/` / `arasuji/` / `memopedia/`）
- アダプタ: `saiverse_memory/adapter.py`（`SAIMemoryAdapter.log_message()` でタグ付き追記）
- 保存先: `~/.saiverse/personas/<id>/memory.db`
- バックアップ: rdiff-backup で `~/.saiverse/backups/saimemory_rdiff/<persona_id>/`（`SAIMEMORY_BACKUP_ON_START=true`）
- 検索スクリプト: `scripts/recall_persona_memory.py`

## 関連概念

- [Chronicle](chronicle.md) — 生ログの時系列圧縮
- [Memopedia](memopedia.md) — 生ログからの知識化
- [Session](session.md) — 生ログの末尾を引き出す短期記憶
- [Metabolism](metabolism.md) — 圧縮・知識化を発火する節目

## 参照

- intent: [`unified_memory_architecture.md`](../intent/unified_memory_architecture.md)
- 地図: [`landscape.md`](../overview/landscape.md) §5
