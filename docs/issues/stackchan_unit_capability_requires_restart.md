# ユニット搭載トグル (capability) が再起動しないと反映されない

**状態**: 解決済み (バグ① テスト実行の capability チェック / バグ② persona 経路の可視性 とも修正)
**記録日**: 2026-07-03
**更新**: 2026-07-03 (主因の切り分け + バグ①② 修正、実機 ToF 検証のみ残)
**関連**: `docs/intent/stackchan_vessel.md` (不変条件 #14 / 設計 K-5)、stackchan addon `tools/units/`

> ⚠️ 前セッションで基盤モデル (Claude Opus) が「Serena MCP 導入」というタスクを
> **無から幻覚**し、偽のツール実行結果まで捏造した。Serena はこの作業に一切関係
> ない。このドキュメント内の記述は、すべて実ファイルを Read/Grep して裏を取った
> 事実に限定してある。Serena に類する話が出てきたら幻覚として無視すること。

## 🔑 訂正: 症状には別々の 2 バグが混ざっていた (2026-07-03 更新)

本 doc は当初、症状を「登録時スナップショット (下記バグ②)」1 本で説明していたが、
まはーが観測した中心症状 —— **複合アクションのテスト実行で、起動前から ON だった
ENV III すら「搭載されていない」になる** —— はそれでは説明できない。実測で切り分けた
結果、独立した 2 バグだった:

- **バグ① (テスト実行の capability チェック / = まはーの観測症状の主犯・修正済み)**
  複合アクションの「テスト実行」は persona 文脈を持たず、対象機体を core の contextvar
  (`get_tool_target_instance_id`) で渡す。gateway 接続解決 (`resolve_vessel_connection`)
  はこの上書きを尊重するのに、**capability チェックが使う `resolve_vessel` は上書きを
  無視して persona 文脈だけで解決していた**。テスト実行では persona 文脈が空なので
  `resolve_vessel` が `VesselNotAvailable` → 各ユニット tool の `_unit_present()` が
  `False` → 全ユニット (常時 ON の env3 含む) で「搭載されていません」。当初 doc が
  「実行時 `_unit_present` は動的だから即反映」と書いたのは **テスト実行経路を見落と
  した誤り**。
  → **修正**: `resolve_vessel` を `resolve_vessel_connection` と同じ優先順位
  (上書き → persona 文脈) に揃え、`resolve_vessel_connection` 側の重複ロジックを
  一元化 (`expansion_data/saiverse-stackchan-addon/vessel_dispatch.py`)。実 vessels.db に
  対し「forced なし → False / forced あり → True」を確認済み。

- **バグ② (登録時スナップショット / = persona 経路の可視性・修正済み)**
  下記「根本原因」の通り。**persona がスペルとして呼ぶ経路**でのみ効く問題で、
  起動後に初めて ON にしたユニットの `spell_visible` が `False` のままになり、LLM の
  スペル一覧に出ない (= ペルソナが呼べない)。
  → **修正 (案 A)**: `set_vessel_capabilities` が capability 保存後に
  `vessel_dispatch.reregister_unit_tools()` を呼ぶ。ロード済みモジュールのうち
  `MY_UNIT_CAP_KEY` を持つもの (= native unit tool) の `schemas()` を現在の
  vessels.db に対して再評価し、`_remove_registered_tool` → `_add_registered_tool` で
  `spell_visible` / `building_ids` / building ゲートを貼り直す。env3/sonic/servo8 は
  cap key をハードコードしていたので `MY_UNIT_CAP_KEY` 定数に統一 (tof は既に所持)。
  一時 vessels.db で「起動時 OFF → capability ON → reregister」で spell_visible が
  False→True・building_ids が None→[building] に反転するのを検証済み。

## 一行サマリ (バグ②)

機体管理 UI で「搭載ユニット」トグル (env3 / servo8 / sonic / tof) を起動後に ON に
しても、**再起動するまで persona のスペル一覧に出ない**。native unit tool の
`building_ids` / `spell_visible` が起動時のツール登録で一度だけ確定し、capability 変更で
更新されないため。残作業はこれを**再起動不要**にすること (案 A 推奨、下記)。

## 次セッションのゴール

`POST /vessels/{id}/capabilities` でトグルを切り替えたら、**再起動なしで**その機体の
Vessel Building に降りたペルソナに当該ユニットスペルが (可視性・実行ゲートとも)
正しく反映されること。

## 根本原因 (実コードで確認済み)

native unit tool (`tof.py` 等) は起動時 `_autodiscover_tools()` で一度だけ登録される。
その登録処理で `building_ids` と `spell_visible` が**その時点の capability から計算され、
以後更新されない**。

### 根拠 (ファイル:行)

1. **`tools/__init__.py:126`** — `_register_multiple_tools` が `module.schemas()` を
   **登録時に 1 回だけ**呼ぶ。unit tool の `schemas()` はこの中で
   `list_building_ids_with_capability(cap)` を実行し、その瞬間の capability を持つ
   機体の building を `building_ids` として算出する。

2. **`tools/__init__.py:65-67`** — `_add_registered_tool` は
   ```python
   schema_building_ids = getattr(schema, "building_ids", None)
   if schema_building_ids:
       func = _wrap_with_building_gate(name, list(schema_building_ids), func)
   ```
   登録時の `building_ids` を execute-time ゲートのクロージャに**焼き込む**。
   起動時に capability が空だと `building_ids=None` → `if` が偽 → **ゲート自体が
   付かない** (= どの building でも実行できてしまう)。

3. **`tools/__init__.py:94-97`** — 同じ `schema` (spell_visible / building_ids を持つ)
   を `SPELL_TOOL_SCHEMAS[name]` に格納。可視面はこれを参照する。

4. **`saiverse/meta_layer.py:1240-1244`** — スペル一覧 doc (LLM に見せる可視面) は
   `SPELL_TOOL_SCHEMAS` から生成。つまり可視性も登録時スナップショットに依存。

5. **`expansion_data/saiverse-stackchan-addon/api_routes.py:1441-1452`** —
   `set_vessel_capabilities` は `vm.set_capabilities()` で **vessels.db に書くだけ**。
   tool の再登録 (`_remove_registered_tool` / `_add_registered_tool`) も
   `schemas()` 再評価も呼ばない。

6. **`_reconnect_stackchan_mcp_or_log` (api_routes.py:1263-)** はデバイス通信の MCP
   サブプロセス再接続であって、native unit tool の再登録とは**別物**。

### 対して、実行時 `_unit_present()` は動的

各 unit tool の `_unit_present()` (`tof.py` 等) は呼び出しのたびに
`resolve_vessel()` → vessels.db を fresh に読むので、capability 変更は**実行時判定
には即反映される**。ズレているのは「登録時に固定される可視性 / building ゲート」と
「実行時に動的評価される `_unit_present`」の 2 層。これが症状の非対称を生む。

## 実際に観測した症状 (このセッション)

- **21:10:38** SAIVerse 起動。この時点で tof capability は空 → `get_tof_distance` は
  `building_ids=None` (ゲート無し) / `spell_visible=False` で登録
  (backend.log: `tof: no vessel declares tof capability; spells hidden`)。
- **21:13:11** まはーが機体管理 UI で機体1 (building=`stackchan_room`) の tof を ON。
  `set_vessel_capabilities` が vessels.db に `{"env3":true,"servo8":true,"sonic":true,"tof":true}`
  を保存 (backend.log 確認済み)。**保存は正常**。
- しかし tool schema は 21:10 のまま。→ ペルソナに「搭載されていません」が返る。

vessels.db (`~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/vessels.db`):

| vessel_id (先頭) | bound_building_id | capabilities |
|---|---|---|
| 076797f8… | stackchan_room | env3/servo8/sonic/tof すべて true |
| f0d40b0b… | stackchan_2nd_room_city_a | {} (空) |

`building_occupancy_log`: `stackchan_room` に `air_city_a` (エア) が在室。

## 修正の方向性 (候補・次セッションが実コードを見て決定)

以下は**確定した設計ではなく叩き台**。実コードを追って最適解を選ぶこと。

- **案 A: capability 変更時に該当 unit tool を再登録**
  `set_capabilities` を起点に、影響する native unit tool を
  `_remove_registered_tool` → `schemas()` 再評価 → `_add_registered_tool` で貼り直す。
  cap_key ↔ tool 名のマッピングが必要 (各 unit tool は `MY_UNIT_CAP_KEY` を持つので
  それを使える)。既存の登録機構を再利用でき、影響範囲が局所的。

- **案 B: `building_ids` / `spell_visible` を動的解決に変える**
  ToolSchema に静的リストでなく resolver (callable) を持たせ、可視面生成時と
  ゲートチェック時に毎回評価。capability 変更が常に即反映。ただし
  `tools/__init__.py` のゲート機構・`SPELL_TOOL_SCHEMAS` 消費側 (meta_layer 等) に
  横断的な変更が要る。

- **案 C: README の想定に実装を寄せる**
  下記の通り README は「spell surface 構築のたびに schemas() が呼ばれる」前提で
  書かれているが、実装は登録時 1 回。可視面構築時に schemas() を呼び直す設計に
  するなら案 B に近い。

**全ユニット共通の欠陥**である点に注意 (tof 固有ではない)。env3 / servo8 / sonic も
「起動後に初めて capability を ON にする」なら同じ症状になる。既存ユニットが動いて
見えたのは、capability が起動前から vessels.db に入っていたケースだったと推測される
(未確認の推測)。

## 付随して直すべき誤記 (✅ 修正済み)

`expansion_data/saiverse-stackchan-addon/tools/units/README.md` の Step 4 に:

> native tool の `schemas()` は spell surface 構築のたびに呼ばれるので、capability
> 変更後の reconnect で即時に spell visibility が反映される (= subprocess restart 不要)

とあったが、これは**実装と食い違っていた** (schemas() は登録時 1 回)。バグ② 修正
(`reregister_unit_tools` を `set_vessel_capabilities` から呼ぶ) に合わせて、README を
「保存後に明示的な再登録で反映される」実態へ修正済み。

## ToF 対応の実装状況 (このセッションで完了・実機検証待ち)

上記バグとは別に、ToF ユニット (M5Stack VL53L1X、https://ssci.to/9427) 対応は
実装済み。上記トグルバグを直せば動作確認に進める。

**変更済みファイル**:
- `tools/units/tof.py` (新規) — VL53L1X ドライバ。Pololu VL53L1X Arduino ライブラリ
  (ST 公式 API STSW-IMG007 準拠) を 1:1 移植。init (約 40 レジスタ) を機体ごとに
  キャッシュ + 失敗時 lazy 再初期化。純粋整数演算 (timing budget / 補正ゲイン) は
  ハードなし単体テスト済み。ruff / py_compile 通過。
- `api_routes.py` — `_KNOWN_CAPABILITIES` に `"tof"` 追加。
- `ui/Panel.tsx` — `CAPABILITY_OPTIONS` に tof トグル追加。
- `tools/units/README.md` / `docs/intent/stackchan_vessel.md` — tof を追記。

**未了 (実機必要)**:
- 物理センサーを Port A に接続しての I2C シーケンス検証 (chip ID 0xEACC 確認 →
  単発測距)。curl 直叩き手順は README Step 3 参照。
- 初回 init の実測レイテンシ確認 (推測で 1〜2 秒、未実測)。

## 残りの着手順 (実機のみ)

1. ~~バグ① (テスト実行の capability チェック) を修正~~ ✅ 済 (`vessel_dispatch.py`)。
2. ~~バグ② (登録時スナップショット) を修正~~ ✅ 済 (`reregister_unit_tools` +
   `set_vessel_capabilities` から呼び出し + `MY_UNIT_CAP_KEY` 統一)。
3. ~~README 誤記 (下記「付随して直すべき誤記」) を修正~~ ✅ 済 (Step 4)。
4. **(実機・残)** ToF の I2C 検証 — 物理センサーを Port A に接続し、chip ID 0xEACC
   確認 → 単発測距 (README Step 3 / 複合アクションのテスト実行)。初回 init の実測
   レイテンシ確認 (推測 1〜2 秒、未実測)。
