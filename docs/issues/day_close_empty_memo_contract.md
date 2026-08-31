# 就寝判断の空メモ契約 — 「意図的な省略」と「劣化出力」を区別できない

**発見**: 2026-07-29（判断プロンプト混入修理 c77bc40 の Codex 再レビュー medium1）
**状態**: 未解決（設計判断待ち = まはー）
**関連**: `builtin_data/tools/judgment_finalize.py`（day_close 適用）、`docs/intent/persona_cognition/judgment_points.md` §8

## 現状の挙動（c77bc40 時点）

- `tomorrow_memo` はスキーマ上必須だが**空文字が有効**。
- 空のときは WARNING + 「（明日へのメモは残さなかった）」の要約行を残し、保存も成功エコーもしない（偽成功の排除は済み）。
- ただし判断そのものは applied=False のまま**台帳では完了扱い**になり、スケジュールは前進する。再試行はされない。

## 何が問題か

「本人がメモを残さないと決めた」のと「LLM 出力が切断・劣化して空になった」を**同じ形で受け取る**ため、後者でも翌朝への引き継ぎが静かに消える。旧実装は day_digest の保存が事実上の保険になっていたが、それは撤去済み（撤去自体は正 — 生材料の再供給だったため）。

なお窓に残る就寝の独白が実質の引き継ぎとして働くので、消えるのは「メモ」経路だけ（完全な記憶断絶ではない）。

## 修正方針の候補（Codex 提案ベース）

1. response_schema に**明示の省略フィールド**（例: `no_tomorrow_memo: true`）を追加。
2. 明示なしの空文字は finalize 失敗として台帳確定前に戻し、スケジュール側の有限回再試行に載せる。
3. 明示的な省略だけを「何もしない成功」として確定する。

歯止めの条件を「空かどうか（種類）」でなく「省略の意図があるか（目的）」で書く形——`feedback_guard_conditions_from_purpose_not_category` と同型。

## 実装時の注意

- Gemini structured output のスキーマ制約（additionalProperties 不可等）の範囲で書けるか確認。
- 再試行の載せ方は ScheduleManager の分類（applied/completed → executed）と整合させる。無限再試行にならないこと（現状はならない——逆に一度もされない）。
