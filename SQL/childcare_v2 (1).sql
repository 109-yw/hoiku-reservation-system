-- 一時保育 予約システム SQLite スキーマ v2
-- 実行: sqlite3 childcare.db < childcare_v2.sql
-- 日時は 'YYYY-MM-DD HH:MM:SS'（ローカル時刻）、日付は 'YYYY-MM-DD' の文字列で保持
-- 排他制御: 予約確定・繰り上げ・ポイント更新は必ず BEGIN IMMEDIATE で開始すること

PRAGMA foreign_keys = ON;

---------------------------------------------------------------
-- 0. 設定値（後から変更しやすいようにパラメータ化）
---------------------------------------------------------------
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    description TEXT
);
INSERT INTO settings (key, value, description) VALUES
  ('hold_timeout_sec',          '600',  '仮押さえのタイムアウト秒数（仮値）'),
  ('waitlist_lose_points',      '1',    '同時予約で優先されなかった側に付与するポイント'),
  ('no_show_grace_minutes',     '60',   '開始からこの分数以内にチェックインが無ければ無断キャンセル'),
  ('no_show_penalty_points',    '-5',   '無断キャンセル時のポイント増減（仮値・変更予定）'),
  ('late_pickup_point_per_hour','-1',   '延滞1時間ごとのポイント増減'),
  ('late_fee_per_hour_yen',     '500',  '延滞料（1時間あたり・仮値）'),
  ('offer_response_minutes',    '30',   '繰り上げ通知への回答期限（分・仮値）'),
  ('open_hour',                 '9',    '開所時刻'),
  ('close_hour',                '17',   '閉所時刻'),
  ('staff_count',               '5',    '保育士数（固定。欠勤・増員は考慮しない）'),
  ('max_children',              '15',   '1時間あたりの児童の上限（過多の判定）'),
  ('min_children',              '3',    '1時間あたりの児童の下限（過少の判定・仮値）');

---------------------------------------------------------------
-- 1. 会員・児童
---------------------------------------------------------------
CREATE TABLE guardians (                    -- 保護者（世帯）
    guardian_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    name_kana       TEXT,
    phone           TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    line_user_id    TEXT,                   -- LINE通知用
    priority_points INTEGER NOT NULL DEFAULT 0,   -- 現在値（履歴は point_ledger）
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE age_classes (                  -- 年齢区分（料金・保育士コスト）
    age_class_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    min_age_months INTEGER NOT NULL,        -- 以上
    max_age_months INTEGER NOT NULL,        -- 未満
    staff_cost     REAL NOT NULL,           -- 1人あたり消費する保育士数（0歳=0.33）
    hourly_fee_yen INTEGER NOT NULL CHECK (hourly_fee_yen >= 0)
);
-- 数値は仮値。配置基準・料金は実態に合わせて修正してください
INSERT INTO age_classes (name, min_age_months, max_age_months, staff_cost, hourly_fee_yen) VALUES
  ('0歳児',    0,  12, 0.33, 1200),
  ('1-2歳児', 12,  36, 0.17, 1000),
  ('3歳児',   36,  48, 0.05,  900),
  ('4歳児以上',48, 120, 0.04,  800);

CREATE TABLE children (                     -- 児童カルテ
    child_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guardian_id    INTEGER NOT NULL,
    name           TEXT NOT NULL,
    name_kana      TEXT,
    birth_date     TEXT NOT NULL,
    allergy_note   TEXT,                    -- アレルギー
    medical_note   TEXT,                    -- 問診・既往歴・配慮事項
    karte_status   TEXT NOT NULL DEFAULT 'draft'
                   CHECK (karte_status IN ('draft','submitted','approved','rejected')),
    karte_reviewed_at TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (guardian_id) REFERENCES guardians(guardian_id) ON DELETE CASCADE
);
CREATE INDEX idx_children_guardian ON children(guardian_id);

CREATE TABLE admin_users (                  -- 施設・管理者
    admin_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);

---------------------------------------------------------------
-- 2. 開所日（4週間分）と日別リソース
---------------------------------------------------------------
CREATE TABLE operating_days (
    care_date     TEXT PRIMARY KEY,
    is_open       INTEGER NOT NULL DEFAULT 1 CHECK (is_open IN (0,1))
);

---------------------------------------------------------------
-- 3. 予約
--   status:
--     held       仮押さえ中（hold_expires_at まで枠を確保）
--     confirmed  確定
--     waitlisted キャンセル待ち（枠は確保しない）
--     cancelled / expired / completed / no_show
---------------------------------------------------------------
CREATE TABLE reservations (
    reservation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id        INTEGER NOT NULL,
    care_date       TEXT NOT NULL,
    start_hour      INTEGER NOT NULL CHECK (start_hour BETWEEN 9 AND 15),
    end_hour        INTEGER NOT NULL CHECK (end_hour   BETWEEN 11 AND 17),
    -- 予約時点のスナップショット（後で年齢区分・料金が変わっても請求額が変わらない）
    age_class_id    INTEGER NOT NULL,
    staff_cost      REAL    NOT NULL,
    fee_yen         INTEGER NOT NULL CHECK (fee_yen >= 0),
    status          TEXT NOT NULL DEFAULT 'held'
                    CHECK (status IN ('held','confirmed','waitlisted','cancelled',
                                      'expired','completed','no_show')),
    hold_expires_at TEXT,                   -- held のときのみ
    reason          TEXT,
    qr_token        TEXT UNIQUE,            -- 登降園QR
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK ((start_hour = 9 AND end_hour IN (13, 17))
        OR (start_hour = 13 AND end_hour = 17)), -- 午前・午後・1日コース
    FOREIGN KEY (child_id)      REFERENCES children(child_id),
    FOREIGN KEY (care_date)     REFERENCES operating_days(care_date),
    FOREIGN KEY (age_class_id)  REFERENCES age_classes(age_class_id)
);
CREATE INDEX idx_resv_date   ON reservations(care_date, status);
CREATE INDEX idx_resv_child  ON reservations(child_id);
-- 同じ子が同じ日に重複して有効な予約を持てない（時間帯重複はアプリ/Cエンジンで検査）
CREATE INDEX idx_resv_child_date ON reservations(child_id, care_date);

-- 予約が占有する1時間単位の枠（held / confirmed のときだけ行を持つ）
CREATE TABLE reservation_hours (
    reservation_id INTEGER NOT NULL,
    care_date      TEXT NOT NULL,
    hour           INTEGER NOT NULL CHECK (hour BETWEEN 9 AND 16),
    PRIMARY KEY (reservation_id, hour),
    FOREIGN KEY (reservation_id) REFERENCES reservations(reservation_id) ON DELETE CASCADE,
    FOREIGN KEY (care_date)      REFERENCES operating_days(care_date)
);
CREATE INDEX idx_rh_slot ON reservation_hours(care_date, hour);

-- 定員（人数）と保育士コストの超過を最後の砦としてDB側で防ぐ
CREATE TRIGGER trg_hour_capacity
BEFORE INSERT ON reservation_hours
BEGIN
    SELECT RAISE(ABORT, '定員超過')
    WHERE (SELECT COUNT(*)
           FROM reservation_hours h
           JOIN reservations r ON r.reservation_id = h.reservation_id
           WHERE h.care_date = NEW.care_date AND h.hour = NEW.hour
             AND (r.status = 'confirmed'
                  OR (r.status = 'held' AND r.hold_expires_at > datetime('now','localtime'))))
          >= (SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'max_children');

    SELECT RAISE(ABORT, '保育士リソース超過')
    WHERE (SELECT COALESCE(SUM(r.staff_cost), 0)
           FROM reservation_hours h
           JOIN reservations r ON r.reservation_id = h.reservation_id
           WHERE h.care_date = NEW.care_date AND h.hour = NEW.hour
             AND (r.status = 'confirmed'
                  OR (r.status = 'held' AND r.hold_expires_at > datetime('now','localtime'))))
          + (SELECT staff_cost FROM reservations WHERE reservation_id = NEW.reservation_id)
          > (SELECT CAST(value AS REAL) FROM settings WHERE key = 'staff_count') + 0.001;
END;

---------------------------------------------------------------
-- 4. キャンセル待ち・繰り上げ通知
---------------------------------------------------------------
CREATE TABLE waitlist_offers (
    offer_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL,        -- status='waitlisted' の予約
    offered_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    respond_by     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','accepted','declined','expired')),
    responded_at   TEXT,
    FOREIGN KEY (reservation_id) REFERENCES reservations(reservation_id)
);

CREATE TABLE notifications (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guardian_id     INTEGER NOT NULL,
    channel         TEXT NOT NULL CHECK (channel IN ('line','email','app')),
    type            TEXT NOT NULL,          -- 'waitlist_offer','cancel_confirm' など
    body            TEXT NOT NULL,
    related_offer_id INTEGER,
    sent_at         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (guardian_id)      REFERENCES guardians(guardian_id),
    FOREIGN KEY (related_offer_id) REFERENCES waitlist_offers(offer_id)
);

---------------------------------------------------------------
-- 5. 優先ポイント
---------------------------------------------------------------
CREATE TABLE point_ledger (
    ledger_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    guardian_id    INTEGER NOT NULL,
    delta          INTEGER NOT NULL,        -- 増減（リセット時は -現在値）
    balance_after  INTEGER NOT NULL,
    reason         TEXT NOT NULL CHECK (reason IN (
                     'conflict_lose',       -- 同時予約で優先されず付与
                     'conflict_win',        -- 同時予約で優先され消費
                     'waitlist_promoted',   -- キャンセル待ちで優先されリセット
                     'no_show',             -- 無断キャンセル減点
                     'late_pickup',         -- 延滞減点
                     'manual')),            -- 管理者による修正
    reservation_id INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (guardian_id)    REFERENCES guardians(guardian_id),
    FOREIGN KEY (reservation_id) REFERENCES reservations(reservation_id)
);
CREATE INDEX idx_ledger_guardian ON point_ledger(guardian_id, created_at);

---------------------------------------------------------------
-- 6. キャンセル
---------------------------------------------------------------
CREATE TABLE cancel_policies (              -- 段階的キャンセル料（開始の何時間前か）
    policy_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    min_hours_before REAL NOT NULL,         -- この時間以上前なら適用
    refund_rate      REAL NOT NULL CHECK (refund_rate BETWEEN 0 AND 1),
    label            TEXT NOT NULL
);
INSERT INTO cancel_policies (min_hours_before, refund_rate, label) VALUES
  (2.0, 1.0, '開始2時間前まで：全額返金'),
  (0.0, 0.5, '開始直前まで：半額返金');

CREATE TABLE cancellations (
    cancellation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id  INTEGER NOT NULL UNIQUE,
    cancel_type     TEXT NOT NULL CHECK (cancel_type IN ('guardian','no_show','system')),
    cancelled_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    refund_rate     REAL NOT NULL,
    refund_yen      INTEGER NOT NULL DEFAULT 0,
    cancel_fee_yen  INTEGER NOT NULL DEFAULT 0,   -- 請求するキャンセル料
    FOREIGN KEY (reservation_id) REFERENCES reservations(reservation_id)
);

---------------------------------------------------------------
-- 7. 登降園（チェックイン／アウト）・延滞
---------------------------------------------------------------
CREATE TABLE attendance (
    attendance_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL UNIQUE,
    checkin_at     TEXT,
    checkout_at    TEXT,
    late_minutes   INTEGER NOT NULL DEFAULT 0,    -- 引き取り延滞（分）
    late_fee_yen   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (reservation_id) REFERENCES reservations(reservation_id)
);

---------------------------------------------------------------
-- 8. 支払い（現地／システム内、キャンセル料・延滞料の請求）
---------------------------------------------------------------
CREATE TABLE payments (
    payment_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL,
    guardian_id    INTEGER NOT NULL,
    charge_type    TEXT NOT NULL CHECK (charge_type IN ('usage','cancel_fee','late_fee')),
    method         TEXT NOT NULL CHECK (method IN ('onsite','online')),
    amount_yen     INTEGER NOT NULL CHECK (amount_yen >= 0),
    refunded_yen   INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','authorized','captured',
                                     'billed','refunded','partial_refunded','failed')),
    stripe_payment_intent_id TEXT,          -- カード番号そのものは絶対に保存しない
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    paid_at        TEXT,
    FOREIGN KEY (reservation_id) REFERENCES reservations(reservation_id),
    FOREIGN KEY (guardian_id)    REFERENCES guardians(guardian_id)
);
CREATE INDEX idx_payments_guardian ON payments(guardian_id);

---------------------------------------------------------------
-- 9. ビュー
---------------------------------------------------------------
-- 1時間ごとの人数・保育士コスト・残り枠
CREATE VIEW v_hourly_load AS
SELECT od.care_date,
       hrs.hour,
       COUNT(r.reservation_id)            AS children_count,
       COALESCE(SUM(r.staff_cost), 0)     AS staff_used,
       (SELECT CAST(value AS INTEGER) FROM settings WHERE key='max_children')
           - COUNT(r.reservation_id)      AS children_remaining,
       -- full=上限到達(過多) / under=下限未満(過少) / ok=適正
       CASE
         WHEN COUNT(r.reservation_id) >= (SELECT CAST(value AS INTEGER) FROM settings WHERE key='max_children') THEN 'full'
         WHEN COUNT(r.reservation_id) <  (SELECT CAST(value AS INTEGER) FROM settings WHERE key='min_children') THEN 'under'
         ELSE 'ok'
       END AS load_status
FROM operating_days od
CROSS JOIN (SELECT 9 AS hour UNION SELECT 10 UNION SELECT 11 UNION SELECT 12
            UNION SELECT 13 UNION SELECT 14 UNION SELECT 15 UNION SELECT 16) hrs
LEFT JOIN reservation_hours h
       ON h.care_date = od.care_date AND h.hour = hrs.hour
LEFT JOIN reservations r
       ON r.reservation_id = h.reservation_id
      AND (r.status = 'confirmed'
           OR (r.status = 'held' AND r.hold_expires_at > datetime('now','localtime')))
GROUP BY od.care_date, hrs.hour;

-- 無断キャンセル候補（開始+60分を過ぎてもチェックインが無い確定予約）
CREATE VIEW v_no_show_candidates AS
SELECT r.reservation_id, r.child_id, r.care_date, r.start_hour
FROM reservations r
LEFT JOIN attendance a ON a.reservation_id = r.reservation_id
WHERE r.status = 'confirmed'
  AND a.checkin_at IS NULL
  AND datetime(r.care_date || ' ' || printf('%02d:00:00', r.start_hour),
               '+' || (SELECT value FROM settings WHERE key='no_show_grace_minutes') || ' minutes')
      < datetime('now','localtime');

---------------------------------------------------------------
-- 10. サンプルデータ
---------------------------------------------------------------
INSERT INTO operating_days (care_date) VALUES ('2026-11-10'), ('2026-11-11');

INSERT INTO guardians (name, name_kana, phone, email, password_hash)
VALUES ('山田 花子', 'ヤマダ ハナコ', '090-0000-0000', 'hanako@example.com', 'dummy_hash');

INSERT INTO children (guardian_id, name, name_kana, birth_date, allergy_note, karte_status)
VALUES (1, '山田 太郎', 'ヤマダ タロウ', '2026-02-01', '卵アレルギー', 'approved');

-- 午前コース 9:00-13:00（0歳児：4時間 × 1200円）
INSERT INTO reservations (child_id, care_date, start_hour, end_hour, age_class_id,
                          staff_cost, fee_yen, status, qr_token)
VALUES (1, '2026-11-10', 9, 13, 1, 0.33, 4800, 'confirmed', 'qr-sample-0001');
INSERT INTO reservation_hours (reservation_id, care_date, hour)
VALUES (1,'2026-11-10',9),(1,'2026-11-10',10),(1,'2026-11-10',11),(1,'2026-11-10',12);

-- ===== クエリ例 =====
-- 空き状況:        SELECT * FROM v_hourly_load WHERE care_date = '2026-11-10';
-- 年齢区分の判定:  SELECT * FROM age_classes
--                  WHERE (SELECT (strftime('%Y','2026-11-10')-strftime('%Y',birth_date))*12
--                                + strftime('%m','2026-11-10')-strftime('%m',birth_date)
--                         FROM children WHERE child_id=1)
--                        >= min_age_months AND ... < max_age_months;
-- 排他制御の基本形: BEGIN IMMEDIATE; -- 空き確認→reservations/reservation_hours INSERT→COMMIT;
-- 期限切れ仮押さえ: UPDATE reservations SET status='expired' WHERE status='held'
--                   AND hold_expires_at <= datetime('now','localtime');
--                   DELETE FROM reservation_hours WHERE reservation_id IN
--                   (SELECT reservation_id FROM reservations WHERE status='expired');
