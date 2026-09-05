# 2026-07-30 のレビュー由来の守りの棚卸し

**ステータス**: 監査完了・修正方針の確認待ち — 現存する不具合 3 系統を再現。製品コードの修正は未実施
**発端**: 2026-09-05、読み戻しの分岐レビューでまはーが指摘

**監査日 / 現行基準**: 2026-09-06 / `c21870ea303b17ee19b96c67aca7a12d39a9aabd` (`hotfix/v0.3.9`)。監査開始時の `344e979b` から更新された 0.3.9 の実装を反映して再照合した。

## 結論

当該セッションの 4 コミット・32 パスを対象に、当時の変更部分と現行の対応経路を監査した。最初に疑われた読み戻しの三つの制限は、現在は撤去済み。一方、**印戻しの下限割れ、head 読取失敗の成功扱い、モデル変更の古い応答による表示巻き戻り**が現行にも残り、合成データで再現した。

「当時のモデルが変な状態だった」という内部状態はログから判定できない。ただし、**実装側が誤った前提を設計文書へ書く → レビュワーがその前提への違反をバグと判定する → テストが誤った前提を固定する**、という連鎖は確認できた。外部レビューだけを原因にする説明では不十分だった。

## セッションの確定と一次資料

原本は Claude の `abecc4a5-74e1-4afe-881d-406e32d0d54e.jsonl`、customTitle は **メタボリズム補充ロジック**。ユーザー / assistant レコードの期間は 2026-07-30 16:26–23:04 JST。

ローカル原本: `C:/Users/shuhe/.claude/projects/C--Users-shuhe-workspace-SAIVerse/abecc4a5-74e1-4afe-881d-406e32d0d54e.jsonl`。以下の L 番号は、この JSONL の物理行番号。

| コミット | セッション内の記録 | 内容 |
|---|---|---|
| `7dae379a` | L654 commit 呼出し | §15 読み戻し、印戻し、保存・提示経路 |
| `7c72848b` | L1086 commit 呼出し | context preview、head 再 capture、読取失敗の扱い |
| `8a8f1721` | L1096 commit 呼出し | 進行台帳更新 |
| `72224d84` | L2134 commit 呼出し | データ送信量 UI、context-status API、モデル切替、旧全体設定の撤去 |

事前の候補 `54a8ae47` / `1ee99d4e` / `a0c1d67a` はこのセッションの commit 呼出しには含まれない。同日の全作業や 7/31 の判断プロンプト変更まで監査した、とは扱わない。原本の Edit / Write は 31 パス、API 参照の生成を含むコミット差分の和集合は 32 パス。レビューは実装の三束について 7 / 5 / 8 巡の記録がある。

## 現存する不具合

### F1 — P2: 印戻しが「残す量」を割り、読み戻しと逆向きに動く

- 起源: `7dae379a` の印戻し。現在位置: `sea/session_lifecycle.py:3956` (`_refold_raw_view_plan`)、特に 3996–4002。
- 条件: 生表示に戻した大きな fold があり、提示量が target を超えている。
- 現行処理は **戻す前**の量だけを判定し、fold 全体を digest にしてから量を測る。戻した後に target を割るか、直近の保護範囲を含むかを判定しない。通常の退場計画より先に行い、下限を割った結果でも `_run_metabolism_locked` の 4361 行付近から成功終了する。
- 再現: 1,000 字 × 12 行、先頭 10 行が一つの生表示 fold、target=5,000 / high=10,000。実際の印戻しと提示関数は **12,000 → 2,164 字**にする。その結果を実際の `_plan_window_refill` に渡すと、同じ fold を開き **12,000 字**へ戻す。
- 影響: 「残す量」の保護に反する窓を保存しうる。逆方向の計画が成立するため、Pulse の経路によっては再開閉・head 再構築を繰り返す可能性がある。連続した本番 Pulse は実行していないので、その発生頻度は未確認。原文の削除ではない。
- 既存テストも「target 以下になるまで古い fold を戻す」を正解としており、下限を跨ぐ一枚の扱いを守っていなかった。
- 修正方針: 印戻しにも直近の保護範囲を適用し、下限を割る fold はそのまま残す。超過の処理は通常の退場計画と整合させる。開く / 戻すを続けたときの不変条件をテストする。

### F2 — P2: head の取得失敗を新しい空 snapshot で覆い隠し、再取得成功と判定する

- 起源: `7c72848b` の検証側が、既存の取得側の失敗契約を取り違えた。現行位置: `sea/session_lifecycle.py:2826` (`_recapture_head_after_refill`)、`sea/head_pipeline/sections/memory_weave.py:113`–125。
- 読み戻し後の検証は snapshot の identity が変われば成功とする。根拠のコメントは「section の例外なら pipeline が旧 snapshot を保持する」。しかし実際の `MemoryWeaveSection.capture` は helper の失敗を自分で捕まえて、**新しい空 snapshot**を返す。通常呼出しでは helper の `raise_on_error` も指定していない。
- 再現: 実際の section と pipeline を用い、有効な Chronicle 1 件の capture 後に helper へ合成読取例外を注入。結果は **1 件 → 0 件、capture_failures={}、identity は変更**。実際の読み戻し後検証は成功を返し、再試行しない。
- 影響: 既存の head Chronicle が空になるのに、成功扱いで処理を進める。これは提示の欠落であり、保存された Chronicle 本体の削除を確認したものではない。
- 後日の追加劣化: `_head_weave_snapshot` が返す `_WEAVE_INSPECT_FAILED` は 9/1 に追加されたが、この呼出し側は未対応。旧 object → 取得失敗 sentinel も成功とすることを別の合成例で再現した。sentinel 自体を 7/30 導入と混同しない。
- 修正方針: 取得失敗と正当な空結果を producer から区別し、pipeline の旧 snapshot 保持契約を通す。検証側も取得失敗を成功にしない。mock の「旧 object が返る」想定だけでなく実 section のエラー経路を検証する。

### F3 — P2: モデル変更応答の JSON 待ち中に次の選択が入ると、表示が古いモデルに戻る

- 起源: `72224d84`。現行位置: `frontend/src/components/ChatOptions.tsx:416`–422。
- seq 照合は `fetch` の後にはあるが、`await res.json()` の後にない。この待ち時間に次の選択が入ると、旧モデルの成功応答が新しい表示を上書きする。
- 再現: A を選択 → A 応答の body 読取を保留 → 「自動」を選択 → A の body を返す → 直列化された「自動」の要求を成功させる。**サーバー状態は null (自動)、表示は model-a**になった。最新応答の `current_model: null` を truthy 条件が無視するので、その要求の完了後も表示が戻らない。
- 検証は製品の `applyModelChange` 本体をソースから抽出して TypeScript transpile し、制御した fetch と state setter で実行した。実ブラウザでの描画確認やサーバーへの要求送信はしていない。
- 修正方針: body 読取後も最新 seq であることを確認し、null を有効な「自動」の結果として反映する。成功 / エラー再同期の各 await 後の state 更新も同じ規則で点検する。サーバー側 seq のテストだけでは、この UI 競合を検出できない。

## 撤去済みの制限と、誤った前提が定着した経路

| 項目 | 原本・差分の証拠 | 現行の扱い |
|---|---|---|
| target を読み戻しの天井にする | L42、最初の提案から実装側が「残す量を超えない範囲」と記載。L214 の初版 planner に存在し、初回レビュー L325 より前 | `9e697a2a` / `dd34c830` の再設計で撤去。今は下限まで丸ごと開く |
| 読んだ範囲に材料がないあらすじで停止する | L214 初版に missing 判定。予算で読んだ範囲の外を材料欠損と同様に停止条件へ入れる | 現行はあらすじを先に選び、読める材料まで読む。歴史 planner の合成例で旧停止を再現 |
| 未被覆の編纂対象行を跨がない | L214 に既に「あらすじの無い領域 (編纂なしで忘れた過去)へは降りない」。第 6 巡 L579 が、段と段の未被覆行まで「意図的に忘却された内容」として high 指摘。L582–606 で処方とテストを補強 | 現行は旧 `plan_rewind` 自体を置き換え、未被覆行を永続的な禁止境界にしない |
| 話しかけられたときだけ読み戻す | 7/30 の会話応答前という起動範囲 | 後日の再設計で作業セッションを含む Pulse 側へ拡張。現行の残存欠陥とは数えない |

初回の user 要求 (L5) は、複雑化を避け、モデルを大きくしたペルソナが長い生ログを再び持てるようにすること。L48 の推奨案への同意を、保存済みの過去を永続的に読み戻せなくしてよい承認とは読めない。

さらに、`7dae379a^:docs/intent/arasuji_levels.md` の §13 (157 / 208 行付近) にも「編纂なしで忘れる」という表現は存在した。ただし対象は **編纂なしで anchor を進める選択**だった。当日、その窓からの退場を **保存された履歴の再読込禁止**へ拡張し、§15 の「設計合意」とした。この拡張の裏付けが欠けている。したがって「Codex がゼロから忘却概念を持ち込んだ」という当初の見立ては訂正する。

当時のレビュー全体を無価値とは判定しない。anchor の CAS、あらすじ全件取得の確認、失敗と空の区別、モデル要求の世代分離などには妥当な目的がある。問題は、その処方が世界の前提と実際の呼出し先の契約を満たすかの確認が抜けたこと。レビュー回数とテスト成功件数は独立した仕様検証の代わりにならなかった。

## 32 パスの監査範囲と判定

対象は下記ファイルの **当該 4 コミットの変更部分**、その前後と現行の対応する呼出し先。巨大ファイルの全時代の全機能を保証するものではない。「追加指摘なし」は、この範囲で新たな裏付けのある不具合を確定しなかったという意味。

| 当時のパス | 調べた責務 / 判定 |
|---|---|
| `sea/window_refill.py` | reopen / rewind の境界と順序。上記三制限は旧版で再現、現行で撤去済み |
| `sea/session_lifecycle.py` | refill 保存・CAS、preview、refold、head 再取得。F1 / F2 |
| `sea/session_window.py` | presented_raw と digest の提示分岐・区間処理。F1 再現に実関数を使用。追加指摘なし |
| `sea/runtime.py` | user response 前の読込・refill の起動位置。現行 Pulse 拡張まで照合 |
| `sea/runtime_context.py` | refill preview と weave 除外の組合せ、失敗時の整合。新規指摘なし |
| `persona/history_manager.py` | anchor 手前の履歴取得と strict 伝播。旧予算制限の呼出し元は置換済み |
| `sai_memory/memory/storage.py` | 過去行の取得・並び順と提示対象フィルタ。追加指摘なし |
| `saiverse_memory/adapter.py` | 同取得の adapter 公開経路。追加指摘なし |
| `saiverse/dynamic_state.py` | head capture dispatch と返り値。F2 の失敗契約を追跡 |
| `builtin_data/tools/get_memory_weave_context.py` | raise_on_error の追加と実利用。preview にはあるが F2 の section 呼出しにはない |
| `api/routes/config.py` | 旧全体設定撤去、モデル watermarks、client_id / seq / lock / TTL。サーバー側回帰成功、F3 は UI 側 |
| `api/routes/people/__init__.py` | 新 route 登録。追加指摘なし |
| `api/routes/people/context_status.py` | 実モデルの解決、測定と失敗表示、下見の読取専用性。回帰成功 |
| `manager/initialization.py` | 廃止設定の初期化除去。追加指摘なし |
| `frontend/src/components/ChatOptions.tsx` | 数字と操作の UI、要求直列化・復旧・世代照合。F3 |
| `frontend/src/components/ChatOptions.module.css` | 上記 UI の表示クラス。変更範囲で追加指摘なし。実ブラウザ視覚 QA は未実施 |
| `frontend/src/components/settings/ModelEditorModal.tsx` | 未設定 / null / 数値、水位検証と保存値。変更範囲で追加指摘なし |
| `tests/test_window_refill.py` | 当時は誤った ceiling / gap / broken と refold を正解として固定。現行回帰成功と欠陥の再現が両立 |
| `tests/test_context_status.py` | 測定失敗・表示・下見等。現行実行 |
| `tests/test_model_change_seq.py` | サーバー世代処理。現行実行。F3 の body 待機はカバーしない |
| `tests/test_model_watermark_validation.py` | モデル水位の型 / 順序 / null。現行実行 |
| `tests/test_persona_voiced_context.py` | 提示 fixture の追従。現行実行 |
| `tests/test_session_anchor_rows.py` | anchor fixture と取得経路の追従。現行実行 |
| `tests/test_gold_panning.py` | 当日の fixture 修正を確認。後日 `66f17ca0` で sluice へ置換され、このパスは現行に存在しない |
| `docs/intent/arasuji_levels.md` | §15 と追補。「設計合意」への前提拡張を特定 |
| `docs/concepts/metabolism.md` | 読み戻し・設定説明。現行再設計との対応を確認 |
| `docs/issues/metabolism_refill_when_below_target.md` | 当時の設計・完了記録。現在は archive 側 |
| `docs/issues/chat_options_metabolism_section_redesign.md` | 当時の UI 設計・レビュー記録。現在は archive 側 |
| `docs/overview/in_flight.md` | 3 実装束の進捗記録。実装動作の証明とは扱わない |
| `docs/overview/landscape.md` | 読み戻し・表示・全体設定廃止の説明。追加指摘なし |
| `docs/developer-guide/project-structure.md` | 新モジュール・API の索引。追加指摘なし |
| `docs/reference/api-endpoints.md` | 生成された route 参照。手編集せず差分と route を照合 |

### 後続の既知課題との切り分け

[読み戻し再設計の完了記録](archive/refill_reads_by_budget_instead_of_arasuji_unit.md) の残留項目 (digest 読取の fail-open、最悪計算量、接続の読取、anchor CAS と folds の同時変更、作業セッションの floor) は、既に記録・裁定された事項として扱い、今回の新規発見数へ加えない。

一方、[8/31 の未発火診断](window_refill_rarely_plans_for_starved_personas.md) は、旧実装を「正しく動く安全規則」として未解決扱いのまま、全履歴の編纂などを処方していた。現行には適用できないため、冒頭に撤回範囲と再設計への参照を追記した。当時の実データ観察を、今回新たに本番で測定した事実としては扱わない。

## 再現と検証

本番ペルソナへの入力、Pulse / Playbook / Spell 起動、有料 LLM 呼出し、記憶・世界状態の書込みは行っていない。Python 検証は隔離した `SAIVERSE_HOME` と合成データ、Node は通信しない fetch stub を用いた。

- 既存の現行回帰: `test_window_refill.py` / `test_context_status.py` / `test_model_change_seq.py` / `test_model_watermark_validation.py` / `test_persona_voiced_context.py` / `test_session_anchor_rows.py` — **186 passed、9 warnings**。全スイートは未実行。7 本を指定した最初の実行は、削除済み `test_gold_panning.py` により収集前に失敗し、存在する 6 本へ訂正した。
- [Python 再現](../../tests/audit_20260730_probes.py): 歴史 planner 三制限、F1 の実 planner 往復、F2 の実 section / pipeline と検証側、後日 sentinel の例。すべて意図した欠陥を再現。
- [Node 再現](../../tests/audit_20260730_model_change.mjs): 製品関数本体を実行し、`server_model=null` / `displayed_model="model-a"` の不一致を再現。
- 再現ファイルは **観察した欠陥があること**を assert する監査用で、通常の自動収集テストではない。修正時に期待動作へ反転して通常の回帰へ取り込む必要がある。
- 当時の記録にある 3360 / 3370 / 3397 件成功は当時の報告値であり、今回の実行結果ではない。
- 監査用 Python の `ruff check`、`git diff --check`、`scripts/check_in_flight.py` は成功。台帳検査の警告は既存 RSS 行の経過措置のみ。

再実行 (リポジトリ root、Python venv / frontend の既存依存が必要):

```powershell
.venv/Scripts/python.exe -X utf8 tests/audit_20260730_probes.py
& 'C:/Program Files/nodejs/node.exe' tests/audit_20260730_model_change.mjs
```

Python 再現は import 時のログ等も一時ディレクトリへ向ける。例外ログ `synthetic read failure` と上限超過 WARNING は合成ケースの期待出力。実ペルソナの障害ではない。

## 次の判断

推奨順は F1 の下限保護 → F2 の取得失敗契約 → F3 の表示整合。監査依頼の範囲で報告と再現例まで用意し、製品コードには変更を加えていない。本 issue は監査そのものが終わっても、残る修正の扱いが決まるまで未解決フォルダに置く。

---

## 経緯: 監査前の問題提起 (原文を保持)

以下は調査前の仮説。セッション範囲と起源の確定結果は上記を正とする。

## 何を疑っているか

読み戻しの再設計で撤去が決まった守りのうち、少なくとも二つはコードに「Codex 指摘 2026-07-30」と明記されている:

1. 「開くと残す量を超えるなら開かない」(残す量を天井として扱う誤り — 残す量は目標量であり下限)
2. 「編纂対象なのにどのあらすじにも覆われていない行を跨いで起点を戻さない」(いわゆる「忘れた過去を復活させない」守り)

特に 2 は「忘れた過去」という、**なにも忘れない SAIVerse の世界前提に存在しない概念**を持ち込んでいた。レビューの処方が一般的な常識 (画面から消えたものを勝手に戻さない) で書かれ、世界前提と照合されずに実装された形 — memory の「レビューの処方は一般的な常識で書かれている」(feedback_review_prescriptions_vs_world_premises) と同じ型。

もう一つ撤去が決まった「材料が提示できる履歴に無いあらすじは開けない」(broken 停止) は、起源の日付が未確認 — 棚卸しで特定する。

まはーの言葉: 「7/30 に何が起きたのか、実装後にでも遡って調べるべきかもしれない。どうもその日に他の地雷も埋めてそうな予感がするんだ」

**スコープ拡大 (2026-09-05 まはー)**: 当時のセッションログを読んだまはーの観察 — 「メティスが相当調子悪かった時に見える (俺がキレてるとこからの推測)。セッション内で触ってるコードを全部再検証するくらいの気持ちでやったほうがいい」。守りの列挙にとどめず、**そのセッションが触ったコード全部の再検証**として行う。

## 対象 (コミットから逆引き)

読み戻し実装セッションのコミットは `7dae379a` (7/30 18:43 §15 読み戻し本体) と `7c72848b` (7/30 20:01 §15 追補 context preview)。同日にはほかに `54a8ae47` (03:34 §14 消し込み 7 巡)・`1ee99d4e` (13:56 水位既定の引き上げ)・`72224d84` (23:04 データ送信量セクション再設計)・`a0c1d67a` (7/31 01:21 判断プロンプト head 移設) がある — どこまでが同一セッションかはログで確定させ、少なくとも読み戻しセッションの 2 コミットは全ファイルを再検証する。

2 コミットが触ったコード (docs とテストを除く):
`sea/window_refill.py` / `sea/session_lifecycle.py` / `sea/runtime.py` / `sea/runtime_context.py` / `sea/session_window.py` / `persona/history_manager.py` / `sai_memory/memory/storage.py` / `saiverse_memory/adapter.py` / `saiverse/dynamic_state.py` / `builtin_data/tools/get_memory_weave_context.py` (+ `tests/test_window_refill.py`)

## やること

- 当時のセッションログとコミットを突き合わせ、セッションの範囲を確定する
- 上記ファイルの当該差分を一行ずつ再検証する — レビュー由来の守り・概念に限らず、実装判断そのものを疑う
- 各項を SAIVerse の世界前提 (なにも忘れない / 住人は見られていなくても生きている / 残す量は下限) に照らし、食い違うものを洗い出す
- 食い違いが見つかったら本 doc に追記してまはーの裁定を仰ぐ
