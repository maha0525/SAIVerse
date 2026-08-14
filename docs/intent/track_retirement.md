# Intent: Track の撤廃 — 最後の住人たちの引っ越し計画

**ステータス**: 実装中（v0.4、2026-08-14）。**裁定 3 点（A 関心の器 / B alert / C 門との線引き）すべて決着済み**。**裁定 B の ②③（閾値ポーラ・Handler tick 拡張点の撤去）は 2026-08-11 に実装完了**（§5-B の実装欄）。**撤去順序①の範囲は §7 で確定（裁定 5 点すべて 2026-08-14 に決着）— 実装フェーズ**。まはーの作戦変更「Track の撤廃計画を完全に立ててからでないと、エピソードの単位の議論がまともにできない」を受けて起草。
**親**: [`persona_cognition/recall_tags_and_track_reduction.md`](persona_cognition/recall_tags_and_track_reduction.md)（§3.2/§4.3 — 「役割縮小 → 溶解」の方向自体は 2026-07-24 に裁定済み）/ [`persona_cognition/life_concept_map.md`](persona_cognition/life_concept_map.md) §10（Track ＝複数概念の未分化な束）
**関連**: [`episode.md`](episode.md)（Wave 1「器と縁」の設計 — 本計画の完成を待って再開する）/ [`../overview/v030_release_gate.md`](../overview/v030_release_gate.md) §2-2

---

## 0. 芯（数行で）

Track は v1 で「ペルソナがやっていること」を全部入れる器として生まれた。その後の再設計で、仕事のほとんどは別の機構（時間割・出来事・判断点・アスペクト）へ移った。しかし概念としては残り続け、今も判断の語彙・記憶の刻印・UI の中心に居る。**残っている仕事を全部数え、それぞれ正しい持ち主へ渡し、Track という概念を消す。** これがこの計画のすべて。

## 1. なぜ今か

- 溶解の方向は 2026-07-24 に裁定済み。だが**実装はゼロ**で、誰のスケジュールにも乗っていなかった
- Wave 1（エピソードの単位）の設計議論が、一晩で三度 Track に足を取られた: ① origin_ref の多義の絡み ② 目的の縁の参照語彙（track:N を混ぜるか）③ 中断中セッション（Track にぶら下がる机メモ）と器の中断の重複
- 撤廃が終わらない限り、新しい設計はすべて「溶ける予定の概念」への参照を書き続け、その参照は**ユーザーのペルソナの永続データに堆積する**（門の篩いそのもの）

## 2. 住人台帳（2026-08-10 全数調査）

Track が現に担っている仕事の全数。「現状」は当日のコード確認に基づく事実、「行き先」は本計画の提案（裁定点は §5）。

### 概念の住人

| # | 住人 | 現状（確認済みの事実） | 行き先 |
|---|---|---|---|
| 1 | **関心（目的の木の大枝）** | 判断語彙の中心として現役。会話終了判断が「新しい関心として立てる」で Track を作る。時間割のコマは ref=track:N を指せる。会話で拾ったタスクは「どの関心にぶら下げるか」を Track から選ぶ（picked_tasks）。タスクの親は track_id 固定（タスク同士の親子は無い） | **裁定済み（§5-A）**: ペルソナ固有のコマの一覧（仮称: レパートリー）＋ Memopedia ページの経験の台帳 ＋ タスク（親をレパートリー項目参照へ張り替え） |
| 2 | **関係の器（永続 Track）** | 対ユーザー会話 Track と交流 Track（is_persistent）。「エアと話す」の常設枠 | 三分割: 意図＝「話す・聞く」型のレパートリー項目 / 知識＝人物ページの経験の台帳（§5-A）/ **記録＝メッセージへの記録からの導出**（episode.md v1.3 §3.3 — 会話のための行は作らない。「エアとの会話の歴史」は参加者にエアが記録されているメッセージ列の検索で得る。途中案の「対話エピソード」は行の中身が人物参照の複製に潰れるため削除）。会話の実行面（待ち・「いま会話中か」）は待ちタイマーへ移管 |
| 3 | **origin_track_id（またいで集める鍵）** | 発言・思考一行ごとに現役で刻印。44 ファイルに波及 | **origin_purpose（目的の直接参照）へ世代交代 — Wave 1 の「目的の縁」と同一の工事**。エピソード・メッセージに目的を直接刻む |
| 4 | **机メモ / 中断中セッション** | track_metadata に格納。「中断中セッション」の列挙は Track の机メモ走査 | **中断中エピソードの「しおり」へ**（2026-08-10 議論: 中断中セッションは器の中断の既存実装そのもの。住所を Track → エピソードへ移す） |
| 5 | **Track Chronicle（目的別あらすじ）** | 生成コード（arasuji/generator.py の track 分岐）と head 自動搭載（get_memory_weave_context）の呼び出しが現存 | エピソード Lv1 概要の導出へ世代交代（episode.md §6.2/6.3 で設計済み。読者への供給が途切れないことの確認後） |

### 実行機構の住人

| # | 住人 | 現状 | 行き先 |
|---|---|---|---|
| 6 | **running 排他（いま動くのは 1 本）** | **判断側は退役完了（2026-08-14、§7.4 実装欄）**: should_fire 削除・v1 メタ判断一式撤去・on_event の「いまの活動」を出来事読みへ付け替え済み。残るのは選択肢列挙（list_pickable_tracks 系 = §7.2 の④群、行き先レパートリー）と deferred track ops（ペルソナの track_* スペルが enqueue 源、④で語彙ごと入れ替え） | 出来事（いま）＋時間割（予定）は**完了**。選択肢は順序④でレパートリーへ |
| 7 | **alert 状態機械** | **撤去完了（2026-08-14、§7.4 実装欄）**。②③（閾値ポーラ・Handler tick）は 2026-08-11、①（会話ハンドラの発話仲裁）は順序①で on_event 判断点への直結に置き換え、set_alert + alert observer 機構ごと削除。STATUS_ALERT 定数と既存 DB 行は互換のため残置（書き手なし、掃除は⑦） | 完了（汎用機構化のみ将来課題 — 裁定 B ①注記） |
| 8 | **wait_response タイマー** | 出来事へ移管済み（タイマーは Track の状態をもう動かさない）。残骸のみ | 掃除のみ |

### 出口の住人

| # | 住人 | 現状 | 行き先 |
|---|---|---|---|
| 9 | **API・フロント UI** | tracks API ルート＋フロント約 20 ファイルが track に触れる（LifeView・RightSidebar・TasksModal 等） | 目的の木 UI ＋ エピソード表示へ貼り替え |
| 10 | **ActionTrack テーブル＋永続データ内の track:N 参照** | slot の ref・purpose_tags の指し先・episode の origin_ref に track:N が書かれ続けている | 参照語彙を task:N（目的ノード）一本へ。テーブル退役は最後（migration） |
| 11 | **ユーザー会話 20 件保持（user_conversation_preserver）** | オーナー会話の生メッセージを常時 20 件複製補完する v0.32 の特殊機構。**オーナー会話 Track の ID を鍵に動く**。2026-08-11 の棚卸しで発見（起草時の見落とし） | **機構ごと退役**（まはー承認 2026-08-11）。需要の引受先 = 会話開始時の読み戻し（arasuji_levels §15）＋会話チャンクの概要。詳細: episode.md ⚡ 到達点 19 |
| 12 | **ゲームセッションの参加帳簿（game_lifecycle）** | RPG セッション開始時に参加ペルソナごと game_session Track を running で作り、終了時に complete する（`saiverse/game_lifecycle.py`）。参加中かの正典は region.state（`is_participating`）で、Track は帳簿の影。2026-08-14 の順序①調査で発見（起草時の見落とし） | 出来事（ゲーム参加の出来事）へ、または region.state 一本で足りるなら単純撤去。実施は順序④（詳細設計はその時点） |

## 3. 使える既存の器（新造を最小にする）

- **episode_inheritance テーブル（実装済み・消費待ち）**: エピソード間の前駆 DAG。fact/digest の層・分岐点参照・由来（continue/branch/merge/import/replant）を持つ。操作は `saiverse/experience_inheritance.py` に集約済み。**Wave 1 の「続きの縁」はここに着地する — 新テーブル不要**
- **purpose_tags 棚（実装済み・書く側のみ稼働）**: 統一参照形式の帰属タグ。「目的の縁」の帰属側はここに乗る
- **episodes の層0（実装済み）**: 目的の直接刻印（origin_purpose）の置き場。現在コマ経由の参照が座標を挟んで一跳び切れている点を、開く瞬間の直接刻印で直す

## 4. 撤去順序（依存から導く）

1. **判断点・meta_layer の Track 状態依存の付け替え**（住人 6・7）— 最深の依存。ここが外れないと何も消せない
2. **目的参照の世代交代**（住人 3・10 の書き込み側）— 新規の刻印を origin_purpose（task:N）へ。Wave 1 の縁の実装と同一工事
3. **机メモの引っ越し**（住人 4）— 中断中エピソードのしおりへ
4. **関心と関係の器の移設**（住人 1・2）— レパートリー（ペルソナ固有のコマ一覧）の新設＋タスク親参照の張り替え＋経験の台帳への接続（裁定 A）
5. **Track Chronicle の世代交代**（住人 5）— エピソード Lv1 の供給が読者に届くことを確認してから
6. **UI・API の貼り替え**（住人 9）
7. **テーブル退役**（住人 10）— migration。既存 DB の track:N 参照の扱いもここで確定

## 5. 裁定点（まはー）

**A. 関心の器 — 裁定済み（まはー 2026-08-10）**

関心は一枚岩でなく、Track が三つの別物を固めた合金だった。単一の器を探さず、顔ごとに持ち主へ返す:

- **方向性 → ペルソナ固有のコマの一覧（仮称: レパートリー。名称は未確定）**。まはー案: 習慣テンプレートのコマ種別（「絵を描く」等の世界共通の一般形）を、ペルソナが自分向けに具体化した一覧——「絵を描く」に対する「Pixiv 投稿用の絵を描く」。朝の時間割設計は「自分はこういうコマを持っている。今日はこの中からこれを選ぶ」という流れになり、Track 時代の絞り込みの梯子（関心 5〜10 本から選ぶ）がそのまま残る。「エアと話す」のような**関係もこの一覧に乗る**（「話す・聞く」型の項目として）
- **経験の蓄積 → Memopedia ページの経験の台帳**（experience_ledger intent の設計どおり。定義だけのページに「この対象と歩んできた経緯＋最前線」の欄を持たせる）
- **実行の束 → タスク**。タスクの親の欄は Track 参照からレパートリー項目参照へ張り替える（「背景ラフを仕上げる」は「Pixiv 投稿用の絵を描く」の下）。**整理機能は捨てない**（起草時の A-3 は親の欄ごと廃止する案で、タスク一覧の整理を黙って失っていた — まはー指摘で修正）

補足裁定:
- 「新しい関心として立てる」（会話終了判断の受け皿）は**使用場面でなく誕生場面** — レパートリーという移行先が確定したので、「会話で心が動いたものを一覧に書き足す」形の誕生経路を実装フェーズで設計する
- コマの指し先（旧 ref=track:N）はレパートリー項目参照になる。task:N（具体タスク直指し）は従来どおり並存

残る詳細設計（実装フェーズ）: レパートリーの置き場（テーブル/一覧の形式）・習慣テンプレートとの参照関係・項目の粒度と上限・誕生/引退の経路。

**B. alert の行き先 — 裁定済み（まはー 2026-08-10）**
実態調査の結果、alert は実弾 1 種（user_utterance = 別行動中のユーザー発話 → メタ判断仲裁）・空砲 1 種（internal_alert = 閾値の書き手がコードに存在せず一度も発火不能）・空の拡張点 1 個（handler tick は定義ゼロ）だった。裁定:
- **① user_utterance → 判断点の起動信号へ直結**（alert 状態を経由しない。「開いている出来事 ≠ 会話」のときのユーザー発話イベントとして機械判定）。**注記: これは特殊処理であり、後々「別行動中に外から刺激が来た」の一般形（ユーザー発話はその一種）として汎用機構に設計し直す見込み**（まはー）。今はやらない
- **② 閾値ポーラ・③ tick 拡張点 → 機構ごと撤去**。将来の身体的欲求・知覚モニタリングは必要時に独立サブシステムとして設計（recall_tags intent §3.2 のまはー観察どおり）

**②③ の実装（2026-08-11、完了）**: `saiverse/internal_alert_poller.py` をファイルごと削除し、`SAIVerseManager` の構築（`InternalAlertPoller` インスタンス化）と `start()` の起動配線、`meta_layer` の言及を撤去。Handler の `tick()` は実装が一つも無く、拡張点の実体は poller 側の `getattr(handler, "tick")` 探索だけだったため、poller の削除でそのまま消えた。環境変数 `SAIVERSE_INTERNAL_ALERT_INTERVAL_SECONDS` も消滅（`docs/reference/environment-vars.md` には元から未記載だったため、リファレンス側の削除は無し）。撤去後、`TrackManager.set_alert` の呼び出し元は `UserConversationTrackHandler.on_user_utterance`（＝①）**の一箇所のみ** — ①は据え置きなので alert 状態機械そのものは現役で残る。設計文書側の追従: landscape §9 に死んだ概念として記録、`persona_action_tracks.md` §内部 alert ポーラ機構 / `persona_cognition/04_handlers.md` §Handler tick 機構 / `persona_cognitive_model.md` §内部 Alert / `pulse_dispatch.md` §8 の β・γ に撤去の断り書き。

**~~C. 門との線引き~~ — 撤回（まはー指摘 2026-08-10）**
起草時は「UI 貼り替え・テーブル退役は出荷後でも遡及の傷を増やさない」と工程の輪切りを提案したが、これは**門の篩い（下限の道具）を出荷可否の定義に誤用した**もの。Track の UI が残ったまま裏の機構だけ退去した製品は破綻している（UI はユーザーへの世界の契約）。**概念の撤廃に半分という状態は無い — 始めるなら v0.3.0 の中で UI・テーブル退役まで完遂する。** §4 の順序は依存順であって出荷の切れ目ではない。

## 6. この計画が Wave 1 に返すもの

撤廃計画が立つと、エピソードの設計は次の足場で再開できる（エピソード側の最終形は [episode.md](episode.md) v1.4 が正典 — 本節の起草時の語彙は同 intent のレビューで一部置き換わった）:

- 目的の参照語彙は**タスク（task:N）とレパートリー項目の二種**（裁定 A）。track:N は新規に書かない
- メッセージには参加者（人物）・所属エピソード・目的参照を書き込み時に記録する（想起用タグの書き込み時記録と同一 — episode.md §3.5）
- 「いま会話中か」は**待ちタイマーの生死**、予定は**時間割**、選択肢は**レパートリー＋タスク** — Track はどの問いの答えにも登場しない

## 7. 撤去順序①の詳細範囲（2026-08-13 全数調査、まはー裁定待ち）

§4 の先頭「判断点・meta_layer の Track 状態依存の付け替え」を実装可能な粒度へ詰めた調査。当日のコード確認に基づく事実と、付け替え地図の提案、裁定点 5 件から成る。

### 7.1 調査した事実

**(a) `MetaLayer.should_fire` は死体。** 本番の呼び出し元がゼロ（grep 全数: 定義本体・`test_cache_keepalive.py` の Fake（「呼ばれないこと」を検証する側）・文書のみ）。旧 TTL keepalive 経路が `run_cache_keepalive` に置き換わった際に呼び出しが消えた。読んでいたのは AUTONOMY_ENABLED / running Track / handler の wait_response。**付け替え不要、削除のみ。**

**(b) v1 メタ判断（meta_layer の状況分類）の生きている入口は 3 本のみ。**

1. `on_track_alert` ← `TrackManager.set_alert` ← `UserConversationTrackHandler` 熟慮経路（別 Track running 中のユーザー発話）＝住人 7 ①。set_alert の本番呼び出し元はこの一箇所（2026-08-13 再確認）
2. `on_periodic_tick` ← `autonomy_wiring.handle_wait_response_timeout` — user_conversation **以外**（social 等）の wait_response タイムアウト。対ペルソナ社交の v2 判断点が未設計のためのフォールバック
3. `on_periodic_tick` ← debug API（`api/routes/people/debug.py`、手動発火）

入口から先が読む Track 状態: `on_periodic_tick` 冒頭の wait_response 抑止（running Track + handler）、`_classify_situation`（LIVE_STATUSES 全行 → running / alert / pending_or_unstarted → 6 状況分類。Playbook dispatch・response_schema の enum・状況テキストの Track 一覧が全部ここから）。出口は `meta_judgment_finalize` が track_activate / pause / complete / abort / create スペルを内部実行 → deferred_track_ops。**v1 は「Track 状態を読み、Track 状態を書く」で閉じた円環**。

v2 判断点側にも running Track の読みが **1 箇所だけ**残っている（get_running 全数掃引 2026-08-14 で発見）: `build_on_event_situation_text` の「いまの活動」表示が、会話中でないとき running Track の題を「〜に取り組んでいます」として LLM へ渡す。これは①で開いている出来事（会話以外の kind）からの導出へ付け替える。get_running のその他の読み手は別順序の住人に帰属: wait_response タイマー再装填の判定（会話の実行面＝④）・Track Chronicle 挿入と head 搭載（住人 5＝⑤）・`get_building_messages` の origin_track_id 刻印（住人 3＝②）・`stop_autonomy` の帳簿揃え（④）・UI/API（住人 9＝⑥）・シム基盤 day_scenario（①の退役に追従）・game_lifecycle（住人 12＝④）。

**(c) deferred track ops の enqueue 源は 2 系統。** ① ペルソナ自身のスペル詠唱（track_create / activate / pause / complete / abort — track_* ツール 7 種すべて spell=True で公開中）、② `meta_judgment_finalize` の内部実行。apply 先は TrackManager の 4 遷移で、activate は on_track_activated hook（切替通知注入・会話出来事 open・main_line Pulse 起動）を引き連れる。機構自体は書き込み側で、読む状態は無い。**v1 を消しても①が残る限り deferred ops 機構は消せない。**

**(d) `list_pickable_tracks`（LIVE_STATUSES）の消費者は 6 箇所。** ① コマ ref / picked_tasks の enum（`collect_pickable_track_refs`）② 層2 棚入れ enum（`collect_purpose_refs`）③ コマ締めの帰属先 enum（`collect_slot_ref_enum`）④ head の PurposeBacklogSection（「進行中のこととやりたいこと」一覧）⑤ slot_close の帰属先提示行 ⑥ experience_ledger API（経験の台帳の索引 kind:"track"）。ほかに LIVE_STATUSES 直読みが 4 箇所: `find_interrupted_session`（机メモ＝住人 4、順序③）、`sanitize_timetable`（track:N コマ検証）、`timetable_template._forced_ref_problem`（テンプレ ref 検証）、`purpose_tree.list_first_tier`（目的の木の第一階層 API）。

**(e) life_purpose_unset の受け皿は v1 にしか無い。** `judgment_points.py` に life_purpose の扱いはゼロ。v1 の状況分類（alert が無く LIFE_PURPOSE 未設定なら最優先）だけが生きる目的の初期設定を発火させる。入口が上記 3 本に痩せている現状でも既に到達機会は稀で、v1 退役で完全にゼロになる。

### 7.2 付け替え地図（提案）

| 依存 | 読んでいる Track の顔 | 行き先 | 実施フェーズ |
|---|---|---|---|
| should_fire | running + wait_response | （死体）削除 | ① |
| on_periodic_tick の wait_response 抑止 | 相手の応答待ち中か | 出来事（開いている会話/社交の出来事）— v1 退役なら抑止ごと消滅 | ① |
| _classify_situation の running / idle | いま何かしているか | 出来事（開いている出来事）＋時間割（現在コマ）— v1 退役なら分類ごと消滅 | ① |
| _classify_situation の alert | 割り込みが来たか | 判断点の起動信号（裁定 B ①の直結） | 裁定点 2 |
| build_on_event_situation_text の「いまの活動」 | running Track の題 | 開いている出来事（会話以外の kind）の題 | ① |
| life_purpose_unset 分類 | — | day_open 判断点の前段 等 | 裁定点 3 |
| finalize の track スペル | 状態機械の書き込み | v1 退役と同時に消滅 | ① |
| ペルソナの track_* スペル語彙 | 同上 | レパートリー操作スペル（誕生・引退、裁定 A 補足） | 裁定点 4 |
| deferred ops 機構 | — | 最後の enqueue 源が消えるとき撤去 | 裁定点 4 に従属 |
| enum 3 種（コマ / 棚 / 締め） | 選べる関心（track:N） | レパートリー項目参照 ＋ task:N | ④ |
| head 一覧 / slot_close 提示行 | 同上 | 同上（enum と集合一致の不変条件ごと移す） | ④ |
| sanitize / _forced_ref_problem | track:N の生存検証 | レパートリー項目の生存検証 | ④ |
| purpose_tree 第一階層 | 木の大枝 | レパートリー項目 ＋ 親なし採用タスク | ④ |
| experience_ledger 索引 | kind:"track" 行 | レパートリー項目 | ④ |

**要旨**: 順序①の判断側依存は v1 メタ判断の中にほぼ閉じているため、「Track 状態の読み替え」ではなく「**v1 メタ判断の残り 3 入口を処理して v1 ごと退役させる**」のが最短の形。一方、選択肢・一覧・検証の系統（表の④群）は行き先がレパートリー＝順序④で新設される器のため、**順序①では付け替え先が存在しない**。起草時の §2 住人 6 は両系統を一括りにしていたが、実施フェーズは分かれる。

### 7.3 裁定点 — **全 5 点裁定済み（まはー 2026-08-14）**

1. **順序①の実体の読み替え** → **承認**。「v1 メタ判断（meta_layer の状況分類）の退役」として工事する。付け替え（running → 出来事 等）を v1 の中で行う必要は無く、v1 自体が消える。
2. **入口 1（ユーザー発話衝突の alert）** → **直結化を①に含める（承認）**。裁定 B ①の「今はやらない」は汎用機構化のみを指す。特殊処理のままの直結化で alert 状態機械を撤去する。
3. **入口 2（social wait_response timeout）と life_purpose_unset の受け皿** → **どちらも受け皿なしで撤去**。対ペルソナ会話は**未実装**のため考慮不要（実装時は会話終了判断点を流用する方針のみ記録）。生きる目的は**発火経路無しで進める** — スキーマ自体が変わる可能性が高く、受け皿候補だった起床判断（day_open）そのものが撤去される想定のため、形は別途設計する（まはー）。
4. **track_* スペル語彙の引き上げ時期** → **④まで残す（承認）**。①で v1 を消してもペルソナのスペルが残るため、deferred ops 機構の撤去も④（レパートリー語彙との入れ替え時）。
5. **④群の工程移動** → **承認**。enum・head 一覧・sanitize・purpose_tree・experience_ledger は順序④（レパートリー新設）と同一工事へ。

### 7.4 順序①の実装範囲（確定）

**退役するもの**: should_fire（死体）／ v1 メタ判断一式（`_classify_situation`・`_SITUATION_PLAYBOOK_MAP`・状況テキスト/スキーマ組み立て・`on_track_alert`・`on_periodic_tick`・meta_judgment_* Playbook 5 種・`meta_judgment_finalize` ツール）／ alert 状態機械（`set_alert`・alert observer 機構。STATUS_ALERT 定数は既存 DB 行の互換のため残し、書き手を無くす）／ debug API の v1 発火経路／ social timeout の v1 フォールバック（WARNING ログ化）。meta_layer に残すもの: `_load_judgment_config`（autonomy_manager / session_lifecycle が使用）・per-persona Lock（判断点の直列化で共用）・`_record_judgment_log`（v2 判断 Pulse の flush 経路）。

**直結化の形（裁定 B ①の特殊処理版）**: `UserConversationTrackHandler` の熟慮経路（衝突あり）を `set_alert → MetaLayer` から **on_event 判断点の流用**へ貼り替える。機械判定は「開いている出来事 ≠ 会話」（`is_in_user_conversation` と開いている出来事の kind で判定 — Track の running 衝突ではなく）。判断が engage_now を選んだら従来どおり `track_manager.activate`（on_track_activated hook 経由で会話出来事が開き main_line が起動 — Track の器は④まで現役）。選ばなければ応答しない（旧 alert 経路と同じ挙動）。自律 OFF のペルソナは判断を経ず直接 activate（常に応答 — `handle_external_event` と同じ流儀）。

**付け替え**: `build_on_event_situation_text` の「いまの活動」を running Track から開いている出来事（会話以外の kind）の題へ。

**追従**: シム基盤（day_scenario）の v1 互換スタブ、テスト（test_meta_layer の v1 部分・test_cache_keepalive の Fake・test_autonomy_wiring の social フォールバック）、ドキュメント（landscape §9・persona_cognition 各所・本 intent §2 住人 6/7）、tool catalog / api-endpoints の再生成。meta_judgment_* の DB playbooks 行は dispatch が消えるため無害な残骸 — 掃除は⑦の migration でテーブルごと。

**実装（2026-08-14、完了 — まはー実機検証待ち）**: 上記の確定範囲どおり実装した。撤去 = `MetaLayer` を共有基盤（`_get_lock` / `_load_judgment_config` / `_record_judgment_log`）だけに刻み直し（`saiverse/meta_layer.py` 全面書き直し）、`TrackManager.set_alert`＋alert observer 機構削除、`meta_judgment*.json` 6 枚（NL 素体含む）と `meta_judgment_finalize.py` をファイル削除、debug API 2 本（fire-meta-judgment = 廃止 no-op / wrap-up-conversation = `handle_wait_response_timeout` 即時発火へ）、social timeout の else 枝 = WARNING 化。直結化 = `autonomy_wiring.handle_user_utterance_conflict` 新設（on_event 判断点流用、engage_now → activate、判断起動不能時は activate に倒す = 呼びかけを機構の不備で黙殺しない、indeterminate は二重応対回避で応答しない）、handler の衝突判定を running-Track 衝突から「開いている出来事 ≠ 会話」へ変更（案 Y の残留 running 誤検知も同時に解消）。付け替え = `build_on_event_situation_text` の「いまの活動」を開いている出来事（meta.title または kind 表示名）から導出。付随発見: 旧 v1 の is_participating ゲート（ゲーム参加中の定期判断抑止）は退役で完全消滅 — v2 判断点は元からこのゲートを通っておらず、ゲーム参加と自律駆動の相互作用は住人 12 と合わせて④で設計（`game_lifecycle.py` の NOTE に記録）。テスト: v1 専用 4 ファイル削除・`test_meta_layer` は共有基盤のみに書き直し・`test_stop_autonomy` 切り出し・handler/track_manager/judgment_points/autonomy_wiring の該当テストを新経路へ書き替え（alert 行互換の activate テスト追加）。

**レビュー消し込み（2026-08-14、Codex 1 巡の 6 件）**: F1（busy 判定が non-running 分岐でしか走らない）は**まはー裁定で却下 = 仕様どおり**（「別行動中でも会話を優先」の方針が直前に確定しており、残留 running 経由の即応答はそれに合致。仲裁経路そのものの存在意義は順序④で問い直す）。残り 5 件を消し込み:

- **F2**: 「別の活動中か」を `episodes.get_open_non_conversation_episode`（会話を除いた open の直接クエリ）へ。「最後に開いた 1 件」を見る読み方だと、会話が作業より後に開いた並びで仲裁が消えていた。判定（handler）と提示（`build_on_event_situation_text`）は同じ集合から引く。
- **F5**: debug の切り上げ発火に、開いている会話の出来事の同期検証を追加（無ければ 409）。案 Y の残留 running を「会話中」と読んで撃つと、存在しない会話の振り返りがペルソナ名義の記憶に残る。social Track は success を返さない（本番経路でも判断は撃たれない）。
- **F6**: 退役した操作面を UI から削除（DebugPanel の「メタ判断を 1 回」ボタンと force、SettingsModal の休眠設定 3 欄）。あわせて no-op で残していた debug エンドポイント 3 本も削除した（呼び手が消えたため）。
- **F3 / F4**: いずれも順序①より前から在る **on_event 系の共通欠陥**なので、経緯は [judgment_seat_contention_and_event_loss.md](../issues/judgment_seat_contention_and_event_loss.md) ④⑤へ記録した（F3 = 判断が走った後の失敗を「起動できなかった」と読んで決定を上書きしていた／F4 = 回収の応対が種別を落としてユーザー発話を外部イベント形で流し込んでいた）。
