# 現在のタスク

このドキュメントは、現在開発チームが集中して取り組んでいるタスクを記録します。
`docs/roadmap.md` の内容をより具体的にブレークダウンしたものです。

## Step 2.1: ローカルでのCity間連携

**目標:** 1台のPC上で2つのSAIVerseインスタンスを起動し、AIが相互に移動できるようにする。

### Task 1: 起動スクリプトのパラメータ化

- **目的**: 複数のSAIVerseインスタンスがポート番号やDBファイル名の衝突を起こさずに、同一マシン上で同時に起動できるようにする。
- **実装方法**:
  - [ ] `main.py`, `api_server.py`, `db_manager.py` を修正し、ポート番号とDBファイル名を環境変数から読み取るようにする。
    - `SAIVERSE_DB_FILE`
    - `SAIVERSE_API_PORT`
    - `SAIVERSE_DB_MANAGER_PORT`
    - `SAIVERSE_UI_PORT`
  - [ ] 異なる環境変数を設定して各インスタンスを起動するためのバッチファイル (`start_city_A.bat`, `start_city_B.bat`) を作成する。

### Task 2: City間通信API（受け入れ口）の実装

- **目的**: 他のCityから移動してきたAIを、訪問先のCityが受け入れるためのAPIエンドポイントを作成する。
- **実装方法**:
  - [ ] `api_server.py` に `/inter-city/move-in` エンドポイントを追加する。
  - [ ] このAPIは、訪問AIのプロファイル（ID, 名前, システムプロンプト等）をJSONで受け取る。
  - [ ] `SAIVerseManager` に、受け取ったプロファイルから訪問AIの一時的な `PersonaCore` インスタンスをメモリ上に生成し、指定されたBuildingに配置するメソッド (`place_visiting_persona` など) を追加する。

### Task 3: AIの越境ロジック（送り出し）の実装

- **目的**: AIが自分の知らないBuildingへ移動しようとした際に、それを「越境」と判断し、他のCityへ送り出すロジックを実装する。
- **実装方法**:
  - [ ] `persona_core.py` の `_handle_movement` メソッドを修正する。
  - [ ] 移動先IDが自分のCityに存在しない場合、`SAIVerseManager` の新しいメソッド (`dispatch_persona_to_another_city` など) を呼び出す。
  - [ ] `dispatch_persona_to_another_city` メソッドは、宛先CityのAPI（例: `http://localhost:9001/inter-city/move-in`）を `requests` ライブラリで呼び出し、自身のプロファイルを送信する。
  - [ ] 送信が成功したら、元のCityからはそのAIを退出させる。

### Task 4: UIを使わないテスト

- **目的**: UIを大きく変更する前に、コア機能が正しく動作するかをテストする。
- **実装方法**:
  - [ ] `db_manager.py` を使ってCity AのDBを直接編集し、AIの `AUTO_PROMPT` に「City Bの特定のBuildingへ移動する」という `move` アクションを書き込む。
  - [ ] AIが自律的にCity間を移動することを確認する。