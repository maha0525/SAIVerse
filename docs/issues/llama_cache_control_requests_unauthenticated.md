# llama.cpp slot cache の制御リクエストが認証もヘッダーも付けずに飛ぶ

**状態**: 未解決 (2026-08-04 起票、OpenRouter アプリ帰属ヘッダー (`docs/intent/model_provider_management.md` §10) の Codex レビュー二巡目で発見)。本件は帰属ヘッダーとは独立した既存の欠陥で、当該変更では扱わなかった。

関連: [`docs/intent/model_provider_management.md`](../intent/model_provider_management.md) §10 / [`docs/reference/providers.md`](../reference/providers.md)

## 芯

**`LlamaCacheManager` の制御用 HTTP だけが、接続の資格情報を持たずに飛ぶ。** 推論そのものは内側の `OpenAIClient` (= OpenAI SDK) を通るので `Authorization` が付くが、slot cache の操作は `llm_clients/llama_cache.py` が `httpx.get` / `httpx.post` を直接呼んでおり、API キーも `default_headers` も渡されない。

- `_fetch_model_id()` → `GET {base_url}/v1/models`
- slot restore / slot save → `POST {base_url}/slots/...`

## 実害

`llama_slot_save_path` を設定した接続先が**認証を要求する remote サーバー**の場合、会話は成功するのに cache の保存・復元だけが 401 で失敗する。しかも失敗は `logger.warning` で握り潰され、`get_model_id()` は `"unknown"` を返して処理を続ける。

結果として**ペルソナ単位の KV cache 永続化が黙って無効になる**。利用者から見えるのは「キャッシュが効いていないらしい」という体感だけで、原因に辿り着く手掛かりがログの warning しかない。

ローカルの llama.cpp server (認証なし) では顕在化しないため、現状の主な利用形態では踏まない。

## 対応の方向

三択で、まだ決めていない。

1. **資格情報と接続ヘッダーを cache manager にも渡す** — 素直だが、`LlamaCacheManager` が接続の詳細を知る範囲が広がる。誰が資格情報を所有するかの線を引き直す必要がある
2. **認証付き endpoint では cache を明示的に無効化する** — 「使えないものは使えないと言う」形。利用者に見える失敗にできる
3. **失敗を可視化するだけに留める** — warning ではなく、cache 無効化を利用者に届く形で通知する

いずれにせよ **「認証が要る接続では slot cache は現状動かない」を沈黙させない**ことが最低条件。今は成功と区別が付かない。

## やらなかった理由 (2026-08-04 時点)

帰属ヘッダーの変更では、`default_headers` が LLM 呼び出しにしか乗らないことを `docs/reference/providers.md` の器の限界として書くに留めた。cache manager への配線は、上記のとおり**資格情報の所有権をどこに置くかという別の設計判断**を伴うため、宣伝用ヘッダーの都合で決めるべきではないと判断した。
