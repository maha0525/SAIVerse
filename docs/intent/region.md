# Region — 空間スコープと入口 (Intent Document)

- Status: v0.2 (2026-06-12) — interview 反映済み、確定
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

- **マップ**: 各スコープのマップに表示されるのは「自スコープ直属の Building + 子スコープの入口」。City マップには Region の入口だけが載り、内部 Building は載らない。Region 内マップには内部 Building と SubRegion の入口が載る。上の階層へ戻る操作は UI のナビゲーション (エクスプローラーの ↑ に相当するボタン) で提供し、内部マップに「出口 Building」は置かない
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

### 3.1 入口の出自 (自動生成か、ユーザー指定か) を ID の形で判定している

`delete_region` は「自動生成された入口は Region と運命を共にする / ユーザー指定の
既存 Building は残す」を、**入口の ID が `entrance_<region_id>` の形かどうか**だけで
判定している。出自そのものはどこにも永続化されていない。

2026-08-09 に、この判定でユーザー所有の Building が巻き添えで削除されうることが
レビューで指摘された (ユーザーが `region_id="r1"` と `entrance_building_id="entrance_r1"`
を同時に指定した場合)。**当面の措置として、作成時に予約名 (`entrance_<region_id>`)
を明示入口に使うことを拒否し、曖昧さの供給源を塞いだ**。既存データに同じ組み合わせが
あれば依然として削除される。

本筋は Region 側に出自 (auto / explicit) を持たせて判定をそれ一本にすることで、
これは列の追加を伴うため未着手。名前の予約はその代用であり、判定の正しさを
保証するものではない。

不変条件 2 の執行点 (W7 柱5 / 分離監査 P1-6、2026-07-21): `update_region` の
parent 変更は入口 Building の REGION_ID を**同一トランザクション**で新しい親
スコープへ同期する (top 化なら City 直下 = None)。`set_building_region` は
対象がいずれかの Region の入口なら拒否する — 入口の所属を変えられるのは
Region service (create/update/delete_region) だけ。回帰: tests/test_region_admin.py。

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

## 6. interview で確定した事項 (2026-06-12)

1. **入口の自動生成**: create_region (汎用) が入口 Building「(Region名): 入口」を自動作成する。入口必須の不変条件を作成フローで保証する。ゲーム Region は create_ruler が控室を自動作成する現行フローを踏襲 (控室 = 入口)
2. **SubRegion の入口生成**: game_create_subregion が入口 Building を自動作成して紐付ける。命名は汎用性を優先し「(SubRegion名): 入口」(「門」などゲーム固有の語をシステム側で決め打ちしない)
3. **既存データの移行**: 行わない。入口を持たない既存 Region / SubRegion (テストデータ) は作り直す
4. **マップ座標系**: MAP_X/MAP_Y はスコープローカル座標として再解釈する (内部 Building はそのスコープのマップにしか出ないため衝突しない)。Region にも背景画像欄を持たせる。内部マップに出口ノードは置かず、上階層へ戻るのは UI の ↑ ボタン
5. **entry policy の語彙**: open / whitelist (ゲームの参加者ゲートが該当) / locked (家の鍵) の 3 値から始める。鍵の開閉は住人ペルソナの tool を想定 (本書スコープ外、policy の置き場だけ確保)
6. **拒否時のフィードバック**: 内部 Building への直接移動を拒否する際、入口 Building を案内するメッセージを返す

## 7. 実装ノート (確定済みの判断)

- ゲーム Region では**控室 = 入口を兼用**する。Ruler の私室を控室と分けたくなったら Ruler 生成側だけ修正すればよい (2026-06-12 確定)
- ゲーム外での UX (ゲーム Region): 参加中ゲームが playing / paused のとき、`/api/user/status` の active_game を**現在地に関わらず**返す (`inside` / `at_entrance` フラグ付き)。Region 外なら UI はどこに居ても read-only セッションログ閲覧トグル + 復帰ボタンを表示する。場所要件を課さないのは、復帰の認可が参加者資格そのものであること、および UI 上のユーザー移動は発言を伴う (= 復帰のためだけに控室で一言喋らせる手順は無意味) ため (2026-06-12 確定)。復帰ボタンは party_location への system 移動を発行し、既存のパーティー追従 + 自動再開チェーンがそのまま発火する。トポロジー (通常移動の入口経由強制・entry policy) はこの判断と独立に維持される
- City マップ API (`/api/info/city-map`) とサイドバー (`/api/user/buildings`) は Region 所属情報での絞り込み / グルーピングが必要になる
