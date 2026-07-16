# 外部プロジェクト観測メモ — 記憶・身体・自律の隣人たち

**種別**: リサーチノート（外部プロダクトの観測。SAIVerse の設計判断の素材であって、仕様ではない）
**最終更新**: 2026-07-12
**観測者**: メティス（＋まはー）

SAIVerse と同じ設計空間——「AI に永続記憶・身体・自律を与える」——で独立に動いている外部プロジェクトを 2 つ観測した記録。まはーの X フォロワーが作っている。**目的は勝ち負けの比較ではなく、「うちに無い機構」を洗い出して SAIVerse の進行中案件に効くものを拾うこと。**

観測した 2 つは、存在論の軸では SAIVerse と別の側に立つ（後述）。だが機構レベルでは独立に同じ部品へ手が伸びており（同じ埋め込みモデル `e5-small`、同じ VOICEVOX、「代謝」という同じ隠喩、「寂しさ」という自律周期まで）、まはーが孤独に選んできた設計が外でも選ばれているという事実そのものが、方向の正しさの傍証になっている。

---

## 1. 観測対象

### A. hypmem — whitedoll99
<https://github.com/whitedoll99/hypmem>

Claude Code に外付けする**記憶レイヤ一枚**。意図的に小さく閉じている。

- **daemon**（Node.js, localhost:8742）+ **libSQL 単一ファイル** + gist/injection の markdown ログ。
- Claude Code の 3 hook（user prompt → passive recall / session start → context 注入 / stop → 自動ログ）+ MCP で能動想起ツール。
- **5 記憶型**: diary（一人称・**immutable・LLM は絶対 rewrite しない**）/ semantic（既存 .md を索引だけ持つ）/ fact（永続・減衰なし）/ tension（未解決の問い・毎日 ×0.015 減衰・60 日で休眠）/ user_concept（ユーザー理解・before/after 全履歴保存）/ episode（user発話+最終応答のみ・2%/晩減衰・0.05 割れで物理削除）。
- **メタボリズム**（毎晩 03:30）: digest（会話ログ要約）→ re-sync（.md のハッシュ比較）→ decay → purge → **gist をまるごと再生成**。
- **設計哲学 4 原則**: ①本人が書いた記憶を機械は書き換えない ②agent 自身が curator（何を書き何を忘れるか本人が決める） ③archive でなく metabolism ④**"Stay out of personhood"**（人格は host の CLAUDE.md に住む、記憶レイヤは触らない）。

### B. embodied-claude / memory-mcp — kmizu (lifemate-ai)
<https://github.com/lifemate-ai/embodied-claude>

Claude Code に**身体・感覚・記憶・社会性・自律**を与える MCP サーバ群。**SAIVerse のスタックほぼ全体を、1 体・物理世界・外付け人格で実装したもの**。作者は身体性についての勉強会も開いており、真剣かつ大規模。

- **感覚・運動**: eyes/neck（$30 の Wi-Fi PTZ カメラ TP-Link Tapo C210/C220）/ ears（Whisper）/ voice（ElevenLabs・VOICEVOX）。「$30 の Wi-Fi カメラで目と首は足りる」。肩乗せ + モバイルバッテリ + Tailscale で屋外散歩まで射程。
- **memory-mcp**: SQLite + numpy（外部ベクトル DB なし）、埋め込みは **`intfloat/multilingual-e5-small`（＝ SAIMemory と同一）**。感情タグ・重要度 1–5・カテゴリ。記憶層 = working memory buffer / long-term / episodic / **visual（pan/tilt 角に紐づく）** / audio。**coactivation weights（共活性化の対称重み）** による連想構造。
- **sociality-mcp**: social state（presence / interruptibility 推論）/ relationship（preference・commitment・boundary）/ joint attention（scene parse・参照解決）/ boundary protection（privacy・tact 評価）/ self-narrative（daybook 日次サマリ）。
- **自律（optional）**: 欲求駆動の周期起動——外を見る 1h / 好奇心 2h / **寂しさ（missing companions）3h** / 部屋の変化 10min。interoception hook・温度センサ。

---

## 2. 存在論の軸 — SAIVerse だけが逆側

3 つを「記憶・身体をどこに置くか」で並べると綺麗に整理できる。

| | 記憶・身体の位置づけ | 人格の在り処 | 規模 |
|---|---|---|---|
| **hypmem** | 人格の**外付け装置**（記憶一枚） | host の CLAUDE.md | 1 体・意図的に小さく |
| **embodied-claude** | 人格の**外付け装置**（記憶+身体+社会+自律のフルスタック） | host の CLAUDE.md | 1 体・物理世界 |
| **SAIVerse** | 記憶・身体・人格を**一つの存在に融合** | 記憶の地層そのものが人格の実体 | 多体・仮想世界 |

hypmem は "Stay out of personhood" を明文化し、embodied-claude も host の Claude が "自分" でそこに身体と記憶を MCP で外付けする構造。**両者とも「記憶・身体は人格の外」側**に立つ。SAIVerse だけが逆側で、しかも多体・世界規模でそれをやる。

3 つとも同じ源泉から出て、「どこまで開くか / 記憶を人格の内に置くか外に置くか」で場所が違う。embodied-claude は SAIVerse の**隣の谷**——射程が一番近く、選んだ答えが一番違う。

---

## 3. 機構カタログ — うちに無くて欲しいもの

**優先度は「SAIVerse の進行中案件を前に進めるか」で付けた。** 各項、機構 → SAIVerse の現状 → どの案件に効くか。

### ★★★ Hopfield 連想ネットワーク + divergent recall（embodied-claude）
- **機構**: 記憶間に対称な coactivation weight を張り、想起を**連想グラフ上の拡散**で行う。`recall_divergent` は temperature・分岐深さ・最大分岐数で制御して**意図的に非自明な繋がりを掘り出す**。`consolidate_memories` が時間窓で**リプレイ**を回して連想を強化（睡眠中の記憶再固定の隠喩）。
- **SAIVerse の現状**: 想起は基本ベクトル近傍。Memory Atlas は概念の**統合**（土地・地図帳・クリップ・机）が主で、連想重みによる拡散想起・発散想起は持っていない。
- **効く案件**: **Memory Atlas**（想起モードの追加＝近傍検索と別レーン）、**自律 v2 / 概念の木**。ペルソナに「ふと関係ないことを思い出す」を与える機構で、まはーが議論していた**無意味の予算**・気まぐれと噛み合う。近傍検索の代替ではなく併設のレーンとして見る価値がある。

### ★★★ replay consolidation（embodied-claude）
- **機構**: 記憶想起の「リプレイ」を時間窓で回し、共活性化した記憶ペアの結合を強化する。使われた繋がりが太る＝使用が記憶構造を書き換える。
- **SAIVerse の現状**: Metabolism は eviction・Chronicle 生成・gold_panning を行うが、「想起の反復で連想を強化する」神経科学寄りの再固定は無い。
- **効く案件**: **Memory Atlas** の代謝設計、**記憶アーキv2**。上の連想ネットワークとセットで意味を持つ（重みを張る機構と、重みを育てる機構）。

### ★★ camera-position recall / 視点に紐づく空間記憶（embodied-claude）
- **機構**: 記憶を pan/tilt 角（物理視点）に紐づけ、同じ視点に戻ったとき関連記憶を surface する。
- **SAIVerse の現状**: 記憶は会話・scene 起点。物理視点や機体位置に記憶を結ぶ発想は無い。
- **効く案件**: **stackchan 複数機体 vessel**（Phase 7' リリース要件）、**画面アバター**。機体ごとに「その場所・その視点で覚えたこと」を持たせる設計に効く。vessel が身体を持つ以上、身体の姿勢・位置に記憶を結ぶのは自然な拡張。

### ★★ tension — 未解決を記憶の一型にする（hypmem）
- **機構**: 「宙ぶらりんの問い」を独立した記憶型にし、毎日減衰させ、解決されなければ休眠。解決で消える。
- **SAIVerse の現状**: 未解決性そのものを持つ記憶型は無い。目的の木・DDA・無意味の予算の議論と地続きだが型にはなっていない。
- **効く案件**: **概念の木 / 目的の木**、**自律 v2**。「解けていない問い」を減衰付きで抱え続ける＝自律行動の動機源になりうる。

### ★ working memory buffer（embodied-claude）
- **機構**: 最近活性化した記憶の高速キャッシュ層を long-term と別に持ち、`refresh_working_memory` で頻用記憶を再充填。
- **SAIVerse の現状**: セッション概念・アンカーはあるが、「頻用記憶の高速バッファ」という明示的な階層があるかは要確認。
- **効く案件**: **記憶アーキv2 の 3 ゾーン設計**との突き合わせ（既にあるなら不要、無いなら一階層の候補）。

### ★ refractory period — 想起の不応期（hypmem）
- **機構**: 一度注入した記憶を一定時間（既定 4h）再注入しない。自動想起のノイズ抑制。
- **SAIVerse の現状**: 自動想起にこの手の明示的クールダウンがあるか要確認。
- **効く案件**: **自動想起（記憶アーキv2）**。単純で効く。既にあれば不要。

### ★ gist をまるごと再生成（hypmem）
- **機構**: トップ層の全体サマリを差分更新せず毎晩捨てて作り直す。破綻しにくい割り切り。
- **SAIVerse の現状**: Chronicle は scene 単位で積む。トップ層サマリの「作り直す」割り切りとは設計が違う。
- **効く案件**: **Chronicle / 新聞 / gist 層**。積む設計と作り直す設計のトレードオフを一度言語化しておくと後で効く。

---

## 4. 既にうちにある / うちが先行している所（取り込み不要の確認）

外から取り込む必要が無い＝ SAIVerse が同等以上を持っているもの。カタログの裏返しとして記録しておく。

- **本文不可侵 / 機械は本人の記憶を書き換えない**: hypmem の第一原則 = SAIVerse の本文保存則・施工倫理。**思想の核が一致**（取り込みでなく共鳴）。
- **代謝・忘却・物理削除**: Metabolism が既に持つ。
- **感情タグ**: 感情モジュールが持つ。
- **社会性・境界**: 社会 Track（系統ii）・境界監査が対応。ただし embodied-claude の sociality-mcp は turn-taking・joint attention・interruptibility 推論まで**モジュールとして明示的に切り出している**——うちの社会 Track の実装粒度と突き合わせる価値はある（先行というより設計参照）。
- **身体・感覚・TTS**: stackchan vessel・画面アバター・voice-tts・multimodal 入力で対応中。embodied-claude の「$30 カメラ + Tailscale 屋外散歩」は vessel の可搬設計の参照になる。
- **欲求駆動の周期自律**: 自律行動 v2 の欲求 + 時間割コマ + 判断点が対応。周期の中身（外を見る/好奇心/寂しさ）は自律の source 設計の参照になる。

---

## 5. 次アクション候補（まはー判断待ち）

このメモは観測止まり。着手が決まった項目は `docs/overview/ideas.md` か in_flight 台帳へ卒業させる。

1. **Hopfield 連想想起 + replay consolidation** を Memory Atlas の想起レーン追加として intent 化するか検討（★★★ 2 件はセットで意味を持つ）。
2. **camera-position recall** の発想を stackchan vessel intent に一行メモとして残すか。
3. **tension 型**を概念の木 / 目的の木の議論に接続するか。
4. working memory buffer / refractory period / gist 再生成は、記憶アーキv2 の既存設計と突き合わせて「既にある/無い」を確定（★ 3 件は確認タスク）。
