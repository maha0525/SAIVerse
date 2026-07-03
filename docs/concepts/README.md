# 概念リファレンス（Developer Concepts）

SAIVerse の中核概念を「**何で・どう動き・どこに実装され・どう増やすか**」の観点で解説する開発者向けリファレンス。

- **全体の位置づけ・概念どうしの関係**は [`overview/landscape.md`](../overview/landscape.md)（俯瞰地図）を見る
- **なぜ作られたか（設計意図・不変条件）**は各ページからリンクする `docs/intent/` を見る
- **実装の現在地（Phase / 進捗）**は [`overview/roadmap_status.md`](../overview/roadmap_status.md) を見る

このディレクトリは「概念 → 実装への入口」のナビ。ページの雛形は [`spell.md`](spell.md)。

---

## 世界の構成（landscape §2）

- [Persona](persona.md) — 考え・選択し・行動する AI 主体
- [Building / City](building-city.md) — 共有メッセージ場（チャットUI）と、それを束ねる世界
- [Item](item.md) — 持ち運べる物 / 拡張中の存在論（Fixture・Observer・Vessel）

## 駆動（landscape §3）

- [Pulse / PulseController](pulse.md) — 認知サイクルと、その起動制御・時間機構
- [Track / Handler](track.md) — 進行中の作業文脈（行動の線）と種別ごとの振る舞い
- [Meta-Judgment](meta-judgment.md) — どの Track を動かすか決める上位視点
- [line / aspect](line.md) — Track 内の処理レーンとキャッシュ制御

## 行動（landscape §4）

- [Beat](beat.md) — ペルソナの最小行動単位
- [Playbook](playbook.md) — 構造化された行動フロー（LLM/tool グラフ）
- [Spell](spell.md) — 平文応答から Tool を呼ぶ構文（ネイティブツールコール撲滅）
- [Tool](tool.md) — 実行の単位
- [Phenomena](phenomena.md) — 世界側からのイベント入口

## 長期記憶（landscape §5）

- [SAIMemory](saimemory.md) — 長期記憶の容れ物（生ログ Thread/Message）
- [Chronicle](chronicle.md) — 時系列圧縮 / Track 再開
- [Memopedia](memopedia.md) — 知識グラフ

## 短期記憶と節目（landscape §6）

- [Session / head](session.md) — ペルソナが今見ているもの、と cache の効く安定領域
- [Metabolism / Anchor](metabolism.md) — 短期リフレッシュ + 長期結晶化の節目

## 拡張（landscape §7）

- [Addon](addon.md) — 拡張点を束ねる配布・導入単位
- [MCP / Elicitation](mcp.md) — 外部ツールサーバー接続

## 冬眠中（landscape §8）

- [SDS](sds.md) — 複数 City を発見するレジストリ（inter-city travel の前提）

---

## legacy/

[`legacy/`](legacy/) は旧 concepts ドキュメント（2026-05-29 のリファレンス整備以前）の退避先。内容が古いので参照時は上記の現行ページを優先する。
