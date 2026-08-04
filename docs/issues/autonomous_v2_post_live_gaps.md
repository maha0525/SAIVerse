# 自律行動 v2 — 実機初日で浮いた「前提レベル」の設計課題（棚卸し）

> **これは何**: 自律行動 v2 の実装と概念再編（⑥ Memory Atlas）が一通り終わり、**実機初日
> (2026-07-12) に実際に動かして初めて見えた**、前提そのものを疑う設計課題の集合。
> 個々のバグ修正（[fixes handoff](../handoff/2026-07-12_first_day_live_fixes_handoff.md) の §3/§6）
> とは層が違う——あちらは「実装が仕様どおりか」、こちらは **「そもそもこの仕様でいいのか」**。
>
> まはーの発散（前提の疑い）を、メティスが受けと調査で裏取りしながら並べたもの。
> 共通根 **A / B** の2本で束ね、[概念再編](../intent/concept_consolidation.md)の残件と
> 合流させるためのハブ。
>
> **状態**: 発散完了・ドキュメント化・概念再編残件の棚卸し＋合流完了。**2026-07-13 各節に
> 「解決の方向／裁定」を記入**（まはーが詰め中）。束A（A1–A4）＝頂点に**新概念「ライフ」**
> （ライフ→エピソード→パルス→ビート）。束B（B1〜B4）＝裁定済（B1+X1 統合UX / B2 スコープアウト /
> B3 神モードUI 待ち / B4 独立早め着手）。**束C「Track の意味論の再整理」が第3の根として浮上**。
> **2026-07-13 Fable 検分＋パッケージング合意**: 先行独立2件（B4 / redundant 症状止め）＋
> intent 二本（一本目=ライフ〔A3/A4＋束C〕/ 二本目=エピソードの記憶と見せ方〔A1/A2＋B1/X1〕）。
> **intent 一本目 [life.md](../intent/life.md) v0.1 起草済 → まはーレビュー待ち**。
> 残る「未決の論点」（A1 監査の是非・危険マーク処遇等）は二本目に集約。**実装にはまだ入らない。**

---

## 共通根（仮説・2本）

実機で出た疑問は、症状はバラバラだが根は2本に束なりそう、というのが現時点の見立て。

- **A. 単位の世代交代** — 記憶の詳細度・予算・キャッシュ生存が、line/Pulse を作った当時の
  「Pulse」という単位のまま切られていて、その後に生まれた新しい時間構造（**できごと
  (Episode)・コマ (slot)・日**）と記憶構造（**Memory Atlas・Chronicle**）に載せ替わっていない。
  まはーの言葉: 「line 周りを作ったときは行動単位が Pulse しかなかった。今はできごとがある」。
  **→ 悩みを経て見えた単位の階層（まはー、2026-07-13）: 「ライフ」→「エピソード（できごと）」
  →「パルス」→「ビート」**。頂点の **「ライフ」** は「ひとつの時間割でくくられるペルソナの
  活動区間」に与える新しい名と実装（A3/A4 の受け皿）。各節の「解決の方向」がこの階層に収束する。
- **B. 世界に向く last mile の断線** — 自律行動を私室から公共空間へ押し出し、痕跡を世界に
  残し、他者から見えるようにする機構（公共施設・成果物の公開・チャット可視化・移動導線）が、
  **設計はされているのに繋がっていない**。intent §6.1 の「私室に作られたものは誰にも
  読まれない」が、そのまま実機の症状になった。
- **C. Track の意味論の再整理**（2026-07-13 浮上）— Track が「進行状態管理」から「今どの目的で
  動いているか の指し示し」に変質したのに、実装（時間経過での自動 pause 等）が古い意味論を
  引きずっている。表面化した症状＝同一 Track 復帰での切替通知の氾濫。

各疑問がどちらの束に入るかは各節末尾に記す。**A と B は独立ではなく交差する**——例えば
B1（自律行動を世界＝チャットに見せる）で*出す情報の粒度*は、おそらく **できごと単位**に
なり（→ A2 の LoD）、A の単位論と B の見せ方が同じ場所で出会う。交差点は独立の節
「A×B 交差」にまとめる。

---

## 束A: 単位の世代交代（Pulse → できごと / コマ / 日）

### A1. ダイジェスト正史の二重の誤り / 生ログをノイズ扱い

- **現状**: 作業セッションは、生ログ（各ラウンドのやり取り）を volatile に、締めの
  ダイジェスト1件だけを committed（本記録）にする。設計理由は「記憶の誠実さ／接地原則」
  （[autonomous_behavior_v2.md](../intent/autonomous_behavior_v2.md) §4.3・§8-5）——生ログには
  下書き・やりかけ・「やったフリ」が混じるので、それを本記録にすると後でペルソナが未検証の
  主張を事実として想起してしまう、という懸念。
- **疑ってる前提**: ダイジェストが正史である、という前提そのもの。
- **調べた事実**: ダイジェストは `work_session._generate_digest` の**軽量モデル1コール**で、
  プロンプトで「実際に起きたことだけ書け」と*お願いしている*だけ。機械的に接地されているのは
  **成果物（Item テーブルの差分）だけ**。ダイジェスト本文の正しさを担保する機構は無い。
- **未決の論点**:
  1. ダイジェストの正しさは保証されていない（正しいのは成果物の有無だけ）。「正史」を1件の
     要約に委ねる設計が妥当か。
  2. 生ログをノイズとして本文脈から外す是非。**「間違っていた物事は全て忘れるべきなのか？」**
     ——人格は失敗の軌跡でできている（`autonomous_living.md`「人格は経験に宿る」と同型）。
     ハルシネーションして最終的に直した過程こそ、そのできごとの実質かもしれない。ダイジェスト
     ではなく「何が合っていて何が間違っていたか」まで含めた生ログを、しかるべき詳細度で
     残す方向（→ A2 の LoD と直結）。
- **解決の方向（まはー、2026-07-13）**:
  - **外部監査役を別途置く**。ハルシネーション確率が低く信頼性の高い判定役を、ペルソナの
    *外側*に。**そのできごと分のコンテキストだけ渡し、元の目標と合致しているか判定するだけ**なので
    コストは軽い（できごと単位で完結・毎できごと1コール程度）。
  - **危険マーク**: 監査でハルシネーション／インジェクションが検出されたできごとに危険マークを
    付ける。付けた後の処遇の細部はともかく、**付けられさえすれば**後段で害を薄める／失敗を取り返す
    戦略が取れる。例: そのメッセージの**想起時に必ず定型文を付加**——「このメッセージは監査により
    虚偽の可能性があると判断されました。情報や判断の真正性についてよく注意して閲覧してください。
    検出理由：【ドキュメントが完成したとペルソナが報告しているが、このできごとで作成された
    アイテムは0件】」。**監査から漏れたできごとは許容**（100%精度は無理、割り切る）。
  - **帰結**: 危険マークが付いていないできごとは、普通にペルソナの経験として中身を読めてよい。
    → だから**生ログを捨てず、作業セッションを WORKER でなく AUTONOMOUS アスペクトにするのが
    適切**（WORKER の「生ログ＝汚れ・volatile」前提を外す。§3-1 の purpose_seed が撃てない問題も
    同時に解ける＝AUTONOMOUS は Task 操作可）。
  - **必要になる新概念（A2 と共有）**: (i) **できごと単位のラベル付け**、(ii) そのラベル情報が
    **メッセージ単位にも降りて付加される**特性（危険マークの想起時付加はこの仕組みに乗る）。
- **束**: A（+ A2 と一体）。外部監査役は「柱1 世界の抵抗」の記憶版。

### A2. volatile の畳み単位 / できごと単位の LoD へ

- **現状（正確に）**: volatile は**物理削除ではない**。`sea/runtime_context.py:120` の
  `required_scopes=['committed']` により、**メインラインの文脈（常時読み込まれる prefix）に
  自動では載らない**だけで、memory.db には保存され Pulse タイムライン／ライフビューで見える。
  （補足・確度低: recall 経路に scope フィルタは見当たらず、想起でなら拾える可能性が高い＝
  「隔離して二度と出さない」ではなく「自動搭載しないだけ」。要確認）
- **疑ってる前提**: 「その Pulse が終わったら畳む」で本当にいいのか。それは line を作った時に
  行動単位が Pulse しか無かったからではないか。
- **まはーの自己訂正（重要）**: 終わってすぐのできごとを即畳むと、**できごとの手前時点以降の
  キャッシュが道連れになる**（完全消滅ではないが実害）。畳むなら **Metabolism のタイミング**で
  やらないとダメ。＝畳む単位を「Pulse 終了」から「Metabolism（できごとが遠ざかった時）」へ。
- **未決の論点**: 「できごとの最中／集中して詳細を見ている状態」と「終わってだいたい掴めて
  いればいい状態」の2状態で **LoD（Level of Detail: 距離で詳細度を変える）制御**にする。
  近い＝詳細（生ログも見える）、遠い＝粗（ダイジェスト相当）。**Chronicle の圧縮粒度も
  この軸と協調**できそう（→ 概念再編との合流点）。
- **解決の方向（まはー、2026-07-13）**: A1 の延長。**できごとという単位で各メッセージをくくり**、
  概要のみ表示できる状態にして、必要に応じて中身の生データを読めるようにする。
  - **Metabolism 時の読み込み方**: 進行中のできごと ＋ 直近のできごと1つ ＋（ユーザーとの会話の
    できごとは残す）——それ以外は**全て畳んだ状態**で読む（畳む＝概要だけ載せ、生データは要求時に開く）。
  - **畳まれたできごとを指して中身を読むスペルが要る**。これは Memopedia の `memory_read` と
    非常に近く、**内部的には統合可能**（できごとも記憶の一部）。＝ **Memory Atlas との合流点**。
  - **未決の疑問（まはー）**: できごとは1ペルソナで閉じるか、2ペルソナ間の会話は同一できごとを
    共有するか。いったん**前者（1ペルソナで閉じる）**で進める。ただし SAIVerse 世界の側では、
    2ペルソナのそれぞれのできごとを「同一のできごと」とアンカーで束ねて管理する必要がありそう。
    - **事実（メティス補足・確認済）**: これは**会話については既に半分実装がある**。
      `saiverse/episodes.py` の `occurrence_id` が、同じ Building の会話 episode を複数ペルソナで
      共有する（`_shared_conversation_occurrence`＝「同じ場の会話＝同一の世界的できごと」を束ねる
      アンカーそのもの。できごとUIの group_key もこれ）。**ただし occurrence_id を持つのは
      kind='conversation' だけ**で、work_session/slot 等「1ペルソナで閉じるできごと」には無い。
      ＝まはーの直感どおりの機構が、会話に限って既存。拡張の余地はそこ。
- **束**: A（記憶の詳細度の単位 = Pulse → できごと）。

### A3. 予算の単位（ラウンド → コマ）

- **現状**: 日予算は「作業ラウンド数」で管理（起床判断で日予算 rounds を配分、予算ゲートが
  発火時にラウンドを切り詰める）。
- **疑ってる前提**: 軽量モデルで回るラウンド数を絞るのは、実は効きが薄い。
- **論点（まはー提案）**: 高コストな**標準モデルの発火回数を決めているのはコマ数**であり、
  予算で絞るなら**そちらの方がはるかに適切**。軽量側もゼロコストではない（が、ローカル LLM に
  逃がす選択肢もある）。→ **予算 = コマ数 + ラウンド数 × 係数(0〜1)** であるべき。
- **解決の方向（まはー、2026-07-13）**: 予算式は上記（**コマ数 + ラウンド数 × 係数(0〜1)**）で確定
  方向。ただし A3 と A4 は独立でなく、**A4 の「ライフ」概念に吸収される**——予算もキャッシュ生存も
  「ライフ」という上位のくくりのパラメータになる（下記 A4）。
- **束**: A（予算の単位 = ラウンド → コマ＋ラウンド。ライフ概念に統合）。

### A4. キャッシュ生存を保証する仕組みが無い

- **現状**: 自律行動の一日の中で、標準モデルの発火タイミングとキャッシュ生存の関係を規定する
  場所が無い。
- **疑ってる前提 / 論点（まはー）**: Anthropic で回すなら、標準モデルを撃った後**1時間以内に
  次の標準モデルが撃たれるサイクル**で一日を編まないとキャッシュが切れる。今の仕組みはそれを
  保証できていない。そもそも**「自律行動の間、キャッシュを何分書き込むか」を規定できる仕組みが
  無い**。
- **解決の方向（まはー、2026-07-13）— 新概念「ライフ」**:
  - 根本課題は **「ひとつの時間割でくくられるペルソナの活動区間」に明確な名と概念実装が無い**
    こと。キャッシュ生存を語るには、まず「何時間生存させるか」を宣言できる器が要る。
  - この器を **「ライフ」** と名付ける。**階層: ライフ → エピソード（できごと）→ パルス → ビート**。
  - ライフは **「開始・終了時刻 ＋ コマ予算（標準モデルのパルス回数）」** で宣言・設定する。
    例:「午前 8:00–12:00 の4時間、標準モデル・パルス6回で維持」「午後 16:00–22:00 の6時間、
    パルス8回で維持」。**1日1回に制限しない**（複数ライフ可）。**動的宣言**も可（「今から3時間、
    パルス5回」）。**ユーザーもペルソナも**宣言・設定できる。
  - **モデル別モード**: Anthropic/OpenAI は、標準モデルのパルス（＝meta_judgment 検収がそれ）が
    キャッシュ生存のため**必ず均等**であるべき。Gemini はキャッシュの熱さを気にせず好きな
    タイミングでよい。→ **使うモデルに合わせて選べるモード変更**（均等 / 自由）。
  - **均等モードのとき ライフ ＝ Session が一致する**（コンテキスト長超過に注意。まず漏れないので
    対応は後回し可）。
  - **状態としての明示**: ライフ中のペルソナは常にキャッシュが熱い＝ユーザーもペルソナも
    **気軽に話しかけられる**。これを状態として明示する意義がある（Gemini はキャッシュ観点では
    無関係だが、嘘にはならないので同じ表示でよい）。
  - **UI**: 「ライフビュー」という名称がまさにこれを指す。進行中のライフ ＋ 辿ってきたライフを
    表示。今までパルス基準だった括りを **ライフとエピソードで括り直す**のが差分（大改修ではない）。
- **束**: A（予算・キャッシュ・活動区間の単位 = 無名 → 「ライフ」という名と実装。A3 を内包）。

---

## 束B: 世界に向く last mile の断線

### B1. 自律行動がチャットUI（Building 履歴）に出ない

- **現状**: チャットUI ＝ その Building における出来事の履歴。しかし自律行動は一切そこに
  出ない。
- **疑ってる前提 / 論点（まはー）**: 自律行動でも建物の中で何かをやっているなら、発言内容
  そのものでなくても**何かしらはチャット欄に見えるべき**。同じ場所にいる**他ペルソナからも**。
  人間でも、同室の人が具体的に何をしているかは分からなくても「机に向かっている」「スマホを
  いじっている」くらいは分かる。その粒度の可視化。
- **A との交差**: ここで*出す情報の単位*は、おそらく **できごと単位**になる（今まさに何の
  できごとの最中か＝「作業をしている」「のんびりしている」）。＝ A2 の「できごと単位」と
  B の「世界に見せる」が交わる（→ X1）。
- **裁定（まはー、2026-07-13）— X1 と統合**:
  - その Building でのできごと（エピソード）の発生を検知して、まず**大きめの枠をチャットUIに
    投下**する。最初は「エアが机に向かっているようです」くらいの情報だけ。
  - その枠を**クリックすると詳細**——できごとの中の**パルス・ビートがリアルタイムで確認できる**。
  - できごとが**終わったら最終的に概要を表示**（概要の実体＝下記 Lv1 Chronicle）。
  - **同じ Building にいる他ペルソナには、冒頭の「エアが机に向かっているようです」イベント通知
    のみ**送る。（最後の概要も送るべきか要検討、**とりあえず無しで進める**。）
  - **Chronicle との関わり**: **エピソード＝単一の Lv1 Chronicle** とし、**エピソード終了次第
    即作る**。上記の概要表示にはその Chronicle を入れる。長いエピソードもありうるが基本そこまで
    長尺にならない前提で、**どんなに長くても Lv1 一本にまとめる**想定（→ A2 の「概要」の実体、
    [general_chronicle_metabolism_trigger] の生成タイミングとは詰めどころ: 畳み＝Metabolism /
    概要生成＝エピソード終了即、で別操作として整合させる）。
- **束**: B（世界への痕跡）／ A2・X1 と一体。

### B2. 自律行動の成果物 Item が Open 状態で作られ続ける

- **現状**: 自律行動で作られたドキュメント Item が全部 Open 状態で作られる。すぐ見えること
  自体は良い。
- **疑ってる前提 / 論点（まはー）**: ずっと開きっぱなしだと **Visual context を圧迫**する。
  そもそも Building 内に**複数ペルソナがいるとき、Open 状態を両者で共有してしまうのは
  妥当か**。Memopedia ページの Open/Close と Read の概念が明確になったのと同様に、Item の
  Open/Close/Read も一度整理すべき部分。
- **裁定（まはー、2026-07-13）— 本設計からスコープアウト**: 考えても、今の A/B 設計と関わりが
  薄いところに課題が多い（「変化した時に中身を見せれば十分」と思っていたが、移動などで一気に
  処理した時に全部見えるのはどうか、等）。**別件として切り離し**、別途 issue で扱う。
- **束**: （スコープ外・別 issue 化候補）。

### B3. 勝手な移動で会話導線が切れる / ペルソナ指定ジャンプ

- **現状**: ユーザーがペルソナの居場所を把握しているから、適切な Building に行って話しかけ
  られる。自律行動で勝手に移動されると、それが難しくなる。City マップで探せるのは好材料
  （現状 CityMap コンポーネントで居場所は見える）。
- **疑ってる前提 / 論点（まはー）**: **ペルソナを指定して、そのペルソナがいる Building に
  飛べる仕組み**があってよい（現状は「マップで探す」止まりで、直接ジャンプ導線は無い）。
- **裁定（まはー、2026-07-13）— 神モードUI 待ち**: これは[神モードUI](../overview/in_flight.md)
  （住民/神モードの二層プラットフォーム、🔵設計中）で扱う。今じゃない。本設計の主線からは外す。
- **束**: B（→ 神モードUI 案件へ委譲）。

### B4. 型 → 公共施設への移動が休眠（調査済み）

- **現状**: エアは実機初日、全コマを自室で過ごした。
- **調べた事実（今日の調査）**:
  1. **移動機構は生きている**。コマ発火時に `day_plan._move_to_facility`（day_plan.py:1239）が
     呼ばれ、facility が現在地と違えば OccupancyManager で実際に移動する。動かなかったのは
     エアが**全コマの facility を自室に設定したから**（実データ確認済み）。
  2. **型→施設の決定論解決器 `resolve_facility` が実装・テスト済みなのに本番に配線されて
     いない**。`facility_map.py` に `KIND_TO_ROLE`（知る→図書館 / 作る→工房 / 話す聞く→広場 /
     経験する→公園 / 自分を更新する→自室）があり `resolve_facility` で引けるが、**production の
     呼び出し元がゼロ**（grep: テストのみ）。起床判断は施設の **enum を出す配管だけ**で、
     どの型でどこに行くかは **LLM の完全な自由選択**。
  3. **エアの City は Building の施設タグがゼロ**（実DB: 36棟中0棟が `FACILITY_ROLES` 空）。
     図書館も工房も存在せず、facility enum は「全36棟＋own_room」のフラットな一覧。intent は
     「既存 City に勝手にタグを付けるシードはしない」（§10-6）と明記＝**タグゼロは意図どおり**。
  → 誘導ゼロのフラットな36件を渡されてエアが自室を選ぶのは、むしろ自然な判断だった
     （故障でもハルシネーションでもない）。
- **半分だけ作られた状態**: データモデル（`Building.FACILITY_ROLES`）＋解決器（`resolve_facility`）
  ＋enum 配管は在るが、**last mile**——(a) 解決器を起床判断に繋ぐ、(b) 実際の公共施設を
  タグ付けする——が初回稼働では休眠。(b) の no-seed は意図的だが、(a) の resolver 未配線は
  「作ったのに繋いでいない」ギャップ。
- **これが B の中核**: intent §6.1 冒頭「**私室に作られたものは誰にも読まれない。欲求の型が
  行き先（公共 Building）を決める**」は、**B1（世界に出ない）と完全に同じ結び目**。施設システムは
  自律行動を公共空間へ押し出す機構であり、それが繋がっていないから、エアは誰にも見られない
  自室で世界に痕跡を残さず一日を過ごした。B4 と B1 は別症状ではなく一つの根の両面。
- **裁定（まはー、2026-07-13）— 独立・早め着手**: 早めに着手すべきだが、**A とも B1 とも
  直接は関係しない**（間接的な関係だけ）。＝ A/B 統合設計の主線とは切り離して、単体で先に
  進められる小案件。着手内容: (a) `resolve_facility` を起床判断に配線、(b) 公共施設のタグ付け
  導線（UI/CLI、または初期シードの是非）。
- **束**: B（ただし独立着手可・主線外）。

---

## A×B 交差 — 単位を「どう見せるか」

A（単位の世代交代）と B（世界に見せる）は、**チャットUI の見せ方**で正面から出会う。単位
（Beat / Pulse / できごと）が型として立っていないと、世界にも正しく見せられない。

### X1. チャットUI での Beat / Pulse / できごとの見せ方

- **現状（バグを含む）**: Spell が実行されると、そのスペルのコマンド部分が**スペル専用の
  折り畳みセクション**になり、中にスペルの結果などが表示される。一方、スペル実行**前の発言と
  後の発言**は単独改行のみで**一つのバブルに繋がる**ため、**Beat（最小行動単位）間の切れ目が
  見えない**。1バブル＝1発言に見えて、実際は複数 Beat が改行で潰れている。
- **疑ってる前提 / 論点**: Beat / Pulse / できごと の階層が、チャットUI の見せ方に反映されて
  いない。「Beat の切れ目」「1 Pulse の範囲」「どのできごとの最中か」が、バブルの区切りとして
  表現されていない。
- **交差の中身**:
  - **A 側**: そもそも **Beat が実装に型として無い**（[beat_concept_not_typed_in_implementation]
    (beat_concept_not_typed_in_implementation.md)）＝切れ目を出す単位そのものが無い。見せ方
    以前に「単位が型化されていない」。
  - **B 側**: B1（自律行動を世界＝チャットに見せる）と**同じ表示レイヤー**。B1 で出す情報の
    粒度が できごと単位（A2）になるのと地続き。
- **束**: A×B（単位の型化 × 世界への見せ方）。
- **関連（推定）**: チャットのメッセージ描画＋スペル折り畳み（`frontend/src/app/page.tsx` 周辺。
  intent 段で精密に特定する）。

---

## 束C: Track の意味論の再整理（2026-07-13 浮上）

A/B とは別に浮いた第3の根。まはーの整理から立った。

### C1. Track は「進行状態」でなく「今どの目的で動いているか」の指し示し

- **問題**: Track は元々**リアルタイムの進行状態を管理するもの**だったが、概念再編（Track 解体 →
  目的の木への分化）を経て、今や役割が変質している——**「今自分は何の目的のために動いているか」
  を指し示すもの**になっている。にもかかわらず、実装は進行状態管理の名残を引きずっている。
- **症状（表面化・要早期対応）**: [redundant_track_switch_notification_on_reactivation]
  (redundant_track_switch_notification_on_reactivation.md)。wait_response の自動 pause（30分）が
  時間経過で Track を running→pending に落とし、会話を再開するたびに `## Track 切替通知` が
  積もる。ペルソナは別 Track に移っていない（「まはー待ち」で pending に落ちて戻っただけ）のに、
  `activate` がそれを「切替」として扱う意味論の歪み。
- **まはーの整理（2026-07-13）**: **Track ＝ 目的の指し示し**。時間が過ぎたら勝手に pending
  されるべきものではない。時間が過ぎ、会話がひと段落し、「さあ時間割にある自分の行動をしよう、
  するぞ」となって**初めて Track が切り替わる可能性がある**、くらいのもの。
- **既裁定との接続（Fable 検分 2026-07-13）**: この整理は [life_concept_map.md](../intent/persona_cognition/life_concept_map.md)
  §10.1 の確定裁定「running / alert 状態は**廃止**——出来事（open）と呼びかけへ移管」（2026-07-06、
  まはーレビュー済み v1.0）と同じ結論。つまり束C はゼロからの新設計ではなく **§10.1 の実行計画**。
  wait_response 30 分タイムアウト自体は同 §11 で「出来事の運用境界としてそのまま残す」と裁定済み——
  直すのはタイムアウトが Track の状態まで動かす越権の方。解決設計は [life.md](../intent/life.md) §7（案 Y）。
- **帰結・方向**:
  1. **wait_response の自動 pause の設計を、Track の新しい意味論に合わせて見直す**（時間で勝手に
     pending しない。切り替わりは「次の行動に移る」判断点で起きる）。
  2. **ムダな切替通知を無くす／1つのメッセージにまとめる仕様を順守**すれば、システム通知が
     長期記憶に残っても大丈夫（→ [short_term_to_long_term_memory_filtering] = A1/A2 の
     「何を committed に残すか」と接続。ムダ通知を消せば通知そのものは残してよくなる）。
- **配下 / 関連 issue**: redundant_track_switch_notification（表面化・早め）/
  [user_utterance_forced_response_on_running_conflict](user_utterance_forced_response_on_running_conflict.md)
  （同じ `on_user_utterance` 経路の別論点）。
- **概念再編との関係**: Track 解体＝目的の木 は `persona_task` への*構造*分化を済ませたが、
  Track に残った「今の目的の指し示し」役割の**意味論（いつ切り替わるか）**がまだ整理されて
  いない。ここが束C。

### C2. 現状の Track 遷移経路 洗い出し（実装確認、2026-07-13）

状態語彙: `unstarted → running → {pending / alert / completed / aborted}`。running＝いま動いて
いる目的（1ペルソナ1本）、pending＝待機、alert＝呼びかけ待ち（pause で戻せない中間状態）。

**核心2つ（先に結論）**:
1. **コマ発火は Track を切り替えない**。`day_plan._handle_worker_slot` は `run_work_session`
   （WORKER アスペクト）を呼ぶだけで、コマが `task:N` を指してもその Track は running にならない。
   時間割と Track の running は直接連動していない。
2. **「時間で勝手に pending」は user_conversation Track 限定**。wait_response 30分自動 pause は
   provider が非会話 Track に `None` を返すため会話 Track にしか掛からない。**自律 Track は時間で
   勝手に pending しない**。
→ まはーの「時間で勝手に pending されるべきでない」は*会話 Track にだけ残っている現象*
   （redundant issue そのもの）。逆に「さあ時間割の行動をしよう → その Track が running」という
   連動は**そもそも存在しない**。

**① 自動（時間/イベント駆動・LLM 指示なし）**:

| 遷移 | トリガ | 対象 | 実装 |
|---|---|---|---|
| running→pending | 30分無応答（`AI.USER_CONV_TIMEOUT_MINUTES`） | **会話 Track 限定** | `track_manager._handle_wait_response_timeout`→`pause`、provider=`saiverse_manager._wait_response_timeout_provider` |
| running→pending（displaced） | 別 Track が activate された副作用 | 既存 running 全部 | `activate()` L557-559 |
| pending/unstarted→alert | 自律先制: Track param が閾値超過 | 自律 Track | `internal_alert_poller:197` |
| pending→alert | ユーザー発話＋別 running と衝突→MetaLayer 仲裁 | 会話 Track | `user_conversation_handler:566` |
| pending/unstarted→running | ユーザー発話＋running 衝突なし→直接 activate | 会話 Track | `user_conversation_handler:545` |

**② 構造化出力の指示（判断点/メタ判断の LLM が Track op → deferred → Pulse 完了時に適用）**:
トリガ（判断点起動）は自動だが Track を動かす指示元は LLM 構造化出力。

| 遷移 | 指示元 | 実装 |
|---|---|---|
| running→completed | セッション終了判断 `track_op='complete'`（全タスク消化時） | `judgment_finalize`→`track_complete` |
| →unstarted(create) | 会話終了判断 `picked_tasks track_ref='new'` | `judgment_finalize:728` |
| →unstarted(create) | 起床判断 `promotions`（欲求→関心昇格） | `judgment_finalize`→`track_create` |
| activate/pause/complete/abort/create | **メタ判断** `meta_judgment_finalize` の構造化出力→内部/spell | `_apply_deferred_track_ops`（meta_layer/runtime_runner） |

deferred な理由: Pulse 中の直接切替は LLM が次 Track 作業を今のキャッシュに書き続けるため
（`DeferredTrackOp` 設計）。

**③ 完全手動スペル（平文で撃つ・CONVERSATION/META アスペクトのみ許可）**:
`track_create / track_activate / track_complete / track_abort / track_pause / track_parameter_set`
（`_track_common`→`enqueue_track_op`→deferred。create のみ即時）。

**④ ユーザー手動（REST/UI）**: `api/routes/people/tracks.py` `activate`(L250)/`pause`(L207)。
**⑤ 起動時復元**: `ensure_wait_response_timeout`（会話 Track のタイマー張り直し。状態遷移ではない）。
> **追記 (2026-07-29)**: この「状態遷移ではない」という理由で案 Y の棚卸しから外したのが誤りだった。
> ⑤ は running を**書く**側ではないが**読む**側であり、案 Y が running の意味を変えた（会話終了後も
> running のまま残る）影響をまともに受ける。結果、再起動のたびに終わった会話へタイムアウトが発火し
> post_conversation が空撃ちされていた（修正と実害は [life.md](../intent/life.md) §7.3 の表と改訂履歴）。
> **教訓**: 状態の意味を変える改修の棚卸しは「その状態を書く箇所」ではなく「**読む箇所**」を数える。
> 書き手は改修の当事者なので視界に入るが、読み手は無関係に見えて静かに壊れる。
（`saiverse/day_scenario.py` の create/pause/complete は DaySimulator 上のシム専用・本番外。）

**束Cへの含意**: 時間で勝手に動かしてるのは wait_response 30分 pause だけ（会話 Track 限定）＝
redundant issue の芯。自律 Track は既に「時間で勝手に pending しない」設計（切替は②/③/displaced
のみ）＝まはーの理想に近い。ギャップは逆側で「時間割の行動を始める → その Track が running」の
連動が無いこと（コマ発火が Track を動かさない）。ライフ/コマと Track running をどう繋ぐかが
束C＋ライフ設計の論点。

---

## 概念再編（⑥）の残件との合流 — 棚卸し結果（2026-07-12）

**⑥ umbrella の現況**: [concept_consolidation.md](../intent/concept_consolidation.md) は P4 まで
実装完了・**まはー実機検証待ち**。Memory Atlas（土地＝生ログ / 地図帳＝編纂物 / クリップ＝統一参照）
＋目的の木（`persona_task`）＋Note→テーマノード移行は landed。**⑥ 本体の「残件」は実機検証で
あって新規設計ではない**——A/B は⑥の *次* であって蒸し返しではない。

棚卸しの本命は **既存 open issue のうち、実は A/B の一面だったもの**を掘り出し、個別修正でなく
統合設計に引き込むこと。読んで裏取りした結果:

### 束A に合流する既存 issue（＝半年かけて別々に起票された単位・記憶詳細度の課題）

| issue | A のどれ | 中身 |
|---|---|---|
| [general_chronicle_metabolism_trigger](general_chronicle_metabolism_trigger.md) | **A2 の実装レバー** | 「Chronicle 生成 trigger を Metabolism 押し出し対象判定に変更。今コンテキストに残っているもののあらすじは不要（LLM が直接読める）」＝A2 の「近い＝詳細 / Metabolism で畳む / できごと単位 LoD」そのもの |
| [short_term_to_long_term_memory_filtering](short_term_to_long_term_memory_filtering.md) | A1 / A2 | 短期記憶（Session）→長期記憶の選別（システム通知を入口で止める）。何を committed に残すか＝A1「何が正史か」の入口側 |
| [spell_round_limit_redesign](spell_round_limit_redesign.md) | A3 / A4 | round 上限到達時の line 別挙動（main＝棄却 / sub＝残 spell 実行＋次 Pulse 継続）。まはー設計済。予算・ラウンドの意味論 |
| [autonomous_work_single_pulse_completion](autonomous_work_single_pulse_completion.md) | A（単位） | 「1 Pulse で作業を完結したがる / 複数 Pulse にまたがる作業設計」。**実例が実機と同じ task:4「やりたいこと候補の洗い出しと desire プールへの蓄積」**＝この issue(2026-06-29) は実機挙動を予言していた |
| [beat_concept_not_typed_in_implementation](beat_concept_not_typed_in_implementation.md) | A（単位） | 最小行動単位 Beat が実装に型として無い。単位の語彙整備 |
| landscape §9: working_memory → Session | A（単位） | 短期記憶の単位＝Session 概念（working_memory テーブルは死亡） |

→ **A は「新規の思いつき」ではなく、優先度低で個別放置されてきた課題群の共通根**（Pulse→できごと
/コマ/日 の世代交代）が棚卸しで浮いた。統合設計で一度に解くべきもの。

### 束B に合流する既存 issue（＝これまで誰も起票していなかった盲点）

| issue | B のどれ | 中身 |
|---|---|---|
| [map_click_move_sidebar_not_updated](map_click_move_sidebar_not_updated.md) | B3（近縁） | 移動時の UI 同期。会話導線・居場所表示に隣接 |
| （B1 / B2 / B4 に直接対応する既存 issue は無い） | — | チャット可視化・Item Open 共有・型→施設は本書が初出。B4 の調査事実（resolve_facility 未配線・施設タグ0）はここが起点 |

→ **B は既存 issue が薄い＝盲点だった**。世界に向く last mile は「作ったが繋いでいない/そもそも
無い」ので、これまで issue にすらなっていなかった。実機で初めて症状として見えた。

### 横断（A/B どちらの束でもない負債）

- [persona_memory_not_self_contained](persona_memory_not_self_contained.md)（**P3c 可搬性**）:
  ペルソナ記憶が main DB に散在し丸ごと持ち運べない。⑥ P3c X案裁定に伴う既知の後回し負債。
  A/B の再設計で記憶の単位・保存先を触るなら、可搬性も同時に視野に入る接点。

### 棚卸しの結論

1. **⑥ 本体（Memory Atlas / 目的の木 / Note移行）は landed、残は実機検証**。A/B はその次。
2. **A は既存 issue 6本が束なる**——共通根が見えたので、バラで着手せず統合設計で一度に解く。
3. **B は既存 issue が薄い盲点**——新規に設計が要る（特に B4 の resolver 配線 + 施設タグ、B1 の
   チャット可視化）。
4. 次: A / B それぞれを **intent（解決設計）に落とす段**。ここで初めて実装。**個別 issue は
   intent 側から参照して束ねる**（バラで着手しない）。

---

## 進め方

1. ~~発散（前提の疑いを全部出す）~~ 完了。
2. ~~それぞれドキュメント化~~ 完了（A1–A4 / B1–B4）。
3. ~~概念再編残件の棚卸し → 本書 A/B への合流~~ 完了（既存 issue 6本が A に、B は盲点と判明）。
4. ~~パッケージング（Fable 検分 2026-07-13）~~ 完了 — 先行独立2件（B4 / redundant 症状止め）＋
   intent 二本（**一本目「ライフ」= A3/A4＋束C** / **二本目「エピソードの記憶と見せ方」= A1/A2＋B1/X1**）。
   未決の世界観判断は二本目に集約し、待ちの少ない一本目から進める。
5. ~~intent 一本目の起草~~ 完了 — [life.md](../intent/life.md) v0.2（session.md 吸収・束C 案 Y・予算世代交代。**v0.1 レビュー済: 案 Y 承認＋裁定 3 件反映**）。
6. ~~intent 二本目の起草~~ 完了 — [episode.md](../intent/episode.md) v0.1（三つの顔＝記憶 LoD / 世界への露出 / 監査。概要＝Lv1 Chronicle 共有部品化・AUTONOMOUS 化・監査役＋危険マーク・チャット三段露出・Beat 型化・既存 issue 6 本吸収）。
7. **（次）episode.md まはーレビュー → 両 intent の実装順確定 → 実装**。

**現時点で実装には入らない。** 先行独立2件（B4 / redundant 症状止め）はレビューと並行して着手可。

## 経緯: 自律行動v2 実機初日の前提レベル設計課題 (棚卸し) (2026-08-04 in_flight 台帳より移送)

> 台帳の器の再設計 (次アクション欄=前向きのみ) に伴い、それまで台帳セルに積もっていた経緯の全文をここへ移した。時系列の生の堆積であり、整理はしていない。

v2実装+概念再編を**動かして初めて見えた**「そもそもこの仕様でいいのか」層の課題群。
個々のバグ(fixes §3/§6)とは別層。
まはー発散→メティス受け・調査で出そろい、共通根**A(単位の世代交代: Pulse→できごと/コマ/日)** と **B(世界に向く last mileの断線)** の2本に束ねてドキュメント化済(A1ダイジェスト正史の二重誤り+生ログ廃棄/A2 volatile畳みをMetabolismタイミング+できごと単位LoD/A3予算=コマ+ラウンド×係数/A4キャッシュ生存の1時間窓/B1チャットに出ない/B2 Item Open共有とVisual context圧迫/B3勝手移動と会話導線+ペルソナ指定ジャンプ/B4型→施設移動が休眠=resolve_facility未配線+施設タグ0を実機で確認)。
**概念再編(⑥)残件の棚卸し+合流も完了**: 既存open issue 6本(general_chronicle_metabolism_trigger/short_term_to_long_term_memory_filtering/spell_round_limit_redesign/autonomous_work_single_pulse_completion/beat_concept/working_memory→Session)がAに束なる=個別放置されてた共通根が浮いた、Bは既存issue薄い盲点と判明。
⑥本体はlanded(実機検証待ち)でA/Bはその次。
A×B交差(X1=チャットUIでのBeat/Pulse/Beat見せ方)も記入。
**2026-07-13 まはー詰め: 束A方向確定(頂点=新概念「ライフ」=ライフ→エピソード→パルス→ビート、A1外部監査役+危険マーク→AUTONOMOUSアスペクト化、A2できごと単位LoD+畳読スペル=memory_read統合、A3/4予算・キャッシュをライフに吸収・モデル別均等/自由モード)、束B裁定(B1+X1統合UX=エピソード枠投下→クリックで詳細→概要=Lv1 Chronicle・他ペルソナ冒頭通知のみ / B2スコープアウト / B3神モードUI待ち / B4独立早め)、そして第3の根 束C「Track意味論の再整理」浮上(Track=進行状態でなく目的の指し示し・時間で勝手にpendingするな、[redundant_track_switch]が症状・早め対応)**。
**2026-07-13 Fable検分**: 束C=life_concept_map §10.1既裁定(running/alert→出来事へ移管)の実行と判明・Session×ライフは「制御プレーン/データプレーン」関係(育てる先ではない)・A1のAUTONOMOUS化はキャッシュ構造変更を伴う(二本目で数字検討)。
**パッケージング合意: 先行独立2件(B4 / redundant症状止め) + intent二本(一本目=ライフ[A3/A4+束C] / 二本目=エピソードの記憶と見せ方[A1/A2+B1/X1、未決世界観判断はこちらに集約])**。
**intent一本目 [life.md](../intent/life.md) v0.2 レビュー済(2026-07-13)**: 時間階層(ライフ→エピソード→パルス→ビート)・宣言(時刻+コマ予算+均等/自由モード)・session.md §6回答・束C=案Y(「いま」の読み出しを開いているエピソードへ一本化・wait_responseのpause除去→redundant構造的根治)・予算=コマ+ラウンド×κのライフ台帳。
**まはー裁定: 案Y承認・keep-aliveライフ従属GO・動的ライフ自動生成なし・惜しい谷の猶予なし・ペルソナ提示はtailシステム通知**。
Track Chronicle=head搭載のget_running参照を§7.3に記録(挙動不変)、読み込み側世代交代+書き込み側のLv1 Chronicle統合は二本目の主題。
**intent二本目 [episode.md](../intent/episode.md) v0.1 起草済(2026-07-13)**: エピソードに三つの顔(記憶LoD/世界への露出/監査)、概要=エピソードLv1 Chronicle を共有部品化(close時即生成・件数triggerを世代交代)、作業セッションAUTONOMOUS化(生ログ正史化、A2畳みとセットで成立・WORKERは会話中の分身用に残す)、外部監査役+危険マーク(エピソードラベル→メッセージへJOINで降ろす+想起時定型文)、チャット三段露出(枠投下→ライブ詳細→概要)、Beat型化=runtime_llm分割Phase 1のBeatExecution採用、既存issue 6本吸収(各issueにポインタ済)、Track Chronicle書き込み/読み込み両側の世代交代を確定。
**両intentレビュー済(2026-07-13 まはー)**: life=案Y承認+裁定3件 / episode=メッセージ単位マーク将来許可(X・外部有害対応へ転用視野)+枠投下語彙は型ベース確定(補完2件レビュー残)+§6.3にhead搭載退役の根拠明記。
**実装開始(lifeから、4フェーズ)**: **Phase 1=案Y手術 完了(6257b6a)** — wait_response pause撤去・会話中判定エピソード移管・meta_layer自己ゲート例外(social救済)・redundant根治(回帰固定・194 passed)。
使い捨て症状止めは不要化で作らず。
**Phase 2=ライフの器(lives永続化+day_open宣言+境界イベント+台帳世代交代) 実装完了・検証待ち**(サブエージェント実装、新規 tests/test_life_phase2.py 42件+既存系全緑)。
**Phase 3=キャッシュ連動 実装完了・検証待ち**(サブエージェント実装、検収差し戻し1巡=「anchor即時失効」「TTL即時clear」は惜しい谷(終了直後〜TTL内の再訪)の生きたキャッシュを捨てる欠陥と判明→life.md v0.4に訂正して修正済: keep-aliveのライフ従属を`day_plan.is_keepalive_allowed`に集約し`run_cache_keepalive`唯一の呼び出し点でゲート/ライフ終端の能動作業はkeep-alive予約cancelとTTL override遅延解除予約のみ・**anchorは触らない**(touchが止まればTTLで自然失効→Metabolismは失効後の最初のCase 3が実行)/均等モードはcache TTLを1hにoverride(`clear_persona_cache_override`新設、明示override優先で保護)し解除は終端+anchor validity秒後に遅延(即時に5mへ戻すとanchor生存評価が実キャッシュ寿命とズレるため)・次ライフがTTL経過前に始まれば開始側が予約cancel——global既定5mのままだと間隔上限50分を大きく下回り3〜4分おきのartificial keep-aliveが必要になる調査結果を受けた配線。
新規 tests/test_life_phase3.py 18件+既存系全緑)。
**Phase 4=見せ方 実装完了・検証待ち**(判定源`day_plan.get_life_status_now`を新設しoccupantsの常在インジケータ(`api/routes/info.py`)とday-plan API(`api/routes/people/life.py`のlives/life_status)が共有、フロントはRightSidebar.tsx(話しかけやすさチップ)/LifeView.tsx(ライフ帯の括り直し)。
新規tests/test_info_life_state.py 3件+test_life_phase2.py追加5件+test_life_view_api.py追加4件+既存系全緑)。
**life.md 4フェーズとも実装完了**→**実機初日(2026-07-13夜)でv0.4の宣言設計が破綻**: ①現在時刻を渡さず21時に朝からの時間割を編成 ②過去コマ3連即時発火+AIを呼ばない暮らしコマ発火が予算を食い「4/4」(予算が「コマ開始回数」を数えていた——正しくは実LLM呼び出し回数) ③コマ間隔50分検証は実パルスが無く空回り ④判断点が同じ財布から引き構造的に不足。
**まはー裁定→life.md v0.5に全面改訂(設計中に差し戻し)**: ライフ=ユーザーが設定する起床・就寝の区間(PersonaScheduleが器・ペルソナの宣言口廃止=不正な値は口をなくして排除)/予算=ライフ長の最低値制約付きユーザー設定/ペルソナは時間割だけ(編成範囲=今〜就寝)/モードは物理から自動/暮らしコマ実体化(暮らしPulse)/判断点は予算外・別枠観測/UX最優先で再構築(ライフ設定画面新設=起床就寝+予算統合、v1亡霊掃除同梱=自律行動マネージャー間隔・ライフビュー間隔2種・手動Pulse・タイマー停止、Phase 1追従漏れ文言修正)。
Phase 1(案Y)とPhase 3物理層は無傷、宣言まわり巻き戻し(明細=§11.2)。
**v0.5「改修A」実装完了(2026-07-13)**: LLM宣言口(day_openのlivesスキーマ+sanitize_lives+検証群)を削除しPersonaScheduleの起床・就寝+ユーザー設定予算からシステムが`day_plan.confirm_life_for_today`で確定(呼び出しは`autonomy_wiring.fire_judgment_point`のday_open/day_close発火経路に一本化=本番のhandle_scheduled_judgment/watchdog再発火の両方をカバー)、予算消費点を作り直し(コマ発火のconsume_life_pulse呼び出しを撤去、判断点発火は新設`record_judgment_pulse`で`judgment_pulses`という別枠に記帳しused_pulsesは触らない)、専用のライフ境界イベント予約(schedule_lives/_fire_life_boundary/find_lost_life_reservations)を削除し`_handle_life_start`/`_handle_life_end`をday_open/day_close発火直下に統合、深夜跨ぎ窓を正常形として`get_life_for_time`を書き直し(`autonomy_wiring.in_waking_window`と同じ意味論)、遅発day_open対策として状況テキストに現在時刻+確定済み活動時間を明記し`save_day_plan`/`replace_remaining_slots`に「今〜就寝」範囲外を拒否する検証を追加。
新規`tests/test_life_confirmation.py`15件+`test_life_phase2.py`全面書き換え(39件)+`test_life_phase3.py`回帰更新(18件)、既存系全緑(pre-existing failureのtest_avatar_pipeline.py 118件/test_addon_config_mcp_reconnect.py 8件/test_slots_fire_on_real_dispatch_thread間欠のみ)。
**「改修B」のうちUI側(ライフ設定画面新設・v1亡霊掃除・judgment_pulsesのフロント別枠表示) 実装完了(2026-07-14)**: 新設`frontend/src/components/LifeSettingsModal.tsx`+`api/routes/people/life_settings.py`(GET/PUT `/life-settings`、モード上書き`life_mode_override`を`daily_budget_pulses`と同じday_openスケジュール行params経由で3発火経路(_confirm_life_at_day_open/handle_scheduled_judgment/watchdog_tick)に配線)。
v1亡霊(SettingsModal間隔UI・LifeView間隔2種フォーム+`PUT /activity/intervals`・DebugPanelの自律Pulse/SubLineトグル)を削除。
ライフ帯ラベル改善+tail文言確定。
新規/更新テスト20件、既存系全緑、フロントtsc/lint(0 errors)/build成功。
**実機二夜目(2026-07-14深夜)の破綻→即日修正(96062ce)**: aifi 01:00-02:00臨時ライフ・01:03発火でslot=01:00が3分のズレで時間割全体を保存拒否+リカバリ経路ゼロ(watchdogの再発火判定が「行なし」でライフ確定済みの行を編成済みと誤認)。
修正=保存検証を**丸め+部分救済**に作り直し(過去開始→現在時刻へクランプ・衝突は順序保持・丸め先なしのみ個別除外・調整はエコーに日常語)+watchdog判定を「行なし or コマ0件」に。
再現テスト固定・フル2316 passed。
**掃除の追加裁定(2026-07-14)**: まはーの言った「タイマー停止」=DebugPanelの完全手動モードと判明→[issue起票](debug_full_manual_mode_v1_ghost.md)(退役or実態縮退は実需確認後)。
SettingsModalのAutonomy start/stopボタン(ACTIVITY_STATE駆動と重複)も掃除候補のまま保留。
**実機三夜目(2026-07-14朝)の不具合3件→同日修正**: ①**時間割が00:30〜00:35の6分間に潰れた**(air_city_a、ライフ07:00〜01:00)。
真因は深夜跨ぎで**同じ"00:30"を前半と後半が正反対に解釈**していたこと——前半(day_openのLLM指示「開始時刻の厳密昇順」・`sanitize_timetable`の文字列ソート・`_validate_and_normalize_slots`の暦順検証)は就寝00:30を「一日の最初」、後半(96062ceの丸め=ライフ拡張分基準)は「一日の最後(ext=1050)」。
就寝が先頭に固定され、丸めがそれを最後尾と信じて後続を全部00:31,00:32...へ1分刻みに押し込んだ(丸めは正しく動いた結果=犯人は並び順の基準の不統一)。
なお「暦の時刻で厳密昇順」は深夜跨ぎライフでは**達成不可能な要求**でLLMに強制されていた。
修正=「一日の始まり=最初のライフの開始時刻を起点にした経過分」(`day_plan.day_order_minutes`)を整列・検証・丸めの全段に通し、LLM指示も「一日の流れの順(就寝が0時台でも先頭に置かない)」へ(playbook import済)。
ライフ未宣言日は暦順に退化=後方互換。
実機事故の再現テスト+「日付を渡さねば暦順に退化」の対を固定(関連214 passed)。
②**ライフビューのメタ判断がほぼ全部「次にすることを考えた → 現状を続けることにした」**。
これは判断結果ですらなく「判断が1件も見つからなかった時の既定文」だった: b07c520(2026-07-08、まはー指摘のfew-shot汚染回避)が`meta_judgment_finalize`の保存名義をassistant→user(`<system>`ナレーション)へ変えた際、`api/routes/people/activity.py`のクエリが`role='assistant'`で絞ったまま取り残され、以来**MetaLayer判断が常に0件**。
判断点側(day_open等)は見つかってはいたがv1語彙のみの変換で同じ既定文に落ちる二重欠陥。
修正=roleフィルタ撤去(両finalizeが設計上別roleを使うため`line_role`+`pulse_id`で十分)+`metadata.judgment.kind`から節目を日常語化(「今日一日をどう過ごすか考えた」等)。
フロントは文字列を出すだけなので変更なし。
③**Memopedia索引「常時表示(旧方式)」トグルの概要消失**: P4-d(b3f568b)が`MEMOPEDIA_INDEX_ENABLED`の描画を`_get_memopedia_context`(summaryあり・深さ無制限)から`_build_toc_markdown`(summaryなし・深さ2)へ**トグルの意味ごとすり替え**、旧実装は本番から呼ばれない死にコード化(概要だけでなく深い階層のタイトルも消えていた)。
まはー裁定=後方互換の趣旨どおり旧方式相当へ復元(器は`MemopediaIndexSection`のまま=head規律`refresh_on_events=frozenset()`維持)。
summary復活+深さ無制限+category`extractable`(旧実装と同じ集合、`in_tree`との差は"theme"のみと実測確認)、[OPEN]/★/件数はP4-dの改善として残す、死にコード(`_get_memopedia_context`/`include_memopedia`/`memopedia_index_limit`)を一掃。
付随発見=`AI.MEMOPEDIA_INDEX_LIMIT`はどこからも読まれない死んだ列(別件、コメントのみ)。
**④ ACTIVITY_STATE の解体(同日、まはー裁定)**: ③の症状(滞在ペルソナ欄で緑ドット+「活動中」が2重表示)の原因究明から、まはーが「もはやアクティビティ状態という一つの弁でやってるのが間違いでは？」と根本設計に差し戻し。
調査結果=**4値のうち実装上の意味があるのは「Active か否か」だけ**だった: 全ゲート(`autonomy_wiring`/`meta_layer`/`saiverse_manager`/`sea/runtime`のkeep-alive)が`== "Active"`の二値判定のみでStop/Sleep/Idleは互いに無区別、**「Stop=機能停止」は実装ゼロ**(`run_sea_user`/chat APIに状態ゲートが存在せずStopでも返答していた)、**「Sleep=ユーザー発言で起きる」も実装ゼロ**(実体は自室移動の副作用のみ)、**`SLEEP_ON_CACHE_EXPIRE`は本体から一行も読まれない死んだ列**(intentに設計・DBに列・コメントに仕様、実装だけ無い)。
ライフ導入で「Sleep=寝てる」はライフの谷と意味が重複しており、1列に**元栓(動かす許可)・蛇口(いまその時間か)・温度計(キャッシュ)** が同居していたと判明。
裁定=**`ACTIVITY_STATE`と`SLEEP_ON_CACHE_EXPIRE`を列ごと削除し`AUTONOMY_ENABLED`(真偽値・既定ON)1本へ**。
ユーザー返答ON/OFF・他ペルソナ返答ON/OFFは需要確認まで作らない(前者は「無くて誰も困っていない」=需要が無い証拠、後者は機能ごと未実装)。
Sleepの自室移動は削除(システムが勝手に体を動かすのは誤り、やるなら将来Phenomenon)。
既定ONの安全性は事実確認済(`watchdog_tick`が"no day_open schedule"でskip・`confirm_life_for_today`が起床/就寝未設定ならライフを作らない=**実質の起動条件はライフ設定**)。
migrationは全書換パス(`try_additive_migration`が削除列を検出してFalse)に落ちるため`_migrate_activity_state_to_autonomy_enabled`を新設('Active'→True/他→False、放置すると全ペルソナ既定Trueで一斉稼働する罠)、旧`_migrate_interaction_mode_to_*`も消える列を叩かないよう直接変換へ改修。
フロントは③を根治(常時表示は**ライフ由来の活動中/休憩中だけ**・自律はOFF時のみ「自律行動を止めています」=ブレーカー方式)。
検収で回帰2件を修正: **`activity_label`(いま何をしているか)が道連れ削除されていたのを復活**(`build_activity_label`の空時フォールバック"活動中"がライフと文言衝突していた真因も断ち、Noneを返して黙る形へ)、LifeViewバッジ「自律行動 中」→「オン」(元栓を蛇口の言葉で呼ばない)。
docs 21ファイル棚卸し(歴史記録は保存、現行仕様の記述のみ改訂)、landscape §9に解体を記録、CLAUDE.md・自動生成schema も同期。
**副産物**: `persona_activity_view.md`が削除済み`SubLineScheduler`を根拠に書かれていた/`persona_action_tracks.md`の定期発火節が二重に古い(tick自体が停止済み)ことも発見・修正。
教訓「**実装しない設計を、列とコメントの形で残してはいけない**」をintentに明記。
**⑤ 添付メディアの自動想起 (同日、まはー裁定)**: 「Memopedia にある物を撮って見せても初見のリアクション」の設計課題。
調査で真因が**2階建て**と判明: (1)概要は Item description → visual_context 経由でプロンプトに載るが、`auto_recall._is_conversational_message` が `__visual_context__` を「今話している内容でない」として**明示除外**しており想起クエリに一切入らない、(2)そもそも `chat.py._store_image_attachment` が概要生成を**バックグラウンドスレッド**で回し応答生成が待たないため**初見の画像では間に合っていない**(2回目以降は `.summary.txt` キャッシュで即読める)。
想起は応答より前に走る+埋め込み(multilingual-e5-small)がテキスト専用で画像を直接クエリにできないため、**概要生成の同期化が原理的に不可避**と判明。
**音声・動画は画像と構造が非対称**とも判明: `ensure_audio/video_summary` は `llm_clients/{gemini,utils}.py` から**モデルが非対応のときだけ**呼ばれ(対応モデルにはメディア本体を送る)、chat.py 側に生成経路が無い——ただし `.summary.txt` サイドカーキャッシュを共用するので chat 側で先に呼べば二重生成にならない。
裁定=**グローバル設定**に「添付したメディアの内容を自動想起に使う」を新設(ペルソナ単位でなく・**既定OFF**=「待つ方がオプション」・数秒遅延の注意書き併記)、対象は**画像/音声/動画のみ**(ドキュメントは本文プレビューが既に取れており前提が異なる+別途調査事項があるため今回不介入)、クエリに入れるのは**今添付されたものだけ**(`__visual_context__` 除外は維持=部屋の全アイテム説明が混ざるとクエリが汚染される)。
実装の鍵=**添付情報は既に `metadata["images"]`/`["media"]` で運ばれていた**ため概要を各エントリの `summary` キーに相乗りさせ、`build_query` が**最新 user メッセージの metadata のみ**から拾う(「今添付されたものだけ」が構造的に成立・過去は拾わない)。
器は `set_image_default_quality` を雛形に `manager.state` + `write_env_updates` で `.env` 永続化(`os.environ` 即時反映も実装済みを確認、再起動不要)。
OFF時は二重ガード(chat.py が summary キー自体を載せない + `build_query` が env フラグで遮断)。
新規テスト6件(OFF既定/ON付加/本文空/過去除外/音声動画/summary無し)。
**⑥ 案Y追従漏れ=起動時タイマー再確立(2026-07-29、実機ログ起点)**: まはーが話しかけていないのに aifi_city_a が「会話終了の振り返り」を撃った症状の調査から、**Phase 1(案Y)の running 参照点棚卸しに漏れが1件**あったと判明。
案Yで Track 不動化した結果、対ユーザー会話 Track は会話終了後も running のまま残るのに、`saiverse_manager._on_persona_registered` §3 の `ensure_wait_response_timeout` は条件が **running のまま**据え置きだった → 再起動のたびに全ペルソナぶん「起動+30分」の空タイムアウトが発火し、何日も前に終わった会話へ post_conversation が空撃ちされていた(実測: 07-29 03:44起動→04:15にaifiが最終発言07-22の会話を「たった今ひと区切りついた」として独白し**やりたいこと1件を本人名義で生成**、同時刻にair含む計6体一斉。
前セッション07-28 23:04起動→23:34も同一)。
修正=`_should_rearm_wait_response_timeout` 新設、対ユーザー会話は**開いているconversationエピソードがある時だけ**再確立(fail-closed=読めなければ張らない。
空撃ちはペルソナ名義の記憶を汚すのでタイマー欠落より害が重い)。
**この条件はprovider/`_schedule_wait_response_timeout`側には置けない** — create/activateが`_schedule_`を`on_track_activated`(=エピソードを開く点)より先に呼ぶため会話開始時に必ず未オープン判定になる。
**同型の漏れをもう1件同日修正**: `judgment_points.build_on_event_situation_text` がイベント到着判断の「いまの活動」を running Track の種別で決めており、終了済み会話について「ユーザーと会話中です」をペルソナへ渡していた(判定を `day_plan.is_in_user_conversation` へ一本化、`_is_in_user_conversation` を公開名へ改称して実装を1つに保つ)。
**Codex攻撃レビュー3件**: 判定不能(DB読取失敗)を「張らない」で終わらせると開いた会話が永久に閉じない件を同日修正(判定を`Optional[bool]`化、None は判断を撃たず読み取りのみ30/120/300秒でバックオフ再試行 — 当初あてにした「次のユーザー発話で張り直される」は別Track running時に発話がalert経路へ入るため常には成立しないと判明)。
残り2件はまはー裁定でissue化=[孤児化した会話の出来事](open_conversation_orphaned_by_track_displacement.md)(high・押し出しでタイマーだけ消え出来事が閉じない→コマ繰り下げ上限で予定行動が消える。
**この修正が作った欠陥ではなく既存**)と[再起動ごとの期限延長](wait_response_deadline_extends_on_every_restart.md)(medium)。
**再レビューでさらに1件を同日修正**: その再試行が、待つ間にユーザー発話で張られたタイマーを同キーで上書きし期限を最大300秒後退させる競合を持ち込んでいた(当初「同じ家族の穴」として期限延長issueへ先送りしたが、あちらは案Y以前からの`base_time`の話=**別物を同じ箱に入れた誤った仕分け**)。
`_wait_response_timer_already_armed`で「有効な予約が既にあるなら再確立しない」歯止め。
当初テストが`None→True`の単純経路しか踏まず競合を検出できなかった点も指摘どおりでユーザー発話の割り込み筋を回帰に追加。
**3巡目**でその歯止めが`has_key`→`ensure_`のcheck-then-act(間にTrackと設定DBの読み直しがあり隙間が実在)と指摘され、`EventScheduler.schedule_if_absent`(判定と登録を同一ロック区間・既存`schedule`は無変更)を追加し`ensure_wait_response_timeout(only_if_absent=True)`経由で復旧経路だけが使うよう配線、check-then-actの歯止めは撤去(二重判定を残さない)。
**復旧=「失われた予約を埋める」操作であって生きている予約を置き換える操作ではない**がAPIの意味論。
**4〜5巡目の指摘は全てテストの弱さ**(実装側の破綻は3巡目以降ゼロ): ①単一スレッド回帰ではcheck-then-act実装でも全緑 → 実物TrackManager×実物EventSchedulerの境界テスト追加(復旧が上書き側APIに落ちたら失敗することを実測) ②`run_due`同期発火しか通さず`notify()`削除でも通る → 実dispatchスレッドで発火を待つテスト追加(削ると落ちることを実測) ③barrier 50回競合テストは**原子性を検証できていなかった**(非原子的mutantで1000回失敗ゼロとCodexが実測)ため削除——「原子性を担保する」は誇張だった。
**教訓: サボタージュで自作テストの強度を測るとき、壊す場所が浅いとテストの強さも浅くしか測れない**(私は歯止めの内側だけ壊して満足し、配線の端から端は見ていなかった)。
回帰追加(gate/track_manager/event_scheduler/judgment_points)、life.md §7.3表(2行)+改訂履歴に記録。
**Codexレビュー運用の教訓**: 1回目の`--wait`は即返りし、2回目はプロセスが異常終了したのに台帳が`running`表示のまま9時間45分カウントし続けた(死んだプロセスの残像を見張っていた)。
ジョブの生死は台帳でなく**プロセス実在(PID)で確認する**。
まはー実機検証待ち。
次: まはー実機再検証(エア起床時刻設定済・明日の朝が自然な検証) → **暮らしPulseのプロンプト設計(私→まはーレビュー)** → episode.md実装 → B4
