# Intent: Unit 配置 — ハブ channel + ラベル + 同アドレス複数ユニット対応

**状態**: v1.0 実装済み (2026-07-03)・実機検証待ち。§11 の設計判断 a〜e + `version`
フィールド + 多段ハブ拡張余地 (§13) を実装。バックエンド (データモデル / per-channel
select / 全ユニット channel 対応 / ToF 複数インスタンス + label / 可視性 / body_status /
`POST /vessels/{id}/unit-config` + 検証) は自動検証済み。UI (配置エディタ) は tsc 0
エラー。**残 = 実機で ch3/ch4 の 2×VL53L1X が混線なく個別に読めることの確認**。
**関連**:
- `docs/intent/stackchan_extension_modules.md` §D「I2C MUX (PaHUB) ハブ経由 Unit 対応」— per-channel select を *future work* と明記していた箇所。本 doc がその future work の設計。
- `docs/intent/stackchan_vessel.md` K-5「capability カタログ」/ 不変条件 #14 — capability = 「有効ユニット集合 + ハブ構成」を per-vessel で持つ、という上位設計。本 doc はその「ハブ構成」を具体化する。
- 実装: `expansion_data/saiverse-stackchan-addon/tools/hubs/pahub.py` / `tools/units/*.py` / `vessel_manager.py` / `api_routes.py` / `ui/Panel.tsx`
- 経緯: `docs/issues/stackchan_unit_capability_requires_restart.md`

---

## 1. なぜ (問題)

同じ Stack-chan (vessel) に、**I2C アドレスが同じユニットを複数**挿したい要求が出た。
最初の実例は ToF 距離センサー (VL53L1X) を 2 個 — ただし **ch3/ch4・前方下向き・崖検知
はあくまで一例**であり、ユーザーは任意のチャンネル・任意の個数・任意の用途 (向きも
固定でない) で挿す。実装はこの一般形を満たさなければ意味がない (汎用基盤を先に作る)。

現状はこれが成立しない。理由が下から 3 層積み重なっている:

1. **PaHub が「全 channel 同時 open」しかできない** (`pahub.py`、`open_all_channels`
   = 制御 register に 0xFF)。VL53L1X はデフォルト I2C アドレスが全個体 `0x29` で同じ
   なので、2 個が同時にバスに乗ると I2C の wired-AND でデータが潰れ合い、**エラーでは
   なく「それっぽく狂った値」**が返る。→ **per-channel select** (`1<<ch` で 1 本ずつ
   選ぶ) が要る。これは `stackchan_extension_modules.md` §D で *future work* と名指し
   されていた拡張そのもの。

2. **ハブ構成が addon 単一グローバル** (`addon.json` の `hub_type` / `hub_addr_a0..a2`
   が全機体で 1 個)。しかし `stackchan_vessel.md` K-5 は「per-vessel に有効ユニット集合
   **+ ハブ構成**を持つ」と既に定めている。現行実装がショートカットで乖離していた。
   → **per-vessel 化** (intent への回帰)。

3. **「同じユニットが複数」という概念が無い**。`get_tof_distance` は `0x29` を 1 個
   叩くだけで、「どのチャンネルの ToF か」「用途 / 向き」を表す場所がない。

## 2. スコープ

### やること (今回)
- per-vessel の **ユニット配置 (unit placement)** モデル: 「どのチャンネルに・何の
  ユニットを・どんなラベルで挿したか」を機体ごとに宣言する。従来の bool capability を
  これに一本化する (①B)。
- PaHub の **per-channel select** (`select_channel(ch)`)。ハブ経由ユニットは自分の
  channel だけ open してから I2C する。
- **同アドレス複数ユニット**が別 channel で混線せず読める。
- ToF スペルを **ラベル指定 (target) で個別に読む**形にする (②b)。
- **機体管理 UI** に配置エディタ (channel → unit → label)。グローバル hub params と
  bool capability トグルを置換。
- **移行**: 既存 bool capability + グローバル hub 設定 → per-vessel 配置。

### やらないこと (今回の非スコープ)
- ⛔ **崖検知そのもの (自動検知 + ルールベース車輪制御)**。まはー判断: 崖検知は
  ペルソナがスペルで距離を読む方式では成立しない。**LLM を介さない反応制御ループ**
  (ToF を継続監視 → 落下を検知 → 車輪制御に即時反映) が要る別サブシステム。よって
  **スペル設計を「崖検知」に寄せない**。本 doc が用意する「構造化された測距 API」
  (§6) を将来その制御ループが下位経路として使える形にはしておくが、制御ループ自体は
  別 intent で扱う。
- ⛔ PaHub 以外の MUX 種別 (TCA9543A / PCA9544A 等)。必要になったら別ドライバ
  (`stackchan_extension_modules.md` §D の方針: MUX 一般抽象は作らない)。
- ⛔ 多段ハブ (ハブの下にハブ)。1 vessel = Port A に 1 ハブまで。ただし将来の拡張余地は
  スキーマ側で確保する (§13。今は実装しないが箱にはハマらない形にしておく)。

## 3. データモデル: per-vessel ユニット配置 (①B)

vessel ごとに以下を持つ (vessels.db に JSON で永続。詳細な列設計は §9 移行)。

```jsonc
{
  "version": 1,                                 // スキーマ版。将来の多段ハブ等の
                                                // 進化を構造推測でなくバージョンで分岐する
  "hub": { "type": "pahub", "addr": "0x71" },   // ハブ無しなら { "type": "none" }
  "units": [
    { "type": "tof",  "channel": 3, "label": "前方左" },
    { "type": "tof",  "channel": 4, "label": "前方右" },
    { "type": "env3", "channel": 0, "label": "" }
  ]
}
```

- **`units[]`** が真実の source。各要素 = 1 個の物理ユニット。
  - `type`: ユニット種別 (`env3` / `servo8` / `sonic` / `tof` / …)。unit driver の
    `MY_UNIT_CAP_KEY` と一致。
  - `channel`: ハブ配下のチャンネル番号 (0-5)。`hub.type == "none"` のときは無視 (直結)。
  - `label`: ユーザー定義の用途 / 向き (「前方左」等)。同 type が複数あるときの
    識別子も兼ねる (§6, §11-a)。
- **`hub`**: vessel の Port A に挟んだハブ (単段前提)。`type=none` なら直結。`addr` は
  物理 A0/A1/A2 パッドから算出した `0x70..0x77` (現行 UI と同じ物理パッド指定を踏襲)。
- **`version`**: config スキーマ版 (現行 `1`)。多段ハブ等でスキーマが進化したとき、
  古い行を構造推測でなくバージョンで判別して migration するための布石 (§9 / §13)。
- **ユニット可視性** (不変条件 #14) は `units[]` から導出: 「type T のユニットが
  `units[]` に 1 件以上あれば」その type のスペルが可視。これで従来の
  `capabilities: {T: bool}` を包含する (bool = 「その type が 1 件以上あるか」)。
- `vessel_dispatch.list_building_ids_with_capability(T)` は「`units[]` に type==T を
  含む vessel の building」を返すよう内部を差し替える (シグネチャは維持)。

## 4. per-channel select 設計

PaHub に channel 選択を追加する (`stackchan_extension_modules.md` §D の future work)。

- `PaHub.select_channel(ch: int)`: 制御 register に `1 << ch` を書き、**その channel
  だけ** open にする (他は close)。TCA9548A 仕様: 単一 channel 選択可。
- ハブ経由ユニットの各 I2C 操作は「**自分の channel を select → I2C 実行**」の順。
  channel の出所は §3 の `units[].channel`。
- **`open_all_channels` (0xFF) の位置づけ**: per-channel select が入ると、単一
  ユニット構成も「その 1 本を select」で足りるため、`open_all` は基本使わない。
  ただし *lazy recovery* の思想 (Stack-chan 再起動でハブが全 channel closed に戻る)
  は維持する: i2c-level failure 時に「対象 channel を select し直して再試行」する。
  `open_all` は「配置情報が無い旧構成」向けの後方互換フォールバックとしてのみ残す
  (§9)。
- **直結時 (`hub.type=none`)**: select 経路は完全にスキップ (現行と等価)。
- hub 抽象は `pahub.py` に閉じる。unit driver は「自分の channel を渡す」だけで、
  select の有無を意識しない (§D の方針: hub 種別の影響を driver に広げない)。

## 5. 同アドレス複数ユニットの成立条件

- 同アドレスユニットを複数挿すには **ハブが必須** (直結の 1 バスでは物理的に分離
  不可能)。モデル上「同 type / 同アドレスが 2 件以上 ⇒ それぞれ異なる channel」を
  UI + backend で強制する (§11-c で最終確認)。
- 測定は channel を切り替えながら逐次 (2 個なら select→測定 を 2 回)。レイテンシは
  個数に比例するが、少数なら許容。
- VL53L1X の init キャッシュは現在 vessel 単位。**(vessel, channel) 単位**に変更する
  (各物理センサーが別個に init を要するため)。

## 6. ToF スペル: ラベル addressable (②b)

まはー判断②b の理由: 将来の柔軟性。崖検知は別レイヤ (§2 非スコープ) なので、スペルは
「全部まとめて 1 文字列」ではなく **個別のセンサーを指定して読める**形にする。

- スペル: `get_tof_distance(target?: string)`。
  - `target` = 読みたいセンサーの **label** (「前方左」等)。
  - ラベルは vessel ごとに違う (ユーザー定義) ため、**スペル schema には静的 enum を
    焼かない**。`target` は自由文字列とし、**実行時**に現在 vessel の `units[]` の
    tof ラベル集合に照合して解決する (spell は元々実行時に vessel を解決するので同じ
    タイミング)。
  - `target` 省略時 / 未一致時の挙動は §11-b で確定 (案: 省略時は利用可能なラベル
    一覧を客観メッセージで返す。tof が 1 個だけなら省略でそれを読む)。
- **構造化測距 API を内部に持つ**: スペルが返す日本語文字列とは別に、
  `measure_tof(vessel, channel) -> {range_mm, status}` 相当の構造化関数を持ち、スペルは
  それを整形するだけにする。将来の崖検知制御ループ (§2 非スコープ) が、この構造化
  API を LLM / 文字列を介さず直接叩けるようにするための布石。
- 戻り値テキストはツール規約通り客観 + 丁寧語 (「前方左: 45mm (VL53L1X)。」)。

### 6.1 ペルソナからの発見可能性: `body_status` に配置を出す (まはー指摘)

②b は「ラベルで個別に読む」形だが、**ペルソナは自分の身体に何がどこに刺さっていて、
どんなラベルが付いているかを知らない**。ラベルを知らなければ `target` に何を渡せば
いいか分からず、addressable が絵に描いた餅になる。→ **`body_status` (共通身体ツール、
自分の身体状態を一括確認するスペル) に「搭載ユニット」セクションを追加**し、現在 vessel
の `units[]` をペルソナに見せる。

- 表示は **ペルソナ視点の語彙**に翻訳する: ユニットの表示名 + ラベル。例:
  ```
  【搭載ユニット】
    ToF 距離センサー: 前方左 / 前方右
    環境センサー (ENV III): 温湿度・気圧
  ```
- **channel 番号など物理配線の詳細は出さない** (ペルソナはラベルで呼ぶのであって
  channel を意識しない。§10 不変条件 3 と同じ思想を可視面にも適用)。
- これが §11-b の「`target` 省略時 / ラベル発見手段」の答えになる: ペルソナは
  `body_status` で使えるラベルを知り、`get_tof_distance(target="前方左")` を撃つ。
  ToF スペルの description にも「利用可能なラベルは body_status で確認できる」旨を
  1 文添える。
- `body_status` は既に `resolve_vessel_connection()` で現在 vessel を解決している
  (`_vessel, conn = resolve_vessel_connection()`) ので、その `vessel.<配置>` を読んで
  1 セクション足すだけで実装できる。

## 7. 崖検知の位置づけ (再掲・重要)

崖検知は本 doc の**成果物ではない**。本 doc は「同アドレス複数 ToF を混線なく個別に
読む汎用基盤 + ラベル指定スペル + 構造化測距 API」までを提供する。自動崖検知
(継続監視 → 落下判定 → 車輪制御への即時反映、LLM 非経由の反応制御) は別 intent。
スペル / capability / 配置モデルを崖検知の都合で歪めないこと。

## 8. 機体管理 UI (配置エディタ)

`ui/Panel.tsx` の per-vessel カードを拡張:
- **ハブ**: `type` (none / pahub) + 物理 A0/A1/A2 トグル (現行のグローバル hub params と
  同じ UX を per-vessel に移設)。
- **ユニット配置**: 行を追加/削除できるリスト。各行 = `type` (ドロップダウン) +
  `channel` (ハブ有効時のみ、0-5) + `label` (テキスト)。
- 従来の `CAPABILITY_OPTIONS` トグルは廃止 (配置リストが可視性の source になる)。
- API: `POST /vessels/{id}/capabilities` を配置全体 (`{hub, units}`) を受ける形へ拡張
  (または新 route)。保存後に `reregister_unit_tools()` を呼ぶ既存フローはそのまま
  (可視性の再起動不要化はバグ② 修正で導入済み)。

## 9. 移行

- vessels.db: 現行 `capabilities` TEXT(JSON、`{T: bool}`) から、`{version, hub, units}`
  を持つ新形へ。**新カラム `unit_config` を追加** (JSON) し、light migration で既存
  `capabilities` の true な type を `units: [{type, channel: null, label: ""}]` に展開し
  `version: 1` を刻む。hub は旧グローバル params があれば `hub` に移す (channel は不明
  なので null、ユーザーが UI で埋める)。`capabilities` カラムは移行期の読み取り互換の
  ため即削除しない。今後スキーマが進化したら `version` を上げ、読み取り側でバージョン
  分岐して再 migration する (§13)。
- addon.json のグローバル `hub_type` / `hub_addr_a*` params は非推奨化 → per-vessel に
  移ったら削除 (params_schema から除去、UI からも撤去)。
- `_KNOWN_CAPABILITIES` (api_routes) は「既知の unit type 集合」の意味で残すが、検証は
  配置リストの `type` に対して行う。

## 10. 不変条件

1. **ユニット可視性の source は per-vessel の `units[]`** (不変条件 #14 を配置モデルで
   実現)。「全 Vessel Building の無条件和集合」で可視性を決めない。
2. **同アドレス複数ユニットは必ず別 channel** (ハブ必須)。
3. **hub 種別の特殊性は `pahub.py` に閉じる**。unit driver は channel を渡すだけ。
4. **スペル / 配置モデルを崖検知の都合で特殊化しない** (§7)。
5. **構造化測距と表示整形を分離** (§6) — 将来の非 LLM 制御ループのため。
6. capability 変更は再起動不要で反映 (既存 `reregister_unit_tools`)。
7. **配置はペルソナから発見可能** (§6.1) — `body_status` にラベルを出す。ペルソナが
   知り得ない情報で addressable スペルを撃たせない。
8. **ハブの「channel 選択 + 測定」は vessel ごとに直列化する** (`pahub.get_hub_lock`)。
   channel 選択はバス全体の状態 (現在 open は 1 つ) なので、複数ユニットの測定が
   並列に走ると select が互い違いになり、両方が最後に選ばれた channel を読む
   (= 2 つの ToF が同じ値・同じエラー)。実機で発覚 (2026-07-04)。select から測定
   完了までを 1 ユニットぶんロックで括る。直結 (hub なし) は競合しないのでロック不要。

> **既知のハード制約 (ソフト対応不要)**: 複数 VL53L1X を PaHub で使うと、片方の至近
> 検出時にもう片方が巻き込まれる電気的結合 (共有 VCC/GND) が起きうる。SAIVerse の
> channel 分離・直列化は正しく、これはハード側 (デカップリング/給電分離) の対策事項。
> 切り分けの経緯と対策: `docs/issues/stackchan_multi_tof_power_coupling.md`。

## 11. 確定事項 (2026-07-03 インタビューで確定)

まはー判断: a〜d はすべて提案通り採用 + ペルソナ発見可能性 (§6.1) を追加。

- **a. ラベルの一意性 ✅**: 同 type が **2 件以上なら label 必須 + vessel 内一意**
  (空ラベル禁止・重複禁止)。1 件だけなら label 任意 (省略可)。addressable (②b) の
  解決キーになるため。UI + backend の両方で検証する。
- **b. `target` 省略 / 未一致時の挙動 ✅**: **tof が 1 個なら省略でそれを読む。複数
  なら利用可能ラベル一覧を客観メッセージで返す** (案 ii)。未一致ラベルも同様に一覧を
  返して促す。ラベルの発見手段は `body_status` (§6.1)。「全部まとめて読む」は提供
  しない (崖検知はスペル非経由 = §7 なので需要が薄い)。
- **c. 直結 + ハブの混在 ✅**: **混在禁止。ハブがあるなら全ユニット channel 指定必須**。
  trunk 直結との混在は電気的な落とし穴があるため許さない。`hub.type=none` のときのみ
  channel 省略 (直結)。
- **d. 保存 API の形 ✅**: **`POST /vessels/{id}/unit-config` を新設** (配置全体
  `{hub, units}` を受ける)。意味が `capabilities` の bool 辞書から変わるため。旧
  `POST /vessels/{id}/capabilities` は移行期の互換のため一時残置 (§9)。
- **e. ペルソナ発見可能性 ✅ (まはー指摘で追加)**: `body_status` に搭載ユニット
  セクションを足し、ラベルをペルソナに見せる (§6.1)。これが無いと ②b が使えない。

## 12. 実装ステップ (承認後・目安)

1. データモデル: vessels.db `unit_config` カラム + VesselManager メソッド + 移行。
2. PaHub: `select_channel(ch)` + recovery を channel-aware に。
3. unit driver: channel を受けて select してから I2C (共通 recovery helper 経由)。
   ToF は複数配置を label で引く + 構造化測距 API 分離 + init キャッシュ (vessel,channel) 化。
4. 可視性: `list_building_ids_with_capability` を配置リスト基準に。
5. スペル: `get_tof_distance(target?)` の実行時ラベル解決 (§11-b の挙動)。
6. `body_status` に搭載ユニットセクション追加 (§6.1、ペルソナ発見可能性)。
7. API + UI: `POST /vessels/{id}/unit-config` 新設、配置エディタ、グローバル hub
   params 撤去、旧 capability route の互換残置。
8. intent 反映: 本 doc 確定 + extension_modules §D / vessel K-5 に前方ポインタ追記。
9. (実機) ch3/ch4 の 2×VL53L1X で混線なく個別に読めることを検証。

## 13. 将来拡張: 多段ハブ (今はやらない・スキーマ余地の確認)

多段ハブ (ハブの下にハブ) は今回スコープ外 (§2)。ただし「今の JSON なら将来拡張できるか」
を確認した結果、**箱にはハマらない**。理由と拡張時の道筋:

- 保存先は vessels.db の JSON カラムで、`vessel_manager._init_db` の light migration
  (PRAGMA でカラム判定) と同じ機構で**後から進化させられる**。`version` フィールド
  (§3) を持たせてあるので、古い行を構造推測でなくバージョンで判別して移行できる。
- ただし現行 v1 の形は **単段前提を符号化**している (`hub` が単一オブジェクト、
  `channel` がスカラー)。多段化は「フィールド 1 個追加」ではなく、次のどちらかの
  **軽い migration** になる (作り直しではない):
  - **案 X: `channel` をスカラー → パス配列** (`[3, 2]` = hub1 の ch3 → hub2 の ch2)。
    加えて中間ハブのアドレスを持つ場所が要る。v1 のスカラー `channel` は
    「単一要素パス」とみなせるので後方互換に寄せやすい。
  - **案 Y: `hub` 単一 → `hubs` ツリー** (各ハブに `id` / `parent` / `parent_channel`、
    unit は `hub` を id 参照 + `channel`)。一般ツリーとして最も素直だが reshape 幅は
    案 X より大きい。
- **今はどちらの構造も入れない** (多段を実装しないのに UI / 検証コストだけ乗るため)。
  `version=1` を置いておくことだけが今回の備え。多段要求が実際に出た時に案 X/Y を
  比較して決める。
