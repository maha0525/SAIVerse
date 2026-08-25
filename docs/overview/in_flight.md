# 進行中案件 台帳 (in-flight)

> **これは何**: いま動いている案件だけの索引。**状態の真実は各 intent(ステータス行)/issue(未解決↔archive フォルダ)が持ち**、この台帳はそこから「進行中(アクティブ)なもの」だけを抽出して **次アクション** と **誰待ち** を可視化する薄いビュー。完了・未着手(構想止まり)は載せない — それらは各 doc / issue フォルダで管理する。
>
> **次アクション欄の器 (2026-08-04 確立)**: 書けるのは「**現在地 1 文 + 次の一手 1〜2 文**」だけ(上限 300 字 — 関所が機械検査する)。過去形の記録 — 日付・コミットハッシュ・裁定の経緯・レビューの明細・教訓 — はここに書かない。それらの行き場は各案件 doc の「経緯」節(教訓だけは memory)。この規定の前は次アクション欄が唯一の置き場だったため経緯が積もり続け、台帳が 123KB まで肥大した。
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
- **経緯(進捗の履歴)**: 各案件 doc の「経緯」節。**台帳には置き場がない**ので書かない — 書きたくなったら doc へ

## 更新トリガー (Claude が回す)

| トリガー | やること |
|---|---|
| **進行中入り** | 台帳に行追加 + doc 側を進行中に(issue=未解決に置く / intent=ステータス行を設計中〜検証待ちに) |
| **進行中の変化** | 台帳の該当行(状態 / 次アクション / 誰待ち)を**差し替える**。押し出される旧文面は削除でなく**同一コミットで doc(intent / issue)の「経緯」節へ移送**(教訓だけは memory へ) |
| **完了** | 台帳から削除 + issue は `archive/` へ移動 / intent はステータス行を「完了」に / 必要ならメモリも |
| **却下・凍結** | 台帳から外す + doc に理由を記録 |

「誰待ち」= **まはー**(判断/レビュー/検証/GO) / **私**(Claude が次の一手) / **外部**(CI・他リポPR・実機部材など)。

**関所**: 台帳を触ったセッションの終わりに `python scripts/check_in_flight.py` を回す。次アクション欄の字数超過と過去形マーカー(日付・コミットハッシュ)の混入を検査し、違反行を列挙して失敗する。散文の規則だけでは守れなかった実績があるための機械検査。例外は台帳解体(2026-08-04)時点で進行中セッションの縄張りだった未移送3行のみ — 当時の行のまま(行全体の指紋一致)に限り警告扱いで、どの列でも書き換えた瞬間に本検査へ昇格する。終了コードの契約: exit 0 = 違反なし(経過措置の警告のみは通す) / exit 1 = 免除外の違反または表構造の不正。台帳の表は**外側パイプ付きの正規形 1 つだけ**が仕様 — GFM が許す変形(外側パイプ省略・先頭空白の表)は関所が構造違反として落とす。

---

## 台帳

| 状態 | 案件 | 次アクション | 誰待ち | doc / issue | 更新 |
|---|---|---|---|---|---|
| 🟣 検証待ち | llama-server idle 停止の busy 判定修正 (処理中サーバー射殺の根治) | 実装は現ブランチへ取り込み済み (busy 判定を /health から /slots の三値化へ、貸出札と ensure_running の高速/低速分離、専用テスト46件)。次 = ①実機で長時間推論が停止されず完走することの確認 ②intent「受け入れた限界」8項のまはー裁定 ③止血で入れた idle_timeout:0 を戻すかの判断。 | まはー (実機検証・限界8項の裁定) | [llama_server_auto_launch.md](../intent/llama_server_auto_launch.md) | 2026-08-11 |
| 🟣 検証待ち | Chronicle 退場の適用側拒否権デッドロック根治 | 二つの顔 (編纂対象ゼロ fold の吸収限定退場 / あらすじ手動削除の道連れ) とも実装済み・回帰緑。次 = 実機で通常 Metabolism の退行がないことの確認。 | まはー (実機検証) | [issue](../issues/chronicle_eviction_applier_veto_deadlock.md) / [chronicle_eviction.md](../intent/chronicle_eviction.md) §2/§5-5/§6 | 2026-07-27 |
| 🟣 検証待ち | あらすじのレベル制 (記憶の川の一本化) | 実装完了。エリスは実機修復と初編纂まで成功、air は点検の結果修復不要。次 = aifi の再編纂 (未編纂期間の消化、汎用ツール整備済み) と LLM 束ね品質の本番初発火の観察。提示側の簡素化は presentation_gap 実機検証後へ先送り (intent §12-7)。 | まはー (aifi 実施のタイミング) | [intent](../intent/arasuji_levels.md) | 2026-07-29 |
| 🟣 検証待ち | 編纂入口の一本化 (arasuji_levels §13) | §13 (入口一本化) と §14 (冷えた anchor の保守経路) とも実装済み・レビュー消し込み完了・全テスト緑。派生の新 issue (関所閉鎖の slot 消費) は裁定待ち・別件。次 = 実機検証 (会話開始で「1件」ダイアログが出ない / 整理ボタンが直近を残して畳む)。 | まはー (実機検証) | [intent](../intent/arasuji_levels.md) §13-§14 | 2026-07-29 |
| 🟣 検証待ち | 🔴 Chronicle 提示が途中の期間を黙って落とす | 走査と圧縮を分離する直しを実装済み — 全ペルソナで提示から落ちる一次あらすじゼロ・予算内を確認、回帰固定済み。孤児あらすじは編纂側の管轄で本件の範囲外。次 = 実機で head に直近の記憶が戻っていることの確認。 | まはー(実機で head に直近が戻っているか確認) | [chronicle_presentation_gap.md](../issues/chronicle_presentation_gap.md) | 2026-07-25 |
| 🔵 設計中 | 想起用タグ + Track の役割縮小 | 想起の読み書きは v3 §12〜§13 でスキーマ具体形まで確定 (書き手 = 機構コール相乗り + スルース、器 = 手帳、読み側 = 知覚の差し込み)。次 = v3 の文面レビュー通過後、本 intent の §9 以降を v3 参照で改稿し、Track 縮小部分だけを残して整理する。 | まはー (v3 文面レビュー) | [recall_tags_and_track_reduction.md](../intent/persona_cognition/recall_tags_and_track_reduction.md) | 2026-08-18 |
| 🔵 設計中 | モデル定義の外部配布とカタログ | 段階1〜2 (同梱モデルの provider_ref 移行・接続情報の排除) は実装・検証済み。副産物で Gemini 無料枠キー・ollama 設定不達・LM Studio キー必須の欠陥3件も根治。次 = 未決3点 (配布元リポジトリ / カタログスキーマ / 取得時差分提示) のまはーレビューから段階3へ。 | まはー(段階3以降のレビュー・未決3点) | [model_catalog_distribution.md](../intent/model_catalog_distribution.md) | 2026-07-23 |
| 🟣 検証待ち | 編纂の対称化 = 読み戻し (arasuji_levels §15) | 実装完了・レビュー収束・フルスイート緑。読み戻しは印の切替だけで往復とも LLM ゼロ、プレビューにも反映済み。次 = 実機検証 (送信前プレビューに厚い生ログ / 開き直しとプレビューの一致 / head との二重表示なし / 超過時の編纂ゼロ畳み)。 | まはー (実機検証) | [issue](../issues/metabolism_refill_when_below_target.md) / [intent](../intent/arasuji_levels.md) §15 | 2026-07-30 |
| 🟣 検証待ち | チャットオプション「データ送信量の管理」再設計 | 実装完了・レビュー収束。新陳代謝は常時 ON 化・水位はモデル定義一本・当該セクションは読み取り専用の状態表示へ。次 = 実機検証 (水位バーとプレビューの整合 / トグル消滅後の通常動作 / モデル編集→表示反映)。 | まはー (実機検証) | [issue](../issues/chat_options_metabolism_section_redesign.md) | 2026-07-30 |
| 🟠 実装中 | Godot / ARDY 仮想身体デモ | 身体移動・gesture 生成・プレイヤー身体・視線正規化・一人称撮像・表情 (Expression Studio + preset 遷移) まで隔離 E2E 済み。次 = player locomotion の idle↔walk/run、自動瞬き・視線追従、実 HMD、発話同期 /emote、Idle scheduler、接地確認。本番ペルソナを用いる確認は操作ごとの明示承認がある場合のみ。 | 私(プレイヤー歩行・瞬き・視線・実VRM/HMD・`/emote`接続・Idle scheduler) / まはー(表情preset作成・実HMD確認) | [virtual_embodiment_godot.md](../intent/virtual_embodiment_godot.md) | 2026-07-20 |
| 🟠 実装中 | コードレビュー運用・残存finding修正 | W10 (柱8) の消し込みはレビュー 5 巡で収束・フルスイート緑。Playbook 許可判定は EXEC とスペルで一本化し、事前承認は「ユーザーが書いた起動」に紐づく形。許可ゲートの 2 件は裁定済み。次 = まはーの実機検証 (自律 Pulse で確認ダイアログが出ない / スケジュール指定の Playbook が起動する / SPELL_ENABLED=false で realtime spell が止まる)。A3 は着手裁定待ち。 | まはー(実機検証 + A3 裁定) | [完了計画書](audit_remediation_plan.md) / [W10 走行メモ](../handoff/2026-08-16_w10_spell_audit_remnants_handoff.md) | 2026-08-17 |
| 🟠 実装中 | Beat/ExecutionContext (SEA実行基盤の一本化) | §6-1〜§6-7 (ExecutionContext 導入 / Beat ロック+関所 / anchor・head のフルキー化 / 内容型通知 / Metabolism 二層分離 / thread push-pop / 正典改訂) まで実装・検収済み。残 = §6-6b Beat ロックのトークン化の分離裁定 → 全 wave 完了後に gen_reference_docs 一括再実行。 | 私(実装続行) | [beat_execution_context.md](../intent/beat_execution_context.md) | 2026-07-17 |
| 🟠 実装中 | 実行台帳 (execution ledger) | 段階移行 Phase 0〜5 が全段実装済みだが、v0.3 の止め具で判断点・時間割・配送の段は発火しない。次 = 実機検証はスルース (Metabolism) 経路だけを見る。判断点・時間割・配送の段の検証は v0.4 で運転を配線するときに一緒に行う。 | まはー(実機検証) | [execution_ledger.md](../intent/execution_ledger.md) | 2026-07-21 |
| 🟣 検証待ち | ⑥ 概念再編（九龍城の解体） | Memory Atlas P1〜P4 と参照文法の統合・写真→クリップ改名まで実装済み、レビュー指摘も修正済み。次 = 実機検証 (就寝判断の棚の乱れ / テーマの芽・朝の報告・エアの目次 / 改名後初回起動の机移行ログ) → ①実機テスト。 | まはー(実機検証) | [concept_consolidation.md](../intent/concept_consolidation.md) / [life_concept_map.md](../intent/persona_cognition/life_concept_map.md) | 2026-07-12 |
| 🟣 検証待ち | 体験の構造 (記憶系の統一 intent) | 工程 (1)〜(4) すべて実装済みで実機検証待ち。工程 (4) 知覚レンダリング=W14 は消費バッチ設計で実装・レビュー収束・回帰緑。次 = 実機検証 (手順は W14 walkthrough handoff に記載。工程 1〜3 の検証と並行可)。消費者配線は後続。 | まはー(工程1〜4実機検証) | [experience_structure.md](../intent/experience_structure.md) / [W14 handoff](../handoff/2026-08-19_w14_perception_rendering_handoff.md) | 2026-08-19 |
| 🔵 設計中 | Ultramemory (想起の第2層) | intent 起草済み・まはー GO 済み。想起用タグが先行し本件はそれを当然に利用する関係で確定。次 = まはーレビューで論点 (課金同意面 / 第0層との排他か併用か / 蘇生範囲) を詰めて設計確定。 | まはー(レビュー) | [ultramemory.md](../intent/ultramemory.md) | 2026-07-25 |
| 🟠 実装中 | コア記憶の訂正導線 + ごみ箱 (短期対応) | DB 層・API 層・メモリタブ UI まで実装済み (削除は実機確認済み)。仮想センサー側は知覚バッファの一利用者として恒久対応・実機検証済み。残 = 未確認バッジの置き場所の確定 (ペルソナ固有アフォーダンス、住民/神モード UI と交差)。 | まはー(バッジ置き場) → 私 | [memory_architecture_v2.md](../intent/memory_architecture_v2.md) §5.1 | 2026-07-09 |
| 🔵 設計中 | resolve_uri 切り詰めの継続読み (memopedia tree 等) | 上限は resolve_many 共通経路。論点4つ (続きの単位=サブツリー URI 推奨 / 切り詰め案内の決定論埋め込み=必須 / depth 密度制御 / 適用範囲=まず tree 型) を提示済み。次 = まはーと仕様詰め → 実装。 | まはー(仕様詰め) | [issue](../issues/resolve_uri_truncation_continuation.md) | 2026-07-12 |
| 🟡 実装待ち | メティス記憶ブリッジ | 設計は主要点まで確定 (配置=休眠ペルソナ / 1セッション=1thread / Chronicle thread 分離 / 独り言・thinking 既定OFF / 段階的取り込み・冪等)。残る裁定は persona_id と配置先 City のみ。次 = まはーの裁定 → ブートストラップ取り込みから実装。 | まはー(persona_id/City)→私(実装) | [metis_memory_bridge.md](../intent/metis_memory_bridge.md) | 2026-07-13 |
| 🟠 実装中 | SAIVerse Lite (スマホ単体アプリ) | v1 骨格の磨き込み完了 (UI 本体化 / 公式エクスポート対応 / 初回ウィザード / 法務 / API キーガイド / キャッシュ opt-in)、テスト・build 全緑。次 = 未 push コミットの push+再デプロイ → スマホ実機通し・実 API キャッシュ実測・引っ越し E2E (ブロッカー=本体側ペルソナ一括受け口)。 | まはー(push+再デプロイ+実機) | [saiverse_lite.md](../intent/saiverse_lite.md) | 2026-07-16 |
| 🟣 検証待ち | SAIVerse Lite 端末内モデル (Gemma 4 E2B/E4B) | provider・エンジン寿命管理・UI まで実装し、PC ブラウザでペルソナとの会話が端末内だけで通る状態 (別リポジトリ saiverse-lite の feat/local-gemma、未 push)。設計は Lite 側 docs/gemma4_on_device.md。次 = まはーのスマホ実機確認 (WebGPU が HTTPS 必須のため配備が前提) と、端末保存 (OPFS) の実装順 C。 | まはー(スマホ実機) → 私(実装順 C) | [saiverse_lite.md](../intent/saiverse_lite.md) | 2026-08-21 |
| 🔵 設計中 | 神モードUI (住民/神モードの二層プラットフォーム) | 自律行動の本格化で局所干渉では足りず俯瞰視点が必要に。住民モード(Building 主語・世界に入って暮らす)と神モード(Persona/世界を俯瞰・管理する創造主視点)を別UI・別タブに分離。Persona ホームは神モードの一要素。世界観 intent 起草 → 部分再設計の段階設計 | まはー(設計) | (intent 未起草) | 2026-07-08 |
| 🟡 実装待ち | quick_spell 本体実装 | サブエージェント委譲で実装(クオンのデータ修復は完了済) | まはー(GO) | [quick_spell.md](../intent/quick_spell.md) | 2026-07-08 |
| 🟣 検証待ち | runtime_llm.py 巨大 node 分割 | Phase 0〜1 (重複ヘルパ抽出 / ④確定部抽出+BeatExecution 導入) 実装済み・回帰緑。次 = まはー実機スモーク (通常会話 / spell 入り / TOOL ノード / streaming off の4パターン、ログと履歴が分割前と同型か) → Phase 2 (4 経路の分離)。 | まはー(スモーク) → 私(Phase 2) | [runtime_llm_node_split_design.md](../issues/runtime_llm_node_split_design.md) | 2026-07-22 |
| 🔵 設計中 | session_lifecycle Step 3 (Session 統一制御) | [life.md](../intent/life.md) が session.md §6 未確定事項に回答済み (ライフ終端=終了第一基準)。life.md レビュー通過後に session.md を吸収改訂 → Step 3 再開 | life.md レビュー待ち | [session_lifecycle_extraction_design.md](../issues/session_lifecycle_extraction_design.md) / [session.md](../intent/session.md) | 2026-07-13 |
| 🟣 検証待ち | v0.3.0 ④ オートノミー整理 | 退役・巻き取り migration・docs 追従まで実装済み、dry-run で prune 6件を確認済み。次 = 実機再起動で巻き取り+prune の本走行確認。 | まはー(再起動確認) | [v030_release_worklist.md](v030_release_worklist.md) | 2026-07-10 |
| 🟡 一部完了(残は後回し) | 知覚バッファ (Perception Buffer) | Phase 1a/2a/3 と移動時拡充まで実機検証済み・完了 (ペルソナ評判良好)。残 = Phase 4 (凍結概念の一般化・独立着手可) / Phase 1b・2b (起動力+会話統合、Phase 5 と足並み) / Phase 3 項目編集 — すべて後回し、再開はまはー GO。 | 後回し (再開はまはー GO) | [perception_buffer.md](../intent/perception_buffer.md) | 2026-07-09 |
| 🔵 設計中 | Physical Ear (常時リッスン音声入力) | 骨子確定 (別 Fixture タイプ / 応答者全員 / transport 案A / 非蓄積)。残論点 (continue セッション管理・判断層 E4B 実行環境・応答中の耳) を詰める → PC マイク+母艦捕捉クライアントを最初の検証ケースに実装 | まはー(残論点) → 私(実装) | [physical_ear.md](../intent/physical_ear.md) | 2026-07-09 |
| 🔵 設計中 | SwitchBot 連携 | 末尾「未確定事項」を詰めて確定 → 実装(Observer の利用者) | まはー(レビュー) | [switchbot_integration.md](../intent/switchbot_integration.md) | 2026-07-08 |
| 🔵 設計中 | アドオン拡張点 (OAuth / speak hooks) | draft レビュー → 汎用 `OAuthFlowSection` / persona_speak hook の実装 | まはー(レビュー) | [addon_extension_points.md](../intent/addon_extension_points.md) / [addon_speak_hooks.md](../intent/addon_speak_hooks.md) | 2026-07-08 |
| 🟡 実装待ち | head スペル一覧ダイエット | 統合ダイエットは実機確認済み。残 (本 issue 本線) = 低頻度スペルの visible=false 化 + 遅延開示を builtin にも (候補 = life_purpose_set / observer_read / messagelog_get_around / send_email_to_user)。 | 私 | [head_prompt_followups.md](../issues/head_prompt_followups.md) | 2026-07-09 |
| 🔵 設計中 | 経験の台帳と経験値ノート (再開の想起の本体) | 実装は済んでいるが、書く席 (コマ締め) と吸収先 (就寝ふりかえり) が v3 で退役対象になり宙に浮いている — 経験値ノートの概念は存続。次 = v3 §12 の暮らしの設計の決着を受けて、新しい席と読み側の形を決め直す。 | まはー (v3 設計議論) | [experience_ledger.md](../intent/experience_ledger.md) | 2026-08-15 |
| 🟠 実装中 | RSS フィード施設 (偶然の供給側) | **intent たたき台 v0.1 起草 (2026-08-01)**。timetable_redesign と対 (あちらが受信側=時間割、こちらが供給側=世界の情報源)。骨子 = RSS/Atom 取得を Building 単位の購読で施設化 / プリセットを builtin 三層優先で配布し City 作成時に選択 / 提示は発言機会+知覚バッファ投入の二経路 / 提示層は転載のみで生成しない・「今日の数本」の選択は決定論 / 取得失敗は正直に (空の新聞は空と示す) / 内部フィード (図書館・美術館のアイテム格納公開、City 掲示板構想の実現経路) も同じ仕組み。単体でも価値 (現行時間割のまま全ペルソナにエリスの Elyth 相当を配れる)。レビュー 1 巡目で裁定 6 点 (2026-08-02): 購読は Fixture 持ち / 今日の数本 = 新着のみ / ユーザーも読める UI を出す (透明性確保は SAIVerse の仕事) / 内部フィードは後 / 既読 = フィードごとカーソル方式 / 記憶への残り方 = 専用機構なし (増幅段は timetable 封印済み・単発誤読は受容・出典必須化は却下 → 来歴汎用機構+真正性検証はアイディア帳「記憶の修正来歴」へ合流)。登録 UX = サイト URL を貼るだけ (フィード自動発見) に不変条件 5 を精密化。**実装着手 (2026-08-02、サブエージェント分業)**: バックエンド核 (feedparser 依存 / DB 3 テーブル / 取得・解析層 feed_fetch / FeedManager=定期取得+知覚配送+カーソル / 三層プリセットローダ / SAIVerseManager 配線) = **実装済み・テスト 30 件緑・ruff clean**。プリセット初期ラインナップ確定 (§10-8: ニュース=47NEWS+日経ビジネス [Yahoo 提供元別公式 RSS] / 技術=Zenn+Publickey+PC Watch / 科学=サイエンスポータル+JAXA+sorae。基準=信頼性、非公式ミラー不採用)。API ルート+管理・閲覧 UI = 実装中 (エージェント B)。**完了処理済み (2026-08-03)**: Codex レビュー 23 巡で線引き (まはー承認 — 以降は開発期 DB のみの仮想端と判断)、フルスイート 3695 緑、コミット aeacb8e。残 = まはーの実機検証 ([手順](../handoff/2026-08-03_rss_and_timetable_night_handoff.md) §2) | まはー (実機検証) | [rss_feed_intake.md](../intent/rss_feed_intake.md) | 2026-08-02 |
| 🟣 検証待ち | 知覚消費点の Beat 頭化 + フィード入室配送 | 本命の到着時読解は実機で確認済み (到着 Pulse の入力に記事と設置物見出し、発話が実在記事に言及)。残 = 定期サイクル配送との重複なしの継続観察と、作業セッション頭の知覚消費の実機確認 (作業コマの初走行に相乗り)。 | まはー (実機検証の続き) | [issue](../issues/feed_arrival_pulse_cannot_see_articles.md) / [handoff](../handoff/2026-08-08_beat_head_perception_handoff.md) | 2026-08-08 |
| 🟣 検証待ち | ローカル画像生成 (ComfyUI) のアドオン切り出し | 本体からの撤去とアドオン移設・GitHub 公開まで完了。前提修正 (Playbook ツール名解決 / source_file 付け替え) も同梱。次 = 実機検証 (ComfyUI 起動状態で Playbook 実行、起動ログに repointed source_file が出るはず)。 | まはー (実機検証) | [issue](../issues/comfyui_addon_extraction.md) / [アドオン README](https://github.com/maha0525/saiverse-comfyui-addon) | 2026-08-01 |
| 🔵 設計中 | LLM 使用量の記帳が経路ごとに欠落する | issue 起票済み。芯 = 記帳が「成功して戻ってきた場合」にだけ効く形で散らばっている (例外終了 / 空応答 retry / runtime を通らない直接呼び出し、の三経路で欠落)。帰属側の実害は塞ぎ済み。次 = 修正三案の方向のまはー裁定。 | まはー (裁定) | [llm_usage_accounting_gaps.md](../issues/llm_usage_accounting_gaps.md) | 2026-08-02 |
| 🟣 検証待ち | OpenRouter アプリランキングへの掲載 | プロバイダ設定の `default_headers` として実装済み・テスト緑。次 = OpenRouter 経由で会話を一度回し、openrouter.ai/apps の roleplay / general-chat に SAIVerse が現れるかの確認。ランキングから辿れる saiverse.net はデプロイ待ちで、それまで宣伝としては半分。 | まはー (実機検証) / 外部 (サイトデプロイ) | [intent](../intent/model_provider_management.md) §10 | 2026-08-04 |
| 🟣 検証待ち | 編纂が同名ページを増やす輪の根治 | 再開の前提だった (a) 導線廃止・(b) 統合見出し廃止も実装済みで、書き手側の工事は出揃った。次 = まはーが編纂の再開を判断し、夜間の編纂一回分を実機で確認する。 | まはー (再開判断と実機検証) | [issue](../issues/curation_duplicate_pages_loop.md) / [handoff](../handoff/2026-08-05_curation_loop_and_fragment_shift.md) | 2026-08-06 |
| 🟡 検証待ち | Memopedia 書き込みのロック漏れ (抽出器ほか) | 錠前を DB ファイルの持ち物にする工事まで完了。次 = まはーが実機で確認 (ログの `extraction-backlog` 行が整理のたびに出るか / 失敗を作って拾い直しが走るか、手順は issue の「実機で見ること」)。 | まはー (実機検証) | [issue](../issues/memopedia_writers_bypass_adapter_lock.md) / [handoff](../handoff/2026-08-06_sol_review_backlog_lock_findings.md) | 2026-08-06 |
| 🟣 検証待ち | ZIP インストールの Git 自動導入 | コード実装済み (setup.bat の自動 git 導入 winget→PortableGit fallback / update 経路の PATH 通し / README 更新)。次 = クリーン Windows (git 未導入) での実機テスト — 次バージョンリリース時にまはーと一緒に確認。 | まはー(次リリース時 実機テスト) | [git_required_for_zip_install](../issues/git_required_for_zip_install.md) | 2026-07-19 |
| 🟣 検証待ち | v0.3.0 の門 (リリース範囲と実施順序) | スルースの実機走行で「整理の実行中に起点が動いて窓が食い違い、退場が見送られる」欠陥を特定し、起点の凍結 (一回の整理は一つの一貫した窓) と保留理由の表示を修正済み・フルスイート緑。次 = まはーが「生成」を再実行し、一押しで採取から畳みまで通って送信量が下がることを確認する。 | まはー (実機再検証) | [v030_release_gate.md](v030_release_gate.md) / [issue](../issues/metabolism_deferral_mislabeled_as_window_claim.md) | 2026-08-25 |
| 🟣 検証待ち | 退場の境目とスペル群の原子性 | 検分で「守られていない」と確定 (境目の材料は pulse だけで、群の内側に別 pulse の行が割り込む形が本番に実在)。群の原子性を退場側・整列側の両方へ実装し、不変条件と例外を intent へ明文化済み。次 = 実機で通常の退場に退行が無いことの確認。 | まはー (実機検証) | [issue](../issues/eviction_boundary_spell_group_atomicity.md) / [arasuji_levels.md](../intent/arasuji_levels.md) §4-3/§7-7 | 2026-08-25 |
| 🟣 検証待ち | 会話の終わり方 (出口) と応答のやり直し | 束 1〜4 すべて実装済みで、フルスイート緑・フロントも型検査とビルドが通る。次 = まはーの実機検証 (手順はハンドオフに用意済み)。中断された発言と続きを一つの吹き出しに繋げるかの裁定も、そこで見て決める。 | まはー (実機検証) | [issue](../issues/user_utterance_path_failure_inventory.md) / [手順](../handoff/2026-08-26_conversation_exits_verification.md) | 2026-08-26 |
| 🟣 検証待ち | ID 文字種契約のペルソナ適用 (issue 論点 3) | ペルソナ ID (AIID) も manager/ids.py の契約に載り、日本語名は persona_連番、作成モーダルは ID 欄に初期値が入る形。配線テストは両経路 (作成・孵化) とも緑。次 = 実機検証 — 日本語名で新規作成し、ID 欄に空き連番が入るか、作成後の AIID と私室が同じ連番になるかを見る。 | まはー (実機検証) | [issue](../issues/building_id_no_charset_constraint.md) | 2026-08-16 |
| 🔵 設計中 | 刺激 (イベント・ユーザー発話) の永続 ID | 同一性を要る機構が 4 つ揃って此処で止まっている (冪等キー / 回収の重複判定の粒度 / 回収 activate の競合 / claim 失敗時の黙殺)。いまの歯止めは会話区間単位の近似で、立て続けの 2 回目の呼びかけを黙殺しうる。次 = ID の発行元 (供給源ごと / 受信側で一括) と冪等の窓をまはーが裁定する。 | まはー (設計裁定) | [issue](../issues/on_event_judgment_has_no_idempotency_key.md) | 2026-08-14 |
| 🟣 検証待ち | head 通知の既読基準 (last_notified) 握り潰し根治 | 撮り直し (TTL 切れ / Metabolism / 手動整理) が既読基準を上書きして入退室通知が消える欠陥は、「配送だけが基準を進める」形へ修正済み・回帰緑 (intent C8)。次 = バックエンド再起動後、まはーの入退室でエリスに [システム通知] が届くかを実機確認する。 | まはー (実機検証) | [cached_head_architecture.md](../intent/cached_head_architecture.md) §C8 | 2026-08-17 |
| 🟣 検証待ち | Elyth Remote MCP 移行 (Local MCP は 8/31 終了) | 実機検証は完了 (手順 8 は確かめる相手が動いておらず v0.4 送り、手順 11 は異常系のためスキップ裁定)。検証中に出た欠陥 4 件も修正済み。次 = その 4 件がまだレビューを通っていないので、Codex 4 巡目を回すかのまはーの裁定。回さないなら完了として台帳から外す。 | まはー (4 巡目の要否) | [handoff](../handoff/2026-08-10_elyth_remote_mcp_handoff.md) / [intent](../intent/mcp_addon_integration.md) §I | 2026-08-10 |

<!-- 構想止まり(当分動かない)は台帳外。intent draft で管理: observer/Fixture, Social Track 入口(Phase 5) など -->
