# Issue: スペル権限タブ(Playbook 権限と同様の許可管理)

**ステータス**: 🔲 未着手
**優先度**: mid
**作成日**: 2026-07-08
**関連**: `frontend/src/components/PlaybookPermissionDialog.tsx`(下敷き), `frontend/src/components/GlobalSettingsModal.tsx`, スペル機構(`sea/` の spell 実行経路), 権限の DB 保存先

## 背景

Playbook には権限タブ(どのペルソナ/範囲がどの Playbook を使えるか)があるが、**スペルにも同様の権限管理**が欲しい。UI 的には Playbook 権限タブと同一タブ内で切り替える形が自然か。

## 調査事項

1. `PlaybookPermissionDialog.tsx` を読み、Playbook 権限の**権限モデル**(誰が・何を・どの粒度で許可するか、DB のどこに保存しているか、API 経路)を把握する。
2. スペル側に権限に相当するデータが既に存在するか(スペルは `spell=True` のツール等。現状「使える/使えない」をどこで決めているか)。無ければ**スペル権限のデータモデル(DB)新設**が要る。
3. スペルと Playbook の権限モデルが**同型**か(同じ「主体 × 対象 × 許可」構造で扱えるか)。同型なら UI/バックエンドを共通化できる。

## 解決案候補

- `PlaybookPermissionDialog` をスペル対応に拡張する(対象種別に「スペル」を足す) or スペル専用ダイアログを併設し、同一タブ内でトグル。
- スペル権限の DB が無ければ backend にモデル + API を新設。Playbook 権限の構造を踏襲する。

## 関連リソース

- `frontend/src/components/PlaybookPermissionDialog.tsx`
- `frontend/src/components/GlobalSettingsModal.tsx`(権限タブの所在)
- スペル機構: `docs/concepts/` の spell 関連、`sea/` の spell 実行経路
- アイディア帳: `docs/overview/ideas.md`「UI / プラットフォーム」

## ログ

- 2026-07-08: 起票。ideas.md から昇格。下敷きは `PlaybookPermissionDialog.tsx`。まず Playbook 権限モデルの把握から。
