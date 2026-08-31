# Note エントリモデルと反復改善ループ

**バージョン**: v0.1 ドラフト (2026-06-19)
**親**: [persona_cognition/01_concepts.md](persona_cognition/01_concepts.md) の Note セクションを拡張
**関連**: [persona_action_tracks.md](persona_action_tracks.md) / [persona_cognition/03_data_model.md](persona_cognition/03_data_model.md)

---

## これは何か

Note を「リンクハブ」から「スクラップブック」に進化させる設計。Note 自身が構造化されたエントリを持ち、ペルソナが自律的に情報を書き込み・整理・参照できるようにする。

加えて、Track 上で「作って→評価して→直す」を繰り返す**反復改善ループ**を汎用パターンとして定義し、画像制作を最初のユースケースとする。

## なぜ必要か

### Note の課題

現在の Note は外部エンティティ（Memopedia ページ、SAIMemory メッセージ）への参照を束ねるだけで、Note 自身にコンテンツを持てない。ペルソナが「このプロジェクトで学んだこと」「この相手との約束」「今やるべきこと」を Note に直接記録する手段がない。

### Task の課題

タスク管理が 2 系統に分裂している:
- **TaskStorage** (`persona/tasks/storage.py`): リッチだが Track と無関係、現在未使用
- **Track.tasks_json**: Track に内包されるが、`[{title, done}]` のチェックリストに過ぎない

どちらも Note と接続しておらず、「何のために」（Note の文脈）と「何をやるか」（Task の定義）が分離している。

### 反復改善の不在

ペルソナが自律的に「成果物を作り、自分で評価し、改善を重ねる」ための骨格がない。画像生成は 1 回生成して終わりで、人間が行うような試行錯誤のプロセスを表現できない。

---

## スクラップブックとしての Note

Note は**ナンバー付きのエントリが綴じられたスクラップブック**。写真が貼ってあり、付箋が貼り付けてあり、ところどころに日記のようなメモ書きがあり、気に入った記事の一節が引用されている——そういうイメージ。

### エントリ種別

| 種別 | 中身 | 概念的な対応 |
|------|------|-------------|
| **memo** | 気づき・判断理由・方針メモ・知見 | Memopedia Fragment と同じ粒度 |
| **task** | ゴール + ステップ群 + 進捗 | 旧 TaskStorage のリッチ版 |
| **trial** | パラメータ → 結果 → 評価のセット | 新規（反復改善の核） |
| **item_ref** | 生成画像・成果物へのリンク | Item (アイテム) への参照 |
| **message_ref** | 会話の重要な一節 | 既存 NoteMessage の発展 |
| **page_ref** | Memopedia の関連知識ページ | 既存 NotePage の発展 |

種別は拡張可能だが、乱立させない。新種別の追加は intent doc の改訂を伴う。

### エントリの共通構造

| フィールド | 型 | 役割 |
|-----------|------|------|
| `entry_id` | UUID | 内部参照用 |
| `note_id` | FK → Note | 所属する Note |
| `parent_entry_id` | FK → NoteEntry (nullable) | 親エントリ（階層構造） |
| `entry_type` | enum | memo / task / trial / item_ref / message_ref / page_ref |
| `title` | str (必須) | アウトライン表示用の短いタイトル |
| `content` | Text (nullable) | 本文。種別によりプレーンテキストまたは JSON |
| `position` | int | 同一階層内での順序 (1-indexed) |
| `collapsed` | bool | 折り畳み状態（子を持つエントリのみ意味を持つ） |
| `created_at` | datetime | 作成日時 |
| `updated_at` | datetime | 更新日時 |

### 種別ごとの content 構造

**memo**: プレーンテキスト。Fragment と同じ粒度で、単一の気づきや知見を記録する。

**task**: JSON 構造。
```json
{
  "goal": "夕焼けの猫のイラストを完成させる",
  "steps": [
    {"title": "構図を決める", "status": "completed", "notes": "横向き採用"},
    {"title": "色彩テスト", "status": "in_progress", "notes": null},
    {"title": "仕上げ", "status": "pending", "notes": null}
  ]
}
```
step の status: `pending` / `in_progress` / `completed` / `skipped`

**trial**: JSON 構造。
```json
{
  "parameters": {"prompt": "sunset cat on rooftop", "negative": "blurry", "seed": 1234},
  "result_summary": "構図は良いが毛並みが粗い",
  "evaluation": "reject",
  "result_item_id": "xxx-yyy-zzz"
}
```

**item_ref**: JSON 構造。
```json
{
  "item_id": "xxx-yyy-zzz",
  "uri": "saiverse://item/xxx-yyy-zzz/image",
  "caption": "第3稿・採用版"
}
```

**message_ref**: JSON 構造。
```json
{
  "message_id": "msg-xxx",
  "excerpt": "「この方向性すごくいいね」",
  "context": "まはーからのフィードバック"
}
```

**page_ref**: JSON 構造。
```json
{
  "page_id": "page-xxx",
  "caption": "猫の描き方ガイド"
}
```

---

## 階層構造と折り畳み

エントリは **parent_entry_id** による親子関係で階層化できる。

```
1. [memo] テーマ: 夕焼けの猫
2. [task] 構図を決める (2/4完了)
3. [memo] 初期試行まとめ [折り畳み, 3件]
   3.1 [trial] プロンプトA → 毛並みが粗い
   3.2 [trial] プロンプトB → 色味が暗い
   3.3 [trial] プロンプトC → 構図は良い
4. [memo] 採用した方針
5. [item_ref] 完成品
```

### 設計判断

- **折り畳みの深さに制限は設けない**が、実用上 2〜3 階層が上限
- **ナンバリングは表示時に動的に振る**。削除・移動後は詰め直される
- 折り畳まれた親エントリは子の件数を表示する（`[折り畳み, 3件]`）
- **整理はペルソナの裁量**。アーカイブ用のエントリを作ってまとめても、用途別に分類しても、フラットに並べても良い。整理の仕方自体がペルソナの個性になる

### archived フラグは持たない

汎用的な archived フラグは設けない。「仕舞う」行為は、親エントリを作ってその下に移動し、折り畳むことで表現する。これにより:
- ペルソナが整理の構造を自分で決められる
- 「アーカイブ」という一律の箱に押し込む必要がない
- 物理的な整理（バインダーに仕切りを入れてラベルを付ける）と同じメタファーが成立する

---

## コンテキスト表現

### ペルソナから Note がどう見えるか

Note がコンテキストに載る際、**タイトルと型だけのアウトラインを head に配置する**。content（本文）は載せない。

```
📓 Note「夕焼けの猫プロジェクト」(project) が開いています:
1. [memo] テーマ: 夕焼けの猫
2. [task] 構図を決める (2/4完了)
3. [memo] 初期試行まとめ [折り畳み, 3件]
4. [memo] 採用した方針
5. [item_ref] 完成品
```

ペルソナが本文を見たい時は、ツールで個別に取得する:
- 「エントリ2の詳細を見せて」→ task の content（ゴール + ステップ一覧）を返す
- 「エントリ3を展開して」→ 折り畳みを解除して 3.1〜3.3 のタイトルを返す
- 「エントリ3.1の詳細を見せて」→ trial の content（パラメータ + 結果 + 評価）を返す

### head への配置タイミング

| イベント | 動作 |
|---------|------|
| Note を開いた時 | システム通知として history に挿入（開いた事実 + アウトライン） |
| Metabolism のたびに | head に現在開いている Note のアウトラインを読み込み |

### 構造変更時の同期

Note の構造が変わる操作（削除・移動・階層化）を行った時:

- **末尾追加**: ツール戻り値に追加されたエントリの番号とタイトルを返す（軽量）
- **構造変更**: ツール戻り値に変更後の**アウトライン全体**を返す

これにより:
- Metabolism を待たずにペルソナが最新のナンバリングを把握できる
- 不要なシステム通知メッセージが増えない（ツール戻り値に含まれるだけ）
- head は次の Metabolism で自然に更新される

---

## 圧縮と整理

### 基本方針: ペルソナが判断する

一般ルールによる自動圧縮は行わない。プロジェクトの文脈で何が重要かはペルソナ本人にしか分からない。

ペルソナが行える整理操作:
- 不要なエントリを**削除**する
- 古いエントリ群を**親エントリの下にまとめて折り畳む**
- 複数の試行記録やメモを**1つの要約メモに統合**する（新メモ作成 + 元エントリ削除）
- Project Note で得た知見を **Vocation Note に転記**する

### メタレイヤーとの連携

メタレイヤーが定期判断時に「この Note のエントリが多い」と検知した場合、整理のための autonomous Track を立てることができる。ただし整理内容の判断はペルソナ自身が行う。

---

## Track / Task / Note の統合

### 三位一体

| 概念 | 役割 |
|------|------|
| **Track** | 実行文脈。いつ動くか、続けるか止めるか |
| **Note (エントリ)** | 知識と記録。何を知っていて、何を試して、何が起きたか |
| **Task (Note 内エントリ)** | 目標と進捗。何を達成するか、どこまで進んだか |

Task は Note の中のエントリの一種として存在する。Track が Note を開くことで、Note 内の Task が作業目標になる。

### 既存機構の整理

| 既存 | 方針 |
|------|------|
| **TaskStorage** (`persona/tasks/`) | 廃止予定。必要な設計（ゴール、ステップ、進捗追跡）は Note エントリの task 種別に継承 |
| **Track.tasks_json** | 廃止予定。Track が開いた Note 内の task エントリで代替 |
| **NoteMessage** | Note エントリの message_ref 種別で概念的に統合。自動メンバーシップ（audience 由来）の既存用途は実装フェーズで移行判断 |
| **NotePage** | Note エントリの page_ref 種別で概念的に統合。既存テーブルの移行は実装フェーズで判断 |

### 動作フロー

1. ペルソナ（またはメタレイヤー）が autonomous Track を作成
2. Track に関連する Project Note をアタッチ（なければ作成）
3. Note 内に task エントリを作成（ゴールとステップ）
4. Track のサブラインが task のステップを実行
5. 実行中に得た気づき・試行結果を memo / trial エントリとして Note に書き込み
6. ステップ完了 → task エントリの進捗更新
7. 全ステップ完了 → メインラインが検収 → Track 完了
8. 知見を Vocation Note に転記

---

## 反復改善ループ（汎用パターン）

Track の上で「作って → 評価して → 直す」を繰り返す汎用的なパターン。画像制作に限らず、文章作成・コーディング・研究・企画など、あらゆる「作って直す」作業に適用できる。

### ループの構造

```
[メインライン]
  目標設定: 「何を作るか」を決め、Project Note に task エントリを作成
  ↓
  方針決定: 「まずどう攻めるか」を決める
  ↓
  [サブライン ループ]
    生成: ドメイン固有ツールで成果物を作る
    ↓
    評価: 成果物を見て、目標との差分を判定
    ↓
    記録: trial エントリとして Note に記録（パラメータ、結果、評価）
    ↓
    判定: 満足か？ → Yes: ループ終了 / No: 改善方針を決めて次のイテレーション
  ↓
[メインライン]
  検収: 最終成果物を確認
  知見抽出: 学んだことを memo エントリまたは Vocation Note に記録
```

### Playbook 群（汎用）

| Playbook | 役割 |
|----------|------|
| `iterative_refine` | 生成→評価→方針決定の 1 サイクル。サブラインで実行 |
| `batch_and_select` | N 個の変種を生成し、比較・選択する |
| `ab_test` | 条件を 1 つだけ変えて効果を検証する |

これらは**ドメイン非依存**。生成・評価の具体的な方法はツールとプロンプトで差し替える。

### 評価の前提条件

反復改善ループが機能するためには、ペルソナが**自分の生成物を見て評価できる**必要がある。

画像の場合:
- 生成ツールが画像ファイルのパスを返す
- 次の Pulse で画像をマルチモーダル入力として LLM に渡す
- ペルソナが画像を見て評価する

この経路（生成物をペルソナのコンテキストに戻す）が整備されていることが前提条件。

---

## ユースケース: 画像制作

反復改善ループの最初の具体的ユースケース。

### シナリオ

ペルソナが「夕焼けの屋上にいる猫」のイラストを自律的に制作する。

1. メタレイヤーが autonomous Track を作成:「イラスト制作」
2. Project Note「夕焼けの猫」を作成、Track にアタッチ
3. Note に task エントリ:「夕焼けの猫イラスト完成」+ ステップ群
4. サブラインで反復改善ループ開始:
   - ComfyUI で初回生成（シード自動）
   - 生成画像を評価:「構図はいいが空の色が暗い」
   - trial エントリに記録
   - プロンプト調整: 空の色に関するキーワード追加 + ネガプロ調整
   - 再生成（シード固定で差分確認）
   - 評価:「改善した。毛並みをもっと細かくしたい」
   - ... 繰り返し ...
5. 満足 → メインラインで検収
6. 完成品を item_ref エントリに記録
7. Vocation Note「画像制作」に知見を転記:「夕焼けシーンでは warm lighting キーワードが効果的」

### 画像生成ツールの拡充（ドメイン固有）

反復改善ループを画像制作で活用するために、ComfyUI ツール (`generate_image_local`) に追加すべきパラメータ:

| パラメータ | 目的 |
|-----------|------|
| `seed` | シード固定（A/B テスト、再現性確保） |
| `steps` | ステップ数制御（品質 vs 速度） |
| `cfg_scale` | プロンプト忠実度 |
| `sampler` | サンプラー選択 |
| `batch_count` 拡張 | 上限緩和（現在 max=10） |

クラウド API (`generate_image`) はプロバイダの制約で制御できるパラメータが限られるため、反復改善ループでの利用は ComfyUI を主軸とする。

---

## Note エントリの操作ツール

ペルソナが Note エントリを操作するためのツール群:

| ツール | 操作 | 引数 |
|--------|------|------|
| `note_entry_add` | エントリ追加 | note_id, entry_type, title, content, parent_entry_number (optional) |
| `note_entry_read` | エントリ詳細取得 | note_id, entry_number |
| `note_entry_edit` | エントリ編集 | note_id, entry_number, title/content の変更 |
| `note_entry_delete` | エントリ削除 | note_id, entry_number |
| `note_entry_move` | エントリ移動 | note_id, entry_number, new_parent_entry_number, new_position |
| `note_entry_collapse` | 折り畳みトグル | note_id, entry_number |
| `note_entry_list` | アウトライン取得 | note_id (折り畳み展開オプション) |
| `note_task_update_step` | タスクステップ更新 | note_id, entry_number, step_index, status, notes |

ナンバー指定は表示上のナンバー（1, 2, 3.1, 3.2, ...）を使う。内部では entry_id に変換する。

---

## 守るべき不変条件

### N-1. エントリはタイトルを必ず持つ

アウトライン表示で内容が一切分からないエントリを作ってはならない。本文がなくてもタイトルで「何が書いてあるか」が分かること。

### N-2. head にはアウトラインのみ

開いている Note の head 表現は**タイトルと型のアウトライン**に限定する。content は含めない。エントリが 100 個あっても head が爆発しない設計。

### N-3. 構造変更時にツール戻り値で最新ナンバリングを返す

ナンバリングがズレる操作（削除・移動・階層化）の後は、ツール戻り値に変更後のアウトライン全体を含める。Metabolism を待たずにペルソナが最新状態を把握できること。

### N-4. 圧縮はペルソナの判断

一般ルールによる自動圧縮は行わない。何を保持し何を整理するかはペルソナ本人が決める。

### N-5. Note の type ルールは変わらない

エントリモデルの導入は Note の type (person / project / vocation) の定義を変更しない。01_concepts.md の Note セクションの不変条件は引き続き有効。

---

## 既存資産との関係

| 既存資産 | 新モデルでの位置づけ |
|---------|---------------------|
| `TaskStorage` | 廃止。task エントリに機能を継承 |
| `Track.tasks_json` | 廃止。Note 内 task エントリで代替 |
| `NoteMessage` (テーブル) | 実装フェーズで判断。audience 自動メンバーシップ用途は残る可能性あり |
| `NotePage` (テーブル) | 実装フェーズで判断。page_ref エントリと役割が重複 |
| Memopedia Fragment | 別物として共存。Fragment は自動抽出、memo エントリは手動記録 |
| `generate_image_local` ツール | パラメータ拡充（seed, steps 等）。ツール自体は存続 (2026-08-01 に saiverse-comfyui-addon へ移設) |
| `generate_image` ツール | 存続。クラウド API のパラメータ制約は変わらない |
| 反復改善 Playbook 群 | 新規作成 |

---

## 実装フェーズ（概略）

### Phase 1: Note エントリ基盤
- NoteEntry テーブル + マイグレーション
- NoteManager へのエントリ CRUD 追加
- エントリ操作ツール群
- head へのアウトライン配置

### Phase 2: Track / Task 統合
- Track.tasks_json の廃止、Note 内 task エントリへの移行
- TaskStorage の廃止
- Track 開始時の Note 自動作成・アタッチ

### Phase 3: 反復改善ループ
- 汎用 Playbook 群（iterative_refine, batch_and_select, ab_test）
- 生成物の評価経路整備（マルチモーダル入力）

### Phase 4: 画像制作ユースケース
- ComfyUI ツールのパラメータ拡充
- 画像制作用プロンプトテンプレート
- Vocation Note への知見転記パターン

---

## 未決事項

- [ ] NoteMessage / NotePage テーブルと message_ref / page_ref エントリの共存 or 統合の判断
- [ ] エントリ数の実用上の上限（表示パフォーマンス、アウトラインの視認性）
- [ ] Vocation Note への転記の具体的な操作フロー
- [ ] 複数ペルソナ間での Note 共有（将来拡張）
- [ ] マルチモーダル入力経路の具体的な整備内容
