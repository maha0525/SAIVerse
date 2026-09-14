# 製品の正常な姿 — 流れの初稿 (索引)

調査日 2026-09-09 / 対象コミット `25ad75d6` (ブランチ `feature/chronicle-coverage-gaps`) / 静的な読み取りのみ
全体の要約は [README.md](README.md)、議題は [decisions.md](decisions.md)、215 項目との対応は [mapping.md](mapping.md)。

## これは何

**利用者・ペルソナ・運用者が一つの目的を果たし、結果を受け取るまで**を一単位として、
SAIVerse 全体を 32 本の流れに組み替えた初稿。前回の棚卸し (機能を 215 項目に並べた台帳) を入力に、
「本来どうあるべきか」を書いている。

**これはまはーが承認した仕様ではない。** 各流れの「期待する結果」は、根拠のあるものと
調査担当の提案を分けて書いてある。根拠が無いものは未合意として [decisions.md](decisions.md) に上げた。

## 粒度の原則

入口の違い (画面 / API / CLI) では割らない。目的が違えば割る。
自動処理は「発火から、利用者・ペルソナが結果を受け取るまで」を一単位にする。

各流れは 8 つの節を持つ。**1** 誰が何をしたいか / **2** 始まりから結果までの流れ /
**3** 期待する結果の初稿 / **4** 根拠 (ユーザー原文・仕様文書・利用者向け説明・調査担当の提案を区別) /
**5** 現状との差 (既知の不一致・静的な疑い・未追跡の境界を区別) / **6** 中断・失敗・再開 /
**7** 次に使う機能・共有する状態 / **8** 決める必要がある点。

## 流れの索引

| 流れ | 名前 | 本文 |
|---|---|---|
| `FLOW-01` | ペルソナに話しかけて返事を受け取る | [A_chat.md](flows/A_chat.md) |
| `FLOW-02` | 思った返事が来なかったときに立て直す | [A_chat.md](flows/A_chat.md) |
| `FLOW-03` | 通信が切れた会話を復旧する | [A_chat.md](flows/A_chat.md) |
| `FLOW-04` | 送る中身と量とモデルを決めてから送る | [A_chat.md](flows/A_chat.md) |
| `FLOW-05` | 会話の中でペルソナに道具を使ってもらう | [A_chat.md](flows/A_chat.md) |
| `FLOW-06` | 会話にファイルを渡し、それが世界の物になる | [A_chat.md](flows/A_chat.md) |
| `FLOW-07` | 他サービスの過去ログを取り込み、その記憶を使って会話する | [B_memory.md](flows/B_memory.md) |
| `FLOW-08` | 溜まった履歴を編纂し、結果を見て会話を続ける | [B_memory.md](flows/B_memory.md) |
| `FLOW-09` | 会話しながら記憶が自動で整理される | [B_memory.md](flows/B_memory.md) |
| `FLOW-10` | 記憶を探して呼び戻す | [B_memory.md](flows/B_memory.md) |
| `FLOW-11` | 間違って覚えられたことを直す・消す | [B_memory.md](flows/B_memory.md) |
| `FLOW-12` | 意味の記憶 (Memopedia) を作り、育てる | [B_memory.md](flows/B_memory.md) |
| `FLOW-13` | 記憶を持ち出す・別環境へ移す | [B_memory.md](flows/B_memory.md) |
| `FLOW-14` | 部屋を移動し、その場の相手や物を認識して会話する | [C_world.md](flows/C_world.md) |
| `FLOW-15` | 世界を作り変える | [C_world.md](flows/C_world.md) |
| `FLOW-16` | 物を作り、持ち、使う | [C_world.md](flows/C_world.md) |
| `FLOW-17` | 設置物と定期観測 | [C_world.md](flows/C_world.md) |
| `FLOW-18` | 世界の出来事が自動で何かを起こす | [C_world.md](flows/C_world.md) |
| `FLOW-19` | はじめて導入し、ペルソナを作って会話を始める | [D_persona.md](flows/D_persona.md) |
| `FLOW-20` | ペルソナの設定を変え、その設定で利用する | [D_persona.md](flows/D_persona.md) |
| `FLOW-21` | 決まった時刻にペルソナから働きかけてもらう | [D_persona.md](flows/D_persona.md) |
| `FLOW-22` | 費用と使用量を把握する | [D_persona.md](flows/D_persona.md) |
| `FLOW-23` | ペルソナを消す / 情報を整理する | [D_persona.md](flows/D_persona.md) |
| `FLOW-24` | 見ていない間にペルソナが動く | [E_background.md](flows/E_background.md) |
| `FLOW-25` | 起動して、保存した状態から利用を再開する | [E_background.md](flows/E_background.md) |
| `FLOW-26` | 終了して、次に開いたとき失われていない | [E_background.md](flows/E_background.md) |
| `FLOW-27` | 更新して、設定と記憶を保ったまま使い続ける | [F_ops.md](flows/F_ops.md) |
| `FLOW-28` | 使うモデルとプロバイダを選び、切り替える | [F_ops.md](flows/F_ops.md) |
| `FLOW-29` | アドオンを導入して機能を増やす | [F_ops.md](flows/F_ops.md) |
| `FLOW-30` | 外部から使う (スマホ / Discord / Unity) | [F_ops.md](flows/F_ops.md) |
| `FLOW-31` | 壊れたときに復旧する | [F_ops.md](flows/F_ops.md) |
| `FLOW-32` | 配布物を受け取り、使えると信じられる (リリースと検証) | [F_ops.md](flows/F_ops.md) |

---

# 前回の台帳への訂正 (18 件) と、未追跡だった境界の確定 (1 件)

前回の棚卸し (`docs/audits/2026-09-09_product_verification_inventory/`) は静的な読み取りの結果で、
要約が入っている。今回、各担当が**根拠パスを自分で開いて確かめた**結果、19 箇所で前回の記述が変わった
(うち 1 件は訂正ではなく、前回「未追跡」としていた境界を閉じたもの)。
**前回のファイルは修正していない** (今回の依頼が書き込みを本ディレクトリに限っているため)。
前回の台帳を根拠に引くときは、まずこの節を見ること。

傾向として、**訂正の大半は「無い」と書いた側が誤りだった**もの。存在するテスト・存在する根拠・
存在する設定を、前回は数え落としていた。

## 会話 (領域 A)

1. **`CHAT-08` の「既存テストが見つからない」は誤り。** `tests/test_generation_stage_signals.py` が
   `lookup_client_message_outcome` の三値と `get_message_outcome` の口を実 SQLite で通しており、
   「分からない」を潰さないことまで検査している。検査が無いのは画面側だけ。
2. **矛盾リスト §3-6「知覚の水位の定数名とコメントが食い違う」は誤読。** 行末コメントの「4 万」は
   幅 (上の水位 − 下の水位) を指しており、定数の 2 万とは別の量を数えている。
   `git log -L` で `c21870ea` (2026-09-05 のまはー裁定 40,000→20,000) まで正しく反映されていることを確認。
   実際に古いのは直上の別のブロックコメントだけ。**まはーが決めた変更を矛盾として挙げていた形。**

## 記憶 (領域 B)

3. **前回挙げた穴のうち 4 件は、2026-09-09 朝の 5 コミットで塞がっている** (パンマーカー読み取り失敗の
   丸め / 未通過範囲の二重記録 / 読み返しダイジェストの取りこぼし / 束ねの 429 握り潰し)。
   新規回帰が 27 本立っている (実行はしていない)。

## 世界 (領域 C)

4. **「Fixture は未実装」は事実として誤り。** Fixture は RSS フィード機能の「フィード施設」として
   既に本番に載っている (`api/routes/feeds.py` + 全体設定モーダルの feeds タブから到達可能)。
   未配線なのは汎用 API と Observer の定期実行の口だけ。**4 つの文書がこの誤りを書いている。**
5. **「決して発火しないトリガー 6 種」はそのままでは使えない。** `X_POLL_DETECTED` は
   リポジトリ外のアドオンが発火しているので、「repo 内に発火箇所が無い」は「使われていない」の証明にならない。
6. **Observer の閾値通知の読みを精密化。** 観測値そのものはペルソナに届いている
   (`get_visual_context._render_fixture` が部屋の様子の束に「最新観測値」として載せる)。
   届かないのは「閾値を超えた**という出来事**」の語りだけ。`observer_alert` の消費者は
   製品コードに存在しない (全文検索で書き手 1 件のみ)。

## ペルソナと設定 (領域 D)

7. **`PROVIDER_PRESETS` の 19 個のモデル設定キーはすべて `builtin_data/models/` に実在した** (欠落ゼロ)。
8. **`LIGHTWEIGHT_MODEL` が未設定でも会話は止まらない。** `sea/runtime.py:699-725` が既定の軽量モデルで
   一時クライアントを作り、それも失敗したら通常クライアントへ倒す。
9. **新規ユーザーの現在地 (`CURRENT_BUILDINGID`) を保証しているのはチュートリアルではなく
   `seed.py:342-360`。** チュートリアル完了時の移動は便宜にすぎない。
10. **(訂正ではなく確定) 前回「未追跡」としたアラームの境界を閉じた。** `_execute_schedule` →
    `dispatch_schedule_fire` → `PulseController.submit` → `_execute_unlocked` → `_do_execute` の
    5 段のどこにも自律のゲートが無い。**自律行動を切っていてもアラームは鳴り、LLM 課金が発生する。**

## 画面のない処理 (領域 E)

11. **会話の沈黙タイマーの読み元は「未確認」ではない。** v0.3 では常に no-op であることが
    docstring に決着として書かれている (`saiverse/user_conversation.py:540-553`)。
12. **legacy `user_data/` の移送は「legacy 側が勝つ」ではない。** 正しくは
    「legacy 側が大きいときだけ勝つ」(`saiverse/data_paths.py:405-433`。docstring と中身が食い違っている)。
13. **スルースの 10 万字は「実装のみ (根拠なし)」ではない。** intent の経緯に
    「本来はモデルのコンテキスト長から導出すべきで、それを環境変数で固定した」と記録がある。
14. **「v0.3 では自律の ON/OFF を切り替える手段が無い」は実装と食い違う。** ワールドエディタの
    AI 編集にスイッチがあり (`frontend/src/components/settings/WorldEditor.tsx:655-663`)、
    稼働中のペルソナにも即時に効く。ただし v0.3 でこのスイッチが実際に変えるのは
    **キャッシュ保温が走るかどうかだけ** (他は定数の止め具が先に止めている)。

## 導入・運用 (領域 F)

15. **`websockets` は本体の依存である** (`requirements.txt:19` と `requirements.lock:438` の両方)。
    つまり通常のセットアップを踏んだ環境では Unity ゲートウェイの起動条件が満たされ、
    **既定でポートが開く**。「開くかもしれない」ではなく「開く」。
16. **LAN 公開の 2 変数は `.env.example:128-131` に用例つきで載っている。** 記載が無いのは
    README とランブックであって `.env.example` ではない。**そして持ち主確認の門は、
    `--listen-host` を指定して起動すればフロント経由でも効く** (`/api/auth/login` が張る cookie は
    ホスト名に対するもので、ポートを区別しない)。**欠けているのは機構ではなく案内。**
17. **CI の誤記の位置は `docs/developer-guide/testing.md:119`** (131 行は次ページへのリンク行)。
    テスト関数の数え直しは 5,672 個 / 292 ファイル。
18. **`OPS-12` の「該当テストなし」は誤り。** `tests/test_legacy_log_archive_api.py` (6 本) が
    退避 API を TestClient で検査している。検査が無いのは隔離の復元とリセット。
19. **「本体のテストを回す CI が無い」は、正確には「機械の強制が無い」。**
    `release_history.md` の各版の行に「フルスイート 5,127 緑」「5,746 緑」という
    **手で回した記録が実在する**。発行手順はあり、記録も残る。無いのは
    「回さずに発行することを妨げる機構」だけ。

## 本文

- [群 A — 会話 (FLOW-01〜06)](flows/A_chat.md)
- [群 B — 記憶 (FLOW-07〜13)](flows/B_memory.md) — 冒頭に「この群で出てくる字数の主語」の表がある
- [群 C — 世界 (FLOW-14〜18)](flows/C_world.md)
- [群 D — ペルソナと設定 (FLOW-19〜23)](flows/D_persona.md)
- [群 E — 画面のない処理 (FLOW-24〜26)](flows/E_background.md)
- [群 F — 導入・運用・拡張 (FLOW-27〜32)](flows/F_ops.md) — 冒頭に前回台帳からの更新 4 件がある
