# Intent: 入れ子サブライン Spell 機構

**親 Intent**: [README.md](README.md)
**ステータス**: 起草中 (v0.1, 2026-05-01)
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

## 8. 揮発設計

### 基本ルール

- サブライン内のメッセージは原則 `internal` タグで揮発する (= 次回のメインライン Pulse のシステムプロンプトには載らない)
- 親に渡るのは `report_to_parent` のみ
- これによりペルソナ本体への影響を最小化し、コンテキスト圧迫も防ぐ

### サブライン内の `<system>` タグ命令や構造化出力応答

これらが直接ペルソナの会話履歴に流れ込むと「いつもと違う指示で動かされた」感じになって人格が乱れるリスクがある。サブラインに閉じ込めることでこのリスクを排除する。

### 例外: `report_to_parent` の取り扱い

親メインラインから見ると、`report_to_parent` は `<system>` タグ付きの user メッセージとして注入される (既存の subplay node の挙動)。これは「ツール実行結果」と同じ位置付けで、ペルソナ自身の発話ではない。

### 永続化

サブライン内のメッセージは SAIMemory には記録される (`internal` タグ付き)。後から振り返りたい場合 (デバッグや「あの時何を調べたっけ」) は recall 経由で取り出せる。揮発は「次回のシステムプロンプトに自動では載せない」という意味。

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

1. **`/run_playbook` Spell 実装 + 深さ制限**: ツール側に Spell 追加。runtime に深さ判定。
2. **`report_to_main` → `report_to_parent` リネーム**: コード + 既存 Playbook 修正。
3. **Playbook 一覧をシステムプロンプトに注入する機構**: prompt builder 改修。
4. **`router_callable` の運用整理**: 既存 Playbook を見直して true/false を再設定。
5. **`track_user_conversation.json` を 1-LLM + Spell 構成に書き換え**: メインライン Playbook の整備。
6. **既存 `meta_user.json` / `sub_router_user.json` を deprecated に**。実機で動作確認後に削除。
7. **動作検証**: 軽い (Spell のみ) / 重い (run_playbook 1 段) / 入れ子 (run_playbook 内で run_playbook) のシナリオで動作確認。

---

## 13. Phase 3 タスクへのマッピング

[phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) §"入れ子サブライン Spell 機構" に追加した項目との対応:

| Phase 3 タスク項目 | 本 intent doc 対応章 |
|---|---|
| `/run_playbook` Spell 仕様確定 | §4 |
| Spell loop → Playbook 起動の橋渡し runtime | §3, §6 |
| 入れ子深さ制限 (上限 4 階層) | §6 |
| `report_to_main` → `report_to_parent` リネームと伝搬経路 | §7 |
| line_id の親子関係 + cancellation 伝搬 | §11 |

実装タスクとしては §12 の段階移行ステップ通りに進める。

---

## 関連ドキュメント

- [README.md](README.md) — 進捗表
- [01_concepts.md](01_concepts.md) — ライン 3 軸の定義
- [02_mechanics.md](02_mechanics.md) — Pulse 階層 / Playbook 起動とラインの関係
- [phases/phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) — Phase 3 タスク
- [phases/sub_line_playbook_sample.md](phases/sub_line_playbook_sample.md) — サブライン Playbook 構造例
- [revisions.md](revisions.md) — 改訂履歴
