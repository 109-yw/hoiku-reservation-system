#include <stdio.h>
#include <stdlib.h>

#include "hoiku.h"

int main(int argc, char *argv[])
{
    if (argc == 1) {
        puts("hoiku_cli OK");
        return 0;
    }

    if (argc != 7) {
        fprintf(stderr,
                "Usage: hoiku_cli DB CHILD_ID DATE START END ATTEMPTED_AT_MS\n");
        return 2;
    }

    sqlite3 *db = NULL;
    if (sqlite3_open_v2(argv[1], &db, SQLITE_OPEN_READWRITE, NULL) != SQLITE_OK) {
        fprintf(stderr, "SQLite could not open the database.\n");
        sqlite3_close(db);
        return 2;
    }

    sqlite3_int64 reservation_id = 0;
    char error[512] = "";

    int result = hoiku_reserve(
        db,
        strtoll(argv[2], NULL, 10),
        argv[3],
        atoi(argv[4]),
        atoi(argv[5]),
        strtoll(argv[6], NULL, 10),
        &reservation_id,
        error,
        sizeof(error)
    );

    sqlite3_close(db);

    if (result == HOIKU_RESERVED) {
        printf("held %lld\n", (long long)reservation_id);
        return 0;
    }

    if (result == HOIKU_WAITLISTED) {
        printf("waitlisted %lld\n", (long long)reservation_id);
        return 0;
    }

    fprintf(stderr, "%s\n", error);
    return 1;
}