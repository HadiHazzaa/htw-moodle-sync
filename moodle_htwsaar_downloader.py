import os
import sys
import re
import json
import urllib.parse
import urllib.request
from collections import defaultdict
from playwright.sync_api import sync_playwright

# ==========================================
# ZUGANGSDATEN & EINSTELLUNGEN
# ==========================================
# Secrets aus den Prozess-Umgebungsvariablen laden (lokal oder GitHub Actions)
USERNAME = os.environ.get("HTW_USER")
PASSWORD = os.environ.get("HTW_PASS")

# Flexible NTFY_TOPIC Steuerung (bevorzugt aus Umgebungsvariablen)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "htw_moodle_4a8b2c1d-9e8f-7a6b-5c4d-3e2f1a0b9c8d")

DOWNLOAD_DIR = "./moodle_downloads"
CACHE_FILE = "./download_history.json"
# ==========================================

# Fail-Fast: Abbruch, wenn Umgebungsvariablen im System fehlen
if not USERNAME or not PASSWORD:
    print("[SECURITY ERROR] HTW_USER oder HTW_PASS fehlt in den Umgebungsvariablen!")
    sys.exit(1)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Download-Verlauf laden (Verhindert mehrfaches Herunterladen)
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            download_history = json.load(f)
    except Exception:
        download_history = {}
else:
    download_history = {}

def save_history():
    """Speichert den aktuellen Download-Verlauf in der JSON-Datei"""
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(download_history, f, ensure_ascii=False, indent=2)

def clean_name(name):
    """Bereinigt Dateinamen und dekodiert URL-Sonderzeichen (%C3%9C -> Ü)"""
    if not name:
        return "Allgemein"
    decoded = urllib.parse.unquote(name)
    decoded = " ".join(decoded.split())
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", decoded)
    return cleaned.strip() or "Allgemein"

def get_safe_path(base_dir, *path_segments):
    """Schützt vor Path-Traversal-Angriffen (CWE-22)"""
    abs_base = os.path.abspath(base_dir)
    target_path = os.path.abspath(os.path.join(abs_base, *path_segments))
    
    # Prüfe Invariante: Liegt der Zielpfad im Download-Ordner?
    if os.path.commonpath([abs_base, target_path]) != abs_base:
        raise PermissionError(f"[SECURITY ALERT] Path Traversal abgefangen: {target_path}")
        
    return target_path

def send_push_notification(grouped_downloads):
    """
    freundlich visualisierte Push-Nachricht mit klarer Struktur:
    🏛️ KURS -> 📂 Echter Moodle-Ordner -> 🔵 Datei
    """
    if not grouped_downloads or not NTFY_TOPIC:
        return

    # Gesamtzahl aller neu heruntergeladenen Dateien berechnen
    total_files = sum(
        len(files) 
        for sections in grouped_downloads.values() 
        for files in sections.values()
    )
    
    title = f"🔔 {total_files} neue Moodle-Datei(en)!"
    message_lines = []

    for course_title, sections in grouped_downloads.items():
        message_lines.append(f"🏛️ {course_title}")
        message_lines.append("─" * 30) # Visuelle Trennlinie
        
        for sec_title, files in sections.items():
            message_lines.append(f"📂 {sec_title}")
            for filename in files:
                message_lines.append(f"  🔵 {filename}")
            message_lines.append("") # Abstand zwischen Ordnern
            
        message_lines.append("=" * 30) # Trenner zwischen Kursen
        message_lines.append("")

    body = "\n".join(message_lines).strip()

    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8").decode("latin-1", "ignore"),
            "Priority": "high",
            "Tags": "bell,file_folder"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                print("\n[PUSH] Benachrichtigung erfolgreich gesendet!")
    except Exception as e:
        print(f"\n[PUSH ERROR] Fehler beim Senden der Benachrichtigung: {e}")

def download_moodle_files():
    # Verschachteltes Dictionary: Kurs -> Moodle-Abschnitt -> Liste von Dateinamen
    newly_downloaded = defaultdict(lambda: defaultdict(list))

    with sync_playwright() as p:
        # Headless Mode (Hintergrundprozess ohne GUI)
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        main_page = context.new_page()

        print("Öffne direkte HTW-Saar Anmeldeseite (idp.htwsaar.de)...")
        main_page.goto("https://moodle.htwsaar.de/auth/shibboleth/index.php")

        try:
            main_page.wait_for_selector("input#username, input#j_username, input[name='j_username']", timeout=15000)
            
            print("Fülle Zugangsdaten aus...")
            main_page.fill("input#username, input#j_username, input[name='j_username']", USERNAME)
            main_page.fill("input#password, input#j_password, input[name='j_password']", PASSWORD)
            
            submit_btn = main_page.locator("button[name='_eventId_proceed'], button[type='submit'], input[type='submit']").first
            submit_btn.click()
            print("Anmeldedaten abgesendet...")

            main_page.wait_for_timeout(3000)

            # Automatisch Nutzungsrichtlinien / BigBlueButton "Fortsetzen" bestätigen
            for _ in range(3):
                continue_btn = main_page.locator("button:has-text('Fortsetzen'), input[value='Fortsetzen'], a:has-text('Fortsetzen')").first
                if continue_btn.count() > 0 and continue_btn.is_visible():
                    print("--> Nutzungsrichtlinie erkannt. Klicke auf 'Fortsetzen'...")
                    continue_btn.click()
                    main_page.wait_for_timeout(2000)

            main_page.wait_for_url("**/moodle.htwsaar.de/**", timeout=15000)
            print("Erfolgreich im Moodle eingeloggt!")

        except Exception as e:
            print(f"Hinweis beim Login: {e}")

        print("\nNavigiere zu 'Meine Kurse'...")
        main_page.goto("https://moodle.htwsaar.de/my/courses.php", wait_until="domcontentloaded")
        main_page.wait_for_timeout(2000)

        course_elements = main_page.locator("a[href*='/course/view.php?id=']").all()
        course_urls = list(set([el.get_attribute("href") for el in course_elements if el.get_attribute("href")]))
        
        print(f"Gefundene Kurse: {len(course_urls)}")

        if len(course_urls) == 0:
            print("(!) Keine Kurse gefunden. Bitte überprüfe deine Zugangsdaten.")
            browser.close()
            return

        for index, course_url in enumerate(course_urls, 1):
            try:
                # 1. Kursseite im Headless-Modus laden
                main_page.goto(course_url, wait_until="domcontentloaded")
                main_page.wait_for_timeout(2000)

                # 2. DOM-Injection: Alle einklappbaren Moodle-Abschnitte (Akkordeons) im Hintergrund erzwingen
                try:
                    main_page.evaluate("""() => {
                        document.querySelectorAll('.collapse, .course-section-header').forEach(el => {
                            el.classList.add('show');
                            el.classList.remove('collapsed');
                        });
                        document.querySelectorAll('[aria-expanded="false"]').forEach(el => {
                            el.setAttribute('aria-expanded', 'true');
                        });
                    }""")
                except Exception:
                    pass

                # Klicke zusätzlich eventuelle "Alles ausklappen"-Buttons an
                try:
                    expand_btn = main_page.locator(".expandall, a:has-text('Alles ausklappen'), button:has-text('Alles ausklappen')").first
                    if expand_btn.count() > 0:
                        expand_btn.click()
                        main_page.wait_for_timeout(800)
                except Exception:
                    pass

                # 3. Echten Kursnamen auslesen
                try:
                    raw_title = main_page.locator("h1").first.inner_text()
                except Exception:
                    raw_title = main_page.title().replace("Kurs:", "").strip()

                safe_course_title = clean_name(raw_title)
                print(f"\n[{index}/{len(course_urls)}] Synchronisiere Kurs: {safe_course_title}")

                # 4. Moodle-Abschnitte durchsuchen
                sections = main_page.locator(".course-content .section, li.section, section.course-section, div.course-section, [id^='section-']").all()
                if not sections:
                    sections = [main_page.locator("body")]

                processed_urls = set()

                for sec in sections:
                    sec_title = "Allgemein"
                    try:
                        header_el = sec.locator(".sectionname, .section-title, h2, h3, h4, .section-header").first
                        if header_el.count() > 0:
                            raw_sec = header_el.inner_text().strip()
                            if raw_sec:
                                sec_title = raw_sec
                    except Exception:
                        pass

                    safe_sec_title = clean_name(sec_title)
                    res_elements = sec.locator("a[href*='/mod/resource/view.php'], a[href*='pluginfile.php']").all()

                    for res_el in res_elements:
                        res_url = res_el.get_attribute("href")
                        if not res_url or res_url in processed_urls:
                            continue
                        
                        processed_urls.add(res_url)

                        # Blitz-Check: Bereits heruntergeladen?
                        if res_url in download_history and os.path.exists(download_history[res_url]):
                            existing_file = os.path.basename(download_history[res_url])
                            print(f"  [⚡ Übersprungen] Bereits vorhanden: {safe_sec_title} -> {existing_file}")
                            continue

                        res_page = context.new_page()
                        try:
                            # 1. Versuch: Direkter Browser-Download
                            try:
                                with res_page.expect_download(timeout=3000) as download_info:
                                    res_page.goto(res_url, wait_until="commit")
                                download = download_info.value
                                file_name = clean_name(download.suggested_filename)
                                
                                save_path = get_safe_path(DOWNLOAD_DIR, safe_course_title, safe_sec_title, file_name)
                                sec_dir = os.path.dirname(save_path)
                                
                                os.makedirs(sec_dir, exist_ok=True)
                                download.save_as(save_path)
                                print(f"  [+] NEU heruntergeladen: {safe_sec_title} -> {file_name}")
                                
                                download_history[res_url] = save_path
                                newly_downloaded[safe_course_title][safe_sec_title].append(file_name)
                                save_history()
                                res_page.close()
                                continue
                            except Exception:
                                pass

                            # 2. Versuch: Eingebettetes PDF (Pluginfile)
                            pdf_src = None
                            for selector in ["object[data*='pluginfile.php']", "embed[src*='pluginfile.php']", "iframe[src*='pluginfile.php']", "a[href*='pluginfile.php']"]:
                                elem = res_page.locator(selector).first
                                if elem.count() > 0:
                                    pdf_src = elem.get_attribute("data") or elem.get_attribute("src") or elem.get_attribute("href")
                                    if pdf_src:
                                        break

                            target_url = pdf_src if pdf_src else (res_page.url if "pluginfile.php" in res_page.url else None)

                            if target_url:
                                clean_url = re.sub(r'\?.*$', '', target_url)
                                file_name = os.path.basename(clean_url)
                                if not file_name or '.' not in file_name:
                                    file_name = "dokument.pdf"
                                
                                file_name = clean_name(file_name)
                                save_path = get_safe_path(DOWNLOAD_DIR, safe_course_title, safe_sec_title, file_name)
                                sec_dir = os.path.dirname(save_path)

                                response = context.request.get(target_url)
                                if response.ok:
                                    os.makedirs(sec_dir, exist_ok=True)
                                    with open(save_path, "wb") as f:
                                        f.write(response.body())
                                    print(f"  [+] NEU heruntergeladen (PDF): {safe_sec_title} -> {file_name}")
                                    
                                    download_history[res_url] = save_path
                                    newly_downloaded[safe_course_title][safe_sec_title].append(file_name)
                                    save_history()

                        except Exception:
                            pass
                        finally:
                            res_page.close()

            except Exception as e:
                print(f"Fehler bei Kurs {course_url}: {e}")

        print("\n" + "="*60)
        print("Synchronisation beendet!")
        print(f"Speicherort: {os.path.abspath(DOWNLOAD_DIR)}")
        print("="*60)
        browser.close()

    # Nach Abschluss: Strukturierte Push-Benachrichtigung senden
    send_push_notification(newly_downloaded)

if __name__ == "__main__":
    download_moodle_files()
