import os
import sys
import traceback
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

try:
    # --- TÜM ASIL UYGULAMA KODLARI BURADA ÇALIŞIR ---
    import uuid
    import hmac
    import hashlib
    from datetime import datetime
    from typing import Optional
    from fastapi import UploadFile, File, Form, Request, Response, Depends, HTTPException, status
    from fastapi.responses import JSONResponse

    from okul.database import db_session, DB_PATH
    from okul.quota import check_and_use_quota, DAILY_LIMIT
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

    @app.get("/", response_class=HTMLResponse)
    async def root():
        try:
            index_file = os.path.join(STATIC_DIR, "index.html")
            if os.path.exists(index_file):
                with open(index_file, "r", encoding="utf-8") as f:
                    return HTMLResponse(content=f.read())
            return HTMLResponse("<h2>OkulAI: index.html bulunamadı.</h2>")
        except Exception as e:
            return HTMLResponse(f"<h2>Sayfa okuma hatası: {str(e)}</h2>")

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page():
        try:
            admin_file = os.path.join(STATIC_DIR, "admin.html")
            if os.path.exists(admin_file):
                with open(admin_file, "r", encoding="utf-8") as f:
                    return HTMLResponse(content=f.read())
            return HTMLResponse("<h2>OkulAI: admin.html bulunamadı.</h2>")
        except Exception as e:
            return HTMLResponse(f"<h2>Admin sayfası okuma hatası: {str(e)}</h2>")

    @app.get("/api/quota")
    async def get_quota_status(request: Request, response: Response):
        ip = get_client_ip(request)
        dev_id = get_device_cookie(request, response)
        allowed, remaining = check_and_use_quota(ip, dev_id)
        return {"daily_limit": DAILY_LIMIT, "remaining_quota": remaining}

    @app.post("/api/analyze")
    async def analyze_question(
        request: Request,
        response: Response,
        image: UploadFile = File(...),
        provider: str = Form(...),
        api_token: str = Form(...),
        grade: int = Form(9),
        profile_id: Optional[str] = Form(None)
    ):
        ip = get_client_ip(request)
        dev_id = get_device_cookie(request, response)

        allowed, remaining = check_and_use_quota(ip, dev_id)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Günlük ücretsiz soru çözme limitiniz doldu! Yarın tekrar deneyebilirsiniz."
            )

        image_bytes = await image.read()
        mime_type = image.content_type or "image/jpeg"

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
    async def search_content(query: str = Form(...), grade: Optional[int] = Form(None)):
        books_results = search_textbooks(query=query, grade=grade, limit=4)
        web_results = await search_web_educational(query=query, limit=3)
        return {
            "textbook_results": books_results,
            "web_results": web_results
        }

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

    @app.post("/api/admin/login")
    async def admin_login(response: Response, username: str = Form(...), password: str = Form(...)):
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

        return {
            "total_analyzed_questions": total_questions,
            "indexed_books": total_books,
            "tracked_users_today": total_tracked
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

except Exception as _startup_err:
    # --- HERHANGİ BİR ÇÖKMEDE 502/503 YERİNE HATAYI DOĞRUDAN EKRANA BASAR ---
    _err_tb = traceback.format_exc()
    app = FastAPI(title="OkulAI Diagnostic")

    @app.api_route("/{path_name:path}", methods=["GET", "POST"])
    async def diagnostic_fallback(path_name: str):
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"><title>OkulAI Rapor</title></head>
        <body style="font-family: -apple-system, sans-serif; background: #0f172a; color: #f8fafc; padding: 25px;">
            <div style="max-width: 800px; margin: 0 auto; background: #1e293b; border-radius: 12px; border: 1px solid #ef4444; padding: 20px;">
                <h2 style="color: #ef4444; margin-top: 0;">⚠️ Sunucu Başlatma Hatası Yakalandı</h2>
                <p style="color: #cbd5e1; font-size: 14px;">Sunucunun 502 vermesine neden olan hata aşağıdadır:</p>
                <pre style="background: #020617; color: #38bdf8; padding: 15px; border-radius: 8px; overflow-x: auto; font-size: 13px; line-height: 1.4;">{_err_tb}</pre>
            </div>
        </body>
        </html>
        """)
