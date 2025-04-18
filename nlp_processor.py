from pythainlp import word_tokenize
import sqlite3
import json
import os
import random
from difflib import SequenceMatcher
import logging
from datetime import datetime
from collections import Counter

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_logs.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

CONFIG_FILE = 'config.json'

def load_config():
    default_config = {
        "intents": {
            "unknown": {
                "keywords": [],
                "responses": ["ขอโทษน้า ไม่แน่ใจว่าถามอะไร ลองถามใหม่ชัดๆ ได้มั้ยจ๊ะ? 😅"]
            }
        },
        "products": [],
        "response_weights": {}
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                logger.info("Loaded config.json successfully")
                if "intents" not in config:
                    config["intents"] = default_config["intents"]
                elif "unknown" not in config["intents"]:
                    config["intents"]["unknown"] = default_config["intents"]["unknown"]
                if "response_weights" not in config:
                    config["response_weights"] = {}
                return config
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding config.json: {e}")
            return default_config
        except Exception as e:
            logger.error(f"Unexpected error loading config.json: {e}")
            return default_config
    logger.warning("config.json not found, using default config")
    return default_config

def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        logger.info("Saved config.json successfully")
    except Exception as e:
        logger.error(f"Error saving config.json: {e}")

def initialize_database():
    conn = sqlite3.connect('chat_history.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        message TEXT,
        response TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        intent TEXT,
        product_id TEXT,
        slip_amount REAL,
        transaction_id TEXT
    )''')
    cursor.execute("PRAGMA table_info(chat_history)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'intent' not in columns:
        cursor.execute('ALTER TABLE chat_history ADD COLUMN intent TEXT')
        logger.info("Added intent column to chat_history")
    if 'product_id' not in columns:
        cursor.execute('ALTER TABLE chat_history ADD COLUMN product_id TEXT')
        logger.info("Added product_id column to chat_history")
    if 'slip_amount' not in columns:
        cursor.execute('ALTER TABLE chat_history ADD COLUMN slip_amount REAL')
        logger.info("Added slip_amount column to chat_history")
    if 'transaction_id' not in columns:
        cursor.execute('ALTER TABLE chat_history ADD COLUMN transaction_id TEXT')
        logger.info("Added transaction_id column to chat_history")
    conn.commit()
    conn.close()

def save_chat_message(user_id, message, response, intent=None, product_id=None, slip_amount=None, transaction_id=None):
    try:
        conn = sqlite3.connect('chat_history.db')
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO chat_history (user_id, message, response, intent, product_id, slip_amount, transaction_id) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (user_id, message, response, intent, product_id, slip_amount, transaction_id)
        )
        conn.commit()
        logger.info(f"Saved chat message for user {user_id}")
    except Exception as e:
        logger.error(f"Error saving chat message: {e}")
    finally:
        conn.close()

def get_recent_context(user_id, limit=5):
    try:
        conn = sqlite3.connect('chat_history.db')
        cursor = conn.cursor()
        cursor.execute(
            'SELECT message, intent, product_id, response, slip_amount, transaction_id FROM chat_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?',
            (user_id, limit)
        )
        context = cursor.fetchall()
        return context
    except Exception as e:
        logger.error(f"Error getting recent context: {e}")
        return []
    finally:
        conn.close()

def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def merge_tokens(tokens):
    merged = []
    i = 0
    while i < len(tokens):
        if i + 1 < len(tokens) and len(tokens[i]) > 1 and len(tokens[i + 1]) > 1:
            merged.append(tokens[i] + tokens[i + 1])
            i += 2
        else:
            merged.append(tokens[i])
            i += 1
    return merged

def find_best_product(tokens, products):
    best_product = None
    best_score = 0
    for product in products:
        score = 0
        for token in tokens:
            for keyword in product["keywords"]:
                sim_score = similarity(token, keyword)
                if sim_score > 0.5:
                    score += sim_score * 2
            if product["name"].lower() in token.lower():
                score += 5
        if score > best_score:
            best_score = score
            best_product = product
    return best_product

def create_new_intent(config, tokens, message, response):
    filtered_tokens = [t for t in tokens if len(t) > 2 and t not in ["ครับ", "ค่ะ", "นะ", "จ้า", "มั้ย", "อะ", "ๆ", "ฯ"]]
    if len(filtered_tokens) < 2:
        return "unknown"
    for intent, data in config["intents"].items():
        if intent == "unknown":
            continue
        common_keywords = set(filtered_tokens) & set(data.get("keywords", []))
        if len(common_keywords) >= len(filtered_tokens) * 0.7:
            return intent
    intent_name = f"intent_{int(datetime.now().timestamp())}"
    intent_data = {
        "keywords": filtered_tokens[:6],
        "responses": [response] if response else ["ได้รับข้อความแล้วจ้า เดี๋ยวจัดให้! 😊"]
    }
    config["intents"][intent_name] = intent_data
    logger.info(f"Created new intent '{intent_name}' with keywords: {filtered_tokens}")
    save_config(config)
    return intent_name

def update_intent_keywords_and_responses(config, intent, tokens, response, context):
    if intent == "unknown":
        return
    intent_data = config["intents"].get(intent, {"keywords": [], "responses": []})
    filtered_tokens = [t for t in tokens if len(t) > 2 and t not in ["ครับ", "ค่ะ", "นะ", "จ้า", "มั้ย", "อะ", "ๆ", "ฯ"]]
    for token in filtered_tokens:
        if token not in intent_data["keywords"]:
            intent_data["keywords"].append(token)
            logger.info(f"Added keyword '{token}' to intent '{intent}'")
    if response and response not in intent_data["responses"]:
        intent_data["responses"].append(response)
        logger.info(f"Added response '{response}' to intent '{intent}'")
    for ctx in context:
        ctx_response = ctx[3]
        if ctx_response and ctx_response not in intent_data["responses"]:
            intent_data["responses"].append(ctx_response)
            logger.info(f"Learned response '{ctx_response}' from context for intent '{intent}'")
    config["intents"][intent] = intent_data
    save_config(config)

def weighted_random_choice(responses, weights):
    if not weights:
        return random.choice(responses)
    total = sum(weights.values())
    r = random.uniform(0, total)
    upto = 0
    for i, response in enumerate(responses):
        w = weights.get(response, 1)
        if upto + w >= r:
            return response
        upto += w
    return random.choice(responses)

def generate_response(intent, product, context, text, config):
    base_responses = {
        "service_inquiry": [
            "สนใจฟาร์มอะไรจ้า? เรามีทั้ง Anime Vanguard และ Arise Crossover น้า 😎 AFK เริ่มแค่ 15 บาท/วัน!"
        ],
        "price_inquiry": [
            "ราคาของ {product_name} อยู่ที่ {product_price} บาทจ้า! 😄 คุ้มสุดๆ สนใจสั่งเลยมั้ย?"
        ],
        "account_issue": [
            "รหัสไม่ออนเหรอจ๊ะ? 😅 ลองเช็ก DM เดี๋ยวแอดมินช่วยดูให้ หรือแจ้งปัญหามาเลยน้า!"
        ],
        "order_request": [
            "เจ๋งเลย! อยากฟาร์ม {product_name} ใช่มั้ยจ้า? 😄 บอกจำนวนวันหรือตัวละครมา เดี๋ยวจัดให้!"
        ],
        "greeting": [
            "หวัดดีจ้า! 😎 ร้านฟาร์ม Roblox พร้อมลุยทั้ง Anime Vanguard และ Arise Crossover สนใจอะไรบอกมาเลย!"
        ],
        "unknown": [
            "เอ๊ะ อะไรน้า? ลองบอกใหม่ชัดๆ สิคะ เดี๋ยวแอดมินร้านฟาร์มจัดให้! 😅"
        ]
    }
    
    intent_responses = config["intents"].get(intent, {}).get("responses", base_responses.get(intent, base_responses["unknown"]))
    if not intent_responses:
        intent_responses = base_responses["unknown"]
    
    response_weights = config.get("response_weights", {}).get(intent, {})
    for resp in intent_responses:
        if resp not in response_weights:
            response_weights[resp] = 1
    selected_response = weighted_random_choice(intent_responses, response_weights)
    response_weights[selected_response] = max(0.5, response_weights.get(selected_response, 1) * 0.8)
    config.setdefault("response_weights", {}).setdefault(intent, {}).update(response_weights)
    
    response = selected_response
    if product:
        response = response.format(
            product_name=product["name"],
            product_price=product["price"],
            product_description=product["description"]
        )
    else:
        response = response.replace("{product_name}", "บริการฟาร์มเจ๋งๆ")
        response = response.replace("{product_price}", "ราคาดีๆ")
        response = response.replace("{product_description}", "ฟาร์มสุดโหด")
    
    save_config(config)
    return response, intent

async def process_text(text, user_id='web_user'):
    initialize_database()
    config = load_config()
    intents = config.get("intents", {})
    products = config.get("products", [])

    tokens = word_tokenize(text, engine='newmm')
    filtered_tokens = [t for t in tokens if len(t) > 1 and t not in [" ", "ๆ", "ฯ"]]
    merged_tokens = merge_tokens(filtered_tokens)
    logger.info(f"Tokenized input: {merged_tokens}")

    context = get_recent_context(user_id)
    previous_intent = context[0][1] if context else None
    previous_product_id = context[0][2] if context else None

    matched_product = find_best_product(merged_tokens, products)
    if not matched_product and previous_product_id:
        matched_product = next((p for p in products if p["id"] == previous_product_id), None)

    best_intent = "unknown"
    best_score = 0
    context_weight = 0.3

    for intent, data in intents.items():
        if intent == "unknown":
            continue
        score = 0
        keywords = data.get("keywords", [])
        for token in merged_tokens:
            for keyword in keywords:
                sim_score = similarity(token, keyword)
                if sim_score > 0.5:
                    score += sim_score * (1 + len(keyword) / 10)
        if intent == previous_intent and context:
            score += context_weight * score
        if score > best_score:
            best_score = score
            best_intent = intent

    response, final_intent = generate_response(best_intent, matched_product, context, text, config)
    
    if final_intent == "unknown" and best_score < 0.8:
        final_intent = create_new_intent(config, merged_tokens, text, response)
    
    update_intent_keywords_and_responses(config, final_intent, merged_tokens, response, context)

    save_chat_message(
        user_id, 
        text, 
        response, 
        final_intent,
        matched_product["id"] if matched_product else None
    )

    return {
        "response": response,
        "tokens": merged_tokens,
        "intent": final_intent,
        "product_id": matched_product["id"] if matched_product else None
    }