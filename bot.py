import time
import json
import os

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from webdriver_manager.chrome import ChromeDriverManager
from telegram import Bot


# ================= CONFIG =================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

URL = "https://eproc2.bihar.gov.in/EPSV2Web/openarea/tenderListingPage.do"
DATA_FILE = "tenders.json"


# ================= DRIVER =================

def get_driver():
    options = Options()

    # Keep browser visible
    # options.add_argument("--headless=new")

    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

    # Anti-detection
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', "
        "{get: () => undefined})"
    )

    return driver


# ================= SCROLL =================

def scroll_page(driver):
    for _ in range(8):
        driver.execute_script(
            "window.scrollTo(0, document.body.scrollHeight);"
        )
        time.sleep(2)


# ================= LOAD OLD DATA =================

def load_old():
    if not os.path.exists(DATA_FILE):
        return []

    with open(DATA_FILE, "r") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ================= SCRAPER =================

def fetch_tenders():
    driver = get_driver()
    tenders = []

    try:
        driver.get(URL)

        time.sleep(6)

        scroll_page(driver)

        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "table tbody tr")
            )
        )

        rows = driver.find_elements(
            By.CSS_SELECTOR,
            "table tbody tr"
        )

        print("Rows found:", len(rows))

        for row in rows:
            cols = row.find_elements(By.TAG_NAME, "td")

            if len(cols) < 6:
                continue

            tender_id = cols[1].text.strip()

            tenders.append({
                "id": tender_id,
                "department": cols[2].text.strip(),
                "title": cols[3].text.strip(),
                "publish_date": cols[4].text.strip(),
                "closing_date": cols[5].text.strip(),
                "link": (
                    "https://eproc2.bihar.gov.in/"
                    "EPSV2Web/openarea/"
                    f"tenderDetailPage?tid={tender_id}"
                )
            })

    except Exception as e:
        print("Error:", e)

    finally:
        driver.quit()

    return tenders


# ================= TELEGRAM =================

def send_to_telegram(tenders):

    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "environment variables are required."
        )

    bot = Bot(token=BOT_TOKEN)

    message = "📢 *New Bihar Tenders*\n\n"

    for i, tender in enumerate(tenders, 1):

        entry = (
            f"{i}. 🆔 {tender['id']}\n"
            f"🏢 {tender['department']}\n"
            f"📄 {tender['title']}\n"
            f"📅 {tender['publish_date']} | "
            f"⏳ {tender['closing_date']}\n"
            f"🔗 {tender['link']}\n\n"
        )

        # Keep Telegram messages within a safe size
        if len(message) + len(entry) > 3500:
            bot.send_message(
                chat_id=CHAT_ID,
                text=message,
                parse_mode="Markdown"
            )
            message = ""

        message += entry

    if message:
        bot.send_message(
            chat_id=CHAT_ID,
            text=message,
            parse_mode="Markdown"
        )


# ================= MAIN =================

def main():

    old = load_old()
    new = fetch_tenders()

    old_ids = {tender["id"] for tender in old}

    new_tenders = [
        tender
        for tender in new
        if tender["id"] not in old_ids
    ]

    if new_tenders:

        send_to_telegram(new_tenders)

        save_data(old + new_tenders)

        print("Sent:", len(new_tenders))

    else:

        print("No new tenders")


# ================= RUN =================

if __name__ == "__main__":
    main()