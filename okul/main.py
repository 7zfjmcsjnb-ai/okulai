import os
import uuid
import hmac
import hashlib
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, Request, Response, Depends, HTTPException, status
from fastapi.responses import JSONResponse, HTMLResponse

from okul.database import db_session, DB_PATH
from okul.quota import check_and_use_quota, add_bonus_quota, get_quota_details, BASE_DAILY_LIMIT
from okul.ai import analyze_question_image
from okul.search import index_pdf_book, search_textbooks, search_web_educational

app = FastAPI(title="OkulAI - Maarif Modeli Asistanı")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", "gizli-anahtar-okulai").encode("utf-8")

def get_client_ip(request: Request) -> str:
    x_forwarded = request.headers.get("X-Forwarded-For")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"

def get_device_cookie(request: Request, response: Response) -> str:
    device_id = request.cookies.get("okulai_dev_id")
    if not device_id:
        device_id = str(uuid.uuid4())
        response.set_cookie("okulai_dev_id", device_id, max_age=31536000, httponly=True, samesite="lax")
    return device_id

def create_admin_token() -> str:
    return hmac.new(SECRET_KEY, f"{ADMIN_USER}:{ADMIN_PASS}".encode("utf-8"), hashlib.sha256).hexdigest()

def verify_admin(request: Request):
    token = request.cookies.get("okulai_admin_token") or request.headers.get("X-Admin-Token")
    expected = create_admin_token()
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Yetkisiz erişim. Admin girişi gerekli.")
    return True

def get_system_default_key() -> str:
    """Admin tarafından kaydedilen sitenin kendi Gemini API anahtarı."""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT val FROM settings WHERE key = 'default_ai_key'")
        row = cursor.fetchone()
        return row["val"] if row else os.getenv("DEFAULT_AI_KEY", "")

# --- GENEL API'LER ---

@app.get("/", response_class=HTMLResponse)
async def root():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h2>OkulAI: index.html bulunamadı.</h2>")

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    admin_file = os.path.join(STATIC_DIR, "admin.html")
    if os.path.exists(admin_file):
        with open(admin_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h2>OkulAI: admin.html bulunamadı.</h2>")

@app.get("/api/quota")
async def get_quota_status(request: Request, response: Response):
    ip = get_client_ip(request)
    dev_id = get_device_cookie(request, response)
    info = get_quota_details(ip, dev_id)
    return {"daily_limit": info["limit"], "remaining_quota": info["remaining"]}

@app.post("/api/quota/bonus")
async def reward_bonus_quota(request: Request, response: Response):
    """Reklam izlendikten sonra +5 hak ekler."""
    ip = get_client_ip(request)
    dev_id = get_device_cookie(request, response)
    add_bonus_quota(ip, dev_id, amount=5)
    info = get_quota_details(ip, dev_id)
    return {"success": True, "message": "Harika! +5 soru çözme hakkı kazandın.", "remaining_quota": info["remaining"]}

@app.post("/api/analyze")
async def analyze_question(
    request: Request,
    response: Response,
    image: UploadFile = File(...),
    provider: str = Form("gemini"),
    api_token: Optional[str] = Form(None),
    grade: int = Form(9),
    profile_id: Optional[str] = Form(None)
):
    ip = get_client_ip(request)
    dev_id = get_device_cookie(request, response)

    user_token = (api_token or "").strip()
    has_custom_key = bool(user_token)

    if not has_custom_key:
        system_key = get_system_default_key()
        if not system_key:
            raise HTTPException(
                status_code=400,
                detail="Sitenin varsayılan yapay zekâ anahtarı henüz admin tarafından ayarlanmamış. Lütfen 'Kendi API Key'im' seçeneğini kullanarak kişisel anahtarınızı girin."
            )
        active_token = system_key
        active_provider = "gemini"
    else:
        active_token = user_token
        active_provider = provider

    # Kendi key'i olanlardan kota DÜŞMEZ, sınırsızdır
    allowed, remaining = check_and_use_quota(ip, dev_id, custom_token_used=has_custom_key)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Bugünkü 10 ücretsiz soru hakkınız doldu! 'Reklam İzle (+5 Hak)' butonuna basarak anında yeni hak kazanabilir veya kendi API key'inizi girerek sınırsız çözebilirsiniz."
        )

    image_bytes = await image.read()
    mime_type = image.content_type or "image/jpeg"

    try:
        analysis_result = await analyze_question_image(
            provider=active_provider,
            api_token=active_token,
            image_bytes=image_bytes,
            mime_type=mime_type,
            grade=grade,
            profile_id=profile_id
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Analiz Hatası: {str(e)}")

    if profile_id:
        with db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO mistakes (profile_id, grade, subject, question_summary, student_error, ai_advice, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                profile_id,
                grade,
                analysis_result.get("subject", "Genel"),
                analysis_result.get("step_by_step_solution", "")[:200],
                analysis_result.get("identified_mistake_risk", ""),
                analysis_result.get("advice", ""),
                datetime.now().strftime("%Y-%m-%d %H:%M")
            ))

    return {
        "success": True,
        "is_unlimited": has_custom_key,
        "remaining_quota": "Sınırsız" if has_custom_key else remaining,
        "analysis": analysis_result
    }

@app.post("/api/search")
async def search_content(query: str = Form(...), grade: Optional[int] = Form(None)):
    books_results = search_textbooks(query=query, grade=grade, limit=4)
    web_results = await search_web_educational(query=query, limit=3)
    return {"textbook_results": books_results, "web_results": web_results}

@app.get("/api/profile/mistakes")
async def get_mistakes(profile_id: str):
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, grade, subject, question_summary, student_error, ai_advice, created_at FROM mistakes WHERE profile_id = ? ORDER BY id DESC LIMIT 20",
            (profile_id,)
        )
        rows = cursor.fetchall()
        return [dict(r) for r in rows]

# --- ADMİN API'LERİ ---

@app.post("/api/admin/login")
async def admin_login(response: Response, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        token = create_admin_token()
        response.set_cookie("okulai_admin_token", token, httponly=True, samesite="lax", max_age=86400 * 7)
        return {"success": True, "token": token}
    raise HTTPException(status_code=401, detail="Hatalı kullanıcı adı veya şifre!")

@app.post("/api/admin/set-default-key", dependencies=[Depends(verify_admin)])
async def set_default_key(default_key: str = Form(...)):
    """Adminin sisteme varsayılan Gemini API key kaydetmesini sağlar."""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, val) VALUES ('default_ai_key', ?)", (default_key.strip(),))
    return {"success": True, "message": "Sitenin varsayılan AI anahtarı güncellendi."}

@app.post("/api/admin/upload-book", dependencies=[Depends(verify_admin)])
async def upload_textbook(
    book_file: UploadFile = File(...),
    book_name: str = Form(...),
    grade: int = Form(...),
    subject: str = Form(...)
):
    if not book_file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Sadece PDF dosyaları yüklenebilir.")
    pdf_bytes = await book_file.read()
    index_pdf_book(pdf_bytes, book_name=book_name, grade=grade, subject=subject)
    return {"success": True, "message": f"{book_name} kitabı başarıyla indekslendi."}

@app.get("/api/admin/stats", dependencies=[Depends(verify_admin)])
async def admin_stats():
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM mistakes")
        total_questions = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(DISTINCT book_name) as count FROM book_pages")
        total_books = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM daily_quotas")
        total_tracked = cursor.fetchone()["count"]

        cursor.execute("SELECT val FROM settings WHERE key = 'default_ai_key'")
        row = cursor.fetchone()
        has_key = bool(row and row["val"])

    return {
        "total_analyzed_questions": total_questions,
        "indexed_books": total_books,
        "tracked_users_today": total_tracked,
        "system_ai_ready": has_key
    }

@app.get("/api/admin/backup", dependencies=[Depends(verify_admin)])
async def download_backup():
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "rb") as f:
            content = f.read()
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename=okulai_backup.db"}
        )
    raise HTTPException(status_code=404, detail="Veritabanı dosyası bulunamadı.")
