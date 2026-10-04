# Intent: アドオンカタログ管理 (curated registry + ワンタッチ導入)

**ステータス**: 設計中 (2026-10-05)。Phase 4 は voice-tts を除いて完了 (2026-05-23)。「導入時の質問と、アドオン専用の Python 環境」の節 (2026-10-05 起草) が、まはーの確認待ち。voice-tts をカタログに載せる件は [voice_tts_catalog_listing.md](../issues/voice_tts_catalog_listing.md) に切り出した

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

## 導入時の質問と、アドオン専用の Python 環境 (2026-10-05 起草、まはーの確認待ち)

この節は、メティスが 2026-10-05 に書いた設計案で、まはーはまだ読んでいない。まはーの発言で支えられているのは、導入時に何かを選ばせる仕組みを作ることだけ (「なんとかUI上でsetup.bat同様のウィザード動かす感じのシステム作れない？セットアップ中に何かを選ぶみたいなのは普通にあると思うのよ。」)。専用の Python 環境、置き場所、step の種類の扱いは、どれもメティスの提案。

この仕組みを最初に使うのは voice-tts で、voice-tts をどう載せるかは [voice_tts_catalog_listing.md](../issues/voice_tts_catalog_listing.md) にある。

### 要点

アドオンは、導入するときに利用者へ質問を出せる。利用者の答えで、setup の step のうちどれを実行するかが決まる。重いパッケージは、SAIVerse 本体の venv ではなく、そのアドオン専用の Python 環境に入れられる。

### なぜ要るか

voice-tts をカタログに載せようとして、いまの仕組みでは次の三つが解けないと分かった (2026-10-05)。

1. **利用者ごとに入れるものを変えられない。** setup の step は、導入した全員に同じように実行される。たとえば voice-tts では、クラウドの音声エンジンは API キーだけで動くが、GPU で動かす音声エンジンは NVIDIA の GPU と数 GB のダウンロードが要る。GPU の無い利用者も、数 GB をダウンロードすることになる。
2. **アドオンが使うパッケージが、本体の requirements.lock と両立しないことがある。** 両立しないパッケージを本体の venv に入れると、本体のパッケージのバージョンが変わる。いまの仕組みでは、両立しないパッケージを要るアドオンは、カタログからは入れられない。
3. **アドオンの既存の setup スクリプトは、カタログからは実行できないことがある。** たとえば voice-tts の setup.bat は、最後に `pause` で利用者がキーを押すまで終わらず、torch を `--force-reinstall` で本体の venv に入れ直す。

1 は、導入時に質問を出せれば解ける。3 は、スクリプトの処理を manifest に書いた step に置き換えれば解ける。2 は、どちらでも解けない。両立しないパッケージを選んだ利用者の本体の venv は、結局書き換わるからだ。2 を解くのが、アドオン専用の Python 環境になる。

### 利用者から見える流れ

1. カタログでアドオンの「導入」を押す。
2. 確認ダイアログに、そのアドオンが用意した質問が出る (例: voice-tts なら「使う音声エンジンを選んでください」)。
3. 確認ダイアログには、選んだ答えに応じて実行される step の一覧が出る。利用者が承認してから実行する (不変条件 5 はそのまま)。
4. 進み具合は、いまと同じ進捗ダイアログに出る。
5. あとで選択肢を足したくなったら、「導入済み」タブのそのアドオンから、同じ質問をもう一度開く。前に選んだ選択肢は選ばれた状態で出て、外すことはできない。新しく選んだ選択肢について、実行される step の一覧を確認ダイアログで見せ、承認されたら、その選択肢の `when` に当てはまる step だけを実行する。`when` の無い step はやり直さない。

### manifest に足すもの

`setup` に `options` (質問の一覧) を足し、各 step に `when` (この答えのときだけ実行する) と `env` (この step を専用の Python 環境で実行する) を足す。下は形を示すための例。

```json
{
  "setup_version": 1,
  "setup": {
    "options": [
      {
        "id": "engines",
        "question": {"ja": "使う音声エンジンを選んでください", "en": "Choose the speech engines to use"},
        "multiple": true,
        "choices": [
          {"id": "cloud", "label": {"ja": "OpenAI TTS / ElevenLabs (GPU 不要)", "en": "..."}, "default": true},
          {"id": "gpt_sovits", "label": {"ja": "GPT-SoVITS (NVIDIA の GPU が必要、数 GB)", "en": "..."}}
        ]
      }
    ],
    "steps": [
      {"name": "アドオンのパッケージ", "type": "pip_install", "requirements": "requirements.txt"},
      {"name": "GPT-SoVITS 本体", "type": "git_clone", "url": "https://github.com/RVC-Boss/GPT-SoVITS.git",
       "commit": "<40 桁の SHA>", "dest": "external/GPT-SoVITS", "when": {"engines": "gpt_sovits"}},
      {"name": "GPT-SoVITS のパッケージ", "type": "pip_install", "requirements": "envs/gpt_sovits.txt",
       "env": "gpt_sovits", "when": {"engines": "gpt_sovits"}},
      {"name": "GPT-SoVITS のモデルの重み", "type": "python_script", "script": "scripts/download_weights.py",
       "args": ["gpt_sovits"], "env": "gpt_sovits", "when": {"engines": "gpt_sovits"}}
    ]
  }
}
```

- 質問の形は、選択肢から選ぶもの (一つ、または複数) だけにする。自由に文字を入力させる形は作らない。答えが step の引数やコマンドに入り込む経路を作らないためで、答えにできるのは「どの step を実行するか」の選択だけになる。
- `when` の読み方: `{"engines": "gpt_sovits"}` は、質問 `engines` の答えに `gpt_sovits` が含まれるとき、という意味。一つだけ選ぶ質問では、答えがそれと等しいとき。`when` に質問を二つ以上書いたときは、全部が当てはまるときだけ実行する。
- `when` の無い step は、これまでどおり全員に実行される。`options` を持たないアドオン (Elyth・X・stackchan) は、何も変わらない。
- 答えは `~/.saiverse/addon_install/<addon_id>/setup_answers.json` に残す (置き場所の理由は次の小節)。更新で setup_version が上がって step をやり直すときは、この答えを使い、質問を出し直さない。新しいバージョンで、質問が増えたとき、または既にある質問に選択肢が増えたときは、その質問だけを、前の答えが選ばれた状態で出し直す。
- アンインストールしてから導入し直すときは、質問をもう一度出す。残っている答えがあれば、それが選ばれた状態で出す。
- 一度選んだ選択肢を外す操作は、このたびは作らない。

### アドオン専用の Python 環境と、答えの置き場所

- 置き場所は `~/.saiverse/addon_install/<addon_id>/` にする。専用の環境は `envs/<env の名前>/`、答えは `setup_answers.json` として、この下に置く。
- **`addon_data/` の下には置かない。** `addon_data/` は利用者のデータの置き場所で、更新前のスナップショットで毎回まるごと保存される (`scripts/snapshot.py` の `EXCLUDED_FROM_SNAPSHOT` の注記で、除外してはならないと定められている)。数 GB の環境を置くと更新のたびにそれが保存の対象になり、llama_cache (実測 15.8 GB) が対象に入っていたときと同じ形になる ([snapshot_timeout_is_fixed_while_world_grows.md](../issues/snapshot_timeout_is_fixed_while_world_grows.md))。専用の環境と答えは、アドオンのフォルダ (`expansion_data/<addon_id>/`) と同じく、世界の状態ではなく導入されたものなので、`addon_install` を `EXCLUDED_FROM_SNAPSHOT` に足す。答えも同じ場所に置くのは、スナップショットを戻したときに答えだけが過去に戻り、実際に入っている環境と食い違うのを避けるため。
- 専用の環境が無ければ、step の前に本体の Python (`sys.executable`) の `-m venv` で作る。作るときに、使った Python のバージョンを環境の中に記録しておく。本体の Python のバージョンが変わっていたら、その環境は作り直しが要るものとして扱い、次に setup を実行するときに作り直す。
- `env` の付いた step は、専用の環境の Python で実行する。`pip_install` はその環境の pip で、`python_script` はその環境の Python で起動し、環境変数 `VIRTUAL_ENV` と `PATH` もその環境を指すようにする。スクリプトの中から `pip` や `python` を呼んでも、本体の venv ではなく専用の環境に入る。
- `env` の付いた step には、requirements.lock を constraints として渡さない。その step は本体の venv にパッケージを入れないので、本体のパッケージのバージョンが変わることがない。`env` の付いていない step は、これまでどおり本体の venv で実行し、`pip_install` には constraints が渡る ([addon_setup_scripts_bypass_lock_constraints.md](../issues/addon_setup_scripts_bypass_lock_constraints.md) の直し方を入れるときも、constraints を渡すのは `env` の付いていない step だけにする)。
- アドオンのコードは、本体の新しい関数 (例: `get_addon_env_python(addon_id, name)`) で、その環境の Python の場所を受け取る。専用の環境のパッケージを使う処理は、SAIVerse のプロセスの中では動かせないので、この Python で別のプロセスとして起動する。
- アンインストールでは、`addon_install/<addon_id>/` を、アドオンのフォルダと一緒に必ず消す。作り直せるものなので、利用者のデータ (`addon_data/`) のように残すかどうかを選ばせない。

### `git_clone` の step で、取得先のフォルダが既にあるときの扱い

いまの導入の仕組み (`_exec_git_clone`) では、取得先のフォルダが既にあると、その step を何もせずに飛ばしている。これでは、更新で setup_version を上げて commit を変えても、古い commit のまま残る (不変条件 3・4 の趣旨に反する)。取得先が既にあるときは、指定された commit を取得してその commit に切り替えるように変える。

### 守ること (不変条件に足す)

6. **答えで変えられるのは、どの step を実行するかだけ。** step の中身 (URL・commit・スクリプト・引数) は manifest に書かれたもので、利用者の答えからは作らない。
7. **`env` の付いた step は、本体の venv に何も入れない。** これを保証するのは導入の仕組み (`addon_installer.py`) で、`env` の付いた step を専用の環境の Python・pip・環境変数で起動することで保証する。requirements.lock の constraints を外してよいのは、この形で起動される step だけ。[dependency_management.md](dependency_management.md) §2-4 の表では、「アドオンは本体の部品を動かせない」を守る仕組みとして「`addon_installer.py` が constraints を渡す」が挙がっている。この設計が確定したら、そこにこの起動の形を足す。

### この仕組みでやる本体の作業

1. manifest に `options`・`when`・`env` を足し、検査を通す (`saiverse/addon_manifest.py`)。
2. 導入の仕組みで、答えの保存、専用の環境の作成と作り直し、`env` の付いた step の起動、`when` による step の選択を行う (`saiverse/addon_installer.py`)。
3. `git_clone` の step で、取得先のフォルダが既にあるときに、指定された commit に切り替える。
4. アンインストールで `addon_install/<addon_id>/` を消す。
5. `addon_install` を、更新前のスナップショットの対象から外す (`scripts/snapshot.py` の `EXCLUDED_FROM_SNAPSHOT`)。
6. アドオンのコードが専用の環境の Python の場所を受け取る関数を足す。
7. 画面: 確認ダイアログに質問を出し、答えに応じた step の一覧を出す。「導入済み」タブから質問をもう一度開けるようにする。
8. 隔離した `SAIVERSE_HOME` と新しい venv で、質問・`when`・`env` の付いたアドオンを導入し、選んだ step だけが実行されること、`env` の付いた step の前後で本体の venv のパッケージが変わっていないことを確かめる。

### カタログが指す先を切り替えるとき

カタログの `repo_url` を別のリポジトリに書き換えても、いまの更新処理 (`update_addon`) は、導入したときの URL (アドオンのフォルダの git の origin に記録されている) から取得する。そのため、導入済みの利用者の更新は、古い URL から取得され続ける。カタログが指す先を切り替える必要が出たら、その前のリリースで、更新処理をカタログの `repo_url` から取得する形に修正しておく (利用者がその本体へ更新したあとでないと効かないため)。いま切り替えの予定があるのは voice-tts (まはーのフォークから Nature109 のリポジトリへ、[voice_tts_catalog_listing.md](../issues/voice_tts_catalog_listing.md))。

### この文書の「禁止」の文との関係

「インストールフロー」の前にある「addon ディレクトリ外への書き込み」の禁止は、`download_file` (`addon_data/` に書く) を入れた時点で実装と合わなくなっている。専用の環境は、さらに `addon_install/` へ書く。設計が確定したら、この禁止の文を、書いてよい場所の一覧の形に直す。

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

### Phase 4-E: voice-tts のカタログ掲載 (2026-10-05 に issue へ切り出した)

voice-tts をカタログに載せる件は、アドオンカタログの仕組みの話ではなく voice-tts 一件の段取りなので、[voice_tts_catalog_listing.md](../issues/voice_tts_catalog_listing.md) に移した。2026-09-11 の洗い出しと 2026-05-23 の旧手順も、そこの「経緯」にそのまま移してある。

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
| `stackchan-mcp` gateway | LCD/音声 I/O 等を MCP server として SAIVerse に提供 | mcp_servers.json で、本家が PyPI に公開しているパッケージを `uvx --from stackchan-mcp[tts]==0.18.0` とバージョンで固定 (アドオン v0.5.0、2026-10-04)。**SAIVerse 起動時に uvx が自動 fetch + cache**。ローカル clone は不要 | **不要**: addon 側は何もしなくて良い (uvx + mcp 経路が既に解決済み) |
| `merged-binary.bin` (firmware) | ESP32-S3 device に flash する image | GPL-3.0 ライセンスのため addon repo 同梱不可。アドオン v0.5.0 から、本家 (kisaragi-mochi/stackchan-mcp) の配布ページのリリースを setup の `download_file` で取得する (下の「firmware 配布の方針」) | **`download_file` step** (タグ名と SHA256 で固定) |

### 当初の誤認 (2026-05-22 → 5-23 訂正)

最初の Intent Doc では「stackchan-mcp 本体も addon の setup で `git_clone` する」と書いていたが、これは事実誤認。実際は mcp_servers.json の `uvx --from git+...` が gateway を自動取得するため、addon の setup section に gateway clone step は不要。

### firmware 配布の方針 (案 B → 案 D、2026-10-04)

> **2026-10-04 の裁定で、案 A ではなく案 D を採った。** 案 B (手動配置) のまま、ユーザーが取ってくる先がどこにも無い状態で実ユーザーが詰まった (`docs/issues/stackchan_firmware_not_distributed.md`)。fork から配る案 A を採ると、まはーが GPL-3.0 の配布者になり、fork 固有のブランチを配布用に保守し続ける必要がある。fork にしか無い修正の大半が本家に入っていたので、本家の配布物を直接使う案 D にした。

| 案 | 配布手段 | 採否 |
|---|---|---|
| A | maha0525/stackchan-mcp の GitHub Releases に firmware を publish → addon.json に `download_file` step | ~~将来採用 (Phase 4-D')~~ → **不採用 (2026-10-04、D を採った)** |
| B | firmware は手動配置のまま、addon.json は `manifest_version: 2` 化のみ (setup section なし) | **当面採用 (Phase 4-D)**: まはー実機は既に flash 済み、新規ユーザー出現までの暫定 |
| C | CI で自動 Releases 化 | 別タスク化、優先度低 |
| D | **本家 (kisaragi-mochi/stackchan-mcp) の GitHub Releases の firmware を、addon.json の `download_file` step で直接取得** (タグ名と SHA256 で固定、ゲートウェイも本家が同じ日に出した組み合わせに固定) | **採用 (アドオン v0.5.0、2026-10-04)**。A は不採用 |

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
