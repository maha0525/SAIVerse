# Memopedia

ペルソナのナレッジベース「Memopedia」の使い方を説明します。

## 概要

Memopediaは、SAIMemoryに記録された会話ログから知識を抽出し、Wikipediaのような構造化されたドキュメント群として管理する機能です。

## 解決する課題

従来のSAIMemoryでは発言そのままの想起は可能ですが、トピックに関する体系的な知識が抜け落ちやすい問題がありました。Memopediaでは、重要なトピックの情報を1ページにまとめ、ペルソナがページ一覧から関連するものを選んで知識を取得できます。

## 4つのルートカテゴリ

| カテゴリ | 説明 |
|----------|------|
| 人物 (People) | 関わりのある人物についての記録 |
| 用語 (Terms) | 用語・概念の記録 |
| 計画 (Plans) | 進行中や計画中のプロジェクト・予定 |
| 出来事 (Events) | 過去に起きた出来事の記録 |

## UIでの使い方

ペルソナメニュー →「記憶」で[記憶モーダル](memory-view.md)を開き、「Memopedia」タブを選択（記憶モーダルには他に チャットログ / Chronicle / インポート 等のタブがある）：

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
| `memopedia_get_page` | タイトル/IDでページ本文を取得 |
| `memopedia_open_page` | 指定したページを開き、内容を取得 |
| `memopedia_close_page` | 指定したページを閉じる |
| `memopedia_note` | 知識フラグメントをページに書き込む |

> このほか `memopedia_manage` / `memopedia_health` / フラグメント系（`memopedia_list_fragments` / `_edit_fragment` / `_delete_fragment`）がある。全一覧は [ツールカタログ](../reference/tool-catalog.md)（`memopedia_*`）。

> Memopedia の生成・整理は、ペルソナが自律行動（`autonomy_memory_organization` / `fragment_organize`）や Metabolism の中で自動的に行う。ユーザーが手動で構築・メンテナンスする通常の導線はない。

## 設計詳細

ページ内容はMarkdown形式で記述され、以下のフィールドを持ちます：

| フィールド | 説明 |
|------------|------|
| title | ページタイトル |
| summary | 概要（常にペルソナに渡す） |
| content | 本文（開いたときに展開） |
| keywords | キーワード（JSON配列） |

## 次のステップ

- [concepts/memopedia.md](../concepts/memopedia.md) - Memopedia の仕組み（開発者向け）
- [SAIMemory](../concepts/saimemory.md) - 記憶システムの詳細
