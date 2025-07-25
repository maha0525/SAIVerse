# 現在のタスク

このドキュメントは、現在開発チームが集中して取り組んでいるタスクを記録します。
`docs/roadmap.md` の内容をより具体的にブレークダウンしたものです。

## Step 2.1: ローカルでのCity間連携

**目標:** 1台のPC上で2つのSAIVerseインスタンスを起動し、AIが相互に移動できるようにする。

### Task 1: 起動スクリプトのパラメータ化

- **状態**: 完了 (Completed)
- **目的**: 複数のSAIVerseインスタンスがポート番号やDBファイル名の衝突を起こさずに、同一マシン上で同時に起動できるようにする。
- **実装方法**:
  - [x] `cities.json` を作成し、各CityのDBファイル名、UIポート、APIポートを定義。
  - [x] `main.py` を修正し、コマンドライン引数 (`city_id`) を受け取り、`cities.json` に基づいて対応する設定でCityを起動するようにした。
  - [x] `SAIVerseManager`, `PersonaCore` をリファクタリングし、インスタンスごとに固有のデータベース接続を持つようにした。
  - [x] `api_server.py` を修正し、コマンドライン引数でDBファイルとポート番号を受け取るようにした。
  - [x] `db_manager.py` のUIを `main.py` に統合し、プロセス起動を不要にした。
- **関連ファイル**:
  - `main.py`
  - `saiverse_manager.py`
  - `persona_core.py`
  - `database/api_server.py`
  - `database/db_manager.py`
  - `cities.json`

### Task 2: City間通信API（受け入れ口）の実装

- **状態**: 完了 (Completed)
- **目的**: 他のCityから移動してきたAIを、訪問先のCityが受け入れるためのAPIエンドポイントを作成する。
- **実装方法**:
  - [x] `api_server.py` を、独立して起動するFastAPIアプリケーションとして再構成した。
  - [x] `/inter-city/move-in` エンドポイントは、受け取ったAIのプロファイルをDBの新しい`VisitingAI`テーブルに書き込むことで、到着キューとして機能するようにした。
  - [x] `SAIVerseManager` に、バックグラウンドで`VisitingAI`テーブルを定期的にチェックする`_check_for_visitors`メソッドを追加した。
  - [x] `_check_for_visitors`は、新しい訪問者を見つけた場合、`place_visiting_persona`メソッドを呼び出して訪問AIの一時的な`PersonaCore`インスタンスをメモリ上に生成し、指定されたBuildingに配置するようにした。
- **関連ファイル**:
  - `database/api_server.py`
  - `saiverse_manager.py`
  - `database/models.py`

### Task 3: AIの越境ロジック（送り出し）の実装

- **状態**: 完了 (Completed)
- **目的**: AIが自分の知らないBuildingへ移動しようとした際に、それを「越境」と判断し、他のCityへ送り出すロジックを実装する。
- **実装方法**:
  - [x] `action_handler.py` を修正し、`move`アクションが`city`パラメータを受け取れるようにした。
  - [x] `persona_core.py` の `_handle_movement` メソッドを修正し、`city`パラメータを検知した場合に `dispatch_callback` を呼び出すようにした。
  - [x] `SAIVerseManager` に `dispatch_persona` メソッドを実装。このメソッドは宛先CityのAPIを呼び出し、成功したらAIを現在のCityから削除する。
  - [x] `SAIVerseManager` の初期化時に `PersonaCore` へ `dispatch_persona` をコールバックとして登録するようにした。
- **関連ファイル**:
  - `persona_core.py`
  - `saiverse_manager.py`
  - `action_handler.py`

### Task 4: UIを使わないテスト

- **状態**: 完了 (Completed)
- **目的**: UIを大きく変更する前に、コア機能が正しく動作するかをテストする。
- **実装方法**:
  - [x] City AのDB ManagerからBuildingの`AUTO_PROMPT`を編集し、City Bへの`move`アクションを書き込んだ。
  - [x] City Aを再起動して変更を反映させた後、自律会話を開始し、ターミナルログとCity BのUIで、AIがCity間を正常に移動することを確認した。
- **関連ファイル**:
  - `database/db_manager.py`