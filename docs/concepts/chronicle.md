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

### 束ね — 字数発火・質量選抜 (2026-07-27 世代交代)

設計正典は [intent/chronicle_consolidation.md](../intent/chronicle_consolidation.md)。旧「次数 + 列のあふれ」(質量 U×B^k 発火・level 別の列) を置き換えた。

- **列** = 未束ね General ノードの時系列一本の列 (level 混在)。各ノードは **coverage_chars** (質量 = 被覆生ログ字数) を持つ
- **発火**: 列のあらすじ字数合計 > X (= 提示予算 `SAIVERSE_CHRONICLE_CHAR_BUDGET` の 1/4)。発火と提示が同じノブに連動する
- **群** (束ねの単位) = 連続部分列で三条件: 比率 (質量 max/min ≤ 10) / 連続性 (壁も未編纂の生ログも跨がない — 偽の隣接 §4-5 の禁止) / 卒業 (合算質量 ≥ 群内最大の5倍)。卒業する窓を丸ごと 1 コールで束ね、軽い群から、上限 3 コール/回。最新端の育ち中の群は X を守れる限り猶予
- **治療**: 卒業に到達できず軽い側で行き詰まったノード/群 (束ね不能) は、発火を待たず比率免除で後ろの隣人へ合流 (metadata `band_kind='treatment'`)。恒等圧縮の子が初めて要約に変わる瞬間に Memopedia Fragment 抽出が走る (`batch_callback`)
- **非常弁**: 発火したのに何も打てない予見外の形に限り最古の隣接2件を比率無視で束ねる (`band_kind='valve'` + WARNING。平常発火ゼロが健全)
- 親 INSERT + 子 mark_consolidated は単一 tx。親の質量 = 子の合算、level = max(子)+1 (読み込み互換の導出キャッシュ)
- 物理格納は `memopedia_pages`（trunk `root_chronicle` 配下、Memory Atlas「時間の地図」。P3b, 2026-07-11）。`arasuji_entries` は読み取り専用 SQL VIEW

### head への読み込み (提示)

`get_episode_context` (`sai_memory/arasuji/context.py`) が、新しい側から過去へ遡ってあらすじを拾い、文字数予算 (既定 2 万字 / `SAIVERSE_CHRONICLE_CHAR_BUDGET`) に収めて返す。**走査と圧縮は別工程**で、この分離が不変条件を支えている。

- **走査**: 各段で選べるのは「読み残しの最前線」(現在位置以前で end_time が最大の未読ノード) と end_time が揃うノードだけ。最前線を飛び越えて古いノードを掴むと、飛ばした範囲は走査位置が過去へ動いたあとなので二度と候補にならず、**その体験は誰にも記録されないまま提示から消える**。同 end_time の候補 (= 同じ範囲の粒度違い) からの粒度選択は**累積質量ルール** (2026-07-27 世代交代、[chronicle_consolidation](../intent/chronicle_consolidation.md) §6): ここまでに見せた質量の累計の 1/10 以上の質量を次のノードに求め、満たす中で最も細かいものを採る。どれも満たさなければ最も粗い候補をそのまま見せる (穴回避 — この件数が「束ねが追いついていない度」の観測メトリクスとして DEBUG ログに出る)。粗さが件数でなく体験量に比例するので、豆粒ノードの混入で健全な記憶が巻き添えで粗くならない。旧 `MIN_ENTRIES_PER_LEVEL` (件数昇格) は Track Chronicle 等の件数ベース経路にのみ残る
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
