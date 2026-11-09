/* C予約ロジック本体。公開する関数の宣言は同じフォルダのhoiku.hにある。 */
#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "hoiku.h"

/*
 * エラー内容を呼び出し元に渡すための共通関数。
 * error_sizeより長いメッセージはvsnprintfが自動的に切り詰めるため、
 * error用の領域をはみ出してメモリを壊すことを防げる。
 */
static void set_error(char *error, size_t error_size, const char *format, ...)
{
    va_list arguments;

    /* エラー文字列の保存先がなければ、何もせず戻る。 */
    if (error == NULL || error_size == 0) {
        return;
    }

    /* printfと同じ形式で、可変個数の値をエラー文に埋め込む。 */
    va_start(arguments, format);
    vsnprintf(error, error_size, format, arguments);
    va_end(arguments);
}

/*
 * INSERTやUPDATEなど、結果の一覧を返さないSQLを実行する。
 * SQLiteのエラーを日本語のメッセージと一緒に呼び出し元へ返す。
 */
static int execute_sql(sqlite3 *db, const char *sql, char *error,
                       size_t error_size)
{
    char *sqlite_error = NULL;
    /* sqlite3_execはSQL文を実行し、失敗時には詳細なエラーを返す。 */
    int result = sqlite3_exec(db, sql, NULL, NULL, &sqlite_error);

    if (result != SQLITE_OK) {
        /* SQLiteが作ったエラー文字列があればそれを使う。 */
        set_error(error, error_size, "SQLiteエラー: %s",
                  sqlite_error != NULL ? sqlite_error : sqlite3_errmsg(db));
        /* sqlite3_execが確保したエラー文字列はSQLiteの関数で解放する。 */
        sqlite3_free(sqlite_error);
        return HOIKU_ERROR;
    }
    return SQLITE_OK;
}

/*
 * SQL文を実行できる形に準備する。
 * ? を含むSQLでは、このあとsqlite3_bind_*で値を安全に設定する。
 */
static int prepare_sql(sqlite3 *db, const char *sql, sqlite3_stmt **statement,
                       char *error, size_t error_size)
{
    /* SQL文字列をSQLiteの「準備済みステートメント」に変換する。 */
    int result = sqlite3_prepare_v2(db, sql, -1, statement, NULL);

    if (result != SQLITE_OK) {
        set_error(error, error_size, "SQLの準備に失敗しました: %s",
                  sqlite3_errmsg(db));
        return HOIKU_ERROR;
    }
    return SQLITE_OK;
}

/*
 * 途中まで行った変更を取り消す。
 * 例えば、予約を追加したあと時間枠の登録に失敗した場合、
 * 予約だけが残る不整合を防ぐため、トランザクション全体を戻す。
 */
static int rollback_transaction(sqlite3 *db, char *error, size_t error_size)
{
    char original_error[512];
    char *sqlite_error = NULL;
    int result;

    /* ロールバックのエラーで、最初に起きた原因を上書きしないよう退避する。 */
    if (error != NULL && error_size > 0) {
        snprintf(original_error, sizeof(original_error), "%s", error);
    } else {
        original_error[0] = '\0';
    }

    /* BEGIN以降の変更をすべて取り消し、DBロックを解放する。 */
    result = sqlite3_exec(db, "ROLLBACK", NULL, NULL, &sqlite_error);
    if (result != SQLITE_OK) {
        set_error(error, error_size, "%s（ロールバックにも失敗: %s）",
                  original_error,
                  sqlite_error != NULL ? sqlite_error : sqlite3_errmsg(db));
    }
    sqlite3_free(sqlite_error);
    return HOIKU_ERROR;
}

/*
 * 接続ごとの外部キー制約を有効にし、ミリ秒精度のキャンセル待ち順を
 * 保存する補助テーブルを作る。既存の予約テーブルは変更しない。
 */
int hoiku_initialize(sqlite3 *db, char *error, size_t error_size)
{
    /*
     * 外部キーのチェックを有効にし、キャンセル待ち受付時刻を保存する
     * 補助テーブルを作る。このCREATE TABLEは未作成の場合だけ実行される。
     */
    const char *sql =
        "PRAGMA foreign_keys = ON;"
        "CREATE TABLE IF NOT EXISTS reservation_attempts ("
        "  reservation_id INTEGER PRIMARY KEY,"
        "  attempted_at_ms INTEGER NOT NULL,"
        "  FOREIGN KEY (reservation_id) REFERENCES reservations(reservation_id)"
        "    ON DELETE CASCADE"
        ");";
    int result;

    /* DBが開かれていない状態では、それ以降の処理を続けられない。 */
    if (db == NULL) {
        set_error(error, error_size, "データベース接続がありません。");
        return HOIKU_ERROR;
    }
    if (error != NULL && error_size > 0) {
        error[0] = '\0';
    }
    /* 他の処理がDBを使用中なら、すぐ失敗せず最大10秒待つ。 */
    result = sqlite3_busy_timeout(db, 10000);
    if (result != SQLITE_OK) {
        set_error(error, error_size, "SQLiteのロック待ち設定に失敗しました: %s",
                  sqlite3_errmsg(db));
        return HOIKU_ERROR;
    }
    /* SQLite接続の設定と補助テーブル作成を実行する。 */
    return execute_sql(db, sql, error, error_size) == SQLITE_OK
               ? SQLITE_OK
               : HOIKU_ERROR;
}

/*
 * settingsテーブルから、指定された設定キーの数値を読む。
 * 例: keyに"hold_timeout_sec"を渡すと、仮押さえの秒数がvalueに入る。
 */
static int read_setting(sqlite3 *db, const char *key, double *value,
                        char *error, size_t error_size)
{
    sqlite3_stmt *statement = NULL;
    int result;

    /* settingsテーブルからkeyに一致する設定値を1件読み込む。 */
    if (prepare_sql(db, "SELECT CAST(value AS REAL) FROM settings WHERE key = ?",
                    &statement, error, error_size) != SQLITE_OK) {
        return HOIKU_ERROR;
    }
    /* SQLの?にkeyを設定する。文字列をSQL文に直接つなげないため安全。 */
    sqlite3_bind_text(statement, 1, key, -1, SQLITE_TRANSIENT);
    result = sqlite3_step(statement);
    if (result == SQLITE_ROW) {
        /* SELECT結果の1列目を数値に変換して呼び出し元へ返す。 */
        *value = sqlite3_column_double(statement, 0);
        sqlite3_finalize(statement);
        return SQLITE_OK;
    }

    /* SQLITE_DONEなら該当する設定がない。その他はDB読み取りエラー。 */
    if (result == SQLITE_DONE) {
        set_error(error, error_size, "settingsに '%s' がありません。", key);
    } else {
        set_error(error, error_size, "settingsの読み取りに失敗しました: %s",
                  sqlite3_errmsg(db));
    }
    sqlite3_finalize(statement);
    return HOIKU_ERROR;
}

/*
 * 予約処理の全体を行う関数。
 * 1. 入力と開所日を確認する
 * 2. 児童の年齢区分と利用リソースを調べる
 * 3. 空きがあれば仮押さえ、なければキャンセル待ちを登録する
 *
 * attempted_at_msには申込受付時刻をUnix時刻のミリ秒で渡す。
 * 戻り値は予約済み=HOIKU_RESERVED、キャンセル待ち=HOIKU_WAITLISTED、
 * エラー=HOIKU_ERROR。成功した予約IDはreservation_idに格納する。
 */
int hoiku_reserve(sqlite3 *db, sqlite3_int64 child_id, const char *care_date,
                  int start_hour, int end_hour, sqlite3_int64 attempted_at_ms,
                  sqlite3_int64 *reservation_id, char *error,
                  size_t error_size)
{
    /* statementは、実行中のSQL文を一時的に保持する変数。 */
    sqlite3_stmt *statement = NULL;
    /* 年齢区分表から取得する予約時点の年齢区分IDと保育士コスト。 */
    sqlite3_int64 age_class_id;
    double staff_cost;
    /* 開所日ごとの保育士数・定員と、仮押さえの有効時間。 */
    double configured_staff_count;
    double configured_max_children;
    double hold_timeout;
    /* 年齢区分ごとの1時間料金と、確認対象にする時間。 */
    int hourly_fee;
    int hour;
    int is_open;
    /* 1ならキャンセル待ち、0なら通常の仮押さえを作る。 */
    int waitlisted = 0;
    int result;

    /* エラー文字列と予約IDを初期状態にする。 */
    if (error != NULL && error_size > 0) {
        error[0] = '\0';
    }
    if (reservation_id != NULL) {
        *reservation_id = 0;
    }
    /*
     * SQLスキーマの時間制限（9〜17時）と最短2時間を確認する。
     * 予約の開始・終了は整数の時刻（例: 9から12）で指定する。
     */
    if (db == NULL || care_date == NULL || reservation_id == NULL ||
        child_id <= 0 || start_hour < 9 || start_hour > 15 ||
        end_hour < 11 || end_hour > 17 || end_hour - start_hour < 2) {
        set_error(error, error_size, "予約情報が不正です。");
        return HOIKU_ERROR;
    }
    /* ロック待ちや外部キーなど、この接続に必要な設定を行う。 */
    if (hoiku_initialize(db, error, error_size) != SQLITE_OK) {
        return HOIKU_ERROR;
    }

    /*
     * BEGIN IMMEDIATEで書き込みトランザクションを開始する。
     * これにより、空き枠の確認中に別の予約処理が割り込むことを防ぐ。
     * 成功時はCOMMIT、途中で失敗したときはROLLBACKでロックを解放する。
     */
    if (execute_sql(db, "BEGIN IMMEDIATE", error, error_size) != SQLITE_OK) {
        return HOIKU_ERROR;
    }

    /*
     * 期限を過ぎた仮押さえをexpired（期限切れ）にする。
     * statusがheld（仮押さえ中）のままだと枠が埋まった扱いになるため、
     * 古い予約を先に無効化してから新しい予約の空き状況を調べる。
     */
    if (prepare_sql(db,
                    "UPDATE reservations "
                    "SET status = 'expired', hold_expires_at = NULL, "
                    "    updated_at = datetime('now','localtime') "
                    "WHERE care_date = ? AND status = 'held' "
                    "  AND hold_expires_at <= datetime('now','localtime')",
                    &statement, error, error_size) != SQLITE_OK) {
        goto failed;
    }
    /* SQLの1個目の?に対象日を入れて、対象日だけを更新する。 */
    sqlite3_bind_text(statement, 1, care_date, -1, SQLITE_TRANSIENT);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        set_error(error, error_size, "期限切れ予約の更新に失敗しました: %s",
                  sqlite3_errmsg(db));
        sqlite3_finalize(statement);
        statement = NULL;
        goto failed;
    }
    sqlite3_finalize(statement);
    statement = NULL;

    /*
     * 期限切れ予約にひもづく時間枠も削除する。
     * reservation_hoursに行が残ると空きとして集計されないため。
     */
    if (prepare_sql(db,
                    "DELETE FROM reservation_hours "
                    "WHERE care_date = ? AND reservation_id IN ("
                    "  SELECT reservation_id FROM reservations "
                    "  WHERE care_date = ? AND status = 'expired'"
                    ")",
                    &statement, error, error_size) != SQLITE_OK) {
        goto failed;
    }
    /* 2つの?には、枠の日付と予約を検索する日付をそれぞれ設定する。 */
    sqlite3_bind_text(statement, 1, care_date, -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 2, care_date, -1, SQLITE_TRANSIENT);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        set_error(error, error_size, "期限切れ枠の解放に失敗しました: %s",
                  sqlite3_errmsg(db));
        sqlite3_finalize(statement);
        statement = NULL;
        goto failed;
    }
    sqlite3_finalize(statement);
    statement = NULL;

    /*
     * operating_days.csvの内容を取り込んだoperating_daysテーブルで、
     * 予約日が登録されているか、またis_openが1（開所）かを確認する。
     * このテーブルには日付と開閉所だけがあるため、人数・保育士の上限は
     * 後ほどsettingsテーブルから別に読み込む。
     */
    if (prepare_sql(db,
                    "SELECT is_open FROM operating_days WHERE care_date = ?",
                    &statement, error, error_size) != SQLITE_OK) {
        goto failed;
    }
    sqlite3_bind_text(statement, 1, care_date, -1, SQLITE_TRANSIENT);
    result = sqlite3_step(statement);
    if (result == SQLITE_ROW) {
        is_open = sqlite3_column_int(statement, 0);
    }
    sqlite3_finalize(statement);
    statement = NULL;
    /* CSVに日付がない場合は、開所日として扱わず予約を受け付けない。 */
    if (result == SQLITE_DONE) {
        set_error(error, error_size, "指定日の開所情報がありません。");
        goto failed;
    }
    if (result != SQLITE_ROW) {
        set_error(error, error_size, "開所日の確認に失敗しました: %s",
                  sqlite3_errmsg(db));
        goto failed;
    }
    /* operating_days.csvのis_openが0なら、その日は休業日として拒否する。 */
    if (is_open != 1) {
        set_error(error, error_size, "指定日は休業日です。");
        goto failed;
    }

    /*
     * childcare_v2.sqlでは定員がsettingsテーブルに保存される。
     * 開所日CSVは日付と開閉所のみなので、予約枠の上限はここから取得する。
     */
    if (read_setting(db, "staff_count", &configured_staff_count, error,
                     error_size) != SQLITE_OK ||
        read_setting(db, "max_children", &configured_max_children, error,
                     error_size) != SQLITE_OK) {
        goto failed;
    }
    /*
     * 施設の設定が5人より大きくても、今回の要件に合わせて
     * 保育リソースは5人分までに制限する。施設側の値が小さい場合は
     * その値を使うので、実際の上限は「設定値と5の小さい方」になる。
     */
    if (configured_staff_count < 0 || configured_max_children < 0) {
        set_error(error, error_size, "予約上限の設定値が不正です。");
        goto failed;
    }
    if (configured_staff_count > 5.0) {
        configured_staff_count = 5.0;
    }

    /*
     * 生年月日と予約日から予約日時点の月齢を計算する。
     * 年の差を月に直し、月の差を足し、誕生日がまだ来ていなければ
     * 1か月引く。得られた月齢をage_classesの範囲に照合する。
     * 児童と保護者をguardian_idで結合し、CSVから取り込んだ親子関係も確認する。
     */
    if (prepare_sql(
            db,
            "WITH child_age AS ("
            "  SELECT child_id, karte_status,"
            "    (CAST(strftime('%Y', ?) AS INTEGER)"
            "       - CAST(strftime('%Y', birth_date) AS INTEGER)) * 12"
            "    + CAST(strftime('%m', ?) AS INTEGER)"
            "       - CAST(strftime('%m', birth_date) AS INTEGER)"
            "    - (strftime('%d', ?) < strftime('%d', birth_date))"
            "      AS age_months"
            "  FROM children c"
            "  JOIN guardians g ON g.guardian_id = c.guardian_id"
            "  WHERE c.child_id = ?"
            ")"
            "SELECT a.age_class_id, a.staff_cost, a.hourly_fee_yen"
            " FROM child_age c JOIN age_classes a"
            "   ON c.age_months >= a.min_age_months"
            "  AND c.age_months < a.max_age_months"
            /* 承認済みカルテだけを予約対象にする。 */
            " WHERE c.karte_status = 'approved'",
            &statement, error, error_size) != SQLITE_OK) {
        goto failed;
    }
    /*
     * SQL中の3つの日付?は年・月・日を計算するために使う。
     * 最後の?には調べる児童IDを設定する。
     */
    sqlite3_bind_text(statement, 1, care_date, -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 2, care_date, -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 3, care_date, -1, SQLITE_TRANSIENT);
    sqlite3_bind_int64(statement, 4, child_id);
    result = sqlite3_step(statement);
    /* 対象の児童がない、承認前、または年齢区分が見つからない場合。 */
    if (result == SQLITE_DONE) {
        sqlite3_finalize(statement);
        statement = NULL;
        set_error(error, error_size,
                  "承認済みの児童、または予約日に対応する年齢区分がありません。");
        goto failed;
    }
    if (result != SQLITE_ROW) {
        set_error(error, error_size, "児童の年齢区分の取得に失敗しました: %s",
                  sqlite3_errmsg(db));
        sqlite3_finalize(statement);
        statement = NULL;
        goto failed;
    }
    /* SELECT結果から年齢区分ID、1人あたりの保育コスト、時間単価を取得。 */
    age_class_id = sqlite3_column_int64(statement, 0);
    staff_cost = sqlite3_column_double(statement, 1);
    hourly_fee = sqlite3_column_int(statement, 2);
    sqlite3_finalize(statement);
    statement = NULL;

    /*
     * 同じ児童・同じ日に、時間が重なる有効予約がないか調べる。
     * Aの開始 < Bの終了 かつ Aの終了 > Bの開始なら時間が重なる。
     * 待機中も同じ時間を二重に申し込めないよう有効予約として扱う。
     */
    if (prepare_sql(
            db,
            "SELECT 1 FROM reservations r"
            " WHERE r.child_id = ? AND r.care_date = ?"
            "   AND r.start_hour < ? AND r.end_hour > ?"
            "   AND (r.status IN ('confirmed','waitlisted') OR"
            "        (r.status = 'held' AND"
            "         r.hold_expires_at > datetime('now','localtime')))"
            " LIMIT 1",
            &statement, error, error_size) != SQLITE_OK) {
        goto failed;
    }
    /* ?には児童ID、日付、申込終了時刻、申込開始時刻を設定する。 */
    sqlite3_bind_int64(statement, 1, child_id);
    sqlite3_bind_text(statement, 2, care_date, -1, SQLITE_TRANSIENT);
    sqlite3_bind_int(statement, 3, end_hour);
    sqlite3_bind_int(statement, 4, start_hour);
    result = sqlite3_step(statement);
    sqlite3_finalize(statement);
    statement = NULL;
    /* 1行見つかった場合は重複予約、0行なら予約可能。 */
    if (result == SQLITE_ROW) {
        set_error(error, error_size, "同じ時間帯に有効な予約があります。");
        goto failed;
    }
    if (result != SQLITE_DONE) {
        set_error(error, error_size, "既存予約の確認に失敗しました: %s",
                  sqlite3_errmsg(db));
        goto failed;
    }

    /*
     * 仮押さえを何秒間有効にするかをsettingsから取得する。
     * 人数・保育士の上限は、先ほど取得した日別の値を使う。
     */
    if (read_setting(db, "hold_timeout_sec", &hold_timeout, error,
                     error_size) != SQLITE_OK) {
        goto failed;
    }
    if (hold_timeout <= 0) {
        set_error(error, error_size, "仮押さえ時間の設定が不正です。");
        goto failed;
    }

    /*
     * 予約時間を1時間ごとに分け、各時間の利用人数と保育コストを調べる。
     * 例えば9時から12時なら、9時・10時・11時の3枠を確認する。
     * いずれかの枠で人数または保育士コストが上限を超えるなら、
     * 全時間帯を確保せず、申込全体をキャンセル待ちにする。
     */
    if (prepare_sql(
            db,
            "SELECT COUNT(*), COALESCE(SUM(r.staff_cost), 0.0)"
            " FROM reservation_hours h"
            " JOIN reservations r ON r.reservation_id = h.reservation_id"
            " WHERE h.care_date = ? AND h.hour = ?"
            "   AND (r.status = 'confirmed' OR"
            "        (r.status = 'held' AND"
            "         r.hold_expires_at > datetime('now','localtime')))",
            &statement, error, error_size) != SQLITE_OK) {
        goto failed;
    }
    /* start_hour以上、end_hour未満の各時間を順に確認する。 */
    for (hour = start_hour; hour < end_hour; ++hour) {
        /* 同じSQLを次の時間にも使うため、前回の結果と値をリセットする。 */
        sqlite3_reset(statement);
        sqlite3_clear_bindings(statement);
        /* ?に日付と確認対象の時間を設定してSQLを実行する。 */
        sqlite3_bind_text(statement, 1, care_date, -1, SQLITE_TRANSIENT);
        sqlite3_bind_int(statement, 2, hour);
        result = sqlite3_step(statement);
        if (result != SQLITE_ROW) {
            set_error(error, error_size, "予約枠の集計に失敗しました: %s",
                      sqlite3_errmsg(db));
            sqlite3_finalize(statement);
            statement = NULL;
            goto failed;
        }
        /*
         * 1列目は現在の児童数、2列目は現在使用中の保育士コストの合計。
         * 新しい児童を加えるとどちらかの上限を超える場合、待機扱いにする。
         */
        if (sqlite3_column_int(statement, 0) >= configured_max_children ||
            sqlite3_column_double(statement, 1) + staff_cost >
                configured_staff_count + 0.001) {
            waitlisted = 1;
        }
    }
    sqlite3_finalize(statement);
    statement = NULL;

    /*
     * 予約本体を作る。空きがある場合はheld（仮押さえ）にして期限を設定し、
     * 上限超過の場合はwaitlisted（キャンセル待ち）にして期限を空にする。
     * 料金は「1時間あたりの料金×予約時間数」で計算する。
     */
    if (prepare_sql(
            db,
            "INSERT INTO reservations ("
            " child_id, care_date, start_hour, end_hour, age_class_id,"
            " staff_cost, fee_yen, status, hold_expires_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?,"
            " CASE WHEN ? = 0 THEN datetime('now','localtime',"
            "  '+' || CAST((SELECT value FROM settings"
            "               WHERE key = 'hold_timeout_sec') AS INTEGER)"
            "  || ' seconds') ELSE NULL END)",
            &statement, error, error_size) != SQLITE_OK) {
        goto failed;
    }
    /* INSERT文の?に、児童・日付・時間・料金などの値を順番に設定する。 */
    sqlite3_bind_int64(statement, 1, child_id);
    sqlite3_bind_text(statement, 2, care_date, -1, SQLITE_TRANSIENT);
    sqlite3_bind_int(statement, 3, start_hour);
    sqlite3_bind_int(statement, 4, end_hour);
    sqlite3_bind_int64(statement, 5, age_class_id);
    sqlite3_bind_double(statement, 6, staff_cost);
    sqlite3_bind_int(statement, 7, hourly_fee * (end_hour - start_hour));
    sqlite3_bind_text(statement, 8, waitlisted ? "waitlisted" : "held", -1,
                      SQLITE_STATIC);
    sqlite3_bind_int(statement, 9, waitlisted);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        set_error(error, error_size, "予約の登録に失敗しました: %s",
                  sqlite3_errmsg(db));
        sqlite3_finalize(statement);
        statement = NULL;
        goto failed;
    }
    /* SQLiteが採番した、今登録した予約のIDを取得する。 */
    *reservation_id = sqlite3_last_insert_rowid(db);
    sqlite3_finalize(statement);
    statement = NULL;

    if (waitlisted) {
        /*
         * キャンセル待ちになった時刻を補助テーブルに保存する。
         * 優先ポイントが同じ人同士を、先に申し込んだ順に並べるために使う。
         */
        if (prepare_sql(
                db,
                "INSERT INTO reservation_attempts "
                "(reservation_id, attempted_at_ms) VALUES (?, ?)",
                &statement, error, error_size) != SQLITE_OK) {
            goto failed;
        }
        /* 予約IDと申込時刻（ミリ秒）を対応づけて保存する。 */
        sqlite3_bind_int64(statement, 1, *reservation_id);
        sqlite3_bind_int64(statement, 2, attempted_at_ms);
        if (sqlite3_step(statement) != SQLITE_DONE) {
            set_error(error, error_size, "申込時刻の記録に失敗しました: %s",
                      sqlite3_errmsg(db));
            sqlite3_finalize(statement);
            statement = NULL;
            goto failed;
        }
        sqlite3_finalize(statement);
        statement = NULL;
    } else {
        /*
         * 通常予約なら予約時間の各1時間分をreservation_hoursに登録する。
         * 登録時にはSQLのトリガーも動き、定員・保育士上限を再確認する。
         */
        if (prepare_sql(
                db,
                "INSERT INTO reservation_hours "
                "(reservation_id, care_date, hour) VALUES (?, ?, ?)",
                &statement, error, error_size) != SQLITE_OK) {
            goto failed;
        }
        for (hour = start_hour; hour < end_hour; ++hour) {
            /* 同じINSERT文を使い、予約時間の枠を1つずつ保存する。 */
            sqlite3_reset(statement);
            sqlite3_clear_bindings(statement);
            sqlite3_bind_int64(statement, 1, *reservation_id);
            sqlite3_bind_text(statement, 2, care_date, -1, SQLITE_TRANSIENT);
            sqlite3_bind_int(statement, 3, hour);
            if (sqlite3_step(statement) != SQLITE_DONE) {
                set_error(error, error_size, "予約枠の確保に失敗しました: %s",
                          sqlite3_errmsg(db));
                sqlite3_finalize(statement);
                statement = NULL;
                goto failed;
            }
        }
        sqlite3_finalize(statement);
        statement = NULL;
    }

    /* すべての登録が成功したので変更を確定し、DBロックを解放する。 */
    if (execute_sql(db, "COMMIT", error, error_size) != SQLITE_OK) {
        return rollback_transaction(db, error, error_size);
    }
    return waitlisted ? HOIKU_WAITLISTED : HOIKU_RESERVED;

failed:
    /* 途中で失敗した場合は、予約本体や枠が一部だけ残らないよう取り消す。 */
    if (statement != NULL) {
        sqlite3_finalize(statement);
    }
    return rollback_transaction(db, error, error_size);
}

/*
 * キャンセル待ち一覧を選考順位順に取得する。
 * まず保護者の優先度ポイントが多い順、同点なら申込時刻が早い順にする。
 * 結果は予約IDの配列として返す。配列のメモリは呼び出し元がfreeする。
 */
int hoiku_get_waitlist_order(sqlite3 *db, sqlite3_int64 **reservation_ids,
                             size_t *count, char *error, size_t error_size)
{
    /*
     * JOINで予約から児童、児童から保護者につなぎ、保護者のポイントを得る。
     * attempted_at_msが小さいほど早い時刻なので、ASC（昇順）に並べる。
     * 受付時刻まで同じ場合は予約IDで順番を固定する。
     */
    const char *sql =
        "SELECT r.reservation_id"
        " FROM reservations r"
        " JOIN reservation_attempts a ON a.reservation_id = r.reservation_id"
        " JOIN children c ON c.child_id = r.child_id"
        " JOIN guardians g ON g.guardian_id = c.guardian_id"
        " WHERE r.status = 'waitlisted'"
        " ORDER BY g.priority_points DESC, a.attempted_at_ms ASC,"
        "          r.reservation_id ASC";
    sqlite3_stmt *statement = NULL;
    sqlite3_int64 *ids = NULL;
    size_t length = 0;
    int result;

    if (error != NULL && error_size > 0) {
        error[0] = '\0';
    }
    /* 配列の受け取り先がない場合は結果を返せないためエラーにする。 */
    if (db == NULL || reservation_ids == NULL || count == NULL) {
        set_error(error, error_size, "キャンセル待ち取得の引数が不正です。");
        return HOIKU_ERROR;
    }
    /* 結果が0件の場合も安全な値になるよう、出力先を初期化する。 */
    *reservation_ids = NULL;
    *count = 0;
    if (hoiku_initialize(db, error, error_size) != SQLITE_OK ||
        prepare_sql(db, sql, &statement, error, error_size) != SQLITE_OK) {
        return HOIKU_ERROR;
    }

    /* SQL結果を1行ずつ読み、予約IDを配列の末尾に追加する。 */
    while ((result = sqlite3_step(statement)) == SQLITE_ROW) {
        /* 必要な件数が増えるたびに、IDを保存するメモリを広げる。 */
        sqlite3_int64 *resized = (sqlite3_int64 *)realloc(
            ids, (length + 1) * sizeof(*ids));
        if (resized == NULL) {
            set_error(error, error_size, "キャンセル待ち一覧のメモリが不足しています。");
            free(ids);
            sqlite3_finalize(statement);
            return HOIKU_ERROR;
        }
        /* reallocが成功した新しいメモリ位置を使う。 */
        ids = resized;
        ids[length++] = sqlite3_column_int64(statement, 0);
    }
    if (result != SQLITE_DONE) {
        set_error(error, error_size, "キャンセル待ちの取得に失敗しました: %s",
                  sqlite3_errmsg(db));
        free(ids);
        /* SQL文を終了し、配列と件数を呼び出し元へ返す。 */
        sqlite3_finalize(statement);
        return HOIKU_ERROR;
    }

    sqlite3_finalize(statement);
    *reservation_ids = ids;
    *count = length;
    return SQLITE_OK;
}

/*
 * 予約をキャンセルする関数。
 * 予約状態をcancelledに変更し、予約時間の行を削除して枠を空ける。
 * 状態変更と枠削除は同じトランザクションにまとめ、片方だけ成功する
 * 不整合を防ぐ。すでにキャンセル済みなどの場合はエラーを返す。
 */
int hoiku_cancel(sqlite3 *db, sqlite3_int64 reservation_id, char *error,
                 size_t error_size)
{
    sqlite3_stmt *statement = NULL;

    if (error != NULL && error_size > 0) {
        error[0] = '\0';
    }
    /* DB接続と正の予約IDが指定されているか確認する。 */
    if (db == NULL || reservation_id <= 0) {
        set_error(error, error_size, "キャンセル情報が不正です。");
        return HOIKU_ERROR;
    }
    if (hoiku_initialize(db, error, error_size) != SQLITE_OK) {
        return HOIKU_ERROR;
    }
    /* キャンセルの状態変更中に他の予約処理が割り込まないようロックする。 */
    if (execute_sql(db, "BEGIN IMMEDIATE", error, error_size) != SQLITE_OK) {
        return HOIKU_ERROR;
    }

    /*
     * held（仮押さえ）、confirmed（確定）、waitlisted（待機中）の
     * 予約だけをキャンセル可能とする。その他の状態は変更しない。
     */
    if (prepare_sql(
            db,
            "UPDATE reservations"
            " SET status = 'cancelled', hold_expires_at = NULL,"
            "     updated_at = datetime('now','localtime')"
            " WHERE reservation_id = ?"
            "   AND status IN ('held','confirmed','waitlisted')",
            &statement, error, error_size) != SQLITE_OK) {
        goto cancel_failed;
    }
    sqlite3_bind_int64(statement, 1, reservation_id);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        set_error(error, error_size, "予約のキャンセルに失敗しました: %s",
                  sqlite3_errmsg(db));
        sqlite3_finalize(statement);
        statement = NULL;
        goto cancel_failed;
    }
    sqlite3_finalize(statement);
    statement = NULL;
    /* UPDATEした行が0なら、そのIDのキャンセル可能な予約は存在しない。 */
    if (sqlite3_changes(db) == 0) {
        set_error(error, error_size,
                  "キャンセル可能な予約が見つかりません。");
        goto cancel_failed;
    }

    /*
     * 予約の時間枠を消す。人数と保育士コストはreservation_hoursを基に
     * 集計されるため、この削除により該当時間のリソースが空く。
     */
    if (prepare_sql(db,
                    "DELETE FROM reservation_hours WHERE reservation_id = ?",
                    &statement, error, error_size) != SQLITE_OK) {
        goto cancel_failed;
    }
    sqlite3_bind_int64(statement, 1, reservation_id);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        set_error(error, error_size, "予約枠の解放に失敗しました: %s",
                  sqlite3_errmsg(db));
        sqlite3_finalize(statement);
        statement = NULL;
        goto cancel_failed;
    }
    sqlite3_finalize(statement);
    statement = NULL;

    /* 状態変更と枠の解放をまとめて確定する。 */
    if (execute_sql(db, "COMMIT", error, error_size) != SQLITE_OK) {
        return rollback_transaction(db, error, error_size);
    }
    return SQLITE_OK;

cancel_failed:
    /* キャンセル処理の途中で失敗したら、変更をすべて元に戻す。 */
    if (statement != NULL) {
        sqlite3_finalize(statement);
    }
    return rollback_transaction(db, error, error_size);
}
