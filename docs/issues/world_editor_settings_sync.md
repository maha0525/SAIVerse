# Issue: ワールドエディタが他経路の設定追加に追いついていない(同期監査)

**ステータス**: 🔲 未着手
**優先度**: mid
**作成日**: 2026-07-08
**関連**: `frontend/src/components/settings/WorldEditor.tsx` ↔ 各設定モーダル(`GlobalSettingsModal.tsx`, `BuildingSettingsModal.tsx`, `PersonaProfileModal.tsx` 等)

## 背景

City / Building / Persona などの設定項目が、個別の設定モーダルやチャット経路で色々**足されてきた**のに、俯瞰編集する側の **ワールドエディタ (`WorldEditor.tsx`) が追いついていない**。本来はワールドエディタと個別経路で同じ項目を編集できるべき(同期しているべき)。

## 調査事項(棚卸しが主体)

1. `WorldEditor.tsx` が現在編集できる項目を、対象(City / Building / Persona / …)ごとに列挙する。
2. 各個別経路(`GlobalSettingsModal` / `BuildingSettingsModal` / `PersonaProfileModal` / ワールド作成ウィザード等)が編集できる項目を列挙する。
3. **差分表**を作る = 「個別経路にはあるがワールドエディタに無い項目」を洗い出す。これが埋めるべき穴。

## 解決案候補

- 差分の項目をワールドエディタに追加していく(短期)。
- 中長期: 設定項目の**定義を単一ソース化**し、ワールドエディタと個別モーダルが同じ定義から UI を生成する仕組みにして、今後のドリフトを構造的に防ぐ(要設計判断。やりすぎない範囲で)。

## 注意

- どこまでワールドエディタに集約するかは情報設計の判断を含むので、差分表を出した上で**まはーと優先順位**を決める(全項目を機械的に足すのが正解とは限らない)。

## 関連リソース

- `frontend/src/components/settings/WorldEditor.tsx`
- `frontend/src/components/GlobalSettingsModal.tsx` / `BuildingSettingsModal.tsx` / `PersonaProfileModal.tsx`
- アイディア帳: `docs/overview/ideas.md`「UI / プラットフォーム」

## ログ

- 2026-07-08: 起票。ideas.md から昇格。まず差分表の作成から着手する。
