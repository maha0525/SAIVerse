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

`test-item-viewer.cjs` は `npm test` に含まれる（単独では `npm run test:item-viewer`）。
チャットの実リンク callback とインベントリの実 TSX を合成 API / hooks ハーネスで実行し、
短縮 ID / UUID の種類・名前・本文表示、削除済み参照、遅延応答、閉じ直し、persona 切替、
閲覧専用の入れ子を検査する。同じ18ケースをネイティブ区切りと `path.win32.relative`
の区切りで実行する。モジュール許可リストは `/` 区切りなので、比較時にOS依存の区切りを
正規化する。Linuxだけの実行でWindowsの子コンポーネントがスタブに化ける不具合を
見逃さないための境界テストで、実Windows実行の代わりとは扱わない。
ブラウザのレイアウトと画像 bytes の取得はこの検査の範囲外。
サーバー側の対応は `python -m pytest tests/test_item_viewer_api.py -n 0` で、
隔離 DB とファイルを使ってメタデータから画像 / 文書本文までを通す。

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

- **HTTP のモックだけでは DNS は止まらない**: provider の接続先検査は SDK / HTTP
  クライアントより前に名前を引く。通信しない設定テストや、HTTP が既にモックされた
  単体テストは `mock_provider_network("期待する公開ホスト")` を明示的に使う
  (`tests/conftest.py`)。provider の名前解決だけを差し替え、列挙していないホストは失敗する。
  プロセス全体の `socket.getaddrinfo` / `connect` / `connect_ex` は変えない。Windows の
  イベントループが内部で使う TCP socketpair を壊さないため。HTTP/SDK のモックは
  呼び出し側の責任であり、この fixture 自体は実通信を封じる仕組みではない。
  IP リテラルの private / metadata / loopback 判定は保つ。URL セキュリティの DNS
  回答を検べるテストは専用の回答を持ち、ローカル HTTP 結合テストには適用しない。
  全体 autouse 化や本番の URL 検査の差し替えで解決しない。全テストへ合成 DNS を
  適用すると、本来の DNS 回答を検査するセキュリティテストやローカル HTTP 結合テスト
  まで別の条件で動き、検査対象を隠してしまうため。
- **共有 API の差し替えは利用者の OS の代替経路も検べる**: 外部通信を止めるつもりで
  全 socket の接続を拒否すると、Windows のイベントループの自己通信用 TCP 接続も
  止まる。Linux の C 実装の socketpair だけではこの失敗を見つけられない。
  [fixture の契約テスト](../../tests/test_provider_test_network.py) では共有 socket API の
  同一性と、公開 socket API で TCP fallback を再現した TestClient の起動・リクエストを
  固定する。ただし Linux 上の再現を実 Windows / ProactorEventLoop の実行と同一視しない。
- **ダミーの資格情報は必要なテストだけに置く**: 実行部分を fake に任せるテストでも、
  モデル選択前に資格情報を検査する場合がある。必要なダミーキーをそのテストだけで設定し、
  全体への注入や資格情報検査の丸ごとの差し替えで未設定時の挙動を隠さない。
- **パスの入口検査と展開先検査は分ける**: snapshot の rooted path は POSIX では入口で、
  Windows では ZIP 展開先の包含検査で拒否される。OS ごとの期待を明示し、展開先の
  包含検査そのものも一時 ZIP を使って独立に固定する。
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
