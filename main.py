import imaplib
import email
import re
import sqlite3
import time
import logging
from email.header import decode_header
from config import EMAIL_PASS, EMAIL_USER

# --- НАСТРОЙКИ ---
IMAP_SERVER = "imap.gmail.com"
SENDER_FILTER = "robertkhairullin13@gmail.com"  # От кого ждем письма
CHECK_INTERVAL = 60  # Интервал проверки (в секундах)

# Настройка логирования (ошибки и успехи будут в файле worker.log)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("worker.log"), logging.StreamHandler()]
)


def setup_db():
    conn = sqlite3.connect('bookings.db')
    cursor = conn.cursor()
    # Таблица для данных
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_ref TEXT,
            product_booking_ref TEXT,
            ext_booking_ref TEXT UNIQUE,
            rate_type INTEGER,
            raw_rate_text TEXT,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Таблица для истории обработанных писем (чтобы не парсить дважды)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS processed_emails (
            message_id TEXT PRIMARY KEY
        )
    ''')
    conn.commit()
    return conn


def parse_rate(rate_string):
    """Преобразует строку rate в код (0, 1, 2)"""
    s = rate_string.lower()
    if '7 days' in s or 'week' in s:
        return 0
    if 'month' in s:
        return 1
    if 'year' in s:
        return 2
    return None


def extract_data(text):
    try:
        # Убираем лишние пробелы и странные символы
        text = " ".join(text.split())

        data = {}
        # Ищем VIA (стал искать просто по паттерну, игнорируя точные слова вокруг)
        res = re.search(r'VIA-[\w\d]+', text)
        data['booking_ref'] = res.group(0) if res else None

        # Ищем HER
        res = re.search(r'HER-[\w\d]+', text)
        data['product_booking_ref'] = res.group(0) if res else None

        # Ищем Ext. booking ref (ищем число, которое идет после этого текста)
        res = re.search(r'Ext\.\s*booking\s*ref\s*(\d+)', text, re.IGNORECASE)
        data['ext_booking_ref'] = res.group(1) if res else None

        # Ищем Rate
        res = re.search(r'Rate\s*(.*?)\s*(?:PAX|Created|Notes|$)', text, re.IGNORECASE)
        if res:
            rate_text = res.group(1).strip()
            data['raw_rate_text'] = rate_text
            data['rate_type'] = parse_rate(rate_text)
        else:
            data['rate_type'] = None

        return data
    except Exception as e:
        logging.error(f"Ошибка парсинга: {e}")
        return None




def process_emails():
    db = setup_db()
    cursor = db.cursor()

    try:
        # Подключение к почте
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")

        # Поиск непрочитанных писем от отправителя
        status, messages = mail.search(None, f'(UNSEEN FROM "{SENDER_FILTER}")')

        if status != 'OK':
            return

        for num in messages[0].split():
            # Получаем структуру письма
            res, msg_data = mail.fetch(num, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    msg_id = msg.get('Message-ID')

                    # 1. Проверка на дубликат письма
                    cursor.execute("SELECT 1 FROM processed_emails WHERE message_id=?", (msg_id,))
                    if cursor.fetchone():
                        continue

                    # Получаем тело письма
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode()
                    else:
                        body = msg.get_payload(decode=True).decode()

                    # 2. Парсинг
                    extracted = extract_data(body)

                    if extracted and extracted.get('ext_booking_ref'):
                        try:
                            # 3. Сохранение в базу
                            cursor.execute('''
                                INSERT INTO bookings (booking_ref, product_booking_ref, ext_booking_ref, rate_type, raw_rate_text)
                                VALUES (?, ?, ?, ?, ?)
                            ''', (
                                extracted['booking_ref'],
                                extracted['product_booking_ref'],
                                extracted['ext_booking_ref'],
                                extracted['rate_type'],
                                extracted['raw_rate_text']
                            ))

                            # Помечаем письмо как обработанное в БД
                            cursor.execute("INSERT INTO processed_emails VALUES (?)", (msg_id,))
                            db.commit()
                            logging.info(f"Успешно сохранено: {extracted['ext_booking_ref']}")

                        except sqlite3.IntegrityError:
                            logging.warning(f"Дубликат данных в БД: {extracted['ext_booking_ref']}")
                            cursor.execute("INSERT INTO processed_emails VALUES (?)", (msg_id,))
                            db.commit()
                    else:
                        logging.error(f"Не удалось извлечь данные из письма {msg_id}")

            # Помечаем как прочитанное в самом почтовом сервисе
            mail.store(num, '+FLAGS', '\\Seen')

        mail.close()
        mail.logout()

    except Exception as e:
        logging.error(f"Критическая ошибка воркера: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    logging.info("Воркер запущен...")
    while True:
        process_emails()
        time.sleep(CHECK_INTERVAL)