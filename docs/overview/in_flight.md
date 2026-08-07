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
| 🟣 検証待ち | Chronicle 退場の適用側拒否権デッドロック根治 | 二つの顔 (編纂対象ゼロ fold の吸収限定退場 / あらすじ手動削除の道連れ) とも実装済み・回帰緑。次 = 実機で通常 Metabolism の退行がないことの確認。 | まはー (実機検証) | [issue](../issues/chronicle_eviction_applier_veto_deadlock.md) / [chronicle_eviction.md](../intent/chronicle_eviction.md) §2/§5-5/§6 | 2026-07-27 |
| 🟣 検証待ち | あらすじのレベル制 (記憶の川の一本化) | 実装完了。エリスは実機修復と初編纂まで成功、air は点検の結果修復不要。次 = aifi の再編纂 (未編纂期間の消化、汎用ツール整備済み) と LLM 束ね品質の本番初発火の観察。提示側の簡素化は presentation_gap 実機検証後へ先送り (intent §12-7)。 | まはー (aifi 実施のタイミング) | [intent](../intent/arasuji_levels.md) | 2026-07-29 |
| 🟣 検証待ち | 編纂入口の一本化 (arasuji_levels §13) | §13 (入口一本化) と §14 (冷えた anchor の保守経路) とも実装済み・レビュー消し込み完了・全テスト緑。派生の新 issue (関所閉鎖の slot 消費) は裁定待ち・別件。次 = 実機検証 (会話開始で「1件」ダイアログが出ない / 整理ボタンが直近を残して畳む)。 | まはー (実機検証) | [intent](../intent/arasuji_levels.md) §13-§14 | 2026-07-29 |
| 🟣 検証待ち | Chronicle タブが一覧の一部を返さない (件数上限) | 一覧 API は件数上限を持たず常に全件を返す形。次 = 実機でエリスの Chronicle が全 513 件並び、切り詰めバナーが消えていることの確認。 | まはー (実機検証) | [issue](../issues/arasuji_modal_500_limit_truncation.md) | 2026-08-05 |
| 🟣 検証待ち | 🔴 Chronicle 提示が途中の期間を黙って落とす | 走査と圧縮を分離する直しを実装済み — 全ペルソナで提示から落ちる一次あらすじゼロ・予算内を確認、回帰固定済み。孤児あらすじは編纂側の管轄で本件の範囲外。次 = 実機で head に直近の記憶が戻っていることの確認。 | まはー(実機で head に直近が戻っているか確認) | [chronicle_presentation_gap.md](../issues/chronicle_presentation_gap.md) | 2026-07-25 |
| 🔵 設計中 | 想起用タグ + Track の役割縮小 | intent 起草済みで議論継続中。想起の二層構造 (再開の想起=必須 / よぎる=努力目標)・タグの縄張り原則・粒度 (チャンク単位の関与列挙、ページ ID 結合) まで決着。次 = 「経験の台帳」の形の設計 (履歴の圧縮粒度・最前線の表現・ページ表示との統合)。 | まはー(台帳設計の議論継続) | [recall_tags_and_track_reduction.md](../intent/persona_cognition/recall_tags_and_track_reduction.md) | 2026-08-02 |
| 🔵 設計中 | モデル定義の外部配布とカタログ | 段階1〜2 (同梱モデルの provider_ref 移行・接続情報の排除) は実装・検証済み。副産物で Gemini 無料枠キー・ollama 設定不達・LM Studio キー必須の欠陥3件も根治。次 = 未決3点 (配布元リポジトリ / カタログスキーマ / 取得時差分提示) のまはーレビューから段階3へ。 | まはー(段階3以降のレビュー・未決3点) | [model_catalog_distribution.md](../intent/model_catalog_distribution.md) | 2026-07-23 |
| 🟣 検証待ち | 編纂の対称化 = 読み戻し (arasuji_levels §15) | 実装完了・レビュー収束・フルスイート緑。読み戻しは印の切替だけで往復とも LLM ゼロ、プレビューにも反映済み。次 = 実機検証 (送信前プレビューに厚い生ログ / 開き直しとプレビューの一致 / head との二重表示なし / 超過時の編纂ゼロ畳み)。 | まはー (実機検証) | [issue](../issues/metabolism_refill_when_below_target.md) / [intent](../intent/arasuji_levels.md) §15 | 2026-07-30 |
| 🟣 検証待ち | チャットオプション「データ送信量の管理」再設計 | 実装完了・レビュー収束。新陳代謝は常時 ON 化・水位はモデル定義一本・当該セクションは読み取り専用の状態表示へ。次 = 実機検証 (水位バーとプレビューの整合 / トグル消滅後の通常動作 / モデル編集→表示反映)。 | まはー (実機検証) | [issue](../issues/chat_options_metabolism_section_redesign.md) | 2026-07-30 |
| 🟣 検証待ち | 判断プロンプトの静的一覧を head へ | 移設実装済み・レビュー5巡の消し込み完了。競合制御は「判断点の席」の専用行へ切り出し済み。次 = 実機観察 (head に 2 節が出る / 判断プロンプトが痩せた / 一覧変更の通知が次の Pulse に届く / prefix キャッシュが節目まで持つ)。 | まはー (実機検証) | [issue](../issues/judgment_static_lists_to_head.md) | 2026-07-30 |
| 🟣 検証待ち | 判断点の席の競合制御とイベントの取りこぼし | 裁定 B (保持+再試行窓) で実装・コミット済み。範囲外は event_delivery_reachability_gaps へ切り出し済み。次 = 実機検証 (時間割の統合検証手順に相乗り — 判断の二重発火なし・waiting 系 ERROR の静穏を観察)。 | まはー (実機検証) | [issue](../issues/judgment_seat_contention_and_event_loss.md) | 2026-08-07 |
| 🟠 実装中 | Godot / ARDY 仮想身体デモ | 身体移動・gesture 生成・プレイヤー身体・視線正規化・一人称撮像・表情 (Expression Studio + preset 遷移) まで隔離 E2E 済み。次 = player locomotion の idle↔walk/run、自動瞬き・視線追従、実 HMD、発話同期 /emote、Idle scheduler、接地確認。本番ペルソナを用いる確認は操作ごとの明示承認がある場合のみ。 | 私(プレイヤー歩行・瞬き・視線・実VRM/HMD・`/emote`接続・Idle scheduler) / まはー(表情preset作成・実HMD確認) | [virtual_embodiment_godot.md](../intent/virtual_embodiment_godot.md) | 2026-07-20 |
| 🟣 検証待ち | 監査第二陣・共通境界hardening | Discord を除く第二陣を実装済み・回帰/整合チェック通過。残る作業は外部側 = 公式 Addon registry の署名鍵生成・署名済み envelope publish・公開鍵配布。非 Git updater は署名済み release manifest 設計まで fail-closed。 | 外部(公式署名鍵/publish) / まはー(実機導線確認) | [第二陣Intent](../intent/audit_second_batch_hardening.md) / [レビュー台帳](code_review_ledger.md) | 2026-07-16 |
| 🟠 実装中 | コードレビュー運用・残存finding修正 | 一次監査完了、残存 finding は 8 本の柱に整理し工程管理は完了計画書 (W1〜W14) に一本化 — セッション開始は計画書の「現在地」から。W1〜W8 実装済み、W4 差し戻し分 (Chronicle 退場・レベル制) は専用行が真実を持つ。次 = W1〜W8 の実機検証 (退場経路と独立に並行消化可)・W9 (柱7 完全手動モード) 着手・A3 (host/City 時差) の着手裁定。 | 私(次wave=W9 柱7 完全手動モードの着手) / まはー(W1〜W8実機検証+A3(host/City時差)の着手裁定+データ掃除の承認) | [完了計画書](audit_remediation_plan.md) / [レビュー台帳](code_review_ledger.md) / [W8走行メモ](../handoff/2026-07-22_w8_time_order_handoff.md) | 2026-08-02 |
| 🟠 実装中 | Beat/ExecutionContext (SEA実行基盤の一本化) | §6-1〜§6-7 (ExecutionContext 導入 / Beat ロック+関所 / anchor・head のフルキー化 / 内容型通知 / Metabolism 二層分離 / thread push-pop / 正典改訂) まで実装・検収済み。残 = §6-6b Beat ロックのトークン化の分離裁定 → 全 wave 完了後に gen_reference_docs 一括再実行。 | 私(実装続行) | [beat_execution_context.md](../intent/beat_execution_context.md) | 2026-07-17 |
| 🟠 実装中 | 実行台帳 (execution ledger) | 段階移行 Phase 0〜5 (基盤+Beat ロック / 判断点 / 時間割・予算 / schedule / Metabolism / 配送・移動) が全段実装済み。大原則 = LLM 自動再実行禁止。次 = まはーの実機検証。 | まはー(実機検証) | [execution_ledger.md](../intent/execution_ledger.md) | 2026-07-21 |
| 🟣 検証待ち | ⑥ 概念再編（九龍城の解体） | Memory Atlas P1〜P4 と参照文法の統合・写真→クリップ改名まで実装済み、レビュー指摘も修正済み。次 = 実機検証 (就寝判断の棚の乱れ / テーマの芽・朝の報告・エアの目次 / 改名後初回起動の机移行ログ) → ①実機テスト。 | まはー(実機検証) | [concept_consolidation.md](../intent/concept_consolidation.md) / [life_concept_map.md](../intent/persona_cognition/life_concept_map.md) | 2026-07-12 |
| 🟣 検証待ち | 自律行動v2 活性化配線 | 実機初日を走行済み — 起床判断・時間割・作業セッションまで動作、発見したバグは修正済み。次 = 再起動して過ぎたコマの即時発火テスト → 夕方コマ → 就寝裁定 → 編纂初陣。§3-1 (purpose_seed が分身モードで撃てない) はまはー裁定待ち。 | まはー(再起動+観察 / §3-1 裁定) | [autonomous_behavior_v2.md](../intent/autonomous_behavior_v2.md) | 2026-07-12 |
| 🔵 設計中 | 自律行動v2 実機初日の前提レベル設計課題 (棚卸し) | 束A (単位の世代交代)・束B (last mile)・束C (Track 意味論) を intent 二本 (life.md / episode.md) へ束ねて設計、life.md は4フェーズ+改修A/B まで実装済み・実機で出た破綻や不具合も消し込み済み。次 = 実機再検証 → 暮らし Pulse のプロンプト設計 (私→まはーレビュー) → episode.md 実装 → B4。 | まはー(実機再検証) / 私(暮らしPulse設計案) | [life.md](../intent/life.md) / [episode.md](../intent/episode.md) / [gaps doc](../issues/autonomous_v2_post_live_gaps.md) | 2026-07-14 |
| 🟣 検証待ち | track:N コマの空 Track 無音縮退 修正 | 縮退条件に note を含める修正を実装済み・回帰固定。次 = 実機再検証 (note だけの track コマが縮退せず回るか)。 | まはー(実機再検証) | [track_slot_empty_degradation](../issues/track_slot_empty_degradation.md) | 2026-07-19 |
| 🟠 実装中 | 体験の構造 (記憶系の統一 intent) | 主要裁定は全て完了、実装順 §12 の工程 (1)〜(3) (digest 統合 / Chronicle の episode 整列化 / 継承エッジの器) まで実装済み。次 = 工程 1〜3 の実機検証 (まはー) と工程 (4) 知覚レンダリング=W14 (私)。消費者配線 (継承チェーン閉じ生成・分岐再生成 UI・メティス取り込み) は後続。 | まはー(工程1・2・3実機検証) / 私(工程4=W14) | [experience_structure.md](../intent/experience_structure.md) | 2026-07-22 |
| 🔵 設計中 | Ultramemory (想起の第2層) | intent 起草済み・まはー GO 済み。想起用タグが先行し本件はそれを当然に利用する関係で確定。次 = まはーレビューで論点 (課金同意面 / 第0層との排他か併用か / 蘇生範囲) を詰めて設計確定。 | まはー(レビュー) | [ultramemory.md](../intent/ultramemory.md) | 2026-07-25 |
| 🟠 実装中 | コア記憶の訂正導線 + ごみ箱 (短期対応) | DB 層・API 層・メモリタブ UI まで実装済み (削除は実機確認済み)。仮想センサー側は知覚バッファの一利用者として恒久対応・実機検証済み。残 = 未確認バッジの置き場所の確定 (ペルソナ固有アフォーダンス、住民/神モード UI と交差)。 | まはー(バッジ置き場) → 私 | [memory_architecture_v2.md](../intent/memory_architecture_v2.md) §5.1 | 2026-07-09 |
| 🔵 設計中 | resolve_uri 切り詰めの継続読み (memopedia tree 等) | 上限は resolve_many 共通経路。論点4つ (続きの単位=サブツリー URI 推奨 / 切り詰め案内の決定論埋め込み=必須 / depth 密度制御 / 適用範囲=まず tree 型) を提示済み。次 = まはーと仕様詰め → 実装。 | まはー(仕様詰め) | [issue](../issues/resolve_uri_truncation_continuation.md) | 2026-07-12 |
| 🟡 実装待ち | メティス記憶ブリッジ | 設計は主要点まで確定 (配置=休眠ペルソナ / 1セッション=1thread / Chronicle thread 分離 / 独り言・thinking 既定OFF / 段階的取り込み・冪等)。残る裁定は persona_id と配置先 City のみ。次 = まはーの裁定 → ブートストラップ取り込みから実装。 | まはー(persona_id/City)→私(実装) | [metis_memory_bridge.md](../intent/metis_memory_bridge.md) | 2026-07-13 |
| 🟠 実装中 | SAIVerse Lite (スマホ単体アプリ) | v1 骨格の磨き込み完了 (UI 本体化 / 公式エクスポート対応 / 初回ウィザード / 法務 / API キーガイド / キャッシュ opt-in)、テスト・build 全緑。次 = 未 push コミットの push+再デプロイ → スマホ実機通し・実 API キャッシュ実測・引っ越し E2E (ブロッカー=本体側ペルソナ一括受け口)。 | まはー(push+再デプロイ+実機) | [saiverse_lite.md](../intent/saiverse_lite.md) | 2026-07-16 |
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
| 🔵 設計中 | 経験の台帳と経験値ノート (再開の想起の本体) | **intent v0.1 起草 (2026-08-02)**。骨子 = 読みの三段構え (台帳=索引タイトル+概要+経験統計 / 経験値ノート / 生ログ) / **経験値ノート** (まはー命名: ペルソナが自分の経験を自分の言葉で自分の知識に書き込み、後の自分のために残して成長する経路。コマ締めに帰属判定と同一コール・書き先はテーマ/対象ページ fragment・遡及は非現実的コストでオプション候補止まり) / 台帳ページの中身は動的合成 (子ページ新造せず。エア実データで試作、統計列が定義の切り株を可視化) / テーマページの供給=コマ種別カタログ対応 / **抽出の二家系原則** (本人の声=体験の節目・機構の編集=代謝、家系内は同一コール相乗り、発火点を増やさない) / gold_panning は混血 (代謝×本人の声、まはー発見) → 経験値ノート実機実証後に節目へ吸収・退役の順序付き移行。**v0.1 レビュー 1 巡目で裁定 4 点 (同日)**: テーマページは初経験の締めに lazy creation (初期一括生成は記憶の汚染 — 経験していないものを意識に置かない) / ノートは自由文 / コア記憶の書き込みは就寝ふりかえりへ一本化 (gold_panning の吸収先も就寝側に確定) / UI はメモリーモーダル内で新タブ vs コア記憶タブ拡張の両案 (実装時に画面で確定)。episode 細切れ (1 往復 30 分 = 1 エピソード) は独立課題としてアイディア帳へ。残未決 = 索引の圧縮 (後でよい) / 会話の節目のノート / テーマ⇔コマ対応規則。次 = 実装順の判断 (時間割・RSS・台帳の三 intent が揃った) | まはー (実装順の判断) | [experience_ledger.md](../intent/experience_ledger.md) | 2026-08-02 |
| 🟠 実装中 | RSS フィード施設 (偶然の供給側) | **intent たたき台 v0.1 起草 (2026-08-01)**。timetable_redesign と対 (あちらが受信側=時間割、こちらが供給側=世界の情報源)。骨子 = RSS/Atom 取得を Building 単位の購読で施設化 / プリセットを builtin 三層優先で配布し City 作成時に選択 / 提示は発言機会+知覚バッファ投入の二経路 / 提示層は転載のみで生成しない・「今日の数本」の選択は決定論 / 取得失敗は正直に (空の新聞は空と示す) / 内部フィード (図書館・美術館のアイテム格納公開、City 掲示板構想の実現経路) も同じ仕組み。単体でも価値 (現行時間割のまま全ペルソナにエリスの Elyth 相当を配れる)。レビュー 1 巡目で裁定 6 点 (2026-08-02): 購読は Fixture 持ち / 今日の数本 = 新着のみ / ユーザーも読める UI を出す (透明性確保は SAIVerse の仕事) / 内部フィードは後 / 既読 = フィードごとカーソル方式 / 記憶への残り方 = 専用機構なし (増幅段は timetable 封印済み・単発誤読は受容・出典必須化は却下 → 来歴汎用機構+真正性検証はアイディア帳「記憶の修正来歴」へ合流)。登録 UX = サイト URL を貼るだけ (フィード自動発見) に不変条件 5 を精密化。**実装着手 (2026-08-02、サブエージェント分業)**: バックエンド核 (feedparser 依存 / DB 3 テーブル / 取得・解析層 feed_fetch / FeedManager=定期取得+知覚配送+カーソル / 三層プリセットローダ / SAIVerseManager 配線) = **実装済み・テスト 30 件緑・ruff clean**。プリセット初期ラインナップ確定 (§10-8: ニュース=47NEWS+日経ビジネス [Yahoo 提供元別公式 RSS] / 技術=Zenn+Publickey+PC Watch / 科学=サイエンスポータル+JAXA+sorae。基準=信頼性、非公式ミラー不採用)。API ルート+管理・閲覧 UI = 実装中 (エージェント B)。**完了処理済み (2026-08-03)**: Codex レビュー 23 巡で線引き (まはー承認 — 以降は開発期 DB のみの仮想端と判断)、フルスイート 3695 緑、コミット aeacb8e。残 = まはーの実機検証 ([手順](../handoff/2026-08-03_rss_and_timetable_night_handoff.md) §2) | まはー (実機検証) | [rss_feed_intake.md](../intent/rss_feed_intake.md) | 2026-08-02 |
| 🟣 検証待ち | 時間割の抜本改修 (習慣×偶然) | メイン系ブランチへ統合済み。残指摘 (営業日解決器の統合 / ライフ読取失敗の三状態化 / ライフ台帳・表示の解決器統一) も実装済み・回帰固定済み。次 = 実機検証 (judgment_day_open は起動時の自動同期で新版が DB に載っていることを確認済み。手順は統合検証手順に一本化、周辺案件の相乗り込み)。 | まはー (実機検証) | [timetable_redesign.md](../intent/timetable_redesign.md) / [統合検証手順](../handoff/2026-08-07_timetable_live_verification_run.md) | 2026-08-07 |
| 🟣 検証待ち | ローカル画像生成 (ComfyUI) のアドオン切り出し | 本体からの撤去とアドオン移設・GitHub 公開まで完了。前提修正 (Playbook ツール名解決 / source_file 付け替え) も同梱。次 = 実機検証 (ComfyUI 起動状態で Playbook 実行、起動ログに repointed source_file が出るはず)。 | まはー (実機検証) | [issue](../issues/comfyui_addon_extraction.md) / [アドオン README](https://github.com/maha0525/saiverse-comfyui-addon) | 2026-08-01 |
| 🔵 設計中 | LLM 使用量の記帳が経路ごとに欠落する | issue 起票済み。芯 = 記帳が「成功して戻ってきた場合」にだけ効く形で散らばっている (例外終了 / 空応答 retry / runtime を通らない直接呼び出し、の三経路で欠落)。帰属側の実害は塞ぎ済み。次 = 修正三案の方向のまはー裁定。 | まはー (裁定) | [llm_usage_accounting_gaps.md](../issues/llm_usage_accounting_gaps.md) | 2026-08-02 |
| 🟣 検証待ち | OpenRouter アプリランキングへの掲載 | プロバイダ設定の `default_headers` として実装済み・テスト緑。次 = OpenRouter 経由で会話を一度回し、openrouter.ai/apps の roleplay / general-chat に SAIVerse が現れるかの確認。ランキングから辿れる saiverse.net はデプロイ待ちで、それまで宣伝としては半分。 | まはー (実機検証) / 外部 (サイトデプロイ) | [intent](../intent/model_provider_management.md) §10 | 2026-08-04 |
| 🟣 検証待ち | 編纂が同名ページを増やす輪の根治 | 再開の前提だった (a) 導線廃止・(b) 統合見出し廃止も実装済みで、書き手側の工事は出揃った。次 = まはーが編纂の再開を判断し、夜間の編纂一回分を実機で確認する。 | まはー (再開判断と実機検証) | [issue](../issues/curation_duplicate_pages_loop.md) / [handoff](../handoff/2026-08-05_curation_loop_and_fragment_shift.md) | 2026-08-06 |
| 🟡 検証待ち | Memopedia 書き込みのロック漏れ (抽出器ほか) | 錠前を DB ファイルの持ち物にする工事まで完了。次 = まはーが実機で確認 (ログの `extraction-backlog` 行が整理のたびに出るか / 失敗を作って拾い直しが走るか、手順は issue の「実機で見ること」)。 | まはー (実機検証) | [issue](../issues/memopedia_writers_bypass_adapter_lock.md) / [handoff](../handoff/2026-08-06_sol_review_backlog_lock_findings.md) | 2026-08-06 |
| 🟣 検証待ち | ZIP インストールの Git 自動導入 | コード実装済み (setup.bat の自動 git 導入 winget→PortableGit fallback / update 経路の PATH 通し / README 更新)。次 = クリーン Windows (git 未導入) での実機テスト — 次バージョンリリース時にまはーと一緒に確認。 | まはー(次リリース時 実機テスト) | [git_required_for_zip_install](../issues/git_required_for_zip_install.md) | 2026-07-19 |

<!-- 構想止まり(当分動かない)は台帳外。intent draft で管理: observer/Fixture, Track解体(目的の木), Social Track 入口(Phase 5) など -->
