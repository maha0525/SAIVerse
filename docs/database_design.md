# SAIVerse データベース設計

このドキュメントは、`saiverse_main.db` のテーブル構造と、各テーブルの設計意図について説明します。

関連ファイル: `database/models.py`

## 設計思想

- **正規化**: データの重複を避け、一貫性を保つために正規化を行っています。
- **外部キー制約**: テーブル間の関連を明確にし、データの整合性を保証するために外部キー制約を積極的に利用します。
- **ユニーク制約**: データの重複を防ぐため、ビジネスロジック上ユニークであるべきカラムの組み合わせ（例：ユーザーごとのCity名）には複合ユニーク制約を設定します。

## 主要テーブル

### `User`
- **役割**: SAIVerseのユーザー情報を管理する。
- **キーカラム**:
  - `USERID` (PK): ユーザーの一意なID。
  - `USERNAME`: ユーザー名。
  - `LOGGED_IN`: 現在のログイン状態。AIの自律思考に影響を与える。

### `AI`
- **役割**: ペルソナ（AIエージェント）の基本設定と動的状態を管理する。
- **キーカラム**:
  - `AIID` (PK): ペルソナの一意なID。
  - `AINAME`: ペルソナの名前。
  - `SYSTEMPROMPT`: ペルソナの性格や行動指針を定義するシステムプロンプト。
  - `EMOTION`: 現在の感情状態（JSON形式）。
  - `INTERACTION_MODE`: 現在の対話モード (`auto` / `user`)。

### `City`
- **役割**: ユーザーが所有する「世界」を定義する。各Cityは複数のBuildingを持つことができる。
- **キーカラム**:
  - `CITYID` (PK): Cityの一意なID。
  - `USERID` (FK to `User`): このCityを所有するユーザー。
  - `CITYNAME`: Cityの名前。`USERID`との組み合わせでユニーク。
  - `IS_PUBLIC`: このCityが他のユーザーに公開されているかどうかのフラグ。

### `Building`
- **役割**: AIが活動する「場所」を定義する。各Buildingは必ず一つのCityに所属する。
- **キーカラム**:
  - `BUILDINGID` (PK): Buildingの一意なID。
  - `CITYID` (FK to `City`): このBuildingが所属するCity。
  - `BUILDINGNAME`: Buildingの名前。`CITYID`との組み合わせでユニーク。
  - `CAPACITY`: このBuildingの収容人数。

### `BuildingOccupancyLog`
- **役割**: どのAIがいつどのBuildingに入退室したかを記録するログテーブル。
- **キーカラム**:
  - `ID` (PK): ログの一意なID。
  - `AIID` (FK to `AI`): AI。
  - `BUILDINGID` (FK to `Building`): Building。
  - `ENTRY_TIMESTAMP`: 入室時刻。
  - `EXIT_TIMESTAMP`: 退室時刻。`NULL`の場合は現在滞在中であることを示す。

## リンクテーブル

- **`UserAiLink`**: ユーザーとAIの多対多の関連を定義する（例：お気に入り登録など）。
- **`AiToolLink`**: AIが利用可能なツールの多対多の関連を定義する。
- **`BuildingToolLink`**: Buildingに設置されているツールの多対多の関連を定義する。