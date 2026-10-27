# Meta Model API (Muse) をプロバイダとして同梱する

**ステータス**: 検証待ち（外部通信なしのモック HTTP テストは通過。実サービスでの接続確認は、まはーの `META_API_KEY` で未実施）

## 利用者が得るものと経路

Meta のキーをモデル管理のプロバイダタブで設定し、Muse Spark 1.3 と
Muse Wonder (Preview) を会話モデル・軽量モデルとして選べるようにする。
これまで Muse Spark は OpenRouter 経由 (`openrouter-meta-muse-spark-1.2`) でしか使えなかった
(Muse 系では、ほかに Muse Glimmer 30B が NVIDIA NIM 経由で同梱されている)。
この 2 本の追加で、既存の OpenRouter 経由・NIM 経由の定義は変えない。

同梱のプロバイダ定義 → 設定ローダー（builtin / expansion / user の3階層から読み込む仕組み） →
モデル定義の `provider_ref`（参照先プロバイダ）の解決 → クライアント生成処理 →
OpenAI SDK → Chat Completions (`POST https://api.meta.ai/v1/chat/completions`)
の経路を使う。Python のコードの変更は無く、キーの保存・利用可能かどうかの表示・構造化出力・
ツール呼び出し・ストリーミング・使用量とキャッシュの記録は既存の仕組みをそのまま使う。
画面側の変更は、プロバイダタブからキー取得手順のページ (`docs/api-keys/meta.md`) を開く
リンクの対応表 (`ProviderManagementPanel.tsx` の `API_KEY_DOCS`) に 1 行足すことだけ。
ペルソナの既存モデルや記憶は変更しない。

## 接続仕様と守るべき条件

- 接続先は `https://api.meta.ai/v1`、認証は `META_API_KEY`。Meta の文書の例示名
  `MODEL_API_KEY` は汎用的すぎて `.env` の中で何のキーか分からないため採らない。
- 登録する対話用モデルは `muse-spark-1.3` と `muse-wonder-1.0` の 2 本だけ (まはー指定)。
  Meta 直結では、Contributor 版・Muse Spark 1.2 / 1.1・画像生成・音声認識・SAM は登録しない
  (既存の `openrouter-meta-muse-spark-1.2` はそのまま残す)。
- Chat Completions を使う。Meta は Responses API も持つが、SAIVerse の
  `openai_compat` クライアントは Chat Completions で全機能を揃えている。
- Meta の Chat Completions の文書に HTTP 400 を返すと明記されている項目は、次の全部:
  `stop` / `logit_bias` / `prediction` / `verbosity` / `modalities` / `audio` /
  `web_search_options` / `n` が 2 以上 / `logprobs: true` (と `top_logprobs`) /
  `reasoning_effort: "none"` / `top_p: 0` / `max_output_tokens` / `text.format`。
  `frequency_penalty` / `presence_penalty` も「非対応で、エラーになりうる」とある。
  これらの項目名はモデル定義の `parameters` に書かない。OpenAI クライアントから送られる
  パラメータは、モデル定義の `parameters` の既定値と、チャット画面での一時的な設定の上書き
  (ペルソナ側で `get_model_parameters(model)` のキーに絞られる) から作られるので、
  キーの面ではこれで送られない。`top_p` は「temperature と併用しない」と案内されて
  いるため定義に入れない。
- 値の面は守れていない: `OpenAIClient.configure_parameters` ではキーの許可リスト
  (`OPENAI_ALLOWED_REQUEST_PARAMS`) しか確認しておらず、`reasoning_effort` の値がモデル定義の
  `options` に入っているかは検査しない。チャット画面での一時的な設定の上書きはモデルの選択と
  対で渡されるので、別のモデルの `none` が Muse へ流れ込む経路は見つけていないが、
  [model_provider_management.md §9](model_provider_management.md) にある「各プロバイダのクライアント処理で
  最終送信値を検査する」という方針は、OpenAI 互換の全モデルでこの点を満たしていない。Muse 固有の
  問題ではないので、この追加では扱わない。
- 推論（Reasoning）は止められない (`none` は 400)。Meta の API では `reasoning_effort` を
  省くとモデル側で深さが決まる。SAIVerse のモデル定義では選択肢を minimal〜max の
  6 段とし、既定値を medium にした (OpenRouter 経由の Muse Spark 1.2 の定義と同じ既定。
  書き手の判断)。
- `max_completion_tokens` は推論のトークンを含む (Meta の仕様。上限 131,072)。
  SAIVerse のモデル定義の既定値は 32,768 にした (書き手の判断)。OpenRouter 経由の
  Muse Spark 1.2 の定義は `max_tokens` 8,192 だが、推論のトークンも同じ上限から
  引かれ、Meta の文書で「上限は余裕を持って設定する」と案内されているため、深い推論のときに
  返事が途中で切れないよう広げた。料金は実際に使ったトークンにだけかかる。
  この既定値はプロンプトに載せられる入力トークン枠の計算で返事の分として差し引かれるが、
  コンテキスト上限の 1,048,576 に対して小さい。
- 1 回の要求に載せる画像は 50 枚まで (`max_image_embeds: 50`)。Meta の API では 51 枚以上の
  要求は 400 エラーになる。1 枚あたりのサイズはクライアント生成処理の既定 (5MB に縮小) のままにする
  (Meta の上限は 1 枚 50MB)。
- 推論の内容を次の要求へ送り返す設定 (`reasoning_passback_field`) は持たない。
  Meta の Chat Completions では外部の呼び出しに推論の内容は返されず (`reasoning_content`
  は空)、ターンをまたいで推論を引き継がないと明記されている。
- 会話の途中の system メッセージは user へ変換する
  (`default_convert_system_to_user`)。Meta の API では system が developer と同列に扱われ、
  会話構造の検証が OpenAI より厳密だと案内されているため、OpenRouter 経由の
  Muse 定義と同じ安全側に倒す。2026-10-28 から、このフラグの意味は「会話の途中の
  システム通知を合言葉付きタグの user に変える」になった
  ([system_notice_delivery.md](system_notice_delivery.md))。この決定自体の撤回は経緯を参照。
- キャッシュは Meta 側で自動 (先頭一致)。`cached_tokens` は
  `prompt_tokens_details` に含まれて返るため、既存の使用量記録処理でそのまま読み取って集計できる。
  `prompt_cache_key` は送らない。SAIVerse にはどのプロバイダにも `prompt_cache_key` を
  送る経路が無く、Meta のキャッシュはこの値が無くても自動で働く。Meta の文書では、
  送るなら利用者ごとではなくアプリや用途ごとの固定値にするよう案内されている。
  送るかどうかは、実サービスでキャッシュが効く割合を見てから決める。
- `stream_options.include_usage` とメッセージの `name` は OpenAI の既定動作のまま送る
  (`openai_compat_options` は宣言しない)。どちらも Meta の API 仕様（スキーマ定義）に明記がある。
- `tool_choice` は、SAIVerse の通常の呼び出し (ツールの一覧を明示して渡す呼び出し) では
  常に `"auto"` を送り、これは Meta の API 仕様（スキーマ定義）に明記がある。関数を名指しする形
  (`{"type": "function", ...}`) は、ツールの一覧を渡さずに呼んだときに、旧来のツール振り分け処理
  (`_resolve_tool_choice` の `route`) で特定の関数が指定された場合だけ送られる。この形は
  Meta の API 仕様に記載が無く、400 になる項目の一覧にも無い。受け付けられるかは
  未確認で、実サービスで 400 が出たら Sarashina と同じ
  `supports_function_tool_choice: false` をプロバイダ定義に宣言する。
- キー値をコード・ログに書かない。

## 検証

テスト用の一時ディレクトリ（`SAIVERSE_HOME`）と空の user / expansion フォルダから
本番と同じ設定ローダーとクライアント生成処理を通し、ダミーキーと httpx MockTransport で
実 SDK の JSON・認証・SSE 処理を確認する (`tests/llm_clients/test_meta_muse.py`)。
Meta の実サーバーへの通信や、本番ペルソナの呼び出し・データ変更は行わない。

## 公式資料（2026-10-27確認）

- https://dev.meta.ai/docs/quickstart
- https://dev.meta.ai/docs/models
- https://dev.meta.ai/docs/pricing-rate-limits
- https://dev.meta.ai/docs/protocols/chat-completions
- https://dev.meta.ai/docs/api-reference/chat-completions/schemas
- https://dev.meta.ai/docs/structured-output
- https://dev.meta.ai/docs/reasoning
- https://dev.meta.ai/docs/image-understanding
- https://dev.meta.ai/docs/prompt-caching

## 経緯

- 2026-10-27: まはーの依頼で着手。公式資料を読み、同梱プロバイダ定義 1 本・モデル定義 2 本・
  文書を追加し、外部通信なしのモック HTTP テストを作成した。
- 2026-10-28: 「会話の途中の system メッセージは user へ変換する」の決定は、PR #429 (2026-10-28 時点で未マージ) で撤回する。システム通知を role=system へ一本化する改修 ([system_notice_delivery.md](system_notice_delivery.md)) で、この撤回を全プロバイダへ一般化した — 途中の system を受け取れないモデルへは、クライアント層が送る直前に合言葉付きタグの user に変える。
