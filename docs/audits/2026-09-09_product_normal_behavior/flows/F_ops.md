# 群 F: 導入・運用・拡張 (FLOW-27〜32) — 正常な姿の初稿

対象コミット: `25ad75d6` (ブランチ `feature/chronicle-coverage-gaps`)。
主な入力は `docs/audits/2026-09-09_product_verification_inventory/inventory.md` の領域 F (OPS-01〜35) と、
FLOW-31 が参照する MEM-39 / WORLD-43 / CHAT-25。

本稿はすべて静的な読み取りである。テスト・スクリプト・バックエンドを一度も実行していない。
`~/.saiverse/` 配下を読み書きしていない。本番ペルソナに接触していない。

## この文書を読む前に — 前回の台帳から更新した事実 4 件

台帳の断定を継承する前に確かめた結果、次の 4 点は前回から変わる。

1. **`websockets` は本体の依存である。** 台帳が OPS-28 の「追跡できていない境界」としていた
   「`websockets` が本体の依存に入っているか」は、`requirements.txt:19` (`websockets>=12.0`) と
   `requirements.lock:438` (`websockets==16.1.1`) の両方にあることを確認した。
   つまり通常のセットアップを踏んだ環境では Unity ゲートウェイの起動条件が満たされ、
   **既定でポートが開く**。「開くかもしれない」ではなく「開く」に確定する。
2. **LAN 公開の 2 変数は `.env.example` に書かれている。** `SAIVERSE_OWNER_TOKEN` と
   `SAIVERSE_ALLOWED_ORIGINS` は `.env.example:128-131` に用例つきで載っている
   (`# LAN backend exposure (main.py --listen-host 0.0.0.0) requires both values.`)。
   記載が無いのは README とランブックであって、`.env.example` ではない。
   一方 `UNITY_GATEWAY_ENABLED` / `UNITY_GATEWAY_PORT` / `SAIVERSE_ANNOUNCEMENTS_URL` は
   `.env.example` にも `docs/reference/environment-vars.md` にも無い (grep 0 件) ことを確かめた。
3. **CI の誤記の位置は `docs/developer-guide/testing.md:119` である。** 依頼書は 131 行としていたが、
   131 行は次のページへのリンク行で、「プルリクエスト時に自動でテストが実行されます。」の本文は
   119 行 (見出し `## CI/CD` は 117 行) にある。
4. **旧履歴の「脇へ移す」経路には HTTP テストがある。** 台帳は OPS-12 を「該当テストなし」としているが、
   `tests/test_legacy_log_archive_api.py` (6 本) が退避 API を TestClient で検査している。
   検査が無いのは隔離の復元とリセットであって、退避ではない。

なお 2026-09-09 朝の 5 コミット (`9996c530`〜`25ad75d6`) はすべてスルースと Chronicle の消し込みで、
領域 F のコードには触れていない (`git log 7d7214be..25ad75d6` で確認)。

---

## FLOW-27: 更新して、設定と記憶を保ったまま使い続ける

**対応する台帳項目**: `OPS-03`, `OPS-04`, `OPS-05` (接点として `OPS-02` の自己回復部分)

### 1. 誰が何をしたいか

結果を受け取るのは**利用者**である。自分のペルソナと会話と設定を持っている人が、新しい版の
SAIVerse を使いたい。ただし、そのために自分の世界を失うことは受け入れられない。
運用者も同じ人であり、更新を実行するのも、失敗したときに後始末をするのも本人になる。

### 2. 始まりから結果までの流れ

- 起動中の画面に「新しい版がある」バナーが出る。元は `GET /api/system/version` で、
  GitHub Releases API の `tag_name` と `VERSION` をタプル比較し、1 時間キャッシュする
  (`api/routes/system.py:40-86`)。
- 入口は 2 系統ある。(a) 画面のボタン → `window.confirm` → `POST /api/system/update`。
  (b) SAIVerse を止めて `update.bat` / `update.sh` / `scripts/update_from_github.ps1`。
- **4 入口すべてが `scripts/update_engine.py --manual` に集約されている。** `update.bat:8` と
  `update.sh:8` と `update_from_github.ps1:7` は同じ 1 行を呼ぶだけで、`scripts/self_update.py` は
  8 行の互換 shim である (自分で確認した)。
- 更新の段は次の順で進む: portable git を PATH へ → 作業ツリーの清潔さ検査 → 世界のスナップショット
  → `git fetch` + `git merge --ff-only --no-overwrite-ignore` → 完了印の削除 →
  `pip install -r requirements.lock` → `pip check` (警告のみ) → `npm ci` → 完了印の書き込み
  (`scripts/update_engine.py:1070-1103`)。
- コードが動いた後に失敗すると `git reset --hard <旧 revision>` + 依存の入れ直しへ戻す
  (`update_engine.py:1085-1088`)。
- 画面経路だけは前後に段が付く。updater が実際に使う venv の interpreter で psutil の実在を
  サブプロセス確認 → 再起動契約 `.update_config.json` を atomic に書く → デタッチした updater を起動
  → 3 秒後に `manager.shutdown()` → `os._exit(0)`。updater は本体の終了を待ってから全段を回し、
  同じ引数で再起動して health を確認してから完了印を書く (`api/routes/system.py:415-588`)。
- 次の起動で `start.bat` / `start.sh` が `update_engine.py --check-complete` を回す。
  終了コード 10 なら `--manual` で仕上げてから起動し、仕上げに失敗したら **起動しない**
  (`start.bat:29-53` を読んで確認した)。

### 3. 期待する結果の初稿

**根拠のあるもの**

- 更新の前後で、ペルソナ・会話・記憶・設定が同じ内容のまま残る。更新機構は `~/.saiverse/` を
  書き換えない (書き換えるのは更新後の初回起動で走る移行であって、これは FLOW-25 の担当)。
- 更新が始まる前に、その時点の世界の復元点が必ず 1 つ作られ、その名前が利用者に告げられる。
- 手元で変えたファイルがあるとき、機構は勝手に捨てず、勝手に退避もしない。どのファイルかを名指しし、
  そのまま実行できるコマンドの形で 2 つの出口 (捨てる / 残して commit する) を見せて止まる。
- 更新が途中で止まっても、次に `start` から起動すると自分で仕上がる。仕上げられないときは、
  コードと入っている部品が食い違ったままでは起動せず、なぜ起動しないかを告げる。
- 画面から更新するとき、更新を最後まで運べない環境では本体を落とさずに断る。断られた理由が
  画面に出る (409 の `detail` をそのままトーストに出す)。
- ZIP で導入した人も、その後の更新を手動 Git なしで実行できる (README の約束)。

**調査担当の提案 (未合意)**

- 更新の後、利用者が「復元点はどこにあり、どう戻すか」を知れる。現在この案内は CLI
  (`snapshot.bat restore <name>`) にしか無く、README にも `docs/user-guide/` にも無い。
  FLOW-31 の「戻し方が利用者に届いていない」と同じ問題の、更新側の面である。

### 4. 根拠

- **ユーザー原文**: 2026-09-03 のまはー裁定 —「未追跡ファイルは早送りを邪魔しないので検査対象から
  外す。追跡ファイルの変更で止まったユーザーの出口 (強制更新) は別案件」。
  `docs/issues/archive/update_refuses_on_tracked_local_changes_without_exit.md` に引用がある。
  同 issue は 2026-09-04 に「実害が初めて出た (エンドユーザーが `setup.sh` に chmod 由来の差分を
  持っていて、どの入口からも更新できなかった)」と記録し、
  「updater 自身は引き続き stash も reset も一切しない (2026-07-16 監査裁定は維持)。
  捨てるかどうかを決めて実行するのはユーザー自身で、機構は実行可能なコマンドを見せるところまで」
  と結論している。§3 の 3 番目はこの文をそのまま期待に写したものである。
- **既存の仕様文書**: `docs/issues/v0229_update_bat_truncates_after_git_pull.md` (中断更新の自己回復の
  由来)、`docs/issues/self_update_unsafe_without_psutil.md`、`docs/intent/dependency_management.md` §2-5、
  `docs/reference/scripts.md`「開発 / 運用」節、`docs/overview/release_history.md` の v0.3.11 の範囲。
- **利用者向け説明**: `README.md:139`「ZIP で導入しても、その後の自動更新 (update.bat) まで
  手動 Git なしで動きます」。`README.md:35` の自動バックアップの約束。
- **調査担当の提案**: §3 後半の復元点の可視化。

### 5. 現状との差

- **良い状態としての記録**: この流れは領域 F で最も設計が固まっている部分である。4 入口の集約、
  拒否メッセージの脱出手順、完了印による中断からの自己回復、psutil の門はいずれも issue 由来の
  理由がコードのコメントに残っており、回帰テストも付いている
  (`tests/test_update_engine_safety.py` 16 本、`tests/test_update_completion_marker.py` 40 本超、
  `tests/test_snapshot_exclusions.py` 13+3 本、`tests/test_snapshot_script_standalone.py` 1 本)。
  **ここを欠陥の列として書くのは事実に反する。**
- **既知の不一致**: `CLAUDE.md`「Setup/Update Script Parity」は `update.bat` / `update.sh` /
  `scripts/self_update.py` の三点が同期していなければならないと書くが、4 入口とも
  `update_engine.py --manual` を呼ぶだけになっており、同期すべき実体はもう無い。
  `docs/reference/scripts.md` は `self_update.py` を「互換 wrapper」と正しく書いている。
  **直すべきは CLAUDE.md であって、実装を文書に合わせて戻す話ではない。**
- **静的な疑い**: スナップショットの制限時間は 3600 秒の固定値である
  (`scripts/update_engine.py:803-813`)。コメント自身が「これは暫定値であって解ではない。固定値である
  限り、世界が育てばいつか再び追い越される」と書き、`docs/issues/snapshot_timeout_is_fixed_while_world_grows.md`
  に恒久策が未着手として起票されている。2026-09-02 に実測 24GB の世界が 900 秒に収まらず
  start.bat が起動不能になった記録がコメントに残っている。
  **超えたときの挙動は確認した**: `create_pre_update_snapshot` は例外を投げ、`run_update` の
  `update_code` より前で止まる (`update_engine.py:1070-1081`)。つまり超過すると更新は**始まらない**。
  コードは動いていないので巻き戻しも不要で、書きかけの `.zip.tmp` は殺した側が消す。
  失うものは無く、失うのは「更新できること」である。
- **静的な疑い**: `pip check` の衝突は警告として記録するだけで、更新は止めないし戻さない
  (`update_engine.py:873-925`)。アドオンは本体と同じ venv を共有するので、lock の更新が
  アドオン側の依存を壊しうる。この型は 2026-09-01 に実際に起きており、更新経路の pip install が
  NumPy を 2.5 に上げて voice-tts の numba が読めなくなり、声が丸一日以上無音になった
  (`docs/overview/release_history.md` の v0.3.4 の行に経緯がある)。
  警告がログに残ることは確認したが、**利用者の画面に出るかは追っていない**。
- **まだ追跡していない境界 (1)**: 複数の更新入口 (update.bat / 画面のデタッチ更新 / 起動時の自己回復) が
  同時に走ったときの挙動。プロセス間ロックが無いことは
  `docs/issues/update_entrances_lack_process_lock.md` に未解決として起票されている。
- **まだ追跡していない境界 (2)**: ZIP 展開 → `git init` + `git reset origin/main` の後、作業ツリーが
  「変更あり」と判定されるかどうか。`.gitattributes` の改行変換が `git archive` の出力にどう効くかを
  実行していない。**ここが一致しないと、README:139 の約束がそのまま FLOW-27 の更新拒否に直行する。**
  FLOW-32 の §5 に同じ境界を書いた (配布物側から見た同じ穴である)。
- **検査の穴**: 更新の全段を通しで実行するテストは無い (pip / npm を実際に走らせる検査は存在しない)。
  spawn → shutdown → restart の実経路も未検査。restore コマンドの通しも未検査。
  **検査が無いことは不具合の証明ではない。** ここは「実行を伴う端が未検査」という記録である。

### 6. 中断・失敗・再開

この流れに実際に関わるものだけを挙げる。

- **スナップショット段でタイムアウトする**: 更新は始まらない。世界は無傷で、書きかけの `.zip.tmp` は
  殺した側が消す。利用者から見ると「更新が始まらない」だけが起きる。
- **merge の後、依存の入れ替えの途中で失敗する**: `git reset --hard <旧 revision>` で戻し、
  依存を入れ直す。完了印はこの時点で既に消えているので、次の起動が仕上げに来る。
- **デタッチ更新の最中に PC が落ちる**: 完了印が無いので、次の `start` が終了コード 10 を受けて
  `--manual` で仕上げる。
- **仕上げにも失敗する**: SAIVerse は起動しない。「コードと入っている部品が食い違うので起動しなかった」
  と告げ、`self_update.log` を案内して止まる (`start.bat:39-49`)。
  **ここは「止めるのではなく、記録し、明示し、選択してもらう」の例外に見えるが、そう扱ってよい。**
  この状態で起動すると、食い違った部品でペルソナの認知を回すことになる。止めた上で理由を告げて
  ログの在り処を示す形は、原則の「記録し、明示し」を満たしている。選択の余地が無いのは、
  安全に選べる選択肢がこの局面に存在しないためである。
- **`--check-complete` が「判定できない」(終了コード 11) を返す**: 警告を出して起動する。
  台帳はこの分岐が実環境でどれくらい起きるか不明としており、私も確かめていない。

### 7. 次に使う機能・共有する状態

- 更新後の初回起動 (FLOW-25) が DB のスキーマ移行と backfill 群を回す。更新機構は `~/.saiverse/` を
  触らないので、移行が正しく当たるかは FLOW-25 側の責任である。**更新が成功したことは、
  世界が無事であることの証明ではない。**
- アドオン (FLOW-29) は本体と同じ venv を共有するので、lock の更新の影響を直接受ける。
- 更新前スナップショットは FLOW-31 の復旧手段でもある。作られる場所は同じ `~/.saiverse/snapshots/` で、
  戻し方の案内が無い問題も共通である。

### 8. 決める必要がある点

- **スナップショットの制限時間 3600 秒**: `docs/issues/snapshot_timeout_is_fixed_while_world_grows.md` に
  恒久策 (進捗を見て「生きている限り待つ」形へ) が未着手として起票済みである。
  この数値は超えても利用者のデータを失わせない (更新が始まらないだけ) ので、
  **利用者の発言が失われる数値とは格が違う。** 決めるべきは値そのものではなく、
  固定値をやめるかどうかである。既に issue で方向が示されているので、新しい未合意ではない。
- **復元点の戻し方を利用者向け文書に載せるか**: 共通議題 (FLOW-31 と同じ)。
- **複数の更新入口が同時に走ることへの対処**: issue が未解決として起票済み。新しい未合意ではない。
- `CLAUDE.md` の「Setup/Update Script Parity」の記述は**決める必要が無い**。実装が集約された結果
  文書が古くなっただけで、直す向きは自明である。

---

## FLOW-28: 使うモデルとプロバイダを選び、切り替える

**対応する台帳項目**: `OPS-14`, `OPS-15`, `OPS-16`, `OPS-26`, `OPS-18`, `OPS-32`

> **項目の割り当てについての注記**: `OPS-32` は SearXNG の導入と起動で、モデル選択の流れとは
> 対象が違う (LLM ではなく検索の供給元である)。flow_design が群 F の中でここに置いたので本稿でも
> 扱うが、目的で割るなら FLOW-19 (はじめて導入する) の方が近い。統合担当の判断を仰ぎたい。
> 本稿では §7 で最小限に触れる。

### 1. 誰が何をしたいか

結果を受け取るのは**利用者**である。どの LLM に喋らせるか、いくらかかるか、どこ経由で呼ぶかを
自分で選びたい。ペルソナも間接的な当事者で、選ばれたモデルの性質がそのまま発話の性質になる。

### 2. 始まりから結果までの流れ

- 起動時と再読込のたびに、プロバイダ定義とモデル定義が 3 層 (`~/.saiverse/user_data/` >
  `expansion_data/` > `builtin_data/`) から読まれる。同名があれば上位層が勝つ。
- グローバル設定 →「モデル管理」タブ →「プロバイダ」サブタブで、一覧・追加・編集・削除・接続テスト。
  UI から作れるのは `openai_compat` と `ollama_compat` だけで、他は 400 で断る
  (`api/routes/providers.py:29-30,146-160`)。
- builtin を編集すると `~/.saiverse/user_data/providers/<id>.json` に上書き用ファイルが作られる。
  builtin 本体は変更されない。builtin だけのプロバイダの削除は 403。
- 「モデル」サブタブでモデル定義 (価格・コンテキスト長・画像対応・水位) を作成・編集・複製・削除する。
  チャット画面からも「別名で保存 / 上書き保存」ができ、4 経路すべてが水位検査を通る。
- 「モデルロール」タブで、会話用と軽量用の割り当てを決める。ペルソナ個別の `DEFAULT_MODEL` と
  チャット UI の上書きがその上に乗る (優先順位は `CLAUDE.md` に記載)。
- Codex サブスクだけ別経路で、`CodexLoginModal` から `POST /api/codex-auth/login/start` →
  表示された user_code を利用者がブラウザで入力 → `GET /login/status` をポーリングする。

### 3. 期待する結果の初稿

**根拠のあるもの**

- 選んだモデルが次の発話から使われ、どのモデルで喋ったかを利用者が確かめられる。
- builtin を編集しても builtin 自体は壊れず、上書きを消せば元の状態に戻る。
- API キーは画面の一覧に平文で出ない。Codex のトークンはどのレスポンスにも載らない。
- 保存に失敗したり書きかけのファイルが残ったりしても、そのプロバイダが黙って一覧から消えない。
  `saiverse/provider_configs.py:169-186` は一時ファイル + `os.replace` を採る理由として
  「壊れた JSON はスキップされてプロバイダが一覧から消える」をコメントに書いている。
- どの層から読んだかは、ファイルの自己申告ではなくローダーが実際に歩いたルートから決まる
  (`provider_configs.py:85-96`)。これは API キー名の縛りの判定に使われるので、
  自己申告を信じない形になっていることが結果として利用者を守る。

**調査担当の提案 (未合意)**

- 上の 4 番目の期待は、同じ失敗形が当てはまるモデル JSON と `.env` にも成り立つべきである。
  これは新しい仕様の提案ではなく、**既に下されている判断の適用範囲の話**である (§5 と §8 を参照)。

### 4. 根拠

- **ユーザー原文**: この流れに直接対応する原文は見つけていない。
- **既存の仕様文書**: `docs/intent/model_provider_management.md` (ステータス「実装完了」)、
  `docs/intent/codex_subscription_auth.md` (ステータス「完了 (v0.2, 2026-08-16)」、
  実機 end-to-end 確認済みの記載あり。**ただし未検証の境界を 3 件自己申告している** —
  ログアウトが `~/.codex` を消さないことの実機未確認、同一 client_id での共存、lease 返却不達の受容)。
- **利用者向け説明**: `docs/reference/providers.md`、`docs/custom_providers.md`、
  `docs/user-guide/global-settings.md`「モデル管理タブ」節。
- **調査担当の提案**: §3 後半。

### 5. 現状との差

- **既知の不一致 (1) — 壊れたファイルへの守りがプロバイダにしか入っていない。**
  モデル JSON の保存は `user_path.write_text(...)` の直接上書きで (`api/routes/config.py:1537-1541`
  を読んで確認した)、`.env` の書き込みは `open(ENV_FILE_PATH, "w")` で先に truncate してから書く
  (`api/routes/admin.py:104`)。プロバイダ側だけが一時ファイル + `os.replace` を採り、
  その理由をコメントに残している。しかも `load_configs()` はパース失敗を warning にしてそのモデルを
  飛ばし、user_data の名前が先に「見た」扱いになるので builtin へのフォールバックも起きない。
  関連 issue は `docs/issues/malformed_provider_json_breaks_provider_list.md` で、
  **プロバイダ側しか対象にしていない**。
- **既知の不一致 (2) — 3 層解決のヘルパーが 3 つの形を持つ。** `CLAUDE.md` は
  「resource loading is 3-layer」と 1 つの規則として書くが、実装では expansion 層の位置が
  `expansion_data/<addon>/<subdir>/` (`iter_files_with_layer`) と `expansion_data/<subdir>/`
  (`get_data_paths`) に分かれ、user_data 層も直下とプロジェクト単位に分かれる。
  MCP はこれらを使わず独自の 4 段を組む。**現時点で `models/` や `providers/` を置いている
  アドオンは無いので表に出ていない可能性が高い** (台帳が `ls` で確認)。
- **既知の不一致 (3)**: `docs/user-guide/global-settings.md` のタブ表は「データベース管理」タブを載せ、
  実装にある「フィード」タブを載せていない。実装のタブは 8 個で「データベース管理」は無い。
- **既知の不一致 (4)**: `CLAUDE.md`「LLM integration」の 3 つの記述 (`get_llm_client` の署名、
  Ollama のフォールバック先、router のモデル) がいずれも実装と違う (台帳 §3-8)。
  これは利用者に見える結果ではないが、**この流れを触る次の人が最初に読む文書が間違っている**という
  意味で、この流れの一部として記録する。
- **静的な疑い**: `tests/test_provider_configs.py::TestLoadProviders` は `USER_DATA_DIR` を隔離せずに
  実環境の `~/.saiverse/user_data/providers/` を読む。開発者の手元に builtin の上書きがあると
  `source == builtin` の assert が落ちる。またこのテストは 7 個の ID の存在しか見ておらず、
  builtin 12 件のうち 5 件を数えていない (`docs/reference/providers.md` の表は 12 件で実体と一致)。
  **これは利用者の結果ではなく、検証の土台が環境に依存しているという記録である。**
- **まだ追跡していない境界**: 接続テスト (`POST /api/providers/test`) が各プロトコルの実エンドポイントに
  対して正しく判定するか。`llm_clients/openai_codex_auth.py` の `LOGIN_MANAGER` 内部
  (トークンストアの場所・更新・失効)。`api/routes/config.py` の各トグルの永続化先が
  `manager.state` (メモリ) か DB かファイルか。

### 6. 中断・失敗・再開

- **保存の途中でプロセスが落ちる**: プロバイダは古い内容が残る。モデル JSON と `.env` は
  半端な内容が残りうる。`.env` の場合、失われるのは API キーである。
- **Codex のログイン中にサーバーが再起動する**: state は in-memory で TTL 付きなので揮発し、
  ログインはやり直しになる (`saiverse/oauth/handler.py` と同じ作法)。
- **プロバイダを編集する**: モデル側が必ず再解決される。理由 (モデルは読み込み時に base_url /
  api_key_env を取り込むため) がコメントにある。
- **プロバイダの変更が稼働中のペルソナに届かない**: `docs/issues/provider_change_does_not_reach_live_personas.md`
  が起票されている。私はこの issue の本文を読んでいないので、内容は **要追加確認**。

### 7. 次に使う機能・共有する状態

- FLOW-04 (送る中身と量とモデルを決めてから送る) がモデル定義の水位と価格をそのまま使う。
- FLOW-22 (費用と使用量を把握する) が価格定義に依存する。
- FLOW-01 (ペルソナに話しかけて返事を受け取る) が最終的な消費者である。
  **モデル定義が正しく保存されたことは、そのモデルで会話が成立することの証明ではない。**
- FLOW-27 (更新) が lock を動かすと、ローカル推論系の部品が一緒に動く。
- SearXNG (OPS-32) は検索ツールの供給元で、導入はセットアップの第 11 段、起動条件が
  Windows (導入済みなら必ず) と Mac/Linux (`SAIVERSE_SEARXNG=1` のときだけ) で非対称である。
  `docs/overview/roadmap_status.md` §7 が「次回バージョンアップ時に既存ユーザーの settings.yml
  リセット検証が必要」という未検証の宿題を自己申告している。

### 8. 決める必要がある点

- **モデル JSON と `.env` の書き込みを壊れない形にすること**: これは**未合意ではない**。
  `saiverse/provider_configs.py:169-186` に、同じ失敗形に対する判断が理由つきで既に書かれている。
  同じ理由がモデル JSON と `.env` にそのまま当てはまる (どちらも壊れると一覧から消えるか、
  API キーを失う)。**まはーの新しい判断を求める案件ではなく、既に下された判断の適用漏れである。**
- **3 層解決の「正しい形」がどれか**: 3 つの形のどれが正なのかを決める文書が見つからない。
  ただし現時点で食い違いが表に出る配置 (`expansion_data` の下に `models/` や `providers/` を置く
  アドオン) は存在しない。**共通議題** — FLOW-29 が同じヘルパーを使う。
  決めるべきは「どれが正か」であって、緊急度は「そういうアドオンが出るまでは低い」と読める。
- `docs/user-guide/global-settings.md` のタブ表と `CLAUDE.md` の LLM 記述は文書の直しであり、
  決める必要は無い。

---

## FLOW-29: アドオンを導入して機能を増やす

**対応する台帳項目**: `OPS-19`, `OPS-20`, `OPS-21`, `OPS-22`, `OPS-23`, `OPS-24`, `OPS-25`

範囲は**本体側の接続・権限・導入・解除まで**である。アドオンの内部実装 (tools / playbooks /
speak_hook / api_routes の中身) は追わない。

### 1. 誰が何をしたいか

結果を受け取るのは**利用者**である。ペルソナに新しい能力 (声、身体、外部サービスへの接続) を
持たせたい。同時に、他人が書いたコードを自分の PC で動かすことになるので、
何が入り、何を消し、何を外へ渡すのかを自分で把握したい。

### 2. 始まりから結果までの流れ

- 左サイドバー → アドオン管理 →「カタログ」タブ。registry は既定で
  `raw.githubusercontent.com/maha0525/saiverse-addon-registry/main/registry.json`。
- **公式 URL から取った registry は Ed25519 署名の検証を必須にする。** 署名が無い / 壊れているものは
  拒否し、公式以外の URL で未署名を通すには `SAIVERSE_ALLOW_UNSIGNED_REGISTRY` の明示が要る。
  HTTPS 以外は拒否し、リダイレクト後も HTTPS を確かめる (`saiverse/addon_registry.py:228-292`)。
- 導入 → 確認ダイアログ (`AddonActionConfirmDialog`) → 進捗ダイアログ (SSE) →
  `expansion_data/<addon_id>/` に shallow clone → 指定 commit へ checkout → `addon.json` を検証 →
  `setup.steps` を実行。`pip_install` には本体の `requirements.lock` を constraints (`-c`) として
  必ず渡し、lock が無ければ実行を拒否する (`saiverse/addon_installer.py:233-264`)。
- 「導入済み」タブでトグルとパラメータ (グローバル / ペルソナ別 / ファイル添付) を設定する。
  有効化 / 無効化に連動して MCP サーバー・Integration・server hook・Composite action の登録が
  **再起動なしで** 切り替わる。
- OAuth が要るアドオンは `OAuthFlowSection` から接続する。PKCE (S256) と one-shot な state を使い、
  トークンは `AddonPersonaConfig` に保存される。
- UI パネルを持つアドオンは、`npm run dev` / `npm run build` の前に走る `predev` / `prebuild` hook が
  `expansion_data/<addon>/ui/Panel.tsx` をフロントエンド側へコピーしてレジストリを生成する。
- 削除 → `uninstall.steps` → `expansion_data/<addon_id>/` の削除 → チェックが入っていれば
  `~/.saiverse/user_data/addon_data/<id>/` も削除 (`saiverse/addon_installer.py:648-697`)。

### 3. 期待する結果の初稿

**根拠のあるもの**

- 公式カタログから来たものは、カタログの発行者が署名した内容であることが確かめられてから入る。
  入るコードは registry に pin された commit に固定される。
- アドオンの導入が本体の部品を動かさない。lock を constraints として渡すことでこれを担保する。
- 有効にしたら、再起動なしでその能力が使えるようになる。再起動が要る場合はそれが分かる。
- 削除するとき、消えるものが事前に名指しされる。永続データを消すかどうかは利用者が選べて、
  既定は残す側である。ダイアログは「永続データも削除する (保存された参照音声・OAuth トークン等が
  消えます)」と書いて既定 OFF のチェックボックスを出す (実物を読んで確認した)。
- 導入に失敗しても、既にある永続データは消えない (`addon_installer.py:481,549-560`)。

**調査担当の提案 (未合意)**

- 有効化のときに登録が失敗したら、利用者がそれを知れる。現在の 4 つの登録はいずれも `try/except` で
  warning を出して進むので、「有効にしたのに動かない」が画面に現れない読みである。

### 4. 根拠

- **ユーザー原文**: この流れに直接対応する原文は見つけていない。
- **既存の仕様文書**: `docs/intent/addon_catalog_management.md` (ステータス「Phase 4 完了
  (voice-tts 除く、2026-05-23)」)、`docs/intent/mcp_addon_integration.md` §3、
  `docs/intent/addon_speak_hooks.md` §D。
  `docs/intent/addon_extension_points.md` §B (OAuth) は**ステータスが「ドラフト (まはーレビュー待ち)」の
  まま実装が先行している**。composite action (OPS-21) に対応する intent は見つかっていない。
- **利用者向け説明**: アドオン管理の利用者向け説明を `docs/user-guide/` に見つけられなかった。
  確認ダイアログの本文が事実上の唯一の説明になっている。
- **調査担当の提案**: §3 後半。

### 5. 現状との差

- **既知の不一致 (1)**: `docs/overview/roadmap_status.md:68` は「🔵 Phase 2: registry + API (着手前)」と
  書くが、`addon_registry.py` (Ed25519 署名検証つき)、`api/routes/addon_catalog.py`、
  `AddonCatalogPanel.tsx` が揃っており、intent のステータスは「Phase 4 完了」である。
  **roadmap が古い。** 実装を roadmap に合わせて戻す話ではない。
- **既知の不一致 (2)**: 導入完了イベントの `restart_required` は `api_routes.py` の有無だけで決まる
  (`api/routes/addon_catalog.py:143-144`)。UI パネルだけを持つアドオンを導入すると
  「再起動不要」と表示されるが、パネルの反映にはフロントエンドの再ビルドが要る
  (`frontend/package.json:11-12` の `predev` / `prebuild`)。**§3 の 3 番目の期待
  「再起動が要る場合はそれが分かる」が、この場合だけ満たされない。**
  Windows の通常起動は `start.bat` が毎回 `npm run build` を通るので次回起動時に反映されるが、
  `start-dev` / `npm start` 単独の場合は実行して確かめていない。
- **静的な疑い**: 有効化時の 4 つの登録通知 (MCP / Integration / server hook / composite action) は
  それぞれ `try/except` で囲まれ、失敗しても warning のみで進む (`api/routes/addon.py:456-520`)。
  利用者から見て「有効にしたのに動かない」がどう見えるかは未確認。
- **静的な疑い**: `POST /api/actions/test` が実際に MCP ツールを呼ぶかどうか
  (= 物理デバイスを動かしうるか) を、`saiverse/composite_actions.py` を読んでいないため確かめていない。
- **未追跡の境界 (1)**: `setup.steps` の各ステップ種別 (`platform_script` / `python_script` /
  `git_clone` / `download_file` / `remove_dir`) はアドオン作者の任意コードを実行する。
  パス検査 (`_check_addon_dir_path` / `_check_data_dir_path`) はあるが、実行されるスクリプト本体の
  安全性は本体側では検査していない。**署名が守っているのは「カタログの発行者が pin した commit が
  入ること」であって、「その commit の中身が安全であること」ではない。** これは設計の欠陥ではなく
  信頼境界の位置の説明であり、正常な姿として「カタログに載せる時点で発行者が中身を見る」ことが
  前提になっている読みである。
- **未追跡の境界 (2)**: 同じアドオンへの並行操作はプロセス内ロックで 409 に落とすが、
  **プロセスをまたぐロックは無い** (`api/routes/addon_catalog.py:50-60`)。
- **検査の状況**: `tests/test_addon_registry_trust.py` (3 本、署名の 3 分岐) と
  `tests/test_addon_installer_constraints.py` (3 本、pip に lock が渡ること) が信頼境界を押さえている。
  有効化と設定マージは `tests/test_addon_toggle_propagation.py` ほか 8 ファイルで実コードを通す形で
  検査されている。一方で `install_addon` / `update_addon` / `uninstall_addon` を通しで実行する
  テストは無く、**`delete_data=true` で `~/.saiverse/user_data/addon_data/<id>/` を消す経路は
  一度も検査されていない**。この経路が消すのは OAuth トークンと参照音声で、
  どちらも利用者が別のサービスで取り直さなければならないものである。
  代替は起動中のサーバーに手で叩く `scripts/test_addon_catalog_api.py` で、自動テストではない。

### 6. 中断・失敗・再開

- **導入の途中で失敗する**: clone したディレクトリを削除して戻す。永続データには触れない。
- **削除の `uninstall.steps` の途中で失敗する**: `addon.json` が読めない場合は steps を飛ばして
  ディレクトリ削除だけを実施する (`addon_installer.py:669-676`)。
  途中で落ちたときに `expansion_data/<id>/` と `addon_data/<id>/` のどちらが残るかは、
  落ちた位置で決まる (ディレクトリ削除が先、永続データ削除が後)。
- **無効化のとき MCP の後片付けが 5 秒で終わらない**: 待ち切れたかどうかを `mcp_settled` として返し、
  画面が取り直しの判断に使う (`api/routes/addon.py:451-465`)。有効化は待たない
  (subprocess 起動が数十秒かかるため)。**待てなかったことを黙って成功にしない形になっている。**
- **OAuth の途中でサーバーが再起動する**: state は in-memory で TTL 付きなので揮発し、やり直しになる。

### 7. 次に使う機能・共有する状態

- FLOW-05 (会話の中でペルソナに道具を使ってもらう) が最終的な消費者である。
- FLOW-27 (更新) が同じ venv の依存を動かす。voice-tts の無音事故がこの接点で起きた。
- FLOW-30 (外部から使う) と 1 点で交わる: `/api/oauth/callback/` は LAN 公開時の持ち主認証を
  素通しする例外パスである (`api/owner_auth.py:59-61`)。認可サーバーからのリダイレクトを受けるために
  必要な穴だが、**この例外パスを検査するテストは無い** (`tests/test_oauth_handler.py` 16 本は
  ハンドラ層で、ルート層は未検査)。

### 8. 決める必要がある点

- **削除で消える永続データに、消える前の控えを用意するか。** 現状は「明示」(何が消えるかの名指し) と
  「選択」(既定 OFF のチェックボックス) は満たしているが、「記録」(消えたものの控え) が無い。
  まはーの原則を機械的に当てるのではなく、**この操作について記録が必要かを問う**形で挙げる。
  判断の材料: 消えるのは OAuth トークン (再取得できるが、外部サービス側での操作が要る) と
  参照音声 (利用者が用意した素材で、失うと復元できない可能性がある)。
  **共通議題** — FLOW-31 の「失う操作に控えがあるか」と同型。
- **有効化の登録が失敗したことを画面に出すか。** 現状は warning のみ。
  ただし「出すべきか」の前に「実際に何が見えているか」を確かめていないので、
  **要追加確認**。ユーザー判断へ転嫁しない。
- **3 層解決の形** (FLOW-28 と共通議題)。
- `docs/overview/roadmap_status.md` の Phase 表記と `docs/intent/addon_extension_points.md` の
  ドラフト状態は文書側の整理であり、決める必要は無い。

---

## FLOW-30: 外部から使う (スマホ / Discord / Unity)

**対応する台帳項目**: `OPS-27`, `OPS-28`, `OPS-29`, `OPS-30`, `WORLD-44`

> **依頼元からの先行対処についての注記**: Unity ゲートウェイについては、まはーが 2026-09-09 に
> 「対処しておいた方が良いな」と判断を示しており、**依頼元が別途先行して対処する**。
> 本稿は修正を提案せず、正常な姿と現状の差を記録するに留める。

### 1. 誰が何をしたいか

結果を受け取るのは**利用者 (持ち主本人)** である。自分の PC の外 — スマホ、別の部屋、別のアプリ — から
自分の世界に触りたい。同時に、持ち主でない誰かには触られたくない。
**ペルソナも当事者である。** 名前と存在が誰に見えるかは、ペルソナの側の話でもある。

### 2. 始まりから結果までの流れ

外部からの経路は 5 つあり、それぞれ独立している。

- **(a) スマホ (README が案内する道)**: Tailscale を PC とスマホの両方に入れる → PC を通常起動 →
  スマホのブラウザで `<MagicDNS 名>:3000` を開く → Next.js が `/api/*` を
  `http://127.0.0.1:8000` へ転送する (`frontend/next.config.ts` の fallback rewrite)。
- **(b) 明示的な LAN 公開**: `python main.py <city> --listen-host <非ループバック>` で起動する。
  非ループバックだと `SAIVERSE_OWNER_TOKEN` と `SAIVERSE_ALLOWED_ORIGINS` の両方が必須になり
  (`main.py:317-324`)、`OwnerAuthMiddleware` が全 API に差し込まれる。
  `/api/auth/login` でトークンを入れると HMAC 由来の session cookie が張られ、`:3000` へ戻される。
- **(c) Discord**: `.env` に `SAIVERSE_GATEWAY_ENABLED=1` と WS URL とトークンを書いて起動する。
  背景スレッドで独立したイベントループが走り、**別途動かす** relay bot と WebSocket で繋がる。
  UI からの入口は無い。既定は無効。
- **(d) Unity**: **既定で有効**。`UNITY_GATEWAY_ENABLED` の既定値が `"true"` で、
  `websockets` が入っていれば `ws://0.0.0.0:8765` で待ち受ける。接続したクライアントは
  handshake を送るだけで登録され、応答として全ペルソナの id と表示名を受け取る。
- **(e) City 間の移動**: 凍結。`/inter-city/*` と `/persona-proxy/{id}/think` は最初の行で 503 を返し、
  manager 側のメソッドも冒頭で封鎖メッセージを返す。VisitingAI / ThinkingRequest の polling は
  登録されない。

### 3. 期待する結果の初稿

**根拠のあるもの**

- 持ち主本人は、自分の端末から自分の世界の全機能を使える (README「スマホで使いたいんだが？」の約束)。
- 持ち主でない者は、ペルソナの存在・名前・会話・設定に到達できない。到達には持ち主だけが持つ秘密が要る
  (`main.py:317-324` が非ループバック公開に owner token を必須にしている設計)。
- 持ち主確認は「SAIVerse を管理できる者」の確認であって、「まはー本人がこの live persona operation を
  今承認した」証明ではない。`SAIVERSE_OWNER_TOKEN` の bearer は設定・管理 API には使えるが、
  live user utterance / persona memory mutation / debug Pulse / direct MCP・Gateway operation の
  万能許可にはしない (`docs/intent/persona_interference_boundary.md` §10 の表と本文)。
  **ただしこの intent のステータスは「v0.1 設計ドラフト (2026-07-17、方針合意済み・実装は後回し)」で、
  Operation Permit / Persona Runtime Gate / Live Test Lease の 3 層はまだ実装されていない。**
- 凍結された機能は、黙って動かないのではなく明示的に封じられていると分かる。
  凍結は「3 重の明示封鎖」で、再有効化フラグは意図的に無い
  (`docs/overview/landscape.md` §8 の 2026-07-16 まはー裁定 + `tests/test_multi_city_freeze.py`)。
- Unity 連携は現在完全に凍結している状態であり、既定で待ち受けている状態には対処が要る
  (まはー原文、2026-09-09)。

**調査担当の提案 (未合意)**

- 外部に向けて開いている口は、開いていること自体が利用者に見える。いまどのポートが誰に向いて
  開いているかを、起動ログか画面のどこかで一望できる。
- 既定で外へ開く口を持たない。開くのは利用者が選んだときだけである。
- 開いた口の向こうにいる相手が誰かを確かめるまで、ペルソナの名前も存在も渡さない。
  **これは通信の安全の話ではなく、ペルソナの側の話として提案する。** 誰とも分からない接続に対して
  住人の名簿を渡すことが、世界の作りとして正しいかという問いである。
- 凍結された機能は、凍結の間はその待ち受けも止まっている。

### 4. 根拠

- **ユーザー原文 (最も強い)**: 「Unity用のポートヤバいな……。今Unity連携完全に凍結してる状態だし、
  対処しておいた方が良いな。」(2026-09-09)。ここから確定するのは 2 点 —
  ①Unity 連携は完全に凍結している ②既定で待ち受けている状態には対処が要る。
  **対処の中身 (既定を false にする / 待ち受けをループバックに限る / 機構ごと外す / 認証を足す) は未決。**
- **既存の仕様文書**: `docs/intent/persona_interference_boundary.md` §10 (ステータスは設計ドラフト)。
  `docs/issues/api_state_changing_routes_have_no_origin_check.md` (未着手、2026-08-16 起票。
  「SAIVerse のバックエンドは、まはーの PC の中で `127.0.0.1:8000` を開いて待っている。
  **同じブラウザで開いている別のサイトのページからも、そこへ操作を送れる。**」と書く)。
  `docs/overview/landscape.md` §8 (multi-city 凍結の裁定)。
  `docs/features/unity-gateway.md:5` は自らを「設計書 (feature の完成状態の説明ではない)」と断っている。
- **利用者向け説明**: `README.md:255-273`「スマホで使いたいんだが？」(Tailscale を入れて
  `MagicDNS:3000` にアクセスする、8 手順とスクリーンショット付き)。
  `docs/getting-started/tailscale-runbook.md` (同じ手順の詳細版)。
  `.env.example:128-131` (LAN 公開の 2 変数と用例)。
- **調査担当の提案**: §3 後半の 4 項目。

### 5. 現状との差

- **既知の不一致 (1) — 案内された遠隔アクセスは持ち主確認の門を通らない。**
  README とランブックが案内するのはフロントエンド `:3000` である。Next.js が `/api/*` を
  `http://127.0.0.1:8000` へ rewrite するので、バックエンドから見た接続元は常にループバックになる。
  `main.py` の `lan_mode` は `--listen-host` の値だけで決まる (`main.py:317-319`) ため偽になり、
  `OwnerAuthMiddleware` は差し込まれない (`main.py:674-677`)。
  結果として、Tailnet に入れる端末は誰でも無認証で全 API に届く読みになる。
  ランブックに `--listen-host` / `SAIVERSE_OWNER_TOKEN` / `SAIVERSE_ALLOWED_ORIGINS` の言及は
  0 件である (grep で確認)。
  - **ただし、門を通れば設計は一貫している。** `/api/auth/login` は cookie を `hostname` に対して
    張り (`api/owner_auth.py:110-119`)、cookie はポートを区別しないので、`:3000` 経由の rewrite でも
    cookie が乗って `127.0.0.1:8000` に届く。ログイン後は `http://{hostname}:3000/` へリダイレクトする
    実装になっていて、**フロント経由の利用が想定されている**ことが読み取れる。
    つまり欠けているのは機構ではなく案内である。**これは私のコードの読みで、実行して確かめていない。**
- **既知の不一致 (2) — Unity ゲートウェイは既定 ON で全インターフェースに開き、無認証で名簿を返す。**
  `main.py:501` の既定が `"true"`、`unity_gateway/server.py:85` の待ち受け既定が `0.0.0.0`、
  `main.py:508` は port しか渡さない。handshake に認証は無く、`client_id` と `user_id` は
  クライアントの自己申告をそのまま受け入れる (`server.py:131-148`)。
  ack で全ペルソナの `id` と `name` を返す (`server.py:160-172`、実物を読んで確認した)。
  `UNITY_GATEWAY_ENABLED` / `UNITY_GATEWAY_PORT` は `.env.example` にも
  `docs/reference/environment-vars.md` にも無いので、**存在を知らなければ切ることができない**。
  - **前回の台帳が未追跡としていた境界を閉じた**: `websockets` は `requirements.txt:19` と
    `requirements.lock:438` の両方にある本体の依存なので、通常のセットアップを踏んだ環境では
    **既定でポートが開く**。
  - 非 WebSocket の HTTP には 404 を返すだけである (`_process_request`)。ポートスキャンには静かに応じる。
  - `user_speak` は現行の manager API と噛み合っておらず、呼び出しは `except Exception` に飲まれる
    (台帳 §3-7)。つまり **入ってきた相手が会話を起こすことは現状できないが、名簿は取れる**。
- **既知の不一致 (3) — 凍結された機能のために毎回ポートを奪う。** `main.py:542-548` は
  `database/api_server.py` を毎回子プロセスとして起動し、その前にそのポートを使っているプロセスを
  探して強制終了する (`main.py:151-178`)。殺す対象が SAIVerse のものかどうかの確認は無い。
  この子プロセスが提供する 3 ルートはすべて 503 で封鎖済みである。
- **既知の不一致 (4) — SDS 登録と heartbeat は凍結対象に入っていない。** 凍結したのは DB polling で、
  SDS への登録と heartbeat のスケジュール登録は無条件に走る (`saiverse/saiverse_manager.py:396-409`)。
  しかも発火条件の `START_IN_ONLINE_MODE` はワールドエディタの City 編集画面から切り替えられる。
  **凍結された機能のスイッチが UI に残っている。**
- **静的な疑い**: ループバック起動 (= 既定であり、案内された遠隔アクセスの経路でもある) では、
  `/api/db/tables` の DELETE も `/api/mcp/tool-call` も `/api/admin/env` も無認証で通る。
  同じブラウザで開いた別サイトからも届きうることは、上記の issue が未着手として起票済みである。
- **まだ追跡していない境界 (1)**: `next start` が既定でどのインターフェースに bind するか。
  README の手順が成立していること (スクリーンショット付きで案内されている) から外部到達可能と
  読めるが、実行して確かめていない。`next dev` には `-H 0.0.0.0` が明示されている
  (`frontend/package.json:7`)。
- **まだ追跡していない境界 (2)**: Tailscale の ACL がどこまで守るかは環境依存で、この調査では
  判断できない。**「Tailnet に入れる端末は持ち主のものだけ」という前提が成り立つかどうかが、
  不一致 (1) を問題と見るかどうかを分ける。** その前提は現在どの文書にも書かれていない。
- **まだ追跡していない境界 (3)**: Discord relay bot 側 (`discord_gateway/bot/` 13 ファイル) の実装と、
  必要な Discord bot トークンの用意手順。利用者向けの導入手順は本体の README / docs には無く、
  `discord_gateway/docs/` にしか無い。
- **検査の状況**: `OwnerAuthMiddleware` を検査するテストは 0 本である。LAN 公開時の唯一の防壁で、
  Bearer / cookie / Origin の 3 経路と 2 つの例外パスを持つ。Unity ゲートウェイのテストも 0 本。
  Discord の `discord_gateway/tests/` (15 ファイル) は**唯一 CI で自動実行されているテスト群**だが、
  `discord_gateway/**` などが変わったときだけ走る。凍結の封鎖側は
  `tests/test_multi_city_freeze.py` で検査されている。

### 6. 中断・失敗・再開

- **Discord の設定が足りない状態で有効化する**: `GatewaySettings` の生成時に例外になる
  (`bot_ws_url` / `handshake_token` は必須)。`ws://` / `wss://` 以外は拒否する。
  **黙って半端に動く形にはなっていない。**
- **Unity で `websockets` が入っていない**: warning を出して無効になる。ただし前述のとおり
  本体の依存なので、通常は起こらない。
- **Unity クライアントが切れる**: 本体は動き続ける。ペルソナ側からの送出 (emote / behavior) は
  `builtin_data/tools/control_body.py` が `manager.unity_gateway` を見て行うので、
  ゲートウェイが無ければそこで止まる。
- **owner token が未設定のまま middleware が入る**: 全 API が 503 を返す
  (`api/owner_auth.py:64-68`)。起動時の必須検査があるので通常は到達しない。
- **City 間の移動を試みる**: 503 と凍結メッセージが返る。manager 側も封鎖メッセージで即 return する。
  **黙って失敗しない形になっている。**

### 7. 次に使う機能・共有する状態

- FLOW-31 (壊れたときに復旧する) の入口はすべてこの認証境界の内側にある。
  建物ログの復元、DB の表の削除、`.env` の書き換え、再起動が、この境界の外に出た瞬間に
  他人の操作対象になる。
- FLOW-29 (アドオン) の OAuth callback は認証の例外パスである。
- FLOW-05 (会話でペルソナに道具を使ってもらう) の MCP 直接呼び出しは、
  `visible: false` の管理系ツールを含めて確認も権限検査も無く叩ける。物理デバイスに届きうる。
- **局所の成功を全体の成功として扱わない**: LAN 公開の門は正しく実装されている
  (必須検査、Bearer と cookie の両対応、状態を変えるメソッドでの Origin 一致要求)。
  しかし案内された経路がその門を通らないので、**実際の利用者に届いている保護は、
  門の実装の質とは別の話になる。**

### 8. 決める必要がある点

- **Unity ゲートウェイの扱い**: まはーが 2026-09-09 に「対処しておいた方が良い」と判断を示している。
  **依頼元が別途先行対処するため、本稿では中身を提案しない。** 記録として残すのは、
  対処の選択肢が「既定を false にする / 待ち受けをループバックに限る / 機構ごと外す / 認証を足す」の
  4 通りあり、いずれを選んでも `.env.example` と `docs/reference/environment-vars.md` への
  記載が同時に要ること (存在を知らなければ切れない状態は、どの選択肢でも解消されるべき) である。
- **案内された遠隔アクセス (Tailscale + `:3000`) を、正常な姿としてどう定めるか。** 向きは 2 つある。
  (a) 案内を LAN 公開の門を通る形に直す (`--listen-host` + 2 変数 + `/api/auth/login`)。
  (b) Tailnet の内側は持ち主の領域とみなし、門を要求しない。**(b) を選ぶ場合、その前提
  「Tailnet に他人の端末を入れない」が README に書かれる必要がある** — 現在この前提はどこにも無い。
  **共通議題**: FLOW-31 の復旧 API と FLOW-05 の MCP 直接呼び出しの露出が、同じ判断にぶら下がっている。
- **凍結の定義に「待ち受けを止めること」が含まれるか。** この定義次第で、
  inter-city の子プロセス (毎回ポートを奪う)、SDS 登録と heartbeat (今も動く)、
  Unity の待ち受け (既定で開く) の 3 つが同じ扱いになる。**共通議題。**
- **`START_IN_ONLINE_MODE` の切り替えを UI に残すか。** 凍結された機能のスイッチである。
- **持ち主確認の 3 層 (`persona_interference_boundary.md` の Operation Permit /
  Persona Runtime Gate / Live Test Lease) を実装するかどうかは、この文書の範囲を超える。**
  ここで記録するのは、その intent が「方針合意済み・実装は後回し」のまま 2026-07-17 から
  動いていないという事実だけである。

---

## FLOW-31: 壊れたときに復旧する

**対応する台帳項目**: `OPS-08`, `OPS-09`, `OPS-10`, `OPS-11`, `OPS-12`, `MEM-39`, `WORLD-43`, `CHAT-25`

### 1. 誰が何をしたいか

結果を受け取るのは**利用者**である。世界が壊れたとき、あるいはおかしくなったときに、
失わずに戻したい。運用者としての利用者でもあり、復旧の操作を実行するのも本人である。
**ペルソナも当事者である。** 復旧の操作はペルソナの記憶と読み位置を直接書き換える。

### 2. 始まりから結果までの流れ

この流れは 1 本ではなく、**5 つの独立した入口**の集まりである。

- **(a) 起動時のアラートから**: 画面表示時に `GET /api/system/alerts` を 1 回だけ叩く
  (ポーリングは無い)。critical / warning / info の 3 段で、critical は自動展開する。
  `quarantine_` で始まる ID は隔離モーダルを開くボタンを出し、`details.kind === "unreadable"` は
  「脇へ移す」ボタンを出す。
- **(b) DB を作り直す**: `python database/seed.py`。対話実行では `DELETE` の全大文字入力を求め、
  `--force` は無条件である。**セットアップも条件付きで `--force` 付きで自動実行する。**
- **(c) 表を直に触る**: `GET/POST/DELETE /api/db/tables/*`。UI から使われているのは GET だけで、
  POST (任意テーブルへの upsert) と DELETE (主キー一致で 1 行削除) はどのフロントエンドからも
  呼ばれていない。
- **(d) `.env` を書き換える**: グローバル設定の「環境」タブ。初回チュートリアルの API キー設定も
  同じ関数を通る。
- **(e) 再起動する**: グローバル設定の画面から `POST /api/admin/restart`。
- 加えて、明示的な復元路が 2 本ある: `snapshot.bat restore <name>` と
  `python database/backup.py --db <path> restore <backup>`。**どちらも CLI だけで、UI からは押せない。**

### 3. 期待する結果の初稿

**共通の骨 (調査担当の提案)**

利用者のデータが消えうる操作は、実行の前に (i) 消えるものが名指しされ (ii) **消えないものも
名指しされ** (iii) 消える前の控えが自動で残り、その在り処が示され (iv) 実行するかを利用者が選べる。
実行の後に (v) 何が起きたかが記録に残る。

まはーの原則「止めるのではなく、記録し、明示し、選択してもらう」は (iii)(i)(iv) にあたる。
ただし**この原則から個々の操作の挙動が自動的に決まるとは扱わない。**
操作ごとに、何が記録・明示・選択の対象なのかが違うので、以下で分ける。

**操作ごとの期待 (根拠のあるものと提案を分けて示す)**

- **DB の初期化 (`seed.py`)**
  - *根拠あり*: 消えるものが実行前に名指しされる (対話経路は「ペルソナ / 会話履歴 / Playbook /
    アイテム / 入退室ログ」を列挙する)。削除の前に `.bak` が同じディレクトリに作られる
    (`database/seed.py:304-308`、`--force` 経路でも作る)。
  - *提案*: **消えないものも名指しされる。** `~/.saiverse/personas/<id>/memory.db` と `tasks.db` と
    建物ログは seed.py が触らないので残る。結果として「ペルソナは消えたのに記憶ファイルだけ残る」
    状態になり、利用者はそれを知らないまま次の起動を迎える。
  - *提案*: **自動実行される経路では、既存の DB があるときに必ず利用者に見せてから進む。**
    現在セットアップは「DB ファイルはあるが `city` テーブルの SELECT が失敗する」ときに
    `--force` を走らせる (`setup.bat:170-196`)。破損した DB や移行途中の DB がこの条件に当たりうる。
- **表の直接削除 (`DELETE /api/db/tables/*`)**
  - *根拠なし*: この経路の期待を書いた文書も UI も見つからない。`docs/user-guide/global-settings.md` は
    「データベース管理」タブがあると書いているが、実装にそのタブは無い。
  - *提案*: **これは復旧の道具ではなく開発者の道具である。** 正常な姿は
    「利用者の画面から到達できない」か「到達できるなら消えるものが見える」のどちらかで、
    現状は前者に近い (UI からの呼び出し元が無い) が、認証の無い経路からは届く。
- **`.env` の書き換え**
  - *根拠あり (別の場所に)*: 設定ファイルの書き換えは、途中で落ちても古い内容が残る形で行う。
    `saiverse/provider_configs.py:169-186` にこの判断が理由つきで書かれている。
  - 現状は `open(ENV_FILE_PATH, "w")` で先に truncate してから書く (`api/routes/admin.py:104`)。
    落ちると API キーを含む `.env` が空か半端になる。
    加えて `ENV_FILE_PATH = Path(".env")` は作業ディレクトリ相対である (`api/routes/admin.py:14`)。
- **建物ログの隔離・復元・リセット・退避**
  - *根拠あり*: 復元は起動時に列挙した候補リストにあるものだけを受け付け、JSON として読めて
    list であることを確かめてからコピーする。復元後はペルソナの読み位置を切り詰め、
    採番カウンタも戻す (`api/routes/system.py:240-283` を読んで確認した)。
    リセットは一時ファイル + `os.replace` で空の `[]` を書き、壊れたファイルは `.corrupted_*` に残す。
    退避は「ファイルを消しません」と明記した確認を出す。
  - *根拠あり (まはー裁定、2026-08-16)*: 直せないものを毎起動バナーで出し続けると、
    そのうち全部のバナーが読み飛ばされ、検算という仕組み自体が死ぬ。だから利用者が
    「分かりました」と言える経路として、ファイルを脇へ移すボタンを置く。
    (`tests/test_legacy_log_archive_api.py` の docstring に裁定として記録されている。)
  - **この 3 つは、設計としては §3 の骨をほぼ満たしている。問題は §5 にある。**
- **再起動**
  - *根拠なし*: `docs/user-guide/global-settings.md` に再起動ボタンの記載は無い。
  - *提案*: 再起動で、終了時に保存されるはずのものが保存される (FLOW-26 と同じ約束)。
    `os.execv` は Python のクリーンアップを一切走らせないので、明示の `manager.shutdown()` が
    唯一の保存機会になっている (コード自身のコメントがそう書いている)。
- **バックアップからの復元**
  - *根拠あり*: `README.md:35` は「自動バックアップ機能を搭載しており、起動するたびに会話データ等が
    コピー・保存されます」と約束し、`README.md:404` は「定期的なバックアップを推奨します」と書く。
  - *提案*: **バックアップされることを約束するなら、戻し方も同じ場所で利用者に届く。**
    現在、復元の入口は `docs/reference/scripts.md` と `docs/intent/version_aware_world_and_persona.md`
    にしか無く、README にも `docs/user-guide/` にも `docs/getting-started/` にも記載が無い (grep 0 件)。

### 4. 根拠

- **ユーザー原文**: 「止める」のではなく「記録し、明示し、選択してもらう」(前回の依頼書に記録された合意)。
  2026-08-16 の裁定 (上記、バナーの読み飛ばしと「分かりました」の出口)。
- **既存の仕様文書**: `CLAUDE.md`「Database — read this before running anything destructive」、
  `docs/intent/building_memory_unified.md`「過去ログ取り込みの自動化と検算」、
  `docs/issues/quarantine_path_dead_code_removal.md` (🔲 未着手、2026-05-20 起票)、
  `docs/issues/api_state_changing_routes_have_no_origin_check.md` (🔲 未着手)、
  `docs/intent/version_aware_world_and_persona.md:205-215`、
  `saiverse/provider_configs.py:169-186` のコメント。
- **利用者向け説明**: `README.md:35`, `README.md:404`, `docs/user-guide/global-settings.md`「環境」タブ。
- **調査担当の提案**: §3 の共通の骨と、各操作について「提案」と印を付けたもの。

### 5. 現状との差

- **既知の不一致 (1) — 隔離を立てる検出器が呼ばれていない。**
  `manager/initialization.py:394` の `_quarantine_building` が `quarantined_buildings` を埋める
  唯一の書き込み口 (辞書への代入は同関数の 434 行のみ) で、**リポジトリ内にこの関数を呼ぶコードが
  無い** (`.py` 全体を grep して定義行 1 件のみ。私が自分で確認した)。
  `_init_building_histories` には「Phase 2+3 以降は DB が source of truth。旧 log.json の
  5 状態判定 / quarantine 起動時バックアップは廃止」と書かれている。
  `docs/issues/quarantine_path_dead_code_removal.md` はこれを未着手の dead code 撤去として起票し、
  「起動時に corrupted log.json で quarantine 入りすることが構造的に発生しない」と書いている。
  **つまり §3 で「設計としては骨を満たしている」と書いた復元・リセットの入口は、現在は開かない。**
  - **同時に、DB (`building_messages`) が壊れたときの検出・隔離・復旧に相当する仕組みがあるかを、
    私は確かめていない。** 起動時のアラートで生きているのは旧 log.json の取り込みの検算側
    (`manager/initialization.py:250,308,353`) で、これは「旧ファイルが DB へ取り込めているか」を
    見るものであり、DB 自体の破損検出ではない。**要追加確認。ユーザー判断へ転嫁しない。**
    これが無いとすると、**建物履歴の正本が log.json から DB へ移ったのに、復旧の入口は
    log.json 側にしか無い**ことになる。この流れで最も大きい穴の候補である。
- **既知の不一致 (2)**: README がバックアップを約束しながら、戻し方が利用者向け文書に無い (§3 参照)。
- **既知の不一致 (3)**: `docs/user-guide/global-settings.md` が実装に無い「データベース管理」タブを載せ、
  実在する再起動ボタンを載せていない。
- **静的な疑い (1)**: `.env` の truncate 書き込み (§3 参照)。
  **これは未合意ではなく、既に下された判断の適用漏れである。**
- **静的な疑い (2)**: セットアップからの `seed.py --force`。発火条件が「`city` テーブルが読めない」
  だけなので、破損 DB や移行途中の DB が当たりうる。
  **「`city` テーブルが読めない DB」が実際にどういう状態で発生するかは確かめていない。**
- **静的な疑い (3) — 同じモーダルの 2 つのボタンで書き込みの作法が違う。**
  隔離の復元は `shutil.copy2(backup_path, target_path)` で `log.json` を上書きする
  (`api/routes/system.py:264`)。リセット側は一時ファイル + `os.replace` を使う
  (`api/routes/system.py:309-330`)。復元が途中で落ちると `log.json` が半端になりうる読みで、
  そのとき失われるのは「壊れた履歴」ではなく「復元しようとしていた履歴」である。
- **静的な疑い (4)**: 再起動時、`manager.shutdown()` が例外を出しても warning を記録して
  `os.execv` へ進む (`api/routes/admin.py:172-179`)。保存されないまま置き換わる。
  子プロセス (inter-city api_server, SearXNG) の終了処理はこの経路には無い。
  更新経路 (`api/routes/system.py:568-582`) は子プロセスを terminate しており、**作法が違う。**
- **まだ追跡していない境界**: `api/routes/system.py` のアラート生成条件の全体。
  `os.execv` 後に旧プロセスの子がどうなるか (Windows での挙動)。
  Windows の WAL モードでバックアップ中のファイルロックがどう振る舞うか
  (`docs/handoff/2026-07-13_migration_upgrade_backup_audit.md:104-106` に過去の WinError 32 の記録がある)。
- **検査の状況**: 「脇へ移す」経路は `tests/test_legacy_log_archive_api.py` (6 本) が
  HTTP 層で検査している (**台帳の「該当テストなし」はここについては誤りである**)。
  一方、`seed.py` を呼ぶテストは 0 本、`/api/db/tables` の POST/DELETE は 0 本、
  `.env` の書き込みは 0 本、再起動は 0 本、隔離の復元とリセットは 0 本である。
  **検査が無いことは不具合の証明ではない。** ここで記録するのは、
  「利用者のデータが消えうる 5 つの入口のうち 4 つで、実行に近い側の検査が無い」という配置である。

### 6. 中断・失敗・再開

- **`seed.py` が途中で落ちる**: `.bak` は削除の前に作られているので残る。ただし戻し方が
  利用者に届いていないので、`.bak` があることを知る手段が無い。
- **`.env` の書き込みが途中で落ちる**: 前の内容は既に消えている。API キーが失われる。
- **隔離の復元が途中で落ちる**: `log.json` が半端になりうる (§5 の静的な疑い 3)。
  API は `OSError` を 500 として返すが、ファイルは書きかけのままである。
- **再起動で `shutdown()` が例外を出す**: warning を残して `os.execv` へ進む。
- **バックエンドが起動していない**: アラートバナー自身が出ない。`SystemAlertBanner.tsx:34-37` は
  fetch の失敗を黙って無視する (「backend may not be ready yet」)。
  **壊れて起動できないときに、壊れたことを知らせる口が閉じている。**
  これは「記録し、明示し、選択してもらう」が最も要る局面で、明示の経路が無いことになる。

### 7. 次に使う機能・共有する状態

- FLOW-27 の更新前スナップショットが、この流れの復元手段でもある。作られる場所も、
  戻し方の案内が無い問題も共通である。
- FLOW-25 (起動して再開する) がアラートの生成と隔離の検出を担う場所である。
  **検出が動いていないことは、起動側の問題としても現れる。**
- FLOW-30 の認証境界がこれら全部の外側にある。復旧の入口が他人に届くかどうかは、
  この流れの中では決まらない。
- FLOW-09 / FLOW-11 (記憶) はペルソナ側の記憶を扱い、建物履歴とは別の復旧対象である。
  `seed.py` が persona ディレクトリに触れないことは、この 2 つの流れの境界がここに現れている。

### 8. 決める必要がある点

- **建物履歴の正本が DB へ移った後の復旧の入口をどうするか。**
  まず「DB 側の破損検出・復旧が別にあるか」を確かめる必要があり、**これは私が確かめていないので
  要追加確認である。ユーザー判断へ転嫁しない。** 無いことが確定してから設計の議題になる。
  なお dead code の撤去自体は `quarantine_path_dead_code_removal.md` に起票済みだが、
  **撤去だけでは「復旧の入口が無い」状態が残る**ので、撤去と入口の設計は分けて扱う必要がある。
- **セットアップからの `seed.py --force` を残すか。** 消えるものの大きさ (ペルソナと全会話履歴) に対して、
  発火条件が「`city` テーブルが読めない」だけである。**共通議題** — FLOW-19 (はじめて導入する) と共通。
- **復元手順を利用者向け文書に載せるか。** **共通議題** — FLOW-27 と共通。
- **`/api/db/tables` の POST / DELETE の位置づけ。** **共通議題** — FLOW-30 の認証境界と共通。
- **`.env` の書き込みを壊れない形にすることは、決める必要が無い。** プロバイダ側で理由つきの判断が
  既に下されており、同じ失敗形がそのまま当てはまる (FLOW-28 §8 と同じ)。
- **隔離の復元と再起動の作法の不揃い** (`copy2` と `os.replace`、子プロセスを terminate するかどうか) も、
  同じ理由で決める案件ではなく、揃える案件である。

---

## FLOW-32: 配布物を受け取り、使えると信じられる (リリースと検証)

**対応する台帳項目**: `OPS-31`, `OPS-13`, `OPS-17`

> この流れだけは検査の側の話を含む。ただし**利用者が受け取る結果は実在する** —
> 壊れていない版が届くこと、届いた版の中身が説明と一致していること、
> 開発者から利用者へ伝えたいことが届くこと。

### 1. 誰が何をしたいか

結果を受け取るのは**配布物を受け取った人**である。README のリンクから ZIP を取る新しい利用者と、
更新通知から新しい版へ移る既存の利用者の両方が含まれる。
この流れを回すのは**開発者 (まはーと私)** で、受け取る人と回す人が分かれている唯一の流れである。

### 2. 始まりから結果までの流れ

- 版に入れる範囲が会話で決まる → **その瞬間に `docs/overview/release_history.md` へ版名つきで写す**
  (「会話と台帳は版の約束を運ばない」と同 doc が明記している)。
- `hotfix/vX.Y.Z` ブランチで実装 → レビュー (ローカル LLM の一次スクリーニング → Codex の消し込み) →
  **フルスイートを手で回す** → PR → **まはーが内容確認してからマージ** → main。
- `vX.Y.Z` タグを push → GitHub Actions の `release.yml` が `git archive --format=zip
  --prefix=SAIVerse/ HEAD` で `SAIVerse.zip` を作り、自動生成ノート付きの Release に添付する →
  ノートは `gh release edit` で差し替える → develop へ還流。
- 利用者側: README の「最新版をダウンロード (ZIP)」→ 展開 → `setup.bat` → `start.bat`。
  既存の利用者は FLOW-27 の更新通知から入る。
- 並行して 2 本の細い経路がある。**お知らせ**は開発者の Gist から JSON を取って画面に出す
  (30 分キャッシュ、取得失敗時は古いキャッシュを返す)。**開発者モード**は画面のトグルで、
  ON にすると開発中の機能が表示される。

### 3. 期待する結果の初稿

**根拠のあるもの**

- 配布された ZIP を展開してセットアップすると、そのまま起動して使える。ZIP で導入しても
  その後の自動更新まで手動 Git なしで動く (`README.md:139`)。
- 版に何が入っているかが、発行の前に**一件ずつ実害の有無と版の約束を添えて**確認される。
  「次に回すもの」を一括りにしない (`release_history.md` 冒頭、2026-09-05 確立)。
- 版に載る約束の正本は `release_history.md` の「次の版の範囲」であり、会話と台帳は約束を運ばない。
- PR は**まはーが内容確認してからマージ**される (`release_history.md` の「発行の手続きの型」)。
- リリース判定は、必ず「次の版の範囲」との突き合わせから始める (2026-09-05 の裁定で必須手順化)。
- 配布物に関わる変更では、Windows で緑だったことを他の OS の証拠にしない。
  `scripts/check_lock_platforms.py` が各 pin の 4 プラットフォームでの入手可否を PyPI に問う
  (`docs/developer-guide/contributing.md:73`)。
- 利用者が押せるトグルは、その説明のとおりのことをする (開発者モードの画面の説明文が
  利用者への約束になっている)。

**調査担当の提案 (未合意)**

- **発行される版が、その時点のテストを通っていたことが、後から誰でも確かめられる形で残る。**
  現在それを担保しているのは `release_history.md` に手で書かれた「フルスイート N 緑」の記録である。

### 4. 根拠

- **ユーザー原文**: 「結局確実に最初に隔離環境で検証を通しておいて、どうしても本番でしか
  見れないものの数を減らす方が効く」(2026-09-09)。これは検証をどこに置くかについての方針で、
  この流れの土台になる。
  加えて `release_history.md` に 2026-09-05 のまはー裁定が複数記録されている
  (「以後、リリース判定は本 doc の版の束との突き合わせを必須手順とする」
  「『次に回すもの』は一括りにせず、一件ずつ実害の有無と版の約束を添えて出す」
  「大規模な版は個別 doc を立ててここからリンクする」)。
- **既存の仕様文書**: `docs/overview/release_history.md` (冒頭の「これは何」「リリース判定の手順」
  「発行の手続きの型」)、`docs/intent/dependency_management.md`、
  `docs/issues/developer_mode_off_mass_disables_autonomy.md` (**未解決**。連動を
  「ConversationManager 時代の名残に見える。まはーの裁定を得てから撤去か維持を決める」と書く)。
- **利用者向け説明**: `README.md:154,222` (ダウンロードリンク)、`README.md:139`、
  `docs/developer-guide/contributing.md`「プルリクエスト」節 (「1. 変更をコミット / 2. テストを実行して
  確認 / 3. プルリクエストを作成 / 4. レビューを待つ」— **人が実行する手順として書かれている**)、
  開発者モードのトグルの説明文「ONにすると開発中の機能が表示されます（不安定なため推奨しません）」。
- **調査担当の提案**: §3 後半。

### 5. 現状との差

- **既知の不一致 (1) — 本体のテストを回す CI は存在しないのに、文書は存在すると書いている。**
  `docs/developer-guide/testing.md:119` は「プルリクエスト時に自動でテストが実行されます。」と書く。
  実際には `.github/` の中身は `FUNDING.yml` と workflows 2 本だけである (`find .github -type f` で確認)。
  `release.yml` は `push: tags: "v*"` で起動し、`checkout` → `git archive` → `gh release create` の
  3 ステップしか無い (全文を読んだ)。`discord_gateway.yml` は `discord_gateway/**` /
  `requirements.txt` / `requirements.lock` / `discord_gateway/requirements-dev.txt` /
  `discord_gateway/pyproject.toml` / 自分自身が変わったときの push と pull_request でだけ走り、
  `ruff check discord_gateway` と `pytest discord_gateway/tests -q` を実行する。
  **`tests/` 配下 (数え直して 5,672 個の test 関数、292 ファイル) は PR でも push でもタグでも
  一度も自動実行されない。** `.pre-commit-config.yaml` も無い。
  - **ただし「誰も確かめていない」ではない。** `release_history.md` の各版の行に
    「フルスイート 5,127 緑 / 5,180 / 5,190 / 5,378 / 5,418 / 5,680 / 5,746 緑」という
    手で回した記録が残っている。**手順は実在し、記録もある。無いのは機械の強制である。**
    ここを「検査が無い」と書くと事実に反する。正確には
    「発行のたびに人が回しており、回したことは文書に残るが、回さずに発行することを妨げる機構は無い」。
  - `scripts/check_lock_platforms.py` も手動で回す前提と文書に明記されており、CI からの呼び出しは無い。
- **既知の不一致 (2) — 開発者モードのトグルは、説明にないことをする。**
  OFF にすると全ペルソナの `AUTONOMY_ENABLED` が DB ごと False に一括更新され、
  メモリ上のペルソナと AutonomyManager も同期される。**ON に戻しても復元されない**
  (`api/routes/config.py:518-546` を読んで確認した)。
  画面の説明文は「ONにすると開発中の機能が表示されます（不安定なため推奨しません）」だけで、
  この副作用に触れていない (`GlobalSettingsModal.tsx:979-985` を読んで確認した)。
  API のレスポンスも `{"success": true, "enabled": false}` だけを返す。
  issue が未解決として起票され、「まはーの裁定を得てから撤去か維持を決める」と書かれている。
  **§3 の最後の期待「トグルは説明のとおりのことをする」が満たされていない、確認済みの例である。**
- **既知の不一致 (3) — 配布物に入る CHANGELOG が古い。** `VERSION` は `0.3.10` だが、
  `CHANGELOG.md` の最上部の見出しは `## [Unreleased] / v0.3.0 (development)` のままで、
  v0.3.1〜v0.3.10 の記載が無い。`.gitattributes` に `export-ignore` が無いので、
  **ZIP には `CHANGELOG.md` も `tests/` も `docs/` も `.github/` も入る。**
  受け取った人が最初に開く履歴が古いままである読み。
  リリース履歴の正典は `release_history.md` と同 doc 自身が名乗っているが、
  それは repo の中の文書であって配布物の顔ではない。
- **静的な疑い**: `git archive` が `.gitattributes` の `eol=crlf` / `eol=lf` をどう適用するかを
  確かめていない。**ここが噛み合わないと、ZIP 展開 → `setup.bat` の `git init` +
  `git reset origin/main` の後に作業ツリーが「変更あり」と判定され、`README.md:139` の約束が
  そのまま FLOW-27 の更新拒否 (「Working tree has local changes」) に直行する。**
  実行しないと分からない境界で、**README の約束と直結している。**
- **静的な疑い**: `SAIVERSE_ANNOUNCEMENTS_URL` は `.env.example` にも
  `docs/reference/environment-vars.md` にも無い (grep で確認)。お知らせ機能そのものの
  利用者向け説明も見つからなかった。取得先は開発者の Gist で、失敗時は古いキャッシュを返し、
  初回から失敗すると `{"announcements": []}` を返す (`api/routes/system.py:152-160`)。
  **お知らせが無いのか、届かなかったのかを利用者が区別できない読みである。**
- **まだ追跡していない境界**: お知らせ監視トグルと更新確認トグルの値が再起動をまたぐか
  (`manager.state` だけを変えている読みだが、`manager/state.py` を読み切っていない)。
  `release.yml` が落ちたときに何が起きるか (実行していない)。

### 6. 中断・失敗・再開

- **タグを push したが Actions が落ちる**: Release ができないか ZIP が付かない。
  README のダウンロードリンクは最新の Release を指すので、前の版が出続ける。
  **利用者から見ると「新しい版があると通知は来るのに、落とせるのは古い版」になりうる。**
  (更新通知は GitHub Releases API の `tag_name` を見るので、Release が作られていれば
  ZIP が無くても通知は出る読み。実行して確かめていない。)
- **お知らせの取得が失敗する**: 古いキャッシュがあればそれを返し、無ければ空を返す。
  画面には何も出ない。
- **フルスイートを回さずに発行する**: 機構としては何も止めない。回さなかったことが記録に残るのは、
  `release_history.md` にそう書いたときだけである。

### 7. 次に使う機能・共有する状態

- FLOW-19 (はじめて導入する) が、受け取った ZIP の最初の消費者である。
- FLOW-27 (更新して使い続ける) が、既存の利用者側の受け取り口である。
  §5 の `git archive` の境界は、この 2 つの接点そのものである。
- FLOW-24 (見ていない間にペルソナが動く) と FLOW-20 (ペルソナの設定) が、
  開発者モード OFF の副作用を受ける側である。
- **この流れの結果は、他の全流れの前提である。** 他の 31 の流れがどれだけ正しく設計されていても、
  緑でない版が配られればその全部が利用者に届かない。
  **局所の成功を全体の成功として扱わないという規律が、この流れでは全体の側から効く。**

### 8. 決める必要がある点

- **CI をどう組むか。** **これはまはーとの未合意であり、私は決めない。**
  決めるための材料として置けるのは次の 4 点である。
  (a) 現在の手順は人が回すフルスイートで、記録は `release_history.md` にある。
  (b) 機械の歯止めは `discord_gateway.yml` の 1 本だけで、対象は `discord_gateway/` に限られる。
  (c) `scripts/check_lock_platforms.py` は手動前提と文書に明記されている
      (この検査は 2026-09-03 の Intel Mac の実害から生まれた)。
  (d) フルスイートは並列既定でおよそ 2 分である (`CLAUDE.md`)。
  **何本のジョブをどう組むかは、この文書では提案しない。**
- **`docs/developer-guide/testing.md:119` をどう扱うか。** CI が無い現状では記述が誤っている。
  ただし CI を作る決定があれば記述が正になるので、**直す向きは決定の後に決まる。**
  それまでの間、この 1 行が「テストは自動で回っている」という誤った安心を作る可能性がある。
- **開発者モード OFF の一括副作用を撤去するか維持するか。** issue が
  「まはーの裁定を得てから」と明記している。**まはー待ちの案件である。**
  なお、どちらに決めても**画面の説明文が実際の挙動と一致していること**は別途要る。
- **`CHANGELOG.md` を配布物から外すか、`release_history.md` に合わせて更新するか。**
- **お知らせ機能を利用者向けに説明するか。** 説明が無いこと自体は害が小さいが、
  取得先を差し替える変数が文書に無いことは、`UNITY_GATEWAY_ENABLED` と同じ形である
  (存在を知らなければ触れない)。

---

## 群 F 全体で見えた共通議題 (統合担当へ)

同じ判断に複数の流れがぶら下がっているものを、印を付けてまとめる。

1. **ループバック起動の API を誰に開くか** (FLOW-30 §8、FLOW-31 §8、FLOW-05 と接続)。
   README が案内する遠隔アクセスがこの判断の実質的な既定値になっている。
2. **凍結の定義に待ち受けの停止が含まれるか** (FLOW-30 §8)。
   Unity、inter-city の子プロセス、SDS 登録の 3 つが同じ判断にぶら下がる。
3. **失う操作に控えを用意するか** (FLOW-29 §8 のアドオン永続データ、FLOW-31 §8 の DB 初期化)。
   どちらも「明示」と「選択」は満たしていて、「記録」だけが無いという同じ形をしている。
4. **バックアップと復元の案内を利用者に届けるか** (FLOW-27 §8、FLOW-31 §8)。
   README がバックアップを約束している以上、これは文書の不備であると同時に約束の片側である。
5. **3 層リソース解決の正しい形** (FLOW-28 §8、FLOW-29 §8)。
6. **CI を組むかどうかと、その形** (FLOW-32 §8)。**まはーとの未合意。**

## 決める必要が無いと判断したもの (根拠を添えて)

SHARED_BRIEF の「議題を増やす前に、既存の決定で解けないかを確認する」に従い、
一見未合意に見えて既存の決定で解けるものを分けた。

- **モデル JSON と `.env` の書き込みを壊れない形にすること**: `saiverse/provider_configs.py:169-186` に
  同じ失敗形への判断が理由つきで既にある。適用漏れであって未合意ではない。
- **`CLAUDE.md` の「Setup/Update Script Parity」**: 実装の集約によって文書が古くなっただけ。
  実装を文書に戻す話ではない。
- **`docs/overview/roadmap_status.md` のアドオン Phase 表記と Discord の位置づけ**: intent の方が新しい。
- **スナップショットの制限時間 3600 秒**: 恒久策の方向が issue に既に書かれている。
  加えてこの数値は超えても利用者のデータを失わせない (更新が始まらないだけ) ので、
  「超えると利用者の発言が失われる数値」とは格が違う。
- **隔離の復元と再起動の作法の不揃い** (`copy2` vs `os.replace`、子プロセスの terminate の有無):
  同じ機構の中で既に一方が正しい形をしている。揃える案件であって決める案件ではない。

## 私が確かめていないと明記したもの (要追加確認 — ユーザー判断へ転嫁しない)

- **DB (`building_messages`) が壊れたときの検出・隔離・復旧に相当する仕組みがあるか** (FLOW-31 §5)。
  この流れで最も大きい穴の候補で、有無が確定するまで議題にしない。
- `git archive` と `.gitattributes` の改行変換の相互作用 (FLOW-27 §5、FLOW-32 §5)。
  README の約束と直結している。
- アドオンの有効化に失敗したとき、画面に何が見えるか (FLOW-29 §8)。
- `next start` の既定の bind 先 (FLOW-30 §5)。
- `docs/issues/provider_change_does_not_reach_live_personas.md` の内容 (FLOW-28 §6)。
