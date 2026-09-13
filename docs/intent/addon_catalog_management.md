# Intent: アドオンカタログ管理 (curated registry + ワンタッチ導入)

**ステータス**: Phase 4 完了 (voice-tts 除く、2026-05-23)。voice-tts の Phase 4-E は未着手 (前提だった PR #4 は 2026-05-24 にマージ済み)。公開前に必要な作業と、まはーが決めることは、2026-09-11 に Phase 4-E の節へ洗い出してある

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

### Phase 4: 既存アドオンの整備 ✅ (voice-tts 除く、2026-05-23 完了)
- **4-A 永続データ migration 機構**: `saiverse/addon_migrations.py` に旧パス → 新規約パスの自動移行ロジック (rename / fallback copy / Windows read-only 対応)、 `ENABLED_ADDONS_FOR_STARTUP` 定数で voice-tts を除外、 main.py 起動初期で呼ぶ
- **4-B Elyth v2 化**: addon.json に `manifest_version: 2` 追加 (setup 不要)、 v0.2.0 タグ + push 済み、 registry 掲載
- **4-C X-addon v2 化**: 1 ヶ月放置の WIP (ポーリング本格実装 + spell 可視性 gate) を別 commit で先に整理、 v2 化は別 commit (`fb3306f`) + tag v0.2.0、 永続データ参照を `get_addon_data_dir` に変更、 push 済み、 registry 掲載
- **4-D Stack-chan v2 化**: gateway は uvx 自動 fetch のため setup 不要、 firmware は GPL-3.0 で当面手動配置 (案 B)、 8 ファイルの永続データパス参照変更、 feature/migrate-to-stackchan-mcp を main に FF merge して v0.4.0 タグ + push 済み、 registry 掲載
- **4-F registry public 化**: github.com/maha0525/saiverse-addon-registry を public で作成 + push、 raw.githubusercontent.com 経由で env override なしで fetch できることを実機確認 ✅
- **インシデント (2026-05-23)**: Phase 4-D で stackchan のコード path 変更を push したが migration 起動時呼び出しを「voice-tts 完了後」 と遅延、 結果まはー の SAIVerse 再起動でアバター画像 / ペアリング情報が UI から不可視に。 手動コピーで復旧後、 `ENABLED_ADDONS_FOR_STARTUP` フィルタを設けて voice-tts 以外を起動時 migration 有効化 (`c362b1e`)。 教訓: 「コード path 変更と migration はセット commit」、 詳細は memory `feedback_code_path_migration_coupling.md`

### Phase 4-E: voice-tts v2 化 (未着手、2026-09-11 に公開前の作業を洗い出し)

voice-tts の upstream (元になっているリポジトリ) は `Nature109/saiverse-voice-tts` で、GitHub 上でそれを複製したフォーク `maha0525/saiverse-voice-tts` もある。PR #4〜#6 は maha0525 が作成し、#4 は Nature109 のアカウントがマージした。

この節では、次の語をこの意味で使う。

- **requirements.lock** は、本体が動作を確かめた版に全パッケージを固定した一覧 ([dependency_management.md](dependency_management.md))。
- **constraints** は、pip の `-c` オプションで渡す「この一覧に書いてある版から動かすな」という指定。
- **venv** は、SAIVerse が使う Python の仮想環境 (virtual environment)。本体とアドオンのパッケージは同じ venv に入る。
- **衝突** は、パッケージ同士の版の条件が両立しないこと (pip check の警告文と同じ呼び方)。
- **GIL** (Global Interpreter Lock) は、Python が一度に一つのスレッドにしか処理をさせない仕組み。

#### 2026-09-11 時点の現状

- 旧手順が前提にしていた PR #4 (音声の配信経路と再生キューの変更) は、2026-05-24 にマージ済み。upstream の main ブランチには、それ以降のコミットが無い。
- `addon.json` に `manifest_version` も `setup` も無い。upstream の main ブランチ、フォークの main ブランチ、まはーの手元の `expansion_data/saiverse-voice-tts/` の三つで確認した。このままでは、アドオンカタログから GPT-SoVITS を入れられない。
- 公開の registry.json に voice-tts は載っていない (載っているのは Elyth・X・stackchan)。
- 合成した音声は、旧来の `~/.saiverse/user_data/voice/out/` に保存されている (`tools/speak/playback_worker.py` の `_OUT_DIR`)。
- ペルソナごとの参照音声は、本体のアップロード処理 (`api/routes/addon.py` の `_resolve_file_dir`) によって `~/.saiverse/user_data/addon_files/saiverse-voice-tts/personas/<persona_id>/` に保存され、その絶対パスが DB の `AddonPersonaConfig.params_json` に記録される。voice-tts の合成では、記録された絶対パスがそのまま使われる (`tools/speak/profiles.py` の `_resolve_ref_audio`)。
- 本体の `saiverse/addon_migrations.py` には、voice-tts の参照音声 (`addon_files/saiverse-voice-tts/` から `addon_data/saiverse-voice-tts/inputs/` へ) と合成音声 (`voice/out/` から `addon_data/saiverse-voice-tts/outputs/` へ) を移す処理がすでにある。`ENABLED_ADDONS_FOR_STARTUP` に voice-tts が入っていないので、起動時には実行されない。
- upstream にまだマージされていない PR が 2 本ある。#5 は GPT-SoVITS の合成の別プロセス化 (2026-05-24 に作成)。#6 は、音声のストリームが止まったときに SAIVerse の終了処理が止まったままにならないよう、待ち時間に上限を付ける修正 (2026-08-27 に作成)。まはーの手元の `feature/tts-out-of-process` ブランチ (#5 のブランチ) には、`tools/speak/engine/gpt_sovits.py` と `tools/speak/playback_worker.py` にコミットされていない変更がある。
- voice-tts には Windows 用の `setup.bat` しかなく、`setup.sh` は無い。voice-tts の requirements.txt のコメントには、どのプラットフォームでも使える導入コマンドとして `python scripts/install_backends.py gpt_sovits` が書かれている。

#### 旧手順の 3 番が、いまのままでは成り立たない理由

旧手順 (この節の最後に残してある) の 3 番は、「`setup.bat` を `platform_script` の step で登録する」としていた。これは requirements.lock の導入 (2026-09-02) より前に書かれたもので、いまは次の二つの理由で成り立たない。3 番を計画から外すかどうかは、まはーが決める。

1. `setup.bat` は `scripts/install_backends.py` を通して、GPT-SoVITS の requirements.txt をそのまま `pip install -r` する。その中の `numpy<2.0` と `pydantic<=2.10.6` は、requirements.lock (numpy は Python 3.12 以上で 2.5.2、3.11 で 2.4.6。pydantic は 2.13.5) と両立しない。さらに、`gradio<5` で入る gradio 4.44.1 は `pillow<11` を、それが連れてくる gradio-client 1.3.0 は `websockets<13` を、`torchmetrics<=1.5` で入る torchmetrics 1.5.0 は `numpy<2.0` を要求していて、requirements.lock の pillow 11.3.0・websockets 16.1.1・numpy と衝突する。
2. `saiverse/addon_installer.py` では、requirements.lock が constraints として pip に渡されるのは `pip_install` の step だけで、`platform_script` の step で実行されるスクリプトには渡されない ([addon_setup_scripts_bypass_lock_constraints.md](../issues/addon_setup_scripts_bypass_lock_constraints.md))。旧手順のまま実行すると、venv の本体のパッケージが requirements.lock の版から引き下げられる。

#### 公開前にやること (2026-09-11 にメティスが洗い出した。進め方はまはー未決)

1. **voice-tts 用の GPT-SoVITS の requirements を、voice-tts 側で持つ。** GPT-SoVITS の requirements.txt を元に、次を変える。
   - `gradio` を外す。GPT-SoVITS の推論で読み込まれるコード (`GPT_SoVITS/TTS_infer_pack/TTS.py` から import を辿れる範囲) は、gradio を import していない。gradio を import しているのは、WebUI の 5 つ (`webui.py`、`GPT_SoVITS/inference_webui.py`、`GPT_SoVITS/inference_webui_fast.py`、`tools/uvr5/webui.py`、`tools/subfix_webui.py`) と `tools/my_utils.py` だった。`tools/my_utils.py` を import しているのは、WebUI、学習・データ準備・書き出し用のスクリプト、実験的なストリーミング推論のスクリプト (`GPT_SoVITS/stream_v2pro.py`) で、voice-tts の入口 (`tools/speak/engine/gpt_sovits.py` の `from TTS_infer_pack.TTS import TTS, TTS_Config`) から辿れる範囲には無い。これはコードを辿った確認で、gradio を外した venv で合成してはいない。
   - `numpy<2.0` と `pydantic<=2.10.6` の上限を外す。2026-09-03 00:11 の再起動で、numpy 2.5.2 と pydantic 2.13.5 が入った venv (まはーの開発機、Windows、Python 3.13) のまま、voice-tts の合成は成功している ([dependency_management.md](dependency_management.md) §5 の 6)。
   - `torchmetrics<=1.5` を `torchmetrics>=1.5.2` にする。torchmetrics は `GPT_SoVITS/AR/models/t2s_model.py` が import していて、推論で必要になる。1.5.0 は `numpy<2.0` を要求するが、1.5.2 以降は numpy の上限を持たない (PyPI で確認)。1.5.2 以降で推論が通るかは未確認。
   - voice-tts の requirements.txt に、numba の版の条件を足す ([dependency_management.md](dependency_management.md) §3-3 に残っている宿題)。まはーの開発機の venv では 2026-09-02 20:47 に numba 0.67.0 へ上がっていて、9/3 の合成はその版で成功した。
2. **`saiverse/addon_installer.py` で、`platform_script` / `python_script` の step で実行されるスクリプトの中の pip にも、requirements.lock が constraints として渡るようにする (本体側)。** [addon_setup_scripts_bypass_lock_constraints.md](../issues/addon_setup_scripts_bypass_lock_constraints.md)。
3. **`addon.json` を manifest v2 にする。** `manifest_version`・`setup_version`・`data_subdirs`・`setup.steps` を書く。1 の requirements をどの step で入れるか (`pip_install` の step に分けるか、スクリプトの中に残すか) は未決。スクリプトを使うなら、`platform_script` の step の `unix` 側のスクリプトも要る。いまの `saiverse/addon_installer.py` は、実行中の OS 向けのスクリプトが無いとその step を失敗にせず飛ばして先へ進むので、`setup.sh` が無いまま載せると、macOS と Linux では GPT-SoVITS が入らないまま導入が成功したように見える。
4. **参照音声と合成音声を、永続データの規約の場所へ移す。** 本体の `ENABLED_ADDONS_FOR_STARTUP` に voice-tts を加えると、起動時に参照音声のファイルが `addon_data/saiverse-voice-tts/inputs/` へ移る。しかし DB に記録された参照音声の絶対パスと、本体のアップロード処理の保存先は、古い `addon_files/` のまま残る。合成では記録された絶対パスがそのまま使われるので、参照音声のファイルが見つからずにエラーになる。移行を有効にするときは、同じリリースで次の三つを揃える (2026-05-23 のインシデントの教訓「コード path 変更と migration はセット commit」)。
   - 本体: voice-tts の参照音声のアップロード先を、移行先の `addon_data/saiverse-voice-tts/inputs/` と揃える。ファイルを受け付ける他のアドオンの保存先をどう扱うかも、あわせて決める。
   - 本体: DB の `AddonPersonaConfig.params_json` に記録された、古い場所の絶対パスを書き換える。
   - voice-tts: 合成音声の保存先 (`_OUT_DIR`) を `get_addon_data_dir(...)/outputs/` に変える。
5. **upstream に PR を出してマージし、registry.json に voice-tts を載せる。**
6. **公開前の検証。** 隔離した `SAIVERSE_HOME` と、requirements.lock だけを入れた新しい venv に、アドオンカタログの導入経路で voice-tts を入れ、GPT-SoVITS で実際に声が出るところまで確かめる。[dependency_management.md](dependency_management.md) §5 の 4 で「voice-tts の実導入は本番 venv の同期のときに」と後に回していた検証にあたる。証拠は次の項目ごとに残す。2026-09-11 の時点では、すべて未検証。
   - Windows で、カタログから入れて声が出る: 未検証。
   - macOS で、カタログから入れて声が出る: 未検証 (`setup.sh` が無い)。
   - Linux で、カタログから入れて声が出る: 未検証 (`setup.sh` が無い)。
   - 参照音声を設定済みのペルソナがいる既存の環境に 4 の移行を当てて、そのペルソナの声が出る: 未検証。

#### まはーが決めること (2026-09-11 時点で未決)

- **旧手順の 3 番 (`setup.bat` を `platform_script` の step で実行する) を、計画から外すか。**
- **upstream にまだマージされていない PR #5・#6 を、公開前にマージするか。** #5 は GPT-SoVITS の合成を別プロセスに移して、SAIVerse 本体のどのスレッドが GIL を握り続けても合成が遅くならないようにするもので、[mcp_cancel_scope_spin_gil_starvation.md](../issues/mcp_cancel_scope_spin_gil_starvation.md) の修正方針 B にあたる。GIL 飢餓の元になった本体側の不具合を直す修正方針 A は、2026-05-24 に回帰テストと一緒に実装済み。
- **Irodori-TTS を公開に含めるか。** Irodori-TTS の pyproject.toml は `transformers>=5.12.1,<6`・`peft>=0.18.0`・`gradio>=5.0.0` を要求していて、GPT-SoVITS の requirements.txt の `transformers>=4.43,<=4.50`・`peft<0.18.0`・`gradio<5` と両立しない。同じ venv に両方は入らない。`addon.json` のエンジンの選択肢 (`engine`) には、今も `irodori` がある。

#### 旧手順 (2026-05-23。3 番は、上に書いた理由でいまのままでは成り立たない)

voice-tts repo は `origin = Nature109/saiverse-voice-tts` の共有 repo で、 現在 PR #4 (subscribe-before-open) がレビュー待ち。 v2 化は別 PR で出す方針:

1. まはー: ナチュレに「manifest v2 化 PR を別途出す」連絡
2. main 起点で `feature/manifest-v2` を切る
3. addon.json v2 化 (manifest_version=2, setup_version, data_subdirs)、 `setup.bat` を `platform_script` step で登録 (skip_if_exists=`external/GPT-SoVITS`)、 永続データ参照を `get_addon_data_dir(...)/inputs/` と `get_addon_data_dir(...)/outputs/` に変更
4. PR 提出 → ナチュレレビュー → マージ
5. マージ後、 SAIVerse 本体の `ENABLED_ADDONS_FOR_STARTUP` に `saiverse-voice-tts` を追加、 registry.json に voice-tts エントリを追加

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
