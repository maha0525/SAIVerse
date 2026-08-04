# 分割設計書: `runtime_llm.py` の巨大閉包 `node` (1,616行)

**ステータス**: 🟠 実装中 — **Phase 0 + Phase 1 実装済み（2026-07-22、実機スモーク待ち）**、Phase 2 以降 未着手
**優先度**: high（自律行動 v2 で Spell loop / Beat 周りに入る前の準備リファクタ）
**作成日**: 2026-07-06
**関連**: `docs/overview/architecture_health.md` §3.1、[Beat 型 issue](beat_concept_not_typed_in_implementation.md)、`docs/issues/spell_html_leak_into_saimemory.md`
**行番号の基準**: commit `b4ca78e`（branch `feature/autonomous-behavior-v2`）時点。ズレたら関数名・コメント文言で探すこと。
**⚠️ Phase 0 実装で行番号は全面的にズレた** — 以降の Phase は関数名・コメント文言で探すこと

## 目的

`sea/runtime_llm.py` の `lg_llm_node`（L2141）が返す閉包 `node` は 1,616 行あり、
Beat 生成の心臓部でありながら部分テスト不能・修正事故率が構造的に高い。
これを**挙動不変の機械的分割**で解体する。

### Beat との関係（2026-07-22 改訂）

起草時（2026-07-06）は「分割の副産物として
[Beat 概念に型を与える issue](beat_concept_not_typed_in_implementation.md) が解決する」と
書いていたが、その後の統合工事（[beat_execution_context.md](../intent/beat_execution_context.md)
§6-1 / §6-2、2026-07-17 実装済み）で **Beat の別の面が先に型になった**。整理すると：

| Beat の面 | 型・機構 | 状態 |
|---|---|---|
| **身分**（誰の・どの thread / line / aspect / model / pulse の実行か） | `ExecutionContext`（`sea/pulse_context.py`） | ✔ 実装済み |
| **直列性**（persona 単位のロック + 実行台帳の関所） | `BeatGate`（`sea/beat_gate.py`） | ✔ 実装済み |
| **中身**（表示用 merged 全文 と SAIMemory 用 continuation の対） | `BeatExecution` → Phase 1 | 🔲 本設計書の担当 |

つまり本設計書が残して解くのは **3 行目だけ**。したがって Phase 1 の
`BeatExecution` は §3.1 の草案どおり persona / building_id を自前で持つのではなく、
**`ExecutionContext` を 1 フィールドとして保持**し、身分の再宣言をしない形にする
（不変条件「実行の身分は一度だけ解決する」= beat_execution_context.md §4-1 を破らないため）。

**この設計書の読み方**: §1 で現状の地形、§2 で重複（分割の主対象）、§3 で目標構造、
§4 で段階手順、§5 で**壊してはいけない不変条件**。実装者は §5 を最初に読むこと。

---

## 1. 現状の構造マップ（`node` 閉包の内訳）

```
2142  async def node(state):
2143-2174   [前処理] cancellation / pre-spells / realtime spells
2176-2198   [前処理] status event / テンプレート変数構築
2199-2244   [①準備] prompt 構築（action → <system> タグ wrap。L2210-2239 の設計判断コメント必読）
2246-2274   [①準備] response_schema 解決（response_schema_source / playbook enum）
2276-2295   [①準備] LLM クライアント選択 + モデル別 system prompt 注入
2297-2304   [①準備] available_tools / spell_enabled 判定
2306-2812   [②生成] Branch A: ツールありモード
   2323-2544    A-stream: streaming tool mode（空応答 retry / tool_detection / both・text 分岐 emit）
   2545-2587    A-sync: 同期 tool mode
   2589-2694    A-共通: output_keys 解析 / bubble1 早期 emit / Spell loop / spell 後 emit
   2696-2810    A-共通: result 分岐（tool_call / both / text）→ state 書き込み
2813-3518   [②生成] Branch B: ツールなしモード
   2830-3321    B-stream: pipeline streaming
                （placeholder 発番 2858-2869 / retry 2871-2928 / cancel finalize 2930-2959 /
                  usage 2961-2998 / reasoning 3000-3016 / Spell loop + finalize 3018-3152 /
                  spell なし完了 3153-3229 / 504 re-speak 3231-3315）
   3322-3489    B-sync: 非 streaming（usage / reasoning / Spell loop / speak emit）
   3491-3518    B-共通: structured output 処理 / output_key(s) 格納
3519-3528   [例外] LLMError 変換
3529-3758   [④確定] state["last"] / assistant message 構築（tool_calls + thought_signature）/
             PulseContext 追記 / sea trace / memorize / important dual-write
3760-3781   persona_context ラッパー（node_with_persona_context）
```

生成部は **(tools あり/なし) × (streaming/sync) の4経路**が並んでおり、
経路間で下記 §2 のコードがコピーされている。これが 1,616 行の主因。

## 2. 重複インベントリ（分割の主対象）

| 重複ブロック | コピー数 | 位置 | 差分 |
|---|---|---|---|
| usage 記録（record_usage + calculate_cost + llm_usage_metadata 構築 + _accumulate_usage + anchor touch） | 4 | 2405-2435 / 2566-2587 / 2962-2996 / 3335-3367 | `node_type` 文字列のみ（llm_tool_stream / llm_tool / llm_stream / llm） |
| reasoning 回収（consume_reasoning → state["_reasoning_text"] / details） | 4 | 2394-2402 / 2555-2563 / 3001-3016 / 3370-3374 | stream 経路のみ thought_signature も後読み |
| msg_metadata 組み立て（base + llm_usage + reasoning + activity_trace + llm_usage_total + auto_recall） | 6 | 2465-2479 / 2518-2535 / 2670-2676 / 3084-3095 / 3179-3197 / 3432-3450 | 含めるキーが微妙に不揃い（意図的でない可能性が高い。統一時は要突合） |
| _emit_say + _last_message_id 捕捉 | 5 | 2481-2485 / 2537-2543 / 2689-2690 / 3225-3229 / 3452-3458 | ほぼ同一 |
| Spell loop 呼び出し + continuation 差し替え | 3 | 2618-2638 / 3035-3152 / 3391-3483 | pipeline 状態の有無 |
| 空応答 retry ループ（usage discard + 再試行） | 2 | 2332-2391 / 2871-2928 | tool 側は tool_detection の peek あり |
| streaming_complete イベント構築 | 3 | 2451-2461 / 2504-2514 / 3160-3170 | ほぼ同一 |

## 3. 目標構造

### 3.1 データの束: `BeatExecution` dataclass

閉包ローカル変数（数十個）のうち経路をまたいで受け渡されるものを 1 つの dataclass に束ねる。
これが **Beat の実装上の型**になる（[Beat issue](beat_concept_not_typed_in_implementation.md) の解決）。

```python
@dataclass
class BeatExecution:
    # 実行文脈（不変）
    # ⚠️ 2026-07-22 改訂: 身分（persona / thread / line / aspect / model / pulse）は
    #    ExecutionContext が既に持つ。ここで再宣言せず保持するだけにする。
    #    （Phase 1 では読み手が居ないため未導入。Phase 2 で入る）
    ctx: ExecutionContext
    persona: Any; building_id: str; node_def: Any; playbook: PlaybookSchema
    state: dict; event_callback: Optional[Callable]
    # ①準備の成果物
    messages: list; prompt: Optional[str]          # action 展開済 <system> prompt
    llm_client: Any; response_schema: Optional[dict]
    effective_tools: list; spell_enabled: bool
    # ②③生成・Spell の成果物
    text: str = ""                # 表示用（Spell 実行後は merged 全文）
    continuation: str = ""        # SAIMemory 保存用（最終発言のみ）
    spell_loop_count: int = 0
    reasoning_text: str = ""; reasoning_details: Any = None
    llm_usage_metadata: Optional[dict] = None
    # pipeline streaming（B-stream のみ）
    pipeline_msg_id: Optional[str] = None; pipeline_sub_seq: int = 0
    pipeline_eff_bid: Optional[str] = None

    @property
    def display_text(self) -> str: ...   # Building / UI 行き
    @property
    def memory_text(self) -> str: ...    # SAIMemory 行き（spell 時は continuation）
```

`display_text` / `memory_text` の対が「Beat が記録先で2つに割れている」問題の型による明示化。

### 3.2 最終ファイル構成（Phase 3 完了時）

| モジュール | 中身 | 出自 |
|---|---|---|
| `sea/spell_parsing.py` | `_parse_spell_*` / `_coerce_*` / `_SpellSpan` / エラー文構築（全て純関数） | runtime_llm.py L247-777 |
| `sea/spell_loop.py` | `_run_spell_loop` / `_execute_pre_spells` / `_execute_realtime_spells` / handy tool 実行 | 同 L778-2140 |
| `sea/beat_execution.py` | `BeatExecution` + 共有ヘルパ（§4 Phase 0 の関数群） | 新規 |
| `sea/llm_node.py` | `lg_llm_node` = ①準備 → ②4経路生成 → ③Spell/emit → ④確定 のディスパッチ | 同 L2141-3781 |
| `sea/runtime_llm.py` | 上記の re-export シム（既存 import 互換のため当面残す） | — |

②の4経路は `_generate_tools_streaming` / `_generate_tools_sync` /
`_generate_plain_streaming` / `_generate_plain_sync` の4関数
（`BeatExecution` を受け取り更新して返す）。重複排除後は各 100〜200 行に収まる見込み。

## 4. 段階手順（各段が独立に出荷可能・挙動不変）

### Phase 0: 重複ヘルパの抽出（最小リスク・最大効果） ✔ 実装済み 2026-07-22

§2 の上位4つを関数化して 4〜6 コピーを置換する。**新ファイル不要**（runtime_llm.py 内でよい）:

- `_record_llm_usage(runtime, llm_client, persona, building_id, playbook_name, node_type, state) -> Optional[dict]`
  — usage 記録一式 + anchor touch。戻り値は llm_usage_metadata
- `_consume_reasoning_into_state(llm_client, state) -> tuple[str, Any]`
- `_build_say_metadata(state, node_def, llm_usage_metadata, reasoning_text, reasoning_details, *, include_total: bool) -> dict`
  — ⚠️ 現状6コピーは含めるキーが不揃い（§2）。**Phase 0 では各呼び出し元の現状キー構成を引数フラグで忠実に再現**し、統一は別判断にする（挙動不変の原則）
- `_emit_say_and_capture(runtime, persona, eff_bid, text, state, *, pulse_id, metadata)`

#### 実装結果（2026-07-22）

抽出したのは 5 関数（設計の 4 + `_store_reasoning_in_state`）:

| 関数 | 置換したコピー | 設計との差 |
|---|---|---|
| `_record_llm_usage` | 4 | `debug_log` フラグ追加 — pipeline streaming 経路だけが出していた `[DEBUG]` 3 行を再現するため（ログも観測可能な挙動なので Phase 0 では変えない） |
| `_consume_reasoning` | 4 | 設計の `_consume_reasoning_into_state` を改名。ツールなし 2 経路は state 格納が Spell ループ後まで遅れるため、`state` は任意引数にして「回収だけ」も選べる形にした |
| `_store_reasoning_in_state` | 2 | 上の遅延格納点（B-stream / B-sync）用に分離 |
| `_build_say_metadata` | 6 | 設計どおり `include_total` フラグで現状差を忠実に再現。`node_def` は取らず、呼び出し元が解決済みの `base_metadata` を渡す形（completion_event と同じ値を使い回すため） |
| `_emit_say_and_capture` | 4 | 設計どおり |

- 閉包 `node` は 1,651 行 → 1,453 行（-198）。
- 回帰: 新設 `tests/test_runtime_llm_helpers.py` 20 件（経路ごとの metadata キー構成と挿入順、
  auto_recall の pop 消費、anchor touch が記帳後に来る順序、message_id 捕捉の分岐）+
  `tests/` 全体 3061 件全緑 + `ruff check sea/` clean。
- **残: まはー実機スモーク**（§「各 Phase の検証」の 4 パターン）。Phase 1 とまとめて 1 回で済ませる設計。

#### Phase 0 で見つけた非対称（直していない・Phase 2 で扱う）

1. **A-common の spell emit だけ `_last_message_id` を捕捉しない**（他 4 箇所は捕捉する）。
   spell 経由の発話の直後に走るツールが最新 message_id を引けない可能性がある。
   バグかどうか未確定なので Phase 0 では触っていない。
2. **metadata のキー構成が 6 経路で不揃い**（§2 の表のとおり）。フラグで現状維持した。
   統一の是非は「'both' 応答に `llm_usage_total` を載せない」「spell 経路に reasoning を
   載せない」に意図があったのかの確認が要る。

### Phase 1: ④確定部の抽出 + BeatExecution 導入 ✔ 実装済み 2026-07-22

L3529-3758（state["last"] / assistant msg / PulseContext / trace / memorize / dual-write）を
`_finalize_beat(runtime, exec: BeatExecution) -> None` に括り出す。
このとき BeatExecution を最小フィールド（text / continuation / prompt / messages / reasoning / usage）で導入。

#### 実装結果（2026-07-22）

- `BeatExecution`（dataclass）+ `_finalize_beat(runtime, beat)` を `runtime_llm.py` 内に追加。
  閉包末尾の 226 行がまるごと関数へ移り、`node` は
  `BeatExecution.from_node_locals(...)` → `_finalize_beat(...)` の 2 呼び出しに縮んだ。
- **抽出の忠実性を機械的に確認**: 移した本文を旧 HEAD の同ブロックと突き合わせ、
  差分は意図した 1 箇所（reasoning を `state.get()` でなく `beat.reasoning_text` から読む）
  のみであることを確認済み。
- 閉包 `node` は 1,453 行 → **1,241 行**（Phase 0 前の 1,651 から -410）。

**フィールドは「④確定が読むものだけ」に絞った**（設計の最小フィールド案からの意図的な差）:

| 設計案のフィールド | Phase 1 | 理由 |
|---|---|---|
| prompt / messages / continuation / reasoning_* | ✔ 入れた | `_finalize_beat` が読む |
| schema_consumed / node_id | ✔ 追加 | 同上（設計案に無かったが確定部が使う） |
| display_text（merged 全文） | ✘ Phase 2 | 確定部は読まない。生成 4 経路が beat を更新する形になって初めて書き手と読み手が揃う |
| llm_usage_metadata | ✘ Phase 2 | 同上 |
| ctx（ExecutionContext） | ✘ Phase 2 | 同上 |

読み手のいないフィールドを先に生やすと「state と beat のどちらが正か分からない値」が
増えるため、Phase 2 で生成経路が beat へ直接書くようになるのと同時に足す。
したがって **Beat 型の「対」（display_text / memory_text）が揃うのは Phase 2**。

- 回帰: 新設 `tests/test_beat_finalize.py` 29 件（continuation だけが記録へ行くこと /
  thought_signature の 3 経路 / memorize と dual-write の排他 / spell 由来記録の
  paired_action_text 抑止 / activity trace の meta_・sub_ ガード）+ `tests/` 全体 3110 件全緑。

### Phase 2: 4経路の分離

Branch A/B × stream/sync を §3.2 の4関数に分割。`node` 本体は
「前処理 → ①準備 → 経路選択 → ③Spell/emit → ④確定」の ~100 行のディスパッチャになる。

### Phase 3: ファイル分割 + Beat 型の公開

§3.2 の構成へ移動。`runtime_llm.py` は re-export シム化
（`from sea.llm_node import lg_llm_node  # noqa` 等）。
`BeatExecution` から表示/保存の対を持つ軽量 `Beat` を切り出し、
[Beat issue](beat_concept_not_typed_in_implementation.md) をクローズ。

### 各 Phase の検証（共通）

1. `ruff check sea/`
2. `python -m pytest tests/sea/ tests/test_work_session.py tests/test_day_scenario.py` +
   spell 系（`tests/` を `spell` で grep して該当を全部）
3. 実機スモーク（まはーとの共同確認は Phase 0+1 をまとめて1回で済ませる設計）:
   通常会話（streaming on）/ spell 入り発話 / TOOL ノード playbook / streaming off 各1回。
   `llm_io.log` と Building 履歴・SAIMemory の保存内容が分割前と同型であることを確認

## 5. 不変条件（壊すと事故る。コード内コメントと過去 issue から集約）

1. **`<system>` タグ + user role 形式は変更禁止**（L2210-2239 の設計判断コメント）。
   role='system' 化は Gemini 互換が壊れる。移設時はコメントごと持っていく
2. **Spell はテキスト順逐次実行**。`_run_spell_loop` / `_execute_pre_spells` の両経路とも
   並列 gather に戻さない（memory: `project_spell_loop_sequential`）
3. **SAIMemory へは continuation（plain）のみ**。merged 全文（`<user_only>` / spellResult HTML 入り）を
   保存すると重複 + HTML 混入（`docs/issues/spell_html_leak_into_saimemory.md`）。
   streaming（L3145-3152）と sync（L3475-3483）で対称に処理されている — 分割後も対称性を保つ
4. **pipeline placeholder の Building は開始時に固定**（L2853-2857）。finalize 時に
   `_effective_building_id` で再解決すると Pulse 中の移動（game_move_party 等）で
   placeholder が永久残留する（2026-06-11 Region RPG 実機事故）
5. **speak=false ノードは emit しない**（handoff route B、L2646-2651 / L3061-3080）が、
   pipeline placeholder の **finalize（空 voice で close）は必要**（L3067-3080）
6. **streaming の "both" 応答では text_chunks 側が正**（L2744-2747）。tool_detection の
   content は切り詰められている可能性がある
7. **空応答 retry は usage を discard してから**（L2378-2386 / L2913-2921)。
   tool-stream 側は retry 前に tool_detection を peek → put back する（L2372-2377）
8. **thought_signature の取得点は経路ごとに異なる**: stream = 全 chunk 読了後に consume
   （L3007-3011）/ sync・tool = result dict から。memorize / dual-write / assistant msg の
   3 箇所へ流れる（`docs/intent/thought_signature_persistence.md`）
9. **anchor touch は LLM 成功後**（Phase 4-e、各 usage 記録ブロック内）。
   prepare_context 側への先行 touch に戻さない
10. **cancellation はどの await 境界でも起きうる**。cancel 時の placeholder finalize
    （L2930-2959）は BeatExecution 化しても finally 相当の位置に残す
11. bubble1 早期 emit（`_emit_bubble1_early`）は Phase 2-C で撤去予定の過渡経路
    （L2656-2662 コメント）。分割時に消さない・依存もしない

## ログ

- 2026-07-06: アーキテクチャ健診（`architecture_health.md` §3.1）を受けて本設計書を起草（エア / Fable 5）
- 2026-07-22: Phase 0（重複ヘルパ抽出）実装。併せて Beat 型との関係を改訂
  （統合工事で `ExecutionContext` / `BeatGate` が先に入ったため、本設計書が担うのは
  「Beat の中身 = 表示用 / SAIMemory 用の対」だけに縮小）。
- 2026-07-22: Phase 1（④確定部の抽出 + `BeatExecution` 導入）実装。
  閉包は 1,651 → 1,241 行。Beat 型は器として立ち上がったが、
  「対」が揃うのは Phase 2（display_text の書き手が生成経路側にあるため）。

## 経緯: runtime_llm.py 巨大 node 分割 (2026-08-04 in_flight 台帳より移送)

> 台帳の器の再設計 (次アクション欄=前向きのみ) に伴い、それまで台帳セルに積もっていた経緯の全文をここへ移した。時系列の生の堆積であり、整理はしていない。

**Phase 0(重複ヘルパ抽出) + Phase 1(④確定部抽出 + `BeatExecution` 導入) 実装済(2026-07-22)** — 閉包 1,651→1,241 行、回帰 49 件新設 + tests/ 全緑(3110)。
次: **まはー実機スモーク**(通常会話 streaming on / spell 入り発話 / TOOL ノード playbook / streaming off の 4 パターン、llm_io.log と Building 履歴・SAIMemory が分割前と同型か)。
その後 Phase 2(4 経路の分離。
ここで Beat の「対」display_text が揃う)
