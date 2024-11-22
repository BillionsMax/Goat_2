import pandas as pd
from binance.client import Client
import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()


class DataLoader:
    def __init__(self, api_key=None, api_secret=None):
        # Загружаем ключи API из переменных окружения, если они не переданы в аргументы
        self.api_key = api_key or os.getenv("API_KEY")
        self.api_secret = api_secret or os.getenv("API_SECRET")

        # Инициализируем клиент Binance
        if self.api_key and self.api_secret:
            self.client = Client(self.api_key, self.api_secret)
        else:
            raise ValueError("API key and secret must be provided.")

    def fetch_historical_data(self, symbol, interval, start_date, end_date):
        """
        Загрузка исторических данных с Binance API.

        :param symbol: Торговый символ, например 'BTCUSDT'
        :param interval: Интервал, например '1d', '1h'
        :param start_date: Начальная дата в формате '1 Jan, 2021'
        :param end_date: Конечная дата в формате '1 Jan, 2022'
        :return: DataFrame с историческими данными
        """
        klines = self.client.get_historical_klines(symbol, interval, start_date, end_date)
        data = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume',
                                             'close_time', 'quote_asset_volume', 'number_of_trades',
                                             'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'])
        data['timestamp'] = pd.to_datetime(data['timestamp'], unit='ms')
        return data[['timestamp', 'open', 'high', 'low', 'close', 'volume']]

    def load_from_csv(self, filepath):
        """
        Загрузка исторических данных из CSV-файла.

        :param filepath: Путь к CSV-файлу
        :return: DataFrame с историческими данными
        """
        data = pd.read_csv(filepath)
        if 'timestamp' in data.columns:
            data['timestamp'] = pd.to_datetime(data['timestamp'])
        return data[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
