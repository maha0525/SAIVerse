# スクリプト一覧

`scripts/` ディレクトリにある保守スクリプトの一覧です。

## SAIMemory関連

### backup_saimemory.py

rdiff-backupでSAIMemoryを差分バックアップ。

```bash
python scripts/backup_saimemory.py air eris --output-dir ~/.saiverse/backups
python scripts/backup_saimemory.py air --full --verbose
```

### export_saimemory_to_json.py

指定期間のメッセージをJSONエクスポート。

```bash
python scripts/export_saimemory_to_json.py air --start 2025-01-01 --end 2025-12-31 --output air.json
```

### import_persona_logs_to_saimemory.py

JSONログをSAIMemoryにインポート。

```bash
python scripts/import_persona_logs_to_saimemory.py --persona air --reset
python scripts/import_persona_logs_to_saimemory.py --persona air --include-archives
```

### prune_sai_memory.py

古いエントリを整理。

```bash
python scripts/prune_sai_memory.py air --days 365
```

### tag_conversation_messages.py

メッセージにタグを付与。

```bash
python scripts/tag_conversation_messages.py air --auto
```

### reembed_memory.py

埋め込みを再生成。

```bash
python scripts/reembed_memory.py air
```

## Memopedia関連

### build_memopedia.py

会話履歴からMemopediaを構築。

```bash
python scripts/build_memopedia.py air --limit 100
python scripts/build_memopedia.py air --model gemini-2.5-pro --dry-run
python scripts/build_memopedia.py air --export backup.json
python scripts/build_memopedia.py air --import backup.json
```

### maintain_memopedia.py

Memopediaを自動メンテナンス。

```bash
python scripts/maintain_memopedia.py air --auto
python scripts/maintain_memopedia.py air --fix-markdown
python scripts/maintain_memopedia.py air --split-large
python scripts/maintain_memopedia.py air --merge-similar
```

## タスク関連

### process_task_requests.py

タスクリクエストを処理。

```bash
python scripts/process_task_requests.py --base ~/.saiverse/personas
```

## Playbook関連

### import_all_playbooks.py

Playbookをデータベースにインポート。

```bash
python scripts/import_all_playbooks.py
```

## Discord関連

### run_discord_gateway_tests.py

Discord Gatewayのテスト実行。

```bash
python scripts/run_discord_gateway_tests.py
```

## ユーティリティ

### memory_topics_ui.py

トピックをブラウザUIで可視化。

```bash
python scripts/memory_topics_ui.py
```

### ingest_persona_log.py

ペルソナログを取り込み。

```bash
python scripts/ingest_persona_log.py air
```

### recall_persona_memory.py

関連記憶を検索。

```bash
python scripts/recall_persona_memory.py air "旅行 温泉" --json
```

### rename_generic_topics.py

トピック名を一括リネーム。

```bash
python scripts/rename_generic_topics.py air --dry-run
```
