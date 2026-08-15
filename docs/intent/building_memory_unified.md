# Intent: Building Memory 統合設計 (DB化・並行クライアント耐性・発言契機入室)

## これは何か

Building のチャットログを `~/.saiverse/cities/<city>/buildings/<bid>/log.json` の単純 JSON ファイルから、`saiverse.db` の `building_messages` テーブルへ移行する。同時に、その移行を契機として「並行クライアントが居ても破綻しない」ための CAS / 冪等キーを導入し、「ユーザーは発言した瞬間に入室する」というセマンティクスへ移行する。視点別レンダリング (一人称/三人称) もこの基盤の上に乗せる。

設計上の核は次の 4 つ:

1. **JSON → SQLite テーブル**: ATOMIC INSERT で race を構造的に排除、`saiverse.db` バックアップ機構に相乗り
2. **並行クライアント Lv1**: 位置更新に CAS、発言に idempotency_key
3. **発言契機の入室**: AI は明示移動、user は「発言 = 入室」、閲覧モードでは移動しない (非対称ルール)
4. **3 箇所連動の視覚フィードバック**: 左サイドバー (現在地マーカー) / メインチャット (移動メッセージ表示) / 右サイドバー (Occupants にユーザー表示)

## 背景

### 2026-04-26 PCクラッシュ事故

PC が突然落ち、`cities/<bid>/log.json` が複数 0 バイトになる。事後調査で JSON ファイルベースの脆弱性が複数判明:

- アトミック書き込みなし → クラッシュで全 0 埋め
- `_init_building_histories` で 5 状態 (不在/0バイト/空配列/正常/破損) を一本化していた → cascade 上書き
- OccupancyManager が `building_histories` を直接 mutation → seq なしの host event が混入し、後続の `add_to_building_only` で `last_seq=0` となり新メッセージに seq=1〜3 という低い値が割り当てられる
- `pulse_cursor` との不整合 → cursor=171, 新 msg seq=2 → ペルソナがプロンプトをスキップ
- restore 時に counter 更新漏れ
- JSON は単一視点テキスト → ペルソナ本人視点と他者視点を区別できない (自分のことを三人称で語る違和感)

応急処置 (実装済み): アトミック書き込み, 隔離システム, バックアップスナップショット, `modified_buildings` 追跡, counter リセット, OccupancyManager 経由を `add_building_event` 統一。それでも根本的な責務過大は残っていた。

### 2026-05-19 並行書き込み事故

まはーがスマホから `stackchan_room` に画像付き発言 → スマホブラウザ閉じる → PC ブラウザで開く、という操作中に、`log.json` が破損:

- seq 344, 345 が二重登録
- 1 つの occupancy enter event の content/metadata が、Air の LLM 応答内容で上書きマージされた (role=host だが content と llm_usage は assistant のもの)
- UI で「プロンプトの上に応答が System 名義で表示される」現象として観測

原因: スマホ・PC 並行セッションが log.json への書き込みを race。書き込みは atomic でも、in-memory dict mutation と seq 採番がロックなしのため、別パスからの同時 append が seq 衝突と内容 merge を起こした。

両事故は「JSON ファイルベース + 並行クライアント許容なし」という現状設計の限界を示している。これを統合的に解決する。

## 守るべき不変条件

設計判断の根拠となる、絶対に壊してはいけない性質:

1. **ユーザー位置は単一**: `User.CURRENT_BUILDINGID` の 1 カラムが真実。multi-presence (複数箇所に同時にいる) は世界モデルとして許容しない
2. **occupancy event は意図的移動に対応**: ユーザーの場合は発言意図、AI の場合は明示移動。閲覧/UI ナビゲーションでは event を発火しない
3. **ログは race / クラッシュで壊れない**: 並行書き込み・電源断のいずれでも、ログの中身が他のメッセージの内容で merge / 上書きされない
4. **seq は building 内で単調増加かつユニーク**: ペルソナの `pulse_cursor` が信頼できる前提を維持
5. **視点による表現の自然さ**: 自分の移動を三人称で語らない / 他者の移動を一人称で語らない
6. **既存の永続データを失わない**: 現在の log.json 群を可逆に DB へ移行する

## 設計

### A. building_messages テーブル (saiverse.db)

```sql
CREATE TABLE building_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    building_id     TEXT    NOT NULL,
    seq             INTEGER NOT NULL,              -- per-building, UNIQUE(building_id, seq)
    role            TEXT    NOT NULL,              -- 'user' | 'assistant' | 'host'
    persona_id      TEXT,                          -- assistant の場合の発話者
    content         TEXT    NOT NULL,
    timestamp       TEXT    NOT NULL,
    heard_by        TEXT    NOT NULL DEFAULT '[]', -- JSON array
    ingested_by     TEXT    NOT NULL DEFAULT '[]', -- JSON array
    event_type      TEXT,                          -- 'occupancy' | 'world' | 'spawn' | null
    event_data      TEXT,                          -- JSON (entity_id, from/to building_id, action 等)
    metadata        TEXT,                          -- 残りメタデータ JSON
    message_id      TEXT,                          -- legacy `building_id:seq` 互換
    client_message_id TEXT,                        -- クライアント生成 UUID (idempotency)
    origin_track_id TEXT,                          -- Track 横断クエリ用
    UNIQUE(building_id, seq),
    UNIQUE(client_message_id)                      -- NULL は重複可
);
CREATE INDEX idx_building_msgs_bid_seq    ON building_messages(building_id, seq);
CREATE INDEX idx_building_msgs_event      ON building_messages(building_id, event_type);
CREATE INDEX idx_building_msgs_client_mid ON building_messages(client_message_id)
    WHERE client_message_id IS NOT NULL;
```

**利点**:

- 既存 `saiverse.db` 自動バックアップ機構 (`SAIVERSE_DB_BACKUP_ON_START`) に乗る
- SQLAlchemy パターンで他テーブルと統一
- ATOMIC INSERT で並列書き込み・クラッシュ耐性が構造的に保証される
- `UNIQUE(building_id, seq)` で seq 衝突が事前検出される
- インデックスで `cursor > seq` の差分取得が高速
- per-persona memory.db との対称性 (あちらは個人記憶、こちらは場所記憶)

**SQLite の並列書き込み**:

- WAL モード (`PRAGMA journal_mode=WAL`) に切り替える。これで read/write 並列、複数 writer も序列化される
- writer は 1 つに直列化されるが、現状の SAIVerse は同一プロセス内なので問題なし
- 将来 inter-city で書き込み競合が起きるとしても、それは別プロセスからの API 呼び出し経由で、サーバ側で序列化される

### B. 並行クライアント Lv1: CAS + 冪等キー

#### B-1. 位置更新 CAS

`POST /api/user/move` の API を変更:

```
POST /api/user/move {
    "target_building_id": "stackchan_room",
    "expected_from_building_id": "air_city_a_room"   ← 新規必須
}
```

サーバ側は DB UPDATE に `WHERE CURRENT_BUILDINGID = ?` を付ける:

```sql
UPDATE users
   SET CURRENT_BUILDINGID = :target
 WHERE USERID = :uid
   AND CURRENT_BUILDINGID = :expected_from
```

rowcount=0 なら「他クライアントが先に移動した」エラーを返す。クライアントは status を再取得して再判断 (リトライ or ユーザー通知)。

これで「タブA が `stackchan_room` から `エアの部屋` へ移動、タブB が `stackchan_room` から `ミラの部屋` へ移動」の race で、後勝ちでも両方の event が log に書かれる現象が止まる。

#### B-2. 発言の idempotency_key

クライアントは発言送信時に `crypto.randomUUID()` で生成した UUID を `client_message_id` として送る:

```
POST /api/chat/send {
    "building_id": "stackchan_room",
    "content": "...",
    "client_message_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

サーバは `building_messages` への INSERT を試み、`UNIQUE(client_message_id)` 違反なら既存レコードの `seq` / `message_id` を返す (no-op)。

これで:

- ネットワーク不安定でクライアントが自動リトライしても、二重発言にならない
- ユーザーが「反応ない」と思って 2 回押しても、二重発言にならない

CAS との役割分担:

| 機構 | 防ぐもの |
|---|---|
| CAS (`expected_from`) | 別クライアントが**別々の意図**を並行送信したときの state race |
| idempotency_key | 同じ送信操作の**リトライ**による二重実行 |

両方別問題で、両方要る。

#### Lv2 はやらない

「タブA で移動するとタブB の表示にも自動反映される」というリアルタイム同期 (SSE / WS push) は本ドキュメントのスコープ外。クライアント側の `status` polling か手動リロードで状態追随する Lv1 で止める。

理由: SAIVerse には現状で汎用的なクライアント push 基盤がない (`addon_events` SSE は別目的)。新規実装すれば AI 発言通知 / ペルソナ移動通知など他機能でも要るので、独立した設計判断として別 Intent Doc で扱う。

### C. 発言契機の入室ルール

#### 現状

ユーザーが Building リストで建物名をクリック → `POST /api/user/move` → CURRENT_BUILDINGID 更新 → enter/leave event 発火 → log にスパム的に積まれる。「閲覧」と「入室」が同一操作になっている。

#### 新ルール

- **AI**: 現状維持。明示的な物理移動 (`OccupancyManager.move_entity`) → enter/leave event 発火
- **User**: 「発言した瞬間に入室」。Building リストクリックは「閲覧モード」(ログ subscribe のみ、CURRENT_BUILDINGID 据え置き、event 発火なし)。発言契機入室は専用エンドポイント `POST /api/chat/utter` が担う (`target_building_id != CURRENT_BUILDINGID` ならサーバ側で `move (W5 台帳実行) → speak` を実行)。raw `POST /api/chat/send` は**サーバ現在地専用**で、別 Building 指定は 409 で拒否する (W7 柱5 / 分離監査 P1-3: 単一位置モデルの API 迂回と「不在の部屋に居た履歴」の封鎖)

```
ユーザー操作                      サーバ動作
建物名クリック                    閲覧モード (CURRENT 据え置き、event なし)
発言送信 (target=現在地)          発言記録のみ
発言送信 (target!=現在地)         /chat/utter が move (原子的な台帳実行) → speak
他建物を閲覧→閉じる               何も起きない
```

utter のコマンド意味論 (W7 で正直化): 入室 = `move.entity` 台帳実行として原子的 /
発言 = durable insert が認知開始の前提条件。「入室成功 → 発言 insert 失敗」では
入室は残り (発言契機の入室は物理事実)、再送は current == target で move を
スキップし `client_message_id` の冪等キーで発言が一度だけ載る。

#### AI / user 非対称の根拠

AI は「閲覧」という行為がなく、移動 = 意図的な世界内行動として一貫している。ユーザーは UI 上で「ちょっと別の部屋を見たい」という閲覧需要が常にある。両者を同じセマンティクスに揃えると、user 側に過剰な操作 (閲覧ボタン / 入室ボタン分離) を強いる、または AI 側に閲覧概念を持ち込んで世界モデルを壊す、のどちらかになる。非対称を許容するのが最も自然。

#### 初回起動時の現在地

現状維持 (前回終了時の `CURRENT_BUILDINGID` がそのまま残る)。「起動 = どこかにいる」を崩さない。「起動直後はどこにも居ない」を許すと、ペルソナから見た「ユーザーがいきなり現れる」のセマンティクスが壊れる。

#### AI 視点の「ユーザーが今ここに居る」判定

`CURRENT_BUILDINGID` 依拠を維持。発言契機ルールでも「発言した瞬間に CURRENT が更新される」ので、ペルソナは「入室 → 発言」を 1 つの一連イベントとして受け取る。これは現実の人間関係 (ノックして部屋に入りながら話し始める) と整合する。

### D. 3 箇所連動の視覚フィードバック

「閲覧中の建物 ≠ 居場所」をユーザーが直感的に認識できるよう、UI の 3 箇所に情報を分散配置する:

1. **左サイドバー (Building リスト)**: 現在地に**人アイコンマーカー**を付ける。サイドバーを見れば「自分はあそこに居る」が一瞥で分かる
2. **メインチャット UI**: 移動メッセージ (leave/enter event の note-box) を**表示する**。移動が乱発しなくなる新ルールの下では、移動メッセージはノイズではなく時系列の意味ある情報になる
3. **右サイドバー (Occupants)**: ユーザーも Occupants 一覧に**自分のアイコンとして表示する**。「自分も場のメンバー」が空間として認識される。発言契機で入室するとここに自分が追加される視覚効果が「あ、今ここに入ったんだ」を学習させる

3 段の情報密度で「閲覧中 ≠ 居場所」が無意識に学習される設計。

### E. 視点別レンダリング

`event_type='occupancy'`, `event_data={entity_id, from_building_id, to_building_id, action}` で構造化保存し、読み手視点で content をレンダリング:

- **一人称** (読み手 == entity_id): 「あなたは [from] から [to] へ移動しました」
- **三人称** (その他): 「[entity_name] が [from] から [to] へ入室しました」
- **ユーザー UI**: 既存の note-box 形式の content を表示 (後方互換)

これにより「自分の移動を三人称で語る」違和感が消え、移動先の建物名も自然に含まれる。

レンダリングは保存時ではなく**読み出し時**に行う。content カラムには「UI 表示用」のテキストを保存しつつ、`event_data` から動的に視点別レンダリングを生成する。

## 移行フェーズ

### Phase 1: テーブル追加 + dual-write

- `building_messages` テーブル作成 (migration script)
- `HistoryManager.add_to_building_only` / `add_building_event` を「DB INSERT + JSON append」の dual-write に
- 読み出しは引き続き JSON から (= まだ既存挙動)
- 全機能が DB にも書き込まれていることを実機で確認

### Phase 2: 読み出しを DB に切替

- `get_building_history` / `get_recent_history` などの読み出し系を DB クエリに変更
- JSON は presentation cache 扱い (= 書き込みは続けるが、読み出しは DB が source of truth)
- pulse_cursor の取り扱いも DB の seq に切替

### Phase 3: JSON 廃止

- JSON への書き込みを止める
- 既存 `cities/<bid>/log.json` は「過去ログのアーカイブ」として残す or 削除
- `manager/history.py` の HistoryMixin の JSON 関連メソッド整理

### Phase 4: 並行クライアント Lv1 + 発言契機入室

- `POST /api/user/move` に CAS 導入
- `POST /api/chat/send` に `client_message_id` 必須化
- フロント: 建物クリック = 閲覧モード、発言時に atomic leave+enter+speak
- 3 箇所視覚フィードバック実装

Phase 1 ~ 3 は DB 化単独で意味のある変更、Phase 4 は並行クライアント対応。Phase 順序は前提依存があるので守る。

### 既存データ migration

`cities/<bid>/log.json` を読んで `building_messages` に INSERT する script を `scripts/migrate_building_logs_to_db.py` として実装。冪等にする (再実行しても重複 INSERT しない)。失敗時は `building_id` 単位でロールバック。

### 過去ログ取り込みの自動化と検算 (2026-08-16 改修)

#### 事故: テスタロッサの部屋の取り込み漏れ

2026-08-16、テスタロッサの部屋の履歴が UI から完全に消えていることが発覚した。実体は Phase 2 切替 (2026-05-20) 時の取り込み漏れ: 移行 script が「`log.json.corrupted_*` マーカーファイルが同じフォルダにある部屋 = 隔離中」とみなして丸ごとスキップしていた。マーカーは 4/26 の PC クラッシュ事故の退避物で、部屋自体はその後修復され健全な log.json (360 件) を持っていたのに、マーカーだけ掃除されず残っていた。saiverse_navi の部屋も同じ理由でファイル時代の履歴が欠けた (こちらは移行後の新規発言で覆われて見えにくかった)。データ自体は log.json に無傷で残っている。

三つの原因の記録:

1. **技術的直接原因**: スキップ判定が「現物の log.json の健全性」でなく「過去のマーカーファイルの存在」を見ていた。派生状態 (マーカー) が元の状態 (修復済みの現物) より優先された。
2. **判断の失敗**: 「skip はログ 1 行出せば十分」とした。移行は一回きりの操作で、その一回のログを読み逃したら二度と気づけない構造だったのに、「ファイルにあった履歴が DB に入ったか」を突き合わせる検算を置かなかった。
3. **プロセスの条件**: 移行 script が手動実行前提で、リリース版ユーザーの経路 (バージョンアップグレード) に組み込まれていなかった。空の部屋は「元から履歴が無い部屋」と見分けがつかず、発覚が 2.5 ヶ月遅れた。

#### 再発防止の構造 (dev5)

役割を三つに分け、実体は `saiverse/legacy_log_import.py` に一本化した:

1. **取り込みの自動実行**: バージョンアップグレード (`saiverse/upgrade_handlers.py` の `0.3.0.dev4 → 0.3.0.dev5` エッジ) で、SAIVerseManager 生成 = 世界が動き出す前に一度だけ走る。City スコープで building log、AI スコープで conscious_log の pulse cursor (City → AI の実行順は upgrade 枠組みが保証し、cursor の legacy_seq リマップが取り込み済みの building_messages を引ける)。
2. **スキップ判定は「現物が読めるか」だけ**: マーカーファイルの有無では判定しない。「取り込み痕跡 (legacy_seq 付き行) が既にある」= 冪等 skip、「痕跡なしで通常経路の行がある」= 順序保護のため skip (自動マージしない)。cursor 取り込みは生きた cursor 行がある環境では何もしない (稼働中の値を古いファイルで上書きしない)。
3. **常設の検算**: 毎起動、`manager/initialization.py` が log.json ↔ DB を突き合わせ (`scan_legacy_log_deficits`)、ファイルに履歴があるのに DB に取り込み痕跡が無い部屋を startup_alerts (UI バナー) に載せる。ループ内の帳簿でなく DB を SELECT し直して確かめる。**取り込みが将来別の理由で漏れても、解消されるまで毎起動アラートが出続ける**——ここが唯一の関所で、アップグレード側は黙って続行してよい。

転移テスト (この防止策が他の同型事故も捕まえるか): (a) log.json が本当に壊れている部屋の取り込み不能も同じ検算がアラートにする (旧実装はログ 1 行で沈黙)。(b) 移行前に世界が動いて新規行が付いた部屋 (saiverse_navi 型) も「live_rows_only」として毎起動可視化される。いずれも「一括移行が黙って一部を落とし、落ちた結果が『元から無い』と見分けがつかない」という族で、検算はこの族全体を覆う。

## やらないこと

スコープを明確化するために、本ドキュメントで意図的にやらないと決めたものを列挙する:

- **Lv2 リアルタイム同期 (SSE/WS push)**: クライアント間の自動状態反映は別 Intent Doc 案件
- **multi-presence**: ユーザーが複数箇所に同時にいる世界モデルは採用しない
- **AI 側の閲覧モード**: AI は明示移動のまま、閲覧概念は持ち込まない
- **「閲覧モード」専用 UI 通知基盤**: ユーザーが他建物を見ているだけの状態を、ペルソナや他クライアントに通知する仕組みは作らない (見たい時は自分の `CURRENT_BUILDINGID` の確認だけで十分)
- **ペルソナ視点の移動メッセージレンダリング全面変更**: 既存の auto_ingest 経路は head_pipeline に移行済み。本ドキュメントの視点別レンダリングは UI 表示と、もし将来 saimemory に書く時の表現に限定する

## 残課題

ドラフト確定後、実装着手時に詰める:

1. **マイグレーション script の具体**: 既存 log.json の各メッセージから `event_type` / `event_data` を抽出するロジック。host メッセージの metadata.event を変換、その他は event_type=NULL
2. **`client_message_id` をどこで生成するか**: フロントの `useChat` 系 hook に追加。既存の optimistic update との関係整理
3. **建物切替の API パス変更**: 「閲覧 = subscribe」の意味で `POST /api/user/move` は廃止 → `GET /api/buildings/<bid>/messages` の polling/subscribe に。サイドバークリック → URL 更新 → メッセージ取得、という UI フロー
4. **移動メッセージ表示**: 現状フロントで note-box 形式の host メッセージがどう描画されているか実機確認。スタイル調整 (色を薄める / 高さを抑える) で「ノイジーじゃない」表示を作る
5. **CAS 失敗時の UX**: `expected_from` mismatch エラー時、フロントは何を見せるか。サイレントに status 再取得して移動取り消し? エラートースト?
6. **既存 quarantine 機構の DB 移行版**: 現状の隔離システムは log.json 破損対応。DB 化後は「行レベル破損」は構造的に起きないが、何らかの隔離単位 (building 単位の「整合性エラー検出時に新規書き込み拒否」など) を残すべきか
7. **dual-write 期間中の整合性チェック**: Phase 1 で「JSON と DB が一致するか」を自動検証する仕組みが必要かどうか

## 影響範囲: Building ログを読む側 / 書く側

Phase 2 (読み出し DB 切替) で書き換えが必要な箇所を、レイヤ別に整理する。

### 構造

現状、Building ログへのアクセスは 3 層構造になっている:

```
Layer 1 (Public Accessor):   読み出しの入口。意味のある API として公開
Layer 2 (Internal Mutation): HistoryManager の add/update。Layer 1 と書き込み経路に同居
Layer 3 (Initialization & I/O): 起動時ロード / atomic 書き出し / 隔離・復旧
```

### Layer 1: 読み出しの公開 API (≈ 6 関数)

ここを DB クエリに書き換えれば、依存している全コードが芋づる式に切り替わる:

| ファイル | 関数 | 用途 |
|---|---|---|
| `manager/history.py:289` | `get_building_history(bid)` | manager レベルの汎用アクセサ |
| `saiverse/saiverse_manager.py:1559` | `get_building_history(bid)` | 上記の委譲 |
| `persona/history_manager.py:611` | `get_building_recent_history(bid, max_chars)` | ペルソナ視点の最近メッセージ |
| `persona/history_manager.py:626` | `get_recent_entrant_events(bid, lookback)` | occupancy event の構造化抽出 |
| `persona/mixins/history.py:171` | `get_building_history(bid)` | ペルソナ mixin の薄いラッパー |
| `persona/mixins/history.py:190` | (周辺) building 履歴フィルタ系 | コンテキスト生成用 |

### Layer 1 を経由しない直接読み出し (≈ 10 箇所)

`history_manager.building_histories.get(bid, [])` / `manager.building_histories.get(bid, [])` を直接呼んでいる箇所。Layer 1 を整備した後に**段階的に Layer 1 経由へ書き換える**:

| ファイル:行 | 用途 |
|---|---|
| `api/routes/chat.py:101` | chat UI に raw_history を返す |
| `api/routes/chat.py:738` (`get_building_history`) | 既に Layer 1 経由 — そのまま |
| `manager/gateway.py:307` | Discord gateway 経由配信 |
| `builtin_data/tools/get_building_messages.py:49, 291` | auto_ingest ツール (host event のスキャン) |
| `persona/mixins/generation.py:360, 487, 651` | コンテキスト生成 |
| `persona/history_manager.py:201, 374, 641, 669, 705` | 自身の内部ロジック (decorate, update, entrant events, should_recall) |
| `sea/runtime.py:184, 189` | メタボリズム前後の長さ比較 |
| `saiverse/track_handlers/user_conversation_handler.py` | Track 処理 |
| `saiverse/content_tags.py` | タグ処理 |

`building_histories[bid]` を**直接読み出し**で添字アクセスしている箇所は **ゼロ** (確認済み — 全てテスト or 書き込み)。これは整理しやすい良い兆候。

### Layer 2: 書き込み経路 (mutation)

dual-write 期間中、これらは「JSON 既存処理 + DB INSERT」の両方を実行する:

| ファイル | API | 役割 |
|---|---|---|
| `persona/history_manager.py:254` | `add_message(msg, bid, ...)` | ペルソナ発話の追加 (persona + building 両方) |
| `persona/history_manager.py:303` | `add_to_building_only(bid, msg, ...)` | building のみ追加 (streaming placeholder 等) |
| `persona/history_manager.py:347` | `update_building_message(bid, mid, ...)` | 既存メッセージの content / metadata 更新 |
| `manager/history.py:97` | `add_building_event(bid, msg, ...)` | OccupancyManager / world event 等の host event |

### Layer 3: I/O と初期化

`log.json` ファイルに直接触る箇所:

| ファイル:行 | 役割 | Phase での扱い |
|---|---|---|
| `manager/initialization.py:155` (`_init_building_histories`) | 起動時 5 状態判定 + ロード + 隔離 | Phase 2 で DB ロードに置換 |
| `manager/initialization.py:126` (`_init_file_paths`) | `building_memory_paths` 構築 | Phase 3 で削除 |
| `manager/history.py:223` (`_save_building_histories`) | atomic 書き出し | Phase 1 では維持 (dual-write の JSON 側)、Phase 3 で削除 |
| `manager/history.py:44` (`create_log_backup_snapshot`) | 起動時バックアップ | Phase 3 で削除 (`saiverse.db` 自動バックアップに統合済) |
| `manager/history.py:295` (`backup_world` / `restore_world`) | zip backup/restore | Phase 3 で zip 内の log.json 廃止、DB のみ |
| `api/routes/system.py:257, 310` | 隔離復旧 UI からの直接書き込み | Phase 1 で DB 側にも反映するよう拡張 |
| `frontend/src/components/QuarantineModal.tsx` | 隔離 UI | DB 化後の隔離単位次第で再設計 (残課題 #6) |

### `building_histories` dict 自体の初期化箇所

Phase 1 では dict も維持 (JSON 経由のキャッシュ)、Phase 2 で読み出しを DB に切り替えた後、dict は不要になる:

- `manager/initialization.py:188, 225` (起動時)
- `manager/persona.py:399` (新規 building 作成時の初期化 — blueprint 経由)
- `manager/blueprints.py:254` (blueprint 経由の private room 作成)
- `saiverse/saiverse_manager.py:1778` (manager 側の building 追加)
- `api/routes/system.py:257, 310` (隔離復旧後の差し戻し)

### 外部スクリプト・他形式 importer

ここは Building ログ本体には触らないが、似た JSON 形式を読む系:

- `scripts/import_chatlog_json.py` — 別形式 chatlog → SAIMemory (Building ログ無関係)
- `scripts/import_persona_logs_to_saimemory.py` — persona log → SAIMemory (Building ログ無関係)

Building ログ自体を読む移行 script (`scripts/migrate_building_logs_to_db.py`) は新規実装が必要。

### テスト

書き換え必要 (Layer 1 / Layer 2 / Layer 3 の挙動が変わるため):

- `tests/test_history_manager.py` — HistoryManager 単体テスト
- `tests/test_building_history_safety.py` — 隔離 / 復旧テスト
- `tests/sea/test_runtime_*.py` (3 ファイル) — runtime 経由の building 履歴
- `tests/test_persona_mixins.py` — mixin の history アクセス
- `tests/test_promote_meta_judgment.py` — meta 判定経由

### 移行戦略の指針

1. **Layer 1 を先に DB 対応に拡張**: 既存 API シグネチャを維持しつつ、内部実装で DB クエリを発行
2. **Layer 2 で dual-write 開始**: 書き込みが両側に行く状態を作る (Phase 1)
3. **Layer 1 を経由しない直接読み出しを Layer 1 経由に集約**: ≈10 箇所を順次書き換え
4. **Layer 1 の内部実装を DB 切替**: in-memory dict 参照 → DB クエリ (Phase 2)
5. **Layer 3 廃止**: `_init_building_histories` / `_save_building_histories` / `building_memory_paths` 削除 (Phase 3)

直接 dict アクセスがゼロだったため、Layer 1 集約が比較的やりやすい。これは設計上の幸運。

## 関連ドキュメント

- `docs/intent/unified_memory_architecture.md`: ペルソナ個人記憶 (SAIMemory) の統一設計。本ドキュメントは「場所の記憶」を扱い、別軸として並走
- `memory/project_building_memory_db_proposal.md` (Claude メモリ): 2026-04-26 時点の初期提案。本ドキュメントの土台
- `manager/history.py`, `persona/history_manager.py`: 現行実装
- `saiverse/occupancy_manager.py`: 移動と event 発火
- `builtin_data/tools/get_building_messages.py`: auto_ingest (DB 化後も基本構造は維持)
