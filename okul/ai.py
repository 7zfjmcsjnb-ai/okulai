import base64
import json
import httpx
from okul.database import db_session

def get_student_mistake_history(profile_id: str, limit: int = 5) -> str:
    """Öğrencinin geçmiş hatalarını AI hafızasına ekler."""
    if not profile_id:
        return ""
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT subject, student_error, ai_advice FROM mistakes WHERE profile_id = ? ORDER BY id DESC LIMIT ?",
            (profile_id, limit)
        )
        rows = cursor.fetchall()
        if not rows:
            return ""
        
        history = "\n[ÖĞRENCİNİN DAHA ÖNCE YAPTIĞI HATALAR]:\n"
        for r in rows:
            history += f"- Ders/Konu: {r['subject']} | Hata Özeti: {r['student_error']} | Tavsiye: {r['ai_advice']}\n"
        return history

async def analyze_question_image(
    provider: str,
    api_token: str,
    image_bytes: bytes,
    mime_type: str,
    grade: int,
    profile_id: str = None
) -> dict:
    """
    Soruyu güncel Gemini 2.0 Flash / GPT / Claude modelleriyle Maarif Modeline göre çözer.
    """
    mistake_history = get_student_mistake_history(profile_id)
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    system_prompt = f"""
Sen Türkiye Yüzyılı Maarif Modeli müfredatına (9, 10, 11. sınıflar) tam hâkim, uzman bir öğretmensin.
Mevcut Sınıf Seviyesi: {grade}. Sınıf.

{mistake_history}

Görevin:
2. Eğer görselde soru yoksa kesinlikle "Lütfen soru içeren resim ekleyin.Soru olmayan içerikler ve bulanık resimler işlenememektedir" de.
1. Görseldeki soruyu adım adım, mantığını kavratacak şekilde çöz.
2. Maarif Modeli'nin beceri temelli yaklaşımına uygun olarak bu soruda hangi kazanımın ölçüldüğünü belirt.
3. Öğrencinin geçmiş hatalarına dikkat et! Eğer bu soru, geçmişte yanlış yaptığı bir konuyla bağlantılıysa onu mutlaka uyar.
4. Konuyu pekiştirmesi için çok kısa bir özet ve dikkat edilmesi gereken püf noktası ver.

Yanıtını kesinlikle sadece şu JSON formatında ver:
{{
  "subject": "Ders ve Konu Başlığı",
  "step_by_step_solution": "Detaylı adım adım çözüm",
  "identified_mistake_risk": "Öğrencinin nerede takılmış olabileceği ve geçmiş hatalarla bağlantısı",
  "maarif_curriculum_note": "Müfredat kazanımı ve püf noktası",
  "advice": "Gelecek sorular için tavsiye"
}}
Sadece geçerli bir JSON çıktısı üret, markdown etiketleri (```json) kullanma.
"""

    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1. GPT-4o-mini
        if provider.lower() == "gpt":
            url = "https://api.openai.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
            payload = {
                "model": "gpt-4o-mini",
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": [
                        {"type": "text", "text": "Lütfen bu soruyu analiz et ve Maarif Modeli'ne göre açıkla."},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_image}"}}
                    ]}
                ]
            }
            res = await client.post(url, json=payload, headers=headers)
            res.raise_for_status()
            content = res.json()["choices"][0]["message"]["content"]

        # 2. Claude 3.5 Sonnet
        elif provider.lower() == "claude":
            url = "https://api.anthropic.com/v1/messages"
            headers = {
                "x-api-key": api_token,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 2048,
                "system": system_prompt,
                "messages": [
                    {"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64_image}},
                        {"type": "text", "text": "Lütfen bu soruyu incele ve sadece istenen JSON formatında yanıtla."}
                    ]}
                ]
            }
            res = await client.post(url, json=payload, headers=headers)
            res.raise_for_status()
            content = res.json()["content"][0]["text"]

        # 3. Google Gemini (Güncel Gemini 2.0 Flash & Güvenli Header Kimlik Doğrulaması)
        elif provider.lower() == "gemini":
            headers = {
                "x-goog-api-key": api_token,
                "Content-Type": "application/json"
            }
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "generationConfig": {"responseMimeType": "application/json"},
                "contents": [{
                    "parts": [
                        {"inlineData": {"mimeType": mime_type, "data": b64_image}},
                        {"text": "Bu soruyu Maarif Modeli'ne göre çöz ve JSON olarak dön."}
                    ]
                }]
            }

            # Google'ın güncel modellerini sırayla dener
            models_to_try = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-flash-latest"]
            content = None
            last_error_msg = ""

            for m in models_to_try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
                try:
                    res = await client.post(url, json=payload, headers=headers)
                    if res.status_code == 200:
                        content = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                        break
                    else:
                        error_detail = res.json().get("error", {}).get("message", res.text)
                        last_error_msg = f"{m} modeli hatası: {error_detail}"
                except Exception as ex:
                    last_error_msg = str(ex)

            if not content:
                raise ValueError(f"Google Gemini yanıt veremedi: {last_error_msg}")

        else:
            raise ValueError("Geçersiz sağlayıcı. 'gemini', 'gpt' veya 'claude' seçiniz.")

    # Modelin olası markdown etiketlerini temizle
    clean_json = content.strip()
    if clean_json.startswith("```json"):
        clean_json = clean_json[7:]
    elif clean_json.startswith("```"):
        clean_json = clean_json[3:]
    if clean_json.endswith("```"):
        clean_json = clean_json[:-3]

    return json.loads(clean_json.strip())
