# Flashscore Tahmin - Web Sürümü

Bu proje, masaüstü Tkinter arayüzünün telefon ve bilgisayarda tarayıcıdan kullanılabilen Flask arayüzüne uyarlanmış ilk sürümüdür.

## Yerelde çalıştırma

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python app.py
```

Tarayıcıda `http://127.0.0.1:5000` adresini açın.

## Render

Bu depoyu GitHub'a yükleyin ve Render'da **New Web Service** ile bağlayın. `render.yaml` içindeki build ve start komutları kullanılabilir.

Not: Flashscore sayfa yapısını, erişim kurallarını veya bot korumasını değiştirebilir. Bu nedenle fikstür/analiz çekimi her zaman çalışmayabilir. Üçüncü taraf sitenin kullanım koşullarına ve erişim kurallarına uyun.
