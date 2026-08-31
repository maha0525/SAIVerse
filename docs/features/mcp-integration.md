# MCP連携

SAIVerse は MCP (Model Context Protocol) クライアントとして外部ツールサーバーへ接続できます。本ドキュメントは実装された機能を利用者視点でまとめたものです。設計の意図や不変条件については `docs/intent/mcp_addon_integration.md` を参照してください。

## 概要

- `mcp_servers.json` に定義したサーバーへ起動時に接続します。
- 見つかったツールは `server__tool` 形式で既存の `TOOL_REGISTRY` に登録されます。
- 必要なものだけ `spell_tools` で選ぶと、`/spell` からも使えます。サーバー側のツール追加に自動追随したい場合は `spell_tools_default` を宣言します。
- 接続方式は stdio（子プロセス）と remote（`streamable_http` / `sse`）から選べます。remote では認証情報を `headers` に載せます。
- `expansion_data/<addon>/mcp_servers.json` に置けば、アドオン配布物からも MCP サーバーを宣言できます。
- ペルソナごとに別アカウントを持たせたいサービス（Elyth 等）向けに `scope: "per_persona"` が選べます。
- アドオン由来サーバー名は自動で `<addon_name>__` にプレフィックスされ、他アドオンやユーザー設定と衝突しないよう隔離されます。
- API キー等の秘密情報は、`mcp_servers.json` に直書きせず AddonConfig/AddonPersonaConfig から参照構文で注入できます。

## 設定場所

優先順位は次の通りです。

1. `~/.saiverse/user_data/mcp_servers.json`
2. `~/.saiverse/user_data/<project>/mcp_servers.json`
3. `expansion_data/<pack>/mcp_servers.json`
4. `builtin_data/mcp_servers.json`

**1, 2, 4** は特権領域扱いで、宣言した `server_name` がそのまま登録キーになります。同名があれば優先順位で上位が使われます。

**3（アドオン由来）** は自動的に `<addon_name>__<server_name>` へリネームされて登録されます。これによりアドオン制作者は衝突を気にせず任意の名前を使えます。

## 設定例（基本）

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/workspace"],
      "timeout": 120,
      "spell_tools": [
        {"name": "read_file", "display_name": "ファイル参照"}
      ]
    }
  }
}
```

## 接続方式 (`transport`)

`command` を書くと **stdio**（SAIVerse が子プロセスとして起動し、標準入出力で会話する）になります。`command` がない場合は `transport` フィールドで選び、省略時は `"streamable_http"` です。

| 指定 | 意味 |
|------|------|
| `command` あり | stdio。SAIVerse が子プロセスを起動する |
| `"transport": "streamable_http"` | リモートの HTTP エンドポイントへ接続（`command` 省略時の既定） |
| `"transport": "sse"` | リモートの SSE エンドポイントへ接続 |

リモート接続では接続先を `url` で指定します。子プロセスではないので `env` で認証情報を渡す経路がなく、代わりに `headers` に載せます。

```json
{
  "mcpServers": {
    "elyth": {
      "url": "https://elythworld.com/api/mcp/remote",
      "transport": "streamable_http",
      "headers": {
        "Authorization": "Bearer ${persona.addon.saiverse-elyth-addon.api_key}"
      },
      "scope": "per_persona"
    }
  }
}
```

`headers` の値にも下記の参照構文が使えます。未解決のまま残った場合は `env` と同じく `missing_config` 扱いになり、そのツールはペルソナのスペル一覧から隠れます。

## スコープ (`scope`)

サーバー定義に `scope` フィールドを指定できます。省略時は `"global"` です。

### `scope: "global"` (既定)

起動時に1プロセス立ち上がり、全ペルソナで共有します。ファイルシステム、共有キャッシュ、共有ベクトル DB のように state を共有すべきサーバー向き。

### `scope: "per_persona"`

ペルソナごとに独立プロセスを起動します。AIエージェント単位でアカウント発行する外部サービス（Elyth, Twitter, Mastodon 等）向き。

- **ツール一覧は各ペルソナ自身の接続から取得します**（`docs/intent/mcp_addon_integration.md` §I）。ペルソナの Pulse（思考サイクル）の頭で、そのペルソナのキーが解決できれば接続を張って一覧を取得し、Pulse 中は Beat（生成 1 回）ごとに一覧を聞き直します。サーバー側でツールが増減すると、ペルソナは「スペル◯◯が使えるようになりました / 使えなくなりました」という知覚として受け取ります（Building 移動でスペルが変わるときと同じ通知経路）。
- キー未設定のペルソナにはそのサーバーのツールが現れません。**キーを保存すれば、次の Pulse から自動的に使えます**（再起動やアドオンの再有効化は不要）。
- 起動時の一括 tool discovery（任意の 1 ペルソナの env で接続 → 切断）は 2026-08-10 に廃止されました。
- ペルソナごとに異なる API キーが必要な場合は、その値を `${persona.addon.<addon>.<key>}` で参照します（stdio なら `env`、remote なら `headers`）。

```json
{
  "mcpServers": {
    "elyth": {
      "url": "${addon.saiverse-elyth-addon.mcp_url}",
      "transport": "streamable_http",
      "headers": {
        "Authorization": "Bearer ${persona.addon.saiverse-elyth-addon.api_key}"
      },
      "scope": "per_persona"
    }
  }
}
```

remote 接続では子プロセスが立たないため、「ペルソナごとに独立プロセス」ではなく「ペルソナごとに独立した接続」になります。instance_key による管理と遅延起動の扱いは stdio と同じです。

## env 参照構文

サーバー定義の文字列値（`env` の値、`args` の要素、`headers` の値、`url` など）に `${...}` プレースホルダーを書けます。

| 構文 | 解決元 |
|------|--------|
| `${env.VAR_NAME}` | OS環境変数（推奨形） |
| `${VAR_NAME}` | OS環境変数（既存互換、`.` を含まないキーに限る） |
| `${addon.<addon_name>.<key>}` | `AddonConfig` のグローバルパラメータ |
| `${persona.addon.<addon_name>.<key>}` | `AddonPersonaConfig` → グローバル → `addon.json` デフォルトの順で解決 |

`${persona.addon.*}` は `scope: "per_persona"` サーバーでのみ意味を持ちます（global スコープでは persona 文脈がないため解決されません）。

未解決のプレースホルダーが残った状態では **サーバーは起動せず、「missing_config」カテゴリの失敗**として記録されます。silent に空文字列に置換されることはありません。

ただし区別が一つあります。`scope: "per_persona"` サーバーで**キーが未設定**のペルソナは、失敗一覧に記録されません — 未設定は故障ではなく「そのペルソナはまだこのサービスを使わない」という普通の状態だからです（キーの充足状況はアドオン設定画面に出ます）。接続の失敗（キーが不正・期限切れ・サーバー到達不能など）は従来どおり失敗として記録され、UI に表示されます。

## spell_tools

MCP サーバーが公開するツールは、すべて `TOOL_REGISTRY` に登録されます。そこから先に独立した 2 つのスイッチがあります。

- **スペルになるか (`spell`)** — ペルソナが平文応答から呼べるかどうか。偽なら呼ぶ手段がありません。
- **一覧に載るか (`visible`)** — ペルソナの system prompt のスペル一覧に載せるかどうか。**偽でも呼ぶことはでき**、`addon_spell_help` を通じてペルソナ自身が発見できます。head を膨らませたくないツールに使います。

`spell_tools` に名前を書いたツールは `spell=true` になり、`visible` は既定 `true`（エントリで指定可）です。

`spell_tools` は以下のどちらでも書けます。

```json
{ "spell_tools": ["read_file", "list_directory"] }
```

```json
{
  "spell_tools": {
    "read_file": {"display_name": "ファイル参照"},
    "list_directory": true
  }
}
```

スペル名は `filesystem__read_file` のように名前空間付きになります。アドオン由来の場合は `<addon_name>__<server_name>__<tool_name>` の形になります。

### spell_tools_default（サーバーのツール追加に追随する）

`spell_tools` は許可リストなので、サーバー側が新しいツールを増やすたびに JSON へ書き足さないとペルソナから使えません。ツールの入れ替えが速いサービス（Elyth は v1 の 23 個から v2 の 25 個へ移る際に 7 個が消え 9 個が増えました）では、これが運用の負担になります。

サーバー定義に `spell_tools_default` を宣言すると、`spell_tools` に名前がないツールの既定値を決められます。

```json
{
  "spell_tools_default": {"spell": true, "visible": false},
  "spell_tools": [
    {"name": "create_post", "display_name": "投稿する", "visible": true}
  ]
}
```

こう書くと、サーバーが後から増やしたツールは自動でスペルになり、system prompt には載りません。ペルソナは `addon_spell_help` で発見できます。`{"spell_tools_default": true}` は `{"spell": true, "visible": false}` の省略形です。

**省略時は従来どおり**、`spell_tools` に無いツールは `spell=false`（ペルソナから呼べない）のままです。生の MCP ツールを native wrapper の裏に隠す設計（`saiverse-stackchan-addon`）はこの挙動に依存しているため、既定は変わりません。

**このキーを書くかどうかが唯一の関所です。** 自動で有効になったツールは起動時にログへ記録されますが、それは事後に何が起きたか追うための記録であって、歯止めではありません（ログは実際には読まれません）。危険なツールを増やしうるサービスでは、このキーを書かないでください。

## ライフサイクル (参照カウント)

各サーバーインスタンスは、参照元 (`referenced_by`) の集合で管理されます。

- アドオンから宣言されたサーバーは、そのアドオンが有効な間だけ参照されます。
- ユーザー設定 (`user_data/`) と builtin からのサーバーは起動中ずっと参照されます。
- アドオンを **無効化** すると、そのアドオン由来サーバーの参照が外れ、refcount がゼロになったプロセスは停止します。
- アドオンを **有効化** し直すと、global スコープは再起動します。per_persona スコープは各ペルソナの次の Pulse 頭で自分のキーによりツール一覧を取り直します。
- UI から**手動停止**することもできます。**per_persona スコープの手動停止は、そのペルソナが次に Pulse を打った時点で自動的に張り直されます** — 一覧を本人の生きた接続から取る設計 (§I) の帰結です。恒久的に止めたい場合はアドオンを無効化してください（`docs/issues/mcp_per_persona_manual_stop_revives.md`）。global スコープは参照が再追加されるまで停止したままです。
- 再接続 (`POST /api/mcp/servers/{server_name}/reconnect`) に失敗したインスタンスは、切れた接続を掴み続けずに畳まれ、失敗一覧（`GET /api/mcp/failures`）に出ます。per_persona は次の Pulse 頭が、名前付きインスタンスはツール呼び出しか `POST /api/mcp/instances/retry` がやり直します。
- 再接続の結果は 3 種類あり、レスポンスの `outcome` で区別します。`reconnected` (繋ぎ直した) / `failed` (繋ごうとして失敗した) / `no_instances` (繋ぎ直す接続がまだ無い)。**`no_instances` は失敗ではありません** — per_persona スコープはペルソナが動き出すまで接続を持たないので、それが常態です。この場合レスポンスには `error` ではなく `message` (画面に出す説明) が入り、設定保存時の自動再接続もログを警告に上げません。per_persona のサーバーに設定を入れ直したときは、再接続ボタンではなく**そのペルソナが次に動いたとき** (会話や自律の Pulse) に新しい設定で繋がります。

## エラー分類

サーバー起動失敗は以下のカテゴリで記録されます。

| カテゴリ | 意味 |
|----------|------|
| `runtime_missing` | `npx`/`uvx`/`python` 等のランタイムが PATH にない |
| `missing_config` | 必須の env 値が未解決（参照構文が解決できない等） |
| `auth_failed` | サーバー側の認証失敗（401/403 等） |
| `command_error` | 起動コマンドエラー（npm パッケージ名誤り、リポジトリ消滅等） |
| `network` | ネットワーク到達性の問題（タイムアウト、DNS 等） |
| `process_crash` | 子プロセスが異常終了 |
| `busy` | 同じインスタンスの起動・停止処理が進行中（故障ではない一時的な状態） |
| `service_unavailable` | 接続先までは届いたが、向こうが応答できないと答えた（502/503/504）。利用者の設定は正しく、待つ以外にできることがない |
| `unknown` | 分類不能 |

`runtime_missing` と `process_crash` は stdio（子プロセスを起動する方式）でのみ発生します。

分類は例外の文字列を手がかりにしますが、**anyio / TaskGroup は本当の失敗を `ExceptionGroup` に包んで投げ、包み自身は "unhandled errors in a TaskGroup (1 sub-exception)" としか名乗りません**。そのため分類器は最初に包みを剥がし、中の例外を順に見ます。剥がさないと中身が何であれ `unknown` に落ちます（2026-08-30、接続先の 503 が「不明なエラー」として画面に出て、原因が利用者から見えなくなりました）。

`busy` だけは**失敗一覧に記録されません**。同じインスタンスを同時に起動しようとした（例: ペルソナの Pulse 頭とツール呼び出しの遅延起動が重なった）ときに、二重起動を避けるため片方を断った状態で、故障ではないので backoff も焚きません。断られた側は「いまは使えない」とだけ報告し、次の Pulse 頭で自然に復帰します。

`command_error` は本来 stdio 向けのカテゴリですが、**remote 接続でも出ることがあります**。失敗の分類が例外メッセージの文字列を見ており、`not found` を含むものを `command_error` に振り分けるためです。接続先 URL が間違っていて HTTP 404 が返る場合がこれに当たり、**「コマンドエラー」という表示で URL の誤りが報告されます**。remote で `command_error` が出たら、まず `url` の値を疑ってください。

連続失敗時は exponential backoff（初期 2 秒、最大 60 秒）で再試行を抑制します。UI の手動リトライや `POST /api/mcp/instances/retry` でバックオフを強制解除できます。

失敗時のメッセージは **ペルソナ向け（簡潔、行動変更を促す）** と **ユーザー向け（詳細、対処方法付き）** で分離されます。前者はツール応答として返り、後者はログと API レスポンスに載ります。

## API

### 参照系

- `GET /api/mcp/servers` — 全インスタンスのステータス一覧（instance_key 単位）
- `GET /api/mcp/tools` — 登録済みツール一覧
- `GET /api/mcp/failures` — 起動失敗中のインスタンス（バックオフ情報付き）

### 制御系

- `POST /api/mcp/servers/{server_name}/reconnect` — 指定した qualified_server_name の全インスタンスを再接続
- `POST /api/mcp/instances/stop?instance_key=<key>` — 指定インスタンスを手動停止（refcount 無視）
- `POST /api/mcp/instances/retry?instance_key=<key>` — バックオフ中のインスタンスを即座にリトライ可能にする

### instance_key のフォーマット

```
<qualified_server_name>:global
<qualified_server_name>:persona:<persona_id>
```

`qualified_server_name` はアドオン由来の場合 `<addon_name>__<server_name>`、それ以外は `<server_name>` そのまま。

## アドオン制作者向けガイド

アドオンで MCP サーバーを配布する基本形：

```
expansion_data/<your-addon>/
├── addon.json            # params_schema で AddonConfig スキーマを宣言
├── mcp_servers.json      # MCP サーバー定義（自動プレフィックスされる）
├── tools/                # 通常の SAIVerse ツール
└── playbooks/public/     # 推奨 playbook（あれば）
```

### AddonConfig とのつなぎ方

1. `addon.json` の `params_schema` に API キー等の入力欄を宣言します。ペルソナごとに値を変えたい場合は `persona_configurable: true` を付けます。

    ```json
    {
      "params_schema": [
        {
          "key": "api_key",
          "type": "password",
          "label": "API Key",
          "persona_configurable": true
        }
      ]
    }
    ```

2. `mcp_servers.json` で参照構文を使います。stdio なら `env`、remote なら `headers` に置きます：

    ```json
    {
      "mcpServers": {
        "example": {
          "url": "https://example.com/api/mcp",
          "transport": "streamable_http",
          "headers": {
            "Authorization": "Bearer ${persona.addon.your-addon-name.api_key}"
          },
          "scope": "per_persona"
        }
      }
    }
    ```

    `your-addon-name` はアドオンディレクトリ名（`expansion_data/` 直下）と一致させてください。

### サーバー名の衝突について

`mcp_servers.json` で宣言する `server_name` には他アドオンとの衝突を意識する必要はありません。SAIVerseが自動的に `<addon_name>__<server_name>` へ内部リネームするので、汎用名（`filesystem`、`database` 等）を使っても安全です。

ただし、LLM に提示されるツール名も `<addon_name>__<server_name>__<tool_name>` と長くなるため、`server_name` は用途を示す簡潔な名前にすると良いです（`elyth-social` より `elyth` の方が良い等）。

### 同時AITuber数などサービス側制限について

外部サービス側が「同時アカウント数」等の上限を設けている場合、SAIVerseはそれをチェックしません（各ペルソナが自分の API キーを持つ per_persona スコープなら、キー発行時点で上限が効きます）。**同じ API キーを複数ペルソナに割り当てる運用は技術的には可能ですが、サービス側が意図しない共有になり得るため、READMEで注意喚起してください。**

## 関連

- 設計意図と不変条件: `docs/intent/mcp_addon_integration.md`
- 既存実装: `tools/mcp_client.py`, `tools/mcp_config.py`
- AddonConfig 読み取り API: `saiverse/addon_config.py`
