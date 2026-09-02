# Intent Document: 依存関係の管理 (lock ファイルと「上げる / 止める」の裁定簿)

**ステータス**: 実装中 (2026-09-02 起草、同日まはー GO。lock の導入と 7 経路の切り替えは実装済み、次は SDK を一族ごとに上げる §3-1)

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

---

## 3. 部品の仕分け (2026-09-02 時点の現物)

### 3-1. 上げる (メジャーが遅れている、機能が入る場所)

| 部品 | 現在 | 最新 | 注意 |
|---|---|---|---|
| openai | 1.97.0 | 3.7.0 | メジャー二段。`llm_clients/openai*.py` と Codex 経路の読み替えが要る |
| anthropic | 0.79.0 | 1.3.0 | メジャー。`llm_clients/anthropic*.py` |
| google-genai | 1.75.0 | 2.21.0 | **完了 (2026-09-02、1.75.0 → 2.21.0)**。上限 `<2.0` は外した (§3-4)。SAIVerse が使う `types.*` / caches API / 私的 `_api_client.HttpResponse` (SSE パッチ) はすべて無変更。コード変更は AFC 明示無効化の 3 箇所だけ (§3-4) |
| xai-sdk | 1.7.0 | 1.19.0 | マイナー |
| fastapi / starlette / uvicorn | 0.116.1 / 0.47.3 / 0.35.0 | 0.141.1 / 1.6.0 / 0.52.4 | starlette がメジャー。三つ一緒に上げる |
| pydantic / pydantic-settings | 2.11.7 / 2.5.2 | 2.13.5 / 2.15.0 | マイナー。Web 一族と同じ回で |
| langgraph 一族 | 1.0.3 | 1.2.11 | マイナー。SEA の実行基盤なのでスイート全体が検証になる |
| SQLAlchemy | 2.0.41 | 2.0.52 | パッチ |

### 3-2. 理由を書いて止める

| 部品 | 上限 | 理由 (requirements.txt に残す文) |
|---|---|---|
| mcp | `<2` | 2.x が `streamablehttp_client` を撤去。v2 API への移行は別案件 (`docs/intent/mcp_protocol_coverage.md`)。**既に書いてある、手本** |

他に止めるべきものは実装時の compile と全スイートで判明する。**「なんとなく上げない」は禁止** — 上げないなら理由を書く。

### 3-3. 本体の責任ではない (アドオンの部品)

torch / transformers / librosa / numba / gradio / funasr など、venv にある 269 個のうち本体の直接依存 (約 30 個) を除く大半は voice-tts が連れてきたもの。これらは本体の requirements には入っていない (現状で正しい)。本体がやることは **constraints で「本体の部品を動かすな」と言う**ことまで。numba の上限などアドオン自身の部品の整合はアドオン側の requirements の責任 (voice-tts に numba の固定を足すのはアドオンのリポジトリの宿題)。

### 3-4. SDK を上げる前に分かっていること (2026-09-02 の下調べ)

- **HTTP の部品が世代交代している。** `httpx` は保守が止まり、Pydantic チームの互換フォーク **`httpx2`** に移った。anthropic 1.0 (2026-08-20) と openai 3.0 (2026-08-12) はどちらも「httpx2 へ移行」が唯一の破壊的変更で、`httpx` はもう自動では入らない。google-genai 2.x と mcp 1.x はまだ `httpx` (<1.0) を使う。**二つは別パッケージとして共存できる**ので、当面は両方が venv に入る。
- SAIVerse 自身が `httpx` を直接使う箇所は 10 ファイル。SDK に渡しているのは `llm_clients/anthropic.py` の `httpx.Timeout(...)` だけで、anthropic 1.x では旧 httpx の型を渡すと構築時に `TypeError` になる (黙って壊れない)。`llm_clients/gemini.py` の `isinstance(err, httpx.ReadTimeout ...)` は google-genai が httpx のままなので変えない。方針: **自分のコードは SDK が使う方の型に合わせる** (anthropic / openai 向けは `httpx2`、genai 向けは `httpx`)。プロセス全体で `import httpx` を httpx2 に差し替える `httpx2.alias_httpx()` は使わない — 起動順に依存し、mcp / genai が期待する型を黙って変える。
- **httpx2 は証明書の信頼元を OS のストアにする** (旧 httpx は certifi)。macOS の Python は OS のストアが空なので、そのままでは LLM 呼び出しが `CERTIFICATE_VERIFY_FAILED` になる — 2026-09-02 に urllib で踏んだのと同じ穴。`saiverse/tls_trust.py` が起動時に `SSL_CERT_FILE` を立てるので httpx2 (trust_env=True) もそれを読む。SDK を上げるコミットで tls_trust.py の docstring (「httpx は影響を受けない」の記述) を現状に合わせ、**macOS の実機で LLM 呼び出しが通ること**を報告者に確認してもらう項目を検証に足す。
- **google-genai 2.0** の破壊的変更は Interactions API だけで、`GenerateContent` は無変更 (公式 CHANGELOG)。SAIVerse は Interactions API を使っていない (`llm_clients/` / `saiverse/llm_router.py` / `persona/emotion_module.py` / `tools/adapters/gemini.py` に参照なし) ので、**上限 `<2.0` は 2026-09-02 に外した** (`google-genai>=2.21.0`)。2.21 への実際の差分で SAIVerse に効いたのは一つだけ: 2.18 から `generate_content` は `automatic_function_calling.disable=True` を明示しない限り「Direct use of AFC ... is not recommended」を WARNING でプロセスごとに一度ログする (tools を渡していなくても)。`llm_clients/gemini.py` は元から無効化していたが、`saiverse/llm_router.py` / `persona/emotion_module.py` / `builtin_data/tools/image_generator.py` の 3 箇所は未指定だったので同じ規則 (SAIVerse は SDK の AFC を使わない) に揃えた。挙動は変わらない (tools が無ければ AFC ループは 1 回で抜ける)。google-genai の HTTP 部品は 2.21 でも `httpx` (<1.0) のまま。
- **anthropic 1.0** は httpx2 の他に、`messages.create()` から `temperature` / `top_p` / `top_k` を撤去 (渡すと `TypeError`。古いモデル向けに要るなら `extra_body`)、`output_format=` にスキーマ dict を渡す形を撤去、`.with_raw_response` の戻りが新クラスに。SAIVerse 側は **該当あり**: `llm_clients/anthropic_request_builder.py` (278〜283 行) が `temperature` / `top_p` / `top_k` を `messages.create()` の引数に積んでいる。1.x では `extra_body` へ移す (API 自体はまだ受け付ける)。
- **openai 2.0** は Responses API の function call output の型が広がっただけ、**3.0** は httpx2 のみ。
- lock の環境マーカーの癖: `hf-xet` の行のマーカーが `platform_machine == 'amd64'` (小文字) で書かれており、Windows (`AMD64`) では偽になって入らない。huggingface_hub は無くても通常のダウンロードに退避するので実害はないが、上流のマーカーをそのまま写した結果であることを記録しておく。

### 3-5. ついでに片付けるもの

- `google-adk 1.5.0` が開発機の venv に孤児で残っている (`docs/issues/starlette_google_adk_version_conflict.md`)。誰も使っていないので uninstall して issue を閉じる。
- README / installation.md / tailscale-runbook の Python 推奨を 3.13 へ (3.11〜3.13 動作の記述は維持。3.14 は `docs/issues/python_314_support_verification.md` の実機検証待ちのまま)。
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
2. **素の venv に lock だけで入る** — 開発機の venv ではなく、新しい venv に `pip install -r requirements.lock` して全スイートが緑。これが「ユーザーの手元」の代理。
3. **update 経路** — 隔離環境 (`docs/test_environment.md`) で `update_engine.py --manual` を通し、完了マーカーが lock の sha を持つこと。
4. **アドオン導入** — 隔離環境で voice-tts を `pip_install` step から入れ、constraints が効くこと (本体の部品を動かそうとしたら失敗すること、を一つ作為的に確かめる)。
5. **SDK の実 API 一回ずつ** — openai / anthropic / gemini / xai を各一回、隔離環境の合成ペルソナから呼ぶ。**課金が出るので、この段の直前にまはーの承認を取る** (対象・回数・見込み額を示して)。
6. **本番の再起動** — まはーの手で。会話一往復、声、記憶タブ。

「全スイート緑」は 2 と 3 の代理にならない (スイートは開発機の venv で走るため)。

---

## 6. 未決事項 (まはーの裁定待ち)

1. **lock を作る道具**: `uv pip compile --universal` を第一候補にしている (理由は §2-3)。pip-tools でも同じことはできるが、プラットフォーム横断の一枚を作るのは uv の方が素直。異論がなければ uv で進める。
2. **SDK 更新の順番**: 私案は Web 一族 → google-genai → anthropic → openai (影響の小さい順、最後が一番読み替えの多い openai)。
3. **google-genai の `<2.0` を外すか**: 2.x の変更点を読んでから決める。外せない理由が出たら §3-2 に理由を書いて止める。

---

## 7. 経緯

- 2026-09-02: voice-tts の無音事故 (numba × NumPy 2.5) を契機に起草。まはー「そろそろやらなきゃダメかな。依存関係全体を洗いたい、新しくするべきとこ新しくして、古いまま止めなきゃだめなやつはそう設定する整理が必要」。Python 推奨は 3.13 へ、feature ブランチで対応、と裁定。
