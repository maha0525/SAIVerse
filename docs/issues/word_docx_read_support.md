# Issue: Word (.docx) ファイルの読み込み対応

**ステータス**: 🔲 未着手
**優先度**: medium
**作成日**: 2026-07-25
**要望元**: SAIVerse ユーザー（第三者。まはー経由で共有）
**関連**: `builtin_data/tools/pdf_read.py`, `api/routes/media.py`, `frontend/src/app/page.tsx`

## 背景

ユーザーから「Word ファイルや PDF の読み込みができると嬉しい」という要望。用途と動機はユーザーの実感として次の通り（真偽の検証は依頼されていない。前提として受け取る）:

- 小説を読ませる際、チャットにテキスト直打ちするより**ファイルの方がセンシティブ表現が弾かれにくい**
- ファイルを**アイテムとして保持**することで、参照のオンオフがしやすく、入力ごと削除せずに**コンテキスト量を調整**できる

「アイテムとして保持 → 必要なときだけツールで読む」という構造は、既存の item + 読み取りツール方式で既に成立している。

## 現状（事実確認済み）

| 形式 | 読み取り | アップロード許可 |
|---|---|---|
| テキスト系 (.txt .md .csv .json .xml) | `document_read` (`builtin_data/tools/document_read.py`) | あり |
| PDF (.pdf) | `pdf_read` (`builtin_data/tools/pdf_read.py`、pypdf 抽出) | あり |
| **Word (.docx)** | **なし** | **なし**（`upload_document` で弾かれる） |

`document_read` は UTF-8 テキストとして行単位で読むだけなので、ZIP+XML の .docx はそのまま渡すと文字化けする。抽出ライブラリが必要。

## 解決案

### 案 A（推奨）: `docx_read` ツールを新設（PDF と対称）

`pdf_read` と同じ形で、item を `python-docx` で開いて段落テキストを抽出するツールを一本足す。PDF と設計が対称になり、既存の document item 経路にそのまま相乗りする。

### 案 B: `document_read` を拡張して拡張子で内部分岐

1 ツールで .docx/.pdf/テキストを吸収。ツール数は増えないが、1 関数が 3 形式を抱えて肥大する。

→ 案 A 推奨。

## 影響範囲（案 A の場合）

新規 DB テーブル・マイグレーション不要（item type=document のまま）。加算的変更で、既存経路を壊さない。

1. `requirements.txt` — `python-docx` 追加
2. `builtin_data/tools/docx_read.py` — 新規（ディレクトリ走査で**自動登録**、レジストリ編集不要）
3. `api/routes/media.py` — `upload_document` の許可を二段とも拡張（content-type `application/vnd.openxmlformats-officedocument.wordprocessingml.document` を `accepted_types` に、`.docx` を `allowed_extensions` に）。`upload_file` も同じ content-type 分岐を通るので合わせる
4. `frontend/src/app/page.tsx:2942` — チャット入力の `accept` 文字列に `.docx` 追加
5. `docs/reference/tool-catalog.md` — `gen_reference_docs.bat` で自動再生成
6. `tests/` — `test_document_tools_spell.py` に倣い docx_read のテスト追加

## 決めどころ（まはー裁定待ち）

- **`.doc`（旧バイナリ形式）は対象外でよいか**。対応するなら別変換が要る。まずは `.docx` だけで足りるか。
- **文字数上限の扱い**。`pdf_read` は `max_chars=8000` で truncate する。小説を読ませる用途では途中で切れると困る。docx_read で同じ上限を踏襲するか、それとも全文到達手段（ページ/段落範囲指定）とセットにするか。関連: メモリ `feedback_no_mechanical_truncation_design`（「文字数上限で切る」を設計の反射にするな）。

## 工数（AI スケール）

`pdf_read` が完成テンプレなので、写して python-docx の段落抽出に差し替える実装本体は短時間。ボトルネックは「実機で .docx をアップロードして読めるか」のまはーとの共同テスト 1 サイクル。

## ログ

- 2026-07-25: issue 起票。PDF 既存・Word 未対応を事実確認。既存 issue 重複なしを確認。
