# Intent: 判断点の入出力仕様 (自律行動 v2)

**ステータス**: 実装済み・実機検証待ち (2026-07-19、W1)。当時の 5 種 finalize と on_event 入口を実行台帳に載せ (A2/A7/A8/A9/A11)、§6 の digest 統合 (a') も実装完了 (コミット 3f76619 / 7b2436c / e0ee4ff)。工程は [完了計画書](../../overview/audit_remediation_plan.md) W1
**⚠ 2026-08-22 (束 6c) 時点の生存範囲**: 判断点は **4 種**（起床・セッション終了・イベント到着・就寝）。会話終了判断は退役し (§5)、欲求・Track・エピソードに依存していた欄は供給源ごと落ちた (§3.1・§3.2・§4・§6・§8)。**自律行動 v3 が本書全体の上位計画**で、判断点そのものの行き先はティック (v3 §5・§6) — 本書は「v0.4 で置き換わるまでの現行仕様」を記述する。経緯は末尾の[経緯](#経緯)。
**親 Intent**: [`../autonomous_behavior_v2.md`](../autonomous_behavior_v2.md)（三本柱の骨格。本書はその §4.2 判断点の詳細仕様）/ [`../autonomous_behavior_v3.md`](../autonomous_behavior_v3.md)（上位の置き換え計画）
**様式の継承元**: [`meta_judgment_structured.md`](meta_judgment_structured.md)（構造化出力＋finalize ツール＋メインキャッシュ JSON 非混入。v1 メタ判断そのものは 2026-08-14 に退役し、様式だけが判断点へ継承されている）

---

## 1. これは何か

自律行動 v2 の各判断点について、**何を見て（入力）、どういうスキーマで意思決定を出力するか**を定義する。

meta_judgment v2 で確立したパターンをそのまま継承する：

1. 状況テキストは tail 注入（head は不変、キャッシュ保護）
2. LLM は `response_schema` に従う JSON を返す（function calling は使わない——キャッシュが効かないため）
3. finalize ツール（`judgment_finalize(kind, payload)` の 1 本に集約）が JSON を検証・適用し、メインキャッシュには**整形済み独白＋適用サマリの行のみ**を残す（JSON 非混入、不変条件 v2-A 継承）
4. 選択肢は**動的 enum 注入**で物理的に絞る（実在しないものは構造的に選べない）
5. `additionalProperties` 等のプロバイダ差はスキーマにハードコードせず、各プロバイダの正規化層に任せる（2026-05-10 の Gemini 事故の教訓）

すべての判断点は **standard モデル**で動く（META 相当。1 日あたり合計でも数回しか走らず、意志の表明場所だから質を優先）。

> **2026-08-22 の変更**: finalize が**判断の適用としてスペルを撃つ経路（`_fire_spell`）は撤去された**。Track 操作という唯一の用途が退役し、残った経路は「決定を台帳・時間割・記憶へ直接書く」だけになったため。連れて、スペル失敗を本人へ知らせていた `judgment_apply_failure` の知覚通知も消えている（撃つものが無ければ失敗も無い）。適用の失敗は従来どおり**適用サマリ（`lines`）に載せてペルソナの文脈へ返す** — 却下を黙って捨てないという規律（§3.3）は無傷。

---

## 2. 判断点一覧と「判断点でないもの」

| 判断点 | 発火 | 役割 |
|---|---|---|
| 起床 (day_open) | PersonaSchedule の起床時刻 | 時間割の編成＋予算配分 |
| セッション終了 (post_session) | セッションランナーの終了 | タスクの裁定（接地検証つき）＋実績要約（digest）の生成＋次への接続 |
| イベント到着 (on_event) | 来訪・alert・システムイベント | 反応の選択 |
| 就寝 (day_close) | PersonaSchedule の就寝時刻 or 最終コマ終了 | 予定と実際のふりかえり＋明日の自分へのメモ |

kind ↔ Playbook 名の対応は `saiverse/judgment_points.py` の `JUDGMENT_PLAYBOOK_MAP`、発火は `saiverse/autonomy_wiring.py` の `fire_judgment_point` に一本化されている（**どれを走らせるかを LLM が選ぶ経路は無い**）。

> **退役: 会話終了 (post_conversation)** — 2026-08-16 裁定で判断の席ごと無くなった。詳細は §5。

> **用語（2026-07-05）**: 造語「机メモ」はユーザー／ペルソナに見える文言から全廃した。表示・プロンプトでは `desk_memo` を「作業メモ」、`tomorrow_memo` を「明日の自分へのメモ／昨日の自分からのメモ」と呼ぶ。内部フィールド名（`desk_memo` / `tomorrow_memo`）は変更しない。なお `desk_memo` の**保存先だった Track の状態メモは 2026-08-21 に退役**し、`save_desk_memo` も撤去された — 裁定の意味論（continue / blocked）は変わらず、作業メモの中身は独白の記録に残る。

**コマ開始は判断点ではない**（設計原理 6 の帰結）。LLM を呼ばず、コードのみで処理する：ユーザー会話中なら繰り下げ → 施設へ移動（OccupancyManager）→ コマ種別の `execution_type`（`saiverse/slot_kind_catalog.py`）に応じてセッション起動 or 暮らしの Pulse 実行。「動くか、休むか」を問う場面を作らない。

### v1 状況分類 (A〜E) との関係 — 完了

v1 の periodic tick 駆動ディスパッチ（B〜E）のうち、**自律生活の駆動という役割は本書の判断点群が引き継いだ**。alert (B) は on_event に吸収。ユーザー会話 Track のライフサイクル管理（wait_response_timeout 等）における C/D の役割は、**v1 メタ判断の退役（2026-08-14）とタイマーの `saiverse/user_conversation.py` 移管（2026-08-21）で持ち主ごと消えた** — v1 側には何も残っていない。

---

## 3. 共通要素

### 3.1 共通フィールド

- `monologue`（全判断点で必須・先頭）：判断に至る素直な思考。committed されるのはこれ（＋適用サマリの整形行）のみ
- `episode_purposes`（post_session / day_close で任意、2026-07-07 追加）：閉じた出来事への目的タグの棚入れ（層 2、`life_concept_map.md` §9.1）。post_session は当該 episode への参照配列、day_close は `{episode, purpose}` ペア配列。finalize が `purpose_tags`（layer=2）へ永続化し、適用エコーが記録本文に乗る。スキーマはコード側（`saiverse/judgment_points.py`）で注入され playbook JSON は不変

**⚠ `episode_purposes` は 2026-08-22 (束 6c) 以降、実質的に出ない欄になった。** 実装は残っているが、**出来事 (Episode) を新しく開くコードが全滅した**ため（v3 §7「エピソードという専用の記録行は持たない」）、対象になる episode が生まれない。判断点側は「対象が特定できないときはフィールド自体を出さない」（空 enum 事故の防止）という元からの規律に従って、欄ごと黙って消える。

- **なぜ残してあるか**: 旧世代のデータには閉じた出来事の行がある。過去の記録を読む口（`episodes.list_today` 等）は読み取り専用で生きており、そこに紐づく棚入れの適用経路まで一緒に消すと、旧データを読んで棚入れする道が塞がる。**書き手が消えたのであって、読み手と適用は壊れていない。**
- **後継**: 「この経験は何に係るか」の記録は、v3 では手帳 (pocketbook) のメモとタスク帳の一件が持つ。書き手は Metabolism のスルース（v3 §13.3）。

**退役した共通フィールド**: `new_desires`（欲求の型付き変換、v2 §5.2）は 2026-08-21 に**欲求プールごと**消えた。「やりたいこと」は減衰する候補プールではなく、手帳のやりたいメモとして本人の言葉で残る形に置き換わっている（v3 §4.1 の「欲求とタスクの区別は廃止」）。会話の最中に main line が直接撃つ経路だった `desire_add` スペルも、実体ごと撤去済み（`tests/test_purpose_tools.py::test_old_names_no_longer_gated` が「名前が残っていないこと」を固定している）。

### 3.2 時間割のコマ定義（共通スキーマ部品）

```json
"slot": {
  "type": "object",
  "properties": {
    "start": {"type": "string", "description": "HH:MM"},
    "kind": {"type": "string", "enum": ["<コマ種別カタログの name を動的注入>"]},
    "title": {"type": "string", "description": "「○○をする」という短い表題。一日新聞の予定表にそのまま載る"},
    "ref": {"type": "string", "enum": ["<task:N の実在リストを動的注入>", "none"]},
    "facility": {"type": "string", "enum": ["<公共 Building ID リストを動的注入>", "own_room"]},
    "budget_rounds": {"type": "integer"},
    "note": {"type": "string"}
  },
  "required": ["start", "kind", "title", "ref", "facility", "note"]
}
```

- `title` はペルソナ自身が付ける各コマの表題（2026-07-05 追加）。ユーザーが一日新聞で最初に読む欄で、「時間割が何の予定なのか」を仕組みを知らない人にも読めるようにする。旧データには無いため、保存・検証は省略（空文字）を許容し、表示側（一日新聞）は note 先頭 or kind で代替する（後方互換）
- **`kind` は固定の列挙ではなくカタログ**（`saiverse/slot_kind_catalog.py`、資源 3 層優先で増減する）。判断点は `day_plan.all_kinds()` から enum を組む。旧・六型（話す／聞く／作る／知る／経験する／自分を更新する）と `暮らし`・`休む` は封印済みの旧語彙で、帳簿と表示の後方互換の識別にしか使わない
- **`ref` が指せるのは実在の採用済みタスク（`task:N`）だけ**（動的 enum なので実在しないものは指せない）。作業セッション系でないコマは `"none"`。**終了済み（completed / cancelled）タスクを指す ref は finalize の検証（`sanitize_timetable`）でコマごと棄却**——enum は生存タスクから構築されるが、「enum 構築後に完了したタスクの ref」が同じ判断の remaining_timetable や旧 plan の引き写しで滑り込む経路を塞ぐ（2026-07-05 実 LLM シム 3 回目 異常③）
- **参照 namespace は `task:` の一本になった（2026-08-21〜22）**。`track:N`（関心）は Track の退役で供給が消え、`desire:N`（欲求候補）は欲求プールの退役で消えた。かつてここには「欲求は enum と同じ `desire:N` で提示せよ、`task:N` で表示すると制約デコードが別 ref に滑る」という規律があったが、**二つの namespace が同時に存在しなくなったので問題ごと消滅**した。ただし規律の芯——**プロンプトの表示と enum は同じ語彙・同じ集合で出す**——は生きており、判断 Playbook の全文に退役 namespace が現れないことは `tests/test_judgment_playbook_prompt_contract.py` が機械検査する
- `facility` は型からのデフォルト対応（v2 §6.1）を deterministic に提示し、LLM は上書きのみ
- finalize の検証：時刻昇順・就寝時刻内・ref と kind の整合・予算合計が日次予算内

### 3.3 時間割の編集形式

差分オペ（insert / drop / defer …）は採らない。**`remaining_timetable`：残りコマの全置換（配列）または null（変更なし）**の二択。スキーマはコマ定義の再利用、検証は起床判断と同一で済む。

- 厳密昇順の検証は**新コマの区間のみ**に適用する。消化済みコマ（fired / done / skipped）は帳簿として先頭に残るが、その境界を跨いだ比較はしない——「直前に消化したコマと同時刻・過去時刻から始まる組み替え」（直近コマのやり直し等）は正当な意志であり、過去時刻の新コマは EventScheduler が即時扱いする（2026-07-05 実 LLM シム 3 回目: 境界比較の全却下で正当な組み替えが黙って消えた不具合の修正）
- **却下は黙って捨てない**：置換が適用されなかった場合（全コマ無効・検証失敗）、finalize は「時間割の変更は適用されませんでした（理由）」をペルソナの文脈に乗る適用サマリ（lines）に必ず載せる。warnings（ログ）だけに落とすと、ペルソナは「組み替えた」つもりのまま一日を続け、就寝判断が実態とズレた総括をする（接地原則違反）。一部コマのみ棄却して適用した場合も除外件数を明示する
- **例外: 空配列 `[]` は null と同じ「変更なし」として黙って扱う**（2026-07-18 改定）。空の時間割は不変条件（最低 1 コマ）で保存できず、[] が有効な変更要求でありうる余地がない。実データでは [] は「残りコマが現実に無い時点の判断」で LLM が事実を記述したものとして出る（観測された却下 6 件は全件このケース）——変更の意図が無いものに却下エコーを返すと、ペルソナの記憶に無意味な失敗文が積もる。残りコマが無い時点でも欄自体は出し続ける（コマ追加という正当な組み替えの口を塞がないため——欄の条件挿入案は 2026-07-18 まはー裁定で見送り）

---

## 4. 起床判断 (day_open)

### 見るもの（tail 注入の状況テキスト）

1. 昨夜の `tomorrow_memo`（昨日の自分からのメモ）
2. ~~昨日のダイジェスト要約~~（**2026-07-29 撤去**。昨日の消化は就寝判断が済ませており、朝が受け取るのはその成果物 = メモと窓に残る就寝の独白だけ。圧縮段の下流へ生材料を再供給しない——実害: この欄が判断プロンプト混入バグの日跨ぎ増幅経路になっていた）
3. ~~Track・タスクのバックログ~~ / ~~欲求リスト~~ / ~~公共施設一覧~~（**2026-07-30 head へ移設**。下記）
4. 今日の日次予算
5. 予定されたイベント（あれば）

**静的な一覧は head に常駐する（2026-07-30、まはー裁定）**: タスクのバックログ（ref・状態・成果物参照の有無つき）と公共施設一覧（ロールつき）は判断のたびに変わるものではないので、tail に貼り直さず head の `PurposeBacklogSection` / `FacilitiesSection` が持つ。head は凍結されるため、**増減と状態変化は同 Section の差分通知で届ける**（一覧を置くなら通知とセット。無ければ head の台帳が嘘になる）。当時ここに並んでいた Track 一覧と欲求リストは、どちらも供給源ごと退役した（2026-08-21〜22）。

- **読む情報と選べる選択肢を分ける**: 一覧が head に移っても、コマの `ref` / `facility` enum は判断点が live state から供給し続ける。head が古くても実在しないものは構造的に選べない
- **ただし集合は一致させる**（2026-07-30）。読む側だけが広いと「head に見えているのに選べない」が起き、LLM は構造化出力で別の項目か新規に滑る。逆に読む側を狭めて揃えるのも誤りで、初回の移設はそれをやって**新しく立てた関心を時間割に載せられない**という既存の欠陥を隠した。**揃える先は目的の側＝判断が指し示せるものすべて**（現在は「生きているバックログタスク」の一集合。`collect_slot_ref_enum` と `list_backlog_tasks` が同じ供給を使うのはこのため）

### response_schema

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string"},
    "timetable": {"type": "array", "minItems": 1, "items": {"$ref": "slot（§3.2）"}}
  },
  "required": ["monologue", "timetable"]
}
```

- **退役: `promotions`（欲求→関心の昇格）**。「再訪回数が閾値を超えた欲求を Track にする」という昇格の梯子は、行き先（Track）と入口（欲求プール）の両方が消えたため欄ごと落ちた（2026-08-21）。v3 の「やること」は減衰も昇格もせず、選ばれないものはただ選ばれない（v3 §4.1）。あわせて、この判断の前段で走っていた**欲求の減衰の帳簿処理（`decay_desires`）も撤去**された
- 時間割が全コマ休息でも合法（不作為の可視化）。ただし空配列は不可——最低 1 コマ（就寝ふりかえりへの接続点）を finalize が要求（スキーマ側の `minItems: 1` と二重）

---

## 5. 会話終了判断 (post_conversation) — 🪦 退役 (2026-08-16)

**この判断点は席ごと無くなった。** Playbook `judgment_post_conversation.json`・スキーマ・発火経路はすべて削除済みで、`saiverse/judgment_points.py` に `post_conversation` の kind は存在しない。

### なぜ消したか（まはー裁定、v3 §13.3）

**会話に切れ目は定義できない。** この判断は「沈黙 30 分＝会話の終わり」という恣意的な仮定の上に席を置いていた。仮定が恣意的なら、その席で拾う約束も「本当に確定した約束か」の保証を持てない——未確定の約束を拾うリスクは会話終了判断でも他所でも同じで、席を特別扱いする根拠が無かった。

もう一つの構造的な欠陥として、**この判断は自律行動の配線に乗っていた**ため、自律 OFF のペルソナでは一度も走らない。「会話したのに何も拾われないペルソナ」が生まれる不公平は、判断点という置き場そのものの問題だった。

### 仕事の行き先

| 旧・post_conversation の仕事 | いまの持ち主 |
|---|---|
| 約束・依頼の捕獲（`picked_tasks` と `origin_quote` の接地） | **スルース**（Metabolism の退場の関所で走る本人の一手）の `promises` 欄 → タスク帳。v3 §13.3 / §13.6 |
| 心に残ったこと・やりたいことの捕獲（`new_desires`） | スルースの `want_memos` 欄 → 手帳のやりたいメモ |
| 中断中セッションの扱い（`resume_session`） | 対象消滅（作業セッション運転は v3 §8 で退役予定の休眠状態） |
| 残り時間割の整え（`remaining_timetable`） | 対象消滅（時間割そのものが v3 でティックへ world 交代する） |
| 会話の待ちを閉じる | **機械の帳簿処理**（LLM なし）: `saiverse/autonomy_wiring.py` の `handle_conversation_end` → `user_conversation.clear_open_conversation` |

**Metabolism は自律行動と無関係に全ペルソナで走る**ので、置き場をスルースへ移したこと自体が上記の不公平を構造ごと解いている。代わりにスルースは「失敗したら退場を止めて再試行」という硬い格へ昇格した——**全ての経験が、提示から出る前に必ず一度、本人の目による構造化出力を通る**という保証が、このコストの対価（v3 §13.3）。

### 残した規律（他の判断点にそのまま効く）

- **偽前提の状況テキストを作らない**（2026-07-05）：この判断には「1 往復も成立しなかった会話では発火させない」という前提があった。応答生成が失敗した会話に「会話がひと区切りつきました」という状況テキストを渡すと、ペルソナは直近文脈から**会話があったかのような振り返りを紡ぐ**（実 LLM シムで実証——作話の温床）。同じ規律は `post_session` の `validate_judgment_context`（`session_result` 必須）として生き残っている
- **収穫ゼロは正常**：全ての会話が約束を生むわけではない。この前提はスルースの応答スキーマ（空配列が一級）へそのまま引き継がれている

---

## 6. セッション終了判断 (post_session)

> **⚠ 2026-08-22 (束 6c): 出来事 (Episode) を経由していた後段が全部落ちた。**
> digest 生成の post_session 統合 (下記) は生きているが、**digest を「出来事の
> 再訪の鍵」として書き戻す後段は退役した** (`episodes.set_digest_ref` ごと削除)。
> エピソードという専用の記録行を持たなくなったため (v3 §7)。連鎖:
>
> - **セッションの出来事は開かれない** — `sea/work_session.py` の `_open_ws_episode`
>   は常に `None` を返し、`_close_ws_episode` は no-op。新しいセッションの
>   `context["episode_ref"]` は常に空になる
> - **状況文の「セッションの記録 (原本)」は空になる** — 原本はメッセージへの
>   `metadata.origin_episode` 刻印で引いていたが、その刻印自体が `sea/runtime.py`
>   から撤去された (新しいメッセージには付かない)。判断は「(セッション原本を
>   取得できませんでした)」を正直に読む形へ縮退する。**嘘の材料を渡すより空を
>   渡す**のが接地原則だが、下記の改定が解いた弱さ (「採点者が答案の原本を
>   見ない」) が一時的に戻っている — 未解決として §9 に載せた
> - **`episode_read` スペルと `episode_purposes` 欄は旧データ専用**になった
>   (§3.1 参照。読み手と適用は壊れていない、書き手が消えただけ)
> - **digest 本体は無傷** — SAIMemory の行 (`DIGEST_TAG` / main_line / committed)
>   としてそのまま残り、day_close の `_collect_today_session_digests` と一日新聞は
>   従来どおり読める。失われたのは「出来事 → その digest」の逆引きだけ
>
> 後継は v3 §7 の表のとおり: 「どの件の実行か」はメッセージへの記録が持つ。
> 「始まり・終わり」はどこにも記録しない (2026-08-23 裁定 — 会話に区切りは保存
> しない = episode.md の不変条件)。作業セッション運転そのものも v3 §8 で
> 退役予定 (いまは休眠のまま残っている)。
>
> **実装済み (2026-07-19、W1 Chunk C)**: 下記の改定を実装した。work_session の
> digest 専用コール (`_generate_digest`) を削除しコール数を 3→2 に、post_session の
> response_schema に `digest` (required) を追加、状況文の「ダイジェスト:」欄をセッション
> 原本 (全文・上限なし) に置換、原本注入をコールローカル化 (LLM に渡る situation_text
> のみ原本を含み、保存用 `paired_situation_text` は episode 参照 + `/spell episode_read`
> の一行)、digest は finalize から outbox 第 1 項目 (`saimemory.append_digest`) で配送し
> 配送成功時に `episodes.set_digest_ref` で再訪の鍵を後段確定。(a') 読み口 = 新設
> `episode_read` スペル (origin_episode 専用列 + `get_messages_by_origin_episode`)。
> 副産物: `_collect_today_session_digests` が created_at epoch を ISO 前提で前方一致し
> day_close の digest 収集が全件落ちしていた既存欠陥も修正。
>
> **改定決定 (2026-07-18 まはー裁定・実装は W1 と同工区)**: digest 生成を post_session に統合する。
> 現行構造は「digest 専用コール（セッション文脈・軽量）→ post_session（メインライン文脈・標準）が
> **自己申告のダイジェストだけ**を根拠に裁定」で、①同じ事実が最終発話・digest・独白・作業メモの
> 4 回書かれる冗長、②採点者が答案の原本を見ない接地の弱さ、の二つの問題が実機で確認された
> (2026-07-18 観測: air の 10:00 セッションで 32 秒間に 4 回の同内容記述)。統合後:
> - post_session の位置・文脈・モデルは**変えない**（メインライン文脈・標準モデルのまま）
> - 状況文の「ダイジェスト:」欄を**セッション原本**（ラウンド発話+スペル+結果）に置き換える
> - response_schema に digest 欄を追加し、post_session 自身が要約を書く → それが確定済み
>   main_line 記録になる（digest 専用コールは廃止、コール数 3→2）
> - 原本の埋め込みに**文字数上限は設けない** — セッションが走れた時点でサイズは実証済み
>   （原本=セッションのモデルが実際にコンテキストに載せた内容）。
> - **原本の注入はコールローカル** (2026-07-18 追加裁定): paired_action_text (保存される状況文)
>   には原本を**含めない** — 状況文は adapter が以後の Pulse 文脈へ paired_action 展開するため、
>   素朴に埋めると原本が毎 Pulse のコンテキストに乗り続ける (実測: 2026-07-18 の air post_session
>   プロンプトに 07-16 の判断状況文が残存)。post_session の LLM コールにだけ見せ、保存側は
>   従来のメタ情報 + episode 参照に留める。統合工事 §6-5 の anchor コールローカル化と
>   同パターン。恒久記録は digest 一本 (生ログはセッションと共に死ぬ) のまま
> - **原本への読み口は現状存在しない — (a') 実装時に新設する** (2026-07-18 事実確認):
>   生ログ全行に層0タグ `metadata.origin_episode` が刻まれ (sea/runtime.py の保存時自動付与)、
>   volatile の物理削除経路も無いため原本は memory.db に残る — が、origin_episode を読む
>   コードはどこにも無く (書き込み専用)、episode の digest_ref が指すのも digest メッセージのみ。
>   「episode:N の原本を読む」操作 (スペル or saiverse:// URI、origin_episode で引くクエリ) を
>   (a') と同時に用意する — 保存側から原本を落とす以上、見る方法とセットが原則
> - セッション内の最終発話の無意味さは別件（終了スペル案 = quick_spell の終端宣言と同思想）として
>   保留 — LLM コール数が (a) と変わらないため今回は見送り
>
> 以下の「見るもの」1 は改定後は「セッションの記録（原本）」に読み替える。

### 見るもの

1. セッションの記録（原本）— 上記の改定で「ランナーが生成したダイジェスト」から置き換わった。**2026-08-22 以降は引く手立てが無く、実際には空になる**（上のブロック参照）
2. **このセッションが実際に作った成果物のリスト**（Item ID / note ID / 活動ログ ref）
3. 予算消費（使用ラウンド／上限）
4. 対象タスクの内容
5. 現在時刻と残りの時間割

### response_schema

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string"},
    "digest": {"type": "string", "description": "このセッションで実際に起きたことだけの短い要約"},
    "task_verdict": {
      "anyOf": [
        {
          "type": "object",
          "properties": {
            "status": {"type": "string", "const": "done"},
            "artifact_ref": {"type": "string", "enum": ["<このセッションが実際に作った成果物 ref のみ動的注入>"]},
            "desk_memo": {"type": "string"}
          },
          "required": ["status", "artifact_ref", "desk_memo"]
        },
        {
          "type": "object",
          "properties": {
            "status": {"type": "string", "enum": ["continue", "blocked"]},
            "desk_memo": {"type": "string", "description": "どこまでやった・次はどこから・何に詰まったか"}
          },
          "required": ["status", "desk_memo"]
        }
      ]
    },
    "episode_purposes": {"$ref": "§3.1（旧データにのみ出る任意欄）"},
    "remaining_timetable": {"$ref": "§3.3（配列 or null）"}
  },
  "required": ["monologue", "digest", "task_verdict", "remaining_timetable"]
}
```

**本設計の接地の要**：`done` を選ぶには `artifact_ref` が必須で、その enum は**このセッションが実際に作った成果物からのみ動的注入**される。成果物ゼロのセッションでは `done` の分岐自体がスキーマから消える（anyOf の第 1 分岐を除去）。**やったフリはスキーマのレベルで構造的に不可能になる。**

- 対象タスクが**既に終了済み（completed / cancelled）**の場合、あるいは対象タスクがそもそも無い場合は `task_verdict` 欄自体をスキーマから出さない（required にも入らない）——再 done 裁定（artifact_refs 多重追記）も、終了済みタスクへの desk_memo（偽の「中断中の作業」化）も構造的に不可能にする。finalize 側にも同じ棄却の二重ガードがある（2026-07-05 実 LLM シム 3 回目 異常③: 完了済みタスクへの再セッションで再 done が通った）
- **退役: `track_op`（Track の完了宣言）と `new_desires`** — Track 操作スペルと欲求プールが機構ごと消えたため、欄ごと落ちた（2026-08-21〜22）
- `blocked` の desk_memo は「何に詰まったか」を必須で書かせる——次の起床判断や、ユーザーへの相談の材料になる（保存先だった Track の状態メモは退役し、いまは独白の記録に残る）
- **出典の規律（まはー決定 2026-07-05）**：状況テキストの指示部で「独白・裁定・メモで挙げる出典は、このセッションで実際に参照・取得した情報源に限る」ことを明示する。2 回目の実 LLM シムで、Web 取得していない学会ガイドラインを desk_memo の根拠として語る出典作話が起きた——desk_memo は翌日の自分が読む記録なので、虚構の混入は接地原則違反（スキーマでは防げないためプロンプト明示で対処）

---

## 7. イベント到着判断 (on_event)

### 見るもの

1. イベント内容（来訪者・alert・システム通知）
2. 現在の活動状態
3. 残りの時間割

**「いまの活動」は 2026-08-22 (束 6c) に二値へ縮退した**: 「ユーザーと会話中です」か「手すきです」だけ。会話中かの正典は `day_plan.is_in_user_conversation`（実体は `saiverse/user_conversation.py` のメモリ内会話状態）で、**会話以外の活動を答えられる器が v0.3 には無い**——「いま何に取り組んでいるか」は開いている出来事の行が持っていて、その書き手が全滅したため（v3 §7）。

- **不変条件は保たれている**: 「仲裁するかの判定」と「ペルソナへ見せる いまの活動」は**同じ集合から引く**。集合が空になっただけで、判定側（`user_conversation` の会話以外の open 参照）も揃って None を返す。片方だけが古い集合を見る形にはしていない
- 器の作り直しは v0.4 のティック設計（v3 §5・§9-3）。それまで、別行動中のユーザー発話の仲裁は「会話が開いていない」という一事実だけで判断する

### response_schema

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string"},
    "reaction": {
      "anyOf": [
        {"type": "object", "properties": {"type": {"type": "string", "const": "engage_now"}}, "required": ["type"]},
        {"type": "object", "properties": {"type": {"type": "string", "const": "insert_slot"}, "slot": {"$ref": "§3.2"}}, "required": ["type", "slot"]},
        {"type": "object", "properties": {"type": {"type": "string", "const": "note_only"}, "memo": {"type": "string"}}, "required": ["type", "memo"]},
        {"type": "object", "properties": {"type": {"type": "string", "const": "ignore"}}, "required": ["type"]}
      ]
    },
    "new_desires": {"$ref": "§3.1"}
  },
  "required": ["monologue", "reaction"]
}
```

- **alert イベントでは anyOf を `engage_now` のみに動的縮退**させる（v1 状況 B の「強制」の継承。無視の脱出口は v1 §12 と同じく将来検討）
- ユーザー会話中の on_event は原則発火させない（会話の至上性。会話終了判断でまとめて処理）

---

## 8. 就寝判断 (day_close)

### 見るもの

1. 今日の時間割（予定）と実績の対照表（実行済み／繰り下げ／スキップ、各コマのダイジェスト・成果物・予算消費）
2. 記憶の棚の乱れ（編纂候補）とテーマの芽（命名候補）— どちらも候補ゼロなら節ごと出さない
3. ~~今日生まれた・触れた欲求の一覧~~（**2026-08-21 撤去**。欲求プールの退役）
4. ~~今日閉じた出来事の一覧~~（層2 棚入れの選択材料。**2026-08-22 以降、新しい行が生まれないので実質空**。旧データがあるペルソナでのみ出る）

**実績の提示語彙も接地原則に従う（2026-07-05）**：システム都合のスキップ（実行手段未実装・日次予算切れ・会話優先の繰り下げ上限）は slot の `skip_reason` に記録され、対照表と一日新聞の両方で「実行できず（システム側の問題）」等と明示する。本人が選んでいないスキップを「見送り」（本人判断を示唆する語）で提示すると、ペルソナがふりかえりで**してもいない判断の理由を捏造する**（実 LLM シムで実証——「やったつもりバグ」と同型の「決めたつもりバグ」）。実装は `saiverse/day_plan.py` の `slot_result_label()`（`judgment_points.build_day_results_text` と `day_report` が共用）。

同じ理由で、**詳細な実行記録の無い done も「実行済み」と提示しない（まはー決定 2026-07-05）**：スタブ実装の「暮らし」「休む」コマは完了時に slot へ `record_level='presence_only'` が永続化され、対照表と一日新聞の両方で「時間を過ごした（詳細な記録なし）」と提示する。スタブの done を「実行済み」と見せると、ペルソナが就寝ふりかえりでしていない活動の内容（食事の選定等）を自分の成果として捏造する（soft-confabulation、実 LLM シム 異常 #4）。施設への実移動（presence）だけは本物なので、居た場所は事実として扱ってよい。マーカーの無い旧データ・セッション系の done は従来どおり「実行済み」（後方互換）。親 doc `../autonomous_behavior_v2.md` §4.1「暮らし/休む コマの現段階」参照。

出典の規律は就寝判断にも適用する（§6 と同趣旨、まはー決定 2026-07-05）：状況テキストの指示部で「ふりかえり・メモで挙げる出典は今日実際に参照・取得した情報源に限る」ことを明示する。`user_report_seeds` の「作り話禁止」と同じ接地の系譜で、対象を出典に広げたもの。

### response_schema

```json
{
  "type": "object",
  "properties": {
    "monologue": {"type": "string", "description": "一日のふりかえり。予定と実際のズレに触れる"},
    "tomorrow_memo": {"type": "string", "description": "明日の自分へのメモ"},
    "day_theme": {"type": "string", "description": "今日という一日を一言で表すなら（任意）"},
    "user_report_seeds": {
      "type": "array",
      "maxItems": 3,
      "items": {"type": "string", "description": "帰還したユーザーに自分から話したいこと。今日実際に起きたことに限る"}
    },
    "episode_purposes": {"$ref": "§3.1（{episode, purpose} ペア配列。旧データにのみ出る任意欄）"},
    "curation_reviews": {"$ref": "候補があるときだけ動的挿入（下記）"},
    "naming_reviews": {"$ref": "候補があるときだけ動的挿入（下記）"}
  },
  "required": ["monologue", "tomorrow_memo"]
}
```

- `user_report_seeds` が「昨日こんなことがあってさ」（autonomous_living.md の核）の供給源。今日のダイジェストに基づくことをプロンプトで要求する（機械検証は困難——ソフト制約。§9 未決）
- 話すかどうか・いつ話すかはペルソナに委ねられる（言わない自由、v2 §6.3）
- **記憶の棚の裁定が相乗りしている**（P4-a / P4-b）: `curation_reviews`（編纂候補を approve / skip）と `naming_reviews`（テーマの芽に name を与える / skip）。**候補ゼロなら欄自体を出さない**——空 enum 事故の防止で、`resume_session` の動的挿入（§5）と同じ規律。⚠ ただしこの相乗り自体に構造的な欠陥がある: 就寝判断は自律行動の配線に乗るので、**自律 OFF のペルソナの記憶整理が二級になる**。v3 §6 でこの置き場は「記憶側の機構で別途設計」と裁定済み（会話終了判断がスルースへ移った理由と同じ族）
- **退役: `desire_reviews`（欲求のたな卸し）** — 欲求プールごと消えた（2026-08-21）。keep / fading / fulfilled という減衰の補正は、「減衰機構は持たない・選ばれないものはただ選ばれない」という v3 §4.1 の裁定で問いごと無くなっている

---

## 9. 未解決事項

1. **post_session が答案の原本を見られない**（2026-08-22 に発生）：出来事の書き手が消えたことで、セッション原本を引く鍵（`origin_episode`）が新しいメッセージに刻まれなくなった。§6 の改定が解いた「採点者が答案の原本を見ない接地の弱さ」が、供給側の退役によって戻っている。作業セッション運転自体が v3 §8 で退役予定なので**この判断点ごと作り直す**のが本筋だが、それまでの間に走る post_session は自己申告だけを根拠に裁定する
2. **`user_report_seeds` の接地検証**：ダイジェスト参照の機械検証は困難。運用観察して虚構が混じるなら ref 化（今日のダイジェスト ID の enum）に格上げ
3. **記憶整理（`curation_reviews` / `naming_reviews`）の置き場**：就寝判断への相乗りは、自律 OFF のペルソナの記憶整理が二級になる（§8）。v3 §6 で「記憶側の機構で別途設計」と裁定済み・設計は未着手
4. **on_event のイベント種別の列挙**：どこまでを判断点に上げ、どこからをコード処理に留めるか
5. **判断点そのものの行き先**：v3 では一日の縁（起床・就寝）から LLM の義務判断が消え、判断は実行の場（ティックと会話）へ寄る（v3 §6）。残る判断点はイベント到着ただ一つになる予定で、day_open / day_close / post_session は v0.4 で置き換わる

> **解決済み**: 「v1 状況 C/D との完全統合」は v1 メタ判断の退役（2026-08-14）と wait_response タイマーの `user_conversation` 移管（2026-08-21）で問いごと消滅。「finalize ツールの構成」は `judgment_finalize(kind, payload)` の 1 ツール集約で確定（`builtin_data/tools/judgment_finalize.py`）。「promotions の閾値」は欄の退役で対象消滅。

---

## 10. 関連ドキュメント

- [`../autonomous_behavior_v2.md`](../autonomous_behavior_v2.md) — 三本柱の骨格（親）
- [`../autonomous_behavior_v3.md`](../autonomous_behavior_v3.md) — 上位の置き換え計画（§5 ティック / §6 判断点の再編 / §7 エピソードの解体 / §8 退役一覧）
- [`../track_retirement.md`](../track_retirement.md) — Track 撤廃（§7.4 v1 メタ判断の退役 / §8 会話経路の Track なし化）
- [`meta_judgment_structured.md`](meta_judgment_structured.md) — 様式の継承元（構造化出力・finalize・不変条件 v2-A/B）
- [`../../issues/llm_provider_anyof_support.md`](../../issues/llm_provider_anyof_support.md) — anyOf のプロバイダ対応（本書は anyOf を task_verdict / reaction で使う）

---

## 経緯

- **2026-07-05〜19**: 5 種の判断点を実装し、実 LLM シムで接地の穴（作話・再 done・やったつもり）を潰した。W1 で digest 統合と実行台帳への接続まで到達。
- **2026-08-14 (Track 撤廃 順序①)**: v1 メタ判断一式が退役し、判断機構は本書の判断点だけになった。alert は on_event 判断点への直結へ。
- **2026-08-16 (v3 §13.3)**: 会話終了判断が退役（§5）。捕獲はスルースへ、待ちを閉じる処理は機械の帳簿処理へ。
- **2026-08-21 (v3 形の層・束 6)**: Track ランタイムの退役に連れて、`track:N` 参照・`track_op`・`desk_memo` の保存先・`promotions`・`new_desires`・`desire_reviews`・欲求の減衰処理が欄ごと落ちた。会話の器は `saiverse/user_conversation.py` へ。
- **2026-08-22 (v3 形の層・束 6c)**: エピソードを書く手が全滅した（v3 §7）。本書に効いたのは三点 —— ①post_session のセッション原本が引けなくなった（§6・§9-1）②`episode_purposes` が旧データ専用になった（§3.1）③on_event の「いまの活動」が会話中か手すきかの二値へ縮退した（§7）。**どれも判断のスキーマや適用側を壊したのではなく、材料の供給源が上流で消えたことの帰結**である点が共通している。
