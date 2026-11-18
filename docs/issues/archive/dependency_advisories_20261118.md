# Issue: 2026-11-18 自動セキュリティ点検の初回実行で検知された依存更新 (8 対象)

**発見日**: 2026-11-18 (`scripts/check_dependency_advisories.py` の新設・初回実行)
**解決日**: 2026-11-18
**関連**: [`docs/intent/dependency_management.md`](../../intent/dependency_management.md) §2-10・§3-2・§7

## 概要

毎日の脆弱性自動点検スクリプト (`scripts/check_dependency_advisories.py`) を `develop` に対して実行したところ、前回の総点検 (2026-10-22) 以降に公開されていた以下のセキュリティ更新・勧告が検知された。

## 対応内容

1. **同梱 Node.js** (`scripts/install_node_portable.ps1`、`README.md`、`docs/getting-started/installation.md`)
   - `v24.22.1` → `v24.22.3` (2026-11-10 公開のセキュリティ更新。22 系の案内も `22.23.5` へ更新)。実ダウンロード・SHA256 検証・展開を Windows PowerShell で確認。
2. **フロントエンド本番 npm** (`frontend/package-lock.json`)
   - `lodash` `4.17.23` (`recharts` の間接依存) → `4.18.1` (`CVE-2026-2950`, `CVE-2026-4800` 解消)。`npm ci`・`npm test`・`npm run build` 合格。
3. **Python `requirements.lock` / `requirements.txt`**
   - `filelock` `3.25.2` → `4.2.12`
   - `langgraph` `1.2.11` → `1.2.22` (`langgraph-sdk` `0.4.4` → `0.4.14`、`langgraph-checkpoint` `4.2.0` → `4.3.2`)
   - `opentelemetry-api` / `opentelemetry-sdk` `1.39.1` → `1.45.2` (`opentelemetry-semantic-conventions` `0.60b1` → `0.66b2`)
   - `pyasn1` `0.6.4` → `0.6.5`
   - `pypdf` `6.21.2` → `7.1.0`
   - `soupsieve` (`python_full_version >= '3.11.5'`) `3.0.4` → `4.1.1`。`< '3.11.5'` 専用分岐の `2.10` は 2.x の最終版で上流に修正版が無く、SAIVerse では Python 3.11 / 3.12 が非推奨のため、`dependency_management.md` §3-2 に記録したうえで `ACCEPTED_ADVISORIES` で `version="2.10"` に限定して除外 (`4.1.1` は引き続き監視)。
   - `check_lock_platforms.py`・素の venv への lock 導入・`pip check`・`pytest`・`python scripts/check_dependency_advisories.py` (exit 0) を確認。
