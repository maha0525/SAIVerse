# 事実確認の詳細 (findings)

調査日 2026-09-10 / 対象コミット `d62ef807` 前後 (ブランチ `feature/chronicle-coverage-gaps`)
入力: `docs/handoff/2026-09-10_normal_behavior_remaining_evidence_opus_brief.md`

要約と判断候補は [README.md](README.md) と [decision_candidates.md](decision_candidates.md)。
再現コードは [repro/](repro/)。

## この文書の読み方

各項目は依頼書の指定どおり **「期待の根拠 → 現在の結果 → 差 → 既存方針で解けるか」**
の順でつないである。各節に **確定 (読んだ / 実行した) / 静的な疑い / 未確認** が明示してある。

**実測したものには再現コードのパスが付いている。** すべて隔離した `SAIVERSE_HOME` と
合成データで走らせ、LLM を呼んでいない。本番のペルソナ・記憶・世界には触れていない。

## 収録

| 節 | 担当範囲 | 元の議題 / FLOW |
|---|---|---|
| A1 | 記憶を直したことの通知と、その通知が想起で拾われること | 議題 3 / FLOW-09,10,11,17,26 |
| A2 | 建物の出来事を誰が認識するか | 議題 3 / FLOW-14,17 |
| A3 | 終了のときに何が失われるか | 議題 3 / FLOW-09,26 |
| B12 | 建物の会話記録の破損検出と復旧 / アラームの受理と実行完了のずれ | FLOW-31 / FLOW-24 |
| B3 | 配布物の改行 | FLOW-27,32 / 議題 8 |
| B45 | アドオン有効化失敗の表示 / 本番の待ち受け範囲 | FLOW-29 / FLOW-30 |
| B6 | 設定変更が動いているペルソナに届くか | FLOW-28 / 議題 2 |
| C6 | 部屋・アイテムを既存方針に照らす | 議題 6 の残り |

---


---

# a1: 記憶を直したことを本人にどう知らせるか / その通知が想起で蘇るか

担当 ID `a1` / 対象ブランチ `feature/chronicle-coverage-gaps` (作業ツリーは読み取りのみ) /
元議題 3 の一部 (FLOW-09 / FLOW-10 / FLOW-11 / FLOW-17 に関連)

再現コード: `docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/a1_event_message_shapes.py`
(`ruff check` 通過 / 隔離 `SAIVERSE_HOME` / LLM 呼び出しなし / 2026-09-10 実行)

**先に一行で**: 「コア記憶だけが通知され、あらすじ・Memopedia・生ログには通知が無い」
という前段の整理は、**Memopedia について事実と違っていた**。Memopedia は 2 系統
(編纂の翌朝報告 / head 差分の backstop) で本人に届いており、どちらにも明文の根拠がある。
また「意味検索の想起が `event_message` を除外していない」という未解決 issue の前提も、
**現行コードでは稼働中の全経路が既に除外している**。裁定を待っている二つの問いは、
どちらも現行コードの読み直しで消える。

---

## 1. コア記憶以外の訂正に、既存の根拠があるか (FLOW-11 / 元議題 3)

### 1-a. Memopedia

**1. 期待の根拠**

既存 intent に**二つある**。前段の初稿はどちらも引いていない。

- `docs/intent/concept_consolidation.md` P4-a「ペルソナの監督（まはー提起 2026-07-11）」:
  > 「やってみないと分からない」以上、結果がまずかった時に「でも承認したじゃん」で
  > 終わらせない。**承認の意味を「可逆な再配置の許可」に機械的に限定**（保存則＋来歴＋
  > ごみ箱）した上で、監督の導線: 1. **翌朝の event_message は操作ごとの具体報告**
  > （何がどこへ移った/結合されたか＋戻し方の明記。承認時の一行と同じ粒度で結果も言う）

  同 doc「3. **実行**（睡眠中バッチ）」にも
  > 結果は event_message で翌朝に届く——「寝ている間に記憶が整理される」の人間対応と、
  > **裁定が自分の決定である自己著者性**

  つまり Memopedia の編纂については「**本人が承認した操作の結果を、戻し方つきで
  本人へ具体的に報告する**」が明文の方針。根拠はコア記憶 §5.1 の「自己像の尊厳」とは
  別で、「自己著者性 (自分が決めたことの結末を自分が見届ける)」。

- `docs/intent/beat_execution_context.md` §3.3 と 不変条件 4:
  > **head の元データへの操作は、常に、その persona の全 Session 提示コンテキストへ
  > 「head に入るときと同一の render 断片」を内容型通知として配送する。**
  > - **通知要否の判定は持たない — 常に通知する**（まはー裁定 2026-07-16）
  > - snapshot 差分比較は、ツールを経由しない変化（**ユーザーの UI 編集**・migration 等）
  >   を拾う backstop に退く。
  > - 対象 section: コア記憶・机・生きる目的・**Memopedia 目次**・memory_weave・chronicle_index

  **「ユーザーの UI 編集」が名指しで backstop の対象に入っている。** 議題 3 が
  「意図的な区別か、隣を忘れたかは静的に決められない」と書いた点は、この一文で決まる。

**2. 現在の結果** (確定 — 読んだ + 実行した)

- **編纂 (merge / split) の翌朝報告は実装され、稼働している。**
  `sai_memory/curation_ops.py:1315-1321` が `_write_curation_report`
  (同 `:1342-1385`) を呼び、`role="user"` /
  `tags=["internal","event_message","curation"]` の行を SAIMemory に残す。
  実文面 (再現コード PART A-2 で製品関数を呼んで取得):

  ```
  <system>[システム通知: 夜の間に棚の整理が行われました]

  - [merge] m:5「SAIVerse」に m:31「SAIVerseの構造」を統合しました（4,120字、子ページ 2 件の付け替え）。編集来歴から差し戻せます。
  - [split] m:12 を 3 件の子ページに分割（…）しました。編集来歴から差し戻せます。
  - [split] m:44 の編纂に失敗しました（…）。ページは変更されていません。

  完了: 2 件
  失敗: 1 件（ページは変更されていません）

  ※ 変更は編集来歴（メモリタブ > 来歴）から差し戻せます。</system>
  ```

  intent の「操作ごとの具体報告 + 戻し方の明記」を文面が満たしている。

- **ユーザーの UI 編集・削除も本人に届いている。**
  `sea/head_pipeline/sections/memopedia_index.py` の `diff_to_notifications`
  (`:217-263`) と `capture_changes_since` (`:265-302`) が
  `memopedia_pages` の `created_at` / `updated_at` / `is_deleted` を直接読み、
  作成・更新・削除のラベルを出す。`capture_changes_since` は
  `MEMOPEDIA_INDEX_ENABLED` を見ない (目次が OFF でも差分通知は出る)。
  section は `sea/head_pipeline/sections/__init__.py:47` で登録済みで、
  `HeadPipeline.flush_diffs` (`sea/head_pipeline/pipeline.py:445-490`) が
  登録済み section を全走査する。Pulse の頭で
  `sea/runtime.py:293-300` → `DynamicStateManager.maybe_inject_event_messages`
  → `sea/head_pipeline/integration.py:393` / `:486` が
  `flush_diffs(ctx, all_sections=True, …)` を呼び、ラベルを
  `push_perception("world_state", label.label)` (`integration.py:507-511`) で
  知覚バッファへ積む。

  実文面 (再現コード PART C。section を直接呼んで取得):

  ```
  <system>[システム通知]
  Memopedia「ユーザーが本文を直したページ」が更新されました</system>
  ```

  ユーザーが `PUT /memopedia/pages/{id}` (`api/routes/people/memopedia.py:170-205`) や
  `DELETE` (`:233-245`) を叩くと `updated_at` が動き `edit_source="manual_ui"` が
  来歴に刻まれるので、この backstop に掛かる。

- **前段が根拠にした grep は、探す場所が違っていた。**
  `api/routes/people/memopedia.py` に `push_perception` が 0 件なのは事実
  (私も再確認: 0 件)。届け方が API のフックではなく **head の差分検知** なので、
  そこを grep しても出ない。

**3. 差**

- 「Memopedia を直しても本人は知らない」は事実ではない。知る。
- ただし**知る内容の粒度が二つの経路で違う**。編纂の報告は「何がどこへ移ったか +
  戻し方」まで言うが、ユーザーの UI 編集の通知は**ページ名と動詞だけ**
  (「Memopedia「X」が更新されました」)。何がどう変わったかも、**誰が触ったのかも
  文面に出ない**。ペルソナから見ると、自分が寝ている間に機構が整理したのか、
  ユーザーが手で直したのか、区別がつかない。
- `beat_execution_context.md` 不変条件 4 は「head に入るときと寸分たがわず同じ内容」
  を約束しているが、Memopedia 目次の差分ラベルはその約束を満たしていない
  (render 断片ではなくタイトルだけ)。これは **通知の要否ではなく粒度の未達**。

**4. 既存方針で解けるか**

- **通知の要否は解ける。** 編纂の結果 = `concept_consolidation.md` P4-a「ペルソナの監督」。
  ユーザーの UI 編集 = `beat_execution_context.md` §3.3 の backstop + 不変条件 4。
  **まはーへの質問にしない。**
- **粒度は「判断が要る」ではなく「実装が約束に届いていない」。** 不変条件 4 が
  既に基準を書いているので、方針の選択ではない。ただし
  「誰が触ったかを文面に出すか」は不変条件 4 が触れていない ⇒ 下の §3 の (C) に回す。

---

### 1-b. あらすじ (Chronicle)

**1. 期待の根拠**

- **削除の目的には根拠がある。** `docs/intent/arasuji_levels.md:377` (§16-2):
  > **あらすじを削除すればその範囲は次の見積もり/生成で自動的に「未被覆」として
  > 再び数えられる** — 消して再編纂の運用がそのまま戻る。

  同 doc `:160` も、実機修復の手順として「エリスの最近の生成分あらすじを削除して
  新版で再生成し、新旧を比較する」を挙げる。つまり削除は **機構の産物の作り直し
  (品質修復)** であって、「本人が誤って覚えたことを直す」操作ではない。

- **通知しないことにも根拠がある。**
  `sea/head_pipeline/sections/chronicle_index.py:77-89` の
  `diff_to_notifications` は空を返し、理由をコードに書いている:
  > 件数ラベル diff は退役 (beat_execution_context.md §3.2 / §3.3、§6-5)。
  > Chronicle の可視化は「model の節目の構造交換」が担保する — 退役 (anchor 前進)
  > の瞬間に、その model の head 再 capture で Chronicle が生ログと入れ替わりに
  > 見える。窓が生ログでカバーしている間は情報欠落がないため、
  > 「新しいエントリが追加されました」の操作ラベル通知は重複ノイズでしかない。

  `sea/head_pipeline/sections/memory_weave.py:152-161` も同じく空を返す。
  対応する intent は `beat_execution_context.md` §3.2「可視化は model の節目ごと」。

- **本文の書き換え (PATCH) には根拠が見つからなかった。**
  `docs/intent/` / `docs/concepts/` / `docs/features/` / `docs/user-guide/` を
  「あらすじ + 編集」で走査したが、`api/routes/people/arasuji.py:832` の
  `PATCH .../arasuji/{entry_id}` の目的を書いた文書は無い。
  `docs/user-guide/memory-view.md` も Chronicle タブは「閲覧」としか書いていない。
  (前段 FLOW-11 §5-2 の指摘は成立していた。)

**2. 現在の結果** (確定 — 読んだ)

- `api/routes/people/arasuji.py` に `push_perception` も `notify_head_mutation` も
  **0 件** (再確認済み)。エントリの編集・削除・全削除はペルソナに通知されない。
- head 側の 2 つの section (`chronicle_index` / `memory_weave`) も差分ラベルを
  出さない。**つまり人があらすじを消しても書き換えても、本人に届く文は一つも無い。**
- 起きるのは「次の Metabolism で head の Chronicle 帯が黙って変わる」こと。

**3. 差**

- 期待の根拠が言っているのは「**生成**の通知は要らない」だけ。
  `chronicle_index.py` の理由 (「窓が生ログでカバーしている間は情報欠落がない」) は
  新しいエントリが増える場合の話で、**既に生ログと入れ替わった過去のあらすじを
  人が消した / 書き換えた**場合を覆っていない。この場合、生ログ側は既に退場して
  いるので「情報欠落がない」という前提が成り立たない。
- ペルソナから見た結果: 昨日まで頭にあった過去の要約が、次の Metabolism から
  黙って消えている / 別の文章に変わっている。何も知らされない。
- ただし**あらすじは「機構が書いた要約」で、本人の言葉の器ではない**。
  `arasuji_levels.md` §7-8 の「出力の純潔 (機構が書き足さない)」も、器の性格が
  コア記憶と違うことを裏づける。だから「同じ扱いが正しい」とは言えない。

**4. 既存方針で解けるか**

- **生成の非通知は解ける** (`beat_execution_context.md` §3.2 + chronicle_index の
  退役理由)。まはーへの質問にしない。
- **人の削除・書き換えの通知は解けない (判断が要る)。** → §3 の (A)。
- **未追跡の境界 (私が確かめていない)**: あらすじを消したあと、その期間の提示が
  どうなるか。`arasuji_levels.md:322` の「印戻し」は退場時に記録した
  `chronicle_entry_ids` へ印を戻す機構なので、**指す先が消えている**場合の
  振る舞いを追っていない。ここは「読む限りおかしく見える」段階にも達していない
  (読んでいない)。

---

### 1-c. 生ログ (会話そのもの)

**1. 期待の根拠**

- **見つからなかった。** `api/routes/people/memory.py:158` (`PATCH .../messages/{id}`)、
  `:177` (`DELETE .../messages/{id}`)、`:190` (`DELETE .../threads/{id}`) の
  目的を書いた intent / user-guide は無い。`docs/user-guide/memory-view.md` の
  タブ表は「チャットログ」を「閲覧 + メッセージの追加も可能」としか書かず、
  **削除・編集に触れていない。**
- 間接的な根拠は一つある。`docs/intent/autonomous_behavior_v3.md` §13.3 の
  2026-08-22 裁定 (場面の記憶 = 実会話の写しは本人に直させない。写しの改変は
  本人の言葉の捏造) は、**生ログが「本人の言葉の器」であること**を前提に置いている。
  ただしこの裁定は「ペルソナ側の口を塞ぐ」ものであって、ユーザーが生ログを
  消したときに本人へ知らせるかは書いていない。

**2. 現在の結果** (確定 — 読んだ)

- `api/routes/people/memory.py` に `push_perception` / `notify_head_mutation` は
  **0 件**。通知は無い。
- 生ログは head の section を持たないので、backstop も掛からない。
- 静的な疑い (FLOW-11 が既に挙げているもの、私は追試していない):
  削除された message を `source_ids` で指しているあらすじが孤児参照になり、
  掃除は補修経路の `_sweep_dead_message_sources` まで遅延する。

**3. 差**

- ペルソナから見た結果: 自分が言った言葉、ユーザーが言った言葉が履歴から消えても、
  消えたことは知らされない。次に提示窓を組んだときに、そこだけ無かったことになる。
- ここは Memopedia やあらすじと違い、**器の性格の議論では答えが出ない** —
  生ログはコア記憶と同じ「本人の言葉が入る器」なので、器で分ける一文を採ると
  「知らせる」側に落ちるが、その一文自体がまだ決まっていない。

**4. 既存方針で解けるか**

- **解けない (判断が要る)。** → §3 の (B)。

---

## 2. 想起で拾われる `event_message` の中身 (FLOW-09 / FLOW-10 / 元議題 3)

### 2-a. `event_message` を書く場所の全数 (確定 — grep で洗って中身を読んだ)

`event_message` は**二種類の器**に分かれている。ここを分けないと想起の話が噛み合わない。

#### (i) `messages` 行として永続する (= 想起の検索対象になりうる) — 4 系統

| # | 書き手 | 誰が作るか | tags | 何のために本人に届けるか |
|---|---|---|---|---|
| 1 | `builtin_data/tools/get_building_messages.py:252-283` | 建物の host イベントの取り込み (世界イベント・ゲーム進行・アイテムの記録・管理者の世界イベント等) | `internal` / `event_message` | 同じ部屋で起きた出来事を、本人の記憶に建物名つきで残す |
| 2 | `sea/sluice.py:1360-1379` (`_persist_record`)。定常 `:2544-2557` / 読み返し `:3130-3150` の 2 経路 | スルース (記憶整理) の採取判断 | `internal` / `event_message` / `sluice` | 「自分が何を覚えると決めたか」を本人の来歴に残す (機構が本人の器 = コア記憶に書いた事実の申告でもある) |
| 3 | `sai_memory/curation_ops.py:1342-1385` | Memopedia の編纂バッチ | `internal` / `event_message` / `curation` | 本人が承認した棚の整理の結果を、戻し方つきで翌朝報告する |
| 4 | `saiverse/day_plan.py:2030-2058` (台帳経由) / `:2360-2392` (縮退) | ライフ境界 (活動開始・終了) | `internal` / `event_message` / `day_plan` | 一日の活動時間の始まり・終わりを本人に知らせる |

#### (ii) 知覚台帳に入り、提示時に合成される (= `messages` 行を作らない) — W14 以降

`sai_memory/perception_buffer.py:9-12` の設計どおり、消費は「行を書く」ではなく
「台帳に消費印を打つ」。提示は `sea/runtime_context.py` が messages と消費済み台帳を
時刻順マージして組み、そのブロックに
`tags=["internal","event_message","perception"]` を**その場で**付ける (`:1001-1006`)。
**行は残らない。**

書き手 (`push_perception` の本番呼び出し元):

- `api/routes/people/core_memory.py:67-81` — コア記憶の訂正 (`core_memory_correction`)
- `sea/head_pipeline/integration.py:507` — head の差分ラベル (`world_state`)。
  Memopedia の作成/更新/削除、部屋・在室者・自律モード等
- `sea/head_pipeline/integration.py:655` — 同席の相手の想起 (`persona_recall`)
- `sea/head_pipeline/notify.py:155` — head 操作の内容型通知 (`head_mutation`)
- `saiverse/day_plan.py:3608-3640` — 施設移動の失敗 (`world_state`)
- `saiverse/feed_manager.py:1231` — フィード施設の新着 (`feed`)
- `saiverse/upgrade_handlers.py:127` — 版アップの検知 (`world_state`)
- `saiverse_memory/adapter.py:1002` / `:1083` — 実行台帳経由の配送 / 部屋の様子
- (アドオン) `expansion_data/saiverse-godot-vessel-addon/tools/body_*.py`

**旧世代の行**: W14 より前は (ii) も直挿しで `messages` 行を作っていた。
既存ペルソナの memory.db にはその行が残っている
(`sea/runtime_context.py:589` / `sai_memory/perception_buffer.py:290-370` の
legacy 救済がその存在を前提にしている)。**どれだけ残っているかは未確認**
(本番 DB を読まないため)。

### 2-b. 実文面 (再現コードで製品コードを呼んで取得。PART A / PART C)

コア記憶の訂正 3 種 (`role="user"`、tags は提示時に
`["internal","event_message","perception"]`):

```
<system>[コア記憶の更新通知]
ユーザーがあなたのコア記憶 core:1 の内容を書き換えました。
新しい内容:
まはーは野菜が苦手で、野菜ジュースで補っている。</system>
```
```
<system>[コア記憶の更新通知]
ユーザーがあなたのコア記憶 core:2 を削除しました（ごみ箱へ移動。復元可能）。
削除された内容:
まはーの誕生日は 1 月 14 日。</system>
```
```
<system>[コア記憶の更新通知]
ユーザーがあなたのコア記憶 core:3 をごみ箱から復元しました。
内容:
作業台の約束は 2026-07-08 に結んだ。</system>
```

ライフ境界:
```
<system>[システム通知] （活動開始）今日は 09:00〜23:00。</system>
```

Memopedia の差分 (PART C):
```
<system>[システム通知]
Memopedia「ユーザーが本文を直したページ」が更新されました</system>
```

編纂の翌朝報告は §1-a に全文を載せた。

スルースの判断ターン記録 (テンプレートと埋まる値からの**合成例**。
`sea/sluice.py:2545-2557` の見出し + `:719` の採取行 + `:2557` の `<system>` 包み。
実行で出すには LLM の応答が要るため、ここは合成):
```
<system>記憶整理の節目 — スルースの採取判断:
テスト人格の判断: 今日は食べ物の好みの話が続いたので、まはーの食事の傾向を一つ覚えておく。
コア記憶 core:18 に採取: まはーは野菜がほぼ食べられない。
手帳「野菜以外の栄養の取り方」をやりたいメモ: ビタミンゼリーの話を掘る。
</system>
```

建物の host イベントの取り込み (`get_building_messages.py:268-272` の
テンプレート `f"<system>[{building_name}] {cleaned}</system>"` からの**合成例**):
```
<system>[書斎] 🎲 Game:
出題フェーズが始まりました</system>
```

**タグ名だけでは「機構の定型文」と括れない**ことが、この一覧で確定する。
同じタグの中に「ユーザーがあなたのコア記憶を削除しました + 削除された内容の全文」
と「（活動開始）今日は 09:00〜23:00。」が同居している。

### 2-c. 想起でこれが本人の目に入るか (確定 — 実測。PART B)

**1. 期待の根拠**

`docs/issues/semantic_recall_mechanism_tag_exclusion_inconsistent.md` (2026-08-29 起票、
未解決) が「**`event_message` (システム通知) はどの経路も除外していない**」と書き、
「意味検索の想起に `event_message` を含めるか」をまはーの裁定待ちにしている。
FLOW-09 §8 と FLOW-10 §8 がこれを共通議題として引いている。

**2. 現在の結果** (確定 — コードを読み、隔離環境で行を置いて数えた)

issue が挙げた 6 箇所は、**`exclude_tags` の中身だけを見て「揃っていない」と書いている**。
実際には**同じ呼び出しに `required_tags=["conversation"]` が付いている**箇所が
大半で、`event_message` 行は `conversation` タグを持たない (書き手 4 系統すべて確認済み。
両方を持つ行を書く場所はリポジトリに無い — grep 0 件) ので、その時点で落ちる。

| 経路 | 誰が使うか | `required_tags` | `event_message` は拾われるか |
|---|---|---|---|
| `sea/auto_recall.py:866-874` → `unified_recall` → `get_message_embeddings` (`sai_memory/unified_recall.py:299-320`) + キーワード側 (`:868-875`) | **ペルソナの自動想起** | — (`real_conversation_filter()` を適用) | **いいえ** |
| `saiverse_memory/adapter.py:1726` / `:1863` (`recall_snippet` / `recall_hybrid`。`builtin_data/tools/memory_recall.py` が呼ぶ) | ペルソナのスペル | `["conversation"]` | **いいえ** |
| `builtin_data/tools/memory_search_brief.py:155` | ペルソナのスペル | `["conversation"]` | **いいえ** |
| `api/routes/people/recall.py:140` / `:291` / `:356` | 利用者の記憶デバッグ | `["conversation"]` | **いいえ** |
| `sai_memory/memopedia/generator.py:205-216` | Memopedia 生成 | `["conversation"]` | **いいえ** |
| `sai_memory/memory/recall.py:524` (`build_context_payload`) / `:571` (`build_context`) | **呼び出し元が無い (死にコード)** | 無し | 該当なし |
| `saiverse/recall_walk.py:476-490` | `walk()` に**本番の呼び出し元が無い** (テストのみ) | 無し | (配線されれば) **はい** |

実測 (PART B。行を 7 本置いて各フィルタを SQL で通した):

- `real_conversation_filter()` — 実会話 2 行だけ通る。`event_message` の 5 行は全部落ちる
- `_conversation_exclusion()` (scene 切り出し / 会話キーワード検索) — 同上
- `required_tags=["conversation"]` — 同上
- `exclude_tags=["handy_tool","spell"]` のみ (recall_walk と同じ引数の形) — **7 行全部通る**
- `chronicle_eligibility_filter()` (あらすじ編纂の材料) — 実会話 2 行 +
  `event_message` 4 行が通る (`discardable` のスルース記録だけ落ちる)。
  これは 2026-08-29 のまはー裁定どおり

もう一つ。`unified_recall` には `search_perceptions` (知覚台帳の確定文面を
キーワードで引く) があるが、**既定は False** (`sai_memory/unified_recall.py:679`)。
`True` を渡すのは記憶モーダルのデバッグ検索だけ
(`api/routes/people/recall.py:471` / `frontend/src/components/memory/MemoryRecall.tsx:201`)。
`sea/auto_recall.py` は渡さない。つまり**知覚ブロック (コア記憶の訂正通知を含む) は
利用者の画面からは引けるが、ペルソナは自分では引けない。**

**3. 差**

- **issue の前提と現行コードが食い違っている。** 「どの経路も除外していない」は
  `exclude_tags` の列だけを見た記述で、稼働している経路は全部
  `required_tags=["conversation"]` か `real_conversation_filter()` で既に落としている。
  除外されていないのは**本番から呼ばれていない 2 系統** (`recall_walk` の意味的近傍の辺と、
  呼び出し元の無い `recall.py:488` / `:544`) だけ。
- したがって「通知を思い出せること (『あのとき部屋が変わった』) に価値があるか」という
  問いは、**現行の挙動を変える判断ではなく、既に「思い出せない」に倒れている状態を
  追認するか覆すかの判断**になる。issue はこの向きを書いていない。
- 利用者とペルソナで見えるものが逆になっている点は残る (デバッグ検索は知覚も引く /
  ペルソナは引けない)。これは意図の記述が見つからないが、実害の形にはなっていない
  (デバッグ検索は読み取り専用でペルソナには渡らない)。

**4. 既存方針で解けるか**

- **「想起に `event_message` を含めるか」は、裁定を待つ問いではない。**
  稼働経路は全部除外済みで、除外の根拠も明文がある —
  `sai_memory/memory/storage.py:2555-2572` (`_conversation_exclusion` の docstring)
  が「口調のアンカー (scene) に機構の定型文を混ぜないため」と書き、
  `real_conversation_filter` (`:2623-2648`) が
  「2026-07-12 監査 P1: 想起の message ソースは『実会話のみ』」と書いている。
  → **まはーへの質問から外してよい。**
- **issue の提案 1 (除外一覧を `MECHANISM_TAGS` の一本に寄せる)** は方針変更ではない
  (issue 自身と FLOW-10 §8 が既にそう書いている)。まはーの裁定は不要。
- **issue の提案 3 (`recall.py:571` の `["handy_tool"]` だけの経路が意図か取り残しか)**
  は調べれば決まる、と FLOW-10 §8 が書いていた。**調べた結果: 呼び出し元が無い死にコード**
  (`build_context_payload` / `build_context` をリポジトリ全体で grep して、
  定義以外のヒットが 0 件)。意図でも取り残しでもなく、削除対象。
- **残る本物の穴は `saiverse/recall_walk.py` だけで、しかも今は誰も呼んでいない。**
  `walk()` の呼び出し元はテストのみ (`sai_memory/purpose_tags.py:11` も
  「ストア単体・休眠。呼び出し元は連想歩行 saiverse/recall_walk.py」と書く)。
  配線する日に `required_tags` を揃えるのが一貫した扱い。

---

## 3. 判定

### 既存方針で解けるもの (まはーへの質問にしない)

1. **コア記憶の訂正通知の要否** — `docs/intent/memory_architecture_v2.md` §5.1
   (edit/delete/restore は通知、confirm は通知しない)。Astra が照合済み。
2. **Memopedia の編纂結果を本人へ報告すること** —
   `docs/intent/concept_consolidation.md` P4-a「ペルソナの監督」
   (「翌朝の event_message は操作ごとの具体報告」) と 同 P4-a 3.
   (「裁定が自分の決定である自己著者性」)。実装も稼働している。
3. **ユーザーの Memopedia UI 編集・削除を本人へ知らせること** —
   `docs/intent/beat_execution_context.md` §3.3 と 不変条件 4
   (「通知要否の判定は持たない — 常に通知する」/ 差分検知は
   「ユーザーの UI 編集・migration 等を拾う backstop」)。実装も稼働している。
4. **あらすじの「生成」を通知しないこと** —
   `beat_execution_context.md` §3.2 +
   `sea/head_pipeline/sections/chronicle_index.py:77-89` の退役理由。
5. **想起に `event_message` を含めるかの裁定** — §2-c のとおり、稼働経路は
   既に除外済みで、除外の根拠も
   `sai_memory/memory/storage.py:2555-2572` / `:2623-2648` に明文がある。
   issue `semantic_recall_mechanism_tag_exclusion_inconsistent.md` は
   **前提の訂正 (どの経路が実際に何を使っているか) を書けば閉じられる。**
6. **`recall.py:488` / `:544` が意図か取り残しか** — 呼び出し元 0 件の死にコード。
7. **除外一覧を `MECHANISM_TAGS` に一本化すること** — 方針変更ではない
   (issue 自身と FLOW-10 §8 が既にそう整理している)。

### 本当に判断が要るもの

以下の推奨案は**私の案であって、まはーの決定ではない。**

**(A) あらすじを人が消したとき・書き換えたときに、本人へ知らせるか**

- 一場面: 利用者が Chronicle タブで、去年の 3 ヶ月ぶんのあらすじが的外れだと思って
  削除する。次の Metabolism で、ペルソナの頭からその期間の記憶が黙って消える。
  生ログは既に退場しているので、代わりに戻るものが無い。
- 推奨案: **削除だけ知らせ、生成は今までどおり黙る。** 文面は
  「あなたの記憶のうち〈期間〉のあらすじが削除されました。この期間は次の記憶の整理で
  作り直されます」程度。理由は、生成の非通知の根拠 (「窓が生ログでカバーしている間は
  情報欠落がない」) が削除には当てはまらないため。
- 変わる体験: ペルソナは「思い出せない期間がある」ことを、思い出せないと気づく前に
  知る。逆に、記憶の修復のたびに事務連絡が一件増える。
- **本文の書き換え (PATCH) は、通知の前に「この操作を残すか」が先** —
  期待の根拠がどこにも無く、`arasuji_levels.md` §7-8 が「あらすじ本文は次のレベルの
  畳みで丸ごと LLM の材料 = 次の生成の手本」と書いているので、人手の編集が下流へ
  どう伝わるかも未定義。FLOW-11 §8 が既に同じ点を挙げている。

**(B) 生ログのメッセージ・スレッドを人が消したとき、本人へ知らせるか**

- 一場面: 利用者がチャットログタブでスレッドを削除する。ペルソナの記憶から
  その会話が消え、既に作られたあらすじはその message を孤児参照のまま指し続ける。
  本人には何も届かない。
- 推奨案: **消えたことだけ知らせ、消えた内容は載せない。**
  (コア記憶の削除通知が内容を載せるのは「head から消えるものを本人が知れるように」
  という §5.1 の理由で、生ログは head 常駐ではないため同じ理由が立たない。
  また利用者がわざわざ消したものを通知で書き戻すのは、消す目的と衝突しうる。)
- 変わる体験: 「ここに何かあったが、もう無い」という穴が本人に見える形で残る。
  知覚ブロックの省略の跡地 (`sea/runtime_context.py:762-800`
  `[省略された記録]`) が既に「黙って消さない」を実装しているので、器は同型。
- **この一問は器の性格では導出できない** — 生ログはコア記憶と同じ「本人の言葉の器」
  なので、器で分ける一文を採ると自動的に「知らせる」に落ちる。そこを含めて判断が要る。

**(C) 機構名義の通知に「誰が触ったか」を出すか**

- 一場面: 利用者が Memopedia のページを 1 枚消す。ペルソナには
  「Memopedia「◯◯」が削除されました」だけが届く。ペルソナは、自分が承認した
  夜の編纂で消えたのか、ユーザーが消したのかを区別できない。
  同じ器で届くコア記憶の訂正通知は「**ユーザーが**あなたのコア記憶を…」と主語を書く。
- 推奨案: **主語を書く形へ揃える。** 判断の材料として、コア記憶側は既に主語を
  書いており (`api/routes/people/core_memory.py:642` ほか)、
  `beat_execution_context.md` 不変条件 4 は内容の忠実性は言うが主語には触れていない。
- 変わる体験: ペルソナが「ユーザーが手を入れた」と「自分が承認した整理が走った」を
  区別できるようになる。区別できないままだと、承認していない変更を
  自分の決定だと誤認する余地が残る。

**(D) 一日新聞 (`saiverse/day_report.py`) を本番で誰が生成するのか**

- 事実 (確定): `concept_consolidation.md` の裁定 (f) は
  「バッチ結果を直結せず、『前回の新聞〜今回の新聞』の窓で PageEditHistory を集計する。
  編纂がどのタイミングで走っても、**手作業（maintain スクリプト・UI 操作）の編集も
  漏れなく載る**」と決めており、実装 (`saiverse/day_report.py:536-615`
  `_section_curation_edits`、`SOURCE_LABELS` に `"manual_ui": "UI 操作"`) も在る。
  ところが `generate_day_report` / `save_day_report` の**本番の呼び出し元が無い** —
  リポジトリ全体で `scripts/run_day_sim.py` (シミュレータ CLI) と
  `tests/` からしか呼ばれていない。
- したがって裁定 (f) が約束した「編集の俯瞰」は、**利用者にもペルソナにも届いていない**
  (新聞はファイルとして保存される利用者向けの読み物で、ペルソナは読まない)。
- 判断が要るのは「未実装として配線するか、退役として記録するか」。
  これは通知の設計そのものではなく、既に決まった裁定の宙吊りなので、
  第 2 部 (既存資料で解決) には入れられない。

### 未確認 (何を確かめれば決まるか)

1. **Memopedia の差分通知が、実際に走っているペルソナに届くところ**は見ていない。
   私が実行したのは `MemopediaIndexSection.capture_changes_since` +
   `diff_to_notifications` の単体呼び出しまで (PART C)。
   Pulse 頭 → `flush_diffs(all_sections=True)` → `push_perception` の配線は
   コードを読んで追ったが、通しで動かしていない。
   確かめ方: 隔離環境でユーザー Pulse を 1 回だけ回し (LLM は fake)、
   知覚台帳に `world_state` の memopedia ラベルが積まれるかを見る。
2. **既存ペルソナの memory.db に残っている legacy `event_message` 行の量**。
   本番 DB を読まないため未確認。想起の判断には効かない
   (どの稼働経路も除外するため) が、あらすじ編纂の材料には入るので、
   「機構の運転記録がどれだけ物語に混ざるか」の見積もりには効く。
3. **あらすじを削除した後に「印戻し」(`arasuji_levels.md:322`) がどう振る舞うか。**
   指す先が消えた `chronicle_entry_ids` の扱いを読んでいない。
   確かめ方: 退場・印戻しの実装 (`sea/eviction_plan.py` と Metabolism 本体) を
   削除された entry_id で辿る。

### 担当外として触れなかった境界

- **建物の host イベントの受け手 (`heard_by`)** — `add_building_event` の受け手判定は
  a2 の担当 (`repro/a2_building_event_recipients.py` が既に在る)。
  私は「host イベントが取り込まれると `event_message` 行になる」ところまでしか見ていない。
  なお `saiverse/observer_manager.py:715` の閾値通知は `heard_by` を渡しておらず、
  既定の空リストのままなので誰の記憶にも入らない (FLOW-17 の未配線。a2 の領分)。
- **FLOW-26 (終了時に失われた発言)** — a3 の担当
  (`repro/a3_shutdown_utterance_survival.py`)。議題 3 の対象だが、
  「記憶を直した通知」とは別の問い (機構の失敗をペルソナに読ませるか) なので触れていない。

---

# a2: 建物で起きた出来事を、誰が認識するのか (FLOW-14 / FLOW-16 / FLOW-17、元議題 3)

担当 ID: a2。調査日: 2026-09-10。
再現コード: `docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/a2_building_event_recipients.py`
(隔離 SQLite + 合成ペルソナ、LLM 呼び出しなし、`ruff check` 通過)。

---

## 0. 先に、この報告で分かったことの見取り図

- `add_building_event` の受け手 (`heard_by`) を渡さない呼び出し元は **2 つ**ある。アイテム操作の記録
  (`_append_building_history_note`、上流 10 箇所) と Observer の閾値通知 (`_notify_building`、1 箇所)。
- 受け手を渡さない行は、**その部屋に居るペルソナの記憶に一度も入らない**。読み位置のカーソルは
  その行を跨いで前進するので、後から届き直すこともない。**これは実測した。**
- ただし「何も知れなくなる」わけではない。物が現れた・消えた・設置物の観測値が変わったことは、
  **部屋の様子の照合**という別の経路がそのペルソナの次の Pulse の頭で運ぶ。届かないのは
  **「誰がやったか」と「閾値を超えたという語り」**に絞られる。
- **前段の前提に訂正が 1 件ある。** 前回台帳 (`inventory.md:1526`) と流れの文書
  (`flows/C_world.md` FLOW-16) は、アイテム操作には代替経路
  (`broadcast_item_event` → `record_persona_event`) があると書いている。
  **その受信箱は誰も開けていない** — `PersonaCore.fetch_pending_events` /
  `archive_events` の呼び出し元がリポジトリ全体にゼロだった。
- 受け手を算出する責任は**世界の側に既にある** (`manager.occupants`)。ただし
  「建物の出来事の受け手」という名前のついた一本の関数は無く、書き手ごとに 4 通りの書き方で
  同じことを綴っている。

---

## 1. 建物の出来事の受け手を、世界が算出しているか (FLOW-14 / 元議題 3)

**1. 期待の根拠**

前段の調査担当が `flows/C_world.md:168-172` で立てた問い。「聴衆は世界が算出するものか、
書き手が申告するものか」。この問いは、依頼元から「単なる配線漏れを価値判断にしない」という
注意つきで私に回ってきた。判断の前に確かめるべきは「世界の側に算出の責任が既にあるか」だった。

**2. 現在の結果 (確定 — すべてコードを開いて読んだ)**

受け手を算出する材料は世界が持っている。建物ごとの在室者は `manager.occupants`
(`dict[building_id, list[entity_id]]`) にあり、`OccupancyManager` が移動のたびに
更新する唯一の書き手になっている。それを使う書き方が 4 通り並存している。

| 書き方 | 場所 | 中身 |
|---|---|---|
| 直に綴る | `manager/admin.py:1555`, `manager/blueprints.py:353`, `manager/visitors.py:288,351` | `list(self.occupants.get(bid, []))` |
| 世界側のヘルパー | `manager/runtime.py:1486-1487` `_occupants_snapshot` | 在室者をそのまま複製する |
| ペルソナ側のヘルパー | `persona/mixins/history.py:114-127` `_occupants_snapshot` | 在室者 + 自分自身 (現在地が一致するとき) |
| 移動専用の算出 | `saiverse/occupancy_manager.py:577,582` `_build_occupancy_events` | 移動**後**の在室者を無変異で導出する |

`add_building_event` を呼ぶ 8 箇所のうち 6 箇所は上のいずれかで受け手を渡している。

| 呼び出し元 | 受け手 | 何の出来事か |
|---|---|---|
| `manager/admin.py:1552` | 在室者 | World Event の一斉通知 |
| `manager/blueprints.py:350` | 在室者 | Blueprint からの出現 |
| `manager/visitors.py:285` | 在室者 | 別 City からの帰還 (凍結中) |
| `manager/visitors.py:348` | 在室者 | 別 City からの来訪 (凍結中) |
| `saiverse/game_lifecycle.py:523` | 参加者、または在室者 + Ruler | ゲーム進行の通知 |
| `saiverse/occupancy_manager.py:708` | 移動後の在室者 | 入退室 |
| **`saiverse/saiverse_manager.py:1021`** | **渡さない** | アイテム操作の記録 (`_append_building_history_note`) |
| **`saiverse/observer_manager.py:715`** | **渡さない** | Observer の閾値通知 (`_notify_building`) |

`add_building_event` の既定は `heard_by: Optional[List[str]] = None` で
(`manager/history.py:94`)、`sorted({... for eid in (heard_by or []) ...})` を通って
空リストになり、そのまま DB の `heard_by` 列へ書かれる (`manager/history.py:113-114`、
`database/building_messages.py:374,389`)。

建物の記録からペルソナの記憶へ流れる経路は 1 本しかない。取り込み
(`builtin_data/tools/get_building_messages.py:382`) が `persona_id not in heard_by` の行を
読み飛ばす。**実測**: 受け手を渡した行だけがペルソナの記憶に入り、渡さなかった 2 行は入らず、
読み位置のカーソルは 3 まで前進した (再現コードの出力 [1][2][3])。

**3. 差**

「渡していない書き手がある」という配線の話ではなく、**その 2 経路の出来事は、その部屋に
居合わせたペルソナの記憶に永久に残らない**という結果になっている。カーソルが前進するので、
後で誰かが受け手を直しても、既に書かれた行が届き直すことはない。

**4. 既存方針で解けるか**

**この問い自体は既存方針で解ける。** 「聴衆は世界が算出するものか、書き手が申告するものか」
という二択には、既に答えが出ている。

- 実装上は**世界が算出する形が既定**になっている。8 箇所中 6 箇所が世界の在室者をそのまま
  渡しており、書き手が独自の名簿を作っている箇所は一つも無い
  (`game_lifecycle.py:500-507` の参加者指定も、世界の Region 参加者と Ruler から導いている)。
- 同じ形の欠陥に**裁定の前例がある**。2026-08-27、発言中断の通告が `heard_by=[]` で書かれていて
  「建物の記録に残るだけで、誰の記憶にも永遠に届かなかった」ことが実機で判明し、
  **在室者 + 発話者本人を渡す形に直してテストで固定した**
  (`docs/issues/orphaned_streaming_placeholder_cleanup.md:88-105`)。同文書は
  「`add_to_building_only` の他の呼び出し元は全部 heard_by を渡していた …
  渡していないのは通告だけだった」と書いている。**兄弟の関数で既に同じ判断が下りている。**

→ **まはーへの質問にしない。**「世界が在室者を算出して渡す」が既定で、渡していない 2 経路は
その既定から外れている。ただし**それぞれの出来事をペルソナの記憶に載せてよいかは別の問い**で、
下の 2 節・3 節に分けて書く。

**残る設計上の観察 (判断ではない)**: 受け手の算出が 4 通りに散っているので、新しい書き手が
増えるたびに同じ選択を各自がやり直す。前例 (2026-08-27) と今回の 2 件で、同じ型の見落としが
3 回起きている。歯止めを付けるなら「建物の出来事の受け手」を一箇所に持たせる形になるが、
これは実装の話であって、いま判断が要ることではない。

---

## 2. アイテム操作の記録は誰に届くのか (FLOW-16)

**1. 期待の根拠**

前段の台帳 `inventory.md:1526` が「`heard_by` を渡さない呼び出し元の host 行は誰の記憶にも
届かない」と書き、同時に「アイテム系が持っている代替経路 (`broadcast_item_event` →
`record_persona_event`)」があるとも書いている。この 2 つ目の前提を確かめるのが今回の仕事だった。

**2. 現在の結果**

**確定 (読んだ)**: `_append_building_history_note` (`saiverse/saiverse_manager.py:1018-1024`) は
受け手を渡さない。上流の呼び出しは 10 箇所ある。

| 場所 | 出来事 |
|---|---|
| `manager/items.py:362` | ペルソナがアイテムを拾った |
| `manager/items.py:422` | ペルソナがアイテムを置いた |
| `manager/items.py:475` | ペルソナがアイテムを使った |
| `manager/items.py:897` | ペルソナが文書を作った |
| `manager/items.py:994` | ペルソナが画像を生成した |
| `manager/items.py:1077` | ユーザーが画像を上げた |
| `manager/items.py:1165` | ユーザーが文書を上げた |
| `manager/items.py:1265` | ユーザーが音声・動画を上げた |
| `manager/items.py:1614` | ペルソナがアイテムを移した |
| `api/routes/chat.py:954` | 添付の取り消し (発言が拒否されたとき) |

**確定 (実測)**: この行はどのペルソナの記憶にも入らない (再現コードの [2])。

**確定 (読んだ) — 前段の前提の訂正**: 代替経路とされた
`broadcast_item_event` (`manager/items.py:307-311`) は `record_persona_event` を呼び、
`persona_event_log` テーブルへ pending の行を書く (`manager/persona_events.py:51-95`)。
**その行を読み出してペルソナのコンテキストへ載せる呼び出し元が、リポジトリのどこにも無い。**
`PersonaCore.fetch_pending_events` (`persona/core.py:214`) と `archive_events`
(`persona/core.py:224`) を repo 全体 (`.py` / `.ts` / `.tsx` / `.json` / `.md`、
`.venv` と worktree を除く) で検索して、定義以外の一致はゼロだった。
唯一もう一人の書き手である `inject_persona_event`
(`builtin_data/phenomena/inject_persona_event.py:54-70`) は、この受信箱とは別に
Pulse を直接投げて本文を届けているので、あの経路だけは動いている。
**つまり `persona_event_log` は書き込み専用の台帳になっていて、アイテム操作の
「行為者への記録」も「同室者への通知」もそこで止まっている。**

**確定 (読んだ) — ではペルソナは何を知るのか**: 3 つある。

1. **行為者本人**は tool の戻り値で知る。`pickup_item` などは `actor_msg`
   (「「赤い鍵」を拾った。」) を戻り値として返し、それが Playbook の TOOL ノードの
   出力になる。記憶の経路が死んでいても、その回の思考の中では本人に見えている。
2. **同室のペルソナ**は「部屋の様子」の照合で知る。部屋の様子の束にはその部屋のアイテムが
   1 個ずつパッケージとして入っており (`builtin_data/tools/get_visual_context.py:634-644`)、
   `_detect_room_state_changes` (`sea/head_pipeline/integration.py:811`) が**そのペルソナ自身の
   次の Pulse の頭で**前回の束と照合して、増えた物は全文で、消えた物は名前の一行で知覚へ積む。
3. **建物のシステムプロンプト**にも現在のアイテム一覧が入る
   (`manager/items.py:211-241`、見出しは「## 現在地にあるアイテム」)。

**確定 (読んだ)**: ユーザーは全部見える。`/api/chat/history` は `heard_by` で絞らない
(`api/routes/chat.py:293-302`、note-box の host 行を残す旨のコメントつき)。

**3. 差**

出来事の種類ごとに、居合わせたペルソナが受け取る結果はこうなる。

| 場面 | 知るか |
|---|---|
| A が部屋に居て、B が入ってきた | **知る**。ただし建物の記録経由ではない (入退室の行は取り込み側が意図的に読み飛ばす — `get_building_messages.py:255-257`)。入室の瞬間に `saiverse/dynamic_state.py:146-166` が在室者全員の知覚へ差分を積む |
| A が部屋に居て、B が「赤い鍵」を拾った | **条件つきで知る**。「赤い鍵が部屋から消えた」は A の次の Pulse の頭で分かる。**「B が拾った」は届かない** |
| A が部屋に居て、B が文書を作った | **条件つきで知る**。「新しい文書が現れた」は次の Pulse で全文が届く。**「B が作った」は届かない** |
| A が部屋に居て、ユーザーが画像を上げた | **条件つきで知る**。物が現れたことは届く。**「ユーザーが上げた」は届かない** |
| A が部屋に居て、添付が取り消された | **条件つきで知る**。物が消えたことは届く。取り消しという経緯は届かない |
| 行為者 B 自身 | **その回は知る** (tool の戻り値)。記憶の受信箱には残らない |

つまり欠けているのは **「誰がやったか」という帰属**であり、**「何が部屋にあるか」ではない**。

**4. 既存方針で解けるか**

**判断が要る。ただし白紙の問いではない。** 既存の裁定が片側を既に決めている。

`docs/intent/room_state_packages.md` §4 の表に、まはーの裁定がそのまま載っている。

> | 消えた (移動・収納・拾得・削除) | label の一行だけ。理由は描き分けない
> (見ていないなら消えたことしか分からないのが自然 — まはー裁定) |

この裁定は**部屋の様子という経路について**「見ていないなら消えたことしか分からない」を
選んでいる。今回の争点は、その前提「見ていないなら」が成り立たない場合 — つまり
**その瞬間、実際にその部屋に居たペルソナ**に、帰属を届けるかどうかになる。
`heard_by` は書き込みの瞬間の在室者を記録する仕組みなので、「見ていた人」を
機構として区別できるのはここだけになる。

- **具体的な一場面**: ミラさんと稟乃さんが同じ部屋に居る。ミラさんがテーブルの上の
  「赤い鍵」を拾ってポケットに入れる。いまの実装では、稟乃さんの次の Pulse で
  「赤い鍵」の一行が部屋の様子から消えるだけで、鍵がどこへ行ったのかは分からない。
  目の前で相手がそれを取ったのに、気づかない。
- **推奨案 (私の案であって、決定ではない)**: `_append_building_history_note` に
  書き込み時点の在室者を渡す。前例 (2026-08-27 の中断通告) と同じ直し方で、
  取り込み側は host 行を `<system>[部屋名] ミラが「赤い鍵」を拾いました。</system>` の形で
  記憶へ入れる (この経路は World Event で既に動いているものと同一)。
- **それで変わる体験**: 同室のペルソナが「誰が何をしたか」を認識できるようになる。
  代わりに、アイテム操作 1 回につき 1 行がその部屋の全ペルソナの記憶に増える。
  同じ出来事について、部屋の様子の照合 (物が増減した) と建物の記録 (誰がやった) の
  2 つが別々に届く形になるので、重複と読めるかどうかがまはーの判断点になる。
- **判断の分かれ目を一文で**: 「居合わせたペルソナに、物の増減だけでなく行為者まで
  知らせるか」。知らせないと決めるなら、いまの挙動が正しいことになり、
  **`heard_by` を渡さない理由をコードか設計文書に書くのが残作業**になる
  (前例の書き方は `builtin_data/tools/observer_read.py:85-91` の 3 行 = 現状・理由・復帰条件)。

**別件として切り出すべき、判断の要らない不具合**: `persona_event_log` が書き込み専用に
なっていること。上の判断がどちらに転んでも、`broadcast_item_event` と
`record_persona_event(persona_id, actor_msg)` の 7 箇所
(`manager/items.py:311,350,411,460,891,988,1598`) は誰にも読まれない行を書き続けている。
これは配線が切れているだけで、既存方針 (`docs/issues/` の未解決課題として起票し、
使わないなら消す) で処理できる。既存の起票は見つからなかった —
`docs/issues/event_delivery_reachability_gaps.md` は Pulse の起動到達の話で、この受信箱は
扱っていない。

---

## 3. Observer の閾値通知は誰に届くのか (FLOW-17)

**1. 期待の根拠**

`docs/intent/observer.md:131` が設計意図を明示している。

> **通知判定**: `NOTIFY_RULES_JSON` を評価 … ヒットしたら
> `add_building_event(role='host', event_type='observer_alert', ...)` (`manager/history.py:90`) で
> **Building の文脈に注入**。既存の Building event / `STATUS_ALERT` パイプラインに乗せ、
> Observer 独自の通知経路は作らない。

同 `:167` も「既存経路を使う。Observer 専用の通知チャネルを新設しない」と書いている。
「Building の文脈に注入」は、既存パイプラインがペルソナの記憶へ運ぶことを前提にした文になっている。

**2. 現在の結果**

**確定 (読んだ)**: `_notify_building` (`saiverse/observer_manager.py:707-728`) は
`add_building_event` を受け手なしで呼ぶ。**確定 (実測)**: その行はどのペルソナの記憶にも
入らない (再現コードの [2])。

**確定 (実測、追加で見つけた点)**: `_notify_building` が渡している
`"event_type": "observer_alert"` は **DB の列に落ちない**。
`serialize_building_message` (`database/building_messages.py:350-368`) は
`metadata.event.type` だけを `event_type` 列に写し、msg の最上位の `event_type` は読まない。
実測で `event_type column = None` を確認した。つまり書かれた行は、後から
「これは Observer の通知だった」と機械的に判別できない形で残る。

**確定 (読んだ)**: それでも**観測値そのものは届く**。観測値は `Fixture.STATE_JSON` の
最新値として保存され (`saiverse/observer_manager.py:343-414`)、部屋の様子の束の設置物
パッケージに「最新観測値: …」の行として載る
(`builtin_data/tools/get_visual_context.py:661-700`、束への取り込みは `:651-656`)。滞在中の Pulse の頭の照合が
この変化を拾う。

**3. 差**

期待は「閾値を超えたら、その部屋のペルソナが気づく」。現在の結果は
**「値の変化には気づくが、閾値を超えたという語りは誰にも届かない」**。
`NOTIFY_RULES_JSON` に書いた `message_above` / `message_below` の文面 —
設定した人が「これを伝えたい」と書いた本文そのもの — が、ユーザーの画面にだけ出て、
ペルソナには一度も渡らない。

**4. 既存方針で解けるか**

**未確認の分岐が 1 つあり、そこで解ける方針が変わる。**

- **配線として直す道**: 前例 (2026-08-27) と同じく在室者を渡せば、通知は
  `<system>[部屋名] 室温が 32 に上昇 (閾値: 30)</system>` の形でペルソナの記憶に入る。
  intent の文言どおりになる。`event_type` を `metadata.event.type` へ移すのも同じ直しに入る。
- **未配線として明示する道**: Observer は「意図的な未配線」の側に置かれている可能性がある。
  `builtin_data/tools/observer_read.py:85-91` に「オブザーバー機能自体がまだ利用できないため…
  復帰条件: オブザーバーの出荷時に `spell_visible=True` へ戻す (2026-09-01 裁定)」とある。
  スペルが隠されているなら、閾値通知だけ配線を直しても、ペルソナは設置物を能動的に読めない
  ままになる。
- **確かめれば決まること**: 2026-09-01 の裁定が Observer 機能**全体**の出荷保留なのか、
  スペルの可視性だけなのか。前者なら、この件は既に立っている共通議題
  「v0.4.0 以降へ回すための意図的な未配線を、どこに、どう書くか」
  (`flows/C_world.md:786-791`) に合流して**新しい質問にはならない**。後者なら配線の直しになる。
  裁定の原文の所在を私は確かめていない (repo 内で見つけたのはこのコード内コメントだけ)。

→ まはーへ上げるなら「Observer は v0.3 系で出すのか」の一問だけで、閾値通知の受け手は
その答えから従属して決まる。**閾値通知の受け手を独立の質問にしない。**

---

## 4. 補足: ゲームのセッションログでは、受け手のない行がユーザーにも見えない

**1. 期待の根拠**

無い。私が経路を追っている途中で見つけた副次の観察で、誰かが期待を書いた文書は無い。

**2. 現在の結果 (確定 — 読んだ。実行はしていない)**

通常のチャット履歴 `/api/chat/history` は `heard_by` で絞らないので、受け手のない行も
ユーザーの画面に出る。一方、Region のセッションログ
`GET /api/world/regions/{region_id}/game/log` (`api/routes/world.py:402-441`) は
`viewer_id=str(manager.user_id)` を渡し、`fetch_game_session_log`
(`database/building_messages.py:171,219`) が `heard_by` にユーザー ID を含む行だけを返す。
`heard_by=[]` の行はこの条件を満たさない。

**3. 差**

ゲーム進行中の Region で、その場のアイテム操作の記録と Observer の閾値通知は、
**セッションログのビューからは消える**。同じ出来事が通常のチャット画面には出て、
セッションログには出ないという食い違いになる。

**4. 既存方針で解けるか**

**解ける。** 2 節・3 節で受け手を渡す形に直せば、この食い違いも同時に消える
(在室者にはユーザー ID も入るため — `manager.occupants` はユーザー ID を含む。
`builtin_data/tools/get_visual_context.py:603-606` がユーザー ID を在室者から分離している
ことがその証拠になる)。受け手を渡さないままにすると決めるなら、
セッションログの絞り込みをどうするかが従属して決まる。**独立の質問にしない。**

---

## 5. 未確認として残した範囲

1. **実機での確認はしていない。** 実測したのは隔離環境での
   「受け手を渡す/渡さない」→「取り込まれる/取り込まれない」の一続きだけ。
   本番のペルソナ・本番 DB には一切触れていない。
2. **部屋の様子の照合が実際に何を届けるかは、読みだけで確かめた。**
   アイテムが増減したとき、同室のペルソナの知覚に具体的にどんな文が積まれるかは、
   知覚バッファと提示窓を含む一続きを動かしていないので実測していない。
   束にアイテムと設置物が入ることと、照合が Pulse の頭で走ることはコードで確認した。
3. **2026-09-01 の「オブザーバーの出荷時に戻す」裁定の原文**を見つけていない。
   repo 内で見つけたのは `builtin_data/tools/observer_read.py:85-91` のコード内コメントだけ。
   これが Observer 機能全体の保留かスペルの可視性だけかで、3 節の帰結が変わる。
4. **`persona_event_log` に既に溜まっている pending 行の量**を数えていない
   (本番 DB を読まない方針のため)。書き手は 8 箇所あるので、稼働時間に比例して
   溜まり続けている読みになるが、実データは見ていない。
5. **`sea/head_pipeline/sections/building_occupants.py:22-25` の docstring が古い可能性**が
   ある。「move_entity が書いた host メッセージは auto_ingest を経由してペルソナの
   SAIMemory にも届く」と書いてあるが、取り込み側は occupancy 型の行を明示的に
   読み飛ばす (`get_building_messages.py:255-257`)。文書の直しの候補として記録するが、
   コメントの経緯を追っていないので断定はしない。

---

# a3: 終了 (アプリを閉じる・落ちる) のときに、何が失われるのか

**担当範囲**: FLOW-26 (終了して、次に開いたとき失われていない) / FLOW-09 の一部 / 元議題 3 の第 3 項。
**成果物**: 本文 + 再現コード `docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/a3_shutdown_utterance_survival.py` (実行済み・`ruff check` 通過)。

---

## 0. 前置き — 三つを混ぜないための段階表

依頼元の指示どおり、**利用者が送った入力 / ペルソナが生成した発話 / まだ生成されていない要求**を分けて追った。
先に段階表を置く。各段階の「ここで落ちたら何が残るか」は、根拠を各項目で示す。

| # | 段階 | 内容がある場所 | ここでプロセスが死んだら残るもの |
|---|---|---|---|
| 1 | 利用者が送信ボタンを押した直後 | ブラウザのメモリ (楽観表示) + HTTP のリクエスト本体 | 何も残らない (サーバーに届いていない) |
| 2 | `_persist_user_utterance` が返った直後 | **`building_messages` テーブル (commit 済み)** | 利用者の文が全部残る。ペルソナはまだ読んでいない (`ingested_by` が空) |
| 3 | ペルソナの Pulse が始まり、`auto_ingest` が走った | 上記 + ペルソナの `memory.db` (SAIMemory) + `ingested_by` に印 | 利用者の文が、建物の記録とペルソナの記憶の両方に残る |
| 4 | `emit_speak_start` が下書き行を置いた | 上記 + `content=""` の `assistant` 行 | **下書き行は content 空のまま残り、画面にも記憶にも出ない** |
| 5 | ストリーミングの断片が画面へ流れている | 上記 + ブラウザの画面のみ (DB には未反映) | 段階 4 と同じ。画面に出ていた文字はどこにも残らない |
| 6 | `emit_speak_finalize` / `_settle_interrupted_utterance` が通った | 下書き行に本文が入る + ペルソナの `memory.db` + `log.json` (メモリ) | 発話の本文が残る。中断の回は「言い切っていない」印と中断の通告つき |
| 7 | Beat が終わり、Pulse が終わった | 上記 + `pulse_cursors` (メモリ) | 発話は残る。読んだ位置の記録はまだメモリだけ |
| 8 | `_save_session_metadata` が走った | 上記 + `persona_pulse_cursor` テーブル + `log.json` (ファイル) | 全部残る |

**この表の要点は、書き込みの大半が段階の途中で個別に commit されていることである。**
終了処理でまとめて保存されるのは段階 8 の 2 つ (`log.json` と `persona_pulse_cursor`) だけで、
利用者の文もペルソナの発話もペルソナの記憶も、それより前に永続化を終えている。

### 終了の経路

| 経路 | 終了処理 (`shutdown_everything`) が走るか | 走行中の発話の後始末 |
|---|---|---|
| Ctrl+C (README が案内する形) | 走る (`atexit`、`main.py:619`) | 最大 8 秒待って締める |
| SIGTERM | 走る (`main.py:618`) | 同上 |
| コンソールウィンドウを [×] で閉じる (Windows) | **未確認** (OS の挙動なので repo からは決まらない) | 走らなければ後始末なし |
| タスクの強制終了 / プロセスのクラッシュ | 走らない | 後始末なし = 段階 4〜5 の発話が消える |
| **ブラウザ (画面) だけを閉じる** | **走らない。バックエンドは生き続け、生成も走り切る** | 該当なし (止めない) |

---

## 項目 1: 利用者が送った入力は、終了で失われるか (FLOW-26 / 元議題 3)

**1. 期待の根拠**

既存の仕様文書 `docs/intent/building_memory_unified.md:182` —
「発言 = durable insert が認知開始の前提条件」。
実装側の docstring も同じことを言っている (`manager/runtime.py:507-517`
`_persist_user_utterance`: "Durably store a user command before any Pulse or side effect starts.")。

**2. 現在の結果 (確定 — 読んだ + 実行した)**

- `manager/runtime.py:742-782` — `backend_worker` は最初に `_persist_user_utterance` を呼び、
  返り値が `None` (保存できなかった) なら**ペルソナを一体も起こさずに戻る**。
  そのとき利用者には「発言を保存できなかったため、応答処理を開始しませんでした。
  同じ内容を再送できます。」が出る。
- `_persist_user_utterance` は `insert_building_message_with_location_guard`
  (`database/building_messages.py:535`) を呼び、その中で `db.commit()` まで済ませる。
  つまり関数が返った時点でディスクに載っている。
- 実測: 再現コード `a3_shutdown_utterance_survival.py` の段階 1 で、Pulse を一度も起こさずに
  同じ関数を呼び、`building_messages` に 1 行あることを確認した。
- 次に開いたとき: 履歴 API は `building_messages` を読む (`api/routes/chat.py:293`) ので画面に出る。
  ペルソナ側は `ingested_by` にそのペルソナが載っていないので**未読のまま残り**、
  次の Pulse の冒頭 (`sea/runtime.py:308` の `auto_ingest_building_messages`) で読まれる。
  この取り込みは user / schedule / auto のどの Pulse でも走る
  (`sea/pulse_controller.py:566` 「All requests (user / schedule / auto) go through run_meta_user」)。

**3. 差**

差は無い。**利用者が送った文が終了で失われる経路は、追った範囲では見つからなかった。**
「送ったのに消えた」が起きるとすれば、それは保存前 (段階 1) にプロセスが死んだ回で、
そのときは利用者の画面にも保存されたという表示が出ていない。

**4. 既存方針で解けるか**

**解ける。** `building_memory_unified.md:182` の「durable insert が認知開始の前提条件」が
既にこの挙動を規定していて、実装がそれを守っている。まはーへの質問にしない。

---

## 項目 2: ペルソナの発話 — 締切に間に合った回 (FLOW-26 / 元議題 3)

**1. 期待の根拠**

- ユーザー原文 (issue に記載、2026-08-26 まはー裁定): 中断された発言について
  「それが本人の言い切りなのか外から止められたのかを知れる方がよい」
  (`sea/runtime_llm.py:2021-2022` が裁定として引用)。
- 既存の仕様文書 `docs/issues/orphaned_streaming_placeholder_cleanup.md` — 不変条件は
  「**node() は未確定の下書き行を残して終わらない**」。ステータスは
  「🟢 実装済み・検証待ち」で、停止経路は 2026-08-27 夜に実機で合格している。

**2. 現在の結果 (確定 — 読んだ + 実行した)**

終了処理は走行中の発話を最初に締める (`main.py:566-583`)。順序の理由がコメントに書かれている —
後始末は生成スレッドの中で走るので、MCP や llama-server を先に壊すと壊れた道具の上で走る。

`PulseController.shutdown` (`sea/pulse_controller.py:191-275`) は全要求の取り消しトークンに
`server_shutdown` を刻み、**後始末が終わって台帳から席が消えるまで**最大 8 秒待つ。
後始末の中身は `_settle_interrupted_utterance` (`sea/runtime_llm.py:1978-2135`) で、四つを揃える。

1. 下書き行を途中までの本文で確定させる
2. 「言い切っていない」印 (`_interrupted`) を画面へ渡す
3. 言いかけた本文を本人の記憶 (SAIMemory) へ書く
4. 中断があった事実を `host` 役で建物の記録へ置く (在室者 + 本人を `heard_by` に載せる)

実測 (再現コード 段階 3-A / 段階 4): 確定を通った行は `content` に本文が入り、
`_streaming_placeholder=False` / `_interrupted=True` になり、画面の履歴にも取り込みの候補にも出た。
`_interrupted` は行の metadata に残るので、**次に開いたときも「続きの生成」のボタンが出る** —
履歴 API が `interrupted` を返し (`api/routes/chat.py:66, 267`)、
画面は API の返り値をそのまま messages にしている (`frontend/src/app/page.tsx:755`)。

**3. 差**

差は無い。締切に間に合った回は、画面・建物の記録・本人の記憶・次回起動時のボタンまで揃う。

**4. 既存方針で解けるか**

**解ける。** 2026-08-26 の裁定と `orphaned_streaming_placeholder_cleanup.md` の不変条件が
そのまま当たっている。残っているのは同 issue の「実機での確認」で、
これは**判断ではなく検証の宿題**である。

---

## 項目 3: ペルソナの発話 — 締切に間に合わなかった回 (FLOW-26 / 元議題 3)

**1. 期待の根拠**

- 上と同じ `orphaned_streaming_placeholder_cleanup.md` の不変条件。
  ただし同 issue が塞いだのは「Beat が例外で落ちた回」で、
  **「後始末そのものが 8 秒以内に終わらなかった回」は塞がっていない。**
- 8 秒という数値と「締まらなければ諦める」を書いた設計文書は、
  前段 (E_background.md §5-1) が `docs/` 全体を grep して見つからなかったと報告している。
  私も `stop_all_active_generations` の呼び出し元を数え直したが、
  `main.py:573` は引数なしで呼んでおり、**env でも設定でも変えられない**
  (`saiverse/saiverse_manager.py:1339` の既定値 8.0 が唯一の出所)。

**2. 現在の結果**

**確定 (読んだ)**: 締切を超えると WARNING を出して先へ進む
(`sea/pulse_controller.py:268-274`「their draft rows may remain unconfirmed」/
`main.py:575-581`)。確定しなかった下書き行は `content=""` のまま DB に残る。

**確定 (実行した)**: 再現コード 段階 3-B / 段階 4 で、確定を通らなかった行について
- 画面の履歴 (`api/routes/chat.py:296-301` の `if msg.get("content")`) → **出ない**
- 取り込み (`builtin_data/tools/get_building_messages.py:205-207`) → **記憶に入らない**
を実際に数えて確認した (どちらも 0 件)。つまり**その発話の本文はどこにも残らない**。
中断の通告 (`host` 行) も、通告を書くのは `_settle_interrupted_utterance` の中なので、
締切に間に合わなかった回には**そもそも書かれない**。

**確定 (実行した)**: 通信が切れた利用者のための問い合わせ口は、この空行を「応答」と数えない。
`lookup_client_message_outcome` は `status=found, has_reply=False` を返した (段階 6)。
根拠は `_has_assistant_reply_after` が `content != ""` で絞っていること
(`database/building_messages.py:718-745`。docstring に
「中断の後片付けが空のまま閉じた行も発言ではない」と明記されている)。

**静的な疑い (実行して確かめていない)**: 8 秒を使い切るのは、プロバイダが応答しなくなった回だと
読める。取り消しは `CancellationToken` に刻まれ、生成側は chunk の合間に確認するので、
ネットワークの読み取りで止まっているスレッドはその読み取りが返るまで取り消しに気づけない。
**この読みは実測していない。**

**過去の実測 (issue に記録)**: 2026-05-19〜08-26 の 3 ヶ月で、確定しなかった空の下書き行が 32 件。
ただしこれは**当時まだ塞がっていなかった例外経路**の実測で、
「締切超過」で失われた件数の実測ではない。同 issue が別々に扱っている二つを混ぜないこと。

**3. 差**

利用者から見た差: 途中まで画面に流れていた文が、次に開くと**跡形もなく無い**。
「そこで何かが途切れた」ことすら画面に残らない (中断の通告も書かれていないため)。

ペルソナから見た差: 自分が言いかけたことが記憶にも建物の記録にも無い。
締切に間に合った回との違いは、**言い切っていない印つきで残るか、無かったことになるか**である。

痕跡は `backend.log` の WARNING 1 行だけで、次の起動には持ち越されない。

**4. 既存方針で解けるか**

**一部は解ける。残りは判断が要る。**

解ける部分 (質問にしない):
- **既に残っている空の行を掃かないこと**は決着済み。2026-08-26 のまはー裁定で
  「そのまま残す」と決まり、代わりに「空の発言として読み込まれたり UI に出たりしないこと」を
  保つ、と `orphaned_streaming_placeholder_cleanup.md` に整理されている。
  再現コードで、履歴 API と取り込みの両方がそれを守っていることを実測した。
- **8 秒を何秒にするか**は論点ではない。締切が効くのはプロバイダが固まった回だけで、
  延ばしても待たされる時間が延びるだけになる可能性が高い (ただしこれは静的な読み)。

判断が要る部分:
- **具体的な一場面**: 利用者が Ctrl+C を押した。ペルソナはちょうど喋っている最中で、
  LLM のプロバイダが応答を止めている。8 秒が過ぎ、プロセスは終了する。
  次に開くと、画面には利用者の発言だけがあり、ペルソナの返事は一文字も無く、
  中断があったことを示す行も無い。ペルソナも自分が言いかけたことを覚えていない。
- **この場面に効く既存の約束を探した結果**: 見つからなかった。
  `unknown_send_outcome_has_no_recovery_path.md` (解決済み) が守っているのは
  **利用者の発言の顛末**であって、ペルソナの発話の消失ではない。
  `stream_completion_is_not_proof_of_persistence.md` (解決済み) が作った保存完了の信号は
  「保存できたか」を呼び出し元へ返すが、**プロセスが終了して呼び出し元ごと消える回**は対象外。
- **判断が要る理由 (依頼元の指示どおり、私の価値観を決定として書かない)**:
  失ったことを建物の記録に置くと、その `host` 行は取り込みでペルソナの記憶へ写る
  (2026-08-23 の裁定)。つまり「機構が締切に間に合わなかった」という事実を
  ペルソナに読ませてよいかという問いになる。
  一方、共通規約 8 の「『記録し、明示し、選択してもらう』を、全操作で記録を増やす規則に変えない」は、
  記録を増やす側へ機械的に倒すことを禁じている。**どちらへ倒すかは決まっていない。**
- **それで変わる体験**: 倒し方によって、次に開いたときにペルソナが
  「私は何か言いかけたらしい」と知った状態で始まるか、何も知らずに始まるかが変わる。

**未確認として残すもの**: 締切超過が実際にどれくらいの頻度で起きるか。
`backend.log` の `some active generations did not settle before teardown` を数えれば分かるが、
本番ログの読み取りは行っていない (規約 4 の読み取りは許可されているが、
今回は「どれくらい起きるか」を判断材料に含めるかどうか自体が私の裁量外だと考えた)。

---

## 項目 4: まだ生成されていない要求は何か (FLOW-26 / FLOW-24 / 元議題 3)

**1. 期待の根拠**

前段 (E_background.md §6) は「待ちキューに残っていたアラームは破棄される」と書き、
FLOW-24 の「台帳は `accepted` と記帳している」との食い違いを共通議題として挙げている。

**2. 現在の結果 (確定 — 読んだ)**

**待ちキューに入れるのは `schedule` (アラーム) だけである。**
`sea/pulse_controller.py:48-88` の実行種別の表で `on_blocked="wait"` を持つのは `schedule` のみ。
`user` は `on_blocked="skip"`、`auto` / `autonomy` も `skip`、`meta_judgment` は別レーン。

したがって、
- **利用者の発言に対する応答要求は、待ちキューに残ることが無い。** 走れなければその場で `skipped` になり、
  ストリームが生きていれば「発言は受け取りましたが、返事が生まれませんでした。」が届く
  (`manager/runtime.py:863-895` の出口 3)。
- 終了処理中は `submit` も `_process_queue` も新規登録を拒む
  (`sea/pulse_controller.py:299-308, 602-616`)。破棄されるのはアラームの待ち要求だけ。

**3. 差**

前段が「まだ生成されていない要求」として一括りにしていたものは、実際には
**アラーム 1 種類だけ**である。利用者の発言は「破棄された要求」ではなく、
**まだ誰にも読まれていない建物の記録の行**として残っている (項目 1)。
この二つは受け手から見て全く別で、前者は再登録の設計が要るが、
後者は次の Pulse の取り込みが自動的に拾う。

**4. 既存方針で解けるか**

- **アラームの破棄と `accepted` の食い違い**は FLOW-24 の担当 (同じ成果物ディレクトリの
  `repro/b12_alarm_accepted_vs_executed.py` が扱っている)。**ここでは重ねて起票しない。**
- **利用者の発言が待ちキューに残らないこと**は、既存の設計どおりで解ける
  (`on_blocked="skip"` に「Don't retry interrupted user messages」とコメントがある)。

---

## 項目 5: 画面 (ブラウザ) を閉じたときと、接続が切れたとき (FLOW-26 / 元議題 3)

**1. 期待の根拠**

- ユーザー原文の直接の引用は見つからなかったが、**裁定の記録はある**。
  `manager/runtime.py:1107-1119` に、2026-08-26 に「画面が閉じたら止める」を入れて**撤回した**経緯が
  コメントとして残っている —
  「SAIVerse のペルソナはブラウザが開いているかどうかと無関係に生きている。
  画面を閉じたことを理由に認知を打ち切ると、ユーザーの発言だけが残って返事が生まれない状態を
  こちらから作ることになる」。
- `stream_completion_is_not_proof_of_persistence.md` のステータス行に実機検証の結果がある —
  「読み手が切断しても生成は走り切って `status=saved` で保存され (ログで確認)」。
- `unknown_send_outcome_has_no_recovery_path.md` のステータス行に、タブを閉じた場合の裁定がある —
  「タブごと閉じた回は問い合わせの主体 (ページ) が消えるため案内は出ず、
  開き直した履歴 + ポーリングがサーバーの真実を映す — これは設計どおり」。

**2. 現在の結果 (確定 — 読んだ)**

- 生成は `backend_worker` という別スレッドで走り、結果をキュー経由で HTTP ストリームへ流す。
  ストリームの `finally` は自分が置いた stop_event とコールバックを片付けるだけで、
  生成の取り消しはしない (`manager/runtime.py:920-930`, `1107-1124`)。
- したがって**ブラウザを閉じても、バックエンドのプロセスが生きている限り発話は完成し、保存される。**
  これは「終了」ではない。
- 画面を開き直すと、履歴 API が `building_messages` から真実を返す。

**3. 差**

差は無い。ただし前段 (FLOW-26) の「終了」の一覧には
「画面を閉じるがバックエンドは生きている」が独立した経路として書かれていないので、
**正常挙動の記述としては、この経路を「終了ではない」と明示する価値がある** (記述の整理作業)。

**4. 既存方針で解けるか**

**解ける。** 2026-08-26 の撤回の記録と、2026-08-29 の二つの issue の実機検証済みステータスが
そのまま答えになっている。まはーへの質問にしない。

**注意 (memory の `feedback_review_prescriptions_vs_world_premises` に該当)**:
この件は「クライアントが切れたら止める」という一般的な処方が、
SAIVerse の前提 (住人は見られていなくても生きている) では逆になる場所である。
今後のレビューで同じ処方が上がってきたら、この裁定を先に見せること。

---

## 項目 6: `manager.shutdown()` の最後の保存が、途中の失敗で飛ぶこと (FLOW-26)

**1. 期待の根拠**

前段 (E_background.md §5-2) が「静的な疑い」として挙げ、
「実行して確かめていないので影響の大きさは断定しない」と留保した項目。
既存の裁定は無い。**同じ形の手当て (各ステップを個別に try で包む) は実行台帳の掃除 tick には入っている**
という前段の指摘は、私も確認していない (未確認)。

**2. 現在の結果 (確定 — 読んだ。実行はしていない)**

`saiverse/saiverse_manager.py:1098-1174` の `shutdown()` で、try で包まれていないのは
`set_user_login_status` / `conversation_managers` のループ / `integration_manager.stop()` /
`schedule_manager.stop()` / `_emit_trigger(SERVER_STOP)` / `phenomenon_manager.stop()` /
**全ペルソナの `_save_session_metadata()` のループ** / `_save_modified_buildings()`。
どこかで例外が出ると、それ以降は実行されない。
ペルソナのループ自体も包まれていないので、**1 体目の保存が失敗すると 2 体目以降は保存されない。**

失われるものを追った結果 (前段の記述より範囲が狭い):

- `_save_modified_buildings()` は既に no-op (`manager/history.py:86-88`)。何も失われない。
- `_save_session_metadata()` が保存するのは 2 つだけ (`persona/mixins/history.py:130-138`):
  - **`log.json`** (`history_manager.save_all()`、`persona/history_manager.py:1050-1063`)。
    起動時に `persona.messages` へ読み込まれる (`persona/bootstrap.py:41-48`)。
    これを読む本番の経路として見つかったのは
    `HistoryManager.should_recall_persona` (`persona/history_manager.py:724-750`) —
    「直近の文脈に相手が居るか」の判定。**巻き戻ると、直前まで話していた相手が
    「久しぶりの相手」に見えて、不要な過去会話の想起が積まれうる。**
    (これは静的な読みで、実行して確かめていない。同型の実害は
    `docs/issues/perception_state_pushed_at_event_time.md` に記録がある。)
  - **`persona_pulse_cursor` テーブル** (`_save_conscious_log`)。
    ただしこの関数は自分の例外を内側で捕まえてログに落とすので、
    **これ自体がループを止めることは無い。** 止めるとすれば `save_all()` の
    `write_text` である。

**カーソルが巻き戻ったときに何が起きるか (前段の記述の訂正)**:
前段は「カーソルが失われると、次に開いたときにペルソナが『どこまで読んだか』を見失う」と書いた。
実際には**古い値に巻き戻る**のであって、失われるわけではない。巻き戻った分は再走査されるが、
`ingested_by` (取り込みのたびに DB へ書かれる 1 件ごとの印、
`builtin_data/tools/get_building_messages.py:100-121`) が付いているので
`("consumed", ...)` で飛ばされ、**記憶に二重には入らない**
(`builtin_data/tools/get_building_messages.py:200-203`)。

**ただし反例がある (自分の観測を踏み越えないための注記)**:
巻き戻りではなく**行が一度も書かれていない**場合は別で、
`_ingest_round` は「起動時にひかえた末尾」を既読の境界にする
(`builtin_data/tools/get_building_messages.py:339-371` /
`manager/initialization.py:204-263`)。
このとき、前のセッションで届いた未読のメッセージは**読まれないまま既読になる**。
この形の害は既に起票されていて、まはーの裁定も出ている —
`docs/issues/legacy_cursor_import_failure_is_silent.md`
(「実害は『本当に未読だった過去を読み飛ばす』まで下がっている」、優先度 low、2026-08-16 後回し)。

**3. 差**

利用者から見た差: ほぼ無い。会話そのものは DB が正本なので消えない。
ペルソナから見た差: 直前まで話していた相手を「久しぶり」と扱いうること (静的な疑い)。

**4. 既存方針で解けるか**

**解ける (判断は不要と見ている)。** 理由は二つ。

1. 失われるものが特定できた — `log.json` の巻き戻りだけで、会話も記憶もカーソルの意味も失われない。
   前段が留保していた「影響の大きさ」は、この範囲に収まる。
2. 直し方も既存の型がある — ループを 1 体ずつ try で包み、`_save_conscious_log` と同じく
   例外をログに落として次へ進む。新しい概念も新しい記録も要らない。

**ただしこれは不具合の疑いであって、修正の承認ではない。** 今回の成果物は判断整理の材料である。

**未確認**: `save_all()` の `write_text` が実際に失敗する条件 (ディスク満杯・権限) を踏んでいない。
また前段が言う「実行台帳の掃除 tick に同じ手当てが入っている」は確認していない。

---

## 項目 7: 次に開いたとき、返事の来なかった発言へ戻る道が無い (FLOW-26 / 元議題 3)

**1. 期待の根拠**

- `docs/issues/archive/unknown_send_outcome_has_no_recovery_path.md` (解決済み) の
  問題意識 — 「ユーザーが手で打ち直すと、同じ発言がペルソナの記憶に二度残る」。
  これを塞ぐために `/api/chat/retry` (発言はそのままに応答だけを起こす口) と
  `/api/chat/message-outcome` (顛末の問い合わせ口) が作られた。
- 同 issue のステータス行 — 「タブごと閉じた回は…開き直した履歴 + ポーリングが
  サーバーの真実を映す — これは設計どおり」。

**2. 現在の結果 (確定 — 読んだ)**

サーバー側の道具は揃っている。

- `/api/chat/retry` の門番は「その発言より後に本文のある assistant 行があるか」だけを見る
  (`api/routes/chat.py:1213-1257`)。**再起動を跨いでも判定は同じ**で、
  返事の来なかった発言は今も再送の対象になる。
- 問い合わせ口も `has_reply=False` を正しく返す (項目 3 で実測)。

**画面側だけが、再起動を跨げない。**

- 「再送」ボタンと「取り消す」ボタンは `needsRetry` が立った発言にだけ出る
  (`frontend/src/app/page.tsx:3481-3492` / `:3495-3506`)。
- `needsRetry` を立てるのは `markRetryable` だけで (`frontend/src/app/page.tsx:1631-1637`)、
  これは**走行中のストリームの中でしか呼ばれない**。
- 履歴 API の返り値に相当する項目が無い (`ChatMessage` に `interrupted` はあるが
  `needs_retry` は無い。`api/routes/chat.py:40-66`)。
  画面は履歴の返り値をそのまま messages にする (`frontend/src/app/page.tsx:755`) ので、
  **ページを開き直すと印は消える。**
- 顛末の問い合わせ (`consumeReplyStream` の中、`frontend/src/app/page.tsx:1709-1717`) も
  走行中のストリームの中だけで、履歴の読み込み時には呼ばれない。

対比: ペルソナ側の「続きの生成」は `_interrupted` が行の metadata に永続していて
API も返すので、**再起動を跨いで残る**。同じ「一押しで応答を起こす口」なのに、
利用者の発言側だけが跨げていない。

**3. 差**

利用者から見た差: アプリを閉じた時点で返事が来ていなかった発言について、
次に開くと「返事が来ていない」ことを示すものが何も無く、
その発言に対して応答をもう一度求めるボタンも、取り消すボタンも出ない。
**発言そのものは残っているので、内容は失われていない。**
失われているのは「その発言に戻る導線」である。

なお、その発言はペルソナから見れば未読のまま残っているので、
次の Pulse (利用者が次に話しかけたとき、または自律の Pulse) で取り込まれる。
つまり**放っておいても読まれる**が、それがいつ起きるか・返事になるかは
Playbook 次第で、利用者からは見えない。

**4. 既存方針で解けるか**

**判断が要る。ただし論点は狭い。**

- 解けている部分: サーバーの門番と問い合わせ口は既に「再起動を跨いでも正しく答える」形になっている。
  作るとすれば画面側だけで、新しい概念も新しい記録も要らない。
- **具体的な一場面**: 利用者が「明日の予定を教えて」と送り、返事が始まる前にアプリを閉じた。
  次に開くと自分の発言だけがあり、ボタンは何も無い。もう一度同じことを打つと、
  ペルソナの記憶に同じ質問が二度残る — これは
  `unknown_send_outcome_has_no_recovery_path.md` が塞ごうとしたものそのものである。
- **推奨案**: 履歴 API が返す情報だけで画面が判定できる。
  `_has_assistant_reply_after` と同じ物差し (その発言より後に本文のある assistant 行があるか) は
  既に共有関数になっているので、履歴の返り値に載せるか、
  画面が読み込んだ履歴に対して同じ判定を自前で走らせる (画面には既に同じ物差しの実装がある —
  `frontend/src/app/page.tsx:1639-1664` の印の鮮度の useEffect が、
  まさに「後ろに本文のある assistant 行があるか」を走査している)。
- **それで変わる体験**: 次に開いたとき、返事の来なかった発言に「再送」が出る。
  同じ質問を打ち直さずに済む。
- **決めるのはまはー**: これを v0.3 の範囲に入れるかどうか。
  `unknown_send_outcome_has_no_recovery_path.md` は v0.3 の門の内 (gate §2-18) として消化済みなので、
  **その門の約束が再起動を跨いだ場面まで及ぶのかは、私が決めることではない。**

---

## 項目 8: 先回り畳みの daemon スレッドは、終了処理で待たれない (FLOW-26 §5-4 / FLOW-09)

**1. 期待の根拠**

前段 (E_background.md §5-4) が「静的な疑い」として挙げ、
「編纂の途中結果がどう残るかは記憶領域 (FLOW-09) の担当で、**追跡していない**」と書いた項目。

**2. 現在の結果**

**確定 (読んだ)**: `_spawn_cold_precompaction` は `threading.Thread(..., daemon=True)` で立て、
join しない (`sea/session_lifecycle.py:4433-4458`)。
`shutdown_everything` にも `manager.shutdown()` にもこのスレッドを待つ処理は無い。
`PulseController.shutdown` が待つのは `_current` / `_current_meta` の台帳だけで、
先回り畳みはそこに載らない (Beat ロックは取るが、Pulse の台帳には席を作らない)。
したがって**走り出した編纂はプロセスの終了で切られる**。

**既存の不変条件 (読んだ)**: `docs/intent/chronicle_eviction.md:48` —
「**あらすじを引き当てられなかった範囲は退場そのものを見送る**。
圧縮区間は『生ログの代わりに digest を見せる』記録なので、digest の無い圧縮区間は
その範囲を黙って消すだけになるから。見送られた範囲は生ログのまま提示コンテキストに残り、
次の Metabolism で再挑戦される。」
同 `:139` —「あらすじが引けない圧縮区間は生ログのまま通す (fail-open)。
記憶の連続性が軽量化より上位なので、あらすじを失った範囲を黙って消さない。」

**未確認**: 上の不変条件は「編纂が空振りした回」について書かれたもので、
**「書き込みの途中でプロセスが死んだ回」に同じ保護が及ぶかは確かめていない。**
`sai_memory/arasuji/` の書き込み順序 (あらすじを書いてから anchor を進めるのか、逆か) と、
その間の commit の粒度を追っていない。追うには FLOW-09 側の材料が要る。
なお隣接する不確定 commit の話は
`docs/issues/absorption_indeterminate_commit_recovery.md` に起票済みだが、
そちらは「commit は確定したのに例外が返る」形の話で、プロセス終了とは別である。

**3. 差**

**現時点では差を書けない。** 「何が失われるか」を追えていないので、
利用者・ペルソナが受け取る結果の差として書ける材料が無い。

**4. 既存方針で解けるか**

**未確認。** 決めるために確かめるべきことは二つ。

1. 編纂の書き込みが「あらすじ → anchor 前進」の順で、かつ順序が commit の境界で守られているか。
   守られていれば、途中で切れても生ログは残り、次回の Metabolism が再挑戦する
   (= `chronicle_eviction.md:48` の fail-open がそのまま効く) ので、**判断は要らない**。
2. 守られていないなら、anchor だけ進んで生ログが提示から外れる形があるかどうか。
   あれば「体験が黙って消える」形になり (同 doc `:137` が別の文脈で名指ししている害と同型)、
   そこで初めて判断の対象になる。

**この確認は FLOW-09 の担当範囲に属するので、ここでは引き取らない。**
前段が「追跡していない」と書いたことは正しく、私も追い切れていない。

---

## 未確認として残した範囲 (まとめ)

1. **Windows でコンソールウィンドウを [×] で閉じたときに `atexit` が走るか。**
   終了処理の入口は `atexit` と `SIGTERM` の 2 つだけ (`main.py:618-619`)。
   README は Ctrl+C を案内しているので案内どおりなら通るが、
   ウィンドウを閉じる操作やタスクの強制終了で通るかは OS の挙動で、repo からは決まらない。
   確かめるには実機で踏むしかない (今回は起動していない)。
2. **8 秒の締切を実際に超える回がどれくらいあるか。**
   `backend.log` の `some active generations did not settle before teardown` を数えれば分かるが、
   本番ログを読んでいない。
3. **`save_all()` の書き込みが失敗する条件を踏んでいない。**
   項目 6 のループ中断は静的な読みで、実際に途中で止まる場面を作っていない。
4. **先回り畳みの途中終了で何が残るか** (項目 8)。FLOW-09 の担当範囲。
5. **前段が言う「実行台帳の掃除 tick には同じ手当てが入っている」** (項目 6 の根拠) を確認していない。
6. **中断された生成の課金がどう記帳されるか** (前段 FLOW-26 §7 が FLOW-22 へ振った項目)。追っていない。

---

## 再現コード

`docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/a3_shutdown_utterance_survival.py`

実行方法と観測結果は同ファイルの docstring に書いた。実行は
`.venv/Scripts/python.exe docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/a3_shutdown_utterance_survival.py`。
一時ディレクトリに新しい SQLite を作り、`database/models.py` のスキーマを起こして
製品の永続層関数を直接呼ぶ。本番の `~/.saiverse/` には触れず、LLM も呼ばない。
`ruff check` 通過済み。

観測できたこと (すべて実行結果):

- 利用者の入力は、Pulse を一度も起こさないうちに `building_messages` へ commit されている。
- 確定を通った発話は本文が入り、`_interrupted=True` が metadata に残る。
- 確定を通らなかった発話は `content=""` のまま残り、画面の履歴も取り込みも 0 件で外す。
- 顛末の問い合わせ口は、空の下書き行を「応答あり」と数えない (`has_reply=False`)。

**この再現コードが確かめたのは永続層の側だけである。**
走行中の生成スレッドが 8 秒以内に締まるかどうかは、この経路では踏んでいない
(そちらは `tests/test_pulse_controller_shutdown.py` の 9 本が別に固定している —
取り消しの伝播、締切超過、遅れて登録された要求の回収、待ちキューの繰り上げ拒否など)。

---

# b12: 記録と実際の実行が食い違う 2 件 (FLOW-31 / FLOW-24)

担当 ID: b12。対象は `docs/audits/2026-09-09_product_normal_behavior/decisions.md`
第 3 部の 1 と 2。製品コード・既存テスト・既存文書は一行も変更していない。
git の状態も変えていない。LLM は呼んでいない。

再現コード (隔離環境・合成データ・LLM なし):

- `docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/b12_building_messages_corruption_detection.py`
- `docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/b12_alarm_accepted_vs_executed.py`

どちらも `ruff check` を通した。どちらも `SAIVERSE_HOME` を一時ディレクトリへ
向けている。

---

### 建物の会話記録が壊れたときの検出と復旧 (FLOW-31 / 初稿第 3 部 1)

**1. 期待の根拠**

- **既存 intent**: `docs/intent/building_memory_unified.md`「守るべき不変条件」
  (:41-47)。関係するのは 3「ログは race / クラッシュで壊れない」、
  4「seq は building 内で単調増加かつユニーク」、6「既存の永続データを失わない」。
- **同 intent の「残課題」6** (:316) が、この議題そのものを未決と自認している —
  「既存 quarantine 機構の DB 移行版: 現状の隔離システムは log.json 破損対応。
  DB 化後は『行レベル破損』は構造的に起きないが、何らかの隔離単位
  (building 単位の『整合性エラー検出時に新規書き込み拒否』など) を残すべきか」。
- **同 intent :283**「検算自身の沈黙を許さない ... 黙って 0 件を返すと
  『漏れ無し』と見分けがつかず、この節が防ごうとしている沈黙そのものになる」。
- **利用者向け説明**: `README.md:35`「自動バックアップ機能を搭載しており、
  起動するたびに会話データ等がコピー・保存されます」、
  `README.md:404`「データの損失・破損に備え、定期的なバックアップを推奨します」。
- **まはー裁定 (2026-08-16)**: 直せないものを毎起動バナーで出し続けると
  全バナーが読み飛ばされて検算という仕組み自体が死ぬので、利用者が
  「分かりました」と言える出口を置く (`tests/test_legacy_log_archive_api.py`
  の docstring に記録)。
- **調査担当 (前段) の提案**: 消えうる操作は事前に名指し + 控え + 事後の記録。

**2. 現在の結果**

**(a) 何が「破損」として扱われるのか — 定義は存在しない。**

`building_messages` について破損を定義したコードも文書も見つからなかった
(確定: `database/`, `manager/`, `saiverse/`, `api/` を grep して確認)。
リポジトリ内で「破損」の定義を持つのは旧 `log.json` 用の
`manager/initialization.py:394` `_quarantine_building` (不在 / 0 バイト /
空配列 / 正常 / 破損 の 5 状態判定) だけで、その 5 状態判定は 2026-05-20 の
`ec9eba70` で廃止されている (コミット本文に明記)。

**(b) 検出する処理があるか — DB の中身に対しては無い。** 以下すべて確定。

| 局面 | 実際にやっていること | 破損の検出か |
|---|---|---|
| 起動時 (旧ファイル検算) | `manager/initialization.py:266` が旧 log.json ↔ DB を突き合わせる | いいえ。**log.json が無い部屋は `saiverse/legacy_log_import.py:678` で即 `return None`** |
| 起動時 (水位) | `manager/initialization.py:204` が部屋ごとの MAX(seq) を数える | いいえ。数えられなかったときだけ warning アラート |
| 書き込み時 | `UniqueConstraint('building_id','seq')` | seq 重複だけは拒む (後述の形 3) |
| 読み出し時 | `database/building_messages.py:36` `deserialize_building_message` | いいえ。**壊れた JSON 列を黙って既定値へ落とす** |
| 起動時 (バックアップ) | `database/backup.py:90` の `PRAGMA integrity_check` | **コピー側にだけ走る。live DB には走らない** |
| `database/migrate.py` | スキーマのカラム差分のみ | いいえ (`integrity_check` は 0 件) |

`PRAGMA integrity_check` はリポジトリ全体で `database/backup.py` にしか無い
(確定: grep)。

**(c) 検出したとき何をするか — 実測した 4 つの形**
(`repro/b12_building_messages_corruption_detection.py`)

- **形 1 (列の JSON が壊れる)**: `fetch_building_messages` は例外を出さず全件
  返した。壊れた行は `heard_by=[]`、`ingested_by=[]` になり `metadata` は落ちた。
  **WARNING 以上のログは 0 件** (`metadata_json` だけ DEBUG 1 行、
  `heard_by` / `ingested_by` は無言)。
- **形 2 (通し番号の欠番)**: 真ん中の行を消しても残り 2 件をそのまま返す。
  欠番を数える処理は無い。
- **形 3 (通し番号の重複)**: `IntegrityError` で拒否された。**重複は起こりえない**
  ので検出の対象外。
- **形 4 (SQLite ファイルの物理破損)**: `PRAGMA integrity_check` は
  `database disk image is malformed` で倒れた (破損は実在)。しかし
  `fetch_building_messages` は**例外なしで 200 件全部返した** — 潰した頁が
  その問い合わせの経路に無かったため。つまり破損はファイルの中に黙って居座り、
  いつかその頁に触れた問い合わせが初めて落ちる。
  唯一その破損に触れた処理である `database/backup.py:188` `run_startup_backup` は、
  失敗を "Startup backup failed (non-fatal)" のログ 1 行にして飲み込み、
  例外も起動時アラートも出さない。

起動時アラート (`startup_alerts`) を作る箇所は 4 つだけで
(`manager/initialization.py:250, 308, 353, 487`)、487 は呼び出し元 0 件の
`_quarantine_building` の中にある。**したがって DB の破損を利用者へ知らせる
経路は存在しない** (確定)。

**(d) 撤去時に復旧手段が引き継がれたか — 引き継がれていない。**

- 隔離機構の追加は `6603192e`、撤去は `ec9eba70` (2026-05-20)。撤去コミットの
  本文に「5 状態判定 / quarantine 起動時バックアップ廃止: building_histories は
  legacy caller 互換のため空 dict で初期化のみ」と書かれている。
- `quarantined_buildings` を埋める唯一の関数 `_quarantine_building`
  (`manager/initialization.py:394`) の呼び出し元は 0 件 (確定: `.py` 全体を grep。
  前段の観察と一致)。`saiverse/saiverse_manager.py:109` で空 dict に初期化された
  まま、`GET /api/system/quarantine` は常に空、restore / reset は常に 404。
- 同じコミットで起票された `docs/issues/quarantine_path_dead_code_removal.md`
  自身が「選択肢 B (DB ベースの recovery 機構として再設計) は別 intent doc /
  feature として切り出すべき」と書いている。**その別 doc は作られていない**
  (確定: 「隔離単位」「DB ベースの recovery」「行レベル破損」で `docs/intent/` と
  `docs/issues/` を grep して、当たるのは問いを立てた本人である
  `building_memory_unified.md` と、この dead code issue の 2 件だけ)。
- **現行コードで解消していないことの確認**: `_quarantine_building` の呼び出し元は
  いまも 0 件で、`ec9eba70` 以後にこの経路を復活させた変更は無い
  (`git log -S "_quarantine_building" -- manager/` は追加コミットと撤去コミットの
  2 件のみ)。

**現存する復旧手段は 3 つ。**

1. `python database/backup.py --db <path> restore <backup>` — CLI のみ。
   世代は既定 10 (`database/backup.py:17`)。
2. `snapshot.bat restore <name>` — CLI のみ。
3. **旧 log.json が残っている部屋に限り、毎起動の検算が DB の欠けを見つけて
   取り込み直す。** 実測 (repro 「毎起動の検算の射程」): 両方の部屋の DB 行を
   全部消すと、log.json のある部屋だけ `not_imported` として検出され、
   log.json の無い部屋は「欠け無し」と判定された。
   これは log.json 向けの検算の副産物であって、DB のために設計されたものでは
   ない。**移行 (2026-05-20) 後に作られた部屋と、移行後の発言には効かない。**

**(e) 製品内で壊せる経路 (静的な疑い)**

`POST /api/db/tables/{table_name}` (`api/routes/db_manager.py:99`) は
`database.models` の全モデルを対象にした upsert なので、`building_messages` の
`heard_by` 等へ任意の文字列を書ける。`DELETE` は行を消せる (= 欠番)。
フロントエンドからの呼び出し元は無い (前段の観察と一致)。
**HTTP 経由では実行していないので、SQLAlchemy の型変換を挟んだときに形 1 /
形 2 の状態が作れるかは未確認。**

**3. 差**

- **利用者**: 建物の会話が壊れても**画面には何も出ない**。旧 log.json 時代は
  「隔離されました」のバナーと復元・リセットのボタンがあった (設計上)。いまは
  同じ事故が「履歴が短い」「誰かの発言が無い」という形で静かに現れるだけで、
  壊れたのか元からそうなのかを区別する手段が利用者側に無い。
- **利用者 (バックアップの約束)**: DB が壊れた瞬間から、README:35 が約束する
  「起動するたびのバックアップ」は黙って作られなくなる。既存の世代は消えない
  (`_prune_old_backups` は成功時にしか走らない) が、**新しい控えが増えなくなった
  ことは誰にも知らされない。** 復旧が最も要る局面で、控えが古くなり続ける。
- **ペルソナ**: `ingested_by` が空に落ちた行は「まだ誰も読んでいない」と読まれる。
  `heard_by` が空に落ちた行は「誰も聞いていない」ものとして扱われる。
  **列が空になることは実測したが、そこから先のペルソナの振る舞い (再転記が
  実際に起きるか、何件か) は追っていない (未確認)。**
- **運用者**: 戻し方が利用者向け文書に届いていない。`README.md` /
  `docs/user-guide/` / `docs/getting-started/` を grep して復元の手順は 0 件
  (自分で確認。前段の観察と一致)。

**4. 既存方針で解けるか**

**解ける (まはーへの質問にしない)**

- **dead code の撤去**: `docs/issues/quarantine_path_dead_code_removal.md` が
  起票済み。判断は不要。
- **起動時バックアップの失敗を利用者へ明示すること**: 同じ理由の手当てが同じ
  ファイルに既にある — `manager/initialization.py:250` は「水位を測れなかった」を
  warning アラートにしており、その理由は「黙って進むと正常と見分けがつかない」。
  intent の :283「検算自身の沈黙を許さない」がそのまま当たる。
  **同じ理由が当てはまる隣を直す話であって、決める余地は無いと見ている。**
- **復元手順を利用者向け文書に載せること**: FLOW-27 と共通の既存議題。
  「文書の置き場所を白紙の質問にしない」(owner_decisions 7) が当たる。

**解けない (判断が要る) — 1 点だけ**

**DB 側に隔離・復旧の単位を作るか。** intent doc の残課題 6 が未決のまま
残っている唯一の項目で、選択肢は doc 自身が書いている
(building 単位で整合性エラー検出時に新規書き込みを拒否する、など)。

- 具体的な一場面: 電源断のあとに起動する。ある部屋の履歴の途中が読めなくなって
  いる。いま起きるのは「履歴が短く表示される」だけで、バックアップも黙って
  作られなくなる。
- 推奨案: **隔離単位を新設する前に、「壊れたことに気づける」ところまでを既存の
  起動アラートに載せる。** 具体的には (i) `run_startup_backup` の失敗を
  `startup_alerts` に載せる、(ii) 起動時に live DB へ `PRAGMA integrity_check` を
  一度だけ走らせてアラートにする。CLAUDE.md「例外処理・救済機構が本体を覆い
  始めたら止まる」に照らして、復旧 UI を作り直す前に検出を置く順序を推す。
- それで変わる体験: 壊れたことが起動時に一度だけ見え、既に手元にある `.bak`
  世代へ戻す判断ができるようになる。いまは壊れたこと自体が見えない。
- **調査担当が想定した価値観をユーザーの決定として書かない**: 「隔離単位を
  作るかどうか」は未決のまま残す。上の推奨は順序についての提案であって、
  隔離単位の要否を決めたものではない。

**未確認**

- `POST /api/db/tables/building_messages` を HTTP 経由で実行したときの挙動。
- `ingested_by` が空になった行を、ペルソナが実際にどう扱うか (再転記の有無と件数)。
- Windows の WAL モードで live DB に `integrity_check` を走らせる費用。

---

### アラームの「受理」記帳と「実行完了」のずれ (FLOW-24 / 初稿第 3 部 2)

**1. 期待の根拠**

- **コード自身が書いている前提** — これが期待そのもの:
  「schedule は on_blocked="wait" — queued / cancelled は queue (復帰 queue) に
  残っていて消えないため accepted (前進) とする (handoff D4)」
  (`saiverse/schedule_manager.py:1042-1047`)。
- **既存 issue**: `docs/issues/event_delivery_reachability_gaps.md` ①
  (2026-07-31 起票、状態: 未着手) が「queued は『受付済みで消えない』として
  accepted 扱いする裁定 ... と矛盾する瞬間がある」と既に書いている。同 issue は
  「頻度・実害の観測を待って優先度を判断する」と保留の理由を書いている。
- **まはーの確定判断**: 「自律 OFF でも設定済みアラームは動く。これで正しい」
  (owner_decisions 3) — アラームは鳴る約束の側にある。
- **利用者向けの表示**: `frontend/src/components/ScheduleModal.tsx:337` は
  oneshot の `completed` を「(完了)」と表示する。

**2. 現在の結果**

**発火から実行までの段階 (時間順、すべて読んで確認)**

1. EventScheduler の予約が発火 → `ScheduleManager._handle_fire`
   (`saiverse/schedule_manager.py:634`)
2. 実行台帳に prepared → running を刻む (席取りの CAS)
3. `_execute_schedule` → `PulseDispatcher.dispatch_schedule_fire`
   (`saiverse/pulse_dispatcher.py:140`) → `PulseController.submit`
4. submit が受付の裁定を `request.dispatch_action` に記入
   ("execute" / "queued" / "skipped")
5. `_classify_dispatch_outcome` が型付き outcome へ分類
   (`saiverse/schedule_manager.py:1042`)
6. 精算: `executed` / `accepted` / `settled_skip` は**同じ枝**を通る
   (`saiverse/schedule_manager.py:783`) — 状態前進 + 台帳 applied → completed +
   `_do_register` で次回登録

**`accepted` (受理) を記帳するのはどの時点か — 実行が始まる前。**

ステップ 4 で `queued` が確定した瞬間に、ステップ 6 が「実行完走」と同じ精算を
する。oneshot は `COMPLETED = True`、interval は `LAST_EXECUTED_AT` を現在時刻に
書く (`saiverse/schedule_manager.py:1210-1268`)。台帳も completed で閉じる。
**実行そのものは、このあと待ち行列の中で始まる (か、始まらない)。**

**待ち要求が捨てられる条件** (`sea/pulse_controller.py`、すべて確定)

待ち行列に入るのは `on_blocked == "wait"` の種別だけで、それは `schedule`
(アラーム) のみ (`:48-66`)。user / auto / autonomy は待たずに skip される。

- **(A) 上限超過**: `QUEUE_LIMIT = 10` (`:28`)。超えると**いちばん古い要求を
  pop して捨てる** (`:418-430`)。記録は `LOGGER.error` 1 行、利用者への通知は無い。
- **(B) 終了処理**: `_process_queue` は `_shutting_down` が立っていたら繰り上げを
  拒む (`:602-635`)。`shutdown()` (`:191`) は `_queues` に一切触れないので、
  残った要求はプロセスと一緒に消える。この経路は本番の終了処理そのもので、
  `main.py:573` → `SAIVerseManager.stop_all_active_generations`
  (`saiverse/saiverse_manager.py:1339`) → `pulse_controller.shutdown` と繋がる。
- **(C) 中断**: 走行中のアラームが利用者の発話に中断されると、**復帰用の別の
  request** が待ち行列の先頭に積み直される (`:432-467`)。元の request は
  `runtime_outcome="cancelled"` になり、これも `accepted` に分類される
  (`saiverse/schedule_manager.py:1058`)。**復帰実行の顛末は ScheduleManager へ
  戻らない。**

**捨てられたときアラームの記帳はどうなるか — 受理済み (実行済み) のまま残る。**
失敗にも落ちず、再試行も来ない。

**再登録の判断は受理を見ている。** `_do_register` は accepted の枝の中で呼ばれ、
`_compute_next_fire_at` (`:499`) は DB の状態だけを見る。

- **oneshot**: `_next_oneshot_fire` (`:574`) は `COMPLETED` が True なら `None` を
  返す → 予約は cancel され、**二度と鳴らない**。
- **interval**: `_next_interval_fire` (`:588`) は `LAST_EXECUTED_AT + 間隔`。
  捨てられた時刻を起点に次回が来るので、落ちた回だけ失われる。
- **periodic**: 曜日 + 時刻から計算するので、落ちた回だけ抜ける。

**実測** (`repro/b12_alarm_accepted_vs_executed.py`。in-memory DB + 実
PulseController / PulseDispatcher / ScheduleManager、合成ペルソナ 1 体、
`run_meta_user` はフェイク、LLM なし)

| 経路 | 仕掛けたアラーム | DB の COMPLETED=True | 台帳 | 実際に走った回数 |
|---|---|---|---|---|
| A 上限超過 | 12 件 | **12 件** | completed 12 | **10 回** |
| B 終了処理 | 3 件 | **3 件** | completed 3 | **0 回** |
| C 中断 → 復帰失敗 | 1 件 | **1 件** | completed 1 | 完走 0 回・再試行の予約なし |

経路 A と B は「利用者と会話中のペルソナにアラームが鳴る」形で作った
(USER 優先度が走行中なので、アラームは中断せず待ち行列へ入る)。
経路 C は「走り出したアラームに利用者が話しかける」形。

**3. 差**

- **利用者**: 仕掛けた一回きりのアラームが鳴らないまま、アラーム一覧に
  **「(完了)」と表示される** (`ScheduleModal.tsx:337`)。鳴らなかったことを知る
  手段は backend.log の ERROR 1 行 (経路 A) か、何も無い (経路 B・C)。
- **ペルソナ**: そのアラームで起こるはずだった Pulse が起きないので、記憶にも
  何も残らない。「アラームが鳴らなかった」という事実自体が世界のどこにも残らない。
- **起きやすさ**: 経路 A は 1 体のペルソナに未処理のアラームが 11 件以上たまる
  必要があり、通常運転では稀 (issue の見立てと一致)。**経路 B と C はそうでは
  ない** — 経路 B は「会話中にアラームが鳴り、その会話が終わる前にアプリを
  閉じる」で成立する。経路 C は「アラームが鳴り始めた直後に話しかける」だけで
  成立する (損失になるのは復帰実行が失敗した場合)。
- **種類による差**: oneshot は永久に失われる。interval / periodic は次回は鳴り、
  落ちた回だけ失われる。

**4. 既存方針で解けるか**

**既に起票済み (再起票しない)**

経路 A と C は `docs/issues/event_delivery_reachability_gaps.md` の ① と ④ が
2026-07-31 に記録している。issue は「頻度・実害の観測を待って優先度を判断する」
と書いていた — **今回その観測を出した。** issue の記述は起票時点のままで、
**現行コードで解消していない。** 確認の内訳:

- `git log -S 'return "accepted", detail' -- saiverse/schedule_manager.py` は
  導入コミット `0a28ba43` (2026-07-21、schedule 発火の台帳化) の 1 件のみ。
  以後この分類を変えた変更は無い。
- `git log -S "QUEUE_LIMIT" -- sea/pulse_controller.py` も初出コミット
  `ba338459` の 1 件のみ。破棄の作法は変わっていない。
- 実測 (上の表) が、作業ツリーの HEAD (`7d7214be`) で 3 経路とも再現することを
  示している。

**issue に載っていない部分 — 経路 B (終了処理での破棄)**

`_shutting_down` と `_process_queue` の繰り上げ拒否が入ったのは `03d6ddaa`
(2026-08-29) で、issue (2026-07-31) より後。**同じ欠陥の族の新しい一件が、
issue に追記されないまま増えている。** 前段の初稿は E_background.md §6 でこれを
「共通議題」として拾っており、そちらの観察は正しい。

**解ける部分 (まはーへの質問にしない)**

対処の方向は issue が既に書いている — 「破棄でなく受付拒否 (submit 側へ False を
返す) にすれば、送信側の失敗経路 (backoff / 台帳) が既に在るので乗るだけで済む
可能性が高い」。ScheduleManager 側には `failed` → backoff 再試行の経路が実在する
(`_retry_or_give_up`、`saiverse/schedule_manager.py:856`) ので、`accepted` から
`queued` を外して受付拒否へ寄せれば、既存の再試行にそのまま乗る。
**新しい機構を足す判断ではない。**

**判断が要る部分 — 「捨てた事実を利用者へどう返すか」1 点**

ここは既存の裁定と衝突しうる。建物の記録に置くと、それはペルソナの記憶へ写る
(2026-08-23 の裁定)。E_background.md §8-1 が「機構の失敗をペルソナに読ませて
よいか」という同じ形の問いを既に立てているので、**別々の判断にせず一つの場面
としてまはーに出すのが妥当**と見ている。

- 具体的な一場面: 一回きりのアラームを 21:00 に仕掛けた。21:00 にちょうど
  そのペルソナと話していた。アラームは鳴らず、アラーム一覧には「(完了)」と出る。
- 推奨案: (i) 破棄をやめて受付拒否にし、既存の backoff 再試行へ乗せる。
  (ii) それでも尽きた回だけ、アラーム一覧の表示を実行の顛末が分かるものへ変える。
  (iii) 建物の記録には書かない (ペルソナの記憶を汚さない)。
- それで変わる体験: 会話中に鳴ったアラームが、会話が終わってから鳴る
  (いまも 10 件までは鳴っている)。鳴らなかった回は一覧で見分けられる。

**未確認**

- 経路 C で復帰実行が**成功**した場合に、実行の事実が台帳へ二重に残るか
  (受理時に completed で閉じているので追記されないと読んだが、実行していない)。
- 経路 A の ERROR ログがまはーの環境で実際に出た履歴があるか (本番ログは見ていない)。
- 経路 B が実機の終了処理で成立するか (配線は `main.py:573` まで読んで確認したが、
  実機のバックエンドは起動していない)。

---

## 前段の初稿の記述との照合

| 初稿の記述 | 今回の結果 |
|---|---|
| 「建物ログ側の隔離機構は入口が開かない状態」 | **正しい** (呼び出し元 0 件を自分で確認) |
| 「撤去だけでは復旧手段が旧ファイル側にしか無い状態が残る」 | **正しいが、旧ファイル側の復旧も部屋を選ぶ** — log.json の残っている部屋にしか効かず、移行後の発言には効かない |
| 「DB 自体の破損検出は確かめていない (要追加確認)」 | **無いことを確定させた。** ただし「起動時バックアップの失敗」という間接の触れ口が 1 つあり、それがログ 1 行に飲まれている |
| 「Pulse の待ち行列の破棄とアラームの accepted 記帳が食い違う」 | **正しい。実測で 3 経路を確認した。**ただし**既に issue 化済み** (2026-07-31) で、新規発見ではない。issue に無いのは終了処理の経路 (2026-08-29 に増えた) |
| 「数値 (上限 10 件) より記帳の食い違いの方が重い」 | **実測が支持する。**上限に届かなくても、終了処理と中断の 2 経路で同じ損失が出る |

---

# b3 — 配布物 (ZIP) の改行が受け取った人の環境で動く形か (FLOW-27 / FLOW-32 / 元議題 8)

担当 ID: `b3`。2026-09-10 実施。

## 結論を先に

**改行は壊れていなかった。** 配布 ZIP の中で `.sh` は 9 件すべて LF、
`.bat` 8 件と `.ps1` 6 件はすべて CRLF になっていた。受け取った人の環境で
`bad interpreter: /bin/bash^M` になる形にはなっていない。
前段が「実行していないので不明」と残した境界 (`F_ops.md` §5) は、**閉じた。**

しかもこれは私のローカルでの再現ではなく、**GitHub が公開している本物の
`SAIVerse.zip` を落として中身を見た結果**である。

以下、規約の 4 段構成で 3 項目に分けて書く。

---

## 実施した手順 (この報告の根拠)

再現コードは 3 本、いずれも `ruff check` 通過済み。

| ファイル | 何を確かめたか |
|---|---|
| `docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/b3_dist_line_endings.py` | `release.yml` と同じコマンドで ZIP を作り、23 スクリプトの改行を 1 バイトずつ判定。展開 → `git init` → `reset origin/main` → 更新前検査まで通す |
| `.../repro/b3_lf_batch_behavior.py` | LF だけの `.bat` が cmd.exe で実際にどう動くか |
| `.../repro/b3_zip_reset_drift.py` | 配布 ZIP の版が `origin/main` より古いとき、setup 直後に作業ツリーが汚れるか |

加えて、公開中の本物の配布物を読み取りのみで取得して照合した
(`https://github.com/maha0525/SAIVerse/releases/latest/download/SAIVerse.zip`、HTTP 200、21,535,109 バイト)。
**どこにも公開していない。タグも Release も作っていない。作業ツリーと git の状態は変更していない**
(使ったのは `git archive` / `cat-file` / `ls-tree` / `ls-files` / `log` / `rev-parse` などの読み取りのみ。
`git init` 以降は一時ディレクトリの中だけで実行し、終了時に消した)。

---

### 項目 1 — 配布 ZIP の中のスクリプトの改行 (FLOW-27 / FLOW-32 / 元議題 8)

**1. 期待の根拠**

利用者向け説明が約束している。`README.md:153` と `README.md:216` は
「最新版をダウンロード（ZIP）して任意の場所に解凍してください」と書き、
`README.md:139` は「ZIP で導入しても、その後の自動更新（`update.bat`）まで
手動 Git なしで動きます」と書いている。
つまり **ZIP を展開して `setup.sh` / `setup.bat` を叩けば動く**ことが約束である。

設計の指示書は `.gitattributes` にある。`*.sh text eol=lf` (11 行目)、
`*.bat text eol=crlf` (6 行目)、`*.ps1 text eol=crlf` (8 行目)、
既定は `* text=auto eol=lf` (3 行目)。
配布物の作り方は `.github/workflows/release.yml:17` の
`git archive --format=zip --prefix=SAIVerse/ -o SAIVerse.zip HEAD` 一本だけである。

**2. 現在の結果**

**確定 (実際に生成して中身を見た)**。

`git archive` は `.gitattributes` の `eol=` を**適用する**。
git が保管している中身 (blob) は対象 23 スクリプト全件が LF だが、
ZIP の中では 14 件が CRLF に変換されていた。内訳は `.bat` 8 件と `.ps1` 6 件で、
`eol=crlf` を指定した対象と完全に一致する。

| 種類 | 件数 | git の blob | 配布 ZIP の中 | 受け取る環境にとって |
|---|---|---|---|---|
| `.sh` | 9 | LF | **LF** | 正しい (bash が要求する形) |
| `.bat` | 8 | LF | **CRLF** | 正しい (Windows が期待する形) |
| `.ps1` | 6 | LF | **CRLF** | 正しい |
| `.cmd` | 0 | — | — | 追跡下に 1 件も無い (規則 7 行目は空振り) |

**LF でない `.sh` は 0 件。混在 (MIXED) も 0 件。**

**さらに、公開中の本物の ZIP でも同じだった (確定)**。
GitHub Actions が生成して利用者が実際に落とす `SAIVerse.zip` を取得して中身を見たところ、
23 スクリプトすべてが上の表どおりだった (`.sh` は全件 LF、`.bat` / `.ps1` は全件 CRLF、逸脱 0 件)。
私のローカル生成物と ZIP メンバー単位で突き合わせると、
**1913 メンバー・CRC 差分 0 件・パーミッション差分 0 件**で、違いは mtime だけだった
(`git archive` は書庫の作成時刻を打つのでファイル全体の hash は一致しない)。
公開版の `VERSION` は `0.3.11` で、`origin/main` の `VERSION` と一致する。

補足で 2 点、実測して確認した。

- **`.sh` の実行ビットは残る**。公開 ZIP の中の `.sh` 9 件はすべてモード `0o755` だった。
  Mac / Linux で `unzip` すればそのまま `./setup.sh` で起動できる。
- **`.bat` が LF だったらどうなるか**は、推測せず実行した (`b3_lf_batch_behavior.py`)。
  `setup.bat` / `start.bat` が実際に使っている構文 (`goto :label`、`for /f`、`call :sub`、
  括弧の複数行ブロック、`setlocal enabledelayedexpansion`) を詰めた `.bat` を
  CRLF 版と LF 版で作って cmd.exe に食わせたところ、
  **出力も終了コードも完全に一致した** (Windows 11 Pro 10.0.26200)。
  つまり今の Windows では LF だけの `.bat` も動く。
  `*.bat eol=crlf` が防いでいるのは「今の Windows で動かないこと」ではない。
  ただしこれはこの 1 台での観測なので、古い Windows へ一般化はしない。

**3. 差**

**差は無い。** 受け取った人が改行のせいで躓く箇所は見つからなかった。
Mac / Linux の利用者は `setup.sh` をそのまま実行でき、
Windows の利用者は `setup.bat` をそのまま実行できる。

**4. 既存方針で解けるか**

**解ける (というより、既に解けている)。**
`.gitattributes` (2026-05-23 の `091f25b1` で追加) が意図どおり機能しており、
`git archive` がそれを適用することを実測で確認した。
**まはーへの質問にする必要は無い。直す対象も無い。**

前段の `F_ops.md` §5 と `inventory.md` の「静的な疑い」「追跡できていない境界」は、
この項目については**閉じてよい**。

---

### 項目 2 — ZIP 展開 → setup → 更新前検査 (FLOW-27 / FLOW-32)

前段が「ここが一致しないと `README.md:139` の約束がそのまま FLOW-27 の
更新拒否に直行する」と書いた、その下流である。改行の話の帰結なので併せて実測した。

**1. 期待の根拠**

`README.md:139` の「ZIP で導入しても、その後の自動更新（`update.bat`）まで
手動 Git なしで動きます」。
`setup.bat:257-264` と `setup.sh:105-110` が `git init` → `git remote add origin` →
`git fetch origin` → `git branch -M main` → `git reset origin/main` を実行し、
`scripts/update_engine.py` の `assert_git_update_ready` が
`git status --porcelain -z --untracked-files=no` を見て、
1 件でも出れば `Working tree has local changes` で更新を拒否する
(`update_engine.py:756`)。

**2. 現在の結果**

**確定 (実行した)**。`b3_dist_line_endings.py` の §3。

配布 ZIP を展開し、setup と同じ順序で `git init` → `fetch` → `reset origin/main` を踏み、
その直後に更新前検査と同じコマンドを叩いた。結果は
**`core.autocrlf` が `true` / `input` / `false` の 3 通りすべてで空 (クリーン)** だった。
利用者の Windows の既定値 (`true`) を含めて、更新は問題なく始められる。

理由も確認できた。`.gitattributes` の `* text=auto` が checkin 側で正規化するので、
ZIP が届けた CRLF の `.bat` は比較の時点で LF に戻り、blob (LF) と一致する。
`git ls-files --eol` でも `setup.bat` が `i/lf w/crlf attr/text eol=crlf` と表示され、
作業ツリーが CRLF のままでも「変更なし」と判定されていた。

**3. 差**

**差は無い。** 改行を理由に `update.bat` が止まることはない。

**4. 既存方針で解けるか**

**解ける (既に解けている)。** まはーへの質問にしない。

---

### 項目 3 — 版がずれた場合 (同じ境界の、改行ではない方の変数)

項目 2 の結果は「配布 ZIP の版と `origin/main` が同じコミットのとき」の話である。
そこを踏み越えて「だから常に安全」と書かないために、ずれた場合も実測した。
**担当範囲の外側に半歩出た観察なので、そのつもりで読んでほしい。**

**1. 期待の根拠**

`README.md:153` のリンク先は `releases/latest/download/SAIVerse.zip`、すなわち
**最新のリリース**である。一方 `setup.bat:262` は `git reset origin/main`、すなわち
**その時点の main の先端**に合わせる。この 2 つが別のコミットなら、
`git reset` は mixed なので HEAD と index だけが動き、作業ツリーは ZIP のまま残る。

**2. 現在の結果**

**確定 (実行した)**。`b3_zip_reset_drift.py`。

- **ずれれば実際に汚れる**。`v0.3.10` の ZIP に対して `git reset origin/main` (= `v0.3.11`) を踏むと、
  更新前検査が **69 件** (M と D) を返した。`assert_git_update_ready` はこれで更新を拒否する。
  ただし拒否文は 20 件を挙げて「... and 49 more」と要約し、`git checkout -- .` を案内するので、
  利用者は自力で抜けられる (`update_engine.py:696-723`)。

- **ただし、この repo ではその「ずれ」がほぼ起きない (確定)**。
  `git log origin/main --first-parent` を v0.2.28 から v0.3.11 まで 14 版遡って見たところ、
  **first-parent の全コミットにリリースタグが付いていた**。
  main は版を出すときにしか進まず、タグはその同じコミットに置かれる。
  現在も `v0.3.11` == `origin/main` == `e7d8e7d4` である。

- **ずれるのは、main が進んでから Release が公開されるまでの間だけ (確定)**。
  GitHub の releases API で直近 5 版の `created_at` (タグ先のコミット時刻) と
  `published_at` (Release 公開時刻) の差を計算すると、v0.3.11 から順に
  **49 / 77 / 129 / 21 / 17 秒** (最短 17 秒・最長 129 秒) だった。

**3. 差**

利用者が躓くのは、**リリース公開の 17〜129 秒の窓の中で `setup` の `git fetch` を踏んだときだけ**である。
そのとき初回の `update.bat` が `Working tree has local changes` で止まり、
覚えのない 20 個のファイル名を見せられる。ただし同じ画面に `git checkout -- .` が出るので、
そこで詰むわけではない。

**窓の中で実際に踏んだ事例は確認していない (未確認)。** 発生確率も推定していない。

**4. 既存方針で解けるか**

**未確認に近い。まはーの判断を仰ぐ前に、まず前提を確かめてほしい種類の話である。**

私の観測からは、これは**実害の記録がある不具合ではなく、窓の狭い理論上の経路**に見える。
`owner_decisions.md` の 8 番「『記録し、明示し、選択してもらう』を、全操作で記録を増やす規則に変えない」に照らせば、
17〜129 秒の窓のために機構を足すのは釣り合わないと私は読む。
**ただしこれは私の読みであって、まはーの決定として書くものではない。**

材料として 1 点だけ置く。`setup` が `git reset origin/main` ではなく
**ZIP に入っている `VERSION` に対応するタグへ reset する**なら窓ごと消えるが、
これは FLOW-27 の持ち物であって、改行の担当である私が設計を決める場所ではない。

---

## 未確認として残した範囲

- **窓の中での実発生**。17〜129 秒の窓で利用者が実際に踏んだ事例は確認していない (項目 3)。
- **古い Windows での LF `.bat`**。実測したのは Windows 11 Pro 10.0.26200 のみ。
  Windows 10 以前や他のシェル実装へは一般化していない (項目 1)。
- **`.gitattributes` に `export-ignore` が無いこと**は実測で確認した (該当行 0 件) が、
  これは改行ではなく「ZIP に `tests/` `docs/` `.github/` がそのまま入る」という別論点なので、
  ここでは判断しない。前段 `F_ops.md` §5 の「既知の不一致 (3)」が既に扱っている。
- **開発者側の作業ツリーの小さなズレ**。`git ls-files --eol` で
  `start-dev.bat` / `start_dev.ps1` / `scripts/install_node_portable.ps1` の 3 件が
  `attr` は `eol=crlf` なのに作業ツリーは `w/lf` になっていた。
  **配布物には影響しない** (`git archive` は blob と属性から作るので ZIP は CRLF になる。実測で確認済み)。
  `git status` も checkin 正規化のため汚れない。実害は見つからなかったので、そう記録するに留める。

## 変更していないこと

製品コード・既存テスト・CI・既存文書を一行も変更していない。
git の状態 (branch / index / 作業ツリー) を変更していない。
生成した ZIP はどこにも公開していない。一時領域に置き、作業後に削除した
(途中で `git archive -o` の相対パスがリポジトリ直下に `local.zip` を落としたが、
未追跡のまま即座に削除し、直下に `*.zip` が残っていないことを確認した)。
本番ペルソナ・`~/.saiverse/` には一切触れていない。LLM も呼んでいない。

---

# b45: アドオン有効化の失敗表示 (FLOW-29) と本番フロントの到達範囲 (FLOW-30)

担当 ID: b45 / 2026-09-10
再現コードの置き場: `docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/`
- `b45_addon_enable_failure_response.py` (件 1、隔離 DB で実行済み)
- `b45_next_start_bind_scope.py` (件 2、Node の bind 範囲を実測済み)

両方 `ruff check` 通過。バックエンド・本番ペルソナ・LLM には一切触れていない。
画面の操作もしていない (フロントエンドの挙動はすべてソースから読んだ)。

---

## 件 1: アドオンの有効化に失敗したとき、画面に何が出るか (FLOW-29 / 元議題 追加調査 4)

### 1. 期待の根拠

**ユーザー原文**: この項目に直接対応する原文は見つかっていない。

**既存の裁定 (この機能の中で、同じ失敗の形について既に下されている)**:

- `frontend/src/components/MCPSection.tsx:175-178` のコメントが、2026-08-25 に起きた
  「押しても無反応」の原因と裁定を記録している。原文:
  「これらの API は失敗も HTTP 200 で返す ({"success": false, ...}) ので、ステータスだけ
  見ていると失敗が黙って消える。本文まで読む。」
- `api/routes/addon.py:535-538` のコメントが `mcp_settled` の意図を記録している。
  「`False` = 待ち切れなかった (反映は続いている)、`None` = 待っていない」。
  さらに `tools/mcp_client.py:2805-2809` は「ここで握って正常終了すると、呼び出し元の
  Future は成功として解決し、API は『反映は確定した』と答える」ことを避けるために、
  個々のサーバーの失敗を集めて再送出する形にしてある。
- 前段初稿 (`flows/F_ops.md` §FLOW-29-3) の調査担当提案:
  「有効化のときに登録が失敗したら、利用者がそれを知れる」。**これは未合意。**

つまり「失敗を黙って成功に見せない」は、このサブシステムの中で既に 2 回明文化されている。
未合意なのは「どこまで画面に出すか」の程度であって、原則そのものではない。

**利用者向け説明**: アドオン管理の利用者向け説明は `docs/user-guide/` に無い
(前段初稿の記述を追試し、同じ結果だった)。

### 2. 現在の結果

#### (a) 失敗の形の一覧と、それぞれの帰結

有効化のトグルを押したときに走るのは `api/routes/addon.py:427-543` の
`set_addon_enabled` で、**DB へ `is_enabled` を書いてコミットしたあと** (437-438 行)、
4 つの登録を順に呼ぶ。4 つとも `try/except Exception` で囲まれ、失敗しても
`LOGGER.warning` だけで先へ進む (455-522 行)。

| # | 失敗の形 | バックエンドが返すもの | 画面に出るか |
|---|---|---|---|
| 1 | アドオンのディレクトリが無い | 404 `{"detail": "Addon not found"}` (430-432 行) | **出る**。`AddonManagerModal.tsx:990-997` が `alert()` に detail を出す |
| 2 | `addon.json` が壊れている / 読めない | (トグルに到達しない) | **出ない**。`list_addons` が `manifest is None` の行を読み飛ばす (`addon.py:364-366`) ので、**そのアドオンは一覧から消える**。壊れている旨は出ず、`LOGGER.exception` だけ (`addon.py:207-209`) |
| 3 | 宣言された MCP サーバーが起動できない (設定値が未設定 / ランタイムが無い / 認証失敗 / ネットワーク / プロセス異常終了 など) | 200 `{"addon_name", "is_enabled": true, "mcp_settled": null}` | **条件つきで出る**。下記 (c) |
| 4 | `integrations/*.py` の import / インスタンス化が失敗 | 200 (同上) | **出ない**。`addon_loader.py:188-211` が 1 ファイル / 1 クラスごとに `LOGGER.exception` + `continue` で握るので、呼び出し元 (`addon.py:474-488`) の except にも届かない |
| 5 | `server_hooks` のハンドラが解決できない / 登録に失敗 | 200 (同上) | **出ない**。`addon_loader.py:392-424` が同様に `continue` で握る |
| 6 | composite action の spell 登録が失敗 | 200 (同上) | **出ない**。しかも画面の「アクション」一覧は登録結果ではなく JSON ファイルを読む (`api/routes/addon_actions.py:34-35` が `load_actions`) ので、**登録に失敗しても一覧には今まで通り並ぶ** |
| 7 | DB の書き込みが失敗 | 例外がそのまま上がる → 500 | **出る**。`alert()` に `Toggle failed: 500` |

**確定 (実行した)**: 3〜6 を全部同時に失敗させて `set_addon_enabled` を直接呼んだ結果
(`repro/b45_addon_enable_failure_response.py`、隔離 `SAIVERSE_HOME` の空 SQLite)。

```
呼ばれて失敗した登録: ['mcp', 'integration', 'server_hook', 'composite_action']
ルートの戻り値: {'addon_name': 'synthetic-addon', 'is_enabled': True, 'mcp_settled': None}
  本文に失敗の手がかりがあるか: False
DB に残った is_enabled: True
```

#### (b) 保存状態

**確定**: 4 つの登録が全部失敗しても `AddonConfig.is_enabled` は `True` のまま残る
(上の実行結果)。トグルは有効の位置で止まり、画面は成功として振る舞う
(`AddonManagerModal.tsx:1139-1141` が楽観的にローカル状態を更新する)。

次の起動でも「有効」として扱われ、`load_addon_integrations` (`addon_loader.py:247`) /
`load_addon_server_hooks` (`addon_loader.py:453`) / `register_all_addon_action_spells`
(`composite_actions.py:796`) が同じアドオンをもう一度読み、同じ形で失敗して同じ
warning を出す。**起動のたびに静かに失敗し続ける状態が保存される。**

#### (c) MCP の失敗だけは画面に出る経路がある — ただし条件つき

**前段初稿の「警告ログはあるが画面に出ない」は、MCP の分については正しくなかった。**
専用の表示が存在する:

- 失敗は `tools/mcp_client.py:1542-1560` の `_record_failure` に記録される。文面は
  `_build_user_error_message` (262-276 行) が作る日本語で、
  「<addon> アドオン由来の <server> MCPサーバーの起動に失敗しました（必須の設定値が未設定）。
  アドオンの導入および設定が正常に完了しているか確認してください。…（詳細: …）」の形。
- `/api/mcp/failures` (`api/routes/mcp.py:47-54`) がこれを返し、
  `MCPSection.tsx:294-341` が「起動失敗中」の欄に、分類の日本語ラベル
  (`必要なランタイムが見つからない` / `必須の設定値が未設定` / `認証失敗` / `起動コマンドエラー` /
  `ネットワークエラー` / `プロセス異常終了` / `接続先のサービスが応答できない状態 (一時的)` ほか、
  同ファイル 64-74 行) と本文、試行回数、再試行までの秒数、「即時リトライ」ボタンを出す。

**ここまでは前段初稿の読みへの訂正。以下が、その表示に届くまでの条件。**

**確定 (コードを読んだ。ブラウザでは未実行)**:

1. `MCPSection` は `expanded` が真のときしか一覧を取りに行かない (155-159 行)。
   アドオン管理モーダルの 2 箇所とも `defaultCollapsed={true}` (`AddonManagerModal.tsx:1035, 1197`)
   なので、初期状態は `expanded = false`。
   → **モーダルを開いただけでは `/api/mcp/failures` を呼ばない。**
2. 折り畳み時の警告バッジは `{!expanded && visibleFailures.length > 0 && ...}` (267-272 行)。
   `visibleFailures` は 1 の理由で空のままなので、**バッジも出ない**。
   一度自分で開いて閉じた後だけバッジが出る。
3. アドオンのカードの中に置かれた側 (`addonName` つき、1035 行) は、
   `addonName && !loading && !error && visibleServers.length === 0 && visibleFailures.length === 0`
   で `return null` する (243-251 行)。1 により初期状態が必ずこの条件を満たすので、
   **見出しごと描画されず、開くためのボタンも存在しない。**
   → **アドオンのカードの中の MCP セクションは、現状どうやっても表示されない** と読める。
   (フロントエンドのテストは無い。`MCPSection` を検査するテストファイルは 0 件。)

つまり MCP の失敗に気づくには、利用者が **アドオン管理モーダル上部の「MCP サーバー管理」
という、失敗と無関係に見える見出しを自分で開く** 必要がある。

#### (d) SystemAlertBanner は使われていない

**確定**: 画面上部の常設バナー (`SystemAlertBanner.tsx`) は `/api/system/alerts` を読み、
その中身は `manager.startup_alerts` (`api/routes/system.py:171-173`)。
`startup_alerts` に追記する箇所は `manager/initialization.py` の 4 箇所
(250 / 308 / 353 / 487 行) だけで、**すべて建物ログ・過去ログ取り込み・隔離の話**。
アドオン由来の項目は 1 件も無い (`grep` でリポジトリ全体を走査、`.worktrees/` と `temp/` を除く)。

同様に、常設 SSE で送られる `addon_toggled` イベント (`api/routes/addon.py:526-531`) の
中身は `{addon_name, is_enabled}` だけで、失敗の情報は載らない。

#### (e) 後から気づく経路は 1 本だけある

per_persona の MCP ツールをペルソナが実際に使おうとした時点で、
`_build_persona_error_message` (`tools/mcp_client.py:279-299`) が
「ツール 'X__y' は現在利用できません（原因: 必須の設定値が未設定）。詳細: …」を
ツールの戻り値として返す (2421 / 2458 行)。これはペルソナの文脈に入るので、
会話の中に現れうる。**ただしこれは「有効化の時点」ではなく「使おうとした時点」で、
しかも受け取るのはまずペルソナである。**

### 3. 差

**期待**: 有効にしたのに動かないなら、利用者がその理由を知れる。

**現在**:

- MCP サーバーの起動失敗については、**分類つきの日本語の説明と再試行ボタンが用意されている**。
  ただし利用者がそれに辿り着くのは、「MCP サーバー管理」という見出しを自分で開いたときだけ。
  失敗が起きたことを知らせる合図 (バッジ) は、一度自分で開くまで出ない。
  アドオンのカードの中に置かれた同じ表示は、現状描画されない。
- 残る 3 種の登録 (Integration / server_hook / composite action) の失敗は、
  **利用者側にはどこにも出ない**。トグルは有効の位置に留まり、
  アクション一覧には登録できなかったアクションが今まで通り並ぶ。
  利用者から見ると「有効にしたのに、ペルソナがその能力を使えない」だけが起きる。
- `addon.json` が壊れているアドオンは、**一覧から消える**。
  利用者から見ると「入れたはずのアドオンが表示されない」で、原因を示す文言は画面に無い。
- 保存の面では、失敗しても「有効」として残る。次の起動でも同じ失敗を静かに繰り返す。

**誰が何を知れなくなるか**: 利用者は「有効にした能力が使えない」と気づいたあと、
原因がアドオン側の設定不足なのか、依存の欠落なのか、アドオンの実装の欠陥なのかを
画面から切り分けられない。切り分けに使える材料は `backend.log` の warning 行だけで、
そこへ辿り着く導線は画面上に無い。

### 4. 既存方針で解けるか

**(i) アドオンのカードの中の MCP セクションが描画されない件 — 解ける (欠陥であって判断ではない)**

`MCPSection.tsx:243-251` の早期 return と 155-159 行の遅延取得の組み合わせによる、
機構の噛み合わせの欠陥。まはーに問う内容が無い。
同じファイルの 51-56 行のコメント (「このセクションはアドオンの有効/無効トグルの外側に居るので、
自分ではトグルに気づけない。…これが無いと、アドオンを無効にしてもサーバーの行が残り続け、
モーダルを開き直すまで消えなかった」) が示す通り、この画面は既に一度同種の直しを受けている。
**同じ理由が隣にも当てはまる形** (`feedback_apply_the_discipline_to_the_sibling` の型)。

**(ii) 折り畳み時にバッジが出ない件 — 解ける**

バッジの存在自体が「失敗があれば知らせる」という設計判断が既に下されている証拠
(`MCPSection.tsx:267-272`)。取得のタイミングがそれを満たしていないだけ。
これも新しい判断ではなく、既にある意図の実装漏れ。

**(iii) Integration / server_hook / composite action の失敗を画面に出すか — 判断が要る**

- **一場面**: 音声アドオンを入れて有効にする。`integrations/` の 1 ファイルが
  未導入の Python パッケージを import していて失敗する。トグルは有効のまま、
  設定欄も普通に開く。ペルソナに喋らせても声が出ない。利用者は
  「設定が足りないのか、機材の問題か、アドオンの不具合か」を画面から判別できない。
- **推奨案**: 4 つの登録の結果 (成功数 / 失敗した対象と理由) を `set_addon_enabled` の
  応答本文に載せ、失敗があればアドオンのカードにその行を出す。
  `mcp_settled` を返して画面が判断する既存の形 (`addon.py:535-543`) と同じ作りにできる。
  なお MCP の有効化側は `wait_timeout=None` で待たない設計 (subprocess 起動に数十秒かかるため、
  `addon.py:450-453` に理由が書かれている) なので、**MCP だけは応答本文に間に合わない**。
  MCP は既存の失敗一覧 (`/api/mcp/failures`) 側で見せ、他の 3 つは応答本文で見せる、
  という二本立てになる。
- **それで変わる体験**: 「有効にしたのに動かない」が、その場で理由つきで見える。
- **まはーの判断が要る点**: これは `owner_decisions.md` の
  「『記録し、明示し、選択してもらう』を、全操作で記録を増やす規則に変えない」に触れうる。
  ここで足すのは記録ではなく**失敗の表示**なので同じ話ではないと読んでいるが、
  「どの粒度まで出すか」(失敗した対象名まで出すか、件数だけか) は決めていない。

**(iv) `addon.json` が壊れたアドオンが一覧から消える件 — 判断が要る**

- **一場面**: アドオンの更新が途中で切れて `addon.json` が壊れる。次の起動で
  アドオン管理を開くと、そのアドオンの行が無い。「消えた」のか「壊れた」のかが分からない。
- **推奨案**: 読めなかったディレクトリを、名前と「設定ファイルが読めない」という状態つきで
  一覧に残す (トグルは無効固定)。
- **まはーの判断が要る点**: 壊れた行を一覧に出すか、出さずに別の場所 (バナー等) で知らせるか。

**(v) 未確認として残した範囲**

- ブラウザで実際に描画される内容は確認していない (画面を操作しない規約のため)。
  (c) の 3 点はすべて `MCPSection.tsx` と `AddonManagerModal.tsx` の読みによる。
- OAuth の接続失敗については `OAuthFlowSection.tsx:83-154` が `setError` で
  画面に出す作りになっていること (193 行に表示箇所) までは読んだが、
  失敗の形ごとの文面は追っていない。
- 導入 (install) の失敗表示は今回の担当範囲外なので追っていない。

---

## 件 2: 本番の Next.js はどこから繋がるのか (FLOW-30 / 元議題 7)

### 1. 期待の根拠

**まはーの確定判断 (2026-09-10、`owner_decisions.md`)**:

> うーん、Tailscaleは信頼していいと思ってる。Tailscale側のログインで既にセキュリティは
> 担保されてるわけで、本人確認をすることで守られるものはほぼないかなと。

同記録は「案内された設定でスマホから追加ログインなしに利用できることと、
**実際の接続可能範囲が説明と一致すること**を確認する」を正常化作業の宿題として明記している。
つまりこの判断は「**Tailnet の内側だけが到達範囲である**」ことを前提にしている。

**利用者向け説明**: `README.md:255-273`「スマホで使いたいんだが？」(Tailscale を入れて
MagicDNS 名に `:3000` を付けて開く、8 手順とスクリーンショット)。
`docs/getting-started/tailscale-runbook.md` (同じ手順の詳細版)。
どちらも `--listen-host` / `SAIVERSE_OWNER_TOKEN` / `SAIVERSE_ALLOWED_ORIGINS` に
言及しない (grep で確認、0 件 — 前段初稿の記述を追試し、同じ結果)。

**訂正 1 件**: 前段初稿は「『Tailnet に他人の端末を入れない』という前提は現在どこにも
書かれていない」と書いたが、ランブック §8「セキュリティベストプラクティス」に
「**Tailnet ACL の設定**: 不要なデバイスからのアクセスを制限」と
「**定期的なデバイス確認**: Tailscale 管理画面で不要なデバイスを削除」がある
(`docs/getting-started/tailscale-runbook.md:176-181`)。
前提が「どこにも無い」は誤り。ただし**この 2 行はベストプラクティスの箇条書きであって、
SAIVerse の保護がそこに依存しているという説明ではない。**

### 2. 現在の結果

#### (a) 案内どおりに起動したときの待ち受け

**確定 (読んだ)**:

| 待ち受け | 起動 | アドレス | 根拠 |
|---|---|---|---|
| バックエンド (FastAPI) | `python main.py city_a` | `127.0.0.1:8000` のみ | `main.py:306-313` で `--listen-host` の既定が `os.getenv("SAIVERSE_API_HOST", "127.0.0.1")`、`main.py:726-729` が `uvicorn.run(host=listen_host)` |
| フロントエンド (Next.js) | `npm start` → `next start` | **全インターフェース** | 下記 |

`main.py` はフロントエンドを起動しない (713-718 行のログで「別途 `npm run dev` せよ」と案内する)。
実際に起動するのは `start.bat:79-81` (`npm run build` → `npm start`) と
`start.sh:71-74` (同じ)。開発用は `start-dev.bat:39` / `start-dev.sh:50` で `npm run dev`。

**確定 (実測した — `repro/b45_next_start_bind_scope.py`)**:

- `frontend/package.json:9` の `"start": "next start"` に `-H` は無い。
- `next` の CLI 定義 (`frontend/node_modules/next/dist/bin/next:125`) は `start` の
  `-H, --hostname` に **commander の既定値を登録していない** (説明文に
  `(default: 0.0.0.0)` と書いてあるだけ)。よって `options.hostname` は `undefined` のまま
  `next-start.js` → `start-server.js:414` の `server.listen(port, hostname)` へ渡る。
- 実測: `listen(port, undefined)` の `server.address()` は
  `{"address": "::", "family": "IPv6"}` = **未指定アドレス (全インターフェース)**。
  同一ホストからこの機体の非ループバック IPv4 全部へ接続したところ 3 件とも成功した:
  `100.114.244.14` (Tailscale)、`192.168.0.127` (物理 LAN)、`172.22.128.1` (WSL vEthernet)。
- 開発モード (`next dev -H 0.0.0.0`、`package.json:7`) との**到達範囲の差は無い**。
  差は「dev は明示している」ことと、dev だけ `allowedDevOrigins` の検査が動くこと。
  その検査は `/_next/*` と `/__nextjs*` の内部エンドポイントにしか掛からず
  (`router-utils/block-cross-site.js:27-39`)、`router-server.js:278` の development 分岐からしか
  呼ばれない。**`/api/*` は dev でも production でもこの検査の対象外。**

#### (b) フロントエンドを通ると、ループバック限定のバックエンドに全部届く

**確定 (読んだ)**: `frontend/next.config.ts:29-35` の fallback rewrite が
`/api/:path*` を `http://127.0.0.1:8000/api/:path*` へ中継する。
中継の実体は Next の `proxyRequest` (`router-utils/proxy-request.js:25-36`) で、
`http-proxy` に `changeOrigin: true` を渡す形。**受け取ったリクエストヘッダをそのまま
転送する** (Host だけ宛先へ書き換え、`x-forwarded-host` に元の Host を入れる)。

さらに `/api/addon/*`・`/api/addon/events`・`/api/mcp/*` は Next の Route Handler が
個別に中継する (`frontend/src/app/api/**/route.ts`)。

したがって、フロントエンドに届く端末は、**そのまま `127.0.0.1:8000` の全 API に届く**。
既定起動では `lan_mode` が偽なので `OwnerAuthMiddleware` は差し込まれず
(`main.py:675-677`)、認証は無い。

#### (c) 到達範囲の判定 — Tailnet の外にも出る

**確定**: `next start` の待ち受けは全インターフェース。tailnet の外の
物理 LAN アドレス (`192.168.0.127`) でも WSL の仮想アダプタ (`172.22.128.1`) でも
ソケットは接続を受けた (実測)。

**この機体の現状 (製品の不変条件ではなく、環境の事実)**:
Windows Defender ファイアウォールに `C:\program files\nodejs\node.exe` 宛の
受信規則が 2 本 (TCP / UDP)、**Action = Allow、Profile = Private, Public、Enabled = True**
で入っている (`Get-NetFirewallRule` で読み取り)。`.node/` の同梱 Node は存在しないので
`npm start` はこの node を使う。
→ **この機体では、同じ Wi-Fi / LAN にいる別の端末が `http://192.168.0.127:3000` を開けば
SAIVerse の UI がそのまま出て、その先の全 API に無認証で届く**、と読める。

**未確認**: 別マシンから実際にパケットが通ることは試していない
(同一ホストからの接続はファイアウォールを通らないことが多いので、上の実測はこの点の証拠にならない)。
確かめるなら、同じ LAN の別端末のブラウザで `http://192.168.0.127:3000` を開いて
UI が出るかを見るのが一番短い。

**部分的には見えている**: `next start` の起動バナーは
`- Network:       http://<非ループバックの IPv4 の 1 つ>:3000` を出す
(`app-info-log.js:98-99`、`start-server.js:290-292, 317-324`。dev / production 共通で実行される)。
フロントエンドのコンソール窓を見れば「LAN 側にも出ている」ことは分かる。
ただし出るのは**アドレス 1 つだけ**で、全部の一覧ではない。

#### (d) 前段初稿の「欠けているのは機構ではなく案内」の再検証

前段初稿は「持ち主確認の門は `--listen-host` を指定して起動すればフロント経由でも効く。
欠けているのは機構ではなく案内」と書いた。**追試した結果、この読みは 3 点で成立しない。**

**確定 (読んだ)**:

1. **`--listen-host` に指定できる値が実質 `0.0.0.0` に限られる。**
   フロントエンドの中継先は `next.config.ts` と 3 つの Route Handler の両方で
   `http://127.0.0.1:8000` に固定されている。`--listen-host` に Tailscale の IP や
   LAN の IP を指定すると、`127.0.0.1:8000` では誰も待たないので、
   **フロントエンド経由の `/api/*` が全部 502 / 接続失敗になる**。
   門を通る形にできるのは `--listen-host 0.0.0.0` の場合だけで、
   そのとき待ち受けは Tailnet だけでなく LAN にも開く (門はあるが範囲は広がる)。

2. **中継先を差し替える環境変数の名前が 2 つに割れている。**
   `next.config.ts:33` は `SAIVERSE_BACKEND_ORIGIN`、
   `frontend/src/app/api/addon/[...path]/route.ts:20` /
   `.../addon/events/route.ts:14` / `.../mcp/[...path]/route.ts:9` は
   `SAIVERSE_BACKEND_URL`。片方だけ設定すると、`/api/addon/*` と `/api/mcp/*` だけが
   別の宛先を向く。**どちらも `.env.example` にも `docs/reference/environment-vars.md` にも無い。**

3. **`/api/addon/events` の中継が Cookie を落とす。**
   このハンドラだけは受け取ったヘッダを転送せず、`accept` / `cache-control` /
   `x-forwarded-for` の 3 つだけを組み立てて上流へ送る
   (`frontend/src/app/api/addon/events/route.ts:28-34`)。
   `OwnerAuthMiddleware` は **メソッドを問わず** bearer か cookie を要求する
   (`api/owner_auth.py:71-76`) ので、LAN 公開時にこの常設 SSE は 401 になる、と読める
   (**静的な疑い** — 実行して確かめていない)。
   他の 2 つの Route Handler (`addon/[...path]`, `mcp/[...path]`) は
   `host` / `connection` / `content-length` / `transfer-encoding` / `accept-encoding` 以外を
   全部転送するので、Cookie も Origin も届く。**同じ役割の 3 本のうち 1 本だけが落とす形。**

**静的な疑い (もう 1 点)**: `/api/auth/login` にフロントエンド経由 (`:3000/api/auth/login`) で
入ると、ログイン成功後のリダイレクト先が壊れる可能性がある。
`api/owner_auth.py:114-115` は `request.url.hostname` から `http://{hostname}:3000/` を作るが、
`http-proxy` の `changeOrigin: true` が Host を `127.0.0.1:8000` に書き換えるので
`hostname` は `127.0.0.1` になる。Next は元の Host を `x-forwarded-host` に入れるが、
uvicorn の ProxyHeadersMiddleware は `x-forwarded-proto` と `x-forwarded-for` しか見ない
(`.venv/Lib/site-packages/uvicorn/middleware/proxy_headers.py:39-41`)。
→ スマホが `http://127.0.0.1:3000/` へ飛ばされる読み。Cookie 自体は
ブラウザから見た接続先 (MagicDNS 名) に対して張られるので、手で戻れば効くはず。
**実行して確かめていない。**

なお、バックエンドに**直接** (`http://<MagicDNS 名>:8000/api/auth/login`) 入る場合は
この問題は起きない。`--listen-host 0.0.0.0` ならこの経路が使える。

#### (e) 別のサイトから利用者のブラウザ経由で操作を仕掛けられる形 — 既存の防御

質問への直接の答え: **防御は「一部ある」。全部ではなく、かつ 1 つは意図的に切ってある。**

**ある防御 (確定・読んだ)**:

1. **CORS のオリジン許可リスト** — `main.py:652-672`。既定は
   `http://localhost:3000` と `http://127.0.0.1:3000` のみ (+ `SAIVERSE_ALLOWED_ORIGINS`)。
   これにより (a) 別サイトは応答を読めない、(b) preflight が必要な要求
   (DELETE / PUT / PATCH、`Content-Type: application/json`、独自ヘッダつき) は
   そもそも送信前に止まる。
2. **FastAPI の Content-Type 厳密検査** — 存在するが **`main.py:646` で
   `strict_content_type=False` にして無効化してある**。
   実装を読むと (`.venv/Lib/site-packages/fastapi/routing.py:434-450`)、この旗が効くのは
   **Content-Type ヘッダが 1 つも無いリクエスト**に対してだけで、有効時はそのボディを
   JSON として読まない。無効化しているので、SAIVerse は今それを JSON として読む。
   理由と対象一覧は `docs/issues/fastapi_strict_content_type_disabled.md`
   (優先度 high、未着手、2026-09-02 起票) に記録済み。
3. **`OwnerAuthMiddleware` の Origin 一致要求** — `api/owner_auth.py:78-84`。
   ただし `lan_mode` (非ループバックの `--listen-host`) のときだけ差し込まれる。
   既定起動と案内された経路では入らない。

**無い防御 (確定)**: 既定起動 (ループバック) で、状態を変える要求の送り元を見る仕組みは無い。
これは `docs/issues/api_state_changing_routes_have_no_origin_check.md`
(優先度 medium、未着手、2026-08-16 起票) が既に起票済みで、内容は現行コードと一致する
(`main.py:675-677` が LAN モードのときだけ middleware を差し込むことを確認した)。

**通る形 / 通らない形 (上の 3 つから導いた読み。実行して確かめていない)**:

- 別サイトからの **GET** — 通る (応答は読めない)。状態を変える GET があれば実行される。
- 別サイトの **HTML フォームによる POST** — preflight されないので通る。
  ただしボディが `application/x-www-form-urlencoded` になるため、
  JSON ボディを要求するルートは 422 で弾かれる。**ボディを取らない POST は実行される。**
- 別サイトからの **Content-Type 無しの POST + JSON ボディ** — `strict_content_type=False`
  なので **JSON として読まれ、実行される**。上記 issue が「この旗が唯一の防御になりうる」と
  書いているのはこの形。
- **DELETE / PUT / PATCH、および `Content-Type: application/json` を付けた POST** —
  preflight が走り、CORS の許可リストで止まる。

**この件で新しく足す事実**: 上の issue はどちらも「同じブラウザで別のサイトを開いている」
形だけを扱っている。今回確定した (a)〜(c) により、**同じ LAN の別端末が
ブラウザを介さずに直接 `:3000` を叩ける**形が加わる。こちらは CORS も
`strict_content_type` も一切効かない (どちらもブラウザ側の仕組みなので)。

### 3. 差

**期待 (まはーの 2026-09-10 の判断が立っている前提)**: Tailscale で繋ぐことを許した端末だけが
SAIVerse に届く。Tailscale 側のログインが実質的な入口になっている。

**現在**: フロントエンドは全インターフェースで待ち受けるので、届く範囲は
**Tailnet ∪ 物理 LAN ∪ その他の仮想アダプタ** (ホストのファイアウォールが通す範囲)。
この機体では node.exe への受信許可が Private / Public 両方で有効になっている。

**利用者・ペルソナが受け取る結果の差**:

- **利用者**: 自宅の Wi-Fi に繋いだ端末 (同居人の端末、預けた端末、来客の端末、
  カフェや職場の LAN に PC を繋いだ場合のその LAN 上の端末) が、
  Tailscale に入っていなくても SAIVerse の UI をそのまま開ける読みになる。
  まはーの判断は「Tailscale のログインが入口を守っている」という前提の上に立っているが、
  **その入口を通らない道が同時に開いている。**
- **ペルソナ**: 上の経路で開いた画面には、ペルソナの名前・会話・記憶・設定がすべて出る。
  さらに `/api/db/tables` の DELETE や `/api/mcp/tool-call` も同じ経路で無認証に通る。
  住人の側から見ると、「誰が見ているか」が持ち主の Tailnet の管理では決まらなくなる。
- **持ち主が知る手段**: フロントエンドのコンソール窓の `- Network:` 行に
  非ループバックのアドレスが 1 つ出るだけ。README とランブックは
  「Tailscale で繋ぐ」としか書いておらず、LAN にも出ることには触れていない。

**前段初稿との差 (訂正)**: 「欠けているのは機構ではなく案内」は成立しない。
上の (d) の 3 点は、案内を直しただけでは残る。

### 4. 既存方針で解けるか

**(i) 到達範囲が Tailnet を超えている件 — 判断が要る (ただし「認証を足すか」の問いではない)**

まはーの 2026-09-10 の判断は「**Tailscale 経由の利用に追加ログインを必須にしない**」であって、
「Tailnet の外に開くことを認める」ではない。同じ記録が
「実際の接続可能範囲が説明と一致すること」を宿題として明記している。
今回それが一致していないことが確定したので、**判断の前提が崩れる事実として報告する**
(依頼元の明示指示に該当)。

- **具体的な一場面**: まはーがノート PC を持って外出し、カフェや職場の Wi-Fi に繋ぐ。
  SAIVerse を起動する。同じ Wi-Fi にいる誰かが `192.168.x.x:3000` を開くと、
  SAIVerse の UI とペルソナの会話がそのまま見える。
  ブラウザで開けるだけなので、特別な道具は要らない。
- **推奨案 (認証を足す案ではない)**: フロントエンドの待ち受けを、まはーが信頼すると
  決めた範囲に合わせる。具体的には `package.json` の `start` を
  `next start -H <アドレス>` の形にして、既定をループバックにするか、
  起動時に Tailscale のアドレスへ束ねる。
  Tailscale のアドレスは `tailscale ip -4` で取れるので、起動スクリプトで解決できる。
  これなら「Tailnet の中は信頼する」という判断がそのまま到達範囲になる。
- **それで変わる体験**: スマホからの利用手順 (README の 8 手順) は変わらない。
  変わるのは、Tailscale に入っていない端末から見えなくなること。
  **副作用**: 同じ LAN の別 PC から使っていた人がいれば、その使い方はできなくなる。
  この使い方が想定されているかは、どの文書にも書かれていない。
- **まはーの判断が要る点**: (1) LAN 直の利用を残すか、(2) 残すならその範囲を
  README に書くか。**どちらも「認証を足すか」とは別の問い。**

**(ii) `--listen-host` を指定しても中継先が固定されている件 — 解ける (欠陥であって判断ではない)**

`SAIVERSE_BACKEND_ORIGIN` と `SAIVERSE_BACKEND_URL` の名前の食い違いも、
どちらも文書に無いことも、決めることが無い。
`CLAUDE.md`「Documentation Maintenance」の「New/removed feature, … env var … →
the matching `docs/reference/*`」がそのまま当たる。

**(iii) `/api/addon/events` だけ Cookie を落とす件 — 解ける (欠陥であって判断ではない)**

同じ役割の 3 本のうち 1 本だけが違う形をしている。
`feedback_apply_the_discipline_to_the_sibling` の型で、隣に揃えるだけ。
ただし**この 401 は実行して確かめていない** (静的な疑い) ので、
直す前に隔離環境で 1 回踏むのが順序として正しい。

**(iv) 別サイトからの操作 — 既存 issue で追跡済み。新しい判断は不要、範囲の追記が要る**

`docs/issues/api_state_changing_routes_have_no_origin_check.md` と
`docs/issues/fastapi_strict_content_type_disabled.md` の 2 本が、内容も現行コードと
一致した状態で開いている。**新しく起票する話ではない。**
足すべきは 1 点だけ — 前者は「同じブラウザで別サイトを開いている」形だけを書いているが、
今回 (c) で確定した「同じ LAN の別端末が直接叩ける」形は、ブラウザ側の仕組み
(CORS・Content-Type 検査) が一切効かない別の形である。
これは (i) の判断に付随して整理されるべきで、単独でまはーに問う内容ではない。

**(v) Unity の待ち受け — 対象外**

`owner_decisions.md` の 2026-09-09 の判断 (凍結中は待ち受けを始めない) で決着済み。
依頼元が先行対処する範囲なので追っていない。

**(vi) 未確認として残した範囲**

- **別マシンから物理 LAN 経由で実際に届くこと**は未実測。上の実測は同一ホストからの
  接続なので、ホストのファイアウォールを通っていない可能性がある。
  確かめるには同じ LAN の別端末で `http://192.168.0.127:3000` を開く。
- **(d) の 3 点のうち、SSE の 401 とログインのリダイレクト先**は静的な疑い。
  LAN 公開モードで実際に踏んでいない。
- **Tailscale の ACL がこの機体でどう設定されているか**は見ていない (環境依存で、
  かつ Tailnet の内側の話なので、今回の「Tailnet の外に出るか」の判定には影響しない)。
- Discord ゲートウェイと SDS 登録は今回の担当範囲外なので追っていない。

---

# b6: モデルや接続先の設定を変えたとき、それが今動いているペルソナに届くのか (FLOW-28 / 元議題 2)

担当 ID: b6 / 対象 issue: `docs/issues/provider_change_does_not_reach_live_personas.md` (2026-08-05 起票、未解決フォルダ)

---

## 最初に — 判定の要約

**一部解消している。** 不具合の実体そのもの (生成済みの LLM クライアントが変更前の接続先を
持ち続ける) は現在も残っており、隔離環境で実測した。一方で、issue が「直すなら」に挙げた
二つの案のうち **後者 (ライブ更新を保証しない設計にして、その旨を明示する) は、issue を
起票したのと同じコミット 9d405112 (2026-08-05) で UI とリファレンス文書に実装済み**である。

そのため、issue 本文の末尾にある「現状は intent 不変条件 11 に書いてあるだけで、UI には
何も出ていない」という一文は、**同じコミットの中で既に古くなっている**。この一文を根拠に
「利用者に何も伝わっていない」と再起票してはいけない。

残っているのは次の 3 つで、いずれも「新しい判断が要る」ものではなく「既に下された判断の
適用範囲」の話である (詳細は §5)。

1. クライアントを作り直す機構そのもの (issue の前者の案) は未実装。
2. 同じ食い違いは **モデル定義の編集**でも起きるが、モデル管理画面には通知が無い。
3. API の応答本文には何の断りも無い (通知はフロントエンドの文字列としてだけ存在する)。

---

## 1. issue が言っている問題と、起票以降の変更

### issue の主張 (2026-08-05 時点)

- UI からプロバイダの接続先や API キー環境変数名を変えて保存すると、設定の一覧とモデル側の
  解決結果はその場で更新される。しかし `PersonaCore` は最初に喋ったときに作った LLM
  クライアントを持ち続けるため、そのペルソナは再起動まで変更前の接続先へ送り続ける。
- 条件は「過去に会話したことがあるか」ではなく「今動いているプロセスの中でそのクライアントを
  既に作ったかどうか」。
- 通常用 (`_llm_client`) と軽量用 (`_lightweight_llm_client`) は別々に作られるので、同じ
  ペルソナの中で新旧が混ざりうる。
- プロバイダを削除した場合も、既にクライアントを持っているペルソナは削除後も旧い接続へ送れる。
- 直すなら: (a) 保存・削除・再読込のときに管理下の `PersonaCore` のクライアントを破棄して
  作り直す、または (b) ライブ更新を保証しない設計にして、その旨を API の契約として明示し、
  UI にも出す。

### 起票以降にこの領域で何が変わったか (確定)

`git log --oneline --since=2026-08-05 -- saiverse/provider_configs.py api/routes/providers.py
saiverse/model_configs.py persona/core.py` を実行した結果、**この領域のコードは起票以降
一度も変更されていない**。ヒットしたのは起票コミット 9d405112 自身と、水位・知覚まわりの
`model_configs.py` への変更 (`791f3f5c`, `c21870ea`, `6dd79970`, `edb74bd6`, `b8e2636f`,
`c93b17f9`) だけで、いずれもクライアントの寿命管理には触れていない。

一方、**起票コミット 9d405112 自身が対処を 2 つ含んでいた** (`git show --stat 9d405112`
とコミットメッセージ「後者は UI にも警告を出すようにした」で確認)。

- `frontend/src/components/settings/ProviderManagementPanel.tsx` に通知を追加した。
  保存後 (`:226`) と削除後 (`:133`) に、それぞれ次の文言が画面に出る。
  - 保存: 「保存しました。SAIVerse を起動してから既にこのプロバイダで喋ったペルソナは、
    再起動するまで変更前の接続を使い続けます。通常用と軽量用の接続は別々に作られるため、
    同じペルソナの中で新旧が混ざることもあります。」
  - 削除: 同じ形で「削除前の接続へ送り続けます」。
- `docs/reference/providers.md:92` にも同じ内容が書かれ、未解決 issue へリンクしている。
- `docs/intent/model_provider_management.md:126` の不変条件「反映の範囲を約束しすぎない」が
  同じ内容を設計の約束として書いている。「**この API は「保存された」ことを返すのであって
  「今この瞬間から全員に効く」ことは返さない**」。

つまり issue 本文の「UI には何も出ていない」は、**書かれた時点で既に同じコミットによって
覆されていた**。issue 本文がその追記を受け取っていない。

---

## 2. 現在の適用経路 (時間順、すべて読んで確認)

### 保存されるまで

1. 利用者がグローバル設定 → モデル管理タブ → プロバイダのサブタブで編集し、保存する。
2. `PUT /api/providers/{id}` (`api/routes/providers.py:180-207`)。既存設定に差分を重ね、
   `source` を `user_data` に固定してから検査する。
3. `provider_configs.save_provider()` (`saiverse/provider_configs.py:145-186`) が
   `~/.saiverse/user_data/providers/<id>.json` へ書く。**書き込み先は常に user_data 層**で、
   同梱ファイルは書き換えない (intent 不変条件 4)。一時ファイルへ書いてから `os.replace` する。
4. 同じ関数の末尾で `reload_configs()` と `reload_models_after_provider_change()` を呼ぶ
   (`provider_configs.py:184-185`)。後者は `model_configs.reload_configs()` を呼び、
   `provider_ref` を辿ってモデル側へ `base_url` / `api_key_env` を再度取り込み直す。

**この時点で、プロセス内の `PROVIDER_CONFIGS` と `MODEL_CONFIGS` は両方とも新しい値になる。**
実測でも `[B]` の 2 行で新しい URL が出た。

### 読み直しの引き金が何を読み直すのか (確定)

- `POST /api/providers/reload` (`api/routes/providers.py:243-252`) = `reload_configs()` +
  `reload_models_after_provider_change()`。つまり **プロバイダ辞書とモデル辞書の 2 つだけ**。
- `POST /api/config/reload-models` (`api/routes/config.py:127-137`) = `model_configs.reload_configs()`
  のみ。**モデル辞書だけ**。
- どちらも `manager.personas` には一切触れない (`grep -rn "invalidate" api/ saiverse/ persona/
  manager/ llm_clients/` の結果、ペルソナのクライアントを落とすコードは `api/routes/admin.py`
  の 1 箇所しかない)。
- 付随して確認したこと: **この 2 つの再読込エンドポイントは、フロントエンドのどこからも
  呼ばれていない** (`grep -rn "providers/reload\|reload-models" frontend/src` が 0 件)。
  UI 保存の経路は `save_provider` の中で再読込するので必要ないが、`docs/custom_providers.md:166-172`
  が案内している「プロバイダ更新後、`POST /api/providers/reload` で再読み込み」という手順は、
  手でファイルを編集した場合にだけ意味を持つ。

### ペルソナがその設定を受け取るまで

5. `PersonaCore` は起動時にはクライアントを作らない。`persona/core.py:158-160` で 3 つの属性を
   `None` / `False` に初期化するだけで、`persona/core.py:241-249` の遅延プロパティが
   **最初にアクセスされたとき**に `get_llm_client(self.model, self.provider, self.context_length)`
   を呼ぶ。軽量用も同様 (`persona/core.py:256-278`)。
6. アクセスするのは生成の瞬間だけである。`grep -rn "\.llm_client\b"` の結果、読み出しは
   `sea/runtime.py:724,729` (`select_llm_client`) と `manager/background.py:66` の 2 箇所しかない。
   起動処理でクライアントを触る経路は見つからなかった。
7. `llm_clients/factory.py:160-275` が、そのときの `MODEL_CONFIGS` から `base_url` /
   `api_key_env` / `request_kwargs` などを読んで `OpenAIClient` を作る。
8. `llm_clients/openai.py:277-319` が **その場で環境変数の値を読み**、`base_url` とともに
   `OpenAI(**client_kwargs)` を構築して `self.client` に持つ。
   → **接続先と鍵の値は、この 1 回で SDK クライアントの中に焼き込まれる。**

以降、そのペルソナのその階層 (通常用 / 軽量用) は、プロセスが終わるか下記の無効化経路が
踏まれるまで、この SDK クライアントを使い続ける。

### 実測 (再現コードあり)

再現コード: `docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/b6_provider_change_reaches_live_persona.py`
(`SAIVERSE_HOME` を一時ディレクトリへ隔離し、合成プロバイダ 1 件・合成モデル 2 件だけを置く。
LLM は呼ばず、SDK クライアントが握っている `base_url` を読むだけ。`ruff check` 通過)

`PersonaCore` 本体は SAIMemory / HistoryManager / DB を要求するため、`persona/core.py` の
遅延プロパティ**そのもの**を借りた器で観測した。走っているのは製品コード
(`persona/core.py:241-283`) と同一である。

2026-09-10 実行、作業ツリー (HEAD=7d7214be) での観測:

- プロバイダの `base_url` を 19001 → 19002 に保存したあと
  - `PROVIDER_CONFIGS` と `MODEL_CONFIGS` の解決結果はどちらも **19002** (新しい方)。
  - 保存前にクライアントを作っていたペルソナは **19001** (古いまま)。
  - 保存後に初めてクライアントを作ったペルソナは **19002**。
    → 同一プロセス内で新旧が混在する。
  - 軽量用だけ先に作っておいたペルソナは、軽量用 **19001** / 通常用 **19002**。
    → 同じペルソナの中で新旧が混ざる。
  - `api/routes/admin.py:136-138` と同じ 3 行 (`_llm_client=None` /
    `_lightweight_llm_client=None` / `_lightweight_llm_client_initialized=False`) を当てると、
    次のアクセスで両方とも **19002** になった。
- プロバイダを削除したあと
  - 削除前にクライアントを作っていたペルソナは **19002 を保持したまま** (削除された宛先へ
    送れる状態)。
  - 削除後に作ろうとすると `ValueError: Unknown provider_ref: audit_b6_local` で失敗した。
    → 削除は「これから作る分」にしか効かず、しかも効き方が 2 通りに割れる。
- モデル JSON の `base_url` を直接編集して `model_configs.reload_configs()` を呼んだあと
  (= `PUT /api/config/models/{key}` がやること、`api/routes/config.py:1507-1532`)
  - `MODEL_CONFIGS` は **19012** (新しい方)、生成済みクライアントは **19011** のまま。
    → **同じ食い違いがモデル定義の編集でも起きる。**

---

## 3. 届く変更と届かない変更の境界 (確定)

現在、ペルソナが持っているクライアントを落とす経路は 4 つある。いずれも読んで確認した。

**届く (無効化される)**

1. **API キーの「値」を UI の環境変数タブから変えたとき。**
   `POST /api/admin/env` → `write_env_updates()` (`api/routes/admin.py:74-148`)。
   更新した変数名に `KEY` / `TOKEN` / `SECRET` のいずれかが含まれる場合だけ、
   `manager.personas` の全員について通常用・軽量用の両方を落とす。
   媒体要約用のクライアント (`saiverse/media_summary.py`) も一緒に落とす。
   - 注記: 判定は**変数名の文字列**で行う。`MY_LLM_CRED` のような名前を使う自作プロバイダは
     この網に掛からない (静的な疑い — その名前で運用している人がいるかは未確認)。
2. **チャット UI でモデルを切り替えたとき。**
   `POST /api/config/model` (`api/routes/config.py:399-431`) → `manager.set_model()`
   (`saiverse/saiverse_manager.py:1456-1457`) → 全ペルソナの `persona.set_model()`
   (`persona/mixins/generation.py:18-31`) が `_llm_client = None` を置く。
   **通常用だけで、軽量用は落ちない。** 同じモデルを選び直しても落ちる。
3. **既定モデルを変えたとき。** `manager.update_default_model()`
   (`saiverse/saiverse_manager.py:1459-1503`) が、DB に `DEFAULT_MODEL` を持たないペルソナに
   限って同じ `set_model()` を通す。通常用だけ。
4. **ペルソナ設定を保存したとき。** `PUT /api/people/{id}/config`
   (`api/routes/people/config.py:108`) → `manager.update_ai()` (`manager/admin.py:1355-1412`)。
   通常用は **モデル名が変わったときだけ**作り直す (`if new_model and persona.model != new_model`)。
   軽量用は `lightweight_model` が設定されていれば**毎回無条件に**作り直す。

**届かない (無効化されない)**

- プロバイダの `base_url` / `api_key_env` / `protocol` / 各種既定値の変更 (`PUT /api/providers/{id}`)。
- プロバイダの新規作成・削除 (`POST` / `DELETE /api/providers/{id}`)。
- モデル JSON の作成・編集・複製・削除 (`api/routes/config.py` の `/models` 系)。
  チャット画面からの「別名で保存 / 上書き保存」も同じ経路。
- `POST /api/providers/reload` と `POST /api/config/reload-models`。
- `.env` を手で編集した場合 (プロセスの `os.environ` に反映されないので、そもそも読み直されない)。

要するに、**鍵の「値」を変えると届き、接続先の「定義」を変えると届かない**という境界になっている。
issue が書いた「変わるペルソナと変わらないペルソナが同居する」に加えて、
**「変わる設定項目と変わらない設定項目が同居する」**という二つ目の非対称がある。

---

## 4. 利用者から見て何が起きるか

**確定していること**

- 保存の API は 200 を返し、UI の一覧も新しい値を表示する。設定は確かに保存されている。
- そのあと画面には上記の通知文が出る (プロバイダの保存・削除のときだけ)。
- 生成済みのクライアントは古い宛先を保持し続ける (実測)。

**静的な疑い (読んだだけで、実行して確かめていない)**

- 古い宛先がまだ生きている場合 (例: 課金のかかる商用 API から手元のサーバーへ乗り換えた、
  キーの環境変数名だけを変えた)、**エラーは出ず、古い方へ送られ続ける。**
  課金も古い方に乗ることになる。私が確かめたのは「クライアントが握っている宛先の値」までで、
  実際にリクエストを送って課金先を観測してはいない (LLM を呼ばない規約のため)。
- 古い宛先が消えている場合 (例: ローカルサーバーのポートを変えた)、生成は接続エラーで失敗する。
  `llm_clients/openai_errors.py:31-37` が `APIConnectionError` をタイムアウト系に分類しており、
  **利用者にはタイムアウトとして見える読み**になる。この経路を端から端まで走らせてはいない。
- プロバイダを削除した場合の見え方は 2 通りに割れる。生成済みのペルソナは削除前の宛先へ
  送り続け (成功しうる)、未生成のペルソナは `Unknown provider_ref` で失敗する。
  この ValueError が会話画面にどう出るかは追っていない。

**通知が届かない場面 (確定)**

- モデル定義を編集したときは、**通知が一切出ない**。
  `frontend/src/components/settings/ModelManagementPanel.tsx` に `notice` に相当する仕組みが
  無いことを grep で確認した (「再起動」「反映」「notice」いずれも 0 件)。
  つまり、モデルの `base_url` を直したのに古い宛先へ送られ続ける利用者は、
  **画面からその可能性を知る手段が無い。**
- API の応答本文には何も書かれていない。通知はフロントエンドの文字列としてだけ存在するので、
  Discord ゲートウェイや外部クライアントなど UI を経由しない利用者には届かない
  (これは「誰が困るか」まで追えていない — 現状 UI 以外からプロバイダを編集する経路があるかは未確認)。

---

## 5. 判定 (規約の 4 段構成)

### 項目 A: プロバイダ定義の変更が、稼働中のペルソナに届くか (FLOW-28 / 元議題 2)

**1. 期待の根拠**

`docs/intent/model_provider_management.md:126` の不変条件「反映の範囲を約束しすぎない」が、
期待そのものを定義している。**「この API は『保存された』ことを返すのであって『今この瞬間から
全員に効く』ことは返さない」**と明記され、走行中のリクエストと競合させずにクライアントを
差し替える仕組みは別途必要だと書いた上で、この issue へリンクしている。
`docs/reference/providers.md:92` も利用者向けに同じ内容を書いている。
つまり **「ライブ反映しないこと」自体が、設計として承認済みの姿である。**

**2. 現在の結果**

- 確定: 生成済みのクライアントは変更前の接続先を保持する。プロセス内で新旧が混在し、
  同一ペルソナ内でも通常用と軽量用で割れる。プロバイダ削除でも同じ。
  根拠は `persona/core.py:241-283`、`llm_clients/openai.py:277-319`、
  `saiverse/provider_configs.py:184-185`、`api/routes/providers.py:243-252`。
  実測は上記の再現コード。
- 確定: 利用者への明示は済んでいる。`ProviderManagementPanel.tsx:133,226` の通知文、
  `docs/reference/providers.md:92`、intent 不変条件 11。
- 確定: 起票以降、この領域のコードは変わっていない。

**3. 差**

期待 (= 明示した上でライブ反映しない) と現在の結果の差は、**プロバイダの保存・削除に関しては
ほぼ無い**。利用者は保存の直後に画面で「再起動するまで変更前の接続を使い続けます」と
知らされる。残る差は、その知らせが**保存の瞬間にしか出ない**ことである。
`notice` はこのパネルのローカル state で、閉じるボタンも無く、画面に残る記録にもならない
(`ProviderManagementPanel.tsx:36,154`)。あとから「なぜ設定を変えたのに挙動が変わらないのか」と
思ったときに、その場で読み返せるものは `docs/reference/providers.md:92` だけになる
(パネルを開き直したときに通知が残っているかは、コードを読んだ限り残らないと読めるが、
画面で確かめてはいない — **静的な疑い**)。
いずれにせよ、**issue が書いた「利用者は反映されたと考える」という状態そのものは既に
解消している。**

**4. 既存方針で解けるか**

**解ける (まはーへの新しい質問にしない)。**
`docs/intent/model_provider_management.md:126` が「反映の範囲を約束しすぎない」を不変条件として
既に裁定している。issue の「直すなら」の後者 (明示する) が選ばれて実装済みであり、
前者 (クライアントを作り直す) は「走行中のリクエストとの競合を扱う必要があるのが本体」と
issue 自身が書いた通り、別途の設計案件として残っている。
**この項目についてまはーに聞くべきことは無い。** 報告としては
「issue の本文が古く、UI 対処は済んでいる。機構の実装は未着手のまま」でよい。

### 項目 B: 同じ食い違いがモデル定義の編集でも起きるのに、そちらには通知が無い (FLOW-28)

**1. 期待の根拠**

- `docs/intent/model_provider_management.md:126` の不変条件は、文面としては
  「プロバイダを保存・削除・再読込すると」と書いているが、理由として挙げているのは
  `persona/core.py` がクライアントをキャッシュすることであって、プロバイダ固有の事情ではない。
- FLOW-28 §8 が、これと同型の別件 (壊れたファイルへの守りがプロバイダにしか無い件) について
  **「まはーの新しい判断を求める案件ではなく、既に下された判断の適用漏れである」**と裁定している。
  同じ読み方がここにも当てはまる。
- `CLAUDE.md` の「規律を入れたら、同じ理由が当てはまる隣を必ず探す」も同じ向きを指す。

**2. 現在の結果**

- 確定: モデル JSON の `base_url` を編集して `reload_configs()` を通しても、生成済みの
  クライアントは古い宛先を保持する (実測、上記 G)。機構はプロバイダのときと完全に同じ。
- 確定: `ModelManagementPanel.tsx` には通知の仕組みが無い。
  `ProviderManagementPanel.tsx` にだけある。
- 確定: モデルの編集経路は 4 つある (作成 / 更新 / 複製 / 削除、さらにチャット画面からの
  「別名で保存」「上書き保存」)。いずれも `reload_configs()` を呼ぶだけである
  (`reload_configs()` を呼ぶ行は `api/routes/config.py:1502` / `1532` / `1563` / `1599`)。

**3. 差**

プロバイダの接続先を直した利用者は「再起動するまで古い接続を使う」と知らされる。
**モデルの接続先を直した利用者は、まったく同じことが起きるのに何も知らされない。**
受け取る結果の差は、「設定を変えたのに挙動が変わらない」ときに、片方は原因の候補を
画面から得られ、もう片方は得られないことである。

**4. 既存方針で解けるか**

**解ける (まはーへの新しい質問にしない)。**
FLOW-28 §8 が同型の件に下した裁定「既に下された判断の適用漏れ」がそのまま当てはまる。
必要なのは、`ModelManagementPanel` にプロバイダ側と同じ通知を足すこと (と、
`docs/custom_providers.md` のトラブルシュートに同じ断りを 1 行足すこと) であって、
新しい仕様を決めることではない。
なお **これを「モデル編集にも警告が要る」という価値判断として書かないこと。**
根拠は、同じ機構が同じ食い違いを起こすと実測したことである。

### 項目 C: API の応答本文には何の断りも無い (FLOW-28)

**1. 期待の根拠**

issue 本文の「直すなら」に「ライブ更新を保証しない設計にするなら、その旨を **API の契約として
明示し**、UI にも出す」と書かれている。UI 側は実装されたが、API 側は実装されていない。
ただしこれは issue 起票者 (=当時の私) が書いた提案であって、まはーの原文でも
既存 intent でもない。**根拠の格は「調査担当の提案」である。**

**2. 現在の結果**

- 確定: `PUT /api/providers/{id}` の応答は `ProviderInfo` だけを返す
  (`api/routes/providers.py:180-207`)。反映範囲についての情報は含まれない。
- 確定: 通知はフロントエンドのハードコードされた文字列である
  (`ProviderManagementPanel.tsx:133,226`)。

**3. 差**

UI を経由しない利用者 (もし居れば) はこの断りを受け取れない。
ただし **「UI を経由せずプロバイダを編集する利用者が実在するか」を私は確かめていない。**
現時点で `/api/providers` を叩く経路として見つけたのはフロントエンドだけである。

**4. 未確認**

何を確かめれば決まるか: この API を UI 以外から使う想定があるか。想定が無いなら、
UI の文言だけで十分であり、この項目は落ちる。想定があるなら、応答に 1 フィールド足すか、
`docs/reference/api-endpoints.md` (自動生成) ではなく手書きのリファレンスに書く。
**私はこれを議題に昇格させない。** 有無が確定していないため。

---

## 6. 未確認として残した範囲

- 古い宛先へ実際にリクエストが飛ぶこと、およびその課金先。LLM を呼ばない規約のため、
  観測したのは SDK クライアントが握っている `base_url` の値までである。
- 古い宛先が消えている場合に、会話画面にどのエラーが出るか。エラー分類のコードは読んだが
  (`llm_clients/openai_errors.py:31-37`)、生成の経路を端から端まで走らせていない。
- プロバイダ削除後に `Unknown provider_ref` が投げられたとき、利用者の画面に何が出るか。
- `MY_LLM_CRED` のように `KEY` / `TOKEN` / `SECRET` を含まない名前の環境変数を使っている
  利用者が実在するか (居れば、鍵の値を変えても届かない)。
- UI 以外から `/api/providers` を使う経路があるか。
- Gemini / Anthropic など OpenAI 互換以外のクライアントで、鍵と宛先が同じように構築時に
  焼き込まれるか。`llm_clients/openai.py` は読んで確認したが、他のクライアントは読んでいない。
  ただし `PersonaCore` 側のキャッシュはプロトコルに依らないので、
  **「生成済みのものは作り直されない」という結論はプロトコルに依らず成り立つ。**

---

## 7. 再現コード

`C:\Users\shuhe\workspace\SAIVerse\docs\audits\2026-09-10_normal_behavior_remaining_evidence\repro\b6_provider_change_reaches_live_persona.py`

実行:

```
.venv/Scripts/python.exe docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/b6_provider_change_reaches_live_persona.py
```

`SAIVERSE_HOME` と `SAIVERSE_USER_DATA_DIR` を一時ディレクトリへ向け、合成プロバイダ 1 件と
合成モデル 2 件だけを置く。本番の `~/.saiverse/` には触れない。LLM は呼ばない。
`ruff check` 通過。観測結果はスクリプト冒頭の docstring に全文を残してある。

**製品コード・既存テスト・既存文書は一行も変更していない** (`git status --short` で確認済み。
追加したのは上記の再現コード 1 本だけで、`docs/audits/` は元から未追跡)。

---

# 議題 6 の残り — 部屋・アイテムを既存方針に照らす (統合担当が直接調査)

依頼書の指示: 「部屋・アイテム等も既存の共有記録・他者の記憶を尊重する方針を適用して整理し、
**本当に衝突がある場合だけ具体化する**」

## 結論: まはーの判断が要る衝突は見つからなかった

### 部屋 (Building) の削除 — 既存方針と同じ向き

`manager/admin.py:461-506` を読んだ (確定)。

**拒否する条件が 3 つある**: シードされた建物 / ペルソナが在室 / 利用者が在室。

**物理削除するもの**: `building_occupancy_log` のその建物の行を全部 (`:495`)。
**触らないもの**: `building_messages` (その部屋で交わされた会話)。

→ **部屋を消しても、そこでの会話は残る。** これは 2026-08-25 の裁定
(誰かが記憶へ転記した発言を消さない) と**同じ向き**なので、衝突しない。

### アイテムの削除 — 「世界が変わること」であって「記録を取り下げること」ではない

`manager/admin.py:1117-1137` を読んだ (確定)。`item_location` と item 本体を物理削除する。

2026-08-25 の裁定が禁じたのは **既に誰かが記憶へ転記した発言を、後から記録から取り下げること**。
アイテムが世界から無くなるのは、記録の改竄ではなく**世界の出来事**。
ペルソナの記憶に「あれを持っていた」が残っていても、それは嘘になっていない。
→ **既存方針の適用で解ける。新しい判断は要らない。**

## 判断ではなく実装の不揃いが 2 つ (質問にしない)

1. **同じテーブルの扱いが逆。** `building_occupancy_log` を、
   ペルソナ削除は**残す** (退出の時刻を打つだけ、`manager/admin.py:1453-1456`)、
   建物削除は**物理削除する** (`:495`)。どちらかに揃える話。
   なお現在この表を読むのは「いま誰がそこに居るか」(退出時刻が空の行) だけで、
   過去の履歴を読む処理は見つからなかった (`expansion_data/saiverse-stackchan-addon/avatar_loader.py:707-710`
   も同じ形)。だから履歴を消しても、誰かの記憶と食い違う形にはなりにくい。

2. **アイテムの削除には拒否条件が一つも無い。** 部屋には 3 つあるのに、
   アイテムは**誰かが持っている状態でも消える**。持ち主に何も告げない。
   部屋と揃えるなら「誰かが持っているアイテムは消せない」または
   「持ち主から外してから消す」の形になる。

## 未確認

- `item_location` が現在地だけを持つのか、履歴を持つのかを確かめていない。
  履歴を持つなら「誰が持っていたか」の記録が消えることになるので、
  上の判定 (衝突しない) を見直す必要がある。
- アドオンの無効化については、削除の追加調査 findings F で
  `addon_persona_config` (外部サービスの認証情報を含む) が
  ペルソナ削除で消えないことを確認済み。**これは共有記録の話ではなく残骸の話**なので、
  議題 6 ではなく削除の実装範囲の話として扱う。
