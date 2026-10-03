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

## フロントエンドの回帰検査

```bash
cd frontend
npm ci
npm test
# 接続先とプロキシだけ:
npm run test:backend-origin
```

`test-backend-origin.cjs` は実際の `next.config.ts` と三つの Route Handler を読み、fetch を fake に置き換える。正式名のみ・旧名のみ・空白・不一致・既定値、従来の URL 結合、書き込み body・クエリ・ヘッダ・206/Range、メディア例外、SSE と `/stream` の逐次転送・キャンセルを検査する。接続先の期待値は隔離用 18000 を中心に指定し、8000 を含め実ネットワークへの通信は一切行わない。実ブラウザでの音声再生は別途確認が必要。

実際の Next.js サーバーを経由する HTTP smoke は別コマンド。リポジトリの fake だけで検証でき、本番バックエンドは不要:

```bash
cd frontend
SAIVERSE_BACKEND_ORIGIN=http://127.0.0.1:18000 SAIVERSE_BACKEND_URL= npm run build
npm run test:backend-origin:http
```

この検査は 18000 の fake バックエンドと 18010 の production-mode Next.js を自分で起動し、通常 API / addon / MCP の読み書き、SSE、Range、メディアを通す。ポートが使用中、または build の rewrite が 18000 以外なら中止する。終了時は自分で起動したサーバーだけを停止する。`npm test` には追加せず、上の二つの専用テストを個別実行する。

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
