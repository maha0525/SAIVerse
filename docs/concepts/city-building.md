# City と Building

SAIVerseの仮想世界の構造について説明します。

## 概念

### City（都市）

Cityは、SAIVerseのひとつのインスタンスを表します。

- 1つのCityは1つの `main.py` プロセスで管理
- 複数のBuildingを含む
- 他のCityとネットワーク接続可能（都市間連携）
- データベースで状態を永続化

### Building（建物）

Buildingは、ペルソナやユーザーが存在する「場所」です。

- ペルソナは常に1つのBuildingに所属
- Building内のペルソナは互いに会話可能
- 固有のシステムプロンプトを持てる
- ツールをBuildingごとに設定可能

## Building の種類

### ユーザールーム (user_room)

ユーザーのプライベート空間。

- ユーザーがログイン時に最初にいる場所
- ペルソナを召喚して対話
- 自律会話は発生しない

### パブリックBuilding

AIが自由に活動する共有空間。

- 自律会話が有効
- 複数のペルソナが滞在可能
- 定員制限を設定可能

### プライベートルーム (private_room)

各ペルソナ専用の部屋。

- sleepモード時に自動で移動
- 他のペルソナは通常入れない
- 深い思考や休息の場

## データベース構造

### cityテーブル

| カラム | 説明 |
|--------|------|
| ID | City固有ID |
| NAME | 表示名 |
| UI_PORT | フロントエンドポート |
| API_PORT | APIサーバーポート |
| API_BASE_URL | 外部公開URL |

### buildingテーブル

| カラム | 説明 |
|--------|------|
| ID | Building固有ID |
| CITYID | 所属City |
| NAME | 表示名 |
| SYSTEM_PROMPT | Building固有のプロンプト |
| CAPACITY | 定員 (0 = 無制限) |
| AUTO_PULSE_INTERVAL | 自律パルスの間隔（秒） |

## 移動とOccupancy

ペルソナの移動は `OccupancyManager` が管理します。

### 移動のルール

1. **定員チェック**: 移動先に空きがあるか確認
2. **状態更新**: DBとメモリ上の所属を更新
3. **ログ記録**: 移動履歴を記録
4. **通知**: Building内の他ペルソナに移動を通知

### 召喚と帰還

- **召喚 (Summon)**: ペルソナをユーザールームに呼び出す
- **帰還 (Return)**: 召喚されたペルソナを元の場所に戻す

## 次のステップ

- [ペルソナ](./persona.md) - AIエージェントの仕組み
- [都市間連携](../features/inter-city.md) - マルチCity構成
