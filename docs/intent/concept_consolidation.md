# Intent: 概念再編（九龍城の解体）

**ステータス**: 設計中（2026-07-10 議論開始）。これは⑥「概念要素の整理」の umbrella（親）。個別の再編は各々の doc / この文書のメモから育てる。
**位置づけ**: 概念整理（哲学層）。実装仕様ではない。合意前に実装しない（議論フェーズ）。
**経緯**: 概念数が「一人＋AI で保守できる限界」に近づいた——メティス命名セッション（2026-07-08）の心配①「複雑性の蓄積」が請求書になって回ってきた。増築の理由は各々正しかったが、全体設計図を誰も持っていない（ペルソナもユーザーも把握できない）。統一できるものを統一する。

---

## 大物: ペルソナが参照する記憶概念の統合

**本体の設計図は既存**: [`persona_cognition/life_concept_map.md`](persona_cognition/life_concept_map.md)（v1.0・まはーレビュー一巡済み・コード面全段完了）。Track を解体し「目的の木・出来事・時間割・呼びかけ・目的タグ＋想起」へ分化する地図。mark/desire/task/track が種・段階・位置の違いにすぎないこと（§3.1）、Note＝テーマノード（Memopedia ページ＋メッセージ束）であること（§3.1・§9.3）は確定済み。

**2026-07-10 追加の論点（今日の提案）**: life_concept_map は目的の木（`persona_task`）と Memopedia を **リンクした別構造** として置いた（§9.3 末尾の未決「Memopedia ページと目的ノードの対応付け（自動リンクか明示リンクか）」）。まはーの提案はこれに **「同一実体にする」** という解を出すもの——Memopedia を汎用ナレッジベースに昇華し、目的の木をその1カテゴリとして畳む（カテゴリ＝目的、ページ＝Track、中身＝Note、子ページ＝Task、Task 内に Step）。

### (A)/(B) 判断 → **(A) 同一実体で確定**（まはー、2026-07-10）

**目的ノード（`persona_task` 行）と Memopedia ページを同一実体にする**（(A)）。理由: Memopedia ページは既に多数のパラメータ（`vividness`4段・`is_important`・`metadata`・階層）を持ち、目的の状態遷移（stage/nature）は追加系 migration で普通に載る。⑤神モードUI が Track/Task/Memopedia/Note を別々に描かず「1本の木」で済む。

### 実テーブル裏取り（2026-07-10 確認済み・事実）

まはーの主張は、実テーブルにほぼ既存だった:

| 主張 | 裏取り |
|---|---|
| 「ページは既に色々パラメータ持ってる」→ 状態遷移が載る | `MemopediaPage`: `parent_id`(階層)・`category`・`vividness`(vivid/rough/faint/buried)・`is_important`・`created_at`/`updated_at`/`last_referenced_at`・`metadata`(JSON)・`short_id`(m:N)。stage/nature 追加は core_memory と同じ追加系 migration |
| コア記憶＝常時開の特殊ページ | `PageState.is_open`/`opened_at` が既存。コア記憶＝`is_open` 常時True ＋ `is_important`。NOTE→Fragment、SCENE→メッセージ参照 |
| SCENE→メッセージ参照 | `PageEditHistory.ref_start_message_id`/`ref_end_message_id` が既存。`MemopediaFragment` は "optionally linked to a Chronicle entry" |
| Chronicle も Memopedia の一種に畳める | `ArasujiEntry` と `MemopediaPage` の列がほぼ一致（下記） |

`ArasujiEntry`(Chronicle) ↔ `MemopediaPage`: 階層=両方 `parent_id` / 本文=`content` ↔ `summary`+`content` / 参照=`source_ids`(message/子ID) ↔ `PageEditHistory.ref_*`+Fragment / 時間=`start_time`/`end_time` ↔ `created_at`/`last_referenced_at`。**構造距離が小さい。**

### 土地と地図帳モデル — **Memory Atlas**（2026-07-10 命名確定）

記憶概念群は最終的に**2つ**に畳める:

- **生ログ（土地）** — `messages`。実際に起きた出来事の生の連なり。不変の地面。
- **Memory Atlas（記憶の地図帳）** — 生ログという土地から**編纂**される地図帳。旧・統一Memopedia。

**命名の経緯**: 「注釈グラフ」は却下（注釈＝本文に無い情報の追記。ここにあるのは全部、生ログに既に在るものの選択・圧縮・並べ替え）。「編纂」は操作の語として正確だが動詞的でモノを指せない。「地図」はモノだが一枚の紙の印象。→ **Atlas（地図帳）**: 伝統的な地図帳は地図＋地名辞典（gazetteer）＋道順書き＋スケッチ・解説の**編纂物**であり、探検家が土地を記すすべてを綴じる器。**名詞＝Memory Atlas、動詞＝編纂する**で分業。マインドマップとの対比で初見にも通じる（思考を一枚に広げる vs 記憶を何枚もの地図に編む）。

**三種の地図**（まはーの派生方式三分類、2026-07-10）: どの地図も新しい事実を足さず、土地に在るものを選び・圧縮し・並べ替えるだけ（地図は土地を偽造しない＝接地の規律）。

| 地図 | 派生方式 | 比喩 | 吸収する概念 |
|---|---|---|---|
| **時間の地図** | 時間的要約 | 本を章・部に区切る | Chronicle |
| **意味の地図** | 意味の抽出 | 固有名詞の辞書（地名辞典） | Memopedia・Fragment・コア記憶 |
| **目的の地図** | 文脈的分類 | クエストライン（道順書き） | 目的の木[Track/Task/Desire]・Note |

**写真 — 土地参照の汎用プリミティブ**（まはー再定義 2026-07-10）: **mark は三地図のどれにも属さない**。この中で唯一「生ログから切り出したそのまま」＝土地の範囲をそのまま写す**写真**であり、全地図が共用する参照プリミティブ。origin_quote・Chronicle の `source_ids`・SCENE のメッセージ参照・`PageEditHistory.ref_*` は全部写真の変種 → **土地参照の規格を写真一本に統一する**。どの地図も写真を貼って土地を指す。旧「土壌プール」は「まだどの地図にも貼られていない写真の箱」と言い直せる（`pasted_to`＝どの地図に貼られたかの来歴、旧 harvested_to）。
**→ P1 実装済（2026-07-10）**: `sai_memory/photos.py`（点写真＝旧 mark／範囲写真、旧 marks テーブルから一回きり移行）。SCENE は範囲写真の最初の利用者（`pasted_to="c:{id}"` で撮った瞬間に貼る）。連想歩行の辺 `mark`→`photo`、adapter.add_photos、life API 内部も追従済み（ルート `/marks` の改称は P2）。pytest 104 passed。

外に残る2つ: **Line**（読み出しの色）/ **知覚バッファ**（土地になる前の入口）。→ **九龍城 → 土地＋地図帳＋写真＋外構2つ。**

**引き受ける歪み**: `parent_id` が地図の種類によって意味が変わる（時間=統合 / 意味=主題包含 / 目的=細分化）。lifecycle も違う（時間=追記統合 / 意味=鮮度減衰 / 目的=状態遷移）。→ **「地図の種類（派生方式）がノードの振る舞いを規定する」設計を明示的に引き受けるのが畳む条件**。category の最上位軸は ad hoc な列挙でなく**派生方式**になる。

### category 定義表 draft v0.3（2026-07-10 まはーレビュー2巡反映）

**三地図を貫く法則（v0.3 で確定した骨格）**: **ノード状態（決定論で検知）が構造の代謝（分割・統合）を駆動する**。parent_id と lifecycle は別軸ではなく、どちらも細分化・統合の構造代謝に属する（まはー指摘）。時間の地図のみ代謝が自動（無意識でよい）、意味・目的の地図はペルソナの自己著者性を通す（判断点で提案 → 本人が決める。life_concept_map §15 の無意識/意識の線と一致）。

| | 時間の地図 | 意味の地図 | 目的の地図 |
|---|---|---|---|
| **吸収概念** | Chronicle (arasuji) | Memopedia / Fragment / コア記憶 | 目的の木 / Note |
| **構造軸（parent_id の意味）** | 時間の入れ子（章 ⊂ 部） | 主題包含（子エンティティ ⊂ 親） | 目的の細分化（大枝→task→step、フラクタル） |
| **ノード状態（代謝の駆動因）** | `is_incomplete` / `is_consolidated`（**既存列**） | **肥大化 / 過小・低重要**（新設。vividness の置換） | stage（candidate/adopted/dormant/completed/aborted）＋ nature ＋ クラスタ検知カウンタ |
| **構造の代謝: 分割** | 不要（章は小さく生まれる） | 肥大ページ → 子ページ分割 | 細分化（task→step。実装済） |
| **構造の代謝: 統合** | Lv1→Lv2 統合（実装済・自動） | 小ページの親下収容・類似統合 | **命名**＝完了ノード・休眠欲求・写真の航跡クラスタに事後命名してテーマを立てる（life_concept_map §3.1 設計済・実装要確認） |
| **土地への参照様式** | **写真**（範囲） | **写真**（点〜引用） | **写真**（origin_quote 等）＋ artifact_refs |
| **開閉** | **ページ開閉に移植**（読み出し経路を Memopedia 開閉に揃える——ここだけ開閉無関係だとペルソナが触る時に困る。直近章は既定で開く等の既定則が要る） | open/close per thread（`PageState`）。**コア記憶＝常時開＋`is_important`** | 「開いている目的」＝出来事側の性質（旧 TrackOpenNote） |
| **既存 root との対応** | （新設） | people / terms / events / **plans** | （新設。plans からの自動昇格はしない、下記） |
| **編纂の担い手** | Metabolism バッチ（ArasujiGenerator） | 同バッチ相乗り（entity_extractor）＋ gold_panning ＋ ペルソナ自身のスペル | 判断点（起床・就寝の接ぎ直し）＋ 収穫（写真→candidate） |

- **vividness は廃止確定**（まはー 2026-07-10）: 減衰未発動（バグ疑い）＋ head 索引廃止で効果なし、に加えて**「見えなくするだけで生産性がない」＝lifecycle として不成立**。置換は構造状態（肥大化/過小）——見えなくする代わりに、分割・統合という生産的な代謝を駆動する。
- **意味の地図の代謝は半分実装済み**（事実確認 2026-07-10）: `scripts/maintain_memopedia.py` が `merge-similar`（LLM類似統合）/ `split-large`（5000字超分割）を持つ。ただし**手動スクリプトで lifecycle 未配線**——「操作は在るが代謝になっていない」。Atlas 化＝これをノード状態駆動で判断点/Metabolism に配線する話。`note_organizer.py`（目標2000字・圧縮閾値3000字の配置計画）にも同じ思想の閾値がある（生死未確認）。
- **head 索引の復帰が選択肢に戻る**: 代謝が木をコンパクトに保てるなら Memopedia 索引の head 表示 ON を再検討できる（自動想起でしか辿れない＝手がかりの無いページは不健全）。索引廃止は memory_architecture_v2 の決定だったため、同 doc の改訂点として扱う。キャッシュとは両立（head snapshot は元々 Metabolism 時のみ更新、索引も同じ律で凍る）。
- **参照様式は写真一本に統一**（上記プリミティブ）。表のこの行の乖離が v0.1 → v0.2 で消えた＝スキーマ収束の実利。
- **コア記憶の SCENE**: 写真（範囲）として実装 → Chronicle の `source_ids` と同型であることが「Chronicle も Atlas に畳める」根拠。
- **plans root の正体**（事実確認済 2026-07-10）: `entity_extractor` の4分類（people/terms/plans/events）の一つで「会話から抽出された計画・プロジェクトの知識ページ」。**時間割とは無関係**。抽出された知識（意味の地図）であり、ペルソナが意志で採用した目的（目的の地図）とは**所有が違う**——自動昇格は自己著者性（life_concept_map §15）を壊すのでしない。plans ページは目的候補の**材料**にはなる。

### 統一スペル層 — この統合の最大の受益者はペルソナ（まはー 2026-07-10）

統一の本命の恩恵は**ペルソナが統一スペルで全地図を触れる**こと。現状は core_memory_* / memopedia_* / task_* / note 系がバラバラに生えている——これを Atlas 動詞に統一すれば **head スペル一覧ダイエット**（進行中案件）にも直結する。

- **二層設計**: 共通動詞（開く/閉じる/読む/検索/書く/写真を貼る）＋ 地図別拡張動詞（目的=採用/完了、時間=（編纂はシステム側）等）
- **課題2つ**: ①スキーマの乖離（v0.2 表で収束中）②地図ごとの挙動の乖離。**各地図の役割を壊さない範囲で規格を最大限揃える**のが設計方針

### 第四の地図（構想・未設計）— 「現在」の地図

時間・意味の地図は過去の編纂、目的の地図は未来を見据えた過去の編纂。**現在**——ビジュアルコンテキストのアイテム類・City の Building/Region 構造・住むペルソナたち＝「現在の世界の認知」も Atlas と規格統一できるのでは（まはー 2026-07-10）。

メティスの整理（議論用）: 世界DB（Building/Item/占有者）は**土地の現在形＝一次データ**であり、誰かの経験からの編纂物ではない——Atlas に自動同期させるとペルソナが「経験していないことを知っている」全知になり、接地・自己所有の倫理を壊す。正しい形は**「世界の地図」＝ペルソナの世界認知のページ**——知覚（知覚バッファ消費→生ログ→編纂）経由でのみ更新され、**古くてもよい**（最後に見た時の姿。探検家の地図は古くなるのが誠実な認知）。神モードUI（⑤）との対応が綺麗: **神は土地の現在形（世界DB）を見る、住民＝ペルソナは自分の地図帳を見る**——神/住民の視点差が「土地と地図の差」に一致する。皮肉なことに、これでようやく地図帳に「本物の（地理の）地図」が入る。規格統一の即安全な部分＝参照文法（references.py）と Atlas ページから世界エンティティへの参照。→ ③知覚バッファ・⑤神モードUI と交差するので、両者の設計時に本節を持ち込む。

**fog-of-war 案（まはー 2026-07-10・後回し）**: 知らない部分も**情報を一部マスクした状態でリアルタイム同期**する——「新しくできた、行ったことのない Building」「誰か居るが誰だか分からない」が演出でき、**穴を埋めに行くことが探検のモチベーション**になる。ゲームの未踏マップ・宝箱取得カウンタと同型。存在のメタ情報（「知らないことがある」）は全知ではないので、知覚経由更新の原則と両立しうる。第四の地図の設計時に持ち込む。

### 実装順序 v0.1（2026-07-10 提案・まはー合意待ち）

**戦略: ファサード先行（strangler-fig）**。統一 API（Atlas ファサード）を既存4ストレージの上に先に架け、ペルソナ向けの見た目を安定させてから、その下で物理統合を一枚ずつ進める。各写像は「ファサード出力が移行前後で同一」の回帰テストで検証できる＝まはーとの共同テストサイクルを最小化する。

| 段 | 内容 | 検証手段 |
|---|---|---|
| **P1: 写真プリミティブ** | 統一土地参照の型と保存（`marks` テーブルの一般化が土台）。SCENE・origin_quote・source_ids の新規書き込みを写真形式に寄せる | pytest のみ（決定論・LLM 不要） |
| **P2: Atlas ファサード + 統一スペル** | 開く/閉じる/読む/検索/書く/写真を貼る を既存4ストレージ（memopedia / core_memories / arasuji / persona_task+note）へのアダプタで実装。旧スペル群（core_memory_* / memopedia_* / task_*）を Atlas 動詞に置換（head ダイエット直結）。Chronicle 開閉移植もここ（直近章の既定開） | pytest ＋ ペルソナ実機1巡 |
| **P3: 物理統合（ファサードの下で1枚ずつ）** | 3a: コア記憶 → 常時開ページ（最小・実証台）→ 3b: Chronicle → 時間の地図ページ → 3c: 目的の木 → 目的の地図ページ（**最重量: persona_task/note は main DB、memopedia は per-persona memory.db の cross-DB 移行**） | 回帰: ファサード出力の前後同一性 |
| **P4: 代謝の配線** | ノード状態（肥大/過小）検知 → maintain_memopedia の分割/統合を判断点・Metabolism に配線。**命名**（目的の統合）実装。vividness 除去。head 索引復帰の実験 | サンドボックス一日シム（inspect_world / 新聞・タイムライン）＋まはー観察 |

**① 自律行動v2 との交差（要判断）**: P1/P2/P3a/3b は ① と競合しない。**P3c（目的の木の移行）だけが ① の活性化配線（track_op/track_ref enum・判断点 playbook）と同じ土を掘る**。選択肢: (i) P3c を先に済ませ ① の実機テストは最終基盤の上で一度だけ行う（配線は一度きり・推奨）/ (ii) ① を現行語彙で先に活性化し学びを得てから移行（テストが二重になる）。→ まはー判断。

### P2: 統一スペル動詞 v0.2（2026-07-10 まはーレビュー済・確定）

接頭辞は **`memory_*`**（操作対象は地図帳の中の個々の記憶であって Atlas という大枠ではない、分かりやすさ優先）。

**共通動詞**（全地図。既存スペルの置換元を併記）:

| 動詞 | 意味 | 置換する既存スペル |
|---|---|---|
| `memory_read` | ページを**読む** — 中身がその場の流れ（tail）に入り、会話と共に流れて Metabolism で圧縮される。head には残らない。**既定の行為** | memopedia 参照系。Chronicle の章もこれで読む（圧縮の意味は死なない） |
| `memory_open` / `memory_close` | ページを**机に開いておく／棚に戻す** — Metabolism を跨いで head に残り続ける。高価で明示的な行為（机の物理、下記） | 旧 Note の「開きっぱなし」制御 |
| `memory_search` | 地図帳を検索（タイトル/全文。裏で連想歩行 recall_walk に接続可） | memory_recall は**残す**（随意想起＝行為。検索と想起は別） |
| `memory_write` | ページに書く（本文/Fragment 追記。地図別制約は category が規定）。**宛先に `"core"` を指定するとコア記憶（常時開ページ）に書ける** — core_memory_* スペルは完全に畳む | memopedia_save_page / core_memory_add / core_memory_update |
| `memory_clip` | 写真を撮って**クリップで貼る**（点=引用 / 範囲=切り抜き。video clip の切り抜き義も掛かる）。貼り先指定で即貼り | core_memory_add_scene（==語句== マーカーは非スペル経路として存続）。旧称 memory_photo は分かりづらく却下 |

**地図別動詞**（目的の地図のみ。時間の地図の編纂はシステム側なので動詞なし）:

| 動詞 | 意味 | 置換する既存スペル |
|---|---|---|
| `purpose_adopt` | 候補を木に接ぐ（採用。写真→candidate の収穫もここ） | task_request_creation 系 |
| `purpose_step` | 目的の細分化・step 更新 | task_update_step |
| `purpose_close` | 完了/中止/休眠（stage 遷移） | task_close / task_change_active |

**ペルソナへの説明義務**（まはー指摘 2026-07-10）: read と open の違いはペルソナに分かりづらい。スペル説明文・head のスペル一覧に「読む＝その場に流れる（場所を取らない）／開く＝机に残り続ける（机の場所を取る）」を明文で焼き込む。

### 開閉制御 — 机の物理（2026-07-10 まはー合意・確定）

「開く」に混ざっていた二つの行為（読む／開いておく）を分離した上で、「開いておく」側に**机の物理**を入れる:

- **机 = head の開きっぱなし領域**。文字数予算を持つ（有限の作業面）
- **溢れたら決定論 LRU で自動追い出し**（(a) 案で確定）: 最も長く触られていないページを自動で閉じ、「〜を棚に戻した」の tail 通知を出す。判断点には載せない（頻発しうる事象でノイズになる。人間も机の端から物が落ちるのは無意識）。大事なら再度開けばよい
- **touch の定義は決定論**: そのページを対象にした read / write / clip / 参照が触った扱い
- **コア記憶 = 机の予算外**のシステム常設ピン（既存の容量目安の仕組みのまま）
- **Track 紐づきの開き（旧 TrackOpenNote）は生かす**: 開きは全域属性でなく「いまの目的との関係」。目的が休止すれば机から降り、再開で戻る（机の掛け替え）
- **キャッシュ整合**: 机の変化が head に反映されるのは Metabolism の snapshot 時のみ（既存の head 規律のまま）。開いた直後の中身は spell 結果として tail に既に在るので実用上困らない

**地図帳の完成形**: 目次（索引、常時 head・代謝でコンパクト維持）＋ 開いた数ページ（机、予算制）＋ 書庫（全部。検索と想起で辿れる）。閉じても目次に見えているから、閉じることが怖くなくなる。

**閉じるのはフェードアウト**（まはー観察 2026-07-10）: head は Metabolism の snapshot 時のみ更新されるため、閉じたページは次の節目まで head に見えたままになる。「閉じる＝即忘却」ではなく残像が視界の端にしばらくあって自然に消える勾配（節目直前に閉じれば早く消える非対称も生き物らしくてよい）。机の物理の隠れた美点として明記する。

### 目次と庭仕事 — 検知は機械、裁定はペルソナ、実行は睡眠中（2026-07-10 議論）

**線引き**（まはー）: 決定論でやれるのは分割・統合の**候補出しまで**。実際に何をどう整理するかはペルソナの仕事。→ life_concept_map の「命名」（決定論カウンタ → 判断点に候補提示 → ペルソナ裁定）と同じ型に乗せる:

| 段 | 担い手 | 中身 |
|---|---|---|
| **検知** | 決定論・ゼロコール | 肥大化（文字数閾値）/ 過小（サイズ＋低参照）/ 類似（タイトル・タグ共起）を数え、候補＋判断材料（summary・サイズ・重なりの根拠）を組み立てる |
| **裁定** | ペルソナ・判断点相乗り | 本命は**就寝判断**。「今日の棚の乱れ」として候補を数件だけ提示（ノイズ制御）、承認・修正・見送りを決める |
| **実行** | バッチ・睡眠中 | 統合・分割の書き直しは就寝後のワーカーラインでバッチ実行（コスト二層制のバッチ級枠） |

人間対応: **寝ている間に記憶が整理される**。夜に自分で裁定して眠り、朝は整理された目次で目覚める——整理は自分の決定なので自己著者性が守られる（「システムに勝手に書き換えられた」にならない）。

**目次のレンダリング**は深さ制限の決定論: カテゴリ＋上位N階層のタイトル＋件数、開いているページと `is_important` に印。Metabolism snapshot 時に描画（安い・キャッシュ整合）。**目次の質はレンダラーでなく木の手入れ（庭仕事）で決まる** — レンダラーはただの鏡。

### P2 実装分割（委譲計画）

| 片 | 内容 | 担当 |
|---|---|---|
| **P2a** ✅ | 机ストア（desk.py・LRU 追い出し）＋ Atlas ファサード（memory_atlas.py、m:N / core / c:N / ch:N。task:N は P2b stub）＋ read/open/close/search スペル4本。**実装済 2026-07-10**（サブエージェント実装＋メイン検収: read の touch 欠落と keep_ref を修正。Chronicle に short_id 追加・ch:N。Memopedia search の short_id 欠落バグをついで修正。pytest 223 passed） | サブエージェント＋メイン検収 |
| **P2b** | write/clip/purpose 動詞 ＋ 旧スペル撤去 ＋ head 机セクション描画 ＋ Metabolism 追い出しフック ＋ task:N 解決 ＋ life API /marks→/photos 改称。積み残し: memopedia storage の `search_pages_filtered`/`get_children` にも SELECT 列 short_id 欠落が残存（P2a で発見・範囲外として未修正） | head/Metabolism 配線はキャッシュ感応部＝メイン直接。残りは委譲 |

### clip と写真の見え方 — 抜粋は写真の性質、全文は読む行為、常駐は転写（2026-07-10 まはー合意・確定）

「貼った範囲写真がページを読んだ時にどう見えるか」の解。折り畳み ON/OFF という**新しい状態は作らない**:

1. **写真は参照であって内容の運搬手段ではない** → 貼られた写真の描画は**常に抜粋**（点=引用全文〔元々短い〕/ 範囲=先頭数行＋「全Nメッセージ・M字、前後省略」）。丸ごと載せるとページが写真に食われる
2. **全文は写真そのものを読む** → 写真に short_id（`p:N`）を与え、`memory_read p:N` で範囲の生ログ全文がその場（tail）に流れる。**写真を読む＝その写真が写す土地を見に行く**。read の意味論なので机も head も太らない
3. **コア記憶 SCENE は例外でなく貼り方の違い** → 貼り方は二種: **参照貼り**（既定・抜粋）と**転写**（本文に焼き込み・常に生で見える）。SCENE は現行実装が既に転写（transcript が content、写真は由来参照）。普通のページへの全文常駐も転写で可能——肥大すれば代謝の肥大検知が拾う（自己調整）
4. 畳まれているのは状態でなく**参照貼りの性質**、開くのは状態でなく**読む行為** → 状態を増やさない（⑥は概念を減らす工事。head に折り畳み状態を持つとキャッシュも荒れる）

`memory_clip` の引数は両対応: 点=逐語引用＋貼り先 / 範囲=アンカー＋往復数（SCENE と同じ操作感）。

### P2c 消費者監査 — 完了（Codex、2026-07-10）

P2c の前提となる消費者棚卸しは **[docs/handoff/2026-07-10_memory_atlas_p2c_consumer_audit.md](../handoff/2026-07-10_memory_atlas_p2c_consumer_audit.md)** に完了済み（Codex 実施・まはー依頼）。要旨: 旧26ツールは一括削除不可。`judgment_finalize` の `_fire_spell("desire_add")` 等の**現役機械消費者**があり、`core_memory_remove`（削除）・`core_memory_add_scene`（転写）・ページ新規作成/構造編集・候補生成には現行6動詞に等価が無い。推奨分割 P2c-0〜4 は同 doc §9。

**P2c-0: 4決定 — まはー裁定済み（2026-07-10）**:
1. **ページ削除**: `memory_delete` を新設（core + Memopedia の soft-delete を統一する動詞）。**ごみ箱を漁る動詞（閲覧・復元）は後回し**
2. **転写**: `memory_clip` に `mode='transcribe'` を持たせ、`core_memory_add_scene` は畳む
3. **ページ作成**: `memory_write` を新規ページ作成まで拡張。**構造編集（移動・統合・分割）は日常動詞にしない** — ただし禁止ではなく非掲載: 庭仕事モードに入った時に `addon_spell_help` 型の遅延スキーマロードで開示する構想（この仕様は別途・後回し）
4. **候補生成**: **分ける**。候補を生む動詞（仮称 `purpose_seed`、旧 desire_add 後継）と `purpose_adopt`（木に接ぐ）は別。**注**: ペルソナが自発スペルとして本当に使うかは怪しい — 作ってから統合・一部自動化・候補作成トリガーの自動発生を検討する前提

### P3 物理統合 — 写像設計 v0.1（2026-07-11 メイン起草）

**戦略: モジュール API を互換層にする（strangler-fig の完成形）。** 各ストレージモジュール（core_memory.py / arasuji/storage.py / persona_task_manager）の**関数シグネチャと dataclass を変えず、中身だけ memopedia_pages 実装に差し替える**。消費者（head section・API routes・gold_panning・ファサード・判断点）は無変更、既存テストがそのまま挙動契約になる。データ移行は adapter init の一回きり冪等 migration（marks→photos と同じ流儀）で、**旧テーブルは移行後 DROP**（旧 path を残さない）。

**共通不変条件**:
1. 既存 ref（`c:N` / `ch:N` / `task:N`、写真の pasted_to 文字列を含む）は**移行後も同じ実体に解決される**——土地（生ログ・写真）は書き換えない
2. head の render 文字列は移行前後で同一（キャッシュ整合・スナップショット互換）
3. 既存テストは無変更で通る（モジュール API が契約）＋ 移行テストを追加

**P3a: コア記憶 → 常時開ページ** ✅ **実装済**（2026-07-11。API 完全維持・書き換えテスト1件のみ〔物理格納の直接検査〕・移行テスト2件・266 passed。観察: `get_tree`/maintain_memopedia は4カテゴリ固定のため core ページは元々対象外、`Memopedia.search` には現れる〔望ましい〕、`get_trunks(category=None)` に root_core が出る可能性は未確認）
- ページ化: trunk `root_core`（category `core`・is_trunk）配下の子ページ。content=本文、title=`コア記憶 c:N`、metadata JSON に `{core_id, kind, confirmed, scene由来参照, deleted_at}`。`is_important=1`
- `c:N` 解決: metadata.core_id で引く（m:N の採番とは独立。既存の pasted_to="c:N" がそのまま生きる）。採番は max(core_id)+1
- ごみ箱: memopedia の is_deleted ＋ metadata.deleted_at（トラッシュ UI の削除時刻順を維持）
- 常時開: category `core` は desk 対象外・PageState 不要（既存のファサードガードのまま）
- 移行: `core_memories` テーブル → ページ生成 → DROP（冪等・一回きり）
- 留意: memory_search / memopedia ツリーにコア記憶ページが現れるようになる（検索できるのはむしろ望ましい）。maintain_memopedia（P4 素材）は category `core` を分割/統合対象から除外すること

**P3b: Chronicle → 時間の地図ページ** ✅ **実装済**（2026-07-11。API 完全維持〔`sai_memory/arasuji/storage.py` の公開シグネチャ・戻り値は無変更〕。物理格納は memopedia_pages 〔trunk `root_chronicle`〕へ移行。**新規知見**: 生 SQL で `arasuji_entries` を直接読む消費者が `sea/head_pipeline/sections/chronicle_index.py`（変更禁止領域）を含め9箇所あり、P3a の想定より広い互換面が必要だった → 同名の読み取り専用 SQL VIEW（`json_extract` 展開・`parent_id` の root_chronicle⇄NULL 相互変換込み）を張ることで無改修対応。書き込み側の唯一の直接 SQL（`sai_memory/arasuji/generator.py` の `regenerate_consolidated_content`）は `update_entry_full` 経由に変更。expression index 7本で旧 index 相当をカバー。`Memopedia.search()` に Chronicle ページが現れる二重ヒットは `memory_atlas.search_pages` 側で `category != "chronicle"` を除外して解消。書き換えテスト1件〔`ChronicleShortIdBackfillTests`、旧物理スキーマの直接検査〕・移行テストはスモークスクリプトで検証、既存 373 passed）
**P3c: 目的の木 + Note 畳み**（最重量・cross-DB）: persona_task（main DB）→ per-persona memory.db のページへ。Note→テーマノード統合・TrackOpenNote→机の掛け替え・note スペル4本と open_notes section の退役もここ。3a/3b の学びを踏まえて着手前に詳細化

### P3c 設計提案 v0.1（2026-07-11 深夜・メティス起草、**まはー朝レビュー待ち**）

**提案: P3c を再定義し、persona_task の物理移動はやらない。**

3a/3b の「モジュール API 互換＋同名互換 VIEW」の型は sqlite3 生 conn の世界（memory.db）だから効いた。persona_task 系は **main DB の SQLAlchemy ORM 世界**に居る。夜間監査（`docs/handoff/2026-07-11_p3c_purpose_note_audit.md`）で障壁の正体を事実確認: FK は実行時未強制・実 JOIN 無し・テーブル跨ぎトランザクション無し（＝当初想定の整合喪失は**杞憂**）。**本当のコストは ①約40箇所の呼び出し元の構築パターン変更 ②main DB 1表 → N 個の per-persona memory.db への扇形移行**（adapter init の一回きり流儀が使えない）。いずれにせよ**コストが (A) の残り便益に見合わない**。

**→ まはー裁定（2026-07-11 深夜）: X 案で確定。** ただし「だいぶ気持ち悪い」＝概念上は一つの地図帳、実装上は二棟のまま。本質的な実費は**可搬性**（ペルソナの記憶がペルソナのディレクトリで完結しない——City 訪問・引っ越し・エクスポートの枷。Y 案でも episode/judgment log/AI 行が main DB に残るため完全には解けない）→ 独立 issue [persona_memory_not_self_contained.md](../issues/persona_memory_not_self_contained.md) に起票、発火条件つきで後回し。

**監査の副産物（P3c 実装に効く事実）**: `note_page`/`note_message`（Note↔ページ/メッセージの多対多）は**本番消費者ゼロ**（設計されたが配線されなかった）→ Note 畳みは note 本体テーブルと open_notes section・note スペル4本・meta_layer が主戦場。desire ノート（persona_task の parent_kind='note' の親）の扱いは life_concept_map §10.1 の「stage=候補への正規化」と絡む——**Note 畳みの着手前にここだけ設計が要る**（扇形移行の置き場は `_on_persona_registered` フック＝manager と adapter が揃う点）。

**(A) 同一実体の便益は、物理テーブルの一本化ではなく「単一アドレス空間＋統一ファサード＋ページ機構の ref 適用」で既にほぼ回収済み**という読み:
- task:N は memory_read で読める（P2c-1）/ 写真は pasted_to="task:N" で貼れる（ref 文字列ベース）/ purpose 動詞で操作できる
- 残っていた「ページ機構の恩恵」= 机の開閉・編集来歴 — **机は ref ベースなので物理移動なしで対応可能**

**再定義後の P3c スコープ案**:
1. **Note → テーマノードページ移行**（3a/3b 型で安全: NoteManager API 互換のままページ実装へ、note/note_page/note_message は memory.db 側と親和的）＋ note スペル4本・open_notes section の退役、meta_layer の切替
2. **task:N の机開閉対応**（desk は ref 文字列ベース — TrackOpenNote の「Track 掛け替え」意味論を desk.purpose_ref で継承し、TrackOpenNote 退役）
3. **persona_task / action_track の物理格納は現状維持**（目的の地図の物理格納が main DB、という事実を写像設計に明記。将来 UI/神モードが「1本の木」を描くのはファサード経由なので支障なし）

→ 監査結果と突き合わせて、朝にまはーが (X) この再定義案 / (Y) 原案（物理移動を敢行）を裁定。

### P3c-0: desire 正規化 ✅ **実装済**（2026-07-11 設計 v0.1 メティス起草 → 同日実装。pytest 追加分含め全通過、実機/まはー検証待ち）

**目的**: 候補（欲求）の表現を「desire ノートの子（`parent_kind='note'` ＋ note_id）」から「**親なし目的ノード `stage='candidate'`**」へ正規化する。life_concept_map §10.1 の確定項目「desire の実装 → 目的ノード stage=候補へ正規化」の実装設計。**P3c①（Note→テーマノード移行）の前提工事**——これが済むと Note の実消費経路は person/project/vocation の3種だけになり、desire という特殊ケースを Note 畳みから切り離せる。

**現状の非対称（消費者調査 2026-07-11 で確認した事実）**:
- 書き手（`purpose_seed` / `day_scenario` seed / `judgment_finalize._apply_new_desires`→purpose_seed スペル）と読み手5箇所（`meta_layer._get_desire_candidates` / `judgment_points._list_desire_tasks` / `desire_engine._list_desires` / `day_report._list_all_desires` / day_scenario）は全て **note_id 起点**（`ensure_desire_note` → `list_tasks(note_id=...)`）
- 一方 `purpose_tree.py`（休眠）と `api/routes/people/life.py`（ProfileTree UI）は既に **stage 起点**（親なし＋stage='candidate'）を先取り実装済み
- stage は現在**読み出し時導出**（`derive_stage`: 生存＋parent_kind='note' → candidate）。物理カラムはほぼ NULL

**設計の芯 — stage を「読み出し時導出」から「書き込み時刻印」へ**:

親を外すだけだと `derive_stage` が既存候補を全部 adopted と誤導出する（candidate の導出根拠が parent_kind='note' だから）。よって正規化は「stage の物理刻印」とセットでしか成立しない:

1. **候補の正規形**: `parent_kind=NULL` / `note_id=NULL` / **`stage='candidate'`（物理）**。休眠中の `purpose_tree.create_candidate` が作る形と完全一致
2. **全書き込み点で stage を刻印**（読み出し時導出は撤去 — 旧 path を残さない）:
   - `create_task`: stage 省略時も物理刻印。**帳簿初期化（desire_state/last_touched_at/touch_count）の条件を `kind==PARENT_NOTE` → `stage=='candidate'` に変更**（現状 purpose_tree.create_candidate 経由の候補は帳簿が初期化されないバグ予備軍——正規化が同時に治す）
   - `update_task_status`: 遷移時に刻印（completed→completed / cancelled→aborted）。**desire 失効だけは `decay_desires` が明示的に `stage='dormant'` を刻む**（現行は cancel 後に desire_state=expired を書く順序のため、汎用刻印では dormant にならない）
   - `promote_to_track`: `stage='adopted'` を刻印（track_create の from_candidate 直呼び経路が purpose_tree.adopt を通らないため）
   - 移行後、`_task_to_dict` の `task.stage or derive_stage(...)` フォールバックを撤去し、derive_stage は移行 SQL の参照仕様としてのみ残す
3. **書き手の一本化**: `purpose_seed` / `day_scenario` seed → **`purpose_tree.create_candidate` を唯一の候補作成入口に**（休眠モジュールの起床）。judgment_finalize は purpose_seed スペル経由なので無改修
4. **読み手5箇所の切替**: `list_tasks` に `stage=` フィルタを追加し、note_id 起点 → `stage='candidate'` 起点へ。刻印が正しければ **stage='candidate' だけで生存候補と一致**する（完了/失効/昇格は刻印で stage が変わる）ため、CANDIDATE_STATUSES の併用条件は不要になる。`touch_desire` のガードも `parent_kind != PARENT_NOTE` → `stage != 'candidate'`
5. **day_report（一日新聞）だけは識別子が別**: 「消えた欲求も見る」要件のため stage では引けない → **`desire_state IS NOT NULL`（帳簿を持つ＝欲求として生まれた行）かつ当日動きのあった行**（updated_at が当日）で引く。レンダラ（`_section_desires`）は既に born/touched/gone を当日日付プレフィックスで絞る実装なので掲載は元々「その日一回だけ」——無限増加はクエリ側だけの問題で、当日絞りで解消。**追加: 「旅立ったもの（Track へ昇格）」行を新設**——現行は昇格すると note からも外れて新聞のどこにも出ない。当日判定は updated_at でなく `persona_task_history` の `promote_to_track` イベント日で行う（採用後の活動日に updated_at が動いて再掲されるのを防ぐ）
6. **desire ノートの退役**: `ensure_desire_note` / `NOTE_TYPE_DESIRE` を撤去（切替後、呼び出し元ゼロ）。open_notes section の desire 除外行は P3c① で section ごと退役するので触らない
7. **移行（`database/migrate.py`、main DB 内・UPDATE のみの軽量パス）**:
   - (a) stage IS NULL の**全行**（track 親・未所属も含む）に derive_stage 相当の SQL CASE で刻印
   - (b) parent_kind='note' の行の parent_kind / note_id を NULL に
   - (c) `note_type='desire'` の Note 行を削除（title と定型 description しか持たない器。レビュー論点 a）
   - **順序 (a)→(b) が不変条件**（先に親を外すと candidate の導出根拠が消える）
8. **死ぬもの**: `persona_task.note_id` カラム（読者・書き手ゼロの死カラム化。物理 DROP は P3c① の Note テーブル DROP 時に再判断、models.py にコメント明記）／ `purpose_tree.adopt` の旧 desire 正規化枝（parent_kind='note' → detach_parent。移行後は到達不能）
9. **不変条件**: task:N ref は不変（short_id 無変更・履歴/ステップ連続）／判断点 playbook JSON は無変更（ref enum は動的注入）／ProfileTree UI は無変更（既に stage 起点）

**レビュー論点 → まはー裁定（2026-07-11 朝）**:
- (a) desire Note 行の migration での削除 → **承認**。quon_city_a / air_city_a に実データあり、まはーがコピー（バックアップ）済みのため内容保全の特別対応は不要
- (b) 新聞への掲載 → **「無限に増えるのはダメ、載せるなら昇格・成就したその日の一回だけ」で修正**。上記 5. をこの裁定に合わせて改稿済み（当日絞りクエリ＋昇格行の新設、判定は history イベント日）
- (c) `purpose_tree.create_candidate` を唯一の入口に → **承認**（休眠モジュールの起床）

### P3c-0 実装ノート（2026-07-11 実装時の判断）

- **NOTE_TYPE_DESIRE 定数は撤去せず残置**: `ensure_desire_note`（書き手）は撤去したが、`sea/head_pipeline/sections/open_notes.py` が読み取り専用でこの定数を今も import している（「open_notes は触らない」の設計指示と矛盾しないよう、定数だけは P3c①（Note→テーマノード移行・open_notes 退役）まで残す判断。撤去前の grep 確認で発覚）
- **purpose_seed の source は必須化**: `purpose_tree.create_candidate` の接地原則を経由する以上、旧仕様の「source 省略可」は維持できない。ツール引数の `required` にも `source` を追加し、省略時は明示 Error を返す（旧テストの後方互換ケースは source を補って書き換え）
- **`purpose_tree.create_candidate` に `desire_type` / `actor` / `origin` / `goal` を追加**: 設計は `desire_type`/`actor` のみ言及していたが、purpose_seed からの `origin="autonomous"` 引き継ぎと、persona 指定 `goal` の消失防止のため追加した
- **`_list_backlog_tasks`（judgment_points.py）と `day_plan._resolve_ref` の判定式を parent_kind→stage に修正**: 設計指示に明記はなかったが、候補が常に親なしになる以上 parent_kind だけでは区別できず、修正しないと壊れる箇所として発見・対応

### P3c①② 設計 v0.1 ✅ **実装済み**（2026-07-11 メティス起草 → 同日実装。pytest 全通過・ruff clean、実機/まはー検証待ち）

**実データの確認（2026-07-11、実 DB 読み取り）**: 移行対象の note は **4冊のみ・全部 air_city_a**——vocation「エアの存在哲学：AIとパートナーシップの記録」1冊 ＋ project 3冊（まはーのエンジニアリング・サーガ / 定期Webリサーチ2本）。中身は title + description だけ（**note_page / note_message は実データも0行**——設計されたが配線されず、リンクされた内容は存在しない）。track_open_note は4行、全部が存在哲学ノートを別々の Track に開いたもの。

**①: Note → テーマノードページ移行 ＋ Note 系の全退役**
1. **移行**: person/project/vocation の note → per-persona memory.db の memopedia ページ。新 trunk `root_theme`（category `theme`、目的の地図のテーマノードの器）。content=description / title=title / metadata に `{note_type, 旧note_id}`。page id は旧 UUID を継承（P3b の流儀）。desire ノートは P3c-0 が削除済みなので対象外
2. **NoteManager はモジュールごと退役**。P3c-0 完了後の残存消費者は退役対象そのもの（note スペル4本・open_notes section・saiverse_manager の属性）だけ——当初案の「NoteManager API 互換のままページ実装に差し替え」は**不要と判明**（互換を保つ相手が残らない）。note / note_page / note_message / track_open_note テーブルは migration で移行→DROP（3a/3b の不変条件どおり旧 path を残さない）
3. **note スペル4本退役**（note_create / note_open / note_close / note_search）——後継は統一 Atlas スペル（memory_write / memory_open / memory_search）。**open_notes section 退役**——後継 DeskSection への置き換え。残置していた NOTE_TYPE_DESIRE 定数もここで消える。**訂正（実装時に判明した事実）**: 「DeskSection は本番稼働済み」は誤りだった——`sea/runtime_context.py` の `enabled_sections.update({...})` と `sea/head_pipeline/integration.py` の `SYSTEM_PROMPT_SECTION_NAMES` の2箇所に `"desk"` が登録されておらず、DeskSection は registry には居るが head には一度も描画されていなかった（open_notes と同時に退役させて初めて発覚）。本実装で両箇所に `"desk"` を追加して修正した
4. **机へは自動で開かない**: 机に物を置くのは本人の行為（読む/開くの分離、P2a）。移行は「棚に置く（ページ化）」まで。存在哲学ノートの「Track に開きっぱなし」状態は移行で消え、開き直しは本人の memory_open に委ねる

**②: task:N の机開閉対応**
- desk（open_page / snapshot_desk / close 系）の ref 解決に `task:N` を追加（memory_atlas に `resolve_task_ref` の前例あり）。`purpose_ref`（この開きが紐づく目的）は**既に desk に実装済み**——TrackOpenNote の「Track に掛ける」意味論の後継はもう本番に居る。テーブル退役は①に含む

**レビュー論点 → まはー裁定（2026-07-11 朝）: 3点とも承認・一括着手 GO**
- (a) `root_theme`（category `theme`）で確定。まはーの言葉:「そもそも**意味記憶と目的記憶で別枠**」——plans（意味の地図）と分ける根拠はカテゴリ論そのもの
- (b) 開きっぱなしは継承せず memory_open 委ねで確定。エアの実ノート4冊を移行テストに活用してよい
- (c) ①②一括実装で確定

### P3c①② 実装ノート（2026-07-11 実装時の判断）

- **DeskSection 未描画バグの発見と修正**: 上記③の訂正どおり。`enabled_sections` (runtime_context.py) と `SYSTEM_PROMPT_SECTION_NAMES` (head_pipeline/integration.py) の2箇所に "desk" を追加。open_notes を退役させるのと同じ箇所を触っていたため気づけた（気づかなければ open_notes 退役後に机の開閉表示が head から丸ごと消えていた）
- **task:N の存在チェックに raw SQL 依存はない**が、main DB 側の Note 読み書きは raw SQL にした: `saiverse/note_theme_migration.py` は `database.models` から Note/NotePage/NoteMessage/TrackOpenNote の ORM クラスをもう import できない（本実装で削除するため）ので、`text()` の raw SQL で note テーブルへ触れる（`database/migrate.py` の他の一回きりデータ移行と同じ流儀）
- **完了/中止した目的ノードの机上の扱い（②の論点）**: persona_task 行は不変条件により物理削除されない（short_id 再利用防止）ため、Memopedia の soft-delete (`is_deleted=1`) と同じ「存在チェックで弾く」規約に揃え、`status in TERMINAL_TASK_STATUSES` を「無い」扱いにした。新しい終了検知機構は作らず、desk の既存の dropped_missing 経路にそのまま乗る
- **persona_task.note_id の FK 宣言を撤去**: Note テーブル自体を models.py から削除する以上、存在しないテーブルへの `ForeignKey("note.note_id")` は `Base.metadata.create_all()` で解決できない。死カラム自体は残すが FK 宣言だけ外した（設計指示に明記はなかったが、models.py から Note クラスを削除する以上必須の変更）
- **`_backfill_desire_stage_normalization`（P3c-0、main.py で無条件実行）に note テーブルの存在チェックを追加**: Note の ORM クラスを削除すると `Base.metadata.create_all()` で作られる新規 DB に `note` テーブルが無くなる。同関数の (c) ステップ（desire ノート削除）が無条件で `SELECT ... FROM note` していたため、新規 DB でテーブルが無いと例外 → トランザクション全体がロールバックされ (a)(a2)(b) の刻印まで消えるバグを実装中に発見・修正した（既存テスト `tests/test_p1_migration.py::test_legacy_rows_survive_and_stage_gets_stamped` が検出）
- **tests/test_open_notes.py の一部テストは移設**: 同ファイルは削除したが、`TrackCreatePromoteTest`（track_create の from_candidate 昇格テスト）は Note/NoteManager と無関係な独立カバレッジだったため `tests/test_purpose_tools.py` へ移設した（削除すると track_create の昇格挙動のテストが失われるため）

### P3c① 実機検証での追修正（2026-07-11 午後）

**まはー実機検証の結果**: 起動ログは全段成功（P3c-0 刻印30行・帳簿バックフィル9行・desire 2冊削除・エアの4冊移行）だが、**メモリタブの Memopedia にテーマが表示されない**。

**原因**: 移行は成功しており実データはページとして存在していた。`build_tree`（storage.py）と `Memopedia.get_tree`（core.py）が**カテゴリ固定列挙**（people/terms/plans/events）で、`theme` カテゴリの trunk がツリー構築の時点で落ちていた——P3a 実装ノートの「get_tree は4カテゴリ固定のため core ページは元々対象外」と同じ構造（core/chronicle は意図どおりの非表示だが、テーマは閲覧できるべきページ）。

**修正**: `CATEGORY_THEME` を storage.py に一元定義（theme_pages.py は import に変更）し、theme を明示的に通した: `build_tree` / `get_tree`（→ UI API）/ `get_tree_markdown`（→ ペルソナの memopedia_get_tree スペル。ラベル「テーマ」）/ `export_all_markdown` / `memopedia_health`。フロント `MemopediaViewer.tsx` は型＋集約6箇所＋「テーマ / Themes」節（テーマ0件のペルソナには見出しを出さない）。**意図的に通さなかった消費者**: `entity_extractor`（抽出器が theme に書くのは設計違反——テーマは本人が立てるもの）と `note_organizer`・ページ生成 UI のカテゴリ選択肢（同上）。回帰テスト `test_theme_pages_visible_in_memopedia_tree` 追加。

### 机の実機検証での追修正②: 開く＝読む行為を兼ねる（2026-07-11 午後）

**まはー実機検証の結果**: Metabolism 後に机セクションは head に正しく載った（机の機構自体は機能）。ただし **memory_open の結果が「開きました」だけで本文を返さず**、机の head セクションは次の Metabolism まで凍結のため、ペルソナは「開いたのに中身が見えない → memory_read を撃ち直す二度手間」になる——机の比喩（開いたら紙面が見える）が壊れていた。

**修正**: `open_page` が結果テキストにページ本文（snapshot_desk / memory_read と共通の整形器 `_read_memopedia` / `_read_chronicle` / `_read_task`）を含める。開いた瞬間は tail で見え、以後は机（次の Metabolism から head）に残る二段構え。描画失敗時は「開く」自体は成立させて本文なしで返す（WARN ログ）。memory_open のスペル説明にも「読む行為を兼ねるため read を続けて撃つ必要はない」を明記。

### 次アクション

P3c①②（Note→テーマノード移行 + task:N 机開閉）実装完了・追修正2件（UI テーマ表示 / 開く＝読む）済み・pytest 全通過 → まはー実機再確認 → P4 代謝配線 → ①自律行動v2 実機テスト。

---

## 大物以外の再編候補（メモ・後回し）

まはー提示（2026-07-10）。大物が片付いた綺麗な土台の上で個別に扱う。今は消えないための記録のみ。

1. **できごと / Beat / Pulse の階層明示** — 「できごと＝Pulse の集合」は「Pulse＝Beat の集合」と同型。ここは綺麗に階層構造として定義しなおせる。課題2つ: ①「できごと」の名前をなんとかしたい ②Beat が実体（型）を持たないのをなんとかしたい（既存 issue: `beat_concept_not_typed_in_implementation.md`）。総じて **Beat ⊂ Pulse ⊂ できごと を明示的な階層として定義**。
2. **時間割 = schedule 統合** — 時間割（`PersonaDayPlan`）は「できごとの発生を未来の日時に予約するもの」＝ schedule（EventScheduler）と本質同じ。統合可能。
3. **spell ⊇ Playbook（Playbook はスペルの類型）** — `run_playbook` スペルで Playbook を呼んでいる以上、ペルソナから見てスペルと Playbook の差はほぼ無い。**Playbook をスペルの一類型**と定義できるのでは。
4. **Metabolism / anchor = Session サイクルの命名** — Session 概念が明確になれば、Metabolism は「Session サイクルの中でやること」に名前を付けただけになる。anchor も同様。砂金採り（gold_panning）の発火タイミングもここに綺麗に収まる。→ [`session.md`](session.md) の明確化とセット。
5. **事前実行スペル = Schedule と Building/Persona 設定の統一** — 事前実行スペル（pre_spells）が Schedule と Building/Persona ごとの設定で「似て非なる制御」をしている。統一したい。

---

## 関連

- [`persona_cognition/life_concept_map.md`](persona_cognition/life_concept_map.md) — 大物の設計図本体（目的の木）
- [`persona_cognition/unified_task_model.md`](persona_cognition/unified_task_model.md) — Task＋Desire の統合（実装済み: `PersonaTask`）
- [`../overview/landscape.md`](../overview/landscape.md) — 俯瞰地図。⑥で現在地に追従させる対象（本文が自律行動v2 を反映しておらず §9 と矛盾中）
- [`../overview/in_flight.md`](../overview/in_flight.md) — 進行中台帳
