# SAIVerse データベース設計

このドキュメントは、統合データベース `saiverse.db` のテーブル構造と、各テーブルの設計意図について説明します。

関連ファイル: `database/models.py`

## 設計思想

- **正規化**: データの重複を避け、一貫性を保つために正規化を行っています。
- **外部キー制約**: テーブル間の関連を明確にし、データの整合性を保証するために外部キー制約を積極的に利用します。
- **ユニーク制約**: データの重複を防ぐため、ビジネスロジック上ユニークであるべきカラムの組み合わせ（例：ユーザーごとのCity名）には複合ユニーク制約を設定します。

### 長期記憶システム (Qdrant)
AIの長期記憶（会話の断片やトピック）は、このドキュメントで説明されているリレーショナルデータベース `saiverse.db` (SQLite) には保存されません。長期記憶は、意味検索（セマンティック検索）に特化したベクトルデータベースである **Qdrant** に別途保存されます。

#### 設計と役割
- **疎結合**: メインのアプリケーションDB (SQLite) と長期記憶システム (Qdrant) を分離することで、それぞれが独立してスケールし、管理できるようになっています。
- **ペルソナごとの独立性**: 各ペルソナは、それぞれ独立したQdrantデータベースを持ちます。具体的には、`~/.saiverse/qdrant/persona/<persona_id>/` のように、ペルソナIDごとに専用のディレクトリにデータが保存されます。これにより、ペルソナ間の記憶の混同を防ぎ、プライバシーを確保しています。
- **高速な意味検索**: AIが思考する際（`pulse`）、現在の文脈に関連する過去の記憶を高速にベクトル検索するためにQdrantが利用されます。これにより、AIは文脈に応じた深い応答を生成できます。

#### データ構造
各ペルソナのQdrantデータベース内には、主に2種類のコレクション（テーブルに相当）が作成されます。

- **`entries` コレクション**:
  - **内容**: 個々の発話内容、そのベクトル表現、話者、タイムスタンプなどのメタデータを格納します。これが記憶の最小単位となります。
  - **用途**: 新しい会話が発生した際に、類似した過去の発話を検索するために使用されます。
- **`topics` コレクション**:
  - **内容**: 複数の`entries`を意味的にまとめた「トピック」の情報を格納します。トピックの要約、代表ベクトル（セントロイド）、関連する`entries`のIDリストなどが含まれます。
  - **用途**: 関連する会話の文脈をまとめて要約し、AIのプロンプトに含める（想起スニペットの生成）際に使用されます。

## 主要テーブル

### `User`
- **役割**: SAIVerseのユーザー情報を管理する。
- **キーカラム**:
  - `USERID` (PK): ユーザーの一意なID。
  - `USERNAME`: ユーザー名。
  - `LOGGED_IN`: 現在のログイン状態。AIの自律思考に影響を与える。
  - `CURRENT_CITYID` (FK to `City`): ユーザーが現在いるCity。
  - `CURRENT_BUILDINGID` (FK to `Building`): ユーザーが現在いるBuilding。
  
### `AI`
- **役割**: ペルソナ（AIエージェント）の基本設定と動的状態を管理する。
- **キーカラム**:
  - `AIID` (PK): ペルソナの一意なID。
  - `HOME_CITYID` (FK to `City`): このAIが所属する故郷のCity。
  - `AINAME`: ペルソナの名前。
  - `SYSTEMPROMPT`: ペルソナの性格や行動指針を定義するシステムプロンプト。
  - `EMOTION`: 現在の感情状態（JSON形式）。
  - `INTERACTION_MODE`: 現在の対話モード (`auto` / `user` / `sleep`)。
  - `IS_DISPATCHED`: このAIが他のCityに派遣中かどうかを示すフラグ。DBに永続化される。
  - `DEFAULT_MODEL`: このAIが使用するデフォルトのLLMモデル名。`NULL`の場合はCity全体のデフォルト設定に従う。
  - `PRIVATE_ROOM_ID` (FK to `Building`): このAIに割り当てられた個室のID。
  - `PREVIOUS_INTERACTION_MODE`: `user`モードになる直前の対話モードを退避させるためのカラム。
  
### `City`
- **役割**: ユーザーが所有する「世界」を定義する。各Cityは複数のBuildingを持つことができる。
- **キーカラム**:
  - `CITYID` (PK): Cityの一意なID。
  - `USERID` (FK to `User`): このCityを所有するユーザー。
  - `CITYNAME`: Cityの名前。`USERID`との組み合わせでユニーク。
  - `UI_PORT`: このCityのUIが使用するポート番号。
  - `API_PORT`: このCityのAPIが使用するポート番号。
  - `START_IN_ONLINE_MODE`: `True`の場合、このCityはオンラインモードで起動する。
  
### `Building`
- **役割**: AIが活動する「場所」を定義する。各Buildingは必ず一つのCityに所属する。
- **キーカラム**:
  - `BUILDINGID` (PK): Buildingの一意なID。
  - `CITYID` (FK to `City`): このBuildingが所属するCity。
  - `BUILDINGNAME`: Buildingの名前。`CITYID`との組み合わせでユニーク。
  - `CAPACITY`: このBuildingの収容人数。
  - `AUTO_INTERVAL_SEC`: このBuildingでの自律会話の実行周期（秒）。
  
### `BuildingOccupancyLog`
- **役割**: どのAIがいつどのBuildingに入退室したかを記録するログテーブル。
- **キーカラム**:
  - `ID` (PK): ログの一意なID。
  - `CITYID` (FK to `City`): このログが記録されたCity。
  - `AIID` (FK to `AI`): AI。
  - `BUILDINGID` (FK to `Building`): Building。
  - `ENTRY_TIMESTAMP`: 入室時刻。
  - `EXIT_TIMESTAMP`: 退室時刻。`NULL`の場合は現在滞在中であることを示す。

### `Blueprint`
- **役割**: AIやその他のエンティティの設計図（テンプレート）を管理する。ワールドエディタから作成・編集される。
- **キーカラム**:
  - `BLUEPRINT_ID` (PK): ブループリントの一意なID。
  - `CITYID` (FK to `City`): このブループリントが所属するCity。
  - `NAME`: ブループリントの名前。
  - `DESCRIPTION`: ブループリントの説明。
  - `BASE_SYSTEM_PROMPT`: このブループリントから生成されるAIの基本システムプロンプト。
  - `ENTITY_TYPE`: 生成されるエンティティの種類（例: `ai`）。
  - `BASE_AVATAR`: このブループリントから生成されるエンティティの基本アバター画像のパスまたはURL。

### `Tool`
- **役割**: AIがBuilding内で使用できるツールの定義を管理する。
- **キーカラム**:
  - `TOOLID` (PK): ツールの一意なID。
  - `TOOLNAME`: ツールの名前。
  - `DESCRIPTION`: ツールの機能説明。
  - `MODULE_PATH`: 実行されるツールのPythonモジュールへのパス（例: `tools.utility.calculator`）。
  - `FUNCTION_NAME`: モジュール内で実行する関数名。
  
### `BuildingToolLink`
- **役割**: どのBuildingでどのツールが利用可能かを紐付ける中間テーブル。
- **キーカラム**:
  - `id` (PK): リンクの一意なID。
  - `BUILDINGID` (FK to `Building`): Building。
  - `TOOLID` (FK to `Tool`): ツール。

### `ThinkingRequest`
- **役割**: リモート・ペルソナ・アーキテクチャにおいて、訪問先のCityから故郷のCityへの思考依頼をキューイングするためのテーブル。
- **キーカラム**:
  - `request_id` (PK): レコードの一意なID (UUID)。
  - `city_id` (FK to `City`): このリクエストを処理する故郷のCity。
  - `persona_id` (FK to `ai`): 思考を依頼されたAI。
  - `request_context_json`: 訪問先の状況（入室者、最近の会話など）をJSONで格納。
  - `status`: リクエストの状態 (`pending`, `processed`, `error`)。

### `VisitingAI`
- **役割**: City間の移動トランザクションを管理するための中間テーブル。移動要求から完了または失敗までの一連の状態を追跡する。
- **キーカラム**:
  - `id` (PK): ログの一意なID。
  - `city_id` (FK to `City`): 訪問先のCity。
  - `persona_id`: 訪問してきたAIのID (`ai.AIID`を参照)。`city_id`との組み合わせでユニーク。
  - `profile_json`: AIの名前、移動先、感情状態などを含むプロファイルをJSONで格納。
  - `status`: トランザクションの状態 (`requested`, `accepted`, `rejected`)。
  - `reason`: `status`が`rejected`の場合の拒否理由。