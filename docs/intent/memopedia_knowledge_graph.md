# Intent: Memopedia ナレッジグラフ化

## これは何か

Memopedia のデータモデルを、現行の「ページにノートが縦積みされるフラット構造」から、**Fragment（知識の最小単位）を中心としたナレッジグラフ構造**に再設計する構想。Fragment はエンティティ（何について）と Chronicle（どの文脈で）の両方に紐づき、embedding 検索による想起と文脈復元を実現する。

Memopedia 固有の概念は Fragment とエンティティの2つだけ。時系列軸のグルーピング（テーマ）は Chronicle の統合階層がそのまま担うため、Memopedia 側に別途テーマノードを持たない。

## 背景と動機

### 現行 Memopedia の問題

Chronicle 生成時に会話からエンティティを抽出し、ページに追記する仕組み（unified_memory_architecture.md Phase 2b）は基本動作としては機能しているが、運用上4つの問題が顕在化した。

**① 会話当事者ページへのノイズ蓄積**

「まはー」「ソフィー」など会話当事者のページに、一過性の行動記録が大量に書き込まれる。

```
悪い例（現状）:
- ローカル環境で発生したデータベース接続時のSSLエラーに直面している。
- マイグレーション実行時の権限エラーを解消するためにコンテナを再構築した。

良い例（あるべき姿）:
- 誕生日は1月14日。
- 誠実さを重視し、挑戦と成長を大切にしている。
```

一過性の行動記録は Chronicle の仕事であり、Memopedia には静的属性（誕生日、好み、価値観、関係性等）のみが記録されるべき。

**② 不要ページの乱立**

一般名詞（「OCR」「Docker」）、ファイル名（`20240108234540_setup.sql`）、CLIコマンド（`pg_ctl`）など、知識ベースとして維持する価値のないページが大量に生成される。air_city_a の実績では74ページ中、半数以上がこの類。

**③ 同一内容の重複書き込み**

書き込み時に既存ページの content 全文を読まないため、ほぼ同じ内容が同日の別バッチで繰り返し追記される。同一日付ヘッダの重複も発生。

**④ 古い詳細情報の滞留**

ChatbotUI 導入期の詳細（PostgreSQL の設定ファイル操作、Supabase マイグレーションの試行錯誤等）がページに残り続ける。現在は SAIVerse に完全移行済みで、これらの詳細を保持する必要はない。ページインデックスから落とすことで**表面上は忘れたように見える**が、関連する話題が出れば Fragment 検索で再浮上する。

### 構造的な限界

問題①②はプロンプト改善で軽減できるが、③④はデータモデル自体の限界に起因する。現行構造ではページ内の個々の記述（ノート）が独立した単位として扱われず、時系列との紐付けもない。Fragment 化により個々の記述が独立した検索可能な単位となり、④はページインデックスの忘却で対処する。

## 設計思想

### Chronicle との連携

Chronicle は時系列データを階層的に圧縮する仕組み:
- 生ログ → Lv-1 あらすじ → Lv-2 統合 → ...
- 古い出来事ほど粗い粒度になる
- 詳細は失われるが、「そういう時期があった」という記憶は残る

Memopedia はこれと**対称ではなく補完**する:
- Fragment は蓄積専用。Chronicle のように段階的に消えていくことはない
- Fragment を Chronicle Lv-1 にリンクすることで、想起時に「そのとき何があったか（あらすじ）」と「同じ文脈で書かれた別の知識（共起 Fragment）」を芋づる式に復元できる
- 忘却の対象は Fragment ではなくページ（エンティティ）のインデックス掲示。最近参照されないページは表面（常時コンテキスト注入）から落ちるが、Fragment embedding 検索でヒットすれば復帰する

### 人間の記憶のメタファー

| メタファー | 対応する仕組み |
|-----------|---------------|
| 普段は忘れてるが、関連する話が出たら思い出す | Fragment embedding 検索 → エンティティページ想起 |
| 思い出したら芋づるで色々出てくる | chronicle_entry_id → 同時期の Fragment + あらすじ |
| 最近の出来事ほど思い出しやすい | ページインデックスの最終参照順 |
| 重要な記憶はいつでも思い出せる | is_important フラグ、常駐コアメモリ |
| 「何があったか」と「何を知っているか」は別 | Chronicle（体験の流れ）と Memopedia（知識の辞書）の棲み分け |

## Fragment-Entity グラフ構造

### 2種のノードと Chronicle 紐付け

```
[エンティティ]               [Chronicle 階層]
  PostgreSQL ───── fragment A ───── Lv-1 entry ──┐
  PostgreSQL ───── fragment B ───── Lv-1 entry   ├── Lv-2 entry (= テーマ)
  Supabase   ───── fragment C ───── Lv-1 entry ──┘
  まはー     ───── fragment D ───── (紐付けなし: 静的属性)
```

テーマ軸のグルーピングは Chronicle の統合階層がそのまま担う。Fragment → Chronicle Lv-1 → Lv-2 → Lv-3+ の既存チェーンを辿れば「どの時期・どの文脈の知識か」が分かるため、Memopedia 側に別途テーマノードを持つ必要がない。

**Fragment（最小知識単位）**:
- 1つの事実・属性・状態変化を表す短い文
- 例: 「PostgreSQL の pg_hba.conf を編集して認証方式を変更した」
- 必ず1つ以上のエンティティノードに属する
- 生成元の Chronicle Lv-1 エントリに紐づく（静的属性など Chronicle 由来でないものは紐付けなし）
- embedding を持ち、検索対象になる

**エンティティノード**:
- 現行の Memopedia ページに相当
- 固有名詞で識別される対象物（人物、AI、プロジェクト等）
- summary（一文定義）+ content（自由形式の説明文）+ Fragment 群の3本立て
- Fragment 群は同じ物事に対する知識断片を時系列縦断で集めたもの
- content は Fragment とは独立した、ページ固有の自由記述領域として維持

### 既存構造との関係

| 現行 | 新構造 |
|------|--------|
| ページ | エンティティノード |
| ページ内の日付ヘッダ下の箇条書き1行 | Fragment |
| （存在しない） | Chronicle 統合階層がテーマ軸を担う |
| ページの親子構造 | エンティティノード間の親子関係（維持） |

### Fragment のライフサイクル

```
1. 抽出: Chronicle Lv-1 生成時のバッチコールバックで Fragment を生成
         → エンティティノードに紐付け
         → 生成元の Chronicle Lv-1 エントリに chronicle_entry_id で紐付け

2. 蓄積: Fragment は削除されない。ページ指定の recall で全 Fragment が返る

3. 想起: embedding 検索で Fragment がヒット
         → 所属エンティティノードの情報を想起
         → chronicle_entry_id → 当時の Chronicle Lv-1（あらすじ）を復元
         → 同じ chronicle_entry_id を持つ他の Fragment も芋づる想起
```

Fragment は圧縮・忘却の対象ではない。忘却は**ページ（エンティティ）のインデックス掲示**で制御する。最近参照されないページは表面から落ちるが、Fragment embedding 検索でヒットすれば復帰する。

## 抽出段階の改善

### プロンプト改善

**会話当事者の扱い（①対策）**:

エンティティ抽出プロンプトに、会話当事者（ペルソナ自身・ユーザー・他の既知AI）については**静的属性のみ**を抽出する指示を追加する。

```
会話当事者（ペルソナ自身、ユーザー、頻繁に登場するAI）について:
- 記録すべき: 誕生日、好き嫌い、価値観、関係性、恒久的な特徴
- 記録すべきでない: 今やっている作業、直面しているエラー、一時的な状態
  （これらは Chronicle が記録する）
```

**エンティティ定義の厳格化（②対策）**:

現行プロンプトの「固有名詞を持つ対象」に加え、明示的な除外条件を追加する。

```
エンティティではないもの（追加）:
- ファイル名やパス（setup.sql, pg_hba.conf 等）
- CLI コマンド（pg_ctl, psql, supabase db push 等）
- 汎用的な技術用語で、この会話固有の文脈なしに定義できるもの（OCR、Docker、Python 等）
- その場限りのエラーメッセージやステータス
```

### 同日マージ（③の部分対策）

`reflect_to_memopedia` の `append_to_content` 時に、既存 content 末尾の日付ヘッダを確認し、同日であれば新しいヘッダを付けずに追記する。単純な文字列処理で実装可能。

### Fragment 重複検出（③対策）

Fragment に embedding を持たせることで、新規 Fragment 生成時に既存 Fragment との類似度を計算し、高類似度の Fragment は追加をスキップできる。

## 忘却メカニズム

### Fragment は忘却しない

Fragment は蓄積専用であり、圧縮・不可視化の対象ではない。ページ指定の recall では常に全 Fragment が返る。

**理由**: Memopedia はページ単位で想起される（「まはーについて思い出して」「PostgreSQL のこと教えて」）。ページ内の Fragment を間引くと recall の情報量が劣化する。Chronicle のように「古い出来事をぼかす」必要はない — 知識は古くても正確であることに価値がある。

### ページインデックスの忘却

忘却の対象は**ページ（エンティティ）のインデックス掲示**。常時コンテキストに注入されるのは各カテゴリ最大100件のタイトル + サマリであり、最終参照/書き込み順でソートされる。長期間参照されないページは表面から落ちるが:

- Fragment embedding 検索でヒットすれば自動的に復帰する
- is_important フラグを持つページは常駐する
- ページや Fragment 自体は削除されない

## 想起メカニズム

### 想起の流れ

**Fragment embedding 検索（メイン経路）**:

```
ユーザー発言 / 会話コンテキスト
  → unified_recall で Fragment embedding 検索
  → ヒットした Fragment の所属エンティティノードを特定
  → エンティティノードの全情報（summary + 全 Fragment）をコンテキストに取り込み
  → chronicle_entry_id → 当時の Chronicle Lv-1 あらすじも取得（文脈復元）
  → 同じ chronicle_entry_id を持つ他の Fragment → 共起知識の芋づる想起
```

Fragment 単位の embedding は、ページ単位と比べて検索精度が高い。「SSL エラー」で検索すれば、PostgreSQL のエンティティに紐づく SSL 関連の Fragment がヒットし、そこから PostgreSQL の全情報が芋づる式に想起される。さらに chronicle_entry_id を辿れば、同じ会話バッチで抽出された Supabase の Fragment なども一緒に浮上する。

### 普段のコンテキスト構成

Fragment 検索による想起が実用的になることで、普段からコンテキストに載せるべき情報は最小限になる:

- **常駐（コアメモリ）**: ペルソナのアイデンティティに関わる is_important なエンティティ情報（ユーザーとの関係性、大切な約束等）
- **インデックス**: 各カテゴリ最大100エンティティの title + summary を、最終参照/書き込み順で掲示。ペルソナが「自分がどんな知識を持っているか」を自覚するための目次。最終参照が古いページは自然にインデックスから落ちるが、Fragment embedding 検索でヒットすれば復帰する
- **それ以外**: Fragment embedding 検索に委ねる。普段はコンテキストに載らないが、関連する話題が出れば随時想起される

## データモデル

### Fragment テーブル（新設）

```sql
CREATE TABLE IF NOT EXISTS memopedia_fragments (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,           -- 1文の事実・属性記述
    entity_id TEXT NOT NULL,         -- 所属エンティティノード
    chronicle_entry_id TEXT,         -- 生成元の Chronicle Lv-1 エントリ（静的属性はNULL）
    source_date TEXT,                -- 抽出元の日付（YYYY-MM-DD）
    created_at INTEGER NOT NULL,
    FOREIGN KEY (entity_id) REFERENCES memopedia_pages(id)
);
CREATE INDEX idx_fragments_entity ON memopedia_fragments(entity_id);
CREATE INDEX idx_fragments_chronicle ON memopedia_fragments(chronicle_entry_id);
```

**vividness カラムは廃止**。Fragment は忘却対象ではないため不要（既存データの vividness カラムは残置しても害はないが、新規コードでは参照しない）。

### Fragment embedding テーブル（新設）

```sql
CREATE TABLE IF NOT EXISTS memopedia_fragment_embeddings (
    fragment_id TEXT PRIMARY KEY,
    vector BLOB NOT NULL,
    FOREIGN KEY (fragment_id) REFERENCES memopedia_fragments(id)
);
```

テーマノードテーブルは不要。Chronicle 統合階層（arasuji_entries テーブル）がテーマ軸を担う。Fragment → chronicle_entry_id → arasuji_entries の parent/child 関係で辿れば「どの時期・どの文脈の知識か」が分かる。想起時には chronicle_entry_id を使って当時のあらすじと共起 Fragment を芋づるで取得する。

### 既存テーブルへの変更

**memopedia_pages（エンティティノード）**: 基本構造は維持。**content フィールドは廃止しない**。

- **content の役割変更**: 自動蓄積される個別知識は Fragment が担うため、content は「ペルソナや人間が自由に書いた静的な説明文」として位置付ける。手動で書かれた長文の既存 content はそのまま維持される
- **summary**: エンティティの一文定義。インデックス掲示に使用
- **Fragment 群**: Chronicle 連動で自動蓄積される個別の知識断片。content とは別の仕組みとして並行稼働する
- **想起時の検索**: content にはページ単位の既存 embedding を維持、Fragment は Fragment 単位の embedding で検索。両経路からヒットしうる
- **移行**: 既存の日付ヘッダ + 箇条書き形式の content は機械的に Fragment に分解可能（移行スクリプトで対応）。自由形式の長文 content は無理に Fragment 化せずそのまま残す

## 既存システムとの関係

### unified_memory_architecture.md との位置付け

本ドキュメントは Phase 2b（エンティティ抽出と Memopedia 自動蓄積）の発展的再設計。Phase 2b の基本方針（エンティティ中心の抽出、Chronicle バッチコールバックでのトリガー）は維持しつつ、データモデルを Fragment ベースに刷新する。

Phase 2c（統一記憶探索）の recall_entry / recall_navigate も、Fragment embedding を検索対象に追加する形で拡張される。

### 移行戦略

既存の Memopedia データ（ページ内の日付ヘッダ + 箇条書き）は、機械的に Fragment に分解できる。各箇条書き行を1 Fragment として切り出し、日付ヘッダから source_date を、ページ ID から entity_id を設定する。

## 守るべき不変条件

### 1. Fragment は最小知識単位として独立する

Fragment は特定のページの「中身」ではなく、独立したデータとして存在する。エンティティノードは Fragment のグルーピングビューの側面を持つが、同時にエンティティ固有の content（自由形式の説明文）も保持する。content と Fragment は役割が異なり共存する（後述「既存テーブルへの変更」参照）。

### 2. Fragment は削除・圧縮しない

Fragment は蓄積専用。recall 時には常に全 Fragment が返る。忘却はページのインデックス掲示で制御し、Fragment 単位では行わない。

### 3. Fragment の embedding は常に検索可能

ページがインデックスから落ちても、Fragment の embedding は検索対象として残る。関連する話題が出れば、インデックスに載っていないページの Fragment もヒットし、ページごと復帰する。

### 4. エンティティノードは固有名詞で識別する

既存の不変条件（unified_memory_architecture.md §11）を継承。抽象テーマのページは作らない。テーマ軸のグルーピングは Chronicle 統合階層が担う。

### 5. Fragment は Chronicle Lv-1 に紐づく

Fragment の生成元を辿れば Chronicle Lv-1 に到達し、そこから生ログまで辿れる。想起時には chronicle_entry_id を使って当時のあらすじと共起 Fragment を取得する。

### 6. エンティティ間の関係性は明示的に記述しない

エンティティ間の関係は、Fragment の共起（同じ chronicle_entry_id を持つ）として暗黙的に保持する。明示的な関係エッジは持たない。想起時に chronicle_entry_id で芋づる検索すれば、同じ文脈で抽出された他のエンティティの Fragment も一緒に浮上する。

## 設計判断（議論で確定済み）

### Fragment 抽出

- 現行の notes 抽出ロジックをそのまま使い、格納先を Fragment テーブルに変更する（1 note = 1 Fragment）
- 1 Fragment は 1 エンティティに所属（現行の抽出構造と同じ）
- 会話当事者の静的属性/一過性情報の判定はプロンプト指示で LLM に委ねる

### 忘却の制御

- Fragment は忘却しない。蓄積専用。recall で常に全件返る
- 忘却はページのインデックス掲示で制御。最終参照/書き込み順で各カテゴリ最大100件
- is_important はエンティティレベルの属性。is_important なページはインデックスから落ちない（常駐）
- vividness カラムは廃止（既存データは残置、新規コードでは不使用）

### 想起

- Fragment embedding は生成時に即座に計算（Chronicle バッチコールバック内で一連の流れ）
- 想起時: ヒットした Fragment → エンティティの全情報 + chronicle_entry_id → あらすじ + 共起 Fragment
- unified_recall は既存のページ embedding（content 用）と Fragment embedding を併存させる

### 移行

- 日付ヘッダ + 箇条書き形式の既存 content をパースして Fragment に分解（移行スクリプト）
- Chronicle Lv-1 との紐付けは source_date の時間範囲マッチング（完全一致は保証しない、紐付け不能なら chronicle_entry_id = NULL）
- 自由形式の長文 content はそのまま残す（Fragment 化しない）

### エンティティの整理

- インデックス掲示は各カテゴリ最大100件、最終参照/書き込み順。101件目以降は検索想起でのみアクセス
- エンティティ統廃合の能動的パスは設けない。参照されないエンティティはインデックスから自然に落ちる
- 上限値（100件/カテゴリ）は運用で調整

## 未決事項

- インデックス上限の最適値は実運用データで検証が必要（初期値100件/カテゴリ）
- 既存データの移行スクリプトの具体的な実装（パーサーの仕様等）は実装フェーズで詰める

## 関連ファイル

| ファイル | 役割 |
|---------|------|
| `sai_memory/memory/entity_extractor.py` | エンティティ抽出 + Memopedia 反映 |
| `sai_memory/memopedia/storage.py` | Memopedia ページの CRUD |
| `sai_memory/memopedia/core.py` | Memopedia 公開 API |
| `sai_memory/arasuji/generator.py` | Chronicle 生成（バッチコールバック） |
| `sai_memory/unified_recall.py` | 統合想起（embedding 検索） |
| `docs/intent/unified_memory_architecture.md` | 統一記憶アーキテクチャ（親ドキュメント） |

## 変更履歴

- **v0.1 (2026-05-27)**: 初版ドラフト。まはーとの設計議論を基に構想を記述
- **v0.2 (2026-05-29)**: Fragment の vividness / 圧縮メカニズムを廃止。Fragment は蓄積専用に変更。忘却対象はページインデックス掲示のみ。chronicle_entry_id の用途を「圧縮トリガー」から「想起時の文脈復元 + 共起 Fragment の芋づる想起」に修正
