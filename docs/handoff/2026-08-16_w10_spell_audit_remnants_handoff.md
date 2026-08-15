# W10 (柱8) Spell 監査残の消し込み — 走行メモ (2026-08-16)

**これは何**: 完了計画書 W10 の実装セッション (Fable) の走行メモ。Fable セッションはレビュー 1 巡 → 指摘と裁定をここに記入して終了し、消し込みループは Opus セッションが本メモを入口に回す ([codex_review_gate 規則](../../docs/overview/code_review_ledger.md) 参照、規則本体は memory)。

**実装コミット**: `b6d15998` (feat(audit): W10 柱8 — Spell 監査残 3 件の消し込み)。ベース = `d73f1e01` (棚卸しコミット)。レビューは `--scope branch --base d73f1e01` で本コミットのみを対象にした (まはーの manager/ 系未コミット変更が並走していたため working-tree スコープは不可)。

## 実装の要約

1. **realtime spell の SPELL_ENABLED gate** (`sea/runtime_llm.py` lg_llm_node 冒頭): `_execute_realtime_spells` を `state["_spell_enabled"]` の内側へ。false のとき `_realtime_spells_executed` フラグは立たない (毎ノード再評価、判定は安価)。
2. **auto_mode の正直化**: `auto_mode=True` を渡す箇所がリポジトリ全体に存在しない実バグを発見。修正 = `run_meta_user` (`sea/runtime.py`) で `pulse_type not in (None, "user")` から導出 → `compile_with_langgraph` (`sea/runtime_graph.py`) が `state["_auto_mode"]` として単調 OR (`bool(auto_mode) or parent の値`) で子へ継承 → 消費点 (スペル実行 3 箇所 + LLM ノード wrap、`sea/runtime_llm.py`) は state を読む。`run_playbook` / `call_playbook` spell (builtin_data/tools/) は contextvar `get_auto_mode()` を継承。`spell_args_decider` 起動は `outer_state` から。
3. **`_` 予約 namespace 保護** (`sea/playbook_models.py`): `PlaybookSchema` の `_check_reserved_state_namespace` validator が書き込みベクトル全種 (input param 名 / output_schema キー / node id [output_key 既定値] / output_key / output_keys / output_mapping / SET assignments) を fail-closed 拒否。全ロードが `PlaybookSchema(**data)` を通る (`sea/runtime.py _load_playbook_from_db`) ことを確認済み = 関所として成立。値が効く merge 3 点 (inherited_vars / output_schema 親書き戻し / SET ノード) に skip+WARN の実行時防御。
4. **入力契約の実行時検証** (`sea/runtime_graph.py _validate_input_param`): 提供値を param_type (number/boolean/enum) へ正規化、変換不能・enum 外は LLMError で正直に失敗。string / object / 未知型と enum_source (動的) は素通し。

回帰 = `tests/test_spell_auto_mode_w10.py` 10 件 + `tests/test_playbook_contract_w10.py` 20 件。フルスイート 4355 passed (レビュー前 1 回、2026-08-16)。

## セッション内の裁定 (まはー未確認のものは「提案」)

| # | 裁定 | 根拠 | 状態 |
|---|---|---|---|
| 1 | required 欠落は warn-only (空文字 fallback + WARNING) | 既存 94 パラメータ中 52 が「required かつ default なし・呼び出しは値を渡さない」に依存 (builtin+開発DB 全数スキャン)。強制は playbook データの required 棚卸しが前提 | 私の裁定・まはー追認待ち |
| 2 | `check_spell_permission(aspect=None)` の fail-open は据え置き | ゲート対象 (TRACK_CONTROL_SPELLS 等) が Track 撤廃で解体予定。aspect 伝播の保証は v3 ライン再設計の仕事 — 解体予定のゲートを磨かない | 私の裁定・まはー追認待ち |
| 3 | pulse_type=None (PulseController を経ない直接呼び出し) は user 扱い | 確認ダイアログを黙って自動承認しない側に倒す (同意ゲートは fail-closed) | 私の裁定・まはー追認待ち |

## 挙動が変わる点 (実機検証の観点)

- **SPELL_ENABLED=false のペルソナ**: binding 済み realtime spell が走らなくなる (従来は毎 Pulse 走っていた)
- **自律 Pulse (schedule/auto) のスペル**: 確認ダイアログが出ず `reason="auto"` で自動承認される (従来は不在ユーザーへダイアログ → timeout か no_channel 素通し)。`list_available_playbooks` が auto フィルタを効かせるようになる — **自律ペルソナから見える Playbook が減る方向の変化**
- **通常会話 (user Pulse)**: 無変化のはず (auto_mode=False は従来どおり)
- **判断点・コマ**: schedule/auto 扱いになる — 判断 Playbook 内でスペルが確認を求める構成があれば挙動が変わる (現行の判断 Playbook は structured output + judgment_finalize でスペル確認は無いはず — 未確認なら実機で)

## Codex レビュー 1 巡の結果と裁定 (2026-08-16、17m12s 完走、判定 No-ship)

全 5 件。head (`b6d15998`) で裏取り済み。**F1 は確認済みの実回帰** — Opus セッションはここから着手すること。

| # | 重大度 | 指摘 | 裏取り結果と裁定 |
|---|---|---|---|
| F1 | **high** | schedule Pulse で `ask_every_time` Playbook の事前承認が到達不能 (`sea/runtime_engine.py:293-305`)。auto_mode=True の即時拒否が「schedule は設定行為=事前承認」の fall-through より**先に**評価され、設定済み自動化が静かに skip される | **確認済み・受諾**。順序の回帰 — 私の変更前は schedule が auto_mode=False だったから偶然届いていた。修正 = schedule 事前承認の判定を auto 拒否より先へ (歯止めは目的から: 「ユーザー不在なら確認できない」の先に「schedule は設定時に承認済み」がある)。user/auto/schedule × ask_every_time の回帰テスト必須。**修正されるまで、schedule から ask_every_time Playbook を起動する自動化は動かない** |
| F2 | medium | `_auto_mode` の実効値 (state) とノード factory が capture する raw `auto_mode` 引数の分裂 (`sea/runtime_graph.py`) | **構造は事実・受諾**。現行の全経路では両者は一致する (root で導出した値が factory へも流れる) が、真実の置き場が二つあり、片方だけ読む消費者が将来ズレを踏む。修正 = factory も実効値 (state 側) を読む一本化 |
| F3 | medium | `_` namespace の実行時防御が全 write sink を覆っていない (output_mapping / tool output_key(s) / tool_call output_key / LLM output_key) | **事実・部分受諾**。本番の全ロードが PlaybookSchema validator を通ることは確認済みで、今日の経路に穴は無い — が、不変条件が「loader の全経路が validator を通ること」に依存しているのは指摘どおり。修正 = state への playbook 変数書き込みを単一 helper に集約し `_` を拒否 (境界を値が効く場所へ、の完成形) |
| F4 | medium | 明示された typed default が入力検証を迂回 (`number` の default="12" が文字列のまま通る) | **確認済み・受諾**。私の検証は args 提供値だけを通し、default 適用側の分岐を素通しにした — 自分が直した欠陥の同族を自分の差分内で見逃した形。修正 = default も同じ validator を通す (ロード時検証が理想) |
| F5 | medium | `enum_values=[]` (空の静的 enum) が「制約なし」として fail-open (`if enum_values and ...` の truthiness 判定) | **確認済み・受諾**。「歯止めの条件を種類 (truthy) で書いた」小型違反。修正 = `is not None` 判定 + 空の静的 enum はロード時拒否 |

**却下 0 件**。処方はいずれも Codex 案の方向で妥当 (F1 のみ順序の入れ替え先を「schedule 判定を前へ」と確定)。

## 残作業 (Opus セッション向け)

1. **F1 (high・確認済み回帰) から着手** — schedule 事前承認の順序修正 + user/auto/schedule × ask_every_time 回帰テスト。F2〜F5 は上表の修正方向で消し込み → 再レビューループ (収束 = 指摘ゼロの観測、予測禁止)
2. 収束後フルスイート 1 回 → コミット
3. レビュー台帳 Spell 行・完了計画書 W10 行の状態更新
4. (別枠・SAIVerse 外) ローカル LLM レビュー基盤 — ideas.md「ローカル LLM レビュー基盤の立て直し」参照。**注意: 「テンプレート非互換が根因」は反証済み** (同型リクエストが 00:39/00:50 の試行では正常通過)。Jinja 500 は 00:28 の試行 1 の一連のリトライでのみ観測。決定打の再現実験 = 試行 1 のプロンプト (`~/.claude/local-llm/logs/20260816_002826.log` に全文) の再送で、通れば一過性・500 なら内容依存。副産物の実バグ = ラッパーの stdin 読み取りが 2 秒待ちで、遅い `git diff` パイプを黙って落とす (試行 1 の「diff 無しレビュー」の原因)
