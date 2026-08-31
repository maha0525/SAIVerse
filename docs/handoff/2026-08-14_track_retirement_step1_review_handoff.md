# ハンドオフ: Track 撤廃 順序① — Codex レビュー 1 巡の指摘と裁定（消し込みは次セッション）

**日付**: 2026-08-14
**書き手**: メティス（Fable セッション。規則により Codex レビューは 1 巡のみ回し、消し込みループは Opus セッションへ引き継ぐ）
**対象**: 順序①の実装一式（コミット済み。`git log` の `feat(track_retirement): 順序① — v1 メタ判断の退役…` を参照）
**正典**: [track_retirement.md](../intent/track_retirement.md) §7.4（確定範囲と実装記録）
**レビュー**: Codex adversarial-review 1 巡（job `review-mssgngdg-bpbqzx`、Verdict: needs-attention / No-ship、high 4 + medium 2）。フルスイートはレビュー前に 1 回実施済み・全緑（4,270 件）。指摘ゼロ後のもう 1 回は消し込み収束後に回すこと。

## 実装の要約（前提知識）

- v1 メタ判断（meta_layer の状況分類 → meta_judgment_* dispatch）と alert 状態機械（set_alert + observer）を退役。
- 別行動中のユーザー発話の仲裁は `autonomy_wiring.handle_user_utterance_conflict`（on_event 判断点の流用）へ直結。機械判定は「開いている出来事 ≠ 会話」。
- `build_on_event_situation_text` の「いまの活動」を開いている出来事から導出。
- debug API: fire-meta-judgment = 廃止 no-op / wrap-up-conversation = `handle_wait_response_timeout` 即時発火。
- 詳細は intent §7.4 実装欄。

## 指摘 6 件と私の裏取り・裁定

各指摘は head の実コードで裏取りした。**事実は 6 件とも正しい**。処方の採否と規模感を付す。

### F1 [high] busy 判定が non-running 分岐でしか走らない

- **事実（確認済み）**: `on_user_utterance` は (i) 新規作成 → 即 activate 応答 (ii) 会話 Track が running → 直接応答、の 2 経路では busy 判定（開いている出来事）を見ない。案 Y では会話終了後も会話 Track が running のまま残るので、作業の出来事が開いていても「残留 running」経路で即応答になる。
- **私の裁定**: **旧経路と同挙動＝順序①での退行ではない**（旧 alert 仲裁も non-running 分岐でしか発動しなかった）。
- **まはー裁定（2026-08-14）: 却下＝仕様どおり**。「別行動中でも会話を優先」の方針が直前（8/12〜13 頃）に確定しており、残留 running 経由の即応答はその方針に合致する。修正不要。なお会話優先が全面方針なら、そもそも仲裁（1-B' 直結経路）自体の存在意義も将来問い直しうる — その整理は順序④以降の設計で。

### F2 [high] get_open_episode(kind=None) は最後に開いた 1 件しか返さない

- **事実（確認済み）**: `_query_latest_open` は SHORT_ID desc の first。会話と作業の出来事が同時に開いていると、後から開いた方しか見えず、順序で仲裁の有無が変わる。同じ latest-only 読みは `build_on_event_situation_text` の「いまの活動」にもある（今回私が書いた側）。
- **裁定**: **採用・修正は小**。「open かつ kind != conversation が存在するか」を直接引く関数を episodes に足し、handler の busy 判定と状況文の両方で共有する（判定と提示の集合一致 — list_pickable_tracks の docstring と同じ規律）。両順序＋孤児 open のテストを足す。

### F3 [high] submitted=False の「outcome 無し」を起動不能と誤読して activate

- **事実（確認済み・head 裏取り）**: `judgment_points.py:2141-2151` — `submit_meta_judgment` の実行時例外は `_classify_runtime_failure`（台帳側は failed/unknown）だが、**戻り値 dict に `outcome` キーが無い**。`handle_user_utterance_conflict` は INDETERMINATE 以外の submitted=False を「起動不能」と読んで activate するため、finalize が note_only 等を適用した**後**の例外では判断を上書きして応答する（不変条件 b 違反）。
- **裁定**: **採用**。直接フォールバック（activate）を「LLM 開始前かつ席放棄済み＝ OUTCOME_ABORTED」に限定する Codex 案が正しい。**同族が `handle_external_event`（lines 987 付近）に元からあり**、`test_external_event_runtime_error_marks_unknown_and_falls_back_once` がその挙動を期待値として固定している — 直すなら両方＋テストの期待値も同時に（テストが欠陥を守っている形）。①より前からある v2 側の穴なので、修正 PR では「順序①の後始末」でなく「on_event 系の共通欠陥」として扱うこと。

### F4 [high] 回復経路が utterance_conflict を外部イベント形で再応答する

- **事実（確認済み）**: `utterance_conflict` の context には発話文とフラグしか無く、回復 tick が indeterminate を再発火して engage_now になると `_dispatch_recovered_event_response` の envelope 無しフォールバック（`<system>[外部イベント通知]` + track_user_conversation Playbook）で応答する。Track は activate されず会話出来事も開かない — 応答は届くが帳簿が食い違う。実装時に「縮退として許容」と判断したのは私で、Codex の言う帳簿乖離はその判断が見落としていた面。
- **裁定**: **採用（設計要）**。台帳 payload に種別と対象（track_id / user_id）を凍結し、回復側で「ユーザー会話 activate」へ分岐する。activate/main_line がちょうど 1 回になる境界テスト込み。規模は中。

### F5 [medium] debug wrap-up が閉じた会話でも空の post_conversation 判断を撃てる

- **事実（確認済み）**: 案 Y の残留 running Track に対して押すと、`handle_conversation_end` の「出来事なしは撃つ側に倒す」既定（`test_conversation_end_defaults_to_fire_when_undetectable`）により、存在しない会話への判断が走り**ペルソナ名義の記憶を汚し得る**。social Track も受理して success を返すが実体は WARNING のみ。
- **裁定**: **採用・修正は小**。開いている会話の出来事の存在を同期検証して無ければ 400/409、social は success にしない。

### F6 [medium] フロントの退役面残留（DebugPanel「メタ判断を1回」/ SettingsModal の休眠設定）

- **事実（確認済み）**: `DebugPanel.tsx` に常に失敗するボタンと無効な force、wrap-up の説明文が旧仕様のまま。`SettingsModal.tsx` に読み手を失った max_retries / retry_backoff_seconds / force_fail の編集 UI。
- **裁定**: **採用**。裁定 C「UI はユーザーへの世界の契約」に照らすと、①の追従に含めるべきだった私の見落とし。ボタンと休眠設定欄の削除＋wrap-up 説明の更新。ダークモード規律に注意。

## 次セッション（Opus）への手順

1. F2 → F5 → F6 → F3 → F4 の順を推奨（小さい確実な修正から。F3/F4 は設計を伴う）。**F1 はまはー裁定で却下済み（仕様どおり）— 触らない。**
2. 修正 → 対象テストのみ → 再レビュー（観点付き adversarial-review）→ 収束（指摘ゼロの観測）後にフルスイート 1 回。
3. 収束後、intent §7.4 実装欄と in_flight 台帳の行を更新。

## 消し込みの結果（2026-08-14、Opus セッション）

5 件とも消し込み済み。コミットは 1 指摘 = 1 コミットで分けた。

| 件 | 直した形 | 記録先 |
|---|---|---|
| F2 | `episodes.get_open_non_conversation_episode`（会話を除いた open の直接クエリ）を新設し、仲裁の判定と「いまの活動」の提示を同じ集合へ寄せた | intent §7.4 |
| F5 | 切り上げ debug API に開いている会話の出来事の同期検証（無ければ 409）。social は success を返さない。デバッグ API のテストを新設 | intent §7.4 |
| F6 | DebugPanel のボタン / SettingsModal の休眠設定 3 欄を削除、切り上げの説明を実挙動へ。あわせて no-op debug エンドポイント 3 本も削除（呼び手が消えたため） | intent §7.4 |
| F3 | 結末語彙を `aborted` / `no_effect` / `ran` / `indeterminate` へ拡張し、判定を `direct_fallback_allowed` に集約（**結末なしは拒否側**）。同族 3 箇所（外部イベント / 仲裁 / schedule 精算）を同時に修正 | [issue](../issues/judgment_seat_contention_and_event_loss.md) ④ |
| F4 | 仲裁の context に応対先を凍結し、回収側は種別で入口を選ぶ（仲裁 = 会話 Track の activate）。二重応対の境界は「開いている会話の出来事」で判定 | 同 issue ⑤ |

F3/F4 は順序①より前から在る on_event 系の共通欠陥なので、経緯は track_retirement ではなく上記 issue に置いた（ハンドオフ §F3 の指示どおり）。テストが欠陥を守っていた 1 件（`test_external_event_runtime_error_marks_unknown_and_falls_back_once`）は期待値ごと入れ替え、直結経路（`handle_user_utterance_conflict`）には直接テストが 1 件も無かったので新設した。

### 二巡目・三巡目とレビューの打ち切り（同日）

消し込み後の差分へ再レビューを 2 巡回した（いずれも `--scope branch --base 9d5e65be`、観点付き）。

- **二巡目**（high 8 + medium 2）: 一巡目の消し込み自体が作った穴 3 件（回収の応答済み判定が `suppress_pulse` の状態を誤認して発話を捨てる / 凍結 payload の Track を無検証で activate / 切り上げの TOCTOU）と、私が書いた契約を裏切っていた既存経路 3 件（台帳 claim の例外が裸で上がる / status 読み取り失敗を成功扱い / 回収 refire が副作用ゼロ確定の失敗を代替応対へ落とさない）を消した。2 件は不採用（1 件は事実誤り、1 件は事実は正しいが台帳の不正遷移が歯止めになるため結論が成立しない — 論証で終わらせず回帰テストで固定した）。
- **三巡目**（high 6）: 2 件を消し（条件付き close への畳み込み / 「閉じるべき会話が無くても撃つ」既定の撤去）、**残り 4 件は角度が違うだけで同じ欠落**（刺激の永続 ID）だったので [issue](../issues/on_event_judgment_has_no_idempotency_key.md) へ集約した。

**まはー裁定（2026-08-14）: ここでレビュー完了。** 根拠は「指摘が今回の実装範囲を外れた根っこまで到達している」こと。順序①は**近似の歯止めのまま実機検証へ進める**（受け入れたリスクと、その根拠は issue の「近似のまま進めた裁定」節）。receipt の設計・実装は別セッション。

## 参考

- ローカル LLM レビューは今回機能しなかった。事後調査で確定した経緯: サーバー起動は成功していたが、サブエージェントがラッパーに 5 分タイムアウトを指定 → 多ターン巡回のレビューが途中で切られ、**モデルが書き上げた模様のレビュー本文（約 1,900 トークン）は受け手不在で失われた**。代行に立った Claude エージェントの手動検証は「指摘ゼロ」＝上の 6 件を一件も拾えていない（検出力の実測 0/6）。詳細と処方は memory `project_local_llm_review` の 2026-08-14 節。
- Codex session ID: `019ffe94-0611-7e12-b6dc-dc36288643ee`（`codex resume` で追質問可）。
