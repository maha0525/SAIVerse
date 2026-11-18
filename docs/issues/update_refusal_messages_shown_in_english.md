# 更新やバージョンの切り替えを断るときの理由が、日本語の画面に英語のまま出る

**状態**: 主要箇所の修正 PR 待ち (2026-10-01 発見 / 2026-11-14 更新・再埋め込み・ペルソナ作成の断り文面を日英化)
**深刻度**: P3 — 動作は正しい (断るべきときに断り、何も壊さない)。ただ、断られた理由を日本語の利用者が読めない

## 見つけた場面 (2026-10-01、まはーの実機)

グローバル設定でアーリーアクセス版への参加を押すと、日本語の画面の中に、次の英語の文がそのまま赤字で出た。

> Switching to the early-access branch is only possible from the main branch, but this checkout is on develop. The channel switch was not started.

まはーの手元は開発用のブランチ (develop) を向いているので、「参加は安定版 (main) からしかできない」という検査で断られた。これは設計どおりの断りである ([early_access_release.md](../intent/early_access_release.md))。

## 利用者が実際に見る文面

利用者の手元は main を向いているので、アーリーアクセス版がまだ配られていない今は、別の検査で断られる。main を向いた隔離の複製 (v0.3.18) で事前検査を走らせて確かめた文面は次のとおり。

> The early-access branch has not been published on origin yet, so there is nothing to switch to. The channel switch was not started.

こちらも英語で、branch や origin という git の言葉が入っている。

## なぜ起きるか

断りの理由は更新プログラム (`scripts/update_engine.py`) が英語の文として組み立て、画面はそれをそのまま表示している。UI の日英対応の intent ([localization.md](../intent/localization.md)) は「API は識別子とパラメータだけを返し、表示は画面側が持つ」を目指す形としているが、この経路はまだその形になっていない。

バージョンの切り替えだけでなく、通常の更新を断るときの理由 (手元に変更がある、など) も同じ更新プログラムが組み立てているので、同じ形で英語のまま出る可能性がある。どの文面が画面まで届くかの数え上げは、まだしていない。

## 決めること

- いつ直すか。最初のアーリーアクセス版を配る日は、利用者がこの入口を初めて本気で押す日なので、それまでに直っているのが望ましい。
- どこまで直すか。バージョンの切り替えの断りだけか、通常の更新の断りも含めるか。

## 掃討セッションの材料と対応状況

最初のアーリーアクセス版を配る前に、表示の言語が画面の設定と合っていない箇所をまとめて直す (2026-10-01 まはー)。

- **対応済み (2026-11-14 修正 PR)**: 更新とバージョンの切り替えを断る理由が、日本語の画面に英語で出る (`scripts/update_engine.py` の `UpdateError` に `ui_key` / `ui_params` を持たせ、`api/routes/system.py` が `ui_message` で返すように変更。あわせて `frontend/src/app/page.tsx` の更新確認ダイアログ・失敗フォールバックも `uiText` 化)。
- **対応済み (2026-11-14 修正 PR)**: `api/routes/system.py` の `psutil` 不在時の断り (409) が日本語ベタ書きだった箇所を `ui_message("api.systemUpdate.psutilRequired")` に変更。
- **未特定 (引き続き観察)**: 画面を英語にしていても、たまに日本語のメッセージが出ることがある (2026-10-01 まはーの実機。上記 `psutil` 以外の箇所があるかは次に見かけたときに特定する)。
- **対応済み (2026-11-14 修正 PR)**: エンベディングの再作成 (`POST /reembed`・`GET /reembed/status`) の進捗・完了・断りの文面を `api/routes/people/reembed.py` で `ui_message` 化。
- **対応済み (2026-11-14 修正 PR)**: ペルソナ作成の断り (名前重複・ID 文字種違反・ID 重複) を `manager/persona.py` の `CreationErrorDetail` と `frontend/src/components/PersonaWizard.tsx` で日英化。
