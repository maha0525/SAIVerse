# 画面内アバター表示 (Screen Avatar)

## この文書の位置づけ / ステータス

- **v0.3 draft (2026-07-04)** — 描画ルートに 2.5D (Anime2.5DRig) を本命として追加、 アセット供給に see-through を追加 (一枚絵 1 枚 → 動くアバターの最低敷居パイプライン)。 v0.2 を継承。 末尾「未確定事項」を詰めてから finalize → 実装。
- **v0.2 (2026-07-02)** — v0.1 の「感情→表情タグ変換層」を撤回し、 `embodied_expression.md` の発声同期インライン機構に乗せる形へ改訂。
- 関連:
  - `docs/intent/embodied_expression.md` — **本機能の表情・エモート駆動はこの機構に乗る。** 画面内アバターは同 doc の言う「新しい vessel 種別」= dispatch 先。
  - `docs/intent/stackchan_avatar_pipeline.md` — 実機 Stack-chan の avatar 資産パイプライン。 2D 描画は資産を共有。
  - `docs/intent/voice_tts_playback_queue.md` — 発火タイミングを subscriber 側で決める設計理由の出典。
  - `docs/intent/mcp_addon_integration.md` — アドオン機構。

## 背景と目的 (WHY)

画面の中でペルソナが「そこにいる」ように見せたい。 Building にいるペルソナが画面右下あたりに常駐し、 喋るときに口パクし、 発話に同期して表情・エモートが動く ── ペルソナが単なるチャットログの発話者ではなく、 実体を持った存在として立ち上がることを狙う。

3D モデルの用意は本来 Unity 連携が要る敷居の高い領域だった (構想はあったが凍結中)。 しかし **VRM をブラウザ (WebGL) で表示するルート (@pixiv/three-vrm)** なら Unity 不要で Next.js フロントに直接載る。 VRoid / VRChat 向けの VRM 資産は入手性が高く、 自作も VRoid Studio で容易。 一方、 手描き PNGTuber (PuruPuru 風 2D) という軽量ルートもある。 この 2 つを **描画方式の選択肢**として扱い、 本体には両者に共通する土台だけを置く。

参照 OSS:
- **[Anime2.5DRig](https://github.com/852wa/Anime2.5DRig) (MIT)** — パーツ分け PSD をブラウザにドロップ → 自動リギング → 2.5D アバター。 WebGL、 メッシュ変形 + 物理 (髪/胸/呼吸) + 表情プリセット + 音素口パク。 **MIT + ブラウザ完結 = SAIVerse に直接コード組込みできる、 本命の 2D(2.5D) 描画エンジン。**
- **[see-through](https://github.com/shitagaki-lab/see-through) (Apache-2.0)** — 一枚絵 → 最大 23 レイヤーの意味分解 PSD (隠れ部分を inpaint)。 ML 駆動 (SDXL 系 diffusion / SAM / depth 推定、 GPU 前提)。 **出力 PSD が Anime2.5DRig の入力 PSD に直結** = 「一枚絵 1 枚 → 動くアバター」の最低敷居パイプラインを成す。
- [PuruPuruPNGTuber](https://github.com/rotejin/PuruPuruPNGTuber) (Apache 2.0) — 2D PNGTuber。 Anime2.5DRig 登場で本命の座は譲るが、 ぷるぷる二次モーションの思想的参考。
- [aituber-kit](https://github.com/tegnike/aituber-kit) — VRM を Next.js + three.js + @pixiv/three-vrm でブラウザ表示。 **v2.0.0 以降カスタムライセンス (商用要ライセンス) なので、 コード流用でなく参考実装として扱う。** VRM 表示の核 @pixiv/three-vrm 自体は MIT なので直接利用してクリーンに自前実装する。

## 全体構図: 共通基盤 (本体) + 描画アドオン

```
┌─────────────────────────── SAIVerse 本体 (core) ───────────────────────────┐
│  共通基盤:                                                                  │
│   ① 発声同期出力レイヤー = 口パク + emote 発火 (subscriber 側 audio 駆動)   │
│   ② 描画 canvas → 実機ストリーミング (Phase 2)                             │
│                                                                             │
│  素の本体 (アドオン無し) のフォールバック描画: 表情ごとの一枚絵 + 切替      │
└───────────────▲───────────────────────────────────▲─────────────────────────┘
                │ render surface + emote dispatch      │
      ┌─────────┴─────────┐               ┌───────────┴───────────┐
      │  2D PNGTuber アドオン │               │      VRM アドオン        │
      │  (PuruPuru 風)       │               │  (@pixiv/three-vrm)     │
      └───────────────────┘               └───────────────────────┘
```

- **共通基盤は本体に組む。** 描画エンジンに依存しない「配線」だけを持つ。
- **描画レイヤーはアドオン。** 2D も VRM も、 本体が用意した描画面 (render surface) に描画を差し込み、 emote dispatch を受ける。 排他ではなく、 有効なアドオンが描画を担当する。
- **アドオンが無い場合も動く。** 本体は最低限「ペルソナの一枚絵を右下に出し、 表情ごとの一枚絵を切り替える」フォールバックを持つ。

## 表情・エモートの駆動 = embodied_expression に乗る (最重要)

v0.1 では「感情 4 軸 → 離散表情タグ変換層」を本体に作る案だったが**撤回**する。 表情変化もエモートも、 既存 intent `docs/intent/embodied_expression.md` の **`/emote <preset>` インライン発声同期機構**で駆動する。

- LLM が本文に `/emote happy` 等を書く → パーサがマーカー除去 + char_offset 抽出 → subscriber (再生端末) が `audio.currentTime` を見てその文字位置で発火。
- **画面内アバター (2D/VRM) は embodied_expression から見た新しい vessel 種別。** preset JSON に `actions.screen_2d` / `actions.vrm` ブロックを足し、 dispatch 層がそれを解釈する:
  - **2D**: 表情差分 PNG 切替 (既存 stackchan avatar の 6 表情資産を流用) + 簡易モーション。
  - **VRM**: expression preset (happy/sad/angry 等) の weight + VRM アニメーションクリップ (VRMA) / procedural モーション。
- **4 軸感情モジュール (stability/affect/resonance/attitude, `EmotionControlModule`) は表情駆動には使わない** (まはー確定 2026-07-02)。 表情は LLM の意図 (インラインマーカー) が直接決める。 4 軸モジュールの去就は本 intent のスコープ外 (将来 TTS 発声トーン制御等で生かす余地はあるが別件)。

### まはー構想: 「発声同期ツール実行」への一般化

まはーの意図 (2026-07-02): 「一部のツールを発声と同期して実行できるようにし、 かつそのツールによって表情変化やエモートの実行が呼ばれる」。 = `/emote` (preset 発火) を、 **「発声と同期して一部の (許可された) ツールを実行する」汎用機構の一適用**として捉え直す方向。 表情・エモートはその同期実行ツールが引き起こす結果になる。

- これは embodied_expression の /emote preset 機構を包含する上位フレーム。 **embodied_expression.md v0.2 (2026-07-02) に一般化を反映済**。
- **絞りの線引き (まはー確定 2026-07-02)**: 引数の有無ではない (表情変更ツールは表情が引数)。 spell が spell_tools で実行可能ツールを絞るのと同型に、 「修飾的な意味合いで発声に同期して実行したい短時間の行動あるいは状態変化」を emote 実行可能ツールとして登録で絞る。 preset はその上の複合エモート表現。 emote ≒ 発声同期版 spell。

## 発声同期出力レイヤー (共通基盤 ①)

口パクと emote 発火は兄弟。 どちらも **subscriber (再生端末) が再生中 `<audio>` の `currentTime` を見て発火**する (embodied_expression 不変条件 3・`voice_tts_playback_queue` の教訓を継承)。

- **口パク (連続)**: 既存 `frontend/src/lib/clientActions/playAudio.ts` の `play_audio` が再生する共有 `<audio>` に WebAudio `AnalyserNode` を噛ませ、 実音声振幅を「口の開き量 0.0〜1.0」として供給。 現状 2D プレビュー (`AnimationPreviewSection`) の口形状ランダム切替を実振幅駆動に置換。 描画側は 2D=口形状 PNG、 VRM=expression `aa` weight にマッピング。
- **emote 発火 (離散)**: embodied_expression の emote_events (char_offset → preset) を audio.currentTime 基準で発火し、 render surface / dispatch 層へ渡す。
- 音声が無い (TTS 未使用) 時の口パク挙動は未確定 (下記)。

## 描画 canvas → 実機ストリーミング (共通基盤 ②, Phase 2)

- 2D の Canvas でも VRM の WebGL canvas でも、 **canvas をフレーム化 (JPEG) して CoreS3 に MJPEG ストリーミング**する経路は共通。 実機はデコード表示のダム端末になり、 現行 avatar.bin (実機側で PNG 重ね) 方式は役目を終える。
- fps / レイテンシ / 音声同期 / 電力は CoreS3 実機で実測 (無印 ESP32 は 8-9fps 頭打ちだが、 ESP32-S3 は JPEGDEC dual-core で 28fps 事例あり = 口パクに要る 15-30fps は射程内、 パイプライン全体では要実測)。

## 描画レイヤー: アドオンとして差し替え

本体は「render surface をアドオンに提供する ui_extension + emote dispatch」を新設する。 現状 `AddonUiExtensions` (`frontend/src/types/addon.ts`) は `bubble_buttons / input_buttons / client_actions` のみで、 **常駐描画ウィジェットを注入する口が無い**。 ここに「render surface に React コンポーネントを登録し、 口パク信号 + emote dispatch を受ける」機構を追加する (具体機構は実装時に詰める)。

### 2.5D リグ アドオン (Anime2.5DRig 系, 本命の 2D ルート)
- **パーツ分け PSD を入力に自動リギングし、 メッシュ変形 + 物理 (髪/胸/呼吸) + 表情プリセット + 口パクで動かす** WebGL エンジン。 MIT + ブラウザ完結なのでコード流用/自前組込みが可能。
- 静的 PNG 切替 (PuruPuru 風) の上位互換。 「ぷるぷる」も 2.5D 頭傾き (parallax + shear) も本体機構に含む。
- emote dispatch = Anime2.5DRig の表情プリセット / メッシュ変形呼び出し (`actions.screen_2d`)。
- MediaPipe 顔トラッキング (Webカメラ追従) は SAIVerse 用途では不要 (ペルソナは自律)。 音素口パク機構を活かすか、 既存 `<audio>` + AnalyserNode 振幅駆動に置換するかは実装時判断。
- PSD 素材は see-through で一枚絵から生成 (下記「アセット供給パイプライン」)。 既存 stackchan avatar 資産 (表情マトリクス PNG) は静的フォールバック / 実機用に残る。
- **取り込み方 = エンジン抽出 (UI は使わない)**: Anime2.5DRig は `index.html`(UI+WebGL 描画) / `lib/rigger.js`(リグ生成エンジン, pure TypedArray・Node テスト可) / `lib/ag-psd.min.js`(PSD パーサ) 構成。 **UI のドロップ (= PSD 1 個制約の正体) は捨て、 rigger.js + 描画部分をライブラリとして取り込む**。 整形層はメモリ上でレイヤーツリーを組み立てて (ag-psd はレイヤー⇄PSD 両方向) 直接エンジンに渡すので、 物理的に 1 個の .psd にする必要すら無く、 「PSD ファイル 1 個」制約は無関係になる。 ただし rigger.js の入力インターフェース (期待するレイヤーオブジェクト形式) は未文書化 → 実コードを読んで合わせる手当てが要る (pure TypedArray・Node テスト可なので密結合ではなく抽出しやすい部類)。

### VRM アドオン (@pixiv/three-vrm)
- react-three-fiber + @pixiv/three-vrm で描画。 Unity 不要。
- VRM 標準機構: expression preset (口形状 aa 等 / blink / 感情)、 SpringBone (揺れもの)、 LookAt (視線)、 自動まばたき、 アイドルモーション。
- 資産入手性が高い (VRoid Hub / VRChat 向け / VRoid Studio 自作)。

## アセット供給パイプライン (see-through) — 最低敷居ルート

2.5D ルートの弱点だった「パーツ分け PSD を用意する手間」を、 **see-through で一枚絵から自動生成**して潰す:

```
一枚絵 (推しキャラ / 自作イラスト 1 枚)
  ↓ see-through (ML: SDXL系 diffusion / SAM / depth 推定。 隠れ部分を inpaint。 最大 23 レイヤー)
パーツ分け PSD (口 1 枚・目 1 状態のみ、 命名は see-through 規約)
  ↓ 【SAIVerse 整形層】命名マッピング + 口開き/閉じ目の差分を AI 生成 (既存 stackchan avatar pipeline 流用)
Anime2.5DRig 規約の PSD (mouth_open/close, eyelash/eye_close 完備)
  ↓ Anime2.5DRig (ブラウザ WebGL, 自動リギング)
動く 2.5D アバター (口パク + 表情 + 物理)
```

- これで **「一枚絵 1 枚あればアバターになる」** = VRM 入手や表情差分手描きより敷居が低い。 まはーが「一番敷居低くなる」と見た核心。
- see-through は重い ML (GPU 前提、 RTX 3090 で可)。 ただし**素材準備時のみのオフライン前処理**。 常駐描画は軽い Anime2.5DRig (WebGL) が担う ── この住み分けが綺麗。
- アドオン構成: see-through は「アセット生成アドオン (重依存・任意)」、 Anime2.5DRig は「描画アドオン (常駐)」に分離できる。 stackchan avatar pipeline (AI 画像生成) と並ぶ、 もう一つの素材供給経路。
- 両者とも MIT / Apache-2.0 でコード流用可 (aituber-kit のカスタムライセンス問題が無い)。

### 実証 (2026-07-04, まはー実機検証) + SAIVerse 整形層の必要性

see-through 出力 PSD を Anime2.5DRig に投入した結果、 **規約ズレが 2 種確認された**:
- **軽微 (命名ズレ)**: `legwear`/`footwear`/`eyewear` が未知名 → body/head フォールバック (表示は出る)。 命名マッピング (例: legwear→bottomwear, eyewear→headwear, mouth→mouth_close) で解消。
- **本質 (差分レイヤー欠如)**: **`mouth_open`/`mouth_close` が無い**。 Anime2.5DRig の口パクは **mouth_open/close 2 枚のクロスフェード方式で、 単一レイヤーのメッシュ変形フォールバックは無い** (実装確認済)。 目パチも同様に `eyelash`(開)/`eye_close`(閉) の差分が要る。 see-through は「一枚絵に描かれた物の分解」なので、 **元絵に無い開き口・閉じ目は原理的に出せない** (分解精度を上げても埋まらない)。
- → **SAIVerse が作る整形層 = 命名マッピング + 口開き/閉じ目の差分 AI 生成**。 差分生成は既存 stackchan avatar pipeline (口 5 形状・目 3 状態を AI 生成) の能力を流用できる (全体生成でなく、 見えてる口/目パーツから欠けた差分だけを部分生成)。 **この整形層こそが 2.5D ルートで SAIVerse が実装する肝。**
- 懸念: AI 生成した口開き/閉じ目差分が元絵の画風・位置と整合するか (= 部分差分生成の品質) は実物で要検証。

### アドオン無し (本体フォールバック): 表情ごとの一枚絵
- ペルソナの一枚絵を右下に常駐表示。 **表情は表情ごとに 1 枚ずつ用意し、 emote/表情タグで丸ごと差し替える** (まはー確定: 一枚絵の場合は表情も一枚絵で用意)。 口パクは (口開き差分を持たせない限り) しない or 簡易。

## 不変条件 / 制約

- **アドオン無しでも成立すること。** 描画アドオンが無くても、 表情ごとの一枚絵切替で最小形が動く。
- **表情・エモート駆動は embodied_expression の機構に一本化すること。** screen_avatar 独自の感情→表情変換層を作らない (二重機構の禁止)。
- **発火タイミングは subscriber 側で決定すること** (embodied_expression 不変条件 3 継承)。 本体は preset 名 + char_offset を渡すだけ。
- **既存 stackchan avatar 資産・パイプラインと共存すること。** 2D 描画は同じ資産を流用し二重管理を作らない。
- **描画方式に依存しない配線であること。** 口パク信号・emote dispatch・canvas ストリーミングは 2D/VRM/フォールバックのどれでも同じインターフェースで供給。
- **aituber-kit のコードは流用しない** (カスタムライセンス)。 参考にとどめ @pixiv/three-vrm (MIT) を直接使う。

## フェーズ分け

- **Phase 1**: PC 画面 (Building ビュー右下) にアバターを常駐表示 + 口パク + emote 同期。 実機不要、 PC 内完結。 = 元の主目的。
- **Phase 2**: 描画 canvas を JPEG フレーム化して CoreS3 に MJPEG ストリーミング。 実機のダム端末化。 fps 等は実機実測。

(embodied_expression 側の段階: Phase A パーサ+stackchan → C で Live2D/3D 横展開。 本 doc の 2D/VRM vessel はその Phase C に相当する dispatch 先を足す作業と重なる。)

## 未確定事項 (インタビュー対象)

### 解決済 (2026-07-02)
- ~~感情タグの出所~~ → 4 軸は使わない。 embodied_expression の /emote インライン機構で LLM が直接駆動。
- ~~表情語彙~~ → 既存 2D avatar の 6 表情 (idle/happy/thinking/sad/surprised/embarrassed) ベース。
- ~~フォールバック一枚絵~~ → 表情ごとに 1 枚用意して丸ごと切替。

### 未解決
1. **spell 機構との統合度**: emote を「発声同期版 spell」として構文・絞り機構 (spell_tools 相当) をどこまで共通化するか、 別系統として持つか (`embodied_expression.md` v0.2 オープン課題)。 発火タイミング (ラウンド内逐次 vs TTS 同期) の違いをどう表現するか。
2. **音声が無い時の口パク**: TTS 未使用時はテキスト長ベースの疑似口パクを出すか、 口を閉じたままにするか。
3. **複数ペルソナ**: 同室に複数いる時、 1 体表示 / 全員並べる / 発話者を切り替える。
4. **配置・サイズ**: 右下固定 / ドラッグ移動可 / チャット UI との重なり方。
5. ~~see-through ↔ Anime2.5DRig の PSD 規約整合~~ → **実証済 (2026-07-04)**: 命名ズレ (マッピングで解消) + 口/目差分欠如 (整形層で AI 生成)。 上記「アセット供給パイプライン > 実証」参照。
6. **整形層の差分 AI 生成の品質**: 口開き/閉じ目の部分差分が元絵の画風・位置と整合するか実検証。 stackchan avatar pipeline 流用時の位置合わせ・画風追従の調整。
7. **Anime2.5DRig 自動リグの見栄え**: 実アバターで破綻の有無を継続確認。
8. **rigger.js 入力インターフェースの特定**: Anime2.5DRig エンジンが期待するレイヤーオブジェクト形式を実コードで特定し、 整形層の出力 (メモリ上のレイヤーツリー) をそれに合わせる (公開 API 未文書化)。
