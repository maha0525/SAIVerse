# ワールドエディタ

SAIVerseの世界を編集する「ワールドエディタ」の使い方を説明します。

## 概要

ワールドエディタでは、City・Building・ペルソナ・ツールの追加・編集・削除を行えます。`database/seed.py` を直接編集することなく、UIから動的に世界を構築できます。

## アクセス方法

サイドバーのメニューから「World Editor」を選択。

## タブ構成

### Cities タブ

Cityの管理。

| 操作 | 説明 |
|------|------|
| 追加 | 新しいCityを作成 |
| 編集 | 名前・ポート設定を変更 |
| オンライン/オフライン | SDS連携の切り替え |

### Buildings タブ

Buildingの管理。

| フィールド | 説明 |
|------------|------|
| ID | 一意の識別子（カスタム入力可） |
| Name | 表示名 |
| System Prompt | Building固有のプロンプト |
| Capacity | 定員（0=無制限） |
| Auto Pulse Interval | 自律パルスの間隔（秒） |
| Interior Image | 内装画像（AIの視覚コンテキスト用） |

### AIs タブ

ペルソナの管理。

| フィールド | 説明 |
|------------|------|
| ID | 一意の識別子 |
| Name | 表示名 |
| System Prompt | 性格・背景設定 |
| Building | 現在の所属Building |
| Private Room | 休眠時に戻る個室 |
| Interaction Mode | auto/user/sleep |
| Appearance Image | 外見画像 |

### Tools タブ

AIが使用できるツールの管理。

| 操作 | 説明 |
|------|------|
| ツール一覧 | 登録されているツールを表示 |
| Building紐付け | どのBuildingでどのツールを使えるか設定 |

### Items タブ

アイテムの管理。

| フィールド | 説明 |
|------------|------|
| Name | アイテム名 |
| Type | picture/document/object |
| Description | 説明（自動生成可） |
| Location | Building/ペルソナ/ワールド |

## 典型的なワークフロー

### 新しいペルソナを追加

1. AIs タブを開く
2. 「Add AI」ボタンをクリック
3. 必要な情報を入力
4. 所属Buildingを選択
5. 「Save」で保存

### Building の設定を変更

1. Buildings タブを開く
2. 編集したいBuildingを選択
3. System Prompt やCapacity を編集
4. 「Save」で保存

### ツールをBuildingに追加

1. Tools タブを開く
2. 対象のBuildingを選択
3. 使用可能にしたいツールをチェック
4. 「Save」で保存

## 注意事項

- 変更は即座にデータベースに保存されます
- 現在稼働中のペルソナに影響する変更は、再起動後に反映される場合があります

## 次のステップ

- [コマンド一覧](./commands.md) - ユーザーコマンド
- [ツールシステム](../features/tools-system.md) - ツールの詳細
