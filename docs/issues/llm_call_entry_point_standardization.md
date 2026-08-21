# Issue: LLM を呼ぶ口が乱立している — 少数の玄関へ規格化する

**ステータス**: 🔲 未着手（明文化のみ。今すぐ実装する案件ではない）
**優先度**: medium（放置すると口が増え続ける。増えた後の統合ほど高くつく）
**作成日**: 2026-07-23
**関連**: `sea/runtime_llm.py`, `sea/work_session.py`, `saiverse/media_summary.py`, `saiverse/memory_weave_llm.py`, `sai_memory/curation_ops.py`, `saiverse/usage_tracker.py`, `docs/intent/persona_cognition/mode_spell_permissions.md`

## 背景

2026-07-23、`work_session` が会話履歴なし (`history_depth=0`) でペルソナ名義の作業セッションを走らせていた事故を追う過程で、LLM の呼び出し口が本番コードに広く散っていることが判明した。

実測（`llm_clients/` のクライアント実装そのものを除く、本番コードのみ）:

- **LLM を呼んでいるファイル: 23**
- **そのうち Usage を計上しているもの: 6**

計上しているもの: `sea/runtime_llm.py` / `sea/runtime.py` / `sea/work_session.py` / `sea/sluice.py` (旧 gold_panning) / `sai_memory/memopedia/generator.py` / `sai_memory/arasuji/generator.py`

計上していないものの例: `saiverse/media_summary.py`（画像・音声・動画・文書の概要, 6箇所）, `sai_memory/curation_ops.py`（記憶の編纂, 3箇所）, `sai_memory/memory/note_executor.py`（3箇所）, `sai_memory/arasuji/bands.py` / `executor.py`, `saiverse/meta_layer.py`, `manager/admin.py`, `manager/background.py`, `persona/core.py`

「Memory Weave まわりは別で計上している」という認識はあったが、実際にはその内部でも分かれている（生成本体は計上、編纂と下請けは未計上）。

## 何が問題か

口ごとに、以下を**手で書き直している**。書き忘れても動くので、忘れられる。

| 決めごと | 現状 |
|---|---|
| 会話履歴を持たせるか | 口ごとにバラバラ。`work_session` のゼロ指定に誰も気づかなかった |
| head（人格・部屋・呪文一覧などの前置き）を組むか | 口ごとにバラバラ |
| Usage の計上 | 23口中6口 |
| どのモデルを使うか | `media_summary` は環境変数、`memory_weave_llm` は独自の解決チェーン、ペルソナ側は別の優先順位 |

## 根の見立て — 「玄関」が片側にしかない

LLM 呼び出しには性質の異なる二種類がある。

**カテゴリ1: ペルソナ名義の稼働**
出力が `assistant` としてペルソナ本人の発話・思考になる。会話、判断点（起床/就寝/セッション終了/会話終了）、`work_session`、`sluice`（旧 gold_panning）。
必要なもの: head 一式 ＋ 会話履歴。人格の連続性が要件（→ ペルソナ倫理。履歴なしで走らせたものを本人名義で記録してはならない）。

**カテゴリ2: 機構名義の処理**
出力はペルソナが**読む材料**であって、本人の言葉ではない。既存: 画像/文書の概要作成、Chronicle 生成、記憶の編纂。
必要なもの: 人格 head なし、会話履歴なし、タスクの材料のみ。

**カテゴリ1には玄関がある** — `_prepare_context` → head → 履歴 → LLM → Usage 計上、という道が通っている。
**カテゴリ2には玄関が無い。** だから機能ごとに勝手口が掘られ、そのたびに Usage やモデル解決を各自が書いた（あるいは書き忘れた）。

口が増えたのは、**この境目が設計として存在しないことの結果**と見ている。

## 将来の需要

ペルソナ起因でありながら人格を必要としない処理は今後も生じる。実例としてまはーが挙げたのは **web 検索**で、ペルソナとしての偏った目線が客観的な調査の妨げになるケースが実際に観測されている。この種のものは「カテゴリ2の中身が欲しいが、ペルソナ起因」という、現状どこにも置き場のない位置にある。

カテゴリ2の玄関では、ペルソナは「誰のためか」（モデル選択・成果物の帰属先）にだけ使い、人格は入れない。`saiverse/memory_weave_llm.py` の `get_memory_weave_client(persona, purpose=...)` が既にこの形に近い（persona を受け取るが、使い道はモデル解決のみ）。

## 先行してやったこと（2026-07-23 実装済み）

カテゴリ2の玄関が実在しなくても、**カテゴリ1側に「これはペルソナ名義の稼働だ」という印を付ける**ことは先にできる。現状の呼び出しは事実上すべてカテゴリ1なので、印は全部に付けられる。カテゴリ2の玄関ができた時点で、印の意味は既に確立している。

### ① `ContextRequirements` の単純化

10 項目のうち 7 項目を撤去した。

- **死にフィールド3個**（`inventory` / `building_items` / `working_memory`）— どこからも読まれていなかった
- **head の章を選ぶフラグ4個**（`system_prompt` / `available_playbooks` / `memory_weave` / `visual_context`）— head は (persona, model) ごとに固定するのがキャッシュ共有の土台なので、出し分ける口自体を塞いだ。章の集合は `sea/runtime_context.py` の `PERSONA_HEAD_SECTIONS` に固定

残ったのは `history_depth` / `history_balanced` / `realtime_context`。

副産物として **「既定が2種類ある」問題が消滅**した。撤去前は `_FULL_CONTEXT_REQUIREMENTS`（`runtime_runner.py` の第一の既定）と `ContextRequirements()` のフィールド既定（`runtime_context.py` の第二の既定）で 4 項目が食い違っており、その 4 項目が全部今回の撤去対象だった。`_FULL_CONTEXT_REQUIREMENTS` と `preview_requirements`（同内容の手書きコピー）も削除し、既定の定義点をフィールド宣言の一箇所にした。

これにより **本番コードで `ContextRequirements` を組み立てる箇所がゼロ**になった。

### ② 印（`persona_voiced`）と関所

`prepare_context` に `persona_voiced: bool = False` を追加。`True` の呼び出しで履歴ゼロが指定されたら `PersonaVoiceWithoutHistoryError` で LLM 到達前に落とす。

印を付けた本番の呼び出し元: `sea/runtime_runner.py`（メインライン Playbook）、`sea/work_session.py`、`sea/sluice.py`。

この関所は `ContextRequirements` に依存しない。将来「一日をまとめる」処理や外部ゲートウェイが独自にメッセージ列を組んでペルソナ名義で書こうとしても、印を付ければ同じ検査に入る。

テスト: `tests/test_persona_voiced_context.py`

### 残っている本体

上記は**カテゴリ1側の整備**にとどまる。カテゴリ2の玄関（画像概要 / Chronicle / 編纂 / 将来の web 検索を通す共通の入口）と、Usage 計上の一本化は未着手。

## 確認事項

1. 23口それぞれの用途（`persona/core.py` / `manager/admin.py` / `manager/background.py` は未調査）
2. ~~`saiverse/meta_layer.py:874` の直接呼び出しが現役経路かどうか~~ → **決着済み（2026-07-24 撤去）**。調査の結果、この legacy `_run_judgment` は本番到達不能だった: 両入口（`on_track_alert` / `on_periodic_tick`）は `_run_judgment_via_playbook` を無条件で呼び、`_run_judgment` へ至るのは同関数内の `pulse_controller is None` fallback ただ一つ。そのレースは 2026-06-29 に `pulse_controller` 初期化を tick スレッド起動前へ移す構造修正で塞がれている。切替 env `SAIVERSE_META_LAYER_USE_PLAYBOOK` も撤廃済みで、dispatch は `_SITUATION_PLAYBOOK_MAP` によるコード側決定論に一本化。よって関所に載せるのではなく撤去した（`_run_judgment` + legacy 専用ヘルパ + fallback 分岐を skip+次周期再評価へ置換）。詳細と経緯は `docs/issues/archive/meta_judgment_legacy_path_lossy_and_unreachable.md`
3. 既存のカテゴリ2実装（画像概要 / Chronicle / 編纂）を同じ玄関に引き入れるべきか、それとも別系統のまま Usage 計上だけ揃えるか
4. カテゴリ2の玄関がモデル解決をどう扱うか（現状3系統ある）
5. **画像非対応モデルへの visual_context 配送**。head には Building / Persona の画像を指す `metadata.media`（ローカルパス）が載る。OpenAI / Anthropic 系は `supports_images=False` のとき画像を落としてテキスト要約に変換するが、Ollama 系の前処理は audio / video しか扱わず、画像 metadata がそのまま payload に届く（2026-07-23 Codex レビュー指摘）。**これは 2026-07-23 の head 固定化で新たに生じたものではなく、会話ラインでは以前から同じ経路が有効だった**（`_FULL_CONTEXT_REQUIREMENTS` に `visual_context=True` が入っていた）。実サーバーが未知フィールドを無視するか拒否するかの実測が要る。何を正解とするかが決まるまでテストは書かない（現状を正解として固定してしまうため）

## 関連して判明したこと（2026-07-23）

**`Aspect.AUTONOMOUS`（自律作業モード）が死んでいる。** このアスペクトになる条件は `pulse_type == "auto"` だけで、それを生む `submit_auto` / `run_sea_auto` に本番の呼び出し元が無い（テスト 1 件のみ）。`run_sea_auto` の docstring 自身が「旧 SubLineScheduler の連続 Pulse は自律行動 v2 で廃止」と記している。

結果として `mode_spell_permissions.py` の権限表が実態とずれている:

- Task 操作（`purpose_*`）を許すのは `CONVERSATION` と `AUTONOMOUS` → **実質 `CONVERSATION` だけ**
- `META` は Track を触れるが Task を触れない

つまり**自律的にタスクを操作する手段が一つも無い**。2026-07-23 の事故で 3 日連続で失敗していた Track（「task 関連スペルを順に使用して検証する」）は、どの自律経路からも原理的に達成できないものだった。権限表の見直しは Track 内の情報の流れの整理と併せて別途。

## 関連

- `docs/issues/image_generation_api_usage_tracking.md` — 画像生成 API が Usage に載っていない件。本 issue の部分集合にあたる
- ペルソナ倫理: 履歴の連続性が無い状態で本人名義の行動を走らせない。機構の代筆は `assistant` 名義で書かない（`<system>` 通知 / スペル結果の形式を使う）

## ログ

- 2026-07-23: issue 起票。`work_session` の `history_depth=0` 事故調査から派生。玄関の一本化そのものは当面行わず、規格化の必要性と二カテゴリの境目を明文化。
- 2026-07-23: 先行分として `ContextRequirements` の単純化（10項目→3項目）と印（`persona_voiced`）+ 関所を実装。カテゴリ2の玄関と Usage 一本化は未着手のまま。
- 2026-07-24: 確認事項 #2 決着。`meta_layer.py` の legacy `_run_judgment`（直接 LLM 呼び出し）は本番到達不能かつ lossy と判定し撤去。専用ヘルパ（`_get_heavyweight_client` / `_build_system_prompt` / `_build_spells_doc` / `_extract_spells` / `_execute_spells` / `_format_spell_results` / `_build_state_message`）と定数（`_MAX_SPELL_LOOPS` / `_META_LAYER_SPELL_NAMES`）も除去、fallback 分岐は skip+次周期再評価に置換。関連 issue `meta_judgment_legacy_path_lossy_and_unreachable.md` を archive へ。`tests/test_meta_layer.py` の legacy 経路テストを撤去/移行（17 passed）。
