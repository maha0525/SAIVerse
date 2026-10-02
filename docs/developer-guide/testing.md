# テスト

SAIVerseのテスト実行方法を説明します。

## テストの実行

### 全テスト

```bash
# pytest（既定で並列実行。約2分で完走する — 2026-08-16 計測、24論理コア）
python -m pytest

# unittest（並列化されない・非推奨。全件を直列で回すと10分超かかる）
python -m unittest discover tests
```

pytest は `pyproject.toml` の設定（`-n auto --dist worksteal`）により pytest-xdist で並列実行される。並列を切りたいとき（デバッガ接続時や、出力を直列で読みたいとき）は `-n 0` を付ける。

### 特定のテストファイル

```bash
python -m pytest tests/test_persona_mixins.py -n 0
```

`-n 0` は並列ワーカーを起動せずその場で実行する指定。対象が1ファイル程度ならワーカー起動（各ワーカーがアプリ本体を import し直す、約15秒）の方が高くつくため、ピンポイント実行では付けるのが速い。

### 特定のテストクラス・メソッド

```bash
# 特定の関数を1つだけ
python -m pytest tests/test_persona_mixins.py::test_timestamp_to_epoch_parses_iso_string

# unittest スタイルのクラス/メソッド指定（該当ファイルがクラスを持つ場合）
python -m pytest tests/<file>.py::<TestClass>::<test_method>
```

## テストファイル

`tests/` に 230 本超（`test_*.py`）。代表例:

| ファイル | 対象 |
|----------|------|
| `test_llm_clients.py` | LLMクライアント |
| `test_llm_router.py` | ツールルーター |
| `test_history_manager.py` | 履歴管理 |
| `test_persona_mixins.py` | ペルソナMixin |
| `test_sai_memory_storage.py` | SAIMemoryストレージ |
| `test_sai_memory_chunking.py` | メッセージ分割 |
| `test_purpose_tools.py` | タスク・目的まわりのスペル |
| `test_user_conversation.py` | ユーザー会話の入口（会話状態・沈黙タイマー・仲裁） |
| `test_judgment_points.py` | 判断点の入出力（動的スキーマ・状況テキスト） |
| `test_sluice.py` | スルース（Metabolism の退場の関所での採取） |
| `test_v3_shape_migration.py` | v0.3「形の層」への機械写し（LIFE_PURPOSE / 旧 Track の関心 / desire 候補 → コア記憶・手帳） |
| `test_autonomy_manager.py` | AutonomyManager |
| `test_entity_extractor.py` | Memopedia エンティティ抽出 |
| `test_image_generator.py` | 画像生成 |
| `test_thread_switch_tool.py` | スレッド切替 |

全一覧は `ls tests/test_*.py` で確認する。**この表は代表例なので、対象コードを消したらここの行も同じコミットで消す**（消えたファイルが残っていると「回帰テストがある」と誤読される）。

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

- **HTTP のモックだけでは DNS は止まらない**: provider の接続先検査は SDK / HTTP
  クライアントより前に名前を引く。通信しない設定テストや、HTTP が既にモックされた
  単体テストは `mock_provider_network("期待する公開ホスト")` を明示的に使う
  (`tests/conftest.py`)。列挙していないホストの DNS と実ソケット接続は失敗する。
  IP リテラルの private / metadata / loopback 判定は保つ。URL セキュリティの DNS
  回答を検べるテストは専用の回答を持ち、ローカル HTTP 結合テストには適用しない。
  全体 autouse 化や本番の URL 検査の差し替えで解決しない。
- **ツールは動的ロードされる**: `TOOL_REGISTRY` はモジュールを動的に読み込んで構築されるため、モジュールトップの参照を差し替える `patch('module.func')` では効かない場合がある。**`patch.object`** で対象オブジェクトを直接差し替える（→ [reference_test_infrastructure]）。
- **DB テストは一時 DB を使う**: 本番 DB を触らない。テンポラリファイルに対して検証する。
- **Windows の SQLite ロック**: Windows ではファイルハンドルが開いたままだと削除・置換で `WinError 32` が出やすい。teardown で接続を確実に close してから片付ける。
- **隔離テスト環境**: バックエンドを本番データなしで叩くには `test_fixtures/`（`SAIVERSE_HOME=test_data/.saiverse`、ポート 18000）。詳細は [test_environment.md](../test_environment.md)。LLM コストを避けるなら `--quick`。

## CI/CD

現在の [Discord Gateway CI](../../.github/workflows/discord_gateway.yml) は、`discord_gateway/` や指定の依存ファイルを変更する PR で、Discord Gateway の lint とテストを実行する。リポジトリ全体の Python テストや frontend の検査を実行する workflow ではない。

本体・frontend の変更は上記の検査を手元の隔離環境で実行し、結果と未検証範囲を PR に記載する。GitHub に check が無いことを、全体テストの成功と扱わない。

## カバレッジ

```bash
python -m pytest --cov=./ --cov-report=html
```

`htmlcov/index.html` でカバレッジレポートを確認。

## 次のステップ

- [コントリビューション](./contributing.md) - プルリクエストの作成
