# Intent: 入れ子サブライン Spell 機構

**親 Intent**: [README.md](README.md)
**ステータス**: 起草中 (v0.2, 2026-05-02)
**位置付け**: Phase 3 残件 (Phase 4 着手前に必須)

---

## 1. なぜ作るか

### 従来の構造の問題

これまでメインラインの判断ロジックは `meta_user.json` Playbook が担っていた。その内部構造は:

```
[meta_user]
  router ノード (LLM #1: どの sub Playbook を呼ぶか判断)
    → sub_router_user / agentic_chat / 各 source_* など
      → sub Playbook (LLM #2: 実際の処理)
        → 結果を internal で記録
  → 最終発話ノード (LLM #3: 応答生成)
```

ペルソナの 1 ターン応答に対して **LLM 呼び出しが 2〜3 回**発生する。さらに「router で何を呼ぶか」と「呼んだ先で何を出力するか」が別 Playbook に分かれているため:

- メインライン LLM は「何を呼ぶか」しか決められず、判断と発話が分離している
- router LLM の判断結果がプロンプトに引き継がれないので、メインライン側から見ると「いつの間にか sub が走った」という不自然さがある
- Playbook グラフでの分岐ロジックが複雑化し、新しい Playbook を追加するたびに router の選択肢を増やす必要がある

### 統一後の構造

**判断と発話を 1 ノードに合体する**。メインライン LLM は通常発話の中で `/run_playbook` Spell を呼んで Playbook を起動でき、結果 (report_to_parent) を踏まえて応答を続ける:

```
[track_user_conversation 等のメインライン Playbook]
  発話ノード (LLM)
    → 通常応答 (Spell 不使用) → 終了
    → /spell (即時ツール) → ツール実行
    → /run_playbook(name="...") → サブライン Pulse 起動
        → サブライン Playbook 実行 (構造化 LLM 群)
        → 完了 → report_to_parent を生成 → 親メインラインに append
    → 結果を踏まえて応答続行 / さらに /spell や /run_playbook 呼び出し可
```

これにより:

- ペルソナの意思決定 (どの Playbook を呼ぶか) が **メインライン LLM 自身の発話の流れ** に乗る
- 軽い処理は Spell で 1 ターン完結、重い処理は `/run_playbook` でサブラインに投げる、という棲み分けが自然
- LLM 呼び出し回数: 通常応答なら 1 回、Playbook を呼んでも 1 + (サブライン内の構造化呼び出し) 回で済む

これが本機構の中心的な動機。

---

## 2. Spell vs Playbook の棲み分け

| 軸 | Spell | Playbook (入れ子サブライン) |
|---|---|---|
| 入出力の往復数 | 1 往復で完結 | 2 往復以上、フローが組める |
| スキーマ複雑度 | 短く簡単 | 複雑 (事前知識やコツを含む) |
| 事前知識の注入 | 困難 (description しか持てない) | 自由 (Playbook ノード内で自由にプロンプト構築) |
| LLM 重さ | メインライン (発話する人格) | 軽量モデル可 (外部発話を伴わないため) |
| 結果のペルソナ還元 | 直接 (履歴に残る) | 報告書 (`report_to_parent`) のみ |
| 履歴の扱い | 履歴に残る | Pulse 内で揮発、最終結果のみ親へ |
| 構文の担保 | ペルソナ依存 (信頼性高いモデル必須) | 構造化出力で保証 |
| システムプロンプト負担 | スキーマ常時注入 (増えると破綻) | 名前と一行説明のみ |
| 条件分岐 / 反復 | 不可 | 可能 (Playbook グラフで表現) |

### Spell が向いているケース

- 一往復で簡単に終わる
- スキーマが短く簡単
- コツや事前知識が要らない
- ワークフローを組む必要がない
- 例: `searxng_search`, `read_url_outline`, `memory_recall_unified`, `track_create`

### Playbook (入れ子サブライン) が向いているケース

- 二往復以上を想定する
- 軽量モデルでも問題ない (ユーザーや外部への発言を伴わない)
- 複雑なスキーマを要する
- 事前知識をある程度注入した状態で LLM に決めてもらう要素がある (画像生成プロンプトなど)
- 決まったフローを連続的に実行したい、条件分岐をしたい
- 結果が重要で、中での経験をペルソナ本体に直接還元する必要が薄い
- そのPulse だけで詳細情報が揮発してもよい、あるいはむしろ揮発させてコンテキスト圧迫を防ぐべきである
- 例: `memory_research`, `deep_research`, `generate_image`, `memopedia_write`, `source_*`

---

## 3. 全体フロー図

```
[メインライン Pulse]
  ┌─ system prompt
  │    ├─ ペルソナ設定
  │    ├─ Spell スキーマ群 (item_view, searxng_search, ...)
  │    ├─ /run_playbook Spell スキーマ
  │    └─ Playbook 一覧セクション (浅い階層)
  │         - memory_research: ChronicleやMemopediaを横断調査
  │         - deep_research: Web検索でレポート作成
  │         - ...
  │
  ├─ 発話ノード (LLM, 重量級)
  │    出力例:
  │      「メモ調べてくる /run_playbook(name="memory_research") 結果を踏まえて答えるね」
  │
  ├─ Spell parser が /run_playbook を検出
  │    → サブライン Pulse 起動 (line_stack に push)
  │
  │  ┌─ [サブライン Pulse: memory_research]
  │  │    起動時: 親の line_id を記録、深さ +1
  │  │    最初の LLM ノード (構造化出力で引数決定)
  │  │      input: メインラインのコンテキスト要約
  │  │      output: { query, depth, context_refs, ... }
  │  │    ↓
  │  │    各種ノード (tool / llm / sub_play / 別の /run_playbook も可)
  │  │    ↓
  │  │    最終ノード: report_to_parent 生成
  │  │      "Memopedia の X / Chronicle の Y を見つけた。要約: ..."
  │  │
  │  └─ サブライン完了 → 親メインラインに report_to_parent を append
  │
  ├─ メインライン LLM が結果を踏まえて応答続行
  │    「なるほど、X と Y があったね。じゃあ ...」
  │
  └─ 終了 / さらに /spell や /run_playbook 可
```

入れ子の場合:

```
[メインライン] → /run_playbook(A)
  [サブライン A (深さ 1)] → /run_playbook(B)
    [サブライン B (深さ 2)] → 完了 → report_to_parent → A に append
  [サブライン A] 続行 → 完了 → report_to_parent → メインラインに append
```

---

## 4. `/run_playbook` Spell 仕様

### スキーマ

```python
@spell
def run_playbook(name: str) -> str:
    """指定された Playbook をサブラインで実行し、report_to_parent を返す。

    Args:
        name: 実行する Playbook の name (システムプロンプトの "Playbook 一覧"
              セクションに記載されているもの)

    Returns:
        サブライン Playbook の最終ノードが生成した report_to_parent (文字列)
    """
```

### 設計判断

- **引数は Playbook 名のみ**: 旧 router 方式の踏襲。Playbook ごとの引数は呼ばれた側の最初の LLM ノードで構造化出力で決める。これにより:
  - メインライン LLM は「何を呼ぶか」だけ決めればよく、引数を組み立てる負担がない
  - Playbook ごとの「コツ」や「サンプル」は呼ばれた Playbook 内のプロンプトで提示できる
  - 引数の構造変更が呼び出し側に波及しない
- **戻り値は文字列 (report_to_parent)**: 構造化された情報が必要なら report_to_parent の中で Markdown / 区切り文字で表現する。Spell の戻り値型としては string で統一。

---

## 5. Playbook 一覧の提示方法

### 採用案: システムプロンプトの浅い階層に独立セクション

```
[システムプロンプト構造]
- ペルソナ設定
- Spell スキーマ群
  - item_view, searxng_search, ...
  - /run_playbook    ← 引数 name の値を決めるための情報は↓のセクションを参照
- 利用可能な Playbook 一覧
  - memory_research: 過去の会話・知識を横断的に調査
  - deep_research: Web 検索を主体としたレポート作成
  - generate_image: 画像生成 (プロンプト、サイズ、スタイル等を内部で決定)
  - ...
- (その他の常駐情報)
```

### 提示する情報

各 Playbook につき 1 行:

```
- {playbook.name}: {playbook.description の 1 行要約}
```

`description` が長い場合は最初の 1〜2 文を抜粋。

### 対象

`router_callable=true` フラグが立った Playbook のみ (詳細は §9)。

### 将来検討

Playbook 数が増えた場合、以下の対応:

- **動的取得**: 別の Spell (`list_playbooks` 等) で必要な時にだけスキーマを取得
- **addon_spell_help 方式**: カテゴリ別グループ化 + 詳細は別 Spell で展開
- **タグベース絞り込み**: ペルソナのコンテキストや building 種別で出す Playbook をフィルタ

導入時点では全件提示で良い。圧迫が現実問題になったら検討する。

---

## 6. 階層構造

### 深さ制限

- **上限: 4 階層**
- メインライン = 深さ 0、最初の `/run_playbook` で 1、入れ子で 2、3、4
- 深さ 5 以上の起動要求は ERROR ログを出して Spell 呼び出しをスキップ
- 親に「深さ超過のため呼べなかった」旨を Spell 結果として返す

### 判定方法

`PulseContext._line_stack` の深さで判定。`/run_playbook` Spell の実行時に現在の深さを取得し、上限超過なら起動せずエラー文字列を返す。

### 各サブライン内の Spell 使用

サブライン Playbook の LLM ノードもメインラインと同じ Spell スキーマ群を持つ (`/run_playbook` 含む)。これにより:

- サブライン内から別の Playbook を呼べる (深さが許す限り)
- サブライン内で軽い Spell を直接呼べる (`memory_recall_unified` 等)
- ノードが構造化出力に固定されている場合は Spell 不使用 (構造化出力の制約による自然な抑止)
- 自由発話ノードがあるサブライン Playbook は意図的に Spell を使えるように設計されている (例: 何かの結果を踏まえて二段目の判断をする系)

### 既存 `sub_play` ノードとの併用

- **静的フロー** (Playbook グラフ内で必ずこの順序で sub Playbook を呼ぶ) → 既存 `sub_play` ノードを引き続き使う
- **動的選択** (LLM が判断して呼ぶか決める) → `/run_playbook` Spell を使う
- 両方が混在することは普通: Spell で呼ばれた Playbook が中で `sub_play` ノードを使うのは自然

---

## 7. `report_to_parent` (旧名 `report_to_main`)

### リネーム

これまで「メインラインに上げる」という想定で `report_to_main` という名前だったが、入れ子の場合は **直接の親** に上げるのが正しい挙動なので `report_to_parent` にリネームする。

### 伝搬経路

各サブラインは「自分の直近の親に対して 1 段だけ昇る」:

```
深さ 3 [孫サブライン] → report_to_parent → 深さ 2 [サブライン]
深さ 2 [サブライン] (孫からの report を解釈) → report_to_parent → 深さ 1 [親サブライン]
深さ 1 [親サブライン] (子からの report を解釈) → report_to_parent → 深さ 0 [メインライン]
```

### 各層の解釈責任

各層で「子の report を解釈して自分の report を作る」LLM ノードが必要 (= 重くなる)。

```python
# サブライン (深さ 2) の最終ノード例
{
    "id": "compose_report",
    "type": "llm",
    "action": "子サブラインからの報告: {child_report}\n\n"
              "これを踏まえて、自分の調査目的に対する結論を report_to_parent としてまとめる",
    "output_key": "report_to_parent",
    ...
}
```

深い階層を組む場合は LLM 呼び出しが各層で発生することを承知の上で設計する。「コストを払ってでも階層化が必要」と判断したフローのみ深く組む。

### 名前変更の影響範囲

- `sea/runtime_nodes.py` の subplay node 完了処理
- `output_schema` に `report_to_main` を含む既存 Playbook (`web_search_sub` は削除済み、`source_*` 系の出力は `research_result` なのでこの名前を使っていない)
- `02_mechanics.md` の関連記述
- `phases/sub_line_playbook_sample.md` のサンプル記述

実装としては小規模なリネーム。後方互換 (旧名サポート) は不要 (使用箇所が限定的)。

---

## 8. 揮発設計 (line ベース)

### 前提: line と memorize タグの責務分離

詳細は [line_tag_responsibility.md](line_tag_responsibility.md) を参照。要点:

- **Line** (`line_role` / `line_id` / `scope`) が「次の Pulse のプロンプトに載るか・サブラインに閉じるか・このターン限りか」を決める
- **タグ** (`metadata.tags`) は意味分類のみで、context 構築には**関与しない**

`/run_playbook` 経由のサブラインも、上記責務分離の上に立つ。タグでサブライン的な揮発を表現する旧来のやり方 (`memorize.tags=["internal"]`) は本機構実装時点で廃止前提。

### 基本ルール

- **サブライン内のメッセージ**: `line_role="sub_line"` + 適切な `scope` で記録される
  - LLM ノードや tool ノードの I/O は自動で `sub_line` line_role が付く (PulseContext がライン階層を管理)
  - 親メインラインの context 構築では `line_role IN ('main_line')` でフィルタされるため、自動的に親プロンプトに載らない
  - SAIMemory には残るので recall や Chronicle / Memopedia 連携には使える
- **`report_to_parent`**: `line_role="main_line"` + `scope="committed"` で記録される
  - 親メインラインの「会話の一部」として次の Pulse から自動的にプロンプトに載る
  - `<system>` タグ付き user メッセージとして注入される (既存 sub_play 挙動を踏襲)

### サブライン内の `<system>` タグ命令や構造化出力応答

これらが直接ペルソナの会話履歴に流れ込むと「いつもと違う指示で動かされた」感じになって人格が乱れるリスクがある。**`line_role="sub_line"`** で記録されることでメインライン context から自動除外され、リスクを排除する。

### 永続化

サブライン内のメッセージは SAIMemory には記録される (`line_role="sub_line"`)。後から振り返りたい場合 (デバッグや「あの時何を調べたっけ」) は recall 経由で取り出せる。揮発は「次回のメインライン Pulse のプロンプトに自動で載せない」という意味で、データそのものは保持される。

### スコープの使い分け (サブライン内)

| 用途 | scope |
|------|-------|
| サブライン内の中間処理 (LLM 呼び出し、tool 呼び出しの記録) | `volatile` (Pulse スコープのみ) |
| サブライン内で確定した中間成果物 (次のサブライン Pulse でも使いたい) | `committed` |
| メタ判断系の試行錯誤 (continue ならば消す) | `discardable` |

`/run_playbook` 経由のサブラインは、原則 `volatile` で書く (Pulse 内で完結するため)。例外があれば Playbook 設計時に明示する。

---

## 9. 呼べる Playbook の範囲

### `router_callable` フラグ

旧 `router_callable` フラグを流用 (名前は変更してもよい)。`/run_playbook` Spell が引数 `name` を受け取った時:

1. DB から該当 Playbook をロード
2. `router_callable=true` でなければエラー文字列を返す
3. true なら起動

### 名前変更の検討

`router_callable` という名前は旧 router ノード時代の名残。意味的には「外部 (メインライン LLM) から呼び出して良い」フラグ。候補:

- `externally_callable`
- `spell_invokable`
- `top_level_invokable`

リネームするなら本機構実装と合わせて行う。後方互換は不要 (内部フラグ)。

### 対象 Playbook

`router_callable=true` のもの (現状: 整理後の 43 件中、確認要):

```bash
sqlite3 saiverse.db "SELECT NAME FROM playbooks WHERE ROUTER_CALLABLE=1"
```

メインラインから直接呼べないものは false にしておく (`source_*` 系は research_task 経由で呼ばれる前提だから false で良い、等の整理が必要)。

---

## 10. ノード単位の Spell 使用可否 (将来検討)

### 現状の挙動

サブライン Playbook の LLM ノードは、メインラインと同じ Spell スキーマ群を持つ。Spell loop が動いて Spell 呼び出しを検出する。

### 構造化出力ノードとの相互作用

- LLM ノードが `response_schema` を持つ場合、出力は JSON に固定される → Spell 構文 (`/spell_name(args)`) は出力されにくい
- 自由発話ノードは Spell 構文を出せる
- → 「構造化出力なら Spell 不使用」「自由発話なら Spell 使える」が自然な棲み分けになる

### 将来追加する設定

特定のサブライン Playbook で「ここで自由発話するけど Spell は使わせたくない」ケースに備えて、ノード単位 or Playbook 単位の `disable_spells` 設定を追加できるようにしておく。優先度は低い (現状で困っていない)。

---

## 11. 失敗時の挙動

### Playbook 名不正

`/run_playbook(name="nonexistent")` のような呼び出し:

- DB に該当 Playbook なし → エラー文字列を返す: `"Playbook 'nonexistent' not found. Available: ..."` (利用可能な Playbook を列挙)
- メインラインは応答続行可能

### `router_callable=false` の Playbook を呼ぶ

- エラー文字列: `"Playbook 'x' is not callable from spell. (router_callable=false)"`
- メインラインは応答続行

### 深さ超過

- 上限 4 階層を超える起動要求
- エラー文字列: `"Subline depth limit (4) exceeded; cannot run playbook 'x'."`
- 親は応答続行

### サブライン内の LLM error / parse error

- 既存 Playbook エラー経路に従う (LLM error の SAIVerse 標準処理)
- サブライン Playbook が完了せずに死んだ場合、`report_to_parent` は欠落
- 親への戻り値: `"Subline failed: <error type>"` のような最低限の情報
- 親メインラインは「子サブラインがエラーで死んだ」と認識して、必要なら再試行 / 別アプローチ判断

### Cancellation 伝搬

- メインラインの cancellation token がサブラインに継承される
- 親が cancel された時点で子サブラインも停止する
- `_line_stack` を辿って全階層に伝搬

---

## 12. 既存機構との整合 + 段階移行

### 残すもの

- **`sub_play` ノード** (静的グラフ内呼び出し): 引き続き使う
- **`source_*` 系 Playbook**: research_task からの呼び出し用、router_callable=false で OK

### 廃止するもの

- **`meta_user.json`**: track_user_conversation に統合廃止
- **`sub_router_user.json`**: track_user_conversation に統合廃止 (router 機能が不要になる)
- **`agentic_chat`** 関連 (既に削除済み, v0.19)

### 段階移行ステップ

1. ✅ **`/run_playbook` Spell 実装 + 深さ制限** (v0.24, 2026-05-01): `builtin_data/tools/run_playbook.py` 新規。Spell として登録。`pulse_ctx._line_stack` の長さで深さ判定 (上限 4 階層 = stack length 5)。テスト 10 件追加。
2. ✅ **`report_to_main` → `report_to_parent` リネーム** (v0.22, 2026-05-01): 段階 4-B と一体実施。コード全箇所と既存テストを更新。
3. 🔲 **Playbook 一覧をシステムプロンプトに注入する機構**: prompt builder 改修。実機検証と一体で実施予定。
4. 🔲 **`router_callable` の運用整理**: 既存 Playbook を見直して true/false を再設定 (現状 18 件 true / 25 件 false)。
5. 🔲 **`track_user_conversation.json` を 1-LLM + Spell 構成に書き換え**: メインライン Playbook の整備。
6. 🔲 **既存 `meta_user.json` / `sub_router_user.json` を deprecated に**。実機で動作確認後に削除。
7. 🔲 **動作検証**: 軽い (Spell のみ) / 重い (run_playbook 1 段) / 入れ子 (run_playbook 内で run_playbook) のシナリオで動作確認。

---

## 13. UI からの Playbook 起動 (pre_spells 機構)

### 動機

旧 `meta_user_manual.json` は「ユーザーが UI で選んだ Playbook を強制実行する」経路として、`auto_route` をスキップして `selected_playbook` を直接 `exec` する設計だった。これにより **メインライン LLM ラウンドは 1 回 + サブライン内コール** で旧 router 系より軽かった。

新アーキ (本 intent doc 本体) は「メインライン LLM が発話の中で `/run_playbook` Spell を呼ぶ」を中心に据えるため、UI からの即時実行を「LLM への助言 (`<system>` タグ付き user メッセージ)」で表現すると:

```
メインライン LLM 1 回目: "ユーザー要望を読んで /run_playbook を呼ぶ" を発話
  → Spell loop が /run_playbook 検出 → サブライン実行 (N 回)
  → メインライン LLM 2 回目: 結果を踏まえて応答
```

旧経路に対して **メインライン LLM ラウンドが +1 回**増える。「絶対に実行できる」かどうかとは別次元で、コスト面で旧 `meta_user_manual` の代替にならない。

### 設計

メインライン Pulse の起動引数として `pre_spells: List[str]` を受け取り、Spell loop の入口で **メインライン LLM の最初のラウンド「前」に Spell を機械的に発火**する。

```
[ユーザー送信 + UI 選択]
  → track_user_conversation 起動 (pre_spells=["/run_playbook(name=memory_research)"])
  → Spell loop 入口: pre_spells を LLM 介さず実行
    → /run_playbook(name=memory_research) → サブライン Pulse 起動
      → サブライン Playbook 実行 (元から N 回)
      → report_to_parent が親 messages に append
  → メインライン LLM 1 回目: 既に結果が入った状態で応答生成 ← ここで終わり
```

メインライン LLM ラウンドが旧 `meta_user_manual` と同じ 1 回に揃う。

### なぜこの形にするか

- **既存 Spell loop の機構 (report_to_parent / media transport / line_role 自動分離) をそのまま流用できる**: 新規ランタイム経路は不要、`tools/run_playbook.py` を Spell loop の入口で直接呼べばいい
- **「ペルソナが意思決定する」哲学を完全には壊さない**: Spell 結果がメインライン LLM に流入するという形は、LLM が自分で `/run_playbook` を呼んだ場合と同形。`<system>` タグ付き user メッセージで「ユーザー要望により実行しました」と添えれば、ペルソナから見ても文脈が自然に繋がる
- **「強制実行」の哲学的歪みを最小化**: ペルソナ側の意思決定ノードはそのまま残り、結果を踏まえて応答する自由は保たれる。「LLM 判断をバイパスして機械的に実行」する範囲は Spell 1 回分だけ

### `pre_spells` の構文

文字列リスト。各要素は Spell 構文と同じ形 (`/spell_name(arg1=value1, ...)` または `/spell_name`)。

```json
{
  "pre_spells": [
    "/run_playbook(name=\"memory_research\")"
  ]
}
```

- 複数 Spell を並べた場合は **Spell loop の通常実行と同じ並列 / 直列規則**に従う (現状: 同ラウンドの spell を並列 `asyncio.gather`)
- 解析失敗 (`/spell_name(...)` パース不可) は WARNING ログ + 該当エントリを skip。pulse は通常起動を続行
- 個々の Spell 実行失敗は通常の Spell loop と同じく、エラー文字列が user message として親 context に注入される

### `<system>` タグ付き user メッセージの併用

UI が `pre_spells` を送る時、合わせて user メッセージ側に文脈ヒントを埋めると自然:

```
<system>このターン、ユーザーの選択により memory_research を事前実行しました。
結果を踏まえて返答してください。</system>
{ユーザーが入力した本文}
```

これでペルソナから見ると「ユーザーが要望を出し、自分が応じて Spell を呼び、結果を踏まえて応答する」という連続的な流れに見える。`<system>` タグの付与は UI 側 or chat API 側の責務とする (実装時に判断)。

### スコープと適用範囲

- 本機構は **メインライン Pulse の起動時にだけ** 使える (= ユーザーターン応答の直前)
- 自律 Pulse (`track_autonomous` 等) には適用しない (= ユーザー UI 操作起点でないため)
- サブライン Pulse 内の `pre_spells` 指定は不要 (サブラインは Playbook グラフで完全制御されているため)

### 実装スコープ (概要)

詳細は実装着手時に handoff doc に展開する。

1. **runtime 側**
   - `sea/runtime_llm.py` の Spell loop entry に `pre_spells: Optional[List[str]] = None` 引数を追加
   - LLM 1 ラウンド目を回す前に pre_spells を Spell parser に通して実行 → 結果を `messages` に append
   - 通常の Spell loop と同じ経路 (`_run_spell_tool_async` / `_run_spell_loop`) を使う
2. **API 側**
   - `/api/chat` リクエストに `pre_spells: Optional[List[str]]` を追加
   - `pulse_controller` 経由で track_user_conversation の起動引数に伝播
3. **UI 側**
   - `ToolModeSelector.tsx` の `TOOL_MODES` ハードコード列挙を撤去 (既存 TODO 解消)
   - `/api/config/playbooks?router_callable=true` で動的に Playbook 一覧を取得
   - 選択時、chat 送信ペイロードに `pre_spells: ["/run_playbook(name=...)"]` を含める
   - 「自動 (= pre_spells なし)」モードも残す (= 通常の track_user_conversation)
4. **旧経路の廃止**
   - `meta_user_manual.json` Playbook を deprecated → 削除
   - `meta_user.json` / `sub_router_user.json` の deprecated 化と一体実施 (§12 のステップ 5-6)

### 失敗時の挙動

| ケース | 挙動 |
|---|---|
| `pre_spells` 構文不正 | WARNING ログ、該当エントリ skip、pulse は通常起動 |
| `/run_playbook(name="存在しない")` | 通常の Spell エラー経路 (エラー文字列が user message に注入)、メインライン LLM がそれを見て応答 |
| `router_callable=false` の Playbook 指定 | 同上 (`run_playbook` Spell 内のチェックで弾かれる) |
| 深さ超過 | メインライン起動時の pre_spells は深さ 0 → 1 への遷移なので、原理的に発生しない |

### Phase との位置付け

Phase 3 の §12 段階移行ステップ 5-6 (`track_user_conversation` 書き換え + `meta_user` / `meta_user_manual` 廃止) と一体で実施。`/run_playbook` Spell 本体 (v0.24) が完成しているので、追加で必要なのは **Spell loop 入口の pre_spells 引数受け入れ** + **API / UI 配線**のみ。

---

## 14. Phase 3 タスクへのマッピング

[phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) §"入れ子サブライン Spell 機構" に追加した項目との対応:

| Phase 3 タスク項目 | 本 intent doc 対応章 |
|---|---|
| `/run_playbook` Spell 仕様確定 | §4 |
| Spell loop → Playbook 起動の橋渡し runtime | §3, §6 |
| 入れ子深さ制限 (上限 4 階層) | §6 |
| `report_to_main` → `report_to_parent` リネームと伝搬経路 | §7 |
| line_id の親子関係 + cancellation 伝搬 | §11 |
| UI からの Playbook 起動 (pre_spells 機構) | §13 |

実装タスクとしては §12 の段階移行ステップ通りに進める。

---

## 関連ドキュメント

- [README.md](README.md) — 進捗表
- [01_concepts.md](01_concepts.md) — ライン 3 軸の定義
- [02_mechanics.md](02_mechanics.md) — Pulse 階層 / Playbook 起動とラインの関係
- [phases/phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) — Phase 3 タスク
- [phases/sub_line_playbook_sample.md](phases/sub_line_playbook_sample.md) — サブライン Playbook 構造例
- [revisions.md](revisions.md) — 改訂履歴
