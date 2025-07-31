import os
import requests
import json
import csv
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

# Attempt Deepgram SDK import
try:
    from deepgram import DeepgramClient, PrerecordedOptions, FileSource
    DEEPGRAM_SDK_AVAILABLE = True
except ImportError:
    DEEPGRAM_SDK_AVAILABLE = False

# Attempt OpenAI import
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# --- Configuration ---
# Xelion API (Replace with your actual Xelion details)
XELION_BASE_URL = os.getenv('XELION_BASE_URL', 'https://lvsl01.xelion.com/api/v1/wasteking')
XELION_USERNAME = os.getenv('XELION_USERNAME', 'your_xelion_username')
XELION_PASSWORD = os.getenv('XELION_PASSWORD', 'your_xelion_password')
XELION_APP_KEY = os.getenv('XELION_APP_KEY', 'your_xelion_app_key')
XELION_USERSPACE = os.getenv('XELION_USERSPACE', 'transcriber-abi-housego')

# Deepgram API
DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY', 'your_deepgram_api_key')

# OpenAI API (Replace with your actual OpenAI API Key)
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', 'your_openai_api_key')

# Database configuration
DATABASE_FILE = 'calls.db'

# Directory for temporary audio files
AUDIO_TEMP_DIR = 'temp_audio'
os.makedirs(AUDIO_TEMP_DIR, exist_ok=True)

# Deepgram pricing and currency conversion
DEEPGRAM_PRICE_PER_MINUTE = 0.0043  # $0.0043 per minute for Nova-2 model
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
    
    # Update global error tracking
    processing_stats['last_error'] = f"{message}: {error}" if error else message

# --- Database Initialization Function ---
def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    log_with_timestamp("Attempting to initialize database...")
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
                raw_communication_data TEXT
            )
        ''')
        conn.commit()
        conn.close()
        log_with_timestamp("Database initialized successfully (or already exists)")
    except sqlite3.Error as e:
        log_error("Failed to initialize database", e)
    except Exception as e:
        log_error("Unexpected error during database initialization", e)

app = Flask(__name__)
init_db()

# --- Xelion API Functions ---
def xelion_login() -> bool:
    global session_token
    with login_lock:
        if session_token:
            log_with_timestamp("Xelion token already exists. Reusing")
            return True

        login_url = f"{XELION_BASE_URL.rstrip('/')}/me/login"
        headers = {"Content-Type": "application/json"}
        
        data_payload = { 
            "userName": XELION_USERNAME, 
            "password": XELION_PASSWORD,
            "userSpace": XELION_USERSPACE,
            "appKey": XELION_APP_KEY
        }
        
        log_with_timestamp(f"Attempting Xelion login for {XELION_USERNAME} with userSpace: {XELION_USERSPACE}")
        try:
            response = xelion_session.post(login_url, headers=headers, data=json.dumps(data_payload))
            response.raise_for_status() 
            
            login_response = response.json()
            session_token = login_response.get("authentication")
            xelion_session.headers.update({"Authorization": f"xelion {session_token}"})
            log_with_timestamp(f"Successfully logged in to Xelion (Valid until: {login_response.get('validUntil', 'N/A')})")
            return True
        except requests.exceptions.RequestException as e:
            log_error(f"Failed to log in to Xelion", e)
            if hasattr(e, 'response') and e.response is not None:
                log_with_timestamp(f"HTTP Status Code: {e.response.status_code}", "ERROR")
                log_with_timestamp(f"Response Body: {e.response.text}", "ERROR")
            session_token = None
            return False

def _fetch_communications_page(limit: int, until_date: datetime, before_oid: Optional[str] = None) -> Tuple[List[Dict], Optional[str]]:
    """Fetches a single page of communications with detailed metadata."""
    params = {'limit': limit}
    params['until'] = until_date.strftime('%Y-%m-%d %H:%M:%S') 
    if before_oid:
        params['before'] = before_oid

    communications_url = f"{XELION_BASE_URL.rstrip('/')}/communications"
    try:
        log_with_timestamp(f"Fetching communications from: {communications_url} with params: {params}")
        response = xelion_session.get(communications_url, params=params, timeout=30) 
        response.raise_for_status()
        
        data = response.json()
        communications = data.get('data', [])
        
        # Log detailed information about what we received
        log_with_timestamp(f"Successfully fetched {len(communications)} communications")
        
        # Track statistics
        processing_stats['total_fetched'] += len(communications)
        processing_stats['last_poll_time'] = datetime.now().isoformat()
        
        # Analyze statuses in the communications
        status_breakdown = {}
        for comm in communications:
            status = comm.get('object', {}).get('status', 'unknown')
            status_breakdown[status] = status_breakdown.get(status, 0) + 1
            processing_stats['statuses_seen'][status] = processing_stats['statuses_seen'].get(status, 0) + 1
        
        log_with_timestamp(f"Status breakdown in this batch: {status_breakdown}")
        
        next_before_oid = None
        if 'meta' in data and 'paging' in data['meta']:
            next_before_oid = data['meta']['paging'].get('previousId')
        
        return communications, next_before_oid
            
    except requests.exceptions.RequestException as e:
        log_error("Failed to fetch communications", e)
        if hasattr(e, 'response') and e.response is not None:
            log_with_timestamp(f"HTTP Status Code: {e.response.status_code}", "ERROR")
            log_with_timestamp(f"Response Body: {e.response.text}", "ERROR")
        
        # If token expires, try to re-login
        if "401 Unauthorized" in str(e) and xelion_login():
            log_with_timestamp("Attempting re-login and re-fetch...")
            try:
                response = xelion_session.get(communications_url, params=params, timeout=30) 
                response.raise_for_status()
                data = response.json()
                communications = data.get('data', [])
                next_before_oid = None
                if 'meta' in data and 'paging' in data['meta']:
                    next_before_oid = data['meta']['paging'].get('previousId')
                log_with_timestamp(f"Successfully re-fetched {len(communications)} communications after re-login")
                return communications, next_before_oid
            except requests.exceptions.RequestException as re:
                log_error("Re-fetch after re-login failed", re)
                return [], None
        return [], None

def _extract_agent_info(comm_obj: Dict) -> Dict:
    """Extract agent info from Xelion communication object with detailed logging."""
    agent_info = {
        'agent_name': 'Unknown',
        'call_direction': 'Unknown',
        'phone_number': 'Unknown',
        'duration_seconds': 0,
        'status': 'Unknown',
        'user_id': 'Unknown'
    }
    
    try:
        # Log the raw communication object structure for debugging
        oid = comm_obj.get('oid', 'Unknown')
        log_with_timestamp(f"Processing communication OID {oid} with keys: {list(comm_obj.keys())}")
        
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
        
        transferred_from = comm_obj.get('transferredFromName', '')
        transferred_to = comm_obj.get('transferredToName', '')
        if transferred_from:
            agent_info['agent_name'] += f" (from: {transferred_from})"
        if transferred_to:
            agent_info['agent_name'] += f" (to: {transferred_to})"
        
        log_with_timestamp(f"Extracted agent info for OID {oid}: {agent_info}")
            
    except Exception as e:
        log_error(f"Error extracting agent info for OID {comm_obj.get('oid', 'Unknown')}", e)
    
    return agent_info

def download_audio(communication_oid: str) -> Optional[str]:
    """Download audio file to a temporary location with detailed logging."""
    audio_url = f"{XELION_BASE_URL.rstrip('/')}/communications/{communication_oid}/audio"
    file_name = f"{communication_oid}.mp3"
    file_path = os.path.join(AUDIO_TEMP_DIR, file_name)

    if os.path.exists(file_path):
        log_with_timestamp(f"Audio for OID {communication_oid} already exists. Skipping download")
        return file_path
    
    try:
        log_with_timestamp(f"Attempting to download audio for OID {communication_oid} from {audio_url}")
        response = xelion_session.get(audio_url, timeout=60) 
        
        if response.status_code == 200:
            with open(file_path, 'wb') as f:
                f.write(response.content)
            file_size = len(response.content)
            log_with_timestamp(f"Downloaded audio for OID {communication_oid} ({file_size} bytes)")
            return file_path
        elif response.status_code == 404:
            log_with_timestamp(f"No audio found for OID {communication_oid} (404 Not Found)")
            return None
        else:
            log_error(f"Failed to download audio for {communication_oid}: HTTP {response.status_code} - {response.text}")
            return None
            
    except requests.exceptions.RequestException as e:
        log_error(f"Audio download failed for {communication_oid}", e)
        return None

# --- Deepgram Transcription ---
def transcribe_audio_deepgram(audio_file_path: str, metadata_row: Dict) -> Optional[Dict]:
    """Transcribe audio file using Deepgram with detailed logging."""
    if not DEEPGRAM_API_KEY:
        log_error("Deepgram API key not configured")
        return None

    oid = metadata_row['oid']
    
    if not os.path.exists(audio_file_path):
        log_error(f"Audio file not found for transcription: {audio_file_path}")
        return None
    
    try:
        if DEEPGRAM_SDK_AVAILABLE:
            try:
                deepgram_client = DeepgramClient(DEEPGRAM_API_KEY)
                options = PrerecordedOptions(
                    model="nova-2", smart_format=True, punctuate=True,
                    diarize=True, utterances=True, language="en-GB"
                )
                log_with_timestamp(f"Transcribing OID {oid} using Deepgram SDK...")
                with open(audio_file_path, 'rb') as audio_file:
                    payload = FileSource(audio_file.read())
                response = deepgram_client.listen.prerecorded.v("1").transcribe_file(payload, options)
                
                transcript_data = response['results']['channels'][0]['alternatives'][0]
                transcript_text = transcript_data['transcript']
                duration_seconds = response['metadata']['duration']
                confidence = transcript_data.get('confidence', 0)
                language = response['metadata'].get('detected_language', 'en')

                log_with_timestamp(f"Deepgram SDK transcribed OID {oid} successfully")
                
            except Exception as e:
                log_with_timestamp(f"Deepgram SDK failed for OID {oid}, trying direct API: {e}", "WARN")
                # Fallback to direct API
                url = "https://api.deepgram.com/v1/listen"
                headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "audio/mpeg"}
                params = {"model": "nova-2", "smart_format": "true", "punctuate": "true",
                          "diarize": "true", "utterances": "true", "language": "en-GB"}
                
                log_with_timestamp(f"Transcribing OID {oid} using Deepgram direct API (fallback)...")
                with open(audio_file_path, 'rb') as audio_file:
                    response = requests.post(url, headers=headers, params=params, data=audio_file, timeout=120)
                
                response.raise_for_status()
                result = response.json()
                
                if 'results' not in result or not result['results']['channels']:
                    log_error(f"No transcription results from direct API for OID {oid}")
                    return None
                
                transcript_data = result['results']['channels'][0]['alternatives'][0]
                transcript_text = transcript_data['transcript']
                duration_seconds = result['metadata']['duration']
                confidence = transcript_data.get('confidence', 0)
                language = result['metadata'].get('detected_language', 'en')
                log_with_timestamp(f"Deepgram Direct API transcribed OID {oid} successfully")

        else: # Only direct API available
            url = "https://api.deepgram.com/v1/listen"
            headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "audio/mpeg"}
            params = {"model": "nova-2", "smart_format": "true", "punctuate": "true",
                      "diarize": "true", "utterances": "true", "language": "en-GB"}
            
            log_with_timestamp(f"Transcribing OID {oid} using Deepgram direct API...")
            with open(audio_file_path, 'rb') as audio_file:
                response = requests.post(url, headers=headers, params=params, data=audio_file, timeout=120)
            
            response.raise_for_status()
            result = response.json()
            
            if 'results' not in result or not result['results']['channels']:
                log_error(f"No transcription results from direct API for OID {oid}")
                return None
            
            transcript_data = result['results']['channels'][0]['alternatives'][0]
            transcript_text = transcript_data['transcript']
            duration_seconds = result['metadata']['duration']
            confidence = transcript_data.get('confidence', 0)
            language = result['metadata'].get('detected_language', 'en')
            log_with_timestamp(f"Deepgram Direct API transcribed OID {oid} successfully")

        if not transcript_text.strip():
            log_with_timestamp(f"Empty transcription for OID {oid}", "WARN")
            return None
        
        duration_minutes = duration_seconds / 60
        cost_usd = duration_minutes * DEEPGRAM_PRICE_PER_MINUTE
        cost_gbp = cost_usd * USD_TO_GBP_RATE
        word_count = len(transcript_text.split())

        log_with_timestamp(f"Transcription complete for OID {oid}: {word_count} words, {duration_minutes:.2f} minutes, £{cost_gbp:.4f}")

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
def analyze_transcription_with_openai(transcript: str) -> Optional[Dict]:
    """Analyze transcription using OpenAI for rankings with detailed logging."""
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        log_error("OpenAI not available or API key not configured")
        return None
    
    client = OpenAI(api_key=OPENAI_API_KEY)
    
    prompt = f"""
    Analyze the following customer service call transcript and provide a rating out of 10 for the following categories:
    1.  **Customer Engagement**: How well did the agent keep the customer involved and address their needs?
        * Sub-category 1: Active Listening (0-10)
        * Sub-category 2: Probing Questions (0-10)
        * Sub-category 3: Empathy & Understanding (0-10)
        * Sub-category 4: Clarity & Conciseness (0-10)
    2.  **Politeness**: How polite and courteous was the agent throughout the call?
        * Sub-category 1: Greeting & Closing (0-10)
        * Sub-category 2: Tone & Demeanor (0-10)
        * Sub-category 3: Respectful Language (0-10)
        * Sub-category 4: Handling Interruptions (0-10)
    3.  **Professional Knowledge**: How well did the agent demonstrate knowledge of the product/service and company policies?
        * Sub-category 1: Product/Service Information (0-10)
        * Sub-category 2: Policy Adherence (0-10)
        * Sub-category 3: Problem Diagnosis (0-10)
        * Sub-category 4: Solution Offering (0-10)
    4.  **Customer Resolution**: How effectively and efficiently was the customer's issue resolved?
        * Sub-category 1: Issue Identification (0-10)
        * Sub-category 2: Solution Effectiveness (0-10)
        * Sub-category 3: Time to Resolution (0-10)
        * Sub-category 4: Follow-up & Next Steps (0-10)

    Provide the output as a JSON object with the following structure. If a category or sub-category is not applicable or cannot be determined, return 0 for its score.

    {{
        "customer_engagement": {{
            "score": [0-10],
            "active_listening": [0-10],
            "probing_questions": [0-10],
            "empathy_understanding": [0-10],
            "clarity_conciseness": [0-10]
        }},
        "politeness": {{
            "score": [0-10],
            "greeting_closing": [0-10],
            "tone_demeanor": [0-10],
            "respectful_language": [0-10],
            "handling_interruptions": [0-10]
        }},
        "professional_knowledge": {{
            "score": [0-10],
            "product_service_info": [0-10],
            "policy_adherence": [0-10],
            "problem_diagnosis": [0-10],
            "solution_offering": [0-10]
        }},
        "customer_resolution": {{
            "score": [0-10],
            "issue_identification": [0-10],
            "solution_effectiveness": [0-10],
            "time_to_resolution": [0-10],
            "follow_up_next_steps": [0-10]
        }},
        "overall_score": [0-10]
    }}

    Transcript:
    {transcript}
    """
    
    try:
        log_with_timestamp("Analyzing transcription with OpenAI...")
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a customer service call analyzer. Rate the agent's performance based on the transcript."},
                {"role": "user", "content": prompt}
            ],
            response_format={ "type": "json_object" }
        )
        
        analysis_json = json.loads(response.choices[0].message.content)
        log_with_timestamp("OpenAI analysis complete")
        return analysis_json
        
    except Exception as e:
        log_error("OpenAI analysis failed", e)
        return None

def categorize_call(transcript: str) -> str:
    """Categorize calls based on keywords in the transcript."""
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
    """
    Downloads audio, transcribes it, analyzes with OpenAI, stores in DB, and deletes audio.
    Returns OID if successful, None otherwise. Enhanced with detailed logging.
    """
    comm_obj = communication_data.get('object', {})
    oid = comm_obj.get('oid')
    
    if not oid:
        log_with_timestamp("Skipping communication with no OID", "WARN")
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

    log_with_timestamp(f"Processing OID: {oid}")

    try:
        # Store raw communication data for debugging
        raw_data = json.dumps(communication_data)
        
        # 1. Download Audio
        audio_file_path = download_audio(oid)
        if not audio_file_path:
            log_with_timestamp(f"Skipping transcription for OID {oid} due to audio download failure or no audio available")
            # Store metadata even if no audio
            xelion_metadata = _extract_agent_info(comm_obj)
            call_datetime = comm_obj.get('date', 'Unknown')
            call_category = "Missed/No Audio" if xelion_metadata['status'].lower() in ['missed', 'cancelled'] else "No Audio"
            
            with db_lock:
                conn = get_db_connection()
                cursor = conn.cursor()
                try:
                    cursor.execute('''
                        INSERT INTO calls (
                            oid, call_datetime, agent_name, phone_number, call_direction, 
                            duration_seconds, status, user_id, category, processed_at, raw_communication_data
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        oid, call_datetime, xelion_metadata['agent_name'], xelion_metadata['phone_number'],
                        xelion_metadata['call_direction'], xelion_metadata['duration_seconds'], xelion_metadata['status'],
                        xelion_metadata['user_id'], call_category, datetime.now().isoformat(), raw_data
                    ))
                    conn.commit()
                    log_with_timestamp(f"Stored OID {oid} (Missed/No Audio) in DB")
                    processing_stats['total_processed'] += 1
                except sqlite3.Error as e:
                    log_error(f"Database error storing OID {oid} (Missed/No Audio)", e)
                    processing_stats['total_errors'] += 1
                finally:
                    conn.close()
            return None

        # 2. Transcribe Audio
        xelion_metadata = _extract_agent_info(comm_obj)
        call_datetime = comm_obj.get('date', 'Unknown')

        transcription_result = transcribe_audio_deepgram(audio_file_path, {'oid': oid})
        
        # 3. Delete Audio File
        try:
            os.remove(audio_file_path)
            log_with_timestamp(f"Deleted audio file: {audio_file_path}")
        except OSError as e:
            log_error(f"Error deleting audio file {audio_file_path}", e)

        if not transcription_result:
            log_with_timestamp(f"Skipping analysis and DB storage for OID {oid} due to transcription failure")
            processing_stats['total_errors'] += 1
            return None

        # 4. Analyze Transcription with OpenAI
        openai_analysis = analyze_transcription_with_openai(transcription_result['transcription_text'])
        if not openai_analysis:
            log_with_timestamp(f"OpenAI analysis failed for OID {oid}. Storing partial data", "WARN")
            openai_analysis = {}

        # 5. Categorize Call
        call_category = categorize_call(transcription_result['transcription_text'])
        
        # 6. Store in Database
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
                        processing_error, raw_communication_data
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
                    None,  # processing_error - None for successful processing
                    raw_data
                ))
                conn.commit()
                log_with_timestamp(f"Successfully stored OID {oid} and analysis in DB")
                processing_stats['total_processed'] += 1
                return oid
            except sqlite3.Error as e:
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
    """
    Enhanced version with detailed logging and status tracking.
    """
    global background_process_running
    background_process_running = True
    
    log_with_timestamp("Starting background fetch and transcribe process...")
    
    if not xelion_login():
        log_error("Login failed. Cannot proceed with fetching")
        background_process_running = False
        return

    until_date = datetime.now()
    processed_oids = set()

    with ThreadPoolExecutor(max_workers=3) as executor:  # Reduced to avoid overwhelming APIs
        while background_process_running:
            try:
                log_with_timestamp(f"Polling for new calls until {until_date.strftime('%Y-%m-%d %H:%M:%S')}")
                comms, _ = _fetch_communications_page(limit=50, until_date=until_date)
                
                if not comms:
                    log_with_timestamp("No new communications found in this poll")
                else:
                    log_with_timestamp(f"Found {len(comms)} communications to analyze")
                
                futures = []
                calls_to_process = []
                
                for comm in comms:
                    comm_obj = comm.get('object', {})
                    oid = comm_obj.get('oid')
                    status = comm_obj.get('status', '').lower()
                    
                    log_with_timestamp(f"Analyzing communication OID {oid} with status '{status}'")
                    
                    if oid and oid not in processed_oids:
                        # Process ALL calls regardless of status to see what happens
                        log_with_timestamp(f"Queuing OID {oid} (status: {status}) for processing")
                        calls_to_process.append(comm)
                        futures.append(executor.submit(process_single_call, comm))
                        processed_oids.add(oid)
                    elif oid in processed_oids:
                        log_with_timestamp(f"Skipping OID {oid} - already processed in this session")
                    else:
                        log_with_timestamp(f"Skipping communication with no OID")

                log_with_timestamp(f"Submitted {len(futures)} calls for processing")

                # Process results
                for future in as_completed(futures):
                    try:
                        result_oid = future.result()
                        if result_oid:
                            log_with_timestamp(f"Successfully processed call OID: {result_oid}")
                        else:
                            log_with_timestamp("Failed to process a call (see detailed logs above)")
                    except Exception as e:
                        log_error("Error processing future result", e)
                
                # Cleanup processed_oids set if it gets too large
                if len(processed_oids) > 1000:
                    log_with_timestamp("Cleaning up processed_oids set...")
                    processed_oids.clear()

                # Log current statistics
                log_with_timestamp(f"Session stats - Fetched: {processing_stats['total_fetched']}, "
                                 f"Processed: {processing_stats['total_processed']}, "
                                 f"Skipped: {processing_stats['total_skipped']}, "
                                 f"Errors: {processing_stats['total_errors']}")
                log_with_timestamp(f"Statuses seen: {processing_stats['statuses_seen']}")

                time.sleep(30)  # Poll every 30 seconds for more frequent updates
                
            except Exception as e:
                log_error("Error in main polling loop", e)
                time.sleep(60)  # Wait longer on error

    background_process_running = False
    log_with_timestamp("Background process stopped")

# --- Flask Routes ---
@app.route('/')
def index():
    """Automatically start background process when accessing root"""
    global background_process_running, background_thread
    
    if not background_process_running:
        log_with_timestamp("Starting background process automatically from root route")
        background_thread = threading.Thread(target=fetch_and_transcribe_recent_calls)
        background_thread.daemon = True
        background_thread.start()
        log_with_timestamp("Background process started")
    else:
        log_with_timestamp("Background process already running")
    
    return render_template('index.html')

@app.route('/status')
def get_status():
    """Get detailed status information"""
    return jsonify({
        "background_running": background_process_running,
        "processing_stats": processing_stats,
        "deepgram_available": DEEPGRAM_SDK_AVAILABLE,
        "openai_available": OPENAI_AVAILABLE,
        "last_poll": processing_stats.get('last_poll_time', 'Never'),
        "last_error": processing_stats.get('last_error', 'None')
    })

@app.route('/fetch_and_transcribe')
def trigger_fetch_and_transcribe():
    """Manual trigger for background process"""
    global background_process_running, background_thread
    
    if background_process_running:
        return jsonify({"status": "Background process already running", "running": True})
    
    log_with_timestamp("Received manual request to start background process")
    background_thread = threading.Thread(target=fetch_and_transcribe_recent_calls)
    background_thread.daemon = True
    background_thread.start()
    return jsonify({"status": "Process initiated manually. Check console for updates.", "running": True})

@app.route('/stop_process')
def stop_process():
    """Stop the background process"""
    global background_process_running
    if background_process_running:
        background_process_running = False
        log_with_timestamp("Background process stop requested")
        return jsonify({"status": "Background process stopping...", "running": False})
    else:
        return jsonify({"status": "Background process not running", "running": False})

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

        # Average ratings for main categories
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

        # Average ratings for sub-categories
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

@app.route('/get_recent_calls')
def get_recent_calls():
    """Get recent calls for debugging"""
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT oid, call_datetime, status, category, agent_name, phone_number, 
                   transcription_text, processed_at, processing_error
            FROM calls 
            ORDER BY processed_at DESC 
            LIMIT 20
        """)
        calls = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return jsonify({"recent_calls": calls})

@app.route('/init_db_manual')
def init_db_manual():
    init_db()
    return "Database initialization attempted (check logs for details)."

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
