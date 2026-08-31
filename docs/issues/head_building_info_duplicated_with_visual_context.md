# head 内で Building 情報が二重に載っている (BuildingSection × ビジュアルコンテキスト)

**ステータス: 未解決** (2026-08-08 起票。時間割実機検証の Step 2 でまはーが発見)

## 症状

head の中に、現在地 Building の情報が二系統で重複して入っている:

| 情報 | BuildingSection (order 300) | VisualContextSection (order 800) |
|---|---|---|
| Building 名 | `## {name} (ID: ...)` 見出し | 「現在、『{name}』にいます。」 |
| 役割・指示 (base_system_instruction) | 本文として全文 | `[システムプロンプト]` として全文 |

役割・指示の全文が head に**二回**入るため、Building の system_instruction が長いほど head が無駄に太る。キャッシュされる領域なので毎回課金される種類の無駄ではないが、コンテキストの占有と「同じ指示が二度読まれる」認知ノイズが常設になる。

## 経緯 (なぜこうなったか)

- `BuildingSection` は旧 `sea/runtime_context.py` の system prompt 第 3 節 (`## {building_name}`) を head パイプライン化 (cached_head_architecture §5.3) の際に移植したもの。
- `get_visual_context` (`builtin_data/tools/get_visual_context.py`) は旧構成の頃から `[システムプロンプト]` を自前で含んでいた (for_perception の分岐に関係なく無条件)。
- つまり二重は head パイプライン化より前の旧構成から引き継がれたもので、移植時に両者の突き合わせが行われなかった。「後のセッションが気付かず増やした」のではない。

## 修正の方向 (未裁定)

縄張りを分ける: **役割・指示と Building 名見出しは BuildingSection の縄張り、ビジュアルコンテキストは視覚情報 (外見・内装・アイテム) だけ**にする — `get_visual_context` の head 経路から `[システムプロンプト]` と「現在、〜にいます。」を外す。

注意点:

- `get_visual_context` は移動時の知覚通知 (`for_perception=True`、「移動先の様子」) と共用。そちらでは移動先の情報として意味があるが、**BuildingSection の `building_changed` diff 通知も移動時に「{name}」の役割・指示を全文同梱している**ため、移動通知側にも同種の二重がある。両経路まとめて縄張りを引き直すこと。
- head 構成の変更は prefix キャッシュを一度壊す (Metabolism の再 capture で切り替わる)。時間割まわりの実機検証と同時にやらない — 検証中の head を動かすと観察対象が濁る。

## 確認手段

- 会話コンテキストのプレビューで head を目視 (発見時の手段)
- `sea/head_pipeline/sections/building.py` / `visual_context.py` / `builtin_data/tools/get_visual_context.py`
