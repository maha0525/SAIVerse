# Meta Model API (Muse) をプロバイダとして同梱する

**ステータス**: 検証待ち（実サービスでの接続は 3 モデルとも確認済み。会話の途中の system メッセージを変換しない設定に変えたので、ペルソナとの実際の会話でまはーが確かめる）

## 利用者が得るものと経路

Meta のキーをモデル管理のプロバイダタブで設定し、Muse Spark 1.3、
Muse Spark 1.3 Contributor、Muse Wonder (Preview) を会話モデル・軽量モデルとして選べるようにする。
これまで Muse Spark は OpenRouter 経由 (`openrouter-meta-muse-spark-1.2`) でしか使えなかった
(Muse 系では、ほかに Muse Glimmer 30B が NVIDIA NIM 経由で同梱されている)。
この 3 本の追加で、既存の OpenRouter 経由・NIM 経由の定義は変えない。

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
- 登録する会話用のモデルは `muse-spark-1.3`・`muse-spark-1.3-contributor`・`muse-wonder-1.0` の
  3 本 (まはー指定)。Meta 直結では、Muse Spark 1.2 / 1.1 (と 1.2 の Contributor 版)・画像生成・
  音声認識・SAM は登録しない (既存の `openrouter-meta-muse-spark-1.2` はそのまま残す)。
- Contributor 版は、Meta が送った内容 (プロンプト・ファイル・コンテキスト・モデルの応答) を
  将来のモデルの学習に使う代わりに、料金が大幅に安い版 (入力 $0.10 / キャッシュ入力 $0.002 /
  出力 $0.20、100 万トークンあたり)。SAIVerse から送る内容には、ペルソナの人格の指示文・記憶・
  会話がそのまま含まれる。だから学習に使われることは、表示名
  (`Muse Spark 1.3 Contributor(学習利用あり)`) とキーの設定手順の文書の両方に書き、
  モデルを選ぶ人がその場で分かるようにする。SAIVerse が最初から選んでおくモデルにはしない。
- Contributor 版は、Meta のモデル一覧で推論の深さ `max` が使えないと明記されている。
  そのためモデル定義の選択肢は minimal〜xhigh の 5 段にする (最初から選ばれている値は通常版と同じ medium)。
  2026-10-27 に実サービスへ `max` を送ったところ、エラーにはならず普通に返答した。
  `max` として扱われたのか、別の深さに落とされたのかは応答から分からないので、
  文書の記載に従って選択肢から外したままにする。
  レート制限も通常版より低い (チームあたり毎分 150 リクエスト・毎分 300 万トークン。
  通常版は毎分 3,000 リクエスト・毎分 400 万トークン)。それ以外の仕様 (コンテキスト長・
  出力上限・画像・ツール・決まった JSON の形での出力 [構造化出力]) は通常版と同じ扱いにする。
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
- 会話の途中の system メッセージは user へ変換せず、system のまま送る
  (プロバイダ定義に `default_convert_system_to_user` を書かない。書かなければ変換しない)。
  SAIVerse が会話の途中に差し込む指示を、Muse が利用者の発言ではなく指示として読むためで、
  まはーの判断 (2026-10-27)。
  根拠は 2026-10-27 の実サービスでの確認。変換しなくても Meta は受け付けた (Spark 1.3 と
  Wonder の両方で、system が会話の途中・末尾・先頭に 2 通連続・assistant の直後にある
  5 通りを送り、全部が普通に返答した)。一方、変換した会話では Wonder が「system の
  指示が user のメッセージの中に来ているので、上位の指示ではなく利用者の依頼として扱う」と
  返答した。
  OpenRouter 経由の `openrouter-meta-muse-spark-1.2` は変換したままにしている。
  OpenRouter を通した場合に変換なしで受け付けられるかは確かめていないため。
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
  文書を追加し、外部通信をせず疑似的な応答で動作を確かめるテストを作成した (PR #426、マージ済み)。
- 2026-10-27: マージ後、まはーが Contributor 版の価格を見て、`muse-spark-1.3-contributor` の
  追加を依頼した (別 PR)。同じ日にまはーがキーを設定し、テスト用の一時ディレクトリから
  テスト用に作ったダミーの入力だけで 3 モデルを実サービスに通した (ペルソナは使っていない)。
  モデル定義の初期値のままでの会話・ストリーミング・画像付きの会話・構造化出力・ツール呼び出しが
  3 モデルとも通り、使用量とキャッシュの記録も返った (費用は合計 $0.035)。
- 2026-10-27: #426 では、Meta の文書に「会話構造の検証が OpenAI より厳密」とあるのを理由に、
  OpenRouter 経由の Muse 定義と同じく会話の途中の system を user へ変換していた。
  上の実サービスでの確認を受けて、まはーの判断で変換をやめた (別 PR)。
  変更後に SAIVerse のクライアント経由で 3 モデルへ「以後の返事の最後に MEOW と付ける」という
  会話の途中の system を送り、system のまま届いて 3 モデルとも従うことを確かめた (費用 $0.007)。
