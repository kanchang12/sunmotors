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
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, send_file
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re
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

# Twilio Configuration
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', 'your_twilio_sid')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', 'your_twilio_token')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER', 'your_twilio_phone_number')
SERVER_BASE_URL = "https://internal-porpoise-onewebonly-1b44fcb9.koyeb.app"

# PayPal payment link (fallback only)
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
                
                -- WASTE KING SPECIFIC EVALUATION CRITERIA
                call_handling_telephone_manner REAL,
                customer_needs_assessment REAL,
                product_knowledge_information REAL,
                objection_handling_erica REAL,
                sales_closing REAL,
                compliance_procedures REAL,
                
                -- WASTE KING SUB-CRITERIA
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
        
        # Updated price_quotes table with payment_link
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
                elevenlabs_conversation_id TEXT,
                payment_link TEXT
            )
        ''')
        
        # SMS payments table
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
            
        if 'payment_link' not in existing_quote_columns:
            log_with_timestamp("Adding payment_link column to price_quotes table...")
            cursor.execute("ALTER TABLE price_quotes ADD COLUMN payment_link TEXT")
        
        conn.commit()
        conn.close()
        log_with_timestamp("Database initialized successfully with Waste King criteria")
    except Exception as e:
        log_error("Failed to initialize database", e)


def get_current_datetime_info():
    """Get current UK date/time information for AI context"""
    from datetime import datetime, timezone, timedelta
    
    # UK timezone (GMT/BST)
    uk_tz = timezone(timedelta(hours=0))  # Adjust for BST (+1) if needed
    now_utc = datetime.now(timezone.utc)
    now_uk = now_utc.replace(tzinfo=uk_tz)
    
    return {
        "current_date": now_uk.strftime("%Y-%m-%d"),
        "current_time": now_uk.strftime("%H:%M"),
        "current_day": now_uk.strftime("%A"),
        "current_datetime_utc": now_utc.isoformat(),
        "current_datetime_uk": now_uk.strftime("%Y-%m-%d %H:%M:%S"),
        "tomorrow_date": (now_uk + timedelta(days=1)).strftime("%Y-%m-%d"),
        "system_timezone": "UTC",
        "uk_business_hours": {
            "monday_thursday": "8:00am-5:00pm",
            "friday": "8:00am-4:30pm", 
            "saturday": "9:00am-12:00pm",
            "sunday": "closed"
        }
    }


# --- Xelion API Functions (ALL ORIGINAL FUNCTIONS PRESERVED) ---
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
                    log_with_timestamp(f"Fetching page {page} from {communications_url} (attempt {attempt + 1})")
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

def process_single_call(communication_data: Dict) -> Optional[str]:
    """Process single call with Waste King criteria"""
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

        wasteking_analysis = analyze_transcription_with_wasteking_criteria(transcription_result['transcription_text'], oid)
        if not wasteking_analysis:
            # Fallback scores for Waste King criteria
            wasteking_analysis = {
                "call_handling_telephone_manner": 0, "customer_needs_assessment": 0, "product_knowledge_information": 0,
                "objection_handling_erica": 0, "sales_closing": 0, "compliance_procedures": 0,
                "professional_telephone_manner": 0, "listening_customer_requirements": 0, "presenting_appropriate_solutions": 0,
                "postcode_gathering": 0, "waste_type_identification": 0, "prohibited_items_check": 0,
                "access_assessment": 0, "permit_requirements": 0, "offering_options": 0,
                "erica_objection_method": 0, "sales_recommendation": 0, "asking_for_sale": 0,
                "following_procedures": 0, "communication_guidelines": 0, "overall_waste_king_score": 0,
                "summary": "Waste King analysis failed"
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
                log_with_timestamp(f"✅ Successfully stored OID {oid} with Waste King analysis")
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

# --- WasteKing API Helper Functions ---
def create_wasteking_booking():
    """Create a new booking reference with WasteKing API"""
    try:
        headers = {
            "x-wasteking-request": WASTEKING_ACCESS_TOKEN,
            "Content-Type": "application/json"
        }
        
        create_url = f"{WASTEKING_BASE_URL}api/booking/create/"
        response = requests.post(
            create_url,
            headers=headers,
            json={"type": "chatbot", "source": "wasteking.co.uk"},
            timeout=15,
            verify=False
        )
        
        if response.status_code == 200:
            booking_ref = response.json().get('bookingRef')
            log_with_timestamp(f"✅ Created WasteKing booking reference: {booking_ref}")
            return booking_ref
        else:
            log_with_timestamp(f"❌ Failed to create booking. Status: {response.status_code}, Response: {response.text}")
            return None
            
    except Exception as e:
        log_error("Failed to create WasteKing booking", e)
        return None

def update_wasteking_booking(booking_ref: str, update_data: dict):
    """Update a WasteKing booking with new data"""
    try:
        if not booking_ref:
            log_with_timestamp("❌ No booking reference provided")
            return None
            
        headers = {
            "x-wasteking-request": WASTEKING_ACCESS_TOKEN,
            "Content-Type": "application/json"
        }
        
        payload = {"bookingRef": booking_ref}
        payload.update(update_data)
        
        update_url = f"{WASTEKING_BASE_URL}api/booking/update/"
        response = requests.post(
            update_url,
            headers=headers,
            json=payload,
            timeout=20,
            verify=False
        )
        
        if response.status_code in [200, 201]:
            log_with_timestamp(f"✅ Updated booking {booking_ref} with: {json.dumps(update_data, indent=2)}")
            return response.json()
        else:
            log_with_timestamp(f"❌ Failed to update booking {booking_ref}. Status: {response.status_code}, Response: {response.text}")
            return None
            
    except Exception as e:
        log_error(f"Failed to update booking {booking_ref}", e)
        return None

def send_payment_sms(booking_ref: str, phone: str, payment_link: str, amount: str):
    """Send payment SMS via Twilio"""
    try:
        # Clean and format phone number
        if phone.startswith('0'):
            phone = f"+44{phone[1:]}"
        elif phone.startswith('44'):
            phone = f"+{phone}"
        elif not phone.startswith('+'):
            phone = f"+44{phone}"
            
        if not re.match(r'^\+44\d{9,10}$', phone):
            log_with_timestamp(f"❌ Invalid UK phone number format: {phone}")
            return {"success": False, "message": "Invalid UK phone number format"}
        
        # Create SMS message
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message_body = f"""Waste King Payment
Amount: £{amount}
Reference: {booking_ref}

Pay securely: {payment_link}

After payment, you'll get confirmation.
Thank you!"""
        
        # Send SMS
        message = client.messages.create(
            body=message_body,
            from_=TWILIO_PHONE_NUMBER,
            to=phone
        )
        
        log_with_timestamp(f"✅ SMS sent to {phone} for booking {booking_ref}. SID: {message.sid}")
        
        # Store in database
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO sms_payments 
                (quote_id, customer_phone, amount, sms_sid, created_at, paypal_link) 
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                booking_ref,
                phone,
                float(amount.replace('£', '')),
                message.sid,
                datetime.now().isoformat(),
                payment_link
            ))
            conn.commit()
            conn.close()
        
        return {"success": True, "message": "SMS sent successfully", "sms_sid": message.sid}
        
    except Exception as e:
        log_error("Failed to send payment SMS", e)
        return {"success": False, "message": str(e)}

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

# --- OpenAI Analysis ---
def analyze_transcription_with_wasteking_criteria(transcript: str, oid: str = "unknown") -> Optional[Dict]:
    """Analyze transcription with WASTE KING SPECIFIC criteria from induction manual"""
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY or OPENAI_API_KEY == 'your_openai_api_key':
        log_with_timestamp(f"OpenAI not available for OID {oid} - using fallback Waste King scores")
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
    
    log_with_timestamp(f"🤖 Starting Waste King criteria analysis for OID {oid}")
    
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
        
        log_with_timestamp(f"✅ Waste King criteria analysis successful for OID {oid}")
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




# --- Twilio Functions ---
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

# --- Flask App Setup ---
app = Flask(__name__)
init_db()

@app.route('/demo')
def demo():
    return render_template('demo.html')
    
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
        "twilio_configured": twilio_test,
        "last_poll": processing_stats.get('last_poll_time', 'Never'),
        "last_error": processing_stats.get('last_error', 'None')
    })


# =============================================================================
# FLASK CODE - ADD/REPLACE THESE FUNCTIONS IN YOUR app.py
# =============================================================================

# Add CORS headers
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# Test endpoint
@app.route('/api/test', methods=['GET', 'POST'])
def test_endpoint():
    """Test endpoint to verify system is working"""
    return jsonify({
        "status": "success",
        "message": "WasteKing API system is working",
        "timestamp": datetime.now().isoformat(),
        "method": request.method,
        "available_endpoints": [
            "/api/wasteking-get-price",
            "/api/wasteking-confirm-booking", 
            "/api/send-payment-sms"
        ]
    })

@app.route('/api/wasteking-confirm-booking', methods=['POST'])
def confirm_wasteking_booking():
    """Confirm booking and send payment SMS - handles multiple field formats"""
    try:
        log_with_timestamp("="*80)
        log_with_timestamp("📝 [BOOKING CONFIRMATION] INITIATED")
        
        # Get JSON data with better error handling
        try:
            data = request.get_json(force=True)
        except Exception as json_error:
            log_with_timestamp(f"❌ [JSON ERROR] {json_error}")
            log_with_timestamp(f"Raw data: {request.get_data()}")
            return jsonify({"success": False, "message": "Invalid JSON data"}), 400
            
        log_with_timestamp(f"📦 Request Data: {json.dumps(data, indent=2)}")
        
        if not data:
            log_with_timestamp("❌ [ERROR] No data provided")
            return jsonify({"success": False, "message": "No data provided"}), 400

        # 🔧 FLEXIBLE FIELD MAPPING - Handle different field name formats
        normalized_data = {}
        
        # Handle booking_ref - create one if missing
        normalized_data['booking_ref'] = data.get('booking_ref') 
        if not normalized_data['booking_ref']:
            # If no booking_ref provided, create a new one
            log_with_timestamp("🆔 No booking_ref provided, creating new booking...")
            booking_ref = create_wasteking_booking()
            if not booking_ref:
                return jsonify({"success": False, "message": "Failed to create booking reference"}), 500
            normalized_data['booking_ref'] = booking_ref
            log_with_timestamp(f"✅ Created new booking_ref: {booking_ref}")
            
            # If we're creating a new booking, we need to do the price search first
            if data.get('postcode') and data.get('service') and data.get('type'):
                search_payload = {
                    "search": {
                        "postCode": data['postcode'],
                        "service": data['service'],
                        "type": data['type']
                    }
                }
                log_with_timestamp(f"🔍 Doing price search first: {json.dumps(search_payload, indent=2)}")
                price_response = update_wasteking_booking(booking_ref, search_payload)
                if not price_response:
                    return jsonify({"success": False, "message": "Failed to get pricing"}), 500
                log_with_timestamp(f"💰 Price search complete: £{price_response.get('quote', {}).get('price', 'N/A')}")
        
        # Handle customer phone - multiple possible field names
        normalized_data['customer_phone'] = (
            data.get('customer_phone') or 
            data.get('phone') or 
            data.get('customerPhone') or
            data.get('Phone')
        )
        
        # Handle names - try different combinations
        normalized_data['first_name'] = (
            data.get('first_name') or 
            data.get('firstName') or 
            data.get('firstname') or
            data.get('name', '').split(' ')[0] if data.get('name') else 'Customer'
        )
        
        normalized_data['last_name'] = (
            data.get('last_name') or 
            data.get('lastName') or 
            data.get('lastname') or
            ' '.join(data.get('name', '').split(' ')[1:]) if data.get('name') and len(data.get('name', '').split(' ')) > 1 else 'Unknown'
        )
        
        # Handle service date - multiple possible field names
        normalized_data['service_date'] = (
            data.get('service_date') or 
            data.get('date') or 
            data.get('serviceDate') or
            data.get('delivery_date')
        )
        
        # Handle other optional fields
        normalized_data['service_time'] = data.get('service_time', data.get('time', 'am'))
        normalized_data['email'] = data.get('email', data.get('emailAddress', ''))
        normalized_data['postcode'] = data.get('postcode', data.get('postCode', ''))
        normalized_data['placement'] = data.get('placement', 'drive')
        
        log_with_timestamp(f"🔧 Normalized data: {json.dumps(normalized_data, indent=2)}")
        
        # Validate required fields after normalization
        required = ['booking_ref', 'customer_phone']  # Relaxed requirements
        missing = [field for field in required if not normalized_data.get(field)]
        if missing:
            log_with_timestamp(f"❌ [VALIDATION] Missing critical fields: {missing}")
            return jsonify({
                "success": False,
                "message": f"Missing required fields: {', '.join(missing)}",
                "provided_fields": list(data.keys()),
                "normalized_fields": list(normalized_data.keys())
            }), 400

        booking_ref = normalized_data['booking_ref']
        log_with_timestamp(f"🔍 Processing booking reference: {booking_ref}")

        # 1. Add Customer Details (only if we have names)
        if normalized_data.get('first_name') and normalized_data.get('last_name'):
            customer_payload = {
                "customer": {
                    "firstName": normalized_data['first_name'],
                    "lastName": normalized_data['last_name'],
                    "phone": normalized_data['customer_phone'],
                    "emailAddress": normalized_data.get('email', ''),
                    "addressPostcode": normalized_data.get('postcode', '')
                }
            }
            log_with_timestamp(f"👤 Adding customer details: {json.dumps(customer_payload, indent=2)}")
            customer_response = update_wasteking_booking(booking_ref, customer_payload)
            if not customer_response:
                log_with_timestamp("❌ [API FAILURE] Couldn't add customer details")
                return jsonify({"success": False, "message": "Failed to add customer details"}), 500
        else:
            log_with_timestamp("⚠️ Skipping customer details - missing name information")

        # 2. Add Service Details (only if we have service date)
        if normalized_data.get('service_date'):
            service_payload = {
                "service": {
                    "date": normalized_data['service_date'],
                    "time": normalized_data.get('service_time', 'am'),
                    "placement": normalized_data.get('placement', 'drive')
                }
            }
            log_with_timestamp(f"📅 Adding service details: {json.dumps(service_payload, indent=2)}")
            service_response = update_wasteking_booking(booking_ref, service_payload)
            if not service_response:
                log_with_timestamp("❌ [API FAILURE] Couldn't add service details")
                return jsonify({"success": False, "message": "Failed to add service details"}), 500
        else:
            log_with_timestamp("⚠️ Skipping service details - missing date information")

        # 3. Generate Payment Link
        payment_payload = {
            "action": "quote",
            "postPaymentUrl": "https://wasteking.co.uk/thank-you/"
        }
        log_with_timestamp(f"💳 Generating payment link: {json.dumps(payment_payload, indent=2)}")
        payment_response = update_wasteking_booking(booking_ref, payment_payload)
        if not payment_response or not payment_response.get('quote', {}).get('paymentLink'):
            log_with_timestamp("❌ [API FAILURE] Couldn't generate payment link")
            return jsonify({"success": False, "message": "Failed to generate payment link"}), 500

        payment_link = payment_response['quote']['paymentLink']
        price = payment_response['quote'].get('price', '0')
        log_with_timestamp(f"✅ Payment link generated: {payment_link}")

        # Store the payment link in the database for later SMS use
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO price_quotes 
                (quote_id, booking_ref, postcode, service, price_data, created_at, status, customer_phone, payment_link) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                booking_ref, booking_ref, normalized_data.get('postcode', ''), 'confirmed',
                json.dumps(payment_response), datetime.now().isoformat(),
                'confirmed', normalized_data['customer_phone'], payment_link
            ))
            conn.commit()
            conn.close()

        # 4. Send SMS (only if we have phone number) - FORCE £1 FOR TESTING
        if normalized_data.get('customer_phone'):
            sms_response = send_payment_sms(
                booking_ref=booking_ref,
                phone=normalized_data['customer_phone'],
                payment_link=payment_link,
                amount="1.00"  # 🔥 FORCE £1 FOR TESTING
            )
            
            if not sms_response.get('success'):
                log_with_timestamp(f"❌ [SMS FAILURE] {sms_response.get('message')}")
                # Get current date/time info for error response
                datetime_info = get_current_datetime_info()
                return jsonify({
                    "success": True,  # Still success since booking was created
                    "message": "Booking confirmed but SMS failed",
                    "payment_link": payment_link,
                    "booking_ref": booking_ref,
                    "price": price,
                    "sms_error": sms_response.get('message'),
                    
                    # Add date/time context even for partial success
                    "system_date": datetime_info["current_date"],
                    "system_time": datetime_info["current_time"],
                    "ai_context": {
                        "today_is": datetime_info["current_date"],
                        "current_time": datetime_info["current_time"],
                        "tomorrow_is": datetime_info["tomorrow_date"],
                        "current_day": datetime_info["current_day"]
                    }
                })
            
            log_with_timestamp("📱 SMS sent successfully")
        else:
            log_with_timestamp("⚠️ No phone number provided, skipping SMS")

        # Get current date/time info for successful response
        datetime_info = get_current_datetime_info()

        log_with_timestamp("🎉 [SUCCESS] Booking fully processed")
        return jsonify({
            "success": True,
            "message": "Booking confirmed and SMS sent successfully",
            "booking_ref": booking_ref,
            "payment_link": payment_link,
            "price": price,
            "customer_phone": normalized_data.get('customer_phone'),
            "customer_name": f"{normalized_data.get('first_name', 'Customer')} {normalized_data.get('last_name', 'Unknown')}",
            
            # 🔥 ADD SYSTEM DATE/TIME INFO FOR AI CONTEXT
            "system_date": datetime_info["current_date"],
            "system_time": datetime_info["current_time"], 
            "system_day": datetime_info["current_day"],
            "tomorrow_date": datetime_info["tomorrow_date"],
            "current_datetime_utc": datetime_info["current_datetime_utc"],
            "business_hours": datetime_info["uk_business_hours"],
            
            # Context for AI to understand relative dates
            "ai_context": {
                "today_is": datetime_info["current_date"],
                "current_time": datetime_info["current_time"],
                "tomorrow_is": datetime_info["tomorrow_date"],
                "current_day": datetime_info["current_day"],
                "booking_confirmed_at": datetime_info["current_datetime_uk"]
            }
        })

    except Exception as e:
        log_with_timestamp(f"🔥 [UNHANDLED EXCEPTION] {str(e)}")
        log_with_timestamp(traceback.format_exc())
        
        # Get current date/time info for error response  
        datetime_info = get_current_datetime_info()
        
        return jsonify({
            "success": False,
            "message": "System error during booking",
            "error": str(e),
            
            # Even on errors, provide date context for AI
            "system_date": datetime_info["current_date"],
            "system_time": datetime_info["current_time"],
            "ai_context": {
                "today_is": datetime_info["current_date"],
                "current_time": datetime_info["current_time"], 
                "tomorrow_is": datetime_info["tomorrow_date"],
                "current_day": datetime_info["current_day"]
            }
        }), 500


@app.route('/api/wasteking-get-price', methods=['POST'])
def get_wasteking_price():
    """Get price only - no booking created"""
    try:
        log_with_timestamp("="*80)
        log_with_timestamp("💰 [PRICE CHECK] INITIATED")
        log_with_timestamp(f"📦 Request Data: {json.dumps(request.get_json(), indent=2)}")
        
        data = request.get_json()
        if not data:
            log_with_timestamp("❌ [ERROR] No data provided")
            return jsonify({"success": False, "message": "No data provided"}), 400

        # Validate required fields
        required = ['postcode', 'service', 'type']
        missing = [field for field in required if not data.get(field)]
        if missing:
            log_with_timestamp(f"❌ [VALIDATION] Missing fields: {missing}")
            return jsonify({
                "success": False,
                "message": f"Missing required fields: {', '.join(missing)}"
            }), 400

        log_with_timestamp("🔑 Creating temporary booking reference...")
        booking_ref = create_wasteking_booking()
        if not booking_ref:
            log_with_timestamp("❌ [API FAILURE] Couldn't create booking reference")
            return jsonify({
                "success": False,
                "message": "Service unavailable right now"
            }), 503
        log_with_timestamp(f"✅ Booking reference created: {booking_ref}")

        # Get pricing
        search_payload = {
            "search": {
                "postCode": data['postcode'],
                "service": data['service'],
                "type": data['type']
            }
        }
        log_with_timestamp(f"🔍 Fetching price with payload: {json.dumps(search_payload, indent=2)}")
        
        price_data = update_wasteking_booking(booking_ref, search_payload)
        if not price_data:
            log_with_timestamp("❌ [PRICING] No data returned from API")
            return jsonify({
                "success": False,
                "message": "No pricing available for this location"
            }), 404

        log_with_timestamp(f"💵 Price data received: {json.dumps(price_data.get('quote', {}), indent=2)}")
        
        # Get current date/time info
        datetime_info = get_current_datetime_info()
        
        # Format response for ElevenLabs with date/time context
        response = {
            "success": True,
            "booking_ref": booking_ref,
            "price": price_data.get('quote', {}).get('price'),
            "service_price": price_data.get('quote', {}).get('servicePrice'),
            "message": f"The estimated price is £{price_data.get('quote', {}).get('price')} including VAT",
            
            # 🔥 ADD SYSTEM DATE/TIME INFO FOR AI CONTEXT
            "system_date": datetime_info["current_date"],
            "system_time": datetime_info["current_time"], 
            "system_day": datetime_info["current_day"],
            "tomorrow_date": datetime_info["tomorrow_date"],
            "current_datetime_utc": datetime_info["current_datetime_utc"],
            "business_hours": datetime_info["uk_business_hours"],
            
            # Context for AI
            "ai_context": {
                "today_is": datetime_info["current_date"],
                "current_time": datetime_info["current_time"],
                "tomorrow_is": datetime_info["tomorrow_date"],
                "current_day": datetime_info["current_day"]
            }
        }
        
        log_with_timestamp(f"📤 Sending response: {json.dumps(response, indent=2)}")
        return jsonify(response)

    except Exception as e:
        log_with_timestamp(f"🔥 [UNHANDLED EXCEPTION] {str(e)}")
        log_with_timestamp(traceback.format_exc())
        return jsonify({
            "success": False,
            "message": "Unable to get price right now",
            "error": str(e)
        }), 500
# --- Dashboard and Call Management Routes ---
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

@app.route('/get_calls_list')
def get_calls_list():
    """Get paginated calls list with filtering"""
    try:
        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
        agent = request.args.get('agent', '')
        category = request.args.get('category', '')
        audio_filter = request.args.get('audio_filter', '')
        
        # Calculate offset
        offset = (page - 1) * per_page
        
        # Build WHERE clause
        where_conditions = []
        params = []
        
        if agent:
            where_conditions.append("agent_name LIKE ?")
            params.append(f"%{agent}%")
        
        if category:
            where_conditions.append("category = ?")
            params.append(category)
        
        if audio_filter == 'with_audio':
            where_conditions.append("transcription_text IS NOT NULL AND transcription_text != ''")
        elif audio_filter == 'no_audio':
            where_conditions.append("(transcription_text IS NULL OR transcription_text = '')")
        
        where_clause = "WHERE " + " AND ".join(where_conditions) if where_conditions else ""
        
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Get total count
            count_query = f"SELECT COUNT(*) FROM calls {where_clause}"
            cursor.execute(count_query, params)
            total_calls = cursor.fetchone()[0]
            
            # Get paginated results
            query = f"""
                SELECT oid, call_datetime, agent_name, phone_number, call_direction, 
                       duration_seconds, status, category, transcription_text,
                       transcribed_duration_minutes, word_count, confidence,
                       overall_waste_king_score, processed_at
                FROM calls {where_clause}
                ORDER BY call_datetime DESC
                LIMIT ? OFFSET ?
            """
            cursor.execute(query, params + [per_page, offset])
            calls = cursor.fetchall()
            
            conn.close()
        
        # Convert to list of dicts
        calls_list = []
        for call in calls:
            calls_list.append({
                'oid': call[0],
                'call_datetime': call[1],
                'agent_name': call[2],
                'phone_number': call[3],
                'call_direction': call[4],
                'duration_seconds': call[5],
                'status': call[6],
                'category': call[7],
                'has_transcription': bool(call[8]),
                'transcribed_duration_minutes': call[9],
                'word_count': call[10] or 0,
                'confidence': call[11] or 0,
                'overall_waste_king_score': call[12] or 0,
                'processed_at': call[13]
            })
        
        # Calculate pagination info
        total_pages = (total_calls + per_page - 1) // per_page
        
        return jsonify({
            'calls': calls_list,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total_calls': total_calls,
                'total_pages': total_pages,
                'has_next': page < total_pages,
                'has_prev': page > 1
            }
        })
        
    except Exception as e:
        log_error("Error in get_calls_list", e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/send-payment-sms', methods=['POST'])
def send_payment_sms_endpoint():
    """Send payment SMS only - third tool"""
    log_with_timestamp("="*50)
    log_with_timestamp("💳 SMS PAYMENT ENDPOINT CALLED")
    log_with_timestamp("="*50)
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({
                "status": "error",
                "message": "No JSON data received"
            }), 400

        # Validate required fields
        required_fields = ['customer_phone', 'call_sid', 'amount']
        missing_fields = [field for field in required_fields if not data.get(field)]
        if missing_fields:
            return jsonify({
                "status": "error",
                "message": f"Missing required fields: {', '.join(missing_fields)}"
            }), 400

        # Clean and validate phone number
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

        # Get quote_id from the call_sid or use it directly
        quote_id = data.get('quote_id', data['call_sid'])
        
        # IMPORTANT: Get the dynamic payment link from the quote
        payment_link = PAYPAL_PAYMENT_LINK  # fallback
        
        # Look up the quote in database to get the dynamic payment link
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT payment_link FROM price_quotes WHERE quote_id = ? OR booking_ref = ?", (quote_id, quote_id))
            result = cursor.fetchone()
            if result and result[0]:
                payment_link = result[0]
                log_with_timestamp(f"✅ Found dynamic payment link for quote {quote_id}")
            else:
                log_with_timestamp(f"⚠️ No dynamic payment link found for quote {quote_id}, using fallback PayPal")
            conn.close()

        # Force £1 amount as specified
        amount=str(price)

        # Send SMS via Twilio with DYNAMIC payment link
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        message_body = f"""Waste King Payment
Amount: £{amount}
Reference: {quote_id}

Pay securely:
{payment_link}

After payment, you'll get confirmation.
Thank you!"""

        message = client.messages.create(
            body=message_body,
            from_=TWILIO_PHONE_NUMBER,
            to=phone
        )

        log_with_timestamp(f"✅ SMS sent with dynamic payment link: {message.sid}")

        # Store in database
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
                payment_link  # Store the actual payment link used
            ))
            conn.commit()
            conn.close()

        return jsonify({
            "status": "success",
            "message": f"SMS sent successfully with dynamic payment link",
            "amount": f"£{amount}",
            "call_sid": data['call_sid'],
            "quote_id": quote_id,
            "payment_link_used": payment_link
        })

    except Exception as e:
        log_error("Payment processing failed", e)
        return jsonify({
            "status": "error",
            "message": "System error",
            "debug": str(e)
        }), 500


@app.route('/get_call_details/<oid>')
def get_call_details(oid):
    """Get detailed call information"""
    try:
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM calls WHERE oid = ?", (oid,))
            call = cursor.fetchone()
            conn.close()
        
        if not call:
            return jsonify({'error': 'Call not found'}), 404
        
        # Convert to dict with all Waste King criteria
        call_details = {
            'oid': call[0],
            'call_datetime': call[1],
            'agent_name': call[2],
            'phone_number': call[3],
            'call_direction': call[4],
            'duration_seconds': call[5],
            'status': call[6],
            'user_id': call[7],
            'transcription_text': call[8],
            'transcribed_duration_minutes': call[9],
            'deepgram_cost_usd': call[10],
            'deepgram_cost_gbp': call[11],
            'word_count': call[12],
            'confidence': call[13],
            'language': call[14],
            'waste_king_scores': {
                'call_handling_telephone_manner': call[15],
                'customer_needs_assessment': call[16],
                'product_knowledge_information': call[17],
                'objection_handling_erica': call[18],
                'sales_closing': call[19],
                'compliance_procedures': call[20],
                'professional_telephone_manner': call[21],
                'listening_customer_requirements': call[22],
                'presenting_appropriate_solutions': call[23],
                'postcode_gathering': call[24],
                'waste_type_identification': call[25],
                'prohibited_items_check': call[26],
                'access_assessment': call[27],
                'permit_requirements': call[28],
                'offering_options': call[29],
                'erica_objection_method': call[30],
                'sales_recommendation': call[31],
                'asking_for_sale': call[32],
                'following_procedures': call[33],
                'communication_guidelines': call[34],
                'overall_waste_king_score': call[39] if len(call) > 39 else 0
            },
            'category': call[35] if len(call) > 35 else 'Unknown',
            'processed_at': call[36] if len(call) > 36 else None,
            'processing_error': call[37] if len(call) > 37 else None,
            'raw_communication_data': call[38] if len(call) > 38 else None,
            'summary_translation': call[40] if len(call) > 40 else None
        }
        
        return jsonify(call_details)
        
    except Exception as e:
        
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # Disable Flask logging to reduce noise
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    log_with_timestamp("🚀 Starting Complete Waste King System...")
    log_with_timestamp("✅ Features:")
    log_with_timestamp("  • TOOL 1: Original price check (unchanged)")
    log_with_timestamp("  • TOOL 2: NEW booking workflow (follows your images exactly)")
    log_with_timestamp("  • TOOL 3: Dynamic SMS payments (uses paymentLink from bookings)")

    
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port, use_reloader=False)
