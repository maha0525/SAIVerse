# llama-swap 越しでは slot キャッシュが黙って働かない

**状態**: 未解決 (2026-09-21 起票。
[llama_cached_client_state_delegation_missing](archive/llama_cached_client_state_delegation_missing.md)
の実測中に見つかった別件)。

## 現象

`llama_slot_save_path` を設定したモデルは `LlamaCachedClient` に包まれ、推論の前に
slot の restore、後に save を行う。これは llama.cpp サーバーの `/slots/{id}?action=...`
API を叩く。

NEBULA (`~/.saiverse/user_data/models/nebula-*.json` が指す
`http://192.168.0.220:8092`) は llama-swap の入口で、この API を中継していない。

```
GET  /slots                    -> 404 page not found
POST /slots/0?action=save      -> 404 page not found
POST /v1/slots/0?action=save   -> 404 page not found
```

save は毎回 404 になり、警告ログを 1 行出して先へ進む。restore は
`LlamaCacheManager.cache_exists()` が **ローカルの** `llama_slot_save_path` を見るため、
写しが一度も作られず常に「キャッシュ無し」と判定されてスキップされる。

結果、NEBULA のモデル設定では **この wrapper の存在目的 (ペルソナごとの KV キャッシュの
持ち越し) が一度も働いていない**。働かないまま、推論のたびに slot の貸し借りと
HTTP 1 往復ぶんの手間だけが乗っている。

## 影響

失われるのは **持ち越し** であって、同じ slot に居座っている間の再利用ではない。
実測 (2026-09-21、artemis-31b、隔離環境の合成ペルソナで 4 往復) では、入力 39,602
トークンのうち 38,957 がキャッシュから読まれていた — サーバー自身が slot に残した
前方一致は効いている。

効かなくなるのは、その slot が別のペルソナに使われた後、あるいはサーバーを
再起動した後に戻ってきたとき。ペルソナごとの写しが一度も作られていないので、
そこから先は毎回フル prefill になる。`llama_parallel` が 1 の設定で複数ペルソナが
同じモデルを使う環境ほど、影響が大きい。

404 は `logger.warning` の 1 行だけなので、設定した本人には「効いていない」ことが
見えない。

## 確かめていないこと

- llama-swap が別のパスや別のポートで `/slots` を中継する設定を持つのかどうか。
- 上流の llama.cpp が `--slot-save-path` 付きで起動しているのかどうか
  (中継が無いので、こちらからは判定できない)。
- リモートのサーバーに保存した写しを、ローカルの `cache_exists()` がどう知るべきか
  (現在の実装はサーバーとローカルのディレクトリが同じである前提に見える)。

## 方向

まず「中継されていない」のか「上流に無い」のかを分けて確かめる。そのうえで、
少なくとも **効いていないことが設定した人に見える**必要がある — restore / save が
連続して失敗するモデルは、警告 1 行ではなく設定画面か起動時の点検で表に出す。
