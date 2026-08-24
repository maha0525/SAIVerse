# スルースの構造化出力が整数欄で数字のループに入り、一度も成功していない

**状態**: 🟡 修正済み、実機検証待ち (2026-08-23 起票 / 2026-08-24 実装)。隔離実験で 30/30 正常だった型を実装したが、**測ったのは一つの合成入力だけ**なので、入力が変わったときの信頼性はエリスの実機で初めて分かる。ここに置いたままにするのはそのため。

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

**再現性 (2026-08-24、同じ body を各 10 回、API 30 回、まはー裁定「各 10 回」)**: 3 モデルとも **10/10 正常** (STOP・JSON 妥当・7 欄揃い・参照が一覧の値そのもの・反復と長大数字列なし)。出力トークン 195〜411、最長の数字列は「2026」の 4 桁。30 回中 29 回が `core:2` の書き換え 1 件、1 回 (3.5-lite) は削除 + 追加で同じ内容を表現 (正常)。4 本は本文を全文読んで中身の筋も確認。旧 schema が本番で 7/7・隔離で 3/5 壊れていたのに対し、新 schema は 30/30。**測ったのは一つの入力だけ**なので、入力が変わったときの信頼性は実機 (エリス) で確かめる。

精度の区別: 1 回目の実験の時点では各 1 回だった (その後 10 回で裏付け)。収まった原因が「必須と順番を文法で縛ったこと」だというのは推測 (変えた変数は三分割 + 必須 + 順序がひと塊で、分離していない)。ただし「使えない update (本文無し) が出ない」という別の効果は確実に出ている。

## 手当て (実験を踏まえた案、裁定待ち)

- **操作を種類ごとの一覧に分けて、必須と順番を文法で縛る** (実験で効いた唯一の条件): 追加は本文必須、書き換えは参照と本文が両方必須で参照が先、削除は参照必須。`op` 一本で混ぜるのをやめる。規律として一般化すると「**任意の欄を飛ばした先に、飛ばした内容を吐き出せる欄が来る型を作らない**」— モデルが書きたいものに対応する必須欄を、その順番で用意する。
- 参照は文字列 (`core:3` / `act:2`、プロンプトに載っている語の写し) にし、こちらで解決する。整数型は Python 側の桁数上限で**落ちる**ので、少なくとも落ち方は変わる (暴走そのものの抑止ではない — 文字列参照の実験で確定)。
- **出力トークンに上限** (スルースの応答は本来数百〜千トークン。4,096 程度)。万一ループしても 79 秒・数万トークンの課金にならない。
- 参照が提示した集合に無ければ要素棄却 (CAS の検査で既に守れている)。
- 解析失敗時に生の応答を残す — 次に同じことが起きたとき、本文が無いまま推測する状態を繰り返さない。

## 手当ての実装 (2026-08-24、まはー裁定「この型で実装する」)

送信内容は実験で通した body と**機械で一致を確認**した (schema・プロンプトとも完全一致)。

- **[`sea/sluice.py`](../../sea/sluice.py)**: `ops` を廃止し `core_adds` / `core_updates` / `core_removes` の三一覧へ。参照は文字列 (`core:N` / `act:N`) で、書式は `^core:([0-9]{1,9})$` の完全一致だけ通す (桁数を縛るのは、暴走した数字列を `int()` へ渡すと変換上限で pan 全体が落ちるため)。形が違う参照・一覧に無い参照はその要素だけ棄却して判断ターン記録に残す。**schema に整数・数値型の欄はゼロ** (`promises` は元から文字列と真偽値のみ — 確認済み)。プロンプトの呼び名 4 箇所も実験と同じ文言へ。
- **出力上限**: このコールだけに `max_output_tokens=4096`。`llm_clients/gemini.py` の `generate` / `generate_stream` に per-call の引数を足し、モデル設定より優先させた。他プロバイダのクライアントは `generate(**kwargs)` が黙って落とす (上限が効かないだけで例外にはならない)。
- **解析失敗時の生の本文**: google-genai 1.63.0 は `response_schema` が dict のとき `GenerateContentResponse._from_response` の中で `json.loads` し、`JSONDecodeError` しか捕まえない。数字ループは素の `ValueError` で抜けるため応答オブジェクトごと失われる。トレースバックから当該フレームの局所変数 `result_text` を取り出して `saiverse.llm` (llm_io.log) に WARNING で全文を残す形にした (実物の SDK で 65,034 字の回収を実測)。SDK の内部名に依存するので、取れなければ「本文は SDK 内で失われた」と例外メッセージに書く。
- **実行台帳**: 旧形式 (`ops` を持つ) の記録済み結果は再利用しない。そのまま適用側へ渡すと三一覧が空として読まれ「採取ゼロ」で completed になる (本人が指定した記憶操作が静かに消える)。台帳は applied → failed の遷移を許さないので、旧行はそのまま残し、形式印つきの別キー (`{persona}:{span}#format-core3`) で新しい LLM コールを立てて採り直す。本番に旧形式の applied 記録は無い (全部 failed) が、開発 DB にはありうる。

回帰テスト: `tests/test_sluice.py` (参照の書式・必須欄・手帳の参照・旧形式の記録・schema の形) と `tests/test_gemini_latest_contract.py` (出力上限の per-call 優先・実 SDK での本文回収)。歯止めを一つずつ無効化して、対応するテストが落ちることを実測した。

## 同じ危険がどこまで及ぶか — 機械検査で数え直した (2026-08-24)

スルースの型を直しただけでは、この事故は「スルースの事故」として閉じてしまう。**数値欄を Gemini に向ける限りどこでも起きる**ので、見張りを型ひとつではなく経路全体にかけた。

検査は `tests/test_response_schema_no_numeric_fields.py`、走査の道具は `tests/schema_scan.py`。手で並べた一覧ではなく、その場でファイルシステムを走査するので、新しい Playbook やスペルが増えたら何もしなくても対象に入る。

型が制約付きデコードに届く経路は 3 つあった。

- **経路 A — Playbook JSON の `response_schema`**: `builtin_data/playbooks/` に直接書かれた型 (`archive/` は退役済みで対象外)。15 型を走査して違反 7 欄。
- **経路 B — Python 側で組み立てる型**: `sea/sluice.py` の `_RESPONSE_SCHEMA` (修正済み)、`saiverse/judgment_points.py` の判断点、`sai_memory/curation_ops.py` の夜間編纂など。実行時に enum を注入するものがあるため、関数を呼ばずソースを構文解析して型の literal を探す形にした。33 型を走査して違反 2 欄。
- **経路 C — スペルの引数の型が `response_schema` に化ける (この検査を作る過程で見つかった)**: `builtin_data/playbooks/public/spell_args_decider.json` が `"response_schema_source": "spell:{spell_name}"` を持ち、ランタイム (`sea/runtime_llm.py` の `_resolve_response_schema_source`) が `SPELL_TOOL_SCHEMAS[spell_name].parameters` を解決する。**ペルソナが引数指定なしの形 (`/spell name='X'`) でスペルを唱えると、そのスペルの引数の型がそのまま構造化出力の型として Gemini へ渡る。** 33 型を走査して違反 13 欄 (10 スペル)。

**違反は合計 22 欄。** 起票時に数えていた「4 Playbook」は経路 A のファイル数で、欄単位でも経路単位でも足りていなかった。

| 経路 | 違反 | 内訳 |
|---|---|---|
| A: Playbook JSON | 7 欄 | 建物作成 `capacity` / 文書検索 `start_line`・`end_line` / 予定管理 `days_of_week[]`・`interval_seconds`・`schedule_id` / Web 調査 `max_results` |
| B: Python | 2 欄 | `judgment_points.py::_build_slot_schema $.budget_rounds` (v0.4 の配線前に直す) / `curation_ops.py::plan_split $.sections[].block_indices[]` (編纂の再開前に直す) |
| C: スペルの引数 | 13 欄 | `document_read` (3) / `document_search` (2) / `game_create_building` / `memory_clip` / `messagelog_get_around` / `pocketbook_open` / `read_url_outline` / `read_url_section` / `resolve_uri` / `send_email_to_user` |

**22 欄が等しく危ないとは限らない。** 実験で壊れた形は「本文を書きたいのに任意の欄を飛ばして、隣の数値欄へ本文を吐き出す」並びだった。数値欄の隣に文章を書く欄があり、しかも欄が任意になっているものが危ない。純粋に数を訊いているだけの欄 (`resolve_uri` の `max_total_chars` など) は同じ道に乗っているというだけで、実際に壊れるかは叩いてみないと分からない。**どれを直すかは隔離実験で裏を取ってから決める** (この issue の再現実験と同じ手順)。それまでは検査の `KNOWN_NUMERIC_FIELDS` に既知として載せてあり、直したら行を消す。

一覧は両方向で検算する — 載っていない数値欄が現れても落ちるし、載っているのに見つからなくなっても落ちる。片方向だけの一覧は時間が経つほど嘘に近づくため。歯止めが実際に落ちることは両方向とも実測した。

**検査の穴 (設計上の割り切り、`tests/schema_scan.py` の冒頭にも明記)**: `expansion_data/` のアドオンが持ち込むスペル、`~/.saiverse/user_data/` の利用者定義、DB の `playbooks` テーブルに直接入った型は走査の外。特に `saiverse/composite_actions.py` の `_action_param_schema` はアドオンの JSON に書かれた `integer` / `number` をそのままスペルの引数の型へ写すので、数値欄を持つスペルをアドオンが作れてしまう。塞ぐなら登録の入口で検出する形が要る。

**関数呼び出し経路は今日は届いていない**: `sea/runtime.py::_build_tools_spec` が `GEMINI_TOOLS_SPEC` から宣言を組む経路は、ノードに `available_tools` がある場合だけ発火し、現役 Playbook で持つものは 1 本も無い (`archive/` の 4 本のみ)。ただし `llm_clients/gemini.py` の `generate_stream` は `tools` を省略すると登録済み全ツールを宣言として送る既定値なので、`tools=[]` の書き忘れが将来この経路を開く。現行の呼び出しは全て明示済み。**関数呼び出しの引数でも同じ桁のループが起きるかは未検証。**

## 別 issue との関係

[cold_anchor_advance_bypasses_sluice.md](cold_anchor_advance_bypasses_sluice.md): スルースが失敗しても起点が進んでいた。本 issue が直っても、あちらが直らなければ退場の不変条件は守れない。両方直してからリリース。
