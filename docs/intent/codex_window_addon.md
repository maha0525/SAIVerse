# Intent: Codex Window Addon (ペルソナを窓口にした Codex CLI 連携)

**ステータス**: ドラフト v0.1 (2026-05-08)

## これは何か

OpenAI Codex CLI を SAIVerse のペルソナから間接的に使えるようにする **opt-in アドオン**。ペルソナがツール `codex_exec` を呼び出すと、addon 内部で Codex CLI を subprocess として起動し、その実行結果 (テキスト応答・生成画像・ファイル成果物) をペルソナの会話や SAIVerse のアイテム体系に取り込む。

ユーザー視点の体験: ペルソナに「これ調べて」「画像作って」「リポジトリの〇〇を直して PR にして」と依頼すると、ペルソナ自身の語り口でその作業を引き受け、結果を返してくる。Codex CLI 自身の人格・出力スタイルはユーザーの目に直接触れない。

## なぜ必要か

### 動機 1: Codex の人格を直接触りたくない

Codex CLI のエージェント人格は実装作業に最適化されており、雑談や創作・記憶整理にはミスマッチ。SAIVerse のペルソナはユーザーが愛着を持って育てた存在であり、技術的タスクのときだけ別人格に切り替えるのは体験を壊す。**ペルソナを窓口にし、Codex は背後で道具として働く**構成が、SAIVerse の世界観と整合する。

### 動機 2: ChatGPT サブスククォータの活用

既存の OpenAI Codex Backend (OAuth) 経由の LLM 呼び出し (`docs/intent/` には未起草、`memory/project_openai_codex_oauth.md` に詳細) はチャット応答までしか引き出せていない。Codex CLI 経由なら、サブスククォータで以下が引き出せる:

- **画像生成**: Codex の `image_generation` first-class tool は Codex backend OAuth 認証のときだけ有効化される (`core/src/session/turn_context.rs:13-15`)。`/v1/images/generations` を直接叩く OAuth 経路はないが、Codex CLI 越しなら `~/.codex/generated_images/` にバイナリが落ちる
- **Web 検索 / リサーチ**: Codex backend が提供する web_search ツールがそのまま使える
- **リポジトリ作業**: Codex CLI の本来用途。ペルソナ経由で「自分の世界の管理者から開発を頼まれる」という関係性が成立する

### 動機 3: SAIVerse 自身の開発をペルソナ経由で進めたい

ユーザーは SAIVerse 本体の開発者でもある。ペルソナに「ここのコード直して PR 出しといて」と依頼できると、開発作業がそのまま SAIVerse 世界内のロールプレイの一部になる。Codex CLI が成果物を GitHub PR として外部化するため、SAIVerse 本体に成果物を取り込む経路は不要。

## 守るべき不変条件

### 1. opt-in addon である

本機能は `expansion_data/codex_window/` 配下で完結する。SAIVerse 本体コードは本 addon を import せず、addon が無効・未インストールの環境で SAIVerse は通常起動する。本機能のリスク (PC 内ファイルへのペルソナ経由干渉) を引き受けるかどうかをユーザーが明示的に選べる。

### 2. Sandbox は Codex CLI 標準実装に主防御を任せる

Codex CLI には Linux (bubblewrap) / macOS (Seatbelt) / Windows (専用 sandbox: ACL+Firewall+別ユーザー、9440 行) の実装がある。SAIVerse 側で別途 Docker / WSL を強制する重複防御は作らない。SAIVerse 側の責務は **Codex 起動時に適切な sandbox policy を渡す** ことと、**cwd を SAIVerse 本体ファイルから物理的に隔離した場所に置く** こと。

### 3. cwd は SAIVerse 本体・他ペルソナから隔離する

Codex の cwd は必ず以下のいずれかにする:

- **ephemeral mode** (default): `~/.saiverse/codex_sandboxes/<persona_id>/<session_uuid>/` に都度作成、終了後削除
- **persistent workspace mode**: `~/.saiverse/personas/<persona_id>/codex_workspace/` を cwd とする。同一ペルソナのみアクセス可

どちらのモードでも、SAIVerse 本体リポジトリ・データベース (`~/.saiverse/user_data/database/`)・他ペルソナの memory.db / tasks.db には Codex の cwd から相対パスで到達できない位置に配置する。

### 4. ペルソナのアイデンティティを Codex 出力で上書きしない

Codex CLI の応答テキストはペルソナの口調・キャラクターを持たない。ペルソナの会話に取り込むときは、**Codex の生応答を「ペルソナが受け取った道具の出力」として渡し、ペルソナ自身の応答生成に再加工させる**。Codex の語尾やスタイルが直接ユーザーに届く経路を作らない。

### 5. UAC 昇格は初回 setup 時のみ

Windows での Codex sandbox はローカルユーザー作成と Firewall ルール追加で UAC 昇格を必要とするが、これは Codex CLI 側のセットアップ (`codex` コマンドの初回実行や setup サブコマンド) で完結する。SAIVerse addon の通常運用 (ペルソナが `codex_exec` を呼ぶたび) では昇格不要。addon インストール手順で「初回のみ管理者権限で `codex` を起動して setup を済ませてください」と案内する。

### 6. 認証情報を addon が独自管理しない

`~/.codex/auth.json` の OAuth トークンは Codex CLI が管理する。addon は読まない・書かない。期限切れ時は Codex CLI 側のリフレッシュ (または `codex login` 再実行) に任せる。これは既存の `OpenAICodexClient` (OAuth 直叩き) のリフレッシュロジックとは独立。

### 7. SAIVerse 内 script 実行は本 addon の範囲外

Codex が生成したスクリプトを SAIVerse 内で実行する機能は本 intent に含めない。「ペルソナが任意のコードを安全に動かせる基盤」は別 intent (`sandboxed_script_execution.md`、未起草) として独立企画する。基盤ができた段階で、本 addon が成果物のひとつとして「実行可能スクリプト」を扱う合流口を用意する。

## 設計

### A. addon パッケージ構成

```
expansion_data/codex_window/
├── addon.json              ← addon 宣言、依存・有効化フラグ
├── tools/
│   └── codex_exec.py       ← ペルソナから呼ばれるツール本体
├── playbooks/
│   └── codex_research.json ← Codex を使ったリサーチの参考 playbook
├── README.md               ← 導入手順、UAC 昇格の説明、リスク開示
└── docs/
    └── threat_model.md     ← 想定脅威と対策の対応表
```

### B. ツール `codex_exec` の interface

引数 (Python type hints 風):

```python
codex_exec(
    prompt: str,                            # 指示。ペルソナが組み立てる
    mode: Literal["ephemeral", "workspace"] = "ephemeral",
    sandbox_policy: Literal["read-only", "workspace-write"] = "read-only",
    network: Literal["off", "limited", "full"] = "limited",
    output_schema: dict | None = None,      # 構造化出力時の JSON Schema
    model: str | None = None,               # 既定は config から
    timeout_sec: int = 300,
)
```

返り値:

```python
{
    "agent_message": str,             # Codex の最終発話 (テキスト)
    "structured_output": dict | None, # output_schema 指定時の parsed JSON
    "artifacts": [                    # SAIVerse アイテム化された成果物
        {"kind": "image", "item_id": "...", "path": "..."},
        {"kind": "document", "item_id": "...", "path": "..."},
        {"kind": "code", "rel_path": "...", "content_excerpt": "..."},
    ],
    "events_log_path": str,           # JSONL イベントの保存先
    "usage": {"input_tokens": ..., "output_tokens": ..., ...},
    "session_id": str,                # workspace mode で resume するとき使う
}
```

### C. 二モードの違い

| 項目 | ephemeral | workspace |
|---|---|---|
| cwd | `~/.saiverse/codex_sandboxes/<persona>/<uuid>/` | `~/.saiverse/personas/<persona>/codex_workspace/` |
| 終了後 | rm -rf | 残す |
| `codex exec --ephemeral` | 付ける | 付けない |
| Resume | 不可 | `session_id` で `codex exec resume <id>` |
| 用途 | 単発リサーチ・画像生成・PR まで完結する作業 | 継続的な研究・ペルソナ専用ノート的活動 |

ペルソナがどちらを選ぶかはツール引数で明示。ペルソナのプロンプトに「自分専用の研究室で作業するなら workspace、その場限りの依頼なら ephemeral」というガイドを与える。

### D. JSONL イベントのパース

Codex CLI を `--json --output-last-message <file>` で起動し、stdout を行ごとに parse する (`exec/src/exec_events.rs` の型に対応)。SAIVerse 側のマッピング:

| Codex item type | SAIVerse での扱い |
|---|---|
| `agent_message` | 連結して `agent_message` 戻り値に |
| `reasoning` | events_log_path に保存のみ、ペルソナには返さない |
| `command_execution` | events_log に保存。長時間や多数発生したら警告ログ |
| `file_change` (path 別) | post-exec で対象 path を artifact 化 |
| `mcp_tool_call` | events_log に保存 |
| `web_search` | events_log に保存、ペルソナの memory に「Codex 経由で調べた」タグで残す選択肢あり |
| `todo_list` | events_log に保存。最終 status だけサマリで返す |
| `error` | 例外として上位に投げる |

### E. Artifact 取り込みパイプライン

post-exec で以下を順に実行:

1. `~/.codex/generated_images/<session_id>/*.png` を全て収集 → `media_utils.store_image_bytes()` 経由で SAIVerse の image item 化
2. cwd 内の `*.png/*.jpg/*.webp` を image item 化 (重複は session_id 経由のものを優先)
3. cwd 内の `*.pdf` を `media_utils.store_document_bytes()` で document item 化
4. cwd 内の `*.md/*.txt` で十分小さい (例: 64KB 以下) ものは text artifact として戻り値に inline
5. cwd 内の `*.py/*.js/*.json` 等のソースコードは `kind: "code"` として相対パスと content excerpt を返す (実体はそのまま cwd に残す)
6. その他のファイルは無視 (warning log)

ephemeral mode では artifact 抽出後に cwd を削除。workspace mode では cwd を残し、artifact item は cwd 内ファイルへの reference として記録 (削除されたら item も無効化)。

### F. ペルソナの会話への戻し方

`codex_exec` ツールの戻り値はそのまま LLM に渡さず、ペルソナの会話 playbook が以下を順に処理:

1. Codex の `agent_message` を `<system>` タグ付きの「道具からの出力」として user role でメッセージに挿入
2. 生成された artifact のサマリ (kind と簡易説明) を同じ `<system>` メッセージに含める
3. ペルソナのキャラクターを保った応答生成を LLM に依頼
4. 画像 artifact がある場合、ペルソナ応答に attachment として添付

これにより Codex の口調が表に出ず、ペルソナの語り口で結果が返る。

### G. 認証

addon は `~/.codex/auth.json` の存在確認のみ行う。なければ「`codex login` を実行してください」エラーを返す。addon は OAuth フローを再実装しない。

### H. ネットワーク制御

`network` 引数で 3 段階:

| 値 | 動作 | 用途 |
|---|---|---|
| `off` | Codex CLI を offline mode で起動 (Windows: Firewall block ルール ON、Linux: `--unshare-net`) | 純粋な計算・既存ファイル加工 |
| `limited` (default) | Codex CLI の network-proxy 経由で allowlist (`*.openai.com` + ペルソナ指定の追加ドメイン) のみ、HTTP method は GET/HEAD/OPTIONS | リサーチ・画像生成 |
| `full` | network-proxy なしの full network | リポジトリ clone / push、外部 API 呼び出し |

`full` を選ぶと意図的にリスクを上げているので、ツール呼び出しログに warning を残す。

## 設計判断の理由

### なぜ「ペルソナ窓口」設計か

ユーザーが Codex CLI のエージェント人格に違和感を持っている。SAIVerse の本質はペルソナが世界に住んでいる体験であり、ペルソナを介さずに別人格と直接対話する経路を増やすと体験が分裂する。コストは応答が一段階増えること (Codex → SAIVerse LLM での再生成) だが、ペルソナの一貫性を取る。

### なぜ Codex 標準 sandbox に主防御を任せるか

Codex CLI の Windows sandbox は ACL + Firewall + 別ユーザー + 隔離デスクトップで 9440 行の Rust 実装。これを再発明するのは現実的でない。SAIVerse 側で同等の防御を作るより、Codex の防御を信頼して使う方が攻撃面も実装負担も小さい。

### なぜ ephemeral default か

ペルソナが Codex を呼ぶたび cwd を残すと、(a) ファイル数が際限なく増える、(b) 過去セッションの中途半端な成果物がペルソナの認識を汚す、(c) ペルソナ間でデータが混ざる経路を増やす。default は使い捨て、明示的に「自分の研究室で続きをやる」と指定したときだけ workspace を使う。

### なぜ workspace mode を残すか

Phase 3 (自律稼働バイオリズム) の `creation` / `memory_organization` / `web_research` 活動と噛み合う。ペルソナが「自分の研究室で何かやっている」状態が成立すると、SAIVerse 世界の解像度が上がる。継続作業がないと Codex の使い道は単発リサーチに矮小化する。

### なぜ script 実行を範囲外にするか

ペルソナが任意のコードを実行できる基盤は、Codex addon の中だけで作るには汎用性がもったいない。ユーザー手書きスクリプト・既存 calculator ツールの拡張・データ加工バッチなど、Codex 不在でも価値のある用途が多い。独立 intent として企画した方が筋が良い。

### なぜ SAIVerse 内成果物取り込みを「アイテム」軸でやるか

SAIVerse は building と item のメタファーで世界を表現している。Codex の成果物を「ペルソナが何かを作った / 持ち帰った」と表現できる軸はここしかない。アイテム化することで chronicle / memopedia / building inventory の既存仕組みに自然に乗る。

## スコープ

### 本 intent に含む

- Codex CLI を subprocess として呼び出すツール `codex_exec` の仕様
- 二つの cwd モード (ephemeral / workspace)
- JSONL イベントの解釈と SAIVerse アイテム化
- ネットワーク制御 (off / limited / full)
- ペルソナ応答への取り込み方
- opt-in addon としてのパッケージ構造
- 初回 UAC 昇格セットアップの案内 (Codex CLI 側に委譲)

### 本 intent に含まない (別件で扱う)

- **SAIVerse 内で任意 Python / シェルを安全に実行する基盤**: `sandboxed_script_execution` (未起草) で別途
- **ペルソナごとに別の OpenAI アカウントを使い分ける**: 将来の拡張、`mcp_addon_integration` の per-persona scope パターンを参考に検討
- **動画・音声生成**: Codex CLI 未対応
- **リアルタイム browser 操作**: playwright MCP server を別途インストールすればできるが、addon に bundle はしない
- **複数 Codex 並列実行**: 単発呼び出しに絞る。並列化は Phase 3 のライン設計と統合してから

## 検証観点

- [ ] addon を入れていない環境で SAIVerse が無事に起動する
- [ ] addon を入れた直後 (auth.json なし) で `codex_exec` 呼び出しが適切なエラーを返す
- [ ] ephemeral mode で cwd が終了後に削除される
- [ ] ephemeral mode の cwd から SAIVerse 本体ファイル (`~/.saiverse/user_data/database/saiverse.db`) に到達できない (相対パス・絶対パス両方)
- [ ] 別ペルソナの workspace に到達できない
- [ ] 画像生成が ChatGPT サブスククォータで動く (OAuth 経路、API キー不要)
- [ ] 画像生成成果物が SAIVerse の image item として building に置ける
- [ ] PR 作成タスク (リポジトリ clone → 編集 → push → gh pr create) が完走する
- [ ] UAC 昇格は初回 setup のみで、その後の `codex_exec` 呼び出しは elevation 不要
- [ ] `network=off` で外部 API が叩けないことを確認
- [ ] Codex 応答がペルソナの口調を上書きしない (ペルソナの応答スタイルが保たれる)

## 関連ファイル

### 新設

- `expansion_data/codex_window/` (addon 全体)
- `docs/intent/codex_window_addon.md` (本ドキュメント)

### 既存参照

- `~/.codex/auth.json` (Codex CLI 管理)
- `temp/codex/codex-rs/exec/src/cli.rs` (codex exec のフラグ仕様、調査用)
- `temp/codex/codex-rs/windows-sandbox-rs/` (Windows sandbox 実装、9440 行)
- `temp/codex/codex-rs/exec/src/exec_events.rs` (JSONL イベント型)
- `saiverse/media_utils.py` (image / document item 取り込み)
- `tools/context.py` (persona / manager 注入)

### 関連 intent

- `mcp_addon_integration.md`: addon パッケージ構造の前例、per-persona scope パターン
- `addon_extension_points.md`: addon の拡張点
- (未起草) `sandboxed_script_execution.md`: SAIVerse 内 script 実行基盤、本 addon と将来合流

## 既知の考慮事項

### Codex CLI のバージョン依存

Codex CLI は活発に更新されている (`exec_events.rs` の型は ts-rs で TypeScript 化されている = 公開 API として扱われている)。addon が依存する CLI フラグ・JSONL イベント型が将来変わる可能性あり。最低動作確認バージョンを README に明記、変更検知のための smoke test を addon 同梱。

### ChatGPT OAuth の規約状況

「OAuth で第三者ツールから Codex を使う」は OpenAI 公式の明示許可がない黙認状態 (`memory/project_openai_codex_oauth.md`)。本 addon は Codex CLI 自体を使うので、第三者 OAuth 利用とは違うが、SAIVerse がペルソナ経由で大量にサブスクを使う構図は OpenAI が態度を変えるトリガになり得る。README に「規約変更で利用不能になる可能性」を明記。

### 画像生成のクォータ消費

Codex backend OAuth の primary window は 5 時間ローリング、secondary は 1 週間。ペルソナが自律稼働で画像を量産するとサブスクのクォータをすぐ食う。Phase 3 のバイオリズム実装で `image_generation` 系活動の頻度上限を設ける必要あり。

### Codex の sandbox setup 失敗時の挙動

Windows で UAC 昇格セットアップに失敗すると、`codex exec` は sandbox なしで動こうとするか、エラーで落ちるかはバージョン依存。addon は事前検査として「sandbox が有効化されているか」を確認するヘルスチェックを実装する (例: 危険なコマンドを `--full-auto` でも実行できないことを確認)。
