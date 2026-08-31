# Issue: pdf_read が 8000字で非明示に打ち切り、続きを読む手段が無い

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-07-25
**関連**: `builtin_data/tools/pdf_read.py`, `builtin_data/tools/document_read.py`

## 問題

`pdf_read` は `max_chars`（default 8000）で本文を打ち切るが、**打ち切られた続き（8001字目以降）をオフセットで指して読む手段が無い**。そこにある情報が、非明示的な理由でペルソナに渡らない。

`document_read`（テキスト系）は `start_line`/`end_line`/`limit` を持ち「続きから」読めるのに対し、`pdf_read` にはその相当が無い。**同じ「読み取り」なのに PDF だけオフセット方式が欠落**している非対称。

### 現状の回避手段（いずれも不十分）

- `pages`（ページ範囲）: ページ単位。1 ページ内で `max_chars` を超えて切れると、その続きには届かない。
- `max_chars` を大きい値で渡す: 上限を上げるだけ。「前回の続き」をピンポイントで指す口ではない。

### 非明示性

打ち切り時は末尾に `... (truncated)` / `... (truncated at N chars)` は出る（`pdf_read.py` 内 2 箇所）。だが「続きをどう取るか」の指示は無く、ペルソナが自力で `pages` をずらすか `max_chars` を上げるのを思いつく必要がある。情報が渡ってこないことが伝わりにくい。

## 解決案

`document_read` の作法に寄せ、抽出後テキストに対する**文字オフセット指定**（`start_char`/`end_char` 相当、あるいは連続読み取り用のカーソル）を `pdf_read` に持たせる。「全文に届く手段」と「1 回の読み取り量上限」を分離し、打ち切りを機構の反射ではなく明示的な範囲操作にする。

## 関連

- メモリ: `feedback_no_mechanical_truncation_design`（「文字数上限で切る」を設計の反射にするな。上限は実害の根拠がある時だけ、全文到達手段とセット）
- メモリ: `feedback_no_truncation_in_persona_memory_text`（ペルソナに渡すテキストの切り詰めは本人発言の捏造に相当）
- `docs/issues/word_docx_read_support.md` — docx_read を新設する際、この truncate 作法を写さず本 issue の方針（範囲指定で全文到達）に寄せる

## ログ

- 2026-07-25: issue 起票。docx 読み込み対応の検討中に、pdf_read のオフセット欠落を同じ穴として分離起票。
