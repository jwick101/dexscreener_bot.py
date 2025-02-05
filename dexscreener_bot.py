import requests
import sqlite3
import time
import json
import os
from datetime import datetime

class DexScreenerBot:
    def __init__(self, db_path="dexscreener.db", config_path="config.json"):
        """
        Initialize the bot by loading configuration, setting up the database,
        and initializing required settings.
        """
        self.db_path = db_path
        self.config_path = config_path
        self.config = self.load_config()
        self.conn = sqlite3.connect(self.db_path)
        self.create_tables()
    
    def load_config(self):
        """
        Load configuration from a JSON file. If not found, use default settings.
        The config should include:
          - filters (e.g. rug_threshold, pump_threshold, tier1_liquidity)
          - coin_blacklist and dev_blacklist (lists of symbols/developer addresses)
          - telegram (telegram_token and telegram_chat_id for notifications)
          - api_endpoints for Pocket Universe and rugcheck.xyz
        """
        if not os.path.exists(self.config_path):
            print(f"[{datetime.now()}] Config file {self.config_path} not found. Using default settings.")
            default_config = {
                "filters": {
                    "rug_threshold": -80,
                    "pump_threshold": 100,
                    "tier1_liquidity": 1000000
                },
                "coin_blacklist": [],
                "dev_blacklist": [],
                "telegram": {
                    "telegram_token": "",
                    "telegram_chat_id": ""
                },
                "api_endpoints": {
                    "pocket_universe": "https://api.pocketuniverse.io/verify",
                    "rugcheck": "https://api.rugcheck.xyz/check"
                }
            }
            return default_config
        else:
            with open(self.config_path, "r") as f:
                config = json.load(f)
                print(f"[{datetime.now()}] Loaded configuration from {self.config_path}.")
                return config
    
    def create_tables(self):
        """
        Create database tables:
          - token_data: Stores token snapshots (with fields such as contract and bundled flag)
          - coin_events: Logs detected events for each token.
        """
        c = self.conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS token_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT,
                symbol TEXT,
                developer TEXT,
                contract TEXT,
                price REAL,
                liquidity REAL,
                volume REAL,
                price_change REAL,
                bundled INTEGER,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS coin_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_id TEXT,
                event_type TEXT,
                details TEXT,
                event_time DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()
    
    def fetch_data(self):
        """
        Fetch token data from Dexscreener. Adjust the URL/parameters per the current API.
        """
        url = "https://api.dexscreener.com/token-profiles/latest/v1"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()  # Raise for HTTP errors
            data = response.json()
            return data
        except Exception as e:
            print(f"[{datetime.now()}] Error fetching data: {e}")
            return None

    def save_token_data(self, token):
        """
        Save the token’s snapshot into the database. We include fields such as:
          - token_id, symbol, developer, contract
          - price, liquidity, volume, price_change, bundled (1 if True, 0 if False)
        """
        c = self.conn.cursor()
        c.execute('''
            INSERT INTO token_data (token_id, symbol, developer, contract, price, liquidity, volume, price_change, bundled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            token.get("id"),
            token.get("symbol"),
            token.get("developer"),
            token.get("contract"),
            self._safe_float(token.get("price")),
            self._safe_float(token.get("liquidity")),
            self._safe_float(token.get("volume")),
            self._safe_float(token.get("price_change")),
            1 if token.get("bundled", False) else 0
        ))
        self.conn.commit()

    def record_event(self, token_id, event_type, details):
        """
        Record a detected event (e.g. 'rugged', 'pumped') in the database.
        """
        c = self.conn.cursor()
        c.execute('''
            INSERT INTO coin_events (token_id, event_type, details)
            VALUES (?, ?, ?)
        ''', (token_id, event_type, details))
        self.conn.commit()

    def _safe_float(self, value):
        """
        Convert a value to a float safely. Return None if conversion fails.
        """
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def verify_volume(self, token):
        """
        Verify the authenticity of the token’s volume.
        Uses the Pocket Universe API if an endpoint is provided;
        otherwise, falls back to a simple check.
        """
        token_id = token.get("id")
        endpoint = self.config.get("api_endpoints", {}).get("pocket_universe", "")
        if not endpoint:
            # Fallback: ensure volume is a positive number.
            volume = self._safe_float(token.get("volume"))
            return volume is not None and volume > 0
        try:
            # Example: API call with token as a query parameter.
            response = requests.get(f"{endpoint}?token={token_id}", timeout=10)
            response.raise_for_status()
            result = response.json()
            # Expecting a field "authentic" (True if volume is genuine)
            authentic = result.get("authentic", False)
            if not authentic:
                print(f"[{datetime.now()}] Token {token.get('symbol')} failed volume authenticity check.")
            return authentic
        except Exception as e:
            print(f"[{datetime.now()}] Error verifying volume for token {token.get('symbol')}: {e}")
            return False

    def verify_rugcheck(self, token):
        """
        Check the token’s contract on rugcheck.xyz.
        Only tokens with a status of 'Good' are allowed.
        """
        contract = token.get("contract")
        if not contract:
            print(f"[{datetime.now()}] No contract info for token {token.get('symbol')}, skipping rugcheck.")
            return False
        endpoint = self.config.get("api_endpoints", {}).get("rugcheck", "")
        if not endpoint:
            # If no endpoint is provided, assume token is good.
            return True
        try:
            response = requests.get(f"{endpoint}?contract={contract}", timeout=10)
            response.raise_for_status()
            result = response.json()
            status = result.get("status", "")
            if status != "Good":
                print(f"[{datetime.now()}] Token {token.get('symbol')} is marked as '{status}' on rugcheck.xyz.")
            return status == "Good"
        except Exception as e:
            print(f"[{datetime.now()}] Error verifying rugcheck for token {token.get('symbol')}: {e}")
            return False

    def send_telegram_notification(self, message):
        """
        Send a notification via Telegram.
        The configuration must include a valid telegram_token and telegram_chat_id.
        """
        telegram_config = self.config.get("telegram", {})
        telegram_token = telegram_config.get("telegram_token", "")
        chat_id = telegram_config.get("telegram_chat_id", "")
        if not telegram_token or not chat_id:
            print(f"[{datetime.now()}] Telegram configuration missing. Skipping Telegram notification.")
            return
        url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message
        }
        try:
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code == 200:
                print(f"[{datetime.now()}] Telegram notification sent.")
            else:
                print(f"[{datetime.now()}] Telegram notification failed with status code {response.status_code}.")
        except Exception as e:
            print(f"[{datetime.now()}] Error sending Telegram notification: {e}")

    def classify_coin(self, token):
        """
        Apply classification thresholds from the config to the token’s metrics.
        Detected events (e.g. 'rugged', 'pumped', 'tier-1', 'listed_on_cex') are recorded.
        Returns the list of events.
        """
        events = []
        token_id = token.get("id")
        symbol = token.get("symbol")
        price_change = token.get("price_change")
        liquidity = token.get("liquidity")
        
        filters = self.config.get("filters", {})
        rug_threshold = filters.get("rug_threshold", -80)
        pump_threshold = filters.get("pump_threshold", 100)
        tier1_liquidity = filters.get("tier1_liquidity", 1000000)
        
        price_change_val = self._safe_float(price_change)
        liquidity_val = self._safe_float(liquidity)
        
        if price_change_val is not None and price_change_val < rug_threshold:
            events.append(("rugged", f"Price dropped by {price_change_val}% (threshold: {rug_threshold}%)"))
        
        if price_change_val is not None and price_change_val > pump_threshold:
            events.append(("pumped", f"Price increased by {price_change_val}% (threshold: {pump_threshold}%)"))
        
        if liquidity_val is not None and liquidity_val > tier1_liquidity:
            events.append(("tier-1", f"High liquidity of {liquidity_val} (threshold: {tier1_liquidity})"))
        
        # Dummy check for CEX listing based on common tokens.
        known_cex_tokens = {"BTC", "ETH", "BNB", "USDT", "USDC"}
        if symbol and symbol.upper() in known_cex_tokens:
            events.append(("listed_on_cex", f"Token {symbol} is typically listed on major CEXs"))
        
        for event_type, details in events:
            self.record_event(token_id, event_type, details)
            print(f"[{datetime.now()}] Detected event for {symbol} ({token_id}): {event_type} - {details}")
            
        return events

    def analyze_tokens(self, data):
        """
        Process tokens from the DexScreener API:
          - Filter tokens based on coin and developer blacklists.
          - Skip tokens with bundled supply.
          - Verify volume authenticity and rugcheck status.
          - Save data, classify events, and send trade notifications via Telegram.
        """
        tokens = data.get("tokens", [])
        print(f"[{datetime.now()}] Processing {len(tokens)} tokens...")
        
        coin_blacklist = set(symbol.upper() for symbol in self.config.get("coin_blacklist", []))
        dev_blacklist = set(addr.lower() for addr in self.config.get("dev_blacklist", []))
        
        for token in tokens:
            symbol = token.get("symbol", "").upper()
            developer = token.get("developer", "")
            bundled = token.get("bundled", False)
            
            # Skip if token is in the coin blacklist.
            if symbol in coin_blacklist:
                print(f"[{datetime.now()}] Token {symbol} is blacklisted. Skipping.")
                continue
            
            # Skip if the developer is in the dev blacklist.
            if developer and developer.lower() in dev_blacklist:
                print(f"[{datetime.now()}] Token {symbol} is from a blacklisted developer ({developer}). Skipping.")
                continue
            
            # Skip tokens with bundled supply.
            if bundled:
                print(f"[{datetime.now()}] Token {symbol} has bundled supply. Skipping.")
                continue
            
            # Verify volume authenticity.
            if not self.verify_volume(token):
                print(f"[{datetime.now()}] Token {symbol} failed volume authenticity check. Skipping.")
                continue
            
            # Verify token on rugcheck.xyz.
            if not self.verify_rugcheck(token):
                print(f"[{datetime.now()}] Token {symbol} failed rugcheck verification. Skipping.")
                continue
            
            # Passed all checks: save token data and classify events.
            self.save_token_data(token)
            events = self.classify_coin(token)
            
            # If token qualifies for trading (e.g., is pumped or tier-1), send a Telegram notification.
            trade_signals = [e for e in events if e[0] in ("pumped", "tier-1")]
            if trade_signals:
                message = f"Trade Signal for {symbol}:\n"
                message += "\n".join([f"{etype}: {detail}" for etype, detail in trade_signals])
                self.send_telegram_notification(message)

    def run(self, interval=60):
        """
        Main loop: fetch data from Dexscreener, analyze tokens, and wait for the next cycle.
        """
        print(f"[{datetime.now()}] DexScreenerBot is starting. Polling every {interval} seconds...")
        while True:
            data = self.fetch_data()
            if data:
                self.analyze_tokens(data)
            else:
                print(f"[{datetime.now()}] No data fetched; retrying in {interval} seconds.")
            time.sleep(interval)

if __name__ == "__main__":
    bot = DexScreenerBot()
    bot.run(interval=60)
