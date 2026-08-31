# エピソードとあらすじの分離 — 影響範囲の洗い出し (2026-07-28)

記憶の川の再設計 (記憶の一本化) は「**エピソード (体験の切れ目の記録) とあらすじ (提示予算のための圧縮) は別の概念**」と決断することを含む。一次あらすじは字数で切られ、エピソード境界には「寄せる」だけになるため、「このエピソードの概要」という 1 対 1 の対応物 (episode digest) が基本存在しなくなる。

この文書は、その決断が**実装済みコードと今後の計画の両方**に与える影響の全量。リポジトリ全域の調査 (2026-07-28、read-only) に基づく。

---

## 1. 背骨が折れる正典 — 分離を採るなら改訂必須の intent 3 枚

| intent | 何が折れるか |
|---|---|
| `docs/intent/episode.md` | **最大級**。「エピソード概要 = エピソード Lv1 Chronicle」を三つの顔 (記憶 LoD / 世界への露出 / 監査) の共有部品に据える設計の背骨。§5 エピソード単位 LoD (畳み = 概要 1 件への置換)、§6 「終了次第、即生成」、§7 監査役の相乗り、§8.1 チャット枠の「概要の確定」段、§12.3 「エピソード close trigger へ世代交代」——全部が宙に浮く。再設計は §12.3 と真逆 (字数 trigger の一般化) |
| `docs/intent/chronicle_eviction.md` | §2 不変条件「episode の原子性 (open を混ぜない・分断しない)」と §3「open は単独、closed 同士はまたぐ」= **畳み拒否権の正典**が廃止対象。§5-5 二段構えの存在理由も消える。§8「育った会話 A の退場を掘らない」裁定は「合いの子 digest 防止」を根拠にしていたため**再裁定が要る** |
| `docs/intent/experience_structure.md` | §3.2 digest 出自語彙から `episode` が落ちる。§4-4「同一レベル再圧縮禁止」の主たる適用先が消え守備範囲が縮む。§9 対応表の「作業セッション digest = episode ノードの digest (1:1)」行が無効化 |

## 2. 再設計が要る計画 (今後の物への影響)

- **想起用タグ 層4** (`docs/intent/persona_cognition/recall_tags_and_track_reduction.md`): 「chronicle_eviction が退場を episode 単位に揃えることで、タグの単位と畳みの単位が一致する」という**待つ理由**が崩れる。ただし本当の要件 (唯一の錨) は「**digest ↔ 被覆元 (episode / source_ids) の対応が引けること**」であり、これは字数切りでも**あらすじが source_ids / episode_refs の記録を持ち続ければ満たせる**。前提の言い回しが壊れるだけで、錨は生存可能 — 再設計の不変条件に「あらすじは被覆元への対応を必ず持つ」を明記して引き継ぐ。
- **`docs/issues/track_episode_continuity.md`** (「この前は何やったんだっけ」問題): 解決候補 3 つのうち 2 つが「episode digest の列」を材料に指定している。「エピソードに時間的に重なるあらすじを引く」形へ書き換えが要る。未解決 issue なので今なら書き換えが安い。
- **UC-2 割り込み復帰** (`use_cases.md`): 「Chronicle 付き復帰」の実体は上記と同じ穴。候補がさらに絞られる。
- **監査役** (`episode.md` §7): Lv1 生成との相乗り前提が消え、単独 LLM コールになりコスト前提が変わる。
- **部分退場の子エピソード** (`session_lifecycle.py _record_partial_episode`): pulse 関節細分そのもの。「境界に寄せるだけ」の世界では子 episode を刻む意味が薄れる — 残すか畳むか要判断 (継承 DAG のエッジ生成元でもある)。

## 3. 撤去対象になる実装

- **転写系一式**: `alignment.py` の `CHUNK_EPISODE_DIGEST` / `_segment_by_digest` / 分裂片降格、`executor.py` の転写分岐、`bands.py` `estimate_leaf_chars` の分岐、見積もりの `chunks_episode`。※現状すでに全ペルソナ発火 0 件 (`docs/issues/work_session_digest_never_reaches_chronicle.md`)。
- **episode digest 索引**: `episodes.py collect_episode_digest_index` (digest_ref の実質唯一の消費者) と、そのラッパ (`session_lifecycle._collect_episode_digests`、`api/routes/people/arasuji.py` の 2 箇所)。
- **畳み拒否権**: `eviction_plan.py` の `unit.is_open` 分岐・二段構え (`allow_undersized_open`)・`_open_episode_refs` 供給。最大の削減対象。
- **digest_ref の後段確定**: `execution_ledger_wiring.py` handler 後半の `set_digest_ref(message:...)`、`judgment_finalize.py` の直書き経路。※この handler は再設計 §4-2「作業要約の直接登録」の受け皿候補地でもある。
- `close_conversation_episode(digest_ref=)` は現状呼び出し元が渡していない未使用パラメータ (即掃除可)。

## 4. 影響なしと確認できたもの (安心材料)

- **フロントエンド**: episode UI (`EventsTimeline.tsx` / `episodeText.ts`) は **digest も概要テキストも一切読んでいない** — kind / 場所 / 参加者 / meta からの決定論テンプレで文生成済み。実装が先に分離を済ませていた形。※ `work_session.py` close の `meta.title` / `meta.artifacts` キー名はフロントとの契約 — 維持必須。
- **提示側** (`sai_memory/arasuji/context.py`): "episode" の語は Chronicle エントリの旧称で、Episode エンティティに依存していない。
- **Stelis アンカー**: thread スコープの Chronicle であって episode スコープではない。直交。
- **エピソードの開閉そのもの** (`work_session` / `day_plan` / `autonomy_wiring` / 判断点): 不変。分離は「概要」だけの話で、切れ目の記録は残る。
- **帰属タグ `origin_episode`** と読み口 (`episode_read` スペル / `get_messages_by_origin_episode`): 不変。むしろ digest が無くなる分、再訪の唯一手段として重要度が上がる (→ `docs/issues/open_episode_context_after_veto_removal.md`)。
- `docs/intent/life.md`: 「いまの真実は開いているエピソードが持つ」は open/close だけを使い digest を見ていない。

## 5. 分離で丸ごと解ける既存 issue

- `docs/issues/chronicle_split_episode_digest_double_description.md` (分裂 episode の digest 二重記述) — 消滅。
- `docs/issues/work_session_digest_never_reaches_chronicle.md` (転写が構造的に発火しない) — 「作業要約の直接登録」で解決。
- `docs/issues/chronicle_undersized_lv1_chunks.md` (恒等圧縮由来の小粒) — 供給源が消える。

## 6. 見落とし注意 (分離しても維持が要るもの)

1. **一日新聞** (`day_report.py`) と day_close 判断は、`session_digest` **タグ**で digest 行を直接収集している。episodes 側の digest_ref を切っても壊れないが、**「セッション終了時に digest 行を書く」こと自体をやめると一日新聞が空になる**。作業要約を列へ直接登録する新経路でも、一日新聞の材料経路 (タグ付き行 or 代替) を維持すること。
2. **digest_ref は 2 形式混在**: `message:{id}` (作業セッション digest = 廃止方向) と `chronicle:{entry_id}` (部分退場の子 episode = あらすじ参照であり分離後も自然な形)。扱いを分けて設計する。
3. **Memopedia Fragment の抽出発火点**: 現行は編纂バッチ + 転写チャンク + 恒等圧縮の束ねで発火。転写と恒等圧縮が消えると、「小さくても要約する」世界では**全チャンクが LLM バッチになり抽出は編纂時に一本化できる** (簡素化方向)。ただし作業要約の直接登録経路には抽出の口を別途用意する必要。
4. **`digest_origin` 語彙** (`identity` / `episode` / `batch` / `band`) は既存データに残る。読み側 (`bands._digest_origins` 等) は既存データ互換のため残す。
5. **`messages.origin_episode` の帰属付与はユーザー発言側が未実装** (`docs/issues/user_messages_missing_episode_attribution.md`) — 分離とは独立に必要な上流修理。

## 7. テストの影響 (参考)

`test_arasuji_alignment.py` (転写チャンク前提)、`test_arasuji_executor.py`、`test_episodes_table.py` (set_digest_ref)、`test_execution_ledger_wiring.py`、`test_work_session.py`、`test_judgment_points.py`、`test_metabolism_two_layer.py`、`test_eviction_plan.py`。
