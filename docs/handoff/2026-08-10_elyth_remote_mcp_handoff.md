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

---

## 8. Codex レビュー結果と裁定 (2026-08-10、Fable セッション記入)

`--scope branch --base b94e527` で 1 回実行 (Fable 1 回規則)。判定は needs-attention、**high 5 件**。全件を私が head で裏取りした上での裁定を書く。**このセッションでは修正しない** — 消し込みは次セッションの担当。

### 消し込みの優先順 (裏取り済みの裁定つき)

1. **[high・筆頭] `reconnect_server` が未解決 placeholder 検査を迂回する** (`tools/mcp_client.py` reconnect 本体) — **妥当、実コードで確認済み**。再解決した config を検査なしで `connection.connect()` に渡すため、接続確立後に鍵を削除して再接続が走ると、remote ヘッダーに `${persona...}` の literal が載って外部へ出る。解決失敗時に cached config で繋ぐ分岐も同族。discovery と `_start_instance` は塞いだのに第三の出口が残った — まさに「入口の検査は境界の保証ではない」の族で、**検査は値が外へ出る場所 (connect 直前) の単一関所へ移すのが正しい修正**。
2. **[high] `run_work_session` が Pulse 頭の取得を迂回する** (`sea/work_session.py`) — **妥当、実コードで確認済み**。私の「全 Pulse は run_meta_user を通る」前提が作業セッションで崩れていた。知覚 flush はあるが取得が無く、スペル無しの作業セッションは本人取得ゼロで head を組む。修正方向: Pulse root 共有の「頭処理」ヘルパーに取得+検知+flush を束ね、両 root から呼ぶ。
3. **[high] 同期橋の timeout が未評価 (None) を fail-open で残す** — **妥当**。初回取得が timeout すると所属は None のままで config 近似に戻る。修正方向: connect=True の取得開始時に None なら先に空集合を置く (timeout = その Pulse は不可、§I の文言と整合) + 遅延完了が新しい結果を上書きしないための世代番号。
4. **[high] 所属が実行時認可に接続されていない** — **観察は正確、ただし半分は設計**。実行の認可は「ペルソナ自身の鍵で張った接続にサーバーが応じるか」が真実で、所属は提示のフィルタ (§I の原理どおり)。空集合のペルソナがツールを呼んでも、自分の接続で試みて正直に失敗するだけで、他人の鍵に乗る経路は無い。**ただしこの整理は intent に明文が無い**ので §I へ追記が要る。None (未評価) の実行保留は 3 と同時に解消するのが自然。
5. **[high] `call_tool` 失敗後も死んだ接続と所属が残る** — **既知 issue (`mcp_remote_connection_recovery_gaps.md` の 1) の族**。新規退行ではないが、「接続断時に所属も空集合化する (無効化コールバックの一元化)」という指摘の形は §I の派生状態原則と合致していて、issue の修正方向に取り込む価値がある。
6. **[low・doc] `tools/mcp_client.py` 冒頭のモジュール docstring が旧 Phase 2c (起動時 discovery) の説明のまま** — 消し込み時に更新。

### 読み方の注意

- 1 と 5 は §I 以前からの構造 (再接続・切断ライフサイクル) に §I の観点が当たって顕在化したもの。2 と 3 は今回の実装の取り残し。4 は設計の明文化不足。
- Codex の総評「出荷不可」は上記 5 件を全部 high と数えた場合の評価。私の見立てでは、実害の即時性が高いのは 1 (鍵削除→再接続で外部送信) と 2 (作業セッションの提示が近似のまま) の 2 件。ただし**打ち切り判定は観測でのみ** — 消し込み後の再レビューで確認すること。

---

## 9. 消し込み結果 (2026-08-10、Opus セッション)

§8 の 6 件すべてに手を入れた。設計上の答えは intent §I の「レビューの消し込みで確定した点」に移し、ここには対応の対照表だけ置く。

| §8 | 対応 |
|---|---|
| 1. reconnect の placeholder 迂回 | 検査を `MCPServerConnection.connect()` の 1 箇所へ移設 (`MCPUnresolvedConfigError` → `missing_config` 分類)。入口側の重複検査は撤去。reconnect は「未解決なら現行接続も畳む / 再解決が落ちたら触らない」の 2 分岐に |
| 2. work_session が Pulse 頭を迂回 | 頭の一手を `sea/mcp_tool_refresh.refresh_mcp_tools_at_head` に切り出し、`run_meta_user` (notify=False) / `run_work_session` (connect=True) / Beat 境界 (connect=False) の 3 箇所から呼ぶ形に |
| 3. 同期橋 timeout の fail-open | 橋を渡る前に未評価の所属を空集合へ倒す (`presume_persona_tools_unavailable`) + 所属に版番号を持たせ、無効化を跨いだ遅延書き込みを捨てる |
| 4. 所属と実行時認可 | §I 機構 5 として明文化 (所属は提示のフィルタ、実行の認可は本人の接続にサーバーが応じるか)。3 の fail-closed で「未評価のまま提示」も消えた |
| 5. 死んだ接続と所属 | Beat 頭で「接続はあるが生きていない」を結論として所属を空集合化。接続ライフサイクル本体は既存 issue に修正方向を追記して残す |
| 6. 旧 docstring | `tools/mcp_client.py` 冒頭を §I の内容に更新 |

**新規テスト 15 本** (`test_mcp_config.py` +7 / `test_mcp_tool_refresh.py` 新設 8 本) と `test_work_session.py` の順序テスト 1 本。関連スイート緑・ruff 緑・フルスイートはコミット前に 1 回。

**残した判断**: 手動停止の自動復活は低優先度 issue に切り出した (`docs/issues/mcp_per_persona_manual_stop_revives.md`、まはー裁定: 停止をほぼ使わないので実害が浮上しない)。

**次**: 再レビュー (Codex 1 巡) → まはーの実機検証 (§6 + intent §I「検証の旅」— 鍵削除後に外部へ何も出ないことと、作業セッション経由での提示が新規項目)。

### 2 巡目 (再レビュー) の結果と対応

判定 needs-attention、**high 1 / medium 3**。1 巡目の 5 件は再指摘なし (関所・取得点・fail-closed・所属の位置づけは通った)。出たのは接続ライフサイクル側で、**high と medium 1 件は同じ根** — 「接続を落とす経路が、復旧に必要な情報まで一緒に捨てる / 捨てるべき死んだ接続を残す」。

| 指摘 | 裁定と対応 |
|---|---|
| [high] 再接続失敗後も死んだ接続・旧所属が残り、失敗記録も backoff も付かない | **妥当**。失敗時に `_record_failure` + `_shutdown_instance` で畳む形に。既存 issue (1) の一部を解消 (残りは構造の統合 = issue 側に明記) |
| [medium] `_shutdown_instance` が名前付き instance の `${instance.*}` context まで捨てて復旧不能にする | **妥当**。context を忘れるのは `stop_instance` (恒久撤去) の責務へ寄せた。`_shutdown_instance` は「接続が無くなったら常に真になること」だけを負う |
| [medium] 版の検査が `_register_tools` の後 (無効化後も wrapper が登録簿に残る) | **妥当**。検査を登録より前へ移動 |
| [medium] 手動停止が次の Pulse 頭で自動復活し、API の説明と食い違う | **機構はまはー裁定で低優先度 issue のまま**。ただし **docstring と API の説明が「次の tool call まで停止」のままだったのは私の書き残した嘘**なので、同じコミットで実装に合わせた |

ついでに `_fire_server_ready` を「全 instance 成功」から「1 つでも生きている」へ (複数 instance の 1 つの失敗で生きている購読者を待たせない)。

**指摘を直した後、自分で裏返しを歩いて 1 件見つけた** (Codex は指摘していない): 失敗した instance を畳むようにしたら、**畳んだ instance が再接続ボタンから見えなくなる** (対象を `_connections` から採っていたため) — 自己修復の頭を持たない global / 名前付き instance がプロセス再起動まで戻れない退行だった。対象に「参照が残っている / 失敗記録がある」ものを含め、接続オブジェクトが無いものは `_start_instance` で立て直す形にして閉じた。畳みの範囲も `recoverable` 引数で「一時的な失敗 = 参照を残す / 恒久的な撤去 = 落とす」に分けた。回帰テスト計 5 本追加。

**この巡の教訓**: 1 巡目の修正 (fail-closed 化) が 2 巡目の指摘を作り、2 巡目の修正 (畳む) が 3 つ目の裏返しを作った。**手近な既存の片付け関数を借りるときは「それが何を忘れるか」を読む** — memory の `feedback_safety_devices_fail_in_both_directions` に追記した。

### 3 巡目の結果と対応

判定 needs-attention、**high 3 件**。全部「同じ instance を起動中 / 停止中に別の経路が触る」競合で、**指摘の的が私の書いた契約から、接続ライフサイクルの競合へ移った**。

| 指摘 | 裁定と対応 |
|---|---|
| [high] 起動中の instance が無効化・全停止から漏れ、停止後に復活する | **妥当**。`_starting` / `_stop_requested` を新設し、掃除 (`shutdown_all` / アドオン無効化) が起動中も列挙する形に。起動は着地した瞬間に自分で畳む |
| [high] 失敗記録だけを根拠に、所有者のない instance を復活させる | **妥当、今巡私が作った分**。復活の条件を「参照があるか (= まだ望まれているか)」に絞った |
| [high] 旧接続を辞書から先に外すため、停止中に同一キーを二重起動できる | **妥当 (§I 以前からの構造だが、Pulse ごとの起動が窓を広げた)**。`_stopping` 中の起動を `MCPInstanceBusyError` で断る形に |

**自分で見つけた地雷 1 件**: 停止要求の旗を接続失敗時に降ろさないと、次に成功した起動が着地した瞬間に自壊する。旗を finally で必ず降ろす形にしてテストを付けた。

**作らなかったもの** (issue へ移送): 起動タスクの明示的キャンセル / desired state の tombstone・世代管理 / `reconnect_server` の生存接続経路の統合。今回入れたのは「遷移中を見えるようにして重なりを断る」だけで、状態機械としては未完成。

**この巡の教訓 (2)**: 3 巡通して、指摘は「私の書いた契約」→「片付けの範囲」→「遷移中の競合」と外周へ移動した。**新しい機構が既存機構の実行頻度を変えたら、既存の競合の露出も変わる** — §I は「Pulse ごとに接続を張る」に変えたので、それまで滅多に踏まれなかった起動時の競合が日常になった。frequency の変化を設計の影響範囲として数えていなかった。

---

## 10. 実機検証の手順 (まはー向け、操作の順番で)

設計上「何が真であるべきか」は intent §I の「検証の旅」にある。ここはそれを**その日やる操作の順番**に並べたもの。

> ⚠️ **3 以降はペルソナが実際に喋る (認知が走り、課金が発生し、記憶に残る)**。1〜2 は繋ぐだけなので費用も履歴もほぼ発生しない。

**見る場所**: `~/.saiverse/user_data/logs/<起動時刻>/backend.log` を `mcp` で絞る。アドオン管理画面の失敗一覧 (`/api/mcp/failures`) も同じ内容を出す。

1. **鍵を入れて起動**  (Elyth アドオンのペルソナ設定に API キー)
   - 期待: 起動直後は **何も繋がらない** (per_persona は起動時接続なし)。`/api/mcp/servers` に「まだ instance が無い」行が出る。
2. **鍵未設定のペルソナで一覧を見る**
   - 期待: そのペルソナのスペル一覧に Elyth のツールが**出ない**。失敗一覧にも出ない (未設定は故障ではないので)。
3. **鍵を入れたペルソナに話しかける** (会話 Pulse 1 回)
   - 期待: Pulse の頭で接続が張られ、backend.log に接続とツール取得が出る。スペル一覧に Elyth のツールが出る。実際に呼べる。
4. **鍵を保存した直後のペルソナで、再起動せずにもう一度話しかける**
   - 期待: 次の Pulse から使える (再起動もアドオンの入れ直しも不要)。
5. **鍵を消して、そのペルソナにもう一度話しかける**
   - 消し方: 鍵の欄の右にあるゴミ箱ボタンを押す。**欄を空にして保存しても消えない** — 画面に見えている `********` は本物の値ではないので、サーバーが「触っていない」と判断して元の鍵を書き戻す (2026-08-25 に削除ボタンを追加。それ以前はこの操作をする手段が無かった)。
   - 期待: ツールが消える。**backend.log に `missing_config` が出て、Elyth へリクエストが飛んでいない** (未解決の `${...}` が外へ出ないことの確認。ここが今回の一番の修正点)。
6. **鍵を消したまま、アドオン管理画面から再接続を押す**
   - 期待: 繋がらず、失敗一覧に「必須の設定値が未設定です」が出る。旧鍵で繋ぎ直さない。
7. **鍵を入れ直して、そのペルソナにもう一度話しかける**
   - 期待: 立て直せる。
   - ⚠️ **per_persona では「再接続」ボタンは出番がない**。接続はペルソナが動き出すときに張られるので、まだ接続が無い状態で押しても繋ぎ直す相手がいない (「繋ぎ直す接続がありません」と画面に出る)。畳んだ instance が再接続の対象から消えていないことを確かめたいなら、**5 の直後** (鍵を消して Pulse を回し、接続が畳まれて参照だけ残っている状態) に押す。プロセスを再起動するとその参照も消えるので、再起動を挟んだら 7 は「話しかける」で確かめる。
8. **コマ発火の作業セッションが回る時間まで待つ** (または作業セッションを起こす)
   - 期待: 会話 Pulse を通さない作業セッションでも Elyth のツールが見えている。
9. **Building 間を移動させる**
   - 期待: 従来の「スペルが増えた/減った」通知が壊れていない。
10. **アドオン管理画面から Elyth を無効化する**
    - 期待: 起動中だった接続も一緒に畳まれる。無効化のあと backend.log に Elyth への通信が出ない (起動中の instance が掃除から漏れて、停止後に復活しないことの確認)。
11. **無効化または再接続を押した直後に、同じペルソナへ続けて話しかける**
    - 期待: 畳んでいる最中に重ねて起動しようとした Pulse は断られ (`MCPInstanceBusyError`)、少し待てば普通に繋がる。同じ接続が二本立たない。

> 10 と 11 は 3 巡目 (§9 の「遷移中の競合」) で入れた修正ぶん。手順 1〜9 を書いた時点ではまだ実装されていなかったので、後から追加した。

**落ちたときの一次情報**: `backend.log` の `[mcp]` と `MCP:` 行 → 失敗一覧の分類 (`missing_config` / `auth_failed` / `network` / `command_error`) → `sea_trace.log` (取得が Pulse/Beat の頭で走っているか)。
