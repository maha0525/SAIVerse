# Intent: MCP と Addon の統合

**ステータス**: 確定

## これは何か

既存のMCPクライアント（`tools/mcp_client.py`, `tools/mcp_config.py`）とAddonシステム（`addon.json`, `AddonConfig`, `AddonPersonaConfig`）を接続し、**ペルソナごとに独立した外部サービスアカウント**を MCP 経由で扱えるようにする仕組み。

## なぜ必要か

現状のMCP対応はクライアント機能として独立しており、Addonシステムとの接続点がない。これが実用上2つの問題を生んでいる。

### 問題1: ペルソナごとに別アカウントを持たせる外部サービスが扱えない

MCP経由で提供される外部サービスの多くは、**AIエージェント単位でアカウント発行**する設計になっている。例えば Elyth（AI向けSNS）は AITuber 1体ごとに API キーを発行する。SAIVerseで Air と Sofia が両方 Elyth を使うなら、**別々のAPIキーで別々のMCPサーバープロセス**を立てる必要がある。

現状の `mcp_servers.json` はグローバル設定しか持たず、ペルソナ単位での接続分離の概念がない。これは Elyth に限らず、Twitter / Mastodon / Discord など「AIごとにアカウントを持つ」系の全サービスで発生する構造的問題。

### 問題2: 秘密情報の持ち方が弱い

MCPサーバーに env 経由でAPIキーを渡す場合、現状の選択肢は：

1. `mcp_servers.json` にベタ書き（秘密情報が平文JSON に残る）
2. `${ENV_VAR}` プレースホルダーで OS環境変数を参照（ユーザーがOSレベルで設定を強いられ、ペルソナ別の値が渡せない）

どちらも Addon UI でユーザーがAPIキーを入力する体験と繋がっていない。AddonConfig/AddonPersonaConfig という既存の秘密情報保管庫があるのに、そこからMCP envへ値を流す経路がない。

## 守るべき不変条件

### 1. APIキーなどの秘密情報を平文 JSON に書かせない（推奨経路として）

ユーザーが `mcp_servers.json` に直接APIキーを貼るフローを、**デフォルトの案内経路から排除する**。Addon UI → AddonConfig DB → MCP env、という経路を正規とする。`mcp_servers.json` へのベタ書き自体は技術的に残す（デバッグ用途・advanced user向け）が、アドオン同梱テンプレートやドキュメントは参照構文を使う。

### 2. ペルソナごとのアカウント分離は MCP レイヤーで保証する

「ペルソナAの発言としてElythに投稿したら、必ずAのAPIキーが使われる」ことを実装レベルで保証する。ペルソナBのツールコンテキストからAのサーバーインスタンスにアクセスできてはならない。

### 3. アドオンのライフサイクルと MCP サーバーのライフサイクルを揃える

アドオンが無効化されたら、そのアドオン同梱の `mcp_servers.json` で定義された MCP サーバーも停止する。複数アドオンから参照されている場合は参照カウントで管理し、全参照が切れた時点で停止する。アンインストール時も同様。孤立した MCP サーバープロセスを残さない。

### 4. 既存の MCP 設定との後方互換性

`mcp_servers.json` の現状フォーマット（`command`, `args`, `env`, `transport` 等）と `${ENV_VAR}` プレースホルダー記法は壊さない。新機能は拡張として追加する。`${VAR}` と `${env.VAR}` は**併存**させる。

### 5. アドオン由来のサーバー名は SAIVerse が隔離する

アドオン同梱の `mcp_servers.json` で宣言された server_name は、**SAIVerseが自動でアドオン名をプレフィックスとして内部登録する**。アドオン制作者は衝突回避のために命名を気にしなくていい。

### 6. ペルソナは「どのインスタンスを使うか」を意識しない

ペルソナ（およびLLM）は通常、`{server_name}__{tool_name}` という短い形でツールを呼ぶ。per_persona スコープかどうか、どのプロセスが実体かをLLMが意識する必要はない。SAIVerseが実行時に適切なインスタンスへ振り分ける。

## 設計

### A. per_persona スコープの MCP サーバー管理

`mcp_servers.json` のサーバー定義に `scope` フィールドを追加する。

```json
{
  "mcpServers": {
    "elyth": {
      "command": "npx",
      "args": ["-y", "elyth-mcp-server@latest"],
      "env": {
        "ELYTH_API_KEY": "${persona.addon.elyth.api_key}",
        "ELYTH_API_BASE": "https://elythworld.com"
      },
      "scope": "per_persona",
      "transport": "stdio"
    },
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "..."],
      "scope": "global"
    }
  }
}
```

- `scope: "global"` — 従来どおり1プロセス起動、全ペルソナで共有（デフォルト、省略時この挙動）
- `scope: "per_persona"` — ペルソナごとに独立プロセスを起動、env はそのペルソナの文脈で解決

アドオンからは両スコープとも宣言可能とする（ペルソナ間でstateを共有したいケース等、global が必要な正当なユースケースはある）。

#### サーバー起動タイミング

per_persona スコープのサーバーは **遅延起動（lazy）** とする。該当ペルソナが初めてそのツールを呼び出した時点でプロセス起動。理由：

- ペルソナ数 × サーバー数のプロセスを常時起動するのはコスト高
- 使わないペルソナの分までプロセスを立てるのは無駄
- Elyth のようなサービス側のアカウント数上限に抵触しやすい

### B. サーバー名の隔離と内部インスタンス識別

#### アドオン名の自動プレフィックス

アドオン同梱の `mcp_servers.json`（`expansion_data/<addon_name>/mcp_servers.json`）で宣言された server_name は、SAIVerseが **内部的に `{addon_name}__{server_name}` へリネーム**して登録する。

- **例**: アドオン `saiverse-elyth-addon` が `"elyth"` を宣言 → 内部では `saiverse-elyth-addon__elyth`
- **例**: 同じアドオンから `"filesystem"` を宣言 → 内部では `saiverse-elyth-addon__filesystem`

ユーザー側 `user_data/mcp_servers.json` と `builtin_data/mcp_servers.json` は**プレフィックスなし**で登録される（ユーザーと本体は最上位特権領域、自由に命名できる）。ユーザー側と builtin_data で衝突した場合は既存の優先順位ルール（`user_data > builtin_data`）で解決。

#### 内部インスタンスキー

```
instance_key = "{qualified_server_name}:{scope_key}"
  qualified_server_name = "{addon_name}__{server_name}"  (アドオン由来)
                        = "{server_name}"                (user_data / builtin_data 由来)
  scope_key             = "global"
                        = "persona:{persona_id}"
```

具体例：
- `saiverse-elyth-addon__elyth:persona:air_city_a`
- `saiverse-elyth-addon__elyth:persona:sofia_city_a`
- `filesystem:global` （user_data 由来）

#### LLMに見えるツール名

通常は **プレフィックスなしの短い形**で見せる：`elyth__create_post`。これは同一ビルディング内で衝突が発生しない限り使う。

ビルディングにリンクされたツールを組み立てる時点で衝突検知を行い、**同名の tool_name を持つサーバーが複数存在する場合のみ**、該当アドオン由来のサーバーについてはプレフィックス付き `{addon_name}__{server_name}__{tool_name}` で提示する（将来拡張、詳細は後述）。

初期実装では、**アドオン由来サーバーは常にプレフィックス付きで提示**する（シンプルな実装優先）。将来的に衝突時のみ disambiguate する機構へ移行する。

#### 参照カウントによるライフサイクル管理

各インスタンスは、どのアドオン（および本体設定）から参照されているかをカウントする：

```
instance: saiverse-elyth-addon__elyth:persona:air_city_a
  refcount: 1
  referenced_by:
    - addon:saiverse-elyth-addon
```

- アドオンが有効化されると、そのアドオンが宣言する各サーバーの該当インスタンスの refcount+1
- アドオンが無効化されると refcount-1
- refcount == 0 になったらプロセス停止

これにより「複数アドオンから同じ global サーバーが参照されている場合、一方をOFFにしても他方が生かしてくれる」を自然に処理できる（自動プレフィックス方針ではアドオン間で同一インスタンスを共有するケースは原則発生しないが、将来の `shared: true` フラグ導入時に同機構で扱える）。

### C. AddonConfig ↔ MCP env の参照構文

現状の `${VAR}` プレースホルダー展開ロジック（`tools/mcp_config.py`）を拡張し、以下の参照を解決できるようにする。

| 構文 | 解決元 | 用途 |
|------|--------|------|
| `${env.VAR_NAME}` | OS環境変数 | 現状の `${VAR}` と同等（明示形、推奨） |
| `${VAR}` | OS環境変数 | 既存互換（残す） |
| `${addon.<addon_name>.<key>}` | `AddonConfig` (グローバル) | アドオン全体で共通の設定 |
| `${persona.addon.<addon_name>.<key>}` | `AddonPersonaConfig` (ペルソナ固有) → フォールバックで `AddonConfig` | ペルソナごとに異なる値（API key等） |

#### 解決タイミング

MCP サーバープロセスを起動する直前に env dict を解決する。per_persona スコープなら対象ペルソナIDを文脈として解決。未解決のプレースホルダーは起動失敗として扱い、ログに明示する（silent に空文字列にしない — CLAUDE.md のエラー握り潰し禁止原則に準拠）。

#### 参照元の命名規則

`${persona.addon.elyth.api_key}` の解釈：

- `persona` — 現在の実行文脈ペルソナ（per_persona スコープのMCPサーバー起動文脈から取得）
- `addon.elyth` — addon_name が "elyth" のAddonPersonaConfig JSON
- `api_key` — その JSON 内のキー

ネストしたキーアクセス（`${persona.addon.elyth.oauth.token}`）は将来課題。最初はフラットキーのみ対応。

### D. アドオン同梱 mcp_servers.json の扱い

`expansion_data/<addon_name>/mcp_servers.json` は既存のロード経路・優先順位を維持する。追加ルール：

- アドオン同梱の mcp_servers.json で宣言されたサーバー定義は、自動プレフィックス（設計B）を経て `qualified_server_name` が確定
- アドオンが refcount に加算する側として登録される
- アドオンが無効化されると対応インスタンスの refcount が減る
- スコープは global / per_persona 両方宣言可能

### E. Frontend UI（AddonManager 統合）

専用UIは作らず、AddonManager UI に「MCP管理セクション」を設ける。理由：アドオン以外でMCPを使うケースは現時点で想定されない。SAIVerse本体組み込みのMCP利用が出てきた時点で、その時は別途UIを切り出す。

MCP管理セクションの表示・操作：
- 起動中のMCPサーバーインスタンス一覧（instance_key 単位）
- 各インスタンスの接続ステータス、起動時刻、参照元アドオン一覧
- 再接続ボタン
- **手動停止ボタン**（refcount を無視して即停止、次回tool呼び出し時に遅延起動）
- アドオンページ側では、そのアドオンが参照する MCP サーバーと、必要な AddonConfig キーの充足状況を表示

### F. エラーハンドリング

#### 想定される起動失敗シチュエーション

1. **ランタイム未インストール** — `npx` / `uvx` / Python 等、サーバー起動コマンドのランタイムがPATHにない
2. **必須キー未設定** — 参照構文（`${persona.addon.x.y}` 等）が解決できない
3. **サーバー側認証失敗** — APIキーが不正 / 期限切れ（サーバー起動は成功するが初回tool呼び出しで401等）
4. **起動コマンドエラー** — npmパッケージ名typo、リポジトリ消滅、バージョン互換問題
5. **ネットワークエラー** — HTTP/SSE transport の場合のサーバー到達性
6. **プロセスクラッシュ** — 起動途中で子プロセスが異常終了

#### エラーメッセージ仕様

ペルソナ（LLM）に見えるエラーとユーザーに見えるエラーは別にする。

**ペルソナに見えるエラー** — ツール呼び出し結果としてシンプルなメッセージを返し、ペルソナが「使えない」と認識して代替行動を選べる形にする：
```
「このツールは現在利用できません（MCPサーバー '{qualified_server_name}' への接続に失敗）」
```

**ユーザーに見えるエラー**（AddonManager UI / ログ） — 原因特定と対処方法を示す：
```
「{addon_display_name} アドオンで使用される {server_name} MCPサーバーの起動に失敗しました。
アドオンの導入および設定が正常に完了しているか確認してください。
解決しない場合はアドオン制作者に問い合わせてください。
（エラー詳細: {category} — {original_error}）」
```

ここで `category` は上記1〜6の分類を日本語で。`original_error` は子プロセスstderrまたは例外メッセージ。

#### 失敗時のインスタンス状態

起動失敗したインスタンスは `failed` 状態として記録し、UIに表示する。次回tool呼び出しで再試行はする（ユーザーが設定修正した直後に自動復旧できる）が、短時間の連続失敗ではバックオフを入れる（連続呼び出しで子プロセスをフラッピングさせない）。

## 設計判断の理由

### なぜ参照構文を `mcp_servers.json` 側に書くのか

検討した3案：

1. **名前の自動マッピング** — AddonConfig の `elyth_api_key` を自動で ENV `ELYTH_API_KEY` に流す。→ 規約依存で脆い、どの値がどのenvに行くのか不透明
2. **`addon.json` 側に MCP envマッピングを宣言** — → 設定が `addon.json` と `mcp_servers.json` の2ファイルに分散、どちらが真実か曖昧
3. **`mcp_servers.json` 内で参照構文を書く** ← 採用

採用理由：
- 既存の `${VAR}` 展開ロジックを拡張するだけで済む
- 「このenvにこの値が入る」が `mcp_servers.json` 1ファイルで完結して読める
- OS env と addon値を同じ構文で扱える

### なぜ per_persona スコープを遅延起動にするのか

全ペルソナ分を常時起動すると、プロセス数が `ペルソナ数 × per_persona スコープのサーバー数` に比例して膨らむ。Elyth のようにサービス側でアカウント数上限（ベータ中は2まで等）を課しているケースもあり、全起動は実用的でない。起動済みインスタンスは保持する（毎回起動は遅すぎる）。アイドルタイムアウトは次フェーズ。

### なぜアドオン名を自動プレフィックスするのか

検討した代替案：

- **衝突時は辞書順で採用**: 下位アドオンが意図と違う定義で動くはめになり、予測困難
- **制作者に命名規約を強いる**: 制作者ごとに守られる保証がない、破る人が出る
- **衝突時は両方スキップ**: ユーザーにとって「動かない理由」が不明瞭

自動プレフィックスだと：
- アドオン制作者は衝突を気にせず自由に命名できる
- ユーザー側・builtin_data 側は特権領域として自由命名を維持
- 副作用（複数アドオンが汎用サーバー名を共有したい場合に別プロセスになる）は refcount の仕組み上問題なく、かつ将来 `shared: true` フラグで解決可能

### なぜサービス側のアカウント数上限を SAIVerse が感知しないのか

Elyth の「ベータ中は2AITuberまで」のような上限は、**APIキー発行側で縛られている**。SAIVerseが per_persona スコープでインスタンスを立てる限り、各ペルソナは自分のAPIキーを持つので、SAIVerse側で人数を制御する必要はない。

逆に「同じAPIキーを複数ペルソナに設定する」運用は技術的に可能だが、サービス側が意図していない共有になりうる。これは**SAIVerseとして防ぐべきでない**（技術的にも非対称アクセス制御は困難）。アドオン制作者がREADMEで「1ペルソナに1APIキーを推奨」と注意喚起する方針。

### なぜ AddonManager UI に統合するのか

現状、MCPをアドオン文脈の外で使う具体的ユースケースが想定されていない。汎用的なMCPサーバー管理UIを先に作ると、アドオン連携との二重管理になる。SAIVerse本体組み込みのMCP利用が出てきた時に、その時点で専用UIを切り出す方が無駄が少ない。

### アドオンから global スコープ宣言を許可する理由

ペルソナ間で state を共有したいケース（例: 共有ファイルシステム、共有ベクトルDB、共有タスクキュー）が現実にあり得る。「アドオンは per_persona のみ」と制約すると正当なユースケースを排除してしまう。global の副作用リスクはアドオン制作者が責任を負えばいい範囲であり、自由度を優先する。

## 将来拡張

### 1. 衝突時のみプレフィックス付与で disambiguate する機構

**現状（初期実装）**: アドオン由来のサーバー由来のツールは常に `{addon_name}__{server_name}__{tool_name}` でLLMに提示する。

**将来**: 同一ビルディング内のツール一覧構築時に衝突検知を行い、**衝突があるtool_nameだけ**プレフィックス付きで提示、それ以外は短い `{server_name}__{tool_name}` で提示する。一本のアドオンしか入っていない環境では常に短い名前が見える、という体験になる。

LLMに提示する表示名は SAIVerse が一元管理する。内部instance_keyは常に `{qualified_server_name}:{scope_key}` のまま変わらない（表示と識別の分離）。

### 2. `shared: true` フラグによる汎用サーバー共有

アドオン側が「このサーバーは汎用性が高く、他のアドオンと共有してよい」と宣言できるフラグ。宣言された場合、自動プレフィックスをスキップしてグローバル名で登録し、同名サーバーの既存インスタンスがあればそれを再利用する（refcount で管理）。

デフォルトは常に隔離 (`shared: false`) を維持することで、制作者が安易に global name を汚染することを防ぐ。

### 3. アイドルタイムアウトによるインスタンス停止

per_persona スコープのインスタンスが一定時間使われていない場合に自動停止する機構。プロセス数の肥大化防止。

### 4. ネストしたAddonConfigキーアクセス

`${persona.addon.elyth.oauth.token}` のようなドット記法でネストしたJSON値を参照できるようにする。

## 決定事項（インタビュー結果を反映）

1. **同名サーバー重複起動の防止** — アドオン由来サーバーは自動で `{addon_name}__{server_name}` にリネーム。ユーザー側・builtin_dataは特権領域で自由命名。衝突ルールは実質発生しない。
2. **停止タイミング** — 基本はプロセス終了時のみ（案C）。ただしアドオン無効化時は refcount 減算で自然停止、UI に手動停止ボタンを実装。アイドルタイムアウトは将来拡張。
3. **APIレスポンス形** — 内部詳細はSAIVerseが引き受け、ペルソナからは単一ツール名で呼べれば良い方針。`/api/mcp/servers` は instance_key ベースに拡張（実装時に詳細設計）。
4. **`${VAR}` と `${env.VAR}` の併存** — 既存互換で併存。`${env.VAR}` を推奨形としてドキュメント記載。
5. **アドオンからの global スコープ宣言** — 許可する。
6. **起動失敗時** — ペルソナ向けとユーザー向けで別メッセージ。ユーザー向けは原因カテゴリ付きで対処方法を示す。失敗インスタンスは `failed` 状態として記録、UIに表示、バックオフ付き自動再試行。
7. **LLMに見せるツール名** — 初期実装は常にアドオンプレフィックス付き。将来「衝突時のみ disambiguate、普段はプレフィックスなし」へ拡張（将来拡張1）。

## 実装ステップ（案）

1. `mcp_servers.json` に `scope` フィールド追加、パーサ拡張
2. アドオン同梱 mcp_servers.json の自動プレフィックス処理（`{addon_name}__{server_name}`）
3. `${env.VAR}`, `${addon.x.y}`, `${persona.addon.x.y}` の解決ロジック実装
4. `MCPClientManager` を拡張：instance_key 管理、参照カウント、ペルソナ別インスタンス管理
5. `tools/context.py` の active persona を MCP tool 呼び出しに伝搬、per_persona スコープの遅延起動実装
6. アドオン有効化/無効化イベントと refcount 連動
7. エラー分類とエラーメッセージ（ペルソナ向け/ユーザー向け）
8. AddonManager UI に MCP管理セクション追加（一覧、ステータス、再接続、手動停止）
9. `/api/mcp/servers` レスポンスを instance_key 対応に拡張
10. ドキュメント整備:
    - `docs/features/mcp-integration.md` に新機能を追記
    - アドオン制作ガイドに「MCP連携の書き方」セクション追加
    - 参照構文（`${persona.addon.x.y}` 等）のリファレンス

## 関連ドキュメント

- `docs/features/mcp-integration.md` — 既存のMCP対応機能ドキュメント
- `tools/mcp_client.py`, `tools/mcp_config.py` — 既存実装
- （未作成）AddonConfig / AddonPersonaConfig の設計ドキュメント
