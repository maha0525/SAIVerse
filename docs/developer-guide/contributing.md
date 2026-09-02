# コントリビューション

SAIVerseへの貢献方法を説明します。

> ⚠️ **現在プレリリース中のため、外部コントリビューションを正式受付できる体制が整っていません**（[README](../../README.md) 参照）。提案・バグ報告は [Discord](https://discord.gg/qMcgEk83Ag) / [GitHub Issues](https://github.com/maha0525/SAIVerse/issues) へ。以下は受付開始後の想定フローです。

## 開発環境のセットアップ

1. リポジトリをフォーク
2. [インストール](../getting-started/installation.md) の手順に従って環境構築
3. 開発用ブランチを `develop` から作成

```bash
git checkout -b feature/your-feature-name
```

### ブランチ戦略

- **main**: 安定・テスト済みリリース
- **develop**: 統合ブランチ（PR の既定ターゲット）
- **feature/\***: 個別機能ブランチ（`develop` から作成）
- **フロー**: `feature/*` → PR → `develop` →（テスト）→ PR → `main`

## コードスタイル

### Python

- Python 3.13 推奨（3.11〜3.13 で動作、3.14 以降は非対応）を対象
- 型ヒントを積極的に使用
- docstring は日本語でOK

### TypeScript (frontend)

- Next.js のプロジェクト構成に従う
- ESLint の設定に従う（`frontend/` で `npm run lint`。エラー 0 を維持する。既存の警告は残っていてよい）

## プルリクエスト

1. 変更をコミット
2. テストを実行して確認
3. プルリクエストを作成
4. レビューを待つ

### コミットメッセージ

```
feat: 新機能の追加
fix: バグ修正
docs: ドキュメントの変更
refactor: リファクタリング
test: テストの追加・修正
```

## Python の依存関係 (requirements.txt と requirements.lock)

Python の部品 (ライブラリ) は二つのファイルで管理しています。背景と裁定は [`docs/intent/dependency_management.md`](../intent/dependency_management.md) にあります。

- **`requirements.txt`** は「意図」です。本体が直接 import する部品だけを、下限と理由つきの上限で書きます (`mcp>=1.10.0,<2` のように、上限には必ず一行の理由を添えます)。ここに `==` は書きません。
- **`requirements.lock`** は「検証した組み合わせ」です。間接依存も含めた全部品が `==` で固定されていて、`setup.bat` / `setup.sh` / `update.bat` はこのファイルから入れます。アドオンの導入時にも constraints として渡され、アドオンは本体が固定した部品を動かせません。人は編集しません。

**利用者は uv を入れる必要はありません** (素の pip で読める形式です)。uv が要るのは、開発者が lock を作り直すときだけです。

lock を作り直す手順 (`requirements.txt` を変えたとき):

```bash
# 既存の固定は維持し、requirements.txt で変わった分だけ解決する
uv pip compile requirements.txt --universal --python-version 3.11 -o requirements.lock

# 特定の部品だけ意図して上げる
uv pip compile requirements.txt --universal --python-version 3.11 -o requirements.lock --upgrade-package <name>
```

`--universal` で Windows / macOS / Linux と Python 3.11〜3.13 を環境マーカーつきの一枚に収めます。作り直したら `python scripts/check_lock_platforms.py` (各 pin が Windows / Linux / macOS arm64 / macOS x86_64 で入るかを PyPI に問う。uv は wheel の有無を見ないので、Windows で通っても Intel Mac で入らない lock ができうる) と全テストを通し、`requirements.txt` と `requirements.lock` を同じコミットに入れてください。`tests/test_requirements_lock_contract.py` が「lock が requirements.txt の範囲に収まっているか」と「導入経路がすべて lock を読んでいるか」を機械検査します。

## テスト

テストは `tests/` ディレクトリに配置。

```bash
# 全テスト実行
python -m pytest

# 特定のテストファイル
python -m pytest tests/test_persona_mixins.py
```

## 質問・議論

- Issue を作成して質問・提案
- 大きな変更は事前に Issue で議論

## 次のステップ

- [プロジェクト構造](./project-structure.md) - ディレクトリ構成
- [テスト](./testing.md) - テストの詳細
