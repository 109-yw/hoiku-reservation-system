# 一時預かり保育予約システム

## Python API（試作）

Python側ではFlaskとSQLiteを使い、予約枠の空き状況確認、仮押さえ、仮押さえの確定・取消を提供します。初回起動時に `SQL/childcare_v2 (1).sql` からデータベースを作成します。スキーマのサンプルデータも登録されます。

```powershell
python -m pip install -r requirements.txt
python hoiku.py
```

サーバーは `http://127.0.0.1:5000` で起動します。DBの保存先は `HOIKU_DATABASE` 環境変数で変更できます。
トップページと予約カレンダーも同じサーバーから配信されます。`http://127.0.0.1:5000/` を開いてください。
ログイン画面では保護者アカウントを新規登録でき、ログインするとマイページに移動します。登録時のパスワードはハッシュ化して保存します。セッション署名鍵は `HOIKU_SECRET_KEY` 環境変数で設定できます。

ローカルテスト用アカウントは起動時に用意されます（パスワードはハッシュ保存）。

| 保護者 | メールアドレス | パスワード |
|---|---|---|
| 佐藤 敏夫 | `hogo1@example.com` | `satou` |
| 鈴木 一郎 | `hogo2@example.com` | `suzuki` |

上記はテスト専用の弱いパスワードです。実サービスで使用したり、インターネットに公開したりしないでください。

### API

- `GET /api/health` — 起動確認
- `GET /api/availability?care_date=YYYY-MM-DD` — 1時間ごとの空き状況
- `GET /api/availability/month?year=YYYY&month=M` — 月内の開所日と空き状況
- `POST /api/holds` — 仮押さえを作成
- `POST /api/holds/<reservation_id>/confirm` — 仮押さえを確定
- `DELETE /api/holds/<reservation_id>` — 仮押さえを取り消す

予約カレンダーは月別APIから開所日・空き状況を取得し、日付選択後に仮押さえと確定を行います。予約画面の児童ID入力欄は、承認済み児童の `child_id` を入力してください。

仮押さえのJSON例:

```json
{
  "child_id": 1,
  "care_date": "2026-11-11",
  "course": "morning"
}
```

`course` は `morning`（9:00〜13:00）、`afternoon`（13:00〜17:00）、`full_day`（9:00〜17:00）から選びます。午前・午後の仮押さえが同じ児童・同じ日に揃うと、同じ予約を1日コースへ自動でまとめ、料金を8時間分に更新します。確定済みの予約は自動変更せず、コース追加を拒否します。

予約は承認済みカルテの児童のみ受け付け、予約日の年齢区分から料金と保育士コストを決めます。仮押さえ作成・確定時はSQLiteの `BEGIN IMMEDIATE` で同時更新を直列化します。

テスト:

```powershell
python -m unittest discover -s tests
```

この段階ではローカル開発用のAPIです。決済、通知、キャンセル待ち繰り上げ、C言語エンジン連携はまだ実装していません。CSRF対策など本番向けの対策も未整備です。インターネットに公開しないでください。
