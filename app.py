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
    lines = [normalize_text(line) for line in (page_text or "").splitlines()]
    lines = [line for line in lines if line]
    try:
        target = datetime.strptime(requested_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Tarih biçimi geçersiz.")
    target_key = (target.day, target.month)
    fixtures, seen = [], set()
    date_time_pattern = re.compile(
        r"^(\d{1,2})\.(\d{1,2})\.(?:\d{4}\.?)?\s+(\d{1,2}:\d{2})$"
    )
    i = 0
    while i < len(lines):
        match = date_time_pattern.match(lines[i])
        if not match:
            i += 1
            continue
        day, month, kickoff = int(match.group(1)), int(match.group(2)), match.group(3)
        if (day, month) != target_key:
            i += 1
            continue
        teams, j = [], i + 1
        while j < len(lines) and len(teams) < 2:
            candidate = lines[j]
            if date_time_pattern.match(candidate):
                break
            upper = candidate.upper()
            if upper in {"PREVIEW", "PREVİEW", "CANLI", "ERTELENDİ", "İPTAL", "MAÇ BİTTİ", "DEVRE ARASI",
                         "İSPANYA:", "ALMANYA:", "İNGİLTERE:", "FRANSA:", "İTALYA:", "TÜRKİYE:", "CANLI PUAN DURUMU"}:
                j += 1
                continue
            if len(candidate) > 1:
                teams.append(candidate)
            j += 1
        if len(teams) == 2:
            key = (kickoff, teams[0], teams[1])
            if key not in seen:
                fixtures.append({"time": kickoff, "home": teams[0], "away": teams[1], "url": "", "analysis": ""})
                seen.add(key)
        i += 1
    return fixtures


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
        url = BASE_URL + league_path + "/?d=" + date_obj.strftime("%Y-%m-%d")
        with sync_playwright() as p:
            browser, context = browser_context(p)
            try:
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
                fixtures = parse_fixtures_from_text(page.locator("body").inner_text(timeout=15000), date_text)
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
