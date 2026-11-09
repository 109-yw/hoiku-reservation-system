#ifndef HOIKU_H
#define HOIKU_H

#include <stddef.h>

#include "../SQL/sqlite3.h"

/* 予約APIの戻り値。 */
enum {
    HOIKU_ERROR = -1,
    HOIKU_RESERVED = 0,
    HOIKU_WAITLISTED = 1
};

/* SQLite接続の外部キー制約とキャンセル待ち用テーブルを初期化する。 */
int hoiku_initialize(sqlite3 *db, char *error, size_t error_size);

/*
 * attempted_at_msには申込受付時点のUnix時刻（ミリ秒）を渡す。
 * 予約済みならHOIKU_RESERVED、キャンセル待ちならHOIKU_WAITLISTED、
 * エラーならHOIKU_ERRORを返す。
 */
int hoiku_reserve(sqlite3 *db, sqlite3_int64 child_id, const char *care_date,
                  int start_hour, int end_hour, sqlite3_int64 attempted_at_ms,
                  sqlite3_int64 *reservation_id, char *error,
                  size_t error_size);

/*
 * ポイント降順、同ポイントなら申込ミリ秒昇順で予約IDを返す。
 * 返された配列は呼出側がfreeする。
 */
int hoiku_get_waitlist_order(sqlite3 *db, sqlite3_int64 **reservation_ids,
                             size_t *count, char *error, size_t error_size);

/* 予約の時間枠を解放してキャンセルする。 */
int hoiku_cancel(sqlite3 *db, sqlite3_int64 reservation_id, char *error,
                 size_t error_size);

#endif
