# 自律 Pulse の器統合 + tell スペル — 実装ハンドオフ (レビュー消し込み用)

**日付**: 2026-08-08 夜 (同日 2 本目のハンドオフ。1 本目 = [Beat 頭知覚](2026-08-08_beat_head_perception_handoff.md) は消し込み済み)
**担当区分**: 実装 + レビュー 1 巡 = Fable セッション (本書)。指摘の消し込み = 次の Opus セッション。
**設計の正典**: [autonomous_pulse_vehicle.md](../intent/autonomous_pulse_vehicle.md) / 経緯 = [issue](../issues/slot_light_pulse_runs_on_conversation_vehicle.md)

## §1 何を作ったか (working tree、フルスイート 4122 緑確認済み)

**A. 暮らしコマのセッション統合** — `sea/work_session.py` に `profile="life"` を新設 (許可形の指示包み `_build_life_instruction_message` / kind='work_session' の出来事を開かない。予算・締めは既存パラメータ)。`saiverse/day_plan.py` の出かける/自室ハンドラは新設 `_run_slot_life_session` (予算 1・close_hook なし) を呼び、**会話 Playbook を借りていた `_dispatch_slot_pulse` は撤去**。ライフ予算の 1 コマ 1 消費 (`consume_life_pulse`) と presence_only 縮退の正直記録は維持。

**B. tell スペル** — `builtin_data/tools/tell.py` 新設 (spell=True、表示名「声をかける」)。`tell target=<user|all|同室ペルソナ名> gist=<任意>`。宛先検証 → 会話中のユーザー宛は no-op + 教育文 → CONVERSATION aspect の 1 Beat (aspect 導出で標準モデル) が言葉を生成 → `_emit_say` (Building 履歴 + UI + TTS + Unity) + `_store_memory` (本人の記憶、metadata に tell_target/tell_gist)。会話エピソード・Track・タイムアウトには触らない。usage 計上は `work_session._record_llm_usage` に playbook_name 引数を足して共用。

**D. 席違いフォールバックの WARN** — `sea/runtime.py` run_meta_user: 非 user の pulse_type が meta_playbook 未指定で既定の会話 Playbook に落ちるとき WARNING (禁止はしない)。`_choose_playbook` の嘘 docstring (「ここに来ることはない」) を実態に合わせて改訂。

**C (剥がし第一段) は A の副次効果** — 暮らしコマが WORKER aspect になったことで、既存のモードゲート (`mode_spell_permissions`) が track_activate 等の Track 操作を自動遮断。追加コードなし。

テスト: `test_tell_spell.py` 新設 6 本 / work_session に life プロファイル 2 本 / runtime_regression に WARN 2 本 / 旧経路前提の 5 スイート追従 (day_plan の冪等性・シナリオの不在日は「暮らしコマも 1 Beat 走る」が新しい正)。ツールカタログ再生成済み。timetable_redesign §未確定 10 に改訂注記。

## §2 レビュー 1 巡の指摘と裁定

**ローカルレビュー (llama.cpp)**: 指摘ゼロ (依頼した疑い所 4 点 — episode_ref None 整合 / tell finally 経路 / エラー分岐計上 / runtime None — すべて現物確認で問題なしと回答)。

**Codex adversarial-review (working-tree)**: needs-attention、指摘 6 件。メティスの裏取りと裁定:

| # | 指摘 (要約) | 裁定 | 消し込み方針 |
|---|---|---|---|
| 1 | **[critical] tell の hold_beat がスレッド間デッドロック** — セッションが BeatGate を保持したまま同期スペルを executor スレッドで実行するため、tell の hold_beat (RLock は同一スレッドのみ再入可) が別スレッドから永久ブロックする。tell テストは beat_gate 無し環境で検出不能 | **妥当・修正必須**。スペルは定義上つねに親 Beat の内側で実行される (関所は親が保持済み) — tell 自身が関所を取るのは設計的にも冗長 | **tell から hold_beat を撤去**する (子 Beat = 親 Beat の一部、beat_execution_context §2.2 と整合)。executor 越しの既知縮退族 (§6-6 トークン化で恒久解) に追記。可能なら実 BeatGate + executor のタイムアウト付きテストを 1 本 |
| 2 | [high] _emit_say / _store_memory の戻り値を無視して成功を返す — 履歴保存失敗でも「声をかけました」になる | **妥当 (範囲を限定)**。戻り値検査は正当。outbox/冪等キーの導入は過剰装備 (既存の発話経路全体と同水準を保つ) | 両方の戻り値を検査し、失敗時は正直な失敗文言を返す。outbox 化はしない |
| 3 | [high] 暮らしセッションが PulseController 管理外になり割り込み優先を失う | **受容 (既存設計と整合)**。作業コマも同じ直接呼び出しで、会話の優先は PulseController でなく **Beat 境界** (spell loop の boundary が周の合間に関所を手放す) が担うのが W 系以降の契約。暮らしは予算 1 = 1 Beat なので待ちは最小 | 対応なし。cancellation token の伝搬は作業コマ共通の将来課題として扱う |
| 4 | [high] ライフ予算の単位が分裂 — consume_life_pulse の既存契約 (標準呼び出しを数える) と新実装 (コマごと 1 回) が不一致、life.md 未更新 | **妥当 (文書の追従漏れ、intent で宣言済みだった)**。tell の標準呼び出しを予算に数えるかは未裁定の設計論点 | life.md §5.3 と consume_life_pulse の docstring を「自発活動の回数」意味論へ更新。**tell を予算に数えるかはまはー裁定** (メティス見解: 数えない — tell は会話の声でありコマの活動ではない) |
| 5 | [medium] WORKER の汎用スペル権限で暮らしコマから永続文書を変更でき、artifacts が帰属されない | **設計論点として記録**。「暮らしでも世界に触れてよい」は意図的許容の可能性が高いが、成果物が無帰属になる点は正当 | 軽処置 = life セッションの result.artifacts が非空なら WARN ログ。スペル遮断まで踏み込むかは**まはー裁定** |
| 6 | [medium] 会話状態の読み取り失敗を「会話中でない」扱いにして発声 (fail-open) | **妥当・軽微** | tell 側の分岐を fail-closed に (確認できないときは発声を見送り、再試行可能な結果文を返す) |

**⚠️ 実機注意**: #1 が未修正のまま自律行動を動かすと、暮らしコマ内で tell が唱えられた瞬間にそのペルソナの Beat 系が固まる可能性がある。**実機検証の再開は消し込み後**。

## §3 Opus セッションへの依頼

1. §2 の裁定表どおりに消し込む: **#1 (hold_beat 撤去) が最優先**、#2 (戻り値検査)、#4 (life.md/docstring 追従)、#6 (fail-closed)。#3 は対応なし、#5 は WARN ログの軽処置のみ
2. まはー裁定待ちの 2 点 (#4 の tell 予算計上 / #5 のスペル遮断範囲) は消し込みをブロックしない — 現状維持で进め、issue の残論点に記録
3. フルスイート 1 回 → コミット
4. 台帳 (in_flight) の本件行を「検証待ち」へ、issue のステータスも追従
5. まはーへ実機検証の再開合図 — 統合検証手順 Step 3 の残り (作業コマ・コマ締め) と、本件の実機確認 (出かけるコマの独白が Building 履歴に発話として**出ない**こと / tell で声が UI+TTS に届くこと) を同じ走行日に相乗りできる

## §4 実機検証への影響

- 出かける/自室コマの見え方が変わる: チャット UI に発話は出なくなり、思考は SAIMemory の内部記録 (Pulse タイムライン) 側に残る
- ペルソナがユーザーへ声を届けたいときは tell を唱える — 検証では「暮らしの一手の中で tell が自然に出るか」も観察対象
- 前夜の Beat 頭知覚 + 入室配送はこの器の上でそのまま生きている (セッション頭の知覚消費が入口)
