# Intent: Codex 応答重複の切り分け

**ステータス**: 診断用 draft。報告事象の根因は未確定・未修正。

## 全体と責任

利用者とペルソナは、応答の表示・記憶・Spell 実行のすべてで、提供元から
届いた発言を余分に増やさず、正当な繰り返しも失わないことを必要とする。
旅程は Codex SSE → `OpenAICodexClient._iter_chunks` → SEA の本文結合 →
Spell 抽出・実行 → 発言の保存と UI 表示。まず、SSE パース後の集約入口で何が届き、
受信側が何文字補ったかを確定させる。UI や Spell 層で同じ本文を消しても、
保存や別の消費者へ渡る本文の正しさは保証できない。

## 今回分かっていること / 分かっていないこと

2026-10-03〜04 の Discord 報告 (v0.3.20 / GPT-5.6 Sol Codex):
完全な回答が2回現れ、Spell のない通常会話でも発生することがある。
別の1応答には同一 `document_edit` が4件あり、1件成功後に3件が対象なしで失敗した。
スクリーンショット・当該ログ・生の受信イベントはまだ未取得。

- 正常な delta → `output_text.done` → `content_part.done` → `output_item.done`
  → `response.completed` では、後続イベントが同じ本文を載せても再出力しない。
- **合成した**同一 item / content part の `output_text.done` 再送では、
  現行の `delta_consumed` が既に進んでいるため、完了本文を再度 yield する。
  同一 done 2回→本文2回、改行終端の Spell を4回→4行を隔離環境で確認した。
  これは今回そのイベントが届いた証拠ではない。
- SEA は渡された本文の各 Spell 行を順に実行する。`document_edit` の置換後に
  同じ old_string が無くなれば、その後の呼び出しが失敗するのは整合する。
  下流だけが1行を4件に増やす経路は見つかっていない。
- `llm_io.log` は Spell round と node の双方で最終 continuation を記録し得る。
  同じ response が2つあるだけで、API が二重生成したとは判断しない。
- 既知の UI の最後の bubble による照合問題は、実ツール4回を説明しない。
- assistant の `phase` を履歴へ保持しない制約は別の候補であり、今回の原因と
  結び付ける証拠はない。本変更で履歴契約を変更しない。

[先行 PR #323](https://github.com/maha0525/SAIVerse/pull/323) の
[2026-09-27 commit](https://github.com/maha0525/SAIVerse/commit/8302996908feca500e6bc32e37d287ee7d443a6b)
(maha0525、Claude Fable 5 co-author) は、未観測の重複・欠落・逆転への照合を
入れず、実測時に再訪すると記録している
([旧 issue](../issues/archive/codex_multi_message_spell_concatenation.md))。
TCP の順序保証だけではサーバー側の同じイベント生成を否定できないが、
合成再送だけを根拠に今回の原因を決めることもできない。この段階では診断だけを足す。

## 最小の観測

`backend.log` の DEBUG に `Codex stream diagnostic` を出す。
追加の設定・保存先・外部送信はない。INFO では生成しない。

- ローカルな `stream` 印で1回の `_iter_chunks` を区別する。
- item / response / call / function名 / 本文・引数の同一性は、**応答内だけの数字の別名**
  (`*_ref` / `ref`) にする。生の ID・本文・引数・推論・トークン・本文ハッシュは出さない。
  同じ ref は同一文字列、異なる ref は異なる文字列。別の stream では比較できない。
  空文字は `chars: 0, ref: null`。
- added / done / completed 等の境界だけに、sequence / output / content の index、
  既知の phase、文字数を残す。done では現在の集約が照合した部分
  (`streamed_part`) と、実際の補完条件で追加される長さ (`recovered_chars`) を残す。
  `streamed_part` はグローバルカーソルの切片であり、正しい part 対応を保証する値ではない。
- delta は通常記録しない。数値の sequence が直前以下になった場合だけ、
  `sequence_nonincreasing: true` の形情報を残す。これ自体を再送の断定には使わない。
- 最後の `assembled` は実際に下流へ渡す本文の文字数・同一性と function call 件数。
- 観測によって本文・tool call・再試行・保存を変えず、重複排除もしない。
  **同一本文の別 item、別 part、別 call ID はすべて元の回数のまま残す。**

## 確認の旅程と終了条件

fake SSE (UTF-8 のバイト境界も分割) → 実際の受信集約 → visible chunk と終端 state、
および `generate` / `generate_stream` をネットワーク・認証なしで検査する。
`generate_stream` → 実 SEA stream 消費 → 実 Spell parser でも1行と4行を確認し、
ツール実行の直前で止める。文書変更・永続保存はこの検査には含めない。
通常 lifecycle の全文再掲、複数 item / part、本文だけの Spell、同引数で別 ID の
function call、DEBUG の有無、ログへ機密文字列が含まれないことを回帰にする。
未観測の done 再送は**診断テスト**としてのみ扱い、正しい出力の仕様とはしない。

報告者の通常利用で発生時刻・吹き出しの形・該当する診断と既存ログを照合し、
上流の別発言 / 同じイベント / 補完による増加 / 下流の問題を絞るまで根因は未確定。
この記録はパース後なので、生の wire 再送とパーサー由来の複製を単独では分離しない。
stream 印は SEA / pulse へ伝播しないため、並行呼び出しと特定の吹き出し・実行の
対応も単独では保証しない。既存ログ・発生時刻との照合が必要になる。
本番ペルソナ・有料 API・既存の履歴には接触していない。原因が決まったら、その
責任境界で修正し、今回用の診断を残す必要があるか見直す。

仕様の比較対象 (実際の接続先は公開 API ではなく Codex backend):
[公式イベント定義](https://developers.openai.com/api/reference/resources/responses/streaming-events)
