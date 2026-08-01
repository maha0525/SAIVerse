# 価格の引き当てが API モデル名へフォールバックする / 旧 UsageLog 行の帰属

**状態**: 未解決 (2026-08-01 起票、Codex レビューの指摘1・2)。設計裁定待ち — 修正が `get_model_pricing` の全呼び出し元と過去データの扱いに及ぶため、使用量帰属の修正 (`92ead95`) のスコープ外として分離した。

関連: [`docs/intent/model_provider_management.md`](../intent/model_provider_management.md) の不変条件「使用量の帰属」

## 1. 価格検索が設定キー完全一致になっていない

`saiverse/model_configs.py` の `get_model_pricing` は、設定キーでの直接検索で pricing が見つからないと `find_model_config` へ落ちる。`find_model_config` は 2 段目で `config["model"]`(API モデル名) の一致も見るため、**API 名を持つ値でも価格が引けてしまう**。

Codex 設定の API 名 (`gpt-5.6-terra`) は従量課金版設定のキーと同一なので、`UsageInfo.model` に API 名が入った瞬間、従量単価が引き当てられる。

- **現状で実害は出ていない**: `92ead95` と本セッションの llama cache 修正で、usage を記録する経路の `config_key` は factory 由来の設定キーに揃っている。確認したのは `openai` / `anthropic` / `xai` / `ollama` / `gemini` / `openai_codex` と `LlamaCachedClient` 経由。
- **残っているのは構造**: 価格引き当ての側が API 名を受け付ける限り、`config_key` が空になる経路が将来入れば同じ誤課金が再発する。防御が「呼ぶ側が正しい値を渡す」ことだけに依存している。
- `sea/runtime_context.py` の送信前見積もりは `persona.model` を渡す。これは `get_llm_client()` に渡る値と同じ設定キーなので現状は一致するが、正規化を経ていない点は同じ構造。

**修正の方向 (裁定待ち)**: 課金計算用の価格検索を設定キー完全一致に限定し、API 名での検索は表示・設定解決の用途に分離する。設定キーで引けない使用量は暗黙に 0 円扱いせず、未帰属として明示する。

## 2. 修正前に記録された UsageLog 行が API 名のまま残っている

`92ead95` より前の Codex 呼び出しは `MODEL_ID` に API 名 (`gpt-5.6-terra` など) と、当時計算された `COST_USD` を保存している。

- `api/routes/usage.py` の summary / daily は保存済みの `COST_USD` をそのまま合計するため、**課金されていない呼び出しの費用が集計に残り続ける**。
- 同ファイルの RPD 集計は `LLMUsageLog.MODEL_ID == config_key` で数えるため、旧行は**呼び出し回数からは抜け落ちる**。同じ行が費用には数えられ回数には数えられない、という食い違いが起きる。
- DB に「Codex 経由か従量 API 経由か」を区別する情報が無い。同じ API 名の従量課金呼び出しも同一の `MODEL_ID` を持つため、**API 名を Codex キーへ一括置換するのは安全でない**(本物の従量課金行まで 0 円になる)。

**判断が要る点 (まはー裁定待ち)**: 過去行をどう扱うか。候補は「出所を証明できない行を legacy として費用・RPD 双方から除外する」「表示上 legacy と明示して残す」「手を触れない」。いずれにせよ一括置換は選ばない。将来行に設定キーを保存する方針自体は `92ead95` で満たされている。
