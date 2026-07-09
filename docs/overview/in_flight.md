# 進行中案件 台帳 (in-flight)

> **これは何**: いま動いている案件だけの索引。**状態の真実は各 intent(ステータス行)/issue(未解決↔archive フォルダ)が持ち**、この台帳はそこから「進行中(アクティブ)なもの」だけを抽出して **次アクション** と **誰待ち** を可視化する薄いビュー。完了・未着手(構想止まり)は載せない — それらは各 doc / issue フォルダで管理する。
>
> **番人**: 更新は Claude(メティス)が担う。まはーは台帳の更新を意識しなくてよい。動きがあったセッションで、Claude が終わる前に台帳と doc のステータスを現況に合わせる(下の「更新トリガー」)。
>
> **手前の容器**: まだ動いていない生アイディアは [アイディア帳 (ideas.md)](ideas.md) に溜める。そこで育ったものが intent 起草 or 実装着手で **この台帳に卒業** してくる。

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
| 🟣 検証待ち | gold_panning 砂金採り | SCENE 除去(NOTE のみ化)＋**クローズ window 起点を pan マーカー基準に修正**(metabolism anchor 流用で採取範囲が cache 都合に縮む問題、sophie 実機で判明)は完了。残: 実機で初回採取・defer-to-hot の確認 | まはー | [gold_panning.md](../intent/gold_panning.md) | 2026-07-08 |
| 🟠 実装中 | コア記憶の訂正導線 + ごみ箱 (短期対応) | **DB層✅ / API層✅ / メモリタブUI✅**(削除は実機確認済)。**仮想センサーは [知覚バッファ](../intent/perception_buffer.md) の一利用者として恒久対応済み・実機検証済み**(通知過多→応急停止→Phase 1a で push→Pulse消費集約に載せ替え、quon で確認)。残: 未確認バッジの置き場所を確定(ペルソナ固有アフォーダンス、住民/神モードUI と交差) | まはー(バッジ置き場) → 私 | [memory_architecture_v2.md](../intent/memory_architecture_v2.md) §5.1 | 2026-07-09 |
| 🔵 設計中 | 神モードUI (住民/神モードの二層プラットフォーム) | 自律行動の本格化で局所干渉では足りず俯瞰視点が必要に。住民モード(Building 主語・世界に入って暮らす)と神モード(Persona/世界を俯瞰・管理する創造主視点)を別UI・別タブに分離。Persona ホームは神モードの一要素。世界観 intent 起草 → 部分再設計の段階設計 | まはー(設計) | (intent 未起草) | 2026-07-08 |
| 🟡 実装待ち | quick_spell 本体実装 | サブエージェント委譲で実装(クオンのデータ修復は完了済) | まはー(GO) | [quick_spell.md](../intent/quick_spell.md) | 2026-07-08 |
| 🟡 実装待ち | runtime_llm.py 巨大 node 分割 | Phase 0(重複ヘルパ抽出)から着手。副産物で Beat 型導入 | 私 / まはー | [runtime_llm_node_split_design.md](../issues/runtime_llm_node_split_design.md) | 2026-07-08 |
| 🔵 設計中 | session_lifecycle Step 3 (Session 統一制御) | `session.md` を実装に移す。抽出済み SessionLifecycle に Session 識別と状態を持たせる | session.md 待ち | [session_lifecycle_extraction_design.md](../issues/session_lifecycle_extraction_design.md) / [session.md](../intent/session.md) | 2026-07-08 |
| 🔵 設計中 | v0.3.0 ④ オートノミー整理 | `autonomy_*` Playbook ↔ Track 機構の関係を設計決定(self_reflection の扱いもここ次第)。v0.3.0 リリース最後の設計未決 | まはー | [v030_release_worklist.md](v030_release_worklist.md) | 2026-07-08 |
| 🟠 実装中 | 知覚バッファ (Perception Buffer) | 核=「主観時間は Pulse でのみ進む／知覚は Pulse 消費時のみ／未消費の間だけ型別 reduce／知覚イベントは起動力属性を持ち決定論で反応可否を決める」。差分通知・メタ記憶訂正・入室想起・**会話取り込み**を一本化する横断基盤。**Phase 1a 実機検証済み**(quon)。**Phase 2a=3直挿入撤去 実装済み・実機検証待ち**(world_state差分/persona_recall もバッファ経由に。検知(push)と消費(flush)を分離〈§4.5〉、4呼び出しサイトの timing 契約を一貫化。pytest 81件)。残: **Phase 1b/2b=起動力ディスパッチャ+会話取り込み統合**は「他ペルソナ発話が Pulse を起こす新能力=Phase 5 UC-2」と重なる設計フォーク(会話型 render 拡張・salience 判定・pulse_controller 接続)→ Phase 5 と足並み揃えて設計後に実装。Phase 3=プレビュー UI。方針確定済(粒度=per-persona・永続化・検知/消費分離・恒久二重禁止) | まはー(2a 実機検証 / 1b・2b 設計方針) → 私 | [perception_buffer.md](../intent/perception_buffer.md) | 2026-07-09 |
| 🔵 設計中 | Physical Ear (常時リッスン音声入力) | 骨子確定 (別 Fixture タイプ / 応答者全員 / transport 案A / 非蓄積)。残論点 (continue セッション管理・判断層 E4B 実行環境・応答中の耳) を詰める → PC マイク+母艦捕捉クライアントを最初の検証ケースに実装 | まはー(残論点) → 私(実装) | [physical_ear.md](../intent/physical_ear.md) | 2026-07-09 |
| 🔵 設計中 | SwitchBot 連携 | 末尾「未確定事項」を詰めて確定 → 実装(Observer の利用者) | まはー(レビュー) | [switchbot_integration.md](../intent/switchbot_integration.md) | 2026-07-08 |
| 🔵 設計中 | アドオン拡張点 (OAuth / speak hooks) | draft レビュー → 汎用 `OAuthFlowSection` / persona_speak hook の実装 | まはー(レビュー) | [addon_extension_points.md](../intent/addon_extension_points.md) / [addon_speak_hooks.md](../intent/addon_speak_hooks.md) | 2026-07-08 |
| 🟡 実装待ち | head スペル一覧ダイエット | 統合ダイエット(document 7→4・item 2→1、二重実装/アイソレーション漏れ解消)は**実機確認済**(2026-07-09、まはー)。残(本 issue 本線): 使用頻度の低いスペルの `visible=false` 化 + `addon_spell_help` 型の遅延開示を builtin にも(候補=life_purpose_set / observer_read / messagelog_get_around / send_email_to_user) | 私 | [head_prompt_followups.md](../issues/head_prompt_followups.md) | 2026-07-09 |
| 🟣 検証待ち | アイディア帳由来 UI 修正3件(Cityタイトル / アイテムサムネイル / usage通貨) | 3 worktree を feature へマージ済。**サムネイル=実機確認済**(2026-07-09、軽量 webp 化で解決)。残: City名反映・通貨別グラフの実機確認 | まはー(検証) | [ideas.md](ideas.md) 由来 | 2026-07-09 |

<!-- 構想止まり(当分動かない)は台帳外。intent draft で管理: observer/Fixture, Track解体(目的の木), Social Track 入口(Phase 5) など -->
