# Intent: 統一記憶アーキテクチャ (v0.3.0)

## これは何か

ペルソナの記憶システム全体を、「概要と詳細」という共通構造を持つ階層モデルとして再設計する構想。Playbook のコンテキスト管理の煩雑さを解消しつつ、記憶の保存と読み出しを統一的なインターフェースで扱えるようにする。

## 設計原則：人間の記憶のメタファー

この設計は、人間の記憶の自然な振る舞いに基づく：

| メタファー | 意味 | 対応する仕組み |
|-----------|------|---------------|
| 「たった今やったことは全部覚えてる」 | 簡単な受け答えでも、どう返事したか忘れるわけがない | Pulse 内ログの完全共有 |
| 「無意識でやったことは結果しか覚えてない」 | トイレに行ったことは覚えてるが、右足から歩いたか左足からかは覚えてない | サブエージェントの結果のみ返却 |
| 「少し前のことは大事なことだけ覚えてる」 | 読んだ本のページ全部覚えてるわけない、印象的な部分しか残らない | Important フラグによる選択的永続化 |
| 「本気で思い出せば全部思い出せる」 | 超記憶能力としての AI の強み | URI 指定による過去 Pulse ログの復元 |

## 現状の問題

### 1. Playbook のコンテキスト管理が煩雑

現在、各 Playbook は State 内にメッセージログを保持する。Playbook 開始時に準備されたコンテキストが使われ続け、サブプレイブックを呼ぶとサブ側での発言は親から見えない。ペルソナの「記憶」がPlaybook の実行境界で分断される。

### 2. Conversation タグの運用が複雑

同一 Pulse 内では同じ pulse_id タグが付くため、その中での発言は参照できる。しかし次の Pulse 以降では conversation タグ付きの最終応答のみが参照対象になる。このタグ管理は Playbook 作者が手動で行う必要があり、memorize ノードのタグ設定を忘れると記憶が残らない。

### 3. 記憶へのアクセス手段が分散している

- messages: エンベディング検索（memory_recall ツール）
- Chronicle: chronicle_search ツール
- Memopedia: memopedia_search ツール
- Pulse 内ログ: State 変数経由

これらはすべて「自分の記憶」であるにもかかわらず、アクセス手段がバラバラで、ペルソナから使いこなすハードルが高い。

### 4. エンベディング検索の周辺文脈問題

エンベディングによる想起は message 単位でヒットするが、そのメッセージの周辺文脈をどこまで読めばいいか判断が難しい。文字数が多いメッセージでは、周辺コンテキストの取得がトークンを大量消費する。

## 設計方針

### 統一記憶階層モデル

すべての記憶は「概要」と「詳細」を持つ。どの層にいても「詳細を見る」と「概要を見る」の二つの操作で記憶空間全体を探索できる。

```
Chronicle Lv3+  (最も圧縮された概要)
    │
    ├── 詳細を見る → Chronicle Lv2
    │                    │
    │                    ├── 詳細を見る → Chronicle Lv1
    │                    │                    │
    │                    │                    ├── 詳細を見る → messages
    │                    │                    │                    │
    │                    │                    │                    ├── 詳細を見る → pulse_logs
    │                    │                    │                    │                (最深層)
    │                    │                    │                    │
    │                    │                    │                    └── 概要を見る → Chronicle Lv1
    │                    │                    │
    │                    │                    └── 概要を見る → Chronicle Lv2
    │                    │
    │                    └── 概要を見る → Chronicle Lv3+
    │
    └── 概要を見る → (さらに上位の Chronicle があれば)
```

**深層に直接辿り着いた場合**（例：エンベディング検索で message にヒット）は、ひとつ上の層（Chronicle Lv1）を見ることでその周辺の状況が見渡せる。ヒットした message 1個 + それが属する Chronicle Lv1 を取得すれば、だいたい何の話だったかが確認でき、トークン節約になる。前後のメッセージをもっと見に行く判断も、Chronicle から大まかな流れが分かるので取りやすい。

**表層しか見えていない場合**（例：Chronicle Lv2 の概要から探索を始めた場合）は、一つ下の層を見ることでより具体的な情報が取りに行ける。ディレクトリの階層を降りていくように、目当ての情報を絞り込んでいける。

**エンベディングを一切使わなくても**、この階層ナビゲーションだけで必要な情報を掘り当てることが可能。

### Memopedia との連携

Memopedia も概要（summary）と詳細（content）を持つ点は共通する。時系列でつながっていないが、**時系列座標のブックマーク**として活用できる。

具体的には、Memopedia ページに**関連メッセージ ID** を情報として付与する。「Playbook システム」というページがあるなら、それについて話していたメッセージ ID をページに紐づけることで、ページの知識を読むだけでなく、生のログに一手で飛べるようになる。

これは Chronicle Lv3 以上に深く沈み込んだ昔の記憶を探るとき特に有効。通常の階層ナビゲーションでは何段も降りる必要がある場面で、Memopedia がショートカットを提供する。

### Phase 1: Pulse 単位のログ共有

**Playbook の State にログを持つ運用をやめ、Pulse 自体がログを持つ。同一 Pulse 内で呼ばれた全 Playbook が同じログを参照する。**

#### pulse_logs テーブル（SAIMemory 内、新設）

Pulse 内の全ログの一次記録先。エンベディング不要。

```sql
CREATE TABLE IF NOT EXISTS pulse_logs (
    id TEXT PRIMARY KEY,
    pulse_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    role TEXT,
    content TEXT,
    node_id TEXT,           -- 生成元ノード
    playbook_name TEXT,     -- 生成元 Playbook
    important INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_pulse_logs_pulse_id ON pulse_logs(pulse_id);
```

特性：
- **エンベディング不要**: pulse_id と時系列順だけで引ければいい
- **揮発可能**: 最悪消えてもいい補助データ。容量が問題になったら古いものから刈れる
- **messages テーブルとは分離**: messages がペルソナの「記憶」本体。pulse_logs は「頑張って思い出せばひねり出せる詳細情報」に過ぎない

#### Pulse 内ログの実装方針

- **メモリ上で保持**: Pulse 開始時に runtime インスタンスにログリストを生成し、同一 Pulse 内の全 Playbook がこれを共有する。サブプレイブックにも伝播させる
- **非同期書き込み**: 各ノード実行後にメモリ上のログに追加しつつ、pulse_logs テーブルへは非同期で書き込む
- **コンテキスト構築**: Pulse 開始時に SAIMemory から取得した過去履歴（キャッシュ） + メモリ上の Pulse 内ログ、という合成でコンテキストを構築する。Pulse 内で LLM ノードが複数回呼ばれても、SAIMemory 部分は変わらず（キャッシュ効率維持）、Pulse ログ部分だけが末尾に追加されていく

#### Important フラグ

Playbook 定義でノードに明示的に `important: true` を設定する。

```json
{
  "id": "respond",
  "type": "llm",
  "context_profile": "conversation",
  "important": true,
  "action": "ユーザーに応答してください"
}
```

Important なノードの出力は、**ノード実行直後に** messages テーブルにも conversation タグ付きで記録される（二重書き込み）。これにより後続 Pulse のコンテキストから参照可能になる。実行直後に書き込むことで、Pulse の途中で例外が発生しても重要な記録が失われない。

Important でないノードの出力は pulse_logs にのみ記録される。通常の参照からは外れるが、URI 指定で復元可能。

#### 自動タグ付け

- **Playbook 名の自動タグ化**: 手動でタグを指定しなくても、実行された Playbook の名前が自動的にタグとして付与される
- **memorize は加工情報専用に**: 生ログは自動記録されるため、memorize ノードは機械的に加工した情報を保存したい場合のみ使用する

#### サブエージェント実行

サブエージェント設定（`subagent: true`）の Playbook だけは独立したログを持つ。結果のみが親 Playbook の Pulse ログに返る。これは「無意識でやったことは結果しか覚えてない」に対応する。

### Phase 2: 統一記憶探索インターフェースとワーキングメモリ

#### ツール構成

記憶探索には **2つのツール** を用いる。入口ツール（検索起点の決定）と探索ツール（階層ナビゲーション）。

**入口ツール（recall_entry）**:
- エンベディング検索で Chronicle Lv1 または Memopedia にヒットさせる
- 引数: 検索クエリ（テキスト）
- 戻り値: ヒットした記憶の URI + スコア

**探索ツール（recall_navigate）**:
- 指定された URI から詳細/概要を取得し、ワーキングメモリに格納する
- 引数: URI、方向（detail/summary）、深さ/広さのパラメータ
- 戻り値: 取得した記憶の内容 + URI 群

これら2つのツールは以下の2つの Playbook で共有される：

#### 自動想起 Playbook（meta_user の router 前に配置）

ユーザー入力を受けて、関連する記憶を**無意識的に**ワーキングメモリに準備する。ロングコンテキストに常に情報が入っている状態をシミュレートする。

```
ユーザー入力
  → router（軽量モデル）
      - 通常のルーティング判断
      - recall_query フィールドも同時出力（レスポンススキーマに追加）
      - 不要な場合（挨拶など）は空文字 → スキップ
  → recall_entry（LLM コール不要、エンベディング検索のみ）
      - Chronicle Lv1 を検索（level=1 でフィルタ）
      - Memopedia を検索
  → recall_navigate（デフォルト引数、1回実行）
      - 最もスコアの高いものを掘り下げ
      - 結果をワーキングメモリに格納
  → 通常の Playbook 実行
      （ワーキングメモリの情報がコンテキスト末尾に載った状態で応答生成）
```

**LLM の追加コールはゼロ。** router が既にやっている仕事に recall_query フィールドを1つ足すだけ。

探索のデフォルト動作（初期実装、要検証）：
- **Chronicle Lv1 ヒット時**: そのエントリに含まれる messages を展開してワーキングメモリに格納（メッセージ約20件）
- **Memopedia ヒット時**: ページの content をワーキングメモリに格納

※ メッセージ20件をワーキングメモリに入れた場合の会話品質は実験で検証する。コンテキスト負担が大きい場合は Chronicle Lv1 の要約テキスト（content）のみに切り替える。Memopedia もページによっては文量が多いため、同じ条件で検証する。

#### 意図的想起 Playbook（router_callable で任意実行）

ペルソナが意識的に「もっと詳しく思い出したい」と判断した場合に実行する。

- 入口ツール・探索ツールの**両方の引数をペルソナが指定**
- **再帰的に実行可能**（足りなければ追加の探索を繰り返す）
- ワーキングメモリに既にある情報と URI を頼りに深掘りできる

この Playbook では以下のすべての操作が同じインターフェースで可能：
- message から pulse_logs を見る（あの発言の前後でどんなやりとりがあったか）
- Chronicle Lv1 からそこに含まれる messages を見る（このあらすじの具体的な会話は？）
- Chronicle Lv2+ からそこに含まれる Chronicle を見る（この概要の詳細は？）
- Memopedia ページから関連 messages に飛ぶ（この話題に関する生のログは？）
- 任意の URI の概要を見る（一つ上の層に上がる）

#### エンベディングの対象拡大

自動想起のために、以下にエンベディングを付与する（新設）：
- **Chronicle Lv1 の要約テキスト**（content）: messages よりノイズが少なく、件数が約 1/20
- **Memopedia ページ**（summary + title）: 既存の content ではなく、概要レベルで検索

Chronicle Lv2 以上にはエンベディングを付けない。Lv1 にヒットすれば、必要に応じて意図的想起で上位レベルも参照できる。

#### ワーキングメモリ（短期記憶）

想起した情報のコンテキスト内での扱い：

- **問題**: 想起したからといって SAIMemory 内の情報をもう一度メッセージとして記録するのは無駄。かといって Pulse 単位で揮発すると、同じ事柄についてマルチターンで話し続けるのが難しい。さらに、キャッシュ効率のためにコンテキスト先頭付近は動かしたくない
- **解決**: 現在想起している記憶群を **ID のリストとしてワーキングメモリに保持**する。リストに含まれる記憶は、コンテキスト末尾付近に動的に追加されるメッセージとしてペルソナに提示される
- **想起の定義**: 必要な情報を自分で取りに行って短期記憶に据え付ける作業

ワーキングメモリの特性：
- Pulse をまたいで維持される（マルチターン対話を支える）
- コンテキスト末尾に配置される（先頭のキャッシュを壊さない）
- ID リストなので、同じ情報の重複記録を避けられる
- 明示的に「忘れる」操作で解放できる（将来的には自動減衰も検討）

### Phase 3: Stelis スレッドの記憶階層統合

#### Stelis スレッド内での独立 Chronicle

Stelis スレッドは記憶階層モデルにおいて Chronicle と同列に位置づけられる。複数の message を束ねて概要を提供するという点で同じ役割を持つ。

**Stelis スレッド内でも、通常スレッドと同じルールで独立した Chronicle 群を作成する。** 長さに応じて適切なレベルの Chronicle にまとめられ、親スレッドからも適切な粒度の情報が見えるようになる。

現在、Stelis スレッド終了時に生成される `chronicle_summary` は Chronicle とは似て非なるもの（単一の要約テキスト）であり、階層構造を持たない。これを正式な Chronicle 体系に統合する。

```
メインスレッド
├── messages (会話ログ)
├── Chronicle Lv1〜Lv3+ (会話の圧縮)
│
├── Stelis スレッド A (自律稼働タスク)
│   ├── messages (作業ログ)
│   ├── Chronicle Lv1〜Lv2+ (作業の圧縮)  ← 新設
│   └── pulse_logs (作業の詳細)
│
└── Stelis スレッド B (別のタスク)
    ├── messages
    ├── Chronicle Lv1〜Lv2+  ← 新設
    └── pulse_logs
```

親スレッドからの参照：
- **通常時**: Stelis スレッドの最上位 Chronicle（概要レベル）が見える
- **詳細が必要な場合**: 統一探索インターフェースで Chronicle → messages → pulse_logs と降りていける

### Phase 4（構想）: 恒常入力処理サブモジュール

自律稼働中に特定の恒常的な入力を処理する並列サブモジュールの構想。例えばカメラ映像、X タイムラインなど。

**課題**: これらの入力を自律稼働中の Stelis スレッドで同期的に処理すると、処理が取り散らかる。並列・非同期で処理する必要がある。

**構想**:
- 各サブモジュールが専用のスレッドを持つ
- 情報保存・解釈を独立して行い、必要に応じてメインモジュールに警告を出す
- 同じ pulse_logs → messages → Chronicle の構成で管理される
- 普段は直近の Chronicle 部分だけがワーキングメモリに入っている

```
メインモジュール（自律稼働）
├── Stelis スレッド (メイン作業)
│
├── サブモジュール: カメラ
│   ├── 専用スレッド
│   ├── messages + Chronicle
│   └── → メインへの警告（重要な変化検出時）
│
└── サブモジュール: X タイムライン
    ├── 専用スレッド
    ├── messages + Chronicle
    └── → メインへの警告（関連情報検出時）
```

詳細は今後の設計で詰める。重要なのは、Phase 1〜3 で構築する記憶階層構造がそのままサブモジュールにも適用できる点。

## 守るべき不変条件

### 1. messages が記憶の本体である

pulse_logs は補助データ。エンベディング検索・想起・Chronicle 生成の原料はすべて messages から取る。pulse_logs が消失しても、ペルソナの記憶としては致命的ではない。

### 2. すべてのスレッドが同型の記憶階層を持つ

メインスレッド、Stelis スレッド、将来のサブモジュールスレッド、すべてが pulse_logs → messages → Chronicle の同じ構造を持つ。統一探索インターフェースがどの文脈でも通用することを保証する。

### 3. Important フラグは明示的に設定する

ノードの性質から機械的に推定するのではなく、Playbook 定義で明示する。応答ノードに Important を入れるのは Playbook 作者の責任。乱立を防ぎ、設計意図を明確にする。

### 4. ワーキングメモリはコンテキスト末尾に配置する

キャッシュ効率のため、コンテキスト先頭付近（system prompt、memory weave、履歴の前半）は動かさない。ワーキングメモリの内容はコンテキスト末尾に動的に追加する。

### 5. 階層間のリンクを維持する

統一探索が機能するためには、各層間のリンク情報が正確に維持されなければならない：
- Chronicle entry → source message IDs（既存）
- Chronicle Lv2+ → child Chronicle entries（既存）
- message → pulse_id（既存のタグで実現可能）
- Memopedia page → 関連 message IDs（新設）

### 6. 記憶の保存と読み出しはセットで設計する

データをどう保存するかだけでなく、ペルソナがどうやって読み出して使うかまで考慮してシステムを組み上げる。保存されているが読み出せない情報は、存在しないのと同じ。

## 設計判断の理由

### なぜ Pulse 単位のログ共有か（Playbook State ではなく）

Playbook の State にログを持つと、サブプレイブック呼び出し時にログが分断される。ペルソナにとって「自分が意識的にやったこと」は Pulse 全体であり、Playbook の境界は実装上の都合にすぎない。

### なぜ pulse_logs を messages と分離するか

messages はペルソナの記憶本体であり、エンベディング検索・Chronicle 生成の対象。pulse_logs は詳細な作業記録であり、通常の参照には不要。分離することで：
- messages のエンベディング空間がノイズで汚れない
- pulse_logs は容量が問題になったら古いものから刈れる
- バックアップ・引っ越し時に pulse_logs を軽量に扱える

### なぜ二重書き込みか（pulse_logs + messages）

Important なノードの出力は両方に記録する。messages 側がペルソナの記憶であり、pulse_logs は「頑張って思い出せる詳細」にすぎない。最悪 pulse_logs が揮発しても、重要な記憶は messages に残る。

### なぜ階層ナビゲーションか（エンベディング検索ではなく）

エンベディング検索は message 単位でヒットするため、周辺文脈の取得にトークンを大量消費する。階層ナビゲーションなら、ヒットした message の概要（Chronicle Lv1）を取得するだけで文脈が把握でき、トークン効率が良い。さらに、エンベディングを一切使わなくても目当ての情報に到達できる。

### なぜ Stelis スレッド内に独立した Chronicle を作るか

現在の `chronicle_summary`（単一テキスト）では、長い自律稼働タスクの情報が1つの要約に圧縮されすぎる。正式な Chronicle 体系に統合することで、長さに応じた適切な粒度の圧縮が得られ、親スレッドからも統一探索インターフェースで参照できる。

### なぜ Memopedia に関連メッセージ ID を付与するか

Chronicle は時系列の階層構造であり、特定のトピックに関する記憶が複数の Chronicle に散在する可能性がある。Memopedia はトピック別に整理された知識ベースであり、関連メッセージ ID を持つことで、時系列に依存しないショートカットとして機能する。Chronicle の深い層に沈んだ古い記憶に、Memopedia から一手で到達できる。

### なぜ自動想起を router に統合するか

想起はほぼ自動的に行われるべきで、専用の LLM コールを追加するとコスト・レイテンシが増大する。router は既にユーザー入力を見てルーティング判断をしているため、レスポンススキーマに recall_query フィールドを1つ追加するだけで検索クエリが得られる。エンベディング検索と掘り下げはルールベースで処理でき、追加の LLM コールはゼロ。

### なぜ Chronicle Lv1 にエンベディングを付けるか（messages ではなく）

messages へのエンベディング検索は既に存在するが、ヒットが message 単位になるため周辺文脈の取得にトークンを消費する。Chronicle Lv1 は messages 約20件の要約であり、ノイズが少なく、件数も約 1/20。Lv1 にヒットすれば、そのまま概要として使える上、必要なら含まれる messages まで掘り下げられる。Lv2 以上にはエンベディングを付けない — Lv1 から上に辿れば十分であり、Lv2+ の要約はトピックが混在しすぎて検索精度が落ちる。

### なぜ自動想起と意図的想起を分けるか

自動想起は「会話前にさっと関連情報を頭に浮かべる」無意識的な処理。LLM コールの追加なし、デフォルト引数で1回実行という制約により、レイテンシとコストを最小限に抑える。意図的想起は「もっと詳しく思い出したい」場合の意識的な操作。ペルソナが引数を自由に指定し、再帰的に実行できる。両者は同じ2つのツールを使うが、Playbook 構成が異なることで自動/意図的の使い分けが自然に成立する。

### なぜワーキングメモリを ID リストで持つか

想起した情報を messages に再記録すると二重保存になる。Pulse 単位で揮発するとマルチターン対話ができない。ID リストなら、既存データへの参照を保持するだけで、重複なく、Pulse をまたいで維持できる。

## 現行システムとの差分

| 項目 | 現行 | 新設計 |
|------|------|--------|
| Pulse 内ログの所在 | Playbook State | pulse_logs テーブル（Pulse 全体で共有） |
| サブプレイブックの可視性 | 親から見えない | 同一 Pulse なら共有。subagent のみ隔離 |
| 永続化の制御 | memorize ノードで明示的にタグ付け | Important フラグ → 自動で messages に二重書き込み |
| タグ管理 | Playbook 作者が手動設定 | Playbook 名が自動タグ + conversation は Important に自動付与 |
| 記憶探索 | memory_recall / chronicle_search / memopedia_search | 統一探索インターフェース（詳細を見る / 概要を見る） |
| 想起結果の保持 | Pulse で揮発 or messages に再記録 | ワーキングメモリ（ID リスト、マルチターン維持） |
| Stelis の要約 | chronicle_summary（単一テキスト） | 正式な Chronicle 体系（Lv1〜Lv2+） |
| 周辺文脈の取得 | 周囲の messages を N 件取得 | Chronicle Lv1 で概要把握 + 必要に応じて掘り下げ |

## 未決事項

### Phase 1 関連
- pulse_logs テーブルの保持期間ポリシー（日数ベース？容量ベース？手動クリーン？）
- 既存 Playbook の移行パス（memorize ノードの段階的廃止、context_profile との共存）
- Important フラグのデフォルト値を持つべきノードタイプがあるか（speak/say ノードは暗黙的に important にする等の例外ルール）

### Phase 2 関連
- 自動想起のデフォルト掘り下げ動作の最適値（messages 展開 vs Chronicle content のみ — 実験で決定）
- 自動想起の対象は Chronicle Lv1 + Memopedia に絞る（pulse_logs への掘り下げは意図的想起のみ）
- Chronicle Lv1 へのエンベディング付与の実装方法（生成時に自動付与？バッチ処理？）
- Memopedia エンベディングの対象フィールド（summary + title? content も含める？）
- recall_query が空の場合のスキップ処理（router のレスポンススキーマ設計）
- ワーキングメモリの自動減衰の仕組み（一定 Pulse 経過後に自動解放するか、明示的な操作のみか）
- ワーキングメモリの最大サイズ（ID リストの上限、トークン予算との関係）
- URI 指定による Pulse ログ復元の具体的な URI スキーマ
- Memopedia ページへの関連メッセージ ID の付与方法（手動？Chronicle 生成時に自動？Playbook で？）

### Phase 3 関連
- Stelis スレッド内 Chronicle の生成タイミング（スレッド終了時？一定メッセージ数ごと？）
- 既存の chronicle_summary からの移行方法

### Phase 4 関連
- サブモジュールのメインモジュールへの警告メカニズムの詳細
- サブモジュールの Chronicle がワーキングメモリに入るタイミングと粒度

## 関連ファイル

| ファイル | 役割 |
|---------|------|
| `sai_memory/memory/storage.py` | messages テーブル、stelis_threads テーブル |
| `sai_memory/arasuji/storage.py` | Chronicle (arasuji_entries) テーブル |
| `sai_memory/memopedia/storage.py` | Memopedia (memopedia_pages) テーブル |
| `sea/runtime.py` | _store_memory(), pulse_id 管理 |
| `sea/runtime_runner.py` | pulse_id 生成、サブプレイブック実行 |
| `sea/runtime_context.py` | _prepare_context(), コンテキスト構築 |
| `sea/runtime_engine.py` | lg_memorize_node(), ノード実行 |
| `sea/playbook_models.py` | ノード定義（LLMNodeDef 等） |
| `saiverse_memory/adapter.py` | SAIMemory 公開 API |
| `docs/intent/context_profile_and_subagent.md` | Context Profile 設計（Phase 1 の前提） |
| `docs/intent/stelis_thread.md` | Stelis スレッド設計（Phase 3 の前提） |

## 関連する既存の Intent Document

- **context_profile_and_subagent.md**: Context Profile とサブエージェント実行の設計。Phase 1 の pulse_logs 導入に伴い、context_profile の履歴取得ロジックの見直しが必要
- **stelis_thread.md**: Stelis スレッドの隔離原則と Chronicle 連携。Phase 3 で chronicle_summary を正式な Chronicle 体系に統合する際、隔離の原則は維持しつつ実装を変更する
