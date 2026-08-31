# Issue: スペル不発時にペルソナへエラーを返して再発言させ、ユーザーにも失敗を表示する

**ステータス**: 🚧 一部実装済み (コア + ヒント + UI 失敗表示。残: 構文崩れ層 / 上限到達時の最終挙動)
**優先度**: medium (一般ユーザーが踏む実害あり — 不発の握り潰し + 生 `/spell` 行の漏れ)
**作成日**: 2026-06-03
**関連**: `sea/runtime_llm.py:_run_spell_loop` (L927 `while`, L934-939 unknown 握り潰し), `_run_spell_tool_async` (L596/L639 既存エラー経路), `_build_spell_user_only_block` (L376 UI 表示), `_parse_spell_lines` (L338), `docs/issues/spell_round_limit_redesign.md`, `docs/issues/spell_html_leak_into_saimemory.md`

## 背景

ペルソナ (エア) が web 検索のテストで以下を発言した:

```
/spell name='web_research' args={'query': '2026年6月3日の主なニュース'}
```

`web_research` は **playbook** であって spell ではない。正しくは `/spell name='run_playbook' args={'name': 'web_research'}` と叩く必要がある (spell 登録されているのは `run_playbook` の方)。

まはーがその場で指摘したが、「うちのペルソナが間違うなら一般ユーザーでも同じミスが出る」「存在しないスペルで不発になったとき、ペルソナにエラーを返してもう一度発言させる仕組みが要る」という問題提起。

## 現状の挙動 (2 症状)

**症状1: unknown spell は警告ログを出して握り潰す** (`runtime_llm.py:934-939`)

```python
unknown = [name for name, _, _, _ in all_parsed if name not in SPELL_TOOL_NAMES]
for name in unknown:
    LOGGER.warning("[sea][spell] Unknown spell '%s', skipping", name)
if not valid_spells:
    break
```

`web_research` は `SPELL_TOOL_NAMES` に無いので unknown 扱い → ログだけ吐いて捨て、有効 spell がゼロなので即 `break`。ペルソナには何のフィードバックも返らず、再発言の機会もない。これが「不発」の正体。

**症状2: 生の `/spell` 行がバブルに漏れる**

unknown だけのとき `loop_count == 0` で抜けるため、`/spell name='web_research' args={...}` の行が `<user_only>` で包まれないまま発言テキストとして残り、ユーザーのバブルに丸見えになる。

## 既存の土台 (流用できる)

「エラーを返して再発言」のループ自体は既に在る:

- `_run_spell_tool_async` はレジストリに無いツールを呼ぶと `"Spell '...' not found in registry"` を返す (`:596`)、例外は `"Spell error (...)"` になる (`:639`)
- これらは `[Spell Result: ...]` の user メッセージとして LLM に戻され、spell loop がもう一周 LLM を再呼び出しする

つまり「失敗結果 → user メッセージ注入 → 再発言」の機構は実装済み。**unknown spell だけが `valid_spells` フィルタ (`:930-933`) で弾かれ、このエラー経路に到達していない** のが欠陥。修正は概念的には「unknown を黙って捨てる」のをやめ「失敗した spell 結果として扱う」に倒すこと。

## 設計 (まはー方針)

### コア: unknown を失敗 spell 結果として扱い、再発言ループに乗せる
- unknown spell を検出したら、有効 spell がゼロでも「失敗 spell 結果」メッセージを組み立てて LLM に返し、もう一周回す。
- `loop_count` をきちんと進めることで `_MAX_SPELL_LOOPS` (env `SAIVERSE_SPELL_MAX_ROUNDS`, 現状 10) が暴走止めになる。同じミスを繰り返しても上限で打ち切られる。
- 「素の発言 (spell なし = 通常応答)」と「unknown spell のみ」を取り違えないこと。前者にエラーを返してはいけない。判定は `_parse_spell_lines` の結果に unknown が含まれるか否かで分ける。

### ヒントを賢く返す (高価値)
ただ「存在しない」と返すより、原因に応じた誘導を入れる:
- unknown 名が **playbook 名と一致** したら → 「`web_research` は playbook です。`/spell name='run_playbook' args={'name': 'web_research'}` を使ってください」と具体的に案内する
- それ以外は利用可能な spell 一覧を添える → **2026-08-25 に「名前が近いものだけ」へ変更**。全列挙は訂正すべき一点を埋め、取り違えのたびにペルソナの文脈を食っていた (下のログ参照)

これでまはーが手で指摘した内容をシステムが自動化でき、一般ユーザーの取りこぼしも拾える。

### 構文崩れの層 (別レイヤ)
今回のケースは canonical 形式で正しくパースできたが名前が未知、というパターン。`/web_research(...)` のように **そもそもパースに失敗** する崩れ方は `_parse_spell_lines` が `Unparseable entries are silently skipped` (`:345`) で消すため、`all_parsed` にすら現れない。これを拾うには生の `/spell` トークンを別途スキャンする追加層が要る。コアとは独立して実装する。

### ユーザーへの失敗表示 (バツ印)
成功スペルは `_build_spell_user_only_block` (`:376`) で `<details class="spellResult">` + 星アイコン (SVG) の折りたたみ UI として表示される。失敗時も **毎回** (上限到達時に限らず)、この成功表示の亜種として失敗を見せる:
- 星アイコンの代わりに **バツ印 (×) アイコン** に分岐させ、ひと目で失敗と分かるようにする
- `result_str` に失敗理由 (「未知のスペル」「playbook なので run_playbook を使え」等) を入れる
- 症状2 の生 `/spell` 行の漏れも、この失敗ブロックで `<user_only>` ラップすることで同時に解消する

### 残る論点: 上限まで失敗し続けたときの最終挙動
`_MAX_SPELL_LOOPS` まで失敗を繰り返したとき、最後をどう締めるか (素の発言に戻して終える / ユーザーに「スペル発動に失敗しました」と明示する 等)。`spell_round_limit_redesign.md` の「上限到達時の line 別挙動」と地続きなので、両者を合わせて方針を決める。

## 実装の足がかり
- unknown 分岐: `runtime_llm.py:927` の while 内、`:934-939`。`unknown` を握り潰さず、失敗結果メッセージの生成 + `loop_count` 加算に変える。
- playbook 名一致判定: playbook レジストリ (DB `playbooks` + ファイル) の名前集合と突き合わせる。
- UI 失敗ブロック: `_build_spell_user_only_block` に失敗フラグ (またはアイコン種別) を足し、星 / バツの SVG を分岐。フロント側 CSS は既存 `.spellResult` / `.spellSummary` / `.spellIcon` を流用できるか確認。
- 構文崩れ scan: 生 `/spell` トークンの検出は `_parse_spell_lines` とは別の軽量正規表現で。

## 実装済み (2026-06-03)

`sea/runtime_llm.py` / `frontend/src/app/page.module.css`:

- **コア**: `_run_spell_loop` のラウンド本体を「valid + unknown を位置順の統一レコード (`round_records`) に畳む」構造に変更。unknown spell は実行せず `_build_unknown_spell_error` で誤りを `[Spell Error: ...]` として LLM に返し、`loop_count` を回して再発言させる。`if not valid_spells and not unknown_spells: break` で通常応答 (spell なし) のみ即抜け。`_MAX_SPELL_LOOPS` が暴走止め。
- **ヒント**: `_build_unknown_spell_error` — unknown 名が router_callable Playbook (`list_available_playbooks` 経由) と一致したら `run_playbook` の正しい呼び出し形を案内。それ以外は `_close_spell_names` が返す**名前が近いスペルだけ**を挙げ、一覧の在処 (スペル一覧セクション / `addon_spell_help`) を案内する。
- **UI 失敗表示 (バツ印)**: `_build_spell_user_only_block(success=False)` で × アイコン + `spellResultError` クラスに分岐。CSS に赤系の `.spellResult.spellResultError` を追加 (× は summary の `currentColor` を拾う)。失敗ブロックは結果が空でも必ず描画。
- **漏れ対策**: `_extract_first_text_before` と `_run_spell_loop` の `text_before` を「最初の任意 spell 行」基準に統一。unknown 行が valid より前にある場合も生 `/spell` が bubble1 に漏れない。
- **テスト**: `tests/test_spell_misfire_feedback.py` (× ブロック生成 / ヒント生成の回帰)。

## 残課題

- **構文崩れ層**: そもそもパースに失敗する崩れ (`/web_research(...)` 等) は `_parse_spell_lines` で消えるため未捕捉。生 `/spell` トークンの別スキャンが要る (上記「構文崩れの層」)。
- **上限到達時の最終挙動**: `_MAX_SPELL_LOOPS` まで失敗継続したときの締め方 (素の発言に戻す / ユーザーに明示) は未決。`spell_round_limit_redesign.md` と合わせて方針を決める。この場合 `final_continuation` に unknown `/spell` 行が残り末尾で漏れうるエッジも残る。

## ログ
- 2026-06-03: 起票。エアが `/spell name='web_research'` (playbook を直接 spell 名に指定) で不発になった件をまはーが指摘。不発の握り潰し + 生 `/spell` 行漏れの 2 症状を確認し、既存のエラー→再発言土台の流用 + ユーザーへの失敗表示 (バツ印) まで方針を固めて起票。
- 2026-06-03: コア + ヒント + UI 失敗表示 + 漏れ対策を実装、テスト追加。構文崩れ層と上限到達時の最終挙動は残課題として保留。
- 2026-08-25: ヒントの全列挙をやめ、名前が近いものだけに絞った。Elyth の API キーを消した実機検証で、消えたスペルを呼んだエリスに登録済み 127 個が丸ごと返り、まはーが「さすがに長すぎる」と指摘。照合はフルネーム (閾値 0.7) とツール名 (最後の `__` 以降、閾値 0.8) の二経路 — 閾値 0.6 のフルネーム照合だと、消えた Elyth のスペル名に無関係な stackchan / X が 5 件並ぶことを実測で確認したため。
