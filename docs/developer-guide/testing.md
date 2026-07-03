# テスト

SAIVerseのテスト実行方法を説明します。

## テストの実行

### 全テスト

```bash
# pytest
python -m pytest

# unittest
python -m unittest discover tests
```

### 特定のテストファイル

```bash
python -m pytest tests/test_persona_mixins.py
```

### 特定のテストクラス・メソッド

```bash
# 特定の関数を1つだけ
python -m pytest tests/test_persona_mixins.py::test_timestamp_to_epoch_parses_iso_string

# unittest スタイルのクラス/メソッド指定（該当ファイルがクラスを持つ場合）
python -m pytest tests/<file>.py::<TestClass>::<test_method>
```

## テストファイル

`tests/` に 100 本超（`test_*.py`）。代表例:

| ファイル | 対象 |
|----------|------|
| `test_llm_clients.py` | LLMクライアント |
| `test_llm_router.py` | ツールルーター |
| `test_history_manager.py` | 履歴管理 |
| `test_persona_mixins.py` | ペルソナMixin |
| `test_sai_memory_storage.py` | SAIMemoryストレージ |
| `test_sai_memory_chunking.py` | メッセージ分割 |
| `test_task_tools.py` | タスク関連ツール |
| `test_track_manager.py` | Track 管理 |
| `test_pulse_scheduler.py` | SubLineScheduler |
| `test_autonomy_manager.py` | AutonomyManager |
| `test_entity_extractor.py` | Memopedia エンティティ抽出 |
| `test_image_generator.py` | 画像生成 |
| `test_thread_switch_tool.py` | スレッド切替 |

全一覧は `ls tests/test_*.py` で確認する。

## テストの書き方

### 基本的なテスト

```python
import unittest

class TestMyFeature(unittest.TestCase):
    def setUp(self):
        # テスト前の準備
        pass
    
    def tearDown(self):
        # テスト後のクリーンアップ
        pass
    
    def test_basic_functionality(self):
        result = my_function("input")
        self.assertEqual(result, "expected")
```

### 非同期テスト

```python
import asyncio
import unittest

class TestAsyncFeature(unittest.TestCase):
    def test_async_function(self):
        async def run_test():
            result = await async_function()
            return result
        
        result = asyncio.run(run_test())
        self.assertIsNotNone(result)
```

### モックの使用

```python
from unittest.mock import Mock, patch

class TestWithMock(unittest.TestCase):
    @patch('llm_clients.gemini.GeminiClient')
    def test_with_mock_llm(self, mock_client):
        mock_client.return_value.generate.return_value = "mocked response"
        # テスト実行
```

## テスト時の注意（実装由来の落とし穴）

- **ツールは動的ロードされる**: `TOOL_REGISTRY` はモジュールを動的に読み込んで構築されるため、モジュールトップの参照を差し替える `patch('module.func')` では効かない場合がある。**`patch.object`** で対象オブジェクトを直接差し替える（→ [reference_test_infrastructure]）。
- **DB テストは一時 DB を使う**: 本番 DB を触らない。テンポラリファイルに対して検証する。
- **Windows の SQLite ロック**: Windows ではファイルハンドルが開いたままだと削除・置換で `WinError 32` が出やすい。teardown で接続を確実に close してから片付ける。
- **隔離テスト環境**: バックエンドを本番データなしで叩くには `test_fixtures/`（`SAIVERSE_HOME=test_data/.saiverse`、ポート 18000）。詳細は [test_environment.md](../test_environment.md)。LLM コストを避けるなら `--quick`。

## CI/CD

プルリクエスト時に自動でテストが実行されます。

## カバレッジ

```bash
python -m pytest --cov=./ --cov-report=html
```

`htmlcov/index.html` でカバレッジレポートを確認。

## 次のステップ

- [コントリビューション](./contributing.md) - プルリクエストの作成
