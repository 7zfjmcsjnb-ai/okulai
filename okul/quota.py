from datetime import date
from okul.database import db_session

BASE_DAILY_LIMIT = 10  # Token girmeyenler için varsayılan günlük limit

def get_quota_details(ip_address: str, device_cookie: str) -> dict:
    today_str = date.today().isoformat()
    keys = [f"ip_{ip_address}", f"dev_{device_cookie}"]
    with db_session() as conn:
        cursor = conn.cursor()
        max_used = 0
        max_bonus = 0
        for key in keys:
            cursor.execute("SELECT request_count, bonus_count, last_date FROM daily_quotas WHERE tracking_key = ?", (key,))
            row = cursor.fetchone()
            if row and row["last_date"] == today_str:
                max_used = max(max_used, row["request_count"])
                max_bonus = max(max_bonus, row["bonus_count"] or 0)
        
        total_limit = BASE_DAILY_LIMIT + max_bonus
        remaining = max(0, total_limit - max_used)
        return {"limit": total_limit, "used": max_used, "remaining": remaining}

def check_and_use_quota(ip_address: str, device_cookie: str, is_admin: bool = False, custom_token_used: bool = False) -> tuple[bool, int]:
    """Kendi API anahtarını kullananlardan veya adminden kota düşmez (Sınırsızdır)."""
    if is_admin or custom_token_used:
        return True, 999999

    today_str = date.today().isoformat()
    keys = [f"ip_{ip_address}", f"dev_{device_cookie}"]

    with db_session() as conn:
        cursor = conn.cursor()
        max_used = 0
        max_bonus = 0

        for key in keys:
            cursor.execute("SELECT request_count, bonus_count, last_date FROM daily_quotas WHERE tracking_key = ?", (key,))
            row = cursor.fetchone()
            if row:
                if row["last_date"] == today_str:
                    max_used = max(max_used, row["request_count"])
                    max_bonus = max(max_bonus, row["bonus_count"] or 0)
                else:
                    cursor.execute("UPDATE daily_quotas SET request_count = 0, bonus_count = 0, last_date = ? WHERE tracking_key = ?", (today_str, key))
            else:
                cursor.execute("INSERT INTO daily_quotas (tracking_key, request_count, bonus_count, last_date) VALUES (?, 0, 0, ?)", (key, today_str))

        total_limit = BASE_DAILY_LIMIT + max_bonus
        if max_used >= total_limit:
            return False, 0

        new_count = max_used + 1
        for key in keys:
            cursor.execute("UPDATE daily_quotas SET request_count = ?, last_date = ? WHERE tracking_key = ?", (new_count, today_str, key))

        return True, (total_limit - new_count)

def add_bonus_quota(ip_address: str, device_cookie: str, amount: int = 5) -> int:
    """Reklam izleyen kullanıcıya günlük +5 hak hediye eder."""
    today_str = date.today().isoformat()
    keys = [f"ip_{ip_address}", f"dev_{device_cookie}"]
    with db_session() as conn:
        cursor = conn.cursor()
        new_bonus = 0
        for key in keys:
            cursor.execute("SELECT bonus_count, last_date FROM daily_quotas WHERE tracking_key = ?", (key,))
            row = cursor.fetchone()
            if row and row["last_date"] == today_str:
                new_bonus = (row["bonus_count"] or 0) + amount
                cursor.execute("UPDATE daily_quotas SET bonus_count = ? WHERE tracking_key = ?", (new_bonus, key))
            else:
                new_bonus = amount
                cursor.execute("INSERT OR REPLACE INTO daily_quotas (tracking_key, request_count, bonus_count, last_date) VALUES (?, 0, ?, ?)", (key, new_bonus, today_str))
        return new_bonus
