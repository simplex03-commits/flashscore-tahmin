# -*- coding: utf-8 -*-
"""Flashscore fikstur ve bahis analizi - web surumu."""
import os
import re
from datetime import datetime
from urllib.parse import urljoin

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


def normalize_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def parse_fixtures_from_text(page_text, requested_date):
    """Flashscore sayfa metninden maçları bulur; tarih/saat satırlarının
    tek satırda veya ayrı satırlarda gelmesini destekler."""
    lines = [normalize_text(line) for line in (page_text or "").splitlines()]
    lines = [line for line in lines if line]

    try:
        target = datetime.strptime(requested_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Tarih biçimi geçersiz.")

    target_key = (target.day, target.month)
    fixtures, seen = [], set()

    date_pattern = re.compile(
        r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\.?$"
    )
    combined_pattern = re.compile(
        r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\.?\s+(\d{1,2}:\d{2})$"
    )
    time_pattern = re.compile(r"^\d{1,2}:\d{2}$")
    ignored = {
        "PREVIEW", "PREVİEW", "CANLI", "ERTELENDİ", "İPTAL",
        "MAÇ BİTTİ", "DEVRE ARASI", "CANLI PUAN DURUMU",
        "İSPANYA:", "ALMANYA:", "İNGİLTERE:", "FRANSA:",
        "İTALYA:", "TÜRKİYE:"
    }

    i = 0
    while i < len(lines):
        day = month = None
        kickoff = None
        consumed = 1

        combined = combined_pattern.match(lines[i])
        if combined:
            day, month, kickoff = int(combined.group(1)), int(combined.group(2)), combined.group(4)
        else:
            date_match = date_pattern.match(lines[i])
            if date_match and i + 1 < len(lines) and time_pattern.match(lines[i + 1]):
                day, month = int(date_match.group(1)), int(date_match.group(2))
                kickoff = lines[i + 1]
                consumed = 2

        if day is None or (day, month) != target_key:
            i += 1
            continue

        teams = []
        j = i + consumed
        while j < len(lines) and len(teams) < 2:
            candidate = lines[j]
            if combined_pattern.match(candidate) or date_pattern.match(candidate):
                break
            if time_pattern.match(candidate) or candidate.upper() in ignored:
                j += 1
                continue
            # Avoid scores, odds, and very short UI labels.
            if len(candidate) > 1 and not re.fullmatch(r"[\d\s:.,+-]+", candidate):
                teams.append(candidate)
            j += 1

        if len(teams) == 2:
            key = (kickoff, teams[0], teams[1])
            if key not in seen:
                fixtures.append({
                    "time": kickoff, "home": teams[0], "away": teams[1],
                    "url": "", "analysis": ""
                })
                seen.add(key)
        i += consumed

    return fixtures


def extract_fixtures_from_dom(page, requested_date):
    """Önce Flashscore maç kartlarını DOM üzerinden okumayı dener."""
    selectors = [
        "div.event__match",
        "div[class*='event__match']",
        "div[class*='eventRow']"
    ]
    found = []
    seen = set()

    for card_selector in selectors:
        cards = page.locator(card_selector)
        try:
            count = min(cards.count(), 500)
        except Exception:
            continue

        for index in range(count):
            card = cards.nth(index)
            try:
                home = card.locator(
                    ".event__participant--home, [class*='participant--home']"
                ).first.inner_text(timeout=1200).strip()
                away = card.locator(
                    ".event__participant--away, [class*='participant--away']"
                ).first.inner_text(timeout=1200).strip()
                kickoff = card.locator(
                    ".event__time, [class*='event__time']"
                ).first.inner_text(timeout=1200).strip()
                if not home or not away or not kickoff:
                    continue

                # Kartlar seçilen fikstür gününde listelendiğinden tarih kartın
                # üst grup başlığından gelebilir; sayfa URL'si de o güne sabitlenir.
                key = (kickoff, home, away)
                if key in seen:
                    continue
                href = ""
                try:
                    href = card.locator("a[href*='/mac/'], a[href*='/match/']").first.get_attribute("href") or ""
                except Exception:
                    pass
                found.append({
                    "time": kickoff, "home": home, "away": away,
                    "url": urljoin(BASE_URL, href) if href else "", "analysis": ""
                })
                seen.add(key)
            except Exception:
                continue

        if found:
            return found

    return found


def attach_match_urls(page, fixtures):
    links = page.evaluate(r"""() => {
      const result = [], seen = new Set();
      for (const a of document.querySelectorAll('a[href]')) {
        const href = a.getAttribute('href') || '';
        if (!href.includes('/mac/')) continue;
        let node = a;
        for (let depth = 0; depth < 5 && node; depth++, node = node.parentElement) {
          const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
          if (text.length > 10 && text.length < 1000) {
            const key = a.href + '|' + text;
            if (!seen.has(key)) { result.push({url: a.href, text}); seen.add(key); }
          }
        }
      }
      return result;
    }""")
    for fixture in fixtures:
        home = normalize_text(fixture["home"]).casefold()
        away = normalize_text(fixture["away"]).casefold()
        for entry in links:
            text = normalize_text(entry.get("text", "")).casefold()
            if home in text and away in text:
                fixture["url"] = entry.get("url", "")
                break


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


def browser_context(p):
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(locale="tr-TR")
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
        url = BASE_URL + league_path + "/?d=" + date_obj.strftime("%Y%m%d")
        with sync_playwright() as p:
            browser, context = browser_context(p)
            try:
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)

                fixtures = extract_fixtures_from_dom(page, date_text)
                if not fixtures:
                    body_text = page.locator("body").inner_text(timeout=15000)
                    fixtures = parse_fixtures_from_text(body_text, date_text)

                if not fixtures:
                    body_text = page.locator("body").inner_text(timeout=10000)
                    app.logger.warning(
                        "Flashscore bos fikstur: status=%s final_url=%s title=%s body=%s",
                        response.status if response else "no-response",
                        page.url,
                        page.title(),
                        normalize_text(body_text)[:1200]
                    )
                attach_match_urls(page, fixtures)
            finally:
                context.close()
                browser.close()
        return jsonify(fixtures=fixtures, count=len(fixtures))
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
        with sync_playwright() as p:
            browser, context = browser_context(p)
            try:
                page = context.new_page()
                page.goto(match_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1800)
                analysis = extract_analysis(page)
            finally:
                context.close()
                browser.close()
        return jsonify(analysis=analysis)
    except Exception as exc:
        app.logger.exception("Analiz alinamadi")
        return jsonify(error=f"Analiz alınamadı: {str(exc)[:300]}"), 500


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
