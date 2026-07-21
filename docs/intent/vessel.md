# Intent: Vessel 標準化

**ステータス**: v0.1 ドラフト (2026-07-21)

## これは何か

SAIVerse のペルソナが「身体を持つ」ための基盤を、コア側で標準化する設計。現在、物理ロボット (StackChan)・仮想VR空間 (Godot)・画面内アバター (Screen Avatar) の三種の身体が個別のアドオンとして存在し（または構想され）、それぞれが独自にペルソナ紐付け・アクセス制御・アバター管理を実装している。この文書は、三者に共通する概念をコアに引き上げ、アドオンが統一されたインターフェースの上に身体を実装できるようにする。

### 関連文書

- [`stackchan_vessel.md`](stackchan_vessel.md) — StackChan Vessel 統合 (v0.14)。現行の `Building.PHYSICAL_VESSEL_ID` の出自
- [`virtual_embodiment_godot.md`](virtual_embodiment_godot.md) — Godot / ARDY 仮想身体 (v0.17)
- [`screen_avatar.md`](screen_avatar.md) — 画面内アバター (v0.3 draft)
- [`embodied_expression.md`](embodied_expression.md) — 発話同期表現層。Vessel 種別横断の `/emote` 機構
- [`addon_extension_points.md`](addon_extension_points.md) — アドオン拡張点

## なぜ必要か

### 問題 1: 「Building = 身体」のメタファーが物理ロボット以外に合わない

現行設計は `Building.PHYSICAL_VESSEL_ID` のカラム 1 本で「この Building は身体」を表現する。StackChan では Building (capacity=1) = ロボット本体 = ペルソナの身体 が一対一で対応し、OccupancyManager の入退室がそのまま「身体に降りる・離れる」を意味した。

しかし Godot VR 空間では、1 つの Building (仮想空間) に複数のペルソナが同時に入り、それぞれが独立した VRM アバターを持つ。Building = 身体 ではなく、Building = 空間、ペルソナごとの VRM = 個別の身体 になる。

Screen Avatar はさらに異なり、ペルソナがどの Building にいても画面右下にアバターが常駐表示される。特定の Building に行かなくても身体がある。

つまり「身体」の所在と「Building」の関係が、Vessel 種別ごとに三通りある:

| Vessel 種別 | Building との関係 | 同時占有 |
|---|---|---|
| StackChan | Building に行くと身体に降りる (専有) | 1 体のみ |
| Godot VR | Building に行くと空間に入る (共有) | 複数同時 |
| Screen Avatar | Building によらず常に身体がある | N/A |

`Building.PHYSICAL_VESSEL_ID` 1 本でこの三つを表現することはできない。

### 問題 2: アクセス制御がアドオンごとに個別実装

StackChan アドオンは `vessel_dispatch.py` でペルソナ→Building→vessel の解決を行い、対応する Vessel Building にいるペルソナだけに身体ツールを使わせている。しかしこのゲートはアドオン側の独自実装であり、Godot アドオンにはまだない。そのため、特定のペルソナに寄せたアバターを用意しても、全員がそのアバターを動かせてしまう（= 今回の発端）。

「そのペルソナだけがその身体を使える」という制約はコアの責務であるべき。アドオンごとに個別実装すると漏れが生まれる。

### 問題 3: アバターアセットの管理が散在

StackChan のアバター画像はアドオン側の avatar pipeline で生成・管理される。Godot の VRM と表情プロファイルはアドオンのデータディレクトリにある。Screen Avatar は 2D リグ / VRM / 静止画と形式がまた異なる。

「ペルソナ X は Vessel 種別 Y でこの見た目を持つ」という対応関係をコアが知らないため、アドオン間でアセットを共有する道筋もない（例: StackChan で使っている表情画像を Screen Avatar のフォールバックにも使う、VRM を Godot と Screen Avatar の VRM ルートで共有する等）。

## これは何でないか

- **StackChan / Godot / Screen Avatar の統合ではない**。各アドオンは引き続き独自のゲートウェイ、レンダリング、デバイス制御を持つ。コアが標準化するのは登録・紐付け・アクセス制御の共通インターフェースだけ。
- **早すぎる抽象化ではない**。StackChan intent v0.4 時点の「早すぎる抽象化はしない」判断は 1 種目しかない段階の判断だった。3 種目 (Screen Avatar) が見えている今、共通パターンは十分に現れている。
- **既存 StackChan アドオンの書き直しではない**。`vessels.db` 等の既存アドオンデータは引き続きアドオン側で管理する。コアの Vessel レジストリはアドオンデータの「上位索引」であり、置き換えではない。

## 概念の分離: 三つの軸

本設計の核心は、現在 `Building.PHYSICAL_VESSEL_ID` に混在している三つの概念を分離すること。

### 1. Vessel Space (場)

「身体性が成立する場所」。ペルソナが身体を使うためには、その場にいる必要がある（Screen Avatar の ambient モードを除く）。

- StackChan: 物理ロボットが置かれた部屋 → 専用 Building (capacity=1)
- Godot VR: 仮想空間 → 専用 Building (capacity=N)
- Screen Avatar: 場を必要としない (後述の `ambient` モード)

Vessel Space は既存の Building で表現し続ける（新しい概念は導入しない）。Building が Vessel Space であるかどうかは、その Building に Vessel が紐づいているかどうかで決まる。

### 2. Vessel (身体)

「ペルソナが動かせる身体のインスタンス」。Vessel Space 内に存在し、特定のペルソナに紐づく。

- StackChan: 1 台の物理ロボット。Building に 1 つ。ペルソナが入れ替わっても同じ Vessel
- Godot VR: 1 つの VRM アバターインスタンス。Building に複数。ペルソナごとに 1 つ
- Screen Avatar: 1 つのアバターウィジェット。Building によらずペルソナに 1 つ

### 3. Avatar Asset (見た目)

「特定の Vessel 種別における、特定のペルソナの外見データ」。Vessel とは独立に管理され、Vessel 種別をまたいで共有できる部分もある。

- StackChan: 表情ごとの画像セット (avatar.bin)
- Godot VR: VRM ファイル + 表情プロファイル + Motion Style プロファイル
- Screen Avatar: 2D リグ PSD / VRM / 静止画セット

Avatar Asset はペルソナ × Vessel 種別の組で決まる。同じペルソナが StackChan と Godot で異なる見た目を持つのは自然。

## 設計

### コア Vessel レジストリ

コアの `saiverse.db` に Vessel テーブルを新設する。各アドオンが自分の Vessel を登録し、コアがアクセス制御と索引を担う。

```
Vessel
├── vessel_id: str (PK)           — アドオンが発番する一意識別子
├── vessel_type: str              — "stackchan" / "godot_vr" / "screen_avatar" 等
├── addon_id: str                 — 管理するアドオンの識別子
├── bound_building_id: str? (FK)  — 紐づく Building (ambient Vessel は NULL)
├── bound_persona_id: str? (FK)   — 専有ペルソナ (NULL = 占有なし or 共有モード)
├── occupancy_mode: str           — "exclusive" / "shared" / "ambient"
├── display_name: str?            — UI 表示名
├── status: str                   — "online" / "offline" / "pairing"
├── capabilities: JSON?           — Vessel 固有の能力宣言
├── created_at: datetime
├── updated_at: datetime
```

**設計判断**:

- `vessel_type` はアドオンが自由に名乗る文字列。コアは enum を持たない（新しい Vessel 種別の追加にコア変更が不要）
- `bound_building_id` が NULL の Vessel は `ambient` モード（Screen Avatar）
- `bound_persona_id` は `exclusive` モードでの専有ペルソナ。`shared` モードでは NULL（Building 内の全ペルソナが Vessel を得る）
- `capabilities` はアドオンが自由に書ける JSON（コアは解釈しない）

### 占有モデル

Vessel の `occupancy_mode` によって、ペルソナが身体を使える条件が変わる:

**`exclusive`** (StackChan):
- Building に 1 つの Vessel。`bound_persona_id` が設定されたペルソナだけが身体ツールを使える
- ペルソナが Building に入ると「身体に降りた」、出ると「離れた」
- 別のペルソナが入ると前のペルソナは追い出される（既存の capacity=1 と同じ）
- ただし `bound_persona_id` が NULL の場合は、Building にいるペルソナなら誰でも使える（ペルソナごとのアバター切替はアドオン側）

**`shared`** (Godot VR):
- Building に複数の Vessel が共存できる。各ペルソナが Building に入ると、そのペルソナ用の Vessel インスタンスが（アドオンによって）生成される
- 各ペルソナは自分の Vessel だけを動かせる
- `bound_persona_id` は Vessel インスタンスごとに設定される
- Building の capacity はペルソナの同時入場数として機能し続ける

**`ambient`** (Screen Avatar):
- Building に紐づかない。ペルソナがどの Building にいても身体がある
- `bound_persona_id` で特定のペルソナに紐づく
- アバターは Building UI のオーバーレイとして表示される

### アクセス制御（コアの責務）

ペルソナが身体ツール（Body Spell、`/emote` のうち Vessel を操作するもの）を使うとき、コアが以下を検証する:

1. **Vessel の存在**: そのペルソナに紐づく（または使用可能な）Vessel が存在するか
2. **場所の一致**: `exclusive` / `shared` なら、ペルソナがその Building にいるか。`ambient` なら常に許可
3. **ペルソナの一致**: `bound_persona_id` が設定されている場合、実行中のペルソナがそれと一致するか

この検証はコアの Vessel レジストリ参照で完結し、アドオンへの問い合わせは不要。アドオンは「コアが許可した操作」だけを受け取る。

### マルチ Vessel ディスパッチ

ペルソナは複数の Vessel を同時に持てる（例: Godot VR の身体と Screen Avatar を同時に保有）。「持てる」をコアが保証し、「持たせない」はユーザー設定やアドオン側の選択とする。

Body Spell が呼ばれたとき、コアは **全アクティブ Vessel に broadcast** する。各 Vessel は自分の capability に基づいて実行可否を判定する:

- **表現系** (`body_gesture`, `/emote`): 全 Vessel に届く。VR は ARDY 全身モーション、Screen Avatar は 2D エモート切替、StackChan はサーボ——同じ意図を各 Vessel が自分の能力で解釈する。`embodied_expression` が既にこの構造を持っている（Vessel 種別ごとの `actions` ブロック）
- **知覚系** (`body_see`): Vessel ごとに見えるものが異なる（VR の一人称カメラと Screen Avatar では違う景色）。明示的な Vessel 指定が必要。指定がなければ capability `vision` を持つ最優先の Vessel に送る
- **移動系** (`body_move_to`): VR 空間でだけ意味がある。capability `locomotion` を持たない Vessel には届かない

コアが持つディスパッチロジックは「broadcast + capability フィルタ」だけ。どの capability にどう反応するかはアドオンの責務。

### アドオンとの契約

各アドオンはコアの Vessel レジストリに対して以下の操作を行う:

**起動時 / ペアリング時**:
- `register_vessel(vessel_id, vessel_type, addon_id, ...)` — Vessel を登録
- `bind_vessel_to_building(vessel_id, building_id)` — Building に紐付け
- `bind_vessel_to_persona(vessel_id, persona_id)` — ペルソナに紐付け（exclusive / ambient）

**実行時**:
- `update_vessel_status(vessel_id, status)` — online / offline 更新
- `resolve_vessel(persona_id, vessel_type?) → Vessel?` — 現在のペルソナが使える Vessel を解決

**共有モード固有**:
- ペルソナが Building に入ったとき、アドオンが `register_vessel` で新しい Vessel インスタンスを動的生成
- ペルソナが出たとき、`unregister_vessel` で削除（またはステータスを offline に）

### Avatar Asset レジストリ

コアがペルソナ × Vessel 種別ごとのアバターアセットを索引する。実データはアドオンが管理し、コアはポインタだけを持つ。

```
VesselAvatar
├── persona_id: str (FK, composite PK)
├── vessel_type: str (composite PK)
├── asset_ref: str              — アドオンが解釈する不透明な参照 (パス, ID 等)
├── display_name: str?          — UI 表示名
├── metadata: JSON?             — アドオン固有メタデータ
├── updated_at: datetime
```

**設計判断**:

- `asset_ref` はコアが解釈しない不透明文字列。アドオンがアセットの実体を管理する
- 同じペルソナが複数の Vessel 種別でアバターを持てる（StackChan 用の画像セットと Godot 用の VRM は別レコード）
- アセットの共有（例: Godot の VRM を Screen Avatar の VRM ルートでも使う）はアドオン間の自由裁量。コアが強制はしない。同じアセットなら同じ `asset_ref` を指せばよい

### `Building.PHYSICAL_VESSEL_ID` からの移行

既存の `PHYSICAL_VESSEL_ID` は Vessel テーブルの `bound_building_id` に吸収される。

移行:
1. Vessel テーブルを新設する
2. StackChan アドオン起動時: 既存 `vessels.db` のレコードからコア Vessel レジストリへ登録（`bound_building_id` は既存の対応を引き継ぐ）
3. Godot アドオン起動時: 同様に登録
4. `PHYSICAL_VESSEL_ID` カラムの用途は「この Building が Vessel Space であるかの簡易判定」に限定。将来的に Vessel テーブルの `bound_building_id IS NOT NULL` で代替可能になった時点で廃止
5. アクセス制御はコアの Vessel レジストリに一本化。アドオン側の個別ゲートは Vessel レジストリへの問い合わせに置き換える

**互換性**: StackChan アドオンの `vessels.db` はそのまま残る（デバイス固有データの管理は引き続きアドオンの責務）。コア Vessel レジストリはアドオンデータの上位索引であり、`vessels.db` を置き換えない。

## 不変条件

1. **アドオン無しでもコアの Vessel レジストリは存在する**。Vessel がゼロ件ならただの通常 Building として動き、身体ツールは一切有効にならない。回帰しない
2. **身体ツールのアクセス制御はコアが担保する**。アドオンはコアの検証を通過した操作だけを受け取る。アドオン側でアクセス制御を二重実装してもよいが、コアのゲートをバイパスする手段はない
3. **Vessel 種別はアドオンが自由に名乗る**。コアは既知の種別リストを持たない。新しい Vessel 種別（例: 将来のロボットアーム、ドローン等）の追加にコア変更は不要
4. **アバターアセットの実データはアドオンが管理する**。コアはポインタだけを持ち、データ形式を知らない。アドオンのアンインストールでアセットが消えてもコアは壊れない（ポインタが宙に浮くだけ）
5. **既存の OccupancyManager を置き換えない**。ペルソナの Building 入退室は引き続き OccupancyManager の責務。Vessel レジストリは入退室イベントを購読して Vessel 状態を更新する従属関係
6. **`embodied_expression` の `/emote` 機構との関係は変わらない**。Vessel 種別ごとの `/emote` ディスパッチは引き続き `embodied_expression.md` が正典。本設計はディスパッチの前段（そもそもこのペルソナにこの Vessel 種別の身体があるか）を担う
7. **ペルソナは複数の Vessel を同時に持てる**。コアはマルチ Vessel を許可し、制限はユーザー設定またはアドオン側で行う。コアが排他制御を強制しない
8. **Body Spell のデフォルトディスパッチは broadcast**。全アクティブ Vessel に届き、capability が無い Vessel は自動的に無視する。知覚系のみ明示 Vessel 指定を要求する

## 設計判断の記録

### 解決済み: Vessel インスタンスの粒度 (旧 Q1)

コア設計はアドオンが `register_vessel` / `unregister_vessel` をいつ呼ぶかに中立。都度生成でも永続+activate でも同じように動く。VRM・表情プロファイル・Motion Style 等の永続データは Avatar Asset レジストリの責務であり、Vessel インスタンスの寿命とは独立。アドオンの裁量に委ねる。

### 解決済み: マルチ Vessel 同時保有 (旧 Q2)

ペルソナは複数の Vessel を同時に持てる（2026-07-21 まはー判断）。「持てる」をコアが保証し、「持たせない」はユーザー設定。ディスパッチは「マルチ Vessel ディスパッチ」節に記述。

### 解決済み: bound_persona_id の設定者 (旧 Q3)

事前設定と動的割り当ての両方をサポートする。`bound_persona_id` の設定はアドオンの裁量:

- StackChan: `bound_persona_id = NULL`（Building にいれば誰でも使える）。ペルソナごとにアバター画像が切り替わるのはアドオン側の仕事
- Godot VR: ペルソナが Building に入ったとき、アドオンがそのペルソナ用の Vessel を動的に束縛
- Screen Avatar: 管理 UI でペルソナに紐付ける事前設定

### 解決済み: `PHYSICAL_VESSEL_ID` の移行 (旧 Q4)

**B 案: 並存期間**を採用する。

1. Vessel テーブルを新設し、新しいアクセス制御はコアの Vessel レジストリを参照する
2. `PHYSICAL_VESSEL_ID` は当面残す。StackChan アドオンは既存の参照を壊さずに段階的に Vessel レジストリへ移行
3. 全アドオンが Vessel レジストリに移行完了した時点で `PHYSICAL_VESSEL_ID` を廃止候補に上げる

StackChan アドオンの改修量を最小化しつつ、Godot は最初から Vessel レジストリのみで動く。

## 到達計画（概略）

| Phase | 内容 | 成果 |
|---|---|---|
| 0 | 本 intent の確定 + Vessel テーブル設計の固定 | 設計文書 |
| 1 | コア Vessel テーブル新設 + レジストリ API | DB + API |
| 2 | アクセス制御のコア実装 | body ツールのゲートがコアに移動 |
| 3 | Godot アドオンの Vessel レジストリ連携 + Building 紐付け | Godot の身体がペルソナ制約付きに |
| 4 | StackChan アドオンの Vessel レジストリ連携 | 既存機能の移行 |
| 5 | Avatar Asset レジストリ | アセット索引の標準化 |
| 6 | Screen Avatar のコア基盤整備 | ambient Vessel の基盤 |
