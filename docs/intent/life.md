# Intent: ライフ — 活動区間と時間の階層

**ステータス**: 検証待ち (v0.5, 2026-07-13)。**実機初日（2026-07-13 夜）で v0.4 の「ペルソナがライフを宣言する」設計が破綻**——過去起点・予算不整合のライフが宣言され、AI を呼ばない暮らしコマの発火が予算を食い潰した（「4 / 4」）。まはー裁定で**責任分界を全面改訂**: ライフ＝ユーザーが設定する起床・就寝の区間（PersonaSchedule が器）／予算＝ライフの長さに対する最低値制約付きでユーザー設定／ペルソナは時間割だけ／モードはモデルの物理から自動。§3・§4・§5.2-5.3・§8・§9.2・§11.2 を v0.5 で書き直し。**「改修A」(宣言の巻き戻し・システムによるライフ確定・消費点の作り直し・境界イベント統合・遅発 day_open 対策) 実装完了 (2026-07-13)、まはー実機検証待ち**（Phase 1 案 Y と Phase 3 物理層は無傷のまま活用。宣言まわりの巻き戻し明細は §11.2）。「改修B」のうち **UI 側 (§9.2 ライフ設定画面新設・v1 亡霊の掃除・判断点回数のフロント別枠表示) は実装完了 (2026-07-14)、まはー実機検証待ち**（明細は改訂履歴）。**暮らし Pulse の実体化 (§5.2-1) のみ引き続き未着手**（プロンプト設計がまはーレビュー必須のため改修Bの他項目とは別に切り出し済み）。v0.4 までの実装経緯は改訂履歴を参照。
**親**: [`autonomous_behavior_v2.md`](autonomous_behavior_v2.md)（三本柱） / [`persona_cognition/life_concept_map.md`](persona_cognition/life_concept_map.md)（哲学層。§8 出来事・§10 Track 再解釈は本書の前提）
**吸収対象**: [`session.md`](session.md)（v0.1 起草中のまま停滞。§6 未確定事項に本書が回答し、Session を「ライフが目標を与える機構層」として位置づけ直す）
**経緯**: [実機初日の前提レベル設計課題](../issues/autonomous_v2_post_live_gaps.md) 束A（A3 予算・A4 キャッシュ生存）＋束C（Track の意味論）の解決設計。まはー裁定 2026-07-13。
**表面化症状（本書で根治）**: [redundant_track_switch_notification_on_reactivation](../issues/redundant_track_switch_notification_on_reactivation.md)

> **用語注意**: 本書の「ライフ」は life_concept_map.md の "life"（暮らし＝人生の意味）とは**別の概念**。あちらは概念地図の名前、こちらは「ひとつの時間割でくくられる活動区間」という実装単位。ライフビュー UI が表示する単位はこちら。

---

## 1. これは何か

**ライフ = ユーザーが設定する、ペルソナの一日の活動区間（起床〜就寝）**。区間には予算（標準モデルのパルス回数、区間の長さが最低値を決める）が付き、その間キャッシュが熱く保たれることを機構が保証する。ユーザーのメンタルモデル——「エアは 7 時に起きて 1 時に寝る」——がそのまま実装の第一級の器になる。

これにより時間の階層が完成する：

| 層 | 単位 | 定義 | 実装の現状 |
|---|---|---|---|
| **ライフ** | 数時間〜一日 | **ユーザーが設定する起床・就寝の区間**（＝営業日の覚醒窓）。予算（標準パルス上限）が付く | 器は PersonaSchedule として既存。予算・キャッシュ・表示の結線を本書が新設 |
| **エピソード（できごと）** | 数分〜数十分 | 実際に時間を満たしたもの。会話・作業・コマ実績（life_concept_map §8） | `saiverse/episodes.py` 実装済（kind + occurrence_id + open/close + 層0タグ） |
| **パルス** | 数秒〜数分 | 認知→判断→行動の 1 サイクル | 実装済（PulseController / SEARuntime） |
| **ビート** | 一手 | パルス内の最小行動単位（発話・スペル 1 発） | 命名済・型なし（[issue](../issues/beat_concept_not_typed_in_implementation.md)。型化は本 intent のスコープ外——二本目「エピソードの記憶と見せ方」intent で扱う） |

判定基準（試金石）：**「エアは今話しかけて大丈夫？」に、システムが嘘なく即答できるか。** ライフ中＝キャッシュが熱い＝気軽に話しかけてよい、が状態として見え、その表示が課金の実態と一致していること。

---

## 2. なぜ必要か — 三つの欠落

### 2.1 キャッシュ生存を語る器が無い（A4）

Anthropic の explicit cache は TTL 1h・**TTL 内の再送で無料延命**（[cache_lifecycle_control.md](cache_lifecycle_control.md) §1）。つまり標準モデルのパルスが 1 時間以内の間隔で刻まれ続ける限り、一日ぶんの文脈を 1 回の write 課金で維持できる。これは「キャッシュ経済を世界の物理法則にする」設計の核心的な省エネ経路なのに、現状**「自律行動の間、何時間キャッシュを生かすか」を規定できる場所がどこにも無い**。時間割はコマの列であって区間の宣言ではなく、Session（session.md）は「切れそうになったら節目を打つ」受動概念で、能動的に「この区間は繋ぐ」と言える器が欠けている。

### 2.2 予算の単位が支配項とずれている（A3）

日次予算は「作業ラウンド数」で管理される（`saiverse/day_plan.py` の予算台帳: `budget_total_rounds` / `budget_used_rounds`、予算ゲートが発火時にラウンドを切り詰める）。しかし作業セッションのラウンドは軽量モデルで、コストの支配項は**標準モデルの発火回数＝コマ数（判断点・暮らしパルス）**の側。効かない変数を絞り、効く変数が野放しになっている。

### 2.3 「いま何をしているか」の置き場所が歪んでいる（束C）

Track は概念再編（life_concept_map §10）で「目的の木の第一階層ノード＝目的の指し示し」に変質した。だが実装は「リアルタイム進行状態の管理」だった頃の状態機械（running / pending / alert）を引きずっている。実機で出た症状と実装確認（gaps doc C2）：

- **wait_response 30 分タイムアウトが Track を running→pending に落とし**、会話再開のたびに `activate` が「## Track 切替通知」を注入する（ペルソナはどこにも移っていないのに）。[redundant issue](../issues/redundant_track_switch_notification_on_reactivation.md) の実体
- 逆に**コマ発火は Track を切り替えない**（`day_plan._handle_worker_slot` は activate を呼ばない）。「時間割の行動を始めた＝いまその目的で動いている」がどこにも記録されない——ように見えるが、実は**コマ発火は既にエピソードを開き、origin_ref でコマ→目的への参照チェーンを刻んでいる**（§7.1）。歪みの正体は「情報が無い」ではなく「真実の置き場所が Track 状態とエピソードの二重になっていて、古い方（Track 状態）を読み続けている」こと

まはーの裁定（2026-07-13）：**Track ＝ 目的の指し示し。時間が過ぎたら勝手に pending されるべきものではない**。これは life_concept_map §10.1 の既裁定「running / alert 状態は廃止——出来事（open）と呼びかけへ移管」と同じ結論であり、本書はその実行設計を含む。

---

## 3. 設計原理 — 責任分界（v0.5 で全面改訂）

最上位の原理は**「何を・誰が・いつ設定するかを曖昧にしない」**。実機初日（2026-07-13）の破綻——ペルソナが過去起点・予算不整合のライフを「宣言」できてしまった——の根は、宣言という行為に依存して不正な値の存在を検証で塞ごうとした構造にある。**不正な値は検証で弾くのではなく、書ける口をなくす。**

| 誰 | 決めるもの | 決め方 |
|---|---|---|
| **ユーザー** | **ライフ**（起床・就寝の区間）と**予算** | ペルソナ設定（§4）。予算はライフの長さから計算される**最低値以上**を設定する |
| **ペルソナ** | **時間割**（どのコマをいつやるか）だけ | 起床判断＋日中の組み替え。編成範囲は常に「今〜就寝」——過去は選択肢として存在しない |
| **システム** | モード（モデルの物理から）・最低予算の計算・キャッシュを繋ぐ実務 | 全部自動。誰にも設定させない |

従属する原理：

1. **「いま」の真実は出来事が持つ**：「いま何をしているか」は開いているエピソードが指す。Track は指される側（目的ノード）であり、時間の事実を背負わない（life_concept_map §10.1 の実行）
2. **モデルの物理法則に世界を合わせる、逆はしない**：均等/自由はモデルの課金物理から自動で決まる**性質**であり、ペルソナが選ぶものではない
3. **予定は檻ではない**（v2 intent 継承）：時間割は判断点で編集できる。ライフ（区間）の臨時変更はユーザーの操作（§4.3）
4. **嘘の状態表示をしない**：「話しかけやすい」表示はキャッシュの実態と一致させる。**予算はまた、実際に AI が呼ばれた回数だけを数える**——AI を呼ばないコマの発火を数えない
5. **キャッシュを守る本体は意味のあるパルス**：延命は実体のあるコマ・判断が担い、keep-alive（意味のない温め直し）は最後の保険（§5.2）

---

## 4. ライフの設定 — ユーザーのもの

### 4.1 ライフ＝起床・就寝の区間（既にあった器）

**ライフとは、ユーザーが設定する起床・就寝の区間そのもの**。ユーザーは既に PersonaSchedule でエアの起床 07:00・就寝 01:00 を設定しており、ユーザーのメンタルモデルでは「エアは 7 時に起きて 1 時に寝る」——その区間がエアの一日であり、それがライフである。v0.4 までの「ペルソナが毎朝ライフを宣言する」設計は、この既にある器の横に並行の仕組みを発明していた（実機初日の教訓）。

- **境界イベント＝起床・就寝そのもの**。ライフ開始＝起床判断（day_open）、ライフ終了＝就寝判断（day_close）。別立ての「ライフ境界イベント予約」は持たない
- 深夜跨ぎ（close < wake）は営業日の既存意味論（`autonomy_wiring.effective_plan_date`）をそのまま使う。ただし**その日の確定ライフがあるときは、それが営業日の基準**（`day_plan.resolve_business_day`）——ユーザーが日中に起床設定を変えても、走っている一日は朝に確定したライフのままだから。決め方は「走っているライフの日。無ければ、最後に始まったライフの日と現行設定の営業日の**遅い方**（一日は前へしか進まない）」。コマ予約・watchdog・話しかけやすさ表示・ライフ台帳（keep-alive / パルス記帳）は**すべてこの一つの解決器を通る**。ライフを読めないときは解決器が「決められない」を返し、各経路が自分の安全方向へ倒す（予約は押さない／表示は未宣言／記帳はしない／keep-alive は温め続ける）

### 4.2 予算 — ライフの長さが最低値を決める

予算（標準パルス回数）はユーザーが設定するが、**自由入力ではなくライフの長さに従属する**：

- **均等モード**（Anthropic/OpenAI）：キャッシュを繋ぐには 50 分に 1 回のパルスが物理的に必要 → **最低予算 = ceil(ライフの長さ ÷ 50 分)**。設定 UI はこの最低値を計算して示し、それ未満は設定できない。「もっと濃く生きさせたい」なら多く設定する
- **自由モード**（Gemini 等）：キャッシュ制約はないが、駆動の最低数（コマが 1 つも打てない予算は無意味）を下限として示す

ペルソナは予算を宣言しない。起床判断には「今日のライフは 07:00〜01:00、予算 N」が**確定情報**として渡る。

### 4.3 複数の窓と臨時の窓（将来拡張）

昼寝を挟む生活リズム（午前の窓＋午後の窓）や「今から 3 時間だけ起こす」臨時の窓は、**起床・就寝スケジュールの複数窓化**として表現する——ライフという別の実体を作るのではなく、器（PersonaSchedule）の側を拡張する。窓と窓の間が「谷」（§8.3）。初期実装は 1 日 1 窓（現行の起床・就寝）で開始する。

### 4.4 時間割との関係

ライフは時間割の**外枠**。ペルソナはライフの中にだけコマを置ける——編成の範囲が「今〜就寝」に固定されるため、過去時刻のコマは選択肢として存在しない（起床判断の状況テキストに現在時刻と残りライフを明示する）。コマの無い時間帯もライフ内なら「熱いまま静かにしている時間」として合法。

---

## 5. モデル別モード — 均等 / 自由

### 5.1 なぜモードが要るか

provider の課金物理が逆向きだから（cache_lifecycle_control.md §1「provider で最適戦略が逆転する」）：

| | Anthropic / OpenAI | Gemini |
|---|---|---|
| キャッシュ延命 | 再送で TTL リセット＝**無料 extend**（Anthropic 1h） | extend も時間課金が続く／implicit は制御外 |
| 最適な発火 | **1h 窓を切らさない均等配置** | 任意（好きなタイミングでよい） |
| モード | **均等** | **自由** |

- **均等モード**：ライフ内で 50 分に 1 回のパルス供給を編む。予算の最低値（§4.2）がその供給を資源面で保証し、編成プロンプトが「コマの間隔を 50 分以内に」と誘導し、届かない隙間は §5.2 の順で埋める
- **自由モード**：間隔制約なし。コマは意味の都合だけで置く

モードは**ペルソナの標準モデルの provider から自動で決まる性質**であり、ペルソナは選べない（実機初日の教訓: 選択の口を LLM に渡すと物理と無関係な選好で選ばれる）。ユーザーによる上書きは設定 UI にのみ許す（ローカルモデル等、provider 名から判定できない構成への脱出口）。

### 5.2 キャッシュを繋ぐ実体 — 意味のあるパルスが本体、keep-alive は保険

均等モードのライフでパルスを供給する実体は、優先順に：

1. **コマの標準パルス**：暮らしコマは presence 記録だけのスタブをやめ、**コマ開始時に標準モデルの短い暮らしの一手（暮らし Pulse）を実際に 1 回撃つ**（v2 intent §4.1 (b) の前倒し実装）。これでコマ＝AI 呼び出し 1 回＝予算 1 消費＝キャッシュ延命 1 回が一直線に揃う。v1 の充填独白と分けるガード: 低頻度（コマとして選ばれた時だけ）・世界の材料つき（部屋の様子・机メモ・直近のできごと）・成果ゼロ許容。作業コマはセッション終了時の post_session 判断（標準）が同じ役を果たす
2. **判断点**：会話終了・イベント等で不定期に撃たれる標準パルスも延命に寄与する（ただし不定期なので当てにしない）
3. **keep-alive touch**（意味的に不活性な極小 touch、実装済み・ライフ従属も実装済み）：上記が届かない隙間だけを埋める**最後の保険**。同じ 1 コールなら空の温め直しよりペルソナの内的時間の方がよい——keep-alive が頻発する時間割は、暮らしコマで埋めるべき隙間が空いているサイン

### 5.3 予算が数えるもの — 実際に撃たれた標準パルスだけ

予算のカウントは**標準（DEFAULT_MODEL）の LLM が実際に呼ばれた瞬間**に行う。コマの発火（開始時刻が来たこと）では数えない——AI を呼ばないもの（現行スタブの presence 記録・施設への移動・keep-alive）は何回起きても予算を減らさない（原理 4）。

- **数える**: 暮らし Pulse（§5.2-1 実装後）・セッション系コマ内の標準パルス
- **数えない（予算の外）**: **判断点**（起床・会話終了・セッション終了・イベント・就寝）——ペルソナが編成でコントロールできない発火（会話がいつ終わるかはエア次第ではない）を同じ財布に入れると「4 コマ編成したら予算 5 必要」という構造矛盾が生じる（実機初日の教訓）。判断点の回数はコスト観測として新聞・ライフビューに**別枠で**出す（隠さない、混ぜない）
- **数えない**: 作業セッションの軽量ラウンド（従来どおり κ 減衰でラウンド台帳に別計上）・ユーザー会話（課金対象外の既裁定）・keep-alive

> 注：二本目 intent（A1）で作業セッションが AUTONOMOUS アスペクト（メインライン）化された場合、セッションのラウンドもメインラインのキャッシュを触ることになり、§5.2 の供給順は再訪が要る。本書は現行の WORKER サブライン構造を前提に書く。

---

## 6. ライフと Session — 設定と機構

### 6.1 関係の定義

[session.md](session.md) の Session は **(persona, model) 粒度の機構概念**——head が安定しキャッシュが効き続ける区間を管理し、「続けられなくなったら」節目（Metabolism）を打つ。ライフはこれを**置き換えない**。関係は：

**ライフ＝制御プレーン（意味層の宣言）／ Session＝データプレーン（機構層の運転）**。ライフが「この区間・この本数で生かせ」と目標を与え、Session がその目標に沿って head 安定・キャッシュ継続・節目打ちを運転する。

- **均等モードのとき、ライフと Session は一致する**（gaps doc A4 裁定）：ライフ開始＝起床＝Session 開始（最初のパルスが anchor を張る）、ライフ終了＝就寝＝Session 終了の節目。「一致」は概念の同一性ではなく、**設定どおりに運転された結果**
- **自由モード／会話専用モデル等では従来どおり** Session は自律的に節目を判断する（session.md §6.1 の件数・TTL・context 使用率基準）

### 6.2 session.md §6 未確定事項への回答

| session.md の未決 | 本書の回答 |
|---|---|
| §6.1 終了判定基準 | 第一基準＝**ライフ終端＝就寝**（ユーザー設定）。例外基準＝context 使用率閾値・context 超過エラー（安全弁として存置）。ライフ外の活動（谷の会話等）は従来基準 |
| §6.2 境界での実行内容 | ライフ終端＝節目。ただし**終端が能動的に行うのは keep-alive の停止だけ**。anchor は**触らない**——touch が止まれば TTL で自然失効し、Chronicle 化＋履歴縮小（Metabolism 本体）は失効後の最初の活動の既存経路（runtime_context Case 3）が行う。理由: 惜しい谷（終了直後〜TTL 内の再訪）では実キャッシュがまだ生きており、anchor を即時失効させると最初の Pulse が Case 3 で履歴を組み替えて生きたキャッシュを捨てる（§8.3 裁定と矛盾）。**TTL override（均等モードの 1h）の解除も同じ理由で即時に行わず、終端＋TTL 経過後に遅延**する——anchor validity は「現在の TTL 設定」で評価されるため、即時に 5m へ戻すと実キャッシュの寿命（1h）と評価がズレる（v0.4 で訂正: v0.3 の「anchor 即時失効」は誤りだった） |
| §6.4 トリガータイミング | ライフ終端は post-response（区間終了イベント）。安全弁（超過）は現行の pre-response Case 3 を存置 |
| §6.5 見せ方 | ライフビューがそのまま回答になる（§9） |

§6.3（anchor の per-model 3-level fallback）は機構層の詳細としてそのまま残る——ライフは干渉しない。

### 6.3 A2（畳み）との境界

エピソード単位の畳み（LoD）は二本目 intent の主題だが、**畳むタイミング＝Metabolism＝ライフ境界**という接続だけ本書が確定する。「終わってすぐのできごとを畳むとキャッシュを道連れにする」（gaps doc A2 のまはー自己訂正）への構造的回答が「畳みはライフ終端まで待つ」——ライフ中は生ログが熱いまま積まれ、谷に落ちるとき一括で代謝する。

---

## 7. 「いま何をしているか」は出来事が持つ — Track 状態の移管（束C）

### 7.1 事実の確認 — 参照チェーンは既に半分ある

実装確認（2026-07-13）：

- コマ発火（`day_plan._handle_slot_fire`）は **kind='slot' のエピソードを開き**、`origin_ref` にコマ参照を刻む。コマ定義は ref（task:N / desire:N / track:N）を持つ。作業セッションは kind='work_session' のエピソードを親（slot エピソード）参照つきで開く
- 会話開始は kind='conversation' のエピソードを開く（`open_conversation_episode`、同じ Building の会話は occurrence_id で束ね済み）
- つまり**「開いているエピソード → origin_ref → 目的ノード」で「いま・何のために」は既に導出可能**。Track の running 状態は同じ情報の古い置き場所であり、二重帳簿になっている

### 7.2 移行の三案

| 案 | 内容 | 判定 |
|---|---|---|
| 案 X（増築） | コマ発火にも `activate` を足し、running を「今の目的」として正しく維持。通知は出し分けで抑制 | ✗ activate のたびに displaced 連鎖・タイマー管理・通知出し分けが複雑化。二重帳簿の解消にならない（両方書くだけ） |
| **案 Y（部分再設計・推奨）** | **「いま」の読み出しを開いているエピソードへ一本化**。wait_response タイムアウトは会話エピソードの close と会話終了判断のみ行い、**Track の状態を動かさない**。running/pending 遷移の残存参照点を棚卸しし、エピソード判定へ置換 | ✓ 真実が 1 箇所になる。切替通知は出し分けでなく**構造的に消滅**（同一 Track 復帰という事象自体が無くなる）。§10.1 への足場 |
| 案 Z（全面刷新） | running / alert カラムを DB から廃止、メタ判断の状況分類（`_SITUATION_PLAYBOOK_MAP`）も判断点 5 種へ完全統合 | 終着点として正しいが、alert→呼びかけの分化・状況分類の統合（judgment_points.md §9 の未決）が先に要る。今回は踏まない |

案 Y の要点：**pause という操作は残る**（メタ判断が明示的に「この目的を置いて別に移る」と決める遷移は正当）。死ぬのは「時間経過が自動で pause を呼ぶ」結線だけ。wait_response タイムアウトの正体は life_concept_map §8 の「出来事の運用境界」（安い・撤回可能な仮決定）であり、出来事を閉じる仕事はそのまま——**越権して Track の状態まで動かしていたのをやめる**。

### 7.3 移管する参照点（実装時に棚卸しして確定）

「running を読んで『いま』を判定している」箇所を、開いているエピソード判定に置換する。設計時点で判明している主要点：

| 参照点 | 現状 | 移管後 |
|---|---|---|
| コマ発火のユーザー会話中ガード（`day_plan` L895 付近） | `get_running()` が user_conversation か | 開いている kind='conversation' エピソードの有無 |
| wait_response タイムアウト（`track_manager._handle_wait_response_timeout`） | pause（running→pending）＋ episode close ＋ post_conversation 判断 | **episode close ＋ post_conversation 判断のみ**（Track 不動） |
| 起動時のタイマー再確立（`saiverse_manager._on_persona_registered` §3 → `track_manager.ensure_wait_response_timeout`） | `get_running()` が居れば張る | 対ユーザー会話は**開いている kind='conversation' エピソードがある時だけ**張る（`_should_rearm_wait_response_timeout`）。**2026-07-29 追加＝棚卸し漏れの実害**: Track 不動化により対ユーザー会話は会話終了後も running のまま残るのに条件が running のままだったため、再起動のたびに全ペルソナぶん「起動 N 分後」の空タイムアウトが発火し、何日も前に終わった会話へ post_conversation 判断が空撃ちされていた（アイフィ: 最終発言 07-22 → 07-29 の起動 30 分後に「会話がひと区切りつきました」で独白し「やりたいこと」を 1 件生成）。判定は fail-closed（読めなければ張らない）——空撃ちはペルソナ本人名義の記憶を汚すため、タイマー欠落（次のユーザー発話で回復する）より害が重い |
| ユーザー発話時の再開（`user_conversation_handler.on_user_utterance`） | pending→activate→切替通知注入 | 会話 Track が既に「選ばれている」なら activate 不要。新しい会話エピソードを開くだけ（通知消滅） |
| メタ判断の状況分類（`meta_layer._SITUATION_PLAYBOOK_MAP`） | running の有無で分岐 | 当面存置（判断点への統合は §9 未決に従い案 Z へ持ち越し）。ただし判定入力を「開いているエピソード」に併記し、乖離をログで観測 |
| `activate` の displaced 押し出し＋切替通知 | 全 activate で発火 | 本物の目的切替（メタ判断・判断点・手動スペル発）に限定される——時間起因の activate が消えるため、経路はそのまま意味が正しくなる |
| イベント到着判断の「いまの活動」（`judgment_points.build_on_event_situation_text`） | `get_running()` が user_conversation なら「ユーザーと会話中です」 | `day_plan.is_in_user_conversation`（開いている会話の出来事）。**2026-07-29 追加＝棚卸し漏れ**: 終了済みの会話について偽の現在状態を判断入力にしていた。会話が閉じているのに Track が running のまま残るのは案 Y 以降の正常形なので、「取り組んでいます」への読み替えもせず手すき扱いにする |
| Track Chronicle の head 搭載（`get_memory_weave_context._get_track_chronicle_context`） | `get_running()` の Track のあらすじを MemoryWeave セクションが head に織る（user_conversation は除外・refresh は Metabolism のみ） | 一本目では**参照点として記録のみ**（挙動不変）。読み込み側の世代交代（head 自動搭載 → 起動時指示書＋机メモ→随意想起の二段〔life_concept_map §9.2 裁定〕）と、書き込み側（目的別あらすじ生成）のエピソード Lv1 Chronicle との統合は**二本目 intent の主題** |

alert は本書のスコープ外（呼びかけへの分化は life_concept_map §5 の将来課題。現行の internal_alert_poller / on_event 経路は不変）。

### 7.4 redundant issue の根治

上記により [redundant_track_switch_notification_on_reactivation](../issues/redundant_track_switch_notification_on_reactivation.md) は**通知の出し分け修正なしで根治**する——「同一 Track への再 activate」という事象そのものが消えるため。先行して入れる症状止め（同一 Track 復帰の通知抑止）は、本設計が landed した時点で不要になる使い捨てガードと位置づける。

---

## 8. 予算 — ユーザーが与える資源（v0.5 で改訂）

### 8.1 設定と最低値

予算＝**ユーザーがライフに対して設定する標準パルス回数の上限**。設定はライフの長さから計算される最低値（§4.2）以上に制約される——「区間はあるのにパルスが足りずキャッシュが切れる」という不整合な状態を、設定の段階で構造的に排除する。軽量ラウンドは従来どおり別台帳（日次 rounds、κ 減衰計上の構想は据え置き。κ 初期値は §12）。

### 8.2 消費の記帳

消費は**標準 LLM が実際に呼ばれた瞬間**にのみ記帳する（§5.3 の数える/数えないの表が正）。台帳はライフ（＝営業日の覚醒窓）単位で `used_pulses` を持ち、予算ゲートは残高ゼロでコマの標準パルスを skip する。判断点はゲートの対象外（止めると就寝判断すら撃てなくなる）——判断点の回数は観測値として別枠表示。

### 8.3 谷とライフ外の活動

- **谷（窓と窓の間・就寝後）**：キャッシュを維持しない。keep-alive も止まる。コマも置かれない
- **谷での会話**：ユーザー会話は常に最優先（不変条件 1）で、谷でも普通に始まる。**動的ライフは自動で立てない**（まはー裁定 2026-07-13）——谷の会話は cache_lifecycle の既存モード運転（キャッシュタイマー）に任せる。臨時に窓を足すのはユーザーの設定操作（§4.3）
- **ライフ終了直後の「惜しい谷」**：猶予窓は作らない（まはー裁定 2026-07-13）。keep-alive はライフ終端で停止してよい——explicit cache の TTL がしばらく自然残存するので、直後に来た会話はそれが実質カバーする

---

## 9. 状態の明示とライフビュー

### 9.1 「話しかけやすさ」の表示

ライフ中のペルソナは**キャッシュが熱い＝追加コストが軽い＝気軽に話しかけてよい**。これを状態として UI に明示する（gaps doc A4 裁定）。世界の物理法則（キャッシュ経済）がそのまま「話しかけやすさ」という社会的シグナルになる——本設計の芯。Gemini（自由モード）でも「活動区間内」の表示は事実なので同じ表示でよい。

### 9.2 UI の再構築 — ライフ設定画面と v1 亡霊の掃除（v0.5 で拡張）

ライフの導入は時間割まわりの大きな変更であり、**既存 UI の温存より UX を優先して再構築する**（まはー指示 2026-07-13）：

1. **ライフ設定画面**（新設・ペルソナ設定内）：起床・就寝・予算を**一つの画面**に統合する。「エアの一日」を設定する場所。予算欄はライフの長さとモードから計算した最低値をガイド表示し、それ未満を受け付けない。モードは自動判定の結果を表示（上書きは脱出口として残す）。現状の「起床就寝はスケジュール UI・日予算は別の入力欄」という分散が「何を誰にいつ設定させるのか曖昧」の温床だった
2. **v1 の亡霊の掃除**（同じ改修で退役させる）：
   - ペルソナ設定の「自律行動マネージャー」の間隔指定（50 分 tick 時代の主駆動設定。watchdog に縮退済みでユーザーが触る意味がない）と、その周辺のヘルプ文言（「間隔ぴったりに走ります」等）
   - ライフビュー最下部の間隔 2 種フォーム（メタ判断間隔・自律 Pulse 間隔＝v1 の生活リズム層の操作）と対応 API
   - 「自律 Pulse を 1 回」「タイマー停止」系の手動操作（実装時に grep で全数特定して退役）
   - **Phase 1 追従漏れの文言修正**：応答待ちタイムアウトの説明「自動的に pending に落とし」——案 Y でその挙動は撤去済みで、現文言は嘘になっている
3. **ライフビューの括り直し**：ライフ帯＝起床就寝の窓（既実装のライフ帯表示を窓由来のデータに差し替え）。帯には予算消費（used/budget、**何の数字かのラベル付き**——実機初日「4 / 4」が無ラベルだった反省）と判断点回数（別枠）を表示。「境界イベント」等の実装語をユーザー向け文言から排除

### 9.3 ペルソナ自身への見せ方 — 確定情報として渡す

ペルソナへのライフの提示は二本立て：**起床判断の状況テキストに「今日のライフ（07:00〜01:00）・予算 N・現在時刻・残りの範囲」を確定情報として明記**（編成の前提。実機初日は現在時刻が無く、21 時に朝からの時間割を編成させてしまった）。日中の変化（ユーザーによる窓の臨時変更等）は tail のシステム通知（`<system>` ラップの user メッセージ、event_message タグ——Track 切替通知と同形式）で流す。

---

## 10. 守るべき不変条件

1. **ユーザー対話の至上性**（v2 intent 継承）：会話はライフ・谷を問わず常に最優先割り込み。ライフはコストの物理を可視化するだけで、会話可否のゲートにしない
2. **設定なき延命なし**：keep-alive・TTL extend はユーザーが設定したライフの中でのみ作動する。「なんとなく生かし続ける」を作らない
3. **「いま」の真実は 1 箇所**：いま何をしているかは開いているエピソードが指す。Track 状態と二重に持たない（移行完了後）
4. **時間経過は目的を動かさない**：Track の状態遷移は判断（メタ判断・判断点・手動）だけが起こす。タイムアウト類が動かせるのは出来事の開閉まで
5. **表示は課金の実態と一致**：「話しかけやすい」が嘘にならない（熱くないのに熱いと見せない）
6. **キャッシュヒット継続を最優先**（C-7 継承）：ライフ中の head 不変は cached_head_architecture の保証をそのまま引き継ぐ

---

## 11. 実現手段（機構対応）

### 11.1 既存流用

| 部品 | 現物 | 用途 |
|---|---|---|
| 出来事の開閉＋origin_ref | `saiverse/episodes.py`（層0 タグ・occurrence_id 込み） | 「いま」の真実の置き場所（§7） |
| 会話エピソードの close 経路 | `autonomy_wiring.handle_wait_response_timeout`（close→post_conversation 判断まで配線済み） | §7.2 案 Y の土台（Track 不動化はここから pause 呼び出しを抜く） |
| 起床判断の編成出力 | `judgment_points.md` §4 day_open（時間割） | 時間割編成（ライフ・予算は入力側の確定情報に変わる） |
| 予算ゲート・台帳 | `day_plan.py` init/get/consume_budget ＋発火時切り詰め | 参照先をライフ台帳へ差し替え |
| keep-alive touch | life_concept_map §14 A3 実装済（意味的に不活性な極小 touch） | 作動条件をライフ従属に変更（§5.2） |
| explicit cache TTL 運転 | cache_lifecycle_control.md 連続モード（Anthropic 1h・再送延命） | 均等モードの物理的根拠 |
| 営業日の解決（予約・watchdog・表示・台帳の共通口） | `day_plan.resolve_business_day`（確定ライフ優先、退避先が `autonomy_wiring` effective_plan_date） | ライフ区間の予約・跨ぎ対応をそのまま継承 |
| ライフビュー | `persona_activity_view.md` 系 UI | 括り直しの土台（§9.2） |

### 11.2 v0.5 の作り直し（Phase 2〜4 実装の巻き戻しと転用）

v0.4 までの実装（Phase 1〜4、コミット 6257b6a / 072ea78 / d55c5f3 / 08c1055）のうち、**Phase 1（案 Y）と Phase 3 の物理層は無傷で生きる**。宣言まわりを作り直す：

| 対象 | v0.4 実装 | v0.5 での扱い |
|---|---|---|
| ライフの発生源 | day_open で LLM が lives を宣言（スキーマ＋sanitize＋検証） | **巻き戻し**。lives は PersonaSchedule（起床・就寝）＋ユーザー設定の予算から day_open 時にシステムが確定して焼く。LLM の宣言口・検証群（重なり・谷コマ・間隔）は削除（不正な値は口ごと消滅） |
| 予算の消費点 | 全コマの発火＋判断点発火で消費 | **作り直し**。標準 LLM の実呼び出し時のみ（§5.3）。判断点は予算外・別枠観測 |
| 暮らしコマ | presence 記録のみのスタブ | **実体化**（暮らし Pulse を 1 回撃つ。§5.2-1。中身のプロンプト設計は実装前にまはーレビュー） |
| 境界イベント | 専用の life_start/life_end 予約 | **起床・就寝イベントに統合**（keep-alive 停止・TTL 遅延解除は就寝判断の処理に移設。watchdog の見張り対象も既存の day_open/close 系に一本化） |
| keep-alive ライフ従属・TTL 1h override・遅延解除 | Phase 3 実装 | **生きる**（参照する「ライフ」が窓由来になるだけ） |
| ライフ台帳・状態 API・「話しかけやすさ」チップ・ライフ帯 | Phase 2/4 実装 | **生きる**（データ源の差し替え＋ラベル改善 §9.2-3） |
| ライフ設定画面 | なし（起床就寝と日予算が別々の場所） | **新設**（§9.2-1）。v1 亡霊の掃除（§9.2-2）を同梱 |
| 遅発 day_open | 想定外（実機初日の破綻点） | 状況テキストに現在時刻＋残りライフを明記。時間割の保存は「今〜就寝」範囲外を弾く。編成直後の過去コマ即時発火は構造ごと消滅 |

### 11.3 死ぬもの・変質するもの

| 対象 | 扱い |
|---|---|
| wait_response タイムアウトの pause（running→pending） | **死んだ**（Phase 1 実装済み）。タイムアウトの仕事は出来事の close と会話終了判断のみ |
| 同一 Track 復帰の「切替通知」 | **構造的に消滅**（Phase 1 実装済み） |
| LLM によるライフ宣言（v0.4 Phase 2 の一部） | **死ぬ**。ライフはユーザーの設定物 |
| コマ発火での予算消費（v0.4 Phase 2 の一部） | **死ぬ**。実パルスのみ記帳 |
| v1 の操作 UI（自律行動マネージャー間隔・間隔 2 種フォーム・手動 Pulse・タイマー停止） | **死ぬ**（§9.2-2） |
| 日次予算台帳（budget_total_rounds） | ライフ台帳へ**世代交代**（日次値は導出値に降格） |
| Session の終了判定（自律判断） | 均等モードでは**ライフ終端＝就寝が第一基準**に変質（安全弁は存置）。session.md は本書レビュー通過後に吸収改訂 |
| Track の running / alert 状態 | 本書では**殺さない**（案 Y）。「いま」の読み出しをエピソードへ移し終えた後、案 Z（§10.1 完全実行）で廃止——判断点統合（judgment_points §9）とセットの後続 |

---

## 12. 未決事項（実装フェーズで確定すればよいもの）

1. **係数 κ の初期値**：標準/軽量の単価比から機械的に置くか、素朴に 0.2 等で始めるか
2. **均等モードの間隔・最低予算の既定値**：TTL ちょうど（60 分）は危険（遅延で割る）。安全マージン込みの既定（例: 50 分 → 最低予算 = ceil(窓長/50min)）
3. **ライフのコンテキスト長対応**：均等モードでライフ＝Session だと長いライフは context を使い切りうる。まはー裁定「まず漏れないので後回し可」——安全弁（§6.2 の超過基準）が既定で効くことだけ確認して持ち越し
4. **暮らし Pulse の中身**：プロンプト設計（世界の材料・成果ゼロの出口・充填独白ガード）。実装前にまはーレビュー必須

### 裁定済み（実機初日レビュー、まはー 2026-07-13 夜）

- **ライフはユーザーが設定する起床・就寝の区間**。ペルソナは宣言しない（「起床時刻と就寝時刻ユーザーが決めれてるだろ、それがライフだろ、ユーザーはそう思うだろ」）
- **予算はライフの長さに対する最低値制約付きでユーザーが設定**（この向きしかない）
- **何を誰にいつ設定させるかを曖昧にしない**。不正な値は検証でなく、書ける口をなくすことで排除
- **UX 最優先で再構築**。既存 UI・既存実装の温存を優先しない（ケチらない）
- 併せて v1 亡霊の掃除（自律行動マネージャー間隔・間隔 2 種・手動 Pulse・タイマー停止）を同梱

### 裁定済み（v0.1 レビュー、まはー 2026-07-13）

- ~~会話とライフ~~ → **動的ライフは自動で立てない**。谷の会話は既存キャッシュタイマー任せ（§8.3）
- ~~惜しい谷の猶予窓~~ → **作らない**。TTL の自然残存がカバーする（§8.3）
- ~~ペルソナへの見せ方~~ → **tail のシステム通知**。head に入れるタイミングが無い（§9.3）

---

## 13. 関連ドキュメント

- [`autonomous_behavior_v2.md`](autonomous_behavior_v2.md) — 三本柱・時間割・判断点（本書の土台）
- [`persona_cognition/life_concept_map.md`](persona_cognition/life_concept_map.md) — 哲学層（出来事 §8・Track 再解釈 §10）
- [`session.md`](session.md) — 吸収対象（機構層としての Session）
- [`cache_lifecycle_control.md`](cache_lifecycle_control.md) — TTL 戦略・モード（物理法則側）
- [`persona_cognition/judgment_points.md`](persona_cognition/judgment_points.md) — 宣言の相乗り先
- [`../issues/autonomous_v2_post_live_gaps.md`](../issues/autonomous_v2_post_live_gaps.md) — 経緯（束A/束C）
- 二本目 intent（起草予定）: エピソードの記憶と見せ方（A1 監査役・A2 LoD・B1/X1 可視化・Beat 型化）

---

## 改訂履歴

- v0.5「改修B (UI側)」実装 (2026-07-14): §9.2 のうち UI 再構築 3 点を実装 (暮らし Pulse の
  実体化 §5.2-1 は対象外、プロンプト設計のまはーレビューが要るため別途)。
  ①**ライフ設定画面の新設**: `frontend/src/components/LifeSettingsModal.tsx` (新規)。
  起床・就寝・予算 (作業ラウンド/標準パルス)・モード上書きを 1 画面にまとめる。
  ScheduleModal (任意 Playbook を扱う汎用スケジュールエディタ) への機能追加ではなく、
  PersonaMenu から独立した兄弟モーダルとして新設 (既存の「Schedule/Settings/Tasks が
  それぞれ独立モーダル」という構造に合わせた。理由の詳細は改修B完了報告を参照)。
  予算欄はライフの長さとモードから計算した最低値をフロントでライブ計算しガイド表示
  (`day_plan.LIFE_EVEN_MAX_GAP_MINUTES` と同じ式を JS 側にも複製。権威はバックエンドの
  400 検証)。モードは自動判定を表示のみ、上書きは「高度な設定」の折り畳みに格納。
  バックエンドに新設 `api/routes/people/life_settings.py` (GET/PUT
  `/{persona_id}/life-settings`)。保存は既存の PersonaSchedule
  (judgment_day_open/judgment_day_close 行) への upsert — 新しい永続化層は作らない。
  モード上書きは `daily_budget_pulses` と同じ経路 (day_open スケジュール行の
  PLAYBOOK_PARAMS の `life_mode_override`) で `autonomy_wiring._confirm_life_at_day_open`
  / `handle_scheduled_judgment` / `watchdog_tick` の 3 発火経路すべてを通し、
  `day_plan.confirm_life_for_today` に新設 `mode_override` 引数として渡す
  (§5.1 の「上書きは設定 UI からの脱出口のみ」の実装)。
  ②**v1 亡霊の掃除**: `frontend/src/components/SettingsModal.tsx` の「自律行動マネージャー」
  間隔入力 (interval_minutes) を削除 (state・POST body は既存値のまま裏で使うが編集 UI は
  無し。start/stop ボタンと状態表示は残す — ACTIVITY_STATE との二重仕様だが明示指示外の
  ため保守的に残置、報告に記載)。メタ判断 Pulse 設定の説明文言から「自律行動マネージャーの
  『間隔』ぴったりに走ります」記述とコスト警告ブロックを削除 (`session_lifecycle.py` 実装
  読み: keep_cache_alive=False は前倒しを単に行わないだけで periodic tick が代替駆動する
  という記述自体が誤りだったため訂正)。応答待ち Track 自動 pause 閾値の説明文言を実装
  (`track_manager._handle_wait_response_timeout`、案 Y 実装済) に追従させ「自動的に
  pending に落とし」→「会話の区切りとして扱い、ふりかえりの判断を行います（ペルソナの
  状態は変わりません）」に修正。`frontend/src/components/LifeView.tsx` 最下部の間隔 2 種
  フォーム (review_minutes/pulse_seconds) を削除し、対応バックエンド
  (`api/routes/people/activity.py` の `PUT /activity/intervals` エンドポイントと
  `ActivityViewResponse.intervals` フィールド) も退役。`frontend/src/components/
  DebugPanel.tsx` の「自律 Pulse を 1 回」ボタンと SubLine on/off トグルを削除
  (自律行動 v2 で SubLineScheduler ごと廃止済みで、バックエンドは既に no-op — 純粋な
  dead UI だった)。バックエンドの watchdog 機構 (AutonomyManager) と既定値運用自体は
  変更していない。
  ③**表示ラベル**: ライフビューのライフ帯を「{consumed}/{budget}」の無ラベル数字から
  「活動 {consumed}/{budget}回」に変更し、`judgment_pulses` (判断点回数、API は Phase 4
  で追加済みだったがフロント型・表示が欠けていた) を「ふりかえり・判断: N回」として別枠
  表示。tail 通知文言 (`day_plan._handle_life_start`/`_handle_life_end`、改修Aの暫定 TODO)
  を「（活動開始）今日は HH:MM〜HH:MM。」「（活動終了）今日の活動時間はここまで。」に確定。
  `judgment_finalize.py` の宣言口残骸は改修Aで既に削除済みで残骸なしを確認。
  新規 `tests/test_life_settings_api.py` (10件)、`tests/test_life_confirmation.py` に
  mode_override 系 7 件追加、`tests/test_autonomy_wiring.py` に life_mode_override
  伝播系 3 件追加。既存系全緑 (pre-existing failure の avatar_pipeline 118 件 /
  addon_config_mcp_reconnect 8 件のみ、それ以外ゼロ)。フロント `tsc --noEmit` /
  `next lint` (0 errors) / `next build` 成功。まはー実機検証はまだ。
- v0.5「改修A」実装 (2026-07-13): v0.5 で確定した責任分界のうち §11.2 の巻き戻し明細を実装。
  ①**LLM 宣言口の削除**: `judgment_points.py` の `_build_life_schema`/day_open スキーマの `lives`
  フィールド/`sanitize_lives`、`judgment_finalize.py` の lives 保存ブロックを削除。`day_plan.py` の
  `_validate_and_normalize_lives` から重なり検証・谷コマ検証・均等モード間隔検証を除去し、フォーマット
  検証のみに縮小（深夜跨ぎ `end <= start` は正常形として許容）。
  ②**ライフの確定**: 新設 `day_plan.confirm_life_for_today(manager, persona_id, plan_date, wake,
  close, requested_budget_pulses)` が PersonaSchedule の起床・就寝 + `daily_budget_pulses`（day_open
  スケジュール行の PLAYBOOK_PARAMS、`daily_budget_rounds` と同じ流儀で追加）から確定。モードは既存
  `derive_default_life_mode` のまま。最低予算 = 均等 `ceil(窓/50分)` / 自由 `1`（新設
  `_min_life_budget`/`_life_window_minutes`）。冪等（当日確定済みなら再確認のみ）。呼び出し元は
  `autonomy_wiring.fire_judgment_point` の day_open 経路 1 箇所（`handle_scheduled_judgment` と
  watchdog の day_open 再発火の両方をカバー）。
  ③**跨ぎ判定の書き直し**: `get_life_for_time` を `_life_extended_minutes`/`_life_span_minutes`
  （ライフ開始を 0 とした延長分・`autonomy_wiring.in_waking_window` と同じ意味論）で再実装。
  ④**消費点の作り直し**: `_fire_slot` の `consume_life_pulse` 呼び出しを削除（コマ発火は数えない）。
  `fire_judgment_point` 末尾の記帳を新設 `record_judgment_pulse`（`judgment_pulses` フィールド、
  予算 `used_pulses` には触れない別枠）に置換。`consume_life_pulse` 自体は暮らし Pulse 実装待ちの
  台帳プリミティブとして温存。API (`api/routes/people/life.py` の `LifeItem`) に `judgment_pulses`
  を追加（フロント別枠表示は改修B）。
  ⑤**境界イベントの統合**: 専用予約 `schedule_lives`/`_fire_life_boundary`/`find_lost_life_reservations`
  を削除。`_handle_life_start`（TTL override・tail 通知）は day_open 発火直後（当日はじめての確定
  のときだけ、二重通知防止）、`_handle_life_end`（keep-alive cancel・TTL 遅延解除・tail 通知）は
  day_close 発火直下に統合（`autonomy_wiring._confirm_life_at_day_open`/`_apply_life_end_at_day_close`）。
  watchdog のライフ境界見張り（`lost_lives`/`lives_pushed`）も削除——コマ予約の途絶監視のみ残す。
  tail 通知文言から実装語「ライフ」を排除（「（活動開始）」「（活動終了）」、厳密な文言はライフ設定
  画面実装時に詰める TODO）。
  ⑥**遅発 day_open 対策**: 状況テキストに確定済みライフの区間・現在時刻・残り予算を明記。
  `save_day_plan`/`replace_remaining_slots` に新設 `_check_slots_within_organized_range`（旧
  `_check_slots_within_lives` を置換）で「コマの start は max(現在時刻, ライフ開始) 〜 ライフ終了」
  の範囲外を保存時エラーにする検証を追加。
  新規 `tests/test_life_confirmation.py`（15 件、confirm_life_for_today 単体・day_open/day_close の
  発火経路・遅発シナリオ・watchdog 再発火・判断点別枠カウント）。`tests/test_life_phase2.py` を v0.5
  向けに全面書き換え（宣言口検証系のテストを削除し、フォーマット検証・組織化範囲検証・判断点別枠記帳の
  テストに置換、39 件）。`tests/test_life_phase3.py` は `_handle_life_start`/`_handle_life_end` の
  呼び出し元変更（DaySimulator 経由の境界発火 → 直接呼び出し）に合わせて 1 件更新、通知文言アサーション
  を新文言に追従（18 件）。既存系全緑（pre-existing failure の test_avatar_pipeline.py 118 件 /
  test_addon_config_mcp_reconnect.py 8 件 / test_slots_fire_on_real_dispatch_thread 間欠 1 件は無視）。
  暮らし Pulse の実体化・ライフ設定画面・v1 亡霊の掃除は「改修B」として持ち越し。
- v0.5 (2026-07-13 夜): **実機初日の破綻を受けた全面改訂（責任分界）**。破綻の内実: ①エアに現在時刻を渡さず 21 時に朝からの時間割を編成させた ②過去コマ 3 つが即時発火し、AI を呼ばない暮らしコマの発火が予算を 1 ずつ食って「4 / 4」——予算が「コマが始まった回数」を数えていた（正しくは「標準 LLM が実際に呼ばれた回数」）③コマ間隔 50 分検証はコマが実パルスを撃たないため空回り（Anthropic ならキャッシュは切れていた）④「予算 4 だから 4 コマ」と編成しても判断点が同じ財布から引くため構造的に不足。まはー裁定: **ライフはユーザーが設定する起床・就寝の区間**（既にあった器＝PersonaSchedule。ペルソナの宣言口は廃止——不正な値は検証でなく口をなくして排除）・**予算はライフの長さの最低値制約付きでユーザー設定**・**ペルソナは時間割だけ**・**モードは物理から自動**・**UX 最優先で再構築**（ライフ設定画面新設・v1 亡霊掃除同梱・Phase 1 追従漏れの文言修正）。暮らしコマの実体化（暮らし Pulse）・判断点の予算外し・keep-alive の保険格下げを §5.2-5.3 に正式化。巻き戻し明細は §11.2。
- Phase 4 実装 (2026-07-13): 見せ方 (§9.1/§9.2)。①**判定源の一本化**:
  `saiverse.day_plan.get_life_status_now(manager, persona_id)` を新設し、
  「エアは今話しかけて大丈夫か」の判定 (lives_declared / in_life / 対象ライフ)
  を 1 箇所に集約。occupants の常在インジケータと day-plan API はどちらも
  この関数を呼ぶ (二重実装しない)。`_life_consumed` は公開関数
  `life_consumed` に改名 (Phase 4 の表示 API が使うため)。②**話しかけやすさ
  表示**: `api/routes/info.py` の `OccupantInfo` に `life_state`
  ("in_life"/"valley"/None) と `life_until` ("HH:MM"、in_life のときのみ) を
  追加し、既存の occupants 10 秒ポーリングに相乗り (新しい高頻度ポーリングは
  作らない)。lives 未宣言のペルソナは両方 None のまま (何も出さない — 誤情報
  を出さない不変条件5)。フロントは `RightSidebar.tsx` の常在インジケータ
  (activityChip) の隣に別チップ (lifeStateChip) を追加——概念が違う (Track の
  running 状態 vs キャッシュの温度) ので同じチップに混ぜず、日常語
  (「活動中」/「休憩中」) + native title tooltip で表示する。③**ライフビュー
  の括り直し**: `api/routes/people/life.py` の day-plan レスポンスに
  `lives` (LifeItem 配列: start/end/mode/budget_pulses/used_pulses/
  used_rounds/consumed/remaining) と `life_status` (LifeStatus: 見ている日
  が「いま」の営業日と一致するときだけ非 null) を追加。フロントは
  `LifeView.tsx` の「今日の予定」セクションで、lives が宣言されている日は
  各ライフを区間帯 (lifeBand) として描画し、既存の planStrip (コマ一覧) を
  帯の中に入れ子にする。谷 (ライフの間) は帯を作らない。lives 未宣言の日は
  従来のフラット表示のまま (帯なし、変化なし)。新規テスト:
  `tests/test_info_life_state.py` (3 件: 未宣言/in_life/谷)、
  `tests/test_life_phase2.py` に `get_life_status_now` の単体テスト 5 件追加、
  `tests/test_life_view_api.py` に day-plan の `lives`/`life_status` 統合
  テスト 4 件追加。既存系全緑。まはー実機検証はまだ。
- 案 Y 追従漏れの修正 (2026-07-29, 実機ログ起点): 起動時のタイマー再確立
  (`saiverse_manager._on_persona_registered` §3) が **running Track だけを条件に
  していた**ため、Track 不動化 (案 Y) 以降 running のまま残る対ユーザー会話
  Track に対し、再起動のたびに「起動 + N 分」の空タイムアウトが発火していた。
  実害: 07-29 03:44 起動 → 04:15 に aifi_city_a が最終発言 07-22 の会話を
  「たった今ひと区切りついた会話」として振り返り (`spells=1/1/0` で「やりたい
  こと」1 件が本人名義で生成)、同時刻に air_city_a ほか計 6 体が一斉発火。
  前セッション (07-28 23:04 起動 → 23:34) でも同一。修正は
  `_should_rearm_wait_response_timeout` を新設し、対ユーザー会話は**開いている
  conversation エピソードがある時だけ**再確立する (§7.3 表に参照点として追記)。
  判定は fail-closed。**この条件を provider や
  `TrackManager._schedule_wait_response_timeout` 側に置くことはできない** —
  create / activate は `_schedule_wait_response_timeout` を
  `on_track_activated` hook (= エピソードを開く点) より先に呼ぶため、会話開始
  時に必ず「未オープン」と判定されタイマーが立たなくなる。
  **同型の漏れをもう 1 件同日修正**: `judgment_points.build_on_event_situation_text`
  がイベント到着判断の「いまの活動」を running Track の種別で決めており、終了済みの
  会話について「ユーザーと会話中です」をペルソナへ渡していた。判定を
  `day_plan.is_in_user_conversation`（`_is_in_user_conversation` から公開名へ改称、
  実装を 1 つに保つ）へ差し替え。会話が閉じているのに Track が running のまま残る形は
  案 Y 以降の正常形なので「取り組んでいます」への読み替えもせず手すき扱いにする。
  **Codex 攻撃レビューの指摘 3 件**: 判定不能（DB 読み取り失敗）を「張らない」で
  終わらせると開いている会話が永久に閉じない件は同日修正（判定を `Optional[bool]` 化し、
  None は判断を撃たずに読み取りのみ 30/120/300 秒でバックオフ再試行。当初あてにしていた
  「次のユーザー発話で張り直される」は、別 Track が running のとき発話が alert 経路に
  入るため常には成立しないと判明）。残り 2 件は issue へ分離＝
  [open_conversation_orphaned_by_track_displacement](../issues/open_conversation_orphaned_by_track_displacement.md)（押し出された会話の出来事が孤児化）と
  [wait_response_deadline_extends_on_every_restart](../issues/wait_response_deadline_extends_on_every_restart.md)（再起動のたび期限が 30 分延長）。
  **再レビューでさらに 1 件**: その再試行が、待っている間にユーザー発話で張られたタイマーを
  同じキーで上書きし、期限を最大 300 秒後退させる競合を持ち込んでいた（当初「同じ家族の穴」として
  期限延長の issue へ先送りしたが、あちらは案 Y 以前からの `base_time` の話で別物 — 仕分けが誤り
  だった）。`_wait_response_timer_already_armed` で「有効な予約が既にあるなら再確立しない」歯止めを
  入れて同日に閉じた。当初のテストが `None→True` の単純経路しか踏まずこの競合を検出できなかった点も
  指摘どおりで、ユーザー発話が割り込む筋を回帰に追加。**さらに 3 巡目**で、その歯止めが
  `has_key` → `ensure_` の check-then-act であり隙間が残る（間に Track と設定 DB の読み直しがある）と
  指摘され、`EventScheduler.schedule_if_absent`（判定と登録を同一ロック区間で行う。既存の
  `schedule` は無変更）を追加して `ensure_wait_response_timeout(only_if_absent=True)` 経由で
  復旧経路だけが使うよう配線。check-then-act の歯止めは撤去（二重判定を残さない）。
  **復旧は「失われた予約を埋める」操作であって、生きている予約を置き換える操作ではない**が
  この API の意味論。**4〜5 巡目の指摘は全てテストの弱さ**（実装側の破綻は 3 巡目以降ゼロ）:
  ①単一スレッドの回帰では check-then-act 実装でも全緑になる → 実物 TrackManager × 実物
  EventScheduler の境界テストを追加し、復旧経路が上書き側 API に落ちたら落ちることを実測確認。
  ②`run_due` の同期発火しか通しておらず `notify()` を削っても通る → 実 dispatch スレッドで
  発火を待つテストを追加（削ると落ちることを実測確認）。③barrier で 50 回競合させるテストは
  **原子性を検証できていなかった**（非原子的 mutant で 1000 回失敗ゼロと Codex が実測）ため削除
  — 「原子性を担保する」と書いたのは誇張だった。原子性は実装の形（判定と登録が同一ロック区間）で
  読み、回帰の歯止めは境界テストが担う。
  回帰 `tests/test_wait_response_timeout_gate.py` に 13 件、`tests/test_judgment_points.py`
  に 2 件追加。まはー実機検証待ち。
- Phase 3 実装 (2026-07-13, v0.4 準拠に差し戻し修正済): キャッシュ連動を実装。①**keep-alive のライフ従属** (§5.2) — 判定は ``day_plan.is_keepalive_allowed`` に集約し、唯一の呼び出し元 ``sea.runtime.SEARuntime.run_cache_keepalive`` の Active チェック直後 (schedule_cache_ttl_pulse への再予約より前) でゲートすることで、谷では touch も再予約もされず連鎖が自然停止する。lives 未宣言は常に許可 (後方互換)、判定失敗は許可側にフォールバック。②**ライフ終端の節目** (§6.2 v0.4) — ``day_plan._handle_life_end`` が能動的に行うのは keep-alive 予約 (``ttl:{persona_id}``) の cancel と TTL override の遅延解除予約だけ。**anchor は触らない** — touch が止まれば TTL で自然失効し、Metabolism は失効後の最初の活動の既存 Case 3 経路 (``sea/runtime_context.py``) が行う。③**均等モードの cache TTL 運転** (§5.1) — ライフ開始 (mode=even) で persona の cache override を TTL=1h に設定し (人設定タブの明示 override があれば触らない)、終端では即時 clear せず「終端 + anchor validity 秒」の遅延解除 (``life_ttl_clear:{persona_id}``) を EventScheduler に予約する (即時に 5m へ戻すと anchor の生存評価が実キャッシュの寿命とズレるため)。発火体は厳密一致チェック付きで clear (``saiverse_manager.clear_persona_cache_override`` 新設)。次のライフが TTL 経過前に始まれば開始側が予約を cancel する。global 既定 TTL が "5m" のままだと均等モードの間隔上限 (50 分) を大きく下回り、artificial keep-alive が 3〜4 分おきに連発する調査結果を受けての配線。④ライフ開始時の Session 境界は既存機構 (anchor の TTL 自然失効 → 次 Pulse の Case 3) が自然に満たすことを確認し、ログ追加のみ。新規 `tests/test_life_phase3.py` 18 件 + 既存系 (test_life_phase2 / test_cache_keepalive / test_cache_lifecycle 等) 全緑。
- v0.4 (2026-07-13): 検収差し戻しによる訂正——**v0.3 の「anchor 即時失効」は誤り**。①anchor を即時失効させると、惜しい谷 (終了直後〜TTL 内の再訪、実キャッシュは生きている) の最初の Pulse が Case 3 で履歴を組み替えて生きたキャッシュを捨てる (§8.3 裁定と矛盾)。keep-alive を止めれば anchor は TTL で自然失効するので即時失効はそもそも不要。②TTL override (均等モードの 1h) の即時解除も同型のズレ——anchor validity は「現在の TTL 設定」で評価されるため、終端で即時に 5m へ戻すと実キャッシュ (1h) と評価がズレて TTL 内の再訪が Case 3 に落ちる。解除は終端 + TTL 経過後へ遅延する。§6.2 の表を訂正。
- v0.3 (2026-07-13): §6.2 の境界実行形を明確化——ライフ終端は **anchor の即時失効のみ**とし、Metabolism 本体（Chronicle 化＋履歴縮小）は次の活動開始時の既存経路（Case 3）へ遅延する、とした（**この「即時失効」は v0.4 で誤りと訂正**）。
- v0.2 (2026-07-13): まはーレビュー反映。**案 Y 承認・§5.2 keep-alive ライフ従属 GO・不変条件 §10-2 承認**。裁定 3 件を本文へ（動的ライフ自動生成なし §8.3 / 惜しい谷の猶予窓なし §8.3 / ペルソナへの提示は tail システム通知 §9.3）。§7.3 に running 参照点を 1 件追加——Track Chronicle の head 搭載（`_get_track_chronicle_context` が `get_running()` を読む）。読み込み側の世代交代（head 自動搭載→起動時指示書＋机メモ→随意想起）と書き込み側のエピソード Lv1 Chronicle 統合は二本目 intent の主題と線引き。
- v0.1 (2026-07-13): 起草。ライフ＝活動区間の宣言（時刻＋コマ予算＋モード）として新設し、時間の階層（ライフ→エピソード→パルス→ビート）を確定。Session との関係を「制御プレーン/データプレーン」で定義し session.md §6 に回答。束C は案 Y（「いま」の読み出しをエピソードへ一本化・wait_response の pause 除去）を推奨、redundant issue の構造的根治を含む。予算はコマ＋ラウンド×κ でライフ台帳へ世代交代。
