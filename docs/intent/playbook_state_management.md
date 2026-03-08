# Intent: プレイブック State 管理の再設計

## これは何か

プレイブックの実行時に使われる state（データの入れ物）の組み立て方と、プレイブックへのデータ受け渡し方式の再設計。

## なぜ必要か

### 現状の問題

#### 1. playbook_params の居場所がない

UIやスケジュールから明示的に渡されるパラメータ（`playbook_params`）が、state の中に独立した居場所を持たない。`parent` dict に直接マージされた上で `_initial_params` にもコピーされ、同じデータが2箇所に存在する。さらに `input_schema` のソース解決が誤った値で上書きし、`forwarded_params` のセーフティネットも `inherited_vars` チェックで無効化され、正しい値にたどり着く経路が全て塞がれるバグが発生した（PR #190）。

#### 2. source 未指定のフォールバックが誤っている

`input_schema` の `source` フィールドが未指定の場合、一律 `user_input` にフォールバックする。これは `input` パラメータ（ユーザー発言を受け取る）にたまたま正しく動作するだけで、`selected_playbook` のように全く別の意味を持つパラメータにも同じデフォルトが適用されてしまう。

そもそも `selected_playbook` に `source` が指定されていない理由は、「`playbook_params` から取る」に相当する source タイプが存在しないため。指定したくても指定する手段がない。

#### 3. 子が親のスコープを直接参照する設計

子プレイブックの `input_schema` で `source: "parent.X"` と書くことで、親 state のキーを参照する仕組みになっている。これは関数呼び出しではなくクロージャに近い。親がキーを設定し忘れてもサイレントに空文字列になるだけでエラーにならず、子プレイブックがどの親プレイブックのどのキーに依存しているかを把握するには子側の定義を読む必要がある。

#### 4. state が flat な dict で名前衝突する

システム変数（`messages`, `persona_obj` 等）とプレイブックのノードが使う変数（`last`, `selected_playbook` 等）と `playbook_params` の値が全て同じ flat な dict に混在する。同じキー名が衝突した場合の優先順位は `**dict` の展開順序という暗黙的なルールで決まる。

## 新しい設計

### 原則: 関数呼び出しモデル

プレイブックの呼び出しを **関数呼び出し** として統一的に扱う:

- **プレイブックの `input_schema`** = 関数のシグネチャ（受け取るパラメータの定義）
- **呼び出し元の `args`** = 関数呼び出し時の引数（呼び出し元が何を渡すか明示）

プレイブックは「自分が何を受け取るか」だけを宣言し、「どこから来るか」は呼び出し元が決める。

この原則は、呼び出し元が外部（UI、スケジュール、X連携）であっても、内部（親プレイブックの exec/subplay ノード）であっても同じ。

```
// 外部からの呼び出し（UI / スケジュール / X連携）
run_meta_user(
    playbook: "meta_user_manual",
    args: {"selected_playbook": "send_email_to_user"}
)

// 内部からの呼び出し（親プレイブックの exec ノード）
{
    "type": "exec",
    "playbook_source": "selected_playbook",
    "args": {"objective": "{objective}", "depth": "{depth}"}
}
```

同じ `args` の仕組み。渡された引数は `input_schema` で解決され、state のプレフィックスなし領域に入る。

### state の名前空間分離

`_` プレフィックスのキーはシステム変数として予約し、プレイブックのノードからは触らない:

```python
state = {
    # システム変数（自動引継ぎ、ノードは触らない）
    "_messages": [...],
    "_persona_obj": ...,
    "_pulse_id": "...",
    "_pulse_type": "schedule",
    "_cancellation_token": ...,
    "_pulse_usage_accumulator": {...},
    "_activity_trace": [...],

    # プレイブックのノードが自由に使う領域
    # args で渡された値もここに入る
    "last": "...",
    "selected_playbook": "...",
    "research_result": {...},
}
```

### args によるデータ渡し

プレイブックへのデータ渡しは、呼び出し元が `args` で明示的に行う。

#### 外部からの呼び出し

`run_meta_user()` 等のエントリーポイントが `args` を受け取り、プレイブックの `input_schema` に基づいて state に展開する:

```python
# 現在の playbook_params は args に統合
runtime.run_meta_user(
    persona, user_input, building_id,
    meta_playbook="meta_user_manual",
    args={"selected_playbook": "send_email_to_user"},
)
```

#### 内部からの呼び出し（exec / subplay ノード）

親プレイブックのノードが `args` フィールドで子プレイブックに渡す引数を明示する:

```json
{
    "id": "exec_source",
    "type": "exec",
    "playbook_source": "selected_playbook",
    "args": {
        "objective": "{objective}",
        "context_text": "{resolved_context}",
        "depth": "{depth}",
        "max_iterations": "{max_iterations_value}"
    },
    "next": "check_result"
}
```

#### 子プレイブックの input_schema

受け取るパラメータの定義のみ。値の取得先は宣言しない:

```json
{
    "input_schema": [
        {"name": "objective", "description": "調査目的"},
        {"name": "context_text", "description": "コンテキスト"},
        {"name": "depth", "description": "調査深度"},
        {"name": "max_iterations", "description": "最大イテレーション数"}
    ]
}
```

### user_input の扱い

`user_input` はシステム変数には入れない。ランタイムが会話履歴への追加とコンテキスト構築に使うだけで、state には関与しない。

プレイブックがユーザー入力を必要とする場合は、呼び出し元が `args` で明示的に渡す。他のパラメータと同じ扱い。

## 廃止するもの

| 廃止対象 | 理由 |
|----------|------|
| `source` フィールド | 子が親のスコープを参照する仕組み。args で置き換え |
| `input_template` | テキスト結合による暗黙的なデータ渡し。args で置き換え |
| `_initial_params` | playbook_params の二重コピー。args に統合 |
| `forwarded_params` | `_initial_params` 透過転送の仕組み。不要になる |
| `parent.update(initial_params)` | parent dict への直接マージ。不要になる |
| `source` 未指定 → `user_input` フォールバック | `input` パラメータ専用ロジックの誤った汎用化 |
| `playbook_params` 引数名 | `args` に統合（外部・内部で同じ仕組み） |

## 守るべき不変条件

### 1. 名前空間の分離

`_` プレフィックスのキーはシステム予約。プレイブックのノード定義（set, LLM, tool 等）からシステム変数を直接変更してはならない。ランタイムだけがシステム変数を管理する。

### 2. 関数呼び出しの統一性

プレイブックへのデータ渡しは、呼び出し元が外部（UI、スケジュール、X連携）であっても内部（exec/subplay ノード）であっても、同じ `args` の仕組みで行う。特別な経路や名前空間を設けない。

### 3. 関数呼び出しの明示性

プレイブックが使うデータは、呼び出し元の `args` で全て明示的に渡す。プレイブックが呼び出し元のスコープを暗黙的に参照する経路を作らない。

### 4. システム変数の自動引継ぎ

`_` プレフィックスのシステム変数は、サブプレイブック呼び出し時に自動的に引き継がれる。プレイブック定義で個別に宣言する必要はない。

## 具体的な変更対象

### ランタイム

- `sea/runtime_graph.py`: `compile_with_langgraph()` の initial_state 組み立てを再設計。`inherited_vars` / `forwarded_params` ロジックを廃止し、名前空間分離 + args 解決に置き換え
- `sea/runtime_runner.py`: `parent.update(initial_params)` / `_initial_params` コピーを廃止。`args` を `input_schema` に基づいて state に展開する仕組みに変更
- `sea/runtime.py`: `run_meta_user()` / `run_meta_auto()` の `playbook_params` 引数を `args` にリネーム
- `sea/runtime_engine.py`: exec ノードの `args` フィールド処理を実装
- `sea/runtime_nodes.py`: subplay ノードの `args` フィールド処理を実装、`input_template` を廃止
- `sea/playbook_models.py`: `InputParam` から `source` フィールドを削除。exec/subplay ノード定義に `args` フィールドを追加

### プレイブック JSON

以下のプレイブックの `input_schema` から `source` フィールドを削除し、呼び出し元に `args` を追加:

- `sub_router_user.json`: `source: "parent.*"` を5箇所削除 → 呼び出し元（meta_agentic, meta_user, meta_user_manual）の subplay ノードに args 追加
- `sub_speak.json`: `source: "parent.metadata"` を削除 → 呼び出し元の subplay ノードに args 追加
- `source_messagelog/web/memopedia/chronicle/document/pdf.json`: `source: "parent.*"` を各4箇所削除 → research_task の exec ノードに args 追加
- `meta_exec_speak.json`: `source: "parent.*"` を削除 → 呼び出し元に args 追加
- subplay ノードの `input_template` を全て `args` に置き換え

### エントリーポイント

- `sea/runtime.py`: `run_meta_user()` の `playbook_params` 引数を `args` にリネーム
- `sea/pulse_controller.py`: `submit_user()` / `submit_schedule()` の `playbook_params` を `args` にリネーム
- `saiverse/schedule_manager.py`: `playbook_params` → `args` に合わせて変更
- API 層 (`api/`): フロントエンドからのリクエストパラメータ名を合わせて変更
