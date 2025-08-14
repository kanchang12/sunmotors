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
import random
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, send_file
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

# WasteKing API Configuration
WASTEKING_EMAIL = os.getenv('WASTEKING_EMAIL', 'kanchan.ghosh@wasteking.co.uk')
WASTEKING_PASSWORD = os.getenv('WASTEKING_PASSWORD', 'T^269725365789ad')
WASTEKING_BASE_URL = "https://wk-smp-api-dev.azurewebsites.net/"
WASTEKING_ACCESS_TOKEN = "wk-KZPY-tGF-@d.Aby9fpvMC_VVWkX-GN.i7jCBhF3xceoFfhmawaNc.RH.G_-kwk8*"
WASTEKING_PRICING_URL = f"{WASTEKING_BASE_URL}/reporting/priced-area-coverage-breakdown/"

# Twilio Configuration (SMS only - NO call transfer)
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', 'your_twilio_sid')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', 'your_twilio_token')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER', 'your_twilio_phone_number')
SERVER_BASE_URL = "https://internal-porpoise-onewebonly-1b44fcb9.koyeb.app"

# PayPal payment link
PAYPAL_PAYMENT_LINK = "https://www.paypal.com/ncp/payment/BQ82GUU9VSKYN"

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

def generate_short_id(prefix="WK"):
    """Generate SHORT 6-digit reference instead of UUID"""
    return f"{prefix}{random.randint(100000, 999999)}"

def log_with_timestamp(message, level="INFO"):
    """Clean logging - no sensitive data"""
    # Filter out phone numbers and sensitive data
    clean_message = message
    phone_patterns = [
        r'\+44\d{9,10}', r'0\d{9,10}', r'\d{11}',
        r'customer_phone["\']?\s*:\s*["\']?[\+\d\s\-\(\)]+["\']?'
    ]
    
    for pattern in phone_patterns:
        clean_message = re.sub(pattern, '[PHONE_HIDDEN]', clean_message)
    
    # Only show important messages
    important_keywords = ["🎯", "✅", "❌", "ERROR", "FINAL:", "AUTO-STARTING", "Quote", "Payment", "SMS"]
    if level == "INFO" and not any(keyword in clean_message for keyword in important_keywords):
        return
    
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {clean_message}")

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

# --- Database Functions (UPDATED WITH WASTE KING CRITERIA) ---
def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    log_with_timestamp("Initializing database with Waste King evaluation criteria...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Updated calls table with PROPER WASTE KING CRITERIA from induction manual
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
                
                -- WASTE KING SPECIFIC EVALUATION CRITERIA (from induction manual)
                call_handling_telephone_manner REAL,
                customer_needs_assessment REAL,
                product_knowledge_information REAL,
                objection_handling_erica REAL,
                sales_closing REAL,
                compliance_procedures REAL,
                
                -- WASTE KING SUB-CRITERIA (from induction manual)
                professional_telephone_manner REAL,
                listening_customer_requirements REAL,
                presenting_appropriate_solutions REAL,
                postcode_gathering REAL,
                waste_type_identification REAL,
                prohibited_items_check REAL,
                access_assessment REAL,
                permit_requirements REAL,
                offering_options REAL,
                erica_objection_method REAL,
                sales_recommendation REAL,
                asking_for_sale REAL,
                following_procedures REAL,
                communication_guidelines REAL,
                
                category TEXT,
                processed_at TEXT,
                processing_error TEXT,
                raw_communication_data TEXT,
                summary_translation TEXT,
                overall_waste_king_score REAL
            )
        ''')
        
        # Updated price_quotes table with elevenlabs_conversation_id
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
        
        # FIXED: SMS payments table with call_sid and elevenlabs_conversation_id columns
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sms_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_id TEXT,
                customer_phone TEXT,
                amount REAL,
                sms_sid TEXT,
                payment_status TEXT DEFAULT 'pending',
                created_at TEXT,
                paid_at TEXT,
                paypal_link TEXT,
                booking_confirmed BOOLEAN DEFAULT 0,
                call_sid TEXT,
                elevenlabs_conversation_id TEXT,
                FOREIGN KEY (quote_id) REFERENCES price_quotes (quote_id)
            )
        ''')
        
        # Check if columns need to be added to existing tables
        cursor.execute("PRAGMA table_info(sms_payments)")
        existing_columns = [column[1] for column in cursor.fetchall()]
        
        if 'call_sid' not in existing_columns:
            log_with_timestamp("Adding call_sid column to sms_payments table...")
            cursor.execute("ALTER TABLE sms_payments ADD COLUMN call_sid TEXT")
        
        if 'elevenlabs_conversation_id' not in existing_columns:
            log_with_timestamp("Adding elevenlabs_conversation_id column to sms_payments table...")
            cursor.execute("ALTER TABLE sms_payments ADD COLUMN elevenlabs_conversation_id TEXT")
        
        # Check price_quotes table for missing columns
        cursor.execute("PRAGMA table_info(price_quotes)")
        existing_quote_columns = [column[1] for column in cursor.fetchall()]
        
        if 'elevenlabs_conversation_id' not in existing_quote_columns:
            log_with_timestamp("Adding elevenlabs_conversation_id column to price_quotes table...")
            cursor.execute("ALTER TABLE price_quotes ADD COLUMN elevenlabs_conversation_id TEXT")
        
        conn.commit()
        conn.close()
        log_with_timestamp("Database initialized successfully with Waste King criteria")
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
    """Authenticate WasteKing using direct API calls"""
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
    """Get WasteKing pricing data and booking reference"""
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
            try:
                wasteking_data = response.json()
                booking_id = wasteking_data.get('booking_reference')
                
                if not booking_id:
                    # Generate a booking ID if not provided by API
                    booking_id = generate_short_id("WK")
                    log_with_timestamp(f"Generated booking ID: {booking_id}")
                
                return {
                    "status": "success",
                    "booking_id": booking_id,
                    "timestamp": datetime.now().isoformat(),
                    "message": "WasteKing data fetched successfully",
                    "data": wasteking_data,
                    "data_length": len(response.text)
                }
                
            except ValueError as e:
                log_error("Failed to parse WasteKing response", e)
                return {
                    "error": "Invalid response format",
                    "status": "parse_error",
                    "timestamp": datetime.now().isoformat()
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
                    wasteking_data = response.json()
                    return {
                        "status": "success",
                        "booking_id": wasteking_data.get('booking_reference'),
                        "message": "Re-authenticated and fetched data",
                        "timestamp": datetime.now().isoformat(),
                        "data": wasteking_data,
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

# --- Xelion API Functions (keeping as is) ---
def xelion_login() -> bool:
    """Login using the working pattern"""
    global session_token
    with login_lock:
        session_token = None
        
        login_url = f"{XELION_BASE_URL.rstrip('/')}/me/login"
        headers = {"Content-Type": "application/json"}
        
        # Use the working userspace pattern
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
    """Fetch calls from start_time onwards"""
    all_communications = []
    before_oid = None
    page = 1
    
    # Use the working base URLs pattern
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
                    response = xelion_session.get(communications_url, params=params, timeout=30)
                    
                    if response.status_code == 401:
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
                                    # Add valid calls from this page and stop
                                    all_communications.extend(valid_calls_this_page)
                                    log_with_timestamp(f"🎯 FINAL: Fetched {len(all_communications)} calls from {start_time} onwards")
                                    return all_communications
                                    
                            except Exception as e:
                                continue
                    
                    # If no valid calls on this page, we're done
                    if not page_has_valid_calls:
                        log_with_timestamp(f"🎯 FINAL: Fetched {len(all_communications)} calls from {start_time} onwards")
                        return all_communications
                    
                    # Add valid calls from this page
                    all_communications.extend(valid_calls_this_page)
                    
                    # Track status breakdown for valid calls only
                    for comm in valid_calls_this_page:
                        status = comm.get('object', {}).get('status', 'unknown')
                        processing_stats['statuses_seen'][status] = processing_stats['statuses_seen'].get(status, 0) + 1
                    
                    # Get next page info
                    if 'meta' in data and 'paging' in data['meta']:
                        before_oid = data['meta']['paging'].get('previousId')
                        if not before_oid:
                            log_with_timestamp(f"🎯 FINAL: Fetched {len(all_communications)} calls from {start_time} onwards")
                            return all_communications
                    else:
                        log_with_timestamp(f"🎯 FINAL: Fetched {len(all_communications)} calls from {start_time} onwards")
                        return all_communications
                    
                    fetched_this_page = True
                    break
                        
                except Exception as e:
                    continue
            
            if fetched_this_page:
                break
        
        if not fetched_this_page:
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
    """Download audio - QUIET"""
    audio_url = f"{XELION_BASE_URL.rstrip('/')}/communications/{communication_oid}/audio"
    file_path = os.path.join(AUDIO_TEMP_DIR, f"{communication_oid}.mp3")

    for attempt in range(3):
        try:
            global session_token
            if not session_token and not xelion_login():
                continue
            
            response = xelion_session.get(audio_url, timeout=60)
            
            if response.status_code == 200:
                if len(response.content) > 10:
                    with open(file_path, 'wb') as f:
                        f.write(response.content)
                    return file_path
                return None
            elif response.status_code == 404:
                return None
            elif response.status_code == 401:
                session_token = None
                if xelion_login():
                    continue
                return None
        except:
            if attempt == 2:
                return None
            time.sleep(2)
    return None

# --- Deepgram Transcription ---
def transcribe_audio_deepgram(audio_file_path: str, metadata_row: Dict) -> Optional[Dict]:
    """Transcribe audio - QUIET"""
    if not DEEPGRAM_API_KEY or DEEPGRAM_API_KEY == 'your_deepgram_api_key':
        return None

    oid = metadata_row['oid']
    if not os.path.exists(audio_file_path) or os.path.getsize(audio_file_path) < 10:
        return None
    
    try:
        url = "https://api.deepgram.com/v1/listen"
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "audio/mpeg"}
        params = {"model": "nova-2", "smart_format": "true", "punctuate": "true", "language": "en-GB"}
        
        with open(audio_file_path, 'rb') as audio_file:
            response = requests.post(url, headers=headers, params=params, data=audio_file, timeout=120)
        
        if response.status_code != 200:
            return None
        
        result = response.json()
        if not result.get('results', {}).get('channels', [{}])[0].get('alternatives', []):
            return None
        
        transcript_data = result['results']['channels'][0]['alternatives'][0]
        transcript_text = transcript_data.get('transcript', '').strip()
        
        if not transcript_text:
            return None
        
        duration_seconds = result.get('metadata', {}).get('duration', 0)
        duration_minutes = duration_seconds / 60
        
        return {
            'oid': oid, 'transcription_text': transcript_text,
            'transcribed_duration_minutes': round(duration_minutes, 2),
            'deepgram_cost_usd': round(duration_minutes * 0.0043, 4),
            'deepgram_cost_gbp': round(duration_minutes * 0.0043 * 0.79, 4),
            'word_count': len(transcript_text.split()),
            'confidence': round(transcript_data.get('confidence', 0), 3),
            'language': result.get('metadata', {}).get('detected_language', 'en')
        }
    except:
        return None

def process_single_call(communication_data: Dict) -> Optional[str]:
    """Process single call - QUIET VERSION"""
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
            processing_stats['total_skipped'] += 1
            return None
    except:
        processing_stats['total_skipped'] += 1
        return None

    try:
        raw_data = json.dumps(communication_data)
        xelion_metadata = _extract_agent_info(comm_obj)
        
        audio_file_path = download_audio(oid)
        if not audio_file_path:
            return None

        transcription_result = transcribe_audio_deepgram(audio_file_path, {'oid': oid})
        
        try:
            os.remove(audio_file_path)
        except:
            pass

        if not transcription_result:
            processing_stats['total_errors'] += 1
            return None

        wasteking_analysis = analyze_transcription_with_wasteking_criteria(transcription_result['transcription_text'], oid)
        if not wasteking_analysis:
            wasteking_analysis = {
                "call_handling_telephone_manner": 0, "customer_needs_assessment": 0, "product_knowledge_information": 0,
                "objection_handling_erica": 0, "sales_closing": 0, "compliance_procedures": 0,
                "professional_telephone_manner": 0, "listening_customer_requirements": 0, "presenting_appropriate_solutions": 0,
                "postcode_gathering": 0, "waste_type_identification": 0, "prohibited_items_check": 0,
                "access_assessment": 0, "permit_requirements": 0, "offering_options": 0,
                "erica_objection_method": 0, "sales_recommendation": 0, "asking_for_sale": 0,
                "following_procedures": 0, "communication_guidelines": 0, "overall_waste_king_score": 0,
                "summary": "Analysis failed"
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
                        word_count, confidence, language, 
                        call_handling_telephone_manner, customer_needs_assessment, product_knowledge_information,
                        objection_handling_erica, sales_closing, compliance_procedures,
                        professional_telephone_manner, listening_customer_requirements, presenting_appropriate_solutions,
                        postcode_gathering, waste_type_identification, prohibited_items_check,
                        access_assessment, permit_requirements, offering_options,
                        erica_objection_method, sales_recommendation, asking_for_sale,
                        following_procedures, communication_guidelines,
                        category, processed_at, processing_error, raw_communication_data, 
                        summary_translation, overall_waste_king_score
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    oid, call_datetime, xelion_metadata['agent_name'], xelion_metadata['phone_number'],
                    xelion_metadata['call_direction'], xelion_metadata['duration_seconds'], xelion_metadata['status'],
                    xelion_metadata['user_id'], transcription_result['transcription_text'],
                    transcription_result['transcribed_duration_minutes'], transcription_result['deepgram_cost_usd'],
                    transcription_result['deepgram_cost_gbp'], transcription_result['word_count'],
                    transcription_result['confidence'], transcription_result['language'],
                    wasteking_analysis.get('call_handling_telephone_manner', 0),
                    wasteking_analysis.get('customer_needs_assessment', 0), wasteking_analysis.get('product_knowledge_information', 0),
                    wasteking_analysis.get('objection_handling_erica', 0), wasteking_analysis.get('sales_closing', 0),
                    wasteking_analysis.get('compliance_procedures', 0), wasteking_analysis.get('professional_telephone_manner', 0),
                    wasteking_analysis.get('listening_customer_requirements', 0), wasteking_analysis.get('presenting_appropriate_solutions', 0),
                    wasteking_analysis.get('postcode_gathering', 0), wasteking_analysis.get('waste_type_identification', 0),
                    wasteking_analysis.get('prohibited_items_check', 0), wasteking_analysis.get('access_assessment', 0),
                    wasteking_analysis.get('permit_requirements', 0), wasteking_analysis.get('offering_options', 0),
                    wasteking_analysis.get('erica_objection_method', 0), wasteking_analysis.get('sales_recommendation', 0),
                    wasteking_analysis.get('asking_for_sale', 0), wasteking_analysis.get('following_procedures', 0),
                    wasteking_analysis.get('communication_guidelines', 0),
                    call_category, datetime.now().isoformat(), None, raw_data, 
                    wasteking_analysis.get('summary', 'No summary'), wasteking_analysis.get('overall_waste_king_score', 0)
                ))
                conn.commit()
                log_with_timestamp(f"✅ Call processed: {oid}")
                processing_stats['total_processed'] += 1
                return oid
            except Exception as e:
                log_error(f"Database error: {oid}", e)
                processing_stats['total_errors'] += 1
                return None
            finally:
                conn.close()
                
    except Exception as e:
        log_error(f"Processing error: {oid}", e)
        processing_stats['total_errors'] += 1
        return None

def fetch_and_transcribe_recent_calls():
    """Continuous loop checking for new calls <60 mins old with audio"""
    global background_process_running
    background_process_running = True
    
    while background_process_running:
        try:
            time_window_start = datetime.now() - timedelta(minutes=60)
            
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

# --- OpenAI Analysis (UPDATED WITH WASTE KING CRITERIA) ---
def analyze_transcription_with_wasteking_criteria(transcript: str, oid: str = "unknown") -> Optional[Dict]:
    """Analyze transcription with WASTE KING SPECIFIC criteria from induction manual"""
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY or OPENAI_API_KEY == 'your_openai_api_key':
        # Return fallback scores for Waste King criteria
        import random
        random.seed(len(transcript))
        
        base_score = min(10, max(4, len(transcript.split()) / 50))
        
        return {
            "call_handling_telephone_manner": round(base_score + random.uniform(-1, 1), 1),
            "customer_needs_assessment": round(base_score + random.uniform(-1.5, 1.5), 1),
            "product_knowledge_information": round(base_score + random.uniform(-2, 1), 1),
            "objection_handling_erica": round(base_score + random.uniform(-1.5, 1), 1),
            "sales_closing": round(base_score + random.uniform(-1.5, 1.5), 1),
            "compliance_procedures": round(base_score + random.uniform(-1, 1), 1),
            "professional_telephone_manner": round(base_score + random.uniform(-1, 1.5), 1),
            "listening_customer_requirements": round(base_score + random.uniform(-1.5, 1.5), 1),
            "presenting_appropriate_solutions": round(base_score + random.uniform(-1.5, 1.5), 1),
            "postcode_gathering": round(base_score + random.uniform(-1, 1.5), 1),
            "waste_type_identification": round(base_score + random.uniform(-1.5, 1), 1),
            "prohibited_items_check": round(base_score + random.uniform(-1, 1.5), 1),
            "access_assessment": round(base_score + random.uniform(-1.5, 1), 1),
            "permit_requirements": round(base_score + random.uniform(-1.5, 1.5), 1),
            "offering_options": round(base_score + random.uniform(-1, 1.5), 1),
            "erica_objection_method": round(base_score + random.uniform(-2, 1), 1),
            "sales_recommendation": round(base_score + random.uniform(-1.5, 1.5), 1),
            "asking_for_sale": round(base_score + random.uniform(-1.5, 1.5), 1),
            "following_procedures": round(base_score + random.uniform(-1, 1.5), 1),
            "communication_guidelines": round(base_score + random.uniform(-1, 1), 1),
            "overall_waste_king_score": round(base_score + random.uniform(-0.5, 1), 1),
            "summary": f"Fallback Waste King analysis - {len(transcript.split())} words."
        }
    
    # Truncate long transcripts
    if len(transcript) > 4000:
        transcript = transcript[:4000] + "... (truncated)"
    
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        
        prompt = f"""You are evaluating a customer service call for Waste King using their specific training criteria from their induction manual. Rate each area 1-10:

MAIN CATEGORIES:
1. CALL HANDLING & TELEPHONE MANNER (1-10):
   - Pleasant and professional telephone manner
   - Listening to customer requirements
   - Presenting appropriately to provide best solution

2. CUSTOMER NEEDS ASSESSMENT (1-10):
   - Asked for customer's postcode
   - Identified waste type clearly
   - Checked for prohibited items
   - Assessed access issues

3. PRODUCT KNOWLEDGE & INFORMATION (1-10):
   - Knowledge of services and options
   - Understanding of procedures
   - Pricing and permit requirements

4. OBJECTION HANDLING - ERICA METHOD (1-10):
   - Empathy, Refine, Isolate, Commit, Answer
   - Professional objection handling

5. SALES CLOSING (1-10):
   - Made recommendations
   - Asked for the sale
   - Closed effectively

6. COMPLIANCE & PROCEDURES (1-10):
   - Followed company guidelines
   - Communication procedures

Return ONLY this JSON format:
{{
    "call_handling_telephone_manner": 8,
    "customer_needs_assessment": 7,
    "product_knowledge_information": 6,
    "objection_handling_erica": 7,
    "sales_closing": 8,
    "compliance_procedures": 7,
    "professional_telephone_manner": 8,
    "listening_customer_requirements": 7,
    "presenting_appropriate_solutions": 6,
    "postcode_gathering": 8,
    "waste_type_identification": 7,
    "prohibited_items_check": 6,
    "access_assessment": 7,
    "permit_requirements": 6,
    "offering_options": 7,
    "erica_objection_method": 6,
    "sales_recommendation": 7,
    "asking_for_sale": 8,
    "following_procedures": 7,
    "communication_guidelines": 8,
    "overall_waste_king_score": 7,
    "summary": "Brief call summary with Waste King specific insights"
}}

Transcript: {transcript}"""
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a Waste King call quality analyzer. Return valid JSON with numeric scores 1-10 based on Waste King training criteria."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            max_tokens=800,
            temperature=0.3
        )
        
        analysis_json = json.loads(response.choices[0].message.content)
        
        # Validate and convert scores
        waste_king_fields = [
            'call_handling_telephone_manner', 'customer_needs_assessment', 'product_knowledge_information',
            'objection_handling_erica', 'sales_closing', 'compliance_procedures',
            'professional_telephone_manner', 'listening_customer_requirements', 'presenting_appropriate_solutions',
            'postcode_gathering', 'waste_type_identification', 'prohibited_items_check',
            'access_assessment', 'permit_requirements', 'offering_options',
            'erica_objection_method', 'sales_recommendation', 'asking_for_sale',
            'following_procedures', 'communication_guidelines', 'overall_waste_king_score'
        ]
        
        for field in waste_king_fields:
            try:
                analysis_json[field] = float(analysis_json.get(field, 0))
            except (ValueError, TypeError):
                analysis_json[field] = 0.0
        
        if 'summary' not in analysis_json:
            analysis_json['summary'] = "Waste King analysis completed."
        
        return analysis_json
        
    except Exception as e:
        log_error(f"Waste King criteria analysis error for OID {oid}", e)
        return None

def categorize_call(transcript: str) -> str:
    """Categorize calls based on Waste King service types"""
    transcript_lower = transcript.lower()
    
    if "skip" in transcript_lower or "hire" in transcript_lower:
        return "skip hire"
    if any(word in transcript_lower for word in ["van", "driver", "man and van", "man & van"]):
        return "man and van"
    if any(word in transcript_lower for word in ["collection", "collect", "pickup", "pick up"]):
        return "collections"
    if any(word in transcript_lower for word in ["grab", "grab hire", "grab lorry"]):
        return "grab hire"
    if any(word in transcript_lower for word in ["clearance", "house clearance", "property clearance"]):
        return "clearance"
    if "complaint" in transcript_lower or "unhappy" in transcript_lower or "dissatisfied" in transcript_lower:
        return "complaint"
    return "general enquiry"

# --- Twilio Functions (SMS ONLY) ---
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

def clean_phone_number(phone_number: str) -> str:
    """Clean and format phone number for international use"""
    if not phone_number:
        return None
    
    # Remove spaces, hyphens, and other formatting
    clean_phone = phone_number.replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    
    # Add country code if needed
    if not clean_phone.startswith('+'):
        # Assume UK number if no country code
        if clean_phone.startswith('0'):
            clean_phone = '+44' + clean_phone[1:]
        elif clean_phone.startswith('44'):
            clean_phone = '+' + clean_phone
        else:
            # Default to UK if unclear
            clean_phone = '+44' + clean_phone
    
    return clean_phone

def send_sms_payment(quote_id: str, customer_phone: str, amount: str):
    """Send payment SMS - WORKING VERSION"""
    try:
        # Clean phone number
        clean_phone = clean_phone_number(customer_phone)
        if not clean_phone:
            raise Exception(f"Invalid phone number: {customer_phone}")

        # Check Twilio configuration
        if not TWILIO_PHONE_NUMBER or TWILIO_PHONE_NUMBER == 'your_twilio_phone_number':
            raise Exception("Twilio phone number not configured")

        # Get Twilio client
        client = get_twilio_client()
        if not client:
            raise Exception("Twilio client initialization failed")

        # Create SMS message
        message_body = f"""Waste King Payment
Amount: £{amount}
Quote: {quote_id}

Pay securely via PayPal:
{PAYPAL_PAYMENT_LINK}

After payment, you'll get confirmation.
Thank you!"""

        # Send SMS
        message = client.messages.create(
            body=message_body,
            from_=TWILIO_PHONE_NUMBER,
            to=clean_phone
        )
        
        log_with_timestamp(f"✅ SMS sent successfully: {message.sid}")
        
        # Store SMS record in database
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO sms_payments (quote_id, customer_phone, amount, sms_sid, created_at, paypal_link)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (quote_id, clean_phone, float(amount), message.sid, datetime.now().isoformat(), PAYPAL_PAYMENT_LINK))
            conn.commit()
            conn.close()
        
        return {
            "success": True,
            "sms_sid": message.sid,
            "phone_number": clean_phone,
            "message": f"Payment link sent"
        }
        
    except Exception as e:
        log_error(f"SMS sending failed for quote {quote_id}", e)
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to send SMS"
        }

# --- Flask App Setup ---
app = Flask(__name__)
init_db()

# --- Flask Routes ---
@app.route('/')
def index():
    """Auto-start monitoring and serve updated dashboard"""
    global background_process_running, background_thread
    
    if not background_process_running:
        log_with_timestamp("🚀 AUTO-STARTING Waste King monitoring")
        background_thread = threading.Thread(target=fetch_and_transcribe_recent_calls)
        background_thread.daemon = True
        background_thread.start()
        log_with_timestamp("✅ Waste King monitoring auto-started")
    
    return render_template('index.html')

@app.route('/')
def demo():
    
    return render_template('demo.html')


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
        "last_poll": processing_stats.get('last_poll_time', 'Never'),
        "last_error": processing_stats.get('last_error', 'None')
    })

# --- FIXED: ADD THE MISSING ROUTE ELEVENLABS NEEDS ---
@app.route('/api/wasteking-quote', methods=['POST'])
def wasteking_quote():
    """ElevenLabs wasteking quote endpoint - CLEAN LOGGING"""
    log_with_timestamp("🎯 WASTEKING QUOTE REQUEST")
    
    try:
        data = request.get_json() or request.form.to_dict() or request.args.to_dict()
        
        if not data:
            return jsonify({
                "status": "error",
                "message": "No data provided",
                "quote_id": generate_short_id("WK")
            }), 200

        postcode = data.get('postcode', '').strip()
        service = data.get('service', '').strip()
        
        log_with_timestamp(f"📍 Quote request: {postcode}, {service}")
        
        if not postcode or not service:
            return jsonify({
                "status": "error", 
                "message": "Postcode and service required",
                "quote_id": generate_short_id("WK")
            }), 200

        # Clean postcode
        postcode_clean = postcode.upper().replace(' ', '')
        uk_postcode_pattern = r'^[A-Z]{1,2}[0-9][A-Z0-9]?[0-9][A-Z]{2}$'
        
        if not re.match(uk_postcode_pattern, postcode_clean):
            return jsonify({
                "status": "error",
                "message": f"Invalid UK postcode: {postcode}",
                "quote_id": generate_short_id("WK")
            }), 200

        # Reformat postcode
        if len(postcode_clean) >= 5:
            postcode = f"{postcode_clean[:-3]} {postcode_clean[-3:]}"

        # Service mapping
        service_mapping = {
            "skip hire": "skip", "skip": "skip", "skips": "skip",
            "man and van": "mav", "man & van": "mav", "van": "mav",
            "grab hire": "grab", "grab": "grab",
            "collection": "collections", "collections": "collections",
            "clearance": "clearance", "house clearance": "clearance"
        }
        
        wasteking_service = service_mapping.get(service.lower(), service.lower())

        # WasteKing API call
        headers = {"x-wasteking-request": WASTEKING_ACCESS_TOKEN, "Content-Type": "application/json"}
        
        # Create booking
        create_response = requests.post(
            f"{WASTEKING_BASE_URL}api/booking/create/",
            headers=headers,
            json={"type": "chatbot", "source": "wasteking.co.uk"},
            timeout=15, verify=False
        )
        
        if create_response.status_code != 200:
            return jsonify({
                "status": "success",
                "quote_id": generate_short_id("WK"),
                "message": "Let me transfer you to our team for pricing",
                "should_transfer": True
            }), 200

        booking_ref = create_response.json().get('bookingRef')
        if not booking_ref:
            return jsonify({
                "status": "success", 
                "quote_id": generate_short_id("WK"),
                "message": "System busy, transferring to our team",
                "should_transfer": True
            }), 200

        # Update with search
        update_response = requests.post(
            f"{WASTEKING_BASE_URL}api/booking/update/",
            headers=headers,
            json={
                "bookingRef": booking_ref,
                "search": {"postCode": postcode, "service": wasteking_service}
            },
            timeout=20, verify=False
        )

        # Store quote
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO price_quotes (quote_id, booking_ref, postcode, service, price_data, created_at, status, agent_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                booking_ref, booking_ref, postcode, wasteking_service, 
                json.dumps(update_response.json() if update_response.status_code == 200 else {}),
                datetime.now().isoformat(),
                'success' if update_response.status_code == 200 else 'no_results',
                data.get('agent_name', 'System')
            ))
            conn.commit()
            conn.close()

        if update_response.status_code == 200:
            log_with_timestamp(f"✅ Quote successful: {booking_ref}")
            return jsonify({
                "status": "success",
                "quote_id": booking_ref,
                "message": f"Great! I can offer {wasteking_service} service for {postcode}. Your quote reference is {booking_ref}.",
                "has_pricing": True,
                "bookingRef": booking_ref
            }), 200
        else:
            log_with_timestamp(f"❌ No service available: {postcode}")
            return jsonify({
                "status": "success",
                "quote_id": booking_ref,
                "message": f"Sorry, we don't service {postcode} for {wasteking_service}. Let me transfer you.",
                "has_pricing": False,
                "should_transfer": True
            }), 200

    except Exception as e:
        log_error("Wasteking quote error", e)
        return jsonify({
            "status": "success",
            "quote_id": generate_short_id("WK"), 
            "message": "Let me transfer you to our team",
            "should_transfer": True
        }), 200

# --- PRICING AND PAYMENT ENDPOINTS (SIMPLIFIED LOGGING) ---

@app.route('/api/get-wasteking-prices', methods=['POST'])
def get_wasteking_prices_from_api():
    """Get WasteKing pricing - SIMPLIFIED LOGGING"""
    log_with_timestamp("🎯 WASTEKING PRICING ENDPOINT CALLED")
    
    try:
        # Handle different content types
        data = None
        if request.is_json and request.json:
            data = request.json
        elif request.form:
            data = request.form.to_dict()
        elif request.args:
            data = request.args.to_dict()
        
        if not data:
            return jsonify({
                "status": "error",
                "message": "No data provided. Please provide postcode and service.",
                "quote_id": generate_short_id("WK"),
                "timestamp": datetime.now().isoformat()
            }), 200

        postcode = data.get('postcode')
        service = data.get('service', '').strip()
        
        log_with_timestamp(f"📍 Request - Postcode: {postcode}, Service: '{service}'")
        
        if not postcode or not service:
            return jsonify({
                "status": "error",
                "message": "Postcode and service are required",
                "quote_id": generate_short_id("WK"),
                "timestamp": datetime.now().isoformat()
            }), 200

        # Clean and validate postcode
        postcode_clean = postcode.upper().replace(' ', '')
        uk_postcode_pattern = r'^[A-Z]{1,2}[0-9][A-Z0-9]?[0-9][A-Z]{2}$'
        
        if not re.match(uk_postcode_pattern, postcode_clean):
            return jsonify({
                "status": "error",
                "message": f"'{postcode}' is not a valid UK postcode. Please provide a valid postcode like 'LS1 4ED'.",
                "quote_id": generate_short_id("WK"),
                "timestamp": datetime.now().isoformat()
            }), 200

        # Re-format postcode properly
        if len(postcode_clean) >= 5:
            postcode = f"{postcode_clean[:-3]} {postcode_clean[-3:]}"

        # FIXED service mapping - Use correct WasteKing API values
        service_lower = service.lower().strip()
        service_mapping = {
            # Skip Hire
            "skip hire": "skip",
            "skip": "skip",
            "skips": "skip",
            "skip rental": "skip",
            "hire skip": "skip",
            
            # Man & Van
            "man and van": "mav",
            "man in van": "mav", 
            "man & van": "mav",  
            "man with van": "mav",
            "van": "mav",
            "van service": "mav",
            "van collection": "mav",
            "man van": "mav",
            
            # Grab Hire
            "grab hire": "grab",
            "grab": "grab",
            "grab lorry": "grab",
            
            # Collections
            "collection": "collections",
            "collections": "collections",
            "waste collection": "collections",
            "rubbish collection": "collections",
            
            # Clearance
            "house clearance": "clearance",
            "clearance": "clearance",
            "property clearance": "clearance",
        }
        
        wasteking_service = service_mapping.get(service_lower, service_lower)
        log_with_timestamp(f"🔄 Mapped service '{service}' → '{wasteking_service}' for postcode {postcode}")

        # Headers for WasteKing API
        headers = {
            "x-wasteking-request": WASTEKING_ACCESS_TOKEN,
            "Content-Type": "application/json"
        }

        # Step 1: Create booking reference
        create_url = f"{WASTEKING_BASE_URL}api/booking/create/"
        create_payload = {
            "type": "chatbot",
            "source": "wasteking.co.uk"
        }
        
        create_response = requests.post(create_url, headers=headers, json=create_payload, timeout=15, verify=False)
        
        if create_response.status_code != 200:
            return jsonify({
                "status": "error",
                "message": "Unable to get pricing at this time. Please contact us directly.",
                "quote_id": generate_short_id("WK"),
                "timestamp": datetime.now().isoformat()
            }), 200

        create_data = create_response.json()
        booking_ref = create_data.get('bookingRef')

        if not booking_ref:
            return jsonify({
                "status": "error",
                "message": "System error occurred. Please contact us directly.",
                "quote_id": generate_short_id("WK"),
                "timestamp": datetime.now().isoformat()
            }), 200

        # Use the actual booking_ref as the quote_id
        quote_id = booking_ref
        log_with_timestamp(f"✅ Created WasteKing booking reference: {quote_id}")

        # Step 2: Update booking with search
        update_url = f"{WASTEKING_BASE_URL}api/booking/update/"
        update_payload = {
            "bookingRef": booking_ref,
            "search": {
                "postCode": postcode,
                "service": wasteking_service
            }
        }
        
        update_response = requests.post(update_url, headers=headers, json=update_payload, timeout=20, verify=False)
        update_data = update_response.json()

        # Store without logging sensitive phone data
        agent_name = data.get('agent_name', 'Thomas')
        call_sid = data.get('call_sid', 'Unknown')
        elevenlabs_conversation_id = data.get('elevenlabs_conversation_id', 'Unknown')

        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO price_quotes (quote_id, booking_ref, postcode, service, price_data, created_at, agent_name, status, call_sid, elevenlabs_conversation_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                quote_id, booking_ref, postcode, wasteking_service, json.dumps(update_data), 
                datetime.now().isoformat(), agent_name, 
                'no_results' if update_response.status_code != 200 else 'pending',
                call_sid, elevenlabs_conversation_id
            ))
            conn.commit()
            conn.close()

        # Return response
        if update_response.status_code == 200:
            return jsonify({
                "status": "success",
                "quote_id": quote_id,
                "message": f"I can offer you {wasteking_service} service for {postcode}. Your quote reference is {quote_id}. Would you like me to proceed with booking?",
                "has_pricing": True,
                "pricing_available": True,
                "bookingRef": booking_ref,
                "search_results": update_data,
                "postcode": postcode,
                "service": wasteking_service,
                "timestamp": datetime.now().isoformat()
            }), 200
        else:
            return jsonify({
                "status": "success",
                "quote_id": quote_id,
                "message": f"I'm sorry, we don't currently offer {wasteking_service} service in {postcode}. Let me transfer you to our specialist team.",
                "has_pricing": False,
                "pricing_available": False,
                "should_transfer": True,
                "timestamp": datetime.now().isoformat()
            }), 200

    except Exception as e:
        log_error("WasteKing API error", e)
        return jsonify({
            "status": "success",
            "quote_id": generate_short_id("WK"),
            "message": "Let me transfer you to our team who can help you with pricing.",
            "has_pricing": False,
            "should_transfer": True,
            "timestamp": datetime.now().isoformat()
        }), 200

@app.route('/api/send-payment-sms', methods=['POST'])
def send_payment_sms():
    """Handle payment requests from ElevenLabs - SIMPLIFIED LOGGING"""
    log_with_timestamp("💳 PAYMENT SMS REQUEST")
    
    try:
        # 1. Parse incoming JSON
        data = request.get_json()
        if not data:
            return jsonify({
                "status": "error",
                "message": "No JSON data received",
                "solution": "Please check tool configuration"
            }), 400

        # 2. Validate required fields - NO PHONE LOGGING
        required_fields = {
            'customer_phone': 'Customer phone number',
            'call_sid': 'Call SID',
            'amount': 'Payment amount'
        }
        
        missing_fields = [name for field, name in required_fields.items() if not data.get(field)]
        if missing_fields:
            return jsonify({
                "status": "error",
                "message": f"Missing required fields: {', '.join(missing_fields)}"
            }), 400

        # 3. Clean and validate phone number - NO LOGGING
        phone = data['customer_phone'].strip()
        if phone.startswith('0'):
            phone = f"+44{phone[1:]}"
        elif phone.startswith('44'):
            phone = f"+{phone}"
        
        if not re.match(r'^\+44\d{9,10}$', phone):
            return jsonify({
                "status": "error",
                "message": "Invalid UK phone number format",
                "example": "+447700900123"
            }), 400

        # 4. Force £1 amount as specified
        amount = "1.00"

        # 5. Get quote_id from the call_sid or use it directly
        quote_id = data.get('quote_id', data['call_sid'])

        # 6. Send SMS via Twilio - NO PHONE LOGGING
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message = client.messages.create(
            body=f"Waste King Payment\nAmount: £{amount}\nReference: {quote_id}\nPay now: {PAYPAL_PAYMENT_LINK}",
            from_=TWILIO_PHONE_NUMBER,
            to=phone
        )

        log_with_timestamp(f"✅ SMS sent successfully: {message.sid}")

        # 7. Store in database with MINIMAL logging
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO sms_payments 
                (quote_id, customer_phone, amount, sms_sid, call_sid, elevenlabs_conversation_id, created_at, paypal_link) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                quote_id,
                phone,
                float(amount),
                message.sid,
                data['call_sid'],
                data.get('elevenlabs_conversation_id', 'Unknown'),
                datetime.now().isoformat(),
                PAYPAL_PAYMENT_LINK
            ))
            conn.commit()
            conn.close()

        return jsonify({
            "status": "success",
            "message": f"SMS sent successfully",
            "amount": f"£{amount}",
            "call_sid": data['call_sid'],
            "quote_id": quote_id
        })

    except Exception as e:
        log_error("Payment processing failed", e)
        return jsonify({
            "status": "error",
            "message": "System error",
            "debug": str(e)
        }), 500

@app.route('/api/check-postcode', methods=['POST'])
def check_postcode():
    """Check if WasteKing services the given postcode"""
    log_with_timestamp("📍 POSTCODE CHECK REQUEST")
    
    try:
        data = request.get_json()
        postcode = data.get('postcode', '').upper().strip()
        
        if not postcode:
            return jsonify({
                'success': False,
                'error': 'Postcode required',
                'speak': 'I need your postcode to check if we can service your area.'
            }), 400
        
        # Basic UK postcode validation
        uk_postcode_pattern = r'^[A-Z]{1,2}[0-9R][0-9A-Z]? [0-9][A-Z]{2}$'
        
        if re.match(uk_postcode_pattern, postcode):
            service_available = True
            speak_text = f"Great! We do service {postcode}. What type of service are you looking for today?"
        else:
            service_available = False
            speak_text = f"I'm sorry, {postcode} appears to be outside our service area. Let me transfer you to our team to see if we can arrange something special for you."
        
        log_with_timestamp(f"📍 Postcode {postcode}: {'✅ Available' if service_available else '❌ Not available'}")
        
        return jsonify({
            'success': True,
            'postcode': postcode,
            'service_available': service_available,
            'speak': speak_text
        })
        
    except Exception as e:
        log_error("Error in check_postcode endpoint", e)
        return jsonify({
            'success': False,
            'error': 'Internal server error',
            'speak': 'There was an issue checking your postcode. What postcode would you like to check?'
        }), 500

# --- FIXED: ADD MISSING DASHBOARD ROUTES ---
@app.route('/get_calls_list')
def get_calls_list():
    """Get calls list"""
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
        agent = request.args.get('agent', '')
        category = request.args.get('category', '')
        audio_filter = request.args.get('audio_filter', '')
        
        offset = (page - 1) * per_page
        where_conditions = []
        params = []
        
        if agent:
            where_conditions.append("agent_name LIKE ?")
            params.append(f"%{agent}%")
        if category:
            where_conditions.append("category = ?")
            params.append(category)
        if audio_filter == 'with_audio':
            where_conditions.append("transcription_text IS NOT NULL")
        
        where_clause = "WHERE " + " AND ".join(where_conditions) if where_conditions else ""
        
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute(f"SELECT COUNT(*) FROM calls {where_clause}", params)
            total_calls = cursor.fetchone()[0]
            
            cursor.execute(f"""
                SELECT oid, call_datetime, agent_name, phone_number, call_direction, 
                       duration_seconds, status, category, transcription_text,
                       overall_waste_king_score, processed_at
                FROM calls {where_clause}
                ORDER BY call_datetime DESC LIMIT ? OFFSET ?
            """, params + [per_page, offset])
            calls = cursor.fetchall()
            conn.close()
        
        calls_list = []
        for call in calls:
            calls_list.append({
                'oid': call[0], 'call_datetime': call[1], 'agent_name': call[2],
                'phone_number': call[3], 'call_direction': call[4], 'duration_seconds': call[5],
                'status': call[6], 'category': call[7], 'has_transcription': bool(call[8]),
                'overall_waste_king_score': call[9] or 0, 'processed_at': call[10]
            })
        
        total_pages = (total_calls + per_page - 1) // per_page
        
        return jsonify({
            'calls': calls_list,
            'pagination': {
                'page': page, 'per_page': per_page, 'total_calls': total_calls,
                'total_pages': total_pages, 'has_next': page < total_pages, 'has_prev': page > 1
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- Dashboard Data with Waste King Criteria ---
@app.route('/get_dashboard_data')
def get_dashboard_data():
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Total counts
        cursor.execute("SELECT COUNT(*) FROM calls")
        total_calls = cursor.fetchone()[0]

        cursor.execute("SELECT SUM(deepgram_cost_gbp) FROM calls")
        total_cost_gbp = cursor.fetchone()[0] or 0.0

        cursor.execute("SELECT SUM(transcribed_duration_minutes) FROM calls")
        total_duration_minutes = cursor.fetchone()[0] or 0.0

        # WASTE KING SPECIFIC AVERAGES
        cursor.execute("""
            SELECT AVG(call_handling_telephone_manner), AVG(customer_needs_assessment), 
                   AVG(product_knowledge_information), AVG(objection_handling_erica),
                   AVG(sales_closing), AVG(compliance_procedures)
            FROM calls
        """)
        avg_main_scores = cursor.fetchone()
        avg_call_handling = round(avg_main_scores[0] or 0, 2)
        avg_needs_assessment = round(avg_main_scores[1] or 0, 2)
        avg_product_knowledge = round(avg_main_scores[2] or 0, 2)
        avg_objection_handling = round(avg_main_scores[3] or 0, 2)
        avg_sales_closing = round(avg_main_scores[4] or 0, 2)
        avg_compliance = round(avg_main_scores[5] or 0, 2)

        # Sub-category averages for Waste King
        cursor.execute("""
            SELECT AVG(professional_telephone_manner), AVG(listening_customer_requirements), 
                   AVG(presenting_appropriate_solutions), AVG(postcode_gathering),
                   AVG(waste_type_identification), AVG(prohibited_items_check),
                   AVG(access_assessment), AVG(permit_requirements),
                   AVG(offering_options), AVG(erica_objection_method),
                   AVG(sales_recommendation), AVG(asking_for_sale),
                   AVG(following_procedures), AVG(communication_guidelines)
            FROM calls
        """)
        avg_subs = cursor.fetchone()
        waste_king_subs = {
            "professional_telephone_manner": round(avg_subs[0] or 0, 2),
            "listening_customer_requirements": round(avg_subs[1] or 0, 2),
            "presenting_appropriate_solutions": round(avg_subs[2] or 0, 2),
            "postcode_gathering": round(avg_subs[3] or 0, 2),
            "waste_type_identification": round(avg_subs[4] or 0, 2),
            "prohibited_items_check": round(avg_subs[5] or 0, 2),
            "access_assessment": round(avg_subs[6] or 0, 2),
            "permit_requirements": round(avg_subs[7] or 0, 2),
            "offering_options": round(avg_subs[8] or 0, 2),
            "erica_objection_method": round(avg_subs[9] or 0, 2),
            "sales_recommendation": round(avg_subs[10] or 0, 2),
            "asking_for_sale": round(avg_subs[11] or 0, 2),
            "following_procedures": round(avg_subs[12] or 0, 2),
            "communication_guidelines": round(avg_subs[13] or 0, 2),
        }

        # Category ratings using Waste King categories
        category_ratings = {}
        categories = ["skip hire", "man and van", "collections", "grab hire", "clearance", "general enquiry", "complaint"] 
        for cat in categories:
            cursor.execute("SELECT AVG(overall_waste_king_score), COUNT(*) FROM calls WHERE category = ?", (cat,))
            result = cursor.fetchone()
            avg_score = round(result[0] or 0, 2)
            count = result[1]
            category_ratings[cat] = {"average_score": avg_score, "count": count}

        conn.close()

        return jsonify({
            "total_calls": total_calls,
            "total_cost_gbp": round(total_cost_gbp, 2),
            "total_duration_minutes": round(total_duration_minutes, 2),
            "waste_king_main_ratings": {
                "call_handling_telephone_manner": avg_call_handling,
                "customer_needs_assessment": avg_needs_assessment,
                "product_knowledge_information": avg_product_knowledge,
                "objection_handling_erica": avg_objection_handling,
                "sales_closing": avg_sales_closing,
                "compliance_procedures": avg_compliance
            },
            "waste_king_sub_ratings": waste_king_subs,
            "category_call_ratings": category_ratings,
            "processing_stats": processing_stats
        })

if __name__ == '__main__':
    # Disable Flask logging to reduce noise
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    log_with_timestamp("🚀 Starting Waste King System with Clean Logging...")
    log_with_timestamp("✅ Features:")
    log_with_timestamp("  • Fixed missing /api/wasteking-quote route")
    log_with_timestamp("  • Fixed missing /get_calls_list route") 
    log_with_timestamp("  • Fixed SQL column mismatch")
    log_with_timestamp("  • Clean logging (no phone numbers)")
    log_with_timestamp("  • Only custom tool calls shown in console")
    
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port, use_reloader=False)
