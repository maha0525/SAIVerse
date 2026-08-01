# 価格の引き当てが API モデル名へフォールバックする / 旧 UsageLog 行の帰属

**状態**: 未解決 (2026-08-01 起票、Codex レビューの指摘1・2)。設計裁定待ち — 修正が `get_model_pricing` の全呼び出し元と過去データの扱いに及ぶため、使用量帰属の修正 (`92ead95`) のスコープ外として分離した。

関連: [`docs/intent/model_provider_management.md`](../intent/model_provider_management.md) の不変条件「使用量の帰属」

## 1. 価格検索が設定キー完全一致になっていない

`saiverse/model_configs.py` の `get_model_pricing` は、設定キーでの直接検索で pricing が見つからないと `find_model_config` へ落ちる。`find_model_config` は 2 段目で `config["model"]`(API モデル名) の一致も見るため、**API 名を持つ値でも価格が引けてしまう**。

Codex 設定の API 名 (`gpt-5.6-terra`) は従量課金版設定のキーと同一なので、`UsageInfo.model` に API 名が入った瞬間、従量単価が引き当てられる。

- **実害は実際に出ていた**: 起票時に「現状で実害は出ていない」と書いたが**誤りだった** (2026-08-01、Codex 三巡目の指摘で判明)。`scripts/` の CLI 6 箇所が `find_model_config` の返す設定キーを持ちながら、factory には API 名 (`actual_model_id`) を渡していた。factory は第一引数を `client.config_key` にするため、Codex 設定を CLI で使うと使用量が `gpt-5.6-terra` に帰属し、**この節のフォールバックが従量単価を引き当てていた**。1M input / 1M output で 0 円ではなく 14.0 USD が記録される。呼び出し側は `5301701` の次のコミットで修正済み。
- **残っているのは構造**: 価格引き当ての側が API 名を受け付ける限り、`config_key` に API 名が入る経路が将来また入れば同じ誤課金が再発する。防御が「呼ぶ側が正しい値を渡す」ことだけに依存している点は変わっていない。上記の CLI はまさにその「呼ぶ側が間違えた」実例。
- `sea/runtime_context.py` の送信前見積もりは `persona.model` を渡す。これは `get_llm_client()` に渡る値と同じ設定キーなので現状は一致するが、正規化を経ていない点は同じ構造。

**この誤りの出方**: `llm_clients/` の内部だけを調べて「usage を記録する経路の config_key は設定キーに揃っている」と断定し、**factory に何が渡されるか (呼び出し側) を調べていなかった**。調べた範囲の外を「問題なし」と書く形で、同セッション内に二度出た誤り (もう一件は [`structured_output_usage_not_recorded.md`](structured_output_usage_not_recorded.md) の訂正記録)。

**修正の方向 (裁定待ち)**: 課金計算用の価格検索を設定キー完全一致に限定し、API 名での検索は表示・設定解決の用途に分離する。設定キーで引けない使用量は暗黙に 0 円扱いせず、未帰属として明示する。

## 2. 修正前に記録された UsageLog 行が API 名のまま残っている

`92ead95` より前の Codex 呼び出しは `MODEL_ID` に API 名 (`gpt-5.6-terra` など) と、当時計算された `COST_USD` を保存している。

- `api/routes/usage.py` の summary / daily は保存済みの `COST_USD` をそのまま合計するため、**課金されていない呼び出しの費用が集計に残り続ける**。
- 同ファイルの RPD 集計は `LLMUsageLog.MODEL_ID == config_key` で数えるため、旧行は**呼び出し回数からは抜け落ちる**。同じ行が費用には数えられ回数には数えられない、という食い違いが起きる。
- DB に「Codex 経由か従量 API 経由か」を区別する情報が無い。同じ API 名の従量課金呼び出しも同一の `MODEL_ID` を持つため、**API 名を Codex キーへ一括置換するのは安全でない**(本物の従量課金行まで 0 円になる)。

**判断が要る点 (まはー裁定待ち)**: 過去行をどう扱うか。候補は「出所を証明できない行を legacy として費用・RPD 双方から除外する」「表示上 legacy と明示して残す」「手を触れない」。いずれにせよ一括置換は選ばない。将来行に設定キーを保存する方針自体は `92ead95` で満たされている。

## 3. モデル識別子の入力契約が lookup API ごとに揃っていない

(2026-08-01 追記、Codex 四巡目の指摘3)

`get_model_provider` / `get_context_length` / `get_cache_config` などは設定キーを直接引く。一方 **`get_model_pricing` だけが API 名フォールバックを持つ** (第1節)。同じ「モデル識別子」を受け取る API なのに、受理する値の範囲が違う。

そのため API 名が設定として保存された場合、**実行系と価格系で結果が食い違う**。provider や context_length の解決は失敗するか誤った設定を選ぶのに、見積もりと価格表示だけは従量課金版へ解決されて金額が出る。

- 設定の保存側 (`default_model` / `lightweight_model` / `memory_weave_model`) は任意文字列をそのまま受け入れる。`api/routes/people/arasuji.py` の見積もりは `persona.memory_weave_model` か env `MEMORY_WEAVE_MODEL` を正規化せず `estimate_chronicle_generation_cost` へ渡す。
- 現状 `memory_weave_model` に入る値は設定キー (tutorial の既定値も全て設定キー形式) なので、**env に API 名を書いた場合だけ**この食い違いに入る。実際にそう設定された記録は確認していない。

**修正の方向 (裁定待ち)**: モデル設定の保存境界で API 名を設定キーへ正規化し、未解決の値を保存しない。lookup API の入力契約を設定キーへ統一し、API 名の解決は明示的な resolver 一本に限定する。

なお `a631096` で factory 境界に「設定キーでない値を渡された」ことを検出する警告を入れた。これは呼び出し側の取り違えを見つける関所であって、本節の契約不統一そのものは解消していない。
