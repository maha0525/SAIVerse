# テスト用の台本と道具

以前ここは一日シミュレータ (`scripts/run_day_sim.py`) のシナリオの置き場だった。一日シムは
v2 の時間割を前提にした道具で、時間割の撤去 (autonomous_behavior_v04_plan.md 段 1-4) と
一緒に撤去した。ティック用の早回しは段 3 で作り直す。

## 台本つきの偽 LLM

`scripted_llm_server.py` は openai 互換の偽 LLM サーバー (ポート 18097)。隔離テスト環境の合成ペルソナ
`test_persona_stub` (部屋 `test_stub_room`) のモデルがこれを向いている (`test_fixtures/definitions/test_data.json`
の `llm_configs` を `setup_test_env.py` が `test_data/user_data/{providers,models}/` へ書く)。台本は
`scripted_llm_server.py preset <名前>` でサーバーを立て直さずに切り替えられる。実 LLM の鍵を無効にして
テストバックエンドを立てる `start_scripted_backend.bat` と組で使う。

- `reply_stop_exit_manual.md` — 返事が途中で止まった回の出口 (docs/intent/reply_stop_exit.md) をブラウザで確かめる手順
- `check_reply_stop_exit.py` — 同じ場面をチャット API で踏むヘッドレス確認
