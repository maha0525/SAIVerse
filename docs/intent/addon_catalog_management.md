# Intent: アドオンカタログ管理 (curated registry + ワンタッチ導入)

**ステータス**: ドラフト v2 (まはー回答 1 周目反映済み、レビュー継続中)

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

### Phase 1: manifest スキーマ確定 + voice-tts 適用
- `addon.json` v2 スキーマ策定 (この Intent Doc + 各 type の Python 実装側仕様)
- voice-tts の `addon.json` を v2 化、`setup.bat` を `platform_script` step として登録
- 手動で「installer を CLI から叩いて voice-tts を導入できる」状態まで持っていく
- アドオンローダ側の reload 経路 (実行時インストール対応) を調査・実装

### Phase 2: registry リポジトリ + バックエンド API
- `saiverse-addon-registry` リポジトリをまはー管理下に作成
- `registry.json` に既存 4 アドオン (voice-tts / elyth / stackchan / X) を登録
- `api/routes/addon_catalog.py` を実装 (registry fetch / install / update / uninstall)
- 進捗ストリーミングの実装 (SSE 推奨、既存パターンの調査要)

### Phase 3: UI 実装
- `AddonManagerModal` をタブ切替対応に再レイアウト
- モーダルサイズ拡大
- カタログタブのカードグリッド + 詳細パネル実装
- 確認ダイアログ + 進捗表示

### Phase 4: 既存アドオンの整備
- 4 アドオンすべての `addon.json` を v2 化
- 各リポジトリに tag を打って registry にバージョン登録
- まはーが README の「expansion_data で git clone」手順を「UI からカタログ導入」に書き換える

## まはー回答 (2026-05-22 一次レビュー)

1. **registry / アドオン両方 public**: 確定。raw.githubusercontent.com 経由 fetch、追加認証不要。
2. **アドオン永続データ配置の統一**: この機会に整理して規約化する (詳細は次節「アドオン永続データ規約」)。
3. **Phase 1 動作確認**: 5GB DL の voice-tts をいきなり試すのは事故時のリカバリコストが高すぎる。**もっと軽いアドオン (Elyth or X) で先に installer の一連動作を検証してから voice-tts に進む**。
4. **アドオンローダの動的 reload**: 必須ではない (確認した範囲では `addon_loader.py` / `addon_external_loader.py` に明示的 reload 経路は見当たらないが、まはーの理解では動的反映されるはず → Phase 1 で実機検証する範囲に含める)。最悪「再起動要」のメッセージ表示で逃げる。

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

## stackchan addon の setup 要件 (2026-05-22 調査)

stackchan addon は **2 種類の外部資産**を伴う、現状の addon 群で最も複雑なセットアップケース:

### 必要な外部資産

| 資産 | 用途 | 取得元 | 現状の取得手段 |
|---|---|---|---|
| `stackchan-mcp` リポジトリ本体 | gateway (Python, uv で実行) を subprocess 起動 | https://github.com/maha0525/stackchan-mcp (推定) | 手動 git clone (= `temp/stackchan-mcp/`) |
| `merged-binary.bin` (firmware) | ESP32-S3 への UI 書き込み | stackchan-mcp の GitHub Releases (想定) | **未自動化**: まはーは手元 ESP-IDF build を `temp/stackchan-mcp/firmware/build/` 経由で参照、一般ユーザーは手動 DL 想定 |

### 設計上の制約

- **firmware を addon リポジトリに同梱できない**: GPL-3.0 ライセンスのため。`api_routes.py:1353-1354` のコメントで明記。→ setup hook は必ず **upstream Releases からの DL** にする
- **`stackchan-mcp` 本体も addon リポジトリに同梱しない**: 同様にライセンス分離 + サイズの観点。setup hook で git clone する
- **3 段階 path 解決は既存実装を尊重**: `_firmware_resolve_path()` (api_routes.py:1345) の優先順位 (AddonConfig > local build > user_default) は維持。setup hook は (3) の `user_default` 配置だけ自動化する

### setup.steps (案)

```json
{
  "setup": {
    "steps": [
      {
        "name": "Python 依存パッケージのインストール",
        "type": "pip_install",
        "requirements": "requirements.txt"
      },
      {
        "name": "stackchan-mcp gateway のクローン",
        "type": "git_clone",
        "url": "https://github.com/maha0525/stackchan-mcp.git",
        "commit": "<pin>",
        "dest": "external/stackchan-mcp",
        "skip_if_exists": "external/stackchan-mcp/.git"
      },
      {
        "name": "ESP32 firmware (merged-binary.bin) のダウンロード",
        "type": "download_file",
        "url": "https://github.com/maha0525/stackchan-mcp/releases/download/v<X>/merged-binary.bin",
        "sha256": "<expected>",
        "dest_data": "firmware/merged-binary.bin",
        "skip_if_exists": "firmware/merged-binary.bin"
      }
    ]
  }
}
```

`dest_data` は永続データ規約に従って `~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/firmware/merged-binary.bin` に展開される (= 既存 `_firmware_resolve_path()` の (3) と合致するよう addon 側の resolver も更新が必要)。

### manifest allowlist への追加 type

stackchan のために以下を `setup.steps[].type` の allowlist に追加:
- `git_clone`: URL + commit SHA pin + dest (addon ディレクトリ内 or `dest_data` で永続データディレクトリ内)
- `download_file`: URL + SHA256 + dest_data (永続データディレクトリ内固定、addon ディレクトリ書き込みは禁止)

両者とも commit SHA / SHA256 検証必須にして、上流改竄リスクを回避。

### Phase 1 検証順序の更新

stackchan を Elyth / X より後ろに置くのは正解 (firmware DL + gateway clone + ESP32 flash まで絡むので、installer の基礎部分が固まってから取り組むべき)。検証順序:

1. Elyth → 2. X → 3. **stackchan の gateway clone 部分のみ** (firmware DL は skip 可能、まはーローカル build を引き続き使う) → 4. voice-tts (最大) → 5. stackchan の firmware Releases DL 経路 (Releases 整備とセット)

## 未確定事項 (二次レビュー待ち)

1. **`addon_data/` 規約の名前**: `addon_data` / `addons` / `addon_storage` 等、好みあれば指定して欲しい。当面 `addon_data` で進める。
2. **共通ヘルパの API 形**: `get_addon_data_dir(__name__)` のようにアドオン側から呼ぶ形で問題ないか? (アドオン id を毎回書かせるより `__name__` 由来で自動取得したい)
3. **進捗ストリーミング方式**: SSE / WS / polling のどれを使うか。既存の SAIVerse API パターンに合わせたい (調査して提案する)。
4. ~~**stackchan の external 資産有無**~~: 解決済み (上記「stackchan addon の setup 要件」節)。firmware は GPL-3.0 で同梱不可、Releases DL が筋。

5. **stackchan-mcp の Releases 整備状況**: 現状の stackchan-mcp リポジトリで `merged-binary.bin` を Releases に上げる運用が確立しているか? Phase 1 段階では「まはーローカル build 参照」のままで installer の (1)(2) ステップだけ自動化、Releases 整備は別タスクとして後ろに回す方針で良いか?
