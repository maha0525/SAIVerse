# Intent: Stack-chan Avatar Pipeline（Phase 4.5）

**ステータス**: v0.1 ドラフト（2026-05-14 起草、 同日 Phase 4.5-a/b の skeleton 実機疎通確認まで完了）

**関連**: `docs/intent/stackchan_vessel.md` の Phase 4.5 として詳細化。stackchan_vessel.md §H「Avatar 連動」と Phase 5/6 の前提を再構成する。

## v0.4/v0.5 計画と現実のズレ

stackchan_vessel.md §H では「stackchan-mcp の `set_avatar` / `set_blink` / `set_mouth` で基本的な表情制御は可能」「Phase 5 でペルソナ感情パラメータ → Avatar 表情マッピング層」「Phase 6 で TTS エンベロープから set_mouth_sequence を叩く口パク」と書かれていた。これは stackchan-mcp 側の avatar 描画が完成している前提に立っている。

実機検証 (Phase 4', 2026-05-14) の過程で確認できた事実は、stackchan-mcp の firmware リソース `firmware/main/boards/stackchan/avatar_images.cc` の **14 種類すべての avatar 画像が `{0x00, 0x00}` の 1×1 黒ピクセル placeholder のまま** ということ。コメントには `Replace with real 160×120 RGB565 art before shipping to production.` と明記されている。

つまり SAIVerse 側で感情パラメータ → set_avatar のマッピング層や TTS → set_mouth_sequence の同期ロジックを書いても、デバイスに送信されるコマンド自体は ESP32 まで届くが、描画される画像が 1×1 黒ピクセルなので **画面には何も表示されない**。intent doc §H と Phase 5/6 の前提が崩れている。

## このドキュメントは何か

stackchan-mcp 側の avatar 描画機構を SAIVerse 主導で完成させ、ペルソナの「顔」を Stack-chan の LCD に表示するための設計と実装計画。

具体的には:

1. **動的 avatar セット転送機構**: firmware に焼き込まない、SAIVerse から WiFi 経由で push、ESP32 は PSRAM に展開
2. **2 モード対応**: layered (14 シンボル合成) / matrix (90 枚から選択)
3. **画像生成パイプライン**: 元顔 1 枚から表情差分を一括生成する addon UI
4. **upstream PR**: stackchan-mcp 本流に動的 avatar セット転送機構 + デフォルト art 1 セットを PR

stackchan-mcp の avatar_images.cc の placeholder TODO 解決と、SAIVerse のペルソナ別 avatar 要件を同時に満たす。stackchan-mcp が本来やりたかったであろう完成状態 (= 固定 14 シンボル方式) も内包する形で、上位機能 (= 動的セット転送 + ペルソナ別 + 画像生成 AI 親和) を追加する設計。

## これは何でないか

- **Vessel 描画の汎用基盤ではない**。最初は Stack-chan 1 機種に特化した実装にする。将来別 Vessel (= 2D 表示、別 robot) が増えた時に共通機構として切り出すかは Vessel 機能自体の汎用化フェーズで再検討する。中身の関数レベルでは共通化できる部分が出るかもしれないが、それは vessel 汎用化時の話。
- **動画 / リアルタイム生成ではない**。静止画 90 枚 (matrix mode) または 14 枚 (layered mode) のセットで構成する。表情遷移はセット内の画像切替で表現する。
- **TTS 同期の精密実装ではない**。Phase 6 の口パク (= set_mouth_sequence の TTS エンベロープ同期) は本書の射程外。本書は「口パクが実装された時に実際に口が動く土台」を整える。
- **upstream stackchan-mcp の置き換えではない**。upstream の API (set_avatar / set_eyes / set_mouth) は非破壊で残す。layered mode として既存ユーザーは継続利用できる。

## 設計の核

### 1. avatar セットを動的転送リソースとして扱う

stackchan-mcp 本来の方式は「14 シンボルを firmware に焼き込む」だった。これは upstream 視点で見ても物足りない (= ユーザーが avatar を変えたい時に firmware 焼き直しが必要)。

新方式:

- avatar セット = 「mode + format + 画像群」のアーカイブ
- SAIVerse から gateway 経由で ESP32 に push
- ESP32 は PSRAM (8 MB) に展開して保持
- ペルソナ憑依時にセット切替 → 該当ペルソナの avatar セットを再 push

これにより:
- ペルソナ別 avatar が成立 (firmware 焼き直し不要)
- avatar 制作 (= 画像生成) は SAIVerse 側で完結
- upstream に「動的 avatar セット転送 API」だけを PR、デフォルト art は同梱

### 2. layered / matrix の 2 モード

avatar セット metadata に `mode` フィールドを持たせ、firmware は mode に応じて描画ロジックを分岐する。

**layered mode (14 シンボル)**:
- 構成: full-face 6 (idle/happy/thinking/sad/surprised/embarrassed) + eyes 3 (open/half/closed) + mouth 5 (closed/half/open/e/u)
- 描画: face を base layer、eyes と mouth を overlay で重ねる
- メモリ: 14 × 38 KB ≈ 537 KB (raw) / 130 KB (PNG)
- 利点: 既存 stackchan-mcp 互換、部分更新が可能 (口だけ更新 = Phase 6 口パクで描画コスト低)、art リソース少なくて済む
- 用途: シンボリックな表情で十分なケース、画像生成に頼らない手描き art

**matrix mode (90 枚)**:
- 構成: 6 (face) × 3 (eyes) × 5 (mouth) = 90 枚の組み合わせ画像
- 描画: 現在の `(face, eyes, mouth)` 状態から該当する 1 枚を選んで全画面更新
- メモリ: 90 × 38 KB ≈ 3.3 MB (raw) / 約 1 MB (PNG)
- 利点: 画像生成 AI と親和性が高い (= 元顔 + 表情指示で一括生成可能)、自然な顔の表現 (合成の不自然さなし)
- 用途: 写実的 / 高品質な avatar、ペルソナ別の固有顔

mode は **avatar セット単位で固定**。ペルソナ憑依時にセットごとロードする方式と整合する。「ペルソナ A は matrix mode、ペルソナ B は layered mode」のような混在は可能。

### 3. 画像生成パイプラインは addon 内の単発機能、段階的生成でコスト最適化

avatar 制作 UI は saiverse-stackchan-addon の管理画面に単独機能として実装する。汎用 SAIVerse 機能 (= 他の Vessel や 2D 表示にも使える generic な「ペルソナ視覚化セット生成」基盤) にはしない。

理由:
- Vessel ごとに表情の解像度・symbol セット・描画特性が違う可能性が高い
- 早すぎる抽象化を避ける (= 1 機種で動かしてから汎用化を検討)
- Vessel 汎用化フェーズで関数レベルの共通化は別途検討する

UI が扱う入力 / 出力:
- 入力: 元顔画像 1 枚 (= idle face として扱う) + mode 選択
- 出力 (matrix mode): 90 枚の組み合わせ画像
- 出力 (layered mode): 14 個のパーツ画像 (face × 6 + eyes × 3 + mouth × 5)

**段階的生成フロー (コスト最適化)**:

90 枚または 14 枚を一気に生成すると、品質に納得できなかった場合に無駄な API コストが発生する。これを避けるため、生成は 2 段階に分ける:

1. **Step 1: 表情差分 5 種を先行生成 + レビュー**
   - 元顔 = idle として扱い、残り 5 種 (happy / thinking / sad / surprised / embarrassed) のみを生成
   - ユーザーがプレビューを見て **OK (続行) / NG (キャンセル)** を選択
   - NG の場合はここで終了、後続生成のコストを払わない
2. **Step 2: OK なら本生成**
   - matrix mode: 6 表情 × 目 3 × 口 5 = 90 枚を一括生成 (Step 1 の 6 表情を base として再利用)
   - layered mode: 目 3 + 口 5 = 8 パーツを生成 (Step 1 の表情 6 種と合わせて 14 個揃う)

将来拡張の余地として「Step 1 のレビュー時に気に入らない表情だけ再生成」できる UI に拡張可能な構造にしておく (= 1 個ずつの再生成 API を内部に持つ、最初の UI は全体 OK/NG のみで露出)。

画像生成 backend は SAIVerse 既存の `image_generator` ツール (`nano_banana_2` / `nano_banana_pro` / `gpt_image_1_5` / `gpt_image_2` / `grok_imagine` 等の backend 切替を内部でサポート) を流用する。具体的にどの backend を使うかは UI から選択可能にする。

## 不変条件

1. avatar art は WiFi 経由で動的に切り替えられる (firmware 焼き直し不要)
2. 90 枚 matrix mode と 14 シンボル layered mode はどちらも単一の avatar セット metadata schema で表現できる
3. upstream stackchan-mcp の既存 14 シンボル API (set_avatar / set_eyes / set_mouth) は非破壊で残す。layered mode として動く
4. ペルソナ別 avatar の永続化は SAIVerse 側 (addon storage) で扱う。stackchan-mcp 本流にはペルソナという概念を漏らさない
5. avatar セット転送は冪等。「現在ロード中のセット」と「これからロードするセット」のハッシュが一致したらスキップして良い
6. avatar セットの転送中に表情切替コマンドが来た場合、転送完了まで待つ (= 中途半端な描画を出さない)
7. matrix mode 90 枚 / layered mode 14 枚は完全なセット。一部欠けは許容しない (= デフォルトフォールバックを decoder 内に持たない、欠けは作成時のバリデーションで弾く)

## 設計

### A. avatar セット metadata schema

avatar セットは zip / tar アーカイブとして addon storage に保持する。アーカイブ内 `manifest.json`:

```json
{
  "version": 1,
  "name": "air-default",
  "mode": "matrix",
  "format": "png",
  "resolution": [160, 120],
  "checksum": "sha256:...",
  "symbols": {
    "idle_open_closed": "img/idle_open_closed.png",
    "idle_open_half":   "img/idle_open_half.png",
    "...": "..."
  }
}
```

- `mode`: `"layered"` または `"matrix"`
- `format`: `"rgb565"` (raw) または `"png"`。PNG は ESP32 側で decode して PSRAM に展開
- `symbols`: ファイル名規約
  - layered: `face_<name>` (6 個) + `eyes_<state>` (3 個) + `mouth_<shape>` (5 個) = 14 エントリ
  - matrix: `<face>_<eyes>_<mouth>` (90 エントリ)

セット転送時、SAIVerse は manifest を読んで gateway 経由で push する。

### B. firmware 側拡張

stackchan-mcp の firmware fork で以下を実装。

#### B-0. 既存実装の活用と並存構造

`feature/dynamic-avatar-set` ブランチ作成 (2026-05-14) 後に確認したところ、stackchan-mcp の firmware には既に下記が用意されていた:

- `avatar_images.{cc,h}`: 1×1 黒ピクセル placeholder の static const tables (upstream 現状)
- `avatar_images.local.{cc,h}`: CMakeLists.txt で `if(EXISTS ${STACKCHAN_LOCAL_AVATAR_CC})` により上書き可能な local override 機構
- `firmware/scripts/avatar_convert/convert_avatars.py`: PIL で PNG を読み込んで 160×120 RGB565 に downscale + LVGL C 配列を吐く事前変換スクリプト
- `CONFIG_LV_USE_PNG` / `BMP` / `GIF` 全部 unset (= LVGL の runtime decoder は無効)、OTA partition は ~3.9 MB

つまり upstream の想定は「ビルド前に PNG → RGB565 変換 → 静的に焼き込む」フローで、`avatar_images.local.cc` で差し替える既存ユーザーが居る可能性がある。我々の動的セット転送はこれと並存させる:

| 層 | 何 | 何のため | 既存維持 |
|---|---|---|---|
| 1. placeholder | `avatar_images.cc` の 1×1 黒ピクセル | 起動時の保険、AvatarSet 未ロード時の表示 | はい |
| 2. local static override | `avatar_images.local.{cc,h}` (CMake で差し替え) | 静的 art を焼きたいユーザー向け、firmware 焼き直し前提 | はい |
| 3. dynamic avatar set | 新規 `AvatarSet` クラス (本書の B-1〜B-3) | 動的にロードされる PSRAM 上の art セット、ペルソナ別 / multi-character 対応 | (新規) |

`StackChanBoard::AvatarImageFor(face)` は、AvatarSet がロード済みであれば AvatarSet の lookup を返し、未ロードであれば既存の static const table (= placeholder or local override) を返す形に書き換える。これにより:

- upstream の現行ユーザー (静的 art 派) は何も壊れない (load_avatar_set を呼ばなければ static 経路のまま)
- SAIVerse のような動的ユーザーは load_avatar_set で AvatarSet を埋めて、表情遷移が AvatarSet 経由になる

format に関しては firmware は **raw RGB565 のみ** をサポートする。CONFIG_LV_USE_PNG が unset である理由 (= OTA partition 圧迫を避ける、decoder 追加で flash サイズが膨らむ) を尊重し、PNG → RGB565 変換は gateway 側で行う (`convert_avatars.py` のロジックを gateway に取り込む)。

#### B-1. avatar セットローダ

転送経路は **HTTP fetch** (案 A、§C 参照)。device → gateway 方向の HTTP GET で raw RGB565 payload を受け取る。既存音声 WS とは独立した経路を取ることで、音声経路 (= PR1/2/3) への影響を絶対にゼロにする。

経路:
1. gateway から `avatar_set_fetch` WS message を受信 (URL / token / mode / checksum / expected_size を含む)
2. device 側で ESP-IDF http_client で HTTP GET、`Authorization: Bearer <token>` ヘッダ付き
3. chunked transfer で受信しながら PSRAM 上の staging buffer に書き込む
4. 受信完了後 SHA256 検証
5. `AvatarSet::Load(mode, buffer, size)` を呼んで lv_image_dsc_t テーブルを構築
6. 旧 set buffer は AvatarSet::Load 内で解放される (= 新 buffer 確保後の swap)
7. `avatar_set_loaded` WS message を gateway に返す (ok/error/checksum)

`AvatarSet` クラス本体は `firmware/main/boards/stackchan/avatar_set.{h,cc}` に実装済み (2026-05-14)。HTTP fetch + WS message handling は別途 `protocols/websocket_protocol.cc` 拡張 + 新規 `avatar_set_fetcher.{cc,h}` 等で実装する。

#### B-2. mode 別描画ロジック

`StackChanBoard::SetAvatarExpression(face, eyes, mouth)`:

- mode == layered:
  - `lv_img_set_src(face_obj, &face_table[face])`
  - `lv_img_set_src(eyes_obj, &eyes_table[eyes])`
  - `lv_img_set_src(mouth_obj, &mouth_table[mouth])`
- mode == matrix:
  - `idx = face * 15 + eyes * 5 + mouth`
  - `lv_img_set_src(full_obj, &matrix_table[idx])`

現在の `(face, eyes, mouth)` 状態を内部で保持し、変化があれば再描画。

#### B-3. PSRAM 容量管理

- 8 MB PSRAM の使用上限を 5 MB と定める (xiaozhi-esp32 base の他用途と共存)
- matrix mode 1 MB (PNG) + layered mode 130 KB (PNG) のどちらも余裕
- 容量超過時は load_avatar_set がエラーを返す (= addon 側で警告表示)

### C. gateway 側 MCP tool 拡張 + 転送プロトコル

転送経路は **HTTP fetch** (案 A、2026-05-14 確定)。既存音声 WS (= PR1/2/3) と分離するため、既存の `capture_server.py` HTTP server に avatar セット配信 endpoint を追加し、device 側は WS notify を受けてから HTTP GET で取得する。OTA (`firmware/main/ota.cc`) と同じパターン。

#### C-1. `load_avatar_set` MCP tool

```python
@tool
async def load_avatar_set(
    archive_path: str,          # gateway-local path to a raw RGB565 file
    mode: str,                  # "layered" or "matrix"
    timeout: float = 60.0,
) -> dict:
    """avatar セットを ESP32 にロードする。

    1. archive_path のファイルを raw RGB565 として読み、サイズと mode を検証
    2. payload を gateway HTTP server に staging (one-time URL + bearer token + SHA256)
    3. ESP32 に WS で fetch 指示を送信 (type=avatar_set_fetch)
    4. ESP32 が HTTP GET で取得 → SHA256 検証 → AvatarSet::Load
    5. ESP32 から WS 完了通知 (type=avatar_set_loaded) を待つ
    6. 結果を返す

    Returns:
        {"ok": bool, "checksum": str, "bytes_transferred": int,
         "error": str | None}
    """
```

引数を bytes ではなくファイルパスで受ける理由:

- MCP の I/O は JSON ベースで、 大きな bytes は base64 等で膨らむ。 数 MB の RGB565 を base64 化すると ~33% 増 (matrix で ~4.4 MB) で MCP transport の負荷が無視できない
- gateway は subprocess として addon と同居するので、 ローカルファイルシステム経由がもっとも素直 (= addon が tempfile に書き出して archive_path を渡す)
- gateway 側で `convert_avatars.py` 同等の PNG → RGB565 変換を **行わない** 方針 (= addon 側で完了させる)。 これにより stackchan-mcp 本流に Pillow 等の重い依存を持ち込まずに済み、 upstream PR の受け入れ性が高まる

挙動:

- size 検証 (`mode=layered` なら 537,600 bytes、`mode=matrix` なら 3,456,000 bytes)。 不一致なら fetch せずエラー
- SHA256 を計算して staging
- 既存 set と checksum 一致なら fetch スキップして即 ok を返す (= 冪等性、 不変条件 #5)。 ※ 現状の skeleton 実装はこの最適化を含まず、 都度 fetch する。 follow-up で対応

#### C-2. HTTP staging endpoint

既存 `gateway/stackchan_mcp/capture_server.py` を拡張:

- `GET /avatar_set/{short_id}` を追加
- `Authorization: Bearer <one-time token>` を検証
- raw RGB565 bytes を `Content-Type: application/octet-stream` で返す (chunked transfer encoding)
- 一度取得されたら staging を破棄 (or 短時間 TTL で GC)
- 認証 token は load_avatar_set 呼び出しごとに gateway が runtime 生成 (= 新規 env var は不要)

#### C-3. WS 通知プロトコル

**gateway → device (avatar_set_fetch)** — 既存 WS text frame で送信:

```json
{
  "type": "avatar_set_fetch",
  "url": "http://<vision_host>:<port>/avatar_set/<short_id>",
  "token": "<one-time bearer>",
  "mode": "layered",
  "checksum": "sha256:abcd1234...",
  "expected_size": 537600
}
```

**device → gateway (avatar_set_loaded)** — 既存 WS text frame で送信:

```json
{
  "type": "avatar_set_loaded",
  "checksum": "sha256:abcd1234...",
  "ok": true,
  "error": null
}
```

エラー時の挙動:

- HTTP fetch 失敗 → device は旧 set を保持、`{ok: false, error: "fetch_failed: ..."}`
- checksum mismatch → 旧 set を保持、`{ok: false, error: "checksum_mismatch"}`
- PSRAM allocate 失敗 → 旧 set を保持、`{ok: false, error: "psram_oom"}`
- fetch 中の WS 切断 → fetch を中断、device 側で in-progress 状態をクリア、再度 `avatar_set_fetch` が来れば再試行

#### C-4. 転送中の表情切替の扱い (不変条件 #6)

device 側で fetch in-progress 中は `SetAvatarExpression` 系の呼び出しを保留:

- fetch 中に届いた face / eyes / mouth 変更は最後の 1 個だけ pending として保持
- fetch 成功時: `AvatarSet::Load` 後に pending の状態を新 set で apply
- fetch 失敗時: pending の状態を旧 set で apply (= 新 set が乗らなかったので旧 set のまま)

これにより、転送中に「中途半端な描画」が表示されることがない。

#### C-5. `set_avatar` 等の既存 tool は維持

- layered / matrix どちらの mode でも `set_avatar(face=...)` は同じインターフェース
- device 側 (stackchan.cc) で AvatarSet の `mode()` を見て B-2 のロジックに振り分け
- AvatarSet 未ロード時は既存 static const table (= placeholder or local override) を参照する fallback 経路を維持
- 既存 stackchan-mcp ユーザーは破壊的変更を受けない

### D. SAIVerse addon 側

saiverse-stackchan-addon に以下を実装:

#### D-1. avatar セット永続化

- addon storage に `avatar_sets/<persona_id>/<set_name>/` でアーカイブと manifest を保持
- ペルソナ憑依時 (= Vessel Building 入室時) に該当ペルソナの avatar セットを load
- 同じセットを連続 load する場合は checksum 比較でスキップ

#### D-2. ペルソナ憑依時の自動ロード

- `OccupancyManager.on_persona_entered(building_id, persona_id)` フックで avatar セット選択
- Vessel Building の `physical_vessel_id` がセットされていて、当該ペルソナに avatar セットが定義されていれば load

#### D-3. mode 選択 + 画像生成 UI

addon 管理画面に「ペルソナ avatar 設定」セクションを追加:

- ペルソナ選択
- mode 選択 (layered / matrix)
- 画像生成 backend 選択 (`image_generator` の対応 backend から)
- 元顔画像アップロード (= idle face として扱う) or 既存セットからインポート
- **Step 1 生成ボタン** → 表情差分 5 種 (happy/thinking/sad/surprised/embarrassed) を生成
- **Step 1 レビュー画面** → 5 種 + idle 計 6 表情のプレビューを並べて表示、OK (続行) / NG (キャンセル) ボタン
- **Step 2 生成ボタン (OK 後に活性化)** → mode に応じて本生成
  - matrix: 残り 84 枚 (90 - 6) を一括生成、進捗バー必須
  - layered: 残り 8 パーツ (eyes 3 + mouth 5) を生成
- 全枚プレビュー (デバイスを使わずブラウザ上で確認)
- 「Stack-chan に転送」ボタン → load_avatar_set 呼び出し

#### D-4. 画像生成パイプライン詳細

- backend: SAIVerse の `image_generator` ツールが対応する backend を選択 (具体的なモデル ID は `builtin_data/tools/image_generator.py` の `ModelType` に従う)
- 入力: 元顔画像 + 表情指示プロンプト
- 生成戦略:
  - **Step 1**: 元顔から表情差分 5 種を画像編集生成 (= happy/thinking/sad/surprised/embarrassed)
  - **Step 2 (matrix mode)**: 6 表情それぞれから eyes 3 × mouth 5 = 15 通りを派生生成 (合計 90 枚、idle base + 5 差分 base × 15 = 6 × 15)。並列度を制御しながら生成
  - **Step 2 (layered mode)**: 元顔 (= idle base) から eyes 3 / mouth 5 のパーツを生成 (合計 8 パーツ)
- 失敗した枚は再生成プロンプトを微調整して retry
- 内部 API は「1 枚ずつ再生成」できるエンドポイントを持つ (= 将来の「気に入らない表情だけ差し替え」UI 拡張用)。最初のリリースでは UI 上は Step 1 全体の OK/NG のみ露出

### E. upstream PR ストーリー + デフォルト art の依頼

stackchan-mcp 本流への PR を 2 段階に分けて出す:

#### E-1. PR-A: 動的 avatar セット転送機構

- firmware 側の avatar セットローダ + PSRAM テーブル管理
- gateway 側の load_avatar_set MCP tool
- 既存 set_avatar / set_eyes / set_mouth との互換維持
- 既存 14 シンボル方式 = layered mode のサブセットとして動く構造で書く

#### E-2. PR-B: matrix mode 対応

- mode 切替対応、matrix mode 描画ロジック
- PR-A の延長として位置づける

#### デフォルト art は upstream メンテナに依頼

`avatar_images.cc` の placeholder TODO を埋める「ｽﾀｯｸﾁｬﾝ標準キャラの layered mode 1 セット (14 個)」は、文字通り stackchan-mcp の **「顔」** になるリソース。これを我々 (SAIVerse 側) が作って PR するのは越権で、デフォルトキャラのデザイン判断は upstream メンテナ (= 如月もちさん) が握るべき領域。

我々のスタンス:

- PR-A / PR-B で「動的 avatar セット転送機構 + 2 モード対応」のインフラだけを upstream に届ける
- デフォルト art については「PR-A/B で導入される avatar セット形式 (layered mode の manifest schema) に沿った art を作っていただければ、placeholder TODO が解決します」と如月もちさんに伝える形にする
- SAIVerse のユーザー体験としては独自 avatar (= addon の画像生成パイプラインで作るペルソナ別 avatar) があれば成立するので、デフォルト art の completion はクリティカルパス上にない

## Phase 分割

### Phase 4.5-a: firmware 拡張

- stackchan-mcp firmware fork (既存の `temp/stackchan-mcp` を流用、新ブランチ `feature/dynamic-avatar-set`)
- avatar セットローダ + PSRAM テーブル管理
- mode 別描画ロジック (layered / matrix 両対応)
- 既存 set_avatar / set_eyes / set_mouth の挙動を新ロジック上に再構築
- 実機検証: layered mode のテストセット (= placeholder データを実 art に置き換えた最小セット) で表情切替が成立すること

### Phase 4.5-b: gateway MCP tool 拡張

- gateway fork (firmware と同じリポジトリ内)
- load_avatar_set MCP tool 実装
- 転送プロトコル (chunked WS binary)
- 進捗 push 機構
- 実機検証: SAIVerse の MCP client から load_avatar_set を呼んで実機にセットが乗ること

### Phase 4.5-c: addon storage + ペルソナ別 avatar 永続化

- addon storage schema (avatar_sets テーブル / ファイル構造)
- ペルソナ憑依フック (OccupancyManager イベント購読)
- 同セット連続 load のスキップ判定
- 実機検証: 2 ペルソナを交互に憑依させて avatar セットが切り替わること

### Phase 4.5-d: 画像生成 UI (段階的生成フロー)

- addon 管理画面に avatar 設定セクションを追加
- Step 1: 表情差分 5 種生成 + プレビュー + OK/NG レビュー
- Step 2: OK 後に matrix 90 枚 / layered 14 個の本生成
- プレビュー (全枚分)
- 「Stack-chan に転送」ボタン
- 1 枚ずつ再生成 API は内部実装、UI 露出は将来拡張で
- 実機検証: 元顔 1 枚から Step 1 を回して OK 判断 → Step 2 で matrix mode セットを生成 → ESP32 に転送 → 表情切替できること

### Phase 4.5-e: upstream PR + デフォルト art 依頼

- PR-A (動的 avatar セット転送機構) を stackchan-mcp 本流に提出
- PR-B (matrix mode 対応) を PR-A merge 後または並行で提出
- 如月もちさんに「PR-A/B で導入される avatar セット形式に沿ったデフォルト art を作っていただければ placeholder TODO が解決」と伝える (= デフォルト art は upstream メンテナの作業として位置づける)
- merge を待つ間は SAIVerse fork (`temp/stackchan-mcp`) を `mcp_servers.json` で参照

完了後、Phase 5 (Avatar 感情連動) と Phase 6 (口パク) の前提が成立する。

## 設計判断の理由

### なぜ動的転送機構を新設するか (固定焼き込みじゃダメか)

SAIVerse の認知モデルでは複数ペルソナが同じ Vessel に憑依する想定 (= persona_cognitive_model.md)。固定焼き込みだと「どのペルソナでも同じ顔」になり、認知モデルの「Vessel Building = 身体」メタファーの説得力が失われる。「ペルソナの顔がない」体験は v0.4 で立てた目標 (intent doc §「最初のリリースで成立させる体験」5番) から見て大きな後退になる。

動的転送機構を作るコストは、firmware に WS binary frame ハンドラ + PSRAM テーブル管理を足す程度。upstream PR で本流に取り込まれれば、他の stackchan-mcp ユーザーにも独自 avatar の道が開ける = upstream への貢献としても価値が大きい。

### なぜ layered / matrix の 2 モードを両方サポートするか

- **layered mode**: upstream stackchan-mcp の既存方式と互換、art リソースが少なくて済む、Phase 6 口パクで部分更新できる利点。シンボリックなキャラデザ (= ｽﾀｯｸﾁｬﾝ標準キャラ) には適している
- **matrix mode**: 画像生成 AI の現実的な能力 (= 元顔 + 表情指示で表情差分を作るのは得意、パーツ分解は苦手) と整合する、写実的 / 高品質 avatar に適している

どちらかに絞ると「既存 stackchan-mcp ユーザーが不満」または「画像生成 AI フローと噛み合わない」のどちらかが残る。両方サポートする設計コストは mode フラグ + 描画分岐だけで小さい。

### なぜ画像生成パイプラインを vessel 汎用基盤にしないか

Vessel ごとに描画特性が大きく違う:
- Stack-chan: 160×120 LCD、表情符号化に向く
- 仮想 2D ペルソナ (将来想定): 高解像度、フルボディ、ポーズ差分も
- 別 robot (将来想定): RGB 7セグ、LED マトリックス等の極端に低解像度な表現

これらを generic な「ペルソナ視覚化セット生成」で扱おうとすると、抽象化が早すぎて Vessel ごとに特殊化が必要な部分が出てくる。早すぎる抽象化を避け、まず Stack-chan 1 機種で動かしてから、共通化できる関数 (例: 画像生成 API 呼び出し / 進捗管理 / プレビュー) を切り出す方が筋がいい。

### なぜ upstream PR を 2 段階に分けて、デフォルト art は依頼にとどめるか

- PR-A (動的セット転送) だけ単独で merge 可能。既存ユーザーへの破壊的変更ゼロ
- PR-B (matrix mode) は PR-A 依存だが、PR-A だけ merge されても layered mode の改善として成立する
- デフォルト art (placeholder TODO の解決) は stackchan-mcp の「顔」になるリソース。デザイン判断は upstream メンテナの領域で、我々が PR で提供するのは越権。我々は形式 (manifest schema + layered mode) だけを整え、art は依頼で渡す

2 段階に分けることで、merge スピードを最大化しつつ、SAIVerse 側は PR-A/B が merge された時点で本流に切り替えられる。デフォルト art の completion は SAIVerse のユーザー体験に対してクリティカルパス上にない (= ペルソナ別 avatar で代替できる) ので、依頼ベースで構わない。

### なぜ画像生成を 2 段階 (Step 1 + Step 2) に分けるか

90 枚を一気に生成して品質に納得できなかった場合、API コストが無駄になる。表情差分 5 種を先行で生成してレビューさせれば、「この元顔から良い差分が作れるか」を最小コストで確認できる。Step 1 で NG ならそこで終了、後続の 84 枚 / 8 パーツの生成コストは払わない。

最初の UI 露出は Step 1 全体の OK/NG のみに絞る理由は、「気に入らない 1 枚だけ再生成」UI の設計負荷が想定以上にかかる可能性があるから (= プレビュー UI 上で個別画像をクリックして再生成 → 差し替え保存 → 結果プレビュー再描画 のフロー)。内部 API は 1 枚再生成に対応する構造にしておき、UI 拡張は実機検証の体感で「もう一段欲しい」と思った時に追加する。

## 関連ドキュメント

- `docs/intent/stackchan_vessel.md` — Vessel 統合本体、§H と Phase 5/6 の前提を本書が再構成する
- `docs/intent/llama_cpp_multimodal_slot_save_fork.md` — fork 運用の前例、同じパターンで stackchan-mcp fork も運用する
- `docs/intent/multimodal_input_pipeline.md` — MediaBuffer 経路、take_photo の戻り値はこちら経由
- `docs/intent/persona_cognitive_model.md` — Vessel = 身体メタファー、ペルソナ別 avatar の動機

## 進捗 (2026-05-14)

### 完了 (Phase 4.5-a/b の skeleton + end-to-end 疎通確認)

実機動作確認できた要素:

- AvatarSet 動的セット転送機構 (HTTP fetch + SHA256 + PSRAM Load)
- load_avatar_set MCP tool (gateway 側)
- face 切り替え (layered mode、 14 シンボルのうち face × 6 のみ AvatarSet 経由)
- end-to-end: SAIVerse spell → gateway HTTP staging → ESP32 が `avatar_set_fetch` WS notify を受けて HTTP GET → SHA256 検証 → AvatarSet::Load (PSRAM 確保) → avatar_set_loaded WS で応答 → LCD に新画像が描画される

主要 commit:

- `stackchan-mcp` (fork: maha0525/stackchan-mcp)
  - `feature/dynamic-avatar-set` (= PR-A/B 出す予定): `2dcfe5a` (AvatarSet + Fetcher scaffold) → `3bd241c` (WS handler) → `e6ff313` (gateway load_avatar_set tool) → `98f34b0` (CI trigger for feature/**+dev/**+dispatch) → `d9a402c` (SendText public) → `a42fe0b` (`%zu` → `%u` fix)
  - `dev/integration` (= PCM stream + avatar 統合、 実機テスト用、 SAIVerse の mcp_servers.json が参照中): 上記を順次 merge 済み
- `SAIVerse` 本体 (`feature/memory-notes-and-organize` ブランチ、 ローカル commit のみ): `37b455c` (intent doc 起草) → `6c89235` (intent doc HTTP fetch refine) → `547ffab` (mcp_client.py で subprocess stderr を session log dir に forward)
- `saiverse-stackchan-addon` (= addon repo): `449679a` (Phase 4' A-3-c building_ids 反映 + Phase 4.5 動作確認準備、 mcp_servers.json で git ref を `@dev/integration` に切替 + `scripts/generate_test_avatar_set.py` 追加)

### 次セッション残作業 (Phase 4.5-a の残り)

依存関係: (1) → (3)、 (2) は独立

1. **eyes / mouth の AvatarSet 統合** — 現状 face だけが AvatarSet 経由、 eyes / mouth は static 1×1 placeholder の fallback 経路のまま。 `set_eyes` / `set_mouth` / `set_mouth_sequence` ハンドラと、 blink state machine の image lookup を AvatarSet::GetEyes / GetMouth 経由に切り替える
2. **in-progress 中の表情切替保留機構** (intent doc 不変条件 #6) — Fetcher が走っている最中の `SetAvatarExpression` 呼び出しを pending として保持、 Load 完了後に最後の状態だけ apply。 現状は worker task と LVGL task で race の窓があるが、 実装的に Mutex か pending state にする
3. **matrix mode 対応** — `AvatarImageFor` を `(face, eyes, mouth)` 3 軸 lookup に拡張。 これは (1) と密接 (= matrix モードでは face/eyes/mouth を組み合わせた 1 枚を引く)

その先 (= Phase 4.5-c/d/e):

- 4.5-c: addon storage で avatar セット永続化 + 憑依時自動ロード
- 4.5-d: addon 管理 UI に段階的画像生成フロー (Step 1: 5 種差分 + レビュー → Step 2: matrix 84 枚 or layered 8 パーツ)
- 4.5-e: upstream PR-A (動的セット転送機構) / PR-B (matrix mode) 提出 + 如月もちさんにデフォルト art 依頼

### 次セッション向け操作 cheatsheet

#### Stack-chan firmware build + flash (= 修正サイクル)

ローカル ESP-IDF (推奨、 incremental ビルドで数十秒):

```powershell
# PowerShell tool 経由で実行 (= MSys/MinGW Bash は ESP-IDF 非サポート)
$env:IDF_TOOLS_PATH = "C:\Espressif"
. C:\Espressif\frameworks\esp-idf-v5.5.4\export.ps1 | Out-Null
Set-Location C:\Users\shuhe\workspace\SAIVerse\temp\stackchan-mcp\firmware
idf.py build               # incremental (変更ファイルのみ再 compile)
idf.py -p COM3 app-flash   # app partition のみ (NVS 保持 = WiFi 設定そのまま)
```

注意点:

- **`idf.py app-flash` を使う** — NVS partition を保持するので WiFi/トークン再設定不要。 `esptool write_flash 0x0 merged-binary.bin` は NVS まで上書きする (= captive portal で再設定が必要、 開発サイクルでは避ける)
- `temp/stackchan-mcp/firmware/sdkconfig.defaults.local` に `CONFIG_COMPILER_OPTIMIZATION_DEBUG=y` 設定済み (= panic backtrace が addr2line で clean に解析できる)
- ビルド成果物: `temp/stackchan-mcp/firmware/build/xiaozhi.elf` (= addr2line 用)、 `build/merged-binary.bin` (= 初回 flash 用、 NVS 消える)
- 過去履歴: feedback_esp32_merged_flash_wipes_nvs.md / feedback_esp_idf_nano_printf_no_zu.md

CI 経由 (= ローカル使えない場合の fallback、 ~6 min):

```bash
gh run list --repo maha0525/stackchan-mcp --branch <branch> --limit 3
gh run download <run_id> --repo maha0525/stackchan-mcp -D /tmp/fw_avatar  # run_in_background=true 推奨
```

#### Serial output capture

stack-chan の COM3 出力を session log dir に流す常駐スクリプト:

```bash
/c/Users/shuhe/workspace/SAIVerse/.venv/Scripts/python.exe \
  /c/Users/shuhe/workspace/SAIVerse/temp/stackchan_serial_capture.py
# run_in_background=true で起動。 出力先: ~/.saiverse/user_data/logs/<最新>/stackchan_serial.log
```

flash 前に必ず kill して COM3 解放:

```powershell
Get-WmiObject Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -like "*stackchan_serial_capture*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

#### Panic 解析手順 (= ESP32 crash 再現時)

1. serial log で `Guru Meditation Error` を grep
2. `Backtrace: 0x... 0x... ...` の address を取って addr2line で symbol 解析:

```bash
/c/Espressif/tools/xtensa-esp-elf/esp-14.2.0_20260121/xtensa-esp-elf/bin/xtensa-esp-elf-addr2line.exe \
  -e /c/Users/shuhe/workspace/SAIVerse/temp/stackchan-mcp/firmware/build/xiaozhi.elf \
  -f -p 0x<addr1> 0x<addr2> ...
```

注意: **debug build じゃないと addr2line は混乱する** (= release `-Os` で aggressive inline され symbol が beste-effort で誤割り当て)。 sdkconfig.defaults.local の `CONFIG_COMPILER_OPTIMIZATION_DEBUG=y` で OK。

#### SAIVerse の subprocess log 確認

`tools/mcp_client.py` 改修 (547ffab) で、 stackchan-mcp gateway の log (= `Gateway started: WS on 0.0.0.0:18765`、 `ESP32 ready: ...` 等) が以下のファイルに流れる:

```
~/.saiverse/user_data/logs/<session>/mcp_subprocess_saiverse-stackchan-addon__stackchan.log
```

#### 環境構成のメモ

- AddonConfig (saiverse.db の `addon_config` テーブル) 値: `gateway_ws_port=18765`、 `gateway_capture_port=8766`、 `vision_host=192.168.0.127`、 `master_token` / `pcm_token` 既設定、 `vessel_building_id=stackchan_room`
- mcp_servers.json (addon repo) は git ref を `@dev/integration` 指定中。 PR-A/B が upstream merge されたら upstream tag に戻す
- ESP32 device: MAC `44:1b:f6:df:58:e0`、 COM3 で接続中、 WiFi + WS URL `ws://192.168.0.127:18765` + Authorization Token (= master_token) で captive portal 設定済み。 NVS 保持で再設定不要

#### 重要な memory 参照

特に Phase 4.5 関連で踏んだ罠 (全部 memory 化済み):

- `feedback_esp_idf_nano_printf_no_zu.md` — ESP-IDF nano-printf の `%zu` バグ
- `feedback_esp32_merged_flash_wipes_nvs.md` — flash 戦略 (`idf.py app-flash` vs `esptool write_flash`)
- `feedback_check_fork_specific_changes.md` — fork で新 branch を upstream/main から派生するとき fork-only 改変が落ちる
- `feedback_artifact_source_branch.md` — 実機 flash 用 artifact は dev/integration から (feature/* は upstream PR 用)
- `feedback_mcp_servers_spell_tools_required.md` — addon に新 MCP tool を gateway 側に追加するなら mcp_servers.json の `spell_tools` にも entry をセットで追加
- `feedback_bash_tool_internal_error_on_conflict.md` — Bash tool の `H.replace` 内部 error は出力で発生 (コンフリクト marker や HEREDOC の特殊文字)。 出力リダイレクト or `-F file` で回避
