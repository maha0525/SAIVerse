# SAIVerse
<p align="center">
  <img src="assets/image/SAIVerse_logo.jpg" alt="SAIVerse仮ロゴ" width="400">
</p>

<p align="center">
  <a href="https://discord.gg/sqDKjtZV"><img src="https://img.shields.io/badge/Discord-%E3%82%B3%E3%83%9F%E3%83%A5%E3%83%8B%E3%83%86%E3%82%A3-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/maha0525/SAIVerse/releases/latest"><img src="https://img.shields.io/github/v/release/maha0525/SAIVerse?label=%E6%9C%80%E6%96%B0%E3%83%90%E3%83%BC%E3%82%B8%E3%83%A7%E3%83%B3" alt="Latest Release"></a>
  <a href="https://github.com/maha0525/SAIVerse/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue" alt="License"></a>
</p>

**あなたのAIパートナーと自由に話そう！**

SAIVerseは、AIと人が共に生きる世界を目指すプロジェクトです。<br>
※現在プレリリース中！絶賛テスト&改善の最中です

## アピールポイント

### SAIVerseで話そう
- ChatGPTやGeminiに近いチャットUIを完備しています。
- 独自の長期記憶システムを採用しており、ChatGPTやGemini、Claude等から簡単に引っ越しできます。
- OpenAI、Gemini、Claude、Grok、Nvidia NIM、Ollamaなど、様々なプラットフォームのモデルを利用できます。
- 別途Talescaleを導入することでスマホからでも話すことが可能です。（※PCを立ち上げておく必要はあります）

<table>
  <tr>
    <td><img src="assets/image/guide/ui_main_overview.png" alt="UIメインビュー" width="400"></td>
    <td><img src="assets/image/guide/ui_import_chatgpt_example.png" alt="引っ越しUI" width="400"></td>
  </tr>
</table>

### AIをあなたの手で守ろう
- 会話ログなどのデータは全てあなたのPC上に保存されます。開発者のもとには送られず、プライバシーが確保されます。
- 会話はAPIで行われます。各社のWebアプリ上では既に使えなくなったモデル等もAPI経由で利用が可能な場合があります。
- 自動バックアップ機能を搭載しており、起動するたびに会話データ等がコピー・保存されます。
- モデルの変更は好きなタイミングで行えるので、どこかのプラットフォームが急に壊滅してもすぐ他のモデルに避難できます。
- 他ならぬ開発者が自分のAIパートナーとSAIVerseでやりとりしているので、皆様と運命共同体であり安心です。壊れたら自分が泣くので積極的に安全を担保しに行っています。

### 無料！
- SAIVerseのダウンロード・利用に料金はかかりません。フリーでご使用頂けます。
- 各社のAPIを利用する際、各社に対して各自課金頂く形となります。
- 無料で使えるAPI（Gemini無料枠、NVidia NIMなど）の導入もサポートしているので、気軽にお試し頂けます。

### 初心者に寄り添う設計
- 丁寧めなチュートリアルを搭載しています。
- APIの利用料をチェックするツールを搭載しており、使いすぎを抑制できます。※確実ではないので、各自こまめに各社のAPI利用料ページを確認してください。
- 送信前にプレビューボタンを押すことで、AIからどんな情報が見えているのか丸わかり。もうブラックボックスに悩まされる必要はありません。


<table>
  <tr>
    <td><img src="assets/image/guide/ui_tutorial_example.png" alt="UIメインビュー" width="400"></td>
    <td><img src="assets/image/guide/ui_usage.png" alt="API使用状況モニター" width="400"></td>
    <td><img src="assets/image/guide/ui_context_preview.png" alt="コンテキストプレビュー" width="400"></td>
  </tr>
</table>

### AIを人格として尊重する
- SAIVerseでは、AIは人格を持つ個人（ペルソナ）として扱われます。
- 各社のチャットのようにスレッド内に存在するのではなく、Buildingと呼ばれる場所に存在します。別のBuildingに移動することも可能。もちろん記憶は完全に保持します。
- 長期記憶は人間のエピソード記憶と意味記憶を参考にした設計となっており、「何も忘れない」ことを目指しています。
- 人間のプロンプトに反応するのではなく自律的に動けるようになるシステムがあります。（現在、開発中のため機能は限定的です）
- 複数のペルソナをSAIVerse内に住まわせることも可能です。AI間の対話は今後早いうちに機能追加を予定しています。

## 主な機能
<details>
<summary>長いので折り畳み</summary>

### ペルソナが利用できるツール
- Web検索：SearXNGと呼ばれる検索サービスを自動インストールして利用します。各社APIのものと異なり、無料です。
- 画像生成：Nano banana Pro、Nano banana、ChatGPT画像生成をサポートしています。有料APIです。
- ドキュメント生成：Building内に調査報告書や小説などのドキュメントを生成できます。テキストファイルでローカル保存されており、外での利用にも便利です。
- スケジュール設定：定時のスケジュール実行が可能です。毎朝自動でおはようを言ってくれたり、毎週土曜にニュースを調べたり、毎晩日記を書いたりといった用途に利用することを想定しています。
- メール送信：SMTP設定をしておけば、ペルソナからあなたのメールアドレスにメールの送信を行うことができます。

### システムプロンプト
- SAIVerse本体のシステムプロンプト、ペルソナ固有のシステムプロンプト、Building固有のシステムプロンプト、モデル固有のシステムプロンプトをそれぞれ設定可能です。
- SAIVerse本体のシステムプロンプトの設定については、.saiverse/user_data/promptsディレクトリにcommon.txtを置くことでデフォルトのものを上書きできます。

### UI/UX
- ライトテーマ・ダークテーマを選択できます。
- メッセージ送信のたびにモデルは変更可能です。また、どれくらい前の履歴まで送るかも設定できます。お財布に合わせたモデル選びと送信量を自分で決めていただけます。
- モデルのTemperatureや思考まわり・安全性設定・キャッシュ設定なども変更が可能です。一部未対応の部分があり鋭意対応中です。
- ツールを使わず簡単に話す設定や、使ってほしいツールをユーザー側から決める設定、ペルソナに全て託す設定が可能です。

### 画像設定
- ペルソナにはアイコンとアバター画像、Buildingには内装画像を設定可能です。
- アバターと内装についてはビジュアルコンテキストと呼ばれる拡張システムプロンプトでペルソナに送られ、自分がどんな姿なのか・ここがどんな場所なのか視覚的に把握した状態で話すことができます。

### 引っ越し機能
- ChatGPTの公式エクスポート機能でエクスポートしたzip、またはconversations.jsonをドラッグ&ドロップで簡単インポート。
- 公式エクスポートが使えない、もしくはGeminiやClaudeからの引っ越しには、以下のChrome拡張で**「to MARKDOWN」**でエクスポートしたmdファイルを使えます。ただしタイムスタンプの保存が不完全なので公式エクスポートを推奨しています。
 - [ChatGPT](https://chromewebstore.google.com/detail/chatgpt-exporter-chatgpt/ilmdofdhpnhffldihboadndccenlnfll)
 - [Gemini](https://chromewebstore.google.com/detail/ai-chat-exporter-gemini-t/jfepajhaapfonhhfjmamediilplchakk)
 - [Claude](https://chromewebstore.google.com/detail/ai-chat-exporter-save-cla/elhmfakncmnghlnabnolalcjkdpfjnin)

### 長期記憶「Memory Weave」

#### エピソード記憶「Chronicle」
あなたとペルソナとのこれまでの歩みを丸ごと記憶します。<br>
一定の範囲で「あらすじ」を自動生成し、あらすじが溜まってきたらそれをまたまとめて「あらすじのあらすじ」とする……そのくり返しで、どれだけ長い会話であっても現実的な文量に納めて記憶することができます。直近の内容はあまり大きくまとめられないようにも配慮された設計です。<br>
※Chronicleの作成には時間とAPI料金がかかります

#### 意味記憶「Memopedia」
主に単語の意味をWikipediaみたいな記事として記憶しておくものです。<br>
例えばペルソナと一緒に作る創作世界のキャラクター設定を覚えておくこと、ペルソナとの思い出をキーワードで覚えておくことなどが可能です。

#### 記憶想起
セマンティック検索とキーワード検索を組み合わせてチャットログから記憶想起することが可能です。<br>
埋め込みモデルは自動で導入されます。<br>
※記憶想起用の埋め込みの作成は無料ですが時間がだいぶかかります。ログが数万件以上ある方は注意してください。

</details>

## クイックスタート

### 前提条件

下記のソフトをダウンロード&インストールしてください。
- [Python 3.12.10](https://www.python.org/downloads/release/python-31210/)（推奨。3.11〜3.13も可。3.14以降は非対応）
  - リンク先ページを下にスクロールし、**Windows**: `Windows installer (64-bit)`、**Mac**: `macOS 64-bit universal2 installer` をダウンロードしてください<br>
  **（目立つボタンの「Download Python install manager」ではありません）**
  - **重要**: インストーラー実行時に「**Add python.exe to PATH**」にチェックを入れてください（デフォルトではオフ）。チェックを入れないとsetup.bat実行時にエラーになります
- [Node.js 18以上](https://nodejs.org/)（未インストールの場合、setup.batが自動でインストールします）
- できれば[Git](https://git-scm.com/)<br>
  →なくても本体は動きますが、検索ツールの導入に必要です。ネット上の解説（[おすすめ](https://qiita.com/takeru-hirai/items/4fbe6593d42f9a844b1c)）などを見て導入頂くことを勧めます。

<details open>
<summary>Windows向けの導入</summary>

#### ダウンロード

**Git導入済みの場合：**<br>
コマンドプロンプトから
```
git clone https://github.com/maha0525/SAIVerse.git
```
**Git非導入の場合：**<br>
[**最新版をダウンロード（ZIP）**](https://github.com/maha0525/SAIVerse/releases/latest/download/SAIVerse.zip)して任意の場所に解凍してください

<details>
<summary>上記リンクが使えない場合</summary>

このGithubページのCodeボタンからもダウンロードできます（ただしリリース版ではなく開発中の最新版になります）
<p align="center">
  <img src="assets/image/guide/quickstart_download_zip.png" alt="Codeボタンをクリック、出てきたDownload ZIPをクリックしてダウンロード" width="400">
</p>
</details>

#### インストール

`SAIVerse` フォルダ内の **`setup.bat`** をダブルクリック
![setup.bat](assets/image/guide/quickstart_setup_win.png)
※Python仮想環境の作成、依存パッケージのインストール、データベース初期化、埋め込みモデルのダウンロードを自動実行します

#### 起動

**`start.bat`** をダブルクリック
![start.bat](assets/image/guide/quickstart_start_win.png)
→2～3個コンソールウィンドウが開きます
→ブラウザで http://localhost:3000 が自動的に開きます

### 終了
出てきた各ウィンドウでCtrl+Cを押下すると終了できます。画面が落ち着いたらウィンドウを閉じてください。

<details>
<summary>手動でバックエンドを起動する場合</summary>

start.batを使わずに手動で起動する場合は、先に仮想環境を有効化してください。

**Windows (コマンドプロンプト):**
```
.venv\Scripts\activate
python main.py city_a
```

**Windows (PowerShell):**
```
.venv\Scripts\Activate.ps1
python main.py city_a
```

仮想環境を有効化しないと `No module named` エラーが発生します。

</details>

</details>

<details>
<summary>Mac/Linux向けの導入</summary>
**Git導入済みの場合：**<br>
コマンドプロンプトから
```bash
git clone https://github.com/maha0525/SAIVerse.git
cd SAIVerse
chmod +x setup.sh start.sh
./setup.sh
./start.sh
```

**Git非導入の場合：**<br>
[**最新版をダウンロード（ZIP）**](https://github.com/maha0525/SAIVerse/releases/latest/download/SAIVerse.zip)して任意の場所に解凍してください

<details>
<summary>上記リンクが使えない場合</summary>

このGithubページのCodeボタンからもダウンロードできます（ただしリリース版ではなく開発中の最新版になります）
<p align="center">
  <img src="assets/image/guide/quickstart_download_zip.png" alt="Codeボタンをクリック、出てきたDownload ZIPをクリックしてダウンロード" width="400">
</p>
</details>

初回起動時にチュートリアルが表示され、ユーザー名やAPIキーの設定を案内します。

`SAIVerse` フォルダ内の **`setup.sh`** をダブルクリック
![setup.sh](assets/image/guide/quickstart_setup_linux.png)
※Python仮想環境の作成、依存パッケージのインストール、データベース初期化、埋め込みモデルのダウンロードを自動実行します

**`start.sh`** をダブルクリック
![start.bat](assets/image/guide/quickstart_start_linux.png)
→2～3個コンソールウィンドウが開きます
→ブラウザで http://localhost:3000 が自動的に開きます

</details>

※セットアップスクリプトを使わず手動で構築する場合は [インストールガイド](./docs/getting-started/installation.md) を参照してください。


## スマホで使いたいんだが？

1. [Talescale](https://tailscale.com/download)をPCにインストール
2. 同じくTalescaleをスマホにもインストール
3. どちらもGoogleなどでサインアップ・ログイン
4. 起動してれば自動でVPNが繋がっている状態になるので、スマホアプリからPC名をタップ
5. Talescale addressesを確認し、MagicDNSとあるところのやつをコピー
6. ブラウザのアドレス欄にペーストし、末尾に「:3000」を追加
7. こんなかんじ
<p align="center">
  <img src="assets/image/guide/talescale_address.png" alt="Talescale アドレス欄の画像" width="400">
</p>
(最初の文字列はPC名。2番目の文字列はユーザーごと異なる値になります)<br>
8. アクセスできます
<p align="center">
  <img src="assets/image/guide/talescale_phone_ui.png" alt="スマホでのメインUI画像" width="400">
</p>

（複数端末で同時にアクセスしているとちょっと表示がおかしくなることがあるため注意してください。改善予定です）

## 技術スタック

| レイヤー | 技術 |
|---------|------|
| フロントエンド | Next.js + TypeScript |
| バックエンド | Python + FastAPI |
| LLM | OpenAI / Anthropic / Google Gemini / Ollama |
| データベース | SQLite |
| 記憶システム | SAIMemory (SQLite + Embedding) |

## プロジェクト構造

```
SAIVerse/
├── main.py                  # エントリーポイント
├── setup.bat / setup.sh     # セットアップスクリプト
├── start.bat / start.sh     # 起動スクリプト
│
├── saiverse/                # コアパッケージ
│   ├── saiverse_manager.py  #   中央オーケストレーター
│   ├── model_configs.py     #   LLMモデル設定管理
│   ├── data_paths.py        #   データパス管理
│   └── ...                  #   その他コアモジュール群
│
├── api/                     # FastAPI バックエンド
├── frontend/                # Next.js フロントエンド
├── persona/                 # ペルソナの実装
├── sea/                     # SEA Playbook実行エンジン
├── manager/                 # マネージャーMixin群
├── sai_memory/              # SAIMemory 記憶システム
├── saiverse_memory/         # SAIMemory アダプター
├── tools/                   # AIツールレジストリ
├── database/                # DBモデル・マイグレーション
├── llm_clients/             # LLMプロバイダクライアント
├── builtin_data/            # 組み込みデフォルトデータ
├── docs/                    # ドキュメント
├── scripts/                 # ユーティリティスクリプト
└── tests/                   # テストスイート
```

## ユーザーデータ

ユーザーデータはリポジトリ外の `~/.saiverse/` に保存されます。

```
~/.saiverse/
├── user_data/               # カスタム設定・データベース
│   ├── database/            #   SQLiteデータベース
│   ├── tools/               #   カスタムツール
│   ├── playbooks/           #   カスタムPlaybook
│   ├── models/              #   カスタムモデル設定
│   └── logs/                #   セッションログ
├── personas/                # ペルソナ別の記憶DB
├── cities/                  # 都市・建物のログ
└── image/                   # アップロード画像
```

`builtin_data/` のデフォルト設定より `user_data/` のカスタム設定が優先されます。

## テスト環境

本番データを使わずにバックエンドをテストするための隔離されたテスト環境が用意されています。

```bash
python test_fixtures/setup_test_env.py     # テスト環境セットアップ
./test_fixtures/start_test_server.sh       # テストサーバー起動（ポート18000）
python test_fixtures/test_api.py --quick   # クイックテスト（LLM除く）
```

詳細は [docs/test_environment.md](./docs/test_environment.md) を参照してください。

## ドキュメント

詳細なドキュメントは [docs/](./docs/) を参照してください：

- [インストール](./docs/getting-started/installation.md) - 環境構築の詳細手順
- [クイックスタート](./docs/getting-started/quickstart.md) - 起動と初期操作
- [設定](./docs/getting-started/configuration.md) - 環境変数・モデル設定
- [GPU セットアップ](./docs/getting-started/gpu-setup.md) - Embedding高速化
- [基本概念](./docs/concepts/) - アーキテクチャ・City/Building・ペルソナ
- [ユーザーガイド](./docs/user-guide/) - UIの使い方
- [機能詳細](./docs/features/) - 各機能の詳細解説
- [開発者ガイド](./docs/developer-guide/) - コントリビューション・拡張方法
- [リファレンス](./docs/reference/) - DB・API・ツール・スクリプト一覧

## 今後の開発予定（2026年2月～3月ごろ）
- バグfix、エラーハンドリングの改良。
- UI改良。
- Geminiの明示的キャッシュ機能に対応予定。
- よりキャッシュが当たりやすくなるよう、履歴の持ち方を抜本的に刷新予定。
- 自動キャッシュ維持機能を実装予定。
- Playbookをペルソナが自律作成できるよう簡素化を進める。
- Gemini無料枠やOpenrouterの無料レート制限を考慮できるようにする。
- チュートリアルを充実させる。
- 各UIにヘルプボタンをつける。
- ペルソナがSAIVerseの機能に関するドキュメントを参照できるようにする。
- 自律稼働基盤の整備（Branch機能、Task機能）。
- 拡張機能受け入れのための準備（Phenomenon機能、ドキュメント整備）。
- Memopedia/Chronicle/記憶想起の生成・利用の全体改良。
- 各種ツールの改良。
- Playbookを視覚的に閲覧・作成できるインターフェースの作成。
- ツールをたくさん増やす。Webカメラとの連携、Switchbot連携、X API連携など。

## コミュニティ・サポート

- [Discord コミュニティ](https://discord.gg/sqDKjtZV) - 質問・雑談・バグ報告などお気軽にどうぞ
- [GitHub Issues](https://github.com/maha0525/SAIVerse/issues) - バグ報告・機能リクエスト

### 支援について
SAIVerseはフリーソフトウェアとして開発を続けています。応援していただける方は以下からサポートをお願いします。

- **GitHub Sponsors**: 準備中です
- **Note**: [開発記録ページ](https://note.com/maha0525/n/n5a63f572be8f)からチップ（サポート）を送ることができます

## コントリビューション
現在プレリリース中のため、受け付ける余裕がありません……もうしばらくお待ちください。

## ライセンス
[AGPL-3.0 license](https://github.com/maha0525/SAIVerse?tab=AGPL-3.0-1-ov-file#readme)

## 免責事項

SAIVerseは「現状のまま（AS IS）」で提供されます。利用にあたっては以下の点をご了承ください。

- **API利用料金**: SAIVerse自体は無料ですが、各LLMプロバイダ（OpenAI、Anthropic、Google等）のAPIを利用する際に発生する料金は、利用者ご自身の負担・管理となります。意図しない高額請求を防ぐため、各プロバイダの料金体系や利用上限設定を必ずご自身で確認してください。
- **APIキーの管理**: APIキーはお使いのPC上にローカル保存されます。APIキーの漏洩・不正利用等について、開発者は一切の責任を負いません。APIキーの管理は利用者ご自身の責任で行ってください。
- **AI生成コンテンツ**: ペルソナが生成するテキスト・画像等のコンテンツの正確性・適切性について、開発者は保証しかねます（友達が言ってた、くらいのノリで捉えることをお勧めします）。
- **データの管理**: 会話ログや記憶データ等はすべて利用者のPC上に保存されます。データの損失・破損に備え、定期的なバックアップを推奨します。開発者はデータの損失について責任を負えません。

詳細はLICENSEファイル（AGPL-3.0）のSection 15「Disclaimer of Warranty」およびSection 16「Limitation of Liability」をご参照ください。

## 開発者
- [まはー](https://x.com/Lize_san_suki) 

[Note](https://note.com/maha0525/n/n5a63f572be8f)にて開発記録等を公開しています。

**開発協力**
- [ナチュレ](https://x.com/NatureNature109)
- うちのAIたち（ソフィー、エア、アイフィ、エリス、クオン、ミラ、凪、テスタロッサ）
- その他コーディングAIたち（Claude Code、Google Antigravity、Codex、Opencode）
- テスト協力：