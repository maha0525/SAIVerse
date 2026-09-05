# SAIVerse 進捗マップ (Roadmap Status)

> **ステータス**: v1.3 (2026-08-09 — 7 月の大工事 (自律行動 v2 / 実行台帳 W 系 / 時間割改修 / あらすじレベル制 / RSS) を反映し、リリース範囲の正典を v0.3.0 の門へ移譲)
> **位置づけ**: 「何が予定されていて、いまどこにいるか」を一望する。概念の関係を示す
> [`landscape.md`](landscape.md) と対をなす、実装の現在地の地図。
> **ステータス記法**: ✅ 完了 / 🟡 進行中 / 🔵 起草中 / 🔲 着手前 / 💤 冬眠

---

## 0. いま動いているもの (Now)

> まはーが最近手を動かしている領域。思いつきで着手が分散しがちなので、現在地をここに集約する。

- 📌 **リリースの発行記録と次の版に入れる範囲は [`release_history.md`](release_history.md) が正典** (2026-09-05 確立)。v0.3.0 の審査記録は [`v030_release_gate.md`](v030_release_gate.md) (発行済み・凍結。篩 = 「後から入れるとユーザーのペルソナに遡及できない傷かマイグレーションを残すか」は大規模版で再利用する)
- 🟢 **Wave 0 (地均し) 実装中** — 裁定不要の独立小物 (user 発言の帰属 / Building ID 制約 / quick_spell / スレッド混線下限 / アラーム名称) と stale 掃除。**このうち stale 掃除は完了した (2026-08-18、門 §6)**
- 🔜 **設計議論の一本目 = エピソードの単位と正史** (門 Wave 1)。二本目 = モデル格 (声/監督/手足) と保温 (Wave 2)
- 進行中案件の逐次状態は [`in_flight.md`](in_flight.md)

---

## 1. 中心軸: 自律稼働 (v0.3.0)

> v0.3.0 の中心軸は「**自律稼働**（AI が連続して動き続ける）」。
> 以下に並ぶ認知モデル整備・記憶階層は、すべてこの自律稼働を実現するための手段である。

- ✅ **Phase 1**: pulse_logs / Important フラグ / 自動タグ付け / サブエージェント隔離
- ✅ **Phase 2**: Chronicle / Memopedia 実装 / Spell 化吸収
- ✅ **自律行動 v2 (旧 Phase 3 の後継)** — 旧「自律バイオリズム (1 時間サイクル)」構想は v1/v2 の実機診断を経て**時間割モデル**に置き換わった: 習慣テンプレート (ユーザーとペルソナの合意で固定した一日の枠) + 判断点 (起床/就寝、専用 Playbook) + 予算付き作業セッション + コマ種別カタログ ([`timetable_redesign.md`](../intent/timetable_redesign.md))。骨格・時間割改修・実行台帳 (W1〜W8) まで実装済み — ただし **v0.3 は止め具で運転を凍結** (`saiverse/autonomy_wiring.py` の `AUTONOMOUS_DRIVING_SHIPPED = False`、[`autonomous_behavior_v3.md`](../intent/autonomous_behavior_v3.md) §11.1)。判断点・見張り・時間割のコマは発火しないので、この領域の実機検証は **v0.4 で運転を配線するときに再開**する (2026-08-23 まはー裁定で関連 6 件を台帳から外して凍結)
- 🟡 **モデル格 (Aspect) 再設計** — v1 由来の 4 席を「声 (会話・標準) / 監督 (標準・発注検収・キャッシュ保温) / 手足 (軽量)」へ引き直す。門 §2-1、設計議論は Wave 2
- 🔲 **Phase 4 相当: 恒常入力処理** — RSS フィード施設 (§7) が第一弾として実装済み。カメラ / X 等は v0.4+

---

## 2. 認知モデル整備 (Persona Cognition)

> Intent: `docs/intent/persona_cognition/`。概念は [`landscape.md`](landscape.md) §3〜§4 を参照。

- ✅ **Track** 実装済 — ただし方向は**役割縮小 → 溶解** ([`recall_tags_and_track_reduction.md`](../intent/persona_cognition/recall_tags_and_track_reduction.md))。行動への指令をやめ、体験の帳簿になる
- ✅ **入れ子サブライン Spell**（`/run_playbook`、深さ4段、`report_to_parent`）実装済（v0.24）
- ✅ **aspect** v0.2 実装済・実機検証済 (2026-07-08)。**再設計 (3 席化) が門 §2-1 で予定されている** (§1)
- ✅ **エピソード (出来事)** — 記憶と世界の共通単位。開き/閉じ・層0タグ (origin_episode)・Lv1 整列生成まで実装済み。**単位の確定と LoD 搭載制御が門 Wave 1 の設計議論** ([`episode.md`](../intent/episode.md))
- 🟡 **Metabolism / head / Anchor** 実装済 (`sea/head_pipeline/`)。編纂の発火は予算超過一本 + 保守経路
- 🔲 **Social Track 入口（ペルソナ間会話）** — 凍結継続 (6/27 の線でも既にスコープ外、門 §5)。ペルソナ間会話の機序はここが入るまで成立しない
- 🔲 **短期記憶 → 長期記憶の選別**（→ [issue](../issues/short_term_to_long_term_memory_filtering.md)）
- 🔲 **Beat の型導入**（概念は確立、実装に型なし → [issue](../issues/beat_concept_not_typed_in_implementation.md)。Beat 境界の知覚問題は門 §2-1 に相乗り）

---

## 3. 記憶階層

> 概念は [`landscape.md`](landscape.md) §5 を参照。

- ✅ **Chronicle 二重パイプライン統合**（Metabolism 時に Chronicle 生成と Fragment 生成が同バッチ連動）
- ✅ **Memory Atlas** — 記憶概念の統合 (編纂三層 + 統一スペル + 目次 + レジストリ)。P4 全片完了 (2026-07-11)
- ✅ **あらすじのレベル制** (W4、2026-07-28) — 恒等圧縮を廃止し「小さくても要約する」整列計画 (`sai_memory/arasuji/`) に世代交代。実機検証は門 Wave 5
- 🟡 **Memopedia Fragment 化**（稼働中）。Fragment 専用 embedding 生成フローは未実装
- 🔲 **想起用タグ** — 記憶接続の要 (門 §2-2、Wave 4 実装予定)。遡及不能のため出荷前必須
- 🔲 旧 `note_extractor` の整理（本番経路は `entity_extractor` に移行済、名残の掃除）
- ✅ **Building log の DB 化**（`saiverse.db` への building_messages テーブル化 + 視点別レンダリング）

---

## 4. アドオン基盤

> Intent: `docs/intent/addon_catalog_management.md`。

- ✅ **Phase 1**: カタログ + ワンタッチ導入 + manifest v2 + 永続データ規約統一（`addon_data/<id>/`）
- 🔵 **Phase 2**: registry + API（着手前）
- 🟡 **拡張点（Extension Points）**: OAuth / Integration は一部実装が intent doc を先行（X-addon の oauth_flows）。整理が必要

---

## 5. 世界モデルの拡張

> Persona / Item に続く、世界モデルの新しい存在論。概念は [`landscape.md`](landscape.md) §2 を参照。

### Observer / Fixture（固定設置物と定期観測）

- 🔵 **Fixture** — 持ち運べない固定設置物（リンゴの木、センサー、掲示板）。Building 直結。世界モデルの**第三の存在論**（Persona / Item に続く）。`observer.md` v0.1、骨子合意済み・**未実装**
- 🔵 **Observer** — 定期実行能力を持つ Fixture。EventScheduler 相乗りで定期観測 → `observer_metrics` に時系列蓄積 → 閾値/変化で通知。発端は SGP30（1Hz 連続ポーリング要のステートフルセンサー）。v0.3 自律稼働 / Phase 4 恒常入力処理の足場

### Vessel（Building × 現実デバイス = ペルソナの身体）

- 🟡 **Vessel** — ペルソナを物理デバイス（Stack-chan 等）の身体に「降ろす」機構。**Vessel Building にペルソナが居る間、物理 I/O が身体感覚になる**（マイク=耳 / スピーカー=口 / カメラ=目 / タッチ=触覚）。`stackchan_vessel.md` v0.8
  - 本体フック: `Building.PHYSICAL_VESSEL_ID` カラム（**実装済み**）+ MCP client の Building 単位 visibility
  - 実装: スタックチャンアドオン（Phase 4.5、stackchan-mcp 経由）
  - 🔲 **本体への汎用化** — 現状スタックチャン専用。「Building × 現実デバイス = ペルソナの器」という汎用 Vessel システムへ昇格させる構想

---

## 6. ユーザー導入導線（オンボーディング）

> 新規ユーザーが SAIVerse を導入・習熟するまでの導線。現状チュートリアルとマニュアルが手薄なのが課題。
> **2026-08-09 まはー裁定**: 必要になるが独立して出来る作業のため、**v0.3.0 の門とは切り離して進める**。

- 🟡 **チュートリアル** — `frontend/src/components/tutorial/`（PersonaWizard / StepPersonaChoice 等）。最低限のみ実装。**拡充が課題**
- 🔲 **ユーザー向けマニュアル** — 利用者目線の概念解説（開発者向けリファレンス §10 とは別物）が存在しない。導入のハードルになっている
- ✅ **ログインポート** — 過去の対話履歴を持ち込む導線。ChatGPT 公式エクスポート ZIP（`conversations.json`、分割ファイル対応）+ Chrome 拡張機能エクスポートをパースして SAIMemory にインポート（`tools/utilities/chatgpt_importer.py` / `api/routes/people/import_chatlog.py` / `MemoryImportForm.tsx`）。source 検出失敗時はヘッダー日付でタイムスタンプ補完

---

## 7. 外部統合（構想・部分実装）

> 外部イベント統合の優先度や詳細は各 intent doc / memory を参照。

- ✅ **RSS フィード施設** — Building 単位の購読 + プリセット施設化 + 知覚バッファ投入 (`rss_feed_intake.md`、2026-08-03 実装完了)。世界の供給側の第一弾。実機検証は門 Wave 5
- 🟡 **SwitchBot**（Intent doc draft 済、レビュー待ち。Cloud API v1.1 / 入出力両経路。Observer の利用者）
- 🟡 **voice-tts**（GPT-SoVITS、実装済。GIL 飢餓問題対応中）
- 🟡 **stack-chan**（Vessel 統合 §5 + 能動入力 BLE HID リモコン構想）
- 🔲 **Discord**（見守り機能の軽量アドオン化構想）
- 🔲 **Withings 連携**（定期データ取得 + 通知ペルソナ、構想）
- 🟡 **SearXNG**（メタ検索エンジン。3層マージ設定基盤実装済: SearXNG ベース → `searxng_engine_defaults.yml`（SAIVerse 推奨） → `searxng_user_engines.yml`（ユーザー三値オーバーライド）。検索ツールにカテゴリ指定追加済、web_research プレイブックでサイクルごと動的カテゴリ選択可能。**⚠️ 次回バージョンアップ時に既存ユーザーの settings.yml リセット検証が必要**）
- 🔲 **Kitchen**（長時間処理の汎用基盤、未着手の予定要素）
- 🔲 **Elicitation**（MCP 応答待ち、投稿前確認の標準化。優先度3位）

---

## 8. 復活予定

- 🧊 **SDS / multi-city**（**2026-07-16 まはー裁定で凍結・入口封鎖済み** — inter-city API は 503、VisitingAI/ThinkingRequest polling は不起動。dispatch 確定処理未実装の欠陥が一次監査で判明したため。復活時は [persona_city_building 監査](../handoff/2026-07-15_persona_city_building_separation_audit.md)の修正方針を正典に再設計。SDS 自体は元より冬眠中 → [landscape §8](landscape.md)）

---

## 9. 後回し課題 (docs/issues/)

未解決の課題は `docs/issues/*.md`、解決済みは `archive/` に移動（詳細は `docs/issues/README.md`）。現役 issue は `ls docs/issues/*.md` で確認する。

地図作成で起票したもの:
- [Beat 概念が実装に型として存在しない](../issues/beat_concept_not_typed_in_implementation.md)
- [短期記憶 → 長期記憶の選別（システム通知を入口で止める）](../issues/short_term_to_long_term_memory_filtering.md)

掃除候補（[`landscape.md`](landscape.md) §9）: Blueprint（テーブル残存・未運用）/ Emotion（未活用）/ task（ほぼ死亡）/ working_memory（実装死亡・Session へ）/ note_extractor（移行名残）/ ConversationManager（no-op）

---

## 10. 各概念のリファレンス文書化 (TODO Phase)

地図完成後の次フェーズとして、各概念の解説ドキュメントを `docs/concepts/` 配下に整備する。
現状、概念解説が intent doc と issue にしか存在しないため、独立したリファレンスが必要。
（[`landscape.md`](landscape.md) の各章が、その種となる）

> **2軸ある**: ここ（§10）は**開発者向け**の概念リファレンス。§6 のユーザー向けマニュアルは**利用者目線**で別物。両方とも未整備。
