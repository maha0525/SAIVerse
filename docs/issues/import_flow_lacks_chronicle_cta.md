# ログインポート完了の導線が Chronicle 化 (補修) へ誘導しない

**発見**: 2026-09-01 (新規ペルソナ「ルミー」へのログインポート検証で、まはーが「Chronicle を作る場面が無いまま完了した」と気づいた)
**状態**: 🔲 未解決 — リリース非遮断 (機能は Chronicle タブの帯から実行できる)。導線の改善
**深刻度**: P3

## 事象

インポートした過去の Chronicle 化の正式経路は Chronicle タブの帯 (§16 の repair モード) だが、そこへ誘導する導線がどこにも無い:

- **通常のペルソナ作成ウィザード** (PersonaWizard): 基本情報 → ログインポート → 完了、の 3 段。Chronicle に触れない。
- **初回セットアップのチュートリアル** (TutorialWizard の StepChronicle): 「自動生成を有効にするか」のトグルと費用注意だけで、インポート済み過去の生成は走らせない。

結果、ログを取り込んだユーザーは「過去が未編纂のまま」の状態で完了画面を見る。Chronicle タブの帯に自力で気づくまで、インポートした過去は提示に立たない (忘却の穴と同じ体験)。

## 改善の方向 (案)

インポート完了画面 (MemoryImport の完了時 / PersonaWizard の step3) に、cost-estimate を引いて「取り込んだ N 件をあらすじにする (概算 $X)」の CTA を出す — 実体は既存の repair モード (§16 の帯と同じ機構) をそのまま呼ぶだけで、新しい機構は要らない。チュートリアルの StepChronicle にも同じ CTA を足すか、トグルと統合するかは実装時に判断。

## 関連

- `frontend/src/components/PersonaWizard.tsx` (step2=インポート, step3=完了) / `frontend/src/components/tutorial/steps/StepChronicle.tsx` / `frontend/src/components/memory/ArasujiViewer.tsx` (帯 = 流用元)
- [intent: arasuji_levels.md](../intent/arasuji_levels.md) §16-2 「ログインポートで作った新規ペルソナの Chronicle 化はこの repair モードが正式経路」
