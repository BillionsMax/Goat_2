import hmac
import time
import hashlib
import requests
import json
import logging
from urllib.parse import urlencode
from cred import KEY, SECRET

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# URL для тестовой и боевой среды
TESTNET_URL = 'https://testnet.binancefuture.com'
PROD_URL = 'https://fapi.binance.com'
BASE_URL = PROD_URL  # Замените на TESTNET_URL для тестовой среды

''' ====== Функции для подписи и отправки запросов ====== '''


def hashing(query_string):
    return hmac.new(SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()


def get_timestamp():
    return int(time.time() * 1000)


def dispatch_request(http_method):
    session = requests.Session()
    session.headers.update({
        'Content-Type': 'application/json;charset=utf-8',
        'X-MBX-APIKEY': KEY
    })
    return {
        'GET': session.get,
        'DELETE': session.delete,
        'PUT': session.put,
        'POST': session.post,
    }.get(http_method, 'GET')


# Функция для отправки подписанных запросов
def send_signed_request(http_method, url_path, payload={}, retries=3):
    try:
        query_string = urlencode(payload)
        query_string = query_string.replace('%27', '%22')
        query_string += f"&timestamp={get_timestamp()}"
        url = f"{BASE_URL}{url_path}?{query_string}&signature={hashing(query_string)}"

        logging.info(f"Отправка запроса: {http_method} {url}")
        response = dispatch_request(http_method)(url=url)
        response.raise_for_status()  # Проверка на наличие ошибок HTTP
        return response.json()
    except requests.exceptions.RequestException as e:
        if retries > 0:
            logging.warning(f"Ошибка запроса {e}. Повторная попытка...")
            time.sleep(1)  # Пауза перед повторной попыткой
            return send_signed_request(http_method, url_path, payload, retries - 1)
        else:
            logging.error(f"Ошибка запроса после {3 - retries} попыток: {e}")
            return {"error": str(e)}


# Функция для отправки публичных запросов
def send_public_request(url_path, payload={}, retries=3):
    try:
        query_string = urlencode(payload, True)
        url = f"{BASE_URL}{url_path}"
        if query_string:
            url += f"?{query_string}"

        logging.info(f"Отправка публичного запроса: GET {url}")
        response = dispatch_request('GET')(url=url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        if retries > 0:
            logging.warning(f"Ошибка запроса {e}. Повторная попытка...")
            time.sleep(1)
            return send_public_request(url_path, payload, retries - 1)
        else:
            logging.error(f"Ошибка публичного запроса после {3 - retries} попыток: {e}")
            return {"error": str(e)}


''' ====== Примеры использования ====== '''


# Пример получения баланса аккаунта
def get_account_balance():
    try:
        balance_data = send_signed_request('GET', '/fapi/v2/account')
        logging.info("Баланс аккаунта получен успешно")
        return balance_data
    except Exception as e:
        logging.error(f"Ошибка при получении баланса аккаунта: {e}")
        return None


# Пример получения текущей цены символа
def get_symbol_price(symbol='BTCUSDT'):
    try:
        price_data = send_public_request('/fapi/v1/ticker/price', {'symbol': symbol})
        logging.info(f"Текущая цена {symbol}: {price_data['price']}")
        return price_data
    except Exception as e:
        logging.error(f"Ошибка при получении цены символа {symbol}: {e}")
        return None
