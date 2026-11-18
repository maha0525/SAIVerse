# Issue: 2026-11-18 自動セキュリティ点検の初回実行で検知された依存更新 (8 対象)

**発見日**: 2026-11-18 (`scripts/check_dependency_advisories.py` の新設・初回実行)
**関連**: [`docs/intent/dependency_management.md`](../intent/dependency_management.md) §2-10・§3-2

## 概要

毎日の脆弱性自動点検スクリプト (`scripts/check_dependency_advisories.py`) を `develop` に対して実行したところ、前回の総点検 (2026-10-22) 以降に公開されていた以下のセキュリティ更新・勧告が検知された。自動点検の導入 PR とはスコープを分け、本 issue で更新と互換検証 (`check_lock_platforms.py`・テストスイート) を行う。

## 検知された対象

1. **同梱 Node.js** (`scripts/install_node_portable.ps1`)
   - 現在: `v24.22.1` → セキュリティ更新: `v24.22.3` (2026-11-10 公開)
2. **フロントエンド本番 npm** (`frontend/package-lock.json`)
   - `lodash` `4.17.23` (`recharts` の間接依存) → 修正版 `4.18.1` (`CVE-2026-2950`, `CVE-2026-4800`)
3. **Python `requirements.lock`**
   - `filelock` `3.25.2` → 修正版 `4.2.1` 以降 (`CVE-2026-112907` ほか 9 件。メジャー更新のため他パッケージの上限制約を確認する)
   - `langgraph` `1.2.11` / `langgraph-sdk` `0.4.4` → 修正版 `1.2.14` / `0.4.6` 以降 (`CVE-2026-118667`)
   - `opentelemetry-api` `1.39.1` → 修正版 `1.45.2` 以降 (`CVE-2026-114603`, `CVE-2026-126360`)
   - `pyasn1` `0.6.4` → 修正版 `0.6.5` (`CVE-2026-119194`〜`119197`)
   - `pypdf` `6.21.2` → 修正版 `6.21.4` 以降 (`CVE-2026-119203`〜`119209`)
   - `soupsieve` `2.10` / `3.0.4` → 修正版 `4.0.0` 以降 (`CVE-2026-126190`〜`126193`。Python バージョン条件と beautifulsoup4 の上限制約を確認し、上げられない場合は理由を `dependency_management.md` §3-2 に記録する)

## 完了条件

- 各対象を修正版へ更新し (上流の制約で上げられないものがあれば `dependency_management.md` §3-2 と `ACCEPTED_ADVISORIES` に理由と解除条件を明記)、`check_lock_platforms.py`・`pytest`・`npm test`・`python scripts/check_dependency_advisories.py` がすべて exit 0 で通ること。
