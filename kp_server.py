# kp_server.py 
# Local server for KP's Stocks (Live) - WITH FIREBASE 
# FIXED v4: Subscription vs Listing Gain Analysis 

# ============ USER CONFIG ============
USERS = {
    "KP": "vsk",
    "PK": "suk",
}

import json
import os
import re
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
import yfinance as yf
import requests
from bs4 import BeautifulSoup
from flask_cors import CORS

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore

# Initialize Firebase
firebase_config = os.environ.get('FIREBASE_SERVICE_ACCOUNT')

if firebase_config:
    cred_dict = json.loads(firebase_config)
    cred = credentials.Certificate(cred_dict)
else:
    cred_path = os.path.join(os.path.dirname(__file__), 'firebase-service-account.json')
    cred = credentials.Certificate(cred_path)

firebase_admin.initialize_app(cred)
db = firestore.client()


app = Flask(__name__, static_folder=".")
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_USER = "KP"


def get_user_from_request():
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


# ============ IPO DATA ENDPOINT (IPOMarkets - WITH ANALYSIS) ============

def scrape_ipomarkets_page(base_url, page=1):
    """Scrape a single page from IPOMarkets.com"""
    try:
        if page == 1:
            url = base_url
        else:
            url = f"{base_url}/page/{page}"
        
        print(f"Scraping: {url}")
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        table = soup.find('table')
        if not table:
            print(f"No table found on page {page}")
            return [], False
        
        ipos = []
        rows = table.find_all('tr')[1:]
        
        for row in rows:
            cells = row.find_all(['td', 'th'])
            if len(cells) >= 7:
                # Get the company cell
                company_cell = cells[0]
                
                # Extract company name
                company_raw = company_cell.get_text(strip=True)
                if not company_raw:
                    continue
                
                # Extract company URL from the <a> tag
                company_url = None
                company_link = company_cell.find('a')
                if company_link and company_link.get('href'):
                    href = company_link.get('href')
                    # Convert relative URL to absolute
                    if href.startswith('/'):
                        company_url = 'https://ipomarkets.com' + href
                    else:
                        company_url = href
                
                if 'Mainboard' in company_raw:
                    company = company_raw.replace('Mainboard', '').strip()
                elif 'SME' in company_raw:
                    continue
                else:
                    company = company_raw
                
                status = cells[1].get_text(strip=True)
                price_band = cells[2].get_text(strip=True)
                gmp = cells[3].get_text(strip=True)
                subscription = cells[4].get_text(strip=True)
                dates = cells[5].get_text(strip=True)
                listing_info = cells[6].get_text(strip=True)
                
                issue_price = ''
                if price_band and '₹' in price_band:
                    prices = re.findall(r'₹([\d,]+\.?\d*)', price_band)
                    if prices:
                        issue_price = prices[-1].replace(',', '')
                
                listing_price = ''
                listing_gain = ''
                if listing_info and listing_info != '—':
                    price_match = re.search(r'₹([\d,]+\.?\d*)', listing_info)
                    if price_match:
                        listing_price = price_match.group(1).replace(',', '')
                    
                    gain_match = re.search(r'\(([+\-]?[\d.]+)%\)', listing_info)
                    if gain_match:
                        listing_gain = gain_match.group(1)
                
                # Determine status - FIXED LOGIC (PROPERLY INDENTED)
                if 'Listed' in status:
                    final_status = 'listed'
                elif 'Allotment awaited' in status or 'allotted' in status.lower():
                    final_status = 'open'
                elif 'Closes today' in status or 'Closes tomorrow' in status or 'Closes in' in status:
                    # IPOs closing today/tomorrow/in X days are UPCOMING (for highlighting)
                    final_status = 'upcoming'
                elif 'Opens' in status:
                    final_status = 'upcoming'
                elif 'upcoming' in status.lower():
                    final_status = 'upcoming'
                else:
                    final_status = 'listed'
                
                ipos.append({
                    'company': company,
                    'company_url': company_url,  # ← ADDED THIS
                    'sector': '',
                    'listing_date': status,
                    'issue_price': issue_price,
                    'listing_price': listing_price,
                    'listing_gain': listing_gain,
                    'close_gain': '',
                    'subscription': subscription.replace('×', 'x'),
                    'gmp': gmp.replace('₹', ''),
                    'ai_prediction': '',
                    'issue_size': '',
                    'status': final_status,
                    'ipo_type': 'Mainboard'
                })
        
        has_more = False
        
        if soup.find('a', string='Next'):
            has_more = True
        if soup.find('a', href=lambda h: h and '/page/' in h and 'next' in h.lower()):
            has_more = True
        if soup.find('button', string=lambda t: t and 'next' in t.lower()):
            has_more = True
        pagination = soup.find('div', class_=lambda c: c and 'pagination' in c.lower())
        if pagination:
            page_numbers = pagination.find_all('a', href=lambda h: h and '/page/' in h)
            if page_numbers:
                has_more = True
        next_page_url = f"{base_url}/page/{page + 1}"
        try:
            test_response = requests.get(next_page_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            if test_response.status_code == 200 and '<table' in test_response.text:
                has_more = True
        except:
            pass
        
        print(f"Page {page}: {len(ipos)} IPOs, has_more={has_more}")
        return ipos, has_more
        
    except Exception as e:
        print(f'Error scraping page {page}: {e}')
        import traceback
        traceback.print_exc()
        return [], False

def scrape_all_ipomarkets_pages(base_url):
    """Scrape all pages from IPOMarkets"""
    all_ipos = []
    page = 1
    
    while page <= 10:
        ipos, has_more = scrape_ipomarkets_page(base_url, page)
        if not ipos:
            break
        all_ipos.extend(ipos)
        if not has_more:
            break
        page += 1
    
    return all_ipos


def calculate_subscription_analysis(ipos):
    """Calculate % of IPOs with listing gain at different subscription thresholds"""
    thresholds = [25, 20, 15, 10, 5]
    results = {}
    
    for threshold in thresholds:
        # Filter listed IPOs with subscription data
        filtered = [ipo for ipo in ipos 
                   if ipo['status'] == 'listed' 
                   and ipo['subscription']]
        
        def parse_subscription(sub_str):
            if not sub_str:
                return 0
            try:
                # Extract number before 'x' (handles "1.58xexchange basis" format)
                sub_str = str(sub_str).strip()
                if 'x' in sub_str:
                    number_part = sub_str.split('x')[0]
                else:
                    number_part = sub_str
                return float(number_part.strip())
            except:
                return 0
        
        # Filter by subscription threshold
        matched = [ipo for ipo in filtered 
                  if parse_subscription(ipo['subscription']) >= threshold]
        
        if matched:
            # Count IPOs with gain (including 0%)
            gain_count = 0
            for ipo in matched:
                if ipo['listing_gain']:  # Has gain data (0% or positive)
                    try:
                        gain = float(ipo['listing_gain'].replace('+', '').replace('%', ''))
                        if gain >= 0:  # 0% or positive = gain
                            gain_count += 1
                    except:
                        pass
                # If listing_gain is empty/blank = LOSS (don't count)
            
            total = len(matched)
            gain_percentage = round((gain_count / total) * 100, 2) if total > 0 else 0
        else:
            gain_count = 0
            total = 0
            gain_percentage = 0
        
        results[threshold] = {
            'count': gain_count,
            'total': len(matched),
            'gain_percentage': gain_percentage
        }
    
    return results


@app.route("/ipo-data")
def get_ipo_data():
    """Get all Mainboard IPOs from IPOMarkets with subscription analysis"""
    try:
        all_ipos = []
        
        print('\n=== FETCHING IPO DATA FROM IPOMARKETS ===')
        
        print('Fetching 2026 Mainboard IPOs from IPOMarkets...')
        all_ipos = scrape_all_ipomarkets_pages('https://ipomarkets.com/ipo-calendar/2026')
        
        print(f'Total IPOs from IPOMarkets: {len(all_ipos)}')
        
        seen = set()
        unique_ipos = []
        for ipo in all_ipos:
            key = ipo['company'].lower().strip()
            if key not in seen:
                seen.add(key)
                unique_ipos.append(ipo)
        
        def parse_date(date_str):
            if not date_str:
                return datetime(1900, 1, 1)
            try:
                match = re.search(r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept|Oct|Nov|Dec)(?:\s+\d{4})?)', date_str, re.IGNORECASE)
                if match:
                    date_str = match.group(1)
                
                for fmt in ['%d %b %Y', '%d %B %Y', '%d %b %y', '%d %B %y']:
                    try:
                        return datetime.strptime(date_str.strip(), fmt)
                    except:
                        continue
                return datetime(1900, 1, 1)
            except:
                return datetime(1900, 1, 1)
        
        unique_ipos.sort(key=lambda x: parse_date(x['listing_date']), reverse=True)
        
        print(f'\nTotal unique Mainboard IPOs: {len(unique_ipos)}')
        print(f'  - Listed: {len([i for i in unique_ipos if i["status"]=="listed"])}')
        print(f'  - Open: {len([i for i in unique_ipos if i["status"]=="open"])}')
        print(f'  - Upcoming: {len([i for i in unique_ipos if i["status"]=="upcoming"])}')
        
        # Calculate subscription analysis
        analysis = calculate_subscription_analysis(unique_ipos)
        
        print('\n=== SUBSCRIPTION ANALYSIS ===')
        for threshold, data in analysis.items():
            print(f'  Subscription >= {threshold}x: {data["count"]}/{data["total"]} IPOs with gain, Probability: {data["gain_percentage"]}%')
        
        return jsonify({
            'ipos': unique_ipos,
            'analysis': analysis
        })
        
    except Exception as e:
        print(f'Error in /ipo-data: {e}')
        import traceback
        traceback.print_exc()
        return jsonify({'ipos': [], 'analysis': {}})


# ============ ANAND RATHI SCREENERS (RESTORED) ============
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


# ============ CORE ENDPOINTS ============
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


@app.route("/analysis")
def analysis():
    user_symbol = request.args.get("symbol", "").strip()
    if not user_symbol:
        return jsonify({"error": "symbol parameter required"}), 400
    
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
        
        return {
            "symbol": symbol,
            "name": name,
            "sector": sector,
            "industry": industry,
            "marketCap": market_cap,
            "pe": pe,
            "eps": eps,
            "week52High": week52_high,
            "week52Low": week52_low,
            "latestClose": latest_close,
            "description": description,
            "shortTermTrend": short_term_trend,
            "longTermTrend": long_term_trend
        }
    except Exception as error:
        return {"error": str(error)}


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
            row = {
                "type": "symbol",
                "symbol": symbol,
                "yahooSymbol": yahoo_symbol,
                "price": None,
                "change": None,
                "changePercent": None,
                "dayHigh": None,
                "dayLow": None,
                "week52High": None,
                "week52Low": None,
                "volume": None,
                "at52WeekLow": False,
                "error": None
            }
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
                row.update({
                    "price": price,
                    "change": change,
                    "changePercent": change_percent,
                    "dayHigh": day_high,
                    "dayLow": day_low,
                    "week52High": week52_high,
                    "week52Low": week52_low,
                    "volume": volume,
                    "at52WeekLow": at_52_week_low
                })
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
            values = [20, 20, 0.00307, 0.000075, 0.0001, 0.0001, 18, 0.015, 0.1, 0.1, 3]
        
        return "\n".join(str(v) for v in values), 200, {"Content-Type": "text/plain"}
    except Exception as error:
        return "Error reading rates: " + str(error), 500, {"Content-Type": "text/plain"}


# ============ VALIDATE ENDPOINT ============
@app.route("/validate")
def validate_user():
    user = (request.args.get("user") or "").strip().upper()
    pwd = request.args.get("pwd") or ""

    if user not in USERS or USERS[user] != pwd:
        return jsonify({"exists": False, "error": "Invalid user name or password."}), 404

    holdings_doc = db.collection('holdings').document(user).get()

    if not holdings_doc.exists:
        try:
            db.collection('holdings').document(user).set({
                'lots': [],
                'lastUpdated': firestore.SERVER_TIMESTAMP
            })
            db.collection('rates').document(user).set({
                'values': [20, 20, 0.00307, 0.000075, 0.0001, 0.0001, 18, 0.015, 0.1, 0.1, 3],
                'lastUpdated': firestore.SERVER_TIMESTAMP
            })
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
        clean_lots.append({
            "buydate": buydate,
            "symbol": symbol,
            "qty": qty,
            "avgPrice": round(avg_price, 2)
        })
    return sorted(clean_lots, key=lambda lot: (parse_holding_buydate(lot["buydate"]), lot["symbol"], lot["qty"], lot["avgPrice"]))


@app.route("/manage-holdings", methods=["POST"])
def manage_holdings():
    user = get_user_from_request()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must contain valid JSON."}), 400
    action = payload.get("action")
    
    try:
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
            
            sorted_lots = normalise_and_sort_holdings(lots)
            doc_ref.set({
                'lots': sorted_lots,
                'lastUpdated': firestore.SERVER_TIMESTAMP
            })
            
            return jsonify({
                "ok": True,
                "message": "Holding lot removed.",
                "removedLot": removed_lot,
                "count": len(sorted_lots)
            })
        
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
            
            sorted_lots = normalise_and_sort_holdings(lots)
            doc_ref.set({
                'lots': sorted_lots,
                'lastUpdated': firestore.SERVER_TIMESTAMP
            })
            
            return jsonify({
                "ok": True,
                "message": "Holding lot added.",
                "addedLot": new_lot,
                "count": len(sorted_lots)
            })
        
        return jsonify({"error": "Invalid action. Use 'add' or 'remove'."}), 400
    
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/manage-wishlist", methods=["POST"])
def manage_wishlist():
    user = get_user_from_request()
    payload = request.get_json(silent=True)
    
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must contain valid JSON."}), 400
    
    action = payload.get("action")
    symbol = payload.get("symbol", "").strip().upper()
    
    if not symbol:
        return jsonify({"error": "Symbol is required."}), 400
    
    try:
        doc_ref = db.collection('wishlist').document(user)
        doc = doc_ref.get()
        
        if doc.exists:
            data = doc.to_dict()
            lines = data.get('lines', [])
        else:
            lines = []
        
        normalized_lines = []
        for item in lines:
            if isinstance(item, str):
                normalized_lines.append({"type": "symbol", "symbol": item})
            elif isinstance(item, dict):
                normalized_lines.append(item)
        
        lines = normalized_lines
        
        if action == "add":
            existing_symbols = [item.get('symbol') for item in lines if isinstance(item, dict) and item.get('type') == 'symbol']
            if symbol in existing_symbols:
                return jsonify({"error": "Symbol already exists in wishlist."}), 400
            
            lines.append({"type": "symbol", "symbol": symbol})
            
            doc_ref.set({
                'lines': lines,
                'lastUpdated': firestore.SERVER_TIMESTAMP
            })
            
            return jsonify({"ok": True, "message": "Symbol added.", "count": len(lines)}), 200
        
        elif action == "remove":
            lines = [item for item in lines if not (isinstance(item, dict) and item.get('symbol') == symbol)]
            
            doc_ref.set({
                'lines': lines,
                'lastUpdated': firestore.SERVER_TIMESTAMP
            })
            
            return jsonify({"ok": True, "message": "Symbol removed.", "count": len(lines)}), 200
        
        else:
            return jsonify({"error": "Invalid action. Use 'add' or 'remove'."}), 400
    
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/save-rates", methods=["POST"])
def save_rates():
    user = get_user_from_request()
    payload = request.get_json(silent=True)
    
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must contain valid JSON."}), 400
    
    values = payload.get("values")
    
    if not isinstance(values, list) or len(values) != 11:
        return jsonify({"error": "Values must be a list of 11 numbers."}), 400
    
    try:
        values = [float(v) for v in values]
        
        doc_ref = db.collection('rates').document(user)
        doc_ref.set({
            'values': values,
            'lastUpdated': firestore.SERVER_TIMESTAMP
        })
        
        return jsonify({"ok": True, "message": "Rates saved successfully."}), 200
    
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
            
            candles.append({
                "time": time_value,
                "open": round(open_price, 2),
                "high": round(high_price, 2),
                "low": round(low_price, 2),
                "close": round(close_price, 2),
                "volume": int(volume) if volume else 0
            })
            close_values.append(close_price)
            candle_times.append(time_value)
        
        if not candles:
            return jsonify({"error": "No valid OHLC data"}), 404
        
        rsi_values = calculate_rsi(close_values, rsi_period)
        ema9_values = calculate_ema(close_values, 9)
        ema21_values = calculate_ema(close_values, 21)
        
        rsi_data = [
            {"time": candle_times[i], "value": round(rsi_values[i], 2)}
            for i in range(len(rsi_values)) if rsi_values[i] is not None
        ]
        ema9_data = [
            {"time": candle_times[i], "value": round(ema9_values[i], 2)}
            for i in range(len(ema9_values)) if ema9_values[i] is not None
        ]
        ema21_data = [
            {"time": candle_times[i], "value": round(ema21_values[i], 2)}
            for i in range(len(ema21_values)) if ema21_values[i] is not None
        ]
        
        return jsonify({
            "symbol": display_nse_symbol(user_symbol),
            "yahooSymbol": yahoo_symbol,
            "period": period,
            "interval": interval,
            "rsiPeriod": rsi_period,
            "latestClose": candles[-1]["close"],
            "latestRsi": rsi_data[-1]["value"] if rsi_data else None,
            "candles": candles,
            "rsi": rsi_data,
            "ema9": ema9_data,
            "ema21": ema21_data
        })
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/pattern-data")
def pattern_data():
    user_symbol = request.args.get("symbol", "").strip()
    days = request.args.get("days", "30").strip()
    
    if not user_symbol:
        return jsonify({"error": "symbol parameter required"}), 400
    
    try:
        days_int = int(days)
        days_int = max(7, min(days_int, 90))
    except ValueError:
        days_int = 30
    
    yahoo_symbol = to_yahoo_symbol(user_symbol)
    
    try:
        ticker = yf.Ticker(yahoo_symbol)
        history = ticker.history(period=f"{days_int}d", interval="5m", auto_adjust=False)
        
        if history is None or history.empty:
            return jsonify({"error": "No pattern data for " + yahoo_symbol}), 404
        
        candles = []
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
                time_str = ts.strftime("%Y-%m-%dT%H:%M:%S")
            except:
                continue
            
            candles.append([
                time_str,
                round(open_price, 2),
                round(high_price, 2),
                round(low_price, 2),
                round(close_price, 2),
                int(volume) if volume else 0
            ])
        
        if not candles:
            return jsonify({"error": "No valid OHLC data"}), 404
        
        return jsonify({
            "symbol": display_nse_symbol(user_symbol),
            "yahooSymbol": yahoo_symbol,
            "days": days_int,
            "candles": candles
        })
    
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/wl-pattern-data")
def wl_pattern_data():
    user_symbol = request.args.get("symbol", "").strip()
    days = request.args.get("days", "30").strip()

    if not user_symbol:
        return jsonify({"error": "symbol parameter required"}), 400

    try:
        days_int = int(days)
        days_int = max(7, min(days_int, 90))
    except ValueError:
        days_int = 30

    yahoo_symbol = to_yahoo_symbol(user_symbol)

    try:
        ticker = yf.Ticker(yahoo_symbol)
        history = ticker.history(period=f"{days_int}d", interval="5m", auto_adjust=False)

        if history is None or history.empty:
            return jsonify({"error": "No pattern data for " + yahoo_symbol}), 404

        candles = []
        for index_value, row in history.iterrows():
            open_price = safe_number(row.get("Open"))
            high_price = safe_number(row.get("High"))
            low_price = safe_number(row.get("Low"))
            close_price = safe_number(row.get("Close"))
            volume = safe_number(row.get("Volume"))

            if any(v is None for v in (open_price, high_price, low_price, close_price)):
                continue

            try:
                import pandas as pd
                ts = pd.Timestamp(index_value)
                time_str = ts.strftime("%Y-%m-%dT%H:%M:%S")
            except:
                continue

            candles.append([
                time_str,
                round(open_price, 2),
                round(high_price, 2),
                round(low_price, 2),
                round(close_price, 2),
                int(volume) if volume else 0
            ])

        if not candles:
            return jsonify({"error": "No valid OHLC data"}), 404

        return jsonify({
            "symbol": display_nse_symbol(user_symbol),
            "yahooSymbol": yahoo_symbol,
            "days": days_int,
            "candles": candles
        })

    except Exception as error:
        return jsonify({"error": str(error)}), 500


# ============ DAILY HISTORY (for index charts) ============
@app.route("/daily-history")
def daily_history():
    """Daily OHLCV history for a symbol (for index vs stock charts)."""
    user_symbol = request.args.get("symbol", "").strip()
    days = request.args.get("days", "30").strip()

    if not user_symbol:
        return jsonify({"error": "symbol parameter required"}), 400

    try:
        days_int = int(days)
        days_int = max(7, min(days_int, 365))
    except ValueError:
        days_int = 30

    if user_symbol.startswith("^"):
        yahoo_symbol = user_symbol
    else:
        yahoo_symbol = to_yahoo_symbol(user_symbol)

    try:
        ticker = yf.Ticker(yahoo_symbol)
        history = ticker.history(period=f"{days_int}d", interval="1d", auto_adjust=False)

        if history is None or history.empty:
            return jsonify({"error": "No daily data for " + yahoo_symbol}), 404

        candles = []
        for index_value, row in history.iterrows():
            open_price = safe_number(row.get("Open"))
            high_price = safe_number(row.get("High"))
            low_price = safe_number(row.get("Low"))
            close_price = safe_number(row.get("Close"))
            volume = safe_number(row.get("Volume"))

            if any(v is None for v in (open_price, high_price, low_price, close_price)):
                continue

            try:
                import pandas as pd
                ts = pd.Timestamp(index_value)
                time_str = ts.strftime("%Y-%m-%dT%H:%M:%S")
            except:
                continue

            candles.append([
                time_str,
                round(open_price, 2),
                round(high_price, 2),
                round(low_price, 2),
                round(close_price, 2),
                int(volume) if volume else 0
            ])

        if not candles:
            return jsonify({"error": "No valid OHLC data"}), 404

        return jsonify({
            "symbol": display_nse_symbol(user_symbol),
            "yahooSymbol": yahoo_symbol,
            "days": days_int,
            "candles": candles
        })

    except Exception as error:
        return jsonify({"error": str(error)}), 500


# ============ TRADE JOURNAL ============
@app.route("/add-trade", methods=["POST"])
def add_trade():
    user = get_user_from_request()
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No JSON body"}), 400

        buy_date = data.get("buyDate")
        sell_date = data.get("sellDate")
        symbol = data.get("symbol")
        total_spent = data.get("totalSpent")
        total_returned = data.get("totalReturned")

        if not all([buy_date, sell_date, symbol, total_spent is not None, total_returned is not None]):
            return jsonify({"error": "Missing required fields"}), 400

        trades_ref = db.collection("users").document(user).collection("trades")

        doc_ref = trades_ref.add({
            "buyDate": buy_date,
            "sellDate": sell_date,
            "symbol": symbol,
            "totalSpent": float(total_spent),
            "totalReturned": float(total_returned)
        })

        trade_id = doc_ref[1].id
        return jsonify({"ok": True, "tradeId": trade_id}), 200

    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/list-trades", methods=["GET"])
def list_trades():
    user = get_user_from_request()
    try:
        trades_ref = db.collection("users").document(user).collection("trades")
        docs = trades_ref.stream()

        trades = []
        for doc in docs:
            data = doc.to_dict()
            trades.append({
                "id": doc.id,
                "buyDate": data.get("buyDate", ""),
                "sellDate": data.get("sellDate", ""),
                "symbol": data.get("symbol", ""),
                "totalSpent": data.get("totalSpent", 0),
                "totalReturned": data.get("totalReturned", 0)
            })

        return jsonify(trades), 200

    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/delete-trade", methods=["POST"])
def delete_trade():
    user = get_user_from_request()
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No JSON body"}), 400

        trade_id = data.get("tradeId")
        if not trade_id:
            return jsonify({"error": "tradeId required"}), 400

        trade_ref = db.collection("users").document(user).collection("trades").document(trade_id)
        trade_ref.delete()

        return jsonify({"ok": True}), 200

    except Exception as error:
        return jsonify({"error": str(error)}), 500



@app.route("/research-ideas")
def research_ideas():
    """Scrape Screener.in delivery volume increase screen"""
    try:
        all_stocks = []
        base_url = "https://www.screener.in/screens/2241244/delivery-volume-increase/"
        
        # Scrape all pages (up to 10 pages for safety)
        for page in range(1, 11):
            url = f"{base_url}?page={page}" if page > 1 else base_url
            print(f"Scraping Screener.in page {page}: {url}")
            
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            table = soup.find('table')
            
            if not table:
                print(f"No table found on page {page}")
                break
            
            rows = table.find_all('tr')[1:]  # Skip header
            
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if len(cells) >= 14:
                    # Get all cell texts
                    cell_texts = [cell.get_text(strip=True) for cell in cells]
                    
                    # Skip header rows
                    if len(cell_texts) > 1 and cell_texts[1].lower() in ['company', 'name']:
                        continue
                    
                    # EXTRACT COMPANY NAME AND URL
                    company_cell = cells[1] if len(cells) > 1 else None
                    
                    company_name = ''
                    company_url = None
                    
                    if company_cell:
                        company_name = company_cell.get_text(strip=True)
                        # Extract href from the <a> tag
                        company_link = company_cell.find('a')
                        if company_link and company_link.get('href'):
                            company_url = 'https://www.screener.in' + company_link.get('href')
                        else:
                            company_url = None
                    else:
                        company_name = ''
                        company_url = None
                    
                    company = company_name
                    
                    if not company:
                        continue
                    
                    # CORRECT COLUMN INDICES:
                    # Vol 1d = index 11, Avg Vol 1Wk = index 12
                    vol_1d_raw = cell_texts[11] if len(cell_texts) > 11 else '0'
                    avg_vol_1w_raw = cell_texts[12] if len(cell_texts) > 12 else '0'
                    
                    # Parse volume numbers
                    def parse_vol(v):
                        v = v.replace(',', '').strip()
                        try:
                            return float(v)
                        except:
                            return 0
                    
                    vol_1d = parse_vol(vol_1d_raw)
                    avg_vol_1w = parse_vol(avg_vol_1w_raw)
                    
                    print(f"  {company}: Vol={vol_1d_raw} ({vol_1d}), Avg={avg_vol_1w_raw} ({avg_vol_1w}), URL={company_url}")
                    
                    # Calculate volume ratio
                    if avg_vol_1w > 0 and vol_1d > 0:
                        vol_ratio = vol_1d / avg_vol_1w
                        
                        # Only include if volume is actually higher (ratio > 1)
                        if vol_ratio > 1:
                            cmp = cell_texts[2] if len(cell_texts) > 2 else '-'
                            all_stocks.append({
                                'symbol': company,
                                'company_url': company_url,  # ← NEW
                                'reason': f"Volume spike: {vol_ratio:.1f}x avg (1-day vs 1-week avg)",
                                'note': f"CMP: ₹{cmp}, Vol: {vol_1d_raw}, Avg Vol: {avg_vol_1w_raw}"
                            })
            
            # Check if there's a next page - FIXED DETECTION
            next_page = soup.find('a', href=lambda h: h and '?page=' in h and 'Next' in h)
            if not next_page:
                # Fallback: check for any ?page= link
                page_links = soup.find_all('a', href=lambda h: h and '?page=' in h)
                if page_links:
                    next_page = page_links[-1]
            
            if not next_page:
                print(f"No more pages after {page}")
                break
        
        print(f"Total stocks with volume spike: {len(all_stocks)}")
        
        # Remove duplicates (keep first occurrence)
        seen = set()
        unique_stocks = []
        for stock in all_stocks:
            if stock['symbol'] not in seen:
                seen.add(stock['symbol'])
                unique_stocks.append(stock)
        
        print(f"Unique stocks after dedup: {len(unique_stocks)}")
        
        # Sort by volume ratio (highest first) and return ALL unique stocks
        unique_stocks.sort(key=lambda x: float(x['reason'].split(':')[1].split('x')[0].strip()), reverse=True)
        
        return jsonify(unique_stocks)
        
    except Exception as e:
        print(f'Error in /research-ideas: {e}')
        import traceback
        traceback.print_exc()
        return jsonify([])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)), debug=False)

# === END OF kp_server.py ===