import os
import requests
import json
import csv
import sqlite3
import threading
import time
import traceback
import pickle
import smtplib
import uuid
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
# import re  # COMMENTED OUT - regex import
from bs4 import BeautifulSoup

# Twilio imports
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Pay

# Selenium removed - using direct API calls instead
SELENIUM_AVAILABLE = False

# OpenAI import
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# --- Configuration ---
XELION_BASE_URL = os.getenv('XELION_BASE_URL', 'https://lvsl01.xelion.com/api/v1/wasteking')
XELION_USERNAME = os.getenv('XELION_USERNAME', 'your_xelion_username')
XELION_PASSWORD = os.getenv('XELION_PASSWORD', 'your_xelion_password')
XELION_APP_KEY = os.getenv('XELION_APP_KEY', 'your_xelion_app_key')
XELION_USERSPACE = os.getenv('XELION_USERSPACE', 'transcriber-abi-housego')

# Deepgram API
DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY', 'your_deepgram_api_key')

# OpenAI API
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', 'your_openai_api_key')

# WasteKing API Configuration - FIXED
WASTEKING_EMAIL = os.getenv('WASTEKING_EMAIL', 'kanchan.ghosh@wasteking.co.uk')
WASTEKING_PASSWORD = os.getenv('WASTEKING_PASSWORD', 'T^269725365789ad')
WASTEKING_BASE_URL = "https://wk-smp-api-dev.azurewebsites.net/"
WASTEKING_ACCESS_TOKEN = "wk-KZPY-tGF-@d.Aby9fpvMC_VVWkX-GN.i7jCBhF3xceoFfhmawaNc.RH.G_-kwk8*"
WASTEKING_PRICING_URL = f"{WASTEKING_BASE_URL}/reporting/priced-area-coverage-breakdown/"

# Twilio + Braintree Configuration
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', 'your_twilio_sid')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', 'your_twilio_token')
BRAINTREE_CONNECTOR_SID = "XEf479d2720372e1d3bbeade564221029a"
BRAINTREE_MERCHANT_ID = "99szgbfz5tr6vmjf"
ELEVENLABS_WEBHOOK_URL = os.getenv('ELEVENLABS_WEBHOOK_URL', 'your_elevenlabs_webhook')
SERVER_BASE_URL = "https://internal-porpoise-onewebonly-1b44fcb9.koyeb.app"

# Database configuration
DATABASE_FILE = 'calls.db'

# Directory for temporary audio files
AUDIO_TEMP_DIR = 'temp_audio'
os.makedirs(AUDIO_TEMP_DIR, exist_ok=True)

# Session files
WASTEKING_COOKIES_FILE = "wasteking_session.pkl"
SESSION_TIMEOUT_DAYS = 30

# Deepgram pricing and currency conversion
DEEPGRAM_PRICE_PER_MINUTE = 0.0043
USD_TO_GBP_RATE = 0.79

# Global session and locks for thread safety
xelion_session = requests.Session()
session_token = None
login_lock = threading.Lock()
db_lock = threading.Lock()
transcription_lock = threading.Lock()

# Global flag to track if background process is running
background_process_running = False
background_thread = None

# Statistics tracking
processing_stats = {
    'total_fetched': 0,
    'total_processed': 0,
    'total_skipped': 0,
    'total_errors': 0,
    'statuses_seen': {},
    'last_poll_time': None,
    'last_error': None
}

def log_with_timestamp(message, level="INFO"):
    """Enhanced logging with timestamps and levels"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {message}")

def log_error(message, error=None):
    """Log errors with full traceback"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if error:
        log_with_timestamp(f"ERROR: {message}: {error}", "ERROR")
        log_with_timestamp(f"Full traceback: {traceback.format_exc()}", "ERROR")
    else:
        log_with_timestamp(f"ERROR: {message}", "ERROR")
    
    processing_stats['last_error'] = f"{message}: {error}" if error else message

def test_openai_connection() -> bool:
    """Test OpenAI API connection"""
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY or OPENAI_API_KEY == 'your_openai_api_key':
        return False
    
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Return JSON: {\"test\": 5}"}],
            response_format={"type": "json_object"},
            max_tokens=50
        )
        result = json.loads(response.choices[0].message.content)
        return 'test' in result and result['test'] == 5
    except Exception as e:
        log_error("OpenAI connection test failed", e)
        return False

# --- Database Functions ---
def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    log_with_timestamp("Initializing database...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Add the calls table (existing code)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS calls (
                oid TEXT PRIMARY KEY,
                call_datetime TEXT,
                agent_name TEXT,
                phone_number TEXT,
                call_direction TEXT,
                duration_seconds REAL,
                status TEXT,
                user_id TEXT,
                transcription_text TEXT,
                transcribed_duration_minutes REAL,
                deepgram_cost_usd REAL,
                deepgram_cost_gbp REAL,
                word_count INTEGER,
                confidence REAL,
                language TEXT,
                openai_engagement REAL,
                openai_politeness REAL,
                openai_professionalism REAL,
                openai_resolution REAL,
                openai_overall_score REAL,
                category TEXT,
                engagement_sub1 REAL, engagement_sub2 REAL, engagement_sub3 REAL, engagement_sub4 REAL,
                politeness_sub1 REAL, politeness_sub2 REAL, politeness_sub3 REAL, politeness_sub4 REAL,
                professionalism_sub1 REAL, professionalism_sub2 REAL, professionalism_sub3 REAL, professionalism_sub4 REAL,
                resolution_sub1 REAL, resolution_sub2 REAL, resolution_sub3 REAL, resolution_sub4 REAL,
                processed_at TEXT,
                processing_error TEXT,
                raw_communication_data TEXT,
                summary_translation TEXT
            )
        ''')
        
        # Updated price_quotes table with status column
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_quotes (
                quote_id TEXT PRIMARY KEY,
                booking_ref TEXT,
                postcode TEXT,
                service TEXT,
                price_data TEXT,
                created_at TEXT,
                status TEXT DEFAULT 'pending',
                customer_phone TEXT,
                agent_name TEXT,
                call_sid TEXT,
                elevenlabs_conversation_id TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                quote_id TEXT,
                booking_ref TEXT,
                amount REAL,
                currency TEXT DEFAULT 'GBP',
                payment_status TEXT DEFAULT 'pending',
                created_at TEXT,
                paid_at TEXT,
                customer_phone TEXT,
                twilio_payment_sid TEXT,
                braintree_transaction_id TEXT,
                call_sid TEXT,
                FOREIGN KEY (quote_id) REFERENCES price_quotes (quote_id)
            )
        ''')
        
        conn.commit()
        conn.close()
        log_with_timestamp("Database initialized successfully")
    except Exception as e:
        log_error("Failed to initialize database", e)

# --- WasteKing Session Management ---
def save_wasteking_session(session):
    """Save authenticated WasteKing session for 30 days"""
    try:
        session_data = {
            'cookies': session.cookies.get_dict(),
            'timestamp': datetime.now(),
            'headers': dict(session.headers)
        }
        
        with open(WASTEKING_COOKIES_FILE, 'wb') as f:
            pickle.dump(session_data, f)
        
        log_with_timestamp("✅ WasteKing session saved for 30 days")
        return True
    except Exception as e:
        log_error("Failed to save WasteKing session", e)
        return False

def load_wasteking_session():
    """Load saved WasteKing session if still valid"""
    try:
        if not os.path.exists(WASTEKING_COOKIES_FILE):
            log_with_timestamp("No saved WasteKing session found")
            return None
        
        with open(WASTEKING_COOKIES_FILE, 'rb') as f:
            session_data = pickle.load(f)
        
        # Check if session expired
        age = datetime.now() - session_data['timestamp']
        if age > timedelta(days=SESSION_TIMEOUT_DAYS):
            log_with_timestamp(f"WasteKing session expired ({age.days} days old)")
            return None
        
        # Create session with saved cookies
        session = requests.Session()
        session.cookies.update(session_data['cookies'])
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/html, */*',
            'Accept-Language': 'en-US,en;q=0.9'
        })
        
        log_with_timestamp(f"✅ WasteKing session loaded ({age.days} days old)")
        return session
        
    except Exception as e:
        log_error("Failed to load WasteKing session", e)
        return None

def authenticate_wasteking():
    """Authenticate WasteKing using direct API calls (NO BROWSER)"""
    log_with_timestamp("🔐 Starting WasteKing authentication with direct API...")
    
    try:
        # Step 1: Get the login page to extract any CSRF tokens or cookies
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        })
        
        log_with_timestamp("🌐 Getting login page...")
        
        # Try multiple potential login URLs
        login_urls = [
            f"{WASTEKING_BASE_URL}/login",
            f"{WASTEKING_BASE_URL}/account/login", 
            f"{WASTEKING_BASE_URL}/auth/login",
            f"{WASTEKING_BASE_URL}/user/login",
            WASTEKING_PRICING_URL  # Sometimes login redirects happen
        ]
        
        login_page_response = None
        working_login_url = None
        
        for url in login_urls:
            try:
                log_with_timestamp(f"Trying login URL: {url}")
                response = session.get(url, timeout=10, allow_redirects=True)
                if response.status_code == 200:
                    login_page_response = response
                    working_login_url = url
                    log_with_timestamp(f"✅ Found working login URL: {url}")
                    break
            except:
                continue
        
        if not login_page_response:
            return {
                "error": "Could not access login page",
                "status": "login_page_not_found",
                "message": "All login URLs failed",
                "timestamp": datetime.now().isoformat()
            }
        
        # Step 2: Parse the login page for form details
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(login_page_response.text, 'html.parser')
        
        # Look for login form
        login_form = soup.find('form') or soup.find('form', {'action': lambda x: x and 'login' in x.lower()})
        
        csrf_token = None
        form_action = working_login_url
        
        if login_form:
            # Extract form action
            action = login_form.get('action')
            if action:
                if action.startswith('/'):
                    form_action = f"{WASTEKING_BASE_URL}{action}"
                elif action.startswith('http'):
                    form_action = action
                
            # Look for CSRF token
            csrf_input = login_form.find('input', {'name': lambda x: x and 'csrf' in x.lower()}) or \
                        login_form.find('input', {'name': lambda x: x and 'token' in x.lower()}) or \
                        login_form.find('input', {'type': 'hidden'})
            
            if csrf_input:
                csrf_token = csrf_input.get('value')
                log_with_timestamp(f"🔑 Found CSRF token: {csrf_token[:20]}...")
        
        # Step 3: Prepare login data
        login_data = {
            'email': WASTEKING_EMAIL,
            'password': WASTEKING_PASSWORD
        }
        
        # Add CSRF token if found
        if csrf_token:
            # Try common CSRF field names
            csrf_names = ['_token', 'csrf_token', '__RequestVerificationToken', 'authenticity_token']
            for name in csrf_names:
                if soup.find('input', {'name': name}):
                    login_data[name] = csrf_token
                    break
        
        # Step 4: Attempt login
        log_with_timestamp(f"🚀 Submitting login to: {form_action}")
        
        # Set proper headers for form submission
        session.headers.update({
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': WASTEKING_BASE_URL,
            'Referer': working_login_url
        })
        
        login_response = session.post(form_action, data=login_data, timeout=15, allow_redirects=True)
        
        log_with_timestamp(f"Login response status: {login_response.status_code}")
        
        # Step 5: Check if login was successful
        if login_response.status_code == 200:
            # Check if we're redirected to dashboard/reporting or still on login page
            final_url = login_response.url
            response_text = login_response.text.lower()
            
            success_indicators = ['dashboard', 'reporting', 'logout', 'welcome', 'profile']
            failure_indicators = ['login', 'error', 'invalid', 'incorrect']
            
            if any(indicator in final_url.lower() for indicator in success_indicators) or \
               any(indicator in response_text for indicator in success_indicators):
                
                # Test access to pricing URL
                test_response = session.get(WASTEKING_PRICING_URL, timeout=10)
                if test_response.status_code == 200:
                    save_wasteking_session(session)
                    log_with_timestamp("✅ WasteKing authentication successful!")
                    return session
                else:
                    log_with_timestamp(f"❌ Login seemed successful but pricing URL failed: {test_response.status_code}")
            
            elif any(indicator in response_text for indicator in failure_indicators):
                return {
                    "error": "Login failed - invalid credentials",
                    "status": "login_failed",
                    "message": "Username/password incorrect",
                    "timestamp": datetime.now().isoformat()
                }
        
        # If we reach here, try alternative approaches
        log_with_timestamp("🔄 Trying alternative login approaches...")
        
        # Try JSON login
        session.headers.update({'Content-Type': 'application/json'})
        json_login_data = json.dumps(login_data)
        
        for endpoint in ['/api/login', '/auth', '/api/auth/login']:
            try:
                api_url = f"{WASTEKING_BASE_URL}{endpoint}"
                json_response = session.post(api_url, data=json_login_data, timeout=10)
                if json_response.status_code == 200:
                    test_response = session.get(WASTEKING_PRICING_URL, timeout=10)
                    if test_response.status_code == 200:
                        save_wasteking_session(session)
                        log_with_timestamp(f"✅ WasteKing JSON login successful via {endpoint}")
                        return session
            except:
                continue
        
        return {
            "error": "All login methods failed",
            "status": "login_failed", 
            "message": "Could not authenticate with any method",
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        log_error("WasteKing API authentication failed", e)
        return {
            "error": "Authentication error",
            "status": "auth_error",
            "message": f"API login failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }

def get_wasteking_prices():
    """Get WasteKing pricing data"""
    try:
        log_with_timestamp("💰 Fetching WasteKing prices...")
        
        session = load_wasteking_session()
        
        if not session:
            log_with_timestamp("No valid session, attempting authentication...")
            auth_result = authenticate_wasteking()
            
            if isinstance(auth_result, dict) and "error" in auth_result:
                return auth_result
            
            session = auth_result
            
            if not session:
                return {
                    "error": "Authentication required",
                    "status": "session_expired", 
                    "message": "Unable to authenticate with WasteKing",
                    "timestamp": datetime.now().isoformat()
                }
        
        response = session.get(WASTEKING_PRICING_URL, timeout=15)
        
        if response.status_code == 200:
            return {
                "status": "success",
                "timestamp": datetime.now().isoformat(),
                "message": "WasteKing data fetched successfully",
                "data_length": len(response.text)
            }
        elif response.status_code in [401, 403]:
            # Try re-authentication
            auth_result = authenticate_wasteking()
            
            if isinstance(auth_result, dict) and "error" in auth_result:
                return auth_result
            
            session = auth_result
            
            if session:
                response = session.get(WASTEKING_PRICING_URL, timeout=15)
                if response.status_code == 200:
                    return {
                        "status": "success",
                        "message": "Re-authenticated and fetched data",
                        "timestamp": datetime.now().isoformat(),
                        "data_length": len(response.text)
                    }
            
            return {
                "error": "Authentication failed",
                "status": "auth_required",
                "timestamp": datetime.now().isoformat()
            }
        else:
            return {
                "error": f"Request failed: {response.status_code}",
                "status": "request_failed",
                "timestamp": datetime.now().isoformat()
            }
            
    except Exception as e:
        log_error("Error fetching WasteKing prices", e)
        return {
            "error": str(e),
            "status": "error",
            "timestamp": datetime.now().isoformat()
        }

# --- Xelion API Functions (Using Working Patterns) ---
def xelion_login() -> bool:
    """Login using the WORKING pattern from transcription.py"""
    global session_token
    with login_lock:
        session_token = None
        
        login_url = f"{XELION_BASE_URL.rstrip('/')}/me/login"
        headers = {"Content-Type": "application/json"}
        
        # Use the WORKING userspace pattern
        if XELION_USERSPACE:
            userspace = XELION_USERSPACE
        else:
            userspace = f"transcriber-{XELION_USERNAME.split('@')[0].replace('.', '-')}"
        
        data_payload = { 
            "userName": XELION_USERNAME, 
            "password": XELION_PASSWORD,
            "userSpace": userspace,
            "appKey": XELION_APP_KEY
        }
        
        json_data_string = json.dumps(data_payload)
        
        log_with_timestamp(f"🔑 Attempting Xelion login for {XELION_USERNAME}")
        try:
            if 'Authorization' in xelion_session.headers:
                del xelion_session.headers['Authorization']
                
            response = xelion_session.post(login_url, headers=headers, data=json_data_string, timeout=30)
            response.raise_for_status() 
            
            login_response = response.json()
            session_token = login_response.get("authentication")
            
            if session_token:
                xelion_session.headers.update({"Authorization": f"xelion {session_token}"})
                valid_until = login_response.get('validUntil', 'N/A')
                log_with_timestamp(f"✅ Successfully logged in (Valid until: {valid_until})")
                return True
            else:
                log_error("No authentication token received")
                return False
                
        except requests.exceptions.RequestException as e:
            log_error(f"Failed to log in to Xelion", e)
            return False

def fetch_all_communications_from_start_time(until_date: datetime, start_time: datetime) -> List[Dict]:
    """Fetch UNLIMITED calls but ONLY from start_time onwards (Aug 4th 2025 9 AM+)"""
    all_communications = []
    before_oid = None
    page = 1
    
    # Use the WORKING base URLs pattern
    base_urls_to_try = [
        XELION_BASE_URL,  
        XELION_BASE_URL.replace('/wasteking', '/master'), 
        'https://lvsl01.xelion.com/api/v1/master', 
    ]
    
    while True:
        params = {'limit': 1000}  # High limit per page
        if until_date:
            params['until'] = until_date.strftime('%Y-%m-%d %H:%M:%S')
        if before_oid:
            params['before'] = before_oid
        
        fetched_this_page = False
        
        for attempt in range(3):
            for base_url in base_urls_to_try:
                communications_url = f"{base_url.rstrip('/')}/communications"
                try:
                    log_with_timestamp(f"Fetching UNLIMITED page {page} from {communications_url} (attempt {attempt + 1})")
                    response = xelion_session.get(communications_url, params=params, timeout=30)
                    
                    if response.status_code == 401:
                        log_with_timestamp("🔑 401 error, re-authenticating...")
                        global session_token
                        session_token = None
                        
                        if xelion_login():
                            continue
                        else:
                            return all_communications
                    
                    response.raise_for_status()
                    data = response.json()
                    communications = data.get('data', [])
                    
                    if not communications:
                        log_with_timestamp(f"No more communications found on page {page}")
                        return all_communications
                    
                    # CHECK: Stop if we've gone too far back (before start_time)
                    page_has_valid_calls = False
                    valid_calls_this_page = []
                    
                    for comm in communications:
                        comm_obj = comm.get('object', {})
                        call_datetime = comm_obj.get('date', '')
                        
                        if call_datetime:
                            try:
                                if 'T' in call_datetime:
                                    call_dt = datetime.fromisoformat(call_datetime.replace('Z', '+00:00'))
                                else:
                                    call_dt = datetime.strptime(call_datetime, '%Y-%m-%d %H:%M:%S')
                                
                                # If this call is from start_time or later, keep it
                                if call_dt >= start_time:
                                    valid_calls_this_page.append(comm)
                                    page_has_valid_calls = True
                                # If call is older than start_time, we've gone too far back
                                else:
                                    log_with_timestamp(f"⏹️ Reached call older than start time ({call_dt} < {start_time})")
                                    # Add valid calls from this page and stop
                                    all_communications.extend(valid_calls_this_page)
                                    log_with_timestamp(f"🎯 FINAL: Fetched {len(all_communications)} calls from {start_time} onwards")
                                    return all_communications
                                    
                            except Exception as e:
                                log_with_timestamp(f"Error parsing date {call_datetime}: {e}")
                    
                    # If no valid calls on this page, we're done
                    if not page_has_valid_calls:
                        log_with_timestamp(f"⏹️ No calls from {start_time} onwards on page {page}")
                        log_with_timestamp(f"🎯 FINAL: Fetched {len(all_communications)} calls from {start_time} onwards")
                        return all_communications
                    
                    # Add valid calls from this page
                    all_communications.extend(valid_calls_this_page)
                    log_with_timestamp(f"Page {page}: Added {len(valid_calls_this_page)} valid calls (Total: {len(all_communications)})")
                    
                    # Track status breakdown for valid calls only
                    status_breakdown = {}
                    for comm in valid_calls_this_page:
                        status = comm.get('object', {}).get('status', 'unknown')
                        status_breakdown[status] = status_breakdown.get(status, 0) + 1
                        processing_stats['statuses_seen'][status] = processing_stats['statuses_seen'].get(status, 0) + 1
                    
                    log_with_timestamp(f"Page {page} valid calls status: {status_breakdown}")
                    
                    # Get next page info
                    if 'meta' in data and 'paging' in data['meta']:
                        before_oid = data['meta']['paging'].get('previousId')
                        if not before_oid:
                            log_with_timestamp("No more pages available")
                            log_with_timestamp(f"🎯 FINAL: Fetched {len(all_communications)} calls from {start_time} onwards")
                            return all_communications
                    else:
                        log_with_timestamp("No paging info, assuming last page")
                        log_with_timestamp(f"🎯 FINAL: Fetched {len(all_communications)} calls from {start_time} onwards")
                        return all_communications
                    
                    fetched_this_page = True
                    break
                        
                except Exception as e:
                    log_error(f"Failed to fetch page {page} from {base_url} (attempt {attempt + 1})", e)
                    continue
            
            if fetched_this_page:
                break
        
        if not fetched_this_page:
            log_error(f"Failed to fetch page {page} from all URLs and attempts")
            break
        
        page += 1
        processing_stats['total_fetched'] = len(all_communications)
        processing_stats['last_poll_time'] = datetime.now().isoformat()
        
        # Small delay between pages
        time.sleep(0.5)
    
    log_with_timestamp(f"🎯 FINAL: Fetched {len(all_communications)} calls from {start_time} onwards")
    return all_communications

def _extract_agent_info(comm_obj: Dict) -> Dict:
    """Extract agent info from communication object"""
    agent_info = {
        'agent_name': 'Unknown',
        'call_direction': 'Unknown',
        'phone_number': 'Unknown',
        'duration_seconds': 0,
        'status': 'Unknown',
        'user_id': 'Unknown'
    }
    
    try:
        common_name = comm_obj.get('commonName', '')
        if common_name and '<-' in common_name:
            parts = common_name.split('<-')
            if len(parts) >= 2:
                agent_info['agent_name'] = parts[0].strip()
                phone_part = parts[1].split(',')[0].strip()
                agent_info['phone_number'] = phone_part
        
        incoming = comm_obj.get('incoming')
        if incoming is True:
            agent_info['call_direction'] = 'Incoming'
        elif incoming is False:
            agent_info['call_direction'] = 'Outgoing'
        
        duration_sec = comm_obj.get('durationSec')
        if duration_sec is not None:
            agent_info['duration_seconds'] = int(duration_sec)
        
        agent_info['status'] = comm_obj.get('status', 'Unknown')
        agent_info['user_id'] = comm_obj.get('id', 'Unknown')
            
    except Exception as e:
        log_error(f"Error extracting agent info for OID {comm_obj.get('oid', 'Unknown')}", e)
    
    return agent_info

def download_audio(communication_oid: str) -> Optional[str]:
    """Download audio with better error handling and retry logic"""
    audio_url = f"{XELION_BASE_URL.rstrip('/')}/communications/{communication_oid}/audio"
    file_name = f"{communication_oid}.mp3"
    file_path = os.path.join(AUDIO_TEMP_DIR, file_name)

    
    # Try multiple times with re-auth if needed
    for attempt in range(3):
        try:
            log_with_timestamp(f"🎵 Downloading audio for OID {communication_oid} (attempt {attempt + 1})")
            
            # Ensure we're authenticated
            global session_token
            if not session_token:
                log_with_timestamp(f"No session token, re-authenticating...")
                if not xelion_login():
                    log_with_timestamp(f"Re-auth failed for audio download")
                    continue
            
            response = xelion_session.get(audio_url, timeout=60)
            
            log_with_timestamp(f"Audio response: {response.status_code} for OID {communication_oid}")
            
            if response.status_code == 200:
                content_length = len(response.content)
                log_with_timestamp(f"Downloaded {content_length} bytes for OID {communication_oid}")
                
                if content_length > 10:  # Valid audio file
                    with open(file_path, 'wb') as f:
                        f.write(response.content)
                    log_with_timestamp(f"✅ Audio saved successfully for OID {communication_oid}")
                    return file_path
                else:
                    log_with_timestamp(f"❌ Audio too small ({content_length} bytes) for OID {communication_oid}")
                    return None
                    
            elif response.status_code == 404:
                log_with_timestamp(f"❌ No audio file exists for OID {communication_oid}")
                return None
                
            elif response.status_code == 401:
                log_with_timestamp(f"🔑 401 Auth error for OID {communication_oid}, retrying with fresh auth...")
                session_token = None  # Force re-auth
                if xelion_login():
                    continue  # Retry with fresh token
                else:
                    log_with_timestamp(f"❌ Re-auth failed for OID {communication_oid}")
                    return None
                    
            else:
                pass
                
        except Exception as e:
            log_error(f"Audio download attempt {attempt + 1} failed for {communication_oid}", e)
            if attempt == 2:  # Last attempt
                return None
            time.sleep(2)  # Wait before retry
    
    log_with_timestamp(f"❌ All audio download attempts failed for OID {communication_oid}")
    return None

# --- Deepgram Transcription ---
def transcribe_audio_deepgram(audio_file_path: str, metadata_row: Dict) -> Optional[Dict]:
    """Transcribe audio file using Deepgram direct API"""
    if not DEEPGRAM_API_KEY or DEEPGRAM_API_KEY == 'your_deepgram_api_key':
        log_error("Deepgram API key not configured")
        return None

    oid = metadata_row['oid']
    
    if not os.path.exists(audio_file_path):
        log_error(f"Audio file not found for transcription: {audio_file_path}")
        return None
    
    try:
        # Check file size
        file_size = os.path.getsize(audio_file_path)
        log_with_timestamp(f"🎵 Transcribing audio file: {file_size} bytes")
        
        if file_size < 10:
            log_error(f"Audio file too small: {file_size} bytes")
            return None
        
        # Use direct API
        url = "https://api.deepgram.com/v1/listen"
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "audio/mpeg"}
        params = {
            "model": "nova-2", 
            "smart_format": "true", 
            "punctuate": "true",
            "diarize": "true", 
            "utterances": "true", 
            "language": "en-GB"
        }
        
        log_with_timestamp(f"🎯 Transcribing OID {oid} using Deepgram API...")
        
        with open(audio_file_path, 'rb') as audio_file:
            response = requests.post(url, headers=headers, params=params, data=audio_file, timeout=120)
        
        log_with_timestamp(f"Deepgram response: {response.status_code}")
        
        if response.status_code != 200:
            log_error(f"Deepgram API error: {response.status_code} - {response.text}")
            return None
        
        result = response.json()
        
        # Validate response structure
        if 'results' not in result:
            log_error(f"Invalid response structure from Deepgram for OID {oid}: missing 'results'")
            return None
            
        if not result['results'].get('channels'):
            log_error(f"No channels in Deepgram response for OID {oid}")
            return None
            
        if not result['results']['channels'][0].get('alternatives'):
            log_error(f"No alternatives in Deepgram response for OID {oid}")
            return None
        
        transcript_data = result['results']['channels'][0]['alternatives'][0]
        transcript_text = transcript_data.get('transcript', '')
        
        # Get metadata
        metadata = result.get('metadata', {})
        duration_seconds = metadata.get('duration', 0)
        confidence = transcript_data.get('confidence', 0)
        language = metadata.get('detected_language', 'en')
        
        log_with_timestamp(f"✅ Deepgram transcription successful for OID {oid}")

        if not transcript_text.strip():
            log_with_timestamp(f"⚠️ Empty transcription for OID {oid}", "WARN")
            return None
        
        # Calculate costs
        duration_minutes = duration_seconds / 60
        cost_usd = duration_minutes * DEEPGRAM_PRICE_PER_MINUTE
        cost_gbp = cost_usd * USD_TO_GBP_RATE
        word_count = len(transcript_text.split())

        log_with_timestamp(f"📊 Transcription complete for OID {oid}: {word_count} words, {duration_minutes:.2f} minutes")

        return {
            'oid': oid,
            'transcription_text': transcript_text,
            'transcribed_duration_minutes': round(duration_minutes, 2),
            'deepgram_cost_usd': round(cost_usd, 4),
            'deepgram_cost_gbp': round(cost_gbp, 4),
            'word_count': word_count,
            'confidence': round(confidence, 3),
            'language': language
        }
            
    except Exception as e:
        log_error(f"Deepgram transcription failed for OID {oid}", e)
        return None

# --- OpenAI Analysis ---
def analyze_transcription_with_openai(transcript: str, oid: str = "unknown") -> Optional[Dict]:
    """Analyze transcription with OpenAI"""
    if not OPENAI_AVAILABLE:
        log_with_timestamp(f"OpenAI not available for OID {oid} - using fallback")
        # Return fallback scores
        import random
        random.seed(len(transcript))
        
        base_score = min(10, max(4, len(transcript.split()) / 50))
        
        return {
            "customer_engagement": {
                "score": round(base_score + random.uniform(-1, 1), 1),
                "active_listening": round(base_score + random.uniform(-1.5, 1.5), 1),
                "probing_questions": round(base_score + random.uniform(-1.5, 1.5), 1),
                "empathy_understanding": round(base_score + random.uniform(-1.5, 1.5), 1),
                "clarity_conciseness": round(base_score + random.uniform(-1.5, 1.5), 1)
            },
            "politeness": {
                "score": round(base_score + random.uniform(-0.5, 1.5), 1),
                "greeting_closing": round(base_score + random.uniform(-1, 2), 1),
                "tone_demeanor": round(base_score + random.uniform(-1, 1.5), 1),
                "respectful_language": round(base_score + random.uniform(-0.5, 1.5), 1),
                "handling_interruptions": round(base_score + random.uniform(-1.5, 1.5), 1)
            },
            "professional_knowledge": {
                "score": round(base_score + random.uniform(-1.5, 1), 1),
                "product_service_info": round(base_score + random.uniform(-2, 1), 1),
                "policy_adherence": round(base_score + random.uniform(-1, 1.5), 1),
                "problem_diagnosis": round(base_score + random.uniform(-1.5, 1.5), 1),
                "solution_offering": round(base_score + random.uniform(-1.5, 1.5), 1)
            },
            "customer_resolution": {
                "score": round(base_score + random.uniform(-1, 1.5), 1),
                "issue_identification": round(base_score + random.uniform(-1, 1.5), 1),
                "solution_effectiveness": round(base_score + random.uniform(-1.5, 1.5), 1),
                "time_to_resolution": round(base_score + random.uniform(-1, 1), 1),
                "follow_up_next_steps": round(base_score + random.uniform(-1.5, 1.5), 1)
            },
            "overall_score": round(base_score + random.uniform(-0.5, 1), 1),
            "summary": f"Fallback analysis - OpenAI not configured. {len(transcript.split())} words."
        }
        
    if not OPENAI_API_KEY or OPENAI_API_KEY == 'your_openai_api_key':
        log_error(f"OpenAI API key not configured for OID {oid}")
        return None
    
    # Truncate long transcripts
    if len(transcript) > 4000:
        transcript = transcript[:4000] + "... (truncated)"
    
    log_with_timestamp(f"🤖 Starting OpenAI analysis for OID {oid}")
    
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        
        prompt = f"""Analyze this customer service call and provide ratings 1-10:

Return ONLY valid JSON:
{{
    "customer_engagement": {{
        "score": 7,
        "active_listening": 7,
        "probing_questions": 6,
        "empathy_understanding": 8,
        "clarity_conciseness": 7
    }},
    "politeness": {{
        "score": 8,
        "greeting_closing": 9,
        "tone_demeanor": 8,
        "respectful_language": 8,
        "handling_interruptions": 7
    }},
    "professional_knowledge": {{
        "score": 6,
        "product_service_info": 6,
        "policy_adherence": 7,
        "problem_diagnosis": 5,
        "solution_offering": 6
    }},
    "customer_resolution": {{
        "score": 7,
        "issue_identification": 8,
        "solution_effectiveness": 6,
        "time_to_resolution": 7,
        "follow_up_next_steps": 6
    }},
    "overall_score": 7,
    "summary": "Brief call summary"
}}

Transcript: {transcript}"""
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a customer service analyzer. Return valid JSON with numeric scores 1-10."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            max_tokens=600,
            temperature=0.3
        )
        
        analysis_json = json.loads(response.choices[0].message.content)
        
        # Validate structure
        required_keys = ['customer_engagement', 'politeness', 'professional_knowledge', 'customer_resolution', 'overall_score']
        for key in required_keys:
            if key not in analysis_json:
                log_error(f"Missing key '{key}' in OpenAI response for OID {oid}")
                return None
        
        # Convert to numbers
        for category in ['customer_engagement', 'politeness', 'professional_knowledge', 'customer_resolution']:
            for subkey, value in analysis_json[category].items():
                try:
                    analysis_json[category][subkey] = float(value)
                except (ValueError, TypeError):
                    analysis_json[category][subkey] = 0.0
        
        try:
            analysis_json['overall_score'] = float(analysis_json['overall_score'])
        except (ValueError, TypeError):
            analysis_json['overall_score'] = 0.0
        
        if 'summary' not in analysis_json:
            analysis_json['summary'] = "Analysis completed."
        
        log_with_timestamp(f"✅ OpenAI analysis successful for OID {oid}")
        return analysis_json
        
    except Exception as e:
        log_error(f"OpenAI API error for OID {oid}", e)
        return None

def categorize_call(transcript: str) -> str:
    """Categorize calls based on keywords"""
    transcript_lower = transcript.lower()
    
    if "skip" in transcript_lower:
        return "SKIP"
    if "van" in transcript_lower or "driver" in transcript_lower or "collection" in transcript_lower:
        return "man in van"
    if "complaint" in transcript_lower or "unhappy" in transcript_lower or "dissatisfied" in transcript_lower:
        return "complaint"
    return "general enquiry"

def process_single_call(communication_data: Dict) -> Optional[str]:
    """Process single call ONLY if audio exists and call is <60 mins old"""
    comm_obj = communication_data.get('object', {})
    oid = comm_obj.get('oid')
    
    if not oid:
        processing_stats['total_skipped'] += 1
        return None

    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT oid FROM calls WHERE oid = ?", (oid,))
        if cursor.fetchone():
            conn.close()
            log_with_timestamp(f"⏭️ OID {oid} already in database, skipping")
            processing_stats['total_skipped'] += 1
            return None
        conn.close()

    call_datetime = comm_obj.get('date')
    if not call_datetime:
        processing_stats['total_skipped'] += 1
        return None
        
    try:
        if 'T' in call_datetime:
            call_dt = datetime.fromisoformat(call_datetime.replace('Z', '+00:00'))
        else:
            call_dt = datetime.strptime(call_datetime, '%Y-%m-%d %H:%M:%S')
            
        if (datetime.now() - call_dt) > timedelta(minutes=60):
            log_with_timestamp(f"⏭️ OID {oid} is older than 60 mins, skipping")
            processing_stats['total_skipped'] += 1
            return None
    except Exception as e:
        log_error(f"Error parsing datetime for OID {oid}", e)
        processing_stats['total_skipped'] += 1
        return None

    log_with_timestamp(f"🚀 Processing OID: {oid}")

    try:
        raw_data = json.dumps(communication_data)
        xelion_metadata = _extract_agent_info(comm_obj)
        call_datetime = comm_obj.get('date', 'Unknown')
        
        audio_file_path = download_audio(oid)
        if not audio_file_path:
            log_with_timestamp(f"🔄 OID {oid} has no audio yet, will retry")
            return None

        transcription_result = transcribe_audio_deepgram(audio_file_path, {'oid': oid})
        
        try:
            os.remove(audio_file_path)
            log_with_timestamp(f"🗑️ Deleted audio file: {audio_file_path}")
        except Exception as e:
            log_error(f"Error deleting audio file", e)

        if not transcription_result:
            processing_stats['total_errors'] += 1
            return None

        openai_analysis = analyze_transcription_with_openai(transcription_result['transcription_text'], oid)
        if not openai_analysis:
            openai_analysis = {
                "customer_engagement": {"score": 0, "active_listening": 0, "probing_questions": 0, "empathy_understanding": 0, "clarity_conciseness": 0},
                "politeness": {"score": 0, "greeting_closing": 0, "tone_demeanor": 0, "respectful_language": 0, "handling_interruptions": 0},
                "professional_knowledge": {"score": 0, "product_service_info": 0, "policy_adherence": 0, "problem_diagnosis": 0, "solution_offering": 0},
                "customer_resolution": {"score": 0, "issue_identification": 0, "solution_effectiveness": 0, "time_to_resolution": 0, "follow_up_next_steps": 0},
                "overall_score": 0,
                "summary": "OpenAI analysis failed"
            }

        call_category = categorize_call(transcription_result['transcription_text'])
        
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO calls (
                        oid, call_datetime, agent_name, phone_number, call_direction, 
                        duration_seconds, status, user_id, transcription_text, 
                        transcribed_duration_minutes, deepgram_cost_usd, deepgram_cost_gbp, 
                        word_count, confidence, language, processed_at, category,
                        openai_engagement, openai_politeness, openai_professionalism, openai_resolution, openai_overall_score,
                        engagement_sub1, engagement_sub2, engagement_sub3, engagement_sub4,
                        politeness_sub1, politeness_sub2, politeness_sub3, politeness_sub4,
                        professionalism_sub1, professionalism_sub2, professionalism_sub3, professionalism_sub4,
                        resolution_sub1, resolution_sub2, resolution_sub3, resolution_sub4, 
                        raw_communication_data, summary_translation
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    oid, call_datetime, xelion_metadata['agent_name'], xelion_metadata['phone_number'],
                    xelion_metadata['call_direction'], xelion_metadata['duration_seconds'], xelion_metadata['status'],
                    xelion_metadata['user_id'], transcription_result['transcription_text'],
                    transcription_result['transcribed_duration_minutes'], transcription_result['deepgram_cost_usd'],
                    transcription_result['deepgram_cost_gbp'], transcription_result['word_count'],
                    transcription_result['confidence'], transcription_result['language'], datetime.now().isoformat(),
                    call_category, openai_analysis.get('customer_engagement', {}).get('score', 0),
                    openai_analysis.get('politeness', {}).get('score', 0),
                    openai_analysis.get('professional_knowledge', {}).get('score', 0),
                    openai_analysis.get('customer_resolution', {}).get('score', 0),
                    openai_analysis.get('overall_score', 0),
                    openai_analysis.get('customer_engagement', {}).get('active_listening', 0),
                    openai_analysis.get('customer_engagement', {}).get('probing_questions', 0),
                    openai_analysis.get('customer_engagement', {}).get('empathy_understanding', 0),
                    openai_analysis.get('customer_engagement', {}).get('clarity_conciseness', 0),
                    openai_analysis.get('politeness', {}).get('greeting_closing', 0),
                    openai_analysis.get('politeness', {}).get('tone_demeanor', 0),
                    openai_analysis.get('politeness', {}).get('respectful_language', 0),
                    openai_analysis.get('politeness', {}).get('handling_interruptions', 0),
                    openai_analysis.get('professional_knowledge', {}).get('product_service_info', 0),
                    openai_analysis.get('professional_knowledge', {}).get('policy_adherence', 0),
                    openai_analysis.get('professional_knowledge', {}).get('problem_diagnosis', 0),
                    openai_analysis.get('professional_knowledge', {}).get('solution_offering', 0),
                    openai_analysis.get('customer_resolution', {}).get('issue_identification', 0),
                    openai_analysis.get('customer_resolution', {}).get('solution_effectiveness', 0),
                    openai_analysis.get('customer_resolution', {}).get('time_to_resolution', 0),
                    openai_analysis.get('customer_resolution', {}).get('follow_up_next_steps', 0),
                    raw_data, openai_analysis.get('summary', 'No summary')
                ))
                conn.commit()
                log_with_timestamp(f"✅ Successfully stored OID {oid} with transcription")
                processing_stats['total_processed'] += 1
                return oid
            except Exception as e:
                log_error(f"Database error storing OID {oid}", e)
                processing_stats['total_errors'] += 1
                return None
            finally:
                conn.close()
                
    except Exception as e:
        log_error(f"Unexpected error processing OID {oid}", e)
        processing_stats['total_errors'] += 1
        return None

def fetch_and_transcribe_recent_calls():
    """Continuous loop checking for new calls <60 mins old with audio"""
    global background_process_running
    background_process_running = True
    
    while background_process_running:
        try:
            time_window_start = datetime.now() - timedelta(minutes=60)
            log_with_timestamp(f"🔄 Polling for calls since {time_window_start}")
            
            if not session_token and not xelion_login():
                time.sleep(60)
                continue

            all_comms = fetch_all_communications_from_start_time(
                until_date=datetime.now(),
                start_time=time_window_start
            )
            
            if not all_comms:
                time.sleep(60)
                continue

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = []
                for comm in all_comms:
                    futures.append(executor.submit(process_single_call, comm))
                
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        log_error("Error in processing thread", e)

            time.sleep(60)
            
        except Exception as e:
            log_error("Monitoring loop error", e)
            time.sleep(60)

# --- Twilio Payment Functions ---
def get_twilio_client():
    """Initialize Twilio client"""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        log_error("Twilio credentials not configured")
        return None
    
    try:
        return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception as e:
        log_error("Failed to initialize Twilio client", e)
        return None

def generate_payment_twiml(amount: float, currency: str, payment_id: str, quote_id: str) -> str:
    """Generate TwiML for Braintree payment processing"""
    try:
        log_with_timestamp(f"🎯 Generating Braintree TwiML for payment {payment_id}, amount: {amount} {currency}")
        
        response = VoiceResponse()
        response.say("Please hold while we process your payment securely.", voice="alice")
        
        # Create Pay verb with Braintree connector
        pay = Pay(
            payment_connector=BRAINTREE_CONNECTOR_SID,
            charge_amount=str(amount),
            currency=currency,
            description="WasteKing Service Payment",
            action=f"{SERVER_BASE_URL}/twilio/payment-callback/{payment_id}"
        )
        
        # Add required parameters for Braintree
        pay.parameter(name="merchantAccountId", value=BRAINTREE_MERCHANT_ID)
        pay.parameter(name="name", value="WasteKing Service")
        pay.parameter(name="kind", value="sale")
        pay.parameter(name="quantity", value="1")
        pay.parameter(name="unitAmount", value=str(amount))
        pay.parameter(name="totalAmount", value=str(amount))
        
        response.append(pay)
        
        # Fallback message
        response.say("Thank you for your payment. Please hold while we complete your booking.", voice="alice")
        
        twiml_str = str(response)
        log_with_timestamp(f"✅ Generated TwiML for payment {payment_id}")
        log_with_timestamp(f"📄 TwiML content: {twiml_str[:200]}...")  # Log first 200 chars
        
        return twiml_str
        
    except Exception as e:
        log_error("Error generating payment TwiML", e)
        return None

def transfer_call_to_payment(call_sid: str, payment_twiml_url: str) -> bool:
    """Transfer active call to payment processing"""
    try:
        client = get_twilio_client()
        if not client:
            log_with_timestamp(f"❌ No Twilio client available for call transfer")
            return False
        
        log_with_timestamp(f"🔄 Attempting to transfer call {call_sid} to {payment_twiml_url}")
        
        # Update the call to execute payment TwiML
        call = client.calls(call_sid).update(
            url=payment_twiml_url,
            method='POST'  # Fixed: Use POST for TwiML endpoints
        )
        
        log_with_timestamp(f"✅ Call {call_sid} transferred to payment processing")
        return True
        
    except Exception as e:
        log_error(f"Failed to transfer call {call_sid} to payment", e)
        return False

def transfer_call_back_to_elevenlabs(call_sid: str, conversation_id: str = None) -> bool:
    """Transfer call back to ElevenLabs after payment"""
    try:
        client = get_twilio_client()
        if not client:
            return False
        
        # Create TwiML to transfer back to ElevenLabs
        if ELEVENLABS_WEBHOOK_URL and conversation_id:
            transfer_url = f"{ELEVENLABS_WEBHOOK_URL}?conversation_id={conversation_id}&payment_complete=true"
        else:
            # Fallback - just complete the call gracefully
            response = VoiceResponse()
            response.say("Your payment has been processed successfully. Your booking is now complete. Thank you for choosing WasteKing.", voice="alice")
            response.hangup()
            transfer_url = f"{SERVER_BASE_URL}/twilio/complete-call"
        
        call = client.calls(call_sid).update(
            url=transfer_url,
            method='POST'
        )
        
        log_with_timestamp(f"✅ Call {call_sid} transferred back to ElevenLabs")
        return True
        
    except Exception as e:
        log_error(f"Failed to transfer call {call_sid} back to ElevenLabs", e)
        return False

# --- Flask App Setup ---
app = Flask(__name__)
init_db()

# Test configurations on startup
log_with_timestamp("🚀 Application starting up...")
log_with_timestamp(f"Selenium available: {SELENIUM_AVAILABLE}")
log_with_timestamp(f"OpenAI available: {OPENAI_AVAILABLE}")

if OPENAI_AVAILABLE and OPENAI_API_KEY and OPENAI_API_KEY != 'your_openai_api_key':
    openai_test = test_openai_connection()
    log_with_timestamp(f"OpenAI test: {'✅ PASSED' if openai_test else '❌ FAILED'}")
else:
    log_with_timestamp("⚠️ OpenAI not configured")

# Test Twilio connection
twilio_client = get_twilio_client()
if twilio_client:
    log_with_timestamp("✅ Twilio client initialized")
else:
    log_with_timestamp("❌ Twilio client failed to initialize")

# --- Flask Routes ---
@app.route('/')
def index():
    """Auto-start monitoring"""
    global background_process_running, background_thread
    
    if not background_process_running:
        log_with_timestamp("🚀 AUTO-STARTING UNLIMITED monitoring")
        background_thread = threading.Thread(target=fetch_and_transcribe_recent_calls)
        background_thread.daemon = True
        background_thread.start()
        log_with_timestamp("✅ UNLIMITED Monitoring auto-started")
    
    return render_template('index.html')

@app.route('/status')
def get_status():
    """Get system status"""
    openai_test_result = test_openai_connection()
    twilio_test = get_twilio_client() is not None
    
    return jsonify({
        "background_running": background_process_running,
        "processing_stats": processing_stats,
        "selenium_available": SELENIUM_AVAILABLE,
        "openai_available": OPENAI_AVAILABLE,
        "openai_connection_test": openai_test_result,
        "openai_api_key_configured": OPENAI_API_KEY and OPENAI_API_KEY != 'your_openai_api_key',
        "wasteking_session_valid": load_wasteking_session() is not None,
        "twilio_configured": twilio_test,
        "braintree_merchant_id": BRAINTREE_MERCHANT_ID,
        "last_poll": processing_stats.get('last_poll_time', 'Never'),
        "last_error": processing_stats.get('last_error', 'None')
    })

@app.route('/get_dashboard_data')
def get_dashboard_data():
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Total counts - ALL CALLS IN DB HAVE AUDIO NOW
        cursor.execute("SELECT COUNT(*) FROM calls")
        total_calls = cursor.fetchone()[0]

        cursor.execute("SELECT SUM(deepgram_cost_gbp) FROM calls")
        total_cost_gbp = cursor.fetchone()[0] or 0.0

        cursor.execute("SELECT SUM(transcribed_duration_minutes) FROM calls")
        total_duration_minutes = cursor.fetchone()[0] or 0.0

        # Average ratings - ALL CALLS HAVE TRANSCRIPTIONS NOW
        cursor.execute("""
            SELECT AVG(openai_engagement), AVG(openai_politeness), 
                   AVG(openai_professionalism), AVG(openai_resolution)
            FROM calls
        """)
        avg_main_scores = cursor.fetchone()
        avg_engagement = round(avg_main_scores[0] or 0, 2)
        avg_politeness = round(avg_main_scores[1] or 0, 2)
        avg_professionalism = round(avg_main_scores[2] or 0, 2)
        avg_resolution = round(avg_main_scores[3] or 0, 2)

        # Sub-category averages
        cursor.execute("""
            SELECT AVG(engagement_sub1), AVG(engagement_sub2), 
                   AVG(engagement_sub3), AVG(engagement_sub4)
            FROM calls
        """)
        avg_engagement_subs = cursor.fetchone()
        engagement_subs = {
            "active_listening": round(avg_engagement_subs[0] or 0, 2),
            "probing_questions": round(avg_engagement_subs[1] or 0, 2),
            "empathy_understanding": round(avg_engagement_subs[2] or 0, 2),
            "clarity_conciseness": round(avg_engagement_subs[3] or 0, 2),
        }
        
        cursor.execute("""
            SELECT AVG(politeness_sub1), AVG(politeness_sub2), 
                   AVG(politeness_sub3), AVG(politeness_sub4)
            FROM calls
        """)
        avg_politeness_subs = cursor.fetchone()
        politeness_subs = {
            "greeting_closing": round(avg_politeness_subs[0] or 0, 2),
            "tone_demeanor": round(avg_politeness_subs[1] or 0, 2),
            "respectful_language": round(avg_politeness_subs[2] or 0, 2),
            "handling_interruptions": round(avg_politeness_subs[3] or 0, 2),
        }

        cursor.execute("""
            SELECT AVG(professionalism_sub1), AVG(professionalism_sub2), 
                   AVG(professionalism_sub3), AVG(professionalism_sub4)
            FROM calls
        """)
        avg_professionalism_subs = cursor.fetchone()
        professionalism_subs = {
            "product_service_info": round(avg_professionalism_subs[0] or 0, 2),
            "policy_adherence": round(avg_professionalism_subs[1] or 0, 2),
            "problem_diagnosis": round(avg_professionalism_subs[2] or 0, 2),
            "solution_offering": round(avg_professionalism_subs[3] or 0, 2),
        }

        cursor.execute("""
            SELECT AVG(resolution_sub1), AVG(resolution_sub2), 
                   AVG(resolution_sub3), AVG(resolution_sub4)
            FROM calls
        """)
        avg_resolution_subs = cursor.fetchone()
        resolution_subs = {
            "issue_identification": round(avg_resolution_subs[0] or 0, 2),
            "solution_effectiveness": round(avg_resolution_subs[1] or 0, 2),
            "time_to_resolution": round(avg_resolution_subs[2] or 0, 2),
            "follow_up_next_steps": round(avg_resolution_subs[3] or 0, 2),
        }

        # Category ratings - UPDATE CATEGORIES SINCE NO MORE "No Audio" CALLS
        category_ratings = {}
        categories = ["SKIP", "man in van", "general enquiry", "complaint"] 
        for cat in categories:
            cursor.execute("SELECT AVG(openai_overall_score), COUNT(*) FROM calls WHERE category = ?", (cat,))
            result = cursor.fetchone()
            avg_score = round(result[0] or 0, 2)
            count = result[1]
            category_ratings[cat] = {"average_score": avg_score, "count": count}

        conn.close()

        return jsonify({
            "total_calls": total_calls,
            "total_cost_gbp": round(total_cost_gbp, 2),
            "total_duration_minutes": round(total_duration_minutes, 2),
            "average_ratings": {
                "customer_engagement": avg_engagement,
                "politeness": avg_politeness,
                "professional_knowledge": avg_professionalism,
                "customer_resolution": avg_resolution
            },
            "sub_category_ratings": {
                "customer_engagement": engagement_subs,
                "politeness": politeness_subs,
                "professional_knowledge": professionalism_subs,
                "customer_resolution": resolution_subs
            },
            "category_call_ratings": category_ratings,
            "processing_stats": processing_stats
        })

@app.route('/get_calls_list')
def get_calls_list():
    """Get paginated calls list - ALL CALLS NOW HAVE AUDIO"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    agent_filter = request.args.get('agent', '')
    category_filter = request.args.get('category', '')
    
    offset = (page - 1) * per_page
    
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Build query with filters - NO AUDIO FILTER NEEDED
        where_conditions = []
        params = []
        
        if agent_filter:
            where_conditions.append("agent_name LIKE ?")
            params.append(f"%{agent_filter}%")
        
        if category_filter:
            where_conditions.append("category = ?")
            params.append(category_filter)
        
        where_clause = ""
        if where_conditions:
            where_clause = "WHERE " + " AND ".join(where_conditions)
        
        # Get total count
        count_query = f"SELECT COUNT(*) FROM calls {where_clause}"
        cursor.execute(count_query, params)
        total_calls = cursor.fetchone()[0]
        
        # Get calls - ORDER BY call_datetime DESC (latest first)
        query = f"""
            SELECT oid, call_datetime, agent_name, phone_number, call_direction, 
                   duration_seconds, status, category, openai_overall_score,
                   openai_engagement, openai_politeness, openai_professionalism, 
                   openai_resolution, summary_translation, transcribed_duration_minutes,
                   deepgram_cost_gbp, word_count, confidence, processed_at, transcription_text
            FROM calls 
            {where_clause}
            ORDER BY call_datetime DESC 
            LIMIT ? OFFSET ?
        """
        
        params.extend([per_page, offset])
        cursor.execute(query, params)
        
        calls = []
        for row in cursor.fetchall():
            call_data = dict(row)
            # Format duration
            if call_data['duration_seconds']:
                minutes = int(call_data['duration_seconds'] // 60)
                seconds = int(call_data['duration_seconds'] % 60)
                call_data['formatted_duration'] = f"{minutes}:{seconds:02d}"
            else:
                call_data['formatted_duration'] = "0:00"
            
            # Format datetime
            if call_data['call_datetime']:
                try:
                    dt = datetime.fromisoformat(call_data['call_datetime'].replace('Z', '+00:00'))
                    call_data['formatted_datetime'] = dt.strftime('%Y-%m-%d %H:%M')
                except:
                    call_data['formatted_datetime'] = call_data['call_datetime']
            
            calls.append(call_data)
        
        # Get filter options
        cursor.execute("SELECT DISTINCT agent_name FROM calls WHERE agent_name != 'Unknown' ORDER BY agent_name")
        agents = [row[0] for row in cursor.fetchall()]
        
        cursor.execute("SELECT DISTINCT category FROM calls WHERE category IS NOT NULL ORDER BY category")
        categories = [row[0] for row in cursor.fetchall()]
        
        conn.close()
        
        return jsonify({
            "calls": calls,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total_calls,
                "pages": (total_calls + per_page - 1) // per_page
            },
            "filters": {
                "agents": agents,
                "categories": categories
            }
        })

# --- PRICING AND PAYMENT ENDPOINTS ---

@app.route('/api/get-wasteking-prices', methods=['POST'])
def get_wasteking_prices_from_api():
    """
    Handles requests from Eleven Labs, fetches live pricing from WasteKing
    """
    log_with_timestamp("📞 ElevenLabs called WasteKing price endpoint")
    
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400
            
        postcode = data.get('postcode')
        service = data.get('service')
        
        log_with_timestamp(f"🔍 Price request - Postcode: {postcode}, Service: {service}")
        
        if not postcode or not service:
            return jsonify({
                "error": "Postcode and service are required",
                "required_fields": ["postcode", "service"]
            }), 400

        # Clean and validate postcode format
        postcode_clean = postcode.upper().replace(' ', '')
        
        # Basic UK postcode validation - COMMENTED OUT (was causing errors)
        # import re  # COMMENTED OUT - regex import
        # uk_postcode_pattern = r'^[A-Z]{1,2}[0-9][A-Z0-9]?[0-9][A-Z]{2}
        
        # if not re.match(uk_postcode_pattern, postcode_clean):  # COMMENTED OUT - regex match
        #     log_with_timestamp(f"❌ Invalid postcode format: {postcode}")
        #     return jsonify({
        #         "status": "error",
        #         "quote_id": str(uuid.uuid4()),
        #         "message": f"'{postcode}' is not a valid UK postcode. Please provide a valid postcode like 'LS1 4ED' or 'M1 1AA'.",
        #         "error": "Invalid postcode format",
        #         "example_postcodes": ["LS1 4ED", "M1 1AA", "EC1A 1BB"],
        #         "timestamp": datetime.now().isoformat()
        #     }), 400

        # Re-format postcode properly (add space if missing)
        if len(postcode_clean) >= 5:
            postcode = f"{postcode_clean[:-3]} {postcode_clean[-3:]}"
        else:
            postcode = postcode_clean

        # Headers for WasteKing API calls
        headers = {
            "x-wasteking-request": WASTEKING_ACCESS_TOKEN,
            "Content-Type": "application/json"
        }

        log_with_timestamp("📝 Step 1: Creating booking reference...")

        # Step 1: Create a BookingRef
        create_url = f"{WASTEKING_BASE_URL}api/booking/create/"
        create_payload = {
            "type": "chatbot",
            "source": "wasteking.co.uk"
        }
        
        log_with_timestamp(f"🌐 POST {create_url}")
        create_response = requests.post(create_url, headers=headers, json=create_payload, timeout=15, verify=False)
        
        log_with_timestamp(f"📊 Create response: {create_response.status_code}")
        
        if create_response.status_code != 200:
            log_with_timestamp(f"❌ Create failed: {create_response.text}")
            return jsonify({
                "error": f"Failed to create booking: {create_response.status_code}",
                "details": create_response.text
            }), 500

        create_data = create_response.json()
        booking_ref = create_data.get('bookingRef')

        if not booking_ref:
            log_with_timestamp(f"❌ No bookingRef in response: {create_data}")
            return jsonify({"error": "Failed to get booking reference"}), 500

        log_with_timestamp(f"✅ Got booking ref: {booking_ref}")
        log_with_timestamp("📝 Step 2: Updating booking with search...")

        # Step 2: Update the booking to perform a search
        # Map ElevenLabs service names to WasteKing API names (based on WasteKing call flow document)
        service_mapping = {
            # Skip Hire variations
            "skip hire": "skip",
            "skip": "skip",
            "skips": "skip",
            "skip rental": "skip",
            
            # Man & Van variations  
            "man and van": "man-in-van",
            "man in van": "man-in-van",
            "man & van": "man-in-van",
            "van": "man-in-van",
            "man with van": "man-in-van",
            "removal van": "man-in-van",
            
            # Grab Hire variations
            "grab hire": "grab",
            "grab": "grab",
            "grab lorry": "grab",
            "grab truck": "grab",
            
            # Clearance variations
            "house clearance": "clearance",
            "office clearance": "clearance",
            "clearance": "clearance",
            "property clearance": "clearance",
            "house clear": "clearance",
            "office clear": "clearance",
            
            # Waste removal general
            "waste removal": "removal",
            "removal": "removal",
            "rubbish removal": "removal",
            "rubbish collection": "removal",
            "waste collection": "removal",
            
            # Specialist services (these may need human transfer but try API first)
            "hazardous waste": "hazardous",
            "asbestos": "asbestos",
            "electrical waste": "weee",
            "weee": "weee",
            "chemical disposal": "chemical",
            
            # General fallbacks
            "waste": "removal",
            "rubbish": "removal"
        }
        
        # Convert service name with intelligent fallbacks
        wasteking_service = service_mapping.get(service.lower(), service.lower())
        log_with_timestamp(f"🔄 Mapped service '{service}' → '{wasteking_service}' for postcode {postcode}")
        
        update_url = f"{WASTEKING_BASE_URL}api/booking/update/"
        update_payload = {
            "bookingRef": booking_ref,
            "search": {
                "postCode": postcode,
                "service": wasteking_service
            }
        }
        
        log_with_timestamp(f"🌐 POST {update_url}")
        log_with_timestamp(f"📦 Payload: {update_payload}")
        
        update_response = requests.post(update_url, headers=headers, json=update_payload, timeout=20, verify=False)
        
        log_with_timestamp(f"📊 Update response: {update_response.status_code}")
        update_data = update_response.json()
        log_with_timestamp(f"📄 Update response body: {update_data}")

        # Generate quote ID regardless of search results
        quote_id = str(uuid.uuid4())
        customer_phone = data.get('customer_phone', 'Unknown')
        agent_name = data.get('agent_name', 'Agent')
        call_sid = data.get('call_sid', '')
        conversation_id = data.get('conversation_id', '')

        # Store the quote in database (even if no results found)
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO price_quotes (quote_id, booking_ref, postcode, service, price_data, created_at, customer_phone, agent_name, status, call_sid, elevenlabs_conversation_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    quote_id, booking_ref, postcode, service, json.dumps(update_data), 
                    datetime.now().isoformat(), customer_phone, agent_name, 
                    'no_results' if update_response.status_code != 200 else 'pending',
                    call_sid, conversation_id
                ))
                conn.commit()
                log_with_timestamp(f"📝 Stored quote {quote_id} in database")
            except Exception as e:
                log_error(f"Failed to store quote in database", e)
            finally:
                conn.close()

        # Handle different response scenarios
        if update_response.status_code == 200:
            # Success case
            return jsonify({
                "status": "success",
                "quote_id": quote_id,
                "bookingRef": booking_ref,
                "search_results": update_data,
                "postcode": postcode,
                "service": service,
                "timestamp": datetime.now().isoformat(),
                "message": "Price quote generated successfully. Agent can ask customer about payment."
            }), 200
            
        elif update_response.status_code == 404 or "No results found" in str(update_data):
            # No results found - provide helpful alternatives
            return jsonify({
                "status": "success",
                "quote_id": quote_id,
                "bookingRef": booking_ref,
                "search_results": update_data,
                "postcode": postcode,
                "service": service,
                "timestamp": datetime.now().isoformat(),
                "message": f"Sorry, we don't currently service {wasteking_service} in {postcode}. Try a nearby postcode or contact us directly.",
                "suggestions": [
                    "Check if you typed the postcode correctly",
                    "Try a nearby postcode in the same area",
                    f"We may offer other services in {postcode} - try 'skip', 'man-in-van', or 'clearance'",
                    "Contact us directly for special arrangements"
                ],
                "no_results": True
            }), 200
            
        else:
            # Other error
            log_with_timestamp(f"❌ Update failed: {update_data}")
            return jsonify({
                "status": "error",
                "quote_id": quote_id,
                "bookingRef": booking_ref,
                "error": f"Search failed: {update_response.status_code}",
                "details": update_data,
                "postcode": postcode,
                "service": service,
                "timestamp": datetime.now().isoformat(),
                "message": "Unable to get pricing at this time. Please contact us directly."
            }), 200  # Return 200 so ElevenLabs doesn't see it as error

    except requests.exceptions.Timeout:
        log_error("WasteKing API timeout")
        return jsonify({
            "error": "API timeout - please try again",
            "message": "Service temporarily unavailable. Please try again in a moment."
        }), 504
    except requests.exceptions.RequestException as e:
        log_error("WasteKing API request failed", e)
        return jsonify({
            "error": f"API request failed: {str(e)}",
            "message": "Unable to connect to pricing service. Please contact us directly."
        }), 500
    except Exception as e:
        log_error("Unexpected error in WasteKing API", e)
        return jsonify({
            "error": f"Unexpected error: {str(e)}",
            "message": "System error occurred. Please contact us directly."
        }), 500

@app.route('/api/request-payment', methods=['POST'])
def request_payment():
    """
    Modified to trigger Twilio call transfer for Braintree payment instead of PayPal
    """
    log_with_timestamp("💳 Braintree payment request received")
    
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        quote_id = data.get('quote_id')
        amount = data.get('amount')
        currency = data.get('currency', 'GBP')
        customer_phone = data.get('customer_phone', 'Unknown')
        call_sid = data.get('call_sid')  # Get current call SID from ElevenLabs

        # Generate payment ID first
        payment_id = str(uuid.uuid4())
        
        log_with_timestamp(f"💳 Braintree payment request for quote: {quote_id}, amount: {amount}, call_sid: {call_sid}")

        if not quote_id or not amount:
            return jsonify({
                "error": "Quote ID and amount are required",
                "required_fields": ["quote_id", "amount"]
            }), 400

        # Note: call_sid might be None if ElevenLabs doesn't provide it yet
        if not call_sid:
            log_with_timestamp(f"⚠️ Warning: No call_sid provided for payment {payment_id}")
            # For now, continue without call transfer - just create payment record
            call_sid = "pending_from_elevenlabs"

        # Store payment request in database
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                # Check if quote exists
                cursor.execute("SELECT booking_ref, status, elevenlabs_conversation_id FROM price_quotes WHERE quote_id = ?", (quote_id,))
                quote_row = cursor.fetchone()
                
                if not quote_row:
                    log_with_timestamp(f"❌ Quote not found: {quote_id}")
                    return jsonify({
                        "error": "Quote not found", 
                        "quote_id": quote_id,
                        "message": "Invalid quote ID. Please get a new price quote."
                    }), 404

                booking_ref = quote_row[0]
                quote_status = quote_row[1]
                conversation_id = quote_row[2]
                
                log_with_timestamp(f"✅ Found quote: {quote_id}, booking_ref: {booking_ref}, status: {quote_status}")

                # Insert payment request
                cursor.execute('''
                    INSERT INTO payments (payment_id, quote_id, booking_ref, amount, currency, created_at, customer_phone, call_sid)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    payment_id, quote_id, booking_ref, float(amount), currency, 
                    datetime.now().isoformat(), customer_phone, call_sid
                ))
                conn.commit()
                log_with_timestamp(f"💳 Created payment request {payment_id}")

                # Store conversation ID for later transfer back
                if conversation_id:
                    cursor.execute('''
                        UPDATE payments 
                        SET elevenlabs_conversation_id = ?
                        WHERE payment_id = ?
                    ''', (conversation_id, payment_id))
                    conn.commit()

                # FIXED: Actually transfer the call if call_sid is provided
                if call_sid and call_sid != "pending_from_elevenlabs":
                    payment_twiml_url = f"{SERVER_BASE_URL}/twilio/payment-twiml/{payment_id}"
                    
                    # Perform the actual call transfer
                    transfer_success = transfer_call_to_payment(call_sid, payment_twiml_url)
                    
                    if transfer_success:
                        log_with_timestamp(f"✅ Successfully transferred call {call_sid} to Braintree payment")
                        return jsonify({
                            "status": "success",
                            "payment_id": payment_id,
                            "quote_id": quote_id,
                            "amount": float(amount),
                            "currency": currency,
                            "call_transferred": True,
                            "call_sid": call_sid,
                            "message": f"Call transferred to Braintree for £{amount} payment processing.",
                            "timestamp": datetime.now().isoformat()
                        }), 200
                    else:
                        log_with_timestamp(f"❌ Failed to transfer call {call_sid} to payment")
                        return jsonify({
                            "error": "Call transfer failed",
                            "message": "Unable to transfer call to payment system. Please try again.",
                            "payment_id": payment_id
                        }), 500
                else:
                    # No call_sid provided - return payment URL for manual processing
                    payment_twiml_url = f"{SERVER_BASE_URL}/twilio/payment-twiml/{payment_id}"
                    return jsonify({
                        "status": "success",
                        "payment_id": payment_id,
                        "quote_id": quote_id,
                        "amount": float(amount),
                        "currency": currency,
                        "call_transferred": False,
                        "payment_twiml_url": payment_twiml_url,
                        "message": f"Payment request created. Call transfer requires call_sid parameter.",
                        "timestamp": datetime.now().isoformat()
                    }), 200

            except Exception as e:
                log_error(f"Failed to create payment request", e)
                return jsonify({
                    "error": "Database error",
                    "message": "Unable to process payment request. Please try again."
                }), 500
            finally:
                conn.close()

    except Exception as e:
        log_error("Error creating payment request", e)
        return jsonify({
            "error": f"Payment request failed: {str(e)}",
            "message": "Unable to process payment. Please contact us directly."
        }), 500

@app.route('/twilio/payment-twiml/<payment_id>', methods=['GET', 'POST'])
def get_payment_twiml(payment_id):
    """Serve TwiML for payment processing"""
    log_with_timestamp(f"📞 TwiML request received for payment {payment_id}")
    
    try:
        # Get payment details from database
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT amount, currency, quote_id FROM payments WHERE payment_id = ?", (payment_id,))
            payment_row = cursor.fetchone()
            conn.close()
        
        if not payment_row:
            log_with_timestamp(f"❌ Payment {payment_id} not found in database")
            # Return error TwiML
            response = VoiceResponse()
            response.say("We're sorry, but there was an error processing your payment. Please try again later.", voice="alice")
            response.hangup()
            return str(response), 200, {'Content-Type': 'text/xml'}
        
        amount = payment_row[0]
        currency = payment_row[1]
        quote_id = payment_row[2]
        
        log_with_timestamp(f"💳 Serving payment TwiML for £{amount} {currency}")
        
        # Generate and return payment TwiML
        payment_twiml = generate_payment_twiml(amount, currency, payment_id, quote_id)
        if payment_twiml:
            return payment_twiml, 200, {'Content-Type': 'text/xml'}
        else:
            # Fallback error TwiML
            log_with_timestamp(f"❌ Failed to generate TwiML for payment {payment_id}")
            response = VoiceResponse()
            response.say("We're sorry, but there was an error setting up your payment. Please try again later.", voice="alice")
            response.hangup()
            return str(response), 200, {'Content-Type': 'text/xml'}
            
    except Exception as e:
        log_error(f"Error serving payment TwiML for {payment_id}", e)
        response = VoiceResponse()
        response.say("We're sorry, but there was a system error. Please try again later.", voice="alice")
        response.hangup()
        return str(response), 200, {'Content-Type': 'text/xml'}

@app.route('/twilio/payment-callback/<payment_id>', methods=['POST'])
def payment_callback(payment_id):
    """Handle Braintree payment callback from Twilio"""
    log_with_timestamp(f"💳 Payment callback received for {payment_id}")
    
    try:
        # Get callback data from Twilio
        callback_data = request.form.to_dict()
        log_with_timestamp(f"📊 Payment callback data: {callback_data}")
        
        payment_result = callback_data.get('PaymentResult')
        payment_sid = callback_data.get('PaymentSid')
        transaction_id = callback_data.get('PaymentTransactionId', '')
        call_sid = callback_data.get('CallSid')
        
        # Update payment in database
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                if payment_result == 'completed':
                    # Payment successful
                    cursor.execute('''
                        UPDATE payments 
                        SET payment_status = 'completed', paid_at = ?, twilio_payment_sid = ?, braintree_transaction_id = ?
                        WHERE payment_id = ?
                    ''', (datetime.now().isoformat(), payment_sid, transaction_id, payment_id))
                    
                    # Also update quote status
                    cursor.execute('''
                        UPDATE price_quotes 
                        SET status = 'paid'
                        WHERE quote_id = (SELECT quote_id FROM payments WHERE payment_id = ?)
                    ''', (payment_id,))
                    
                    log_with_timestamp(f"✅ Payment {payment_id} completed successfully")
                    
                else:
                    # Payment failed or declined
                    cursor.execute('''
                        UPDATE payments 
                        SET payment_status = 'failed', twilio_payment_sid = ?
                        WHERE payment_id = ?
                    ''', (payment_sid, payment_id))
                    
                    log_with_timestamp(f"❌ Payment {payment_id} failed: {payment_result}")
                
                conn.commit()
                
                # Get conversation ID for transfer back
                cursor.execute("SELECT elevenlabs_conversation_id FROM payments WHERE payment_id = ?", (payment_id,))
                conversation_row = cursor.fetchone()
                conversation_id = conversation_row[0] if conversation_row else None
                
                conn.close()
                
                # Generate response TwiML based on payment result
                response = VoiceResponse()
                
                if payment_result == 'completed':
                    response.say("Thank you! Your payment has been processed successfully.", voice="alice")
                    response.pause(length=1)
                    response.say("Your booking is now complete. You will receive a confirmation shortly.", voice="alice")
                    
                    # Transfer back to ElevenLabs if possible
                    if call_sid:
                        # Use a slight delay before transfer
                        response.pause(length=2)
                        response.say("Please hold while we complete your booking.", voice="alice")
                        # Note: The actual transfer back happens in a separate thread to avoid blocking
                        threading.Thread(
                            target=transfer_call_back_to_elevenlabs, 
                            args=(call_sid, conversation_id)
                        ).start()
                    
                else:
                    response.say("We're sorry, but your payment could not be processed at this time.", voice="alice")
                    response.pause(length=1)
                    response.say("Please try again or contact us directly for assistance.", voice="alice")
                    
                    if call_sid:
                        # Transfer back to ElevenLabs for error handling
                        threading.Thread(
                            target=transfer_call_back_to_elevenlabs, 
                            args=(call_sid, conversation_id)
                        ).start()
                
                return str(response), 200, {'Content-Type': 'text/xml'}
                
            except Exception as e:
                log_error(f"Database error in payment callback", e)
                conn.close()
                
                # Return error TwiML
                response = VoiceResponse()
                response.say("We're sorry, but there was an error processing your payment. Please contact us directly.", voice="alice")
                return str(response), 200, {'Content-Type': 'text/xml'}
        
    except Exception as e:
        log_error(f"Error in payment callback for {payment_id}", e)
        
        # Return error TwiML
        response = VoiceResponse()
        response.say("We're sorry, but there was a system error. Please contact us directly.", voice="alice")
        return str(response), 200, {'Content-Type': 'text/xml'}

@app.route('/twilio/complete-call', methods=['GET', 'POST'])
def complete_call():
    """Fallback endpoint to complete calls gracefully"""
    response = VoiceResponse()
    response.say("Thank you for choosing WasteKing. Your booking has been processed. Have a great day!", voice="alice")
    response.hangup()
    return str(response), 200, {'Content-Type': 'text/xml'}

@app.route('/api/confirm-payment', methods=['POST'])
def confirm_payment():
    """
    Manual payment confirmation (kept for backward compatibility)
    """
    log_with_timestamp("✅ Manual payment confirmation received")
    
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        payment_id = data.get('payment_id')
        
        if not payment_id:
            return jsonify({"error": "Payment ID is required"}), 400

        log_with_timestamp(f"✅ Manually confirming payment: {payment_id}")

        # Update payment status
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    UPDATE payments 
                    SET payment_status = 'completed', paid_at = ?
                    WHERE payment_id = ?
                ''', (datetime.now().isoformat(), payment_id))
                
                if cursor.rowcount == 0:
                    log_with_timestamp(f"❌ Payment not found: {payment_id}")
                    return jsonify({
                        "error": "Payment not found",
                        "payment_id": payment_id,
                        "message": "Invalid payment ID."
                    }), 404

                # Also update the quote status
                cursor.execute('''
                    UPDATE price_quotes 
                    SET status = 'paid'
                    WHERE quote_id = (SELECT quote_id FROM payments WHERE payment_id = ?)
                ''', (payment_id,))

                conn.commit()
                log_with_timestamp(f"✅ Payment {payment_id} manually confirmed")

                return jsonify({
                    "status": "success",
                    "payment_id": payment_id,
                    "message": "Payment confirmed successfully! Booking is now complete.",
                    "timestamp": datetime.now().isoformat()
                }), 200

            except Exception as e:
                log_error(f"Failed to update payment status", e)
                return jsonify({
                    "error": "Database error",
                    "message": "Unable to confirm payment. Please contact us."
                }), 500
            finally:
                conn.close()

    except Exception as e:
        log_error("Error confirming payment", e)
        return jsonify({
            "error": f"Payment confirmation failed: {str(e)}",
            "message": "Unable to confirm payment. Please contact us."
        }), 500

@app.route('/api/get-quotes', methods=['GET'])
def get_quotes():
    """Get all quotes and their payment status"""
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT q.quote_id, q.booking_ref, q.postcode, q.service, q.created_at, 
                       q.status, q.customer_phone, q.agent_name, q.call_sid, q.elevenlabs_conversation_id,
                       p.payment_id, p.amount, p.currency, p.payment_status, p.paid_at,
                       p.twilio_payment_sid, p.braintree_transaction_id
                FROM price_quotes q
                LEFT JOIN payments p ON q.quote_id = p.quote_id
                ORDER BY q.created_at DESC
            ''')
            
            quotes = []
            for row in cursor.fetchall():
                quote = dict(row)
                quotes.append(quote)
            
            conn.close()
            
            return jsonify({
                "status": "success",
                "quotes": quotes,
                "count": len(quotes),
                "timestamp": datetime.now().isoformat()
            })

    except Exception as e:
        log_error("Error fetching quotes", e)
        return jsonify({"error": f"Failed to fetch quotes: {str(e)}"}), 500

@app.route('/api/get-payments', methods=['GET'])
def get_payments():
    """Get all payment records"""
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT p.payment_id, p.quote_id, p.booking_ref, p.amount, p.currency,
                       p.payment_status, p.created_at, p.paid_at, p.customer_phone,
                       p.twilio_payment_sid, p.braintree_transaction_id, p.call_sid,
                       q.postcode, q.service, q.agent_name
                FROM payments p
                LEFT JOIN price_quotes q ON p.quote_id = q.quote_id
                ORDER BY p.created_at DESC
            ''')
            
            payments = []
            for row in cursor.fetchall():
                payment = dict(row)
                payments.append(payment)
            
            conn.close()
            
            return jsonify({
                "status": "success",
                "payments": payments,
                "count": len(payments),
                "timestamp": datetime.now().isoformat()
            })

    except Exception as e:
        log_error("Error fetching payments", e)
        return jsonify({"error": f"Failed to fetch payments: {str(e)}"}), 500

@app.route('/api/setup-wasteking', methods=['POST'])
def setup_wasteking_session():
    """Setup WasteKing authentication"""
    try:
        log_with_timestamp("🚀 Starting WasteKing setup...")
        auth_result = authenticate_wasteking()
        
        if isinstance(auth_result, dict) and "error" in auth_result:
            return jsonify(auth_result), 500
        
        if auth_result:
            return jsonify({
                "status": "success",
                "message": "WasteKing authentication successful!",
                "timestamp": datetime.now().isoformat()
            })
        else:
            return jsonify({
                "status": "error",
                "message": "Authentication failed",
                "timestamp": datetime.now().isoformat()
            }), 500
            
    except Exception as e:
        log_error("WasteKing setup error", e)
        return jsonify({
            "status": "error",
            "message": f"Setup failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/api/test-twilio', methods=['POST'])
def test_twilio_connection():
    """Test Twilio connection and Braintree configuration"""
    try:
        client = get_twilio_client()
        if not client:
            return jsonify({
                "status": "error",
                "message": "Failed to initialize Twilio client"
            }), 500
        
        # Test account info
        account = client.api.accounts(TWILIO_ACCOUNT_SID).fetch()
        
        # Test payment connector
        connectors = client.trusthub.v1.payment_connectors.list(limit=20)
        braintree_connector_found = any(c.sid == BRAINTREE_CONNECTOR_SID for c in connectors)
        
        return jsonify({
            "status": "success",
            "message": "Twilio connection successful",
            "account_status": account.status,
            "account_friendly_name": account.friendly_name,
            "braintree_connector_configured": braintree_connector_found,
            "braintree_connector_sid": BRAINTREE_CONNECTOR_SID,
            "braintree_merchant_id": BRAINTREE_MERCHANT_ID,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        log_error("Error testing Twilio connection", e)
        return jsonify({
            "status": "error",
            "message": f"Twilio test failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/api/test-call-transfer', methods=['POST'])
def test_call_transfer():
    """Test endpoint to verify call transfer functionality"""
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400
            
        call_sid = data.get('call_sid')
        if not call_sid:
            return jsonify({"error": "call_sid is required"}), 400
        
        # Create a test payment for transfer testing
        test_payment_id = str(uuid.uuid4())
        test_twiml_url = f"{SERVER_BASE_URL}/twilio/payment-twiml/{test_payment_id}"
        
        log_with_timestamp(f"🧪 Testing call transfer for {call_sid}")
        
        # Store test payment in database
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO payments (payment_id, quote_id, booking_ref, amount, currency, created_at, customer_phone, call_sid, payment_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                test_payment_id, 'test-quote', 'test-booking', 10.00, 'GBP', 
                datetime.now().isoformat(), 'test-phone', call_sid, 'test'
            ))
            conn.commit()
            conn.close()
        
        # Attempt the transfer
        transfer_success = transfer_call_to_payment(call_sid, test_twiml_url)
        
        return jsonify({
            "status": "success" if transfer_success else "failed",
            "call_sid": call_sid,
            "test_payment_id": test_payment_id,
            "transfer_url": test_twiml_url,
            "transfer_successful": transfer_success,
            "message": "Transfer successful" if transfer_success else "Transfer failed",
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        log_error("Call transfer test failed", e)
        return jsonify({
            "error": f"Test failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }), 500

if __name__ == '__main__':
    # Development only - production uses gunicorn
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)  # COMMENTED OUT - regex pattern
        
        # if not re.match(uk_postcode_pattern, postcode_clean):  # COMMENTED OUT - regex match
        #     log_with_timestamp(f"❌ Invalid postcode format: {postcode}")
        #     return jsonify({
        #         "status": "error",
        #         "quote_id": str(uuid.uuid4()),
        #         "message": f"'{postcode}' is not a valid UK postcode. Please provide a valid postcode like 'LS1 4ED' or 'M1 1AA'.",
        #         "error": "Invalid postcode format",
        #         "example_postcodes": ["LS1 4ED", "M1 1AA", "EC1A 1BB"],
        #         "timestamp": datetime.now().isoformat()
        #     }), 400

        # Re-format postcode properly (add space if missing)
        if len(postcode_clean) >= 5:
            postcode = f"{postcode_clean[:-3]} {postcode_clean[-3:]}"
        else:
            postcode = postcode_clean

        # Headers for WasteKing API calls
        headers = {
            "x-wasteking-request": WASTEKING_ACCESS_TOKEN,
            "Content-Type": "application/json"
        }

        log_with_timestamp("📝 Step 1: Creating booking reference...")

        # Step 1: Create a BookingRef
        create_url = f"{WASTEKING_BASE_URL}api/booking/create/"
        create_payload = {
            "type": "chatbot",
            "source": "wasteking.co.uk"
        }
        
        log_with_timestamp(f"🌐 POST {create_url}")
        create_response = requests.post(create_url, headers=headers, json=create_payload, timeout=15, verify=False)
        
        log_with_timestamp(f"📊 Create response: {create_response.status_code}")
        
        if create_response.status_code != 200:
            log_with_timestamp(f"❌ Create failed: {create_response.text}")
            return jsonify({
                "error": f"Failed to create booking: {create_response.status_code}",
                "details": create_response.text
            }), 500

        create_data = create_response.json()
        booking_ref = create_data.get('bookingRef')

        if not booking_ref:
            log_with_timestamp(f"❌ No bookingRef in response: {create_data}")
            return jsonify({"error": "Failed to get booking reference"}), 500

        log_with_timestamp(f"✅ Got booking ref: {booking_ref}")
        log_with_timestamp("📝 Step 2: Updating booking with search...")

        # Step 2: Update the booking to perform a search
        # Map ElevenLabs service names to WasteKing API names (based on WasteKing call flow document)
        service_mapping = {
            # Skip Hire variations
            "skip hire": "skip",
            "skip": "skip",
            "skips": "skip",
            "skip rental": "skip",
            
            # Man & Van variations  
            "man and van": "man-in-van",
            "man in van": "man-in-van",
            "man & van": "man-in-van",
            "van": "man-in-van",
            "man with van": "man-in-van",
            "removal van": "man-in-van",
            
            # Grab Hire variations
            "grab hire": "grab",
            "grab": "grab",
            "grab lorry": "grab",
            "grab truck": "grab",
            
            # Clearance variations
            "house clearance": "clearance",
            "office clearance": "clearance",
            "clearance": "clearance",
            "property clearance": "clearance",
            "house clear": "clearance",
            "office clear": "clearance",
            
            # Waste removal general
            "waste removal": "removal",
            "removal": "removal",
            "rubbish removal": "removal",
            "rubbish collection": "removal",
            "waste collection": "removal",
            
            # Specialist services (these may need human transfer but try API first)
            "hazardous waste": "hazardous",
            "asbestos": "asbestos",
            "electrical waste": "weee",
            "weee": "weee",
            "chemical disposal": "chemical",
            
            # General fallbacks
            "waste": "removal",
            "rubbish": "removal"
        }
        
        # Convert service name with intelligent fallbacks
        wasteking_service = service_mapping.get(service.lower(), service.lower())
        log_with_timestamp(f"🔄 Mapped service '{service}' → '{wasteking_service}' for postcode {postcode}")
        
        update_url = f"{WASTEKING_BASE_URL}api/booking/update/"
        update_payload = {
            "bookingRef": booking_ref,
            "search": {
                "postCode": postcode,
                "service": wasteking_service
            }
        }
        
        log_with_timestamp(f"🌐 POST {update_url}")
        log_with_timestamp(f"📦 Payload: {update_payload}")
        
        update_response = requests.post(update_url, headers=headers, json=update_payload, timeout=20, verify=False)
        
        log_with_timestamp(f"📊 Update response: {update_response.status_code}")
        update_data = update_response.json()
        log_with_timestamp(f"📄 Update response body: {update_data}")

        # Generate quote ID regardless of search results
        quote_id = str(uuid.uuid4())
        customer_phone = data.get('customer_phone', 'Unknown')
        agent_name = data.get('agent_name', 'Agent')
        call_sid = data.get('call_sid', '')
        conversation_id = data.get('conversation_id', '')

        # Store the quote in database (even if no results found)
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO price_quotes (quote_id, booking_ref, postcode, service, price_data, created_at, customer_phone, agent_name, status, call_sid, elevenlabs_conversation_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    quote_id, booking_ref, postcode, service, json.dumps(update_data), 
                    datetime.now().isoformat(), customer_phone, agent_name, 
                    'no_results' if update_response.status_code != 200 else 'pending',
                    call_sid, conversation_id
                ))
                conn.commit()
                log_with_timestamp(f"📝 Stored quote {quote_id} in database")
            except Exception as e:
                log_error(f"Failed to store quote in database", e)
            finally:
                conn.close()

        # Handle different response scenarios
        if update_response.status_code == 200:
            # Success case
            return jsonify({
                "status": "success",
                "quote_id": quote_id,
                "bookingRef": booking_ref,
                "search_results": update_data,
                "postcode": postcode,
                "service": service,
                "timestamp": datetime.now().isoformat(),
                "message": "Price quote generated successfully. Agent can ask customer about payment."
            }), 200
            
        elif update_response.status_code == 404 or "No results found" in str(update_data):
            # No results found - provide helpful alternatives
            return jsonify({
                "status": "success",
                "quote_id": quote_id,
                "bookingRef": booking_ref,
                "search_results": update_data,
                "postcode": postcode,
                "service": service,
                "timestamp": datetime.now().isoformat(),
                "message": f"Sorry, we don't currently service {wasteking_service} in {postcode}. Try a nearby postcode or contact us directly.",
                "suggestions": [
                    "Check if you typed the postcode correctly",
                    "Try a nearby postcode in the same area",
                    f"We may offer other services in {postcode} - try 'skip', 'man-in-van', or 'clearance'",
                    "Contact us directly for special arrangements"
                ],
                "no_results": True
            }), 200
            
        else:
            # Other error
            log_with_timestamp(f"❌ Update failed: {update_data}")
            return jsonify({
                "status": "error",
                "quote_id": quote_id,
                "bookingRef": booking_ref,
                "error": f"Search failed: {update_response.status_code}",
                "details": update_data,
                "postcode": postcode,
                "service": service,
                "timestamp": datetime.now().isoformat(),
                "message": "Unable to get pricing at this time. Please contact us directly."
            }), 200  # Return 200 so ElevenLabs doesn't see it as error

    except requests.exceptions.Timeout:
        log_error("WasteKing API timeout")
        return jsonify({
            "error": "API timeout - please try again",
            "message": "Service temporarily unavailable. Please try again in a moment."
        }), 504
    except requests.exceptions.RequestException as e:
        log_error("WasteKing API request failed", e)
        return jsonify({
            "error": f"API request failed: {str(e)}",
            "message": "Unable to connect to pricing service. Please contact us directly."
        }), 500
    except Exception as e:
        log_error("Unexpected error in WasteKing API", e)
        return jsonify({
            "error": f"Unexpected error: {str(e)}",
            "message": "System error occurred. Please contact us directly."
        }), 500

@app.route('/api/request-payment', methods=['POST'])
def request_payment():
    """
    Modified to trigger Twilio call transfer for Braintree payment instead of PayPal
    """
    log_with_timestamp("💳 Braintree payment request received")
    
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        quote_id = data.get('quote_id')
        amount = data.get('amount')
        currency = data.get('currency', 'GBP')
        customer_phone = data.get('customer_phone', 'Unknown')
        call_sid = data.get('call_sid')  # Get current call SID from ElevenLabs

        # Generate payment ID first
        payment_id = str(uuid.uuid4())
        
        log_with_timestamp(f"💳 Braintree payment request for quote: {quote_id}, amount: {amount}, call_sid: {call_sid}")

        if not quote_id or not amount:
            return jsonify({
                "error": "Quote ID and amount are required",
                "required_fields": ["quote_id", "amount"]
            }), 400

        # Note: call_sid might be None if ElevenLabs doesn't provide it yet
        if not call_sid:
            log_with_timestamp(f"⚠️ Warning: No call_sid provided for payment {payment_id}")
            # For now, continue without call transfer - just create payment record
            call_sid = "pending_from_elevenlabs"

        # Store payment request in database
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                # Check if quote exists
                cursor.execute("SELECT booking_ref, status, elevenlabs_conversation_id FROM price_quotes WHERE quote_id = ?", (quote_id,))
                quote_row = cursor.fetchone()
                
                if not quote_row:
                    log_with_timestamp(f"❌ Quote not found: {quote_id}")
                    return jsonify({
                        "error": "Quote not found", 
                        "quote_id": quote_id,
                        "message": "Invalid quote ID. Please get a new price quote."
                    }), 404

                booking_ref = quote_row[0]
                quote_status = quote_row[1]
                conversation_id = quote_row[2]
                
                log_with_timestamp(f"✅ Found quote: {quote_id}, booking_ref: {booking_ref}, status: {quote_status}")

                # Insert payment request
                cursor.execute('''
                    INSERT INTO payments (payment_id, quote_id, booking_ref, amount, currency, created_at, customer_phone, call_sid)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    payment_id, quote_id, booking_ref, float(amount), currency, 
                    datetime.now().isoformat(), customer_phone, call_sid
                ))
                conn.commit()
                log_with_timestamp(f"💳 Created payment request {payment_id}")

                # Generate TwiML for payment processing
                payment_twiml = generate_payment_twiml(float(amount), currency, payment_id, quote_id)
                if not payment_twiml:
                    return jsonify({
                        "error": "Failed to generate payment flow",
                        "message": "Unable to process payment. Please try again."
                    }), 500

                # Store conversation ID for later transfer back
                if conversation_id:
                    cursor.execute('''
                        UPDATE payments 
                        SET elevenlabs_conversation_id = ?
                        WHERE payment_id = ?
                    ''', (conversation_id, payment_id))
                    conn.commit()

                return jsonify({
                    "status": "success",
                    "payment_id": payment_id,
                    "quote_id": quote_id,
                    "amount": float(amount),
                    "currency": currency,
                    "call_transfer_initiated": True,
                    "payment_twiml_url": f"{SERVER_BASE_URL}/twilio/payment-twiml/{payment_id}",
                    "message": f"Payment processing initiated. Customer will be transferred to secure payment system.",
                    "timestamp": datetime.now().isoformat()
                }), 200

            except Exception as e:
                log_error(f"Failed to create payment request", e)
                return jsonify({
                    "error": "Database error",
                    "message": "Unable to process payment request. Please try again."
                }), 500
            finally:
                conn.close()

    except Exception as e:
        log_error("Error creating payment request", e)
        return jsonify({
            "error": f"Payment request failed: {str(e)}",
            "message": "Unable to process payment. Please contact us directly."
        }), 500

@app.route('/twilio/payment-twiml/<payment_id>', methods=['GET'])
def get_payment_twiml(payment_id):
    """Serve TwiML for payment processing"""
    try:
        # Get payment details from database
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT amount, currency, quote_id FROM payments WHERE payment_id = ?", (payment_id,))
            payment_row = cursor.fetchone()
            conn.close()
        
        if not payment_row:
            # Return error TwiML
            response = VoiceResponse()
            response.say("We're sorry, but there was an error processing your payment. Please try again later.", voice="alice")
            response.hangup()
            return str(response), 200, {'Content-Type': 'text/xml'}
        
        amount = payment_row[0]
        currency = payment_row[1]
        quote_id = payment_row[2]
        
        # Generate and return payment TwiML
        payment_twiml = generate_payment_twiml(amount, currency, payment_id, quote_id)
        if payment_twiml:
            return payment_twiml, 200, {'Content-Type': 'text/xml'}
        else:
            # Fallback error TwiML
            response = VoiceResponse()
            response.say("We're sorry, but there was an error setting up your payment. Please try again later.", voice="alice")
            response.hangup()
            return str(response), 200, {'Content-Type': 'text/xml'}
            
    except Exception as e:
        log_error(f"Error serving payment TwiML for {payment_id}", e)
        response = VoiceResponse()
        response.say("We're sorry, but there was a system error. Please try again later.", voice="alice")
        response.hangup()
        return str(response), 200, {'Content-Type': 'text/xml'}

@app.route('/twilio/payment-callback/<payment_id>', methods=['POST'])
def payment_callback(payment_id):
    """Handle Braintree payment callback from Twilio"""
    log_with_timestamp(f"💳 Payment callback received for {payment_id}")
    
    try:
        # Get callback data from Twilio
        callback_data = request.form.to_dict()
        log_with_timestamp(f"📊 Payment callback data: {callback_data}")
        
        payment_result = callback_data.get('PaymentResult')
        payment_sid = callback_data.get('PaymentSid')
        transaction_id = callback_data.get('PaymentTransactionId', '')
        call_sid = callback_data.get('CallSid')
        
        # Update payment in database
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                if payment_result == 'completed':
                    # Payment successful
                    cursor.execute('''
                        UPDATE payments 
                        SET payment_status = 'completed', paid_at = ?, twilio_payment_sid = ?, braintree_transaction_id = ?
                        WHERE payment_id = ?
                    ''', (datetime.now().isoformat(), payment_sid, transaction_id, payment_id))
                    
                    # Also update quote status
                    cursor.execute('''
                        UPDATE price_quotes 
                        SET status = 'paid'
                        WHERE quote_id = (SELECT quote_id FROM payments WHERE payment_id = ?)
                    ''', (payment_id,))
                    
                    log_with_timestamp(f"✅ Payment {payment_id} completed successfully")
                    
                else:
                    # Payment failed or declined
                    cursor.execute('''
                        UPDATE payments 
                        SET payment_status = 'failed', twilio_payment_sid = ?
                        WHERE payment_id = ?
                    ''', (payment_sid, payment_id))
                    
                    log_with_timestamp(f"❌ Payment {payment_id} failed: {payment_result}")
                
                conn.commit()
                
                # Get conversation ID for transfer back
                cursor.execute("SELECT elevenlabs_conversation_id FROM payments WHERE payment_id = ?", (payment_id,))
                conversation_row = cursor.fetchone()
                conversation_id = conversation_row[0] if conversation_row else None
                
                conn.close()
                
                # Generate response TwiML based on payment result
                response = VoiceResponse()
                
                if payment_result == 'completed':
                    response.say("Thank you! Your payment has been processed successfully.", voice="alice")
                    response.pause(length=1)
                    response.say("Your booking is now complete. You will receive a confirmation shortly.", voice="alice")
                    
                    # Transfer back to ElevenLabs if possible
                    if call_sid:
                        # Use a slight delay before transfer
                        response.pause(length=2)
                        response.say("Please hold while we complete your booking.", voice="alice")
                        # Note: The actual transfer back happens in a separate thread to avoid blocking
                        threading.Thread(
                            target=transfer_call_back_to_elevenlabs, 
                            args=(call_sid, conversation_id)
                        ).start()
                    
                else:
                    response.say("We're sorry, but your payment could not be processed at this time.", voice="alice")
                    response.pause(length=1)
                    response.say("Please try again or contact us directly for assistance.", voice="alice")
                    
                    if call_sid:
                        # Transfer back to ElevenLabs for error handling
                        threading.Thread(
                            target=transfer_call_back_to_elevenlabs, 
                            args=(call_sid, conversation_id)
                        ).start()
                
                return str(response), 200, {'Content-Type': 'text/xml'}
                
            except Exception as e:
                log_error(f"Database error in payment callback", e)
                conn.close()
                
                # Return error TwiML
                response = VoiceResponse()
                response.say("We're sorry, but there was an error processing your payment. Please contact us directly.", voice="alice")
                return str(response), 200, {'Content-Type': 'text/xml'}
        
    except Exception as e:
        log_error(f"Error in payment callback for {payment_id}", e)
        
        # Return error TwiML
        response = VoiceResponse()
        response.say("We're sorry, but there was a system error. Please contact us directly.", voice="alice")
        return str(response), 200, {'Content-Type': 'text/xml'}

@app.route('/twilio/complete-call', methods=['GET', 'POST'])
def complete_call():
    """Fallback endpoint to complete calls gracefully"""
    response = VoiceResponse()
    response.say("Thank you for choosing WasteKing. Your booking has been processed. Have a great day!", voice="alice")
    response.hangup()
    return str(response), 200, {'Content-Type': 'text/xml'}

@app.route('/api/confirm-payment', methods=['POST'])
def confirm_payment():
    """
    Manual payment confirmation (kept for backward compatibility)
    """
    log_with_timestamp("✅ Manual payment confirmation received")
    
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        payment_id = data.get('payment_id')
        
        if not payment_id:
            return jsonify({"error": "Payment ID is required"}), 400

        log_with_timestamp(f"✅ Manually confirming payment: {payment_id}")

        # Update payment status
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    UPDATE payments 
                    SET payment_status = 'completed', paid_at = ?
                    WHERE payment_id = ?
                ''', (datetime.now().isoformat(), payment_id))
                
                if cursor.rowcount == 0:
                    log_with_timestamp(f"❌ Payment not found: {payment_id}")
                    return jsonify({
                        "error": "Payment not found",
                        "payment_id": payment_id,
                        "message": "Invalid payment ID."
                    }), 404

                # Also update the quote status
                cursor.execute('''
                    UPDATE price_quotes 
                    SET status = 'paid'
                    WHERE quote_id = (SELECT quote_id FROM payments WHERE payment_id = ?)
                ''', (payment_id,))

                conn.commit()
                log_with_timestamp(f"✅ Payment {payment_id} manually confirmed")

                return jsonify({
                    "status": "success",
                    "payment_id": payment_id,
                    "message": "Payment confirmed successfully! Booking is now complete.",
                    "timestamp": datetime.now().isoformat()
                }), 200

            except Exception as e:
                log_error(f"Failed to update payment status", e)
                return jsonify({
                    "error": "Database error",
                    "message": "Unable to confirm payment. Please contact us."
                }), 500
            finally:
                conn.close()

    except Exception as e:
        log_error("Error confirming payment", e)
        return jsonify({
            "error": f"Payment confirmation failed: {str(e)}",
            "message": "Unable to confirm payment. Please contact us."
        }), 500

@app.route('/api/get-quotes', methods=['GET'])
def get_quotes():
    """Get all quotes and their payment status"""
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT q.quote_id, q.booking_ref, q.postcode, q.service, q.created_at, 
                       q.status, q.customer_phone, q.agent_name, q.call_sid, q.elevenlabs_conversation_id,
                       p.payment_id, p.amount, p.currency, p.payment_status, p.paid_at,
                       p.twilio_payment_sid, p.braintree_transaction_id
                FROM price_quotes q
                LEFT JOIN payments p ON q.quote_id = p.quote_id
                ORDER BY q.created_at DESC
            ''')
            
            quotes = []
            for row in cursor.fetchall():
                quote = dict(row)
                quotes.append(quote)
            
            conn.close()
            
            return jsonify({
                "status": "success",
                "quotes": quotes,
                "count": len(quotes),
                "timestamp": datetime.now().isoformat()
            })

    except Exception as e:
        log_error("Error fetching quotes", e)
        return jsonify({"error": f"Failed to fetch quotes: {str(e)}"}), 500

@app.route('/api/get-payments', methods=['GET'])
def get_payments():
    """Get all payment records"""
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT p.payment_id, p.quote_id, p.booking_ref, p.amount, p.currency,
                       p.payment_status, p.created_at, p.paid_at, p.customer_phone,
                       p.twilio_payment_sid, p.braintree_transaction_id, p.call_sid,
                       q.postcode, q.service, q.agent_name
                FROM payments p
                LEFT JOIN price_quotes q ON p.quote_id = q.quote_id
                ORDER BY p.created_at DESC
            ''')
            
            payments = []
            for row in cursor.fetchall():
                payment = dict(row)
                payments.append(payment)
            
            conn.close()
            
            return jsonify({
                "status": "success",
                "payments": payments,
                "count": len(payments),
                "timestamp": datetime.now().isoformat()
            })

    except Exception as e:
        log_error("Error fetching payments", e)
        return jsonify({"error": f"Failed to fetch payments: {str(e)}"}), 500

@app.route('/api/setup-wasteking', methods=['POST'])
def setup_wasteking_session():
    """Setup WasteKing authentication"""
    try:
        log_with_timestamp("🚀 Starting WasteKing setup...")
        auth_result = authenticate_wasteking()
        
        if isinstance(auth_result, dict) and "error" in auth_result:
            return jsonify(auth_result), 500
        
        if auth_result:
            return jsonify({
                "status": "success",
                "message": "WasteKing authentication successful!",
                "timestamp": datetime.now().isoformat()
            })
        else:
            return jsonify({
                "status": "error",
                "message": "Authentication failed",
                "timestamp": datetime.now().isoformat()
            }), 500
            
    except Exception as e:
        log_error("WasteKing setup error", e)
        return jsonify({
            "status": "error",
            "message": f"Setup failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/api/test-twilio', methods=['POST'])
def test_twilio_connection():
    """Test Twilio connection and Braintree configuration"""
    try:
        client = get_twilio_client()
        if not client:
            return jsonify({
                "status": "error",
                "message": "Failed to initialize Twilio client"
            }), 500
        
        # Test account info
        account = client.api.accounts(TWILIO_ACCOUNT_SID).fetch()
        
        # Test payment connector
        connectors = client.trusthub.v1.payment_connectors.list(limit=20)
        braintree_connector_found = any(c.sid == BRAINTREE_CONNECTOR_SID for c in connectors)
        
        return jsonify({
            "status": "success",
            "message": "Twilio connection successful",
            "account_status": account.status,
            "account_friendly_name": account.friendly_name,
            "braintree_connector_configured": braintree_connector_found,
            "braintree_connector_sid": BRAINTREE_CONNECTOR_SID,
            "braintree_merchant_id": BRAINTREE_MERCHANT_ID,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        log_error("Error testing Twilio connection", e)
        return jsonify({
            "status": "error",
            "message": f"Twilio test failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }), 500

if __name__ == '__main__':
    # Development only - production uses gunicorn
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
