import cv2
import numpy as np
import pytesseract
import re
import aiohttp
import logging
import sqlite3
import json
from datetime import datetime
from collections import defaultdict
from pyzbar import pyzbar
from PIL import Image
import io
import exifread

pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_logs.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# รายชื่อธนาคารที่รองรับและสีสำหรับตรวจจับโลโก้
BANK_PATTERNS = {
    'SCB': {
        'names': ['Siam Commercial Bank', 'ธนาคารไทยพาณิชย์', 'SCB'],
        'hsv_lower': np.array([110, 40, 40]),  # สีม่วง
        'hsv_upper': np.array([160, 255, 255])
    },
    'KBank': {
        'names': ['Kasikornbank', 'ธนาคารกสิกรไทย', 'KBank', 'K-Bank'],
        'hsv_lower': np.array([80, 40, 40]),  # สีเขียว
        'hsv_upper': np.array([140, 255, 255])
    },
    'BBL': {
        'names': ['Bangkok Bank', 'ธนาคารกรุงเทพ', 'BBL'],
        'hsv_lower': np.array([100, 40, 40]),  # สีน้ำเงิน
        'hsv_upper': np.array([140, 255, 255])
    },
    'KTB': {
        'names': ['Krungthai Bank', 'ธนาคารกรุงไทย', 'KTB'],
        'hsv_lower': np.array([90, 40, 40]),  # สีน้ำเงิน
        'hsv_upper': np.array([130, 255, 255])
    },
    'TrueWallet': {
        'names': ['TrueMoney Wallet', 'True Wallet', 'ทรูมันนี่'],
        'hsv_lower': np.array([10, 40, 40]),  # สีส้ม
        'hsv_upper': np.array([30, 255, 255])
    }
}

def initialize_database():
    """เริ่มต้นฐานข้อมูลสำหรับเก็บประวัติสลิป"""
    try:
        conn = sqlite3.connect('chat_history.db')
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS slip_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            slip_url TEXT,
            amount REAL,
            bank_type TEXT,
            is_valid BOOLEAN,
            transaction_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        cursor.execute("PRAGMA table_info(slip_history)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'transaction_id' not in columns:
            cursor.execute('ALTER TABLE slip_history ADD COLUMN transaction_id TEXT')
            logger.info("Added transaction_id column to slip_history")
        conn.commit()
    except Exception as e:
        logger.error(f"Error initializing slip_history database: {e}")
    finally:
        conn.close()

def save_slip_data(user_id, slip_url, amount, bank_type, is_valid, response, transaction_id=None):
    """บันทึกข้อมูลสลิปลงฐานข้อมูล"""
    try:
        conn = sqlite3.connect('chat_history.db')
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO chat_history (user_id, message, response, intent, slip_amount, transaction_id, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (user_id, f"ส่งสลิป: {slip_url}", response, 'slip_verification', amount, transaction_id, datetime.now())
        )
        cursor.execute(
            'INSERT INTO slip_history (user_id, slip_url, amount, bank_type, is_valid, transaction_id) VALUES (?, ?, ?, ?, ?, ?)',
            (user_id, slip_url, amount, bank_type, is_valid, transaction_id)
        )
        conn.commit()
        logger.info(f"Saved slip data for user {user_id}: {amount} from {bank_type}, valid={is_valid}, transaction_id={transaction_id}")
    except Exception as e:
        logger.error(f"Error saving slip data: {e}")
    finally:
        conn.close()

def check_image_metadata(image_data):
    """ตรวจสอบ metadata ของภาพเพื่อหาสัญญาณการแก้ไข"""
    try:
        img = Image.open(io.BytesIO(image_data))
        exif_data = exifread.process_file(io.BytesIO(image_data), details=False)
        if not exif_data:
            logger.warning("No EXIF data found, possible edited image")
            return False
        # ตรวจสอบซอฟต์แวร์ที่ใช้สร้างภาพ
        software = exif_data.get('Image Software', '').lower()
        if any(x in software for x in ['photoshop', 'gimp', 'paint']):
            logger.warning(f"Suspicious software detected in metadata: {software}")
            return False
        # ตรวจสอบวันที่แก้ไข
        modify_date = exif_data.get('Image DateTime', '')
        if modify_date:
            try:
                modify_time = datetime.strptime(str(modify_date), '%Y:%m:%d %H:%M:%S')
                if modify_time > datetime.now() - datetime.timedelta(minutes=30):
                    logger.info("Recent modification date, likely legitimate")
                else:
                    logger.warning("Old modification date, possible reuse")
            except ValueError:
                logger.warning("Invalid modification date format")
        return True
    except Exception as e:
        logger.error(f"Error checking image metadata: {str(e)}")
        return False

def validate_amount(amount, config):
    """เปรียบเทียบยอดเงินในสลิปกับราคาบริการใน config"""
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        valid_amounts = [product['price'] for product in config_data.get('products', [])]
        # เพิ่มความยืดหยุ่น ±0.01 เพื่อจัดการข้อผิดพลาด OCR
        for valid_amount in valid_amounts:
            if abs(amount - valid_amount) < 0.01:
                logger.info(f"Amount {amount} matches service price {valid_amount}")
                return True
        logger.warning(f"Amount {amount} does not match any service price")
        return False
    except Exception as e:
        logger.error(f"Error validating amount: {str(e)}")
        return False

async def process_slip_image(image_url, user_id):
    """ประมวลผลภาพสลิปเพื่อดึงยอดเงินและตรวจสอบความถูกต้อง"""
    initialize_database()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as response:
                if response.status != 200:
                    logger.error(f"Failed to download image: {response.status}")
                    return None, None, False, "ไม่สามารถดาวน์โหลดภาพสลิปได้ค่ะ 😅"
                image_data = await response.read()

        nparr = np.frombuffer(image_data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return None, None, False, "ไม่สามารถอ่านภาพสลิปได้ค่ะ 😅"

        # ตรวจสอบ metadata
        metadata_valid = check_image_metadata(image_data)
        validation_messages = []
        if not metadata_valid:
            validation_messages.append("metadata ของภาพน่าสงสัย อาจถูกแก้ไข")

        qr_codes = pyzbar.decode(img)
        amount = None
        date = None
        ref_id = None
        bank_type = 'Unknown'
        is_valid = True
        qr_found = False
        transaction_id = None

        if qr_codes:
            qr_found = True
            for qr in qr_codes:
                amount, date, ref_id, bank_type, transaction_id = extract_data_from_qr_code(qr.data)
                logger.info(f"QR data: amount={amount}, date={date}, ref_id={ref_id}, bank_type={bank_type}, transaction_id={transaction_id}")
                if amount and ref_id:
                    break

        if not (amount and ref_id):
            logger.info("QR Code not found or incomplete, falling back to OCR")
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            gray = cv2.medianBlur(gray, 3)
            thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
            thresh = cv2.dilate(thresh, np.ones((3, 3), np.uint8), iterations=1)
            thresh = cv2.resize(thresh, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

            text = pytesseract.image_to_string(thresh, lang='tha+eng', config='--psm 6')
            logger.info(f"OCR output: {text}")

            if not amount:
                patterns = [
                    r'(\d{1,3}(?:,\d{3})*(?:\.\d{2}))',  # 1,234.56
                    r'(\d+\.\d{2})',  # 1234.56
                    r'จำนวนเงิน\s*(\d+[,.]\d{2})',  # จำนวนเงิน 1234.56
                    r'ยอดโอน\s*(\d+[,.]\d{2})',  # ยอดโอน 1234.56
                    r'(\d{1,3}(?:\s\d{3})*(?:\.\d{2}))'  # 1 234.56 (TrueMoney)
                ]
                for pattern in patterns:
                    match = re.search(pattern, text)
                    if match:
                        amount_str = match.group(1).replace(',', '').replace(' ', '')
                        try:
                            amount = float(amount_str)
                            break
                        except ValueError:
                            continue

            if bank_type == 'Unknown':
                for bank, patterns in BANK_PATTERNS.items():
                    for pattern in patterns['names']:
                        if pattern.lower() in text.lower():
                            bank_type = bank
                            break
                    if bank_type != 'Unknown':
                        break

            if not date:
                date_patterns = [
                    r'(\d{1,2}\s*[ก-ฮ]+\s*\d{2,4}(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?)',  # 12 ม.ค. 2567
                    r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',  # 12/01/2567
                    r'(\d{1,2}/\d{1,2}/\d{2})',  # 12/01/67
                    r'(\w{3}\s+\d{1,2},\s+\d{4})',  # Jan 12, 2024
                    r'(\d{1,2}\s+\w{3}\s+\d{4})',  # 12 Jan 2024 (TrueMoney)
                    r'(\d{6,8})'  # 25670112
                ]
                for pattern in date_patterns:
                    date_match = re.search(pattern, text, re.IGNORECASE)
                    if date_match:
                        date = date_match.group(0)
                        logger.info(f"Extracted date from OCR: {date}")
                        break
                else:
                    is_valid = False
                    validation_messages.append("ไม่พบวันที่ในสลิป")

            if not ref_id:
                ref_patterns = [
                    r'(Ref\.?|เลขที่อ้างอิง|Reference|เลขที่คำสั่งซื้อ|รหัส(?:อ้างอิง)?)\s*[:\s]*([A-Za-z0-9\-]+)',
                    r'(\d{10,16}|[A-Za-z0-9]{6,20})'  # รหัสสั้นสำหรับ TrueMoney
                ]
                for pattern in ref_patterns:
                    ref_match = re.search(pattern, text, re.IGNORECASE)
                    if ref_match:
                        ref_id = ref_match.group(2) if len(ref_match.groups()) > 1 else ref_match.group(1)
                        logger.info(f"Extracted ref_id from OCR: {ref_id}")
                        break
                else:
                    is_valid = False
                    validation_messages.append("ไม่พบเลขที่อ้างอิง")

        # ตรวจสอบ font consistency
        if not qr_found:
            font_consistency = check_font_consistency(img)
            if not font_consistency:
                is_valid = False
                validation_messages.append("ตัวอักษรในสลิปมีความผิดปกติ อาจมีการแก้ไข")

        # ตรวจสอบโลโก้ธนาคาร
        logo_detected = detect_bank_logo(img, bank_type)
        if not logo_detected and bank_type != 'Unknown':
            is_valid = False
            validation_messages.append(f"ไม่พบโลโก้ของ {bank_type}")

        # ตรวจสอบยอดเงินกับ config
        if amount and not validate_amount(amount, config):
            is_valid = False
            validation_messages.append("ยอดเงินไม่ตรงกับราคาบริการใดๆ")

        if amount and ref_id and not validation_messages:
            is_valid = True
            validation_messages = []

        if amount and is_valid:
            response = f"ได้รับยอดโอน {amount:,.2f} บาทจาก {bank_type} เรียบร้อยค่ะ 😊"
        elif amount:
            response = f"พบยอดโอน {amount:,.2f} บาท แต่สลิปอาจไม่ถูกต้อง: {', '.join(validation_messages)} กรุณาตรวจสอบค่ะ 😅"
        else:
            response = f"ไม่สามารถอ่านยอดเงินจากสลิปได้: {', '.join(validation_messages)} กรุณาส่งสลิปที่ชัดเจนกว่านี้ค่ะ 😅"

        save_slip_data(user_id, image_url, amount, bank_type, is_valid, response, transaction_id)
        return amount, bank_type, is_valid, response

    except Exception as e:
        logger.error(f"Error processing slip: {str(e)}")
        return None, None, False, f"เกิดข้อผิดพลาดในการอ่านสลิป: {str(e)} 😓"

def check_font_consistency(img):
    """ตรวจสอบความสม่ำเสมอของตัวอักษรในภาพ"""
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # ใช้ Sobel edge detection เพื่อจับขอบตัวอักษร
        sobelx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=5)
        sobely = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=5)
        edges = cv2.magnitude(sobelx, sobely)
        edges = cv2.convertScaleAbs(edges)
        _, binary = cv2.threshold(edges, 50, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APROX_SIMPLE)
        if len(contours) < 5:
            logger.warning("Too few contours detected")
            return False
        heights = [cv2.boundingRect(c)[3] for c in contours]
        mean_height = np.mean(heights)
        std_height = np.std(heights)
        if std_height / mean_height > 0.5:
            logger.warning("Inconsistent font sizes detected")
            return False
        return True
    except Exception as e:
        logger.error(f"Error checking font consistency: {str(e)}")
        return False

def detect_bank_logo(img, bank_type):
    """ตรวจสอบโลโก้ธนาคารจากสีและลักษณะ"""
    try:
        if bank_type == 'Unknown':
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        bank = BANK_PATTERNS.get(bank_type)
        if not bank:
            return False
        mask = cv2.inRange(hsv, bank['hsv_lower'], bank['hsv_upper'])
        pixel_count = np.sum(mask)
        if pixel_count > 10000:  # ปรับ threshold ตามขนาดภาพ
            logger.info(f"Detected logo for {bank_type}")
            return True
        logger.warning(f"No logo detected for {bank_type}")
        return False
    except Exception as e:
        logger.error(f"Error detecting bank logo: {str(e)}")
        return False

def extract_data_from_qr_code(qr_data):
    """ดึงข้อมูลจาก QR Code"""
    try:
        qr_data = qr_data.decode('utf-8')
        logger.info(f"QR Code raw data: {qr_data}")

        amount = None
        date = None
        ref_id = None
        bank_type = 'Unknown'
        transaction_id = None

        amount_pattern = r'amount=(\d+\.\d{2})|(\d+\.\d{2})|ยอดโอน\s*(\d+\.\d{2})'
        amount_match = re.search(amount_pattern, qr_data)
        if amount_match:
            amount = float(amount_match.group(1) or amount_match.group(2) or amount_match.group(3))

        date_pattern = r'date=(\d{4}-\d{2}-\d{2}(?:\s*\d{2}:\d{2}(?::\d{2})?)?)|(\d{1,2}\s*[ก-ฮ]+\s*\d{2,4}(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?)|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}/\d{1,2}/\d{2}|\w{3}\s+\d{1,2},\s+\d{4}|(\d{1,2}\s+\w{3}\s+\d{4})'
        date_match = re.search(date_pattern, qr_data, re.IGNORECASE)
        if date_match:
            date = date_match.group(0)
            logger.info(f"Extracted date from QR: {date}")

        ref_pattern = r'ref=([A-Za-z0-9\-]+)|transaction_id=([A-Za-z0-9\-]+)|([A-Za-z0-9]{6,20})'
        ref_match = re.search(ref_pattern, qr_data)
        if ref_match:
            ref_id = ref_match.group(1) or ref_match.group(2) or ref_match.group(3)
            logger.info(f"Extracted ref_id from QR: {ref_id}")

        trans_pattern = r'transactionId=([A-Za-z0-9\-]+)'
        trans_match = re.search(trans_pattern, qr_data)
        if trans_match:
            transaction_id = trans_match.group(1)
            logger.info(f"Extracted transaction_id from QR: {transaction_id}")

        for bank, patterns in BANK_PATTERNS.items():
            for pattern in patterns['names']:
                if pattern.lower() in qr_data.lower():
                    bank_type = bank
                    break
            if bank_type != 'Unknown':
                break

        return amount, date, ref_id, bank_type, transaction_id
    except Exception as e:
        logger.error(f"Error extracting QR data: {str(e)}")
        return None, None, None, 'Unknown', None