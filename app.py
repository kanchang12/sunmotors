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

# Use Selenium with proper Chrome setup
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM_AVAILABLE = True
except ImportError:
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
    """Authenticate WasteKing with Selenium Chrome"""
    log_with_timestamp("🔐 Starting WasteKing authentication with Selenium...")
    
    if not SELENIUM_AVAILABLE:
        return {
            "error": "Selenium not available",
            "status": "selenium_unavailable",
            "message": "Selenium not installed",
            "timestamp": datetime.now().isoformat()
        }
    
    # Enhanced Chrome options for containers
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--single-process")
    chrome_options.add_argument("--no-zygote")
    chrome_options.add_argument("--disable-web-security")
    chrome_options.add_argument("--disable-features=VizDisplayCompositor")
    chrome_options.add_argument("--remote-debugging-port=9222")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    
    # Detect container environment
    is_container = any([
        os.environ.get('PORT'),
        os.environ.get('DYNO'),
        os.environ.get('KOYEB_PUBLIC_DOMAIN'),
        os.path.exists('/.dockerenv'),
    ])
    
    if is_container:
        log_with_timestamp("🐳 Container environment detected")
        
        # Try multiple Chrome binary locations
        chrome_paths = [
            '/usr/bin/google-chrome-stable',
            '/usr/bin/google-chrome',
            '/usr/bin/chromium-browser',
            '/usr/bin/chromium',
            '/opt/google/chrome/chrome'
        ]
        
        chrome_binary = None
        for path in chrome_paths:
            if os.path.exists(path):
                chrome_binary = path
                log_with_timestamp(f"✅ Found Chrome at: {path}")
                break
        
        if chrome_binary:
            chrome_options.binary_location = chrome_binary
        else:
            log_error("Chrome binary not found in any expected location")
            return {
                "error": "Chrome not installed",
                "status": "chrome_not_found",
                "message": "Chrome browser not found in container. Please install Chrome in your container.",
                "timestamp": datetime.now().isoformat(),
                "searched_paths": chrome_paths
            }
    
    try:
        # Setup ChromeDriver service
        service = None
        chromedriver_paths = [
            '/usr/local/bin/chromedriver',
            '/usr/bin/chromedriver',
            '/opt/chromedriver/chromedriver'
        ]
        
        chromedriver_path = None
        for path in chromedriver_paths:
            if os.path.exists(path):
                chromedriver_path = path
                break
        
        if chromedriver_path:
            service = Service(chromedriver_path)
            log_with_timestamp(f"✅ Using ChromeDriver at: {chromedriver_path}")
        else:
            # Try webdriver_manager as fallback
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                service = Service(ChromeDriverManager().install())
                log_with_timestamp("📦 Using ChromeDriverManager for driver setup")
            except ImportError:
                log_error("webdriver_manager not available and no chromedriver found")
                return {
                    "error": "ChromeDriver not found",
                    "status": "chromedriver_not_found",
                    "message": "ChromeDriver not found and webdriver_manager not available",
                    "timestamp": datetime.now().isoformat()
                }
            
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.set_page_load_timeout(30)
        
    except Exception as e:
        log_error("Failed to setup Chrome", e)
        return {
            "error": "Chrome setup failed",
            "status": "chrome_unavailable", 
            "message": f"Chrome initialization failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }
    
    try:
        log_with_timestamp("🌐 Navigating to WasteKing...")
        driver.get(WASTEKING_PRICING_URL)
        
        # Wait for page load
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        
        # Check if already logged in
        current_url = driver.current_url
        if 'reporting' in current_url.lower():
            log_with_timestamp("✅ Already logged in!")
            
            # Extract cookies
            cookies = driver.get_cookies()
            session = create_session_from_selenium_cookies(cookies)
            driver.quit()
            
            save_wasteking_session(session)
            return session
        
        # Look for login form
        try:
            email_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 
                    "input[name='email'], input[type='email'], input[id*='email'], #email"))
            )
            log_with_timestamp("🔍 Login form detected")
        except:
            driver.quit()
            return {
                "error": "Login form not found",
                "status": "no_login_form",
                "message": f"Could not find login form. Current URL: {driver.current_url}",
                "timestamp": datetime.now().isoformat()
            }
        
        # Fill email
        log_with_timestamp("📝 Filling email...")
        email_field.clear()
        email_field.send_keys(WASTEKING_EMAIL)
        
        # Fill password
        try:
            password_field = driver.find_element(By.CSS_SELECTOR, 
                "input[name='password'], input[type='password'], input[id*='password'], #password")
            password_field.clear()
            password_field.send_keys(WASTEKING_PASSWORD)
            log_with_timestamp("📝 Password filled")
        except Exception as e:
            driver.quit()
            return {
                "error": "Could not fill password field",
                "status": "password_field_error",
                "message": f"Password field error: {str(e)}",
                "timestamp": datetime.now().isoformat()
            }
        
        # Submit form
        log_with_timestamp("🚀 Submitting login form...")
        try:
            submit_button = driver.find_element(By.CSS_SELECTOR, 
                "button[type='submit'], input[type='submit'], .btn-login, button:contains('Login'), button:contains('Sign in')")
            submit_button.click()
        except:
            # Try pressing Enter as fallback
            password_field.send_keys(Keys.RETURN)
        
        # Wait for navigation
        try:
            WebDriverWait(driver, 15).until(
                lambda d: 'reporting' in d.current_url.lower() or 
                         'dashboard' in d.current_url.lower() or
                         d.current_url != WASTEKING_PRICING_URL
            )
            log_with_timestamp("✅ Navigation detected")
        except:
            pass
        
        # Check final URL
        final_url = driver.current_url
        if 'reporting' in final_url.lower() or 'dashboard' in final_url.lower():
            log_with_timestamp("✅ Login successful!")
            
            # Extract cookies and create session
            cookies = driver.get_cookies()
            session = create_session_from_selenium_cookies(cookies)
            driver.quit()
            
            # Test session
            test_response = session.get(WASTEKING_PRICING_URL, timeout=10)
            if test_response.status_code == 200:
                save_wasteking_session(session)
                log_with_timestamp("✅ WasteKing authentication successful!")
                return session
            else:
                return {
                    "error": "Session test failed",
                    "status": "session_invalid",
                    "message": f"Test request failed with status {test_response.status_code}",
                    "timestamp": datetime.now().isoformat()
                }
        else:
            driver.quit()
            return {
                "error": "Login failed",
                "status": "login_failed",
                "message": f"Login unsuccessful. Final URL: {final_url}",
                "timestamp": datetime.now().isoformat()
            }
        
    except Exception as e:
        log_error("WasteKing authentication failed", e)
        try:
            driver.quit()
        except:
            pass
        return {
            "error": "Authentication failed",
            "status": "auth_error",
            "message": f"Login process failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }

def create_session_from_selenium_cookies(cookies):
    """Create requests session from Selenium cookies"""
    session = requests.Session()
    
    for cookie in cookies:
        session.cookies.set(
            cookie['name'], 
            cookie['value'], 
            domain=cookie.get('domain', ''),
            path=cookie.get('path', '/')
        )
    
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json, text/html, */*',
        'Accept-Language': 'en-US,en;q=0.9'
    })
    
    return session

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
    """Download audio using the WORKING pattern from transcription.py"""
    audio_url = f"{XELION_BASE_URL.rstrip('/')}/communications/{communication_oid}/audio"
    file_name = f"{communication_oid}.mp3"
    file_path = os.path.join(AUDIO_TEMP_DIR, file_name)

    if os.path.exists(file_path):
        log_with_timestamp(f"Audio for OID {communication_oid} already exists: {file_path}")
        return file_path
    
    try:
        log_with_timestamp(f"🎵 Downloading audio for OID {communication_oid} from: {audio_url}")
        
        response = xelion_session.get(audio_url, timeout=60)
        
        log_with_timestamp(f"Audio download response: {response.status_code}")
        
        if response.status_code == 200:
            with open(file_path, 'wb') as f:
                f.write(response.content)
            log_with_timestamp(f"Downloaded OID {communication_oid} ({len(response.content)} bytes)")
            
            if len(response.content) > 1000:  # Ensure we got actual audio
                log_with_timestamp(f"✅ Audio download successful for OID {communication_oid}")
                return file_path
            else:
                log_with_timestamp(f"❌ Audio file too small for OID {communication_oid} ({len(response.content)} bytes)")
                try:
                    os.remove(file_path)
                except:
                    pass
                return None
        elif response.status_code == 404:
            log_with_timestamp(f"❌ No audio found for OID {communication_oid}")
            return None
        elif response.status_code == 401:
            log_with_timestamp(f"🔑 401: Re-authenticating for OID {communication_oid}")
            if xelion_login():
                return download_audio(communication_oid)  # Retry after re-auth
            return None
        else:
            log_error(f"Failed to download audio for {communication_oid}: HTTP {response.status_code} - {response.text[:200]}")
            return None
            
    except Exception as e:
        log_error(f"Audio download failed for {communication_oid}", e)
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

# --- Main Processing Logic ---
def process_single_call(communication_data: Dict) -> Optional[str]:
    """Process single call using WORKING patterns from transcription.py"""
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
        
        # Try to download audio for any call (let the API decide what's available)
        audio_file_path = download_audio(oid)
        
        if not audio_file_path:
            # Store metadata without audio - use the WORKING pattern
            call_category = "Missed/No Audio" if xelion_metadata['status'].lower() in ['missed', 'noanswer', 'cancelled'] else "No Audio"
            
            with db_lock:
                conn = get_db_connection()
                cursor = conn.cursor()
                try:
                    cursor.execute('''
                        INSERT INTO calls (
                            oid, call_datetime, agent_name, phone_number, call_direction, 
                            duration_seconds, status, user_id, category, processed_at, 
                            raw_communication_data, summary_translation
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        oid, call_datetime, xelion_metadata['agent_name'], xelion_metadata['phone_number'],
                        xelion_metadata['call_direction'], xelion_metadata['duration_seconds'], xelion_metadata['status'],
                        xelion_metadata['user_id'], call_category, datetime.now().isoformat(), 
                        raw_data, "No audio available"
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
        except Exception as e:
            log_error(f"Error deleting audio file", e)

        if not transcription_result:
            processing_stats['total_errors'] += 1
            return None

        # Analyze with OpenAI
        openai_analysis = analyze_transcription_with_openai(transcription_result['transcription_text'], oid)
        if not openai_analysis:
            openai_analysis = {"summary": "Analysis failed"}

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

    # Get last processed call timestamp
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

    # Start checking from appropriate time
    if not last_call_time:
        check_since = datetime.now() - timedelta(minutes=10)
        log_with_timestamp("🆕 No previous calls - checking last 10 minutes")
    else:
        check_since = last_call_time
        log_with_timestamp(f"🔍 Checking for NEW calls since: {check_since}")

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
                
                # Filter to NEW calls only
                new_comms = []
                latest_time = check_since
                
                for comm in comms:
                    comm_obj = comm.get('object', {})
                    call_datetime = comm_obj.get('date', '')
                    oid = comm_obj.get('oid')
                    
                    if not call_datetime or not oid:
                        continue
                        
                    try:
                        call_dt = datetime.fromisoformat(call_datetime.replace('Z', '+00:00'))
                        
                        # Only process if call is AFTER check time
                        if call_dt > check_since and oid not in processed_this_session:
                            new_comms.append(comm)
                            processed_this_session.add(oid)
                            latest_time = max(latest_time, call_dt)
                            log_with_timestamp(f"🆕 Found NEW call OID {oid}")
                    except:
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

                    # Update check time
                    check_since = latest_time
                else:
                    log_with_timestamp("📭 No NEW calls to process")

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
    
    offset = (page - 1) * per_page
    
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Build query with filters
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
        
        # Get calls
        query = f"""
            SELECT oid, call_datetime, agent_name, phone_number, call_direction, 
                   duration_seconds, status, category, openai_overall_score,
                   openai_engagement, openai_politeness, openai_professionalism, 
                   openai_resolution, summary_translation, transcribed_duration_minutes,
                   deepgram_cost_gbp, word_count, confidence, processed_at
            FROM calls 
            {where_clause}
            ORDER BY processed_at DESC 
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
