# Intent: 画像生成モデルの追加

**ステータス**: 検証待ち (GPT Image 2.6 対応。隔離テストまで完了、実 API 生成は未確認)。
Nano Banana 2.1 は 2026-10-09 にまはーの本番で確認済み。

## 全体と不変条件

利用者・ペルソナが Playbook または `generate_image` ツールで指定した画像モデルを、
対応するプロバイダへ渡し、返された画像を従来のメディア保存・表示経路へ届ける。
モデル選択の正典は画像ツールであり、会話用モデル設定とは独立する。
ツールの型・利用可能一覧・スキーマ・Playbook の選択肢は新モデルを同じ名前で公開する。
既存のモデル ID と品質設定は変更しない。まはー確認済みのレビュー方針により、
既定モデルと自動フォールバックの Nano Banana 2 の位置は 2.1 へ置き換える。

## Nano Banana 2.1

- 選択名 `nano_banana_2_1` を追加し、公式 ID `gemini-nano-banana-2.1` へ送る。
- 既存 Nano Banana 2 のリクエスト・応答処理を再利用し、有料の `GEMINI_API_KEY` を必要とする。
- 品質 low/medium/high は既存契約どおり 1K/2K/4K。xhigh/max は 4K。
  API 自体の既定 1K とは別に、ツールは従来の全体品質設定を適用する。
- テキストと画像の応答、参照画像（プロバイダ上限14枚）、縦横比を扱う。
  function calling・structured output・cache は送らない。
- 既定値と自動 fallback の先頭を `nano_banana_2_1` にする。旧 `nano_banana_2` は明示指定の互換経路として残す。
  旧 preview API ID の stable ID 移行は別作業として 2026-10-09 に行った: `nano_banana_2` は
  `gemini-3.1-flash-image`、`nano_banana_pro` は `gemini-3-pro-image` を使う (Google が 2026-05-28 に
  preview の ID の終了を予告し、同日に正式版を公開。移行時点で preview も正式版も Google のモデル一覧に
  存在することを確認済み。正式版での実際の生成は課金を伴うため確認していない)。

公式仕様（2026-10-08 確認）:
https://ai.google.dev/gemini-api/docs/models/gemini-nano-banana-2.1

## GPT Image 2.6

- 2026-11-04 リリース。API モデル ID は `gpt-image-2.6-flare`（高速・反復向け）と
  `gpt-image-2.6-sunburst`（精細な視覚作業・編集の一貫性向け）の 2 変種で、2.5 と同じ
  Flare/Sunburst 構成。エンドポイント（images generations / edits）とサイズ制約は 2.5 と同一。
- 選択名 `gpt_image_2_6_flare` / `gpt_image_2_6_sunburst` を追加し、2.5 と同じ既存の
  `images.generate` / `images.edit` 経路と `_aspect_ratio_to_openai_size` を再利用する。
- quality は low / medium / high / xhigh / max の全段階をサポートする。xhigh / max は
  2.5 と 2.6 の両方で素通しし、旧 GPT Image モデルだけ high へクランプする（high 以上の
  トークン数は 2.5 と同一。公式 docs 由来の情報で 2026-11-06 に確認済み）。
- 方針: ツールでは 2.5 も明示指定の互換経路として残す。Playbook のモデル選択肢は 2.5 から
  2.6 へ世代交代する。自動 fallback は 2.6 の 2 変種を OpenAI 系の先頭（nano_banana_pro の
  直後、2.5 より前）に置く。既定モデルは従来どおり `nano_banana_2_1` のまま。
- 対象外: 2.6 の新機能である complexity パラメータと GIF / SVG 出力は今回扱わない。
  将来候補として別作業にする。
- 検証: 隔離テストでスキーマ・Playbook の選択肢、2.6 のラッパーが正式 ID へ届くこと、
  xhigh / max が 2.6 で素通しされ旧モデルが high へクランプされること、dispatch、
  fallback の並びを確認する。実 API での生成は未確認（検証待ち）。

## 検証の境界

隔離テストでスキーマ・Playbook から選択可能なこと、選択名から SDK の正式 ID・
品質・参照画像・応答形式へ届くこと、画像 bytes/MIME が既存保存経路へ渡ること、
有料キー必須、旧モデルの明示指定、2.1 が既定値・fallback の先頭になることを確認する。
SDK 呼び出しは mock とし、実 API の生成品質・課金・本番保存は確認しない。

## 経緯

- 2026-10-09: 台帳の次アクションは「まはーが本番で、Haiku 5.5 のペルソナとの会話と、既定のままの
  画像生成が通ることを見る」だった。Haiku 5.5 の疎通はまはーが確認し、残りは既定のままの画像生成だけに
  なった。次のバージョンを出す前に、まはーが一枚描いて確かめる。
- 2026-10-09 (完了): まはーの本番で、エリスの画像生成が Nano Banana 2.1 で通った。backend.log
  (20261009_191802) の 19:27〜19:28 に、送り先が `models/gemini-nano-banana-2.1:generateContent` で
  200 が返り、画像が 1 枚保存された記録がある。このときはペルソナが画像のモデルとして 2.1 を自分で
  選んでいたので、モデルを選ばなかったときに 2.1 へ落ちる経路は本番では通っていない (そこは隔離の
  テストで確かめた範囲のまま)。台帳の行を外した。
- 2026-11-06: GPT Image 2.6 (flare / sunburst) 対応を追加。公式 docs の裏取りは済みで、
  隔離テストまで通した。実 API 生成は未確認のため検証待ち。
