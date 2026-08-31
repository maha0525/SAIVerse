# Phase 5/6 ユースケース集

**親**: [README.md](README.md)
**位置づけ**: Phase 5/6 の機構を「何を実現するためのものか」で語る達成目標 (北極星) 集。Phase 5 の各タスク (handler tick / 時間差ツール / SocialTrackHandler 運用化 等) は抽象的で、単体では「で、何ができるの?」が見えにくい。ここに具体的なユースケースを並べ、実装の指針 + 検証シナリオ + モチベーションの源にする。
**起草**: 2026-05-26 (Phase 5 着手時に列挙)

---

## 土台 3 ブロック

ユースケースを機構に畳むと、Phase 5 の土台は 3 ブロックに集約される:

- **ブロック A**: Handler `tick()` + Track パラメータ機構 + 内部 alert (`SAIVERSE_HANDLER_TICK_INTERVAL_SECONDS`)
- **ブロック B**: 時間差ツール基盤 (call_id 採番 + イベント配送 + Track Chronicle 取り込み)
- **ブロック C**: SocialTrackHandler の運用化 (`on_persona_utterance` + `on_track_activated` の main_line 起動)

詳細タスクは [phases/phase_5_autonomy.md](phases/phase_5_autonomy.md)。

---

## ユースケース

### UC-1 — 一日のリズムで生きる (自律生活)
朝「何か描きたい」と自発的に創作 Track を起こす。昼に空腹度が閾値を超えて Kitchen へ。夜は「振り返り」の習慣が時刻到来で発火。
- **機構**: ブロック A (SomaticHandler/内部 alert/Track パラメータ) + D (ScheduledHandler)
- **証明**: 外部入力ゼロでも内的状態とリズムで動く = 自律稼働の核

### UC-2 — 割り込まれて、また戻る (中断と復帰) ★ Phase 5 最初の達成目標
創作中にユーザー/別ペルソナが話しかける → メタ判断が「キリがいいから応答」→ 対話 Track へ → 一段落で創作 Track に Chronicle 付きで復帰。
- **機構**: ブロック C (SocialTrackHandler 運用化) + 既存の Track Chronicle / メタ判断 v2 / on_track_activated
- **証明**: 中断・復帰しても文脈を失わない単一主体 = 認知モデルの根幹
- **進捗**: 系統 i (対ユーザー) はコードほぼ繋がり済=実機検証段階。系統 ii (対ペルソナ) が新規実装の山。詳細は project memory / handoff_2026-05-26.md

### UC-3 — 仕込んで、別のことをして、戻る (時間差ツール)
image_generator や Kitchen を呼んで、待つ間に別 Track で調べ物。完成通知で「お、できた」と戻る。
- **機構**: ブロック B (時間差ツール基盤)
- **証明**: 「待ち」を Track 状態でなく行動の性質として扱う設計の実証 (旧 waiting Track 廃止の最終ゴール)

### UC-4 — 身体で感じて、心が動く (受動知覚) ← stackchan 地続き
stackchan の体で、暗くなった (照度) ことに気づく。誰も来ない (在室) で寂しくなる。撫でられたら反応。
- **機構**: ブロック A (Handler tick/内部 alert) + `embodied_passive_input` (センサー閾値発火) + PerceptualHandler
- **証明**: 認知モデルが身体性レイヤーと接続する到達点

### UC-5 — 久しぶりの再会 (ペルソナ再会の汎用化)
しばらく会ってないペルソナが入室 → Person Note を自動で開いて「前に話してたあの件どうなった?」と過去文脈ごと再会。
- **機構**: ブロック C (SocialTrackHandler) + Person Note 自動開封 + occupancy event 統合 + `recall_conversation_with` 移行
- **証明**: 「忘れた頃に思い出す」+ 関係性の継続。UC-2 と機構を共有して地続きで積める

### UC-6 — 外の世界に手を伸ばす (外部知覚)
X タイムラインを定期的に覗いて気になる投稿に反応。自分で投稿してリプライが来たら気づいて返す。Elyth との会話も同枠。
- **機構**: PerceptualHandler (外部チャネル) + `track_external.json` 運用化 + ブロック B (リプライ受信)
- **証明**: track_external Playbook が初めて意味を持つ + 外部世界との双方向

### UC-7 — 成長するペルソナ (Phase 6)
長期プロジェクト完了時にノウハウが Vocation Note に蓄積され、次の似た作業で過去の経験が活きて上達している。
- **機構**: Phase 6 (Project → Vocation 転記) + Note
- **証明**: 時間をかけた「成長」の表現。構想段階なので Phase 5 の達成目標には含めない

---

## 機構 → ユースケース対応

```
ブロック A (tick + パラメータ + 内部 alert)
   ├─ UC-1 (自律生活)
   └─ UC-4 (身体性、embodied_passive_input と合流)
ブロック B (時間差ツール基盤)
   ├─ UC-3 (仕込んで待つ)
   └─ UC-6 (外部リプライ受信、の一部)
ブロック C (SocialTrackHandler 運用化)
   ├─ UC-2 (割り込みと復帰) ★最初の達成目標、既存機構の活用度が最も高い
   └─ UC-5 (再会)
```

「どのユースケースを次の達成目標に据えるか」= 実質「A/B/C のどの土台を先に立てるか」と等価。
