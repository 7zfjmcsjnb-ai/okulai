import io
import httpx
from pypdf import PdfReader
from okul.database import db_session

def index_pdf_book(pdf_bytes: bytes, book_name: str, grade: int, subject: str):
    """
    Yüklenen PDF kitabını ayıklar ve aranabilir metin olarak kaydeder.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    with db_session() as conn:
        cursor = conn.cursor()
        for idx, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                cursor.execute("""
                INSERT INTO book_pages (grade, subject, book_name, page_num, content)
                VALUES (?, ?, ?, ?, ?)
                """, (grade, subject, book_name, idx + 1, text))

def search_textbooks(query: str, grade: int = None, limit: int = 5) -> list[dict]:
    """
    Yüklenmiş MEB kitapları içinde anahtar kelime araması yapar.
    """
    with db_session() as conn:
        cursor = conn.cursor()
        sql = "SELECT book_name, grade, subject, page_num, content FROM book_pages WHERE content LIKE ?"
        params = [f"%{query}%"]
        if grade:
            sql += " AND grade = ?"
            params.append(grade)
        sql += " LIMIT ?"
        params.append(limit)
        
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        
        results = []
        for r in rows:
            snippet = r["content"][:250].replace("\n", " ") + "..."
            results.append({
                "book_name": r["book_name"],
                "grade": r["grade"],
                "subject": r["subject"],
                "page": r["page_num"],
                "snippet": snippet
            })
        return results

async def search_web_educational(query: str, limit: int = 5) -> list[dict]:
    """
    İnternet genelinde MEB ve Maarif Modeli içeriklerini arayan hafif tarayıcı.
    DuckDuckGo HTML üzerinden çalışır.
    """
    search_query = f"{query} maarif modeli meb ders anlatımı"
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    results = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, data={"q": search_query}, headers=headers)
            if res.status_code == 200:
                # Basit metin ve link ayıklayıcı
                from re import findall
                matches = findall(r'<a class="result__snippet[^>]*href="([^"]+)"[^>]*>(.*?)</a>', res.text)
                for link, text in matches[:limit]:
                    clean_text = text.replace("<b>", "").replace("</b>", "").strip()
                    results.append({"title": clean_text[:60] + "...", "snippet": clean_text, "link": link})
    except Exception:
        pass
    return results
