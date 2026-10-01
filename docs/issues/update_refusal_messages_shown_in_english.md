# 更新や版の切り替えを断るときの理由が、日本語の画面に英語のまま出る

**状態**: 未着手 (2026-10-01 発見。直す時期はまはーの裁定待ち — メティスの推奨は「最初のアーリーアクセス版を配る前」)
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

版の切り替えだけでなく、通常の更新を断るときの理由 (手元に変更がある、など) も同じ更新プログラムが組み立てているので、同じ形で英語のまま出る可能性がある。どの文面が画面まで届くかの数え上げは、まだしていない。

## 決めること

- いつ直すか。最初のアーリーアクセス版を配る日は、利用者がこの入口を初めて本気で押す日なので、それまでに直っているのが望ましい。
- どこまで直すか。版の切り替えの断りだけか、通常の更新の断りも含めるか。
