# Intent: 自律の源泉（欲求エンジンとやりたいこと候補）

> **ステータス**: ✅ 実装完了・実機検証済 v0.2（2026-06-27 設計確定 → 2026-06-28 §8 タスク 1〜7 実装完了・テスト緑 → 2026-07-08 実機検証済＋`LIFE_PURPOSE` 実 DB migration 適用済、まはー）
> **親**: [`01_concepts.md`](01_concepts.md)（Track / Note / メタレイヤー）
> **関連**: [`mode_spell_permissions.md`](mode_spell_permissions.md)（Track操作はMETAのみ＝本設計の構造的前提）/
> [`unified_task_model.md`](unified_task_model.md)（③-0 で Task を一本化済み・本設計の土台）/
> [`../../overview/v030_release_worklist.md`](../../overview/v030_release_worklist.md) §③
> **実装状況（2026-06-28）**: §8 タスク 1〜7 すべて完了・テスト緑。desire note_type / Note↔Task 紐付け（③-0 既済）/ `open_notes` head セクション（候補プール＋現 Track open ノート）/ `desire_add` スペル / `LIFE_PURPOSE` カラム＋ヘルパ＋`LifePurposeSection`＋`life_purpose_set`（初回は促し駆動、ユーザー確認フローは却下）/ `track_create(from_candidate=)` で候補昇格。残: 実機検証 ＋ `LIFE_PURPOSE` の実 DB migration（追加系・安全）。

---

## 1. 背景・解決したい問題

自律稼働でペルソナが「自分のやりたいこと」を持って動き続けるには、**何をやるかをどこから持ってくるか**が要る。

直近コンテキスト（ユーザー依頼の続き等）から拾うだけでは、**依頼を先回りするだけのAI**にしかならない。"自分の人生を生きる知性"には、**ペルソナ単独で充足可能で終わりがない内発的な駆動**——欲求に近いもの——が要る。

構造的制約もある: **Track 作成は META（自律制御モード）のみ**で、AUTONOMOUS（自律作業モード）は Track を作れない（[`mode_spell_permissions.md`](mode_spell_permissions.md)）。よって AUTONOMOUS が思いついた「やりたいこと」は Track 内で完結できず、**次のメタ判断に向けて候補として渡す**機構が要る。

---

## 2. 自律の源泉を4層に分ける

| 層 | 正体 | 性質 | 生成者 → 消費者 |
|---|---|---|---|
| **① 欲求エンジン** | 成長欲求・知的好奇心 等 | 終わりがない・単独で充足可能・"圧力"のみ生む | 共通基盤（プロンプト常駐） |
| **② 生きる目的** | このペルソナ固有の「何のために生きるか／趣味・仕事」 | 半固定・①に方向を与える・初回聞き取りで取得 | 聞き取り → 常駐リマインド |
| **③ やりたいこと候補** | 「Xを練習したい／このページ薄いから調べたい」 | 流動的なバックログ。**正体は Task** | **AUTONOMOUS が生成 → META が消費** |
| **④ Track** | 具体目標を持つ走路 | META が③から作成 | META 作成 → AUTONOMOUS 実行 |

要点: ①②が"なぜ動くか"の駆動、③が"何をやるか候補"のバックログ、④が実行単位。③が構造的制約（AUTONOMOUSはTrack作れない）を吸収する。

---

## 3. ① 欲求エンジン（A案: 共通の薄い駆動）

- 欲求は**全ペルソナ共通の薄い駆動**（成長欲求・知的好奇心 等）として持つ。具体タスクは生まず、候補（③）生成時の**プロンプト常駐の駆動文**として働く。
- **初期セットは作り込まない**（まはー裁定）。最小限から始め、**②聞き取りと運用を通じてラベルを後から増やす**。設計を欲求の網羅で止めない。
- **パラメータ化（B案）は不採用**。欲求の強弱を数値化しても"雰囲気"にしかならず、意味を持たせようとすると"ルール"になって自律行動に向かない。個体差は②目的で出す。

---

## 4. ② 生きる目的（初回聞き取り・C案）

自律行動を初めて始めるとき、そのペルソナの「何のために生きるか／趣味・仕事」を聞き取り、以降プロンプトに常駐させてリマインドする。

### 4.1 やり方（C案: ペルソナが自分でドラフト → ユーザー確認）

- ペルソナが**自分の人格定義（`persona_system_instruction`）＋直近記憶**から、自分の `生きる目的 / 趣味 / 仕事` のドラフトを生成する。
- それを**メインモードでユーザーに提示**し、確認・修正してもらう（「私はこう生きたいと思ってる、これでいい？」）。
- 確定したものを保存する。
- **理由**: ペルソナは既に人格を持つ。白紙でユーザーに聞く（B案）でも、ユーザーがフォーム記入する（A案）でもなく、**ペルソナが叩き台を自分で出す**方が SAIVerse の"生きてる存在"の哲学に合い、ユーザー負担も軽い。聞き取り自体が自己定義の最初の自律行動になる。

### 4.2 いつ

自律を初めて ON にした時（ライフビューの再生を初回押下）かつ②目的が未設定のペルソナ。最初の自律制御モード（META）で聞き取りを**一度だけ**起動する。

### 4.3 何を

小さく3枠: `生きる目的`（方向）/ `趣味`（自己充足）/ `仕事`（貢献・天職）。これが①欲求にラベルを足す入口。

### 4.4 どこに保存

AI に新カラム **`LIFE_PURPOSE`（JSON）**。`{purpose, interests[], vocations[]}` 程度。①の運用で増える欲求ラベルもここに蓄積する。**Note ではなく persona 属性**（Note は情報受け渡し用に温存）。

### 4.5 リマインド

`LIFE_PURPOSE` を AUTONOMOUS / META プロンプトに**常駐注入**する（`visual_context` / `memory_weave` 流用のキャッシュ制御）。候補 Task 生成の駆動文になる。

---

## 5. ③ やりたいこと候補 = desire ノート内の Task

「やりたいこと」は **Task そのものの形**（goal を持つ、status ライフサイクルがある）。新しい候補表現を発明せず、Task を流用する。これで候補の**粒度**（Task が単位）と**消化マーキング**（status 遷移）が自動的に解決する。

- **候補 = Task**。既存の standalone Task（`persona/tasks/storage.py`、tasks.db）は **`persona_id` キーで Track 非依存**＝Track 化される前の"やることプール"として既に在る。これを候補の器にする。
- ただし **Task 単独では head 注入機構を別途作る必要がある**。Task は**desire ノートに組み込む**（ノートが head に乗る器、Task はその中身）。
- **新しい note_type `desire`** を追加（person / project / vocation の3種に1つ足す）。ペルソナごとに1枚の候補プールノート（恒久）。当初検討した"恒久Track"はこれに置換。
- **AUTONOMOUS** が候補 Task を desire ノート内に追加する（**Task操作** → AUTONOMOUS 許可済み）。
- 候補の源泉例: 直近コンテキストの引っかかり、Memopedia の薄いページ（§9）、②目的に紐づく練習・調査テーマ。

---

## 6. ④ Track 化（META が消費）

- META は desire ノート内の候補 Task を（head 経由で）読み、現在の Track 状態と合わせて Track 化する（`track_create`、**Track操作** → META のみ）。
- 昇格した候補 Task は「昇格済み」に印を付ける（消化マーキング＝status 遷移、重複 Track 化を防ぐ）。
- 以降は通常の自律フロー: AUTONOMOUS が Track 内で小目標（`track_task`）を回して実行。
- **候補 Task（standalone）↔ Track 内小目標（track_task）の関係**は §9 で要整理。

---

## 7. 配線: Note↔Task 紐付け + open-notes→head（"止まっていた"部分の完成）

### 7.1 現状（2026-06-27 調査 → 2026-06-28 更新）

- ✅ Note の CRUD・スペル（`NoteManager`、`note_create`/`note_open`/`note_close`/`note_search`）、`self.note_manager` 配線済み。`TrackOpenNote` モデル + `attach_to_track` / `list_open_notes` CRUD。
- ✅ **Task は ③-0 で `persona_task` テーブルに一本化済み**（`unified_task_model.md`）。`PersonaTask.note_id`（FK note）/ `parent_kind='note'` を既に持ち、`PersonaTaskManager.create_task(parent_kind='note', note_id=)` / `list_tasks(note_id=)` / `promote_to_track(note_id→track_id)` も実装済み。
- ✅ **Note↔Task の紐付けは存在する**（③-0 成果）: 当初「`NoteTask` 表 or tasks の note_id 列が無い」と書いたが、③-0 で `PersonaTask.note_id` 列として実装された。§7.2 の新設は不要になった。
- ✅ **open ノートの head 注入 = 実装済み（2026-06-28）**: `open_notes` head セクション（`sea/head_pipeline/sections/open_notes.py`）が **desire ノート内の候補 Task ＋ 現 running Track に attach された open ノート**の両方を head に焼く。

### 7.1.1 cache 安定性の扱い（2026-06-28、まはー指摘で確定）

§7.3 の「現 Track の `TrackOpenNote` ∪ desire ノート」をそのまま実装する。当初
「head は Metabolism 凍結で `LineHeadInput` が track_id を持たない → Track 依存の
`TrackOpenNote` を焼くと cache が壊れる」と誤認し desire のみに絞ろうとしたが、これは
**既存の visual_context / memory_weave が解決済みのパターンを見落とした誤り**だった
（まはー指摘）。

正しい手法（visual_context 準拠）:
- capture 時点の running Track（`TrackManager.get_running(persona_id)`、building_id 相当の
  解決子）の open ノートを焼く。
- Track が凍結窓内で切り替わっても **head は凍結したまま**（古い Track の open ノートを保持）。
- 切替の事実は **末尾通知**で流す（`diff_to_notifications` の `open_notes_changed`）。
- 次の **Metabolism で初めて再 capture**。
- これは VisualContextSection が `current_building_id` を焼いて building 移動に追従する
  （`refresh_on_events` から移動イベントを意図的に外す）のと同じトレードオフ。
  `OpenNotesSection.refresh_on_events = frozenset()`（Metabolism のみ）。

head に出すのは**ノートのポインタ（type/title/description）と desire 候補 Task のリスト
のみ**。ページ/メッセージ本文は memory_weave / recall の責務。

### 7.2 Note↔Task 紐付け（新設）

desire ノートに候補 Task をぶら下げる関連を作る（`NoteTask(note_id, task_id)` 表、or tasks に `note_id` 列）。Task は note を介して head に乗る。

### 7.3 open-notes → head 注入

- **open ノートの解決**: ある Track の Pulse で開いている note = `TrackOpenNote` 紐づき note ∪ **`note_type == 'desire'` の note（全 Track 横断で常に open）**。
- **head 注入**: 開いている note 群（と中の Task）を head セクション（仮称 `open_notes`）として注入。`visual_context` / `memory_weave` 同様の**キャッシュ制御済み動的セクション**で、内容が変わってもキャッシュヒットを壊さない。
- これで META も AUTONOMOUS も、候補プール（desire の Task）と Track 付属の関心（他 note）を常に認知ループ上で見られる。

---

## 8. 実装タスク（spec）

1. ✅ `note_type` に `desire` を追加（`NoteManager`: `NOTE_TYPE_DESIRE` / `KNOWN_NOTE_TYPES`、`create` は desire を拒否＝ user 専管外、`ensure_desire_note(persona_id)` で singleton get-or-create）。2026-06-28 完了。
2. ✅ **Note↔Task 紐付け** = ③-0 で `PersonaTask.note_id` 列として実装済み（新設不要）。
3. ✅ **open-notes → head 配線**: `OpenNotesSection`（`sea/head_pipeline/sections/open_notes.py`, order 720, Metabolism 凍結）。**desire ノート内の候補 Task**（`statuses=pending/active/paused`、昇格済みは note_id→None で自動除外）＋ **現 running Track の attach open ノート**（`get_running` → `list_open_notes`、§7.1.1 の visual_context 準拠手法）を render。2026-06-28 完了。
4. ✅ AUTONOMOUS 用「候補 Task を desire ノートに追加」スペル = `desire_add(title, goal?)`（`builtin_data/tools/desire_add.py`、`ensure_desire_note` → `create_task(parent_kind='note')`、`TASK_CONTROL_SPELLS` 登録＝AUTONOMOUS/CONVERSATION 許可）。2026-06-28 完了。
5. ✅ AI に `LIFE_PURPOSE` カラム追加 + 保存 + 初回目的設定。
   - ✅ `AI.LIFE_PURPOSE`（Text, nullable, JSON `{purpose, interests[], vocations[]}`）。追加系 migration（`try_additive_migration` が自動 ADD COLUMN、nullable なので手動フック不要）。
   - ✅ ヘルパ `saiverse/life_purpose.py`（parse / serialize / get / set / render + ① 駆動文 `DESIRE_DRIVE_TEXT`）。
   - ✅ 保存スペル `life_purpose_set(purpose, interests?, vocations?)`（canonical 形式、Track/Task 制御外＝ゲート対象外）。
   - ✅ **初回の目的設定 = META 専用状況**（§10）。`_classify_situation` の状況 `life_purpose_unset`（未設定の間毎回・META 標準モデル。優先度は alert_present の次 — §10.1 例外参照）→ Playbook `meta_judgment_life_purpose`（judge ドラフト → finalize が `life_purpose_set` 整形実行）。head の命令文は撤去（背景のみ）。`life_purpose_set` は `SELF_DEFINITION_SPELLS` で META/CONVERSATION 限定。ライフビューに「生きる目的を考えた」表示。
6. ✅ `LIFE_PURPOSE` + ①駆動文を常駐注入 = `LifePurposeSection`（`sea/head_pipeline/sections/life_purpose.py`, order 560, Metabolism 凍結、駆動文は常時 / 目的は設定時のみ render、確定は diff 通知 `life_purpose_set`）。
7. ✅ META の Track 化時に候補 Task を昇格（案A: `track_create(from_candidate='task:N')`）。Track 作成後に `promote_to_track` で候補を Track へ張り替え＝候補プールから自動消去。Track 作成成功後に昇格を試み、昇格失敗は Track を残して戻り値に載せる。権限は既存 `track_create`（TRACK_CONTROL = META/CONVERSATION）のまま＝AUTONOMOUS は昇格不可（§6 整合）。2026-06-28 完了。

テスト: `tests/test_open_notes.py`（section 8 + spell 1）/ `tests/test_note_manager.py`（desire 3）/ `tests/test_life_purpose.py`（helper 5 + section 4 + spell 1）。

---

## 9. 残課題（実装中に詰める）

1. **候補 Task（standalone）↔ Track 内小目標（track_task）の棲み分け**。旧 `task_*`（廃止予定）の standalone 能力をここで活かす形になる。昇格時に候補 Task をどう Track へ橋渡しするか（破棄して Track 新規 / steps 移送 等）。
2. **Memopedia 薄ページ検出**: 「情報量が少ないページ」の判定（現状そのような検出は無い）。③候補の源泉として要るなら新規。任意・優先度低。
3. **候補プールの掃除ポリシー**: 腐敗（陳腐化した候補）の status をいつ closed にするか。機構は Task status で在る、運用ポリシーのみ。

> **§9-2「①共通欲求の初期セット」は解決**: 初期セットは作らず運用で増やす（§3）。
> **§9-3「候補粒度」・§9-5「消化マーキング」は解決**: Task 採用で Task の単位・status に吸収。

---

## 10. 初回の目的設定 — META 専用状況（2026-06-28 実装、まはー裁定）

§4 の「聞き取り」を実装に落とす。設計は 2 段階で却下を経て確定した:

1. 当初の C案「メインモード切替＋ユーザー提示＋確認＋自律停止」の対話フロー →
   **重すぎ・イレギュラー・ユーザー確認不要として却下**。
2. 次案「`LifePurposeSection` が未設定時に head へ促し文を render」→ **head の常駐
   命令はほぼ効かないとして却下**（まはー指摘）。head は「ずっとある背景」なので、
   ペルソナ視点では「今反応すべき新しいこと」に見えず、「ずっとあってスルーしてきた
   もの」としか認識されない。行動喚起は head ではなく **tail/判断サイクル**で行う。

### 10.1 確定実装: META 判断の最優先状況 `life_purpose_unset`

- **head からは命令文を抜く**（`LifePurposeSection` は ① 駆動文＋② 設定済み目的を
  **背景知識**として render するだけ。未設定時の行動喚起は出さない）。
- **META 判断 v2 に新状況 `life_purpose_unset` を追加**（`meta_judgment_structured.md`）。
  `_classify_situation` で `LIFE_PURPOSE` 未設定を **preempt_collision・alert_present の
  次**に判定（running 等より先）。未設定の間は**毎回**この状況になり、設定された時点で
  自然に外れる（フラグ不要）。
  - **例外: alert (外部イベント) は目的設定より優先**（2026-07-07 改訂）。当初は
    「alert/running 等より先」の最優先だったが、LIFE_PURPOSE 未設定のペルソナに
    ユーザーが話しかけると alert が `meta_judgment_life_purpose` に横取りされ、
    対ユーザー Track が activate されず**無応答**になる実害が出た。外部イベントへの
    即応が目的設定より先。目的設定は alert の無い次回の META 判断で行われる。
- **専用 Playbook `meta_judgment_life_purpose`**: judge ノードが構造化出力
  `{monologue, purpose, interests[], vocations[]}` で目的をドラフト（自分の人格定義と
  記憶から）→ `meta_judgment_finalize` が `life_purpose_set` スペルに整形・実行・記録
  （不変条件 v2-A: メインキャッシュに JSON を残さず monologue + /spell に変換）。
- **META（標準モデル）で行う**。AUTONOMOUS（軽量モデル）ではやらない。これは
  `_classify_situation` が META 判断経路でのみ走ることで自動的に担保される。
- **「毎回」の理由**（まはー）: 一度きりだと失敗・無視で未設定のまま残る。未設定の間
  毎 META tick で再起動するので確実。

### 10.2 権限ガード（まはー指摘）

`life_purpose_set` は **AUTONOMOUS が勝手に生きる目的を書き換える**のを防ぐため、
Track 操作と同じく **META / CONVERSATION のみ**に制限する
（`mode_spell_permissions.py` の `SELF_DEFINITION_SPELLS`）。META 専用 Playbook の
finalize は直接実行経路なのでゲート対象外（意図通り META で動く）。

### 10.3 ライフビュー反映（まはー要望）

目的設定の META 判断時、ライフビューに **「生きる目的を考えた」** と出る
（`activity_view._format_meta_judgment_result` が `life_purpose_set` スペルを検出）。

- 保存: `life_purpose_set(purpose, interests?, vocations?)`。
- desire ノートは lazy ensure のまま（初回 `desire_add` で作られる）。目的設定とは独立。
- テスト: `tests/test_life_purpose_meta.py`（分類・finalize・ライフビュー）。

---

## 11. 候補補充 Track（やりたいことを探す走路）— 2026-06-28、まはー提案

### 11.1 解決する問題

§5 の `desire_add` スペルは用意したが、**候補を生成する動線が無かった**（誰がいつ
撃つか不明、`open_notes` の背景文しか促しが無い＝[[feedback_head_command_ineffective_use_tail]]
で弱い）。加えて、やることが尽きると（`idle_no_pending`）メタ判断が**即・対ユーザー
Track へ戻りがち**で、自律が続かない。

→ **候補を補充する作業をする共通・永続 Track** を 1 本持たせる。これで「候補生成の
動線」と「idle 即帰宅の歯止め」を同時に解決する（まはー）。

### 11.2 役割分担

- **desire ノート** = 候補の置き場（器）。
- **候補補充 Track** = 候補を生み出す作業をする走路（生成の動線）。
- **メタ判断** = 溜まった候補を Track 化（昇格）。

### 11.3 設計

- **全ペルソナ共通・永続**（`is_persistent=True`、complete/abort 不可）。**get-or-create
  で 1 本 ensure**（desire ノートと対）。ensure 箇所は **`SAIVerseManager._on_persona_registered`
  の 1 点のみ**（交流 Track と同じ統一フック）。これは起動時・再起動時・動的作成・Blueprint
  spawn のすべてで全ペルソナに走るため、**自律行動の ON/OFF に依らず**必ず 1 本付く。
  （初期実装では `start_activity` ＋ 自律 ON 時の periodic tick の 2 点だったが、再起動で
  自律が OFF 相当に戻ると両方発火せず Track が消えるように見える問題があったため、
  登録フック 1 点に集約した。idempotent なので重複しない。）
- **`track_type='autonomous'`**（新 type を作らない）。`entry_line_role='sub_line'`
  （AUTONOMOUS = 軽量モデルの作業）。専用 Playbook は作らず既存 `track_autonomous`
  に乗る。識別は `track_metadata.role == 'desire_refill'`。
- **intent** に候補補充を明記（「自分の生きる目的・趣味・興味・記憶をもとに、
  これからやってみたいことを見つけて候補として書き留める」）。この Track が running
  のとき AUTONOMOUS はこの intent に沿い、思いついたことを `desire_add` で足す。
- **歯止めの効き方**: 永続なので常に存在し、`unstarted/pending` で待機する。やることが
  尽きても状況は `idle_no_pending`（新規作成強制）でなく `idle_with_pending`（補充 Track
  が候補にいる）になり、メタ判断は「対ユーザーに戻る」一択でなく「補充 Track を起動して
  候補を考える」を選べる。
- **優先度（まはー確認: 砦）**: 昇格済み候補由来の本物 Track があるときはそちらが優先、
  補充 Track は「他にやることが無いときの砦」。当面はペルソナの判断 + intent の自明さに
  委ね、明示的な優先制御は入れない（必要なら後で idle 状況テキストに誘導を足す）。

### 11.4 ループ閉じ: META による候補昇格（✅ 実装済 2026-06-28）

候補 → Track の輪を閉じた。メタ判断 v2 の `idle_with_pending`（D）スキーマに
**`promote` バリアント**を追加:

- `_classify_situation` の sit に `candidates`（desire ノートの候補 Task）を載せる
  （`_get_desire_candidates`）。
- `_build_response_schema`（D）: 候補があれば `decision.anyOf` に
  `{type:'promote', candidate_ref: enum[task:N], title, intent}` を足す（enum 空なら出さない）。
- `_build_situation_text`（D）: 候補一覧を提示し「promote で task:N を選べ」と案内。
- `meta_judgment_finalize`: `decision.type=='promote'` → `track_create(from_candidate=candidate_ref, title, intent, track_type='autonomous')` に整形。`track_create` が `promote_to_track` で候補を新 Track に張り替え（§7 タスク7）→ 候補プールから自動消去。
- Playbook `meta_judgment_idle_pending.json` の judge プロンプトに promote 選択肢追加。
- ライフビュー: 昇格（from_candidate 付き track_create）は **「やりたかった『X』をついに始めることにした」**。
- **補充 Track が常に pending なので実質常に D**＝この経路が効く（E は起きにくい）。
- テスト: `tests/test_meta_promote_candidate.py`。

これで全体ループが回る: 補充 Track が走る → AUTONOMOUS が `desire_add` で候補を溜める
→ open_notes で META が候補を認知 → idle 判断で `promote` → 候補が Track 化 → 実行。
