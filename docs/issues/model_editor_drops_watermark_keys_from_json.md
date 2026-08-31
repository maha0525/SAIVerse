# モデル編集の追加設定 JSON に書いた水位キーが、保存時に黙って消える

**発見**: 2026-08-31 (掃討の水位バー追従検証中、まはーが実機で発見 — JSON に `"metabolism_high_chars": 150000` を書いて保存 → 次に開くと消えている)
**状態**: 🔲 未解決 — 原因特定済み・修正方針確定 (まはー案)。吸収改修のコミット後に着手
**深刻度**: P3 — 専用欄に入れれば設定は効く。ただし「書いた値が無言で消える」のはユーザーの意図の黙殺で、設定が効かない誤解を生む

## 原因

`ModelEditorModal.tsx` の設計 (2026-07-30): 水位 3 項目 (`metabolism_high_chars` / `metabolism_target_chars` / `metabolism_low_chars`) は専用欄が**単独所有**し、追加設定 JSON からは保存時に常に除外する。二重所有だと「欄を空にしても JSON 側の値が復活する」(当時の Codex 指摘) ため。その帰結として、JSON に書いた水位キーは**警告なしに剥ぎ取られる**。

## 修正方針 (2026-08-31 まはー案で確定)

保存時、追加設定 JSON に水位キーを見つけたら剥ぎ取るのではなく**専用欄へ引き取る**:

- 専用欄が空 → JSON の値を採用して欄に入れる (null は "none" として引き取る)。
- 専用欄に値がある → 見えている欄が勝つ (JSON 側は破棄 — ここは従来どおりだが、可能なら「JSON の水位は専用欄へ引き取りました」の一言を出すとなお良い)。

所有は専用欄一本のまま (JSON には保存しない) なので、7/30 の「復活」問題は再発しない。

## 関連

- `frontend/src/components/settings/ModelEditorModal.tsx` — `WATERMARK_FIELDS` / 保存時の extraJson 剥ぎ取り
- [issue: chat_options_metabolism_section_redesign (archive 想定)](chat_options_metabolism_section_redesign.md) — 水位のモデル定義一本化 (7/30)
