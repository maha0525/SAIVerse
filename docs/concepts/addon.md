# Addon（拡張パッケージ）

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §7](../overview/landscape.md)、**設計意図**は intent [`addon_extension_points.md`](../intent/addon_extension_points.md) / [`addon_catalog_management.md`](../intent/addon_catalog_management.md) を参照。

## 一言で

[Tool](tool.md) / [Playbook](playbook.md) / [Phenomena](phenomena.md) / [MCP](mcp.md) サーバー / ペルソナフックを束ねて配布・導入・管理する単位。

## 役割

本体を書き換えずに、外部サービス連携や新しい能力をパッケージ単位で追加する拡張点。ワンタッチ導入・更新・削除の対象になる。

## 仕組み

### リソースの3層優先順位

拡張リソースは以下の優先順位で解決される:

```
~/.saiverse/user_data/  >  expansion_data/<addon>/  >  builtin_data/
（最優先）                   （中間）                    （最低）
```

これにより、ユーザーは expansion pack を上書きでき、expansion pack はビルトインを上書きできる。

### マニフェストと永続データ

- `addon.json`（**manifest v2**）で拡張点を宣言
- 永続データは `~/.saiverse/user_data/addon_data/<addon_id>/` に置く
- 導入は審査済みレジストリ経由のワンタッチ UI、または手動 git clone

### 提供できる拡張点

| 拡張点 | 中身 |
|---|---|
| Tools | `tools/` 配下の Tool |
| Playbooks | `playbooks/public/` の Playbook |
| Phenomena | 外部イベント源 |
| MCP サーバー | `mcp_servers.json` で宣言 |
| ペルソナフック | speak hooks 等 |

### 状況

既存アドオン（Elyth / voice-tts / stack-chan / X / ComfyUI ローカル画像生成）は v2 化済み。**カタログ機構は Phase 1〜4 実装済**。

## 増やし方（アドオン作成の要点）

- Pydantic を使うアドオンでは `from __future__ import annotations` を**使わない**（forward ref 解決が破綻する）
- MCP を含む場合は `mcp_servers.json` の `spell_tools[]` に登録しないと [Spell](spell.md) 化されない（`visible: true` も必要）
- native binary の同梱可否は「枯れた・小・寛容ライセンスなら同梱、発展中・大は CI DL + SHA256」

## 実装

- カタログ管理: intent [`addon_catalog_management.md`](../intent/addon_catalog_management.md)
- 拡張点ロード: 3層優先の resource loader（`tools/` / `playbooks/` / `phenomena/`）
- 永続データ: `~/.saiverse/user_data/addon_data/<addon_id>/`

## 関連概念

- [Tool](tool.md) / [Playbook](playbook.md) / [Phenomena](phenomena.md) — アドオンが提供する拡張点
- [MCP](mcp.md) — アドオンが内包する外部ツールサーバー
- [Spell](spell.md) — MCP tool を平文から呼べるようにする接続

## 参照

- intent: [`addon_extension_points.md`](../intent/addon_extension_points.md) / [`addon_catalog_management.md`](../intent/addon_catalog_management.md)
- 地図: [`landscape.md`](../overview/landscape.md) §7
