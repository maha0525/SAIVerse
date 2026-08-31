# ハンドオフ: `docs/issues` 監査（2026-07-10）

> **監査完了・issue 本体の移動/書換えは未実施。**
> `docs/issues/README.md` の規則（現役=未解決、`archive/`=解決済み）と、
> 現コード・`docs/overview/in_flight.md`・`landscape.md` を照合した。
>
> 調査時点: active 75件（README除外）、archive 7件。

## 0. 結論

issue 台帳は内容自体には価値があるが、フォルダを source of truth とする運用からかなり
ドリフトしている。

- active 直下に、明示的に「完了 / 解決済み / 実装済み」と書かれた文書が複数残る。
- 削除済みの `SubLineScheduler` / `track_autonomous` を前提にした issue が現役のまま。
- active 5件、archive 1件に status 行がない。
- 古い一括 issue の一部だけ実装済みになり、残作業と本文の対応が崩れている。
- 同じ左サイドバー問題の重複 issue が2本ある。
- 少なくとも3本の実在 Markdown リンクが移動後のパスに追従していない。
- archive 2件は置き場所は正しいが、本文 status/log が未完了のまま。

最初の整理はコード変更なしでできる。**完了・重複・廃止機構由来の文書を closure note 付きで
archive へ移し、status とリンクを直す**だけで、現役一覧の信頼性がかなり戻る。

## 1. フォルダ整合: archive 推奨

### A. 完了が本文上も明確（そのまま archive 候補）

| issue | 根拠 | 推奨処理 |
|---|---|---|
| `chronicle_generation_dual_pipeline.md` | `状態: 解決済み (2026-05-28)`、解決内容まで記録 | status をテンプレート形式に揃えて archive |
| `gemini_auto_cache_mode.md` | `状態: 実装済み`。`llm_clients/gemini.py` に env 判定と generate/stream 両経路が現存 | archive。保持時間可変化は `ideas.md` の別アイデアとして既に分離 |
| `phase3_4d_dead_code_removal.md` | `✅ 完了`、削除内訳と全785テスト pass のログあり | archive |
| `realtime_info_current_time_toggle.md` | `✅ 実装完了`。DB/API/UI/runtime 実装が現存 | archive |
| `track_short_id_for_spell_args.md` | `状態: 実装済み`。`track:N` resolver/migration/tests が現存 | archive。本文の旧 `t:N`/SubLineScheduler 記述は履歴として残してよい |
| `stackchan_mcp_integrate_branch_commits.md` | `構築完了、実機 flash 済`。以後の upstream 追跡は別 issue に分離済み | archive（内容は実装ハンドオフ記録として保存） |
| `stackchan_multi_tof_power_coupling.md` | SAIVerseソフト無関係・ハード原因確定。ソフト変更不要と明記 | software issue として closure → archive。ハード対策メモは履歴として残す |

### B. 廃止機構により superseded（解決ではなく「前提消滅」で archive）

| issue | 現況 | 推奨処理 |
|---|---|---|
| `subline_scheduler_ignores_activity_state.md` | 対象 `saiverse/pulse_scheduler.py` は削除済み | 「自律行動v2で駆動源ごと廃止」と追記して archive |
| `autonomous_track_tone_collapse.md` | 検証対象 `track_autonomous` は死亡、JSON/DB掃除待ち | 実機検証待ちを取り下げ「v1退役でsuperseded」として archive |
| `autonomous_work_single_pulse_completion.md` | `track_autonomous` の1 Pulse詰め込み問題。v2は予算付き `WorkSession` へ構造置換 | v1固有 issue として archive。似た症状がv2で出たら新規起票 |

### C. 重複として archive

`left_sidebar_system_section_layout.md`（2026-05-09）と
`left_sidebar_system_section_crowding.md`（2026-07-08）は、調査事項と解決案がほぼ同じ。
後者の方が `Sidebar.tsx`、小画面で「場所」を圧迫する実害、レビュー要件まで具体的なので、
後者を正とし、前者に `superseded by ...crowding.md` を追記して archive がよい。

## 2. active 維持だが status を整理すべきもの

実装済みでも**検証や残作業が明記されているものは active のままで正しい**。ただし
先頭が `✅` だと folder invariant と衝突する。

| issue | 現在の表記 | 推奨 status |
|---|---|---|
| `git_required_for_zip_install.md` | 実装済み・実機テスト待ち | `🟣 検証待ち` 相当 |
| `native_tool_return_4tuple_bug.md` | `✅ 修正実装済` + 実機検証待ち | `🟣 検証待ち` |
| `phase4_meta_judgment_recovery.md` | `✅ 実装完了` + 実機検証待ち | `🟣 検証待ち` |
| `spell_html_leak_into_saimemory.md` | 修正済み・実機検証待ち | `🟣 検証待ち` |
| `spell_judgment_recorded_after_subline.md` | 修正済み・実機検証待ち | `🟣 検証待ち` |
| `stackchan_touch_false_stroke_events.md` | 初動検証済み・長期monitor中 | `🟣 検証待ち` |
| `stackchan_unit_capability_requires_restart.md` | バグは解決済み、末尾に別件ToF実機検証が残る | capability issue はarchiveし、ToF検証を別issueへ分離するのが最も明瞭 |
| `spell_loop_continuation_contract.md` | `✅ 設計決着 → 実装待ち` | 先頭を `🟡 実装待ち` にする（設計完了は本文へ） |

`docs/issues/README.md` の語彙は `🔲 / 🟡 / ⚠️ / ✅` だが、現役では `🩹 / 🚧 / 🟢 /
🔍 / 🔴` も使われる。`in_flight.md` と揃えるなら `🔵設計中 / 🟡実装待ち /
🟠実装中 / 🟣検証待ち / ⚠️保留 / ✅完了` へ README 側を更新し、status の詳細は
括弧内に書く方が機械監査しやすい。

## 3. status 行が無い文書

### active（5件）

- `emitter_saimemory_responsibility_cleanup.md` — `🔲 未着手 / low` 相当
- `head_prompt_followups.md` — in_flight では `🟡 実装待ち`。doc側にも同じ status が必要
- `stackchan_multi_vessel_phase7_followups.md` — 完了・検証待ち・未修正が1文書に混在。下記の分割推奨
- `stackchan_multi_vessel_verification_handoff.md` — issue ではなく handoff 文書。`docs/handoff/` へ移す候補
- `voice_tts_streaming_audio_bleed.md` — 根本未解明。`🔲 未着手` ではなく `🔵 調査中` 相当が自然

### archive（1件）

- `archive/arasuji_orphaned_source_ids.md` — source は修正済み。
  `delete_incomplete_entries()` が削除IDを親の `source_ids_json` から除去する実装を持つため、
  archive の置き場所は正しい。`✅ 解決済み` と修正日/検証を追記する。

## 4. 内容が現コードに追いついていない active issue

### `legacy_action_handler_cleanup.md`

本文の案A（PersonaCore 4メソッド削除）と案B（`run_auto_conversation` /
`run_scheduled_prompt` 等）は既に完了している。現コードで確認できた残りは:

- `saiverse/action_handler.py::ActionHandler` 本体
- `PersonaCore` の callback 4本と manager/blueprint の注入

ThinkingRequest はもはや `persona._generate()` / ActionHandler を通らず、
`manager/background.py` から `persona.llm_client.generate(messages, tools=[])` を直接呼ぶ。

したがって issue は「未着手」ではなく**一部完了**。案A/Bを完了ログへ移し、残件を
`ActionHandler + callback 残骸の最終撤去` に縮めるべき。

### `building_histories_dict_cleanup.md`

起票時の行番号とスコープが古い。現在も `building_histories` は manager state、runtime/admin
service、OccupancyManager、PersonaCore constructor、複数テストへ広く配線されている。
`api/routes/chat.py` の参照も起票時 `:111` ではなく現在は `:299`。

「空dictへのno-op writeを数箇所消す」だけではなく、互換 surface をどこまで落とすかの再監査が
必要。実装前に issue の残存箇所表を `rg` 結果で更新すること。

### `building_auto_interval_setting_removal.md`

「UIがあるか確認」段階ではない。現在も次が実在する:

- `database.models.Building.AUTO_INTERVAL_SEC`
- `BuildingSettingsModal.tsx` / `WorldEditor.tsx`
- `manager/admin.py` の保存
- `saiverse/buildings.py` の属性
- `SAIVerseManager` が no-op `ConversationManager` を building ごとに生成する際の interval

ConversationManager の最終撤去と同じ作業に束ねる方がよい。DB migration と setup/update
スクリプト整合が必要になるため、単純なUI掃除として扱わない。

### `head_prompt_followups.md`

内容は現況に追従しており in_flight にも載るが、doc status がない。Memory Atlas P2c が旧
Spell 18本を落とすため、「低頻度スペルを隠す」前に P2c 後の行数/文字数を再計測するのが自然。

### v1 自律関連

`track_autonomous` / `SubLineScheduler` 前提 issue は §1B の通り archive する。一方、
`persistent_track_complete_attempt.md` は現 Track/persistent 概念がまだ残るため active 維持。
Memory Atlas P3c の目的の木移行時に再評価する。

## 5. 大型追跡文書の分割候補

### `stackchan_multi_vessel_phase7_followups.md`

単一 issue 内に完了、実装済み検証待ち、未修正 blocker、upstream PR 状態、別issue参照が
大量に混在し、先頭 status を1つ置けない。次の分割が扱いやすい:

1. 完了項目 → 本文を closure log として archive/handoff に保存
2. 実機検証待ち → verification checklist 1本
3. 未修正 blocker → 1問題1issue
4. upstream PR 状態 → `stackchan_mcp_upstream_pr_strategy.md` のみを正典にする

`stackchan_multi_vessel_verification_handoff.md` は名前も内容も handoff なので、残検証が現役なら
`docs/handoff/` へ移し、issue 側からリンクする。

### `stackchan_unit_capability_requires_restart.md`

capability反映バグは本文上解決済み。残る ToF I2C 実機検証は別機能なので、同じ issue の
未完了条件にしない。分離後に本 issue を archive する。

## 6. archive 側の本文ドリフト

### `archive/history_manager_timestamp_tz_drift.md`

先頭が `🔲 未着手` のままだが、コードは `datetime.now(timezone.utc).isoformat()` に修正済み。
Git 履歴にも `d556e56 fix: timestamp TZ drift — datetime.now() を UTC 化 + issue アーカイブ移動`
がある。置き場所は正しいので、status とログだけ `✅ 解決済み (d556e56)` へ直す。

### `archive/arasuji_orphaned_source_ids.md`

status/log が無いが修正実装は存在する（§3）。archive のまま closure metadata を足す。

## 7. Markdown リンク監査

実在リンクとして直すべきもの:

| 文書 | 現リンク | 修正先 |
|---|---|---|
| `no_spell_mode.md` | `../intent/subplay_result_flow.md` | `../intent/archive/subplay_result_flow.md` |
| `archive/general_chronicle_user_pulse_only.md` | `general_chronicle_metabolism_trigger.md`（2箇所） | `../general_chronicle_metabolism_trigger.md` |
| `archive/stackchan_avatar_loader_device_reboot_cache.md` | `stackchan_avatar_psram_peak.md` | `../stackchan_avatar_psram_peak.md` |

`stackchan_serial_log_integration.md` の `../../memory/...` はリポジトリ外メモリへの参照で、
別環境から辿れない。必要な不変条件を issue 本文へ要約するか、repo内 doc に移す。

`[画像](.../image.jpg)`、`[text](URI)`、`[text](url)` のような説明用プレースホルダーは
リンク監査の false positive なので対象外。

## 8. 推奨整理順

### Pass 1: 判断不要の台帳修復（コード変更なし）

1. §1A の明確な完了7件を archive
2. §1B の v1廃止機構3件を superseded closure 付きで archive
3. 左サイドバー旧issueを duplicate closure 付きで archive
4. archive 2件の status/log、§7 のリンク3系統を修正

### Pass 2: active doc の status 正規化

1. statusless 5件に status/priority/date/related を追加
2. 検証待ち文書の先頭 `✅` を `🟣` 相当に変更
3. `spell_loop_continuation_contract` を実装待ちとして明示
4. README の状態語彙を in_flight と同期

### Pass 3: 内容の再スコープ

1. `legacy_action_handler_cleanup` を残存コードだけに縮める
2. `building_histories_dict_cleanup` の参照表を再生成
3. `building_auto_interval_setting_removal` を ConversationManager 撤去と統合
4. Stack-chan 大型追跡文書を issue / handoff / upstream tracker に分割

## 9. 監査後の完了条件

- `Get-ChildItem docs/issues/*.md` が「本当に未解決のもの」だけを返す。
- active issue は全て先頭に機械判定可能な status を持つ。
- `✅ 完了/解決済み` は active 直下に残らない。
- 削除済みファイルを修正対象として指す issue がない。
- handoff と issue の役割が混ざらない。
- Markdown相対リンク監査で、意図した外部メモリ参照以外の missing target がない。

## 10. 今回変更しなかったもの

監査の証跡を先に残すため、今回は issue ファイルの移動、status修正、リンク修正を実施していない。
停止中の Memory Atlas WIP とも無関係なため、次の独立した docs-only commit で Pass 1 を
まとめて行える。
