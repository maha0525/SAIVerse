# Nvidia NIM APIキーの取得方法

## 概要

NVIDIA NIM の API カタログ（[build.nvidia.com](https://build.nvidia.com/)）は、NVIDIA が自社のGPUでいろいろな会社のモデル（Google の Gemma、Meta の Muse、Moonshot AI の Kimi、NVIDIA 自身の Nemotron など）を動かし、APIとして試せるようにしているサービスです。クレジットカードの登録なしで、無料の試用枠から使い始められます。

## 1. NVIDIAアカウントの作成とAPIキーの生成

1. [build.nvidia.com](https://build.nvidia.com/) で、使いたいモデルのページを開きます（例: Gemma 4 31B）。
2. ページ右側の「Get API Key」をクリックします。
3. メールアドレスを入力して「Next」をクリックします。これで NVIDIA Developer Program に登録されます。
   - すでに NVIDIA アカウントがある場合は、そのアカウントのメールアドレスを入力するとサインインを求められます。
   - アカウントがない場合は、パスワードを決めるよう求められるので、画面の案内に従ってアカウントを作ります。
4. モデルのページに戻ると、新しいAPIキーが表示されます。「Copy Key」でコピーして、安全な場所に控えます。

APIキーは [設定ページ](https://build.nvidia.com/settings) から作ることもできます。

## 2. 料金と無料枠の条件

**NIM の API カタログは、すべてのモデルに無料の試用枠があり、クレジットカードの登録は要りません**（2026年10月時点、[build.nvidia.com の案内](https://build.nvidia.com/llms.txt) で確認）。

ただし「試用」なので、条件があります（[NVIDIA API Trial Terms of Service](https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf) で確認）。

- **試用と評価のためのサービス**です。規約では、別途有料の契約を結ばない限り、社内での試験と評価の目的に限り、本番運用には使えないとされています。
- **使える量や期間には限りがあります。** 規約上、NVIDIA は試用のクレジットを付与して使った分を差し引くことがあり、使い切ったあとは追加の受け取りや購入が必要になることがあります。
- **回数制限**: 公式サイトの表示では、1分あたり最大40回、1日あたり10,000回です。ただしモデルによって違うことがあり、ほかの利用者の混雑で遅くなったり断られたりすることもあります。
- **入力してはいけないもの**: 規約では、機密情報や個人データ（そのサービスが明示的に認めている場合を除く）などを入力しないことが求められています。ペルソナとの会話に実在の人の個人情報を含める使い方には向きません。
- 規約上、NVIDIA は各セッションの終了時に入力と出力を保存・利用しないとしています。ただし、セキュリティや不正防止のための記録は例外です。

ブラウザ上で build.nvidia.com のモデルを試すぶんには、APIのクレジットは減りません（[NIM の FAQ](https://docs.api.nvidia.com/nim/docs/faq) で確認）。

## 3. 利用可能なモデル

SAIVerse に設定済みで、2026年10月9日の確認時点で NIM のモデル一覧に載っているモデルです。

- **Gemma 4 31B**: Google の画像対応モデルです。チュートリアルの NIM 設定の標準モデルです。SAIVerse の設定では考える工程を使わない既定のままなので、会話の返事が速めです。2026年9月に無料枠で動くことを確認しています。
- **Muse Glimmer 30B**: Meta の画像対応モデルです。SAIVerse の設定では考える深さの既定が low です（high 以上にすると、無料枠ではかなり遅くなります）。チュートリアルの NIM 設定の軽量モデル・Memory Weave モデル・画像要約モデルです。2026年9月に無料枠で動くことを確認しています。
- **Kimi K3**: Moonshot AI の大規模なマルチモーダル推論モデルで、画像に対応しています。常に考える工程を通るため、無料枠では1回の返事に数分かかることがあります。
- **Nemotron 3 Ultra**: NVIDIA の大規模推論モデルです。

一覧に載っていても、実際に呼べるとは限りません。たとえば Kimi K2.6 は、2026年9月の時点で一覧に載っていながら呼べない状態でした（Kimi K2.6 は SAIVerse には設定していません）。

### 提供が終わったモデル

NIM のモデルは、NVIDIA が決めた提供終了日を過ぎると呼べなくなり、一覧からも消えます。SAIVerse に設定していたもののうち、次の2つは提供が終わったため、2026年10月9日に組み込みから外しました（NIM のAPIが返した提供終了日で確認）。

- **DeepSeek V4 Flash 0731**: 2026年9月21日に提供終了
- **DeepSeek V4 Pro 0813**: 2026年9月14日に提供終了

これらを選んでいる場合は、別のモデルを選び直してください。ほかのモデルでも、選んでいたモデルが一覧から消えた場合は同じように選び直してください。

## 4. 特徴

- **OpenAI互換API**: すべてのモデルが OpenAI の Chat Completions API と同じ形で呼べます。SAIVerse では接続先を設定済みなので、キーを入れるだけで使えます。
- **無料で試せる**: クレジットカードなしで、いろいろな会社の大きなモデルを試せます。
- **速さは混み具合しだい**: 無料枠は共用なので、時間帯やモデルによって返事の速さが大きく変わります。安定した速さが必要な使い方には向きません。

## 5. APIエンドポイント

Nvidia NIM はOpenAI互換APIを提供します：
```
https://integrate.api.nvidia.com/v1
```

## 環境変数

SAIVerseでは以下の環境変数名を使用します：
```
NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 参考リンク

- [NVIDIA API カタログ（build.nvidia.com）](https://build.nvidia.com/)
- [モデル一覧](https://build.nvidia.com/models)
- [APIのはじめ方（公式）](https://docs.api.nvidia.com/nim/docs/api-quickstart)
- [NIM の FAQ](https://docs.api.nvidia.com/nim/docs/faq)
- [NVIDIA API Trial Terms of Service（試用規約、PDF）](https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf)
