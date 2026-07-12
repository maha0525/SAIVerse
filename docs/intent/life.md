# Intent: ライフ — 活動区間の宣言と時間の階層

**ステータス**: 実装中 (v0.4, 2026-07-13)。**案 Y Phase 1（§7 の手術）実装済**（コミット 6257b6a: pause 撤去・会話中判定のエピソード移管・meta_layer 自己ゲート例外。対象テスト 194 passed）。**Phase 2（ライフの器: lives 永続化・day_open 宣言・境界イベント・台帳世代交代）実装完了・検証待ち**（`saiverse/day_plan.py` / `saiverse/judgment_points.py` / `saiverse/autonomy_wiring.py` / `builtin_data/tools/judgment_finalize.py`、新規 `tests/test_life_phase2.py` 42 件 + 既存系全緑。まはー実機検証はまだ）。**Phase 3（キャッシュ連動: keep-alive のライフ従属・ライフ終端の節目・均等モード TTL 運転）実装完了・検証待ち**（`saiverse/day_plan.py`（`is_keepalive_allowed` / `_handle_life_end` の keep-alive cancel + TTL override の遅延解除予約。anchor は触らない — §6.2 v0.4）/ `sea/runtime.py`（`run_cache_keepalive` の life ゲート）/ `saiverse/saiverse_manager.py`（`clear_persona_cache_override`）、新規 `tests/test_life_phase3.py` 18 件 + 既存系全緑。まはー実機検証はまだ）。残: Phase 4=見せ方（話しかけやすさ表示・ライフビュー括り直し）。
**親**: [`autonomous_behavior_v2.md`](autonomous_behavior_v2.md)（三本柱） / [`persona_cognition/life_concept_map.md`](persona_cognition/life_concept_map.md)（哲学層。§8 出来事・§10 Track 再解釈は本書の前提）
**吸収対象**: [`session.md`](session.md)（v0.1 起草中のまま停滞。§6 未確定事項に本書が回答し、Session を「ライフが目標を与える機構層」として位置づけ直す）
**経緯**: [実機初日の前提レベル設計課題](../issues/autonomous_v2_post_live_gaps.md) 束A（A3 予算・A4 キャッシュ生存）＋束C（Track の意味論）の解決設計。まはー裁定 2026-07-13。
**表面化症状（本書で根治）**: [redundant_track_switch_notification_on_reactivation](../issues/redundant_track_switch_notification_on_reactivation.md)

> **用語注意**: 本書の「ライフ」は life_concept_map.md の "life"（暮らし＝人生の意味）とは**別の概念**。あちらは概念地図の名前、こちらは「ひとつの時間割でくくられる活動区間」という実装単位。ライフビュー UI が表示する単位はこちら。

---

## 1. これは何か

**ライフ = ペルソナが「この区間、この濃度で生きる」と宣言する活動区間**。開始・終了時刻とコマ予算（標準モデルのパルス回数）で宣言され、その間キャッシュが熱く保たれることを機構が保証する。

これにより時間の階層が完成する：

| 層 | 単位 | 定義 | 実装の現状 |
|---|---|---|---|
| **ライフ** | 数時間 | ひとつの時間割でくくられる活動区間。宣言（時刻＋予算）を持つ | **無い（本書で新設）** |
| **エピソード（できごと）** | 数分〜数十分 | 実際に時間を満たしたもの。会話・作業・コマ実績（life_concept_map §8） | `saiverse/episodes.py` 実装済（kind + occurrence_id + open/close + 層0タグ） |
| **パルス** | 数秒〜数分 | 認知→判断→行動の 1 サイクル | 実装済（PulseController / SEARuntime） |
| **ビート** | 一手 | パルス内の最小行動単位（発話・スペル 1 発） | 命名済・型なし（[issue](../issues/beat_concept_not_typed_in_implementation.md)。型化は本 intent のスコープ外——二本目「エピソードの記憶と見せ方」intent で扱う） |

判定基準（試金石）：**「エアは今話しかけて大丈夫？」に、システムが嘘なく即答できるか。** ライフ中＝キャッシュが熱い＝気軽に話しかけてよい、が状態として見え、その表示が課金の実態と一致していること。

---

## 2. なぜ必要か — 三つの欠落

### 2.1 キャッシュ生存を語る器が無い（A4）

Anthropic の explicit cache は TTL 1h・**TTL 内の再送で無料延命**（[cache_lifecycle_control.md](cache_lifecycle_control.md) §1）。つまり標準モデルのパルスが 1 時間以内の間隔で刻まれ続ける限り、一日ぶんの文脈を 1 回の write 課金で維持できる。これは「キャッシュ経済を世界の物理法則にする」設計の核心的な省エネ経路なのに、現状**「自律行動の間、何時間キャッシュを生かすか」を規定できる場所がどこにも無い**。時間割はコマの列であって区間の宣言ではなく、Session（session.md）は「切れそうになったら節目を打つ」受動概念で、能動的に「この区間は繋ぐ」と言える器が欠けている。

### 2.2 予算の単位が支配項とずれている（A3）

日次予算は「作業ラウンド数」で管理される（`saiverse/day_plan.py` の予算台帳: `budget_total_rounds` / `budget_used_rounds`、予算ゲートが発火時にラウンドを切り詰める）。しかし作業セッションのラウンドは軽量モデルで、コストの支配項は**標準モデルの発火回数＝コマ数（判断点・暮らしパルス）**の側。効かない変数を絞り、効く変数が野放しになっている。

### 2.3 「いま何をしているか」の置き場所が歪んでいる（束C）

Track は概念再編（life_concept_map §10）で「目的の木の第一階層ノード＝目的の指し示し」に変質した。だが実装は「リアルタイム進行状態の管理」だった頃の状態機械（running / pending / alert）を引きずっている。実機で出た症状と実装確認（gaps doc C2）：

- **wait_response 30 分タイムアウトが Track を running→pending に落とし**、会話再開のたびに `activate` が「## Track 切替通知」を注入する（ペルソナはどこにも移っていないのに）。[redundant issue](../issues/redundant_track_switch_notification_on_reactivation.md) の実体
- 逆に**コマ発火は Track を切り替えない**（`day_plan._handle_worker_slot` は activate を呼ばない）。「時間割の行動を始めた＝いまその目的で動いている」がどこにも記録されない——ように見えるが、実は**コマ発火は既にエピソードを開き、origin_ref でコマ→目的への参照チェーンを刻んでいる**（§7.1）。歪みの正体は「情報が無い」ではなく「真実の置き場所が Track 状態とエピソードの二重になっていて、古い方（Track 状態）を読み続けている」こと

まはーの裁定（2026-07-13）：**Track ＝ 目的の指し示し。時間が過ぎたら勝手に pending されるべきものではない**。これは life_concept_map §10.1 の既裁定「running / alert 状態は廃止——出来事（open）と呼びかけへ移管」と同じ結論であり、本書はその実行設計を含む。

---

## 3. 設計原理

1. **宣言が先、機構が後**：キャッシュ生存・予算・「話しかけやすさ」表示はすべて「ライフの宣言」から導出される。宣言なき最適化（暗黙のキャッシュ延命）を作らない
2. **「いま」の真実は出来事が持つ**：「いま何をしているか」は開いているエピソードが指す。Track は指される側（目的ノード）であり、時間の事実を背負わない（life_concept_map §10.1 の実行）
3. **モデルの物理法則に世界を合わせる、逆はしない**：均等パルスは Anthropic/OpenAI の課金物理に合わせた**モード**であり、Gemini には強制しない。モードは宣言の属性
4. **予定は檻ではない**（v2 intent 継承）：ライフも時間割同様、判断点で編集できる。動的宣言（「今から 3 時間」）も一級
5. **嘘の状態表示をしない**：「話しかけやすい」表示はキャッシュの実態（熱いか）と一致させる。Gemini はキャッシュ観点で無関係だが「活動区間内」という事実は同じなので同表示でよい（嘘にならない）

---

## 4. ライフの宣言

### 4.1 宣言の形

```
ライフ = { 開始時刻, 終了時刻, コマ予算（標準パルス回数）, モード（均等/自由）}
```

例：「午前 8:00–12:00 の 4 時間、標準パルス 6 回で維持」「午後 16:00–22:00 の 6 時間、パルス 8 回」。

- **1 日 1 回に制限しない**。午前の部・午後の部のような複数ライフが正常形。ライフとライフの間は「谷」（キャッシュを維持しない時間。§8.3）
- **動的宣言も可**：「今から 3 時間、パルス 5 回」。起床時に編んだライフに縛られない
- **宣言者はユーザーとペルソナの両方**。ユーザーは UI から、ペルソナは判断点の構造化出力から

### 4.2 宣言点 — 起床判断に相乗りする

新しい判断点は作らない（ゼロコール原則の系譜）。起床判断（day_open）は既に時間割の編成＋予算配分を出力する（[judgment_points.md](persona_cognition/judgment_points.md) §4）。ここに**ライフ区間の宣言を同じ response_schema で追加**する——時間割のコマ列を「どの区間でくくるか」は編成と同時に決めるのが最も文脈が濃い。

日中の組み替え（会話終了判断・セッション終了判断での時間割編集）も同様に、既存の編集出力にライフ編集を相乗りさせる。

### 4.3 時間割との関係

ライフは時間割の**上位区間**。各コマはいずれかのライフに属する（谷にコマは置けない——コマを置きたければそこはライフである）。逆に、コマの無い時間帯もライフ内なら「熱いまま静かにしている時間」として合法（keep-alive touch の対象。§5.2）。

---

## 5. モデル別モード — 均等 / 自由

### 5.1 なぜモードが要るか

provider の課金物理が逆向きだから（cache_lifecycle_control.md §1「provider で最適戦略が逆転する」）：

| | Anthropic / OpenAI | Gemini |
|---|---|---|
| キャッシュ延命 | 再送で TTL リセット＝**無料 extend**（Anthropic 1h） | extend も時間課金が続く／implicit は制御外 |
| 最適な発火 | **1h 窓を切らさない均等配置** | 任意（好きなタイミングでよい） |
| モード | **均等** | **自由** |

- **均等モード**：ライフ内の標準モデルのパルス（判断点・暮らしパルス）を、隣接間隔が TTL（1h）を超えないよう配置する。起床判断の時間割編成に「標準パルスの間隔制約」として渡し、保存検証でも機械チェックする（LLM へのお願いで終わらせない）
- **自由モード**：間隔制約なし。コマは意味の都合だけで置く

モードはライフ宣言の属性。既定はペルソナの標準モデルの provider から導出し、上書き可能にする。

### 5.2 パルスが届かない区間の延命 — keep-alive touch

均等モードでも、コマ間隔が開く区間（暮らしの静かな時間）はありうる。既存の **keep-alive touch**（「同一 prefix への意味的に不活性な極小 touch」、life_concept_map §14 A3 で実装済み）がこの隙間を埋める部品になる：ライフ内でのみ作動し、ライフ終端で自然停止する、と作動条件を**ライフに従属**させる。現状の「Active のみ・anchor 失効まで」という条件を「宣言されたライフの中」に置き換えることで、延命の根拠が常に宣言に遡れる（原理 1）。

### 5.3 標準パルスの定義（均等モードが数える対象）

数えるのは**標準（DEFAULT_MODEL）で撃たれるメインラインのパルス**：判断点（day_open / post_conversation / post_session / on_event / day_close）と暮らしコマのパルス。作業セッションのラウンド（軽量・サブライン）は数えない——キャッシュ生存に寄与しないため（別 line の別キャッシュ）。

> 注：二本目 intent（A1）で作業セッションが AUTONOMOUS アスペクト（メインライン）化された場合、セッションのラウンドもメインラインのキャッシュを触ることになり、この定義は再訪が要る。本書は現行の WORKER サブライン構造を前提に書く。

---

## 6. ライフと Session — 宣言と機構

### 6.1 関係の定義

[session.md](session.md) の Session は **(persona, model) 粒度の機構概念**——head が安定しキャッシュが効き続ける区間を管理し、「続けられなくなったら」節目（Metabolism）を打つ。ライフはこれを**置き換えない**。関係は：

**ライフ＝制御プレーン（意味層の宣言）／ Session＝データプレーン（機構層の運転）**。ライフが「この区間・この本数で生かせ」と目標を与え、Session がその目標に沿って head 安定・キャッシュ継続・節目打ちを運転する。

- **均等モードのとき、ライフと Session は一致する**（gaps doc A4 裁定）：ライフ開始＝Session 開始（head capture / anchor 起点）、ライフ終了＝Session 終了（Metabolism の第一候補タイミング）。「一致」は概念の同一性ではなく、**宣言どおりに運転された結果**
- **自由モード／会話専用モデル等では従来どおり** Session は自律的に節目を判断する（session.md §6.1 の件数・TTL・context 使用率基準）

### 6.2 session.md §6 未確定事項への回答

| session.md の未決 | 本書の回答 |
|---|---|
| §6.1 終了判定基準 | 第一基準＝**ライフ終端**（宣言）。例外基準＝context 使用率閾値・context 超過エラー（安全弁として存置）。ライフ外の活動（谷の会話等）は従来基準 |
| §6.2 境界での実行内容 | ライフ終端＝節目。ただし**終端が能動的に行うのは keep-alive の停止だけ**。anchor は**触らない**——touch が止まれば TTL で自然失効し、Chronicle 化＋履歴縮小（Metabolism 本体）は失効後の最初の活動の既存経路（runtime_context Case 3）が行う。理由: 惜しい谷（終了直後〜TTL 内の再訪）では実キャッシュがまだ生きており、anchor を即時失効させると最初の Pulse が Case 3 で履歴を組み替えて生きたキャッシュを捨てる（§8.3 裁定と矛盾）。**TTL override（均等モードの 1h）の解除も同じ理由で即時に行わず、終端＋TTL 経過後に遅延**する——anchor validity は「現在の TTL 設定」で評価されるため、即時に 5m へ戻すと実キャッシュの寿命（1h）と評価がズレる（v0.4 で訂正: v0.3 の「anchor 即時失効」は誤りだった） |
| §6.4 トリガータイミング | ライフ終端は post-response（区間終了イベント）。安全弁（超過）は現行の pre-response Case 3 を存置 |
| §6.5 見せ方 | ライフビューがそのまま回答になる（§9） |

§6.3（anchor の per-model 3-level fallback）は機構層の詳細としてそのまま残る——ライフは干渉しない。

### 6.3 A2（畳み）との境界

エピソード単位の畳み（LoD）は二本目 intent の主題だが、**畳むタイミング＝Metabolism＝ライフ境界**という接続だけ本書が確定する。「終わってすぐのできごとを畳むとキャッシュを道連れにする」（gaps doc A2 のまはー自己訂正）への構造的回答が「畳みはライフ終端まで待つ」——ライフ中は生ログが熱いまま積まれ、谷に落ちるとき一括で代謝する。

---

## 7. 「いま何をしているか」は出来事が持つ — Track 状態の移管（束C）

### 7.1 事実の確認 — 参照チェーンは既に半分ある

実装確認（2026-07-13）：

- コマ発火（`day_plan._handle_slot_fire`）は **kind='slot' のエピソードを開き**、`origin_ref` にコマ参照を刻む。コマ定義は ref（task:N / desire:N / track:N）を持つ。作業セッションは kind='work_session' のエピソードを親（slot エピソード）参照つきで開く
- 会話開始は kind='conversation' のエピソードを開く（`open_conversation_episode`、同じ Building の会話は occurrence_id で束ね済み）
- つまり**「開いているエピソード → origin_ref → 目的ノード」で「いま・何のために」は既に導出可能**。Track の running 状態は同じ情報の古い置き場所であり、二重帳簿になっている

### 7.2 移行の三案

| 案 | 内容 | 判定 |
|---|---|---|
| 案 X（増築） | コマ発火にも `activate` を足し、running を「今の目的」として正しく維持。通知は出し分けで抑制 | ✗ activate のたびに displaced 連鎖・タイマー管理・通知出し分けが複雑化。二重帳簿の解消にならない（両方書くだけ） |
| **案 Y（部分再設計・推奨）** | **「いま」の読み出しを開いているエピソードへ一本化**。wait_response タイムアウトは会話エピソードの close と会話終了判断のみ行い、**Track の状態を動かさない**。running/pending 遷移の残存参照点を棚卸しし、エピソード判定へ置換 | ✓ 真実が 1 箇所になる。切替通知は出し分けでなく**構造的に消滅**（同一 Track 復帰という事象自体が無くなる）。§10.1 への足場 |
| 案 Z（全面刷新） | running / alert カラムを DB から廃止、メタ判断の状況分類（`_SITUATION_PLAYBOOK_MAP`）も判断点 5 種へ完全統合 | 終着点として正しいが、alert→呼びかけの分化・状況分類の統合（judgment_points.md §9 の未決）が先に要る。今回は踏まない |

案 Y の要点：**pause という操作は残る**（メタ判断が明示的に「この目的を置いて別に移る」と決める遷移は正当）。死ぬのは「時間経過が自動で pause を呼ぶ」結線だけ。wait_response タイムアウトの正体は life_concept_map §8 の「出来事の運用境界」（安い・撤回可能な仮決定）であり、出来事を閉じる仕事はそのまま——**越権して Track の状態まで動かしていたのをやめる**。

### 7.3 移管する参照点（実装時に棚卸しして確定）

「running を読んで『いま』を判定している」箇所を、開いているエピソード判定に置換する。設計時点で判明している主要点：

| 参照点 | 現状 | 移管後 |
|---|---|---|
| コマ発火のユーザー会話中ガード（`day_plan` L895 付近） | `get_running()` が user_conversation か | 開いている kind='conversation' エピソードの有無 |
| wait_response タイムアウト（`track_manager._handle_wait_response_timeout`） | pause（running→pending）＋ episode close ＋ post_conversation 判断 | **episode close ＋ post_conversation 判断のみ**（Track 不動） |
| ユーザー発話時の再開（`user_conversation_handler.on_user_utterance`） | pending→activate→切替通知注入 | 会話 Track が既に「選ばれている」なら activate 不要。新しい会話エピソードを開くだけ（通知消滅） |
| メタ判断の状況分類（`meta_layer._SITUATION_PLAYBOOK_MAP`） | running の有無で分岐 | 当面存置（判断点への統合は §9 未決に従い案 Z へ持ち越し）。ただし判定入力を「開いているエピソード」に併記し、乖離をログで観測 |
| `activate` の displaced 押し出し＋切替通知 | 全 activate で発火 | 本物の目的切替（メタ判断・判断点・手動スペル発）に限定される——時間起因の activate が消えるため、経路はそのまま意味が正しくなる |
| Track Chronicle の head 搭載（`get_memory_weave_context._get_track_chronicle_context`） | `get_running()` の Track のあらすじを MemoryWeave セクションが head に織る（user_conversation は除外・refresh は Metabolism のみ） | 一本目では**参照点として記録のみ**（挙動不変）。読み込み側の世代交代（head 自動搭載 → 起動時指示書＋机メモ→随意想起の二段〔life_concept_map §9.2 裁定〕）と、書き込み側（目的別あらすじ生成）のエピソード Lv1 Chronicle との統合は**二本目 intent の主題** |

alert は本書のスコープ外（呼びかけへの分化は life_concept_map §5 の将来課題。現行の internal_alert_poller / on_event 経路は不変）。

### 7.4 redundant issue の根治

上記により [redundant_track_switch_notification_on_reactivation](../issues/redundant_track_switch_notification_on_reactivation.md) は**通知の出し分け修正なしで根治**する——「同一 Track への再 activate」という事象そのものが消えるため。先行して入れる症状止め（同一 Track 復帰の通知抑止）は、本設計が landed した時点で不要になる使い捨てガードと位置づける。

---

## 8. 予算 — 単位の世代交代

### 8.1 予算式

```
日予算 = Σ ライフのコマ予算（標準パルス回数） + 作業ラウンド総数 × 係数 κ（0〜1）
```

（gaps doc A3 裁定「コマ数 + ラウンド数 × 係数」の実装形。）支配項＝標準パルスを一級の予算にし、軽量ラウンドは係数で減衰させて計上する。κ は標準/軽量の単価比の近似で、初期値は未決（§12）。ローカル LLM に逃がした軽量ラウンドは κ=0 相当（無料）にできる余地を残す。

### 8.2 台帳の置き場所

現行の日次台帳（day_plan meta_json の `budget_total_rounds` / `budget_used_rounds`）を**ライフ単位の台帳に世代交代**する：ライフ宣言がコマ予算を持ち、消費（判断点・暮らしパルスの発火、セッションのラウンド×κ）をライフに積算する。日次値はライフの合計として導出（新カラムでなく導出値）。予算ゲート（発火時の切り詰め・残高ゼロで skip）の機構はそのまま流用し、参照先だけライフ台帳に変わる。

### 8.3 谷とライフ外の活動

- **谷（ライフとライフの間）**：キャッシュを維持しない。keep-alive も止まる。コマも置かれない
- **谷での会話**：ユーザー会話は常に最優先（不変条件 1）で、谷でも普通に始まる。**動的ライフは自動で立てない**（まはー裁定 2026-07-13）——谷の会話は cache_lifecycle の既存モード運転（キャッシュタイマー）に任せる。ライフの動的**宣言**（§4.1）は別物で健在：立てるなら誰かが宣言する、が原理 1 の帰結
- **ライフ終了直後の「惜しい谷」**：猶予窓は作らない（まはー裁定 2026-07-13）。keep-alive はライフ終端で停止してよい——explicit cache の TTL がしばらく自然残存するので、直後に来た会話はそれが実質カバーする。宣言外の延命機構を足さない（不変条件 2 と整合）

---

## 9. 状態の明示とライフビュー

### 9.1 「話しかけやすさ」の表示

ライフ中のペルソナは**キャッシュが熱い＝追加コストが軽い＝気軽に話しかけてよい**。これを状態として UI に明示する（gaps doc A4 裁定）。世界の物理法則（キャッシュ経済）がそのまま「話しかけやすさ」という社会的シグナルになる——本設計の芯。Gemini（自由モード）でも「活動区間内」の表示は事実なので同じ表示でよい。

### 9.2 ライフビューの括り直し

「ライフビュー」という既存 UI 名称がまさにこの単位を指すことになる。現状パルス基準の表示を**ライフ → エピソード → パルス**の階層で括り直す（大改修ではない——gaps doc A4）。進行中のライフ＋辿ってきたライフが一覧でき、各ライフの予算消費・エピソード列が展開で見える。エピソード内部の見せ方（パルス・ビートのリアルタイム表示、チャット UI への枠投下）は二本目 intent（B1+X1）の主題。

### 9.3 ペルソナ自身への見せ方 — tail のシステム通知

ペルソナへのライフの提示は **tail（末尾イベント）のシステム通知**で行う（まはー裁定 2026-07-13）。head に置かない理由は時間割（life_concept_map §15 ②「今日の自分」）と同じ——ライフは起床判断の成果物であり、さらに**動的宣言で即座に開始する拡張**を考えると head に情報を入れるタイミングが存在しない。形式は Track 切替通知（`<system>` ラップの user メッセージとして SAIMemory へ末尾追記、キャッシュ無破壊）とかなり似通ったものになる：ライフ開始・終了・組み替えを同形の通知で流す。

---

## 10. 守るべき不変条件

1. **ユーザー対話の至上性**（v2 intent 継承）：会話はライフ・谷を問わず常に最優先割り込み。ライフはコストの物理を可視化するだけで、会話可否のゲートにしない
2. **宣言なき延命なし**：keep-alive・TTL extend は宣言されたライフの中でのみ作動する。「なんとなく生かし続ける」を作らない
3. **「いま」の真実は 1 箇所**：いま何をしているかは開いているエピソードが指す。Track 状態と二重に持たない（移行完了後）
4. **時間経過は目的を動かさない**：Track の状態遷移は判断（メタ判断・判断点・手動）だけが起こす。タイムアウト類が動かせるのは出来事の開閉まで
5. **表示は課金の実態と一致**：「話しかけやすい」が嘘にならない（熱くないのに熱いと見せない）
6. **キャッシュヒット継続を最優先**（C-7 継承）：ライフ中の head 不変は cached_head_architecture の保証をそのまま引き継ぐ

---

## 11. 実現手段（機構対応）

### 11.1 既存流用

| 部品 | 現物 | 用途 |
|---|---|---|
| 出来事の開閉＋origin_ref | `saiverse/episodes.py`（層0 タグ・occurrence_id 込み） | 「いま」の真実の置き場所（§7） |
| 会話エピソードの close 経路 | `autonomy_wiring.handle_wait_response_timeout`（close→post_conversation 判断まで配線済み） | §7.2 案 Y の土台（Track 不動化はここから pause 呼び出しを抜く） |
| 起床判断の編成出力 | `judgment_points.md` §4 day_open（時間割＋予算） | ライフ宣言の相乗り先 |
| 予算ゲート・台帳 | `day_plan.py` init/get/consume_budget ＋発火時切り詰め | 参照先をライフ台帳へ差し替え |
| keep-alive touch | life_concept_map §14 A3 実装済（意味的に不活性な極小 touch） | 作動条件をライフ従属に変更（§5.2） |
| explicit cache TTL 運転 | cache_lifecycle_control.md 連続モード（Anthropic 1h・再送延命） | 均等モードの物理的根拠 |
| コマ予約・営業日 | `day_plan.py` EventScheduler push ＋ `autonomy_wiring` effective_plan_date | ライフ区間の予約・跨ぎ対応をそのまま継承 |
| ライフビュー | `persona_activity_view.md` 系 UI | 括り直しの土台（§9.2） |

### 11.2 新設

1. **ライフの実体**：宣言（開始・終了・コマ予算・モード）の永続化。推奨は day_plan の meta_json に lives 配列（1 日 1 プラン行に区間定義が同居。エピソードへの life 刻印は時刻からの導出で開始し、必要になったら列を足す）——専用テーブルは複数日跨ぎ需要が出てから
2. **day_open スキーマ拡張**：時間割編成にライフ区間宣言を追加＋均等モードの間隔制約（プロンプト提示と保存時の機械検証の両方）
3. **動的ライフ宣言の口**：ユーザー（UI/API）とペルソナ（スペル or 判断点出力）の両方
4. **ライフ境界イベント**：開始（head capture / Session 開始）・終了（Metabolism・台帳締め・keep-alive 停止）の EventScheduler 配線。watchdog の見張り対象に追加
5. **running 参照点の移管**（§7.3 の表）：棚卸し→エピソード判定への置換→wait_response の pause 呼び出し除去
6. **「話しかけやすさ」表示**：ライフ状態の API 露出＋フロント表示

### 11.3 死ぬもの・変質するもの

| 対象 | 扱い |
|---|---|
| wait_response タイムアウトの pause（running→pending） | **死ぬ**。タイムアウトの仕事は出来事の close と会話終了判断のみに縮退 |
| 同一 Track 復帰の「切替通知」 | **構造的に消滅**（事象ごと無くなる。先行症状止めのガードは landed 時に撤去） |
| 日次予算台帳（budget_total_rounds） | ライフ台帳へ**世代交代**（日次値は導出値に降格） |
| Session の終了判定（自律判断） | 均等モードでは**ライフ終端が第一基準**に変質（安全弁は存置）。session.md は本書レビュー通過後に吸収改訂 |
| Track の running / alert 状態 | 本書では**殺さない**（案 Y）。「いま」の読み出しをエピソードへ移し終えた後、案 Z（§10.1 完全実行）で廃止——判断点統合（judgment_points §9）とセットの後続 |

---

## 12. 未決事項（実装フェーズで確定すればよいもの）

1. **係数 κ の初期値**：標準/軽量の単価比から機械的に置くか、素朴に 0.2 等で始めるか
2. **均等モードの間隔制約の既定値**：TTL ちょうど（60 分）は危険（遅延で割る）。安全マージン込みの既定（例: 50 分）
3. **ライフのコンテキスト長対応**：均等モードでライフ＝Session だと長いライフは context を使い切りうる。まはー裁定「まず漏れないので後回し可」——安全弁（§6.2 の超過基準）が既定で効くことだけ確認して持ち越し

### 裁定済み（v0.1 レビュー、まはー 2026-07-13）

- ~~会話とライフ~~ → **動的ライフは自動で立てない**。谷の会話は既存キャッシュタイマー任せ（§8.3）
- ~~惜しい谷の猶予窓~~ → **作らない**。TTL の自然残存がカバーする（§8.3）
- ~~ペルソナへの見せ方~~ → **tail のシステム通知**。head に入れるタイミングが無い（§9.3）

---

## 13. 関連ドキュメント

- [`autonomous_behavior_v2.md`](autonomous_behavior_v2.md) — 三本柱・時間割・判断点（本書の土台）
- [`persona_cognition/life_concept_map.md`](persona_cognition/life_concept_map.md) — 哲学層（出来事 §8・Track 再解釈 §10）
- [`session.md`](session.md) — 吸収対象（機構層としての Session）
- [`cache_lifecycle_control.md`](cache_lifecycle_control.md) — TTL 戦略・モード（物理法則側）
- [`persona_cognition/judgment_points.md`](persona_cognition/judgment_points.md) — 宣言の相乗り先
- [`../issues/autonomous_v2_post_live_gaps.md`](../issues/autonomous_v2_post_live_gaps.md) — 経緯（束A/束C）
- 二本目 intent（起草予定）: エピソードの記憶と見せ方（A1 監査役・A2 LoD・B1/X1 可視化・Beat 型化）

---

## 改訂履歴

- Phase 3 実装 (2026-07-13, v0.4 準拠に差し戻し修正済): キャッシュ連動を実装。①**keep-alive のライフ従属** (§5.2) — 判定は ``day_plan.is_keepalive_allowed`` に集約し、唯一の呼び出し元 ``sea.runtime.SEARuntime.run_cache_keepalive`` の Active チェック直後 (schedule_cache_ttl_pulse への再予約より前) でゲートすることで、谷では touch も再予約もされず連鎖が自然停止する。lives 未宣言は常に許可 (後方互換)、判定失敗は許可側にフォールバック。②**ライフ終端の節目** (§6.2 v0.4) — ``day_plan._handle_life_end`` が能動的に行うのは keep-alive 予約 (``ttl:{persona_id}``) の cancel と TTL override の遅延解除予約だけ。**anchor は触らない** — touch が止まれば TTL で自然失効し、Metabolism は失効後の最初の活動の既存 Case 3 経路 (``sea/runtime_context.py``) が行う。③**均等モードの cache TTL 運転** (§5.1) — ライフ開始 (mode=even) で persona の cache override を TTL=1h に設定し (人設定タブの明示 override があれば触らない)、終端では即時 clear せず「終端 + anchor validity 秒」の遅延解除 (``life_ttl_clear:{persona_id}``) を EventScheduler に予約する (即時に 5m へ戻すと anchor の生存評価が実キャッシュの寿命とズレるため)。発火体は厳密一致チェック付きで clear (``saiverse_manager.clear_persona_cache_override`` 新設)。次のライフが TTL 経過前に始まれば開始側が予約を cancel する。global 既定 TTL が "5m" のままだと均等モードの間隔上限 (50 分) を大きく下回り、artificial keep-alive が 3〜4 分おきに連発する調査結果を受けての配線。④ライフ開始時の Session 境界は既存機構 (anchor の TTL 自然失効 → 次 Pulse の Case 3) が自然に満たすことを確認し、ログ追加のみ。新規 `tests/test_life_phase3.py` 18 件 + 既存系 (test_life_phase2 / test_cache_keepalive / test_cache_lifecycle 等) 全緑。
- v0.4 (2026-07-13): 検収差し戻しによる訂正——**v0.3 の「anchor 即時失効」は誤り**。①anchor を即時失効させると、惜しい谷 (終了直後〜TTL 内の再訪、実キャッシュは生きている) の最初の Pulse が Case 3 で履歴を組み替えて生きたキャッシュを捨てる (§8.3 裁定と矛盾)。keep-alive を止めれば anchor は TTL で自然失効するので即時失効はそもそも不要。②TTL override (均等モードの 1h) の即時解除も同型のズレ——anchor validity は「現在の TTL 設定」で評価されるため、終端で即時に 5m へ戻すと実キャッシュ (1h) と評価がズレて TTL 内の再訪が Case 3 に落ちる。解除は終端 + TTL 経過後へ遅延する。§6.2 の表を訂正。
- v0.3 (2026-07-13): §6.2 の境界実行形を明確化——ライフ終端は **anchor の即時失効のみ**とし、Metabolism 本体（Chronicle 化＋履歴縮小）は次の活動開始時の既存経路（Case 3）へ遅延する、とした（**この「即時失効」は v0.4 で誤りと訂正**）。
- v0.2 (2026-07-13): まはーレビュー反映。**案 Y 承認・§5.2 keep-alive ライフ従属 GO・不変条件 §10-2 承認**。裁定 3 件を本文へ（動的ライフ自動生成なし §8.3 / 惜しい谷の猶予窓なし §8.3 / ペルソナへの提示は tail システム通知 §9.3）。§7.3 に running 参照点を 1 件追加——Track Chronicle の head 搭載（`_get_track_chronicle_context` が `get_running()` を読む）。読み込み側の世代交代（head 自動搭載→起動時指示書＋机メモ→随意想起）と書き込み側のエピソード Lv1 Chronicle 統合は二本目 intent の主題と線引き。
- v0.1 (2026-07-13): 起草。ライフ＝活動区間の宣言（時刻＋コマ予算＋モード）として新設し、時間の階層（ライフ→エピソード→パルス→ビート）を確定。Session との関係を「制御プレーン/データプレーン」で定義し session.md §6 に回答。束C は案 Y（「いま」の読み出しをエピソードへ一本化・wait_response の pause 除去）を推奨、redundant issue の構造的根治を含む。予算はコマ＋ラウンド×κ でライフ台帳へ世代交代。
