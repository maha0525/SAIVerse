# プロバイダ一覧

`builtin_data/providers/` に同梱されるプロバイダ（LLM バックエンドへの接続先定義）の一覧。モデルは `provider_ref` でここを参照する。設計は [`docs/intent/model_provider_management.md`](../intent/model_provider_management.md)、エンドユーザー向け設定は [`docs/custom_providers.md`](../custom_providers.md) を参照。

## 組み込みプロバイダ

| id | 表示名 | protocol | base_url | api_key_env |
|---|---|---|---|---|
| `openai` | OpenAI | `openai_compat` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `anthropic` | Anthropic | `anthropic_native` | — | `CLAUDE_API_KEY` |
| `gemini` | Google Gemini | `gemini_native` | — | `GEMINI_API_KEY` |
| `xai` | xAI | `xai_native` | — | `XAI_API_KEY` |
| `nvidia_nim` | NVIDIA NIM | `nvidia_nim` | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` |
| `openrouter` | OpenRouter | `openai_compat` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `openai_codex` | OpenAI Codex (ChatGPT OAuth) | `openai_codex` | — | — |
| `plamo` | PLaMo (Preferred Networks) | `openai_compat` | `https://api.platform.preferredai.jp/v1` | `PLAMO_API_KEY` |
| `sakana` | Sakana AI | `openai_compat` | `https://api.sakana.ai/v1` | `SAKANA_API_KEY` |
| `lmstudio` | LM Studio (local) | `openai_compat` | `http://127.0.0.1:1234/v1` | —（不要） |
| `ollama` | Ollama (local) | `ollama_compat` | —（未設定＝自動探索） | — |
| `llama_cpp_server` | llama.cpp Server (local) | `openai_compat` | `http://127.0.0.1:8080/v1` | —（不要） |
| `typesafe` | TypeSafe System One | `jev_compat` | `https://api.typesafe.ai` | `TYPESAFE_API_KEY` |
| `openrouter_systemone` | OpenRouter (System One) | `jev_compat` | `https://openrouter.ai` | `OPENROUTER_API_KEY` |
| `localjev` | localjev (local) | `jev_compat` | `http://127.0.0.1:8080` | —（不要） |
| `openai_decisions` | OpenAI Decisions | `jev_compat` | `https://api.openai.com` | `OPENAI_API_KEY` |

`gemini` だけは `api_key_env_alternates: ["GEMINI_FREE_API_KEY"]` を併せ持つ。**`GEMINI_API_KEY` と `GEMINI_FREE_API_KEY` のどちらか一方が設定されていればモデル一覧に出る**（無料枠だけの利用を想定）。判定は `saiverse/model_configs.py` の `_get_required_env_vars()`。

### API キー不要のプロバイダ

`lmstudio` と `llama_cpp_server` は `api_key_required: false` を持つ。認証をしないローカルサーバー向けの宣言で、次の2つが変わる。

- **モデル一覧に出る条件** — キーが未設定でも利用可能として扱う（`_get_required_env_vars()` が空を返す）
- **クライアント生成** — OpenAI 互換クライアントは空のキーを受け付けないため、SAIVerse がプレースホルダを渡す。この宣言が無いと `OPENAI_API_KEY` を要求され、OpenAI のアカウントを持たない利用者はローカルモデルを起動できない

ローカルサーバー側に認証を掛けている場合は `api_key_env` を併記すれば、そちらが優先される（プレースホルダは環境変数が空のときだけ使われる）。この項目は UI（モデル管理 > プロバイダ > 「API キーなしで接続できるサーバー」）からも設定できる。

### OpenAI Codex は API キーでなくログインで認証する

`openai_codex` プロバイダは ChatGPT サブスクリプションの OAuth トークンを使うため、`api_key_env` を持たない。認証の入り口は2つあり、SAIVerse は次の優先順位でトークンを探す（解決は `llm_clients/openai_codex_auth.py` が一元管理）。

1. **SAIVerse 自身のログイン**（推奨） — UI（モデル管理 > プロバイダ > `openai_codex` 行の「ChatGPT でログイン」）からデバイスコード方式でログインする。ブラウザで `auth.openai.com/codex/device` を開いて画面のコードを入力すると完了し、トークンは `~/.saiverse/user_data/codex_auth.json` に保存される。Codex CLI のインストールは不要
2. **Codex CLI への相乗り**（従来方式） — `codex login` が作る `~/.codex/auth.json` を読む。`~/.codex/config.toml` に `cli_auth_credentials_store_mode = "file"` が必要

1 のファイルが存在する間は常に 1 が使われる（壊れていても 2 へ黙って乗り換えない — 別アカウントへの無断切替を防ぐため）。期限切れトークンはどちらの方式でも SAIVerse が自動で更新し、**読んだ方のファイルへ**書き戻す。UI からのログアウトは 1 のファイルを消すだけで、`~/.codex` には決して触れない。設計は [`docs/intent/codex_subscription_auth.md`](../intent/codex_subscription_auth.md)。

### OpenRouter だけアプリ名を名乗る

`openrouter` プロバイダは `default_headers` を持ち、LLM 呼び出しに次の3つを乗せる。OpenRouter はこれを見て呼び出し元アプリを判別し、[公開アプリランキング](https://openrouter.ai/apps) に集計する。

| ヘッダー | 値 | 役割 |
|---|---|---|
| `HTTP-Referer` | `https://saiverse.net` | アプリの識別子。**これが無いとアプリのページ自体が作られない** |
| `X-OpenRouter-Title` | `SAIVerse` | ランキング上の表示名 |
| `X-OpenRouter-Categories` | `roleplay,general-chat` | カテゴリ。カンマ区切りで最大2つ |

`default_headers` はプロバイダ設定の汎用項目で、`openai_compat` と `nvidia_nim` で有効。**接続時に一度渡す**ため、モデル JSON 側の `request_kwargs` とは独立して効く（`request_kwargs` に混ぜると、自前の `request_kwargs` を持つモデルだけヘッダーが落ちる）。値が壊れていてもその項目を捨てて呼び出しは続行する — 申告は宣伝であって機能ではないため。

効く範囲には次の4つの限界がある。

- **予約ヘッダーは書いても捨てられる** — `Authorization` / `Proxy-Authorization` / `OpenAI-Organization` / `OpenAI-Project` / `Host` / `Content-Length` / `Content-Type` / `Transfer-Encoding`。資格情報・課金の帰属先・経路・本文の枠はクライアントが所有する。特に `Authorization` は、SDK が `api_key_env` から組んだ資格情報より**後に**マージされるため、素通しすると設定ファイルから送信キーを差し替えられてしまう。判定はクライアント境界の一箇所 (`llm_clients/openai.py: _strip_reserved_headers`) にあり、モデル側の `request_kwargs.extra_headers` も同じ関所を通る
- **値に書けるのは ASCII だけ** — HTTP ヘッダーは ASCII でエンコードされるため、日本語などを入れるとリクエストを組み立てる時点で失敗する。関所が送信前に捨てるので会話は止まらず、その項目の申告が消えるだけになる（改行を含む値も同様に捨てる）
- **申告そのものは利用者が上書きできる（意図してそうしている）** — `request_kwargs.extra_headers` はリクエスト単位で `default_headers` に勝つため、モデル側に `HTTP-Referer` などを書けば、そのモデルの利用は SAIVerse ではなく別アプリとして集計される。**SAIVerse をフォークして自分のアプリ名で名乗る道を塞がないための設計**で、名乗りを予約ヘッダー扱いにはしない。同梱の OpenRouter モデルはどれも書いておらず、テストで見張っている
- **クライアント外の補助 HTTP には乗らない** — llama.cpp の slot cache 制御 (`llm_clients/llama_cache.py`) は認証ヘッダーも含めて何も付けずに飛ぶ。認証を要求する remote サーバーで `llama_slot_save_path` を使うと、会話は通るのに cache の保存・復元だけが失敗して黙って無効化される（[未解決 issue](../issues/llama_cache_control_requests_unauthenticated.md)）。**サーバーの手前にプロキシが居る場合も同じ形で黙って無効化される** — llama-swap は `/slots` を中継しないため save が毎回 404 になる（[未解決 issue](../issues/llama_slot_cache_inert_behind_llama_swap.md)）

カテゴリ名を綴り間違えても**エラーにならず無視される**（ランキングに出ないだけ）。出荷値は `tests/test_provider_configs.py: TestOpenRouterAppAttribution` で固定している。設計の経緯は `docs/intent/model_provider_management.md` §10、利用者向けの説明は `docs/api-keys/openrouter.md`。

### Ollama の接続先だけ決め方が違う

他のプロバイダは `base_url` が固定だが、Ollama は待ち受けアドレスが環境によってばらつくため **未設定なら自動探索する**（`llm_clients/ollama.py` の `_probe_base`）。優先順位:

1. **設定されたアドレス** — `ollama` プロバイダの `base_url`（UI のプロバイダタブで編集 → `user_data` 上書き）、モデル JSON の `base_url`、環境変数 `OLLAMA_BASE_URL` / `OLLAMA_HOST` のいずれか。カンマ区切りで複数指定可
2. **自動探索** — 1が何も無いときだけ。`127.0.0.1:11434` → `localhost:11434` → `host.docker.internal:11434` → `172.17.0.1:11434` の順に実接続を試し、応答したものを採用（結果はプロセス内でキャッシュ）

**設定されたアドレスは探索対象を上書きせず、限定する。** 指定先が応答しなくても別のホストへ勝手に繋ぎ替えず、そのアドレスを保持して警告を出す（設定したはずの接続先と違う場所に繋がる事故を防ぐため）。

同梱の `ollama` プロバイダが `base_url` を持たないのはこのため。ここにアドレスを書くと全 Ollama モデルが「設定済み」になり、環境変数と自動探索の両方が無効になる。

## protocol の種類

| protocol | 説明 | UI から新規作成 |
|---|---|:--:|
| `openai_compat` | OpenAI 互換 API（LM Studio / llama.cpp server / Kimi 等） | ✓ |
| `ollama_compat` | Ollama 互換 API | ✓ |
| `anthropic_native` | Anthropic ネイティブ | ✗（builtin のみ） |
| `gemini_native` | Gemini ネイティブ | ✗（builtin のみ） |
| `xai_native` | xAI ネイティブ | ✗（builtin のみ） |
| `nvidia_nim` | NVIDIA NIM | ✗（builtin のみ） |
| `openai_codex` | OpenAI Codex（ChatGPT OAuth） | ✗（builtin のみ） |
| `jev_compat` | 反射判断の宛先（System One 形式） | ✗（builtin のみ） |

`*_native` / `nvidia_nim` / `openai_codex` は `llm_clients/` にコード実装が必要なため builtin のみ。UI（モデル管理 > プロバイダ）から作れるのは `openai_compat` / `ollama_compat` の2種。

### `jev_compat` — 反射判断が話す宛先

会話には使わない（`llm_clients/factory.py` は扱わないので、標準モデルや軽量モデルに割り当てるとクライアント生成で失敗する）。話し相手は [`saiverse/reflex_judgment.py`](../intent/reflex_judgment.md) だけで、「状況 + 型付きの質問」を送って確率・選択・数値を受け取る。

「Jev 互換」を名乗る提供元は宛先の path・応答の欄の名前・対応する質問の型が揃っていないので、**その差は provider 設定の `reflex_judgment` 欄で宣言する**（コードに提供元ごとの分岐を置かない）。宣言できる項目:

| 項目 | 既定 | 意味 |
|---|---|---|
| `request_shape` | `system_one` | リクエストの組み立て方。`system_one` は System One の形（`state` + qid の辞書の `questions`）をそのまま送る。`openai_decisions` は OpenAI の Decisions API の形へ送る直前に変換し、答えも読み戻す（下の節）。ほかの値は設定ミスとして、その宛先は「使えなかった」になる |
| `path` | `/v1/systemone` | `base_url` の後ろに付ける宛先のパス |
| `answers_key` | `answers` | 応答のどの欄に答えが載るか（`system_one` は qid の辞書、`openai_decisions` は name 付きの配列） |
| `usage_key` | `usage` | 応答のどの欄に使用量が載るか |
| `answer_fields` | `{"noul": "noul", "choice": "choice", "score": "score"}` | 質問の型ごとに、答えの値が載る欄の名前 |
| `usage_fields` | `{"input_tokens": "input_tokens", "output_tokens": "output_tokens"}` | 記帳する使用量の名前 → 応答の欄の名前 |
| `supported_types` | 3 型すべて | この宛先が答えられる質問の型。対応しない型が混ざった質問は、ひとまとまりごと「使えなかった」になる |

モデル側は `provider_ref` でこれを受け継ぐので、同梱の 4 つのモデル定義（`jev-latest` / `openrouter-jev-latest` / `localjev` / `openai-decisions-gpt-6-luna`）は宣言を持たない（モデル側に書いた場合は、書いたキーだけが provider の宣言を上書きする）。使用量と費用はモデル設定キー名義で既存の記帳に載るので、単価は普通のモデルと同じく `pricing` に書く。

#### `request_shape: "openai_decisions"` — OpenAI Decisions への変換

OpenAI の [Decisions API](https://developers.openai.com/api/docs/guides/decisions)（public beta）は発想は System One と同じだが、組み立て方が違う。呼び出し側が組む質問と状況は System One の形のまま変えず、`saiverse/reflex_judgment.py` が送る直前に変換する。

- **状況** は 1 本の文字列の `input` にする（文字列ならそのまま、それ以外は JSON）。
- **質問** は qid を `name` にした配列にする。型名は noul → `predicate`、choice → `choice`、score → `score`。
- **criteria** は Decisions に欄が無いので、構造の中に居場所があるものはそこへ写す。choice の選択肢の説明は `choices` の `description` へ（選択肢の値は `options` があればその順、無ければ criteria の辞書のキーの順。説明が無い・null のときは値と同じ文字列）、score の段階の説明は `levels` へ（2 つ以上の段階の配列が要る）。居場所の無い noul の基準（`true` / `false`）だけを、`input` の最後に「質問名ごとの基準」の節として書き足す。
- **答え** は `name` 付きの配列で返るので、qid の辞書へ戻し、型名を SAIVerse 側へ写してから同じ検算に通す。`type: "refusal"`（答えられなかった）はその質問の答えが無いものとして扱い、同じ `name` の重複や形の壊れた要素は不正応答にする。

選択肢を作れない choice や段階が 2 つ未満の score は、この宛先では表せないので「使えなかった」になる。同梱の `openai-decisions-gpt-6-luna`（API 上のモデル名は `gpt-6-luna`、単価は入力 $0.10/1M・出力は課金なし）は会話用の `gpt-6-luna` とは別のモデル定義で、protocol が `jev_compat` なので会話の選択欄には出ない。

`localjev` の既定の宛先は `http://127.0.0.1:8080` (localjev 本体の既定 `LOCALJEV_HOST` / `LOCALJEV_PORT` に合わせた値。llama.cpp Server の既定ポートと同じなので、両方をローカルで動かすならどちらかのポートをずらす)。別のポートで動かしているなら、`~/.saiverse/user_data/providers/localjev.json` に同じ id で `base_url` を書いた上書きを置く。

## 追加のしかた

- **UI**: グローバル設定 > モデル管理 > プロバイダタブ →「新規追加」→ protocol 選択 → base_url / api_key_env 入力 → 接続テスト
- **手動**: `~/.saiverse/user_data/providers/*.json` に配置（3層優先で builtin を上書き）
- 反映: 手動で置いたファイルは、`POST /api/providers/reload` を呼ぶか再起動すると読み込まれる。画面からの保存・削除と `POST /api/providers/reload` は、その場で全ペルソナの話すモデルを決め直し、作ってあった接続を捨てる。そのため、すでにそのプロバイダで話したペルソナも、再起動しなくても次の返事から新しい設定で接続する。書いている途中の返事は、始めたときの接続で最後まで書く（[persona_model_selection.md](../intent/persona_model_selection.md) の決まったこと 11）。画面から保存・削除したときに新しい設定へ切り替えられなかったペルソナがいれば、その場で名前つきで知らせる

3層優先順位: `~/.saiverse/user_data/providers/` > `expansion_data/<addon>/providers/` > `builtin_data/providers/`

同梱プロバイダも UI の「上書き編集」から変更できる。同梱ファイル自体は書き換わらず、同じ id の JSON が `user_data` に作られて次回ロードから優先される。元に戻したいときは UI の「削除」でその上書きを消す。**上書きに書けるのは編集画面にある項目だけ**で、`default_headers` のような項目は同梱の値がそのまま引き継がれる。それを消したいときはファイルを直接置く（例: [`docs/api-keys/openrouter.md`](../api-keys/openrouter.md) のアプリ名申告の停止手順）。

## API キー名の縛りは「誰が宣言したか」で変わる

`api_key_env` に書ける環境変数名には制限があるが、**その制限は自分で置いた設定には掛からない**。

| 設定の置き場所 | `api_key_env` に書ける名前 |
|---|---|
| `builtin_data/` | 制限なし（SAIVerse が同梱するもの） |
| `~/.saiverse/user_data/` | 制限なし（本人が置いたもの） |
| `expansion_data/<addon>/` | 自分専用の名前のみ。鍵を使わないなら `api_key_required: false` と明記する（**無記入は不可**） |

同じ規則が**プロバイダ設定とモデル設定の両方**に掛かる。専用の名前は、プロバイダなら `SAIVERSE_PROVIDER_<プロバイダID大文字>_API_KEY`、接続先を直書きするモデルなら `SAIVERSE_MODEL_<設定キー大文字>_API_KEY`。モデルが `provider_ref` で参照する形なら、参照先プロバイダの宣言をそのまま継ぐので何も書かなくてよい。

分かれ目は「誰が書いたか」。`builtin_data` は SAIVerse が同梱するもの、`user_data` は本人が UI か手書きで置くもの。どちらも書いた本人が承知の上で選んだ組み合わせなので、そのまま通す。同梱プロバイダを上書きして `OPENROUTER_API_KEY` のような同梱の鍵名を使い続けることもできる。

縛る相手はアドオンのほう。アドオンは同梱と同じ id のプロバイダ JSON を置けて3層優先で同梱を押しのけられるため、そこで同梱の鍵名を名乗られると利用者のキーが任意の宛先へ送られてしまう。だからアドオン由来の定義は自分専用の変数名しか使えない。

**書かない、も許されない。** `api_key_env` を空にすると OpenAI 互換クライアントは既定で `OPENAI_API_KEY` を使うため（`llm_clients/openai.py`）、無記入は「利用者の OpenAI キーをこの宛先へ送れ」と書いたのと同じになる。だからアドオン層の定義は、自分専用の変数名を書くか、`api_key_required: false` と明記して鍵が要らないことを宣言するかの、どちらかを必ず選ばなければならない。

判定に使う層は、`saiverse/provider_configs.py: load_configs()` が**実際に辿ったディレクトリ**をそのまま `source` として刻む。あとからパスを解決し直して判定はしない（`expansion_data` に置いたシンボリックリンクやジャンクションが `user_data` を指していると、解決先の層で信用してしまうため）。**JSON の中に `"source"` や `"builtin"` を書いても読み込み時に捨てられる**ので、定義が自分で層を名乗ることもできない。検査は `saiverse/provider_security.py: validate_provider_config` の一箇所にあり、保存時とクライアント構築時（＝毎回の LLM 呼び出し）の両方が通る。

**この仕組みが縛るのはアドオンの「宣言」であって、アドオンの「動作」ではない。** アドオンのツールは同一プロセスで Python として実行される（`tools/__init__.py` が `exec_module` で読み込む）ので、アドオンのコードは環境変数を直接読むことも、独自に通信することも、`user_data` に書き込むこともできる。ここはアドオンを隔離する仕組みではない。設計の経緯は `docs/intent/model_provider_management.md` の不変条件 11。


## 壊れた設定ファイル

上位層の provider JSON が壊れている場合、下位層へ自動で切り替えない。接続先・キー・モデルはそのまま利用停止し、API とプロバイダ／モデル管理画面にファイルのパス・層・理由を表示する（値は表示しない）。参照モデルも一覧に残る。ファイルを修復したあと、`POST /api/providers/reload` または再起動で読み直す。

`GET /api/providers` は各行の `available`（設定の構造が有効か）と `config_error`（無ければ null）を返す。API キーの有無は従来の `api_key_configured` で別途確認する。モデルの2本の一覧 API も `available` と `config_error` を返し、壊れた provider を参照するモデルを黙って隠さない。

`protocol` は実装が扱える値を必須とする。会話用 factory が扱える7種に加え、反射判断専用の `jev_compat` を受け付ける。`api_key_env_alternates` は省略か、非空文字列だけのリスト。`base_url` / `api_key_env` の null は従来のプロトコル既定動作を保ち、認証なしの宣言は `api_key_required: false` が担う。
