# 一時預かり保育予約システム

## Python API（試作）

Python側ではFlaskとSQLiteを使い、予約枠の空き状況確認、仮押さえ、仮押さえの確定・取消を提供します。CSVデータを使用して初回データベースを作成します。起動時に `SQL/childcare_v2.sql` のスキーマと `SQL/csv_export_all.zip` 内の全テーブルCSVを読み込み、CSVに含まれるIDと親子関係をそのまま反映します。同じ保護者IDを持つ複数児童は同一世帯として扱われます。データベースの初回作成に成功した後は通常起動で再インポートせず、予約や登録内容を保持します。既存のDBは初回移行時にCSV全体で置き換わるため、CSVにない予約やアカウントも置き換えられます。

```powershell
python -m pip install -r requirements.txt
python hoiku.py
```

サーバーは `http://127.0.0.1:5000` で起動します。DBの保存先は `HOIKU_DATABASE` 環境変数で変更できます。
トップページと予約カレンダーも同じサーバーから配信されます。`http://127.0.0.1:5000/` を開いてください。
ログイン画面 (`/login` または `/html/login.html`) では保護者アカウントを新規登録でき、ログインすると `html/user_home.html` のユーザー別マイページ (`/html/user_home.html`) に移動します。ログイン中の保護者IDを使って、その世帯の児童・予約・ポイントを表示します。登録時のパスワードはハッシュ化して保存します。セッション署名鍵は `HOIKU_SECRET_KEY` 環境変数で設定できます。

CSV内の保護者アカウントはパスワードが `dummy_hash` のため、そのままではログインできません。ログインが必要な場合は登録画面からアカウントを作成してください。CSV上の既存アカウントに設定するパスワードは別途決定するまで登録していません。

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

## C言語の予約ロジック

C言語の予約処理は `c/hoiku.c`、公開関数の宣言は `c/hoiku.h` にあります。既存のSQLiteデータベースを開き、その接続（`sqlite3 *`）を関数に渡して使います。ヘッダーは `../SQL/sqlite3.h` を参照するため、リポジトリ内の `SQL/` フォルダと `c/` フォルダの配置を保ってください。

### 実装している機能

- **同時予約の排他制御:** 予約の確認から登録まで `BEGIN IMMEDIATE` トランザクションで直列化します。SQLiteが使用中の場合は最大10秒待ちます。成功時の `COMMIT`、失敗時の `ROLLBACK` でトランザクションとロックを終了します。
- **予約とキャンセル待ち:** `hoiku_reserve` は開所日、予約時間、児童の承認状態を確認して予約します。予約時間は9時から17時まで、2時間以上で指定できます。利用可能なら仮押さえ（`held`）と時間枠を登録し、上限を超える場合は時間枠を確保せずキャンセル待ち（`waitlisted`）に登録します。
- **年齢別リソース計算:** 予約日の生年月日から児童の月齢を計算し、`age_classes` から料金と `staff_cost` を取得します。予約する各1時間枠について、利用中の児童数と保育士コストを合計します。人数は `operating_days.max_children`、保育士コストは `operating_days.staff_count` と5人分のうち小さい方を上限に判定します。
- **キャンセル待ちの優先順位:** `hoiku_get_waitlist_order` は保護者の `priority_points` が多い順、同じポイントなら受付時刻が早い順に予約IDを返します。受付時刻は `attempted_at_ms` 引数でUnix時刻のミリ秒として渡します。
- **キャンセルと枠の解放:** `hoiku_cancel` は予約状態をキャンセルにし、その予約の時間枠を削除します。キャンセルによって空いた枠からキャンセル待ちを自動で繰り上げる処理はありません。

### C APIの使い方

最初に `hoiku_initialize(db, error, sizeof(error))` を呼び、SQLite接続の設定を行います。予約APIの戻り値は `HOIKU_RESERVED`（仮押さえ登録）、`HOIKU_WAITLISTED`（キャンセル待ち登録）、`HOIKU_ERROR`（失敗）です。失敗時は `error` に説明が入り、成功時は `reservation_id` に予約IDが入ります。

```c
#include "c/hoiku.h"

char error[512];
sqlite3_int64 reservation_id;
int result = hoiku_reserve(
    db,
    1,                  /* 児童ID */
    "2026-11-11",       /* 予約日 */
    9, 12,              /* 9時から12時まで */
    attempted_at_ms,    /* 申込受付時刻（Unix時刻のミリ秒） */
    &reservation_id,
    error, sizeof(error)
);
```

キャンセル待ち一覧の配列は呼び出し側が `free()` で解放します。`c/hoiku.c` は `main` 関数を持たない部品のため、単独では実行ファイルになりません。VS CodeのCビルドタスクではオブジェクトファイルとしてコンパイルします。実際にアプリとして実行する場合は、`main` 関数を持つプログラムと `SQL/sqlite3.c` を合わせてビルド・リンクしてください。

このCロジックとPython APIはまだ接続されていません。Cロジック用のDBは、記載のテーブルと列を持つ `childcare_v2.sql` で初期化してください。

テスト:

```powershell
python -m unittest discover -s tests
```

この段階ではローカル開発用のAPIです。決済、通知、キャンセル待ち繰り上げ、C言語エンジン連携はまだ実装していません。CSRF対策など本番向けの対策も未整備です。インターネットに公開しないでください。
