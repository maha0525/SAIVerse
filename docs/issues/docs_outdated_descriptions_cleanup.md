# Issue: ドキュメントの古い記載を棚卸しして更新する

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-06-18
**関連**: `README.md`、`AGENTS.md`、`CLAUDE.md`、`docs/developer-guide/project-structure.md`、`docs/index.md`

## 背景

SAIVerse の実装・ディレクトリ構成が進んだ一方で、README や開発者向けドキュメントに古い構成・旧名称・現在の実装とずれた説明が残っている可能性がある。

直近のドキュメント整合性確認では、特に以下の領域が棚卸し対象として挙がった。

- `README.md`: プロジェクト概要、導入手順、リンク、現在の機能一覧が最新実装と一致しているか
- `AGENTS.md` / `CLAUDE.md`: エージェント向け作業指示・構成説明が現在のコード構造と一致しているか
- `docs/developer-guide/project-structure.md`: 実際のディレクトリ構成、主要モジュール説明、廃止/移動済み要素の記載が残っていないか
- `docs/index.md`: ドキュメントへのリンク切れ、古い導線、未整備ページへの参照がないか

このまま放置すると、新しく触る人や将来の自分が古い説明を信じて調査・実装の入口を誤るリスクがある。

## 解決案候補

1. **現状棚卸し**
   - 実際のリポジトリ構成と主要ドキュメントの記載を照合する
   - `docs/index.md` から辿れるリンクの実在確認を行う
   - README / AGENTS / CLAUDE / project-structure の古い記述候補を High / Medium / Low に分類する

2. **更新方針の決定**
   - 明らかに誤っている記載は修正
   - まだ構想段階の内容は「未実装」「設計中」「legacy」など状態を明記
   - 重複している説明は、正本となるドキュメントへ集約し、他からはリンクする

3. **検証**
   - Markdown リンクが壊れていないことを確認
   - コード構成説明が実際のファイル/ディレクトリと矛盾していないことを確認
   - Windows/WSL 環境で git 差分が CRLF のみになっていないことを確認

## 関連リソース

- `README.md`
- `AGENTS.md`
- `CLAUDE.md`
- `docs/index.md`
- `docs/developer-guide/project-structure.md`
- `docs/overview/roadmap_status.md` §9（`docs/issues/` 運用）

## ログ

- 2026-06-18: Hermes cron によるドキュメント整合性確認の流れで、主要ドキュメントに古い記載が残っている可能性があるため起票。まずは棚卸しと優先度分類を行い、後で着手できる状態にする。
