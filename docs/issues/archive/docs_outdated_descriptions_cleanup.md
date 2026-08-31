# Issue: ドキュメントの古い記載を棚卸しして更新する

**ステータス**: ✅ 完了（2026-07-03）
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
- 2026-07-03: 起票の4対象（README / AGENTS / project-structure / index）を実装と全照合して更新完了。主な修正:
  - **`docs/developer-guide/project-structure.md`**: 全面書き直し。実態と乖離していた記述を修正（マネージャ群はルート直下でなく `saiverse/` 配下、存在しない `ui/` Gradio・`models.json`/`cities.json` ルート配置・リポジトリ直下 `user_data/` の記載を削除、Playbook は `sea/playbooks/` でなく `builtin_data/playbooks/public/`、ツール定義は `tools/defs/` でなく `builtin_data/tools/`、api は `routes/` サブ構成）。3層優先順位・`~/.saiverse/` 構成・`builtin_data/providers/` を反映。
  - **`docs/index.md`**: 基本概念のリンク切れ（legacy 退避済みファイル参照）を新 `concepts/` 22ページ構成へ差し替え、`features/mcp-integration.md` を追加、存在しない `docs_legacy/` リンクを overview/issues 導線へ置換。
  - **`README.md`**: 陳腐化した「今後の開発予定（2026年2月〜3月ごろ）」の日付固定を外し、維持中の `roadmap_status.md`/`landscape.md` へ誘導。構造セクションは照合の結果正確だったので維持。
  - **`AGENTS.md`**: `CLAUDE.md` の古い複製が別々保守でドリフトしていたため、単一正典（CLAUDE.md）を読ませる薄いポインタに一本化（Codex 固有の枠のみ残置）。
  - **`CLAUDE.md`**: 存在しない doc 参照（`architecture.md`/`database_design.md`/`test_manual.md`/`sea_integration_plan.md`）と `sea/playbooks/`・`tools/defs/` 参照を実在先へ surgical 修正（計6箇所）。
  - 併せて `docs/concepts/` に概念リファレンス18本 + 索引 README を新規整備し、`overview/landscape.md` 末尾の「将来作成」注記を完成状態へ更新。
  - 検証: 対象5ファイルから壊れたトークンが全滅したことを grep で確認。→ 完了につき `archive/` へ移動。
