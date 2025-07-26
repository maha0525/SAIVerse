# 現在のタスク

このドキュメントは、現在開発チームが集中して取り組んでいるタスクを記録します。
`docs/roadmap.md` の内容をより具体的にブレークダウンしたものです。

## Step 2.2: リモート・ペルソナ・アーキテクチャへの移行

**目標:** AIのシステムプロンプトなどの機密情報を他のCityに送信することなく、安全なCity間連携を実現する。

### Task 1: 送信プロファイルの軽量化と「思考API」の実装

- **状態**: 完了 (Completed)
- **目的**: AIの移動時に送信する情報から機密情報を削除し、代わりに外部から思考をリクエストできるAPIを故郷のCityに実装する。
- **実装方法**:
  - [x] `database/models.py`に思考依頼をキューイングするための`ThinkingRequest`テーブルを追加した。
  - [x] `database/api_server.py`に、思考を依頼するための`/persona-proxy/{persona_id}/think`エンドポイントを追加した。このAPIはロングポーリングで結果を待つ。
  - [x] `saiverse_manager.py`に、DBに記録された思考依頼をバックグラウンドで処理する`_process_thinking_requests`メソッドを追加した。
  - [x] `saiverse_manager.py`の`dispatch_persona`メソッドを修正し、送信するプロファイルから`system_prompt`を削除した。
- **関連ファイル**:
  - `saiverse_manager.py`
  - `database/api_server.py`
  - `database/models.py`

### Task 2: 「プロキシ・ペルソナ」の実装

- **状態**: 完了 (Completed)
- **目的**: 訪問先のCityで、AI本体の代わりに動作する軽量な代理オブジェクトを導入する。
- **実装方法**:
  - [x] 故郷の「思考API」を呼び出す`RemotePersonaProxy`クラスを新規作成した。
  - [x] `saiverse_manager.py`の`place_visiting_persona`メソッドを修正し、`PersonaCore`の代わりにこの`RemotePersonaProxy`をインスタンス化するように変更した。
- **関連ファイル**:
  - `saiverse_manager.py`
  - `remote_persona_proxy.py`