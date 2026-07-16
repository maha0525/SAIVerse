# Intent: Track Chronicle (Track 内必要情報の維持機構)

**親 Intent**: [README.md](README.md)
**ステータス**: 起草 (v0.1, 2026-05-09)
**位置付け**: Phase 3 残件 (中断・再開機構の本体)
**関連**: [revisions.md](revisions.md) v0.31〜v0.32, [phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md), [phase_5_autonomy.md](phases/phase_5_autonomy.md), [`docs/issues/general_chronicle_metabolism_trigger.md`](../../issues/general_chronicle_metabolism_trigger.md), [`docs/issues/general_chronicle_user_pulse_only.md`](../../issues/general_chronicle_user_pulse_only.md)

---

## 1. なぜ作るか

### 出発点: pause_summary の限界

旧仕様には `action_tracks.pause_summary` カラムと `build_dynamic_section` の「### 前回までのサマリ」がそれぞれ用意されていたが、実際には:

- **書き込み側がそもそも実装されていなかった** (`saiverse/meta_layer.py:28` の「責務外」コメントで宙ぶらりんに)
- **読み出し側 `build_dynamic_section` も `prepare_pulse_root_context` も dead code** (Phase 1.1 で並列パスとして実装されたがランタイム統合されず、どこからも呼ばれない)
- **「中断 → 再開」専用に閉じた発想**で、同 Track 内で動き続けて Metabolism によりコンテキストから押し出される情報の維持には無力

### 本質の再定義

書き込みと読み込みの本質を以下のように再整理する:

- **書き込みの本質**: Track 内のメッセージが Metabolism で押し出される際に、Track 目的に沿って必要情報を圧縮保存する
- **読み込みの本質**: 現コンテキストに含まれていない Track 内の必要情報を洗い出して補う
- **Track の役割**: 「Track 内に居る間は Track 内の過去情報すべてにアクセスできる」という**必要情報の基準線**として機能する。これは不変条件 2 (単一主体の記憶) の運用面の具体化

「中断 → 再開」は、本機構がカバーする多くのケースのうちの 1 つに過ぎない。同 Track 内で長時間作業して Metabolism が発生した場合も、別 Track にしばらく行って戻ってきた場合も、必要情報を呼び戻す挙動は同一になる。

### General Chronicle との関係

既存 General Chronicle (arasuji) は「**全体の流れ**を多段圧縮で残す」機構として優秀。Track Chronicle はこれと**独立**に走り、「**Track 目的に沿った作業遂行情報**を Track 単位で残す」機構として並走する。

| 軸 | General Chronicle | Track Chronicle |
|---|---|---|
| 対象 | 全メッセージ (Track 横断) | 特定 Track の origin_track_id 紐付きメッセージ |
| 抽出視点 | 出来事のあらすじ | Track 目的に向けた作業遂行情報 (計画 / 完了 / 進行中 / 課題等) |
| 書き込み trigger | 現状: 件数閾値 (改善 issue 別途あり) | Metabolism 連動 (押し出し対象を生成) |
| 読み込み | Memory Weave context として head 側 | アクティブ Track 分のみ head に乗せる + 切り替え時に history 末尾近く挿入 |
| 重複 | 同じメッセージから両方独立に生成可 (抽出視点が違うため重複に問題なし) | 同上 |

---

## 2. 用語整理

| 用語 | 定義 |
|---|---|
| **押し出し対象** | Metabolism 発火時に anchor 移動でコンテキストから外される予定のメッセージ群 |
| **Track 別生成** | 押し出し対象を origin_track_id でグループ化し、Track ごとに Chronicle entry を作る処理 |
| **incomplete Lv1** | バッチサイズ未満で作られた一時的な Lv1 entry。後でメッセージが揃ったら正規 Lv1 として再生成される |
| **head 入れ替え** | Metabolism 時、メインキャッシュ head の Track Chronicle セクションを「今アクティブな Track の Chronicle 一式」に差し替える挙動 |
| **切り替え通知挿入** | Track 切り替え時、メタ判断独白の committed 昇格直後に切り替え先 Track の Chronicle を独立メッセージとして history 末尾近くに挿入する挙動 |
| **時刻アンカー** | Metabolism 時、最古残存メッセージの直前に揮発で挿入する `<system>以下、YYYY-MM-DD HH:MM:SS 以降のやり取りです</system>` 表記 |

---

## 3. 全体像

3 つの構成要素が協調する:

```
[書き込み] Metabolism 発火 → 押し出し対象を Track ごとに分けて
            arasuji_entries に origin_track_id 付き entry 追加
                  ↓
[読み込み (head)] Metabolism のたびに head の Track Chronicle セクションを
                  「今アクティブな Track 全体の流れ」に入れ替える
                  (= get_episode_context を origin_track_id でフィルタして取得)
                  ↓
[読み込み (history)] Track 切り替え時、_promote_meta_judgment_in_pulse の延長で
                     メタ判断独白の committed 昇格直後に
                     切り替え先 Track の Chronicle を独立メッセージとして INSERT
```

書き込みは「Chronicle DB への entry 追加」、読み込みは 2 種類 (head 入れ替え / 切り替え時挿入) の異なるタイミング・配置で動く。

---

## 4. 書き込み側

### 4-1. Metabolism 連動

Track Chronicle の生成 trigger は **Metabolism のみ**。閾値ベースの定期生成は走らせない (今コンテキストにあるメッセージのあらすじを作る必要は無いため)。

### 4-2. Track ごとの分割生成

Metabolism 押し出し対象に複数 Track のメッセージが混在することは普通にある (line_role/scope フィルタは Track 横断のため、コンテキスト残存も押し出し対象も Track 横断)。これを `origin_track_id` でグループ化し、**Track ごとに独立な Chronicle entry を作る**。

「アクティブ Track のものだけ作る」では、再開時に他 Track の流れが復元できなくなる。アクティブ含めて全 Track 分を等しく書く。

### 4-3. バッチサイズ未満の扱い

Track ごとに分割すると、ある Track では押し出し対象が 20 件 (= 既存 General Chronicle のバッチサイズ) 未満になることが多い。

ポリシー:

- **バッチサイズ未満でも Lv1 を作る**。ただし `metadata` に `incomplete: true` フラグを立てる
- 後で同 Track のメッセージが追加されて 20 件分溜まったら、incomplete Lv1 を**削除して新しい完全 Lv1 を作る**
- Lv2 への上方統合は完全 Lv1 のみを対象に動く (incomplete Lv1 は対象外)

これは既存 gap-fill 機構 (Lv2 落ち込み回避) とは別ロジック。incomplete フラグ + 再生成は新規実装。

### 4-4. 1000 字未満のスキップ

Track ごとの押し出し対象の合計文字数が 1000 字以下なら、Chronicle 化を**スキップ**する。短すぎる内容を要約しても情報量は変わらず、LLM 呼び出しコストの無駄。

スキップした分のメッセージは押し出されてコンテキストから消えるので、読み込み側で別経路 (§5-3) で取得する。

### 4-5. 抽出プロンプト

Track Chronicle の抽出視点は **Track 目的に沿った作業遂行情報**:
- 計画 / 立てた目標
- 完了済み作業
- 進行中の課題
- 待ち事項 / 未解決の問い
- 結論 / 決定

General Chronicle の「出来事のあらすじ」とは抽出視点が異なるため、生成プロンプトも別。Track の `intent` / `title` を渡して目的駆動の抽出を行わせる。

### 4-6. 同期実行

まず動くものを優先するため**同期実行**で実装する (Pulse 内で LLM 呼び出しを伴う処理を走らせる)。

非同期化 (バックグラウンド生成) はパフォーマンス改善案件として後で別途。Metabolism 自体の頻度は低いので、同期実行でも実用上問題にならない見込み。

### 4-7. 独立経路として実装

既存 `_generate_chronicle` (`sea/runtime.py:1817`) は `pulse_type == "user"` 限定 + ユーザー確認 dialog 必須 + バッチ未満スキップ等、Track Chronicle のポリシーと噛み合わない制約を持つ。これを拡張するのではなく、**新規関数 `_generate_track_chronicle` を別建て**する。

理由:
- ユーザー確認 dialog 不要 (ペルソナの自律的な記憶整理)
- バッチサイズ未満許容
- 1000 字未満スキップ
- 自律稼働中の Pulse でも走る

ArasujiGenerator の核ロジック (LLM 呼び出し / DB 書き込み / 多段圧縮 / gap-fill) は再利用可能。Track 用の入力フィルタ + 抽出プロンプト差し替え + ポリシー固有の判定 (incomplete / スキップ) を被せる形で実装する。

---

## 5. 読み込み側

### 5-1. head 配置 (アクティブ Track の流れ)

メインキャッシュの head 側 (Memory Weave context 経由) に**今アクティブな Track の Chronicle 一式**を載せる。General Chronicle / Memopedia と並ぶ位置で、Track Chronicle セクションを追加する。

整形は General Chronicle と同じ `get_episode_context` アルゴリズム (reverse level promotion) を流用する:
- `origin_track_id` でメッセージ・Chronicle entry をフィルタ
- 直近: Lv0 (生メッセージ) → Lv1 (あらすじ) → Lv2+ (あらすじのあらすじ) と過去に向かって自然に粒度が粗くなる
- ID ベース重複回避

書式 (案):
```
## このトラック「{title}」での作業履歴

### 最近の出来事
- {生メッセージ}

### あらすじ
【{start} ~ {end}】
{Lv1 content}

### あらすじのあらすじ
【{start} ~ {end}】
{Lv2 content}
```

### 5-2. head 入れ替えの挙動

**Metabolism のたびに head の Track Chronicle セクションは入れ替わる** (= 今アクティブな Track のものに差し替え)。Track 切り替えだけでは head は触らない (前 Track のものが残る)。

これにより:
- 通常 Pulse (= Metabolism なし) では head は不変、cache hit 継続
- Track 切り替え時も head は不変、cache hit 継続
- Metabolism 時のみ head が更新される (= anchor 移動でどのみち key 部分が動くタイミング)

### 5-3. 1000 字未満ぶんの生メッセージ取得

§4-4 でスキップした分は Chronicle 化されていない。読み込み時に SAIMemory から直接取得して挿入する経路を持つ。

```sql
SELECT * FROM messages
WHERE origin_track_id = ?
  AND created_at < {残存最古時刻}
  AND id NOT IN ({Chronicle source_ids})
  AND line_role = 'main_line'
  AND scope = 'committed'
ORDER BY created_at ASC
```

これらは Track Chronicle の「### 最近の出来事」セクション末尾に生で添える形で出す。

**実装注記 (2026-06-29)**: 上記 SQL の `{残存最古時刻}` は **metabolism anchor の
`created_at`** を使う (`_get_track_unprocessed_messages_text` の `history_anchor_message_id`)。
当初の実装はこの `created_at <` 条件が抜けており、anchor 以降 (= まだ会話履歴に載っている)
の Track 生発言まで生ダンプに含めていた。結果、自律 Track の発言が head(track_chronicle) と
tail(履歴) に二重に乗り 10k+ tokens を浪費していた。anchor cutoff で履歴窓内を除外し、本来
意図どおり「押し出された分だけ」を補完する形に修正した。回帰テスト: `tests/test_memory_weave_track_dump.py`。

### 5-4. Track 切り替え時の挙動 (history 末尾近く)

Track 切り替え時、**head の前 Track Chronicle はそのまま残し** (アンロードしない)、切り替え先 Track の Chronicle は **history 末尾近く に独立メッセージとして挿入**する。

これにより:
- head は変えない → cache hit 継続
- 末尾に追加された分のみ cache miss (Track 切り替えごとに必ず発生する分なので許容)
- 次の Metabolism が来た時点で head が新アクティブ Track のものに入れ替わる

実装場所: `_promote_meta_judgment_in_pulse` (`saiverse/saiverse_manager.py:1168`) の延長。

メタ判断独白の committed 昇格 SQL UPDATE の直後に、切り替え先 Track の Chronicle テキストを以下の形式で SAIMemory に INSERT する:

- role: `user`
- content: `<system>\n## トラック「{title}」の作業履歴\n\n{Chronicle 整形テキスト}\n</system>`
- line_role: `main_line`
- scope: `committed`
- pulse_id: 同 pulse_id
- origin_track_id: 切り替え先 Track の id

---

## 6. キャッシュ挙動の具体例

不変条件 7 (キャッシュヒット継続を最優先) に違反しないことを確認するため、典型シナリオを通して挙動を追う。

### シナリオ: Track A → B → A の往復

- t1: Track A アクティブで作業中 → Metabolism 発火
  - 押し出し対象を Track ごとに分けて DB 書き込み (Track A 分は w0, w1, w2 / Track B 分は p0)
  - **head の Track Chronicle セクションを Track A の Chronicle 一式 (w0, w1, w2) に入れ替え**
  - cache: head 〜 history まで一旦 miss して書き込み直し (Metabolism による正常な cache 投資)

- t2: Track A → Track B に切り替え (メタ判断 Pulse で `/spell track_activate(B)`)
  - メタ判断独白が committed 昇格
  - **その直後に Track B の Chronicle (p0) を独立メッセージとして INSERT**
  - **head は Track A のまま** (アンロードしない)
  - cache: head は hit 継続、history 末尾の追加分のみ miss

- t2.5: Track B で動き続ける → Metabolism 発火
  - 押し出し対象を Track ごとに分けて DB 書き込み (Track B 分は p1, p2)
  - **head の Track Chronicle セクションが Track B の Chronicle 一式 (p0, p1, p2) に入れ替わる** (Track A の w0, w1, w2 は head から消える、必要時は DB から)
  - cache: head 入れ替え分が miss、再投資

- t3: Track B → Track A に戻り
  - メタ判断独白が committed 昇格
  - **その直後に Track A の Chronicle (w0, w1, w2 + 戻ってからの新規 w3 まで含めた最新版) を独立メッセージとして INSERT**
  - **head は Track B のまま** (アンロードしない)
  - cache: head は hit 継続、history 末尾の追加分のみ miss

- t3.5: Track A で動き続ける → Metabolism 発火
  - **head の Track Chronicle セクションが Track A の最新版 (w0, w1, w2, w3) に入れ替わる**

### 重要な性質

- **head に同じ Track の Chronicle が二度載ることはない**。head は常に「最後の Metabolism 時点でアクティブだった Track の Chronicle 一式」のみ
- **Track 切り替え時の cache miss は末尾追加分のみ**で head は変わらない
- **Metabolism 時の cache 入れ替えは anchor 移動と同期**しているので、Metabolism 単体の追加コストは小さい

---

## 7. 時刻アンカー

Metabolism 時、コンテキスト内の最古残存メッセージの**直前**に揮発で時刻メタを挿入する:

```
<system>以下、2026-05-09 14:23:45 以降のやり取りです</system>
```

書式上の注意:
- role='user' + `<system>` タグでラップ (Gemini 互換のため、role='system' は使わない)
- 「Metabolism 発生時刻」ではなく**最古残存メッセージの created_at**
- リアルタイム情報 (現在時刻) と Track Chronicle (過去時刻範囲明示) と組み合わせることで、ペルソナは時系列を判別できる

メッセージ自体に時刻メタを付ける案は不採用。理由:
- 過去 AI 応答はペルソナの「自分はこのように発言する存在だ」のお手本になっており、各メッセージに時刻メタが付くとペルソナ自身も時刻を発言し始める失敗パターンがある
- プロンプト側に同形式のメタ情報が常時並ぶとアテンションが偏り、同様の真似が起きる
- 「Metabolism 時に 1 か所だけ揮発で挿入」は記憶されず、単発で時系列起点を示すため副作用が出にくい

---

## 8. 撤去対象

Track Chronicle 実装に伴って撤去する dead / 旧仕様コード:

| 対象 | 場所 | 理由 |
|---|---|---|
| `prepare_pulse_root_context` | `sea/pulse_root_context.py:288` | Phase 1.1 で実装されたが呼び出し元が無い dead code。Track Chronicle の挿入位置は legacy `prepare_context` 側に統合する |
| `build_fixed_section` | 同上 226 | 同上 |
| `build_dynamic_section` | 同上 244 | 同上 |
| `is_first_pulse` / `mark_cache_built` / `reset_cache_built` | 同上 97-146 | 上記と一体の cache_built_at 管理。Track Chronicle 挙動には不要 |
| `pause_summary` カラム | `database/models.py` action_tracks | Track Chronicle で完全に置き換わる |
| `pause_summary_updated_at` カラム | 同上 | 同上 |
| `track.pause_summary` API 露出 | `api/routes/people/tracks.py:57-58`, `api/routes/people/models.py` | 同上 |
| `pause_summary` 表示 | 旧 `frontend/src/components/memory/TracksViewer.tsx` (UI 自体も 2026-07-16 退役) | 同上 |
| `meta_layer.py:28` の「責務外」コメント | `saiverse/meta_layer.py` | 旧仕様の宙ぶらりんコメント、書き込み責務が確定したため不要 |

撤去は migration ファイルを書いて DB 側もクリーンに削除する。

---

## 9. Phase 配置

Track Chronicle 本体の実装は **Phase 3** に乗せる (中断・再開機構の本体に相当)。

[phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) の進捗表に以下の項目を新設:

- `_generate_track_chronicle` 関数新設 + Track 別 entry 生成 + incomplete フラグ + 1000 字未満スキップ
- arasuji_entries に `origin_track_id` カラム追加 (migration)
- ArasujiGenerator に Track 用入力フィルタ + 抽出プロンプト差し替え経路
- Track Chronicle 用の `get_episode_context` フィルタ拡張
- `get_memory_weave_context` への Track Chronicle セクション追加
- 1000 字未満生メッセージ取得経路実装
- `_promote_meta_judgment_in_pulse` 延長で Track 切り替え時 Chronicle 挿入
- 時刻アンカー揮発挿入 (Metabolism 時)
- dead code 撤去 (§8)

実装順序の目安:
1. arasuji_entries の `origin_track_id` カラム追加 (migration)
2. ArasujiGenerator の Track 用拡張
3. `_generate_track_chronicle` 新設 + Metabolism 連動
4. `get_memory_weave_context` の Track Chronicle セクション追加 (head 配置)
5. `_promote_meta_judgment_in_pulse` 延長 (切り替え時挿入)
6. 時刻アンカー
7. 1000 字未満生メッセージ経路
8. dead code 撤去

---

## 10. 守るべき不変条件との関係

| 不変条件 | Track Chronicle が果たす役割 |
|---|---|
| 1. 同時実行しない | (関係なし) |
| 2. 単一主体の記憶 | Track Chronicle は「Track 内に居る間は Track 内の過去全情報にアクセスできる」基準線を実装で担保する |
| 3. メタレイヤーが切り替えを独占 | 切り替え時 Chronicle 挿入は `_promote_meta_judgment_in_pulse` 経由 (= メタレイヤー管轄の延長) |
| 4. Track 永続化 | arasuji_entries が Track 紐付きで永続化される |
| 5. 古い Track の忘却 | (Phase 5 の Track 忘却自動化と連動。forgotten Track の Chronicle 扱いは別途決定) |
| 6. メタレイヤーは恒常的に存在 | (関係なし) |
| 7. **キャッシュヒット継続を最優先** | head は Metabolism 時のみ入れ替え、Track 切り替え時は head 不変 |
| 8. 軽量 / 重量級モデルの使い分け | (関係なし、Chronicle 生成側は軽量モデル想定) |
| 9. 他者との会話は重量級モデル | (関係なし) |
| 10. Metabolism 機構を活用 | Track Chronicle 生成 trigger は Metabolism 発火と一体 |
| 11. メタ判断はペルソナの自分の思考 | (関係なし) |
| 12. 親-子ラインの寿命関係 | (関係なし、Chronicle は Track スコープなのでラインスコープより長寿命) |

特に**不変条件 7 はキャッシュ挙動の具体例 (§6) で詳細に確認している**。Track 切り替えごとに head を変えない設計は、この不変条件の具体的な実装表現にあたる。

---

## 11. ユーザー会話 Track の親スレッド保持機構 (v0.32 で追加)

### 動機

Track Chronicle はあくまで「中断・再開時に呼び戻すための圧縮サマリ」として作られているが、ユーザー会話 Track にこれを適用すると重大な副作用が出る:

- 抽出プロンプトが「計画 / 完了 / 進行中 / 結論」型なので、対ユーザー会話の温度感や対話のニュアンスが失われる
- General Chronicle と二重保存になる (LLM コスト + 容量)
- 自律稼働でメッセージが完全に押し出された後、ユーザー会話に戻ってきても **生メッセージが 1 件もコンテキストに無い** 状況が発生し得る → ペルソナの人格・対話温度が崩れる懸念

これは SAIVerse の中心需要 (人格の安定性) に対して致命的。

### 機構の本質

Stelis 親子スレッドのメタファーを流用する:

- **ユーザー会話 Track = 親スレッド** (常に保持)
- **その他 Track (autonomous / external 等) = 子スレッド** (メインラインで入れ替わる)

ユーザーとの会話の最新一定数 (デフォルト 20 件) を、自律稼働中であってもコンテキスト上部に**生メッセージ**として確保する。

### 不足分補完方式

「常に N 件」ではなく「**メインラインに既に居る数を数えて不足分だけ補完**」方式を採用 (重複回避のため):

1. Metabolism 後の history 内の `origin_track_id == owner_user_conversation_track_id` のメッセージ数 = `existing_count`
2. `needed = target_count - existing_count`
3. needed > 0 なら、history 最古より過去のオーナー Track メッセージから needed 件取得して上部補完
4. needed <= 0 なら何もしない (history 内に既に十分残っている)

これによりアクティブが user_conversation の場合は上部補完が省略され、自律稼働中の場合は最低 N 件を保持できる。

### オーナーユーザー会話 Track の特定

複数 user_conversation Track があり得る場合の優先順位:

1. **リンクユーザー (UserAiLink)** — ペルソナのオーナーとして登録されているユーザーの user_conversation Track
2. **フォールバック** — リンクユーザー未設定の場合、最古の user_conversation Track をオーナーとして自動採用

### コンテキスト構成 (改訂)

```
[head]
  system prompt
  Memory Weave context:
    ## これまでの出来事 (General Chronicle)
    ## 記憶ベース (Memopedia)
    ## トラック「{title}」での作業履歴 (Track Chronicle, アクティブが ≠ user_conversation のときのみ)
  visual context

[親スレッド保持セクション (アクティブが user_conversation 以外のときのみ)]
  時刻アンカー① <system>以下、YYYY-MM-DD HH:MM:SS 以降のユーザーとの会話です</system>
  ユーザー会話 Track の不足分補完メッセージ (古→新)

[メインライン履歴セクション]
  時刻アンカー② <system>以下、YYYY-MM-DD HH:MM:SS 以降のやり取りです</system>
  history (line_role=main_line, scope=committed のメッセージ列)

[末尾]
  realtime info (時刻 / Building 等)
```

### ユーザー会話 Track の Track Chronicle 化はスキップ

親保持機構が生メッセージで文脈を担保するため、ユーザー会話 Track は Track Chronicle の対象外とする:

- `_generate_track_chronicle` のループ内で `track_type='user_conversation'` をスキップ
- `_get_track_chronicle_context` でアクティブ Track が user_conversation なら "" を返す
- `_insert_track_chronicle_on_switch` で切り替え先 Track が user_conversation なら早期 return

### 設定パラメータ

- `SAIVERSE_USER_CONV_PRESERVE_COUNT` (環境変数、デフォルト 20)
- ペルソナごとには設定しない (= AI 全般に共通の人格安定性パラメータとして扱う)

### 実装場所

- `saiverse/user_conversation_preserver.py` — オーナー Track 特定 + 不足分補完取得
- `sea/runtime_context.py` — `prepare_context` 内で history 取得後に補完セクション + 時刻アンカー① / ② 挿入

### キャッシュ挙動

- ユーザーが新規発言したとき: 「ユーザー会話 Track」のメッセージが追加 → 該当セクションが伸びる → そこ以降が cache miss (ユーザー発言の頻度なら問題なし)
- 自律稼働で他 Track が動いているとき: 親保持セクションは安定 → cache hit 継続。Memory Weave context (Track Chronicle 含む) は Metabolism のたびに動くが、親保持セクションは独立に安定する

---

## 12. オープン項目

実装段階で詰める or 後続議論に回す項目:

- forgotten / aborted / completed Track の Chronicle 扱い (head には載せない / DB 上は保持で OK か)
- dormant Track の Chronicle を head にどう載せるか (Phase 5 の dormant 機構実装後に再評価)
- General Chronicle 改善 (`general_chronicle_metabolism_trigger.md` / `general_chronicle_user_pulse_only.md`) と Track Chronicle 実装の優先順位調整
- Track Chronicle の context window 圧迫上限 (Track 数 × Chronicle トークン量) の運用閾値
- Memory Settings UI 上での Track Chronicle 一覧表示 (調査・運用観察用)

---

## 13. 関連ドキュメント

- [revisions.md](revisions.md) v0.31 (待ち機構の整理 + pause_summary 廃止) / v0.32 (本 Intent doc 起草)
- [01_concepts.md](01_concepts.md) — 不変条件と Track 状態
- [02_mechanics.md](02_mechanics.md) — Metabolism と Pulse 階層
- [03_data_model.md](03_data_model.md) — action_tracks スキーマ
- [phases/phase_3_lines_playbooks.md](phases/phase_3_lines_playbooks.md) — Track Chronicle 実装の進捗管理
- [phases/phase_5_autonomy.md](phases/phase_5_autonomy.md) — 時間差ツール基盤との関係 (時間差ツールが結果到着でメッセージ追加 → 次の Metabolism で Track Chronicle 化される連動)
- [`docs/issues/general_chronicle_metabolism_trigger.md`](../../issues/general_chronicle_metabolism_trigger.md) — General 側の trigger 改善
- [`docs/issues/general_chronicle_user_pulse_only.md`](../../issues/general_chronicle_user_pulse_only.md) — General 側の自律稼働欠落
