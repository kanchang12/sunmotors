import os
import requests
import json
import csv
import sqlite3
import threading
import time
import traceback
import pickle
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
import re
from bs4 import BeautifulSoup

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
WASTEKING_BASE_URL = "https://wasteking-suppliermarketplace-dev.azurewebsites.net"
WASTEKING_PRICING_URL = f"{WASTEKING_BASE_URL}/reporting/priced-area-coverage-breakdown/"

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

def _fetch_communications_page(limit: int, until_date: datetime, before_oid: Optional[str] = None) -> Tuple[List[Dict], Optional[str]]:
    """Fetch communications using the WORKING pattern from transcription.py"""
    params = {'limit': limit}
    if until_date:
        params['until'] = until_date.strftime('%Y-%m-%d %H:%M:%S') 
    if before_oid:
        params['before'] = before_oid

    # Use the WORKING base URLs pattern from transcription.py
    base_urls_to_try = [
        XELION_BASE_URL,  
        XELION_BASE_URL.replace('/wasteking', '/master'), 
        'https://lvsl01.xelion.com/api/v1/master', 
    ]
    
    for attempt in range(3):
        for base_url in base_urls_to_try:
            communications_url = f"{base_url.rstrip('/')}/communications"
            try:
                log_with_timestamp(f"Fetching from {communications_url} (attempt {attempt + 1})")
                response = xelion_session.get(communications_url, params=params, timeout=30) 
                
                if response.status_code == 401:
                    log_with_timestamp("🔑 401 error, re-authenticating...")
                    global session_token
                    session_token = None
                    
                    if xelion_login():
                        continue
                    else:
                        return [], None
                
                response.raise_for_status()
                data = response.json()
                communications = data.get('data', [])
                
                log_with_timestamp(f"Successfully fetched {len(communications)} communications from {base_url}")
                
                processing_stats['total_fetched'] += len(communications)
                processing_stats['last_poll_time'] = datetime.now().isoformat()
                
                # Track status breakdown
                status_breakdown = {}
                for comm in communications:
                    status = comm.get('object', {}).get('status', 'unknown')
                    status_breakdown[status] = status_breakdown.get(status, 0) + 1
                    processing_stats['statuses_seen'][status] = processing_stats['statuses_seen'].get(status, 0) + 1
                
                log_with_timestamp(f"Status breakdown: {status_breakdown}")
                
                next_before_oid = None
                if 'meta' in data and 'paging' in data['meta']:
                    next_before_oid = data['meta']['paging'].get('previousId')
                
                return communications, next_before_oid
                    
            except Exception as e:
                log_error(f"Failed to fetch from {base_url} (attempt {attempt + 1})", e)
                continue
    
    log_error("Failed to fetch communications from all URLs and attempts")
    return [], None

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
                
                if content_length > 1000:  # Valid audio file
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
                log_with_timestamp(f"❌ Unexpected response {response.status_code} for OID {communication_oid}")
                if attempt == 2:  # Last attempt
                    return None
                time.sleep(2)  # Wait before retry
                continue
                
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
        
        if file_size < 1000:
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
    """Process single call - try audio for ALL calls like transcription.py"""
    comm_obj = communication_data.get('object', {})
    oid = comm_obj.get('oid')
    
    if not oid:
        processing_stats['total_skipped'] += 1
        return None

    # Check if already processed
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT oid FROM calls WHERE oid = ?", (oid,))
        if cursor.fetchone():
            conn.close()
            return None
        conn.close()

    log_with_timestamp(f"🚀 Processing OID: {oid}")

    try:
        raw_data = json.dumps(communication_data)
        xelion_metadata = _extract_agent_info(comm_obj)
        call_datetime = comm_obj.get('date', 'Unknown')
        
        log_with_timestamp(f"📊 Call - Agent: {xelion_metadata['agent_name']}, Duration: {xelion_metadata['duration_seconds']}s, Status: {xelion_metadata['status']}")
        
        # TRY AUDIO FOR ALL CALLS - let Xelion API decide what exists
        log_with_timestamp(f"🎵 Attempting audio download for OID {oid} (ignoring status/duration)")
        audio_file_path = download_audio(oid)
        
        if not audio_file_path:
            # Store without audio with detailed reason
            call_category = "Missed/No Audio" if xelion_metadata['status'].lower() in ['missed', 'noanswer', 'cancelled'] else "No Audio"
            
            # Determine specific reason for no audio
            if xelion_metadata['status'].lower() in ['missed', 'noanswer']:
                audio_reason = f"Call was {xelion_metadata['status'].lower()} - no audio recorded"
            elif xelion_metadata['status'].lower() == 'cancelled':
                audio_reason = "Call was cancelled before connection - no audio recorded"
            elif xelion_metadata['duration_seconds'] < 5:
                audio_reason = f"Call too short ({xelion_metadata['duration_seconds']}s) - no meaningful audio"
            else:
                audio_reason = f"Audio file not available from Xelion API (Status: {xelion_metadata['status']}, Duration: {xelion_metadata['duration_seconds']}s)"
            
            with db_lock:
                conn = get_db_connection()
                cursor = conn.cursor()
                try:
                    cursor.execute('''
                        INSERT INTO calls (
                            oid, call_datetime, agent_name, phone_number, call_direction, 
                            duration_seconds, status, user_id, category, processed_at, 
                            raw_communication_data, summary_translation, processing_error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        oid, call_datetime, xelion_metadata['agent_name'], xelion_metadata['phone_number'],
                        xelion_metadata['call_direction'], xelion_metadata['duration_seconds'], xelion_metadata['status'],
                        xelion_metadata['user_id'], call_category, datetime.now().isoformat(), 
                        raw_data, audio_reason, audio_reason
                    ))
                    conn.commit()
                    processing_stats['total_processed'] += 1
                    log_with_timestamp(f"✅ Stored OID {oid} without audio")
                except Exception as e:
                    log_error(f"Database error storing OID {oid} (No Audio)", e)
                    processing_stats['total_errors'] += 1
                finally:
                    conn.close()
            return None

        # Transcribe Audio using WORKING pattern
        transcription_result = transcribe_audio_deepgram(audio_file_path, {'oid': oid})
        
        # Delete Audio File
        try:
            os.remove(audio_file_path)
            log_with_timestamp(f"🗑️ Deleted audio file: {audio_file_path}")
        except Exception as e:
            log_error(f"Error deleting audio file", e)

        if not transcription_result:
            processing_stats['total_errors'] += 1
            # Store with transcription failure
            with db_lock:
                conn = get_db_connection()
                cursor = conn.cursor()
                try:
                    cursor.execute('''
                        INSERT INTO calls (
                            oid, call_datetime, agent_name, phone_number, call_direction, 
                            duration_seconds, status, user_id, category, processed_at, 
                            raw_communication_data, summary_translation, processing_error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        oid, call_datetime, xelion_metadata['agent_name'], xelion_metadata['phone_number'],
                        xelion_metadata['call_direction'], xelion_metadata['duration_seconds'], xelion_metadata['status'],
                        xelion_metadata['user_id'], "Transcription Failed", datetime.now().isoformat(), 
                        raw_data, "Transcription failed", "Deepgram transcription failed"
                    ))
                    conn.commit()
                    processing_stats['total_processed'] += 1
                    log_with_timestamp(f"✅ Stored OID {oid} with transcription failure")
                except Exception as e:
                    log_error(f"Database error storing OID {oid} (Transcription Failed)", e)
                finally:
                    conn.close()
            return None

        # Analyze with OpenAI
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

        # Categorize Call
        call_category = categorize_call(transcription_result['transcription_text'])
        
        # Store in Database using WORKING pattern
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
    """Monitor for NEW calls only"""
    global background_process_running
    background_process_running = True
    
    log_with_timestamp("🚀 Starting NEW calls monitoring...")
    
    if not xelion_login():
        log_error("Xelion login failed")
        background_process_running = False
        return

    # Get last processed call timestamp - WITH BUFFER
    last_call_time = None
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(call_datetime) FROM calls WHERE call_datetime IS NOT NULL")
        result = cursor.fetchone()[0]
        if result:
            try:
                last_call_time = datetime.fromisoformat(result.replace('Z', '+00:00'))
                log_with_timestamp(f"📞 Last processed call: {last_call_time}")
            except:
                pass
        conn.close()

    # Start checking from appropriate time - WITH BUFFER
    if not last_call_time:
        check_since = datetime.now() - timedelta(minutes=30)  # Increased buffer
        log_with_timestamp("🆕 No previous calls - checking last 30 minutes")
    else:
        # IMPORTANT FIX: Add 2-minute buffer to catch any missed calls
        check_since = last_call_time - timedelta(minutes=2)
        log_with_timestamp(f"🔍 Checking for NEW calls since: {check_since} (with 2min buffer)")

    processed_this_session = set()

    with ThreadPoolExecutor(max_workers=3) as executor:
        while background_process_running:
            try:
                log_with_timestamp(f"🔄 Polling for NEW calls since {check_since.strftime('%Y-%m-%d %H:%M:%S')}")
                
                comms, _ = _fetch_communications_page(limit=50, until_date=datetime.now())
                
                if not comms:
                    log_with_timestamp("📭 No communications found")
                    time.sleep(30)
                    continue
                
                # Filter to NEW calls only - IMPROVED LOGIC
                new_comms = []
                latest_time = check_since
                
                log_with_timestamp(f"🔍 Examining {len(comms)} communications for new calls...")
                
                for comm in comms:
                    comm_obj = comm.get('object', {})
                    call_datetime = comm_obj.get('date', '')
                    oid = comm_obj.get('oid')
                    
                    if not call_datetime or not oid:
                        continue
                        
                    try:
                        # Parse datetime with better error handling
                        call_dt = None
                        if 'T' in call_datetime:
                            call_dt = datetime.fromisoformat(call_datetime.replace('Z', '+00:00'))
                        else:
                            # Try different datetime formats
                            for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f']:
                                try:
                                    call_dt = datetime.strptime(call_datetime, fmt)
                                    break
                                except:
                                    continue
                        
                        if not call_dt:
                            log_with_timestamp(f"⚠️ Could not parse datetime: {call_datetime}")
                            continue
                        
                        # Check if already processed in database
                        with db_lock:
                            conn = get_db_connection()
                            cursor = conn.cursor()
                            cursor.execute("SELECT oid FROM calls WHERE oid = ?", (oid,))
                            already_exists = cursor.fetchone() is not None
                            conn.close()
                        
                        # IMPROVED LOGIC: Process if call is newer than check_since AND not already in DB
                        if call_dt > check_since and not already_exists and oid not in processed_this_session:
                            new_comms.append(comm)
                            processed_this_session.add(oid)
                            latest_time = max(latest_time, call_dt)
                            log_with_timestamp(f"🆕 Found NEW call OID {oid} at {call_dt}")

                        elif call_dt <= check_since:
                            log_with_timestamp(f"⏭️ Skipping OID {oid} - too old ({call_dt} <= {check_since})")
                            
                    except Exception as e:
                        log_with_timestamp(f"⚠️ Error processing comm {oid}: {e}")
                        continue

                if new_comms:
                    log_with_timestamp(f"🎯 Processing {len(new_comms)} NEW calls")

                    # Process NEW calls
                    futures = []
                    for comm in new_comms:
                        futures.append(executor.submit(process_single_call, comm))

                    # Wait for results
                    for future in as_completed(futures):
                        try:
                            result_oid = future.result()
                            if result_oid:
                                log_with_timestamp(f"✅ Processed NEW call: {result_oid}")
                        except Exception as e:
                            log_error("Error processing call", e)

                    # Update check time - but keep the buffer
                    check_since = latest_time - timedelta(minutes=1)  # Keep 1min buffer
                else:
                    log_with_timestamp(f"📭 No NEW calls found (examined {len(comms)} communications)")

                log_with_timestamp(f"📊 Stats - Processed: {processing_stats['total_processed']}, Errors: {processing_stats['total_errors']}")
                time.sleep(30)
                
            except Exception as e:
                log_error("Error in monitoring loop", e)
                time.sleep(60)

    background_process_running = False
    log_with_timestamp("🛑 Monitoring stopped")

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

# --- Flask Routes ---
@app.route('/')
def index():
    """Auto-start monitoring"""
    global background_process_running, background_thread
    
    if not background_process_running:
        log_with_timestamp("🚀 AUTO-STARTING monitoring")
        background_thread = threading.Thread(target=fetch_and_transcribe_recent_calls)
        background_thread.daemon = True
        background_thread.start()
        log_with_timestamp("✅ Monitoring auto-started")
    
    return render_template('index.html')

@app.route('/status')
def get_status():
    """Get system status"""
    openai_test_result = test_openai_connection()
    return jsonify({
        "background_running": background_process_running,
        "processing_stats": processing_stats,
        "selenium_available": SELENIUM_AVAILABLE,
        "openai_available": OPENAI_AVAILABLE,
        "openai_connection_test": openai_test_result,
        "openai_api_key_configured": OPENAI_API_KEY and OPENAI_API_KEY != 'your_openai_api_key',
        "wasteking_session_valid": load_wasteking_session() is not None,
        "last_poll": processing_stats.get('last_poll_time', 'Never'),
        "last_error": processing_stats.get('last_error', 'None')
    })

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

        # Average ratings
        cursor.execute("""
            SELECT AVG(openai_engagement), AVG(openai_politeness), 
                   AVG(openai_professionalism), AVG(openai_resolution)
            FROM calls WHERE transcription_text IS NOT NULL
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
            FROM calls WHERE transcription_text IS NOT NULL
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
            FROM calls WHERE transcription_text IS NOT NULL
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
            FROM calls WHERE transcription_text IS NOT NULL
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
            FROM calls WHERE transcription_text IS NOT NULL
        """)
        avg_resolution_subs = cursor.fetchone()
        resolution_subs = {
            "issue_identification": round(avg_resolution_subs[0] or 0, 2),
            "solution_effectiveness": round(avg_resolution_subs[1] or 0, 2),
            "time_to_resolution": round(avg_resolution_subs[2] or 0, 2),
            "follow_up_next_steps": round(avg_resolution_subs[3] or 0, 2),
        }

        # Category ratings
        category_ratings = {}
        categories = ["SKIP", "man in van", "general enquiry", "complaint", "Missed/No Audio"] 
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
    """Get paginated calls list"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    agent_filter = request.args.get('agent', '')
    category_filter = request.args.get('category', '')
    audio_filter = request.args.get('audio_filter', 'with_audio')  # Default to with_audio
    
    offset = (page - 1) * per_page
    
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Build query with filters
        where_conditions = []
        params = []
        
        # Audio filter (default is with_audio)
        if audio_filter == 'with_audio':
            where_conditions.append("transcription_text IS NOT NULL AND transcription_text != ''")
        # If audio_filter == 'all', don't add any condition (show all calls)
        
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
        
        # Get calls
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

# --- ElevenLabs Endpoints ---
@app.route('/api/get-wasteking-prices', methods=['GET'])
def elevenlabs_get_wasteking_prices():
    """ElevenLabs webhook for WasteKing pricing"""
    log_with_timestamp("📞 ElevenLabs called WasteKing endpoint")
    
    try:
        result = get_wasteking_prices()
        return jsonify(result)
    except Exception as e:
        log_error("Error in ElevenLabs endpoint", e)
        return jsonify({
            "error": "Internal server error",
            "status": "error",
            "timestamp": datetime.now().isoformat()
        }), 500

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

if __name__ == '__main__':
    # Development only - production uses gunicorn
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
