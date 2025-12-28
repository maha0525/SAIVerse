# Memopedia

ペルソナのナレッジベース「Memopedia」の使い方を説明します。

## 概要

Memopediaは、SAIMemoryに記録された会話ログから知識を抽出し、Wikipediaのような構造化されたドキュメント群として管理する機能です。

## 解決する課題

従来のSAIMemoryでは発言そのままの想起は可能ですが、トピックに関する体系的な知識が抜け落ちやすい問題がありました。Memopediaでは、重要なトピックの情報を1ページにまとめ、ペルソナがページ一覧から関連するものを選んで知識を取得できます。

## 3つのルートカテゴリ

| カテゴリ | 説明 |
|----------|------|
| 人物 (people) | 関わりのある人物についての記録 |
| 出来事 (events) | 過去に起きた出来事の記録 |
| 予定 (plans) | 進行中や計画中のプロジェクト・予定 |

## UIでの使い方

サイドバーから「Memory & Knowledge」→「Memopedia」タブを選択：

### Knowledge Tree

ページの階層構造を表示。

- `>` マークをクリックで展開/格納
- ページ名をクリックで右側に内容を表示

### 履歴ボタン

ページ選択時に表示され、編集履歴を確認可能。

- 編集タイプ（作成/更新/追記/削除）
- 参照メッセージ範囲
- 差分（diff）の表示

## ツール（AI用）

ペルソナが会話中にMemopediaを操作するためのツール：

| ツール名 | 説明 |
|----------|------|
| `memopedia_get_tree` | ページツリーをMarkdown形式で取得 |
| `memopedia_open_page` | 指定したページを開き、内容を取得 |
| `memopedia_close_page` | 指定したページを閉じる |

## CLIでの構築

既存の会話履歴からMemopediaを自動構築：

```bash
# 基本的な使い方
python scripts/build_memopedia.py <persona_id> --limit 100

# dry-runで確認
python scripts/build_memopedia.py <persona_id> --limit 100 --dry-run

# モデルを指定
python scripts/build_memopedia.py <persona_id> --model gemini-2.5-pro
```

### エクスポート/インポート

```bash
# JSONエクスポート
python scripts/build_memopedia.py <persona_id> --export backup.json

# JSONインポート
python scripts/build_memopedia.py <persona_id> --import backup.json
```

### メンテナンス

```bash
# 全自動メンテナンス
python scripts/maintain_memopedia.py <persona_id> --auto

# 個別実行
python scripts/maintain_memopedia.py <persona_id> --fix-markdown
python scripts/maintain_memopedia.py <persona_id> --split-large
python scripts/maintain_memopedia.py <persona_id> --merge-similar
```

## 設計詳細

ページ内容はMarkdown形式で記述され、以下のフィールドを持ちます：

| フィールド | 説明 |
|------------|------|
| title | ページタイトル |
| summary | 概要（常にペルソナに渡す） |
| content | 本文（開いたときに展開） |
| keywords | キーワード（JSON配列） |

## 次のステップ

- [SAIMemory](../concepts/saimemory.md) - 記憶システムの詳細
- [スクリプト一覧](../reference/scripts.md) - 保守スクリプト
