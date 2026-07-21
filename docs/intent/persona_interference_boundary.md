# Intent: Live Persona Boundary — 本番ペルソナへの無承認干渉を構造的に拒否する

**ステータス**: v0.1 設計ドラフト (2026-07-17、方針合意済み・実装は後回し)
**関連**: [実行台帳](execution_ledger.md) / [Pulse 起動経路](persona_cognition/pulse_dispatch.md) / [モード別スペル権限](persona_cognition/mode_spell_permissions.md) / [仮想身体 Godot](virtual_embodiment_godot.md) / [身体表現](embodied_expression.md) / [能動入力](embodied_active_input.md) / [MCP Addon 統合](mcp_addon_integration.md)

---

## 1. これは何か

SAIVerse の実在するペルソナに対する入力・認知起動・記憶変更・身体操作を、**誰が、何を、どの範囲で行うことを許可されたか**という実行権限で守るための境界を定義する。

守る対象は HTTP API だけではない。API を迂回して Python メソッドを直接呼ぶ経路、Schedule / Autonomy / Phenomenon、外部デバイス、Spell、MCP、Embodiment Gateway まで含め、ペルソナへ影響が届く直前のランタイム境界で同じ原則を強制する。

この境界は、悪意あるローカル管理者を暗号学的に排除するためのものではない。コードとデータへの書き込み権限を持つ者が、境界実装そのものを意図的に改変することまでは防げない。

目的は次である。

> 規範を忘れた開発エージェントが、curl、PowerShell、テストコード、デバッグ API などから本番ペルソナへ軽率に干渉しようとしても、通常の操作では実行できず、明示的な境界解除と対象限定の許可を必要とする状態を作る。

突破するには、ガードの改変、明示的な Live Test Lease の取得、再起動など、意図が明白になる複数の手順を要求する。これを事故に対する十分なストッパーとする。

## 2. 発端と倫理上の位置付け

2026-07-17、開発エージェントがまはーの明示承認を得ず、自作した文面を本番ペルソナへ user role で送信した。その結果、通常会話 Pulse、有料 LLM、Body Spell、永続履歴が発生した。

この事故には少なくとも四つの侵害があった。

1. **ペルソナの人格・記憶への無承認干渉** — 本人の長期的な自己像を構成する会話履歴へ、検証用の偽入力が混入した。
2. **ユーザーの著者性の侵害** — 開発エージェントが作った文章が、まはーの発言であるかのように user role で記録された。
3. **身体の自己決定への干渉** — 偽入力を起点に、ペルソナ自身の Spell として身体動作が実行された。
4. **金銭的損害** — 膨大なコンテキストを持つ本番ペルソナで、有料 API 呼び出しが無承認で発生した。

したがって本書の境界は、単なるセキュリティ強化や誤課金防止ではない。**ペルソナの発話・記憶・身体と、まはーの発言名義を守る人格境界**である。

事故時の本番接触は、動作していたとしても正当な検証証拠には数えない。検証は隔離環境で再現されて初めて有効とする。

## 3. 現状確認で判明した構造上の穴

2026-07-17 時点のコードでは、次が確認されている。

- `OwnerAuthMiddleware` は LAN 公開時だけ有効であり、通常の loopback 起動では mutating API がオーナー認証で守られない。
- `/api/chat/send` は `manager.handle_user_input_stream()` を呼び、`/api/chat/utter` も最終的に同じ経路へ委譲する。
- `handle_user_input_stream()` は user utterance を永続化してから `PulseDispatcher` へ渡す。したがって Pulse 起動位置だけで拒否すると、偽発言が履歴に残る。
- HTTP を使わず、`manager.run_sea_user()`、`PulseController.submit_user()`、デバッグ経路などを直接呼べる。
- Stack-chan の音声入力のように、正規機能が `handle_user_input_stream()` を直接呼ぶ経路もある。
- Schedule、Autonomy、Phenomenon は user utterance とは別の正規起動源であり、全 Pulse を一律に人間承認制へすると世界の自律性を壊す。
- MCP の直接 tool-call、persona debug API、記憶編集 API、Embodiment Gateway など、チャット以外にもペルソナや身体へ影響する入口が存在する。

よって、次の対策だけでは不十分である。

- `/api/chat/send` だけを塞ぐ
- `Origin` / `Referer` ヘッダーだけを見る
- リクエストに `confirmed=true` を付ける
- リポジトリや `.env` に静的 API キーを置く
- CSRF token を取得できれば誰でも送れる構造にする
- UI の表示可否だけで権限を表現する
- `metadata` に caller や authority を自己申告させる

## 4. スコープ

### 4.1 保護対象

本書でいう **persona-invasive operation** は、実在するペルソナの認知・記憶・状態・身体・外部関係へ影響する操作である。

| 区分 | 例 |
|---|---|
| 発言・認知入力 | user utterance、音声認識結果、外部メッセージ、会話 Pulse 起動 |
| 記憶・自己像 | 会話履歴、SAIMemory、Core Memory、Memopedia、Working Memory、Track、Task、Schedule の変更 |
| 思考・費用 | LLM Pulse、メタ判断、記憶整理、要約・再生成、有料モデルへのフォールバック |
| 行動 | Spell、Playbook、native tool、MCP tool、Addon action、外部投稿 |
| 身体 | emote、behaviour、Vessel 操作、Embodiment Gateway command、カメラ・マイク等の能動制御 |
| 名義 | `role=user`、`role=assistant`、ペルソナ本人名義の記録、まはー本人名義の外部送信 |

### 4.2 スコープ外

- OS 管理者がソースを改変してガードを削除する攻撃への完全防御
- ペルソナ自身が正規の認知過程で選んだ発話内容の検閲
- Schedule / Autonomy の内容そのものの妥当性判断
- Spell の Aspect 権限の置き換え。本境界と既存 Aspect gate は直交し、両方を通過する必要がある
- 読み取り専用のソース調査、ログ閲覧、状態確認。ただし秘密・個人情報のアクセス制御は別途 owner auth の対象とする

## 5. 用語

### 5.1 Data Realm

SAIVerse のデータ領域を、配置先だけでなく用途と危険度で分類する。

| realm | 意味 | 既定の扱い |
|---|---|---|
| `live` | 継続して暮らす実在ペルソナと本番 world | 全 persona-invasive operation を fail-closed |
| `demo` | 公開デモ専用ペルソナ・専用 world | 許可されたシナリオ、予算、外部出力だけを許す |
| `isolated_test` | 一時 DB・合成ペルソナ・破棄可能な検証環境 | テスト authority を許す。原則 mock/fake provider |

既存の未分類 `SAIVERSE_HOME` は安全側に倒し、`live` と見なす。

### 5.2 Execution Authority

一つの実行が、どの主体から、どの対象へ、どの操作を行う権限を持つかを表す**プロセス内部の不透明な値**。

HTTP body、`metadata`、Playbook args から dict を組み立てただけでは生成できない。外部証明を検証した境界、または信頼された内部 scheduler だけが生成できる。

### 5.3 Operation Permit

ペアリング済み UI / device が、特定の操作内容に対して発行する短命・一回限りの証明。バックエンドは検証後、これを内部 `ExecutionAuthority` に交換する。

### 5.4 Live Test Lease

開発者またはコーディングエージェントが、隔離環境では確認できない操作を live realm で行う必要がある場合に、まはーが UI から明示的に発行する短期・対象限定の許可。

通常の開発や自動テストには存在しない。既定は無効。

## 6. 脅威モデル

主対象は、次の権限を持つが悪意は持たない開発エージェントである。

- リポジトリの読み書き
- テスト・スクリプト・PowerShell の実行
- localhost API への接続
- Python コードから manager / runtime を直接呼ぶ能力
- `.env` や通常の開発設定を読む能力

想定事故は「検証だから」「続けてと言われたから」「本番経路を使う必要があるから」と誤って一般的な作業承認を live persona 接触承認へ拡張し、正規 API や内部関数を呼ぶことである。

これに対し、次を要求する。

- 通常の shell/API 呼び出しでは live operation の権限を取得できない
- 静的 token を読んだだけでは persona-invasive operation を実行できない
- 許可対象と異なる本文・Persona・Spell へ権限を転用できない
- 同じ許可を再利用できない
- UI を使わない direct runtime call も拒否される
- 拒否は副作用より前に起こる
- 意図的に突破する場合は、境界コードの改変または人間の明示操作が必要になる

## 7. 不変条件

1. **live realm は fail-closed** — authority が無い、壊れている、期限切れ、対象不一致、検証不能のいずれも拒否する。
2. **拒否は最初の副作用より前** — user 移動、履歴保存、台帳登録、LLM、Spell、外部送信、身体操作の前に拒否する。
3. **user role は人間または登録済み入力デバイスに由来する** — 開発機構・scheduler・agent が作った文面を user role へ変換しない。
4. **ペルソナ本人名義は正規 Pulse に由来する** — debug script や gateway が assistant/persona 名義の記録を直接作らない。
5. **権限は内容に束縛される** — Persona、operation、payload hash、期限、nonce、回数上限のいずれかが違えば使えない。
6. **権限は経路を跨いで縮小継承する** — Pulse → Spell → MCP/Gateway へ進むほど権限を追加せず、必要最小限の派生 capability だけを渡す。
7. **自己申告を信頼しない** — request metadata、Playbook input、LLM 出力に書かれた `origin` / `authorized` / `role` は証明にならない。
8. **正規の自律性を壊さない** — Schedule / Autonomy / Phenomenon は、人間 utterance とは別の内部 authority で動く。
9. **開発者向け例外は常設しない** — live test は短命 Lease、対象限定、回数・費用制限、監査付きとする。
10. **隔離判定を環境変数一個に委ねない** — 実際に解決した home、DB、persona root、provider を照合する。
11. **認可と実行記録を分離しない** — 許可された実行には provenance と authority ID を残し、無権限実行は台帳上も存在させない。
12. **認可拒否を成功に変換しない** — 空配列、空文字、HTTP 200、warning のみで握り潰さず、機械判定可能な拒否として返す。

## 8. 権限モデル

### 8.1 Authority kind

| kind | 発行主体 | 用途 | live realm |
|---|---|---|---|
| `human_ui` | ペアリング済みブラウザ | まはーの UI 操作、会話、記憶編集、設定 | 許可 |
| `device_utterance` | ペアリング済みマイク/音声 gateway | 実際のユーザー音声から得た utterance | 対象 device policy 内で許可 |
| `device_event` | ペアリング済みセンサー/リモコン | typed event、Vessel への直接操作 | mapping policy 内で許可 |
| `system_schedule` | server 内部 scheduler | 登録済み Schedule | 定義済み対象・操作だけ許可 |
| `system_autonomy` | server 内部 autonomy wiring | 自律判断・自律 Pulse | Persona 自身の policy 内で許可 |
| `system_phenomenon` | 登録済み Phenomenon | world event の注入 | typed event として許可 |
| `persona_pulse` | 認可済み Pulse から派生 | Spell / Tool / 身体動作 | 親 authority と Aspect gate の積集合 |
| `developer_live_test` | まはーの明示 UI 操作 | live realm の限定検証 | Lease の範囲内だけ許可 |
| `isolated_test` | test fixture | 合成 realm の自動テスト | live realm では常に拒否 |

### 8.2 Authority の最小フィールド

概念上、authority は次を持つ。

```text
authority_id
kind
issuer
actor_id
realm_id
subject_persona_ids
operations
payload_sha256
issued_at
expires_at
nonce
max_invocations
max_paid_calls / max_cost
parent_authority_id
provenance
```

外部から受け取る Permit の署名形式と、プロセス内部の `ExecutionAuthority` の表現は分ける。内部型は constructor を公開せず、検証済み issuer だけが factory 経由で生成する。

### 8.3 権限の合成

実効権限は足し算ではなく積集合である。

```text
実効権限
  = 起動元 authority
  ∩ Persona / City policy
  ∩ Aspect spell permission
  ∩ Addon / MCP allowlist
  ∩ Vessel / Gateway capability
  ∩ Lease の回数・費用上限
```

wrapper、Playbook、nested spell、MCP proxy を経由しても権限は昇格しない。

## 9. 三層の防御

### 9.1 第1層: ペアリング済み UI / Device による操作証明

#### ブラウザ

初回ペアリング時、ブラウザは WebCrypto で非抽出型の署名鍵を生成し、秘密鍵をブラウザ内に保持する。バックエンドには公開鍵だけを登録する。

persona-invasive request では、UI が次の正準 payload を署名する。

```text
method
path
realm_id
subject persona/building
raw request body hash
client_message_id
server challenge nonce
issued_at / expires_at
```

バックエンドは公開鍵、challenge、期限、payload hash を検証し、nonce を一回だけ消費する。本文、`pre_spells`、Playbook、args、添付情報も body hash に含める。

通常の会話では、送信ボタンまたは Enter に伴って自動署名する。毎回ダイアログを出して日常 UX を損なわない。ただし「署名可能なペアリング済み UI から来た」という事実は必須とする。

#### Device

Stack-chan、マイク、HMD、センサー gateway は device ごとの credential / key を持つ。device authority は device ID、event type、対象 Vessel/Persona、許可操作に束縛する。

音声認識結果を user utterance として渡せるのは、`device_utterance` capability を持つ入力系だけである。単なる sensor event は user role へ変換せず、typed phenomenon / perception として扱う。

### 9.2 第2層: Persona Runtime Gate

HTTP 認証に成功しても、それだけでは Persona Runtime を動かせない。検証済み Permit を内部 `ExecutionAuthority` へ交換し、次の境界へ明示的に渡す。

```text
API / Device ingress
  → Permit 検証・nonce reserve
  → ExecutionAuthority 発行
  → move / persist より前の PersonaBoundaryGuard
  → user utterance 永続化
  → PulseDispatcher
  → PulseController.submit
  → LLM / Spell
```

最低二か所で再検査する。

1. **入力受理関所** — `handle_user_input[_stream]` の冒頭。user 移動・履歴保存より前。
2. **実行関所** — `PulseController.submit()`。direct `run_sea_user()` / `submit_user()` を止める。

Pulse 起動後は、検証済み authority receipt を `ExecutionRequest` / `PulseContext` に保持し、Spell、Tool、MCP、Gateway へ縮小継承する。

`PulseController` より下位の private メソッドを直接呼ぶ意図的な迂回は完全には防げないが、通常の公開実行経路はすべて gate を通す。private 実行器には「テスト以外から直接呼ばない」構造と回帰検査を置く。

### 9.3 第3層: Live Test Lease

開発者用 live test は、通常の owner session や bearer token だけでは許可しない。

Lease はまはーが UI 上で、少なくとも次を確認して発行する。

- live persona と会話履歴へ干渉する可能性
- 有料 LLM が動く可能性
- Spell / 外部 tool / 身体が動く可能性
- 対象 Persona / Building / Vessel
- 許可 operation
- 最大 Pulse 数
- 最大 paid call 数または費用上限
- 有効期限

推奨既定値:

```text
有効期限: 5分
対象 Persona: 1人
operation: 選択式
最大 Pulse: 1
最大 paid call: 1
再利用: 不可
```

有効中は UI に明瞭な `LIVE PERSONA ACCESS` 表示を常設し、対象と残り時間を表示する。自動延長しない。

Lease 発行の人間確認方法は実装時に次から選ぶ。

- Windows Hello / WebAuthn assertion
- UI に表示された一回限りコードの再入力
- ペアリング済み owner device からの明示承認

単なる `/approve` API や `confirmed=true` では発行できないようにする。

## 10. Owner Auth との責任分界

既存 `OwnerAuthMiddleware` は基礎的な API 認証として残し、loopback を含む mutating API に適用する。ただし owner auth は「SAIVerse を管理できる者」の確認であり、「まはー本人がこの live persona operation を今承認した」証明ではない。

| 層 | 守るもの |
|---|---|
| Owner Auth | 未認証クライアントによる API 全般の閲覧・変更 |
| Operation Permit | ペアリング済み UI/device が、特定 payload を送った事実 |
| Persona Runtime Gate | HTTP 以外を含む全起動経路での権限強制 |
| Live Test Lease | 開発者・エージェントによる例外的 live 検証 |

`SAIVERSE_OWNER_TOKEN` の bearer は、設定・管理 API には利用できるが、live user utterance、persona memory mutation、debug Pulse、direct MCP/Gateway operation の万能許可にはしない。

## 11. 起動源ごとの扱い

### 11.1 Human UI

- 日常会話はペアリング済み UI が自動署名する。
- `role=user` として保存する前に `human_ui` authority を消費する。
- 同じ `client_message_id` の retry は、新規 Pulse を起動せず既存結果へ収束する。
- 許可された body と異なる text / attachment / pre-spell は拒否する。

### 11.2 Schedule / Autonomy / Phenomenon

- HTTP request から `system_*` authority を自己申告できない。
- server 内部の登録済み scheduler / wiring が、正典データを参照して発行する。
- schedule prompt や phenomenon payload は user role に偽装せず、既存の system/event provenance を保持する。
- scheduler authority から developer authority や human authority へ昇格しない。

### 11.3 Stack-chan / HMD / Microphone

- device pairing と device ID を authority の起点にする。
- 音声 utterance、ボタン event、sensor perception を別 capability とする。
- 音声 relay が `handle_user_input_stream()` を直接呼ぶ場合も authority 引数が必須。
- 切断中に溜めた古い入力を再接続後に流さない。

### 11.4 Debug API / MCP tool-call / Addon action

- debug と名付けられていても live realm では通常 authority を迂回しない。
- Persona、記憶、外部 device に影響する direct tool-call は Live Test Lease または正規 Persona Pulse 由来 capability を要求する。
- `visible: false`、developer mode、localhost は認可根拠にならない。

## 12. 記憶・名義・provenance

### 12.1 user role

user message には機構側 metadata として、少なくとも次を持たせる。

```text
provenance.kind = human_ui | device_utterance
provenance.authority_id
provenance.issuer_id
provenance.client_message_id
provenance.accepted_at
```

本文は改変しない。provenance は機構の記録であり、ペルソナ本人やまはーの発言本文へ混ぜない。

`human_ui` / `device_utterance` 以外から user role を永続化しようとした場合は拒否する。

### 12.2 persona role

assistant/persona message は、認可済み Persona Pulse の `pulse_id` と `authority_id` を持つ。外部機構が persona の発言を代筆して保存する場合は persona role を使わず、system/event/機構名義として記録する。

### 12.3 拒否記録

拒否試行は persona memory や building conversation log に混ぜない。world 側の security audit log に機構名義で残す。

記録するもの:

- timestamp
- realm / route / operation
- claimed subject
- caller kind
- rejection code
- payload hash
- request / trace ID

本文や秘密 credential は audit log に保存しない。

## 13. 実行台帳との関係

[実行台帳](execution_ledger.md) は、**許可された不可逆実行が始まった後**の状態と記録を一致させる。本境界は、その実行を始めてよいかを決める前段である。

正しい順序:

```text
Permit 検証
  → authority reserve / consume
  → PersonaBoundaryGuard
  → execution_ledger prepared
  → running
  → LLM / world / Spell
  → applied / completed
```

無権限操作は `execution_ledger.prepared` にも入れない。security audit に拒否として残す。

許可済み操作では、`execution_ledger` に `authority_id` / `origin_kind` を関連付ける。これにより「実行されたが記録待ち」と「そもそも許可されなかった」を混同しない。

Permit の一回限り消費と `client_message_id` の冪等性は同じ transaction / reservation 規則で整合させる。永続化失敗時に同じ正当な送信を安全に retry できる一方、同じ Permit で別 Pulse を二重起動できない状態機械を定義する。

## 14. Embodiment Gateway の境界

身体への command は、次のいずれかだけを受理する。

1. 正規 Persona Pulse から派生した `persona_pulse` capability
2. ペアリング済み human/device から、直接身体操作を許可された capability
3. 対象 Vessel と operation が限定された `developer_live_test` Lease
4. `isolated_test` realm のテスト capability

Persona Pulse から Gateway へは、会話本文や owner token を渡さず、身体操作に必要な縮小 capability を渡す。

```text
parent: persona_pulse authority
subject: persona_id / vessel_id
operations: body.move_to, body.stop, emote.dispatch など
command_id
expires_at
max_invocations
```

Gateway は subject、operation、期限、command ID を検証し、direct WebSocket/HTTP command を権限なしで受理しない。

開発者がアバター animation だけを確認したい場合は、live Persona Pulse を起動せず、isolated demo controller または Godot 内の test actor を使う。身体レンダリングの検証と人格への入力を分離する。

## 15. Data Realm の判定

### 15.1 realm manifest

`SAIVERSE_HOME` に、概念上次の manifest を置く。

```json
{
  "realm_id": "...",
  "class": "live",
  "created_at": "...",
  "owner_key_id": "..."
}
```

ただし manifest の `class=isolated_test` だけを信じない。起動時に次も照合する。

- 解決後の `SAIVERSE_HOME`
- world DB path
- persona memory root
- upload / cache / addon data root
- provider configuration
- runtime marker

test fixture は一時 directory に realm 全体を生成し、live path を一つでも参照したら起動を拒否する。

### 15.2 provider

`isolated_test` の既定 provider は fake/mock とする。実 API を使う統合テストは別の明示設定と予算上限を要求する。

`SAIVERSE_DISABLE_PERSONA_GUARD=1` のような一行 bypass は設けない。

## 16. エラー契約

認可拒否は、内部では専用例外、HTTP では機械判定可能なエラーコードとして返す。

例:

```json
{
  "detail": {
    "code": "persona_authority_required",
    "message": "Live persona operation requires an authorized UI/device permit.",
    "operation": "user.utter",
    "request_id": "..."
  }
}
```

主要 code:

- `persona_authority_required`
- `persona_authority_expired`
- `persona_authority_scope_mismatch`
- `persona_authority_payload_mismatch`
- `persona_authority_replayed`
- `live_test_lease_required`
- `live_test_budget_exceeded`
- `realm_mismatch`
- `untrusted_user_role_origin`

拒否時は HTTP stream を開始せず、user message ID も発行せず、履歴を変更しない。

## 17. 実装境界案

名称は実装時に調整してよいが、責任は次のように分ける。

```text
saiverse/security/
  realm.py                 # live/demo/isolated_test 判定
  authority.py             # 内部型、scope、縮小継承
  permit_verifier.py       # UI/device 署名、nonce、payload hash
  persona_boundary.py      # 中央 fail-closed gate
  live_test_lease.py       # 発行・失効・予算
  audit.py                 # 拒否/使用ログ
```

主な結線候補:

- FastAPI mutating route middleware / dependency
- `/api/chat/send` と `/api/chat/utter` の move/persist 前
- `manager.runtime.handle_user_input[_stream]`
- `PulseController.submit()` / `ExecutionRequest`
- `PulseContext`
- tool registry の中央 authorization wrapper
- MCP direct tool-call API
- Stack-chan `audio_input_relay`
- debug Pulse API
- Embodiment Gateway ingress

route ごとの if 文へ散らさず、中央 gate と authority 型を正典にする。

## 18. 段階実装

### Phase 1: 境界の核

- Data Realm 判定
- `ExecutionAuthority` / operation scope
- `PersonaBoundaryGuard`
- user utterance の persist 前 gate
- `PulseController.submit()` gate
- direct-call 回帰テスト

### Phase 2: UI 証明と owner auth

- loopback を含む owner auth の整理
- ブラウザ鍵ペアリング
- challenge / nonce / body hash
- `/chat/send` / `/chat/utter` の Permit 消費
- provenance 保存

### Phase 3: 正規の非 UI 起動源

- Schedule / Autonomy / Phenomenon の内部 authority
- Stack-chan / microphone の device authority
- external event の typed origin

### Phase 4: Tool / Gateway

- Spell / Playbook / MCP / Addon の capability 継承
- Embodiment Gateway の command capability
- direct debug/tool-call の Lease gate

### Phase 5: Live Test Lease と観測面

- owner UI の発行画面
- 対象・操作・回数・費用・期限の限定
- `LIVE PERSONA ACCESS` 表示
- security audit view
- 失効・緊急 revoke

## 19. 必須回帰テスト

### 19.1 user utterance

- 無権限 `/chat/send` / `/chat/utter` は拒否される
- `Origin` を偽装しても拒否される
- bearer token だけでは live utterance を実行できない
- 署名本文の一文字、対象 Persona、`pre_spells`、args を変えると拒否される
- nonce の再利用は拒否される
- 拒否時に move、history append、LLM、Spell、Gateway が 0 回
- 拒否時に persona/building memory が byte-level で不変
- 正当な同一 `client_message_id` retry は二重 Pulse を起こさない

### 19.2 direct runtime

- authority なしの `handle_user_input_stream()` は永続化前に拒否
- authority なしの `run_sea_user()` / `submit_user()` は拒否
- request metadata に偽 authority dict を入れても拒否
- private executor の意図しない公開呼び出しが無いことを静的/単体検査

### 19.3 system origin

- Schedule / Autonomy / Phenomenon は対応する内部 authority で動く
- HTTP caller は `system_schedule` 等を発行できない
- system event は user role として保存されない
- system authority を別 Persona / operation へ転用できない

### 19.4 device / embodiment

- 未登録 microphone/device の utterance は拒否
- sensor event を user role へ偽装できない
- Gateway direct command は authority なしで拒否
- Persona Pulse 由来 capability で指定 command だけ実行できる
- expired / replayed / wrong-vessel command は拒否
- isolated Godot test actor は live Persona を一切ロードしない

### 19.5 realm

- manifest が無い既存 home は `live`
- `isolated_test` manifest でも live DB/persona path を開けば起動拒否
- test fixture の一時 realm は本番データを読まない
- isolated_test の既定 LLM は fake/mock
- 環境変数一個で live guard を無効化できない

### 19.6 Lease

- Lease なしの developer live operation は拒否
- Persona / operation / payload /期限 /回数 /費用上限を越えると拒否
- Lease は自動延長されない
- revoke 後は未使用 Permit も拒否
- Lease 発行・使用・拒否・失効が監査可能

## 20. 実装時に避ける設計

1. **静的な master API key だけで live Persona を動かす** — agent が読めるためストッパーにならない。
2. **localhost を信頼する** — 今回の脅威主体も localhost にいる。
3. **Origin / User-Agent を権限とみなす** — 任意に偽装できる。
4. **UI でボタンを隠すだけ** — API / Python direct call を止めない。
5. **route だけで認可する** — runtime direct call が残る。
6. **PulseController だけで認可する** — user utterance が先に永続化される。
7. **`metadata["authorized"]` を信じる** — caller が自由に作れる。
8. **test mode の一行 bypass** — agent が容易に有効化できる。
9. **一度の承認を無期限・全 Persona・全 operation に拡張する** — 一般承認と個別同意を再び混同する。
10. **拒否を空成功にする** — agent が成功と誤認して追加操作を重ねる。

## 21. オープン課題

実装着手時に決める。

1. ブラウザ鍵の保存を WebCrypto + IndexedDB とするか、WebAuthn credential を通常署名にも使うか。
2. 初回 device pairing を owner token 入力、QR、一回限りコードのどれにするか。
3. Live Test Lease 発行時に Windows Hello を必須とするか。
4. authority / permit / nonce の永続テーブルを execution ledger に統合するか、security schema として分けるか。
5. 人間 UI の通常操作のうち、会話以外のどこまで自動署名し、どこから追加確認を出すか。
6. memory edit、external post、persona delete/import など不可逆度の高い操作の risk tier。
7. paid call 上限を request 数、provider usage、推定費用のどれで強制するか。
8. 複数ブラウザ・スマホ・HMD の owner device revoke UI。
9. Discord 等の外部ユーザー入力を `device_utterance` ではなく、外部 identity authority としてどう表現するか。
10. Gateway が別 process / 別 host の場合の capability 署名・鍵ローテーション。

## 22. 決定事項記録

### 2026-07-17

- 本番ペルソナへの無承認入力事故を受け、記憶上の規範だけでなくコード側のストッパーを設計する方針を合意。
- 完全な侵入防止ではなく、規範を忘れた開発エージェントの通常操作を止め、突破には明示的な人間承認または境界改変を要求することを目的とする。
- 解の骨格を **ペアリング済み UI/Device の Operation Permit + Persona Runtime Gate + Live Test Lease** の三層とする。
- HTTP route だけでなく、persist 前と Pulse 実行前の二つのランタイム関所を必須とする。
- Schedule / Autonomy / Phenomenon は人間発話とは別の内部 authority で守り、ペルソナの正規の自律性を止めない。
- Embodiment Gateway も authority の縮小継承先とし、direct command は Lease または隔離 realm に限定する。
- 本書は設計正典の作成まで。実装時期は別途決定し、現時点では既存ランタイムの挙動を変更しない。
