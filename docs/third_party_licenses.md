# Third-Party Licenses

SAIVerse が依存する全てのサードパーティコンポーネントのライセンス一覧です。

最終更新: 2026-02-10

---

## ライセンス要注意事項

### SearXNG (AGPL-3.0) — リスク低

SearXNG も **AGPL-3.0** ですが、独立したプロセスとして起動し HTTP API 経由でのみ通信するため、SAIVerse 本体へのコピーレフト伝播はありません。

- SearXNG のソースコードを改変した場合、その改変部分は AGPL-3.0 で公開する必要がある
- 現在のWindows互換パッチ (`valkeydb.py` の `pwd` モジュール対応) は SearXNG への軽微な改変に該当するが、パッチ自体を AGPL-3.0 で提供すれば問題ない

---

## Python 依存パッケージ (requirements.txt)

| パッケージ | ライセンス | 用途 |
|---|---|---|
| discord.py | MIT | Discord ゲートウェイ連携 |
| websockets | BSD-3-Clause | WebSocket 通信 |
| httpx | BSD-3-Clause | HTTP クライアント |
| fastapi | MIT | API サーバーフレームワーク |
| google-genai | Apache-2.0 | Gemini API クライアント |
| langgraph | MIT | SEA フレームワーク (ワークフローグラフ) |
| openai | Apache-2.0 | OpenAI API クライアント |
| anthropic | MIT | Anthropic API クライアント |
| pydantic | MIT | データバリデーション |
| pydantic-settings | MIT | 設定管理 |
| python-dotenv | BSD-3-Clause | .env ファイル読み込み |
| requests | Apache-2.0 | HTTP クライアント |
| SQLAlchemy | MIT | ORM / データベースアクセス |
| uvicorn | BSD-3-Clause | ASGI サーバー |
| fastembed | Apache-2.0 | 埋め込みベクトル生成 |
| numpy | BSD-3-Clause | 数値計算 |
| Pillow | HPND | 画像処理 |
| markdownify | MIT | HTML → Markdown 変換 |
| pypdf | BSD-3-Clause | PDF 処理 |
| ruff | MIT | リンター (開発ツール) |

## Python オプション依存

| パッケージ | ライセンス | 用途 | ファイル |
|---|---|---|---|
| onnxruntime-gpu | MIT | GPU 推論 (埋め込み高速化) | requirements-gpu.txt |
| llama-cpp-python | MIT | ローカル LLM 実行 | requirements-local-llm.txt |

## Python 開発・テスト依存

| パッケージ | ライセンス | 用途 |
|---|---|---|
| pytest | MIT | テストフレームワーク |
| pytest-asyncio | Apache-2.0 | 非同期テストサポート |
| black | MIT | コードフォーマッター |

## フロントエンド依存 (npm)

| パッケージ | ライセンス | 用途 |
|---|---|---|
| next | MIT | React フレームワーク |
| react | MIT | UI ライブラリ |
| react-dom | MIT | React DOM レンダリング |
| react-markdown | MIT | Markdown レンダリング |
| recharts | MIT | グラフ描画 |
| rehype-raw | MIT | Raw HTML パース |
| rehype-sanitize | MIT | HTML サニタイズ |
| remark-breaks | MIT | 改行処理 |
| lucide-react | ISC | アイコン |
| typescript | Apache-2.0 | 型システム |
| eslint | MIT | リンター |
| eslint-config-next | MIT | Next.js 用 ESLint 設定 |
| @types/node | MIT | Node.js 型定義 |
| @types/react | MIT | React 型定義 |
| @types/react-dom | MIT | React DOM 型定義 |

## 外部コンポーネント

| コンポーネント | ライセンス | 用途 | 備考 |
|---|---|---|---|
| **SearXNG** | **AGPL-3.0** | Web 検索エンジン | 独立プロセスとして HTTP API 経由で使用。コピーレフト伝播なし |
| BAAI/bge-m3 | MIT | 埋め込みモデル (デフォルト) | |
| intfloat/multilingual-e5-base | MIT | 埋め込みモデル (代替) | |

---

## ライセンス種別サマリー

| ライセンス | 種別 | パッケージ数 | 商用利用 | コピーレフト |
|---|---|---|---|---|
| MIT | 許容的 | 27 | 可 | なし |
| BSD-3-Clause | 許容的 | 6 | 可 | なし |
| Apache-2.0 | 許容的 | 6 | 可 | なし |
| ISC | 許容的 | 1 | 可 | なし |
| HPND | 許容的 | 1 | 可 | なし |
| **AGPL-3.0** | **コピーレフト** | **1** | **条件付き** | **強い (ネットワーク条項あり)** |

- 許容的ライセンス (MIT, BSD, Apache, ISC, HPND): **41 パッケージ** — 制約なし
- コピーレフトライセンス (AGPL-3.0): **1 コンポーネント** — SearXNG (リスク低: 独立プロセスとして使用)
