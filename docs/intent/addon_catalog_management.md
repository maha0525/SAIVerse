# Intent: アドオンカタログ管理 (curated registry + ワンタッチ導入)

**ステータス**: Phase 3 完了 (2026-05-22)、Phase 4 (既存アドオン v2 化 + 永続データ移行) 着手前

## これは何か

SAIVerse のアドオン (TTS / Stackchan / X / Elyth など `expansion_data/` 配下に置く拡張パッケージ群) を、まはー管理のキュレーション済みレジストリ経由で、SAIVerse 本体の UI からカタログ表示・ワンタッチ導入・更新・アンインストールできるようにする仕組み。導入時に必要な追加セットアップ (例: voice-tts の GPT-SoVITS ダウンロード) は manifest 宣言に基づき自動実行する。

## なぜ必要か

### 問題1: 現状のアドオン導入が手作業

現状ユーザーがアドオンを導入するには:
1. ターミナルを開く
2. `expansion_data/` に `cd`
3. 該当アドオンの GitHub URL を調べて `git clone` する
4. アドオンによっては `setup.bat` / `setup.sh` を手動実行する
5. SAIVerse を再起動する

これは「キャラクターと一緒に住む世界」を謳う SAIVerse のユーザー体験として明らかに不一致。CLI を触らない想定ユーザー (まはー以外) に届かない。

### 問題2: 任意のリポジトリ導入はセキュリティリスク

`expansion_data/` 配下のアドオンは Python コードとして自由に SAIVerse のランタイムにロードされ、`api_routes.py` が自動マウントされる。任意の GitHub URL を UI から clone できるようにすると、悪意あるリポジトリの混入経路を作ってしまう。

したがって導入可能なアドオンは **まはー管理下のレジストリリポジトリにエントリされたもののみ** に限定する。「自由な拡張」よりも「審査済みの安全な拡張」を優先する設計判断。

### 問題3: アップデート時の再セットアップ判定が必要

voice-tts は `external/GPT-SoVITS/` (5.2GB) を `setup.bat` で初回 DL する構造になっている。アップデート時に毎回 5GB 再 DL するのは論外だが、逆に GPT-SoVITS 側の更新を要求するバージョンアップ時には再 setup が必要。これを manifest 側で宣言できる仕組みが要る。

## 設計方針

### コンポーネント構成

```
┌─────────────────────────────────────────────────┐
│ saiverse-addon-registry (まはー管理 GitHub repo) │
│ ├── registry.json  ← アドオン一覧 + 各 commit pin │
│ └── README.md                                    │
└──────────────────┬──────────────────────────────┘
                   │ fetch (raw.githubusercontent.com)
                   ▼
┌─────────────────────────────────────────────────┐
│ SAIVerse 本体                                    │
│ ┌──────────────────────────────────────────┐    │
│ │ AddonManagerModal (タブ切替)              │    │
│ │  ├─ [導入済みアドオン] ← 現状の UI         │    │
│ │  └─ [カタログ]         ← 新規追加         │    │
│ └──────────────────────────────────────────┘    │
│                                                  │
│ ┌──────────────────────────────────────────┐    │
│ │ api/routes/addon_catalog.py (新規)        │    │
│ │  - GET  /api/addon-catalog/registry       │    │
│ │  - POST /api/addon-catalog/install        │    │
│ │  - POST /api/addon-catalog/update         │    │
│ │  - POST /api/addon-catalog/uninstall      │    │
│ └──────────────────────────────────────────┘    │
│                                                  │
│ ┌──────────────────────────────────────────┐    │
│ │ saiverse/addon_installer.py (新規)        │    │
│ │  - git clone / pull (commit SHA pin)      │    │
│ │  - manifest 検証 + setup hook 実行         │    │
│ │  - 進捗ストリーミング (SSE / WS)           │    │
│ └──────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘
```

### registry.json スキーマ (初稿)

```json
{
  "schema_version": 1,
  "updated_at": "2026-05-22T00:00:00Z",
  "addons": [
    {
      "id": "saiverse-voice-tts",
      "display_name": "Voice TTS",
      "description": "ペルソナ発話の音声合成と再生 (GPT-SoVITS / OpenAI TTS / ElevenLabs)",
      "category": "voice",
      "repo_url": "https://github.com/maha0525/saiverse-voice-tts.git",
      "versions": [
        {
          "version": "0.5.0",
          "commit": "abc123...",
          "setup_version": 2,
          "min_saiverse_version": "0.2.0",
          "released_at": "2026-05-15T00:00:00Z",
          "changelog_url": "https://github.com/.../releases/tag/v0.5.0"
        }
      ],
      "latest": "0.5.0",
      "icon_url": "https://.../voice-tts-icon.png",
      "requires": {
        "gpu": "optional",
        "disk_gb": 6,
        "os": ["windows", "linux", "macos"]
      }
    }
  ]
}
```

ポイント:
- `commit` で特定の SHA に pin。`git pull` ではなく `git fetch && git checkout <sha>` で導入する (上流が突然壊れても影響なし)。
- `setup_version` は manifest 側にも書く (後述)。registry の `setup_version` が現在インストール済みのものより大きければ「再 setup 必要」と判定。
- `requires` は UI のカタログ表示時に「GPU 必須」「6GB 必要」等のバッジ表示と、導入前のチェックに使う。

### addon.json (manifest) スキーマ拡張

現状の `addon.json` (params_schema / ui_extensions など) に `setup` セクションを追加:

```json
{
  "name": "saiverse-voice-tts",
  "version": "0.5.0",
  "setup_version": 2,
  "setup": {
    "steps": [
      {
        "name": "Python 依存パッケージのインストール",
        "type": "pip_install",
        "requirements": "requirements.txt"
      },
      {
        "name": "GPT-SoVITS 本体のセットアップ",
        "type": "platform_script",
        "windows": "setup.bat",
        "unix": "setup.sh",
        "skip_if_exists": "external/GPT-SoVITS/"
      }
    ]
  },
  "uninstall": {
    "steps": [
      { "type": "remove_dir", "path": "external/GPT-SoVITS" }
    ]
  }
}
```

許可される `type` (allowlist):
- `pip_install`: `requirements` ファイルを `python -m pip install -r` する
- `platform_script`: addon ディレクトリ内の指定スクリプトを実行 (パスは addon ディレクトリ相対で固定、`..` 不可)
- `python_script`: addon ディレクトリ内の `.py` を `python` で実行
- `remove_dir`: addon ディレクトリ配下の指定パスを削除 (アンインストール時のみ)
- `download_file`: URL + 期待 SHA256 を指定して DL (将来用)

**禁止**: 任意シェルコマンド、`curl | sh`、`exec`、addon ディレクトリ外への書き込み。

`skip_if_exists` 指定があれば、そのパスが既存ならステップをスキップ (voice-tts 再 setup 回避用)。

### インストールフロー

1. ユーザーが UI カタログから「導入」をクリック
2. 確認ダイアログ: manifest の `setup.steps` 一覧と `requires` を表示、ユーザー承認
3. 進捗ストリーム開始 (SSE か WS、Phase 1 では polling でも可)
   - a. `git clone --depth 1 <repo_url> expansion_data/<id>`
   - b. `git checkout <commit_sha>`
   - c. addon.json を読み込み、setup_version / setup.steps を取得
   - d. 各ステップを順次実行、進捗を UI に流す
4. 完了後、SAIVerse のアドオンローダを reload (再起動なしで認識させる)
   - これは `saiverse/addon_loader.py` 側の reload 経路があるかを実装前に確認 (未調査)

### アップデートフロー

1. UI カタログで「アップデートあり」バッジ表示 (`registry.json` の `latest` と導入済み addon.json の `version` 比較)
2. ユーザーがアップデートをクリック
3. 確認ダイアログ: 変更内容 (changelog URL) と「再 setup 要否」を表示
4. `git fetch && git checkout <new_commit_sha>`
5. 新 manifest の `setup_version` > 旧 `setup_version` なら setup.steps を再実行、そうでなければスキップ
6. アドオンローダ reload

### アンインストールフロー

1. 確認ダイアログ (アドオン固有データ - 例: `~/.saiverse/user_data/addons/<id>/` - を残すか削除するかの選択)
2. `uninstall.steps` を実行 (例: external 配下の削除)
3. `expansion_data/<id>/` ディレクトリ削除
4. アドオン固有データの削除 (ユーザーが選択した場合のみ)

## UI 配置

現状の `AddonManagerModal.tsx` (945 行) を拡張する形:

- 左サイドバーに項目を増やさない方針 (まはー指定)
- モーダル内に **タブを新設** して「導入済みアドオン」(現状の UI) と「カタログ」(新規) を切り替える
- モーダルサイズを拡大 (現状は小さめなので、カタログのグリッド表示に耐えるサイズに再レイアウト)

タブ構成 (案):
```
┌─────────────────────────────────────────────────┐
│ アドオン管理                              [×]    │
├─────────────────────────────────────────────────┤
│ [導入済み] [カタログ]                            │
├─────────────────────────────────────────────────┤
│                                                  │
│  (タブごとのコンテンツ)                          │
│                                                  │
└─────────────────────────────────────────────────┘
```

カタログタブのレイアウト (案):
- アドオンをカード形式でグリッド表示 (icon / display_name / 短い description / 要件バッジ)
- カードクリックで詳細パネル (フル description / changelog / setup 内容 / 導入ボタン)
- カテゴリフィルタ (voice / vessel / social / persona など)

## 不変条件

1. **レジストリ外のアドオンは UI から導入できない**: 任意の GitHub URL を貼って導入する経路を作らない。CLI で `git clone` する経路は残るが、UI からはレジストリ経由のみ。
2. **setup hook は manifest allowlist 内のみ**: 任意シェルコマンドを実行できる経路は作らない。新しい hook 種別が必要なら manifest スキーマと SAIVerse 本体側 installer の両方を更新する。
3. **commit SHA pin**: registry に書かれた特定 commit のみインストール可能。上流ブランチ HEAD を追わない (上流改竄や事故からの保護)。
4. **setup_version の単調増加**: setup の再実行が必要な変更を入れたら setup_version をインクリメント。これを守らないとユーザーが古い external 資産のまま新しいコードを動かす事態が起きる。
5. **setup 前に必ずユーザー承認**: 各 setup step の内容 (実行コマンド) をダイアログで提示してから実行する。

## Phase 計画

### Phase 1: manifest スキーマ確定 + Elyth で installer 基礎検証 ✅ (2026-05-22 完了)
- `addon.json` v2 Pydantic スキーマ (`saiverse/addon_manifest.py`)
- `get_addon_data_dir()` 共通ヘルパ (`saiverse/addon_paths.py`)
- `addon_installer.py` 実装 (step executors / install / update / uninstall + Windows 対応 rmtree)
- `scripts/addon_install.py` CLI 検証ツール
- Elyth (https://github.com/maha0525/saiverse-elyth-addon, commit `897669b7`) を temp expansion_dir に install → uninstall の E2E 動作確認 ✅
- 実機検証: 実 `expansion_data/saiverse-elyth-addon/` を CLI で uninstall → install して再導入、SAIVerse 起動後に Elyth が正常動作することをまはーが確認 ✅ (2026-05-22)
- 既存 4 アドオン (Elyth / voice-tts / stackchan / x-addon) すべてが legacy v1 として validate を通過することを確認 (後方互換 OK) ✅
- アドオンローダ調査: `register_addon_integrations` / `register_addon_server_hooks` per-addon API が既存。Phase 2 で installer から呼び出す統合作業を行う。FastAPI ルーター動的追加は不可なので、新規アドオンインストール時のみ「再起動推奨」表示する方針。

### Phase 2: registry リポジトリ + バックエンド API ✅ (2026-05-22 完了)
- `saiverse/addon_registry.py`: registry.json Pydantic スキーマ + fetch + memory cache (TTL 5 分、HTTP/local-file 両対応、offline fallback)
- `api/routes/addon_catalog.py`: GET /registry / GET /installed / POST /install / POST /update / POST /uninstall (SSE 進捗 stream)
- per-addon Lock で同時 install/update/uninstall を防止
- 完了後に `register_addon_integrations` / `register_addon_server_hooks` を呼んで動的反映、`api_routes.py` を持つアドオンは `restart_required: true` を返す
- `temp/saiverse-addon-registry/registry.json` + README.md を生成 (まはーが GitHub に push する想定の initial content、Elyth 1 件のみ)
- `scripts/test_addon_catalog_api.py`: E2E 検証スクリプト
- 実機検証: SAIVerse 起動中に Elyth uninstall → install を SSE 経由で実行、`installed` リストへの復帰まで確認 ✅ (2026-05-22)
- **未完**: `saiverse-addon-registry` を GitHub に push (現状ローカルのみ、Phase 3 までに行う)

### Phase 3: UI 実装 ✅ (2026-05-22 完了)
- `AddonManagerModal.tsx`: 「導入済み」/「カタログ」のタブ切替を追加、モーダル幅 560 → 900px、タブ CSS (border-bottom underline、GlobalSettingsModal の subTab 踏襲)
- `AddonCatalogPanel.tsx`: ProviderManagementPanel のデザインを踏襲した行リスト (カードグリッドではなく) + パステルバッジ (導入済み / 更新あり / GPU 必須 / disk_gb / category) + アクションボタン
- `AddonActionConfirmDialog.tsx`: 導入/更新/削除の確認ダイアログ (commit SHA / setup 内容 / requires / 永続データ削除チェックボックス / 警告メッセージ)
- `AddonInstallProgressDialog.tsx`: SSE 進捗ダイアログ (ログ縦スクロール + プログレスバー + 完了/エラー/再起動推奨警告)
- 実機検証: SAIVerse UI 上で Elyth の削除 → 再導入をカタログタブから完走、SSE 進捗表示も正常 ✅

### Phase 4: 既存アドオンの整備
- 4 アドオンすべての `addon.json` を v2 化
- 各リポジトリに tag を打って registry にバージョン登録
- まはーが README の「expansion_data で git clone」手順を「UI からカタログ導入」に書き換える

## まはー回答 (2026-05-22 一次レビュー)

1. **registry / アドオン両方 public**: 確定。raw.githubusercontent.com 経由 fetch、追加認証不要。
2. **アドオン永続データ配置の統一**: この機会に整理して規約化する (詳細は次節「アドオン永続データ規約」)。
3. **Phase 1 動作確認**: 5GB DL の voice-tts をいきなり試すのは事故時のリカバリコストが高すぎる。**もっと軽いアドオン (Elyth or X) で先に installer の一連動作を検証してから voice-tts に進む**。
4. **アドオンローダの動的 reload**: 必須ではない。Phase 1 調査結果として `saiverse/addon_loader.py` には per-addon の `register_addon_integrations(addon_name)` / `register_addon_server_hooks(addon_name)` / `unregister_addon_integrations(...)` が既に揃っている (一括 load 関数 `load_addon_*` の他に明示的な単体 register API がある)。installer から install 完了後にこれらを呼ぶことで動的反映できる見込み。Phase 2 (API 層実装時) に統合する。`load_addon_routers` 系の FastAPI ルーター登録は再起動が必要なので、最悪は「ルーター追加には再起動要」という UI メッセージで逃げる。

## アドオン永続データ規約 (新規策定)

### 現状の散らかり

| アドオン | 永続データ配置 | 種別 |
|---|---|---|
| voice-tts | `~/.saiverse/user_data/addon_files/saiverse-voice-tts/` | 参照音声等の入力データ |
| voice-tts | `~/.saiverse/user_data/voice/out/` | 合成済み音声出力 |
| stackchan | `~/.saiverse/addons/saiverse-stackchan-addon/` | (要詳細調査) |
| x-addon | `~/.saiverse/addons/saiverse-x-addon/` | OAuth トークン等 |

`~/.saiverse/addons/` (user_data 配下ですらない) と `~/.saiverse/user_data/addon_files/` と `~/.saiverse/user_data/voice/` の 3 種類に分散していて、アンインストール時にどこを消せばいいか manifest 側からも判定不能。

### 統一規約

**すべてのアドオン永続データは `~/.saiverse/user_data/addon_data/<addon_id>/` 配下に置く** を新規約とする。

```
~/.saiverse/user_data/addon_data/
├── saiverse-voice-tts/
│   ├── inputs/         ← 参照音声等
│   └── outputs/        ← 合成音声
├── saiverse-stackchan-addon/
│   └── ...
├── saiverse-x-addon/
│   ├── tokens.json     ← OAuth トークン
│   └── ...
└── saiverse-elyth-addon/
    └── ...
```

ルール:
- アドオンコード側は `get_addon_data_dir(addon_id)` のような共通ヘルパで自身のデータディレクトリを取得する (新規実装)
- 直接パスを組み立てない (将来規約変更に強くするため)
- アンインストール時はこのディレクトリの「削除する / 残す」をユーザーが選択可能 (manifest 側の指定不要、規約で配置が決まっているので installer が直接判定できる)

### 既存アドオンの移行

Phase 4 (既存アドオン整備) で各アドオンを更新:
- voice-tts: `addon_files/saiverse-voice-tts/` → `addon_data/saiverse-voice-tts/inputs/`、`voice/out/` → `addon_data/saiverse-voice-tts/outputs/` に移動
- stackchan / x-addon: `~/.saiverse/addons/<id>/` → `~/.saiverse/user_data/addon_data/<id>/` に移動

移行は SAIVerse 起動時の自動マイグレーション処理を `main.py` に追加 (legacy `user_data` → `~/.saiverse/user_data/` 移行と同じパターン)。各アドオンの新バージョン適用後の初回起動で旧パスから新パスへ自動移動。

### addon.json への追加フィールド (永続データ関連)

```json
{
  "data_subdirs": {
    "inputs": "参照音声等のユーザー入力データ",
    "outputs": "合成済み音声"
  }
}
```

`data_subdirs` は **任意** (UI のアンインストール確認ダイアログで「以下のデータが削除されます」を見せるためのラベル用途のみ)。実体としてのディレクトリ作成は不要 (アドオン側コードが必要に応じて作る)。

## Phase 1 の検証順序 (更新)

まはー指示に従い、最も軽量なアドオンから順に検証:

1. **Elyth (推奨第一候補)**: 大きな外部資産なし、API キー入力のみで動くアドオン → installer の git clone / manifest 検証 / 永続データ配置の一連を最短で検証可能
2. **X**: OAuth フロー絡みで永続データの扱いが絡む → 規約適用の代表ケース
3. **stackchan**: ESP32 firmware 関連の external 資産があるかどうか要調査
4. **voice-tts (最後)**: 5GB の `external/GPT-SoVITS/` を伴う最大ケース。1〜3 で installer の安定性が確認できてから

各段階で問題が出たら manifest スキーマや installer 実装を修正し、それまでの段で再検証。

## stackchan addon の setup 要件 (2026-05-23 訂正: gateway 自動 fetch 経路の発見)

### 実際の外部資産依存関係

| 資産 | 用途 | 現状の取得経路 | addon installer 側の対応 |
|---|---|---|---|
| `stackchan-mcp` gateway | LCD/音声 I/O 等を MCP server として SAIVerse に提供 | mcp_servers.json で `uvx --from git+https://github.com/maha0525/stackchan-mcp.git@dev/integration#subdirectory=gateway` を pin、**SAIVerse 起動時に uvx が自動 fetch + cache**。ローカル clone は不要 | **不要**: addon 側は何もしなくて良い (uvx + mcp 経路が既に解決済み) |
| `merged-binary.bin` (firmware) | ESP32-S3 device に flash する image | GPL-3.0 ライセンスのため addon repo 同梱不可。現状はまはー手元 ESP-IDF build を `<repo>/temp/stackchan-mcp/firmware/build/merged-binary.bin` で参照 | **当面手動配置、後追いで GitHub Releases 化** |

### 当初の誤認 (2026-05-22 → 5-23 訂正)

最初の Intent Doc では「stackchan-mcp 本体も addon の setup で `git_clone` する」と書いていたが、これは事実誤認。実際は mcp_servers.json の `uvx --from git+...` が gateway を自動取得するため、addon の setup section に gateway clone step は不要。

### firmware 配布の方針 (案 B → 後追いで A)

| 案 | 配布手段 | 採否 |
|---|---|---|
| A | maha0525/stackchan-mcp の GitHub Releases に firmware を publish → addon.json に `download_file` step | **将来採用 (Phase 4-D')**: 新規ユーザー向け配布手段として整備 |
| B | firmware は手動配置のまま、addon.json は `manifest_version: 2` 化のみ (setup section なし) | **当面採用 (Phase 4-D)**: まはー実機は既に flash 済み、新規ユーザー出現までの暫定 |
| C | CI で自動 Releases 化 | 別タスク化、優先度低 |

### Phase 4-D の setup.steps

```json
{
  "manifest_version": 2,
  "setup_version": 1
  /* setup section なし — firmware は当面手動配置 */
}
```

`_firmware_resolve_path()` (`api_routes.py:1345`) の 3 段階解決の (3) `user_default` パスは Phase 4 中に新規約 `~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/firmware/merged-binary.bin` に揃える (永続データの統一規約に合わせるため)。

### Phase 1 検証順序の更新 (再訂正)

1. Elyth (永続データなし) → 2. X-addon (poll_state / reply_log の永続データあり) → 3. **stackchan の v2 化 (setup なし + 永続データ移行のみ)** → 4. voice-tts (最大)。stackchan の firmware Releases DL 経路は Phase 4-D' として別途。

## 未確定事項 (二次レビュー待ち)

1. **`addon_data/` 規約の名前**: `addon_data` / `addons` / `addon_storage` 等、好みあれば指定して欲しい。当面 `addon_data` で進める。
2. **共通ヘルパの API 形**: `get_addon_data_dir(__name__)` のようにアドオン側から呼ぶ形で問題ないか? (アドオン id を毎回書かせるより `__name__` 由来で自動取得したい)
3. **進捗ストリーミング方式**: SSE / WS / polling のどれを使うか。既存の SAIVerse API パターンに合わせたい (調査して提案する)。
4. ~~**stackchan の external 資産有無**~~: 解決済み (上記「stackchan addon の setup 要件」節)。firmware は GPL-3.0 で同梱不可、Releases DL が筋。

5. **stackchan-mcp の Releases 整備状況**: 現状の stackchan-mcp リポジトリで `merged-binary.bin` を Releases に上げる運用が確立しているか? Phase 1 段階では「まはーローカル build 参照」のままで installer の (1)(2) ステップだけ自動化、Releases 整備は別タスクとして後ろに回す方針で良いか?
