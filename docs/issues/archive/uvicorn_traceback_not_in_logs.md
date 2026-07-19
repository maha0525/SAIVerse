# Issue: uvicorn の 500 エラー Traceback が backend.log に出ない

**ステータス**: ✅ 解決済み (2026-07-19)
**優先度**: medium
**作成日**: 2026-05-08
**関連**: `saiverse/logging_config.py`, `main.py` (uvicorn 起動箇所)

## 背景

API ハンドラ内で例外が発生して 500 Internal Server Error が返る場合、uvicorn は Traceback を **stderr に直接書き出す** が、これが `~/.saiverse/user_data/logs/{session}/backend.log` には載らない。

```
INFO:     127.0.0.1:59464 - "PATCH /api/people/air_city_a/config HTTP/1.1" 500 Internal Server Error
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  ...
```

の形式の出力は、まはーが SAIVerse を起動しているターミナルウィンドウにのみ表示され、後から `backend.log` を grep しても何も見つからない。

2026-05-08 のセッションで、Phase 4-e の `update_ai` フォワードメソッド漏れによる 500 エラーを調査する際、Claude Code が `backend.log` を延々と grep して原因に辿り着けなかった事故が発生 (`memory/feedback_check_terminal_first.md` 参照)。

## 何が問題か

- **デバッグ効率が悪い**: ターミナルを直接見られない (リモート / 別マシン / バックグラウンド実行) 状況で、500 の原因が完全にわからなくなる
- **ログ集約が破綻**: 「session ごとに backend.log にすべて出る」という設計上の前提が破られている
- **再現困難なエラーを失う**: ターミナルがスクロールしてしまうと記録が残らない (`tee` 等しないと永続化されない)

## 原因の見当

uvicorn は内部で `logging` モジュールを使うが、`Exception in ASGI application` の Traceback は `logging.error()` ではなく `traceback.print_exc()` 相当で stderr に直書きしている可能性が高い。または uvicorn のロガー (`uvicorn.error`) が SAIVerse の logging 設定に紐付けられていない。

## 解決案候補

### 案 A: uvicorn のロガーを SAIVerse の root logger にマージ

`logging_config.py` (or 起動コード) で、`uvicorn` / `uvicorn.error` / `uvicorn.access` の各ロガーに root と同じ FileHandler を付ける。

```python
import logging
for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    logger = logging.getLogger(name)
    # backend.log と同じハンドラを共有
    logger.addHandler(file_handler)
    logger.propagate = False  # 二重出力を避ける
```

### 案 B: FastAPI に exception_handler を設定して Traceback を root logger に流す

```python
@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    logging.exception("Unhandled exception in %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": str(exc)})
```

ただしこれだと FastAPI 側で例外が消費されるので uvicorn の `Exception in ASGI application` は出なくなる代わりに、自分のログには確実に流れる。

### 案 C: uvicorn 起動時に `--log-config` で SAIVerse の logging 設定を流し込む

uvicorn は `--log-config <yaml>` で外部設定を読める。SAIVerse が自前で uvicorn を spawn しているなら、この経路で root logger と同じ FileHandler を効かせる。

### 推奨

**案 A + 案 B の併用** が筋がいい:
- 案 A で uvicorn 由来のあらゆるログ (access / startup / error) を backend.log に集約
- 案 B で FastAPI ハンドラ内例外を確実に root logger 経由でファイルに残す

## 関連リソース

- `saiverse/logging_config.py` — SAIVerse の logging 設定
- `main.py` — uvicorn 起動コード
- `memory/feedback_check_terminal_first.md` — 同件で生成された Claude Code 用メモ
- 関連事例: 2026-05-08 の Phase 4-e `update_ai` フォワード漏れの 500 デバッグで、ファイルログだけでは原因が見つからなかった

## ログ

- 2026-05-08: issue 起票。Phase 4-e の `update_ai` フォワード漏れ事件をきっかけに、uvicorn の Traceback がファイルログに載らない問題を認識。
- 2026-07-19: **解決**。`main.py` の `uvicorn.run(...)` に `log_config=None` を追加。これで uvicorn は自前の logging 設定 (`dictConfig`) を丸ごとスキップし、`uvicorn` / `uvicorn.error` / `uvicorn.access` ロガーは既定の伝播 (handlers 無し・propagate=True) のまま root の `TeeHandler` (console + backend.log) に流れる。案 A 相当だが、手動でハンドラを付け直すのではなく uvicorn の再設定自体を抑止する一行で達成。ASGI 例外の `Exception in ASGI application` traceback は `uvicorn.error` が `exc_info` 付きで吐くため、これで backend.log にフル traceback が残る。access / startup ログも同時に backend.log へ集約される。隔離 `SAIVERSE_USER_DATA_DIR` で uvicorn の `Config(log_config=None).configure_logging()` を実際に通し、`uvicorn.error` の traceback が backend.log に着地することを実証済み。案 B (FastAPI exception_handler) は不要と判断 (伝播経路だけで十分・uvicorn 本来の 500 ログも残せる)。
