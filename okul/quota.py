from datetime import date
from okul.database import db_session

DAILY_LIMIT = 5  # Giriş yapmamış kullanıcı için günlük soru çözdürme kotası

def check_and_use_quota(ip_address: str, device_cookie: str, is_admin: bool = False) -> tuple[bool, int]:
    """
    Kota durumunu kontrol eder ve kotadan 1 düşer.
    (İzin verildi mi?, Kalan Hak) döner.
    """
    if is_admin:
        return True, 9999

    today_str = date.today().isoformat()
    # Hem IP hem Cookie takip edilir, herhangi biri dolduysa izin verilmez
    keys = [f"ip_{ip_address}", f"dev_{device_cookie}"]

    with db_session() as conn:
        cursor = conn.cursor()
        max_used = 0

        for key in keys:
            cursor.execute("SELECT request_count, last_date FROM daily_quotas WHERE tracking_key = ?", (key,))
            row = cursor.fetchone()

            if row:
                count, last_date = row["request_count"], row["last_date"]
                if last_date == today_str:
                    max_used = max(max_used, count)
                else:
                    # Yeni gün, sıfırla
                    cursor.execute("UPDATE daily_quotas SET request_count = 0, last_date = ? WHERE tracking_key = ?", (today_str, key))
            else:
                cursor.execute("INSERT INTO daily_quotas (tracking_key, request_count, last_date) VALUES (?, 0, ?)", (key, today_str))

        if max_used >= DAILY_LIMIT:
            return False, 0

        # Kotayı 1 artır
        new_count = max_used + 1
        for key in keys:
            cursor.execute("UPDATE daily_quotas SET request_count = ?, last_date = ? WHERE tracking_key = ?", (new_count, today_str, key))

        return True, (DAILY_LIMIT - new_count)
