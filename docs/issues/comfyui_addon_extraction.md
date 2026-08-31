# ローカル画像生成 (ComfyUI) のアドオン切り出し

**状態**: 未解決 — 実装完了 (2026-08-01)、まはーの実機検証待ち。

builtin から `generate_image_local` ツール+Playbook+Anima ワークフローを撤去し、独立 git リポジトリの新設アドオン `expansion_data/saiverse-comfyui-addon` へ移設した件。アドオン本体は [maha0525/saiverse-comfyui-addon](https://github.com/maha0525/saiverse-comfyui-addon) (README にセットアップ手順、2026-08-01 公開・まはー承認済み)。

## 残作業

実機検証: ComfyUI 起動状態で Playbook を実行し、画像生成が従来どおり通ること。起動ログに `playbook_sync: repointed source_file` が出るはず (source_file 付け替えの本走行確認)。

## 経緯: ローカル画像生成 (ComfyUI) のアドオン切り出し (2026-08-04 in_flight 台帳より移送)

> 台帳の器の再設計 (次アクション欄=前向きのみ) に伴い、それまで台帳セルに積もっていた経緯の全文をここへ移した。時系列の生の堆積であり、整理はしていない。

実装完了 (2026-08-01, `b1fef20`): builtin から `generate_image_local` ツール+Playbook+Anima ワークフローを撤去し、新設 `expansion_data/saiverse-comfyui-addon` (独立 git リポジトリ、README にセットアップ手順) へ移設。
前提修正2本を同梱: ① Playbook TOOL ノードの素名→`<addon>__<name>` 解決 (`canonicalize_tool_name`) — X アドオン Playbook の潜在破損 (2026-07-19 名前空間化以降) も解消 ② 層移動した Playbook の source_file 付け替え (playbook_sync / import_all_playbooks) — 誤 prune による Playbook+Permission 消失の防止 (Codex 指摘)。
GitHub 公開済み ([maha0525/saiverse-comfyui-addon](https://github.com/maha0525/saiverse-comfyui-addon)、2026-08-01 まはー承認)。
残 = まはー実機検証 (ComfyUI 起動状態で Playbook 実行、起動ログに `playbook_sync: repointed source_file` が出るはず)
