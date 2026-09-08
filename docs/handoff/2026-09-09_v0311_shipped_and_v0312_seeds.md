# 2026-09-09 夜の引き継ぎ — v0.3.11 発行済み、v0.3.12 の種

寝る前のまはーと、次のメティスのための現在地。**v0.3.11 は発行済み**
([Release v0.3.11](https://github.com/maha0525/SAIVerse/releases/tag/v0.3.11)、
発行の明細は [release_history.md](../overview/release_history.md))。稟乃さんへの
更新案内はまはーが送付済みで、回復確認の返事待ち。前の引き継ぎは
[2026-09-09_sluice_cold_skip_and_v0311_handoff.md](2026-09-09_sluice_cold_skip_and_v0311_handoff.md)。

## ① v0.3.11 で何が済んだか (一行ずつ)

- psutil 必須化 (#286) / Chronicle 被覆の穴 第一段 + 追加修正 4 件 (#287) /
  スルースの被覆の穴 第一段 (#287 同梱)。
- レビュー = ローカル LLM 1 巡 (採用 0) + Codex 三巡 (採用 8)。目玉は二つ:
  パンマーカーの読み取り失敗が「未走行」に化けて fail-closed を打ち消していた穴と、
  束ねの呼び直しループがレート制限中でも残予算ぶん叩き続ける穴。
- 実機 = まはーの本番 (会話・手帳・補修「5 回分が一発」) + 隔離環境の実サーバーで
  「冷たいときの飛ばし」を実際に動かして確認 (13 万字の縮尺再現 → 21 秒で応答・
  範囲の記録 1 件・429 への再試行は一回で止まった)。
- 隔離環境 (`test_data/`) には、未整理の履歴 13 万字を仕込んだテスト用ペルソナが
  残っている — スルース第二段の検証にそのまま使える。

## ② v0.3.12 の種 (まだ束は組んでいない — 範囲の確定は release_history の手順で)

1. **GPT-Image 2.5 対応** (2026-09-09 まはー意向、比較的小さい): リリースされた
   GPT-Image 2.5 を SAIVerse から使えるようにする。モデル定義と、画像生成の
   経路 (generate_image playbook / llm_clients の画像系) の対応確認。
2. **v0.3.10 発行後からの持ち越し** (release_history「次の版の範囲」に記載済み):
   知覚の既定値の調整 / 保存時検査の余裕 10,000 の妥当性 / perception_high null の
   素通し / アイテム個数上限 (新 feature ブランチの裁定)。
3. **第二段ふたつの着手裁定**: スルースの期間選択 UI (設計済み、intent
   sluice_coverage_gaps 第二段) と Chronicle の仮のあらすじと事後承認 (未起草)。
   同じ画面群になる見込み。
4. **今日のレビューで拾った小さい負債** (新 issue
   [generation_job_stores_shared_quirks.md](../issues/generation_job_stores_shared_quirks.md)):
   ジョブの器 (Chronicle 生成 / 採取) が完了後も一覧に残り続ける・中止中への
   再中止の返事が変。両方とも既存の癖で実害は小さい。

## ③ リリース前の検証体制の強化 (まはー主導・進行中)

- まはーが Codex 側と連携して「今やるべきこと」を洗い出し中。**作業ツリーの
  未追跡ファイル (`Codex.local.md`・`docs/audits/`・
  `docs/handoff/2026-09-09_product_verification_inventory_opus_brief.md`・`memory/`)
  の一部はこの過程の資料** — メティスは触らない・コミットに入れない。
- 源流はまはーの 2026-09-08 発案 (リリース前の全自動実機ジャーニーテスト構想、
  ideas.md に記録済み)。畳み 1 回の材料上限の見落としと、稟乃さんの実害が動機。
- まはーの見立てでは対応は重め。洗い出しの結果が出たら、束の設計から一緒にやる。

## ④ 運用メモ

- ブランチ: 全部 push 済み・未コミット無し (未追跡は③の資料のみ)。
  feature/chronicle-coverage-gaps と feature/self-update-psutil-failclosed は
  マージ済みで、消すのはまはーの判断でよい。
- Codex の config は `gpt-5.6-luna` + high のまま (元からその設定だった)。
- 次の版に上げるとき、画面からの更新が一発で通るかの実機確認を忘れない
  (psutil の件をこれで閉じる — in_flight 台帳に記載)。
