# ワールドエディタ

SAIVerseの世界を編集する「ワールドエディタ」の使い方を説明します。

## 概要

ワールドエディタでは、City・Building・ペルソナ・アイテムの追加・編集・削除を行えます。`database/seed.py` を直接編集することなく、UIから動的に世界を構築できます。

## アクセス方法

左サイドバー フッターの歯車（設定）→「ワールドエディタ」タブ。

## タブ構成

実 UI のサブタブは **City / Building / ペルソナ / Blueprint / ツール / アイテム / Playbook** の7つ。主なものを以下で説明する（Blueprint = ペルソナ生成テンプレート、Playbook = Playbook 管理、ツール = 下記の注記参照）。

### Cities タブ

Cityの管理。

| 操作 | 説明 |
|------|------|
| 追加 | 新しいCityを作成 |
| 編集 | 名前・ポート設定を変更 |
| オンラインモードで起動 | SDS 連携を有効化（City 選択時のチェックボックス、既定 off） |

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
| 都市（city_id） | 所属 City |
| Description | 説明 |
| 追加プロンプトファイル | 追加のプロンプト |

### AIs タブ

ペルソナの管理。

| フィールド | 説明 |
|------------|------|
| 名前 | 表示名 |
| ホーム都市 | 所属 City |
| デフォルトモデル / 軽量モデル | 使用モデル |
| アクティビティ状態 | 自律性の状態（`ACTIVITY_STATE`: Stop / Sleep / Idle / Active） |
| アバター / 外見画像 | アイコン・外見画像 |
| システムプロンプト | 性格・背景設定 |
| 説明 | ペルソナの説明 |

> **ツールタブ / Building 紐付けについて**: ワールドエディタには「ツール」タブと Building へのツール紐付け UI（`BuildingToolLink`）が**残っているが、現在は実効性がない**——この紐付けではペルソナにツールは届かない。ペルソナに使わせるには Spell 化（`spell=True`）するか Playbook の TOOL ノードに置く（→ [ツールの追加](../developer-guide/adding-tools.md)）。

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

## 注意事項

- 変更は即座にデータベースに保存されます
- 現在稼働中のペルソナに影響する変更は、再起動後に反映される場合があります

## 次のステップ

- [ツールシステム](../features/tools-system.md) - ツールの詳細
