# SAIVerse item システム実装計画

## 1. 目的とゴール
- ペルソナが自律的に行動する際の「やること」を提供するため、SAIVerse 内に「item（モノ）」の概念を導入する。
- Building や Persona が item を所有し、取得・設置・使用を通じて状態を変化させられる仕組みを作る。
- まずは `type=object` のシンプルなアイテムを扱い、今後 `book` や `container` などの拡張に繋げられる基盤を整える。

## 2. 機能要件（今回実装範囲）
1. item 定義
   - `uuid`, `name`, `type`, `description` などの基本フィールドを持つ。
   - 追加属性は JSON で保持できるように `state_json` フィールドを用意する。
2. 所有場所
   - Building または Persona が item を所有する。
   - アイテムの現在位置は別テーブルで管理し、障害時のロールバックや履歴追跡をしやすくする。
3. ツール実行
   - `item_pickup`: Building 内の item を取得して Persona の inventory に移す。
   - `item_place`: Persona の inventory 内の item を現在地の Building に置く。
   - `item_use`: Persona の inventory 内の item を使用し、`type=object` の場合は `description` を変更できる。
4. 知覚とプロンプト
   - Persona の inventory は共通システムプロンプトに「### インベントリ」という節を追加して提示。
   - Building 内の item は Building の system instruction の「## 現在地」節に追記する。
   - item の追加／削除／内容変更があった場合、次回パルスで状況スナップショットに通知する。
5. イベント記録
   - ペルソナごとに「発生イベントメモ」のテーブル（テキストログ）を持ち、item 更新などの重要イベントを追記。
   - パルスで通知が完了したら対象エントリを削除または済フラグを立てる。
6. ワールドエディタ
   - item の CRUD と所有者の設定ができる UI セクションを追加。

## 3. データベース設計
### 3.1 テーブル追加
1. `ITEMS`
   | フィールド | 型 | 備考 |
   | --- | --- | --- |
   | ITEM_ID | TEXT (UUID) | PK |
   | NAME | TEXT | |
   | TYPE | TEXT | 初期値 `object` |
   | DESCRIPTION | TEXT | |
   | STATE_JSON | TEXT | 任意属性（JSON 文字列） |
   | CREATED_AT | DATETIME | |
   | UPDATED_AT | DATETIME | |

2. `ITEM_LOCATIONS`
   | フィールド | 型 | 備考 |
   | --- | --- | --- |
   | LOCATION_ID | INTEGER | PK |
   | ITEM_ID | TEXT | FK → ITEMS |
   | OWNER_KIND | TEXT | `building` or `persona` or `world` |
   | OWNER_ID | TEXT | BuildingID または PersonaID 等 |
   | UPDATED_AT | DATETIME | |

3. `PERSONA_EVENT_LOGS`
   | フィールド | 型 | 備考 |
   | --- | --- | --- |
   | EVENT_ID | INTEGER | PK |
   | PERSONA_ID | TEXT | FK → AI |
   | CREATED_AT | DATETIME | |
   | CONTENT | TEXT | 発生イベント本文（1 行は 1 イベント） |
   | STATUS | TEXT | `pending` / `archived` |

### 3.2 マイグレーション
- `database/migrations` に新規スクリプトを追加し、既存 DB へのテーブル追加を行う。
- 既存データには影響なし。初期 item はゼロ件。

## 4. サーバーサイド改修
### 4.1 モデルとロード処理
- `database/models.py`
  - `Item` / `ItemLocation` / `PersonaEventLog` ORM モデルを追加。
- `manager/state.CoreState`
  - `items`, `item_locations` のキャッシュ領域を追加。
- `saiverse_manager.py`
  - 初期化時に item と location を読み込み、Building ごと・Persona ごとのマップを構築。
  - item 変更時にはキャッシュを更新し、該当 Persona のイベントログにレコードを追加。
- `PersonaCore`
  - 初期化時に inventory を受け取る。
  - `common_prompt` を組み立てる際に inventory 情報を追加表示。
  - Pulse の状況スナップショットを組み立てる際、`PersonaEventLog` から pending イベントを取り出して記載し、成功したら `STATUS=archived` に更新。
  - inventory 更新時は `history_manager` のログと `PersonaEventLog` の両方に記録。

### 4.2 Building system instruction 補強
- `buildings.py` で Building の system_instruction を生成する際、該当 Building に紐づく item リストを `## 現在地` 節に付与。
  - 表示フォーマット例: `- [item-name] description`。
  - item 数が多い場合は最大件数やサマリを検討（初期は無制限で様子を見る）。

### 4.3 イベントメモ処理
- `manager/runtime.RuntimeService`
  - item 関連ツールが実行されたとき、対象 Persona のイベントログに追記。
- `persona/mixins/pulse.PersonaPulseMixin`
  - パルス開始時に未読イベントを取得してスナップショットに追記、反映後は `STATUS=archived` にするヘルパーを追加。

## 5. ツールの設計
- `tools/defs.py` に以下のエントリを追加。
  - `item_pickup(building_id: str, item_id: str)`  
    - 所有者を Persona に更新。成功時は「<item> を拾った」とログ。
  - `item_place(building_id: str, item_id: str)`  
    - 所有者を Building に更新。成功時は「<item> を設置した」とログ。
  - `item_use(item_id: str, new_description: str)`  
    - `type=object` の場合のみ許可。Description を更新し履歴に残す。
- 実装は `tools/items.py`（仮）を新設。共通関数として `change_item_owner`, `update_item_description` を用意し、DB更新→キャッシュ更新→イベントログ追記までを一括で行う。
- 失敗ケース（item が見つからない、所有権が矛盾、type 不一致）はエラーメッセージを返却。

## 6. プロンプトと履歴の反映
- Inventory 表示
  - `persona/core.PersonaCore._build_messages` 内で `### インベントリ` ブロックを追加（空のときは `(所持品なし)`）。
- Building 表示
  - `PersonaGenerationMixin._build_messages` で Building の system instruction から item 情報を受け取り、そのまま内包。
- ツール結果
  - item 使用や所有移動は `history_manager` にメッセージとして追加（role: system / host）。例: `<div class="note-box">🧺 item_use: ...</div>`。

## 7. UI 変更
- ワールドエディタ (`ui/world_editor.py`)
  - item タブを追加し CRUD 操作を提供。
  - 所有者を Building / Persona から選択できるドロップダウンを配置。
  - `STATE_JSON` を編集できるテキストエリア（上級者向け）を用意。
- ワールドビュー
  - 初期実装では変更なし。将来、右サイドバーに item インタラクションを置くための余地を残す。

## 8. 実装ステップ
1. **DB マイグレーション**
   - テーブル追加スクリプト実装・適用テスト。
2. **モデルとキャッシュ**
   - ORM モデル定義、`saiverse_manager` でのロードとキャッシュ構築。
3. **イベントログ処理**
   - `PersonaEventLog` の読み書きヘルパーを実装し、テストケース追加。
4. **ツール実装**
   - `item_pickup`, `item_place`, `item_use` を追加し、単体テストで動作確認。
5. **プロンプト連携**
   - Persona 共通プロンプトと Building system instruction の更新。
   - Pulse のスナップショットにイベントを表示するロジックを追加。
6. **UI 拡張**
   - ワールドエディタに item 管理 UI を追加。CRUD 操作をテスト。
7. **最終統合テスト**
   - Persona が Building から item を取得 → inventory を表示 → use → Building に戻す、の一連の流れを確認。
   - 既存機能（会話・自律パルス・ログ）の退行がないか手動・自動テスト。

## 9. 今後の拡張余地
- item `type=book` を追加し、`use` で SAIMemory にページ内容を追記する。
- `container` タイプを作り、item 内に item を格納できるようにする。
- 共有／複数所持サポート (`quantity`, `stackable` フラグ) の導入。
- PersonaEventLog を汎用イベントバスとして拡張し、感情変化や外部通知にも転用する。

以上の手順で実装を進めれば、item を介した自律行動の基盤が整う想定です。
