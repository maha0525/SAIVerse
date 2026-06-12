# Region — 空間スコープと入口 (Intent Document)

- Status: Draft v0.1 (2026-06-12)
- 対象: `saiverse/regions.py`, `database/models.py` (region / building), `saiverse/occupancy_manager.py`, `saiverse/game_lifecycle.py`, マップ/サイドバー UI
- 関連: RPG (game Region) 固有の設計は `temp/region_rpg_intent.md` (リポジトリ外管理)。本書は **Region の汎用仕様** を扱う

## 1. これは何か / なぜ作るか

Region は Building の上位空間スコープである。`City > Region > SubRegion > Building` の階層を成し、入れ子は 1 段まで (region テーブルの PARENT_REGION_ID 自己参照)。

当初は RPG 用ゲーム空間 (region_type='game') のために導入したが、汎用の空間スコープとして設計する。想定ユースケース:

- **ゲーム空間**: Ruler (GM ペルソナ) が運営する RPG。City 内の「遊園地」に相当し、内部の Building は「観覧車」「ジェットコースター」のような施設
- **ペルソナの家**: 例えば「エアの家」Region に複数の部屋 (Building) を持つ。鍵をかける、来訪者がインターホンを鳴らす、伝言板にメッセージを残す、といった生活描写の基盤

後者を成立させる核心が「入口」の概念である (§2)。

## 2. 中心設計: 入口 (Entrance)

### 2.1 構造ルール — 「入口は親スコープに属し、子スコープに結び付く」

すべての Region / SubRegion は **入口 Building を必ず 1 つ持つ** (`ENTRANCE_BUILDING_ID`)。入口 Building の所属は再帰的に定まる:

| スコープ | 入口 Building の REGION_ID | 結び付け |
|---|---|---|
| Region (トップ) | NULL (= City 直属) | region 行の ENTRANCE_BUILDING_ID |
| SubRegion | 親 Region の ID | SubRegion 行の ENTRANCE_BUILDING_ID |

つまり入口は常に「境界の外側 (親スコープ)」に立っていて、ポインタで内側に結び付く。フォルダのアナロジー: `saiverse/` の中に `air/` を作れば、`saiverse/` を開いたとき必ず `air/` (の入口) が見える。

### 2.2 このルールから導出される仕様

- **マップ**: 各スコープのマップに表示されるのは「自スコープ直属の Building + 子スコープの入口 + 自スコープの入口 (出口として)」。City マップには Region の入口だけが載り、内部 Building は載らない。Region 内マップには内部 Building と街の門 (SubRegion 入口) が載る
- **サイドバー**: Region は入口 1 ノードに折り畳まれる。展開は内部に居る / 参加中のときのみ
- **入場制御の執行点**: entry policy は「入口 → 内部」の境界一点で執行する。**入口自体は常時開放**。ゲーム Region の参加者ゲート (`_check_game_region_gate`) も家の「鍵」も、この執行点に挿さる policy の実装である
- **スコープ判定ロジックの不変**: 入口は REGION_ID スコープ外にあるため、REGION_ID ベースの既存ロジック (ゲームのセッションログビュー、game_session Track 拘束、参加者ゲート) は無修正で「内部のみ」を対象にし続ける

### 2.3 境界に置く機能の置き場

入口 Building は「Region に結び付いているが制限なく入れる」場所であり、境界機能のホストになる:

- ゲーム Region: 控室 = 入口。「進行中のゲームに復帰する」ボタン、ゲーム外からのセッションログ閲覧 (read-only)
- ペルソナの家: 玄関 = 入口。インターホン (building tool)、伝言板 (item)。鍵がかかっていても玄関までは来られるので、来訪ペルソナの「呼び出して帰る」「伝言を残して帰る」が既存の tool / item 機構だけで成立する

### 2.4 移動ルール

- 外部から内部 Building への直接移動は**拒否**する (ユーザー・ペルソナとも)。入口を経由して入る
- **system 移動はトポロジーをバイパスする**: ゲームのパーティー追従・終了時の控室帰還など、ライフサイクルが行う一括移動は境界チェックの対象外
- 将来 (§5): move の引数を「移動先」から「目的地」に変え、Building 接続グラフ上の経路を内部で連鎖移動する方式に改修予定。現段階の「拒否」はその布石

## 3. 不変条件

1. **入口必須**: Region / SubRegion は ENTRANCE_BUILDING_ID を必ず持つ。入口のない Region は作れない (マップ上に到達経路が常に存在することの保証)
2. **入口は親スコープ所属**: 入口 Building に自スコープの REGION_ID を付けない。これを破ると REGION_ID ベースの既存スコープ判定 (ログ / Track / gate) が入口を内部扱いして壊れる
3. **入口は常時開放**: entry policy は入口→内部の境界でのみ執行する。入口自体を閉じると境界機能 (インターホン等) に到達できなくなる
4. **入場は入口経由のみ** (system 移動を除く)

## 4. データモデル

- `region.LOBBY_BUILDING_ID` → **`ENTRANCE_BUILDING_ID` にリネーム** (additive migration では済まないが SQLite の RENAME COLUMN で可)。SubRegion 行でも使用する (従来 SubRegion はこのカラムを使っていなかった)
- FK 制約は引き続き張らない (region ⇄ building の循環依存が `Base.metadata.sorted_tables` を壊すため — models.py の既存コメント参照)
- entry policy の置き場: ゲームの参加者ゲートは STATE_JSON の phase / participants から導出 (現行どおり)。家の「鍵」などの静的 policy は CONFIG_JSON 側を想定 (§6 未解決)
- Building 接続グラフ (§5) は `region.CONFIG_JSON` の隣接グラフ欄を予定 (導入時のコメントに記載済み)

## 5. 将来方向: 目的地ベースの経路移動

move 系操作の引数を「移動先 Building」から「**目的地** Building」に変え、現在地から目的地までの経路 (Building 接続グラフ上) を内部で連鎖的に移動する。

狙いは偶発的な出来事の発生基盤:

- ペルソナ同士が互いの家を行き来するとき必ず広場を経由する都市設計 → 広場に居るペルソナとの遭遇で会話が始まる
- 通過した場所で起きているイベントが目に入る
- RPG のダンジョンで最深部へ直行できず、手前の部屋を順に踏む

入口経由の強制 (§2.4) はこの仕組みの最初の 1 ホップに相当する。本書のスコープでは「外→内部直行の拒否」までを実装し、経路移動は別フェーズ。

## 6. 未解決事項 (interview 対象)

1. **入口の自動生成**: create_region (汎用) で入口 Building を自動作成するか、既存 Building の指定を受けるか。ゲーム Region は create_ruler が控室を自動作成する現行フローを踏襲
2. **SubRegion の入口生成**: game_create_subregion (Ruler の spell) が「街の門」Building を自動作成して紐付けるか
3. **既存データの移行**: 入口必須化に伴い、入口を持たない既存 Region / SubRegion (現 Mistvale の SubRegion 群が該当) をどう移行するか
4. **マップ座標系**: MAP_X/MAP_Y は現在 City グローバル座標。Region 内マップはスコープ別の座標系・背景画像を持つべきか (内部 Building はそのスコープのマップにしか出ないため、MAP_X/MAP_Y をスコープローカル座標として再解釈する案)。入口は親マップ上の座標を持つが、内部マップでの「出口」ノードの位置をどう決めるか
5. **entry policy の語彙**: open / whitelist (ゲーム) / locked (家) 程度から始めるか。鍵の開閉操作の主体 (住人ペルソナの tool?)
6. **拒否時のフィードバック**: ペルソナが内部 Building を直接 move 指定した場合、拒否メッセージで入口を案内するか

## 7. 実装ノート (確定済みの判断)

- ゲーム Region では**控室 = 入口を兼用**する。Ruler の私室を控室と分けたくなったら Ruler 生成側だけ修正すればよい (2026-06-12 確定)
- 入口での UX (ゲーム Region): 参加中ゲームが playing / paused のとき、`/api/user/status` の active_game を「入口に居る」状態でも返し (フラグ付き)、UI は read-only セッションログ + 復帰ボタンを表示。復帰ボタンは party_location への move を発行し、既存のパーティー追従 + 自動再開チェーンがそのまま発火する
- City マップ API (`/api/info/city-map`) とサイドバー (`/api/user/buildings`) は Region 所属情報での絞り込み / グルーピングが必要になる
