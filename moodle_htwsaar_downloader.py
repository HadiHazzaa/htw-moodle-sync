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
USERNAME = os.environ.get("HTW_USER")
PASSWORD = os.environ.get("HTW_PASS")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "htw_moodle_4a8b2c1d-9e8f-7a6b-5c4d-3e2f1a0b9c8d")

DOWNLOAD_DIR = "./moodle_downloads"
CACHE_FILE = "./download_history.json"
# ==========================================

if not USERNAME or not PASSWORD:
    print("[SECURITY ERROR] HTW_USER oder HTW_PASS fehlt in den Umgebungsvariablen!")
    sys.exit(1)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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

def fix_utf8_mojibake(text):
    """Repariert fehlerhafte UTF-8 Dekodierungen (z. B. Ãœ -> Ü, Ã¤ -> ä)"""
    if not text:
        return ""
    try:
        return text.encode('latin1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text

def clean_name(name):
    """Bereinigt Dateinamen, repariert Umlaute und dekodiert URL-Sonderzeichen"""
    if not name:
        return "Allgemein"
    
    name = fix_utf8_mojibake(name)
    decoded = urllib.parse.unquote(name)
    decoded = fix_utf8_mojibake(decoded)
    
    decoded = " ".join(decoded.split())
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", decoded)
    return cleaned.strip() or "Allgemein"

def get_safe_path(base_dir, *path_segments):
    """Schützt vor Path-Traversal-Angriffen (CWE-22)"""
    abs_base = os.path.abspath(base_dir)
    target_path = os.path.abspath(os.path.join(abs_base, *path_segments))
    
    if os.path.commonpath([abs_base, target_path]) != abs_base:
        raise PermissionError(f"[SECURITY ALERT] Path Traversal abgefangen: {target_path}")
        
    return target_path

def send_push_notification(grouped_downloads):
    """Push-Nachricht an ntfy.sh senden"""
    if not grouped_downloads or not NTFY_TOPIC:
        return

    total_files = sum(
        len(files) 
        for sections in grouped_downloads.values() 
        for files in sections.values()
    )
    
    title = f"🔔 {total_files} neue Moodle-Datei(en)!"
    message_lines = []

    for course_title, sections in grouped_downloads.items():
        message_lines.append(f"🏛️ {course_title}")
        message_lines.append("─" * 30)
        
        for sec_title, files in sections.items():
            message_lines.append(f"📂 {sec_title}")
            for filename in files:
                message_lines.append(f"  🔵 {filename}")
            message_lines.append("")
            
        message_lines.append("=" * 30)
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
    newly_downloaded = defaultdict(lambda: defaultdict(list))

    with sync_playwright() as p:
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
                main_page.goto(course_url, wait_until="domcontentloaded")
                main_page.wait_for_timeout(2000)

                # Aufklappen aller Moodle-Abschnitte erzwingen
                try:
                    main_page.evaluate("""() => {
                        document.querySelectorAll('.collapse, .course-section-header, .drawer').forEach(el => {
                            el.classList.add('show');
                            el.classList.remove('collapsed');
                        });
                        document.querySelectorAll('[aria-expanded="false"]').forEach(el => {
                            el.setAttribute('aria-expanded', 'true');
                        });
                    }""")
                except Exception:
                    pass

                try:
                    raw_title = main_page.locator("h1, .page-header-headings h1").first.inner_text()
                except Exception:
                    raw_title = main_page.title().replace("Kurs:", "").strip()

                safe_course_title = clean_name(raw_title)
                print(f"\n[{index}/{len(course_urls)}] Synchronisiere Kurs: {safe_course_title}")

                # Präzise Analyse: Wandert Elternknoten ab ODER nutzt Positionsabgleich im HTML-Baum
                material_items = main_page.evaluate("""() => {
                    const items = [];
                    const seenUrls = new Set();
                    const links = document.querySelectorAll('a[href*="/mod/resource/view.php"], a[href*="/mod/folder/view.php"], a[href*="pluginfile.php"]');

                    links.forEach(link => {
                        const href = link.getAttribute('href');
                        if (!href || seenUrls.has(href)) return;
                        seenUrls.add(href);

                        let secTitle = "Allgemein";

                        // 1. Hierarchie-Check: Elternknoten nach Moodle-Abschnitt absuchen
                        let curr = link.parentElement;
                        while (curr && curr !== document.body) {
                            if (curr.classList.contains('section') || 
                                curr.classList.contains('course-section') || 
                                (curr.id && curr.id.includes('section-')) || 
                                curr.tagName === 'SECTION') {
                                
                                const header = curr.querySelector('.sectionname, .section-title, .section-header, [data-for="section_title"], h2, h3, h4');
                                if (header) {
                                    let t = header.innerText || header.textContent || "";
                                    t = t.replace(/\\s+/g, ' ').strip ? t.replace(/\\s+/g, ' ').strip() : t.replace(/\\s+/g, ' ').trim();
                                    if (t) {
                                        secTitle = t;
                                        break;
                                    }
                                }
                            }
                            curr = curr.parentElement;
                        }

                        // 2. Fallback: Positioneller Dokumentenabgleich (Nächstes vorheriges Headline-Element)
                        if (secTitle === "Allgemein") {
                            const allHeaders = Array.from(document.querySelectorAll('.sectionname, .section-title, .section-header, [data-for="section_title"], h3[id*="section"], .course-section h2, .course-section h3'));
                            let bestHeader = null;
                            for (const h of allHeaders) {
                                if (h.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING) {
                                    bestHeader = h;
                                } else {
                                    break;
                                }
                            }
                            if (bestHeader) {
                                let t = bestHeader.innerText || bestHeader.textContent || "";
                                t = t.replace(/\\s+/g, ' ').trim();
                                if (t) secTitle = t;
                            }
                        }

                        items.push({ url: href, section: secTitle });
                    });

                    return items;
                }""")

                print(f"   🔍 Im Kurs gefundene Material-Links: {len(material_items)}")

                if len(material_items) == 0:
                    print("   ℹ️ Keine Dokumente in diesem Kurs gefunden.")
                    continue

                for item in material_items:
                    res_url = item["url"]
                    raw_sec_title = item["section"]
                    safe_sec_title = clean_name(raw_sec_title)

                    # Check: Bereits heruntergeladen?
                    if res_url in download_history and os.path.exists(download_history[res_url]):
                        existing_file = os.path.basename(download_history[res_url])
                        print(f"  [⚡ Übersprungen] {safe_sec_title} -> {existing_file}")
                        continue

                    # Herunterladen per HTTP-Request
                    try:
                        resp = context.request.get(res_url)
                        content_type = resp.headers.get("content-type", "").lower()

                        if "text/html" in content_type and "pluginfile.php" not in resp.url:
                            html_text = resp.text()
                            plugin_match = re.search(r'(https?://moodle\.htwsaar\.de/pluginfile\.php/[^\s"\'<>]+)', html_text)
                            if plugin_match:
                                target_url = plugin_match.group(1)
                                resp = context.request.get(target_url)
                            else:
                                print(f"  [ℹ️ Hinweis] Kein direkter Download-Link in HTML: {safe_sec_title}")
                                continue

                        if resp.ok:
                            content_disp = resp.headers.get("content-disposition", "")
                            filename = None
                            
                            match_utf8 = re.search(r"""filename\*=(?:UTF-8''|utf-8'')([^";]+)""", content_disp, re.IGNORECASE)
                            if match_utf8:
                                filename = urllib.parse.unquote(match_utf8.group(1))
                            else:
                                match = re.search(r'filename="?([^";]+)"?', content_disp, re.IGNORECASE)
                                if match:
                                    filename = match.group(1)

                            if not filename:
                                clean_url = re.sub(r'\?.*$', '', resp.url)
                                filename = os.path.basename(clean_url)

                            if not filename or filename == "view.php" or "." not in filename:
                                filename = "dokument.pdf"

                            file_name = clean_name(filename)
                            save_path = get_safe_path(DOWNLOAD_DIR, safe_course_title, safe_sec_title, file_name)
                            sec_dir = os.path.dirname(save_path)

                            os.makedirs(sec_dir, exist_ok=True)
                            with open(save_path, "wb") as f:
                                f.write(resp.body())

                            print(f"  [+] NEU heruntergeladen: {safe_sec_title} -> {file_name}")

                            download_history[res_url] = save_path
                            newly_downloaded[safe_course_title][safe_sec_title].append(file_name)
                            save_history()
                        else:
                            print(f"  [❌ HTTP {resp.status}] {res_url}")

                    except Exception as err:
                        print(f"  [❌ Fehler] {res_url}: {err}")

            except Exception as e:
                print(f"Fehler bei Kurs {course_url}: {e}")

        print("\n" + "="*60)
        print("Synchronisation beendet!")
        print(f"Speicherort: {os.path.abspath(DOWNLOAD_DIR)}")
        print("="*60)
        browser.close()

    send_push_notification(newly_downloaded)

if __name__ == "__main__":
    download_moodle_files()
