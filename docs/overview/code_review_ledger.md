# SAIVerse コードレビュー台帳

> **目的**: SAIVerse の各サブシステムを、ファイル単位ではなく不変条件と実行経路の単位で継続監査する。レビュー結果を一度きりの所感にせず、再現テストと修正状況まで追跡する。
> **工程はここではなく [監査対応 完了計画書](audit_remediation_plan.md) が持つ** — 「どこまで終わって次に何をやるか」は計画書、finding 単位の消し込み状態は本台帳。
> **運用体制 (2026-07-12 まはー)**: 一次監査の実施は Codex (GPT-5.6 Sol)、設計・実装指揮・指摘の裏取りと修正は Claude (Fable/メティス) の役割分担。レビュー指摘は head のコードで裏取りしてから台帳・修正に反映する。

## 状態

| 状態 | 意味 |
|---|---|
| 未監査 | 体系的なレビューをまだ行っていない |
| 一次監査中 | 境界・不変条件・主要経路を確認中 |
| 指摘あり | 再現可能な指摘があり、修正待ち |
| 回帰固定済み | 指摘を修正し、同型事故を防ぐテストがある |
| 実機確認済み | 回帰テストに加え、実際の運用経路でも確認済み |

## 共通の監査軸

1. **データ保全** — 生ログ・本文・履歴を欠落または無断変形させない。
2. **人格境界** — persona / thread / city / model / line の境界を越えて状態を混ぜない。
3. **自己著者性** — システム生成・ユーザー訂正・本人決定を区別し、本人名義を捏造しない。
4. **原子性と可逆性** — 複合更新は途中状態を残さず、失敗報告と実状態を一致させる。
5. **参照整合性** — 派生物から一次データへの参照を壊さず、削除・移行後も正直に扱う。
6. **観察可能性** — 重要な分岐・失敗・移行結果をログまたは履歴で追跡できる。

## 台帳

| 優先 | サブシステム | 状態 | 最終監査 | 結果 / 次アクション | 記録 |
|---|---|---|---|---|---|
| P0 | 記憶・人格境界 | **指摘あり（一次監査完了）** | 2026-07-17 | 合計 **P1×18 / P2×2**（Atlas編纂P1×3含む）。P1×10 / P2×2は修正・回帰固定済み（snapshot restore staging/rollback化は第二陣 / **native import P1×2+P2×1は柱4=復元/移植分離で消し込み 2026-07-17**: 復元は不一致を書き込み前拒否・移植は明示フラグ+原子写像+transplanted_from provenance・replace単一トランザクション・embedding非生成化。回帰=tests/test_native_import_separation.py 10件）、P1×2はまはー裁定で現状仕様として保留。**まはー裁定(2026-07-16): RemoteProxy転送/visitor heard_byのP1×2はmulti-city凍結スコープへ**。**Metabolism×3=統合工事§6-5で消し込み(2026-07-17)**: 共通排他なし=実行台帳の冪等claim(world DB UNIQUE=プロセス/コネクション横断。arasuji側のsource集合UNIQUE制約は未) / 無効・失敗でもanchor前進=status退役ゲート(disabledの非圧縮退役は設計として引き受け) / TTL失効後の旧anchor touch=call-local化(metabolism_anchor_message_id全廃)。head snapshot model未分離も§6-3bで消し込み。残=Building転記cursor×1(→柱1) | [一次監査](../handoff/2026-07-12_memory_persona_boundary_audit.md) / [Atlasレビュー](../handoff/2026-07-12_concept_consolidation_code_review.md) |
| P0 | migration / upgrade / backup | **第二陣修正・回帰固定済み** | 2026-07-16 | 第一陣に加え、mutation前pre-upgrade世代、検証済み独立retention、停止状態DB restore、upgrade chain fail-closed、world snapshot v2/staging rollback、process marker、全updater共通engine、同一argv/City/DB再起動healthまで固定。persona `memory.db`個別backupは独立維持 | [一次監査](../handoff/2026-07-13_migration_upgrade_backup_audit.md) / [第二陣Intent](../intent/audit_second_batch_hardening.md) |
| P0 | 自律行動・判断点・schedule | **指摘あり（一次監査完了）** | 2026-07-15 | 現行HEAD `113567e` で **P1×13 / P2×1**（P1×1は`e3c78cb`で回帰固定済み）。全起動入口、life/budget/slot/Episode、5種finalize、manual、schedule CRUD/発火失敗までcoverageを完了。以後は修正追跡 | [一次監査](../handoff/2026-07-14_autonomy_judgment_schedule_audit.md) |
| P1 | SEA runtime / Session / head-tail | **指摘あり（P1×4+P2×1修正済み）** | 2026-07-17 | **P1×6 / P2×3** のうち **P1×4 / P2×1 は統合工事§6で修正・回帰固定済み (2026-07-17)**: S1 実model無視のanchor更新=§6-3a(usage.model記帳) / S2 Chronicle失敗後anchor前進=§6-5(status退役ゲート) / S3 配送前last_notified前進=§6-4(outbox確定後にB前進) / S4 Stelis復元漏れ=§6-6a(threadスタック+graph finally巻き戻し+クラッシュ孤児復旧) / S8 anchor並列RMW=§6-3a(session_anchor行upsert)。**残 P1×2 / P2×2**: S5 perception保存失敗の成功扱い=**部分残**(§6-4でoutbox配送化・flushの例外時保持は済みだが、append戻り値Noneの静かな失敗経路が未検査→柱1) / S6 head capture失敗時もLLM実行(→柱1) / S7 秒精度timestamp境界(→柱6) / S9 token trigger件数gate拒否(→柱8) | [一次監査](../handoff/2026-07-15_sea_runtime_session_head_tail_audit.md) |
| P1 | Spell / Tool / Playbook 権限 | **指摘あり（一次監査完了）** | 2026-07-16 | 現行HEAD `113567e` で **P1×5 / P2×2**。うち **P2×1**（`run_playbook`候補名漏洩）は第一陣、**P1×3**（run_playbook city権限 / Aspect権限のtool node迂回 / disabled addon）は第二陣の共通実行時認可gateで修正・回帰固定済み。P1×1（realtime spell）は部分修正（`SPELL_ENABLED`迂回・auto_mode固定が残存）。残: `_`予約namespace、入力contract | [一次監査](../handoff/2026-07-15_spell_tool_playbook_permission_audit.md) |
| P1 | Persona / City / Building 分離 | **指摘あり・version境界修正済み・multi-city凍結(入口封鎖済み)** | 2026-07-16 | City移動profileへSAIVerse versionを必須化(完全一致要求)。**まはー裁定(2026-07-16): multi-city機能は凍結** — dispatch確定処理未実装/来訪profile署名/RemoteProxy転送のP1×2は修正でなく入口封鎖で対応。**封鎖実装済み(2026-07-16)**: inter-city/persona-proxy API=503+凍結メッセージ、VisitingAI/ThinkingRequest polling不起動、dispatch_persona/return_visiting_persona入口ガード、回帰=tests/test_multi_city_freeze.py。残りの単一City内finding(移動原子性/occupancy一意性/chat境界/Region/City変更)は修正追跡を継続 | [一次監査](../handoff/2026-07-15_persona_city_building_separation_audit.md) |
| P2 | API / frontend | **第二陣境界修正・回帰固定済み** | 2026-07-16 | 第一陣に加え、LAN owner認証/CORS/Origin、secret write-only応答、managed path、bounded/streaming upload、model/provider credential接続先束縛、user utterance先行永続化・idempotencyを共通境界へ実装 | [一次監査](../handoff/2026-07-15_api_frontend_audit.md) / [第二陣Intent](../intent/audit_second_batch_hardening.md) |
| P2 | 外部連携（LLM / Addon / MCP / Discord） | **第二陣境界修正・Discord保留** | 2026-07-16 | 実行時中央認可、stream commit point、結果不明MCP callの再送禁止、暗黙paid fallback撤去、provider接続先束縛、Addon HTTPS/full SHA/署名検証を固定。公式署名鍵のpublishは運用側必須作業。Discordはまはー判断どおり対象外 | [一次監査](../handoff/2026-07-15_external_integration_audit.md) / [第二陣Intent](../intent/audit_second_batch_hardening.md) |

## 運用

- 新機能は「実装完了 → コードレビュー → 回帰テスト固定 → 実機検証」の順に通す。
- finding は重大度、根拠行、最小再現、影響、修正後に必要なテストを必ず記録する。
- 修正済みでもテストが無ければ「回帰固定済み」にしない。
- 一次監査は全行読破を意味しない。監査文書の coverage に、見た範囲と未確認範囲を明記する。
