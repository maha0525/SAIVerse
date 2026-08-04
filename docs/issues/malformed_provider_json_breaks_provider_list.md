# 壊れたプロバイダ JSON 一枚でプロバイダ一覧が 500 になる

**起票**: 2026-08-04（資格情報の層束縛の改修中に発見。一度直そうとして、より悪い状態を作ったので差し戻した）
**状態**: 未着手
**関連**: `saiverse/provider_configs.py: load_configs()`、`saiverse/data_paths.py: iter_files_with_layer()`、`api/routes/providers.py: _to_provider_info()`

## 何が起きるか

`~/.saiverse/user_data/providers/*.json` の一枚に型の打ち間違いがあると、**そのプロバイダだけでなく一覧全体**が壊れる。

```json
{ "id": "typo", "display_name": "Typo", "protocol": "openai_compat",
  "base_url": "https://example.com/v1", "api_key_env": 0 }
```

`_to_provider_info()` が `ProviderInfo`（`api_key_env` は `Optional[str]`、`display_name` と `protocol` は必須 `str`）を組み立てるところで検証に失敗し、`GET /api/providers` が 500 を返す。`display_name: null` や `protocol: null` でも同じ（キーが存在するので既定値に置き換わらない）。UI のプロバイダタブが丸ごと開かなくなる。

## 素朴な「読み飛ばし」では直らない（一度やって差し戻した）

`load_configs()` で不正な設定を警告付きスキップする、という手を一度入れたが**より悪い**。`iter_files_with_layer()` はファイル名を `seen_names` に登録してから yield するため、上位層のファイルを後からスキップしても**同名の下位層ファイルはもう列挙されない**。結果、`user_data/providers/openrouter.json` を打ち間違えると同梱の `openrouter` まで消え、それを参照する 39 モデルが全滅する。エンドポイント 1 本の 500 より被害が大きい。実測で確認済み。

さらに、同じ id を別のファイル名で置いた場合は `seen_keys` の側で弾かれるため下位層が採用され、**結果がファイル名に依存する**という分かりにくさも残る。

## 部分書き込みでも同じ結末になる（2026-08-05 に片側だけ対処）

保存が途中で止まって半端な JSON が残った場合も、次のロードで同じ「provider ごと消える」に落ちる。書き込み側は一時ファイルへ書いてから `os.replace()` で置き換える形にしたので、この経路からの破損は起きなくなった。ただし**下位層へ落ちない問題そのものは残っている**ので、手で壊れたファイルを置けば同じことになる。

また、`save_provider()` は provider の再読み込みとモデルの再解決を続けて別々に行う。その隙間を並行する読み手が見ると、provider は新しく model は旧いという一瞬の混在が起こりうる。両方を一つのスナップショットとして差し替える形にするのが本筋だが、グローバル設定の公開方法そのものを変える話になるためここに記録するに留める。

## 直すなら

読み飛ばしの判断を、ファイル名を確定する前（＝ローダーの内側）に移す必要がある。

1. `iter_files_with_layer()` を「候補を全部返す」形にするか、妥当性判定を渡せる形にして、**正常に読めた候補で初めて shadowing を確定**する
2. 必須フィールド（`display_name` / `protocol`）は `null` も不正として扱い、任意フィールド（`base_url` / `api_key_env`）だけ `null` を許す
3. 警告には絶対パス・層・provider id・下位層に落ちたかどうかを含める（ファイル名だけでは何が起きたか追えない）
4. 同名衝突と下位採用、両フィールドの `null` を API テストで固定する

## なぜ今回の改修に含めなかったか

資格情報の層束縛という主題とは別の話で、しかも上のとおり loader の列挙構造に手を入れないと正しく直らない。中途半端に塞ぐと被害が増えることを実際に確認したので、構造ごと直せるときにまとめてやる。
