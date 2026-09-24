import requests
from bs4 import BeautifulSoup
import time
import re
import os
import json
import logging
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

# ==========================================
# 1. КОНСТАНТЫ И НАСТРОЙКИ
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

USERNAME = 'USERNAME'
PASSWORD = os.getenv('KUZELKA_PASSWORD') 
BOT_TOKEN = os.getenv('BOT_TOKEN') 

LOGIN_URL = 'https://planovac.kuzelka.sk/sign/login'
CALENDAR_URL = 'https://planovac.kuzelka.sk/ziak/kalendar'

USERS_FILE = os.path.join(BASE_DIR, 'users.json')
HISTORY_FILE = os.path.join(BASE_DIR, 'sent_slots.json')

REQUEST_TIMEOUT = 15  
SLOVAKIA_TZ = ZoneInfo("Europe/Bratislava")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# ==========================================
# 2. НАДЕЖНАЯ РАБОТА С СЕТЬЮ И ФАЙЛАМИ
# ==========================================

def safe_request(method, url, session=None, retries=3, **kwargs):
    kwargs.setdefault('timeout', REQUEST_TIMEOUT)
    for attempt in range(retries):
        try:
            req_obj = session if session else requests
            res = req_obj.request(method, url, **kwargs)
            res.raise_for_status()
            return res
        except requests.RequestException as e:
            logging.warning(f"Сбой сети [{url}] (Попытка {attempt + 1}/{retries}): {e}")
            time.sleep(2) 
    
    logging.error(f"❌ Не удалось достучаться до {url} после {retries} попыток.")
    return None

def load_data(filename, default_type=list):
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.error(f"Файл {filename} поврежден! Пытаюсь восстановить из бэкапа...")
            if os.path.exists(filename + ".bak"):
                shutil.copy(filename + ".bak", filename)
                return load_data(filename, default_type)
            return default_type()
    return default_type()

def save_data(filename, data):
    if os.path.exists(filename):
        shutil.copy(filename, filename + ".bak")
        
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# ==========================================
# 3. ФУНКЦИИ TELEGRAM
# ==========================================

def get_new_users():
    # Без цикла нам не нужно хранить offset в памяти, просто забираем последние сообщения
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    
    response = safe_request("GET", url)
    if not response: return

    data = response.json()
    if not data.get("ok"): return

    users = set(load_data(USERS_FILE))
    new_found = False

    for update in data.get("result", []):
        if "message" in update:
            chat_id = update["message"]["chat"]["id"]
            if chat_id not in users:
                users.add(chat_id)
                new_found = True
                logging.info(f"🆕 Новый пользователь: {chat_id}")

    if new_found:
        save_data(USERS_FILE, list(users))

def send_to_all(text):
    users = load_data(USERS_FILE)
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    
    for chat_id in users:
        safe_request("POST", url, json={'chat_id': chat_id, 'text': text})
        time.sleep(0.1) 

# ==========================================
# 4. ОСНОВНАЯ ЛОГИКА ПАРСЕРА
# ==========================================

def check_and_notify():
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36'})

    safe_request("GET", 'https://planovac.kuzelka.sk/', session=session)
    login_data = {'login': (None, USERNAME), 'password': (None, PASSWORD), 'prihlasit': (None, 'Prihlásiť')}
    post_res = safe_request("POST", LOGIN_URL, session=session, files=login_data)
    if not post_res: return

    time.sleep(2)
    session.headers.update({'Referer': post_res.url})
    response = safe_request("GET", CALENDAR_URL, session=session)
    if not response: return
    
    soup = BeautifulSoup(response.text, 'html.parser')
    current_found_slots = []
    
    for cell in soup.find_all('td', class_='kalendar-termin'):
        onclick = cell.get('onclick', '')
        if 'prihlasit' in onclick:
            match = re.findall(r"'(.*?)'", onclick)
            try:
                if len(match) >= 7 and match[2] == 'Bežná Jazda':
                    slot_time_str = match[6] 
                    instructor = match[3]
                    car = match[4]
                    
                    slot_id = f"{slot_time_str}_{instructor}"
                    slot_text = f"🗓 {slot_time_str[:-3]} | 👤 {instructor} | 🚘 {car}"
                    
                    slot_time = datetime.strptime(slot_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=SLOVAKIA_TZ)
                    current_found_slots.append((slot_id, slot_text, slot_time))
            except IndexError:
                continue
            except ValueError:
                continue

    history = load_data(HISTORY_FILE, dict)
    now = datetime.now(SLOVAKIA_TZ)
    
    updated_history = {}
    for s_id, s_time_str in history.items():
        try:
            s_time = datetime.strptime(s_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=SLOVAKIA_TZ)
            if s_time > now:
                updated_history[s_id] = s_time_str
        except ValueError:
            pass 
    
    new_notifications = []
    for s_id, s_text, s_time in current_found_slots:
        if s_id not in updated_history and s_time > now:
            new_notifications.append(s_text)
            updated_history[s_id] = s_time.strftime('%Y-%m-%d %H:%M:%S')

    save_data(HISTORY_FILE, updated_history)

    if new_notifications:
        msg = "🚗 НОВЫЕ ТЕРМИНЫ!\n\n" + "\n".join(new_notifications)
        msg += f"\n\nЗаписаться: {CALENDAR_URL}"
        send_to_all(msg)
        logging.info(f"Отправлено {len(new_notifications)} новых окон.")
    else:
        logging.info("Новых мест нет.")

# ==========================================
# 5. ОДИНОЧНЫЙ ЗАПУСК ДЛЯ GITHUB ACTIONS
# ==========================================
if __name__ == '__main__':
    logging.info("🚀 Запуск одиночной проверки (GitHub Actions).")
    
    # Защита: проверяем, задал ли ты секреты в настройках GitHub
    if not BOT_TOKEN or not PASSWORD:
        logging.critical("❌ ОШИБКА: Не найден BOT_TOKEN или KUZELKA_PASSWORD. Проверь раздел Secrets в GitHub!")
        exit(1)
        
    try:
        get_new_users()
        check_and_notify()
        logging.info("✅ Скрипт успешно завершен.")
    except Exception as e:
        logging.critical(f"💥 Критическое падение: {e}")
        exit(1)
