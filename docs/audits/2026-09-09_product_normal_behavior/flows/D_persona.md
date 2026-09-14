# 群 D: ペルソナと設定 — 流れごとの「正常な姿」初稿 (FLOW-19〜23)

対象コミット: `feature/chronicle-coverage-gaps` の `25ad75d6`。
すべて静的な読み取り。バックエンドもテストも実行していない。本番ペルソナには一切触れていない。

前回の台帳 (`docs/audits/2026-09-09_product_verification_inventory/inventory.md` 領域 D) を主な入力とし、
重要な主張は根拠パスを自分で開いて確かめた。確かめた結果が前回と違う箇所は各流れの §5 に明記した。

---

## FLOW-19: はじめて導入し、ペルソナを作って会話を始める

**対応する台帳項目**: `OPS-01`, `PERS-14`, `PERS-15`, `PERS-16`, `PERS-01`, `PERS-02`, `PERS-03`

### 1. 誰が何をしたいか

**利用者 (新規)** が、SAIVerse を自分の PC に入れて、自分のペルソナを 1 体持ち、そのペルソナと会話を始めたい。
この流れは新規ユーザーが必ず一度は通る一本道で、ここが通らないと製品が始まらない。

### 2. 始まりから結果までの流れ

- ZIP 展開または `git clone` → `setup.bat` / `setup.sh` を実行 (`README.md:129-200`)
- セットアップが Python 仮想環境・依存パッケージ・Node.js/Git・埋め込みモデルを用意し、
  DB が無ければ確認なしで `seed.py --force` を走らせる (`setup.bat:170-196`, `setup.sh:57-84`)
- seed が既定ユーザーの初期位置 (`CURRENT_CITYID` / `CURRENT_BUILDINGID` = `user_room_city_a`) を書く
  (`database/seed.py:342-360`)
- `start.bat` / `start.sh` でバックエンドとフロントエンドが立ち、ブラウザが `localhost:3000` を開く
- 画面が `GET /api/tutorial/status` を叩き、`tutorial_completed == false` または
  `needs_initial_setup == true` (City が 0 件または AI が 0 件) なら `TutorialWizard` が開く
  (`frontend/src/app/page.tsx:513-535`, `api/routes/tutorial.py:162-183`)
- 8 ステップ: Welcome → ユーザー名 → City 名 → **ペルソナ** → API キー → モデル設定 → Chronicle → 完了。
  スキップできるのは 2, 3, 5, 6, 7 で、**ペルソナ作成 (4) はスキップ対象に入っていない**
  (`TutorialWizard.tsx:67-76, 456`)
- ステップ 4 は `PersonaWizard` を埋め込みで開き、`POST /api/world/ais` が `ai` 行・私室 Building 行・
  在室ログ・(ユーザーが 1 人なら) `user_ai_link` を 1 トランザクションで作る (`manager/persona.py:444-695`)
- ステップ 5 の「次へ」で API キーを `.env` と `os.environ` に書き、`POST /api/tutorial/auto-configure-models` が
  キーのあるプロバイダを優先順で 1 つ選び、6 つのモデルロール env var を書き、
  `manager.update_default_model()` で基準モデルを差し替える (`TutorialWizard.tsx:186-210`, `api/routes/tutorial.py:489-575`)
- 完了ボタンで `POST /api/tutorial/complete` を打ち、`createdRoomId` があれば `POST /api/user/move` で
  そのペルソナの部屋へ移動する (`TutorialWizard.tsx:243-262`)
- 利用者がその部屋で最初の発言を送る (ここから先は FLOW-01)

### 3. 期待する結果の初稿

**根拠のある期待**

- 利用者は、コマンドラインを使わずに (Windows なら `setup.bat` と `start.bat` のダブルクリックだけで)
  ブラウザに動く画面が出るところまで到達できる。
- チュートリアルを最後まで通した利用者は、**自分のペルソナが 1 体存在し、自分がその部屋にいて、
  そこに話しかけると返事が返ってくる**状態を受け取る。README が「丁寧めなチュートリアルを搭載しています」と
  約束しているのは、この一本道を案内することである。
- 無料の API (Gemini 無料枠・NVIDIA NIM 等) だけでも会話が始められる。README が
  「無料で使えるAPIの導入もサポートしているので、気軽にお試し頂けます」と書いている。

**調査担当の提案 (根拠のある期待とは分けて読むこと)**

- スキップできるステップを飛ばした利用者が、**何がまだ揃っていないか**を完了画面で受け取れるとよい。
  現状はステップ 5 (API キー) を飛ばしても完了でき、そのことが完了画面には出ない。
  最初の発言を送って初めて分かる形になっている。
- 「導入が成功した」の判定は、利用者にとっては「返事が返った」ことである。
  チュートリアルの完了フラグ (`TUTORIAL_COMPLETED`) はその判定にはなっていない。

### 4. 根拠

- **ユーザー原文**: この流れについてのまはーの直接の発話は、今回の資料の中には見つからなかった。
- **既存の仕様文書**: `docs/getting-started/installation.md`、`docs/intent/dependency_management.md` §2-2
  (セットアップの依存導入)。`docs/overview/roadmap_status.md` §6 は
  「🟡 チュートリアル … 最低限のみ実装。拡充が課題」と、現状を未完成として位置づけている。
- **利用者向け説明**: `README.md:44`「丁寧めなチュートリアルを搭載しています」、
  `README.md:40-42` (無料枠の案内)、`README.md:129-200` (クイックスタート)。
  完了画面の文言「サイドバーの『システム』を開いて『チュートリアル』を選択してください」
  (`tutorial/steps/StepComplete.tsx`) が、後からやり直せることの約束になっている。
- **調査担当の提案**: 上記 §3 の後半 2 項目。

### 5. 現状との差

**前回の台帳で「未確認」だったものを、今回確かめた結果**

- `PROVIDER_PRESETS` が挙げる 19 個のモデル設定キーは、**すべて `builtin_data/models/` に実在した**
  (欠落ゼロ。`api/routes/tutorial.py:335-408` の値を 1 件ずつ照合)。
  前回の台帳が「実在するかは検証していない」としていた懸念は、この時点では発生していない。
- **軽量モデル未設定は、この経路では会話を止めない。** `sea/runtime.py:699-725` は、ペルソナに
  軽量クライアントが無ければ `SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL` (無ければ組み込み既定
  `gemini-3.1-flash-lite-preview`、`saiverse/model_defaults.py:11`) で一時クライアントを作り、
  それも作れなければ通常クライアントへ倒す。ウィザードが `LIGHTWEIGHT_MODEL` を NULL のまま作ることは
  事実だが、「一部のノードが動かない」という前提は、この経路については成立していない。
- **`CURRENT_BUILDINGID` を保証しているのはチュートリアルではなく seed である。**
  `database/seed.py:342-360` が初期化時に `user_room_city_a` を書く。チュートリアル完了時の
  `POST /api/user/move` は「作ったペルソナの部屋へ連れて行く」便宜であって、前提の担保元ではない。
  したがって移動が失敗しても、利用者は seed が置いた部屋に立っている。
- **キー入力前に作ったペルソナも、ステップ 5 で追随する。** `manager.update_default_model`
  (`saiverse/saiverse_manager.py:1461-1506`) は、DB に `DEFAULT_MODEL` を持たないロード済みペルソナに対して
  `persona.set_model(...)` を呼び直す。ペルソナ作成 (ステップ 4) が API キー入力 (ステップ 5) より
  前に起きる並びは事実だが、そのために古いクライアントが残る、という結果にはなっていない。

**既知の不一致**

- **ステップ 4 の 2 枚のカードは、どちらも同じ `PersonaWizard` を開く。**
  「他のプラットフォームから引き継ぐ」を選んでも、ログ取り込みの画面へ直行するのではなく、
  新規作成と同じ 1 枚目 (名前・ID・City・システムプロンプト) から始まり、取り込みは 2 枚目の
  スキップ可能なステップとして現れる (`StepPersonaChoice.tsx:20-28, 66-76`, `PersonaWizard.tsx:312-328`)。
  カードの説明文「ChatGPT等の会話ログをインポートして記憶を引き継ぎます」と、実際に開く画面が食い違う。
- **ステップ 5 を飛ばすと、ステップ 6 は空の割り当て一覧を表示し、その場で手動割り当てもできない。**
  `editMode` は `startAtStep === 6` のときだけ有効なので、初回の通し実行で飛ばした場合は
  編集モードに入らない (`TutorialWizard.tsx:186-210, 431-441`)。
- **`AdminService.create_ai` の成功メッセージ「A restart is required for the AI to become active.」は
  事実と合わない。** 同じ呼び出しの中で `_create_persona` が PersonaCore を `self.personas` に登録している
  (`manager/admin.py:1190-1197`, `manager/persona.py:679-685`)。ウィザードはこのメッセージを表示しないので
  画面上の利用者には見えないが、API を直接使う利用者には見える。
- **`docs/user-guide/global-settings.md` が挙げるタブ一覧が実装と 1 つずれている** (文書の「データベース管理」は
  存在せず、実装の「フィード」が文書に無い)。チュートリアル完了後に利用者が最初に開く画面の説明なので、
  この流れの終端に触れる。

**静的な疑い (実行して確かめていない)**

- `saiverse/llm_router.py:18` の `ROUTER_MODEL` は `BUILTIN_DEFAULT_LITE_MODEL` (Gemini) の固定値で、
  env var を読まない。Gemini のキーを持たない導入 (Anthropic だけ、OpenAI だけ) でこのモジュールの
  `route()` が実際に呼ばれるかどうかは追跡していない。`docs/features/tools-system.md:12` は
  「旧 `llm_router.py` … はこの Spell 方式に置き換わっている」と書いており、死んでいる可能性がある。
  **確定させていないので、これを不具合として扱わない。**
- チュートリアル完了時の `POST /api/user/move` が失敗しても `console.error` を出すだけで先へ進む
  (`TutorialWizard.tsx:249-256`)。上に書いたとおり seed の初期位置が残るので致命ではないが、
  「作ったペルソナの部屋に着いたはず」という利用者の理解とはずれる。

**まだ追跡していない境界**

- `setup.bat` / `setup.sh` の分岐 (Node.js/Git の自動導入、`.venv` 破損時の自己修復が Windows のみ、
  ZIP 展開後の `git reset origin/main` が作業ツリーを汚すかどうか)。前回の台帳が挙げた
  「ZIP → git 化の後に更新が拒否されるか」は実行しないと分からない。
- `needs_initial_setup` の判定とほかのモーダル (地図など) の競合 (`page.tsx:3694-3698` のコメントのみ読んだ)。
- ステップ 5 を飛ばしたときに利用者の画面に実際に何が出るか。

**検査の状況** (検査が無いことは不具合の証明ではない、という前提で)

- `tests/` に `tutorial` の語を含むファイルは 1 本も無い (今回 `grep -rln "tutorial" tests/` で確認、結果は空)。
  初回起動判定・API キー保存・モデル自動設定・途中コースからの再実行が、いずれも自動検査で押さえられていない。
- `OPS-01` の検査は `tests/test_requirements_lock_contract.py` が `setup.bat` / `setup.sh` の本文を
  文字列として見るだけで、スクリプトを実行するものは無い。

### 6. 中断・失敗・再開

- **セットアップの途中で止まった場合**: `setup.bat` / `setup.sh` を再実行できる。DB 初期化は
  「`SELECT 1 FROM city` が通るか」だけで判定するので、通れば二度目は seed を走らせない
  (`setup.bat:170-196`)。逆に、DB が壊れていて通らないと**確認なしで全消去**が走る。
- **チュートリアルを途中で閉じた場合**: `TUTORIAL_COMPLETED` が立たないので次回起動でまた開く。
  ただし `TutorialWizard` は開くたびに state をリセットするので、前回どこまで進んだかは残らない
  (`TutorialWizard.tsx:110-131`)。作ったペルソナは DB に残っている。
- **後からやり直す場合**: サイドバー →「システム」→「チュートリアル」で 4 コース
  (最初から / ペルソナ作成 / API キー設定 / モデル設定) を選べる (`TutorialSelectModal.tsx:22-48`)。
  ただし途中コースから入ると `createdPersonaId` が空のまま進むので、ステップ 7 の Chronicle 設定保存は
  何もせず素通りする (`TutorialWizard.tsx:117-136, 340-351`)。
- `POST /api/tutorial/reset` は実装されているが、フロントから呼ぶ箇所が見つからなかった。

### 7. 次に使う機能・共有する状態

- 作られた `ai` 行・私室 Building・`user_ai_link` は FLOW-20 (設定変更)、FLOW-21 (アラーム)、
  FLOW-23 (削除) の対象そのものになる。
- 書かれた 6 つのモデルロール env var は FLOW-28 (モデルとプロバイダの選択) と共有される。
  チュートリアルは `.env` を直接書くので、後からグローバル設定で変えた値と競合しうる (追跡していない)。
- 最初の発言は FLOW-01 に渡る。ここで初めて API キーの有無が結果に出る。
- `TUTORIAL_COMPLETED` は `user_settings` (USERID=1) にあり、FLOW-25 (起動と復帰) の起動時判定を左右する。

### 8. 決める必要がある点

- **「導入が成功した」を製品として何で判定するか。** 現状の判定 (`TUTORIAL_COMPLETED` が立ったか) と、
  利用者の実感 (返事が返ったか) が一致していない。スキップできるステップがある以上、
  完了フラグは「案内を最後まで見た」以上のことを意味していない。**要追加確認ではなく未合意** —
  判定の意味を決めるのはまはー。
- **ステップ 4 の「引き継ぐ」カードが同じウィザードを開くのは意図か。** 説明文と挙動が違うことは
  確認できたが、どちらへ寄せるか (説明文を直す / 取り込み画面へ直行させる) は決まっていない。
- 上記以外は、既存の決定 (`roadmap_status.md` §6 が「拡充が課題」と位置づけている) の範囲内で、
  新しい議題を増やす必要は無いと考える。

---

## FLOW-20: ペルソナの設定を変え、その設定で利用する

**対応する台帳項目**: `PERS-05`, `PERS-06`, `PERS-27`, `PERS-26`, `PERS-10`, `PERS-25`

### 1. 誰が何をしたいか

**利用者**が、ペルソナ 1 体の振る舞い (使うモデル、記憶の整理のしかた、話しかけたときに自動で走るスペル、
リンクするユーザー、活動する時間帯) を自分の使い方に合わせて変え、**変えたとおりに使いたい**。

### 2. 始まりから結果までの流れ

- ペルソナメニュー →「設定」で `GET /api/people/{id}/config` を読み、モーダルに現在値が出る
  (`SettingsModal.tsx:193`)
- 利用者が値を変え、「保存」で `PATCH /api/people/{id}/config` を送る (`SettingsModal.tsx:317-384`)
- 保存前に、**読み込み元の persona_id と保存先の persona_id の一致を検査する**。不一致なら拒否する
  (2026-04-30 の別ペルソナ上書き事故の対策。`SettingsModal.tsx:294-313, 984-990`)
- `AdminService.update_ai` が `ai` 行を更新し、ロード済み PersonaCore の属性を即時反映し、
  必要なら LLM クライアントを作り直す (`manager/admin.py:1324-1424`)
- 反映のタイミングが項目で違う。モデル・プロンプト・自律は保存時に即時。Chronicle 文字数と
  Memopedia 索引は「次に記憶の整理が起きたとき」と画面に明示されている (`SettingsModal.tsx:668, 736`)
- 事前実行スペル (`PERS-27`) だけは保存ボタンを経由せず、追加・削除の操作で直接 API を叩く
  (`SettingsModal.tsx:812-815, 881-889`)
- 自律行動の ON/OFF はこのモーダルには出ず、グローバル設定 → ワールドエディタ → AIs タブの
  チェックボックスだけが入口 (`WorldEditor.tsx:655-664`)
- 活動時間帯 (ライフ) を利用者が決める入口は、**現在存在しない**

### 3. 期待する結果の初稿

**根拠のある期待**

- 保存した設定は、画面が説明したタイミングで実際に効く。即時と書いてある項目は次の発言から、
  「次の記憶の整理から」と書いてある項目はその整理から。
- **別のペルソナの設定を上書きしない。** これは 2026-04-30 の事故を受けて既にコードに歯止めがある。
- ペルソナごとの設定は、そのペルソナだけに効く。ある画面の操作が、利用者に告げずに
  他の全ペルソナの設定を書き換えることはない。

**調査担当の提案**

- 利用者が活動時間帯 (起床・就寝) を決められること自体は、v0.4 の設計 (§8 の未決参照) で
  改めて器が用意される予定になっている。**v0.3 の正常な姿としては、入口が無いこと自体は不整合ではない** —
  不整合なのは、入口が無いのに文書が「設定できる」と案内していることのほう。

### 4. 根拠

- **ユーザー原文**: 今回の資料の範囲では、設定画面についてのまはーの直接の発話は見つからなかった。
  ただし `docs/intent/autonomous_behavior_v3.md:154` に、v0.4 の自律行動管理 UI について
  「2026-08-19 まはー方針: 起床・就寝時間 / 自律行動の ON/OFF (将来は曜日単位の制御も) /
  ルーチン一覧 (アラーム含む) を、ペルソナごとの一画面に同居させる」という記載がある。
  **設計書に方針として記載されているもので、まはーの原文そのものは同文書には引用されていない。**
- **既存の仕様文書**: `docs/concepts/persona.md:17`「自律行動の ON/OFF は AUTONOMY_ENABLED の 1 本だけ。
  OFF にすると時間割も判断点も発火しない (会話への返答は止まらない)」。
  `docs/intent/autonomous_behavior_v3.md` §11.1 (v0.3 の止め具の裁定、2026-08-23 まはー裁定)。
  `docs/intent/life.md` の冒頭ステータス行は「💤 凍結 (2026-08-23、まはー裁定)」「実装は入ったまま (撤去していない)」。
- **利用者向け説明**: `docs/user-guide/persona-settings.md` (内容が古い。下記 §5)、
  `docs/user-guide/world-editor.md`、`docs/user-guide/global-settings.md`、
  `docs/features/autonomous-mode.md` (内容が古い。下記 §5)。
- **調査担当の提案**: §3 の後半。

### 5. 現状との差

**既知の不一致 (文書と実装)**

- **`ACTIVITY_STATE` を現役の設定として説明している文書が 3 本ある。**
  `docs/user-guide/persona-settings.md:23-31` が Active / Idle / Sleep / Stop の表を載せ、
  `docs/user-guide/world-editor.md:52` が AIs タブのフィールドとして挙げ、
  `docs/overview/landscape.md:73` が「自律性は ACTIVITY_STATE (4段階) で外部に宣言される」と書く。
  実装ではこの列は 2026-07-14 に削除済みで、`tests/test_cognitive_model_schema.py:70` が
  `assert "ACTIVITY_STATE" not in cols` で不在を固定している。UI にも該当の欄は無い。
  `docs/features/autonomous-mode.md` は解体を明記しているので、この 1 本だけは現状に追いついている。
- **`docs/features/autonomous-mode.md` の「グローバル制御」節が、存在しないトグルを案内している。**
  「サイドバー / ライフビューから自律行動の再生・停止をトグルできる」とあるが、
  `Sidebar.tsx:69` は「『できごと』とライフビューへの導線は v0.3 で隠した」と注記している。
- **ライフ設定は、intent が「実装完了」と書いている一方で、API とテストが削除されている。**
  `docs/intent/life.md` §9.2 は「ライフ設定画面新設 … 実装完了 (2026-07-14)、まはー実機検証待ち」と書き、
  冒頭ステータス行も「実装は入ったまま (撤去していない)」とする。
  実際には `api/routes/people/life_settings.py` と `tests/test_life_settings_api.py` が
  commit `f2a36010` (束 6c、運転 UI 撤去) で削除されている。フロントにも画面は無い。
  「実装は入ったまま」は `saiverse/day_plan.py` の確定ロジックについては正しいが、**設定 API と画面については
  正しくない**。
  **これは「文書に合わせて実装を戻す」話ではない。** 撤去は後日 (2026-08-23 の凍結裁定と束 6c) の決定であり、
  intent の §9.2 がその決定より前の記述のまま残っているだけである。直すべきは文書のほう。
- **自律トグルの UI は、隠した側と隠していない側が両方ある。** `SettingsModal.tsx:99-102` は
  「v0.3 で UI から隠した」として非表示にし、`WorldEditor.tsx:655-664` は同じ設定をチェックボックスとして
  出している。`autonomous_behavior_v3.md` §11.1 は「v0.3 ではその切り替え UI を隠した — つまり新しい
  ユーザーの世界では『ペルソナは既定で自律 ON、OFF にする手段が無い』」と書いており、
  **ワールドエディタが残っていることを前提にしていない**。
- **v0.3 では、ワールドエディタのチェックボックスを操作しても駆動側の挙動は変わらない。**
  `saiverse/autonomy_wiring.py:91` の `AUTONOMOUS_DRIVING_SHIPPED = False` の間、
  `is_autonomy_on` は設定値に関わらず常に False を返す (`:199-224`)。
  ただし後述のとおり、この設定値を `is_autonomy_on` を通さずに直読みしている箇所が 1 つあり、
  そこには効いてしまう。

**既知の不一致 (画面の説明と副作用)**

- **「開発者モード」を OFF にすると、全ペルソナの `AUTONOMY_ENABLED` が False に一括更新される。**
  `api/routes/config.py:519-546`。画面の説明文は
  「ONにすると開発中の機能が表示されます（不安定なため推奨しません）」だけで
  (`GlobalSettingsModal.tsx:975-990`)、この副作用に触れていない。
  再度 ON にしても元には戻らない (戻す処理が無い)。
  **これは既に未解決 issue として起票済み**: `docs/issues/developer_mode_off_mass_disables_autonomy.md`
  (2026-09-01 起票、状態は「未解決 (裁定待ち — 現行の自律行動 v2 でこの連動が正しいか)」)。
  したがって新しい議題ではない。
- **この副作用は、v0.3 でも観察できる結果を持つ。** `sea/runtime.py:1994` の
  キャッシュ保温は `persona.autonomy_enabled` を **`is_autonomy_on` を通さずに直読みしている**ため、
  止め具の影響を受けない。開発者モードを OFF にすると、explicit キャッシュを使うペルソナの
  キャッシュ保温が全員分止まる。**静的な読み取りであり、実行して確かめてはいない。**

**まだ追跡していない境界**

- `avatar_path` が GET では URL 化されて返り、PATCH ではその URL がそのまま `AVATAR_IMAGE` へ
  書かれる往復が、値を保存し続けられるかどうか。
- 既存ユーザーの DB に残る起床・就寝の `persona_schedule` 行 (過去のライフ設定 UI が作ったもの) の扱い。
- `ensure_autonomy_for` の中で AutonomyManager が実際に何を止めるか。

**検査の状況**

- `/api/people/{id}/config` を HTTP で往復するテストは見つからなかった。manager 層は
  `tests/test_admin_ai_edit_contract.py` が厚く見ている。
- フロントの「読み込み元と保存先の一致検査」(2026-04-30 の事故の再発防止) を見る検査は無い。

### 6. 中断・失敗・再開

- 保存に失敗すると応答に `warning` が付き、フロントが alert で見せる
  (`api/routes/people/config.py:140-179`, `SettingsModal.tsx:386-395`)。
- **リンクユーザーの更新だけが失敗した場合、モデル等の更新は残る。** `update_ai` が先に commit し、
  リンク更新はその後で走るため (`config.py:108-135` と `:148-174` の順序)。利用者から見ると
  「保存が失敗した」と出るが一部は保存されている。
- `META_JUDGMENT_CONFIG` は丸ごと置換されるので、このモーダルが編集しないキーは
  読み込み値からマージして送り返している (`SettingsModal.tsx:361-374`)。マージ元の読み込みが
  失敗した状態で保存すると、編集していないキーが落ちうる (追跡していない)。

### 7. 次に使う機能・共有する状態

- モデル設定は FLOW-01 (会話) と FLOW-04 (送る量とモデルの決定) が読む。
- Chronicle 系の設定は FLOW-08 / FLOW-09 (記憶の整理) が読む。
- `AUTONOMY_ENABLED` は FLOW-24 (画面のない自動処理) の入口だが、v0.3 では止め具が先に効く。
  ただし上記のとおりキャッシュ保温だけは直読みで効く。
- 事前実行スペルは FLOW-05 (道具を使ってもらう) と会話ごとの費用に接続する。
- リンクユーザーはシステムプロンプトに名前として現れる。

### 8. 決める必要がある点

- **利用者が活動時間帯 (起床・就寝) をどう決められるべきか。** 現在は入口が無い。
  v0.4 の方針 (`autonomous_behavior_v3.md:154`) は「起床・就寝時間 / 自律行動の ON/OFF /
  ルーチン一覧を、ペルソナごとの一画面に同居させる」と記録されているが、
  **v0.3 の利用者に対して「決められない」ままでよいのかは、この記録からは決まらない。**
  FLOW-21 の未合意と同じ画面の話なので、**共通議題**として扱うのがよい。
- **開発者モード OFF の一括 OFF 連動を残すか外すか。** 既に
  `docs/issues/developer_mode_off_mass_disables_autonomy.md` が裁定待ちで起票済み。
  今回の追加材料は「v0.3 でもキャッシュ保温には効く」という 1 点だけ。**新しい議題ではない。**
- **自律トグルの入口を 1 箇所にするか。** 隠した側 (設定モーダル) と隠していない側 (ワールドエディタ) の
  どちらが意図かは、intent の記述からは決まらない。
- 古い文書 3 本 (`persona-settings.md` / `world-editor.md` / `landscape.md:73` の `ACTIVITY_STATE`、
  `autonomous-mode.md` のグローバル制御、`life.md` §9.2 のライフ設定画面) は、
  **決める必要のある点ではなく、直す必要のある文書**として扱う。判断は要らない。

---

## FLOW-21: 決まった時刻にペルソナから働きかけてもらう

**対応する台帳項目**: `PERS-08`, `PERS-09`, `AUTO-05`, `AUTO-06`

### 1. 誰が何をしたいか

**利用者**が「毎朝おはようと言ってほしい」「毎週土曜にニュースを調べてほしい」「毎晩日記を書いてほしい」
といった定時の働きかけをペルソナに仕掛け、**その時刻にペルソナが動いた結果を受け取りたい**。

結果を受け取るのは利用者だが、動くのは**ペルソナ**であり、動けば LLM の課金が発生し、
ペルソナの記憶に残る。

### 2. 始まりから結果までの流れ

- ペルソナメニュー →「アラーム管理」(`ScheduleModal`) で登録。種別は periodic / oneshot / interval の 3 種
  (`api/routes/people/schedule.py:19-49`)
- `POST /api/people/{id}/schedules` が `persona_schedule` 行を作り、同一 commit で `SYNC_GENERATION` を
  インクリメントし、`ScheduleManager.register_schedule` で EventScheduler に次回発火を積む
  (`schedule.py:105-165`)
- **画面は `meta_playbook` を送らない** (`ScheduleModal.tsx:262-268` のコメントが理由を明記)。
  送らないとサーバー既定の `DEFAULT_META_PLAYBOOK = "track_user_conversation"` が入る
  (`saiverse/schedule_manager.py:51`)。これは判断点の Playbook ではない
- 時刻が来ると `EventScheduler` の dispatch スレッドが `_handle_fire` → `_execute_schedule` を呼ぶ
  (`saiverse/event_scheduler.py:94-104`, `schedule_manager.py:1096-`)
- `META_PLAYBOOK` が判断点名 (`judgment_day_open` / `judgment_day_close`) なら
  `handle_scheduled_judgment` へ迂回し、そこで `is_autonomy_on` に当たる (`schedule_manager.py:1129-1152`)
- **それ以外は** `all_personas` からペルソナを引き、その `current_building_id` を宛先にして
  `pulse_dispatcher.dispatch_schedule_fire` を呼ぶ (`schedule_manager.py:1155-1189`)
- ペルソナが `<system>` プロンプト付きの Pulse を回し、発言し、記憶に残り、LLM 課金が発生する

### 3. 期待する結果の初稿

**根拠のある期待**

- 利用者が仕掛けた時刻に、**ペルソナがその働きかけを実行する**。README が
  「アラーム設定：決まった時刻にペルソナへ働きかける定時実行が可能です。毎朝自動でおはようを言ってくれたり、
  毎週土曜にニュースを調べたり、毎晩日記を書いたりといった用途に利用することを想定しています。」と
  約束している (`README.md:73`)。この約束は v0.3 の出荷物に対するものである。
- **アラームは会話中でも鳴る。** `autonomous_behavior_v3.md:70`「アラームは会話中でも鳴る (電話が鳴れば
  目は逸れる)」。
- 登録したアラームが**予約に載らなかった場合、利用者はそれを知れる**。これは既存の設計 (API が
  `scheduler_synced` を返す) が前提にしている結果である。

**調査担当の提案**

- 利用者が「このアラームは鳴ると費用がかかる」と登録時点で分かること。画面には現在その表示が無い。
  ただし README は「APIの利用料をチェックするツールを搭載」と別途案内しているので、
  **これを新しい必須要件として立てるのは提案の域を出ない。**

### 4. 根拠

- **ユーザー原文**: `README.md:73` は利用者向けの約束であってまはーの設計発話ではないが、
  `docs/intent/autonomous_behavior_v3.md:154` に、v0.4 の自律行動管理 UI についての
  2026-08-19 まはー方針の記載があり、その中に
  **「未決 = 「自律行動はさせたくないがアラームだけは設定したい」の扱い — ON/OFF が一枚スイッチでよいかに
  繋がる。v0.3 の形の設計には影響しない」**と明記されている。
  **この論点は、v0.4 の未決事項として既に記録されている。** ただし記録は「v0.3 の形の設計には影響しない」と
  添えており、v0.3 で実際にどちらの挙動になっているかは記録されていない。
- **既存の仕様文書**: `docs/intent/autonomous_behavior_v3.md` §11.1 (v0.3 の止め具、2026-08-23 まはー裁定)。
  「止めるもの」に判断点・見張り・起動時のコマ予約の再確立・実イベントの判断経由の応対を挙げ、
  「止めないもの」に会話・Metabolism のスルース・手帳とタスク帳・会話の沈黙タイマー・
  実イベントと仲裁の直接応答を挙げる。
  **どちらの一覧にも、スケジュール駆動の発火は載っていない。**
  `docs/handoff/2026-07-20_w3_schedule_ledger_handoff.md` D2/D7 (台帳化の設計)。
- **利用者向け説明**: `README.md:73`。アラーム管理画面の文言 (`ScheduleModal.tsx:375-453`)。
  利用者向けの独立した説明文書 (`docs/user-guide/`) は見つからなかった。
- **調査担当の提案**: §3 の後半、および §8 の整理。

### 5. 現状との差

#### 追跡結果 — 自律を切っていてもアラームは鳴る (この流れの主題)

前回の調査は「`dispatch_schedule_fire` を投げた後で `AUTONOMY_ENABLED` を見て止まるかどうか」を
未確認としていた。**今回、実行点まで追った。結果は「止まらない」。**

`ScheduleManager._execute_schedule` から LLM に届くまでの呼び出しの鎖は次の 5 段で、
**どの段にも自律のゲートが無い**。

| 段 | 場所 | 自律の判定 |
|---|---|---|
| 1 | `saiverse/schedule_manager.py:1155-1189` (非判断点の分岐) | 無し (`is_autonomy_on` の呼び出しがファイル内に 1 つも無い) |
| 2 | `saiverse/pulse_dispatcher.py:140-223` (`dispatch_schedule_fire`) | 無し。`ExecutionRequest` を組んで `submit` するだけ |
| 3 | `sea/pulse_controller.py:290-403` (`submit`) | 無し。見るのは終了処理中かどうか・優先度・待機列だけ |
| 4 | `sea/pulse_controller.py:468-530` (`_execute_unlocked`) | 無し。捕まえるのは Beat 関所閉鎖・中断・最終防衛ライン未達・例外 |
| 5 | `sea/pulse_controller.py:554-589` (`_do_execute`) | 無し。`sea_runtime.run_meta_user` を呼ぶ |

裏付けの取り方は 2 つ。

1. `is_autonomy_on` の呼び出し元は、リポジトリ全体で 6 箇所しか無い —
   `saiverse/autonomy_wiring.py:455` (判断点の共通入口) / `:1013` (実イベント) / `:1138` (仲裁) /
   `:1647` (watchdog)、`saiverse/saiverse_manager.py:1228` (起動時のコマ再予約) / `:1547`
   (`ensure_autonomy_for`)。上の鎖はどれも通らない。
2. `autonomy_enabled` 属性の直読みは、`sea/` パッケージ全体で `sea/runtime.py:1994` の 1 箇所だけで、
   これはキャッシュ保温の入口 (別の流れ)。`saiverse/` 側の直読みは
   `autonomy_wiring.py:222` (= `is_autonomy_on` の中身)、`saiverse_manager.py:1624`、`:2064`, `:2093` のみ。

**したがって v0.3 の出荷状態では、次の非対称がある。**

- **画面から作れる唯一の種類のアラーム** (既定 Playbook = `track_user_conversation`) は、
  止め具 (`AUTONOMOUS_DRIVING_SHIPPED = False`) にも、ペルソナの `AUTONOMY_ENABLED = False` にも
  止められずに発火し、LLM 課金が発生する。
- **画面から作れない種類** (起床・就寝の判断点) だけが止まる。
- 同じく費用の発生する**キャッシュ保温は `AUTONOMY_ENABLED` を直読みして止まる** (`sea/runtime.py:1994`)。
  費用の出る 2 つの自動処理が、同じ設定に対して逆の反応をする。

**この非対称がどこにも記録されていないことも確認した。** `autonomous_behavior_v3.md` §11.1 の
「止めるもの / 止めないもの」の両方の一覧に、スケジュール駆動の発火は現れない。

**既知の不一致**

- **予約に載らなかったアラームが「登録できた」ように見える。** API は EventScheduler への同期に
  失敗しても HTTP 200 のまま `scheduler_synced: false` で返す (`schedule.py:158-164`) が、
  `ScheduleModal.handleSave` は `res.ok` だけを見て一覧を読み直す (`ScheduleModal.tsx:285-294`)。
  利用者は登録できたと理解するが、その時刻には鳴らない。
- **利用者から見て、起床・就寝のアラームを作る入口が一つも無い。**
  画面は `meta_playbook` を送らず (`ScheduleModal.tsx:262-268`)、Playbook 選択欄は 2026-09-01 の裁定で
  撤去されている (`api/routes/people/summon.py:92-95` に経緯)。
  ライフ設定の画面も削除済み (FLOW-20 §5)。残っているのはペルソナ自身の `schedule_add` スペルだけ
  (`builtin_data/tools/schedule_add.py:29`)。
  なお `api/routes/people/schedule.py:229-231` のコメントは、ライフ設定が現存する前提で書かれている。
- **`create_schedule` / `delete_schedule` はペルソナの存在を検査しない** (`schedule.py:105-146, 311-321`)。
  存在しない ID を渡すと孤児行ができる。これは FLOW-23 の結果と繋がる。

**静的な疑い**

- 発火先の建物はペルソナの `current_building_id` であって、利用者がいる建物ではない
  (`schedule_manager.py:1160` 付近)。利用者が別の部屋にいると、鳴った結果はその場では見えない。
  利用者から見た「働きかけてもらった」がどう成立するかは、実行して確かめていない。

**まだ追跡していない境界**

- `_generate_schedule_prompt` が組む `<system>` プロンプトの中身。
- `PLAYBOOK_PARAMS.pre_spells` の実行経路 (`docs/issues/schedule_pre_spells_args_ui.md` が
  UI からの引数指定の需要を未着手として起票している)。
- `INSTANCE_TOKEN` / 実行台帳の精算。

**検査の状況**

- API 側は厚い (`tests/test_schedule_api_sync.py` / `test_schedule_dispatch_outcome.py` /
  `test_schedule_manager_ledger.py` / `test_schedule_reconciliation.py` / `test_event_scheduler.py`)。
- ただし `tests/conftest.py:17-33` の autouse fixture が全テストで `AUTONOMOUS_DRIVING_SHIPPED=True` に
  差し替えるため、テストスイート全体は「v0.4 の世界」を検証している。
  **上で確定させた非対称 (v0.3 で非判断点アラームだけが鳴る) を固定しているテストは無い。**
  出荷状態を見るのは `tests/test_v03_autonomy_gate.py` 1 本だけで、そこが見るのは `is_autonomy_on` の
  戻り値と呼び出し点の存在。
- フロントが `scheduler_synced` を無視していることを見る検査も無い。

### 6. 中断・失敗・再開

- 発火が失敗すると 120 秒 backoff で最大 3 回まで再試行する
  (`SCHEDULE_DISPATCH_RETRY_BACKOFF_SECONDS = 120.0`, `SCHEDULE_DISPATCH_MAX_ATTEMPTS = 3`、
  `schedule_manager.py:58-59, 882-919`)。
- 尽きたら periodic は次回 (翌日) を登録し、oneshot / interval は 60 秒周期の照合が拾って
  cadence を落として再試行し続ける (`schedule_manager.py:915-919`)。
- **`unknown` (LLM が動いたか不明) は自動再試行しない。** 二重課金・二重発言を避けるため。
- `META_PLAYBOOK` が空の行は既定へ倒して WARNING を出す (`schedule_manager.py:1097-1109`)。
  鳴らないアラームにしないための処理。
- 予約は EventScheduler のインメモリ heap なので再起動で消えるが、起動時に
  `ScheduleManager.start()` が ENABLED 全件を積み直す (`schedule_manager.py:156-184`)。
  ただし**このとき対象ペルソナの存在は検査しない**。

### 7. 次に使う機能・共有する状態

- 発火は FLOW-01 と同じ Pulse の器を通り、記憶 (FLOW-09) と費用の記帳 (FLOW-22) に接続する。
- `AUTONOMY_ENABLED` を共有するのは FLOW-20 (設定) と FLOW-24 (画面のない処理)。
  上記のとおり、この流れは共有しているつもりで共有していない。
- `persona_schedule` 行は FLOW-23 (ペルソナ削除) で消えずに残り、孤児として動き続ける。
- EventScheduler (AUTO-06) は、アラームのほかに台帳掃除・フィード取得・キャッシュ保温・
  沈黙タイマーなどと 1 本の dispatch スレッドを共有し、callback はそのスレッドで同期実行される
  (`saiverse/event_scheduler.py:272-313`)。アラームの実行が長引くと他の予約が遅れる可能性があるが、
  実行して確かめていない。

### 8. 決める必要がある点

- **【未合意・この流れの主題】利用者がアラームを仕掛けたとき、自律行動を切っていたら鳴るべきか。**
  - 材料 1: README は「決まった時刻にペルソナへ働きかける定時実行が可能」と、自律行動と切り離した
    機能として案内している (`README.md:73`)。
  - 材料 2: `autonomous_behavior_v3.md:154` は「自律行動はさせたくないがアラームだけは設定したい」の扱いを
    **v0.4 の未決**として記録し、「ON/OFF が一枚スイッチでよいかに繋がる」としている。
  - 材料 3: 現在の実装は「鳴る」(上記 §5 で確定)。**ただしそれは設計判断として記録されたものではなく、
    止め具の適用範囲を数え上げたときに漏れた形になっている** (§11.1 の「止めるもの」「止めないもの」の
    どちらの一覧にも無い)。
  - 材料 4: 同じく費用の出るキャッシュ保温は `AUTONOMY_ENABLED` を直読みして止まる。
    費用の出る 2 つの自動処理が同じ設定に逆の反応をしている。
  - **どちらが正しいかはここでは決めない。** 決めるのはまはー。
  - 決めるときに必要な情報として: v0.3 では `AUTONOMY_ENABLED` を利用者が切る入口が
    ワールドエディタのチェックボックス 1 つと、開発者モード OFF の副作用 (FLOW-20) の 2 つある。
    後者は利用者が意図せず全ペルソナを OFF にしうるので、「OFF なら鳴らない」を選ぶ場合は
    そちらの副作用が「知らないうちにアラームが全部止まる」に化ける。**共通議題** (FLOW-20 と共有)。
- **【要追加確認、ユーザー判断へ転嫁しない】起床・就寝のアラームを利用者が作る入口をどうするか。**
  入口が無いこと自体は 2026-09-01 の裁定 (Playbook 選択欄の撤去) の結果で、
  「一日のリズムはライフ設定が所有する」という前提に立っている。その前提のライフ設定が削除済みなので、
  前提が崩れている。**この崩れが意図されたものか (v0.4 まで空席にする) は、まだ調べ切っていない。**
  FLOW-20 の同名の議題と**共通議題**。
- **【不具合の疑い、未合意ではない】`scheduler_synced` をフロントが読んでいないこと。**
  API 側は既に「同期できなかった」を返す設計になっており、利用者に伝える意図は既に存在する。
  判断を求める議題ではなく、実装の欠落として扱うのが妥当と考える (調査担当の提案)。

---

## FLOW-22: 費用と使用量を把握する

**対応する台帳項目**: `PERS-17`, `PERS-18`, `PERS-19`

### 1. 誰が何をしたいか

**利用者**が、自分が各社の API にどれだけ課金されているかを SAIVerse の中で見て、
**使いすぎる前に気づきたい**。SAIVerse 自体は無料で、課金は利用者が各社に対して直接負う。

### 2. 始まりから結果までの流れ

- LLM 呼び出しのあとに `get_usage_tracker().record_usage(...)` が `llm_usage_log` へ 1 行を書く
  (batch_size=1 で即時反映。`saiverse/usage_tracker.py:40, 109-112, 152-180`)
- 呼び出し元は `sea/runtime_llm.py`、`sea/runtime.py`、`sea/work_session.py`、`sea/sluice.py`、
  `sai_memory/arasuji/generator.py`、`sai_memory/memopedia/generator.py` など
- 利用者はサイドバー →「システム」→「API使用状況」で `/usage` を開く
- ページが summary / daily / personas / categories / by-category の 5 本を叩き、期間・ペルソナ・
  カテゴリで絞った合計コストとトークン、モデル別日次コストのグラフを描く (`frontend/src/app/usage/page.tsx`)
- 別経路として、チャット画面のモデル選択に連動して `GET /api/usage/rpd?model_id=...` が
  無料枠の当日残量を返す (`api/routes/usage.py:399-446`)

### 3. 期待する結果の初稿

**根拠のある期待**

- 利用者は、SAIVerse の画面の中で自分の API 使用料の**目安**を見られる。
  README は「APIの利用料をチェックするツールを搭載しており、使いすぎを抑制できます。
  ※確実ではないので、各自こまめに各社のAPI利用料ページを確認してください。」と書いている
  (`README.md:46`)。**この「※確実ではない」という但し書きは、既に利用者向けの約束の一部である。**
- 通貨が混ざる場合、混ぜて合計されない。実装も `costs_by_currency` で通貨ごとに分けて返している
  (`api/routes/usage.py:82-103`)。
- 無料枠のあるモデルについて、当日の残り回数が見える。

**調査担当の提案**

- README の但し書きは「多少ずれる」ことを許しているが、**系統的に少なく出る**ことまで
  許しているとは読めない。「使いすぎを抑制できる」ためには、少なくとも実費を下回り続けないことが要る。
  ただしこれは提案であって、まはーが「どこまでずれてよいか」を決めた記録は見つかっていない。

### 4. 根拠

- **ユーザー原文**: 今回の資料の範囲では見つからなかった。ただし
  `docs/issues/usage_tracking_gaps.md` に「画像生成でかかった金額は本来コストとして確認できるはずなのに、
  現在の usage 集計にまったく計上されていない。**まはーの気づきから起票**」とあり、
  まはーがこの流れの結果に期待を持っていることは記録されている。
- **既存の仕様文書**: `docs/intent/model_provider_management.md` の不変条件「使用量の帰属」
  (`docs/issues/llm_usage_accounting_gaps.md` が引用している)。
- **利用者向け説明**: `README.md:40-46` (料金の考え方と使用状況ツール)、
  `docs/user-guide/world-view.md:13` (サイドバーの「システム」に API 使用状況があること)。
  使用状況ページ自体を説明する利用者向け文書は見つからなかった。
- **調査担当の提案**: §3 の後半。

### 5. 現状との差

**既知の不一致 (すべて起票済み。新しい発見ではない)**

- `docs/issues/llm_usage_accounting_gaps.md` (**未解決**) — 芯は
  「使用量の記帳が『成功して戻ってきた場合』にだけ効く形で散らばっている。API 呼び出しが成立した時点で
  課金は発生しているのに、その後の解釈・検証・retry の都合で記録が消える経路が複数ある。加えて、
  runtime を経由しない直接の LLM 呼び出しは最初から誰も記帳していない。」
  同 issue は「一箇所ずつ塞ぐと同じ形の穴が別経路で開き続けるので、
  『応答を受け取ったら必ず一度記帳する』を持つ層を決めるのが本題」としている。
- `docs/issues/usage_tracking_gaps.md` (**未着手**) と
  `docs/issues/image_generation_api_usage_tracking.md` (**未着手**) — 画像生成のコストが
  `llm_usage_log` に一切残らない。画像は枚数・解像度ベースの課金で、
  トークン前提の `record_usage` にそのまま乗らない。
- `docs/issues/usage_pricing_lookup_falls_back_to_api_name.md` (**未解決**) — 価格の引き当てが
  API モデル名へフォールバックしうる構造。同 issue は、起票時に「実害は出ていない」と書いたのが誤りで、
  実際に `scripts/` の 6 箇所で誤課金が記録されていたと訂正している (呼び出し側は修正済み、構造は残存)。
- `docs/issues/structured_output_usage_not_recorded.md` (**実装済・実機検証待ち**) —
  OpenAI 互換 / NVIDIA NIM の構造化出力経路で記録されなかった欠落。修正済みだが実機未検証。
- `docs/issues/spell_usage_not_shown_in_chat.md` (**未着手**) — スペル実行分の使用量が
  チャット UI のメッセージ単位表示に来ない (集計画面には載っている可能性がある)。

**今回コードで確かめた 2 点**

- **キャッシュ保管コストの通貨が落ちる。** `record_usage` はモデルの pricing から通貨を引いて
  レコードに入れる (`saiverse/usage_tracker.py:90-107`) が、`record_cache_storage` は
  `"currency"` キーを入れない (`:205-218`)。`_flush_to_db` が `record.get("currency", "USD")` で
  読むため (`:168`)、**JPY 建てモデルのキャッシュ保管コストが USD 行として記録される**。
  使用状況ページは通貨ごとに合計するので、その合計に紛れ込む。
- **`cache_write_tokens` は保存されない。** レコードには入っているが (`:101`)、
  `LLMUsageLog` に渡す辞書に該当の列が無い (`:159-172`)。コスト計算には使われている。

**静的な疑い**

- 無料枠の残量 (`GET /api/usage/rpd`) は `llm_usage_log` の**行数**を数える
  (`api/routes/usage.py:428-435`)。記帳の欠落はそのまま残量の過大表示 (使ったのに減っていない) になる。
  同じ台帳を数えるので、上記の欠落と同じ影響を受ける。**実行して確かめていない。**

**まだ追跡していない境界**

- `calculate_cost` のキャッシュ割引・書き込み割増の正確さ。
- `formatCost` の通貨表示規則と、グラフの積み上げ規則。
- `GET /api/usage/by-persona` と `GET /api/usage/models` はフロントのどこからも呼ばれていない
  (前回の台帳の grep 結果。今回は再確認していない)。

**検査の状況**

- `tests/` に `/api/usage/*` を叩くものは見つからなかった (今回 `grep -rln "api/usage\|/usage/" tests/` は空)。
- `tests/test_usage_tracker.py` は tracker を直接呼ぶだけで、実際の LLM 呼び出し経路からの記帳は通していない。
- キャッシュ保管コストの通貨が落ちることを見る検査も無い。

### 6. 中断・失敗・再開

- `configure()` が呼ばれていないプロセスでは、レコードを捨てて WARNING を出す
  (`usage_tracker.py:131-147`)。本番では `manager/initialization.py:52` で設定される。
- DB への書き込みに失敗すると `LOGGER.error` を出して rollback する (`usage_tracker.py:176-182`)。
  **その回の使用量は失われ、利用者には何も見えない。**
- 記帳は batch_size=1 で即時なので、プロセスが落ちても未書き込みの滞留は最小。

### 7. 次に使う機能・共有する状態

- `llm_usage_log` は使用状況ページと無料枠の残量表示の両方が読む単一の台帳。
- FLOW-21 (アラーム)、FLOW-09 (記憶の自動整理)、FLOW-24 (画面のない処理) が生む費用は、
  すべてこの台帳を通してしか利用者に見えない。**「見ていない間に発生した費用」を利用者が知る唯一の経路。**
- 削除されたペルソナの `llm_usage_log` 行は残る (FLOW-23)。`PERSONA_ID` には外部キー宣言が無い
  (`database/models.py:545` 付近) ので、集計上は名前の引けない行になる (追跡していない)。

### 8. 決める必要がある点

- **無し**、という判断が妥当だと考える。理由は次のとおり。
  - この流れの不一致は 5 件すべてが既に issue として起票済みで、うち 1 件は
    「記帳の置き場所そのものの設計課題」として設計裁定待ちの形になっている
    (`llm_usage_accounting_gaps.md`)。**新しい議題を立てる必要は無い。**
  - README の但し書き (「※確実ではないので、各自こまめに各社のAPI利用料ページを確認してください」) が、
    厳密さの水準についての利用者への約束を既に定めている。「どこまでずれてよいか」を数値で
    新しく決める必要は、この流れの目的からは出てこない。
- ただし、上で新しく確かめた 2 点 (キャッシュ保管コストの通貨が落ちる / `cache_write_tokens` が
  保存されない) は、既存 issue のどれにも書かれていない。**これらは判断を求める議題ではなく、
  既存 issue へ追記すべき材料**として統合担当に渡す。

---

## FLOW-23: ペルソナを消す / 情報を整理する

**対応する台帳項目**: `PERS-07`, `PERS-12`, `PERS-20`, `PERS-29`

### 1. 誰が何をしたいか

**利用者**が、もう使わないペルソナを世界から消したい。あるいは自分のプロフィール
(表示名・アバター・メールアドレス) を整えたい。

この流れは、消す対象が**記憶を持った存在**である点で他の削除と違う。取り返しがつかない側と、
消し残しが動き続ける側の、両方に結果がある。

### 2. 始まりから結果までの流れ

- グローバル設定 → ワールドエディタ → AIs タブ →「削除」→
  `confirm("このペルソナを削除しますか？")` → `DELETE /api/world/ais/{id}` (`WorldEditor.tsx:393`)
- `AdminService.delete_ai` が実行する内容 (`manager/admin.py:1431-1483`):
  - シードされたペルソナと dispatched なペルソナは拒否する
  - 開いている `building_occupancy_log` に EXIT を打刻する (行は消さない)
  - `task_book.purge_persona_entries` で `task_book` の当該行を物理削除する
  - `ai` 行を削除して commit する
  - インメモリの `personas` / `persona_map` / `id_to_name_map` / `avatar_map` / `occupants` から外す
- 画面上の undo は無い。復旧手段は DB のバックアップ (`SAIVERSE_DB_BACKUP_ON_START`) だけ

### 3. 期待する結果の初稿

**根拠のある期待**

- シードされたペルソナと、他都市へ出張中のペルソナは削除できない。これは実装が既に守っている
  安全策で、削除の取り返しのつかなさに対する歯止めになっている。

**調査担当の提案 (ここは根拠のある期待が薄い。分けて読むこと)**

- 「ペルソナを消す」は、利用者にとっては**その人格が世界から居なくなる**ことである。
  したがって最低限、①一覧に出ない ②話しかけられない ③その人格として何かが動き出さない、
  の 3 つが揃うのが自然な理解だと考える。①②は現在成立しており、**③が成立していない**。
- 記憶 (`~/.saiverse/personas/<id>/memory.db`) を消すか残すかは、①〜③とは別の判断である。
  記憶は取り返しがつかないので、**消さないこと自体は保守的な選択として筋が通る**。
  ただし現在は「残る」ことが利用者に告げられていない。
- 削除の確認は `confirm("このペルソナを削除しますか？")` の 1 行だけで、何が消えて何が残るかを
  伝えていない。**取り返しのつかない操作の手前で、結果を見せることは妥当だと考える。**

### 4. 根拠

- **ユーザー原文**: 今回の資料の範囲では、ペルソナ削除についてのまはーの直接の発話は見つからなかった。
- **既存の仕様文書**: 削除の仕様を書いた文書は見つからなかった。
  ただし関連する構造は `docs/issues/persona_memory_not_self_contained.md` (2026-07-11 起票、
  **まはー指摘**、種別「アーキテクチャ負債（後回し確定・凍結ではない）」) に記録されている。
  同 issue は「ペルソナの記憶・状態が `~/.saiverse/personas/<id>/` だけで完結せず、
  main DB 側に散在している」ことを問題として挙げ、main DB 在住のペルソナ帰属データを列挙している。
  **「消えないものが多い」のは、この既知の負債の裏返しである。**
- **利用者向け説明**: 見つからなかった。`docs/user-guide/world-editor.md` は AIs タブの説明に
  削除の項目を持たない。
- **調査担当の提案**: §3 の後半 3 項目。

### 5. 現状との差

**確かめた事実 — 何が残るか**

`database/models.py` で `ForeignKey("ai.AIID")` を宣言している列は **21 本**あった (今回数え直した)。
`delete_ai` が触るのはそのうち 2 本だけである。

- **触る**: `building_occupancy_log` (EXIT 打刻。行は残る) / `task_book` (物理削除)
- **触らない (19 本)**: `user_ai_link` / `ai_tool_link` / `thinking_request` / `playbook.created_by_persona_id` /
  `persona_event_log` / **`persona_schedule`** / `addon_persona_config` / `persona_building_state` /
  `line_head_snapshot` / `session_head_snapshot` / `action_track` / `meta_judgment_log` /
  `persona_pulse_cursor` / `building_message.persona_id` / `persona_task` / `persona_day_plan` /
  `persona_timetable_template` / `episode` / `episode_inheritance`
- **外部キー宣言が無いので上の 21 本に入っていないもの**: `llm_usage_log.PERSONA_ID`
- **ファイル**: `~/.saiverse/personas/<id>/` (memory.db、log.json、conscious_log.json 等)。
  削除経路に `shutil.rmtree` も persona ディレクトリの操作も無い。
- **私室 Building**: 削除経路に Building の削除が無いので、`ai` 行と一緒に作られた私室 Building 行は残る。
  利用者がその部屋に立っている状態でも削除は通る (立入りの検査が無い)。

**残骸が動き続ける — 孤児スケジュール**

- 起動時に `ScheduleManager.start()` が ENABLED の `persona_schedule` を全件 EventScheduler に積む。
  **このときペルソナの存在を検査しない** (`schedule_manager.py:156-184`)。
- 発火すると `all_personas.get(persona_id)` が None になり、`"persona not found"` で failed になる
  (`schedule_manager.py:1159-1162`)。
- failed は 120 秒間隔で最大 3 回まで再試行され、尽きると periodic は**次回 (翌日) を登録する**
  (`schedule_manager.py:882-919`)。つまり毎日 3 回失敗し続ける。
  oneshot / interval は 60 秒周期の照合が拾い、cadence を落として再試行を続ける。
- **利用者にこれが見える経路は無い。** ペルソナは消えているのでアラーム管理の画面が開けない。
  見えるのはバックエンドのログだけ。

**同じ実装が 2 箇所にある**

- `manager/admin.py:1431` と `manager/persona.py:707` にほぼ同一の `delete_ai` がある。
  後者の docstring 自身が「AdminService 側の同名定義と行単位の複製関係にある — 片方を変えたら
  もう片方も揃えること」と書いている。実際に呼ばれるのは AdminService 側
  (`saiverse/saiverse_manager.py:2114-2116`)。
  `docs/issues/archive/persona_mixin_ai_edit_dead_duplicate.md` は `get_ai_details` / `update_ai` の
  複製を 2026-08-12 に撤去した記録だが、`delete_ai` は残っている。

**ユーザープロフィール (PERS-12) の副作用**

- 表示名を変えると、**全 City の `user_room_<slug>` Building の表示名が
  「<新しい表示名>の部屋」に書き換わる** (`api/routes/user.py:229-243`)。
  利用者が部屋名を自分で変えていた場合でも、条件分岐なく上書きする。
  この副作用を説明する利用者向け文書は見つからなかった。

**お知らせ (PERS-20) の状態**

- `GET /api/system/announcements` は `manager.state.announcements_enabled` を見ていない
  (`api/routes/system.py:153-158`)。トグルは読み書きできるが、バックエンドに消費側が無い。
  未読バッジの抑止としてはフロント側で効いている (`frontend/src/app/page.tsx:1145-1160`)。

**まだ追跡していない境界**

- `_is_seeded_entity` の判定基準。
- 削除されたペルソナの `building_message` 行が、建物の履歴表示でどう見えるか。
- `~/.saiverse/personas/<id>/` が残ったまま同じ ID のペルソナを作り直したとき何が起きるか
  (ID の重複検査は大文字小文字を畳んで行われるので、同名の作り直しは弾かれる。
  `manager/persona.py:506-521`。弾かれた後どうなるかは追っていない)。

**検査の状況**

- `delete_ai` 自体を呼ぶテストは見つからなかった (今回 `grep -rn "delete_ai" tests/` の結果は
  `tests/test_task_book.py` の 2 行のみで、そこは `purge_persona_entries` を直接呼び、
  「commit は呼び手 (delete_ai) の責任」としてテスト側が代行している)。
- 「何が消えないか」を固定するテストも、孤児スケジュールの挙動を見るテストも無い。

### 6. 中断・失敗・再開

- 削除は 1 トランザクションで、例外時は rollback してエラー文字列を返す (`manager/admin.py:1477-1480`)。
  DB 側は一貫するが、**ファイルは最初から触っていない**ので不整合の対象にならない。
- commit の後にインメモリの整理が走る。ここで例外が出た場合の扱いは追っていない。
- **画面上の undo は無い。** 復旧手段は起動時バックアップ (`SAIVERSE_DB_BACKUP_ON_START`) からの復元だけで、
  これは FLOW-31 (壊れたときに復旧する) の話になる。
- 削除に失敗した場合、返るのは `"Error: ..."` の文字列で、画面がこれをどう見せるかは追っていない。

### 7. 次に使う機能・共有する状態

- 残った `persona_schedule` は FLOW-21 (アラーム) の実行経路に毎日入り続ける。
- 残った `llm_usage_log` は FLOW-22 (費用の把握) の集計に残る。
- 残った `~/.saiverse/personas/<id>/` は FLOW-13 (記憶の持ち出し) の対象として生きている。
  つまり「消したペルソナの記憶を後から取り出す」ことは、現在の実装では可能である。
- 残った私室 Building は FLOW-14 (部屋の移動) と FLOW-15 (世界を作り変える) から見える。

### 8. 決める必要がある点

- **【未合意】利用者から見て「ペルソナを消す」とは何が消えることであるべきか。**
  - 材料 1: 現在は `ai` 行・在室ログ・`task_book` の 3 つだけが消え、記憶ファイルと 19 本の関連列が残る。
  - 材料 2: `docs/issues/persona_memory_not_self_contained.md` は、この散在を
    **後回し確定の負債**として既に記録しており、まはーの指摘が起点になっている。
    したがって「散在していること」自体は新しい発見ではない。
  - 材料 3: 記憶を消すことは取り返しがつかない。残すことは「消えたはずのものが残る」という
    別の問題を生む。**どちらを既定にするかは、利用者への説明とセットでないと決まらない。**
  - どちらが正しいかはここでは決めない。
- **【不具合の疑い、未合意ではない】孤児スケジュールが毎日失敗し続けること。**
  記憶を残すかどうかとは独立に、**消えた人格として何かが動き出す**のは、
  上の §3 の①〜③のどの理解を採っても望ましくないと考える (調査担当の提案)。
  塞ぐ場所の候補は 3 つあり、どれを選ぶかは設計判断になる。
  - 削除時に `persona_schedule` を消す (削除の責任を増やす)
  - 起動時の再登録でペルソナの存在を検査する (`ScheduleManager.start`)
  - 作成時にペルソナの存在を検査する (`create_schedule` が今それをしていない)
  なお `create_schedule` の未検査は、削除とは別に「存在しない ID を渡すと孤児行ができる」経路として
  独立に存在する (FLOW-21 §5)。**同じ欠陥の 2 つの入口なので、片方だけ塞ぐと残る。**
- **【要追加確認、ユーザー判断へ転嫁しない】削除の確認ダイアログが何を伝えるべきか。**
  現状は 1 行だけ。何を伝えるべきかは、上の未合意 (何が消えるべきか) が決まらないと書けないので、
  この 2 つは順序がある。
- **【判断は要らない】`delete_ai` の二重定義。** 到達しない複製で、docstring 自身が同期の必要を書いている。
  掃除の対象であって議題ではない。
- **【判断は要らない】ユーザープロフィールの表示名変更が全 City の部屋名を上書きすること。**
  利用者向け説明が無いだけなので、説明を書くか、上書きをやめるかの実装判断。
  ただし**利用者が部屋名を自分で変えていた場合に無条件で上書きする**点は、
  利用者の意図を消す動きなので、実装判断としては軽くないと考える (調査担当の提案)。
