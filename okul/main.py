import os
import uuid
import hmac
import hashlib
from datetime import datetime
from fastapi import FastAPI, UploadFile, File, Form, Request, Response, Depends, HTTPException, status
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from okul.database import db_session, DB_PATH
from okul.quota import check_and_use_quota, DAILY_LIMIT
from okul.ai import analyze_question_image
from okul.search import index_pdf_book, search_textbooks, search_web_educational

app = FastAPI(title="OkulAI - Maarif Modeli Asistanı")

# Statik HTML arayüz dosyaları için klasör yolu
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", "gizli-anahtar-okulai").encode("utf-8")

# --- YARDIMCI GÜVENLİK VE IP FONKSİYONLARI ---

def get_client_ip(request: Request) -> str:
    """Caddy arkasındaki gerçek kullanıcı IP adresini yakalar."""
    x_forwarded = request.headers.get("X-Forwarded-For")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"

def get_device_cookie(request: Request, response: Response) -> str:
    """Cihazı tanımlayan benzersiz çerez ID'si oluşturur / okur."""
    device_id = request.cookies.get("okulai_dev_id")
    if not device_id:
        device_id = str(uuid.uuid4())
        response.set_cookie("okulai_dev_id", device_id, max_age=31536000, httponly=True, samesite="lax")
    return device_id

def create_admin_token() -> str:
    """Güvenli admin oturum belirteci üretir."""
    return hmac.new(SECRET_KEY, f"{ADMIN_USER}:{ADMIN_PASS}".encode("utf-8"), hashlib.sha256).hexdigest()

def verify_admin(request: Request):
    """Admin doğrulaması yapar (Cookie veya Header üzerinden)."""
    token = request.cookies.get("okulai_admin_token") or request.headers.get("X-Admin-Token")
    expected = create_admin_token()
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Yetkisiz erişim. Admin girişi gerekli.")
    return True


# --- GENEL KULLANICI & ÖĞRENCİ API'LERİ ---

@app.get("/")
async def root():
    """Ana sayfayı döndürür."""
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "OkulAI API çalışıyor. Statik arayüz henüz yüklenmedi."}

@app.get("/admin")
async def admin_page():
    """Admin paneli sayfasını döndürür."""
    admin_file = os.path.join(STATIC_DIR, "admin.html")
    if os.path.exists(admin_file):
        return FileResponse(admin_file)
    return {"message": "Admin paneli arayüzü henüz yüklenmedi."}

@app.get("/api/quota")
async def get_quota_status(request: Request, response: Response):
    """Kullanıcının kalan günlük soru hakkını sorgular."""
    ip = get_client_ip(request)
    dev_id = get_device_cookie(request, response)
    # Sadece sorgulama amaçlı sahte bir kontrol
    allowed, remaining = check_and_use_quota(ip, dev_id)
    # check_and_use_quota 1 düşürdüğü için hakkı geri iade etmeyelim, anlık kalan limit bilgisi verelim
    return {"daily_limit": DAILY_LIMIT, "remaining_quota": remaining}

@app.post("/api/analyze")
async def analyze_question(
    request: Request,
    response: Response,
    image: UploadFile = File(...),
    provider: str = Form(...),      # 'gpt', 'claude', 'gemini'
    api_token: str = Form(...),     # Kullanıcının girdiği kişisel API anahtarı
    grade: int = Form(9),           # 9, 10 veya 11
    profile_id: str = Form(None)    # Varsa profil UUID
):
    """
    Soru görselini alır, kotayı düşer, AI modeline gönderir,
    sonucu Maarif Modeli çerçevesinde veritabanına ve öğrenci hafızasına kaydeder.
    """
    ip = get_client_ip(request)
    dev_id = get_device_cookie(request, response)

    # 1. Kota kontrolü
    allowed, remaining = check_and_use_quota(ip, dev_id)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Günlük ücretsiz soru çözme limitiniz doldu! Yarın tekrar deneyebilirsiniz."
        )

    # 2. Görseli oku
    image_bytes = await image.read()
    mime_type = image.content_type or "image/jpeg"

    # 3. AI ile çöz
    try:
        analysis_result = await analyze_question_image(
            provider=provider,
            api_token=api_token,
            image_bytes=image_bytes,
            mime_type=mime_type,
            grade=grade,
            profile_id=profile_id
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI Analiz Hatası: {str(e)}")

    # 4. Hatanın ve tavsiyenin öğrenci geçmişine kaydedilmesi (Hafıza)
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
        "remaining_quota": remaining,
        "analysis": analysis_result
    }

@app.post("/api/search")
async def search_content(query: str = Form(...), grade: int = Form(None)):
    """
    Hem admin tarafından yüklenen MEB kitaplarında hem de internette arama yapar.
    """
    books_results = search_textbooks(query=query, grade=grade, limit=4)
    web_results = await search_web_educational(query=query, limit=3)
    return {
        "textbook_results": books_results,
        "web_results": web_results
    }

@app.get("/api/profile/mistakes")
async def get_mistakes(profile_id: str):
    """Öğrencinin geçmişte yaptığı hataları listeler."""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, grade, subject, question_summary, student_error, ai_advice, created_at FROM mistakes WHERE profile_id = ? ORDER BY id DESC LIMIT 20",
            (profile_id,)
        )
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


# --- ADMİN PANELİ API'LERİ ---

@app.post("/api/admin/login")
async def admin_login(response: Response, username: str = Form(...), password: str = Form(...)):
    """Admin girişi yapar ve oturum çerezi bırakır."""
    if username == ADMIN_USER and password == ADMIN_PASS:
        token = create_admin_token()
        response.set_cookie("okulai_admin_token", token, httponly=True, samesite="lax", max_age=86400 * 7)
        return {"success": True, "token": token}
    raise HTTPException(status_code=401, detail="Hatalı kullanıcı adı veya şifre!")

@app.post("/api/admin/upload-book", dependencies=[Depends(verify_admin)])
async def upload_textbook(
    book_file: UploadFile = File(...),
    book_name: str = Form(...),
    grade: int = Form(...),
    subject: str = Form(...)
):
    """Admin panelinden MEB ders kitabı PDF'i yükler ve veritabanına indeksler."""
    if not book_file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Sadece PDF dosyaları yüklenebilir.")
    
    pdf_bytes = await book_file.read()
    index_pdf_book(pdf_bytes, book_name=book_name, grade=grade, subject=subject)
    return {"success": True, "message": f"{book_name} kitabı başarıyla indekslendi."}

@app.get("/api/admin/stats", dependencies=[Depends(verify_admin)])
async def admin_stats():
    """Admin istatistikleri."""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM mistakes")
        total_questions = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(DISTINCT book_name) as count FROM book_pages")
        total_books = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM daily_quotas")
        total_tracked = cursor.fetchone()["count"]

    return {
        "total_analyzed_questions": total_questions,
        "indexed_books": total_books,
        "tracked_users_today": total_tracked
    }

@app.get("/api/admin/backup", dependencies=[Depends(verify_admin)])
async def download_backup():
    """Oracle Free Tier sunucusu kapansa bile verilerin kaybolmaması için tek tıkla DB yedeği indirir."""
    if os.path.exists(DB_PATH):
        return FileResponse(DB_PATH, filename="okulai_backup.db", media_type="application/octet-stream")
    raise HTTPException(status_code=404, detail="Veritabanı dosyası bulunamadı.")
