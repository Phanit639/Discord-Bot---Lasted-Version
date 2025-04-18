import sqlite3
import json
import logging
from pythainlp import word_tokenize
from datetime import datetime
from collections import Counter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def load_chat_history():
    conn = sqlite3.connect('chat_history.db')
    cursor = conn.cursor()
    cursor.execute('SELECT message, intent, response FROM chat_history WHERE intent IS NOT NULL')
    data = cursor.fetchall()
    conn.close()
    return [{"message": msg, "intent": intent, "response": resp} for msg, intent, resp in data]

def update_config_with_new_keywords(config_file='config.json'):
    chat_data = load_chat_history()
    if not chat_data:
        logger.warning("No chat history found")
        return

    with open(config_file, 'r', encoding='utf-8') as f:
        config = json.load(f)

    intent_keywords = {intent: set(data.get("keywords", [])) for intent, data in config.get("intents", {}).items()}
    intent_responses = {intent: set(data.get("responses", [])) for intent, data in config.get("intents", {}).items()}
    response_weights = config.get("response_weights", {})

    for chat in chat_data:
        message = chat["message"]
        intent = chat["intent"]
        response = chat["response"]
        tokens = word_tokenize(message, engine='newmm')
        filtered_tokens = [t for t in tokens if len(t) > 2 and t not in ["ครับ", "ค่ะ", "นะ", "จ้า", "มั้ย", "อะ", "ๆ", "ฯ"]]
        merged_tokens = []
        i = 0
        while i < len(filtered_tokens):
            if i + 1 < len(filtered_tokens) and len(filtered_tokens[i]) > 1:
                merged_tokens.append(filtered_tokens[i] + filtered_tokens[i + 1])
                i += 2
            else:
                merged_tokens.append(filtered_tokens[i])
                i += 1
        
        if intent == "unknown" and len(filtered_tokens) >= 2:
            # ตรวจสอบความซ้ำก่อนสร้าง intent ใหม่
            new_intent = None
            for existing_intent, keywords in intent_keywords.items():
                if existing_intent == "unknown":
                    continue
                common_keywords = set(filtered_tokens) & keywords
                if len(common_keywords) >= len(filtered_tokens) * 0.7:
                    new_intent = existing_intent
                    break
            if not new_intent:
                new_intent = f"intent_{int(datetime.now().timestamp())}"
                config["intents"][new_intent] = {
                    "keywords": merged_tokens[:6],
                    "responses": [response] if response else ["ได้รับข้อความแล้วจ้า เดี๋ยวจัดให้! 😊"]
                }
                intent_keywords[new_intent] = set(merged_tokens[:6])
                intent_responses[new_intent] = {response} if response else {"ได้รับข้อความแล้วจ้า เดี๋ยวจัดให้! 😊"}
                response_weights[new_intent] = {response: 1} if response else {"ได้รับข้อความแล้วจ้า เดี๋ยวจัดให้! 😊": 1}
                logger.info(f"Created new intent '{new_intent}' from chat history")
            else:
                intent = new_intent
        else:
            for token in merged_tokens:
                if token not in intent_keywords.get(intent, set()):
                    intent_keywords.setdefault(intent, set()).add(token)
                    logger.info(f"Added keyword '{token}' to intent '{intent}'")
            if response and response not in intent_responses.get(intent, set()):
                intent_responses.setdefault(intent, set()).add(response)
                response_weights.setdefault(intent, {})[response] = 1
                logger.info(f"Added response '{response}' to intent '{intent}'")

    for intent, keywords in intent_keywords.items():
        if intent in config["intents"]:
            config["intents"][intent]["keywords"] = list(keywords)
    for intent, responses in intent_responses.items():
        if intent in config["intents"]:
            config["intents"][intent]["responses"] = list(responses)
    config["response_weights"] = response_weights

    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    logger.info("Updated config.json with new keywords and responses")

if __name__ == "__main__":
    update_config_with_new_keywords()