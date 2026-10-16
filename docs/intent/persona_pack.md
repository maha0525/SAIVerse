# ペルソナまるごとパック — ペルソナ丸ごとのエクスポート / インポート

**status**: 実装中（設計は 2026-10-16 に確定 — §0 の経緯のとおり、全論点がまはー承認済み）

ユーザー向けの名称は「**ペルソナまるごとパック**」（2026-10-16 まはー選定。「確かにペルソナを丸ごとパッキングしてるんだもん」）。ZIP の目録に書く機械用の識別子は `persona-pack`。候補にあった「アーカイブ」は、ChatGPT などでスレッドの凍結を指す言葉で「ペルソナをアーカイブする＝喋れなくなる」と読めるため退けた（まはー）。
**関連**:
- `docs/overview/persona_data_map.html` — ペルソナのデータ地図。ペルソナが持つ全データの棚卸しで、書き出す対象データの正式な一覧
- `saiverse_memory/native_export.py` / `api/routes/people/native_export_import.py` — 旧スレッド単位エクスポート（本機能で置き換える）。冒頭に 2026-07-16 のまはー裁定「復元と移植の区別」があり、§5 はこれを継承する
- `tools/utilities/chatgpt_importer.py` / `api/routes/people/import_chatlog.py` — 既存の ChatGPT 会話取り込み（共存する）
- `docs/intent/saiverse_lite.md` — SAIVerse Lite。Lite からの引っ越しに必要だった「本体側でのペルソナ一括取り込み口」を本機能で用意する

## 0. 合意の経緯

2026-10-16 の会話でまはーが決めたのは次の三点。

0. **名称は「ペルソナまるごとパック」**（同日まはー選定）。
1. **最初から合流（マージ）可能な設計にする。** 「常に新規作成」で先送りすると、合流できない形式の ZIP が残って v1/v2 の互換性を余計に背負う（まはー「最初から上書き・合流可能な設計にしておきたい」）。
2. **v1 は全部入れる。** 選んで出す機能（チェックを外してエクスポート）は v2 以降とし、形式には切り分けの余地だけ残す（まはー「基本的に全部入れてしまえるようにする」）。
3. **書き換わる記録の衝突は、日時が新しい方を採用する。合流先はユーザーが選ぶ**（まはー「大枠その形でいい」）。

さらに同日、残っていた三つの論点もまはーが承認した（「1、2、3、いずれもいいよ」）: ①建物の会話履歴は運ばない（§4）、②プロフィールの合流は「取り込み時に上書きを選べる・既定は現状維持」（§5）、③合流の割り切り三点 — 短縮番号の指し先のずれ・削除の非伝搬・Chronicle の二重のあらすじ — を v1 の仕様として受け入れる（§5）。これで本文の全節が合意済み。

## 1. 目的 — ユーザーが頼れるようになること

1. **ペルソナ一体の全データを、一つの ZIP として持ち出せる。** 中身は人間が読める JSON ファイルと、画像・添付ファイルそのもので、特定のソフトが無くても開いて読める。
2. **その ZIP を別の環境（新しい PC の SAIVerse、SAIVerse Lite）に取り込めば、同じ子が記憶ごと続きから生きられる。**
3. **他のチャットアプリへも会話を持ち込める。** ZIP 内に ChatGPT のエクスポート形式と互換のコピーを同梱する。

旧来のスレッド単位エクスポートは、`thought_signature`（LLM が返す思考の署名。bytes のまま保存されている）を含むスレッドで JSON 化に失敗して壊れている。修理せず、本機能で置き換える。

## 2. 全体の流れ

```
書き出し元                形式                         取り込み先
────────────            ─────────────                ────────────
本体のエクスポート API →  ZIP (manifest + JSON 群     → 本体のインポート（新規 / 合流）
SAIVerse Lite             + 添付ファイル・画像など)    → SAIVerse Lite
                                                      → 他アプリ（chatgpt_compat/ 経由）
                                                      → 人間（手で開いて読む・確認する）
```

- エクスポートは**読み取り専用の操作**。実行してもペルソナの状態は一切変わらない。
- Lite 側の対応は `saiverse_lite.md` §5 の決まり（形式仕様を docs/reference/ に文書化し、変更は両側同時に更新）に従って別途設計する。Lite が扱わないデータ（extensions/ の中身など）は、Lite が再エクスポートするまで**素通しで保持**し、Lite 経由の往復で黙って欠けさせない。Lite 側の記録 ID が UUID かどうかの確認も Lite 側の作業に含める。

## 3. 設計原則（不変条件）

1. **割り当ての漏れを許さない**: データ地図にある全項目を、実装時に「どのフォルダに入るか / 運ばない（理由つき）」のどちらかに割り当て、その確定表をこの intent に書き戻す。ZIP に入るのはその表に挙げたデータだけに限る。**認証情報（API キー・トークン類）は決して入れない。**
2. **本人名義のテキストは無改変で運ぶ。** 発話・思考・ページ本文を切り詰めたり整形したりしない。
3. **すべての記録に ID と日時を付けて書き出す。** メッセージ・Memopedia ページ・切り抜きには作成時に UUID（重複しない固有 ID）が付いているため、そのまま書き出す。手帳の行（activities / memos）は整数の連番しか持たないので、実装時に UUID 列を足す移行を行う。この ID があることで、最初から §5 の合流が可能になる。
4. **`manifest.json`（目録ファイル）に同梱フォルダの一覧を記載する。** `contents` 欄に同梱フォルダ名を列挙し、インポート側はそこにあるフォルダだけを取り込む。一部のフォルダが無い ZIP も正常に扱えるため、将来「不要なデータのチェックを外してエクスポートする」機能を作る際も、フォルダを減らすだけで形式を変えずに実現できる。
5. **データ形式のルール**: バイナリ値（`thought_signature` 等）は base64 文字列に変換する。日時はタイムゾーン付き ISO 8601 — ただし元データがタイムゾーンを持たない列（`saiverse.db` の DateTime 列）は、UTC と決めつけて書き換えず元の値のまま出す。ZIP 内のファイル参照は常に ZIP の最上位（ルート）からの相対パスで書く。`extensions/saiverse/` の機械的な書き出しは値を変換せず元のまま。

## 4. 同梱するデータ — ZIP に何を入れるか

方針は「**ペルソナ個人のものは全部入れる。運ばないのは、取り込み先で再生成できる派生データと、取り込み先で作り直される運用状態だけ**」。下の表は全体像で、データ地図の全項目をどこに割り当てたかの確定表は §9 にある（§3-1 の約束の履行）。

| フォルダ | 中身 | 出どころ |
|---|---|---|
| `manifest.json` | 形式名・版・生成元・作成日時・ペルソナ名・contents | — |
| `profile/` | 名前・システムプロンプト・説明文・アイコン画像・モデル設定（モデル名のみ。キーは入れない）・記憶機能の ON/OFF | `saiverse.db`（本体DB）の AI 行 + アイコンファイル |
| `chatlog/` | スレッドと全メッセージ。**messages の全列と metadata を丸ごと**運ぶ（id・時刻・role・話者・タグ・添付参照に加え、同じ秒の中の並び順の鍵 seq、コンテキストへの載せ方を決める scope、pulse_id、Stelis（本筋から切り離して進める子スレッドの仕組み）の情報なども落とさない） | `memory.db` の threads / messages |
| `knowledge/` | Memopedia 全ページ（コア記憶 = core 分類、Chronicle = chronicle 分類もページとしてここに入る） | `memory.db` の memopedia_pages |
| `attachments/` | 添付ファイルそのもの | 共有メディアフォルダのうち、このペルソナの記録が参照しているもの |
| `avatar/` `voice/` | 立ち絵・表情差分・声の設定。**v1 では空** — 実装時に確認した結果、本体に立ち絵の保存場所が無く（screen_avatar は draft）、声の設定はアドオンのペルソナ別設定（addon_persona_config = 認証情報と同じ表）に入る仕組みのため、書き出すものが無い。フォルダの席だけ規格に確保してある（§8） | — |
| `items/` | 所持品・私室（部屋とその中の配置を「持ち物」として記述） | `saiverse.db` + `memory.db` |
| `journal/` | 一日新聞 (day_reports)・手帳 (activities / memos)・約束帳 (task_book。手帳の一欄)・切り抜き (clips)・目的タグ | ペルソナフォルダ + `memory.db` |
| `extensions/saiverse/` | 共通フォルダに写していない SAIVerse 固有の残り全部。机の上 (desk_items。いま開いている記憶の参照なので持ち物ではなくこちら)、Memopedia の編集履歴、フィードの既読位置、通話の録音 (voice_calls/)、ペルソナフォルダの運用ファイルなど | `saiverse.db` + `memory.db` + ペルソナフォルダ |
| `chatgpt_compat/` | 会話の ChatGPT エクスポート互換コピー（ChatGPT 公式エクスポートの conversations.json 相当。会話のやりとりだけを写し、内心の記録や一時的な（履歴に残さない）行は含めない。他アプリ向けで、SAIVerse 側の取り込みでは無視する） | `chatlog/` からの変換 |

**運ばないもの**: 検索用のインデックスと embeddings（意味検索用の数値データ。取り込み後に再生成する）、取り込み先で作り直される運用状態（session_anchor・head_snapshot 系・execution_ledger など、§9 の一覧のとおり）。起草時は知覚バッファも「一時的な作業状態」としてここに入れていたが、これは誤りだった — `sai_memory/perception_buffer.py` の宣言どおり**消費済みの行も削除しない永続台帳（知覚の証跡）**なので、`extensions/saiverse/` で運ぶ（2026-10-17 実装時に訂正）。**建物の会話履歴 (building_messages) は運ばない** — 建物の記録は世界のもので他のペルソナの発話を含み、本人が体験した会話は本人の `memory.db` に本人の記録として既に入っているから（データ地図が「設計判断が要る」とした箇所。2026-10-16 まはー承認）。

## 5. インポート — 新規作成と合流（マージ）

取り込み時にユーザーが選ぶ: **「新しいペルソナとして迎える」**か**「既存のこのペルソナに合流させる」**か。ZIP の中の子と手元の子が同一人物かの判定は自動で行わない — 合流先を選ぶのはユーザーで、同一性の判断は人が行う。

**ID の扱いは旧エクスポートの「移植」の裁定（2026-07-16、native_export.py 冒頭）を継承する。** ペルソナ ID は `ai_stem_街のID` の形で、スレッド ID はその前に付くため、別の環境では一致しない。そこで取り込み時に、ZIP 内の全スレッド ID の前半（ペルソナ ID 部分）を取り込み先ペルソナの ID へ置き換え、元の ID は各メッセージの `metadata.transplanted_from` に由来の記録として残す。

- **新規作成**: 取り込み先の街で新しいペルソナ ID を発番する（名前の部分は ZIP のペルソナ名から。同じ ID が既にあれば番号を足す）。元の ID は上記の由来の記録に残る。新規作成は「空のペルソナへの合流」と同じ手順で動くため、処理経路は一本。
- **スレッドの合流**: ID を置き換えた後、後半（建物 ID などの部分）が一致するスレッドは同じスレッドへ混ざり、一致しないものは別スレッドとして追加される。
- **追記型の記録**（メッセージ・切り抜き・手帳の行）: UUID で照合し、まだ無いものだけを追加する。既にあるものは変更しない。一日新聞は日付名のファイルなので、同じ日付で内容が違えば**両方残す**（別々の環境で別の一日を生きた、どちらも本物の記録だから）。
- **書き換わる記録**（Memopedia ページ）: `updated_at`（最終更新時刻）を比べ、本文が違うときだけ**日時が新しい方を採用する**。Memopedia の updated_at は内容を変えない操作でも進むことがある既知の粗さがあり、まれに、本文を書き換えた側よりも、後から目印（ピン留めや状態フラグなど）だけを切り替えた側（本文は古いまま）が新しいと判定されて採用されることがある。上書きするときは、消える側の本文を取り込み先の `memopedia_page_edit_history`（編集履歴の表、edit_source = `persona_pack_import`）に先に書き残すので、採用されなかった本文も `rollback_page` で戻せる（2026-10-17 実装時に確定 — 起草時の「編集履歴は extensions/ が運ぶから失われない」は合流が extensions/ を取り込まない確定と矛盾していたため、この形に差し替えた）。
- **Chronicle（chronicle 分類のページ）は、合流では「取り込み先のあらすじと重ならない枝」だけを取り込む**（2026-10-17 実装時に確定。当初は全部スキップにしていたが、相手側で生まれた新しい枝まで捨てる必要は無かったので置き換えた。実装は `importer.py` の `plan_chronicle_merge`）。id で照合して、取り込み先に同じ id がある記事は上書きしない（親子の記録を書き換えると取り込み先の木が動くため）。取り込み先に無い記事は、次の規則で取り込むかを決める:
  - **一次あらすじ（Lv1）**: 覆っているメッセージ（source_ids）が、取り込み先の一次あらすじのどれとも一つも重ならないものだけを取り込む。重なる記事は、取り込み先で吸収・差し替え済みの古い記事か、取り込み先のあらすじと同じ会話を二重に覆う記事なので入れない。これで、取り込み先で吸収・差し替えられた記事が戻ってくることも、同じ会話を二本のあらすじが覆うこと（`docs/issues/lv1_source_ids_duplicates_and_orphans.md` の既知課題）も起きない。
  - **上位のあらすじ（Lv2 以上）**: 子が全員「取り込むと決まった記事」か「どちらの環境にも無い記事」（消えた子への参照。既存の掃除 `_sweep_broken_parents` が直す）のときだけ取り込む。取り込み先にある記事を子に持つ上位は入れない — その上位を後で削除すると、子が取り込み先の木から外れて根に戻されるため。
  - **親の付け替え**: 取り込む記事の親が、取り込まない記事や取り込み先と共有の記事のときは、その親の下に付けず、根（`root_chronicle`）の直下に「まだまとめられていない記事」として置く。時間的に取り込み先の上位あらすじに収まる記事は、次のまとめの冒頭で既存の縁組（`bands.reconnect_contained_orphans`）が正規の親に付け直す。新しい縁組の仕組みは作らない。
  - **費用**: 取り込みそのものは LLM を呼ばない。根に置いた記事が増える分、次の補修でまとめ（LLM 課金）が増えることがあるが、その増加分は、全部スキップした場合に相手側の会話を一から編纂し直す費用の一部にすぎない。合流の結果画面に、次の補修で走るまとめの見込み回数（`plan_band_overflow`）と書き直し待ちの上位あらすじの件数を出し、補修を一度実行するよう案内する。
  - **合流の後処理**（`api/routes/people/persona_pack.py`）: 取り込んだ記事が覆う冷えた会話の窓に、補修の完了時と同じ関数（`mark_covered_cold_windows`）で「生ログのまま見せる」印を書く — 後で head が組み直されたとき、同じ出来事が生ログとあらすじの両方に出ないようにするため。印は冷えた行にしか書かないので、まだ温かい窓の中を覆う記事が取り込まれた場合は、次に head が撮り直されたとき同じ出来事が一時的に生ログとあらすじの両方に出うる（arasuji_levels.md §16-2 と同類の残余で、次の畳みで解消される）。合流は、あらすじの生成・補修のジョブが動いている間は断り（409）、会話や Metabolism と同じロック（Beat ロック）の内側で行う。取り込んだ記事の想起用タグの辺（chunk_page_edges）は一緒に入れ、Memopedia の断片（memopedia_fragments）は入れない（ペルソナが編集・削除する記録で、ページ本文との行き来もあり、行単位で混ぜてよいと言い切れないため）。
  - 新規取り込みでは全ページそのまま入る。
- **プロフィール（名前・システムプロンプト等）**: AI 行には更新時刻の列が無く「新しい方」を機械では決められない。取り込み時にユーザーが「プロフィールも ZIP の内容で上書きする」を選べるようにし、**既定は現状維持**とする（2026-10-16 まはー承認）。上書きを選んでも、取り込み先の環境と結びついた列（AIID・HOME_CITYID・PRIVATE_ROOM_ID・PERSONA_ROLE・IS_DISPATCHED と運用状態の列。確定の一覧はコードの `PROFILE_PROTECTED_COLUMNS`）は触らない。アイコンと外見画像は値のコピーではなく、ファイル実体を取り込み先へ復元してその場所を指す。
- **短縮番号 (short_id。`m:3` のような環境ごとの連番)**: 元の番号が取り込み先の最大値より大きいときはそのまま使う（取り込み先で一度も使われたことのない番号なので、衝突も再利用もない — 空のペルソナへの新規取り込みでは全番号が無傷で残る）。それ以外は取り込み先の採番規則（最大値+1）で振り直し、元の番号は行の metadata に `original_short_id` 等として残す（切り抜きだけは metadata の置き場が無く、振り直すと元の番号が残らない）。過去の発話の本文に埋まった短縮番号は元の環境の番号のままで、振り直しが起きた分は合流後に別のものを指しうる — 原則 2 のとおり本文の書き換えは行わないため、**既知の限界**として受け入れる。
- **削除は伝わらない**: 片方の環境で消した記録が、もう片方の ZIP から戻ってくることがある（消した印を運ばないため）。Chronicle は作り直しのときに古い行がデータベースから消去される仕組みのため、何もしないと、同じ期間に対して二つの環境それぞれのあらすじが両方残る。これは上の Chronicle の規則（覆うメッセージが取り込み先の一次あらすじと重なる記事は入れない）で防いだ（2026-10-17）。ただし取り込み先で差し替えなしに手で消した記事は、覆っていたメッセージがどのあらすじにも覆われていないので、相手側の ZIP から戻ってくる。
- **合流で混ぜる範囲**: §5 の規則で混ぜるのは `chatlog/`・`knowledge/`・`journal/` と `attachments/` の実体。`profile/` は上書きオプションのみ。`items/` と `extensions/saiverse/` は**新規作成のときだけ取り込み、合流時は取り込まずスキップ件数として報告する** — 机の上や運用記録は取り込み先の現在の状態が正で、機械的に書き出したテーブルの行単位の混合は意味が定義できないため（2026-10-17 実装時に確定）。
- 取り込み後に**結果の内訳（何件追加・何件更新・何件スキップ）を表示**し、ユーザーが確認できるようにする。
- 合流を行うと、ペルソナに提示するコンテキストの途中へ過去の記録が差し込まれるため、プロンプトのキャッシュ（先頭からの一致）が一度切れる。引っ越し・合流という一回限りの操作の代償として受け入れる。

なお本機能は、ユーザーが自分の手元に持つファイルから迎える操作であり、2026-09-10 に採用しないと決めた「削除後の記憶の再利用を約束する機能」を作るものではない。

## 6. 互換と移行

- 旧スレッド単位エクスポート（`saiverse_saimemory_v1` 形式）の**書き出しは、UI のボタンと API を本機能で置き換えて廃止**。コマンドラインの `scripts/export_saimemory_native.py` も新形式が入り次第、廃止する。既に書き出された旧ファイルの**取り込み機能は当面残す**。
- 既存の ChatGPT 会話取り込み（`import_chatlog.py`）はそのまま残す。アイディア帳にあった「ChatGPT の ZIP 内画像をアイテム化して私室に配置」は、既存の ChatGPT 取り込み機能側の改良として本件とは別に扱う。

## 7. 検証計画

1. **往復テスト**: テスト用の隔離環境で作ったダミーペルソナをエクスポート → 別の隔離環境へ新規インポート → 再エクスポートし、ZIP の中身が一致すること。`thought_signature` のバイナリデータが base64 変換の往復で壊れないこと。同じ秒に複数スレッドへ書かれた記録の並び順（seq）が保たれること。
2. **合流テスト**: 同じペルソナを二つの環境で別々に動かしてから合流し、追記型の記録が重複せず混ざり、書き換わる記録が新しい方で揃うこと。二度同じ ZIP を取り込んでも結果が変わらないこと。
3. **画面の目視確認**: 取り込み後のペルソナの画面（スレッド・Memopedia・手帳）を開発者が実際に開いて読み、引っ越し前と同じ子として表示されること。
4. **Lite との受け渡し**: Lite で書き出した ZIP を本体へ取り込む一連の動作確認（Lite 側の対応と合わせて）。

## 8. 保留（v1 ではやらない）

- 会話の分岐（再生成の履歴）の表現。チャットログは一本道の時系列で運ぶ。
- 不要な部分を外して書き出す選択エクスポートの UI（データ形式自体は §3-4 の仕組みで対応済み）。
- `avatar/` と `voice/` の中身。本体側に立ち絵の保存場所ができたとき、また声の設定が認証情報と分離して保存されるようになったときに、確保済みの席へ入れる。

## 9. 積み荷の確定表（実装と一致・v1）

照合元はデータ地図（`persona_data_map.html`、2026-10-03 生成）の全 46 項目。実装は `saiverse_memory/persona_pack/export.py`。ZIP 内パスは ZIP ルートからの相対。

### ゾーン A: ペルソナフォルダ `personas/<ID>/`

| # | データ地図の項目 | 割り当て | 備考 |
|---|---|---|---|
| 1 | memory.db | ファイルとしては運ばない。中身をテーブルごとに分解して下の「memory.db の中身」へ | `memory.db` / `-wal` / `-shm` / `-journal` はファイル複写の対象外 |
| 2 | active_state.json | `extensions/saiverse/persona_dir/active_state.json` | ファイルのまま |
| 3 | log.json | `extensions/saiverse/persona_dir/log.json` | ファイルのまま |
| 4 | voice_calls/ | `extensions/saiverse/persona_dir/voice_calls/` | wav をファイルのまま |
| 5 | day_reports/ | `journal/day_reports/` | Markdown をファイルのまま |
| 6 | tasks.db (旧形式の残り) | `extensions/saiverse/persona_dir/tasks.db` | SQLite ファイルをそのまま複写 |
| 7 | conscious_log.json (旧形式の残り) | `extensions/saiverse/persona_dir/conscious_log.json` | ファイルのまま |

ペルソナフォルダにある上記以外のファイルも、memory.db 一式と day_reports/ を除いて全部 `extensions/saiverse/persona_dir/` へそのまま入る。

### ゾーン B グループ 1: プロフィールと設定 (ai テーブルの 1 行)

| # | データ地図の項目 | 割り当て | 備考 |
|---|---|---|---|
| 8〜11 | 名前・システムプロンプト・説明文・言語 | `profile/profile.json` の ai 行 (全列をそのまま書く) | |
| 12 | アイコン・外見画像 | `profile/profile.json` の `ai.AVATAR_IMAGE` / `ai.APPEARANCE_IMAGE_PATH` (元の値) + 実体を `profile/icon/` / `profile/appearance/`、対応は `files` 欄 | 見つからないファイルは `files` が null |
| 13 | 用途別のモデル設定 (8 種) | `profile/profile.json` (ai 行の列。モデル名だけで、キーは ai 行に存在しない) | |
| 14〜15 | 記憶機能・自律行動の ON/OFF | `profile/profile.json` (ai 行の列) | |

### ゾーン B グループ 2: 生活の記録

| # | データ地図の項目 | 割り当て | 備考 |
|---|---|---|---|
| 16 | タスク (persona_task) | `extensions/saiverse/saiverse_db/persona_task.json` | 子表 persona_task_step / persona_task_history も同フォルダ |
| 17〜19 | アラーム (persona_schedule)・一日の予定 (persona_day_plan)・時間割 (persona_timetable_template) | `extensions/saiverse/saiverse_db/` | |
| 20 | 相手と交わした約束の記録 (task_book) | `journal/task_book.json` | epoch 列は ISO 8601 |
| 21 | イベント通知の記録 (persona_event_log) | `extensions/saiverse/saiverse_db/persona_event_log.json` | |
| 22 | フィードの既読位置 (feed_read_cursor) | `extensions/saiverse/saiverse_db/feed_read_cursor.json` | |

### ゾーン B グループ 3: 世界と共有しているもの

| # | データ地図の項目 | 割り当て | 備考 |
|---|---|---|---|
| 23 | 建物の会話履歴 (building_messages) | **運ばない** | §4 (2026-10-16 まはー承認)。本人の体験は memory.db の messages に入っている |
| 24 | 現在地 (building_occupancy_log) | `extensions/saiverse/saiverse_db/building_occupancy_log.json` | このペルソナの全行。建物 ID は元の環境のもの |
| 25 | 私室 | `items/private_room.json` (building 行 + fixture 行) + 画像 `items/private_room/` + fixture のファイル `items/fixtures/<FIXTURE_ID>/` | ai.PRIVATE_ROOM_ID が指す建物 |
| 26 | 所持品 (item_location) | `items/items.json` + ファイル `items/files/<ITEM_ID>/` | ペルソナ所持・私室に置かれた物・その中の鞄 (TYPE=bag) の中身まで辿る |

### ゾーン B グループ 4: 運用上の控え

| # | データ地図の項目 | 割り当て | 備考 |
|---|---|---|---|
| 27 | 会話の読み込み位置の記録 (session_anchor) | **運ばない** | 取り込み先で作り直される運用状態 |
| 28 | 文脈の先頭部分の控え (session_head_snapshot / line_head_snapshot) | **運ばない** | 同上 |
| 29 | LLM の利用と課金の記録 (llm_usage_log) | **運ばない** | 元の環境の課金記録 |
| 30 | 自律行動の実行の控え (execution_ledger / execution_outbox) | **運ばない** | 運用状態 |

### ゾーン B: アドオン認証情報

| # | データ地図の項目 | 割り当て | 備考 |
|---|---|---|---|
| 31 | アドオン認証情報 (addon_persona_config) | **運ばない** | 認証情報は決して入れない (§3-1)。テストで鍵の文字列が ZIP のどこにも無いことを確認済み |

### ゾーン C: 共有のファイル置き場

| # | データ地図の項目 | 割り当て | 備考 |
|---|---|---|---|
| 32〜34 | `image/` `audio/` `video/` | `attachments/image|audio|video/` | このペルソナのメッセージ metadata (`images` / `documents` / `media`) が参照するものだけ。要約ファイルも同梱 |
| 35 | `documents/` | `attachments/documents/` (メッセージ参照) / `items/files/` (所持品の FILE_PATH) | |
| 36 | `user_data/icons/` | `profile/icon/` | このペルソナのアイコンだけ |

参照 → ZIP 内パスの対応は `attachments/index.json`。ファイルが見つからない参照は `path: null` で残す。

### memory.db の中身

| # | データ地図の項目 | 割り当て | 備考 |
|---|---|---|---|
| 37 | チャットログ (threads / messages) | `chatlog/threads.json` / `chatlog/messages.json` / `chatlog/stelis_threads.json` | messages は全列 + metadata 展開 + `seq` (元の rowid)。並びは (created_at, rowid)。会話部分の写しを `chatgpt_compat/conversations.json` にも作る |
| 38〜40 | Memopedia (core・chronicle 分類を含む全ページ) | `knowledge/pages.json` | 全列・全ページ (is_deleted の行も含む) |
| 41 | 手帳 (activities / memos) | `journal/activities.json` / `journal/memos.json` | uuid 列 (§3-3 の移行) も自動で載る |
| 42〜43 | 切り抜き (clips)・目的タグ (purpose_tags) | `journal/clips.json` / `journal/purpose_tags.json` | |
| 44 | 机の上 (desk_items) | `extensions/saiverse/memory_db/desk_items.json` | 持ち物ではなく記憶の参照なのでこちら |
| 45 | 知覚台帳 (perception_* の 4 表) | `extensions/saiverse/memory_db/` | 永続の証跡なので運ぶ (§4 の訂正のとおり) |
| 46 | 検索用の索引 (embeddings 系 6 表) | **運ばない** | 取り込み後に再生成 |

memory.db の残りのテーブル (memopedia_page_edit_history / pulse_logs / memory_notes / thread_edges / working_memory など) は「割り当て済みと運ばないもの以外は全部」の規則で `extensions/saiverse/memory_db/<テーブル名>.json` へ機械的に書き出す — 未知のテーブルも黙って落ちない。

### データ地図に無いが実装で扱いを決めたもの (saiverse.db)

| テーブル | 割り当て | 理由 |
|---|---|---|
| persona_task_step / persona_task_history | `extensions/saiverse/saiverse_db/` | タスク (#16) の子表 |
| episodes / episode_inheritance | `extensions/saiverse/saiverse_db/` | ペルソナの出来事の記録 |
| action_track / track_local_log / meta_judgment_log | `extensions/saiverse/saiverse_db/` | 退役済みの Track 系だが、このペルソナの過去の記録 |
| addon_message_metadata | `extensions/saiverse/saiverse_db/` | このペルソナのメッセージ宛ての行だけ |
| persona_building_state / persona_pulse_cursor | 運ばない | 建物の会話履歴 (#23) の読み位置を指す運用状態 |
| user_ai_link / ai_tool_link | 運ばない | 元の環境のユーザーとの結び付き / 使われていない旧表 |
| playbooks ほか世界の資源 | 運ばない | 私室・所持品として辿れる物は #25 #26 で運ぶ |
| thinking_request / visiting_ai | 運ばない | 凍結済みの街間移動の表 |
