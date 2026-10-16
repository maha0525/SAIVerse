# Windows の SearXNG 用環境に tzdata が導入されない

初回調査日: 2026-10-16。以下は修正前の事実確認。導入漏れの対策と版固定は [SearXNG 導入 intent](../intent/searxng_installation.md) に実装・検証結果を記録している。

## 報告と結論

初回の setup.bat 後、start.bat で起動できず、次の操作で復旧したという報告。

```powershell
.\scripts\.searxng-venv\Scripts\python.exe -m pip install tzdata
```

SearXNG 用環境への tzdata の導入漏れは確認できた。「要求したパッケージのインストールが失敗した」という証拠はなく、この環境ではそもそも導入対象に指定されていない。

現在取得される上流 SearXNG の Bilibili エンジンで、欠落による読み込みエラーも再現した。ただし、この例外はエンジンローダーが捕捉して続行する。報告者のエラー全文・取得した SearXNG の版は未提供であり、SAIVerse 全体または SearXNG サーバー全体の停止原因まで特定したものではない。

## 依存を導入する環境の違い

- setup.bat は本体用の `.venv` に requirements.lock を導入する。requirements.txt と requirements.lock には tzdata がある。
- scripts/setup_searxng.ps1 は別の `scripts/.searxng-venv` を作り、上流 SearXNG の requirements.txt と editable package を導入する。本体用の依存一覧は使わず、tzdata の追加指定もない。
- ローカルの両環境は `include-system-site-packages = false`。本体用 `.venv` にある tzdata は SearXNG 用環境には届かない。
- Python の zoneinfo は OS 側の IANA データ、または Python パッケージ tzdata を使う。Windows を含む OS 側データがない環境への配布では、tzdata の依存宣言が推奨されている。

根拠: [Python 公式 zoneinfo 文書](https://docs.python.org/3/library/zoneinfo.html#data-sources)。

## 上流取得の時期で変わる挙動

setup_searxng.ps1 / .sh の既定取得先は固定コミットではなく `master`。既存ソースに requirements.txt があれば、Windows 版は再取得しない。

ローカルに既存の SearXNG は `896863802e0d14b1d7003b442a842317a0272b0f`（2025-12-17）。調査時の GitHub API が返した master は `ff8f5ddcc9f2c58d8b0276ec030f6a3adfa17525`（2026-10-15T22:18:44Z）。旧版の Bilibili エンジンにはない次の式が、現行版ではモジュール読み込み時に実行される。

```python
_CN_TZ = ZoneInfo("Asia/Shanghai")
```

公式コミット履歴では、タイムゾーンを使う時間範囲検索の導入は `8c4df4cf3facd70d11888007ed01ee187db2e083`（2026-06-29）。旧環境で問題が目立たず、新規取得で読み込みエラーが出る条件が成立する。

上流の settings.yml では Bilibili は `disabled: true` だが、`load_engines()` が読み込みを省くのは `inactive: true` のとき。disabled のエンジンも import するので、利用者が Bilibili 検索を選んでいなくてもエラーが出る。

根拠:

- [Bilibili の時間範囲検索導入](https://github.com/searxng/searxng/commit/8c4df4cf3facd70d11888007ed01ee187db2e083)
- [調査対象の Bilibili エンジン](https://github.com/searxng/searxng/blob/ff8f5ddcc9f2c58d8b0276ec030f6a3adfa17525/searx/engines/bilibili.py#L43)
- [調査対象のエンジンローダー](https://github.com/searxng/searxng/blob/ff8f5ddcc9f2c58d8b0276ec030f6a3adfa17525/searx/engines/__init__.py#L131)

## 実測

本番の SAIVerse・ペルソナは起動せず、既存パッケージの追加・削除も行っていない。

1. ローカル本体用 `.venv`: tzdata が存在し、`ZoneInfo('UTC')` 成功。
2. ローカル SearXNG 用環境: tzdata が存在せず、`ZoneInfo('UTC')` と `ZoneInfo('Asia/Shanghai')` が ZoneInfoNotFoundError。
3. 同じ SearXNG 用環境で `python -m pip check`: `No broken requirements found.`。依存宣言にない欠落なので、この検査だけでは検出できない。
4. 現行上流の bilibili.py を一時領域へ取得し、現行上流の `load_engine` 関数を既存 SearXNG のユーティリティと組み合わせて実行。tzdata なしでは `Cannot load engine "bilibili"`、ZoneInfoNotFoundError、戻り値 None。その後の処理まで到達し、プロセス終了コードは 0。
5. 比較用の別プロセスで tzdata パッケージだけを参照可能にすると、同じモジュール・ローダーで `load_engine` の戻り値は bilibili、終了コード 0。これはパッケージ参照だけを切り替えたエンジン読み込み試験であり、現行 SearXNG 全体の新規インストール・HTTP 起動試験ではない。
6. setup.bat が使う Windows PowerShell で `$ErrorActionPreference = 'Stop'` を設定し、外部コマンドを終了コード 23 で失敗させた。後続の成功表示まで到達し、PowerShell プロセスの終了コードは 0 だった。

## セットアップで検出できない理由

- `find_spec('searx')` は場所の存在検査であり、import・タイムゾーン取得・サーバー起動の検査ではない。既存環境はこれが成功すると依存再導入を省く。
- setup_searxng.ps1 の pip 3 呼び出しには `$LASTEXITCODE` の判定がない。Windows PowerShell では `$ErrorActionPreference = 'Stop'` だけで外部コマンドの非ゼロ終了を例外にできない。
- 設定生成には終了コード判定があるが、最後の補助処理にはなく、末尾で `[OK] SearXNG setup complete` を表示する。今回 pip が実際に失敗したことを示す証拠はないが、別の導入失敗を見逃す経路が存在する。
- run_searxng_server.ps1 の再セットアップ条件はソース requirements.txt と python.exe の存在のみ。欠落した依存は修復対象にならない。
- start.bat は SearXNG を別窓で起動し、固定時間を待つだけで backend/frontend の起動へ進む。SearXNG の終了コードや HTTP 応答を判定していないため、同プロセスの失敗だけでバッチが後続を止める構造ではない。

## 対策の判断材料

修正する場合は、SearXNG 用環境の依存として tzdata を明示し、新規導入と導入済み環境の再実行の両方を扱う。本体用 requirements に追記するだけではこの別環境を直せない。

導入コマンドの失敗を呼び出し元へ返すことと、実ランタイムでタイムゾーン取得・実 import・隔離したサーバーの起動応答を検査することを分ける。新規環境、依存欠落、pip 失敗、再実行による復旧を Windows の実ランチャー経路で検証する必要がある。上流の取得版を固定・記録する方針も、新規導入で挙動が変わる条件への対策として検討する。

報告者の「起動できなかった」の範囲を確定するために必要なのは、失敗時のエラー原文と SearXNG / backend / frontend のどこまで起動したか。tzdata 追加後に復旧したという報告は保持し、今回再現した一部エンジンの失敗と同一だとは断定しない。

## 対応

まはーと相談し、検証したコミットを固定する方針を採用。上流メンテナーは [Windows ネイティブを主対象としていない](https://github.com/searxng/searxng/discussions/4029) と説明しており、上流の依存定義に tzdata がないことだけで上流の設定不備とは判断しない。Windows で直接動かす SAIVerse 側で必要な依存と互換処理を補う。

共通インストーラーで固定版と tzdata を導入し、依存欠落・pip の失敗を検出する。隔離した実セットアップと実ランチャーで新規導入、依存欠落からの復旧、HTTPと静的資源配信まで検証した。報告者の停止箇所についての初回調査の留保は、これらの試験結果で上書きしない。
