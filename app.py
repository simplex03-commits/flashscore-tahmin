# -*- coding: utf-8 -*-
"""Flashscore fikstur ve bahis analizi - web surumu."""
import os
import re
import threading
import atexit
from datetime import datetime
from urllib.parse import urljoin

import httpx
from lxml import html as lxml_html

from flask import Flask, jsonify, render_template, request

# Chromium'u Render'in gecici cache klasoru yerine uygulama dizininde tut.
# Ayni yol hem build sirasinda hem de uygulama calisirken kullanilir.
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".playwright-browsers"
)

from playwright.sync_api import sync_playwright

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



# Önce hafif HTTP istemcisi denenir; Flashscore içeriği sunucu tarafında
# gelmiyorsa mevcut Playwright akışına geri dönülür.
HTTP_TIMEOUT = httpx.Timeout(connect=8.0, read=18.0, write=10.0, pool=8.0)
HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
}
_HTTP_CLIENT = httpx.Client(
    headers=HTTP_HEADERS,
    timeout=HTTP_TIMEOUT,
    follow_redirects=True,
)


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


def attach_match_urls(page, fixtures):
    """Her fikstürü kendi maç satırındaki Flashscore bağlantısıyla eşleştirir.

    Önceki sürüm, bağlantının üst kapsayıcılarının tamamını tarıyordu.
    Bu kapsayıcılar birden fazla maçı içerdiğinde ilk maçın bağlantısı
    bütün fikstürlere atanabiliyordu. Yeni sürüm en küçük eşleşen DOM
    kapsayıcısını seçer ve aşırı geniş kapsayıcıları dikkate almaz.
    """
    links = page.evaluate(r"""() => {
      const result = [];
      const seen = new Set();
      const norm = s => (s || '').replace(/\s+/g, ' ').trim();
      for (const a of document.querySelectorAll('a[href*="/mac/"]')) {
        const href = a.href;
        let node = a;
        // En yakın ve kısa maç satırlarını topla; tüm fikstür listesini
        // kapsayan büyük üst elemanları özellikle dışarıda bırak.
        for (let depth = 0; depth < 7 && node; depth++, node = node.parentElement) {
          const text = norm(node.innerText);
          if (text.length < 8 || text.length > 260) continue;
          const key = href + '|' + text;
          if (!seen.has(key)) {
            result.push({url: href, text, length: text.length});
            seen.add(key);
          }
        }
      }
      return result;
    }""")

    for fixture in fixtures:
        home = normalize_text(fixture["home"]).casefold()
        away = normalize_text(fixture["away"]).casefold()

        # Eşleşen en küçük metin, genellikle ilgili maçın kendi satırıdır.
        candidates = []
        for entry in links:
            text = normalize_text(entry.get("text", "")).casefold()
            if home in text and away in text:
                candidates.append(entry)

        if candidates:
            candidates.sort(key=lambda item: item.get("length", len(item.get("text", ""))))
            fixture["url"] = candidates[0].get("url", "")
        else:
            fixture["url"] = ""


def extract_analysis(page):
    result = page.evaluate(r"""() => {
      const norm = s => (s || '').replace(/\s+/g, ' ').trim();
      const lower = s => norm(s).toLocaleLowerCase('tr-TR');
      const disclaimers = ['AI kullanılarak çevrilmiştir', 'Bu içerik yapay zeka kullanılarak oluşturuldu'];
      const cutAtDisclaimer = text => {
        const lowered = text.toLocaleLowerCase('tr-TR');
        let cutIndex = -1, cutLength = 0;
        for (const phrase of disclaimers) {
          const index = lowered.indexOf(phrase.toLocaleLowerCase('tr-TR'));
          if (index >= 0 && (cutIndex < 0 || index < cutIndex)) { cutIndex = index; cutLength = phrase.length; }
        }
        return cutIndex >= 0 ? text.slice(0, cutIndex + cutLength).trim() : text;
      };
      const headings = [...document.querySelectorAll('h1,h2,h3,h4,[role="heading"],strong,b,div,section,article')]
        .filter(el => lower(el.innerText) === 'bahis analizi');
      for (const heading of headings) {
        let node = heading;
        for (let depth = 0; depth < 7 && node; depth++, node = node.parentElement) {
          const text = norm(node.innerText);
          if (lower(text).startsWith('bahis analizi') && text.length > 25 && text.length < 3000) {
            const cleaned = cutAtDisclaimer(text.replace(/^bahis analizi\s*/i, '').trim());
            if (cleaned) return cleaned;
          }
        }
      }
      const body = (document.body.innerText || '').replace(/\r/g, '');
      const lowBody = body.toLocaleLowerCase('tr-TR'), marker = 'bahis analizi';
      const index = lowBody.indexOf(marker);
      if (index >= 0) {
        let section = body.slice(index + marker.length).trim();
        const original = section;
        section = cutAtDisclaimer(section);
        if (section === original) {
          const stops = ['\nİstatistik', '\nMaç Merkezi', '\nCanlı Skor', '\nPuan Durumu', '\nSonuçlar', '\nHaberler'];
          let end = section.length;
          for (const stop of stops) { const pos = section.toLocaleLowerCase('tr-TR').indexOf(stop.toLocaleLowerCase('tr-TR')); if (pos >= 0 && pos < end) end = pos; }
          section = section.slice(0, end);
        }
        section = norm(section);
        if (section.length > 15 && section.length < 3000) return section;
      }
      return '';
    }""")
    result = normalize_text(result)
    return result or "Bu maç sayfasında 'Bahis Analizi' bölümü bulunamadı."


# Playwright senkron API'si thread-bound'dur. Flask istekleri farklı
# thread'lerde çalışabileceği için her thread kendi Playwright/Chromium
# örneğini kullanır. Aynı thread sonraki isteklerde tarayıcıyı yeniden kullanır.
_BROWSER_STATE = threading.local()


def browser_context(_p=None):
    playwright = getattr(_BROWSER_STATE, "playwright", None)
    browser = getattr(_BROWSER_STATE, "browser", None)

    if playwright is None or browser is None or not browser.is_connected():
        # Bozuk/eski oturumu bu thread içinde temizle.
        try:
            if browser is not None and browser.is_connected():
                browser.close()
        except Exception:
            pass
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            pass

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        _BROWSER_STATE.playwright = playwright
        _BROWSER_STATE.browser = browser

    context = browser.new_context(locale="tr-TR")
    # Görsel/video/font indirmeleri maç metni ve analiz için gerekli değil.
    context.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ("image", "media", "font")
        else route.continue_()
    )
    return browser, context


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
        league_path = LEAGUES[league].rstrip("/")
        if not league_path.endswith("/fikstur"):
            league_path += "/fikstur"
        league_path += "/"
        url = BASE_URL + league_path + "?d=" + date_obj.strftime("%Y-%m-%d")

        # 1) Hızlı yol: HTTPX + lxml (Chromium başlatılmaz).
        try:
            response, document, page_text = http_get_html(url)
            fixtures = parse_fixtures_from_text(page_text, date_text)
            if fixtures:
                attach_match_urls_from_html(document, fixtures)
            # Eksik liste veya maç bağlantısı varsa sağlam mevcut yönteme dön.
            if fixtures and all(item.get("url") for item in fixtures):
                app.logger.info("Fikstur kaynagi=httpx status=%s count=%s", response.status_code, len(fixtures))
                return jsonify(fixtures=fixtures, count=len(fixtures), source="httpx")
            app.logger.info("HTTPX fikstur verisi yetersiz; Playwright yedegine geciliyor. count=%s", len(fixtures))
        except Exception as http_exc:
            app.logger.warning("HTTPX fikstur denemesi basarisiz; Playwright yedegine geciliyor: %s", http_exc)

        # 2) Yedek yol: çalışan Playwright akışı.
        browser, context = browser_context()
        try:
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(900)
            page_text = page.locator("body").inner_text(timeout=15000)
            fixtures = parse_fixtures_from_text(page_text, date_text)
            attach_match_urls(page, fixtures)
            app.logger.info(
                "Fikstur kaynagi=playwright status=%s url=%s fixtures=%s body_chars=%s",
                response.status if response else "no-response", page.url, len(fixtures), len(page_text)
            )
            if not fixtures:
                app.logger.warning("Flashscore sayfa metni (ilk 1800 karakter): %s",
                                    normalize_text(page_text)[:1800])
        finally:
            context.close()
        return jsonify(fixtures=fixtures, count=len(fixtures), source="playwright")
    except Exception as exc:
        app.logger.exception("Fikstur alinamadi")
        return jsonify(error=f"Maçlar alınamadı: {str(exc)[:300]}"), 500


@app.post("/api/analysis")
def api_analysis():
    data = request.get_json(silent=True) or {}
    match_url = data.get("url", "")
    if not match_url.startswith(BASE_URL + "/"):
        return jsonify(error="Geçersiz maç bağlantısı."), 400
    try:
        # 1) Hızlı yol: analiz HTML içinde mevcutsa doğrudan al.
        try:
            response, document, _ = http_get_html(match_url)
            analysis = extract_analysis_from_html(document)
            if analysis:
                app.logger.info("Analiz kaynagi=httpx status=%s url=%s", response.status_code, match_url)
                return jsonify(analysis=analysis, source="httpx")
            app.logger.info("Analiz HTTPX yanitinda bulunamadi; Playwright yedegine geciliyor.")
        except Exception as http_exc:
            app.logger.warning("HTTPX analiz denemesi basarisiz; Playwright yedegine geciliyor: %s", http_exc)

        # 2) Flashscore içeriği JavaScript ile oluşturuyorsa mevcut tarayıcı yolu.
        browser, context = browser_context()
        try:
            page = context.new_page()
            page.goto(match_url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_function(
                    "() => (document.body?.innerText || '').toLocaleLowerCase('tr-TR').includes('bahis analizi')",
                    timeout=5000
                )
            except Exception:
                pass
            analysis = extract_analysis(page)
        finally:
            context.close()
        return jsonify(analysis=analysis, source="playwright")
    except Exception as exc:
        app.logger.exception("Analiz alinamadi")
        return jsonify(error=f"Analiz alınamadı: {str(exc)[:300]}"), 500


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
