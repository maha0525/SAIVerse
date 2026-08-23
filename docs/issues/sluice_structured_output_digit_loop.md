# スルースの構造化出力が整数欄で数字のループに入り、一度も成功していない

**状態**: 🔴 未解決 (2026-08-23 起票)。**v0.3 リリース前に必ず直す** — スルースが成功しないと退場が止まる (設計どおり) ため、記憶整理が完了しない。原因は未特定 (応答本文が SDK の解析前に落ちて残らない)。隔離環境での再現実験の承認待ち。

関連: [`sea/sluice.py`](../../sea/sluice.py) (response_schema) / [`llm_clients/gemini.py`](../../llm_clients/gemini.py) L1347 (`generate`) / google-genai SDK `types.GenerateContentResponse._from_response` (`json.loads(result_text)`)
出自: 2026-08-23 実機検証。

## 事実

- 2026-08-22 02:52 (束 3 の投入直後) 以降、`[ledger] failed ... kind=sluice.pan` が 7 回、成功は 0 回 (いずれもエリス、model=gemini-3.7-flash、構造化出力 + thinking MEDIUM + cached_content)。
- エラーは毎回同じ: `ValueError: Exceeds the limit (4300 digits) for integer string conversion: value has 65113 digits` (桁数は 64,9xx〜65,2xx で揃っている = 出力トークン上限まで数字を吐いて止まった形)。API は 200 で返り、SDK が `json.loads` する段で落ちる。所要 79 秒。
- 一回だけ別の失敗 (`server error`、2026-08-23 21:56) がある。
- スルースの出力で整数型なのは `want_memos[].activity_id` / `did_memos[].activity_id` / `ops[].memory_id` の三つ (いずれも任意)。プロンプトに載る ID の実物は小さい (エリス: コア記憶 core:1〜6、アクティビティ 1〜3、約束 0 件) — **長い数字を書かざるを得ない欄は無い** (2026-08-23 読み取り専用で確認)。

## 仮説 (未検証)

任意の整数欄は、モデルが `"activity_id": ` まで書いた時点で文法上「整数を出す」以外の道が無く、書くものが無いまま数字を並べ続ける (文字列欄と違って空の逃げ道が無い)。Gemini の構造化出力 (constrained decoding) で整数欄が暴走する形として筋は通るが、応答本文を見ていないので断定できない。

## 次の一手 (承認待ち)

隔離環境 (本番ペルソナ・本番の記憶を使わない) で合成の短い会話を作り、同じ response_schema で Gemini を一回叩き、**SDK を通さず生の応答本文を保存**する。出力上限は 2,000 トークンに絞る (ループしても入力 1 万トークン強 + 出力 2 千)。Gemini API への課金が一回発生する。これで「どの欄で、何を書こうとして、どう壊れたか」が見える。

## 再現実験の結果 (2026-08-24、隔離環境、まはー承認のもと API 5 回)

同じ合成会話・同じ schema・出力上限 2,000 で、モデルだけ替えて各 1 回。スクリプトと生の応答はスクラッチパッド (`sluice_repro/`) に保存。

| モデル | thinking | finishReason | `ops[0].memory_id` | `content` | 所見 |
|---|---|---|---|---|---|
| gemini-3.7-flash | medium | STOP | **201 桁** (`25425672` + ゼロ 192 + `2`) | 無し | 数字ループ (自力で脱出) |
| gemini-3.6-flash | medium | **MAX_TOKENS** | **1275 桁** (`2026` の反復) | 無し | 数字ループ (上限まで) |
| gemini-3.5-flash | high | STOP | `2` (正解) | 有り | 正常 |
| gemini-3.5-flash-lite | minimal | **MAX_TOKENS** | **1910 桁** (`20260702001` + ゼロ) | 無し | 数字ループ (上限まで) |
| gemini-3.1-flash-lite | minimal | STOP | `2` (正解) | (remove なので不要) | 正常 |

観察した事実:
- 壊れるのは常に `memory_id` — schema で唯一の整数欄。文字列欄 (`reflection` / `content` / `text`) は 5 応答とも正常。
- 思考部分を読むと、壊れた回もモデルは「core:2 を更新する」と値を決めていた。3.7 の数字列の最後の一桁は本来の `2`。**書くものが無かったのではなく、整数の途中で抜けられなくなった**。
- 壊れた 3 件はすべて `content` (更新後の本文) を省いていた。`required` が `op` だけで、`update` に本文が要ることを文法で縛っていない。
- 数字の種は `2026` や日付らしき並び — 会話中の年月日に引きずられた形に見える (推測)。
- 数字が短い回 (4,300 桁未満) は本番では**落ちずに静かに失敗する**: JSON は読めて、`memory_id` が存在しない番号として要素だけ棄却され、スルースは成功扱いで退場が進む。7 回のクラッシュは目に見えて壊れた側で、見えない失敗がほかにあった可能性がある。

Web の報告 (同じ症状):
- [Google 開発者フォーラム 2026-08-17](https://discuss.ai.google.dev/t/gemini-3-7-flash-schema-constrained-json-output-degenerates-into-repeated-0-until-maxoutputtokens-regression-vs-gemini-3-flash-preview/178681): gemini-3.7-flash で配列内オブジェクトの整数欄が `0` を繰り返して MAX_TOKENS まで走る。合成で約 1/3、本番相当で最大 100%。gemini-3-flash-preview では起きない (退行)。temperature 0・thinking 低でも防げず。3 日で 241 回暴走。Google の返答なし。
- [同 2026-07-17](https://discuss.ai.google.dev/t/structured-output-repetition-loop-inside-a-json-number-literal-runs-to-max-tokens-flash-vertex/175138): gemini-3.5-flash / 2.5-flash (Vertex) でも。核心の一文: 「JSON の数値の桁の列は文法で縛った出力では上限が無く、ループに入るとどのトークンも文法違反にならないので何も止められない」。プロンプト側の工夫は効かず。
- [cookbook issue #449 (2025-02)](https://github.com/google-gemini/cookbook/issues/449): Gemini 2.0 Flash の構造化出力で値の繰り返しと欄の欠落。

結論: **Gemini の構造化出力における「数値リテラル」の構造的な弱点**で、3.7 で特に悪化した。私たちの使い方の問題ではないが、数値欄を持つ schema を Gemini に向ける限り避けられない。文字列・enum・真偽値は文法的に閉じられる (`"` / 候補 / 二択で終わる) ので、この罠に入らない。

## 追加実験 (2026-08-24、壊れた 3 モデル × 2 条件、API 6 回)

**文字列参照** (整数欄 `memory_id` → 文字列 `memory_ref` に `core:N` を写す。他は同じ):

| モデル | 結果 |
|---|---|
| 3.7-flash | JSON は妥当だが `memory_ref` が 101 字 — `core:2reset core:2 -> core:2 update core:2 content: 2026年9月頃〜…` と**本文と推敲が参照欄に流れ込む** |
| 3.6-flash | **MAX_TOKENS**。`core:2` を 195 回反復 (`wait_schema_says_core:2_ref_only_ … memory_ref: core:2_ ok_`) — 参照欄の中で「何を書けばいいか」を悩み続ける |
| 3.5-flash-lite | `"core:2"` の 6 字で正常 |

→ **「整数だから壊れる」は反証された** (まはーの疑いどおり)。型を文字列にしても 3.6 は同じ重さで壊れる。壊れ方の中身が手がかり: 壊れた応答は全部 `op: "update"` のあと `content` を飛ばして参照欄に入り、参照欄の中に本文や迷いを吐き出している。

**種類別一覧** (`ops` + `op` 欄を廃止し、`core_adds` [本文必須] / `core_updates` [`memory_ref` → `content` の順で両方必須] / `core_removes` [`memory_ref` 必須] の三一覧に分ける。参照は文字列のまま):

| モデル | 結果 |
|---|---|
| 3.7-flash | STOP、`memory_ref` = `"core:2"` (6 字)、`content` 59 字。出力 347 トークン |
| 3.6-flash | STOP、`"core:2"`、`content` 48 字。出力 340 トークン |
| 3.5-flash-lite | STOP、`"core:2"`、`content` 47 字。出力 259 トークン |

3 モデルとも JSON 妥当、反復・独り言の混入ゼロ、本文の最長の数字列は「2026」の 4 桁。壊れた応答に共通していた「書き換えなのに本文を飛ばして参照欄へ入る」並びが、文法上もう作れない。

精度の区別: 各 1 回なので、再現性の程度は測っていない。収まった原因が「必須と順番を文法で縛ったこと」だというのは推測 (変えた変数は三分割 + 必須 + 順序がひと塊で、分離していない)。ただし「使えない update (本文無し) が出ない」という別の効果は確実に出ている。

## 手当て (実験を踏まえた案、裁定待ち)

- **操作を種類ごとの一覧に分けて、必須と順番を文法で縛る** (実験で効いた唯一の条件): 追加は本文必須、書き換えは参照と本文が両方必須で参照が先、削除は参照必須。`op` 一本で混ぜるのをやめる。規律として一般化すると「**任意の欄を飛ばした先に、飛ばした内容を吐き出せる欄が来る型を作らない**」— モデルが書きたいものに対応する必須欄を、その順番で用意する。
- 参照は文字列 (`core:3` / `act:2`、プロンプトに載っている語の写し) にし、こちらで解決する。整数型は Python 側の桁数上限で**落ちる**ので、少なくとも落ち方は変わる (暴走そのものの抑止ではない — 文字列参照の実験で確定)。
- **出力トークンに上限** (スルースの応答は本来数百〜千トークン。4,096 程度)。万一ループしても 79 秒・数万トークンの課金にならない。
- 参照が提示した集合に無ければ要素棄却 (CAS の検査で既に守れている)。
- 解析失敗時に生の応答を残す — 次に同じことが起きたとき、本文が無いまま推測する状態を繰り返さない。

## 別 issue との関係

[cold_anchor_advance_bypasses_sluice.md](cold_anchor_advance_bypasses_sluice.md): スルースが失敗しても起点が進んでいた。本 issue が直っても、あちらが直らなければ退場の不変条件は守れない。両方直してからリリース。
