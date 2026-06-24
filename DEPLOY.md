# Capital Gain Radar 公開手順

このアプリは `outputs` フォルダを GitHub Pages で公開し、GitHub Actions が毎日データを更新する構成です。

## 初回設定

1. GitHubで公開リポジトリを作成します。
2. このフォルダの内容をそのリポジトリへpushします。
3. GitHubのリポジトリ画面で `Settings` → `Pages` を開きます。
4. `Build and deployment` の `Source` を `GitHub Actions` にします。
5. `Actions` タブで `Update data and deploy Pages` を手動実行します。

初回デプロイ後、GitHub Pages のURLでスマホからも開けます。PCの起動やローカルサーバーは不要です。

## EDINET財務指標

PER、PBR、ROE、ROA、自己資本比率などは、EDINET API v2の有価証券報告書XBRLから算出します。

1. EDINET API v2の無料APIキーを取得します。
2. GitHubリポジトリで `Settings` → `Secrets and variables` → `Actions` を開きます。
3. `New repository secret` で `EDINET_API_KEY` を作成し、APIキーを保存します。
4. `Actions` タブで `Update data and deploy Pages` を手動実行します。

`EDINET_API_KEY` が未設定の場合もアプリは動作します。その場合、銘柄詳細のPER/PBR/ROEなどは `未取得` と表示されます。

## 自動更新

`.github/workflows/deploy-pages.yml` が毎日 06:15 JST に実行され、`outputs/data/latest-candidates.json` を更新してからPagesへ公開します。

## 注意

- 公開リポジトリに置くため、URLを知っている人はアプリとJSONデータを閲覧できます。
- データ取得元の応答やGitHub Actionsの混雑により、更新が失敗する日があります。その場合は前回デプロイ済みのページが引き続き表示されます。
