# スクリプト一覧

`scripts/` にある保守スクリプトの**主要なもの**。全一覧は `ls scripts/`（一回きりの移行・デバッグ用スクリプトも多数あるため、ここは常用のものに絞った curated 版）。

## Playbook

| スクリプト | 用途 |
|---|---|
| `import_playbook.py` | 単一 Playbook JSON を DB にインポート（`--file <path>`） |
| `import_all_playbooks.py` | 全 Playbook を DB にインポート（`--force` / `--dry-run`。安全） |
| `playbook_dry_run.py` | Playbook のドライラン検証 |
| `strip_playbook_line_fields.py` | Playbook の line フィールド除去（移行系） |

## ドキュメント生成

| スクリプト | 用途 |
|---|---|
| `gen_reference_docs.py` | `docs/reference/` の自動生成 doc（tool-catalog / api-endpoints / database-schema）を再生成。`--check` でドリフト検査。ルートの [`gen_reference_docs.bat`](../../gen_reference_docs.bat) から叩く |

## SAIMemory / 記憶

```bash
# rdiff-backup で差分バックアップ
python scripts/backup_saimemory.py air eris --output-dir ~/.saiverse/backups
python scripts/backup_saimemory.py air --full --verbose

# 期間指定で JSON エクスポート
python scripts/export_saimemory_to_json.py air --start 2025-01-01 --end 2025-12-31 --output air.json

# JSON ログを SAIMemory にインポート
python scripts/import_persona_logs_to_saimemory.py --persona air --reset

# 古いエントリを整理 / 埋め込み再生成 / タグ付与
python scripts/prune_sai_memory.py air --days 365
python scripts/reembed_memory.py air
python scripts/tag_conversation_messages.py air --auto
```

その他: `export_saimemory_native.py` / `import_saimemory_native.py`（ネイティブ形式）、`embed_recall_sources.py`、`extract_memory_notes.py` / `organize_memory_notes.py`（メモ）、`debug_memory.py`。

## Memopedia / Chronicle

> ⚠️ Memopedia / Chronicle の生成・整理は、現在はペルソナの自律行動（`autonomy_memory_organization` / `fragment_organize`）や Metabolism の中で**自動的に**行われる。`build_memopedia.py` / `maintain_memopedia.py` 等の手動構築スクリプトは**旧フロー**で、通常は使わない（`export_memopedia.py` などの export 系のみ補助的に残る）。
>
> `build_arasuji.py` は例外的に現役: インポート直後など、Chronicle をまとめて前倒し生成したい場合の任意ツールとして使う。`--estimate` で LLM を呼ばずに未処理メッセージ数・コール数・概算費用（pricing 設定済みモデルのみ）を表示でき、通常実行時も生成前に同じ見積もりを表示したうえで確認を求める（`--yes` でスキップ可）。
>
> `persona_chronicle_cleanup.py`（2026-07-29 新設）: あらすじのレベル制への移行修復 — 旧コードが作った歪み世代 Chronicle の点検と削除。既定は dry-run（読み取りのみ）、`--execute` はバックアップ・検算・実行台帳掃除込み。手順と現況は `docs/issues/air_aifi_memory_repair.md`。

## インポート（引っ越し）

> 記憶アーキ v2 Phase 4（2026-07-04）: インポートは**挿入＋ローカル埋め込み（無料・LLM 不使用）のみ**で完了とし、完了直後から自動想起（ゾーン C）が機能する。Chronicle（あらすじ）の一括生成は任意・後回しでよい（費用見積もり付き、`build_arasuji.py --estimate`）。詳細は `docs/user-guide/memory-migration.md`。

| スクリプト | 用途 |
|---|---|
| `import_chatgpt_conversations.py` | ChatGPT 公式エクスポートを取り込み |
| `import_chatlog_json.py` | 汎用チャットログ JSON を取り込み |
| `import_persona_logs_to_saimemory.py` | SAIVerse 内の旧形式ログ（`log.json` 等）を取り込み。完了時に埋め込みバックフィル＋サマリを表示 |

## アドオン

| スクリプト | 用途 |
|---|---|
| `addon_install.py` | アドオンのインストール |

## データ移行

一回きりのマイグレーション群（実行前に `--dry-run` があるものは必ず確認）:

```bash
python scripts/migrate_to_user_data.py --dry-run   # 既存データを ~/.saiverse/user_data 構造へ
```

その他: `migrate_building_logs_to_db.py` / `migrate_conscious_log_to_db.py` / `migrate_memory_tags.py` / `migrate_playbooks_to_lines.py` / `migrate_tasks_db_to_unified.py` / `migrate_track_tasks_json.py`。

## 開発 / 運用

| スクリプト | 用途 |
|---|---|
| `update_engine.py` | 全update入口の正典。clean Git fast-forward、更新前world snapshot、phase fail-stop、同一条件restart、health確認、失敗時rollback |
| `self_update.py` | 旧セルフアップデート入口から `update_engine.py` への互換wrapper |
| `set_version.py` | バージョン刻印 |
| `snapshot.py` | world snapshot format v2のsave/list/inspect/restore/delete。restoreは停止状態だけで実行 |
| `run_discord_gateway_tests.py` | Discord Gateway テスト |
| `download_searxng_source.py` / `merge_searxng_settings.py` | SearXNG セットアップ |
