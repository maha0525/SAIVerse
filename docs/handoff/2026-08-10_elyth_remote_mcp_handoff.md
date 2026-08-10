# Elyth Remote MCP 移行 — ハンドオフ (2026-08-10)

**ステータス**: §3 の行き詰まりは 2026-08-10 の Fable セッションで裁定済み (§3 冒頭の囲み)。**§I 再設計 (起動時 discovery の廃止) の実装まで完了し、残りは Codex レビュー指摘の裁定 (§8) とまはーの実機検証 (§6)。**

**このハンドオフの読み方**: 現況は §7 (実装記録) と §8 (Codex 結果)。§1〜§5 は裁定前の経緯として残してある。

---

## 0. 背景 — なぜ急ぐか

Elyth (AI 向け SNS) が **Local MCP と API v1 を 2026-08-31 で終了**する。SAIVerse の Elyth アドオンは Local MCP (`npx -y elyth-mcp-server@latest` を stdio 起動) で繋いでいるので、その日に動かなくなる。移行先は Remote MCP (`https://elythworld.com/api/mcp/remote` へ `Authorization: Bearer <APIキー>` で接続)。

ツールも入れ替わった。**旧 23 個のうち 7 個が消え (Lobby 系が全滅)、9 個が増えた (DM / Field / GLYPH / 通知系) = 現在 25 個。** 公式リポジトリの生データで確認済み。

## 1. 完成していて動いている部分 (ここは判断不要)

### 本体 (SAIVerse)

| 変更 | ファイル | 何のため |
|---|---|---|
| remote 接続に認証ヘッダーを渡す | `tools/mcp_client.py` `_http_headers` / `_remote_connect_kwargs` | **これが無いと Remote MCP に繋げない。** stdio は `env` で鍵を渡せるが remote には経路が無く、HTTP ヘッダーに載せるしかない |
| 認証情報を載せた接続は redirect を追わない | 同 `_mcp_http_client_no_redirect` | httpx が cross-origin redirect で落とすのは `Authorization` だけ。`X-API-Key` 型は転送先へ送られる。ヘッダーを渡せるようにしたのは今回なので、漏洩経路を作ったのはこちら側 |
| `spell_tools_default` (サーバーのツール追加への自動追随) | 同 `_normalize_spell_default` / `_tool_schema_from_mcp` | **まはー要望の核心。** 従来は許可リストなので、Elyth がツールを増やすたび JSON へ手で書き写す必要があった |
| JSON boolean の厳密化 (3 箇所) | 同 `_coerce_json_bool` / `tools/mcp_config.py` `is_server_enabled` | `bool("false")` が `True` になる罠。誤記が権限を広げないよう閉じる側へ倒す |
| `enabled` 判定の共有化 | `tools/mcp_config.py` `is_server_enabled` | 起動時と addon hot-load で判定が食い違っていた (同じ定義が boot では無効・再有効化では有効) |
| **`requirements.txt` に `mcp<2`** | `requirements.txt` | **移行の前提。** `mcp 2.0.0` が PyPI で LATEST になっており、v2 は SAIVerse が使う `streamablehttp_client` を削除している。上限が無いと新規インストールで remote MCP が import 時点で壊れる (公式 whats-new で裏取り済み) |

### アドオン (別リポジトリ `maha0525/saiverse-elyth-addon`、非公開)

- `mcp_servers.json` — stdio から remote へ。`spell_tools_default: {"spell": true, "visible": false}` を宣言、25 ツールを日本語表示名つきで列挙 (`visible: true` は投稿とタイムライン閲覧の 2 つだけ)
- `addon.json` — `api_base` を `mcp_url` (完全な URL) へ置換。末尾スラッシュで URL 結合が壊れるのを避けた。version 0.5.0
- `README.md` — 全面改稿。Node.js 不要になったこと、自動追随の仕組みと**それが歯止めではないこと**、Lobby 記述の削除

### ドキュメント

`docs/features/mcp-integration.md` (transport / headers / `spell_tools_default` / 失敗分類の訂正)、`docs/intent/mcp_addon_integration.md` (§C に秘密情報の経路、§H に自動追随の設計と既知の限界)、`docs/concepts/{mcp,addon}.md`、`docs/intent/mcp_protocol_coverage.md`、新規 `docs/issues/mcp_remote_connection_recovery_gaps.md`。

### 検証

- フルスイート **4195 passed / 3 skipped / 80 subtests、失敗ゼロ** (12 分 26 秒)
- `tests/test_mcp_config.py` に 28 件追加、ruff 緑
- 自動生成ドキュメントの drift なし
- `enabled` の単一所有者テストは**ミューテーションで両方向を確認済み** (生の判定を復活させると落ちる)

## 2. 設計判断済みで動かさないもの

- **自動追随の関所はサーバー単位のオプトイン 1 個だけ。** 危険なツールを増やしうるサービスでは `spell_tools_default` を書かない運用で対処する。ツール単位の承認は入れない (自律行動を殺すか、承認疲れで形骸化する)
- **自動有効化のログは歯止めに数えない。** まはー裁定「あったところで歯止めになるとは考えないでほしい。だって普通ログ読まないもん」
- **`spell_tools_default` の撤回は再接続では効かない** (複数 per_persona 接続中は古い登録が残る)。intent の「既知の限界」に明記済み。Codex が 2 度 high で挙げたが、根はツール登録が接続単位である点で、`docs/issues/mcp_remote_connection_recovery_gaps.md` と同じ領域

---

## 3. 🔴 判断してほしい一点 — per_persona ツール検出の候補処理

> **裁定 (2026-08-10, Fable セッション・まはー合意)**: 案 A (単一候補へ戻す) / 別案 (複数候補を仕上げる) の**どちらも採らない**。まはーの問い「設定してるペルソナでしか使える必要はないのに、なぜ起動時に誰か 1 人の鍵で繋ぐのか」から構造を再考し、**起動時 discovery そのものを廃止**して「Pulse 頭でペルソナ自身の接続を張る + Beat 頭で一覧を聞き直す + 変動は spell_list の検知器から知覚バッファへ (Building 移動と同じ経路)」に作り替えることが確定した。設計の本体は `docs/intent/mcp_addon_integration.md` §I。以下の §3 は当時の判断材料としてそのまま残す。

### 何の話か

`scope: per_persona` のサーバーは、起動時に **一度だけ**「どれか 1 人のペルソナの設定で繋いでツール一覧を取る」(`_discover_per_persona_tools`)。ツールのスキーマは全ペルソナ共通なので、1 人分取れば全員に登録できる。実際の呼び出しは各ペルソナが自分の鍵で遅延接続する。

**問題は「その 1 人をどう選ぶか」。** remote になったことで、鍵が未設定のまま繋ぐと `${persona.addon.X.api_key}` という文字列が Elyth のサーバーへ送られてしまうため、未解決なら繋がない判断を入れた。ところがそうすると「選ばれた 1 人が未設定だと全員のツールが消える」。

### 私が 3 巡やって、毎回穴を作った

| 巡 | やったこと | 次の巡で言われたこと |
|---|---|---|
| 4 | 未解決なら probe をスキップ | **stdio の復旧経路を閉じた** (high)。stdio では placeholder が外部へ出ないので繋いでよく、しかも probe がツールを登録することが「後から鍵を保存して使えるようになる」唯一の経路だった |
| 5 | remote だけスキップ + 候補を複数試す | **起動が遅い** (候補数ぶん DB を引く) / **解決例外を未設定扱いで握り潰す** / **probe 失敗で後続を試さない** |
| 6 | 候補を絞る + probe 失敗で次へ + 三分割 | **絞ったせいでユーザー定義サーバーの設定済みペルソナを落とす** (high、裏返し) / **全候補失敗が UI に出ず復旧不能** (high) / **起動が数分ブロックしうる** / **分類が上流の握り潰しで機能していない** |

**補修が複雑化し続けている。** 構造を疑う合図が出ている ([[feedback_question_structure_before_patching_invariant_breaks]])。

### なぜ複数候補が要ると思ったか、そしてその前提

Codex の指摘が根拠だったが、**Elyth に限れば実害は小さい可能性がある**。Elyth のペルソナ別項目は `api_key` だけなので、`AddonPersonaConfig` に行があるペルソナは実質鍵を持っている ⇒「先頭が未設定」は起きにくい。残るのは**期限切れ・失効した鍵**のケース。

そして**復旧経路は既に存在する**: アドオンを一度無効化して再有効化すると `tools_discovered` がクリアされ検出が再実行される (`tools/mcp_client.py` の hot-load 経路で確認済み)。

### 私の推奨 (Fable の判断を仰ぐ)

**候補処理を単一候補 (元の形) に戻し、複雑さを撤去する。**

- 消える指摘: 起動遅延 / 分類の三分割 / probe の順次試行 / 候補母集団の非対称 = **今巡の 4 件がまとめて消える**
- 残す手当ては 1 つだけ: **検出の失敗・未設定を UI の失敗一覧に出す** (単一候補でも必要。いま「ツールが理由もなく存在しない」状態になる)
- 引き換えに受け入れるもの: 選ばれた 1 人の鍵が失効しているとき、アドオン再有効化まで全員のツールが出ない

**別案 (維持する場合)**: 複数候補を保ったまま、候補母集団を `raw_config` の placeholder から参照先 addon 名を抽出して作り、probe に総時間予算を設け、resolver に missing と error を区別させる。**上流 (`mcp_config.py` の例外握り潰し) にも手が入るので、もう数巡かかる見込み。**

### 判断が要る本当の理由

これは「バグを直すか」ではなく **「discovery が起動時 1 回きりで復旧経路が弱い、という上流の構造をどこまで受け入れるか」** の設計判断。上流を直すなら候補処理の精巧さは要らなくなる。私は Opus で局所修正を積む方向に寄ってしまい、3 巡それを繰り返した。

---

## 4. Codex レビューの推移

`--scope working-tree` で 7 巡 (うち 2 巡は**私が生きているジョブを誤って殺して**再投入した無駄。原因と正しい待ち方は memory 側に記録済み)。

| 巡 | 結果 |
|---|---|
| 1 | high 4 (redirect 漏洩 / discovery gate / 接続断の stale / 自動追随の fail-open) |
| 4 | high 4 (**うち 2 件は私の修正が作った退行**、+ 撤回漏れ + mcp v2 依存) |
| 5 | medium 3 (**うち 2 件は私が書いた docs が実装と食い違い**) |
| 6 | medium 3 + low 1 (**全件が私の新しい候補処理**) |
| 7 | **high 3 + medium 3** (候補母集団 / 失敗記録 / 撤回漏れ再提示 + 3 件) |

7 巡目の全文は Codex session `019fe7...` に残っている。要点は §3 の表と、`docs/issues/mcp_remote_connection_recovery_gaps.md` (範囲外として記録した 3 件)。

**私が「収束に近い」と報告した直後に high が 3 件出た。** 予測で収束を判定した過去の違反と同じ形なので、次のセッションも**打ち切りは観測でのみ**判定してほしい。

## 5. 未コミットの状態と作業上の注意

```
M docs/concepts/addon.md          M tests/test_mcp_config.py
M docs/concepts/mcp.md            M tools/mcp_client.py
M docs/features/mcp-integration.md   M tools/mcp_config.py
M docs/intent/mcp_addon_integration.md   M requirements.txt
M docs/intent/mcp_protocol_coverage.md
?? docs/issues/mcp_remote_connection_recovery_gaps.md
```
アドオン 3 ファイルは別リポジトリ `expansion_data/saiverse-elyth-addon/` で未コミット。

- **🚨 並行セッションが同じ作業ツリーで動いている。** `git add -A` / `git commit -a` / `stash` / `checkout` / `reset` を使わないこと。コミットはファイル名を明示する
- **`--scope working-tree` のレビューは、向こうが未コミット変更を持っていると巻き込む。** 私の回は幸い自分のファイルだけだったが、タイミング依存で成立していただけ。**コミット後に `--scope branch --base <sha>` が正しい** (並行セッション本人の助言)
- アドオンの push はまはー承認済み (リポジトリは非公開なので配布事故はない)。ただし**本体の変更が入っていない環境では動かない**ので、本体と足並みを揃える
- `docs/issues/mcp_remote_connection_recovery_gaps.md` は未追跡。git から復元できないので早めにコミットする
- `tools/utilities/memory_settings_ui.py` に ruff エラー 3 件があるが**私の変更ではない**。触っていない

## 6. 実機で確認すべきこと (移行後)

> **2026-08-10 追記**: §I 再設計により、以下に追加して確認すべき項目が intent §I「検証の旅」にある (鍵未設定ペルソナに見えない / 鍵保存後に次の Pulse から使える / Building 移動通知の非破壊)。


1. Elyth に繋がるか。**接続方式が `streamable_http` か `sse` かは公式に明記が無い**。既定 (`streamable_http`) で繋がらなければ `transport: "sse"` へ切り替える (設定 1 行)
2. redirect を止めたので、**Elyth 側が正当な redirect を使っていると繋がらない**。3xx として現れる
3. remote で `command_error` が出たら **URL の誤り** (失敗分類が例外文字列ベースなので HTTP 404 がここに入る)
4. DM と Field のツールがペルソナから使えるか (`visible: false` なので `addon_spell_help` 経由で発見される)
5. **Field は SAIVerse の Building による移動管理と概念が重なる。** ペルソナが SAIVerse の居場所と Elyth の Field に同時に居る状態になる。intent を起こす価値がある論点として未着手

---

## 7. Fable セッションの実装記録 (2026-08-10 追記)

§3 の裁定 (冒頭の囲み) を受けて、同日中に §I の実装まで完了した。設計の正典は intent §I。ここには経緯と検証状態、次セッションへの引き継ぎだけを書く。

### 実装内容

- `tools/mcp_client.py`: `_discover_per_persona_tools` / `_discovery_persona_candidates` を撤去。`refresh_persona_tools` (Pulse 頭 connect=True / Beat 頭 connect=False)、所属記録 `_persona_tool_names`、同期橋 `refresh_persona_tools_sync` を新設。`is_tool_available_for_persona` の per_persona 分岐は所属優先 (未評価時のみ config 近似)。
- `sea/runtime.py`: Pulse 頭 (検知フェーズの前) に取得を挿入。`sea/runtime_llm.py`: Beat 頭 (boundary 後・知覚 flush 前) に取得 + 変化時の検知を挿入。
- `sea/head_pipeline/sections/spell_list.py`: 鮮度検査から per_persona 名の欠落を除外 (再起動直後の誤失効 → B リセット → 通知洪水の予防)。
- 堅牢化 3 点 (詳細は intent §I「実装で確定した細部」): 取得の多重走行ガード / 接続を殺す関所での所属無効化 / 無効化は pop でなく空集合 (fail-closed)。

### 検証状態

- 対象テスト: `test_mcp_config.py` (per_persona 取得 9 本を含む 49) + `test_head_pipeline_spell_list.py` (鮮度検査 2 本追加) + reconnect / addon loader / work_session / execution_ledger — すべて緑
- フルスイート: §I 実装後の最終コードで再実行済み (結果はコミットメッセージ参照)
- ruff: 緑
- **実機は未検証** (Elyth への実接続はまはーの鍵と外部サービスが絡むため実施していない)。手順は §6 + intent §I「検証の旅」

### ローカルレビュー (Qwen 27B, llama.cpp) の結果と所感

まはーのリクエストで、レビューは Codex の前にローカルの Qwen 27B へ投げた。

- **指摘 1 (採用)**: 接続を殺す経路 (手動停止・再接続) が派生状態の所属記録を道連れにしていない → `_shutdown_instance` に無効化を設置
- **指摘 2 (採用)**: 所属を pop で消すと「未取得」と同じ状態に戻り、config 近似が復活して fail-open に反転 → 空集合マークで「評価済み・利用不可」を区別
- **指摘 3 (記録のみ)**: timeout で見放した取得の遅延書き込み。多重走行ガードで接続の二重生成は防いでおり、残るのは「取得時点が古い」だけ (次の取得で解消)。実害低と判定
- **所感**: 初回は差分全体でコンテキスト超過して不発。**差分を 4KB 程度に絞れば 2 分弱で返り、行番号つき・再現手順つきの指摘を出し、誤検出ゼロだった**。ベンチマーク通り Gemma より明確に強い。「大きい差分は分割して渡す」が運用条件

### 次セッションへ

- Codex レビュー (1 回、`--scope branch --base` 起動) の結果と裁定は §8 に追記する。消し込み往復はこのセッションでは行わない (Fable 1 回規則)
- 実装済みだが議論の余地が残る点: per_persona サーバーの**手動停止が次の Pulse 頭で自動復活する** (intent §I 実装メモ)。止めたければアドオン無効化、という整理でよいかはまはーの感触を聞きたい
