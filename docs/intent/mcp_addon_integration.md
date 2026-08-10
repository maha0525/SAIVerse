# Intent: MCP と Addon の統合

**ステータス**: 確定 (§I は 2026-08-10 実装済み・実機検証待ち)

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

名前付きインスタンス（設計 G）の場合、同名ツールが複数インスタンスに並ぶため本体だけでは一意に振り分けられない。この場合は呼び出し側 addon の native wrapper が現在の実行文脈（例: ペルソナが居る Building）から対象インスタンスを解決する。本不変条件の達成手段は「global / per_persona は本体が文脈解決」「名前付きインスタンスは addon wrapper が解決」と分担するが、いずれの場合もペルソナがインスタンスを意識しない点は共通して守る。

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

→ **2026-08-10 改訂 (§I)**: 接続の張り時は「初回ツール呼び出し時」ではなく **Pulse 頭**へ変更が確定。ツール一覧の提示がその Pulse の生きた接続を前提にするため (§I の要請 2)。「全ペルソナ分を常時起動しない」という上の動機は Pulse 単位の接続でもそのまま保たれる。

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
| `${instance.<key>}` | 名前付きインスタンス起動時に addon が渡す per-instance context（設計 G） | 同一 server 定義から複数インスタンスを立てる際の個別値（token / port 等） |

#### 秘密情報を渡す先は transport で変わる (2026-08-09 追記)

stdio サーバーは SAIVerse 自身の子プロセスなので `env` が秘密情報の経路になる。**remote サーバー (`streamable_http` / `sse`) には env という経路が無い** — 認証情報は HTTP リクエストの `headers` に載せるほかない (Elyth Remote MCP は `Authorization: Bearer <api key>` を要求する)。

**認証情報を載せた接続は redirect を追わない。** httpx が cross-origin redirect で除去するのは `Authorization` だけで、`X-API-Key` のような他の認証ヘッダーは転送先ホストへそのまま送られる。MCP の SDK は `follow_redirects=True` を固定しているため、SAIVerse 側で redirect を無効化した client factory を差し込む (`_mcp_http_client_no_redirect`)。remote MCP のエンドポイントは固定 URL なので redirect を追う利得はなく、鍵を第三者へ渡す危険だけが残る。

条件は「ヘッダーを送るかどうか」で判定する — 認証情報が無ければ漏れる秘密も無いので、ヘッダー無しの remote サーバーは SDK 既定のまま (既存挙動を変えない)。**ヘッダーの種類 (Authorization かどうか) では判定しない**: 守るべきは「認証情報を第三者へ渡さない」という目的であって、特定のヘッダー名ではない。

初回の per_persona discovery (起動時にどれか 1 人のペルソナの設定で繋いでツール一覧を登録する工程) については、remote 化に伴い「未解決 placeholder を外部へ送らない」検査・候補ペルソナの複数試行・スキップが失敗一覧に出ない問題への対処を 2026-08-09 に重ねたが、**2026-08-10 に discovery という工程そのものの廃止が確定した (§I)**。候補処理の複雑化はこの工程が「誰の出来事でもない起動時に、誰かの鍵を借りる」構造を持つことの帰結であり、補修では収束しなかった (経緯は `docs/handoff/2026-08-10_elyth_remote_mcp_handoff.md` §3)。

この過程から残す教訓は一つ: **検査の条件は「未解決かどうか」(種類) ではなく「値が外部へ出るか」(目的) で書く。** 種類で書いた検査は、placeholder が自分の子プロセスにしか渡らない stdio の復旧経路まで巻き添えに塞いだ。

したがって参照構文は `headers` の値でも解決される。`_interpolate_value` が config 全体を再帰的に走るため、これは経路ごとの分岐ではなく「config のどこに書いても解決される」性質として担保されている。未解決検出 (`_find_unresolved_placeholders`) も同じく config 全体を見るので、キー未入力時に `missing_config` としてスペルを隠す挙動は env と headers で同一に効く。

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

### G. 名前付きインスタンス（同一 server 定義からの複数動的起動）

#### 動機

これまで instance_key の scope_key は `global` / `persona:{id}` の 2 値で、1 つの server 定義からは（per_persona でも）「ペルソナ 1 人につき 1 インスタンス」しか立たなかった。だが「**同一 server 定義を設定違いで複数同時に立てたい**」需要がある。最初の駆動ユースケースは saiverse-stackchan-addon が複数の物理機体それぞれに gateway subprocess を立てるケース（各 gateway は別ポート・別 token で listen。詳細は `stackchan_vessel.md` 設計 K）。これは Stack-chan 固有ではなく、「同一 MCP server を別エンドポイント / 別認証で N 個」という汎用パターンとして本体に置く。

#### scope_key への instance 次元追加

設計 B の scope_key に第 3 の型を足す:

```
scope_key = "global"
          = "persona:{persona_id}"
          = "instance:{instance_id}"   ← 新規（名前付きインスタンス）
```

`instance:{instance_id}` は「同一 `qualified_server_name` から、`instance_id` で区別される複数の独立 subprocess」を表す。`instance_id` の意味（vessel_id 等）は宣言側 addon が決め、本体は不透明な識別子として扱う。具体例: `saiverse-stackchan-addon__stackchan:instance:<vessel_uuid>`。

#### per-instance config context

各インスタンスは別々の env（token / port 等）で起動する必要がある。設計 C の参照構文に `${instance.<key>}` を足し、名前付きインスタンス起動時に addon が渡す per-instance context dict から解決する。既存の `resolve_config_placeholders` が persona context で `${persona.addon.x.y}` を解決する仕組みを、instance context に拡張する形（`_start_instance` が context を受け取り、解決に渡す）。

例: stackchan の gateway インスタンスは `mcp_servers.json` の env に `${instance.master_token}` / `${instance.ws_port}` / `${instance.capture_port}` を書き、addon が vessel ごとの値を context で渡す。

#### 動的 register / start / stop

global / per_persona は config source（`mcp_servers.json`）を起点に起動するが、名前付きインスタンスは **実行時に外部 source（addon が持つ DB 等）から動的に登録・起動・停止**する。本体 MCP client に口を足す:

- `register_instance(qualified_server_name, instance_id, context)` — per-instance context を添えて登録し、起動する
- `stop_instance(instance_key)` — 停止（既存 `manual_stop_instance` を流用）
- `_start_instance` を per-instance context を受け取れるよう拡張

addon は自分のライフサイクル（例: vessel ペアリング追加 / 削除、アプリ起動時の全件ロード）でこれらを呼ぶ。refcount は addon referrer タグで管理（既存機構そのまま）。

**静的宣言との関係**: 名前付きインスタンスは `mcp_servers.json` に「テンプレート」として 1 つの server 定義を書き（`${instance.*}` プレースホルダ入り）、実体は動的 register で N 個立てる。「機体数ぶんのエントリを静的手書き」はしない（駆動ユースケース側の要件、`stackchan_vessel.md` K-2）。

#### ツール名前空間と不変条件 #6 の達成

名前付きインスタンスのツールは instance_key ごとに登録されるため、LLM から見ると複数インスタンスで同名ツールが並ぶ。これを単一論理名に集約してペルソナにインスタンス差を見せないのは **呼び出し側 addon の native wrapper の責務**（本体は instance ごとの素の名前空間を提供するに留める）。不変条件 #6 の達成手段の分担はその不変条件本文に記載した通り。

### H. サーバー側のツール変化への追随 (`spell_tools_default`, 2026-08-09)

#### 動機

`spell_tools` は許可リストであり、そこに名前の無いツールは `spell=False` になってペルソナから呼べない。外部サービスがツールを増減させるたびに、リポジトリオーナーが JSON へ手で名前を書き写す必要がある。

Elyth は API v1 → v2 の移行で 23 個中 7 個が消え 9 個が増えた (Lobby 系が消滅し、DM / Field / GLYPH 系が追加)。この速度で変わるサービスに対し、手作業の追記を運用の前提に置くことはできない。

#### 設計

サーバー定義に `spell_tools_default` を宣言すると、`spell_tools` に名前の無いツールの既定値になる。既定値は宣言の目的から導く: `spell` は `true` (このキーを書く目的が自動開放そのもの)、`visible` は `false` (サービスがツールを増やすたびに全ペルソナの head が太るのを防ぐ)。

`spell_tools` に書かれたエントリの意味は変わらない (`spell=true`、`visible` 既定 `true`)。**キーを省略した場合の挙動も従来のまま** — `saiverse-stackchan-addon` は `spell_tools` を「生 MCP ツールを隠して native wrapper に差し替える」ために使っており、全体の既定を反転すると `gateway_config_set` や `i2c_write` のような管理者向けツールがペルソナへ開く。したがって**この機能はサーバー単位のオプトインでなければならない**。

#### 不変条件: 関所は 1 個、場所は「宣言するかどうか」

自動開放には「サービスが将来足す危険なツールも自動で開く」という弱点が構造的に残る。まだ存在しない名前を先回りして拒否することはできない。

**この弱点に対する歯止めは `spell_tools_default` を書くか書かないかの一点だけであり、それ以外の場所に関所を増やさない。** ツール単位の承認を挟むとペルソナの自律行動そのものを止めるか、承認疲れで形骸化する。危険なツールを増やしうるサービスでは、このキーを書かない運用で対処する。

自動有効化されたツールは起動時に INFO ログへ記録するが、**これは事後に何が起きたか追うための記録であって歯止めではない** (2026-08-09 まはー裁定「あったところで歯止めになるとは考えないでほしい。だって普通ログ読まないもん」)。実装・設計の議論でこのログを安全装置の欄に数えないこと。

#### 既知の限界: 撤回は再接続では効かない (2026-08-09、レビュー指摘)

`spell_tools_default` を削除または `spell: false` へ変えても、**複数の per_persona 接続が生きている間の `reconnect_server()` では古いツール登録が残る**。`_unregister_instance_tools()` は他の instance が残っている間は実質 no-op で、続く `_register_tools()` は既に登録済みの namespaced name を skip するため、`SPELL_TOOL_NAMES` と wrapper が残留する。**撤回を確実に効かせるには SAIVerse の再起動が必要。**

これは「関所は 1 個」という設計の弱点であり、ログでは止められない。根はツール登録が接続単位である点で、`docs/issues/mcp_remote_connection_recovery_gaps.md` と同じ領域 (qualified server 単位での原子的な再構築が要る)。**関所の説明として「キーを消せば閉じる」と書くのは不正確** — 再起動までは閉じない。

→ **2026-08-10 追記**: §I の再設計 (提示する一覧を毎 Pulse / Beat、生きた接続から読み直す) が入ると、「一度登録したものが残り続ける」という根が per_persona については消えるため、この限界は機構ごと解消される見込み。実装時に確認する。

#### 既知の限界: 認証ヘッダー付き接続は redirect を一切追わない

`headers` が非空なら、それが認証情報でなくても `follow_redirects=False` になる。同一オリジンの canonical URL へ 301/307 する endpoint や、`Accept` 等だけを送る endpoint も繋がらない。**remote MCP の `url` には redirect を経ない最終 URL を書くこと。** origin を検証して同一オリジンだけ許可する実装は将来課題 (実装するまでは、繋がらない側に倒して鍵を守る)。

### I. ツール一覧の所有と鮮度 — 起動時 discovery の廃止 (2026-08-10 実装済み・実機検証待ち)

#### 出発点

per_persona サーバーのツール検出は「起動時に、どれか 1 人のペルソナの設定で繋いで一覧を取り、全員分を登録する」形だった。remote 化で「未設定の鍵 placeholder を外部へ送らない」制約が加わると「その 1 人をどう選ぶか」の候補処理が必要になり、3 巡の補修が毎回新しい穴を作った (経緯: ハンドオフ 2026-08-10 §3)。まはーの問い**「設定してるペルソナでしか使える必要はないのに、なぜ起動時に誰か 1 人の鍵で繋ぐのか」**から構造を再考した結果、「誰を選ぶか」は解くべき問題ではなく、検出が「誰の出来事でもない起動時」に置かれていることの副産物だと確定した。よって起動時検出そのものを廃止する。

#### 要請 (この設計が守る不変条件)

1. **ツール一覧の真実はサーバー側にあり、証言できるのは有効な鍵で張られた生きた接続だけ。** キャッシュ・代表者の使い捨て接続・保存済み一覧は代用品であり、古い情報をペルソナに渡しうるので、一覧の出どころにしない。
2. **LLM に提示する一覧は、提示するその時点で、そのペルソナ自身の接続から読んだものであること。** 提示の瞬間に確定していない一覧を渡さない。
3. **鍵が無い・失効している・サーバーが落ちているペルソナにはツールが無い。** それは故障ではなく正直な状態。逆に、設定済みペルソナのツールが他人の鍵の都合で消えることがあってはならない。
4. **一覧の変動はペルソナが知覚する出来事として届く。** Building 移動でスペルが変わるのと同じ検知器・同じ配送路を使い、変動専用の新しい経路や知覚型を作らない。

#### 機構

1. **接続は Pulse 頭で張り、Pulse 中維持する。** そのペルソナの placeholder が解決できなければ張らない — 誰の鍵も借りないので、未解決の placeholder が外部へ出る経路が構造的に存在しない。接続失敗は既存の失敗記録 (`_record_failure` / `get_failed_instances`) に載せ、UI から見えるようにする。
2. **一覧は毎 Beat 頭で聞き直す。** 張ってある接続の上の `tools/list` は軽い一往復。ツール呼び出し自体が一覧を変えるサーバー (モードチェンジ型 — MCP プロトコルが `tools/list_changed` 通知を定義しており、正規に想定された挙動) の変動を、同じ Pulse 内の次の Beat で拾うため。
3. **Beat 頭の並びは「一覧取得 → 状態更新 → 検知 (spell_list Section の snapshot 比較 → 知覚バッファへ push) → flush → 生成」。** この順でなければ、積んだ知覚が次の Beat まで読まれない。検知と配送は Building 移動のスペル変動と完全に同じ器 (`diff_to_notifications` → 知覚バッファ → Beat 頭 flush、`perception_buffer.md` §4.2/§4.5)。
4. **起動時の `_discover_per_persona_tools` と候補ペルソナ選択は廃止する。** per_persona ツールの「起動時に一括登録して全員に見せる」形をやめ、ペルソナ単位の照会 (spell_list Section の capture が参照する `is_tool_available_for_persona` の裏を、そのペルソナの live 一覧に付け替える) に置き換える。

5. **所属記録 (`_persona_tool_names`) は提示のフィルタであって、実行の認可ではない。** 実行の認可は「そのペルソナ自身の鍵で張った接続に、サーバーが応じるか」だけが持つ — ツール実行は本人の instance を通り、他人の鍵に乗る経路が構造的に無い。だから所属が空のペルソナがツール名を書いても、自分の接続で試みて正直に失敗するだけで、他人のアカウントで実行されることはない。所属の役目は「使えないものを一覧に出さない」= LLM に無駄な失敗を踏ませないことであり、**認可の最後の砦をここに期待しない** (期待すると、提示側の穴が権限の穴に見えて対策の置き場を間違える)。同じ理由で、`${...}` を外へ出さない歯止めも提示側ではなく接続の直前に置く (下の「関所の位置」)。

#### fetch 失敗の扱い (失敗 ≠ 消滅)

- **Beat 頭の聞き直しが失敗** → その Pulse で直前に得ていた一覧を維持し、「使えなくなりました」は発火しない。取得失敗は「一覧が変わった」証拠ではない。
- **Pulse 頭で接続からできない** → その Pulse はそのサーバーのツール無し。失敗一覧に記録する。

#### 影響範囲

- 対象は **per_persona スコープのみ** (現在は Elyth 一つ)。global (共有一本、現在利用者なし) と instance_template (stackchan — 検出を最初から通らず、ペルソナ向けスペルは native wrapper が提供) は従来どおり。global を将来同じ原理 (Beat 頭で共有接続から聞き直す) に寄せることは可能だが今回のスコープ外。
- 将来 stdio の per_persona サーバーが現れた場合、「接続」はプロセスの生存であり、Pulse ごとに殺さない。原理は「提示する一覧は生きた接続から読む」であって「接続を毎回張り直す」ではない。

#### この設計が消すもの

- 代表者選び (候補母集団・順次 probe・resolve 結果の三分類) と、その補修の複雑さ — 機構ごと消滅
- 「選ばれた 1 人の鍵失効で全員のツールが消える」故障モード
- §H の「撤回は再接続では効かない」限界 (per_persona について。上記追記参照)
- `docs/issues/mcp_remote_connection_recovery_gaps.md` の 2 (鍵保存後に検出が再実行されない) と 3 (キー未設定が UI に出ない) — 「一回きりの検出」という工程が無くなり、鍵保存の次の Pulse から自然に使える。1 (切れた接続を掴み続ける) も接続が Pulse 単位になることで形が変わる (issue 側に追記)

#### 引き換えに受け入れるもの

- Pulse 頭の接続確立 + Beat 頭の `tools/list` 一往復ぶんのレイテンシ。remote HTTP 前提で軽い見込みだが、実測して重ければそのとき詰める
- head の spell_list は Metabolism まで凍結のまま。変動直後は「head は旧一覧・tail の知覚が訂正する」という Building 移動 (`perception_buffer.md` §5.4) と同じ作法になる
- レガシー実行分岐 (呼び出し元に running loop がある場合の executor 退避) では Beat 頭消費が飛び、知覚が次 Pulse 頭に遅れる既知の縮退をそのまま継承する (恒久解は Beat ロックのトークン化 §6-6b に合流)

#### 設計時の未確定点と、実装で出した答え

- **spell_list Section capture の参照先** → `is_tool_available_for_persona` の per_persona 分岐を所属記録優先に付け替えた (未評価のときだけ従来の config 近似)。Section 側は変更なし。
- **プロンプトキャッシュとの関係** → head の spell_list は従来どおり Metabolism まで凍結。一覧の変動は tail の知覚で訂正する (Building 移動と同じ作法) ので、Beat ごとの取得が head を揺らすことはない。
- **per_persona 接続の Pulse 終端** → 切らない。取得時に `persona:<id>` の自己参照を付け、停止はアドオン無効化 / 手動停止 (= refcount の除去) 側が担う。Pulse ごとに張り直すのは原理ではない (§機構 1 の注記)。
- **Beat 頭の検知の範囲** → 限定しない。ただし**一覧が変わった Beat だけ**検知器を呼ぶので、全 Section の capture が Beat 頻度で走ることはない。
- **`visible: false` ツールと `addon_spell_help` の列挙元との整合** → 未着手 (per_persona で `visible: false` を使うアドオンが現れていないため、実害の確認ができていない)。

#### 実装で確定した細部 (2026-08-10 実装時)

- **spell_list の鮮度検査の除外**: 再起動直後は per_persona ツールが登録簿に無いが、これは「まだ今セッションで誰も取得していない」だけで消えたのではない。head snapshot の鮮度検査 (`_check_spell_freshness`) が per_persona 名の欠落だけを理由に失効を出すと、Pulse 前の head 構築 (プレビュー等) が再キャプチャ → 通知既読 (B) のリセットを起こし、初回取得時に全ツールぶんの「使えるようになりました」が知覚へ流れ込む。per_persona サーバーの名前空間に属する名前の欠落は失効にしない。
- **取得の多重走行ガード**: 同期側が timeout で見放した取得は MCP ループ上で走り続けるため、次の Pulse の取得が同じ (サーバー, ペルソナ) の接続開始を重ねないよう走行中集合で弾く。
- **所属は接続の派生状態、無効化は空集合で**: 接続を殺す関所 (`_shutdown_instance`) と取得失敗の各経路は、所属記録を**「確認済み・利用不可」= 空集合**にする (手動停止・再接続・refcount 0・鍵消失・接続失敗)。残すと「切れた接続の一覧」が真実の顔で出続け、逆に **pop で消すと「未取得」と同じ状態に戻って config 近似が復活し fail-open に反転する** (ローカルレビュー 2026-08-10 の 2 指摘)。「まだ一度も評価していない (近似で可)」と「評価した結果使えない (不可)」を区別する。完全に忘れて近似へ戻すのはアドオン入れ直しだけ。次の Pulse 頭の取得が復元するので、生きた運用で通知はばたつかない。
- **手動停止の意味の変化**: per_persona サーバーを UI から手動停止しても、そのペルソナの次の Pulse 頭で接続が張り直される (従来は「次のツール呼び出しまで停止」)。ペルソナが活動している限り停止が数分しか持たないのは新設計の帰結で、止めたければアドオンを無効化する。
- **鍵未設定は失敗一覧に載せない**: 未設定は故障ではなく「まだ使わない」状態 (充足状況はアドオン設定画面が示す)。接続失敗 (鍵不正・期限切れ・到達不能) は従来どおり失敗記録 → UI 表示。

#### レビューの消し込みで確定した点 (2026-08-10、Codex レビュー後)

- **関所の位置 — 未解決 placeholder の検査は `MCPServerConnection.connect()` に 1 個だけ**。実装当初は起動側の入口 (`_start_instance`・取得経路) で検査していたが、再接続経路 (`reconnect_server`) が検査を通らず、鍵を消した後の再接続で `${persona.addon...}` の literal が remote のヘッダーに載る穴が残っていた。**守りたいのは「値が外へ出ない」という結果なので、検査は結果を作る場所 (transport を開く直前) に置く** — 入口を数え上げる形は、入口を一つ見落とした時点で漏れる。入口側の事前判定は「繋ぐ価値があるか」の方針判断 (鍵未設定を失敗記録に載せないための早期 return) としてだけ残す。
- **再接続は fail-closed**: 再解決の結果に `${...}` が残ったら、旧鍵が焼き込まれた現行接続も畳む (鍵を消した利用者の意図は「使わせない」であり、起動時の値で喋り続ける接続を残す方が有害)。再解決そのものが例外で落ちた回は、逆に現行接続を**触らない** — 現在の設定値が分からないなら kill + respawn は目的を果たさないまま生きた接続を殺すだけになる。⚠️ 既知の限界: `tools/mcp_config` の解決は AddonConfig 読み取りの失敗を握り潰して placeholder を残すため、「消された」と「読めなかった」が呼び出し側から区別できない。後者を前者として扱う (= 畳む) 側に倒してある。誤って畳んでも次の Pulse 頭の取得が張り直すため、実害は 1 Pulse ぶんのツール不在。
- **Pulse root は 2 本ある**: 会話・schedule・auto の Pulse (`run_meta_user`) と作業セッション (`sea/work_session.py`)。取得を前者にだけ置いていたため、スペルを一度も撃たない作業セッションは接続ゼロのまま head を組んでいた。頭の一手は `sea/mcp_tool_refresh.refresh_mcp_tools_at_head` に切り出し、両 root と Beat 境界の 3 箇所がこれを呼ぶ (Pulse root を増やすときはこの 1 行を頭に置く)。
- **取得できなかった Pulse は fail-closed**: 同期橋は取得を投げる**前**に、そのペルソナの未評価の所属を空集合へ倒す。timeout で結果を受け取れなかったときに「未評価」のままだと config 近似 (鍵が解決できれば使える顔) へ戻り、一度も繋げていないツールを提示してしまう。倒すのは未評価のみで、実績のある一覧は触らない。
- **無効化を跨いだ書き込みは捨てる**: 所属に版番号を持たせ、意図的な無効化 (停止・鍵消失・アドオン入れ直し) が版を進める。timeout で見放された取得が完了して古い一覧を書き戻すと「止めたのにツールが復活する」ため、開始時と版が違う書き込みは捨てる。
- **死んだ接続は Beat 頭で結論になる**: ツールコールの失敗で接続は切れるが `_connections` には残る。Beat 頭 (`connect=False`) が「接続オブジェクトはあるが生きていない」を見たら、証言者不在の結論として所属を空集合へ倒す (次の Pulse 頭が張り直す)。接続ライフサイクル自体の穴 (死んだ接続を掴み続ける・健全性検査が無い) は `docs/issues/mcp_remote_connection_recovery_gaps.md` の 1 が引き続き持つ。

#### 検証の旅

- 鍵設定済みペルソナ: Pulse 頭で接続が張られ、スペル一覧に Elyth ツールが出て、呼べる
- 鍵未設定ペルソナ: ツールが見えず、UI に理由が出る
- 鍵を保存した直後: 再起動なしで、次の Pulse から使える
- Pulse 内でツール呼び出しが一覧を変えた場合: 次の Beat の生成が変動を知覚している (sea_trace + SAIMemory の event_message で確認)
- **会話 Pulse を通さない作業セッション** (コマ発火 → `run_work_session`) でも Elyth ツールがスペル一覧に出る
- **鍵を消した後**: 再接続でも新規接続でも外部へ何も送られない (アドオン設定から鍵を削除 → 該当ペルソナの Pulse を回し、backend.log に `missing_config` の記録が出て、Elyth 側にリクエストが届いていないこと)
- Building 移動によるスペル変動の既存通知が壊れていない

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
