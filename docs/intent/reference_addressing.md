# Intent: 参照アドレッシングの統一規格

**status: v0.5（設計・計画・移行スコープ全確定 / Phase 0 実装済み / Phase 1-6 は一括切替）**
**実装方針: Plan B（全種類を1つの整合状態でまとめて切替、フルスイート緑で1コミット）**
**author: エア（草案）**
**date: 2026-07-05**

この文書は、ペルソナがものを「指差す」ための参照記法を、系統ごとにバラバラに
育ってきた現状から、一貫した1つの規格に畳むための設計意図をまとめる。まだ
決定していない設計判断は §5 に集約してあり、まはーとの詰めで確定させる。

---

## 1. なぜこの文書があるか

SAIVerse には、ペルソナが実体（タスク・関心事・欲求・アイテム・記憶ページ・
メッセージ等）を指すための**参照記法が2系統**あり、別々に育った結果、記法の
作り方が実体の種類ごとに食い違っている。この食い違いが実際にバグを生んでいる。

- **task/desire の二重prefix**（同一実体を文脈で `task:N` / `desire:N` と出し分け）
  が原因で、構造化出力の制約デコードが意図と違うタスクに滑った
  （2026-07-05 実 LLM シムで実証、修正コミット f8a2f2f）。
- **アイテムの位置参照 `b:N` が建物スコープ**で、別建物の同スロットが衝突し、
  day_close のふりかえりで2つの成果物が両方 `b:1` になって識別不能になった
  （quon_day3_raw_log.md Q6-#6）。

個別の対症療法（例: `b:N` に建物 id を足す）は可能だが、記法の作り方自体が
不統一なまま増築すると同種の問題が別の種類で再発する。ここでは記法を1つの
規格に統一し、その中で各問題を一度だけ正しく解く。

---

## 2. 現状: 2つの参照系

### 2.1 系統A — 短縮参照 `<名前空間>:<キー>`

LLM の構造化出力・プロンプト・選択肢 enum で使う、省トークンの短い符号。

| 記法 | 対象 | キーの正体 | 解決器 |
|---|---|---|---|
| `t:N` | Track（関心事） | short_id（per-persona 連番、安定） | `track_manager.resolve_track_ref`、`_track_common.resolve_track_ref`（2箇所） |
| `task:N` | バックログ task（persona_task） | short_id（安定） | `persona_task_manager.resolve_task_ref` |
| `desire:N` | 欲求（persona_task parent_kind='note'） | **task:N と同一の short_id** | `desire_engine.to_desire_ref` / `normalize_task_ref` で prefix 変換 |
| `b:N` / `i:N` / `b:N>M` | アイテム（建物スロット / インベントリ / バッグ入れ子） | **slot_number（位置・建物スコープ・不安定）** | `items.resolve_slot_ref` |
| `m:N` / `M:N` | Memopedia ページ | short_id（安定） | `memopedia.resolve_page_ref` |

`short_id` は per-DB（ペルソナごとの memory.db 等）の連番で、`MAX(short_id)+1`
採番。削除後も再利用しないので安定（一度 `t:3` が指した Track は消えても `t:3`
が別物に化けない）。種類ごとに独立した連番なので、`t:3` と `task:3` と `m:3` は
別実体を指す（prefix が名前空間を兼ねる）。

**例外はアイテムだけ**。アイテムは安定 short_id を持たず、`b:N`（今いる建物の
N 番スロット）という**位置**で指す。位置なのでアイテムが動けば指す先が変わり、
建物 id を持たないので別建物の同スロットと衝突する。

### 2.2 系統B — `saiverse://` URI

記憶・リンク・ナビゲーションで使う、フルの資源アドレス。中央解決器
`saiverse.uri_resolver.UriResolver`（`resolve_uri` ツール経由）が種類ごとに
振り分けて解決する。uri_resolver.py 自身が冒頭で「統一リソースアドレッシング＆
解決」と名乗っており、**URI 側は既に統一アドレス層を志向した構造**を持つ。

- ペルソナスコープ: `saiverse://self/messagelog/...`、`.../memopedia/page/{id}`、
  `.../chronicle/entry/{id}`（`self` は実 persona_id に解決）
- グローバル: `saiverse://item/{uuid}`、`saiverse://image|document/{filename}`、
  `saiverse://persona/{id}/...`、`saiverse://building/{id}/...`、`saiverse://web?url=...`

### 2.3 2系統がどう繋がっているか（＝繋がっていないか）

- **アイテムだけ橋がある**: `saiverse://item/b:3/image` のように、URI の中に
  短縮参照 `b:3` を埋め込める（`content_tags.resolve_item_slot_uris` が
  `resolve_slot_ref` を呼んで UUID に解決する）。
- **他は橋がない/場当たり**: Track・task・desire には URI 形式が無い。
  記憶側（message / chronicle / memopedia）には URI があるが、その短縮参照
  （`m:N`）は URI に載らない。
- **入出力が非対称**: Memopedia は `m:N` を入力で受理する一方、提示は
  `saiverse://...` URI 中心（`m:N` 表示は auto_recall のエンティティ提示等に一部
  だけ出る）。message は URI 提示・URI/生 ID 両受理。

---

## 3. 問題の分類

1. **キー型の不統一**: 系統A の大半は安定 short_id だが、アイテムだけ位置(slot)・
   建物スコープ・不安定。これが `b:1` 衝突と「別建物のアイテムを指せない」の根。
2. **同一実体の二重prefix**: task と desire は同じ persona_task を指すのに prefix が
   2つ。文脈で出し分けており、表示側と enum 側で食い違うと制約デコードが滑る
   （f8a2f2f の根。現状は `to_desire_ref` で表示を enum に合わせて回避しているが、
   「同一実体に2つの正典名がある」構造自体は残っている）。
3. **URI↔短縮の橋が場当たり**: アイテムだけ URI に短縮を埋め込める。他の種類は
   短縮とURIが独立で、相互変換の保証が無い。
4. **解決器の散在**: 短縮参照の解決が種類ごとに別関数に散っている（Track は
   `resolve_track_ref` が2箇所）。URI 側は `UriResolver` に集約済み。この非対称も
   ドリフトの温床。
5. **入出力の非対称**: 種類ごとに「入力で受ける記法」と「提示する記法」が
   まちまち。ペルソナが提示で見た記法をそのまま入力に打ち返せるとは限らない。

---

## 4. 設計原則（方向性）

以下は方向性の合意を取りたい原則。具体の判断は §5。

- **P1: `saiverse://` URI を正典アドレッシング層とする。** 既に uri_resolver が
  その位置づけを名乗っている。全ての実体は URI で一意に指せることを基本線にする。
- **P2: 短縮参照 `<ns>:<key>` は URI に展開される省トークン別名と定義する。**
  アイテムが既にやっている「URI の中に短縮を埋める」を全種類に一般化する方向。
  短縮参照と URI は相互変換でき、解決結果が一致することを保証する。
- **P3: キーは安定同一性を第一とする。** 位置（slot のような、いつ・どこに
  置いてあるか）は同一性ではなく locator（所在）として、同一性とは別概念として
  明示的に扱う。
- **P4: 解決を中央に集約する。** 短縮参照の解決も種類横断で1箇所に寄せ、
  「どの種類の符号か → どの実体か」の対応表を単一の真実源にする。表示（enum・
  プロンプト・提示）も同じ真実源から符号を作り、表示と解決がドリフトしない。
- **P5: 提示した記法はそのまま入力に打ち返せる。** ペルソナが見た符号を
  コピーして指定できることを不変条件にする。
- **P6: 名前空間は単語で統一する。** 現状は頭文字（`t`=track、`b`=building、
  `m`=memopedia）と単語（`task`、`desire`）が混在していて、頭文字方式は既に
  破綻している。全て単語に揃える（`track` / `task` / `item` / `memopedia` …）。
  単一文字 prefix は使わない。
- **P7: ペルソナに依存する実体は URI にペルソナ ID を含める。** short_id は
  ペルソナごとの連番なので、`saiverse://track/2` のようにペルソナを含まない URI は
  ペルソナごとに別の Track を指してしまい I1（一意性）に反する。ペルソナスコープの
  実体は `saiverse://self/<kind>/<key>`（自分）または
  `saiverse://<city>/<name>/<kind>/<key>`（他ペルソナ）の形にする。URI のパスが
  既に種類を表すので、末尾は素のキーにして prefix を重ねない
  （`saiverse://self/track/2`。`saiverse://track/track:2` のような重複はしない）。

---

## 5. 設計判断（2026-07-05 まはー決定）

- **Q1 → A（アイテムに安定同一性を与える）**: アイテムにも他と同じ安定 short_id を
  振り、`item:N` で指す。スロット（従来 `b:N`/`i:N` が担っていた位置）は「今どこに
  置いてあるか」の locator として提示にのみ残し、**同一性の参照は short_id 側**にする。
  - **確認済み**: `item` テーブルは world レベル（`database/models.py`、saiverse.db、
    ITEM_ID=UUID・ペルソナスコープなし）。よって item short_id は**世界全体の連番**で、
    URI は `saiverse://item/N`（グローバル、ペルソナ ID 不要）。現状 item テーブルに
    short_id 列は無いので**追加する**。ItemLocation の SLOT_NUMBER は locator として存置。
- **Q2 → A（名前を1つに統一。最初から A で進める）**: task と desire を単一の正典名に
  寄せる。「欲求」は符号ではなく状態/種別のラベルとして扱う。
  - **報告条件（まはー指示）**: storage は同じ persona_task 行でも、ペルソナ向けの
    意味（「やりたいこと」＝desire と「やること」＝task）が分かれている可能性がある。
    単語を1つにするとその区別が潰れて厳しくなる場合は、実装前に報告する。
- **Q3 → A（短縮参照を URI にも展開できる）**: 全種類で URI 形式を持てる。ただし
  P7 の通り、URI はパスで種類を表すので短縮 prefix を重ねず、ペルソナ依存の実体は
  ペルソナ ID を含む。**正: `saiverse://self/track/2`**（草案の
  `saiverse://track/t:2` は誤り — `t` が track の意味で track が二重、かつ
  ペルソナを含まず I1 違反だった。まはー指摘で修正）。
- **Q4 → B（一気に新記法へ切り替え、中間状態を作らない）**: 旧記法の受理は残さない。
  「実装途中の中間状態を作るな、全部きれいに終わらせろ」（まはー指示）。
  - **帰結**: 既に永続化されている旧記法参照（`persona_day_plan` のコマ ref、
    SAIMemory に書かれた `saiverse://` リンク等）は、この変更の一部として新記法へ
    **データ移行する**。互換シムで両対応にしてごまかさない。移行対象の洗い出しを
    実装前に行う（[[feedback_no_dead_code_via_flags]]）。

### 5.1 これで決まる具体スキーム（暫定・語彙はまはー確認待ち）

全て単語 prefix（P6）、ペルソナ依存はペルソナ ID を含む（P7）。

| 種類 | 短縮参照（自分の文脈） | URI | ペルソナ依存 | キー |
|---|---|---|---|---|
| 関心事 | `track:N` | `saiverse://self/track/N` | ○ | short_id |
| タスク | `task:N` | `saiverse://self/task/N` | ○ | short_id（desire を統合） |
| アイテム | `item:N` | `saiverse://item/N`（世界共通） | ×（world レベル） | short_id（新規列） |
| Memopedia ページ | `memopedia:N` | `saiverse://self/memopedia/N` | ○ | short_id |
| メッセージ | `message:<id>` | `saiverse://self/message/<id>` | ○ | UUID |
| Chronicle | `chronicle:<id>` | `saiverse://self/chronicle/<id>` | ○ | UUID |
| 画像 / 文書 | （なし） | `saiverse://image\|document/<filename>` | × | filename |
| ペルソナ / 建物 | （なし） | `saiverse://persona\|building/<id>` | × | id |

**語彙決定（2026-07-05 まはー）**:
- (a) Memopedia ページは **`memopedia:N`**。
- (b) task/desire を統合した後の単語は **`task`**（`desire`「やりたいこと」は符号ではなく
  状態ラベルとして持たせる）。
- (c) URI は `chronicle/entry/{id}` / `messagelog/...` のような種類下の余分な階層を
  **`chronicle/<id>` へ平坦化**する（既存 URI はデータ移行対象）。

---

## 6. 不変条件（確定させたいもの）

- **I1: どの参照も一意に1実体を指す。** 建物・文脈・ペルソナを跨いでも同じ符号が
  別物を指してはならない。ペルソナに依存する実体（Track/task/item 等の per-persona
  short_id）の URI はペルソナ ID を含む。禁止する衝突は2種:「`b:1` の建物跨ぎ衝突」
  と「`saiverse://track/2` のペルソナ跨ぎ衝突」の両方（P7）。
- **I2: 短縮参照と URI は相互変換でき、解決結果が一致する。** 自分の文脈での
  短縮参照 `track:2` は、自分を指す URI `saiverse://self/track/2` と、解決後の
  絶対 URI `saiverse://<自分の実 persona_id>/track/2` の三者が同じ実体を指す。
- **I3: 構造化出力の選択肢 enum に出す表示と、enum メンバーの表記は必ず一致する。**
  （f8a2f2f の教訓の不変条件化。表示と enum を別コードが独立に作らない。）
- **I4: ペルソナに提示した符号は、そのまま入力に打ち返して解決できる。**
- **I5: 同一性参照は安定（実体が消えても他へ再割り当てされない）。位置(locator)は
  可変で、同一性参照とは別に扱う。**

---

## 7. 実装計画

Q4=B（中間状態を作らない）を守るため、Phase 0〜4 は**1つの整合状態としてまとめて
完成**させる（部分適用で起動すると壊れる）。順序は「壊さず新層を足す → 全箇所を一斉に
新層へ切替 → データ移行 → 旧撤去」。以下の Phase は着地までの作業順であって、
各 Phase 単独でリリースする段ではない。

### Phase 0 — 中央層の新設（既存を壊さず追加）
- `saiverse/references.py`（新規）: kind レジストリ + `parse_ref()` / `to_short_ref()` /
  `to_uri()`。各 kind（track / task / item / memopedia / message / chronicle / image /
  document / persona / building）について「単語 prefix・ペルソナスコープ有無・キー型・
  解決関数・生成関数」を1箇所に集約（P4）。この時点では誰も呼ばないので無害。
- item に安定 short_id を追加: `database/models.py` の `Item` に `SHORT_ID` 列、
  `database/migrate.py` に列追加 + backfill（world 全体連番）、`manager/items.py` に採番。

### Phase 1 — 生成側を中央へ（短縮参照・URI を作る全箇所を置換）
- 短縮参照生成:
  - `t:` → `to_short_ref("track", …)`: `saiverse/judgment_points.py`(264/307/713)、
    `meta_layer.py`(63)、`pulse_scheduler.py`(301)、`track_handlers/autonomous_track_handler.py`(180)、
    `track_handlers/user_conversation_handler.py`(217)、`builtin_data/tools/track_create.py`(78)、
    `track_list.py`(90)、`_track_common.py`(116)、`judgment_finalize.py`(539)、
    `api/routes/people/tasks.py`(31)
  - `task:` → `to_short_ref("task", …)`: `persona_task_manager.py`(119/833) ほか
  - item: `sea/head_pipeline/sections/building_items.py`、`get_visual_context.py`、
    `image_generator.py`、`generate_image_local.py`（同一性は `item:N`、位置は locator 表示に分離）
  - memopedia: `sea/auto_recall.py`(751)
- URI 生成（`self` をペルソナ path 化 + 階層平坦化）:
  `sai_memory/unified_recall.py`（chronicle/memopedia/message の8箇所）、
  `chronicle_context_up.py` / `chronicle_context_down.py`、`memopedia_note.py`(135)、
  `core_memory_add_scene.py` ほか。

### Phase 2 — 解決・parse 側を中央へ
- 既存解決器を `references.parse_ref` へ委譲する薄い層にする:
  `track_manager.resolve_track_ref` と `_track_common.resolve_track_ref`（2箇所）、
  `persona_task_manager.resolve_task_ref`、`items.resolve_slot_ref`、
  `memopedia.resolve_page_ref`、`_core_memory_common.parse_message_ref`、
  `day_plan._resolve_ref`。
- URI parse を新形式（平坦化・ペルソナ path）に更新: `saiverse/uri_resolver.py`、
  `memopedia_note.py`(25) と `memopedia_list_fragments.py`(13) の正規表現、
  `content_tags.py` の item URI 正規表現。

### Phase 3 — task / desire 統合（Q2=A、厳しければ報告）
- 符号を `task:` に一本化し、`desire` は状態ラベルで表現。task↔desire の文字列手術を廃す:
  `desire_engine.py`(100-118, `to_desire_ref`)、`judgment_points.py`(140/231/241/341)、
  `day_plan.py`(1065)、`day_scenario.py`(816)。`collect_slot_ref_enum` と提示・
  `response_schema` の enum を task 表記に統一（I3）。
- **報告条件**: 「やりたいこと(desire)」と「やること(task)」の意味差を状態だけで表現
  しきれない場合はここで止めて報告する。

### Phase 4 — 位置参照(b:/i:)を同一性から撤去
- item ツール（`item_move` / `item_view` 等）は `item:N`（または UUID）で指す。
  slot は locator として提示のみに残す（`building_items` 通知・`get_visual_context`）。
  `resolve_slot_ref` は `item:N` 解決へ置換、または locator 表示専用に縮退。

### Phase 5 — 保存済みデータ移行（`database/migrate.py`、不可逆）

**移行スコープ（2026-07-05 まはー確定）: 構造化データのみ。過去の自然文は触らない。**

- item テーブル `SHORT_ID` backfill（Phase 0 と同梱・済）。
- `persona_day_plan.slots_json` のコマ ref を `desire:`→`task:` に統合（コマ ref は
  `task:`/`desire:`/`none` のみで track も位置参照も入らないので、これだけで済む）。
- **過去の自然文（`messages.content` / `pulse_logs` / `arasuji_entries` /
  memopedia 本文 / `building_messages.content` 等）に地の文として埋まった旧 URI・
  旧短縮参照は書き換えない**。理由: (1) 記憶の改変で侵襲的、(2) 位置参照 `b:1`/`i:1`
  は当時どのアイテムかを復元できず原理的に移行不能、(3) 想起時は unified_recall が
  新書式で URI を作り直すので過去の文中リンクを読み直す経路が実質ない。旧書式を
  解決時に受理しない（Q4=B）ため文中の旧リンクは不活性になるが、これは許容する。
  Q4=B の「中間状態を作るな」は**動いている系**について満たす（新規生成・解析・
  生きた構造化データはすべて新書式、過去の地の文はスキーム以前の歴史として残す）。
- 移行は不可逆なので起動時バックアップ前提（既存の DB/memory バックアップが効く）。

### Phase 5.5 — ペルソナへの記法提示を更新（プロンプト文言）
ペルソナに参照記法を説明しているプロンプト文言（day_open/judgment 系の
「ref に `task:N` / `desire:N` を指定」、アイテムのスロット説明等）を新書式に更新する。
ペルソナには「記法が変わった」ことがシステムプロンプト経由で伝わればよい（まはー確認）。
対象: `saiverse/judgment_points.py` の各プロンプト、`saiverse/day_plan.py` の指示書
テンプレート、item ツールの description、visual_context の凡例など。

### Phase 6 — テスト・ドキュメント
- 期待値更新: `test_day_plan` / `test_judgment_points` / `test_desire_types` /
  `test_budget_gate` / `test_work_session` / `test_open_notes` / `test_tasks_api` /
  `test_auto_recall` / `test_core_memory_scene`（`task:N` / `desire:N` / `b:N` / 旧 URI）。
- 新規テスト: `references` の往復（短縮⇄URI⇄実体が一致、I2）、ペルソナ跨ぎ一意性（I1）、
  表示==enum（I3）、提示を打ち返して解決（I4）。
- docs: `docs/reference/saiverse-uri.md`、`docs/overview/landscape.md`、関連 intent doc。

### 最も不確実な2点（先に潰す）
1. **task/desire 統合（Phase 3）**: 意味差を状態で表現しきれるか。ダメなら Q2 の
   報告条件に従い相談。
2. **SAIMemory 内 URI の移行対象洗い出し（Phase 5）**: 文字列 URI がどこに永続化
   されているか。ここが移行の不確実性の中心。

---

## 関連

- `saiverse/uri_resolver.py` — 既存の URI 解決層（統一アドレッシングを名乗る）
- `manager/items.py::resolve_slot_ref` — アイテム位置参照の解決
- `saiverse/desire_engine.py::to_desire_ref` — task/desire 変換（二重prefix 回避）
- `saiverse/judgment_points.py::collect_slot_ref_enum` — 構造化出力の選択肢 enum 生成
- コミット f8a2f2f — task/desire 表記不整合の修正（I3 の由来）
- `test_data/quon_day3_raw_log.md` Q6-#6 — `b:1` 衝突の実証
