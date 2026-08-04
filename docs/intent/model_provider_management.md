# Intent: モデル＆プロバイダ管理 UI

**ステータス**: 実装完了（Phase 1-4 完了、2026-05-11）。実機検証はまはー側で実施。§10（アプリ帰属ヘッダー）のみ実装済み・実機検証待ち。

## これは何か

LLM プロバイダとモデル設定を、ユーザーが UI から完結的に追加・編集・複製・削除できるようにする。具体的には:

1. **カスタムプロバイダの UI 追加**: Kimi (Moonshot) / LM Studio / llama.cpp サーバーなど、OpenAI 互換または Ollama 互換のエンドポイントを `~/.saiverse/user_data/providers/<id>.json` として UI から作成
2. **モデルファイルの UI 編集**: builtin/expansion のモデルを UI から「編集」「複製」操作で `~/.saiverse/user_data/models/<id>.json` に派生させる
3. **チャット UI からの永続化**: `ChatOptions` で調整中の thinking_effort / cache TTL / parameters を「別名で保存」「上書き保存」でモデルファイル化
4. **接続テスト**: 新規プロバイダ作成時にエンドポイントへの疎通確認を 1 クリックで実行

## なぜ必要か

### 問題1: カスタムプロバイダ追加の敷居が高い

ユーザーから寄せられている「Kimi API と接続したい」「LM Studio と接続したい」「llama.cpp サーバーと接続したい」という要望に対し、現状の解決策は **モデル JSON を手動で書いてもらう** しかない。具体的な敷居:

- `~/.saiverse/user_data/models/` の存在を知る必要がある
- `provider`, `base_url`, `api_key_env`, `context_length`, `parameters` 等のスキーマを把握する必要がある
- API キーは別途 `.env` ファイルに環境変数として設定する必要がある
- 1つのプロバイダで複数モデルを使いたい場合、各モデル JSON に同じ `base_url` / `api_key_env` を重複記述する必要がある

OpenAI 互換 API を提供するサービス・ローカル LLM サーバーは今後も無限に増える（vLLM、SGLang、LiteLLM proxy、OpenRouter、Together AI、Fireworks 等）。その都度モデル JSON 手書きを案内するのは持続不能。

### 問題2: 既存モデル設定の編集が面倒

builtin モデル（例: `claude-opus-4-7.json`）の thinking_effort、cache TTL、temperature 等をユーザーが恒久的に変えたい場合、現状の手順は:

1. `builtin_data/models/claude-opus-4-7.json` を `~/.saiverse/user_data/models/` にコピー
2. テキストエディタで開いて該当フィールドを編集
3. `python scripts/...` で reload するか、サーバー再起動

これも UI で完結するべき作業。さらに、チャット UI で `ChatOptions` から調整した値（thinking_effort や cache 設定）は **その場限り** で、永続化したければ上記手順を別途踏む必要がある。試行錯誤と保存が分断されている。

### 問題3: プロバイダ情報がモデル JSON に分散

現状、`base_url` / `api_key_env` / `request_kwargs` / `convert_system_to_user` など **プロバイダ単位で共通の情報** を各モデル JSON に直書きしている (`saiverse/model_configs.py:80-200` 周辺の `factory.py` 分岐参照)。例えば LM Studio で 5 つのモデルを使うと、5 つの JSON に同じ `base_url: http://localhost:1234/v1` が重複する。LM Studio のポートを変更したくなったら 5 ファイル全部直す必要がある。

### 問題4: factory.py がプロバイダごとに if-elif で分岐

`llm_clients/factory.py:53-219` は `provider == "openai"`, `"anthropic"`, `"gemini"`, ... と分岐している。新しい「カスタム互換系」プロトコルを増やすには、Python コードに分岐を足す必要があり、UI 完結にならない。

## 守るべき不変条件

### 1. 3 層優先順位は維持する

`user_data/` > `expansion_data/` > `builtin_data/` の優先順位は、プロバイダ JSON ・モデル JSON ともに維持する。`saiverse/data_paths.py:iter_files()` が既にこの優先順位で検索しているため、プロバイダディレクトリも同じ仕組みに乗せる。

### 2. 既存モデル JSON は引き続き読める

すでに 100+ 個ある `builtin_data/models/*.json` には `provider` / `base_url` / `api_key_env` が直書きされている。新スキーマ `provider_ref` を追加するが、**直書きフィールドが指定されていればそれを優先**し、`provider_ref` のみが指定されている場合に参照先 JSON から継承する。後方互換は完全に維持。

### 3. API キー本体は環境変数のまま

UI 上で管理するのは「どの環境変数名（`api_key_env`）を使うか」だけで、キーの値そのものは `.env` ファイルまたは OS 環境変数で持つ。理由:

- キー値を JSON / DB に保存すると、バックアップ・git 管理・スナップショット時の漏洩リスクが上がる
- 現状の `GlobalSettingsModal` の環境変数タブが既に env 変数管理 UI を提供している（`api/routes/config.py` の `model_roles` 経由）。同じ仕組みに乗せれば追加のセキュリティ設計が不要
- env 変数方式なら CI/CD・Docker 環境でも標準的な扱いができる

### 4. builtin / expansion は不可変

builtin と expansion 配下の JSON はユーザー操作で書き換わってはいけない。「編集」アクションは常に **user_data へのコピーを作ってから編集** する。これにより:

- アップデート（`update.bat` の git pull）でユーザー編集が消えない
- expansion パッケージの更新が安全
- 「リセット」操作 = user_data の派生ファイルを削除するだけで済む

### 5. プロバイダ参照は 1 段階のみ

モデル JSON が参照するプロバイダは 1 段（`provider_ref: "lmstudio"`）のみ。プロバイダがさらに別プロバイダを参照する多段継承は許可しない。理由はループ検出やデバッグの複雑性に対するコスト効果が薄いため。

### 6. UI から作成可能なプロトコル範囲

Phase 1 では **OpenAI 互換** と **Ollama 互換** のみ。Anthropic 互換 / Gemini 互換 / ネイティブ llama.cpp / Codex OAuth 等は builtin プロバイダとして固定で同梱し、UI 作成対象外。Anthropic 互換 API（DeepSeek-Anthropic 互換等）は将来対応の余地として設計に残す。

### 7. プロバイダ削除時のモデル整合性

プロバイダを削除すると、それを参照するモデル JSON は壊れる。削除前に **参照しているモデル一覧を表示し、ユーザーに確認** を取る。強制削除した場合のモデル側の挙動は「ロード時に警告ログを出してスキップ」（既存の `model_configs.py:34` の missing field 扱いに準拠）。

### 8. チャット UI の試行錯誤体験を壊さない

`ChatOptions` の操作感は変えない。「別名で保存」「上書き保存」ボタンは追加するが、既存のスライダー・入力欄の挙動・即時反映は維持。詳細編集は別 UI に飛ばすことで、チャット UI 自体の情報密度を増やさない。

### 9. モデル固有の API 契約をモデル定義からプロバイダ境界まで保つ

モデル JSON の `parameters` は UI 表示だけでなく、実際の API request capability と一致しなければならない。上位の SEA runtime、メディア要約、keepalive などは共通 `LLMClient` 契約として `temperature` を渡すことがあるため、非対応モデルの JSON からスライダーを消すだけでは送信を防げない。

- **最終結果**: ユーザーとペルソナは、選択したモデルの最新 API 契約に適合したリクエストだけが送られ、非対応パラメータや不正な会話末尾による回避可能な 400 を受けない。
- **ライフサイクル**: builtin / expansion / user_data のモデル定義 → `model_configs` の優先解決 → factory による client 構築 → runtime からの呼び出し時 override → provider request 構築 → 外部 API、という全経路で同じ capability を維持する。
- **責任境界**: モデル JSON は capability の正典、provider client は最終送信の番人とする。UI に項目がないことだけを安全境界にせず、直接引数や古い user override が来ても provider が非対応値を送らない。
- **会話の完全性**: provider が不正な末尾 role を自動で user role に変えたり、架空の user message を追加したりしてはならない。API が禁止する prefilled model turn は送信前に検出し、原因が追える `invalid_request` とログで停止する。
- **関数呼び出しの同一性**: 上流が発行した tool call ID は Gemini の `FunctionCall.id` / `FunctionResponse.id` まで同一値で運ぶ。名前だけの照合へ情報を落とさない。
- **使用量の帰属**: 使用量と費用は API モデル名ではなく設定キー (JSON のファイル名) に帰属させる。Codex のようなサブスクで賄われる設定は従量課金版と同じ API モデル名を持つため、API 名で価格を引くと課金されていない呼び出しに従量単価が付く。`LLMClient.config_key` を価格引き当ての正典とし、client 側が `_store_usage(model=...)` で API 名に差し替えてはならない。
- **検証**: モデル JSON の価格・capability 読み込み、runtime 由来の sampling override 除去、通常 user 終端の通過、model 終端のローカル拒否、function call/response ID の往復を、外部 API を呼ばないテストで境界横断して確認する。

### 10. アプリ名の申告 (`default_headers`) — 接続に属し、会話には属さない

一部のバックエンドは、呼び出し元アプリを名乗るヘッダーを受け取り、それを公開ランキングに集計する。OpenRouter がこれで、`HTTP-Referer`（アプリの識別子）・`X-OpenRouter-Title`（表示名）・`X-OpenRouter-Categories`（カテゴリ、カンマ区切りで最大2つ）を送ったアプリだけが `openrouter.ai/apps` に載る。SAIVerse はここに `roleplay` と `general-chat` の二枚看板で並ぶことで、同種のアプリを探しているユーザーからの発見経路を得る。

- **最終結果**: SAIVerse を OpenRouter 経由で使うユーザーは、自分の利用が SAIVerse というアプリの利用として集計されることを、事前に知った上で使える。集計されるのはトークン量であって会話の内容ではない。
- **責任境界**: ヘッダーは**接続（プロバイダ）に属する**。リクエストパラメータ (`request_kwargs`) ではない。プロバイダからの継承はフィールド単位なので、ヘッダーを `request_kwargs` に混ぜると、自前の `request_kwargs` を持つモデル（GLM-5 が reasoning をこの形で有効化している）だけが申告から漏れる。同じ理由で、モデル JSON 側に個別記載させる形も採らない — OpenRouter モデルが1枚増えるたびに書き忘れが穴になる。
- **不変条件**: 申告は宣伝であって機能ではない。**ヘッダーの不備で会話が止まってはならない。** 壊れた値は警告付きで落とし、呼び出しはそのまま通す (`llm_clients/openai.py: _strip_reserved_headers`)。この約束は型だけ検べても守れない — HTTP ヘッダーは ASCII でエンコードされるので、日本語を書いた `str` は型として正しいまま送信時に例外を投げて会話を止める。**「壊れている」の判定は、送信路が受け付ける形かどうかで決める。**
- **資格情報の所有権**: ヘッダーは**接続の身元を名乗る器であって、資格情報を差し替える器ではない**。OpenAI SDK は設定由来のヘッダーを自前のヘッダーより後にマージする (per-request `extra_headers` > client `default_headers` > `api_key` 由来の認証) ため、`Authorization` を素通しすると `api_key_env` で解決したキーではない値が送信され、§9「使用量の帰属」と `provider_security` の接続先検証 (`api_key_env` と `base_url` しか見ない) を設定ファイルから迂回できてしまう。予約ヘッダーは落とす — 資格情報 (`Authorization` / `Proxy-Authorization`)、**課金の帰属先** (`OpenAI-Organization` / `OpenAI-Project`。SDK が環境変数から設定する。誰に請求されるかを設定ファイルが決められるのは、鍵を差し替えられるのと同じ穴)、経路と本文の枠 (`Host` / `Content-Length` / `Content-Type` / `Transfer-Encoding`)。
- **関所は一箇所、入口は全部そこを通す**: 上の判定は `llm_clients/openai.py: _strip_reserved_headers` だけが持ち、`default_headers` と `request_kwargs.extra_headers` の**両方**が通る。片方だけ守ると、不変条件が一方の経路で真・他方で偽になり、それは不変条件ではない。設定を読む側 (`factory`) は形だけ検べて中身の可否を判定しない — 二箇所に同じ規則を置けば必ず片方が古くなる。
- **経路の網羅**: SDK を通らない経路を作ったら、そこにも明示的に乗せる。NVIDIA NIM の structured output は生 HTTP でヘッダーを手組みしており、放置すると「structured output を頼んだときだけヘッダーが変わる」形になっていた。**同じ経路は body 側でも同じ穴を開けており** (モデルの `extra_body` が落ちる — [issue](../issues/nim_structured_output_drops_request_kwargs.md))、根治は SDK 経路との組み立て共有。LLM 呼び出しではない補助 HTTP (llama.cpp の slot cache 制御) はヘッダーが乗らないうえ認証も付かず、認証付きサーバーで cache が黙って死ぬ ([issue](../issues/llama_cache_control_requests_unauthenticated.md))。
- **透明性**: 他人の API キーで送られる申告なので、集計対象になることを `docs/api-keys/openrouter.md` に明記する。黙って計上しない。
- **名乗りは奪わない**: 予約ヘッダーに**帰属ヘッダー自体は含めない**。モデルの `extra_headers` で `HTTP-Referer` を書けば、その利用は SAIVerse ではなく書いた人のアプリとして集計される — これは穴ではなく設計。守るのは資格情報と課金の帰属先であって、SAIVerse のランキング順位ではない。**そして SAIVerse は OSS なので、ここを塞いでもコードを書き換えれば済む** (まはー裁定 2026-08-04)。塞ぎきれないものに歯止めを置くと、目的を達成しないまま利用者の自由だけが削れる。宣伝の都合で防御を足したくなったら、まずそれが本当に防げるものかを問う。
- **静かに失敗する性質**: 認識されないカテゴリ名は OpenRouter 側で**エラーにならず無視される**。綴りを間違えてもランキングに出ないだけで、API 呼び出しは成功し続ける。出荷物のヘッダー値はテストで固定する (`tests/test_provider_configs.py: TestOpenRouterAppAttribution`)。

### 11. 資格情報の束縛は「同梱かどうか」ではなく「どの層が宣言したか」で決める

API キーを無関係な送信先と結び付けさせない、という束縛は維持する。ただし**誰から守るのかを基準に据え直す**。

分かれ目は**誰がその定義を書いたか**である。`builtin_data/` は SAIVerse が同梱するもの、`user_data/` は本人が UI か手書きで置くもので、どちらも「鍵の名前と送信先の組」を承知の上で選んでいる。SAIVerse が本人から本人を守る筋合いは無い。守るべき相手は**持ち込まれた定義**——アドオンが同梱と同じ id のプロバイダ JSON を置くと3層優先で同梱を押しのけられるため、そこで同梱の鍵名を名乗られると利用者のキーが任意の宛先へ送られる。

- **最終結果**: 利用者は自分のプロバイダ設定を UI から自由に編集できる。同梱プロバイダを上書きしても、同梱の鍵名(`OPENROUTER_API_KEY` 等)をそのまま使い続けられる。一方、アドオンが**同梱を装った JSON を置く**ことで利用者の既知のキーを自分の宛先へ向けることはできない。
- **保護範囲の限界(明示)**: これはアドオンの**宣言**を縛るものであって、アドオンの**動作**を縛るものではない。アドオンのツールは同一プロセスで `exec_module` により実行されるため、アドオンのコードは `os.environ` を直接読み、独自に通信し、`user_data/` へ書き込むこともできる。これは旧設計でも同じで、本変更が広げた面ではない。**アドオンのコードを未信頼として扱うなら、それは別プロセス/権限制御という別の機構の仕事であり、この不変条件を「アドオンの隔離」と読んではならない。**
- **責任境界**: 層の判定は `provider_configs.load_configs()` が**実際に辿ったルート**をそのまま `source` として刻む(`data_paths.iter_files_with_layer()`)。読み込み後にパスから導出し直してはならない — `expansion_data/` 配下の symlink や Windows junction が別層へ解決されると、置いた場所ではなく解決先の層で信用してしまう。ファイル内に書かれた `source` / `builtin` は読み込み時に捨てる。刻印の無い設定は信用しない(fail-closed)。
- **非信頼層では「書かない」も許さない**: `api_key_env` を空にすると OpenAI 互換クライアントは `OPENAI_API_KEY` へフォールバックする (`llm_clients/openai.py`)。したがって未記入は中立ではなく「利用者の既定キーをこの `base_url` へ送れ」という宣言と同義になる。非信頼層の定義は、名前空間付き変数を名乗るか `api_key_required: false` を明記するかの**いずれかを必ず選ばせる**。歯止めの条件をプロトコル種別で書いてはならない — 目的は「非信頼の定義が、自分で名指ししていない資格情報を送らせないこと」であって、特定プロトコルの都合ではない。
- **モデル設定にも同じ層を刻む**: 資格情報を送るかどうかを決めるのはプロバイダだけではない。モデル JSON も `base_url` を直書きでき、その場合 `api_key_env` を書かなければクライアントが同梱の変数へフォールバックする。したがってモデルも `model_configs.load_configs()` で `source` を刻み、**接続先を自分で名指しする非信頼層のモデルには、プロバイダと同じ二択（自分専用の `SAIVERSE_MODEL_<キー>_API_KEY` を名乗るか、本物の `api_key_required: false` を明記するか）を課す**。`user_data` のモデルは本人が書いたものなので従来どおり自由。ファイル内に書かれた `source` は読み込み時に捨てる。
    - この規則を「同梱モデルに該当例が無いから不要」と判断してはならない。出荷物に無いことは、利用者やアドオンが後から足すモデルへの境界を何ら保証しない。
- **名前空間の一意性**: `SAIVERSE_PROVIDER_<ID>_API_KEY` の `<ID>` は非英数字を `_` に潰すため、`addon-bar` と `addon_bar` は同じ変数名に落ちる。非信頼層の定義が他パッケージ用に設定された変数を読めてしまうので、衝突する id が同時に読み込まれている場合は非信頼側を拒否する。
- **反映の範囲を約束しすぎない**: プロバイダを保存・削除・再読込すると、設定の辞書とモデル側の解決結果は更新される。しかし **すでに動いているペルソナは作成済みの LLM クライアントを持ち続ける**（`persona/core.py` が `_llm_client` と `_lightweight_llm_client` を初回生成時にそれぞれ独立してキャッシュする）。効く・効かないの境目は「過去に会話したか」ではなく **今のプロセスでそのクライアントを既に生成したか**であり、通常用と軽量用は別々に切り替わるので**同一ペルソナ内でも新旧が混在しうる**。まだ生成していないペルソナは新しい設定で動く。**この API は「保存された」ことを返すのであって「今この瞬間から全員に効く」ことは返さない。** 走行中のリクエストと競合させずにクライアントを差し替える仕組みは別途必要で、[`docs/issues/provider_change_does_not_reach_live_personas.md`](../issues/provider_change_does_not_reach_live_personas.md) に記録した。
- **保存経路と実送信経路は別扱い**: 保存 (`PUT`/`POST /api/providers`) は user_data へ落ちるので本人の宣言として扱う。一方、接続テスト (`POST /api/providers/test`) は**指定された環境変数の値を指定されたホストへ実際に送る**。既定の loopback 起動では owner 認証が入らない (`main.py` は LAN モード時のみ `OwnerAuthMiddleware` を付ける)。**`provider_id` はリクエスト本文の一値にすぎないので、既知の id を名乗るだけでその層の信頼を得てはならない** — さもなくば `provider_id: "openrouter"` と他人の `base_url` の組で同梱キーを任意の宛先へ送れる。信頼を引き継ぐのは、`base_url` と `api_key_env` の両方が保存済みの値と一致するときだけ。変更した組み合わせは非信頼として扱う (変数名を書かない疎通確認は何も送らないので従来どおり通る)。保存はできるがテストは通らない組み合わせが生じるが、秘密の実送信を先に緩める側には倒さない。
- **不変条件**: 判定基準は `provider_security.validate_provider_config` の一箇所に置く。保存時だけの検査にしてはならない — 同じ関数が `llm_clients/factory.py` の client 構築時にも呼ばれるため、保存経路だけを緩めると「保存は通るのに次の発話で落ちる」状態になる。
- **旧設計との差**: 2026-08-04 以前は `builtin` フラグの有無で判定していた。UI の上書き編集は保存前にこのフラグを落とすため、`api_key_env` を持つ同梱プロバイダ8種は**値を変えずに保存するだけで必ず 400** になっていた(不変条件 4 が定めた「編集 = user_data へコピーしてから編集」が成立していなかった)。またフラグがファイル内の記述から読まれていたため、アドオンが `"builtin": true` と自筆するだけで束縛を素通りできた。
- **検証**: 同梱12種すべてが無変更の保存で 200 を返すこと、アドオン層の定義が同梱の鍵名を借りられないこと、自筆の層刻印が無視されること、刻印の無い設定が拒否されること、上書き後もそのプロバイダを参照するモデルが構築できることを、外部 API を呼ばないテストで固定する(`tests/test_provider_configs.py: TestCredentialLayerBinding`)。

## 設計

### A. プロバイダ JSON スキーマ

新規ディレクトリ: `~/.saiverse/user_data/providers/` および `builtin_data/providers/`

スキーマ例（OpenAI 互換）:

```json
{
  "id": "lmstudio",
  "display_name": "LM Studio (local)",
  "protocol": "openai_compat",
  "base_url": "http://localhost:1234/v1",
  "api_key_env": "LMSTUDIO_API_KEY",
  "default_request_kwargs": {},
  "default_convert_system_to_user": false,
  "default_supports_images": false,
  "default_max_image_bytes": 5242880
}
```

スキーマ例（Ollama 互換）:

```json
{
  "id": "ollama-remote",
  "display_name": "Ollama (Remote)",
  "protocol": "ollama_compat",
  "base_url": "http://192.168.1.10:11434"
}
```

スキーマ例（builtin、Anthropic 純正）:

```json
{
  "id": "anthropic",
  "display_name": "Anthropic",
  "protocol": "anthropic_native",
  "api_key_env": "CLAUDE_API_KEY"
}
```

どの層のファイルかは**ファイル自身には書かない**。読み込み時に `source` として刻まれる（不変条件 11）。

**フィールド説明**:

- **`id`**: プロバイダ一意識別子（ファイル名 stem と一致させる）
- **`protocol`**: `openai_compat` / `ollama_compat` / `anthropic_native` / `gemini_native` / `xai_native` / `openai_codex` / `nvidia_nim`。UI 作成可能なのは前2つのみ
- **`base_url` / `api_key_env`**: 該当プロトコルが必要とする接続情報
- **`api_key_env_alternates`**: `api_key_env` の代わりに受け付ける環境変数名の配列。**どれか1つでも設定されていればそのモデルは利用可能**として扱う。Gemini が該当（`GEMINI_API_KEY` に加えて無料枠の `GEMINI_FREE_API_KEY`）。判定は `saiverse/model_configs.py` の `_get_required_env_vars()` → `is_model_available()` で、`GET /api/models` がモデル一覧に出すかどうかを決める
- **`api_key_required`**: `false` なら認証しないバックエンド（LM Studio / llama.cpp server などのローカルサーバー）。キー未設定でもモデル一覧に出し、OpenAI 互換クライアントにはプレースホルダのキーを渡す（SDK が空キーを拒否するため）。`api_key_env` が併記され、その環境変数が設定されていればそちらが優先される
- **`default_*`**: モデル JSON 側で `request_kwargs` / `convert_system_to_user` 等が未指定の場合のフォールバック値
- **`default_headers`**: そのバックエンドへの全リクエストに乗せるヘッダー（文字列のペア）。リクエストごとの引数ではなくクライアント生成時に渡すので、`request_kwargs` とは独立して効く。OpenRouter のアプリ帰属ヘッダーがこれ（§10）。`openai_compat` と `nvidia_nim` で有効
- **`source`**（JSON には書かない・読み込み時に付く）: そのファイルを読み出した層（`builtin` / `expansion` / `user_data`）。資格情報の許容範囲をここで決める（不変条件 11）。**ファイルに書いても捨てられる。** API 応答の `builtin` フィールドはこれを `source == "builtin"` として導出した表示用の値で、UI はこれを見て「上書き編集」表示と削除の可否を決める

### B. モデル JSON の `provider_ref` 対応

新フィールド `provider_ref` を追加。解決順序:

1. モデル JSON に `base_url` / `api_key_env` 等が直書きされていればそれを使う（既存挙動）
2. 直書きがなく `provider_ref` があれば、参照先プロバイダ JSON から継承
3. 両方ない場合は従来通り `provider` フィールドの builtin デフォルト

例:

```json
{
  "model": "qwen2.5-72b-instruct",
  "display_name": "Qwen 2.5 72B (LM Studio)",
  "provider_ref": "lmstudio",
  "context_length": 32768,
  "parameters": {
    "temperature": { "type": "slider", "min": 0, "max": 2, "default": 0.7 }
  }
}
```

`provider_ref` 解決後、内部的には旧 `provider` / `base_url` / `api_key_env` が埋まった状態と等価になる。`factory.py` から見れば既存の dict と同じ構造。

実装場所: `saiverse/model_configs.py` に `_resolve_provider_ref(config: dict) -> dict` を追加し、`load_configs()` 内で各モデル設定読み込み時に解決を実行する。

### C. プロバイダ設定モジュール

新規モジュール: `saiverse/provider_configs.py`

API:

```python
def load_configs() -> dict[str, dict]: ...
def reload_configs() -> dict[str, dict]: ...
def get_provider(provider_id: str) -> dict | None: ...
def save_provider(provider_id: str, config: dict) -> None: ...  # user_data に書く
def delete_provider(provider_id: str) -> None: ...  # user_data からのみ削除可
def list_models_using_provider(provider_id: str) -> list[str]: ...  # 削除前確認用
def is_builtin(provider_id: str) -> bool: ...
```

`load_configs()` は `iter_files_with_layer(PROVIDERS_DIR, "*.json")` で 3 層優先順位読み込み。走査したルートが `(path, layer)` で返るので、その `layer` をそのまま `source` として刻む。**ファイル内に書かれた `source` / `builtin` は捨てる**（不変条件 11）。API 応答の `builtin` フィールドは `source == "builtin"` から導出した表示用の値。

### D. factory.py の更新

`llm_clients/factory.py` の分岐を以下のように変更:

```python
def get_llm_client(model: str, provider: str, context_length: int, config: dict | None = None) -> LLMClient:
    if config is None:
        config = get_model_config(model)

    # provider_ref 解決後の config が来る前提（model_configs.py 側で解決済み）
    protocol = config.get("protocol") or _legacy_provider_to_protocol(config.get("provider"))

    if protocol == "openai_compat":
        return _build_openai_client(model, config, context_length)
    elif protocol == "ollama_compat":
        return _build_ollama_client(model, config, context_length)
    elif protocol == "anthropic_native":
        return _build_anthropic_client(model, config, context_length)
    # ... 以下既存
```

`_legacy_provider_to_protocol()` は `provider == "openai"` → `"openai_compat"`、`"ollama"` → `"ollama_compat"`、その他はそのまま native 扱いにする後方互換マッピング。既存モデル JSON は `protocol` フィールドを持たないため、必ずこの経路を通る。

各 `_build_*_client()` 関数は現状の if-elif ブロックの中身を関数化したもの。ロジック変更はせず分割のみ。

### E. API エンドポイント

新規ルート: `api/routes/providers.py`

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/providers/` | プロバイダ一覧（builtin / user / expansion バッジ付き） |
| GET | `/api/providers/{id}` | プロバイダ詳細 |
| POST | `/api/providers/` | プロバイダ新規作成（user_data へ） |
| PUT | `/api/providers/{id}` | プロバイダ更新（builtin の場合は自動で user_data コピーに切り替えて編集） |
| DELETE | `/api/providers/{id}` | プロバイダ削除（user_data 配下のみ可） |
| POST | `/api/providers/{id}/test` | 接続テスト（後述） |
| GET | `/api/providers/{id}/models` | このプロバイダを参照するモデル一覧 |
| POST | `/api/providers/reload` | ディスクから再読み込み |

既存 `api/routes/config.py` のモデル管理エンドポイントを拡張:

| メソッド | パス | 用途 |
|---------|------|------|
| POST | `/api/config/models/` | モデル新規作成（user_data へ） |
| PUT | `/api/config/models/{key}` | モデル更新（builtin/expansion なら user_data コピー作成→編集） |
| DELETE | `/api/config/models/{key}` | モデル削除（user_data 配下のみ可） |
| POST | `/api/config/models/{key}/clone` | モデル複製（新しい key で user_data に保存） |
| POST | `/api/config/models/save-from-chat` | チャット UI の現在設定を新規モデル化（後述） |

### F. 接続テスト

`POST /api/providers/{id}/test` の実装:

- **OpenAI 互換**: `GET {base_url}/models` を Bearer token 付きで叩く。200 が返れば成功、レスポンスのモデル一覧を返す（フロントで「あなたのプロバイダは X 個のモデルを公開しています」と表示してモデル作成支援）
- **Ollama 互換**: `GET {base_url}/api/tags` を叩く。同様にモデル一覧を返す
- タイムアウト 5 秒、エラー時はステータスコード・エラーメッセージを構造化して返す

接続テストの結果はサーバー側で保存しない（その時点の疎通確認のみ）。

### G. フロントエンド UI

新規セクション「モデル管理」を `GlobalSettingsModal` の新タブとして追加（独立画面化はしない、既存パターン踏襲）。

タブ内構成:

```
┌─────────────────────────────────────────────┐
│ [プロバイダ] [モデル]                       │
├─────────────────────────────────────────────┤
│ プロバイダタブ:                              │
│  ┌────────────────────────────────────────┐ │
│  │ OpenAI         [builtin]  [接続テスト] │ │
│  │ Anthropic      [builtin]  [接続テスト] │ │
│  │ LM Studio (local) [user]  [編集][削除] │ │
│  │ + 新規プロバイダ作成                    │ │
│  └────────────────────────────────────────┘ │
│                                              │
│ モデルタブ:                                  │
│  ┌────────────────────────────────────────┐ │
│  │ Filter: [All] [Anthropic] [LM Studio]  │ │
│  │ ────────────────────────────────────── │ │
│  │ Claude Opus 4.7 [builtin]              │ │
│  │   ↳ [編集 → user_data コピー作成]      │ │
│  │   ↳ [複製]                             │ │
│  │ Qwen 2.5 72B (LM Studio) [user]        │ │
│  │   ↳ [編集] [削除]                      │ │
│  │ + 新規モデル作成                        │ │
│  └────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘
```

新規プロバイダ作成モーダル:

- プロトコル選択（OpenAI 互換 / Ollama 互換）
- ID（slug 形式、ファイル名になる）
- 表示名
- base_url
- api_key_env（OpenAI 互換のみ、Ollama 互換は通常不要）
- 「接続テスト」ボタン → 成功時に「モデルを取り込む」ボタン表示

新規モデル作成モーダル:

- プロバイダ選択（プルダウン）
- モデル ID（API 呼び出し時の名称）
- 表示名
- context_length
- supports_images チェックボックス
- parameters（temperature 等のスライダー定義、Phase 1 では JSON エディタ。構造化フォームは将来検討）
- pricing（任意、入力支援あり）
- cache 設定（プロバイダが対応している場合のみ表示）

### H. チャット UI 連携

`ChatOptions.tsx` への追加:

- **「別名で保存」ボタン**: 現在表示中のモデル設定 + 編集中のパラメータを新規モデル JSON 化。モーダルで新しい `display_name` と key 名を入力させる
- **「このモデルに上書き保存」ボタン**:
  - user_data モデルの場合: 直接上書き
  - builtin/expansion モデルの場合: 「このモデルは builtin です。user_data にコピーを作成して編集します」と確認ダイアログ → user_data コピーを作って上書き
- **「詳細編集」リンク**: モデル管理 UI のモデル編集モードを **チャット UI の上にモーダル重ねで開く**（チャット UI は閉じない）。編集完了後にモデル管理モーダルを閉じれば元のチャット UI に戻る

「別名で保存」「上書き保存」のリクエストは `POST /api/config/models/save-from-chat` または既存の `PUT /api/config/models/{key}` に流す。チャット UI の状態（current_values）をそのままペイロードに含める。

### I. ディレクトリ構成

```
builtin_data/
├── providers/
│   ├── anthropic.json       ← protocol: anthropic_native（層は読み込み時に付く）
│   ├── gemini.json          ← protocol: gemini_native
│   ├── openai.json          ← protocol: openai_compat (公式 OpenAI)
│   ├── ollama.json          ← protocol: ollama_compat (localhost:11434)
│   ├── nvidia_nim.json      ← protocol: nvidia_nim
│   ├── xai.json             ← protocol: xai_native
│   └── openai_codex.json    ← protocol: openai_codex
└── models/                  ← 既存 100+ JSON、当面は触らない（互換性）

~/.saiverse/user_data/
├── providers/
│   ├── lmstudio.json        ← ユーザーが UI で作成
│   ├── kimi.json
│   └── llama-cpp-server.json
└── models/
    ├── qwen-via-lmstudio.json    ← provider_ref: "lmstudio"
    ├── kimi-k2.json              ← provider_ref: "kimi"
    └── claude-opus-4-7-tweaked.json  ← builtin からコピーして編集
```

### J. データベース変更なし

プロバイダ・モデル設定は JSON ファイルのまま。DB スキーマ変更不要。

## 設計判断の理由

### なぜ provider_ref を独立 JSON にするか（Q2 の答え）

選択肢として「プロバイダ概念は UI 入力支援だけにし、保存時は各モデル JSON に展開する」もあったが、独立 JSON 方式を採った理由:

- **1 プロバイダ変更 → 全モデル波及**: LM Studio のポートを変更したい時、プロバイダ JSON 1 つを直すだけで参照する全モデルに反映される
- **API キー env 変数名の集約**: プロバイダ単位で `api_key_env` を持てば、複数モデルを使うユーザーが env 変数名を覚え直さなくて済む
- **プロバイダ削除時の整合性チェックが可能**: 「このプロバイダを使っているモデル」を逆引きできる
- **既存仕組みとの互換**: モデル JSON 側の直書きフィールドを優先する仕様にすることで、100+ 個の既存 builtin モデルを書き換えずに済む

### なぜ API キーを環境変数のままにするか

UI で API キー値そのものを保存する選択肢もあったが、以下の理由で env 変数方式を維持:

- **既存仕組みの再利用**: `GlobalSettingsModal` の環境変数タブが既に存在する。プロバイダ作成時に「`LMSTUDIO_API_KEY` を設定してください」と促し、env タブへのリンクを置けば UX は完結
- **セキュリティ**: JSON / DB に平文保存するとバックアップ・スナップショット・git 経由の漏洩リスクが増える。暗号化保存するなら鍵管理の問題が発生
- **CI/CD・Docker 互換**: env 変数なら標準的な秘匿情報の扱い方に乗れる
- **ローカル LLM サーバーは API キー不要**: LM Studio や llama.cpp サーバーはデフォルトで認証なし。`api_key_env` を空にすれば dummy key (`"sk-no-key-required"` 等) を OpenAIClient 内部で使う既存挙動が活きる

### なぜ Anthropic 互換を Phase 1 に入れないか

DeepSeek が Anthropic 互換 API を出す等、需要は今後発生し得るが:

- 現時点でユーザーから明示的な要望は OpenAI 互換系（Kimi/LMStudio/llama.cpp）のみ
- Anthropic クライアントは thinking_effort / cache TTL 等の固有機能が多く、汎用「Anthropic 互換プロバイダ」として整理するには既存 `AnthropicClient` の設計レビューが必要
- 互換系 Anthropic API がどこまで純正 API の機能を再現するかは実装ごとにまちまちで、汎用化の難易度が高い

将来的に追加するときは `protocol: "anthropic_compat"` を増やすだけの設計余地は残してある。

### なぜ builtin の直接編集を許さないか

選択肢として「builtin JSON を直接書き換え可能」もあったが:

- `update.bat` の git pull で builtin が上書きされ、ユーザー編集が消える
- リポジトリ管理されているファイルが手元で書き換わると git 状態が汚れる
- 「builtin に戻したい」操作が「リポジトリから再取得」と等価になり、編集をリセットする標準的な手段がない

「編集 → 自動で user_data コピー作成」フローなら、リセット = user_data ファイル削除で完結する。

### なぜ接続テストを MVP に含めるか

新規プロバイダ作成時に「設定して保存 → 別画面でモデルを作って割り当て → チャットで初めて呼び出して失敗」という長いフィードバックループは UX として苦しい。プロバイダ作成画面で即座に疎通確認できれば:

- base_url のタイポ（末尾 `/v1` 忘れ等）が即座に判明
- API キー env 変数が未設定なことに気付ける
- レスポンスから取得したモデル一覧で、モデル作成画面のモデル ID プルダウンを補助できる

実装コストも低い（プロトコルごとに 1 エンドポイント叩くだけ）。

### なぜチャット UI に編集本体を置かず、モデル管理 UI に引き継ぎとするか（Q3 の答え）

`ChatOptions` に thinking_effort / cache / parameters の全フィールド編集を押し込むと:

- チャット UI が情報過多になり、本来の試行錯誤体験が損なわれる
- 同じ編集 UI が「チャット内」「モデル管理画面」の 2 箇所に存在することになり、保守コストが倍

チャット UI は「現在使っている値の試行錯誤＋その値を保存する 2 操作」に絞り、フル編集はモデル管理 UI に集約する。

### なぜプロトコルを `openai_compat` / `ollama_compat` の 2 種に分けるか

LM Studio は OpenAI 互換 API を提供するため `openai_compat` で扱える。Ollama 互換 API も提供しているが、SAIVerse からは `OpenAIClient` で十分動作する。それでも分けたのは:

- Ollama 互換サーバー（`OllamaClient`）は API キー不要・コンテキスト長指定方法が異なる等、`OpenAIClient` と挙動差がある
- Ollama 互換であることをユーザーが明示できると、UI で適切な接続テスト（`/api/tags` vs `/v1/models`）を実行できる
- 将来的に Ollama 固有機能（ローカルモデルのプル等）を UI から触る余地を残せる

## スコープ

### Phase 1 — バックエンド基盤

1. `~/.saiverse/user_data/providers/` および `builtin_data/providers/` ディレクトリ概念を追加（`saiverse/data_paths.py` に `PROVIDERS_DIR` 追加）
2. `saiverse/provider_configs.py` 新規実装（`load_configs` / `get_provider` / `save_provider` / `delete_provider` / `list_models_using_provider` / `is_builtin`）
3. builtin プロバイダ JSON を `builtin_data/providers/` 配下に作成（anthropic, gemini, openai, ollama, llama_cpp, nvidia_nim, xai, openai_codex の 8 個）
4. `saiverse/model_configs.py` の `load_configs()` で `_resolve_provider_ref()` を実行し、`provider_ref` 指定時にプロバイダ JSON からフィールドを継承
5. `llm_clients/factory.py` を `protocol` フィールドベースの分岐に変更（`_resolve_protocol()` + `_LEGACY_PROVIDER_TO_PROTOCOL` で後方互換）。各分岐の関数化は実施せず、既存ロジックに触らない方針で最小変更とした（関数分割は将来 Phase で検討）
6. テスト: 既存全モデル JSON が新ロード経路で同じ挙動になること、`provider_ref` 解決の優先順位、プロバイダ削除時の参照モデル列挙

### Phase 2 — API

7. `api/routes/providers.py` 新規実装（一覧・詳細・作成・更新・削除・接続テスト・参照モデル列挙・reload）
8. `api/routes/config.py` 拡張（モデル CRUD、複製、save-from-chat）
9. `_get_required_env_vars()` を `provider_ref` 経由でも env 変数を解決できるよう更新
10. テスト: API レベルの CRUD、builtin → user_data 自動コピー、接続テスト

### Phase 3 — フロントエンド

11. `GlobalSettingsModal` に「モデル管理」タブ追加
12. `ProviderManagementPanel.tsx` 新規（プロバイダ一覧・編集モーダル・接続テスト）
13. `ModelManagementPanel.tsx` 新規（モデル一覧・フィルタ・編集モーダル・複製・削除）
14. `ProviderEditor.tsx` / `ModelEditor.tsx` 新規（フォーム、parameters JSON エディタ）
15. `ChatOptions.tsx` に「別名で保存」「上書き保存」「詳細編集」ボタン追加
16. UX 検証: 新規プロバイダ作成 → 接続テスト → モデル作成 → チャットで使用、の一連の流れがスムーズか

### Phase 4 — ドキュメント整備

17. `CLAUDE.md` の「Model Configuration」セクション更新（provider_ref、UI 操作、protocol 一覧）
18. `docs/getting-started/` に「カスタムプロバイダ追加ガイド」を追記（Kimi・LM Studio・llama.cpp サーバー の具体例）
19. `README.md` の機能リストに記載

### 将来 Phase（範囲外、メモのみ）

- **モデル単位の任意fallback chain**（2026-07-16、まはー構想）:
  - 各model configから、障害時に切り替える別modelを順序付きで指定できるようにする。
  - fallback先は同一provider・paid modelに限定しない。free→paid、重量級→軽量、remote→local、別providerへの切替を同じ仕組みで表現する。
  - 現行routerのように「free失敗時は固定paid clientへ切替」「一度成功するとprocess全体をpaidへ固定」する暗黙policyは廃止し、requestごとにmodel configの明示chainを評価する。
  - failure分類は例外文字列substringではなくproviderの型付きstatus/retry metadataを使う。認証・入力不正・content policy等、fallbackしても解決しない失敗はchainを進めない。
  - streaming開始後や外部tool副作用後にはmodel fallbackでrequest全体を再実行しない。fallback可能なのはLLM出力・副作用がまだ確定していない境界だけとする。
  - chainの循環、存在しないmodel、利用不能credential、最大段数をload時に検証する。実行logには元model、選択先、分類済み理由を本文なしで残す。
  - paid modelを含むchainは明示opt-inとし、将来はbudget/cost ceilingもchain policyへ持たせる。
  - **今回（監査第二陣）は新規実装しない**。まず暗黙のprocess-global paid固定を停止し、明示設定なしではfallbackしない安全な暫定形へ直す。その後、本項を正典としてUI・schema・runtimeを設計する。
- `llm_clients/factory.py` の各 `if protocol == ...` 分岐を `_build_*_client()` 関数に分割するリファクタ（Phase 1 では分岐条件の変更のみで保留）
- Anthropic 互換プロトコル（DeepSeek-Anthropic 互換等）対応
- Gemini 互換プロトコル対応
- プロバイダごとの推奨パラメータプリセット（OpenRouter 用、vLLM 用 等）
- プロバイダの export / import（JSON ファイルダウンロード・アップロード、コミュニティ共有）
- 接続テスト時に取得したモデル一覧から「全モデルをまとめて作成」ボタン

## 検証観点

実機検証で必ず通すケース:

- **新規プロバイダ作成（OpenAI 互換）**: LM Studio をローカルで起動 → UI でプロバイダ作成 → 接続テスト成功 → モデル作成 → チャットで応答取得
- **新規プロバイダ作成（Ollama 互換）**: Ollama を別マシンで起動 → リモート IP でプロバイダ作成 → 接続テスト成功
- **接続失敗の挙動**: base_url タイポ・API キー env 未設定の場合、エラー内容が UI に表示される
- **builtin モデル編集**: Claude Opus 4.7 の thinking_effort を編集 → user_data にコピーが作成される → 編集前の builtin は変わらない
- **チャットからの保存**: ChatOptions で temperature を変更 → 「別名で保存」→ 新規モデルがリストに出現
- **チャットからの上書き（builtin）**: ChatOptions で値変更 → 「上書き保存」→ 確認ダイアログ → user_data コピー作成 → 上書き
- **プロバイダ削除時の参照確認**: 使用中のプロバイダを削除しようとすると、参照モデル一覧が表示されて警告
- **`provider_ref` 解決**: プロバイダの base_url を変更 → 参照する全モデルが新 base_url で動作
- **既存 builtin モデルの後方互換**: 100+ 個の既存モデルが従来通り読み込まれ、API 経由で利用可能
- **削除されたプロバイダを参照するモデル**: ロード時に警告ログ → モデル管理 UI で「壊れた参照」警告表示 → 修復 UI から別プロバイダに付け替え可能

## 補足: 設計上の前提

- **接続テストはサーバー側から実行**: フロントから直接 `localhost:1234` 等を叩くと、SAIVerse が別マシンで動いていてユーザー PC で LM Studio が動いている場合に接続できない。サーバー側 API として実装することで「SAIVerse が見える場所」での疎通確認になる
- **expansion_data のプロバイダも対象**: 将来的にアドオンが独自プロバイダを同梱する余地として、`expansion_data/<addon>/providers/` も `iter_files()` の対象に含める
- **既存の `is_model_available()` との関係**: 環境変数チェックは引き続き機能。`provider_ref` 経由で `api_key_env` を解決した後、既存ロジックがそのまま使える
- **`get_provider_for_model()` ヘルパー**: UI での「このモデルはどのプロバイダを使っているか」表示用に、新規ヘルパーを `model_configs.py` に追加（`provider_ref` 優先、なければ `provider` フィールドから推定）
