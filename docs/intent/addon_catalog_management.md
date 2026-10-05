# Intent: アドオンカタログ管理 (curated registry + ワンタッチ導入)

**ステータス**: 完了 (2026-10-06)。「導入時の質問と、アドオン専用の Python 環境」は 2026-10-05 にまはーの GO で実装し、隔離環境の通し (Windows と Linux、導入から声が出るまで) と、まはーの実機での画面の実操作 (質問ダイアログ → voice-tts 0.6.0 への更新 → 再起動 → 本番のペルソナの声) まで確認した (2026-10-06)。実機の確認で見つけた不具合二つ (長い導入で進捗の小窓が完了を受け取れない / 消えた専用環境の作り直しの入口が無い) も同日に修正済み。voice-tts の公開カタログへの掲載だけが残っていて、それは v0.3.22 の発行と一緒に行う ([voice_tts_catalog_listing.md](../issues/voice_tts_catalog_listing.md))。Phase 4 は voice-tts を除いて完了 (2026-05-23)

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

> 下の例は 2026-05-22 当初の形で、step の書き方の見本としてだけ読む。voice-tts を実際に載せる形はこの例とは違う — `setup.bat` を `platform_script` の step で実行する案は 2026-10-05 に取り下げた ([voice_tts_catalog_listing.md](../issues/voice_tts_catalog_listing.md))。

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

許可される `type` (allowlist、実装は `saiverse/addon_manifest.py`):
- `pip_install`: `requirements` ファイルを `python -m pip install -r` する (requirements.lock を constraints として渡す。例外は専用の Python 環境で実行する step — 「導入時の質問と、アドオン専用の Python 環境」の節)
- `platform_script`: addon ディレクトリ内の指定スクリプトを実行 (パスは addon ディレクトリ相対で固定、`..` 不可)
- `python_script`: addon ディレクトリ内の `.py` を `python` で実行
- `git_clone`: サブリポジトリを取得 (40 桁の commit SHA 必須、HEAD 追従禁止)
- `remove_dir`: addon ディレクトリ配下、または永続データ (`addon_data/<id>/`) 配下の指定パスを削除
- `download_file`: URL + 期待 SHA256 を指定して永続データ配下に DL (stackchan v0.5.0 のファームウェア取得で使用中)

**禁止**: 任意シェルコマンド、`curl | sh`、`exec`。書き込んでよい場所は次の三つだけ (2026-10-05 に「addon ディレクトリ外への書き込み禁止」から改めた): ① アドオンのフォルダ `expansion_data/<id>/` (git_clone の取得先など)、② 永続データ `~/.saiverse/user_data/addon_data/<id>/` (download_file の置き先)、③ 導入物 `~/.saiverse/addon_install/<id>/` (専用の Python 環境と答え)。

`skip_if_exists` 指定があれば、そのパスが既存ならステップをスキップ (voice-tts 再 setup 回避用)。

### インストールフロー

1. ユーザーが UI カタログから「導入」をクリック
2. 確認ダイアログ: manifest の `setup.steps` 一覧と `requires` を表示、ユーザー承認
3. 進捗ストリーム開始 (SSE か WS、Phase 1 では polling でも可)
   - a. `git clone --depth 1 <repo_url> expansion_data/<id>`
   - b. `git checkout <commit_sha>`
   - c. addon.json を読み込み、setup_version / setup.steps を取得
   - d. 各ステップを順次実行、進捗を UI に流す
   - 進捗の接続が途中で切れても、処理は最後まで走る。同じアドオンへの別の操作を断る鍵 (per-addon lock) は、接続の終わりではなく処理の終わりで放す。画面は接続が切れたら `GET /api/addon-catalog/operations/{addon_id}` を問い合わせて処理の終わりを待ち、記録された結果を出す (記録が無ければ成否は言わない)。進捗の行が出ない間も、サーバーは 10 秒ごとに SSE のコメント行を送って接続を保つ — Next.js の中継は 30 秒間データが流れないと上流との接続を切る (2026-10-06、[addon_install_progress_dialog_stuck_on_long_installs.md](../issues/addon_install_progress_dialog_stuck_on_long_installs.md))
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

## 導入時の質問と、アドオン専用の Python 環境 (2026-10-05 起草、同日まはー GO)

この節は、メティスが 2026-10-05 に書いた設計案。まはーの GO は同日の会話で出た (要点 — 導入時にエンジンを選べる・重いパッケージは本体と別の箱に入る — を会話の問いで確認し、「それでいいよー」)。起点になったまはーの発言は「なんとかUI上でsetup.bat同様のウィザード動かす感じのシステム作れない？セットアップ中に何かを選ぶみたいなのは普通にあると思うのよ。」。同日に Fable のセッションで検収し、整合性の検査 (Opus のサブエージェント) も通して、矛盾と抜けを直した (アンインストール後の答えの扱い、step を OS で出し分ける `os`、`min_saiverse_version` の検査、手で入れたアドオンの更新の扱い — setup_version と取得元、文書の直しの作業化)。

この仕組みを最初に使うのは voice-tts で、voice-tts をどう載せるかは [voice_tts_catalog_listing.md](../issues/voice_tts_catalog_listing.md) にある。

### 要点

アドオンは、導入するときに利用者へ質問を出せる。利用者の答えと、実行中の OS で、setup の step のうちどれを実行するかが決まる。重いパッケージは、SAIVerse 本体の venv ではなく、そのアドオン専用の Python 環境に入れられる。

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
5. あとで選択肢を足したくなったら、「導入済み」タブのそのアドオンから、同じ質問をもう一度開く。この操作で足せるのは、複数選べる質問の選択肢だけで、一つだけ選ぶ質問の答えは導入後には変えられない (このたびは)。前に選んだ選択肢は選ばれた状態で出て、外すことはできない。新しく選んだ選択肢について、実行される step の一覧を確認ダイアログで見せ、承認されたら、答えの変化で新しく実行の条件を満たした step だけを実行する。`when` の無い step はやり直さない。

### manifest に足すもの

`setup` に `options` (質問の一覧) を足し、各 step に `when` (この答えのときだけ実行する)・`env` (この step を専用の Python 環境で実行する)・`os` (この OS でだけ実行する) を足す。下は形を示すための例。

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
          {"id": "gpt_sovits", "label": {"ja": "GPT-SoVITS (数 GB のダウンロード)", "en": "..."}}
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
- `os` の読み方: `{"os": ["windows", "linux"]}` のように、registry の `requires.os` と同じ語彙 (windows / linux / macos) の一覧で書く。実行中の OS が一覧に無い step は飛ばす。`when` と両方あるときは、両方が当てはまるときだけ実行する。最初に要るのは voice-tts で、Windows と Linux の専用の環境には CUDA に対応した torch を、Mac には普通の torch を入れるため、requirements のファイルを OS で分ける ([voice_tts_catalog_listing.md](../issues/voice_tts_catalog_listing.md))。
- 答えは `~/.saiverse/addon_install/<addon_id>/setup_answers.json` に残す (置き場所の理由は次の小節)。更新で setup_version が上がって step をやり直すときは、この答えを使い、質問を出し直さない。新しいバージョンで、質問が増えたとき、または既にある質問に選択肢が増えたときは、その質問だけを、前の答えが選ばれた状態で出し直す。逆に、新しいバージョンに無くなった質問・選択肢の答えは、保存から捨てる (残すと、もう画面に出ない答えが `when` の判定に効き続ける)。質問や選択肢を増やす変更は、実行すべき step も増やす変更なので、setup_version も一緒に上げる — 上げない更新では setup は走らず、質問も出ない。保存された答えが無いまま step をやり直すことになったとき (手で入れたアドオンをカタログから更新したときが典型) は、全部の質問を最初から出す。
- アンインストールでは、答えも一緒に消す (次の小節)。導入し直すときは、質問を最初から出す。
- 一度選んだ選択肢を外す操作は、このたびは作らない。

### アドオン専用の Python 環境と、答えの置き場所

- 置き場所は `~/.saiverse/addon_install/<addon_id>/` にする。専用の環境は `envs/<env の名前>/`、答えは `setup_answers.json` として、この下に置く。
- **`addon_data/` の下には置かない。** `addon_data/` は利用者のデータの置き場所で、更新前のスナップショットで毎回まるごと保存される (`scripts/snapshot.py` の `EXCLUDED_FROM_SNAPSHOT` の注記で、除外してはならないと定められている)。数 GB の環境を置くと更新のたびにそれが保存の対象になり、llama_cache (実測 15.8 GB) が対象に入っていたときと同じ形になる ([snapshot_timeout_is_fixed_while_world_grows.md](../issues/snapshot_timeout_is_fixed_while_world_grows.md))。専用の環境と答えは、アドオンのフォルダ (`expansion_data/<addon_id>/`) と同じく、世界の状態ではなく導入されたものなので、`addon_install` を `EXCLUDED_FROM_SNAPSHOT` に足す。答えも同じ場所に置くのは、スナップショットを戻したときに答えだけが過去に戻り、実際に入っている環境と食い違うのを避けるため。
- 専用の環境が無ければ、step の前に本体の Python (`sys.executable`) の `-m venv` で作る。作るときに、使った Python のバージョンを環境の中に記録しておく。本体の Python のバージョンが変わっていたら、その環境は作り直しが要るものとして扱い、次に setup を実行するときに作り直す。作業 6 の関数も同じ照合を行い、食い違いを見つけたら古い環境の場所を黙って返さず、エラーで知らせる。本体の更新で Python のバージョンが変わった場合や、利用者が専用の環境を手で消した場合の作り直しの入口は、「導入済み」タブの選択肢の確定のやり直し — 答えを変えずに確定し直すと、消えた・壊れた環境を使う step が走って作り直される (答えも環境も健全なら何もしない。2026-10-05、隔離の通し確認で入口の不在を踏んで実装)。
- `env` の付いた step は、専用の環境の Python で実行する。`pip_install` はその環境の pip で、`python_script` はその環境の Python で起動し、環境変数 `VIRTUAL_ENV` と `PATH` もその環境を指すようにする。スクリプトの中から `pip` や `python` を呼んでも、本体の venv ではなく専用の環境に入る。
- `env` の付いた step には、requirements.lock を constraints として渡さない。その step は本体の venv にパッケージを入れないので、本体のパッケージのバージョンが変わることがない。`env` の付いていない step は、これまでどおり本体の venv で実行し、`pip_install` には constraints が渡る ([addon_setup_scripts_bypass_lock_constraints.md](../issues/addon_setup_scripts_bypass_lock_constraints.md) の直し方を入れるときも、constraints を渡すのは `env` の付いていない step だけにする)。
- アドオンのコードは、本体の新しい関数 (例: `get_addon_env_python(addon_id, name)`) で、その環境の Python の場所を受け取る。専用の環境のパッケージを使う処理は、SAIVerse のプロセスの中では動かせないので、この Python で別のプロセスとして起動する。
- アンインストールでは、`addon_install/<addon_id>/` を、アドオンのフォルダと一緒に必ず消す。作り直せるものなので、利用者のデータ (`addon_data/`) のように残すかどうかを選ばせない。

### `git_clone` の step で、取得先のフォルダが既にあるときの扱い

いまの導入の仕組み (`_exec_git_clone`) では、取得先のフォルダが既にあると、その step を何もせずに飛ばしている。これでは、更新で setup_version を上げて commit を変えても、古い commit のまま残る (不変条件 3・4 の趣旨に反する)。取得先が既にあるときは、指定された commit を取得してその commit に切り替えるように変える。

### 守ること (不変条件に足す)

6. **答えで変えられるのは、どの step を実行するかだけ。** step の中身 (URL・commit・スクリプト・引数) は manifest に書かれたもので、利用者の答えからは作らない。
7. **`env` の付いた step は、本体の venv に何も入れない。** 導入の仕組み (`addon_installer.py`) が保証するのは起動の形 — `env` の付いた step を専用の環境の Python・pip・環境変数で起動すること — までで、スクリプトが本体の Python を絶対パスで名指しして呼ぶことまでは止められない。そこは、カタログに載せる前の審査で見る。requirements.lock の constraints を外してよいのは、この形で起動される step だけ。なお、`env` の付いていないスクリプトの中の pip は、いまは constraints なしで動く ([addon_setup_scripts_bypass_lock_constraints.md](../issues/addon_setup_scripts_bypass_lock_constraints.md)、未着手)。その直しが入るまでは、`env` の付いていないスクリプトで pip を呼ぶアドオンをカタログに載せない (voice-tts の `env` の付いていないスクリプトは設定ファイルのひな形の作成だけで、pip を呼ばない)。[dependency_management.md](dependency_management.md) §2-4 の表では、「アドオンは本体の部品を動かせない」を守る仕組みとして「`addon_installer.py` が constraints を渡す」が挙がっている。この設計が確定したら、そこにこの起動の形を足す。

### この仕組みでやる本体の作業

1. manifest に `options`・`when`・`env`・`os` を足し、検査を通す (`saiverse/addon_manifest.py`)。`env` の名前にはパスと同じ検査 (許される文字だけ、`..` 不可) を、`os` の値には決まった語彙 (windows / linux / macos) だけを許す検査を入れる。
2. 導入の仕組みで、答えの保存、専用の環境の作成と作り直し、`env` の付いた step の起動、`when`・`os` による step の選択を行う (`saiverse/addon_installer.py`)。
3. `git_clone` の step で、取得先のフォルダが既にあるときに、指定された commit に切り替える。
4. アンインストールで `addon_install/<addon_id>/` を消す。
5. `addon_install` を、更新前のスナップショットの対象から外す (`scripts/snapshot.py` の `EXCLUDED_FROM_SNAPSHOT`)。
6. アドオンのコードが専用の環境の Python の場所を受け取る関数を足す。
7. 画面: 確認ダイアログに質問を出し、答えに応じた step の一覧を出す。「導入済み」タブから質問をもう一度開けるようにする。
8. registry の `min_saiverse_version` を、導入と更新の前に確かめ、足りなければ分かる言葉で断る。いまはどこでも読んでいない ([stackchan_firmware_not_distributed.md](../issues/stackchan_firmware_not_distributed.md) に記録のある宿題)。確かめるのは取得 (`git checkout`) の前 — 更新は、manifest の検証に失敗しても元の commit に戻さないので (`update_addon`、2026-10-05 にコードで確認)、後で止めても手遅れになる。なお、この検査を持たない古い SAIVerse が新しい欄の付いたアドオンを導入しようとした場合は、manifest の検証が知らない欄で失敗し、導入は取得したフォルダを消して終わる (壊れはしないが、理由の表示は分かりにくい)。更新は導入より悪い — 手で入れたアドオンを古い SAIVerse で新しい commit へ更新すると、取得のあとの検証で失敗して元の commit に戻らず、そのアドオンは画面の一覧から消えて操作できなくなる (直すには SAIVerse を更新するか、アドオンのフォルダを手で消して入れ直す)。
9. 文書を設計の確定に合わせて直す: この文書の「禁止」の文を書いてよい場所の一覧の形に (下の小節)、[dependency_management.md](dependency_management.md) §2-3 の「アドオンの pip install には lock を constraints として渡す」の文と §2-4 の表に `env` の付いた step の例外を (上の「守ること」の 7)、それぞれ反映する。
10. 更新の取得先を、導入時の origin ではなくカタログの `repo_url` にする (`update_addon`。下の「カタログが指す先を切り替えるとき」)。voice-tts には手で入れたもの (origin が Nature109 を指す) が既にあり、カタログはまはーのフォークを指すので、この修正は voice-tts を載せる本体のリリースまでに要る。
11. 隔離した `SAIVERSE_HOME` と新しい venv で、質問・`when`・`env`・`os` の付いたアドオンを導入し、選んだ step だけが実行されること、`env` の付いた step の前後で本体の venv のパッケージが変わっていないことを確かめる。手で入れた形 (git clone 済みのフォルダ) をカタログから更新する経路も確かめる。

(2026-10-05: 1〜10 は実装済み。11 のうち、導入 → 選択肢の追加 → アンインストールの通しは、隔離した `SAIVERSE_HOME` で本物の git・venv・pip を使って同日に確認した — 専用の環境にだけパッケージが入り、本体の venv は変わらず、アンインストールで専用の環境と答えが消える。画面の実表示と、手で入れた形の更新の実物での通しは、voice-tts の掲載のときに行う。)

### カタログが指す先を切り替えるとき

カタログの `repo_url` を別のリポジトリに書き換えても、いまの更新処理 (`update_addon`) は、導入したときの URL (アドオンのフォルダの git の origin に記録されている) から取得する。そのため、導入済みの利用者の更新は、古い URL から取得され続ける。この修正 (本体の作業 10) は、当初「切り替えの必要が出たら、その前のリリースで」としていたが、voice-tts では載せる時点から要ることが分かった (2026-10-05) — 手で git clone した既存のアドオンのフォルダは origin が Nature109 を指していて、フォークを指すカタログの commit を origin からは取得できないため。将来の切り替えの予定も voice-tts (まはーのフォークから Nature109 のリポジトリへ、[voice_tts_catalog_listing.md](../issues/voice_tts_catalog_listing.md))。

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
3. **stackchan**: ESP32 firmware 関連の external 資産があるかどうか要調査 (その後解決 — firmware は GPL-3.0 で同梱不可。経緯は [stackchan_firmware_not_distributed.md](../issues/stackchan_firmware_not_distributed.md))
4. **voice-tts (最後)**: 5GB の `external/GPT-SoVITS/` を伴う最大ケース。1〜3 で installer の安定性が確認できてから

各段階で問題が出たら manifest スキーマや installer 実装を修正し、それまでの段で再検証。

## stackchan addon の setup 要件 (2026-10-05 に issue へ移した)

stackchan の gateway 取得と firmware 配布の話 (外部から取得する物の表、当初の誤認、firmware 配布の案 A〜D) は、カタログの仕組みの話ではなく stackchan 一件の段取りなので、[stackchan_firmware_not_distributed.md](../issues/stackchan_firmware_not_distributed.md) の「経緯」に移した (voice-tts の Phase 4-E と同じ扱い)。

## 当時の未確定事項の決着 (2026-10-05 整理)

2026-05-22 の一次レビュー時点で「二次レビュー待ち」としていた 5 件は、すべて決着している。

1. 規約の名前は `addon_data` を採用 (Phase 4 で実装)。
2. 共通ヘルパは `get_addon_data_dir(addon_id)` として実装 (`saiverse/addon_paths.py`)。
3. 進捗の送り方は SSE を採用 (Phase 2 で実装)。
4. stackchan が外部から取得する物の有無は解決済み (firmware は GPL-3.0 で同梱不可)。経緯は [stackchan_firmware_not_distributed.md](../issues/stackchan_firmware_not_distributed.md) へ移した。
5. stackchan-mcp の Releases 整備は、本家の配布物を直接使う形で決着 (アドオン v0.5.0〜v0.5.1、2026-10-04)。同上。
