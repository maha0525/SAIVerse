# SAIVerse 進捗マップ (Roadmap Status)

> **ステータス**: v1.1 (2026-05-29 — Now セクション + 世界モデルの拡張を追加)
> **位置づけ**: 「何が予定されていて、いまどこにいるか」を一望する。概念の関係を示す
> [`landscape.md`](landscape.md) と対をなす、実装の現在地の地図。
> **ステータス記法**: ✅ 完了 / 🟡 進行中 / 🔵 起草中 / 🔲 着手前 / 💤 冬眠

---

## 0. いま動いているもの (Now)

> まはーが最近手を動かしている領域。思いつきで着手が分散しがちなので、現在地をここに集約する。

- 🟡 **認知モデル更新** — Track / aspect 実装済、session（短期記憶）起草中。次の山は **Social Track 入口（ペルソナ間会話）**（§2）
- 🔵 **Observer / Fixture** — SwitchBot 連携中に浮上した「定期観測する固定設置物」概念（§5）。`observer.md` v0.1、骨子合意済み・未実装
- 🟡 **スタックチャン → Vessel** — ペルソナを物理デバイスに降ろす Vessel 統合。アドオン Phase 4.5 実装済み。**本体の汎用 Vessel システムへの昇格**が次（§5）

---

## 1. 中心軸: 自律稼働 (v0.3.0)

> v0.3.0 の中心軸は「**自律稼働**（AI が連続して動き続ける）」。
> 以下に並ぶ認知モデル整備・記憶階層は、すべてこの自律稼働を実現するための手段である。

- **目標**: 4 月完遂
- ✅ **Phase 1**: pulse_logs / Important フラグ / 自動タグ付け / サブエージェント隔離
- ✅ **Phase 2**: Chronicle / Memopedia 実装 / Spell 化吸収
- 🟡 **Phase 3**: 自律バイオリズム（1 時間サイクル、活動種別 = conversation / creation / memory_organization / web_research / self_reflection、Claude 検収 + 意思決定 → 軽量実行 → 検収レポート。割り込みはタスク一時停止 → 対応 → 再開で並列しない）
- 🔲 **Phase 4**: 恒常入力処理（カメラ、X 等）
- 🔲 **Phase 5**: 認知モデル本格化（最初の達成目標 = UC-2 割り込みと復帰）

---

## 2. 認知モデル整備 (Persona Cognition)

> Intent: `docs/intent/persona_cognition/`。概念は [`landscape.md`](landscape.md) §3〜§4 を参照。

- ✅ **Track** 実装・拡張中
- ✅ **入れ子サブライン Spell**（`/run_playbook`、深さ4段、`report_to_parent`）実装済（v0.24）
- ✅ **aspect** v0.2 実装済・実機検証待ち
- 🔵 **session（= 短期記憶 / ワーキングメモリ）** 起草中（`docs/intent/session.md` v0.1）— 統一制御単位はコード未実装。Session は「節と節の間」という時間区間に留まらず、**ペルソナが今見ている短期記憶**（長期記憶の末尾・head・進行中 Beat・外界入力・システム通知の集約）であり、全 LLM 判断の入力ハブ
- 🟡 **Metabolism / head / Anchor** 機構は Phase 1 実装済（`sea/head_pipeline/`）。Session 概念への統合は検討中
- 🔲 **Social Track 入口（ペルソナ間会話）** — `SocialTrackHandler` と Track 自動作成（ensure_track）はあるが、**「他ペルソナ発話イベントの受け口」が未実装**（Phase B-Y）。「相手は誰か」判定もこれから。ペルソナ間会話の機序はここが入るまで成立しない
- 🔲 **短期記憶 → 長期記憶の選別**（システム通知を長期記憶に渡さない入口選別 → [issue](../issues/short_term_to_long_term_memory_filtering.md)）
- 🔲 **Beat の型導入**（概念は確立、実装に型なし → [issue](../issues/beat_concept_not_typed_in_implementation.md)）
- 🔲 **Phase 5 土台**: A=tick/パラメータ/内部alert、B=時間差ツール、C=Social 運用化（UC-2 は C 軸）

---

## 3. 記憶階層

> 概念は [`landscape.md`](landscape.md) §5 を参照。

- ✅ **Chronicle 二重パイプライン統合**（Metabolism 時に Chronicle 生成と Fragment 生成が同バッチ連動）
- 🟡 **Memopedia Fragment 化**（稼働中、air_city_a で 1162 件）。Fragment 専用 embedding 生成フローは未実装
- 🔲 旧 `note_extractor` の整理（本番経路は `entity_extractor` に移行済、名残の掃除）
- 🔵 **Building log の DB 化**（`saiverse.db` への building_messages テーブル化 + 視点別レンダリング、Phase 2.5 候補）

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

## 6. 外部統合（構想・部分実装）

> 外部イベント統合の優先度や詳細は各 intent doc / memory を参照。

- 🟡 **SwitchBot**（Intent doc draft 済、レビュー待ち。Cloud API v1.1 / 入出力両経路。Observer の利用者）
- 🟡 **voice-tts**（GPT-SoVITS、実装済。GIL 飢餓問題対応中）
- 🟡 **stack-chan**（Vessel 統合 §5 + 能動入力 BLE HID リモコン構想）
- 🔲 **Discord**（ナチュレ見守り体制の軽量アドオン化構想）
- 🔲 **Withings / 健康モニタリング**（見守りペルソナ + メール通報、構想）
- 🔲 **Kitchen**（長時間処理の汎用基盤、未着手の予定要素）
- 🔲 **Elicitation**（MCP 応答待ち、投稿前確認の標準化。優先度3位）

---

## 7. 復活予定

- 💤 **SDS / multi-city**（Nature109 作。現状単一 City 運用のため停止中。将来 inter-city travel を復活させる際に再起動）

---

## 8. 後回し課題 (docs/issues/)

未解決の課題は `docs/issues/*.md`、解決済みは `archive/` に移動（詳細は `docs/issues/README.md`）。現役 issue は `ls docs/issues/*.md` で確認する。

地図作成で起票したもの:
- [Beat 概念が実装に型として存在しない](../issues/beat_concept_not_typed_in_implementation.md)
- [短期記憶 → 長期記憶の選別（システム通知を入口で止める）](../issues/short_term_to_long_term_memory_filtering.md)

掃除候補（[`landscape.md`](landscape.md) §9）: Blueprint（テーブル残存・未運用）/ Emotion（未活用）/ task（ほぼ死亡）/ working_memory（実装死亡・Session へ）/ note_extractor（移行名残）/ ConversationManager（no-op）

---

## 9. 各概念のリファレンス文書化 (TODO Phase)

地図完成後の次フェーズとして、各概念の解説ドキュメントを `docs/concepts/` 配下に整備する。
現状、概念解説が intent doc と issue にしか存在しないため、独立したリファレンスが必要。
（[`landscape.md`](landscape.md) の各章が、その種となる）
