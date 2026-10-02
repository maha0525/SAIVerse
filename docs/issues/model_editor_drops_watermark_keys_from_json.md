# モデル編集の追加設定 JSON に書いた水位キーが、保存時に黙って消える

**発見**: 2026-08-31 (掃討の水位バー追従検証中、まはーが実機で発見 — JSON に `"metabolism_high_chars": 150000` を書いて保存 → 次に開くと消えている)
**状態**: 🟣 検証待ち — 修正を PR で提案。develop への取り込みと画面での確認は未実施
**深刻度**: P3 — 専用欄に入れれば設定は効く。ただし「書いた値が無言で消える」のはユーザーの意図の黙殺で、設定が効かない誤解を生む

## 原因

`ModelEditorModal.tsx` の設計 (2026-07-30): 水位項目 (`metabolism_high_chars` / `metabolism_target_chars`。発見当時は `metabolism_low_chars` も — 低水位は 2026-09-04 に廃止され専用欄から外れた) は専用欄が**単独所有**し、追加設定 JSON からは保存時に常に除外する。二重所有だと「欄を空にしても JSON 側の値が復活する」(当時の Codex 指摘) ため。その帰結として、JSON に書いた水位キーは**警告なしに剥ぎ取られる**。

## 修正方針 (2026-08-31 まはー案で確定)

保存時、追加設定 JSON に水位キーを見つけたら剥ぎ取るのではなく**専用欄へ引き取る**:

- 専用欄が空 → JSON の値を採用して欄に入れる (null は "none" として引き取る)。
- 専用欄に値がある → 見えている欄が勝つ (JSON 側は破棄 — ここは従来どおりだが、可能なら「JSON の水位は専用欄へ引き取りました」の一言を出すとなお良い)。

所有は専用欄一本のまま (JSON には保存しない) なので、7/30 の「復活」問題は再発しない。

## 関連

- `frontend/src/components/settings/ModelEditorModal.tsx` — `WATERMARK_FIELDS` / 保存時の extraJson 剥ぎ取り
- [issue: chat_options_metabolism_section_redesign (archive 想定)](archive/chat_options_metabolism_section_redesign.md) — 水位のモデル定義一本化 (7/30)

## 経緯

- 2026-10-02: 確定方針に沿って、保存時に JSON の水位を空の専用欄へ引き取る変更を提案。数値と null を受け付け、非数値・正の整数以外は保存前に既存の入力エラーで止める。専用欄に値があればそちらを優先する。引き取りと同時に JSON の同名キーを取り除くため、保存失敗や水位の順序違反のあとで欄を空にしても復活しない。廃止済みの水位キーや他の追加設定は従来どおり保つ。
- 検証: 実際の保存処理を TypeScript AST から読み出し、作成/編集、数値/null、専用欄優先、不正値、実効既定値との順序、保存失敗後の再操作を、偽 API とローカルの state setter で確認する回帰を `frontend/scripts/test-model-editor-watermarks.cjs` に追加した。画面描画と実バックエンドへの永続化はこの回帰の対象外。
