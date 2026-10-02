# Intent: 単体テストの外部環境依存を閉じる

**ステータス**: 検証待ち（2026-10-02、Linux の隔離回帰済み・差分レビュー待ち）

## 全体と所有境界

開発者は、実 API を呼ばないテストを DNS 接続・個人の API キー・OS に依存せず
実行できなければならない。設定の読み込み → 接続先と資格情報の検査 → クライアントの
組み立て → MockTransport でのリクエスト検査、という経路は本物の実装を通す。
本番の URL 検査を緩めたり、資格情報検査を丸ごと差し替えたりして通してはいけない。

## 今回の範囲

- HTTP が既にモックされているリクエスト単体テストと、通信しない設定検査だけが
  明示的な `mock_provider_network` fixture を使う。各利用箇所が期待する公開ホストを
  列挙し、それ以外の名前解決は失敗させる。IP リテラルと localhost のアドレス種別は保つ。
- fixture の有効範囲だけソケット接続も拒否する。MockTransport の付け忘れを実通信へ
  逃がさない。他の URL セキュリティ検査やローカル HTTP 結合テストには適用しない。
- 通常 LLM を選べることを調べる自動想起テストは、そのテストだけダミーのキーを置く。
  本物のキーやペルソナを使わず、判断の実行は既存の fake に任せる。
- snapshot の rooted path は、POSIX では入口で拒否、Windows では展開先の包含検査で
  拒否される。OS 差を明示し、展開先検査そのものも一時 ZIP で独立に固定する。

## 検証

未変更ベースと統合ブランチの失敗一覧を照合して既存の環境依存であることを確認する。
対象テストと fixture 自体の契約テストを、空の HOME / SAIVERSE_HOME / ログ先で実行する。
公開ホストの許可、未知ホストの拒否、private / metadata / loopback の区別、接続の拒否を
それぞれ確認する。実サービスへの疎通や Windows 実機での実行はこの検証の対象外。

## 経緯

- 2026-10-02: 未変更ベースの 76 件の失敗（subtest を含む）のうち、統合ブランチと
  共通する 74 件がこの範囲。HTTP モックより前の DNS 検査、未設定の Gemini キー、
  POSIX で Windows の Path 動作を期待していた assertion が原因。
- 対象 8 ファイルは 394 passed / 48 subtests passed。外部 DNS を明示的に失敗させる
  別ハーネスでも同じ結果。fixture の適用範囲の独立レビューでは指摘なし。
- 全体回帰は 7308 passed / 424 subtests passed、既存の legacy log archive API の
  1 件だけ失敗。この実行では Node を PATH に含めず frontend 検査が skip されたため、
  Node を加えて再検査し、未変更ベースと同じ TypeScript 依存未導入による localization
  の失敗を確認した。Next.js の依存も未導入で dev-origin 検査は skip。本修正による
  新しい失敗はないが、全体 green や frontend 検証済みとは扱わない。
- Python 変更箇所の ruff、差分検査、台帳検査は合格。Windows 実機での実行は未確認。
