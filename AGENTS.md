# AGENTS.md

This file provides guidance to Codex (and other coding agents) when working in this repository.

## 単一の正典: CLAUDE.md

**このプロジェクトの作業指示・アーキテクチャ説明・規約は、すべて [`CLAUDE.md`](./CLAUDE.md) に集約されている。作業前に必ず `CLAUDE.md` を読み、その内容に従うこと。**

以前この `AGENTS.md` は `CLAUDE.md` の内容を複製していたが、別々に保守した結果ドリフト（古い記述の残存）が起きた。それを防ぐため、本ファイルは薄いポインタに一本化し、実体は `CLAUDE.md` に置く。両者を二重に保守しないこと。

`CLAUDE.md` は "Claude Code" 向けの文面になっているが、記載されているプロジェクト規約・構成説明・デバッグ規律はエージェント種別を問わず適用される。Codex もそのまま従うこと。

## Codex 固有の前提（CLAUDE.md 冒頭の対応部分）

- **Language**: Think in English, respond in Japanese. リポジトリオーナーは日本語でのやり取りを好む。
- **Local preferences**: リポジトリルートに `Codex.local.md`（または `CLAUDE.local.md`）があれば読むこと（名前・個人設定などの追加コンテキスト）。

## 特に外してはいけない要点（詳細はすべて CLAUDE.md）

- **既存コードの属性・メソッド名を推測しない**。使う前に必ずソースを読んで正確な名前を確認する。
- **`database/seed.py` を不用意に実行しない**（DB を全消去する）。Playbook 更新は `scripts/import_all_playbooks.py`（安全）。
- **Python を書き換えたら `ruff check` を実行**してから完了扱いにする。
- **推測で修正しない**。ログ・コンソール出力を一次情報として原因を特定してから直す。
- **Intent Documents**: 機能に着手する前に `docs/intent/<feature>.md` を確認し、無ければ先に起草する。

上記はあくまで抜粋。**必ず `CLAUDE.md` 全文を参照すること。**
