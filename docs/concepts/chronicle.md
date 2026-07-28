# Chronicle（時系列圧縮）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §5](../overview/landscape.md) を参照。設計正典は [あらすじのレベル制](../intent/arasuji_levels.md)。

## 一言で

蓄積された [Message](saimemory.md) を、レベル別のあらすじの並びへ段階的に圧縮した咀嚼層。

## 役割

生ログは無限に増えるが、[Session](session.md)（短期記憶）に載せられる量は限られる。Chronicle は古い経験を digest へ畳んで、ペルソナが過去の文脈を圧縮された形で参照できるようにする。

## 仕組み (あらすじのレベル制 — 2026-07-28 世代交代)

設計正典は [intent/arasuji_levels.md](../intent/arasuji_levels.md)。旧世代 (episode 整列 + 恒等圧縮 + 転写 + 字数発火・質量選抜の束ね) は landscape §9 に記録。

**規則はただ 1 つ** — どのレベルの並びにも字数の予算 (上限 / 残す量) があり、上限を超えたら古い側を「残す量」に収まるまで 1 個の要約に畳み、1 つ上のレベルの並びへ送る。

### レベル0 (生ログ) → 一次あらすじ

- 退場計画 (`sea/eviction_plan.py::plan_eviction`): 残す量 (既定 6 万字) より古い側を、古い順に U (既定 1 万字、`SAIVERSE_CHRONICLE_BAND_BUDGET`) ずつの範囲に刻んで全部畳む。切り位置は pulse 関節 (発言の切れ目) に寄せる。**エピソードに畳みを止める権利は無い** (open episode も畳む)。末尾の U 未満の端数は畳まず次回へ残す — 小さい一次あらすじを作らない
- チャンク計画 (`sai_memory/arasuji/alignment.py::plan_alignment`): 退場範囲を U ずつのチャンクに計画。全チャンクが LLM 圧縮 (**小さくても要約する** — 生ログを生のまま置く恒等圧縮は廃止)
- 実行 (`sai_memory/arasuji/executor.py::execute_plan`): チャンク単位の単一トランザクション。材料に作業ダイジェスト行 (tag `session_digest`) が混ざる場合は `[作業のまとめ]` の種別ラベルを付けて LLM に渡す — **作業セッションの体験が長期記憶へ入る唯一の道はこの合流** (入口は一本)

### レベル1 以上 (束ね)

- `sai_memory/arasuji/bands.py::run_band_overflow`: レベルごとの並び (未束ねノードの時系列列) の字数合計が上限 (既定 5,000 字) を超えたら、古い側を残す量 (2,500 字) まで 1 個の親に畳んでレベル +1 へ。連鎖 (レベル1 の畳みがレベル2 を溢れさせる) も同じ規則で流れる
- メンバーの大きさ (被覆 coverage_chars) は**判定に使わない** — 被覆は「あらすじ → 元の体験」を辿る錨・統計として合算で親へ引き継ぐだけ
- 材料には種別 (あらすじ / 生ログ断片) を明示して LLM に渡す — 旧世代の恒等圧縮データが混ざっても壊れない
- 親 INSERT + 子 mark_consolidated は単一 tx (tx 内で子の未束ねを再検査)
- 物理格納は `memopedia_pages`（trunk `root_chronicle` 配下、Memory Atlas「時間の地図」）。`arasuji_entries` は読み取り専用 SQL VIEW

### head への読み込み (提示)

`get_episode_context` (`sai_memory/arasuji/context.py`) が、新しい側から過去へ遡ってあらすじを拾い、文字数予算 (既定 2 万字 / `SAIVERSE_CHRONICLE_CHAR_BUDGET`) に収めて返す。走査と圧縮は別工程 (2026-07-25 の欠落バグ修正)。**レベル制の完成形では「各レベルの並びをそのまま出す」に簡素化される予定** (intent §12-7 — presentation_gap 修正の実機検証後)。

### 生成タイミングと原子性

[Metabolism](metabolism.md) 発火時に `generate_chronicle` (sea/session_lifecycle.py) が「新 anchor より古い範囲」だけを編纂する。anchor 前進は編纂成功後のみ (S2 ゲート)。全入口 (自動 Metabolism / anchor 失効 / 手動 organize-memory / session close / API 生成ジョブ) は実行台帳の冪等 claim `metabolism.run` を通る (M1)。Fragment 生成 (entity 抽出) は全チャンクに相乗りする。

## 実装

- 退場計画: `sea/eviction_plan.py`（`plan_eviction` — 純関数）
- チャンク計画: `sai_memory/arasuji/alignment.py`（`plan_alignment` — 純関数、見積もりと生成の一点管理）
- チャンク実行: `sai_memory/arasuji/executor.py`（`execute_plan`）
- レベル束ね + 帰化: `sai_memory/arasuji/bands.py`（`run_band_overflow` / `backfill_coverage`）
- 単一エントリ再生成の一次あらすじ部品: `sai_memory/arasuji/generator.py`（`generate_level1_arasuji`）
- ストレージ: `sai_memory/arasuji/storage.py`（物理格納は `memopedia_pages`、`arasuji_entries` は互換 VIEW）
- コンテキスト構築 (読み込み): `sai_memory/arasuji/context.py`
- 読み出しツール: `builtin_data/tools/chronicle_*.py`（search / read_detail / context_up / context_down）

## 関連概念

- [SAIMemory](saimemory.md) — Chronicle を内包する容れ物
- [Memopedia](memopedia.md) — 同じ編纂チャンクで Fragment 連動生成
- [Metabolism](metabolism.md) — Chronicle 生成を発火する節目
- [Episode](../intent/episode.md) — 体験の切れ目の記録 (あらすじとは分離 — 2026-07-28 裁定)

## 参照

- 地図: [`landscape.md`](../overview/landscape.md) §5 / §9 (旧世代の記録)
- 設計: [`arasuji_levels.md`](../intent/arasuji_levels.md)
