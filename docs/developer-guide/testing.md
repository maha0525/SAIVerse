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
python -m pytest tests/test_persona_mixins.py::TestMovementMixin
python -m pytest tests/test_persona_mixins.py::TestMovementMixin::test_move_to_building
```

## テストファイル一覧

| ファイル | 対象 |
|----------|------|
| `test_llm_clients.py` | LLMクライアント |
| `test_llm_router.py` | ツールルーター |
| `test_history_manager.py` | 履歴管理 |
| `test_persona_mixins.py` | ペルソナMixin |
| `test_sai_memory_storage.py` | SAIMemoryストレージ |
| `test_sai_memory_chunking.py` | メッセージ分割 |
| `test_task_storage.py` | タスクストレージ |
| `test_task_tools.py` | タスク関連ツール |
| `test_image_generator.py` | 画像生成 |
| `test_thread_switch_tool.py` | スレッド切替 |

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

## CI/CD

プルリクエスト時に自動でテストが実行されます。

## カバレッジ

```bash
python -m pytest --cov=./ --cov-report=html
```

`htmlcov/index.html` でカバレッジレポートを確認。

## 次のステップ

- [コントリビューション](./contributing.md) - プルリクエストの作成
