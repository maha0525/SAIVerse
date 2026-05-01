# サブライン Playbook サンプル (旧 web_search_sub.json)

**親**: [phase_3_lines_playbooks.md](phase_3_lines_playbooks.md)

Phase 3-a (旧 Phase C-2b) の動作確認用に作成された web_search_sub.json をサンプルとして記録する。実 Playbook としては 2026-05-01 に削除したが、`line: "sub"` で呼ばれるサブライン Playbook の構造例として参照する価値がある。

---

## 用途

- メインラインから `sub_play` ノードで `line: "sub"` 指定で呼び出されることを想定
- SearXNG で検索した結果を要約し、`report_to_main` としてメインラインに返す
- サブラインで実行されることでメインラインのキャッシュを汚さない

---

## ポイント

- **`output_schema`** に `report_to_main` を含める (`can_run_as_child=true` 子 Playbook の必須要件)
- **`output_keys`** で tool ノードの戻り値を複数の state 変数に展開 (`raw_results`, `_search_snippet`)
- **`output_key: "report_to_main"`** で LLM ノードの出力を `report_to_main` に格納し、ライン runtime が親メッセージに append できるようにする

---

## サンプル本体

```json
{
    "name": "web_search_sub",
    "display_name": "ウェブ検索（サブライン）",
    "description": "メインラインから line:'sub' で呼ばれることを想定した、サブライン専用のウェブ検索 Playbook。SearXNG で検索し、結果を要約して report_to_main としてメインラインに返す。Phase C-2b の動作確認サンプル。",
    "input_schema": [
        {
            "name": "query",
            "description": "検索したいトピック・質問。親メインラインが args で渡す。"
        },
        {
            "name": "purpose",
            "description": "なぜこの検索をするか。要約時の方向付けに使う。",
            "required": false
        }
    ],
    "output_schema": [
        "report_to_main",
        "raw_results"
    ],
    "router_callable": false,
    "nodes": [
        {
            "id": "search",
            "type": "tool",
            "action": "searxng_search",
            "args_input": {
                "query": "query",
                "max_results": 5
            },
            "output_keys": [
                "raw_results",
                "_search_snippet"
            ],
            "next": "summarize_for_main"
        },
        {
            "id": "summarize_for_main",
            "type": "llm",
            "action": "あなたはサブラインで web 検索を実行しました。検索結果を、メインラインのあなた自身に伝える形で要約してください。\n\n検索クエリ: {query}\n検索の目的: {purpose}\n\n検索結果:\n{raw_results}\n\n要約は以下の点を意識してください:\n- 何が分かったかを 1〜3 段落で\n- 取得した URL のうち重要なものを最大 3 つまで併記\n- 元の目的に対して結果が十分か / 追加検索が必要そうかも一言添える",
            "output_key": "report_to_main",
            "memorize": {
                "tags": [
                    "internal",
                    "web_search_sub"
                ]
            }
        }
    ],
    "start_node": "search"
}
```

---

## 削除の経緯

- 実用としては `source_web` Playbook (より構造化された research_result を返す) が同等以上の機能を持つ
- 動作確認用サンプルとしての役目は終えていた
- Playbook 整理 (2026-05-01) で削除し、本ドキュメントに記録のみ残す
