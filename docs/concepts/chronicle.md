# Chronicle（時系列圧縮）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §5](../overview/landscape.md) を参照。設計正典は [体験の構造](../intent/experience_structure.md)。

## 一言で

蓄積された [Message](saimemory.md) を「体験の単位 (episode)」に整列して圧縮した咀嚼層。

## 役割

生ログは無限に増えるが、[Session](session.md)（短期記憶）に載せられる量は限られる。Chronicle は古い経験を digest へ畳んで、ペルソナが過去の文脈を圧縮された形で参照できるようにする。

## 仕組み (W4 = 体験の構造 工程(2) で世代交代、2026-07-21)

### episode 整列チャンク生成

旧「20 件固定バッチ」は廃止。編纂対象 (退役する範囲の未編纂メッセージ) を**整列計画** (`plan_alignment`) がチャンク列に分け、チャンク単位の単一トランザクションで一次あらすじを確定する:

- **episode 転写** (`digest_origin='episode'`): digest 確定済み episode の範囲は post_session が書いた digest を**恒等転写** (LLM なし)。**同一レベルの** digest を再圧縮しない (§4-4 — 上の次数への束ねは正当。下の「列のあふれ束ね」がそれ)
- **LLM 束ね** (`digest_origin='batch'`): digest の無い範囲を、episode を原子として被覆合計 ≒ U (1万字) まで束ねて LLM 圧縮。episode は分割しない (§4-2)
- **恒等圧縮** (`digest_origin='identity'`): 1000 字未満で束ねる相手のない豆粒は生のまま一次あらすじに置く (LLM なし、§4-3)

### 次数と列のあふれ束ね

> ⚠️ **この束ね機構は再設計が確定している** (2026-07-27、正典 = [intent/chronicle_consolidation.md](../intent/chronicle_consolidation.md))。発火は質量 (U×B^k) から**未束ねのあらすじ字数** (提示予算連動) へ、相手選びは level 別の列から**質量の比率で集める群** (連続性・卒業条件付き) へ変わる。以下は現行実装の記述。

- 各ノードは **coverage_chars** (被覆生ログ字数) を持つ。次数 k の標準被覆 = U×B^(k-1) (U=1万・B=10)
- 次数 k の未束ね列 の被覆合計が U×B^k を超えたら、**古い端から**束ねて次数 k+1 の親を作る (`run_band_overflow`)。親 INSERT + 子 mark_consolidated は単一 tx
- **上の次数ノードは壁** — 再要約されない (既存 Lv2/Lv3 は帰化バックフィルで coverage を得るだけで content 不変)
- `level` 列は読み込み互換の導出キャッシュ
- 物理格納は `memopedia_pages`（trunk `root_chronicle` 配下、Memory Atlas「時間の地図」。P3b, 2026-07-11）。`arasuji_entries` は読み取り専用 SQL VIEW

### head への読み込み (提示)

`get_episode_context` (`sai_memory/arasuji/context.py`) が、新しい側から過去へ遡ってあらすじを拾い、文字数予算 (既定 2 万字 / `SAIVERSE_CHRONICLE_CHAR_BUDGET`) に収めて返す。**走査と圧縮は別工程**で、この分離が不変条件を支えている。

- **走査**: 各段で選べるのは「読み残しの最前線」(現在位置以前で end_time が最大の未読ノード) と end_time が揃うノードだけ。最前線を飛び越えて古いノードを掴むと、飛ばした範囲は走査位置が過去へ動いたあとなので二度と候補にならず、**その体験は誰にも記録されないまま提示から消える**。同 end_time の候補 (= 同じ範囲の粒度違い) からは、昇格制限 `MIN_ENTRIES_PER_LEVEL` の内側で最も粗いものを採る。制限が穴を強いる場合は制限より穴回避を優先する
- **圧縮**: 読み切ったあと、予算を超えていれば古い側から順に「子が全員そろっている親」へ置き換える (`_compress_within_budget`)。置き換えは被覆範囲を保存するので、粒度が粗くなるだけで体験は落ちない。畳める親が尽きたら予算超過のまま返し WARNING を出す — 削って穴を作るより、記憶の連続性を優先する (超過は束ねが効いていないサインでもある)

2026-07-25 まで、圧縮は「予算を超えたら昇格閾値を下げて走査ごとやり直す」方式だった。粗いレベルを優先する再走査が最前線を飛び越えるため、**上位あらすじが覆っていない直近の期間が丸ごと落ちる**欠陥があった (eris で 3.7 日 41 件、aifi で 271 日 98 件が head から消えていた)。回帰は `tests/test_episode_context.py::TestNoPresentationGap`。

### 生成タイミングと原子性

[Metabolism](metabolism.md) 発火時に `generate_chronicle` (sea/session_lifecycle.py) が「新 anchor より古い範囲」だけを編纂する (退場時圧縮 §4-1 — 提示コンテキストに残る範囲は圧縮しない)。anchor 前進は編纂成功後のみ (S2 ゲート)。全入口 (自動 Metabolism / anchor 失効 / 手動 organize-memory / session close / API 生成ジョブ) は実行台帳の冪等 claim `metabolism.run` を通る (M1)。Fragment 生成 (entity 抽出) は LLM 束ね / episode 転写チャンクに相乗りする。

### Track 専用 Chronicle (生成廃止)

旧 Track Chronicle の**生成は W4 で廃止** ([experience_structure.md](../intent/experience_structure.md) §11-10)。既存データの読み込みは残る。Track 再訪問題は [track_episode_continuity](../issues/track_episode_continuity.md) が管轄。

## 実装

- 整列計画: `sai_memory/arasuji/alignment.py`（`plan_alignment` — 純関数、見積もりと生成の一点管理）
- チャンク実行: `sai_memory/arasuji/executor.py`（`execute_plan`）
- 列のあふれ束ね + 帰化: `sai_memory/arasuji/bands.py`（`run_band_overflow` / `backfill_coverage`）
- 単一エントリ再生成の一次あらすじ部品: `sai_memory/arasuji/generator.py`（`generate_level1_arasuji`）
- ストレージ: `sai_memory/arasuji/storage.py`（物理格納は `memopedia_pages`、`arasuji_entries` は互換 VIEW）
- コンテキスト構築 (読み込み): `sai_memory/arasuji/context.py`
- 読み出しツール: `builtin_data/tools/chronicle_*.py`（search / read_detail / context_up / context_down）

## 関連概念

- [SAIMemory](saimemory.md) — Chronicle を内包する容れ物
- [Memopedia](memopedia.md) — 同じ編纂チャンクで Fragment 連動生成
- [Metabolism](metabolism.md) — Chronicle 生成を発火する節目
- [Episode](../intent/episode.md) — 整列の単位 (体験の最小単位)

## 参照

- 地図: [`landscape.md`](../overview/landscape.md) §5
- 設計: [`experience_structure.md`](../intent/experience_structure.md) §4 (圧縮七原則)
