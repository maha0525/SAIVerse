# City の識別子と表示名

> **ステータス**: 検証待ち (2026-08-14) — 実装・自動テスト・実 DB コピーでの移行予行演習まで完了。まはーの実機検証待ち
>
> 関連: [概念リファレンス Building / City](../concepts/building-city.md) / [landscape §2](../overview/landscape.md)

## 1. 何を解決するのか

City は「名前」を入れる欄を 2 つ持っているが、**どちらが表示名でどちらが内部の識別子なのかが、画面ごとに食い違っている**。

まはーが街マップ画面の見出しに `city_a` を見たのが、この食い違いの表面化だった。あの見出しは `CITYNAME` を出していて、`CITYNAME` は表示名ではなく内部の識別子として使われている欄だった。

この intent は、City の名前まわりの欄が**それぞれ何を持ち、誰が真実の持ち主か**を確定させ、画面と DB の意味を一致させる。

### 誰が影響を受けるか

| 立場 | 何を頼りにできるようになるか |
|---|---|
| ユーザー (まはー含む) | 街に好きな名前 (日本語を含む) を付けられ、それが画面に出る。名前を変えても世界が壊れない |
| ペルソナ | 自分が住む街の名前を、将来プロンプト経由で正しく知れる (現状は届いていない。§2-3 参照) |
| 運用 | 起動コマンドの引数・ログの保存先・二重起動チェックが、表示名の変更に影響されない |
| 保守者 (未来の Claude) | `NAME` の付く列がリポジトリ全体で表示名を意味する。City だけ例外という罠が消える |

## 2. 現状 (2026-08-14 の実測)

### 2-1. 欄の実態

| 欄 | 型・制約 | 実際の中身 (まはーの DB) | 実際の役割 |
|---|---|---|---|
| `CITYID` | Integer 主キー | `1`, `2` | DB 内部の参照。外部キーの参照先 |
| `CITYNAME` | VARCHAR(32) / (USERID, CITYNAME) 一意 | `city_a`, `city_b` | **内部の識別子**。英数字とアンダースコアのみ (`manager/admin.py` の `_validate_city_name`) |
| `DESCRIPTION` | VARCHAR(1024) | `city_aの街です。` | **表示名の置き場として使われている**。ただし seed が書いた値は説明文の形をしている |

`builtin_data/seed_data.json` の現行値は `シティA` だが、まはーの DB には `city_aの街です。` が入っている。両者は一致しない (古い seed 由来と思われるが、経緯は未確認)。

### 2-2. `CITYNAME` (識別子) の消費者

表示以外に、この文字列から**別のものが組み立てられている**。ここが「触ると壊れる」理由。

| # | 用途 | 場所 |
|---|---|---|
| 1 | 起動コマンドの引数 → City 行の検索キー | `main.py` → `manager/initialization.py:58` |
| 2 | ユーザーの部屋の建物 ID `user_room_{CITYNAME}` | `manager/initialization.py:113` / `database/seed.py:337` / `api/routes/user.py:234` |
| 3 | 建物ログの保存先 `~/.saiverse/cities/{CITYNAME}/buildings/…/log.json` | `manager/initialization.py:161` |
| 4 | 二重起動チェックの鍵 | `saiverse/runtime_marker.py` |
| 5 | ペルソナ ID の生成 `{name}_{CITYNAME}` | `manager/blueprints.py:193` / `frontend/src/components/PersonaWizard.tsx:67,79` |
| 6 | Building ID の生成 `{name}_{CITYNAME}` | `manager/admin.py` の `create_building` |
| 7 | SDS への登録名 | `manager/sds.py:172` |
| 8 | 他 City 設定の辞書キー (inter-city、凍結中) | `manager/initialization.py:123` |
| 9 | テスト環境クローンの `--city` 引数 | `scripts/clone_world_to_test_env.py` |

### 2-3. 表示名として読まれている場所

| 場所 | 今読んでいる欄 |
|---|---|
| 街マップ画面の見出し | `CITYNAME` ← **ここだけ識別子を出している** |
| World Editor の City 一覧・各種セレクト | `DESCRIPTION \|\| CITYNAME` |
| BuildingSettingsModal / PersonaWizard のセレクト | `DESCRIPTION \|\| CITYNAME` |
| チュートリアルの「City名」(入力・プリフィル) | `DESCRIPTION` に書き込み / `DESCRIPTION \|\| CITYNAME` から復元 |
| DB 閲覧 UI の行ラベル候補 | `CITYNAME` (`database/db_manager.py:146`) |

つまり**チュートリアルと World Editor は既に「DESCRIPTION = 表示名」で動いており、マップ画面だけが移行から取り残されている**。

### 2-4. 現状の欠陥

| # | 欠陥 | 影響 |
|---|---|---|
| A | World Editor から `CITYNAME` を編集できてしまう。編集してもメモリ上の `user_room_id` が書き換わるだけで、DB の building 行 (`user_room_city_a`) とディスクのログフォルダは旧名のまま (`manager/admin.py:185`、building テーブルを触る処理は存在しない) | ユーザーの部屋が行方不明になり、過去の建物ログが読めなくなる |
| B | チュートリアルの City 名入力欄が英数字とアンダースコアのみに制限されている (`StepCityName.tsx:26`) | 表示名を入れる欄なのに日本語の街名を付けられない |
| C | 同ヒント文「スキップした場合は『city_a』になります」が実際の動きと違う (実際は `DESCRIPTION` の既存値が残る) | 説明が嘘をついている |
| D | プロンプトの差し込み口 `{current_city_name}` が、存在しない属性 `persona.current_city_id` を読んでいる (`sea/head_pipeline/sections/common_prompt.py:66` / `builtin_data/tools/get_system_prompt.py:68`)。実在する属性は `persona.city_name` | 常に `unknown_city` になる。現行の `common.txt` はこの差し込み口を使っていないので実害は出ていない (潜在) |

欠陥 A は起動時の「CITYNAME 自動修復」と同じ根から出ている。自動修復のコメントは、その救済理由を「チュートリアルが内部の識別子を非 ASCII の表示名で上書きしてしまった場合」と記録しており、**同種の事故が過去に起きていた**ことを示す。

## 3. 決めたこと — 4 つの欄と責任分界

| 欄 | 型・制約 | 役割 | 書く権限を持つのは | 読むのは |
|---|---|---|---|---|
| `CITYID` | Integer 主キー | DB 内部の参照 | DB (自動採番) | 外部キー・API のパスパラメータ |
| `CITY_SLUG` | VARCHAR(32) / (USERID, CITY_SLUG) 一意 / ASCII 英数字とアンダースコアのみ | **内部の識別子**。ファイル名・ID の材料として安全な短い文字列 | City 作成時のみ。**既存 City では変更不可** | §2-2 の 9 用途すべて |
| `CITYNAME` | VARCHAR(64) | **表示名**。自由な文字列 (日本語可) | ユーザー (チュートリアル / World Editor / マップの編集ボタン) | 画面の見出し・一覧・セレクト |
| `DESCRIPTION` | VARCHAR(1024) | **街の説明文** | ユーザー (World Editor のみ) | 将来: ペルソナへの提示、一覧の補足 |

`slug` は、URL やファイル名に使える短い識別子を指す一般的な語。この列名を選んだのは、**`NAME` の付く列は表示名を意味する**という規則をリポジトリ全体で成立させるため。Building は `BUILDINGNAME`、ペルソナは `AINAME` が既に表示名なので、City だけが逆になっていた状態を解消する。

## 4. 不変条件

1. **`CITY_SLUG` は ASCII 英数字とアンダースコアのみ、空でない。** (USERID, CITY_SLUG) で一意。
2. **`CITY_SLUG` は既存 City では変更できない。** UI から編集させず、API 層でも変更要求を拒否する。
   - 理由: 発行済みの建物 ID・ペルソナ ID・ディスク上のログフォルダが既にこの文字列を含んでおり、後から変えるとそれらと食い違う。§2-4 の欠陥 A はまさにこれ。
   - 起動時の「CITYNAME 自動修復」は、この不変条件が破られた世界を救う機構として残す (供給源は塞ぐが、既に壊れた DB の救済経路は要る)。
3. **`CITYNAME` (表示名) は一意性を要求しない。** 空文字を許し、空なら画面は `CITY_SLUG` を代わりに表示する。
4. **表示名を変えても、識別子から派生したものは一切変わらない。** 部屋の建物 ID、ペルソナ ID、ログの保存先、二重起動チェックの鍵は `CITY_SLUG` だけを見る。

不変条件 2 が今回もっとも大きな挙動変更にあたる。今は World Editor から識別子を編集でき、それが壊れる供給源になっている。

## 5. 変更を置く場所と、その理由

| 変更 | 場所 | なぜここが持ち主か |
|---|---|---|
| 列のリネームと追加 | `database/models.py` + `database/migrate.py` | 欄の意味の真実は DB スキーマが持つ。ここを直さずに画面だけ直すと、意味の食い違いが残ったまま見た目だけ揃う |
| 識別子の変更拒否 | `manager/admin.py` の `update_city` | 表示層 (UI) で編集欄を消すだけでは、API を直接叩く経路が残る。値が書き込まれる場所で止める |
| 見出しの表示名化 + 編集ボタン | `api/routes/info.py` (city-map 応答) + `frontend/src/components/CityMap.tsx` | 見出しの供給源は city-map の応答。ここが表示名を返せば、消費側は 1 箇所の差し替えで済む |
| チュートリアルの入力制限撤廃 | `frontend/src/components/tutorial/steps/StepCityName.tsx` | 表示名を受け取る唯一の入口。制限は書き込み先が識別子だった頃の名残 |

## 6. 移行

`database/migrate.py` の `KNOWN_COLUMN_RENAMES` に City を追加し、以下の順で当てる。

1. `city.CITYNAME` → `city.CITY_SLUG` にリネーム (`ALTER TABLE RENAME COLUMN`)。一意制約も `uq_user_city_slug` へ移す。
2. 新しい `city.CITYNAME` (表示名) を追加。既定値は空文字。
3. **`DESCRIPTION` の中身を `CITYNAME` へコピーする。`DESCRIPTION` は消さない。**
4. コピー元が空だった場合は `CITY_SLUG` を入れる。

### 一意制約の名前だけは揃わない (受け入れた不揃い)

SQLite の `ALTER TABLE RENAME COLUMN` は制約が参照する列名を書き換えるが、制約の**名前**は変えない。そのため移行を通った DB は `CONSTRAINT uq_user_city_name UNIQUE ("USERID", "CITY_SLUG")` となり、新規作成の DB (`models.py` 由来) は `uq_user_city_slug` になる。**覆う列は同じ**なので不変条件 1 は両者で成立する。名前だけを揃えるには全書換が要るので、それに見合わないと判断して受け入れた。

### なぜコピー元を `DESCRIPTION` にするのか

チュートリアルで街の名前を入力した人の入力は `DESCRIPTION` に入っている。ここを移さないと、その入力が黙って消える。逆に `DESCRIPTION` が seed の説明文のままだった世界 (まはーの DB がこれ) では、表示名が一度だけ説明文になる。それは**マップの編集ボタンで直せる**ので、取り返しがつく側の不都合を選ぶ。

`DESCRIPTION` を空にしない判断は、まはー裁定 (2026-08-14)。説明文として読める値がそのまま残る方が、情報を捨てるより安全という理由。

## 7. 検証の道筋

**リネームだけを先に当てた状態でテストを通す。** この段階では `CITYNAME` という列が存在しないので、識別子のつもりで `CITYNAME` を読んでいる取りこぼしは**すべて実行時に落ちる**。表示名の列を先に足してしまうと、取りこぼしが「静かに間違った値を返す」形になり、日本語のペルソナ ID が生成されるような事故を見逃す。

順序:

1. リネームのみ適用 → 全テストを通す → `CITYNAME` の参照が 0 件になったことを確認
2. 表示名の列を追加 → バックフィル → テスト
3. 画面側 (マップ見出し・編集ボタン・チュートリアル) を接続

### 済んだ検証 (2026-08-14)

| 何を | 結果 |
|---|---|
| 識別子の改名だけを当てた状態で全テスト | 4306 passed / 3 skipped。旧名を識別子として読む取りこぼしはゼロ |
| 新設テスト `tests/test_city_identity.py` | 22 件。移行・冪等性・一意制約の追随・識別子の不変性・表示名 PATCH・見出しのフォールバック |
| フロントの型検査 (`tsc --noEmit`) | エラーなし |
| **まはーの実 DB のコピーで移行を予行演習** | 追加系パスで完了 (全書換に落ちない)。`city_a` / `city_b` の識別子は保持、表示名は `DESCRIPTION` から復元、`DESCRIPTION` は残存、一意制約は `CITY_SLUG` へ追随、`user_room_city_a` とペルソナ ID 18 件は無変化。2 回目の実行は無変化 (冪等) |

予行演習で判明した実データの見え方: まはーの世界は `DESCRIPTION` が seed の説明文 (`city_aの街です。`) のままなので、**移行直後の表示名は「city_aの街です。」になる**。§6 で選んだ「取り返しがつく側の不都合」がそのまま出る形で、マップ画面の編集ボタンで直せる。

### 実機で確認すること (境界をまたぐ journey)

| # | 確認 | どの境界を越えるか |
|---|---|---|
| 1 | 起動して、マップ見出しに表示名が出る | DB → API → 画面 |
| 2 | 編集ボタンで日本語の街名に変え、見出しに反映される | 画面 → API → DB → 画面 |
| 3 | **再起動しても、まはーの部屋と過去の建物ログが元のまま** | DB → 起動時のファイルパス解決 → ディスク |
| 4 | 新しいペルソナを作り、ID が `{名前}_city_a` の形になる | DB → ID 生成 |

3 が今回の本丸。表示名の変更が識別子から派生したものに漏れていないことは、**再起動を跨がないと確かめられない**。

## 8. やらないこと / 積み残し

- **チュートリアルに説明文の入力欄は追加しない** (まはー裁定 2026-08-14)。初対面のユーザーに街の説明を書かせるのは負担が大きい。`DESCRIPTION` の編集は World Editor 側だけで足りる。
- **inter-city 関連 (`cities_config`) は凍結中**のため機能追加はしない。辞書のキーを `CITY_SLUG` に合わせる追従のみ。
- **Python 側の属性名 `manager.city_name` / `state.city_name` / `PersonaCore.city_name` は今回は改名しない。** これらが持つのは識別子 (`CITY_SLUG`) であり、「`NAME` は表示名」の規則から外れている。DB の列と同じ罠だが、改名は SDS の登録ペイロードや inter-city (凍結中) の JSON キーまで巻き込むため、欠陥 D の修理と同じ独立したコミットへ回す。**そのとき `PersonaCore.city_name` は表示名を運ぶ側にする** (現状この属性は代入されるだけでどこからも読まれておらず、欠陥 D で死んでいる)。
- **欠陥 D (`{current_city_name}` が届かない) は別コミットで直す。** 今回の変更で City の表示名が確定するので、この差し込み口が表示名を渡すべきか識別子を渡すべきかも同時に決まる (表示名を渡す)。現行の `common.txt` は使っていないため、直しても挙動は変わらない。
- **Building ID 生成の文字種検証** ([building_id_no_charset_constraint.md](../issues/building_id_no_charset_constraint.md)) は既存の別 issue。`CITY_SLUG` 化でサフィックス側の安全は確保されるが、Building 名側の日本語問題は残るため、この issue は閉じない。

## 経緯

- **2026-08-14 起草**: まはーが街マップ画面の見出しに `city_a` が出ていることに気づき、City の名前と ID の扱いの状況確認を依頼。調査の結果、チュートリアルと World Editor は既に `DESCRIPTION` を表示名として使っており、マップ画面だけが取り残されていることが判明した。
- **2026-08-14 裁定 (まはー)**: 説明文の欄は将来のために残す。`DESCRIPTION` が実質的な表示名になっている状態は後の保守者を混乱させるため、欄の名前自体も意味に合わせる。増築 (`DISPLAY_NAME` 追加) ではなく**付け替え** (`CITYNAME` → `CITY_SLUG`、空いた `CITYNAME` を表示名に) を採用。
- **2026-08-14 裁定 (まはー)**: `DESCRIPTION` は表示名へコピーした後もそのまま残す。チュートリアルには説明文の入力欄を追加しない。
