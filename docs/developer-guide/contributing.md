# コントリビューション

SAIVerseへの貢献方法を説明します。

## 開発環境のセットアップ

1. リポジトリをフォーク
2. [インストール](../getting-started/installation.md) の手順に従って環境構築
3. 開発用ブランチを作成

```bash
git checkout -b feature/your-feature-name
```

## コードスタイル

### Python

- Python 3.11+ を対象
- 型ヒントを積極的に使用
- docstring は日本語でOK

### TypeScript (frontend)

- Next.js のプロジェクト構成に従う
- ESLint の設定に従う

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
