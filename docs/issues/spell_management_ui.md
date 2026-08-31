# Issue: Spell の管理・可視化 UI

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-05-29
**関連**: `docs/concepts/spell.md`、`tools/` registry（spell フラグ）、`SpellConfirmDialog.tsx` / `ToolModeSelector.tsx`、コンテキストプレビュー機能

## 背景

開発者リファレンス（`docs/concepts/spell.md`）を書く過程で、Spell の管理・可視化が UI として不足していることが2点判明した。

### 課題1: Spell を増やす手順が煩雑（ワンタッチ化が無い）

新しい Spell を追加するには、コードで Tool を定義 → 登録 schema に `spell=True` を設定 → reload / 再起動、というコード作業が必要。SAIVerse は「ユーザーがコンフィグ可能 + 我々が増やしていく」前提なので、**Tool 管理 UI から各 Tool の spell 化を ON/OFF できる**べき（モデル管理 UI が前例）。

### 課題2: ペルソナが今使える Spell の一覧が見えない

あるペルソナが現在どの Spell を使えるのかを一覧する専用の手段が無い。**現状はコンテキストプレビュー機能でシステムプロンプト部分を覗いて確認するしかなく、実用上厳しい**。Spell の可視性は building / persona の状況で変わるため、状況に応じた「利用可能 Spell 一覧」ビューが要る。

## 解決案候補

- **Tool 管理 UI**: 各 Tool の `spell` / `spell_visible` / `spell_display_name` を UI でトグル・編集。ワンタッチで spell 化を切り替え（グローバル設定「モデル管理」UI が前例）
- **利用可能 Spell 一覧ビュー**: ペルソナ（+ 現在の building）について、いま使える Spell を一覧表示。システムプロンプトを覗かずに確認できる
- 上記2つは「Spell 管理パネル」として統合できる可能性がある

## 関連リソース

- `docs/concepts/spell.md`（本 issue の発端）
- `tools/` registry の `spell` / `spell_display_name` / `spell_visible` フィールド
- `frontend/src/components/SpellConfirmDialog.tsx` / `ToolModeSelector.tsx`
- コンテキストプレビュー機能（現状の代替手段）
- 前例: グローバル設定「モデル管理」UI（プロバイダ/モデルの UI 管理）

## ログ

- 2026-05-29: 開発者リファレンス（spell.md）作成中に判明、起票。
