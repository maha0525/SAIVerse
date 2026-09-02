# Intent Document: 依存関係の管理 (lock ファイルと「上げる / 止める」の裁定簿)

**ステータス**: 完了 (2026-09-03。実装・レビュー 3 巡・lock だけの venv でのフルスイート・本番 venv の同期と再起動 (まはー、エリスと会話・声あり)・OpenAI / Anthropic / xAI の実 API 一回ずつ (隔離環境、下記 §5) まで済。残るのは develop へのマージとリリース発行、それと報告者の macOS で LLM 呼び出しが通ることの確認 (リリース後))

## 概要

SAIVerse が使う Python の部品 (ライブラリ) について、**「意図した範囲」と「実際に検証した組み合わせ」を別のファイルで持ち、セットアップと更新は後者から入れる**ようにする。あわせて、いま古いまま止まっている部品を「上げる / 理由を書いて止める」に仕分け、止めるものには必ず理由を残す。

芯を専門用語なしで言うと: **「動くと確かめた部品の版の一覧」を配布物に含め、ユーザーの手元にはその一覧どおりに入れる。** いまは「この版以上なら何でも」という指定が混ざっていて、入れる日によって違う組み合わせが入る。

---

## 1. なぜ必要か (解決する問題)

### 1-1. 起きた実害 (2026-09-02)

ペルソナの声が 9/1 04:18 から出なくなっていた。原因は、更新経路の `pip install -r requirements.txt` が NumPy を 2.5.2 へ上げたこと。requirements.txt の NumPy は `>=1.26.0` (下限だけ) なので pip はこれを合法と見なす。一方、音声合成アドオン (voice-tts) が使う numba 0.65 は NumPy 2.4 までしか受け付けず、以後の全発話が「部品を読み込めない」で無音になった。backend.log には表層のエラーしか出ず、根因は `voice_tts_worker.log` にしか写らなかったので、丸一日以上気づかれなかった。

### 1-2. 構造の問題 (実害の供給源)

1. **requirements.txt が二種類の書き方を混ぜている。** `fastapi==0.116.1` のような「この版ちょうど」と、`anthropic>=0.42.0` のような「下限だけ」。前者は何年でも動かず (openai は 1.97 で最新 3.7、メジャー二段遅れ)、後者は pip の都合で動く。どちらも「決めていない」の別の顔。
2. **lock ファイルが無い。** 開発機とユーザーの手元で入っている版が違いうるし、`update.bat` を叩いた日によっても違う。棚卸しをしても翌月には崩れる。
3. **アドオンの部品が本体と同じ venv に入り、互いを動かせる。** 本体の pip install がアドオンの部品 (numba) を壊した。逆 (アドオンが本体の部品を動かす) も起きうる。アドオンの requirements は `numpy>=1.24` で numba の上限を知らない。
4. **Python の推奨版が現実とずれている。** README は 3.12.10 推奨、開発機は 3.13.2。まはー裁定 (2026-09-02): **3.13 を推奨に上げる** (開発機で見ている版を推奨にする)。

---

## 2. 全体像 (末端から末端まで)

### 2-1. 誰が何に依存できなければならないか

- **ユーザー**: `setup.bat` / `setup.sh` / `update.bat` を叩けば、開発機で検証したのと同じ部品の組み合わせが入る。入れる日によって違うものが入らない。
- **アドオンの利用者**: アドオンを入れても本体が壊れない。本体を更新してもアドオンが黙って壊れない (壊れるなら入れる時点で失敗して理由が出る)。
- **開発者 (まはー・私)**: 部品を上げるときは lock を作り直してテストし、その結果がそのまま配布物になる。「止めている部品」には理由が書いてあり、読めば外してよい条件が分かる。

### 2-2. 部品の版が決まる経路 (現状)

| 経路 | 何を読むか | 誰が叩くか |
|---|---|---|
| `setup.bat` / `setup.sh` | `pip install -r requirements.txt` | 新規ユーザー |
| `scripts/update_engine.py update_dependencies` | 同上 | `update.bat` / `start.bat` の更新続行 |
| `scripts/update_engine.py completion_fingerprint` | requirements.txt の sha256 | 更新完了マーカーの照合 |
| `scripts/update_engine.py missing_dependencies` | requirements.txt を読んで不足を判定 | 起動時の「更新が要るか」判定 |
| `saiverse/addon_installer.py` (`pip_install` step) | アドオンの requirements | アドオン導入 |
| `.github/workflows/discord_gateway.yml` | `discord_gateway/requirements-dev.txt` (中で `-r ../requirements.txt`) | CI (Python 3.11) |
| `docs/getting-started/installation.md` | 手動手順に `pip install -r requirements.txt` | 手で入れる人 |

lock を導入するなら、この **7 箇所すべて**が lock を読むように揃える。一箇所でも requirements.txt を直接読む経路が残ると、その経路だけ違う組み合わせが入る (入口の検査ではなく境界の保証として、読む側を数え切る)。

### 2-3. 変更後の形

```
requirements.txt      ← 意図の範囲 (人が書く。下限と、理由つきの上限)
requirements.lock     ← 全部品の版を固定した一覧 (機械が作る。人は編集しない)
```

- **requirements.txt** は「本体が直接 import する部品」だけを、**下限 + 理由つきの上限**で書く。`==` は使わない (釘を打つのは lock の仕事)。上限を書くときは必ず一行の理由を添える (mcp<2 の書き方が手本)。
- **requirements.lock** は `requirements.txt` から機械生成する。全部品 (間接依存も含む) が `==` で並び、プラットフォーム差 (Windows / macOS / Linux、Python 3.11〜3.13) は環境マーカーで一枚に収める。ユーザー側は素の pip で読める形式 (`pip install -r requirements.lock`) に限る — ユーザーに新しい道具を入れさせない。
- 生成の道具は開発者だけが使う。`uv pip compile --universal` を第一候補とする (開発機に導入済み、pip 互換の出力、プラットフォーム横断の一枚を作れる)。ユーザーの手元では uv は不要。
- **アドオンの pip install には lock を constraints として渡す** (`pip install -r <addon>/requirements.txt -c requirements.lock`)。アドオンは本体が固定した部品を動かせなくなり、動かす必要があるなら導入時に失敗して理由が出る (黙って壊れる代わりに)。

### 2-4. 不変条件と持ち主

| 不変条件 | 持ち主 (真実の源) |
|---|---|
| ユーザーの手元に入る部品の版は lock と一致する | `requirements.lock` |
| lock は requirements.txt の範囲の中にある | 生成の道具 (compile が保証) |
| 止めている部品には理由がある | `requirements.txt` のコメント行 |
| アドオンは本体の部品を動かせない | `addon_installer.py` が constraints を渡す |
| 更新完了マーカーは lock の内容と結びつく | `update_engine.completion_fingerprint` (lock の sha256 を含める) |

### 2-5. 移行

- 既存ユーザー: 次の `update.bat` で lock どおりに入れ直される。上げる部品 (§3) があるので、入れ替えは起きる。requirements.txt の sha が変わるので更新完了マーカーは自然に「要更新」になる。
- 開発機: 同じ。ただし voice-tts などアドオンの部品は lock の外にあるので、constraints と衝突するものがあれば導入し直しで判明する。
- lock の無い版 (v0.3.3 以前) から最初の lock 版へ上げる途中で pip / npm が失敗したとき: 巻き戻しは古い版に `git reset` したあと、その版に lock が無いので、その版自身の `requirements.txt` から入れ直す (`update_engine._rollback_code_and_dependencies` だけがこの経路を選び、WARNING で記録する。通常の更新経路は lock が無ければそのまま失敗する)。

---

## 3. 部品の仕分け (2026-09-02 時点の現物)

### 3-1. 上げる (メジャーが遅れている、機能が入る場所)

| 部品 | 現在 | 最新 | 注意 |
|---|---|---|---|
| openai | 1.97.0 | 3.7.0 | **完了 (2026-09-02、1.97.0 → 3.7.0)**。SAIVerse のコード変更はゼロ — `llm_clients/openai.py` が SDK に渡すのは文字列と数値だけ (timeout は `float`、`llm_clients/factory.py:250-252`) で、httpx 型の物を渡している箇所が無かった。変えたのはテストの SDK 境界 (`tests/test_llm_clients.py` の `_wire_headers_for` が実 `OpenAI` に注入する `http_client` / `Response` を httpx2 に) と、構築経路の型を固定する 2 テストの追加 (§3-4)。Codex 経路 (`openai_codex*.py`) は SDK を使っておらず無関係 |
| anthropic | 0.79.0 | 1.3.0 | **完了 (2026-09-02、0.79.0 → 1.3.0)**。コード変更は 2 箇所: `llm_clients/anthropic.py` の Timeout を `httpx2.Timeout` に、`llm_clients/anthropic_request_builder.py` の temperature / top_p / top_k を `extra_body` へ (§3-4)。httpx2 は直接依存として requirements.txt に載せた |
| google-genai | 1.75.0 | 2.21.0 | **完了 (2026-09-02、1.75.0 → 2.21.0)**。上限 `<2.0` は外した (§3-4)。SAIVerse が使う `types.*` / caches API / 私的 `_api_client.HttpResponse` (SSE パッチ) はすべて無変更。コード変更は AFC 明示無効化の 3 箇所だけ (§3-4) |
| xai-sdk | 1.7.0 | 1.19.0 | **完了 (2026-09-02、1.7.0 → 1.19.0)**。1.8〜1.19 の破壊的変更は Files / Collections API だけで、SAIVerse が触る `xai_sdk.Client(api_key, timeout)` / `client.chat.create(model, tools, response_format, reasoning_effort, store_messages)` / `chat.append` / `chat.sample()` / `chat.stream()` / `chat.parse(Model)` / `xai_sdk.chat.{tool,assistant,image,system,user}` と、Response の `content` / `reasoning_content` / `tool_calls` / `usage` / `finish_reason` は 1.19.0 の実物 (inspect.signature と dir) で全部健在。コード変更ゼロ。同じ回で requests 2.32.4 → 2.34.2、python-dotenv 1.1.1 → 1.2.3 も下限化して上げた (どちらもパッチ相当、対象テスト緑、警告なし) |
| fastapi / starlette / uvicorn | 0.116.1 / 0.47.3 / 0.35.0 | 0.141.1 / 1.6.0 / 0.52.4 | starlette がメジャー。三つ一緒に上げる |
| pydantic / pydantic-settings | 2.11.7 / 2.5.2 | 2.13.5 / 2.15.0 | マイナー。Web 一族と同じ回で |
| langgraph 一族 | 1.0.3 | 1.2.11 | **完了 (2026-09-02、1.0.3 → 1.2.11。langgraph-checkpoint 3.0.1 → 4.2.0、langgraph-prebuilt 1.0.7 → 1.1.0、langgraph-sdk 0.2.15 → 0.4.4、langchain-core 1.2.12 → 1.6.1、langsmith 0.7.1 → 0.12.1、langsmith が新たに `langchain-protocol` 0.0.19 を連れてきた)**。1.1 で `invoke()` / `ainvoke()` に `version="v2"` が入り、そちらは dict ではなく `GraphOutput` を返す (dict 風アクセスは `LangGraphDeprecatedSinceV11` 警告)。SAIVerse の `sea/langgraph_runner.py` は `version` を渡さないので既定の `"v1"` のまま素の dict が返り、`sea/runtime_graph.py` が結果に対して行う `isinstance(final_state, dict)` 判定 (output_schema の親への書き戻しと PulseContext の flush の入口) は成立し続ける。SEA の既存テストは全部 `compile_playbook` を偽物に差し替えていて実物の langgraph を通っていなかったので、実物で compile → ainvoke して「返るのは素の dict で、langgraph の非推奨警告が出ない」を固定するテストを `tests/sea/test_langgraph_runner_boundary.py` に足した。コード変更ゼロ |
| SQLAlchemy | 2.0.41 | 2.0.52 | **完了 (2026-09-02、2.0.41 → 2.0.52)**。パッチ。DB / migrate / admin 系の対象テストで `SAWarning` / `MovedIn20Warning` なし。コード変更ゼロ |

### 3-2. 理由を書いて止める

| 部品 | 上限 | 理由 (requirements.txt に残す文) |
|---|---|---|
| mcp | `<2` | 2.x が `streamablehttp_client` を撤去。v2 API への移行は別案件 (`docs/intent/mcp_protocol_coverage.md`)。**既に書いてある、手本** |
| onnxruntime | `<1.24` | 1.24 以降は macOS x86_64 (Intel Mac) と macOS 13 の wheel が無く sdist も無い。universal lock が 1.24+ を掴むと Intel Mac では `pip install -r requirements.lock` 自体が失敗する (2026-09-02 Codex レビューで発覚。fastembed 0.8 も Python 3.13 で 1.24.0/1.24.1 を除外)。外す条件 = onnxruntime が Intel Mac の wheel を再開する、または Intel Mac 対応を打ち切ると決める |

**universal lock の盲点 (2026-09-02 に学んだこと)**: `uv pip compile --universal` は依存関係のメタデータだけで解決し、その版の wheel が各プラットフォーム向けに公開されているかは見ない。Windows で入って全スイートが緑でも、Intel Mac で入らない lock はできる。だから lock を作り直したら `python scripts/check_lock_platforms.py` (各 pin の wheel / sdist の有無を PyPI に問い、対象 4 プラットフォーム × Python 3.11〜3.13 で入らない組合せを exit 1 で返す) を回す。判定は pip と同じ `packaging.tags` で行い、macOS の床は x86_64 が 13 (対応する Intel Mac の最低 OS 版)、arm64 が 14、Linux の床は glibc 2.31 (Ubuntu 20.04 LTS / Debian 11 = 対応すると言う最古の Linux。これより新しい glibc を要求する wheel しか無い pin は Linux では「無い」と数える)。Requires-Python は PyPI が wheel / sdist ごとに持つのでファイル単位で見て、その Python を除外するファイルは候補に数えない (どのファイルも許さなければ FAIL)。yanked (取り下げ) されたファイルも候補に数えず、全ファイルが yanked の pin は FAIL。PyPI の照会に失敗した pin は「入る」と数えず FAIL にする (fail-closed。exit 0 は全 pin を取得して判定できたときだけ)。§5 の 1b。

他に止めるべきものは実装時の compile と全スイートで判明する。**「なんとなく上げない」は禁止** — 上げないなら理由を書く。

### 3-3. 本体の責任ではない (アドオンの部品)

torch / transformers / librosa / numba / gradio / funasr など、venv にある 269 個のうち本体の直接依存 (約 30 個) を除く大半は voice-tts が連れてきたもの。これらは本体の requirements には入っていない (現状で正しい)。本体がやることは **constraints で「本体の部品を動かすな」と言う**ことまで。numba の上限などアドオン自身の部品の整合はアドオン側の requirements の責任 (voice-tts に numba の固定を足すのはアドオンのリポジトリの宿題)。

ただし本体の更新は、壊したことを**その場で見せる**義務は負う。`update_engine.update_dependencies` は lock を入れた直後に `pip check` を回し、lock の外にあるパッケージ (アドオンか手で入れたもの) との衝突を `[deps] pip check: ...` の WARNING として更新ログに残す (2026-09-02 の voice-tts の実害は、翌日まで誰も気づかなかったことが問題だった)。衝突と読むのは `pip check` の exit 1 だけで、それ以外の非ゼロ (2 = pip 自身の使い方の誤りや内部エラー、負数 = シグナル) は「pip check が走らず、衝突は検査できなかった」として stderr と一緒に別の WARNING で残す (pip の落ち方をアドオンの衝突に見せない)。可視化だけで、更新は失敗にしないし巻き戻しもしない — 直すのはアドオン側。

### 3-4. SDK を上げる前に分かっていること (2026-09-02 の下調べ)

- **HTTP の部品が世代交代している。** `httpx` は保守が止まり、Pydantic チームの互換フォーク **`httpx2`** に移った。anthropic 1.0 (2026-08-20) と openai 3.0 (2026-08-12) はどちらも「httpx2 へ移行」が唯一の破壊的変更で、`httpx` はもう自動では入らない。google-genai 2.x と mcp 1.x はまだ `httpx` (<1.0) を使う。**二つは別パッケージとして共存できる**ので、当面は両方が venv に入る。
- SAIVerse 自身が `httpx` を直接使う箇所は 10 ファイル。SDK に渡していたのは `llm_clients/anthropic.py` の `httpx.Timeout(...)` だけで、**2026-09-02 に `httpx2.Timeout` へ切り替えた** (httpx2 は requirements.txt の直接依存に載せた)。下調べでは「旧 httpx の型を渡すと構築時に `TypeError`」と書いていたが、1.3.0 の実物で確かめると **`Timeout` は構築時に拒否されない** — 受け取られたうえで、transport に渡る connect / read / write / pool の各値が数値ではなく Timeout オブジェクトそのものになる (黙って壊れる型)。構築時に `TypeError` になるのは `http_client=` に旧 `httpx.Client` を渡したときだけ。だから `tests/test_llm_clients.py` で「構築されたクライアントの timeout が `httpx2.Timeout`」を実構築経路で固定した。`llm_clients/gemini.py` の `isinstance(err, httpx.ReadTimeout ...)` は google-genai が httpx のままなので変えない。方針: **自分のコードは SDK が使う方の型に合わせる** (anthropic / openai 向けは `httpx2`、genai 向けは `httpx`)。プロセス全体で `import httpx` を httpx2 に差し替える `httpx2.alias_httpx()` は使わない — 起動順に依存し、mcp / genai が期待する型を黙って変える。
- **httpx2 は certifi を同梱しない。** `SSL_CERT_FILE` が立っていればその束、無ければ truststore 経由で OS のストアを信頼元にする (httpx2 2.12 の `_config.create_ssl_context` で確認)。`saiverse/tls_trust.py` が起動時 (`main.py` の import 時、`SAIVerseManager` より前) に `SSL_CERT_FILE` を立てるので、Python が OS のストアを読めない macOS 環境では anthropic 1.x の呼び出しも certifi で検証される。docstring は 2026-09-02 に現状へ合わせた。残る確認: **macOS の実機で anthropic 経由の LLM 呼び出しが通ること**を報告者に確認してもらう (truststore 単体でキーチェーンを読めるかは未確認だが、読めるなら環境変数なしでも通る方向の差)。
- **google-genai 2.0** の破壊的変更は Interactions API だけで、`GenerateContent` は無変更 (公式 CHANGELOG)。SAIVerse は Interactions API を使っていない (`llm_clients/` / `saiverse/llm_router.py` / `persona/emotion_module.py` / `tools/adapters/gemini.py` に参照なし) ので、**上限 `<2.0` は 2026-09-02 に外した** (`google-genai>=2.21.0`)。2.21 への実際の差分で SAIVerse に効いたのは一つだけ: 2.18 から `generate_content` は `automatic_function_calling.disable=True` を明示しない限り「Direct use of AFC ... is not recommended」を WARNING でプロセスごとに一度ログする (tools を渡していなくても)。`llm_clients/gemini.py` は元から無効化していたが、`saiverse/llm_router.py` / `persona/emotion_module.py` / `builtin_data/tools/image_generator.py` の 3 箇所は未指定だったので同じ規則 (SAIVerse は SDK の AFC を使わない) に揃えた。挙動は変わらない (tools が無ければ AFC ループは 1 回で抜ける)。google-genai の HTTP 部品は 2.21 でも `httpx` (<1.0) のまま。
- **anthropic 1.0** は httpx2 の他に、`messages.create()` / `messages.stream()` から `temperature` / `top_p` / `top_k` を撤去 (渡すと `TypeError`。古いモデル向けに要るなら `extra_body`)、`output_format=` にスキーマ dict を渡す形を撤去、`.with_raw_response` の戻りが新クラスに。SAIVerse 側で該当したのは `llm_clients/anthropic_request_builder.py` の `temperature` / `top_p` / `top_k` だけで、**2026-09-02 に `request_params["extra_body"]` へ移した** (SDK が JSON の最上位に合流させるので、API に届く形は以前と同じ。`max_tokens` は今も正規の引数)。`tests/llm_clients/test_anthropic_request_builder.py` で、extra_body に入ること・最上位に残らないこと・その dict が 1.3 の `messages.create()` に `TypeError` なしで束縛され JSON の最上位に届くこと (httpx2 の MockTransport で通線) を固定した。`with_raw_response` / `output_format=` / Text Completions / `HUMAN_PROMPT` / `anthropic.Transport` / `BetaBase64PDFBlockParam` は `llm_clients/anthropic*.py` に無し (grep で確認)。`anthropic_retry_policy.py` は SDK の例外クラス (`APITimeoutError` / `APIConnectionError` / `APIStatusError`) だけを見ていて httpx の型に触れていないので無変更。
- **openai 2.0** は Responses API の function call output の型が広がっただけ (SAIVerse は `chat.completions` しか使わない)、**3.0** は httpx2 のみ。**2026-09-02 に 1.97.0 → 3.7.0 へ上げた。** lock は `--upgrade-package openai` で openai と jiter (0.13 → 0.16) だけ動き、openai 3.x は distro / tqdm / httpx を連れて来なくなった (distro は google-genai、tqdm は fastembed / huggingface_hub、httpx は google-genai / mcp / langgraph-sdk / langsmith と SAIVerse 自身の直接依存として残る)。3.7.0 の実物で確かめたこと: `OpenAI(...)` に SAIVerse が渡す `api_key` / `base_url` / `timeout` (float) / `default_headers` はすべて健在で、`chat.completions.create()` に積む `n` / `tools` / `tool_choice` / `response_format` / `stream` / `stream_options` / `extra_body` / `extra_headers` と `OPENAI_ALLOWED_REQUEST_PARAMS` の 15 個は全部まだ正規の引数 (撤去なし、`extra_body` への移送は不要)。例外クラス (`RateLimitError` / `APIStatusError` / `APITimeoutError` / `APIConnectionError` / `AuthenticationError` / `BadRequestError`) と `openai_errors.py` が読む `.status_code` / `.body` も無変更で、`.response` の型が `httpx2.Response` になっただけ (SAIVerse は httpx の型を `isinstance` していないので影響なし)。streaming の chunk 型 (`choices[0].delta.content` / `.tool_calls[].function.name|arguments` / `finish_reason` / 末尾 chunk の `usage.prompt_tokens_details.cached_tokens`) も 1.x と同形。`builtin_data/tools/image_generator.py` の `images.generate` / `images.edit` (image に file の list) も引数健在。anthropic と違い、openai 3.7.0 は `http_client=` に旧 `httpx.Client` を渡しても構築時に拒否せず MockTransport の往復まで通ってしまう (duck typing) — 型注釈は `httpx2.Client` なので、テストは SDK と同じ側に寄せた。float でない `timeout` に旧 `httpx.Timeout` を渡すと anthropic と同じく黙って壊れる (httpx2 の Timeout が Timeout を包んで各段が数値でなくなる) が、SAIVerse は float しか渡さないので該当なし。`tests/test_llm_clients.py` に、factory 経由で構築した実クライアントの `_client` が `httpx2.Client` で timeout が届いていることを固定する 2 テストを足した。SDK 由来の警告は対象テストで 0 件 (3.7 の `DeprecationWarning` は生 bytes を `body=` に渡す低レベル経路だけで、SAIVerse は使っていない)。
- lock の環境マーカーの癖: `hf-xet` の行のマーカーが `platform_machine == 'amd64'` (小文字) で書かれており、Windows (`AMD64`) では偽になって入らない。huggingface_hub は無くても通常のダウンロードに退避するので実害はないが、上流のマーカーをそのまま写した結果であることを記録しておく。

### 3-5. ついでに片付けるもの

- `google-adk 1.5.0` が開発機の venv に孤児で残っている (`docs/issues/archive/starlette_google_adk_version_conflict.md`)。誰も使っていないので uninstall して issue を閉じる。**衝突自体は解消済み (2026-09-02)**: Web 一族の更新で starlette が 1.6.0 になり、google-adk が要求する `>=0.46.2` を満たす。開発機での uninstall は venv を lock に合わせ直すときに行い、それが済んだら issue を archive へ。
- README / installation.md / tailscale-runbook の Python 推奨を 3.13 へ (3.11〜3.13 動作の記述は維持。3.14 は `docs/issues/python_314_support_verification.md` の実機検証待ちのまま)。**済 (2026-09-02)**: README.md と `docs/getting-started/installation.md` のリンク先を 3.13.15 (この日の 3.13 系の最新で、Windows 用インストーラがある版) に、`docs/developer-guide/contributing.md` の「3.12 推奨」も 3.13 に。tailscale-runbook は「3.11-3.13」と幅しか書いておらず推奨版を名指ししていないので触っていない。
- requirements.txt の `==` は 2026-09-02 に全廃した (最後まで残っていた langgraph / python-dotenv / requests / SQLAlchemy を下限化)。以後この file に `==` が現れたら、それは lock に書くべきものが漏れた印。
- `discord_gateway.yml` の CI は Python 3.11 で回している。lock が 3.11〜3.13 を環境マーカーで覆うので、そのままでよい。

---

## 4. 変更の置き場所

- **lock の導入と 7 経路の切り替え** (§2-2): `requirements.txt` / `requirements.lock` (新規) / `setup.bat` / `setup.sh` / `scripts/update_engine.py` / `saiverse/addon_installer.py` / `.github/workflows/discord_gateway.yml` / `docs/getting-started/installation.md`。setup と update は同期義務の組 (CLAUDE.md「Setup/Update Script Parity」) なので同じコミットで。
- **SDK の更新**: `llm_clients/` の各クライアント。**一族ごとに別コミット** (openai 一族 / anthropic / google-genai / Web 一族)。壊れたときに切り分けられるように。
- **Python 推奨の更新**: docs 3 箇所 + README。

置かない場所: 個々のクライアントに「版が古ければこう振る舞う」の分岐を足さない。版は lock で一つに決まるので、分岐は不要 (env flag や旧経路を残さない規則と同じ)。

---

## 5. 検証 (境界を跨ぐ順に)

1. **compile が通る** — requirements.txt から lock が生成でき、pip check で衝突ゼロ。
   1b. **全プラットフォームで入る** — `python scripts/check_lock_platforms.py` が exit 0 (Windows / Linux / macOS arm64 / macOS x86_64 × Python 3.11〜3.13 の全組合せに wheel か sdist があり、Requires-Python にも除外されず、全 pin を PyPI から取得できた。wheel の適合は `packaging.tags` で判定し、macOS の床は x86_64=13 / arm64=14)。照会に失敗した pin があれば exit 1 (fail-closed)。開発機では確かめられない環境の代理。
2. **素の venv に lock だけで入る** — 開発機の venv ではなく、新しい venv に `pip install -r requirements.lock` して全スイートが緑。これが「ユーザーの手元」の代理。
3. **update 経路** — 隔離環境 (`docs/test_environment.md`) で `update_engine.py --manual` を通し、完了マーカーが lock の sha を持つこと。
4. **アドオン導入** — 隔離環境で voice-tts を `pip_install` step から入れ、constraints が効くこと (本体の部品を動かそうとしたら失敗すること、を一つ作為的に確かめる)。**pip の機構としては 2026-09-02 に確認済み**: lock venv で `pip install --dry-run -r <アドオン風の requirements> -c requirements.lock` を実行すると、マーカー付き行と `# via` コメントを含む lock をそのまま constraints として受け付け、`numpy<2` のような本体の pin と矛盾する要求は `ResolutionImpossible` で止まる。`addon_installer` が `-c` を渡す配線はテストで固定済み。voice-tts の実導入は本番 venv の同期のときに。
5. **SDK の実 API 一回ずつ** — openai / anthropic / gemini / xai を各一回、隔離環境から呼ぶ。**課金が出るので、この段の直前にまはーの承認を取る** (対象・回数・見込み額を示して)。**済 (2026-09-03、まはー承認)**: ペルソナは使わず SAIVerse のクライアントクラス (`get_llm_client` → `generate` / `generate_stream`) を隔離環境 (`SAIVERSE_HOME=test_data/.saiverse`) から直接叩いた。gpt-5.4-nano (generate 4.7s / stream 1.3s)、claude-haiku-4-5 (1.3s / 0.8s — extra_body 経路と messages.stream の両方)、grok-4-1-fast-reasoning (2.8s)、全部 'pong' が返った。Gemini はまはー本人のエリスとの会話 (本番、gemini-3.7-flash-paid、キャッシュ有効) で確認済み。
6. **本番の再起動** — まはーの手で。会話一往復、声、記憶タブ。**済 (2026-09-03 00:11 起動)**: バージョン鎖 0.3.3 で完了、lifespan で addon_events のループ登録 → startup complete、TLS 退避の発火なし、エリスと会話 (LLM 成功・Gemini 明示キャッシュ)、voice-tts は 64 片を enqueue して import 失敗 0・合成失敗 0 (前日まで全滅していた経路)。Traceback は stackchan アドオンの Gemini 関数定義変換の既存 3 件のみ。

「全スイート緑」は 2 と 3 の代理にならない (スイートは開発機の venv で走るため)。

---

## 6. 決定済み (2026-09-02 時点で未決事項はない)

1. **lock を作る道具**: `uv pip compile --universal` で確定 (理由は §2-3。lock の冒頭コメントに再生成の手順を書いてある)。
2. **SDK 更新の順番**: Web 一族 → google-genai → anthropic → openai の順で実施済み。残りの xai-sdk / langgraph / SQLAlchemy / requests / python-dotenv は最後にまとめて一回で上げた (§3-1)。
3. **google-genai の `<2.0`**: 2.x の破壊的変更が Interactions API だけと確認して外した (§3-4)。

---

## 7. 経緯

- 2026-09-02: voice-tts の無音事故 (numba × NumPy 2.5) を契機に起草。まはー「そろそろやらなきゃダメかな。依存関係全体を洗いたい、新しくするべきとこ新しくして、古いまま止めなきゃだめなやつはそう設定する整理が必要」。Python 推奨は 3.13 へ、feature ブランチで対応、と裁定。
- 2026-09-02 深夜 (レビュー 1 巡目): ローカル LLM と Codex (ブランチ全体、develop 基点)。**採用**: ①lock の onnxruntime 1.24.1 は Intel Mac の wheel も sdist も無く、Intel Mac では lock が入らない (Codex high) → `onnxruntime<1.24` を理由つきで置き、`scripts/check_lock_platforms.py` を新設して同族を機械検査 (§3-2)。②packaging 無しの退化パーサーがマーカー付き行を「必要」と読み、macOS で Windows 限定の pin を未導入と判定して起動のたびに更新へ送る (ローカル low) → マーカー付き行は未検査扱いへ。③壊れた dist-info (版が読めない) を満たしている扱いにしていた (Codex medium) → 未検査へ。④requirements.txt の `==` 禁止を契約テストへ (ローカル low)。**採用せず**: lock が読めない・メタデータが列挙できないときに CHECK_READY を返す fail-open (Codex high) — 以前からの設計で、代案の INCONCLUSIVE も起動する点は同じ (印を書かない) なので挙動差が無い。`strict_content_type=False` は CSRF 防御の保留 (Codex high) — 0.116 には検査自体が無かったので退行ではないが、issue の優先度を high に上げてフロントエンドを監査対象に加えた。thinking と sampling の同時送信 (Codex medium) — この分岐は以前から同じ引数を top-level で送っており、`extra_body` への移動で挙動は変わっていない (別件)。
- 2026-09-02 深夜 (レビュー 2〜3 巡目、Codex)。**2 巡目 3 件すべて採用**: `check_lock_platforms.py` が PyPI に問えなかった pin を通していた → fail-closed に / wheel の判定を文字列の部分一致から `packaging.tags` へ (macOS の床 13 / 14) / 本体の更新が同居アドオンの部品を壊しても気づけない → `update_dependencies` の直後に `pip check` を回して衝突を WARNING で更新ログに残す (止めない・戻さない、§3-3)。**3 巡目 4 件すべて採用**: lock の無い旧版 (v0.3.3 以前) からの更新が途中で失敗したとき、巻き戻しが旧版に無い lock を探して失敗する → 巻き戻し経路だけ旧版の requirements.txt を読む (§2-5) / manylinux の列挙を glibc の床 2.31 からの生成へ / Requires-Python と yanked をファイル単位で判定 / `pip check` は exit 1 だけを衝突として読み、他の非 0 は「検査できなかった」と書く。3 巡で採用した指摘の的は「lock の中身 (Intel Mac)」→「新設した検査の精度」→「移行の一回きりの経路」と外周へ移っており、私の見立てでは収束。4 巡目を投げるかはまはーの判断。
