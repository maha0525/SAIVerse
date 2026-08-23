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

## 原因が分かったあとの手当ての候補 (原因を見てから決める)

- 整数欄を無くす: 参照はプロンプトに載っている語をそのまま写す文字列 (`core:3` / `act:2`) にし、こちらで解決する。ループしても JSON としては読めるので、その要素だけ棄却できる。
- 出力トークンに上限を置く (スルースの応答は本来数百〜千トークン)。ループしても 79 秒・数万トークンの課金にならない。
- 解析失敗時に生の応答を残す (SDK の解析より前で) — 次に同じことが起きたとき、本文が無いまま推測する状態を繰り返さない。

## 別 issue との関係

[cold_anchor_advance_bypasses_sluice.md](cold_anchor_advance_bypasses_sluice.md): スルースが失敗しても起点が進んでいた。本 issue が直っても、あちらが直らなければ退場の不変条件は守れない。両方直してからリリース。
