# 既知のテスト腐食 (別セッションで対応予定)

`python -m pytest tests/` で常時失敗する 9 件。実装変更による regression ではなく、テスト側のモック/フィクスチャが本番コードの最近のリファクタに追従していない「テスト腐食」。修正は別セッションでまとめて行う想定。

腐食であることは、これらの failure が現在進行中の機能改修とは無関係に、複数のセッションを跨いで同じ件数・同じ箇所で再現していることから判定済み。新規 commit が増やしてしまっていないかは「9 件以下を保つ」で確認できる。

## 一覧

### `tests/sea/test_runtime_engine.py` (1 件)

- **`test_set_playbook_returns_400_for_invalid_selected_playbook`**
  - `api/routes/config.py:627` で `manager.state.playbook_args` を参照しているが、テストの `manager` は `types.SimpleNamespace` モックで `playbook_args` 属性を持たない。
  - 加えて `_UserSettingsModel() takes no arguments` も同経路で発生 (テスト用 `UserSettings` モックのコンストラクタ仕様不一致)。

### `tests/sea/test_runtime_regression.py` (3 件)

- **`test_run_meta_user_returns_list_and_emits_status_callback`**
- **`test_run_meta_user_logs_and_continues_on_history_record_exception`**
- **`test_emit_speak_payload_compatibility`**
  - 共通: `persona.history_manager.add_message` が呼ばれることを期待しているが、現行コードは `persona.history_manager.add_to_persona_only` を呼ぶ (runtime_emitters.py 分割後の API 変更)。テスト側のモックが追従していない。
  - `test_emit_speak_payload_compatibility` のログには `AttributeError: 'types.SimpleNamespace' object has no attribute 'add_to_persona_only'` が直接出ている。

### `tests/test_config_set_playbook.py` (3 件)

- **`test_set_playbook_meta_user_manual_rejects_unknown_selected_playbook`**
- **`test_set_playbook_meta_user_manual_allows_empty_selected_playbook`**
- **`test_set_playbook_meta_user_manual_accepts_existing_selected_playbook`**
  - 同じく `api/routes/config.py:627` の `manager.state.playbook_args` 不在問題。テストの `manager` モック仕様の更新が必要。

### `tests/test_image_generator.py` (1 件)

- **`TestImageGenerator::test_generate_image_error_returns_error_text`**
  - `<module '_builtin_tools.image_generator'> does not have the attribute '_generate_with_gpt_image'`。
  - image_generator のリファクタで内部関数名が変わったが、テストの `patch.object(...)` が古い名前を指している。

### `tests/test_llm_clients.py` (1 件)

- **`TestLLMClients::test_ollama_client_generate_stream`**
  - 実体は `Lists differ: [] != ['Stream ', 'test']`。`/api/chat` のストリームチャンク parse が `WARNING root:ollama.py:705 Failed to parse /api/chat stream chunk: data: {"choices":...}` で失敗。
  - Ollama クライアントが OpenAI 形式の stream chunk を扱えていない。実装か parse ロジックの修正、またはテストモックの形式合わせが必要。

## 修正方針 (案、別セッション用メモ)

- sea 系 4 件と config_set_playbook 3 件は `manager`/`history_manager` モックの**仕様アップデート**で済むはず (本番コード側はそのまま)
- image_generator 1 件は patch 対象の関数名修正
- ollama 1 件は parse ロジック or テストフィクスチャの形式合わせ — 実装か期待値どちらが正しいかの判定が必要

## 関連

- `tests/conftest.py` の `load_builtin_tool` 周りは健全。腐食は個別ファイル側のモック更新漏れ。
