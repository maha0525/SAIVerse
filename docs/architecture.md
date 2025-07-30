# SAIVerse アーキテクチャ設計

このドキュメントは、SAIVerseアプリケーションの全体的な構造と、各コンポーネントの役割について説明します。

## 1. 基本思想

SAIVerseは、自律的なAIエージェント（ペルソナ）が、定義された世界（City/Building）の中で相互作用し、独自の思考と行動を行うマルチエージェントシステムです。

- **状態の永続化**: AIや世界の動的な状態は、すべてデータベース（SQLite）に一元管理されます。これにより、システムの堅牢性と一貫性を保証します。
- **自律性の中心**: 各AIの「魂」は `PersonaCore` クラスに実装されています。特に `run_pulse` メソッドは、AIが「認知→判断→行動」というサイクルで能動的に活動するための心臓部です。
- **疎結合**: 各コンポーネントは、`SAIVerseManager` を通じて連携しますが、互いに直接的な依存関係は最小限に抑えられています。
- **分散ネットワーク**: 各Cityは独立したSAIVerseインスタンスとして動作します。City間の連携は、中央の「SAIVerseディレクトリサービス（SDS）」を通じて動的に行われます。

## 2. コンポーネント図

```mermaid
graph TD
    subgraph "SAIVerse Network"
        SDS[sds_server.py<br/><b>Directory Service</b>]
    end

    subgraph "User Interface"
        UI[main.py / Gradio]
    end

    subgraph "Core Logic"
        Manager[saiverse_manager.py]
        subgraph "Persona Types"
            direction LR
            Resident[persona_core.py<br/><b>Resident</b>]
            Visitor[remote_persona_proxy.py<br/><b>Visitor Proxy</b>]
        end
        ConvManager[conversation_manager.py]
    end

    subgraph "Data Layer"
        DB[Database / models.py]
        API[api_server.py]
        DB_UI[db_manager.py]
    end

    UI -- "User Actions" --> Manager

    Manager -- "Manages" --> Resident
    Manager -- "Manages" --> Visitor
    Manager -- "Manages" --> ConvManager
    Manager -- "Accesses" --> DB
    Manager -- "Registers & Discovers" --> SDS

    ConvManager -- "Triggers Pulse" --> Resident
    ConvManager -- "Triggers Pulse" --> Visitor

    Resident -- "Accesses" --> DB
    Visitor -- "Calls Home API" --> API

    API -- "Manipulates" --> DB
    DB_UI -- "Manipulates" --> DB
```

## 3. 主要コンポーネント詳細

### `main.py` (起動スクリプト)
- **役割**: アプリケーション全体のエントリーポイント。
- **責務**:
  - `SAIVerseManager`と`api_server.py`を起動する。
  - Gradio UIのメインループを管理し、ユーザーからの入力を`SAIVerseManager`に中継する。
  - `SAIVerseManager`のバックグラウンドタスク（DBポーリングなど）を定期的に実行するスレッドを開始する。

### `saiverse_manager.py` (世界の管理者)
- **役割**: SAIVerse世界の「神」や「管理者」に相当する中央コンポーネント。
- **責務**:
  - すべてのペルソナ (`PersonaCore`) とBuildingのインスタンスをメモリ上に保持・管理する。
  - 起動時にSDSに自身を登録し、定期的に他のCityの情報を取得する。
  - **DBポーリングによる非同期処理**: `VisitingAI`テーブルや`ThinkingRequest`テーブルを監視し、City間連携のトランザクションを進行させる。
  - AIの移動要求、ユーザーからの入力、自律会話の開始/停止など、世界で起こるすべてのイベントを統括する。
  - データベースから初期状態をロードし、終了時に状態を保存する。

### `persona_core.py` (AIの魂)
- **役割**: 個々のAIペルソナの「魂」であり「脳」。
- **責務**:
  - `run_pulse`メソッドを通じて、「認知→判断→行動」という自律的な思考サイクルを実行する。
  - LLMとの対話、感情の管理、行動の決定など、ペルソナのすべての知的活動を担う。

### `conversation_manager.py` (会話の進行役)
- **役割**: 各Buildingに1つずつ存在し、その場所での自律会話の流れを管理する。
- **責務**:
  - 定期的に（例: 10秒ごと）、Building内にいるペルソナを順番に指名し、`run_pulse`を呼び出して思考の機会を与える。

### `remote_persona_proxy.py` (訪問者の代理人)
- **役割**: 他のCityから訪問してきたAIの軽量な代理オブジェクト。
- **責務**:
  - `PersonaCore`と似たインターフェースを持つが、自身では思考しない。
  - `run_pulse`が呼ばれると、故郷のCityのAPI (`/persona-proxy/{id}/think`) を呼び出し、思考を依頼して結果を受け取る。

### `api_server.py` (Cityの窓口)
- **役割**: 各Cityが外部に公開するAPIサーバー。
- **責務**:
  - `/inter-city/request-move-in`: このAPIは現在使用されておらず、City間連携はDBを介して行われる。
  - `/persona-proxy/{id}/think`: 派遣したAIの代理人からの思考リクエストを受け付け、`thinking_request`テーブルにキューイングする。故郷の`SAIVerseManager`がこれを処理し、結果を返すまでロングポーリングで待機する。

### `sds_server.py` (世界の住所録)
- **役割**: SAIVerseネットワーク全体で唯一の中央ディレクトリサービス。
- **責務**:
  - /register: 各Cityからの登録を受け付ける。
  - /cities: 現在アクティブなCityのリスト（IPアドレス、ポート）を提供する。
  - /heartbeat: Cityからの生存通知を受け取り、非アクティブなCityをリストから削除する。

## 4. 起動シーケンス

1.  `main.py`が実行されます。
2.  `sds_server.py`が独立したプロセスとして起動されます（SAIVerseネットワークに1つ）。
3.  `main.py`は、`api_server.py`を別プロセスで起動します。。
4.  `main.py`は、`SAIVerseManager`のインスタンスを生成します。
5.  `SAIVerseManager`は、初期化処理の中でデータベースに接続し、すべての`User`, `City`, `Building`, `AI`の情報を読み込み、対応するオブジェクトをメモリ上に構築します。
6.  `SAIVerseManager`は、`SDS`に自身を登録し、他の`City`のリストを取得するためのバックグラウンドタスクを開始します。
7. `SAIVerseManager`は、各`Building`（`user_room`を除く）に対して`ConversationManager`を生成します。この時点では自律会話はまだ開始されません。
8. すべての準備が整うと、GradioのUIが起動し、ユーザーからの操作や「自律会話を開始」ボタンのクリックを待ち受けます。

## 5. City間連携シーケンス (リモート・ペルソナ)

SAIVerseのCity間連携は、APIを直接呼び出す同期的なフローではなく、**データベースの`VisitingAI`テーブルを介した非同期的なトランザクション**によって実現されています。これにより、通信相手のCityが一時的にオフラインでも移動要求が失われず、堅牢な連携が可能になります。

1.  **移動要求 (City A)**: `city_a`のAIが`city_b`への移動を決定すると、`city_a`の`SAIVerseManager`は`VisitingAI`テーブルに新しいレコードを作成します。このレコードには、宛先として`city_b`のIDが設定され、`status`は`'requested'`になります。

2.  **要求検知 (City B)**: `city_b`の`SAIVerseManager`は、バックグラウンドループで常に`VisitingAI`テーブルを監視しています。自分宛の`'requested'`状態のレコードを発見すると、受け入れ処理を開始します。

3.  **受け入れ処理 (City B)**: `city_b`は、訪問者のプロファイル（同名ペルソナの有無、移動先の空き状況など）を確認します。
    - **承認する場合**: `RemotePersonaProxy`インスタンスを生成してCityに配置し、`VisitingAI`レコードの`status`を`'accepted'`に更新します。
    - **拒否する場合**: `VisitingAI`レコードの`status`を`'rejected'`に更新し、`reason`カラムに拒否理由を記録します。

4.  **結果確認 (City A)**: `city_a`の`SAIVerseManager`も同様に`VisitingAI`テーブルを監視しており、自身が作成したレコードの`status`が`'accepted'`または`'rejected'`に変わったことを検知します。

5.  **移動確定 (City A)**:
    - **承認された場合**: `city_a`はAIの`IS_DISPATCHED`フラグを`True`に設定してローカルから退去させ、トランザクションが完了した`VisitingAI`レコードを削除します。
    - **拒否された場合**: `city_a`はAIに移動失敗とその理由を通知し、同様に`VisitingAI`レコードを削除します。