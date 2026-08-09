# Building ID に文字種の制約が無く、サイドバーの + からは日本語 ID が黙って生まれる

**ステータス: 実装済み・レビュー待ち** (2026-08-08 起票 → 2026-08-09 論点 1 を実装 (v0.3.0 の門 Wave 0)。契約と生成式を `manager/ids.py` 一枚に集約し、**Building を作る 4 経路すべて**に適用: `create_building` / `create_region` の入口自動作成 / ペルソナ個室 (`manager/persona.py`) / ブループリント個室 (`manager/blueprints.py`)。カスタム ID は `[A-Za-z0-9][A-Za-z0-9_-]*` 以外を拒否、自動生成は ASCII slug 化 + 日本語名は連番フォールバック。読み変換 (ローマ字化) は導入しない。Region ID も同じ契約に従う (入口 Building の ID `entrance_<region_id>` の材料のため)。**論点 2 (既存 3 件) は裁定どおり放置**、論点 3 のうち **City ID / ペルソナ ID (AIID) 自体の文字種は未着手のまま残る**)

## 経緯

- **2026-08-09 初回実装**: 作成の口を `create_building` 一箇所と見なして契約を置いた。
- **同日 レビュー (Codex) で漏れが発覚**: `create_region` が入口 Building を `db.add` で直接作っており、`create_building` を通らない。しかも `region_id` 自体が無検証で、`game_create_subregion` (Ruler ペルソナが自分で SubRegion を作る口) から日本語名がそのまま渡ってくる — 手動 UI 限定ではなく、ペルソナが自律で踏む経路だった。
- **消し込み時に族として点検**: 同じ形の口をさらに 2 つ発見 (ペルソナ個室・ブループリント個室)。いずれも `f"{name.lower().replace(' ','_')}_{city}_room"` で ID を自作していた。契約が `manager/admin.py` のプライベート定数に閉じていたことが、他の口が漏れた原因 — だから消し込みでは定数を移すのではなく、契約と生成式を共有モジュールへ集約する形にした。
- **同日 レビュー第 2 巡で、族への拡大が作った退行が見つかった**: slug 化は情報を落とす写像 (「A店」と「A森」はどちらも `a`) なので、私室 ID が「重複検査済みの AIID の純粋関数」でなくなった。検査を通った別ペルソナが同じ Building ID に落ち、PK 衝突で commit が失敗する。しかも `create_persona` / `spawn_entity_from_blueprint` はどちらも **commit より前にインメモリの世界状態 (`building_map` / `occupants` / `personas`) を更新していた**ため、rollback してもキャッシュ側は既存ペルソナの部屋を上書きしたまま残る。派生 ID 用の `ensure_unique` で衝突自体を塞ぎ、両経路のインメモリ更新を commit の後ろへ移した。
- **未検証の境界 (正直な記録)**: `create_persona` / `spawn_entity_from_blueprint` にはこのリポジトリに統合テストが 1 本も無く、上記の修正は `manager/ids.py` のヘルパ単体テストと、Region 経路の統合テストでしか押さえられていない。**呼び出し側の配線 (引数の渡し方・commit の順序) は実機で未確認。**

## 残っている論点 3 の中身

- **City ID**: `AdminService._validate_city_name` が既に ASCII 英数字 + `_` を強制している (作成・更新の両方)。文字種の穴は無い。
- **ペルソナ ID (AIID)**: 無検証。`custom_ai_id` も名前由来の自動生成も日本語が通る。AIID は `~/.saiverse/personas/<id>/` のフォルダ名になるため Building ID と同じ性質の永続キー。**ここが論点 3 の実体。**

## 症状

左サイドバー「場所」セクションの + ボタンからの Building 作成は名前しか聞かず、ID は `manager/admin.py create_building` が自動生成する。生成式は `name.lower().replace(' ', '_') + '_' + CITYNAME` — 日本語名は小文字化しても日本語のままなので、**日本語 ID がそのまま DB に入る**。カスタム ID を渡す経路 (グローバル設定側の作成フォーム等) も strip するだけで文字種検証なし。

## 実態 (2026-08-08 時点の本番 DB)

`鉄腕の道具店_city_a` / `霧雨の宿亭_city_a` / `白霧の社_city_a` の 3 件が既に日本語 ID で存在 (Mistvale / Region RPG 期の作成と推定)。数週間の運転で目に見える破損は出ていない — ただし**意図した検証は一度もされていない**。

## なぜ問題か

Building ID は識別子としてログのフォルダパス (`~/.saiverse/cities/<city>/buildings/<id>/log.json`)・`saiverse://building/<id>/image` URI・API パス引数に素で入る。日本語で「動いている」のは偶然の互換であって契約ではない。URI エンコード・パス正規化・外部連携 (アドオン/エクスポート) のどこかで割れる余地が残り続ける。

## 直すときの論点

1. **新規作成の口を塞ぐ**: ID 自動生成に文字種制約 (ASCII slug 化。日本語名→ローマ字変換はアイディア帳「ID 決めで日本語名→アルファベット自動変換」の論点と共通 — kuroshiro 級の読み変換 vs 簡易フォールバック `building_<連番>`)。カスタム ID 経路にも同じ検証。
2. **既存 3 件の扱い**: リネームは参照 (occupancy・ログフォルダ・履歴・saiverse:// を含む記憶内テキスト) の追従が要るため軽くない。実害が出るまで放置 + 新規だけ塞ぐ、が現実的な初手か。ID リネームは記憶内テキストの書き換えを伴わない範囲に限る (ペルソナ記憶の改変はしない)。
3. **同族**: City ID / ペルソナ ID の作成経路にも同じ無検証がないか点検 (アイディア帳「City ID 設計の相談」と合流)。

## 関連

- アイディア帳: 「ID 決めで日本語名→アルファベット自動変換」「City タイトルに名前でなく ID が出ている → City ID 設計の相談」
- 同じ検証で出た動線の話: フィード施設作成でその場で Building も作れるように (ideas.md) — 作成 UI を統合するならこの検証も同じ改修面
