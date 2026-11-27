# Intent: アドオン管理のペルソナ別設定内パネル (`ui/PersonaPanel.tsx`)

**ステータス**: 検証待ち（本体・アドオンとも実装およびまはー実機確認済み。本体側は PR #519 / #520 で develop に入り、v0.3.35 に載る。アドオン側をカタログへ載せるのは v0.3.35 の発行後）

## これは何か

アドオン管理モーダル (`AddonManagerModal.tsx`) の「ペルソナ別設定」ブロック内に、アドオン独自の UI パネル (`expansion_data/<addon>/ui/PersonaPanel.tsx`) を差し込める拡張点を追加する。

既存の `ui/Panel.tsx`（アドオンカード末尾に置く全体パネル）と対になる仕組みで、ペルソナ別設定のプルダウンで選んだペルソナに連動する操作 UI（最初の利用者は `saiverse-voice-tts` v0.7.0 のテスト再生・WAV 保存・一括 ZIP 出力）を、ボイス ID や参照音声の設定欄のすぐ下に配置する。

## なぜ必要か

`saiverse-voice-tts` のようなペルソナ別設定を持つアドオンで「設定を変えてすぐテストする」操作を提供したいとき、既存の `ui/Panel.tsx` だけでは:

- カードの一番下（全体設定・ペルソナ設定の外側）にしか置けない
- ペルソナ別設定で選んだ `selectedPersonaId` がパネルに渡らず、パネル内にもう一つペルソナ選択プルダウンを置く二度手間が生じる
- 上の入力欄（Voice ID や話速）を変えた直後に下のテストボタンを押すと、保存 PUT の完了前に古い設定で合成が走りうる

ペルソナ別設定ブロック自体に差し込み口と「未完了の保存を待つ口 (`flushPendingSaves`)」を用意すれば、設定変更からテスト実行までを一つの文脈で完結でき、他のアドオンでも同じ拡張点を再利用できる。

## 全体像（結果・経路・所有境界）

- **結果と関係者**: 利用者はアドオン管理画面でペルソナを一度選べば、そのペルソナのパラメータ編集と連動した試聴・確認をその場で行える。アドオン作者は `ui/PersonaPanel.tsx` を置くだけで本体コードを触らずにペルソナ連動 UI を追加できる。
- **エンドツーエンドの経路**:
  1. アドオンが `expansion_data/<addon>/ui/PersonaPanel.tsx` を同梱する（`ui/Panel.tsx` と独立して任意・併用可）。
  2. `frontend/scripts/sync-addon-panels.mjs`（`predev` / `prebuild` で自動実行）が `ui/` 配下を `frontend/src/addon-panels/<addon>/` へコピーし、`frontend/src/addon-panels.generated.ts` に `ADDON_PANELS` と `ADDON_PERSONA_PANELS` および `AddonPersonaPanelProps` を生成する。
  3. `AddonManagerModal.tsx` の `ParamsSection` が `ADDON_PERSONA_PANELS[addon.addon_name]` を引き、ペルソナ選択後に `personaConfigBlock` と `OAuthFlowSection` の直下へ `Suspense` 付きで描画する。
- **不変条件と所有境界**:
  - **発見と配線は本体が所有、パネルの中身と API はアドオンが所有**: 本体側は `personaId` / `personaName` / `personas` / `addonApiBase` / `onConfigChanged` / `flushPendingSaves` / `configRevision` / `getConfigRevision` の契約だけを提供する。
  - **保存完了と鮮度判定の保証**: `ParamsSection` の直列保存鎖 (`saveChainRef`) を `flushPendingSaves` として公開し、さらにグローバル設定・ペルソナ設定の変更ごとに同期インクリメントする連番 (`configRevision` / `getConfigRevision`) を渡すことで、パネル側が設定変更後の古いプレビューキャッシュを誤再利用しないようにする。
  - **ペルソナ欄の表示条件**: `persona_configurable` なパラメータや `oauth_flows` が無くても、`PersonaPanel` を持つアドオンならペルソナ選択セクションを表示する。
  - **バージョン互換**: 古い SAIVerse 本体（v0.3.34 以前）は `ui/PersonaPanel.tsx` を無視するだけで壊れないが UI が出ないため、`ui/PersonaPanel.tsx` に依存するアドオン版はカタログ側で `min_saiverse_version: "0.3.35"` を要求する。

## 検証の経路

- `frontend/scripts/test-sync-addon-panels.cjs`: `Panel.tsx` のみ / `PersonaPanel.tsx` のみ / 両方 / 対象外 `ui/` の検出・コピー・コード生成を検証。
- `frontend/scripts/test-addon-number-param.cjs`・`npm --prefix frontend test`・`npx tsc --noEmit`: 既存のアドオン管理 UI・i18n・`saiverse-voice-tts` の `PersonaPanel.tsx` 込みの型整合を検証。
- 実機検証済み: まはーが本番相当の画面で `AddonManagerModal` のペルソナ別設定下からテスト再生・WAV 保存・一括 ZIP 出力を通り問題ないことを確認（2026-11-26）。

## 経緯

- 2026-11-23: `saiverse-voice-tts` のテスト再生・WAV/ZIP 出力機能の配置相談を受け、ペルソナ別設定のすぐ下に置ける本体拡張 (`ui/PersonaPanel.tsx`) を採用。SAIVerse v0.3.35 向けに実装し、アドオン側 (v0.7.0) は v0.3.35 リリース後に `min_saiverse_version: "0.3.35"` でカタログ登録する方針で合意。
- 2026-11-26: まはーが実機確認で問題なしと確認。本体 (`feature/addon-persona-panel`) とアドオン (`feature/voice-test-and-batch-export`) の PR を作成。
- 2026-11-27 (v0.3.35 のリリース準備で台帳を現況化): 本体側の PR #519 と #520 は develop に入り、v0.3.35 の範囲に入った。台帳にあった行:

| 🟣 検証待ち | アドオン管理のペルソナ別パネル枠 (`ui/PersonaPanel.tsx`) と Voice TTS テスト再生・一括 ZIP 出力 | 本体側の `PersonaPanel` 枠と `saiverse-voice-tts` v0.7.0 のテスト再生・WAV/ZIP 出力はまはーが実機で確認済みで、両リポジトリの PR がレビューを待っている。次 = まはーが PR をマージし、v0.3.35 発行後にカタログへ `min_saiverse_version: "0.3.35"` で載せる。 | まはー (PR のレビュー) | [intent](../intent/addon_persona_panel.md) | 2026-11-26 |
