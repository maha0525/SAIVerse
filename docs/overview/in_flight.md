# 進行中案件 台帳 (in-flight)

> **これは何**: いま動いている案件だけの索引。**状態の真実は各 intent(ステータス行)/issue(未解決↔archive フォルダ)が持ち**、この台帳はそこから「進行中(アクティブ)なもの」だけを抽出して **次アクション** と **誰待ち** を可視化する薄いビュー。完了・未着手(構想止まり)は載せない — それらは各 doc / issue フォルダで管理する。
>
> **番人**: 更新は Claude(メティス)が担う。まはーは台帳の更新を意識しなくてよい。動きがあったセッションで、Claude が終わる前に台帳と doc のステータスを現況に合わせる(下の「更新トリガー」)。

## 状態の語彙

進行中(＝台帳に載る)は 4 段階:

| | 状態 | 意味 |
|---|---|---|
| 🔵 | **設計中** | intent 起草・詰め・レビュー待ち(まだコードを書いていない) |
| 🟡 | **実装待ち** | 設計確定。コード未着手だが「次にやる」候補 |
| 🟠 | **実装中** | コードを書いている |
| 🟣 | **検証待ち** | 実装済み。実機/まはー検証待ち |

台帳外(各 doc / issue フォルダで管理):
**未着手**(構想止まり・当分動かない) / ✅ **完了**(実装+検証済) / 💤 **凍結**(意図的に止めた)

## 状態の置き場 (source of truth)

- **issue**: フォルダ位置。`docs/issues/` = 未解決 / `docs/issues/archive/` = 完了
- **intent**: 各 doc 冒頭のステータス行(語彙は上表 + 完了/凍結)
- **この台帳**: 上記から「進行中」だけを抽出した索引。**状態は背負わない**(doc が真実、台帳はビュー)

## 更新トリガー (Claude が回す)

| トリガー | やること |
|---|---|
| **進行中入り** | 台帳に行追加 + doc 側を進行中に(issue=未解決に置く / intent=ステータス行を設計中〜検証待ちに) |
| **進行中の変化** | 台帳の該当行(状態 / 次アクション / 誰待ち)を更新 |
| **完了** | 台帳から削除 + issue は `archive/` へ移動 / intent はステータス行を「完了」に / 必要ならメモリも |
| **却下・凍結** | 台帳から外す + doc に理由を記録 |

「誰待ち」= **まはー**(判断/レビュー/検証/GO) / **私**(Claude が次の一手) / **外部**(CI・他リポPR・実機部材など)。

---

## 台帳

| 状態 | 案件 | 次アクション | 誰待ち | doc / issue | 更新 |
|---|---|---|---|---|---|
| 🟠 実装中 | 自律行動v2 活性化配線 | playbook 5本 import → `--real` 行動テスト(まはーレビュー) → 起床/就寝/セッション終了/会話終了/on_event の配線 → 旧 track_autonomous 停止 | 私 → まはー | [autonomous_behavior_v2.md](../intent/autonomous_behavior_v2.md) | 2026-07-08 |
| 🟣 検証待ち | gold_panning 砂金採り | 実機で採取(コア記憶化)とセッションクローズ・defer-to-hot の動作確認 | まはー | [gold_panning.md](../intent/gold_panning.md) | 2026-07-08 |
| 🟡 実装待ち | quick_spell 本体実装 | サブエージェント委譲で実装(クオンのデータ修復は完了済) | まはー(GO) | [quick_spell.md](../intent/quick_spell.md) | 2026-07-08 |
| 🟡 実装待ち | runtime_llm.py 巨大 node 分割 | Phase 0(重複ヘルパ抽出)から着手。副産物で Beat 型導入 | 私 / まはー | [runtime_llm_node_split_design.md](../issues/runtime_llm_node_split_design.md) | 2026-07-08 |
| 🔵 設計中 | session_lifecycle Step 3 (Session 統一制御) | `session.md` を実装に移す。抽出済み SessionLifecycle に Session 識別と状態を持たせる | session.md 待ち | [session_lifecycle_extraction_design.md](../issues/session_lifecycle_extraction_design.md) / [session.md](../intent/session.md) | 2026-07-08 |
| 🔵 設計中 | v0.3.0 ④ オートノミー整理 | `autonomy_*` Playbook ↔ Track 機構の関係を設計決定(self_reflection の扱いもここ次第)。v0.3.0 リリース最後の設計未決 | まはー | [v030_release_worklist.md](v030_release_worklist.md) | 2026-07-08 |
| 🔵 設計中 | SwitchBot 連携 | 末尾「未確定事項」を詰めて確定 → 実装(Observer の利用者) | まはー(レビュー) | [switchbot_integration.md](../intent/switchbot_integration.md) | 2026-07-08 |
| 🔵 設計中 | アドオン拡張点 (OAuth / speak hooks) | draft レビュー → 汎用 `OAuthFlowSection` / persona_speak hook の実装 | まはー(レビュー) | [addon_extension_points.md](../intent/addon_extension_points.md) / [addon_speak_hooks.md](../intent/addon_speak_hooks.md) | 2026-07-08 |

<!-- 構想止まり(当分動かない)は台帳外。intent draft で管理: observer/Fixture, Track解体(目的の木), Social Track 入口(Phase 5) など -->
