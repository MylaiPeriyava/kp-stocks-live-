# kp_server.py
# Local server for KP's Stocks (Live) - WITH FIREBASE

import json
import os
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
import yfinance as yf
import requests
from bs4 import BeautifulSoup

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore
import json
import os

# Initialize Firebase from environment variable
firebase_config = os.environ.get('FIREBASE_SERVICE_ACCOUNT')

if firebase_config:
    cred_dict = json.loads(firebase_config)
    cred = credentials.Certificate(cred_dict)
else:
    # Fallback for local development
    cred_path = os.path.join(os.path.dirname(__file__), 'firebase-service-account.json')
    cred = credentials.Certificate(cred_path)

firebase_admin.initialize_app(cred)
db = firestore.client()


app = Flask(__name__, static_folder=".")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Default user
DEFAULT_USER = "KP"


def get_user_from_request():
    """Get user from query parameter or default"""
    return (request.args.get("user") or DEFAULT_USER).strip().upper()


def to_yahoo_symbol(symbol):
    value = str(symbol).strip().upper()
    if not value:
        return ""
    if value.endswith(".NS") or value.endswith(".BO"):
        return value
    if value.endswith("-EQ"):
        value = value[:-3]
    if value.endswith("-BE"):
        value = value[:-3]
    return value + ".NS"


def display_nse_symbol(symbol):
    value = str(symbol).strip().upper()
    if value.endswith(".NS"):
        return value[:-3] + "-EQ"
    if value.endswith(".BO"):
        return value[:-3] + "-BO"
    if value.endswith("-EQ") or value.endswith("-BE"):
        return value
    return value + "-EQ"


def safe_number(value):
    try:
        if value is None:
            return None
        number = float(value)
        if number != number:
            return None
        return number
    except (TypeError, ValueError):
        return None


def calculate_rsi(close_values, period=14):
    if not close_values:
        return []
    rsi_values = [None] * len(close_values)
    if len(close_values) <= period:
        return rsi_values
    gains = []
    losses = []
    for i in range(1, len(close_values)):
        change = close_values[i] - close_values[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    if average_loss == 0:
        rsi_values[period] = 100.0
    else:
        rs = average_gain / average_loss
        rsi_values[period] = 100 - (100 / (1 + rs))
    for i in range(period + 1, len(close_values)):
        gain = gains[i - 1]
        loss = losses[i - 1]
        average_gain = ((average_gain * (period - 1)) + gain) / period
        average_loss = ((average_loss * (period - 1)) + loss) / period
        if average_loss == 0:
            rsi_values[i] = 100.0
        else:
            rs = average_gain / average_loss
            rsi_values[i] = 100 - (100 / (1 + rs))
    return rsi_values


def calculate_ema(close_values, period):
    if not close_values or period <= 0:
        return []
    ema_values = [None] * len(close_values)
    ema_values[0] = close_values[0]
    multiplier = 2 / (period + 1)
    for i in range(1, len(close_values)):
        if ema_values[i - 1] is None:
            continue
        ema_values[i] = ((close_values[i] - ema_values[i - 1]) * multiplier) + ema_values[i - 1]
    return ema_values


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "kp_stocks.html")


@app.route("/holdings")
def get_holdings():
    user = get_user_from_request()
    try:
        doc_ref = db.collection('holdings').document(user)
        doc = doc_ref.get()
        
        if doc.exists:
            data = doc.to_dict()
            lots = data.get('lots', [])
        else:
            # Return empty list for new users
            lots = []
        
        return jsonify(lots)
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/price")
def get_price():
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "symbol parameter required"}), 400
    yahoo_symbol = to_yahoo_symbol(symbol)
    try:
        ticker = yf.Ticker(yahoo_symbol)
        fast_info = ticker.fast_info
        last_price = safe_number(fast_info.get("lastPrice"))
        if last_price is None:
            info = ticker.info
            last_price = safe_number(info.get("regularMarketPrice") or info.get("currentPrice"))
        if last_price is None:
            return jsonify({"error": "price not available"}), 404
        return jsonify({"symbol": display_nse_symbol(symbol), "yahoo_symbol": yahoo_symbol, "price": last_price})
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/chart-data")
def chart_data():
    user_symbol = request.args.get("symbol", "").strip()
    period = request.args.get("period", "1mo").strip()
    interval = request.args.get("interval", "1d").strip()
    rsi_period_text = request.args.get("rsi", "14").strip()
    if not user_symbol:
        return jsonify({"error": "symbol parameter required"}), 400
    allowed_periods = {"5d", "1mo", "3mo", "6mo", "1y"}
    allowed_intervals = {"1d", "30m", "60m"}
    if period not in allowed_periods:
        return jsonify({"error": "Unsupported period. Use 5d, 1mo, 3mo, 6mo, or 1y."}), 400
    if interval not in allowed_intervals:
        return jsonify({"error": "Unsupported interval. Use 1d, 30m, or 60m."}), 400
    if interval in ("30m", "60m") and period not in ("5d", "1mo"):
        return jsonify({"error": "Intraday intervals only supported for 5d and 1mo."}), 400
    try:
        rsi_period = int(rsi_period_text)
    except ValueError:
        rsi_period = 14
    rsi_period = max(2, min(rsi_period, 50))
    yahoo_symbol = to_yahoo_symbol(user_symbol)
    try:
        ticker = yf.Ticker(yahoo_symbol)
        history = ticker.history(period=period, interval=interval, auto_adjust=False)
        if history is None or history.empty:
            return jsonify({"error": "No chart data for " + yahoo_symbol}), 404
        candles = []
        close_values = []
        candle_times = []
        use_unix_time = interval in ("30m", "60m")
        for index_value, row in history.iterrows():
            open_price = safe_number(row.get("Open"))
            high_price = safe_number(row.get("High"))
            low_price = safe_number(row.get("Low"))
            close_price = safe_number(row.get("Close"))
            volume = safe_number(row.get("Volume"))
            if open_price is None or high_price is None or low_price is None or close_price is None:
                continue
            try:
                import pandas as pd
                ts = pd.Timestamp(index_value)
                time_value = int(ts.timestamp()) if use_unix_time else index_value.strftime("%Y-%m-%d")
            except:
                continue
            candles.append({"time": time_value, "open": round(open_price, 2), "high": round(high_price, 2), "low": round(low_price, 2), "close": round(close_price, 2), "volume": int(volume) if volume else 0})
            close_values.append(close_price)
            candle_times.append(time_value)
        if not candles:
            return jsonify({"error": "No valid OHLC data"}), 404
        rsi_values = calculate_rsi(close_values, rsi_period)
        ema9_values = calculate_ema(close_values, 9)
        ema21_values = calculate_ema(close_values, 21)
        rsi_data = [{"time": candle_times[i], "value": round(rsi_values[i], 2)} for i in range(len(rsi_values)) if rsi_values[i] is not None]
        ema9_data = [{"time": candle_times[i], "value": round(ema9_values[i], 2)} for i in range(len(ema9_values)) if ema9_values[i] is not None]
        ema21_data = [{"time": candle_times[i], "value": round(ema21_values[i], 2)} for i in range(len(ema21_values)) if ema21_values[i] is not None]
        return jsonify({"symbol": display_nse_symbol(user_symbol), "yahooSymbol": yahoo_symbol, "period": period, "interval": interval, "rsiPeriod": rsi_period, "latestClose": candles[-1]["close"], "latestRsi": rsi_data[-1]["value"] if rsi_data else None, "candles": candles, "rsi": rsi_data, "ema9": ema9_data, "ema21": ema21_data})
    except Exception as error:
        return jsonify({"error": str(error)}), 500


def get_analysis_for_symbol(user_symbol):
    if not user_symbol:
        return {"error": "symbol parameter required"}
    yahoo_symbol = to_yahoo_symbol(user_symbol)
    try:
        ticker = yf.Ticker(yahoo_symbol)
        info = ticker.info
        symbol = display_nse_symbol(user_symbol)
        name = info.get("shortName") or info.get("longName") or ""
        sector = info.get("sector") or "Not available"
        industry = info.get("industry") or "Not available"
        market_cap = safe_number(info.get("marketCap"))
        pe = safe_number(info.get("trailingPE"))
        eps = safe_number(info.get("trailingEps"))
        week52_high = safe_number(info.get("fiftyTwoWeekHigh"))
        week52_low = safe_number(info.get("fiftyTwoWeekLow"))
        fast_info = ticker.fast_info
        latest_close = safe_number(fast_info.get("lastPrice") or info.get("regularMarketPrice") or info.get("currentPrice"))
        description = info.get("longBusinessSummary") or ""
        try:
            hist = ticker.history(period="6mo", interval="1d", auto_adjust=False)
        except:
            hist = None
        close_values = []
        if hist is not None and not hist.empty:
            close_values = [safe_number(row["Close"]) for _, row in hist.iterrows()]
            close_values = [c for c in close_values if c is not None]
        short_term_trend = "No short-term trend analysis available."
        long_term_trend = "No long-term trend analysis available."
        if len(close_values) >= 20:
            recent = close_values[-20:]
            if len(recent) >= 2:
                if recent[-1] > recent[0] * 1.05:
                    short_term_trend = "Over the last 20 trading days, the price has risen by more than 5%, indicating a positive short-term trend."
                elif recent[-1] < recent[0] * 0.95:
                    short_term_trend = "Over the last 20 trading days, the price has fallen by more than 5%, indicating a negative short-term trend."
                else:
                    short_term_trend = "Over the last 20 trading days, the price has moved within a narrow range (±5%), indicating a sideways short-term trend."
        if week52_high and week52_low and latest_close:
            if latest_close >= week52_high * 0.9:
                long_term_trend = "The current price is near its 52-week high (within 10%), suggesting a strong long-term uptrend."
            elif latest_close <= week52_low * 1.1:
                long_term_trend = "The current price is near its 52-week low (within 10%), suggesting a weak long-term trend or downtrend."
            else:
                long_term_trend = "The current price is in the middle of its 52-week range, indicating a neutral long-term trend."
        if len(close_values) >= 15:
            rsi_vals = calculate_rsi(close_values, 14)
            latest_rsi = None
            for v in reversed(rsi_vals):
                if v is not None:
                    latest_rsi = v
                    break
            if latest_rsi is not None:
                if latest_rsi > 70:
                    short_term_trend += " The 14-day RSI is above 70, which can indicate an overbought condition."
                elif latest_rsi < 30:
                    short_term_trend += " The 14-day RSI is below 30, which can indicate an oversold condition."
        return {"symbol": symbol, "name": name, "sector": sector, "industry": industry, "marketCap": market_cap, "pe": pe, "eps": eps, "week52High": week52_high, "week52Low": week52_low, "latestClose": latest_close, "description": description, "shortTermTrend": short_term_trend, "longTermTrend": long_term_trend}
    except Exception as error:
        return {"error": str(error)}


@app.route("/analysis")
def analysis():
    user_symbol = request.args.get("symbol", "").strip()
    if not user_symbol:
        return jsonify({"error": "symbol parameter required"}), 400
    result = get_analysis_for_symbol(user_symbol)
    if "error" in result:
        return jsonify(result), 400 if "not available" in result["error"] else 500
    return jsonify(result)


def get_anand_rathi_table(url, table_name):
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("table")
        if not table:
            return jsonify({"error": f"{table_name} table not found"}), 500
        rows = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            rows.append([cell.get_text(" ", strip=True) for cell in cells])
        if not rows:
            return jsonify({"error": f"{table_name} table contained no rows"}), 500
        return jsonify({"source": "anandrathi", "rows": rows})
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/52week-low-ar")
def fifty_two_week_low_ar():
    return get_anand_rathi_table("https://anandrathi.com/share-market-today/52-weeks-low", "52 Weeks Low")


@app.route("/top-losers-ar")
def top_losers_ar():
    return get_anand_rathi_table("https://anandrathi.com/share-market-today/top-losers-today", "Top Losers Today")


@app.route("/52week-high-ar")
def week_52_high_ar():
    return get_anand_rathi_table("https://anandrathi.com/share-market-today/52-weeks-high", "52 Weeks High")


@app.route("/top-gainers-ar")
def top_gainers_ar():
    return get_anand_rathi_table("https://anandrathi.com/share-market-today/top-gainers-today", "Top Gainers Today")


@app.route("/volume-gainers-ar")
def volume_gainers_ar():
    return get_anand_rathi_table("https://anandrathi.com/share-market-today/volume-gainers", "Volume Gainers")


@app.route("/wishlist")
def wishlist():
    user = get_user_from_request()
    try:
        doc_ref = db.collection('wishlist').document(user)
        doc = doc_ref.get()
        
        if doc.exists:
            data = doc.to_dict()
            lines = data.get('lines', [])
        else:
            lines = []
        
        rows = []
        for item in lines:
            if item.get("type") == "heading":
                rows.append({"type": "heading", "text": item["text"]})
                continue
            symbol = item.get("symbol", "")
            yahoo_symbol = to_yahoo_symbol(symbol)
            row = {"type": "symbol", "symbol": symbol, "yahooSymbol": yahoo_symbol, "price": None, "change": None, "changePercent": None, "dayHigh": None, "dayLow": None, "week52High": None, "week52Low": None, "volume": None, "at52WeekLow": False, "error": None}
            try:
                ticker = yf.Ticker(yahoo_symbol)
                fast_info = ticker.fast_info
                price = safe_number(fast_info.get("lastPrice"))
                previous_close = safe_number(fast_info.get("previousClose"))
                day_high = safe_number(fast_info.get("dayHigh"))
                day_low = safe_number(fast_info.get("dayLow"))
                week52_high = safe_number(fast_info.get("yearHigh"))
                week52_low = safe_number(fast_info.get("yearLow"))
                volume = safe_number(fast_info.get("lastVolume"))
                if price is None:
                    info = ticker.info
                    price = safe_number(info.get("regularMarketPrice") or info.get("currentPrice"))
                    previous_close = safe_number(info.get("regularMarketPreviousClose") or info.get("previousClose"))
                    day_high = safe_number(info.get("regularMarketDayHigh") or info.get("dayHigh"))
                    day_low = safe_number(info.get("regularMarketDayLow") or info.get("dayLow"))
                    week52_high = safe_number(info.get("fiftyTwoWeekHigh"))
                    week52_low = safe_number(info.get("fiftyTwoWeekLow"))
                    volume = safe_number(info.get("regularMarketVolume") or info.get("volume"))
                change = None
                change_percent = None
                if price is not None and previous_close not in (None, 0):
                    change = price - previous_close
                    change_percent = (change / previous_close) * 100
                at_52_week_low = day_low is not None and week52_low is not None and abs(day_low - week52_low) < 0.01
                row.update({"price": price, "change": change, "changePercent": change_percent, "dayHigh": day_high, "dayLow": day_low, "week52High": week52_high, "week52Low": week52_low, "volume": volume, "at52WeekLow": at_52_week_low})
            except Exception as error:
                row["error"] = str(error)
            rows.append(row)
        return jsonify({"count": len(rows), "rows": rows})
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/rates")
def get_rates():
    user = get_user_from_request()
    try:
        doc_ref = db.collection('rates').document(user)
        doc = doc_ref.get()
        
        if doc.exists:
            data = doc.to_dict()
            values = data.get('values', [20, 20, 0.00307, 0.000075, 0.0001, 0.0001, 18, 0.015, 0.1, 0.1, 3])
        else:
            # Default rates
            values = [20, 20, 0.00307, 0.000075, 0.0001, 0.0001, 18, 0.015, 0.1, 0.1, 3]
        
        return "\n".join(str(v) for v in values), 200, {"Content-Type": "text/plain"}
    except Exception as error:
        return "Error reading rates: " + str(error), 500, {"Content-Type": "text/plain"}


# ============ VALIDATE ENDPOINT FOR AUTHENTICATION ============
# Whitelist of valid users - add new usernames here
VALID_USERS = {"KP", "PK"}  # Add more users like: {"KP", "RA", "ADMIN"}

@app.route("/validate")
def validate_user_files():
    user = (request.args.get("user") or "").strip().upper()
    
    # Check if user is in whitelist
    if user not in VALID_USERS:
        return jsonify({"exists": False, "error": "User not found. Please check spelling or contact admin."}), 404
    
    # User is valid - check if they have data in Firestore
    holdings_doc = db.collection('holdings').document(user).get()
    
    if not holdings_doc.exists:
        # First time login - auto-create empty documents with default values
        try:
            # Create holdings document
            db.collection('holdings').document(user).set({
                'lots': [],
                'lastUpdated': firestore.SERVER_TIMESTAMP
            })
            
            # Create rates document with default values
            db.collection('rates').document(user).set({
                'values': [20, 20, 0.00307, 0.000075, 0.0001, 0.0001, 18, 0.015, 0.1, 0.1, 3],
                'lastUpdated': firestore.SERVER_TIMESTAMP
            })
            
            # Create wishlist document
            db.collection('wishlist').document(user).set({
                'lines': [],
                'lastUpdated': firestore.SERVER_TIMESTAMP
            })
            
            print(f"Auto-created Firestore documents for new user: {user}")
        except Exception as e:
            print(f"Error auto-creating documents for {user}: {e}")
            return jsonify({"exists": False, "error": "Failed to initialize user data"}), 500
    
    return jsonify({"exists": True}), 200


# ============ MANAGE HOLDINGS ============
def parse_holding_buydate(value):
    if not isinstance(value, str):
        return datetime.max
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y")
    except (TypeError, ValueError):
        return datetime.max


def normalise_and_sort_holdings(lots):
    clean_lots = []
    for lot in lots:
        if not isinstance(lot, dict):
            continue
        symbol = str(lot.get("symbol", "")).strip().upper()
        buydate = str(lot.get("buydate", "")).strip()
        qty = lot.get("qty")
        avg_price = lot.get("avgPrice")
        try:
            qty = int(qty)
            avg_price = float(avg_price)
        except (TypeError, ValueError):
            continue
        try:
            datetime.strptime(buydate, "%d/%m/%Y")
        except ValueError:
            continue
        if not symbol or qty <= 0 or avg_price <= 0:
            continue
        clean_lots.append({"buydate": buydate, "symbol": symbol, "qty": qty, "avgPrice": round(avg_price, 2)})
    return sorted(clean_lots, key=lambda lot: (parse_holding_buydate(lot["buydate"]), lot["symbol"], lot["qty"], lot["avgPrice"]))


@app.route("/manage-holdings", methods=["POST"])
def manage_holdings():
    user = get_user_from_request()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must contain valid JSON."}), 400
    action = payload.get("action")
    
    try:
        # Get current holdings from Firebase
        doc_ref = db.collection('holdings').document(user)
        doc = doc_ref.get()
        
        if doc.exists:
            lots = doc.to_dict().get('lots', [])
        else:
            lots = []
        
        if action == "remove":
            index = payload.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index >= len(lots):
                return jsonify({"error": "Invalid holding lot index."}), 400
            removed_lot = lots.pop(index)
            
            # Save back to Firebase
            sorted_lots = normalise_and_sort_holdings(lots)
            doc_ref.set({
                'lots': sorted_lots,
                'lastUpdated': firestore.SERVER_TIMESTAMP
            })
            
            return jsonify({"ok": True, "message": "Holding lot removed.", "removedLot": removed_lot, "count": len(sorted_lots), "file": f"Holdings_{user}.json"})
        
        if action == "add":
            lot = payload.get("lot")
            if not isinstance(lot, dict):
                return jsonify({"error": "A holding lot is required."}), 400
            symbol = str(lot.get("symbol", "")).strip().upper()
            buydate = str(lot.get("buydate", "")).strip()
            qty = lot.get("qty")
            avg_price = lot.get("avgPrice")
            try:
                qty = int(qty)
                avg_price = float(avg_price)
            except (TypeError, ValueError):
                return jsonify({"error": "Quantity and average price must be valid numbers."}), 400
            try:
                datetime.strptime(buydate, "%d/%m/%Y")
            except ValueError:
                return jsonify({"error": "Buy date must be in DD/MM/YYYY format."}), 400
            if not symbol:
                return jsonify({"error": "Symbol is required."}), 400
            if qty <= 0:
                return jsonify({"error": "Quantity must be greater than zero."}), 400
            if avg_price <= 0:
                return jsonify({"error": "Average price must be greater than zero."}), 400
            new_lot = {"buydate": buydate, "symbol": symbol, "qty": qty, "avgPrice": round(avg_price, 2)}
            lots.append(new_lot)
            
            # Save back to Firebase
            sorted_lots = normalise_and_sort_holdings(lots)
            doc_ref.set({
                'lots': sorted_lots,
                'lastUpdated': firestore.SERVER_TIMESTAMP
            })
            
            return jsonify({"ok": True, "message": "Holding lot added.", "addedLot": new_lot, "count": len(sorted_lots), "file": f"Holdings_{user}.json"})
        
        return jsonify({"error": "Invalid action. Use 'add' or 'remove'."}), 400
    
    except Exception as error:
        return jsonify({"error": str(error)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)), debug=False)
