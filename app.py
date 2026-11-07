# -*- coding: utf-8 -*-
"""Flashscore fikstur ve bahis analizi - web surumu."""
import os
import re
import threading
import sqlite3
import json
from datetime import datetime
from urllib.parse import urljoin

import httpx
from lxml import html as lxml_html

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
BASE_URL = "https://www.flashscore.com.tr"
LEAGUES = {
    "Bundesliga": "/futbol/almanya/bundesliga/fikstur/",
    "Ligue 1": "/futbol/fransa/ligue-1/fikstur/",
    "Premier League": "/futbol/ingiltere/premier-league/",
    "La Liga": "/futbol/ispanya/laliga/fikstur/",
    "Serie A": "/futbol/italya/serie-a/fikstur/",
    "Süper Lig": "/futbol/turkiye/super-lig/fikstur/",
    "Şampiyonlar Ligi": "/futbol/europe/sampiyonlar-ligi/fikstur/",
    "Avrupa Ligi": "/futbol/europe/avrupa-ligi/fikstur/",
    "Konferans Ligi": "/futbol/europe/konferans-ligi/fikstur/",
}



# Flashscore verileri yalnızca HTTPX + lxml ile alınır.
# Tarayıcı otomasyonu / Playwright yedeği kullanılmaz.
HTTP_TIMEOUT = httpx.Timeout(connect=8.0, read=18.0, write=10.0, pool=8.0)
HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
}
# Render'in 512 MB bellek sınırında eşzamanlı HTML ayrıştırmalarını önle.
# İstekler sıraya girer; aynı içerik SQLite önbelleğindeyse sırayı beklemeden döner.
_SCRAPE_SEMAPHORE = threading.BoundedSemaphore(1)

# SQLite önbelleği: kayıtlar süre dolduğu için silinmez.
# Render'da kalıcı disk bağlanırsa CACHE_DB_PATH=/var/data/flashscore_cache.sqlite3
# ayarlanarak önbellek deploy/yeniden başlatmalardan sonra da korunabilir.
CACHE_DB_PATH = os.environ.get(
    "CACHE_DB_PATH", "/tmp/flashscore_cache.sqlite3"
)

_HTTP_CLIENT = httpx.Client(
    headers=HTTP_HEADERS,
    timeout=HTTP_TIMEOUT,
    follow_redirects=True,
    limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
)



def _cache_connection():
    folder = os.path.dirname(CACHE_DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return conn


def _serialize_cache_key(key):
    return json.dumps(key, ensure_ascii=False, separators=(",", ":"))


def cache_get(key):
    """Daha önce çekilmiş veriyi süresiz SQLite önbelleğinden döndürür."""
    conn = _cache_connection()
    try:
        row = conn.execute(
            "SELECT payload FROM cache WHERE cache_key = ?",
            (_serialize_cache_key(key),)
        ).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def cache_set(key, value):
    """Başarılı veriyi süresiz saklar; aynı anahtar tekrar indirilmez."""
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    conn = _cache_connection()
    try:
        conn.execute(
            "INSERT INTO cache(cache_key, payload) VALUES(?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, "
            "created_at=CURRENT_TIMESTAMP",
            (_serialize_cache_key(key), payload)
        )
        conn.commit()
    finally:
        conn.close()


def acquire_scrape_slot():
    # Diğer cihazın isteği bitene kadar bekle; sunucuya paralel yük bindirme.
    return _SCRAPE_SEMAPHORE.acquire(timeout=60)


def http_get_html(url):
    """Flashscore HTML'sini tarayıcı başlatmadan almaya çalışır."""
    response = _HTTP_CLIENT.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and "text" not in content_type:
        raise ValueError(f"Beklenmeyen içerik türü: {content_type or 'bilinmiyor'}")
    page = lxml_html.fromstring(response.text)
    title = " ".join(page.xpath("//title//text()"))
    body_text = "\n".join(t.strip() for t in page.xpath("//body//text()") if t.strip())
    # Bot kontrolü / erişim engeli sayfalarını başarılı veri gibi kullanma.
    blocked_markers = ("access denied", "just a moment", "captcha", "verify you are human")
    if any(x in (title + " " + body_text[:1500]).lower() for x in blocked_markers):
        raise RuntimeError("Flashscore HTTP isteğini doğrulama/erişim sayfasına yönlendirdi.")
    return response, page, body_text


def attach_match_urls_from_html(document, fixtures):
    """HTML içindeki maç bağlantılarını takım adlarıyla eşleştirir."""
    links = []
    seen = set()
    for anchor in document.xpath('//a[contains(@href, "/mac/")]'):
        href = urljoin(BASE_URL, anchor.get("href", ""))
        node = anchor
        for _ in range(7):
            if node is None:
                break
            text = normalize_text(" ".join(node.xpath(".//text()")))
            if 8 <= len(text) <= 260:
                key = (href, text)
                if key not in seen:
                    links.append({"url": href, "text": text, "length": len(text)})
                    seen.add(key)
            node = node.getparent()

    for fixture in fixtures:
        home = normalize_text(fixture["home"]).casefold()
        away = normalize_text(fixture["away"]).casefold()
        candidates = [x for x in links
                      if home in normalize_text(x["text"]).casefold()
                      and away in normalize_text(x["text"]).casefold()]
        candidates.sort(key=lambda x: x["length"])
        fixture["url"] = candidates[0]["url"] if candidates else ""


def extract_analysis_from_html(document):
    """Bahis Analizi metnini statik HTML'den çıkarmayı dener."""
    text = "\n".join(t.strip() for t in document.xpath("//body//text()") if t.strip())
    return extract_analysis_from_text(text)


def extract_analysis_from_text(page_text):
    """Bahis Analizi başlığından metni alır; sayfa metni yetersizse boş döner."""
    if not page_text:
        return ""
    lines = [x.strip() for x in page_text.replace("\r", "").splitlines() if x.strip()]
    for i, line in enumerate(lines):
        if line.casefold() == "bahis analizi":
            section = " ".join(lines[i + 1:i + 35]).strip()
            lowered = section.casefold()
            for marker in ("bu içerik yapay zeka kullanılarak oluşturuldu",
                           "ai kullanılarak çevrilmiştir"):
                pos = lowered.find(marker.casefold())
                if pos >= 0:
                    section = section[:pos + len(marker)]
                    break
            section = normalize_text(section)
            if 15 < len(section) < 3000:
                return section
    return ""

def normalize_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def parse_fixtures_from_text(page_text, requested_date):
    """Sayfadaki görünen fikstür metninden seçilen tarihin maçlarını çıkarır."""
    lines = [normalize_text(line) for line in (page_text or "").splitlines()]
    lines = [line for line in lines if line]

    try:
        target = datetime.strptime(requested_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Tarih biçimi YYYY-AA-GG olmalı.")

    target_key = (target.day, target.month)
    fixtures = []
    seen = set()
    i = 0

    date_time_pattern = re.compile(
        r"^(\d{1,2})\.(\d{1,2})\.?\s+(\d{1,2}:\d{2})$"
    )

    while i < len(lines):
        match = date_time_pattern.match(lines[i])
        if not match:
            i += 1
            continue

        day, month, kickoff = int(match.group(1)), int(match.group(2)), match.group(3)
        if (day, month) != target_key:
            i += 1
            continue

        # Flashscore metninde tarih/saat satırının ardından genellikle iki takım gelir.
        teams = []
        j = i + 1
        while j < len(lines) and len(teams) < 2:
            candidate = lines[j]
            if date_time_pattern.match(candidate):
                break
            if candidate.upper() in {
                "PREVIEW", "PREVİEW", "CANLI", "ERTELENDİ", "İPTAL",
                "MAÇ BİTTİ", "DEVRE ARASI"
            }:
                j += 1
                continue
            if len(candidate) <= 1:
                j += 1
                continue
            # Menü/lig başlıklarını atla.
            if candidate.upper() in {
                "İSPANYA:", "ALMANYA:", "İNGİLTERE:", "FRANSA:",
                "İTALYA:", "TÜRKİYE:", "CANLI PUAN DURUMU"
            }:
                j += 1
                continue
            teams.append(candidate)
            j += 1

        if len(teams) == 2:
            key = (kickoff, teams[0], teams[1])
            if key not in seen:
                fixtures.append({
                    "time": kickoff,
                    "home": teams[0],
                    "away": teams[1],
                    "url": "",
                    "analysis": "",
                })
                seen.add(key)

        i += 1

    # Render'da Flashscore bazen fikstürü saatli ayrı satırlar yerine
    # "07.11 Takım A - Takım B, Takım C - Takım D" biçiminde sunuyor.
    # Yukarıdaki klasik ayrıştırıcı sonuç bulamazsa bu görünümü de oku.
    if not fixtures:
        compact_text = normalize_text(page_text)
        date_blocks = re.finditer(
            r"(?<!\d)(\d{1,2})\.(\d{1,2})\s+(.+?)"
            r"(?=(?:\s+\d{1,2}\.\d{1,2}\s)|$)",
            compact_text
        )

        for block in date_blocks:
            day, month = int(block.group(1)), int(block.group(2))
            if (day, month) != target_key:
                continue

            match_list = block.group(3)
            for item in match_list.split(","):
                item = normalize_text(item)
                teams = re.split(r"\s+[–—-]\s+", item, maxsplit=1)
                if len(teams) != 2:
                    continue

                home, away = map(normalize_text, teams)
                # Menü metni veya açıklama parçalarının maç sanılmasını önle.
                if not home or not away or len(home) > 55 or len(away) > 55:
                    continue
                if any(word in home.upper() for word in (
                    "FUTBOL", "BASKETBOL", "VOLEYBOL", "FİKSTÜR", "PUAN DURUMU"
                )):
                    continue
                key = ("—", home, away)
                if key not in seen:
                    fixtures.append({
                        "time": "—",
                        "home": home,
                        "away": away,
                        "url": "",
                        "analysis": "",
                    })
                    seen.add(key)

    return fixtures



@app.get("/")
def index():
    return render_template("index.html", leagues=list(LEAGUES.keys()))


@app.post("/api/fixtures")
def api_fixtures():
    data = request.get_json(silent=True) or {}
    date_text, league = data.get("date", ""), data.get("league", "")
    try:
        date_obj = datetime.strptime(date_text, "%Y-%m-%d")
        if league not in LEAGUES:
            return jsonify(error="Lütfen geçerli bir lig seçin."), 400

        cache_key = ("fixtures", date_text, league)
        cached = cache_get(cache_key)
        if cached is not None:
            return jsonify(fixtures=cached, count=len(cached), source="cache")

        if not acquire_scrape_slot():
            response = jsonify(error="Sunucu şu anda başka bir veri isteğini işliyor. Lütfen tekrar deneyin.")
            response.status_code = 503
            response.headers["Retry-After"] = "5"
            return response
        try:
            # Sıra beklerken aynı veri başka istek tarafından önbelleğe alınmış olabilir.
            cached = cache_get(cache_key)
            if cached is not None:
                return jsonify(fixtures=cached, count=len(cached), source="cache")

            league_path = LEAGUES[league].rstrip("/")
            if not league_path.endswith("/fikstur"):
                league_path += "/fikstur"
            league_path += "/"
            url = BASE_URL + league_path + "?d=" + date_obj.strftime("%Y-%m-%d")

            response, document, page_text = http_get_html(url)
            fixtures = parse_fixtures_from_text(page_text, date_text)
            if fixtures:
                attach_match_urls_from_html(document, fixtures)

            missing_urls = sum(1 for item in fixtures if not item.get("url"))
            app.logger.info(
                "Fikstur kaynagi=httpx status=%s count=%s missing_urls=%s",
                response.status_code, len(fixtures), missing_urls
            )
            if missing_urls:
                app.logger.warning(
                    "HTTPX fiksturlerinde %s mac icin mac baglantisi bulunamadi.",
                    missing_urls
                )
            cache_set(cache_key, fixtures)
            return jsonify(fixtures=fixtures, count=len(fixtures), source="httpx")
        finally:
            _SCRAPE_SEMAPHORE.release()
    except Exception as exc:
        app.logger.exception("HTTPX ile fikstur alinamadi")
        return jsonify(error=f"Maçlar HTTPX ile alınamadı: {str(exc)[:300]}"), 502


@app.post("/api/analysis")
def api_analysis():
    data = request.get_json(silent=True) or {}
    match_url = data.get("url", "")
    if not isinstance(match_url, str) or not match_url.startswith(BASE_URL + "/"):
        return jsonify(error="Geçersiz maç bağlantısı."), 400

    cache_key = ("analysis", match_url)
    cached = cache_get(cache_key)
    if cached is not None:
        return jsonify(analysis=cached, source="cache")

    if not acquire_scrape_slot():
        response = jsonify(error="Sunucu şu anda başka bir veri isteğini işliyor. Lütfen tekrar deneyin.")
        response.status_code = 503
        response.headers["Retry-After"] = "5"
        return response
    try:
        # Sıra beklerken aynı analiz başka istek tarafından alınmış olabilir.
        cached = cache_get(cache_key)
        if cached is not None:
            return jsonify(analysis=cached, source="cache")

        response, document, _ = http_get_html(match_url)
        analysis = extract_analysis_from_html(document)
        if not analysis:
            app.logger.warning(
                "Bahis analizi HTTPX yanitinda bulunamadi: %s", match_url
            )
            return jsonify(
                error="Bu maçın bahis analizi HTTPX yanıtında bulunamadı. Flashscore sayfa içeriğini değiştirmiş olabilir."
            ), 502

        cache_set(cache_key, analysis)
        app.logger.info(
            "Analiz kaynagi=httpx status=%s url=%s",
            response.status_code, match_url
        )
        return jsonify(analysis=analysis, source="httpx")
    except Exception as exc:
        app.logger.exception("HTTPX ile analiz alinamadi")
        return jsonify(error=f"Bahis analizi HTTPX ile alınamadı: {str(exc)[:300]}"), 502
    finally:
        _SCRAPE_SEMAPHORE.release()


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
