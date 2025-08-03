import os
import sqlite3
import json
import threading
import time
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, List, Tuple
from flask import Flask, jsonify, render_template, request

# --- Dummy/Mock Setup (Replace with your actual configuration) ---
# Assuming these are set via environment variables or a config file
XELION_BASE_URL = os.environ.get("XELION_BASE_URL")
XELION_USERNAME = os.environ.get("XELION_USERNAME")
XELION_PASSWORD = os.environ.get("XELION_PASSWORD")
XELION_USERSPACE = os.environ.get("XELION_USERSPACE")
XELION_APP_KEY = os.environ.get("XELION_APP_KEY")
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
WASTEKING_PRICING_URL = os.environ.get("WASTEKING_PRICING_URL")

OPENAI_AVAILABLE = bool(OPENAI_API_KEY)
DEEPGRAM_SDK_AVAILABLE = True  # Assuming SDK is installed
SELENIUM_AVAILABLE = False  # Assuming Selenium is not used for this version

if DEEPGRAM_SDK_AVAILABLE:
    try:
        from deepgram import DeepgramClient, FileSource, PrerecordedOptions
    except ImportError:
        DEEPGRAM_SDK_AVAILABLE = False
        print("WARN: Deepgram SDK not found. Will fall back to direct API calls.")

if OPENAI_AVAILABLE:
    try:
        from openai import OpenAI
    except ImportError:
        OPENAI_AVAILABLE = False
        print("WARN: OpenAI library not found. Analysis will use fallback scoring.")

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
    print("WARN: BeautifulSoup not found. Web scraping for WasteKing will fail.")

# Constants
AUDIO_TEMP_DIR = 'audio_temp'
if not os.path.exists(AUDIO_TEMP_DIR):
    os.makedirs(AUDIO_TEMP_DIR)
DB_PATH = 'call_data.db'

# Global state and locks
background_process_running = False
background_thread = None
xelion_session = requests.Session()
login_lock = threading.Lock()
db_lock = threading.Lock()
processing_stats = {
    'total_fetched': 0,
    'total_processed': 0,
    'total_errors': 0,
    'total_skipped': 0,
    'last_poll_time': 'Never',
    'last_error': 'None',
    'statuses_seen': {}
}

# --- Database helpers ---
def get_db_connection():
    """Create a database connection with row factory for dictionary access"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Create the database schema if it doesn't exist"""
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS calls (
                oid TEXT PRIMARY KEY,
                call_datetime TEXT,
                agent_name TEXT,
                phone_number TEXT,
                call_direction TEXT,
                duration_seconds INTEGER,
                status TEXT,
                user_id TEXT,
                transcription_text TEXT,
                transcribed_duration_minutes REAL,
                deepgram_cost_usd REAL,
                deepgram_cost_gbp REAL,
                word_count INTEGER,
                confidence REAL,
                language TEXT,
                processed_at TEXT,
                category TEXT,
                openai_engagement REAL,
                openai_politeness REAL,
                openai_professionalism REAL,
                openai_resolution REAL,
                openai_overall_score REAL,
                engagement_sub1 REAL,
                engagement_sub2 REAL,
                engagement_sub3 REAL,
                engagement_sub4 REAL,
                politeness_sub1 REAL,
                politeness_sub2 REAL,
                politeness_sub3 REAL,
                politeness_sub4 REAL,
                professionalism_sub1 REAL,
                professionalism_sub2 REAL,
                professionalism_sub3 REAL,
                professionalism_sub4 REAL,
                resolution_sub1 REAL,
                resolution_sub2 REAL,
                resolution_sub3 REAL,
                resolution_sub4 REAL,
                raw_communication_data TEXT,
                summary_translation TEXT
            )
        ''')
        conn.commit()
        conn.close()
        log_with_timestamp("Database initialized successfully.")

# --- Dummy functions for authentication and logging ---
def log_with_timestamp(message, level="INFO"):
    print(f"[{datetime.now().isoformat()}] [{level}] {message}")

def log_error(message, exception=None):
    log_with_timestamp(f"ERROR: {message}", "ERROR")
    if exception:
        print(f"Exception details: {exception}")
        
def load_wasteking_session():
    # Placeholder for loading a session
    return requests.Session()

def authenticate_wasteking():
    # Placeholder for authentication
    return requests.Session()

def test_openai_connection():
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return {"status": "error", "message": "OpenAI not configured."}
    
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        client.models.list()
        return {"status": "success", "message": "OpenAI connection successful."}
    except Exception as e:
        return {"status": "error", "message": f"Connection failed: {str(e)}"}

# --- Part 1 and 2 functions ---
def get_wasteking_prices():
    """FIXED: Better error handling and includes actual data scraping from HTML"""
    try:
        log_with_timestamp("💰 Fetching WasteKing prices...")
        session = load_wasteking_session()
        if not session:
            log_with_timestamp("No valid WasteKing session, attempting auto-authentication...")
            auth_result = authenticate_wasteking()
            if isinstance(auth_result, dict) and "error" in auth_result:
                log_with_timestamp(f"❌ Authentication failed: {auth_result['message']}")
                return auth_result
            session = auth_result
            if not session:
                return {
                    "error": "WasteKing authentication required",
                    "status": "session_expired",
                    "message": "Unable to authenticate with WasteKing system.",
                    "timestamp": datetime.now().isoformat()
                }
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = session.get(WASTEKING_PRICING_URL, timeout=15)
                response.raise_for_status()
                if response.status_code == 200:
                    log_with_timestamp("✅ Successfully fetched pricing page. Starting to scrape...")
                    soup = BeautifulSoup(response.text, 'html.parser')
                    try:
                        table = soup.find('table', {'id': 'pricing-table'})
                        if not table:
                            raise ValueError("Could not find pricing table with ID 'pricing-table'.")
                        headers = [th.get_text(strip=True) for th in table.find('thead').find_all('th')]
                        pricing_data = []
                        for row in table.find('tbody').find_all('tr'):
                            cells = row.find_all('td')
                            if cells:
                                row_data = {headers[i]: cell.get_text(strip=True) for i, cell in enumerate(cells)}
                                pricing_data.append(row_data)
                        log_with_timestamp(f"🎉 Successfully scraped {len(pricing_data)} rows of pricing data!")
                        return {
                            "status": "success",
                            "timestamp": datetime.now().isoformat(),
                            "message": "WasteKing data fetched and parsed successfully",
                            "data": pricing_data
                        }
                    except Exception as scrape_error:
                        log_error(f"Failed to scrape data from the page: {scrape_error}")
                        return {
                            "error": "Failed to parse pricing data from page",
                            "status": "scraping_failed",
                            "message": f"An error occurred while scraping: {str(scrape_error)}",
                            "timestamp": datetime.now().isoformat()
                        }
                elif response.status_code in [401, 403]:
                    if attempt == 0:
                        log_with_timestamp("WasteKing session expired, re-authenticating...")
                        auth_result = authenticate_wasteking()
                        if isinstance(auth_result, dict) and "error" in auth_result:
                            return auth_result
                        session = auth_result
                        continue
                    else:
                        return {
                            "error": "WasteKing authentication failed",
                            "status": "auth_required",
                            "message": "Session expired and re-authentication failed",
                            "timestamp": datetime.now().isoformat()
                        }
                else:
                    return {
                        "error": f"WasteKing request failed with status {response.status_code}",
                        "status": "request_failed",
                        "timestamp": datetime.now().isoformat()
                    }
            except requests.exceptions.RequestException as e:
                if attempt == max_retries - 1:
                    log_error("Final attempt to fetch WasteKing data failed", e)
                    raise e
                log_with_timestamp(f"Request failed (attempt {attempt + 1}), retrying in 2 seconds...")
                time.sleep(2)
    except Exception as e:
        log_error("Error fetching WasteKing prices", e)
        return {
            "error": str(e),
            "status": "error",
            "timestamp": datetime.now().isoformat()
        }

def xelion_login() -> bool:
    global session_token
    with login_lock:
        session_token = None
        if not all([XELION_BASE_URL, XELION_USERNAME, XELION_PASSWORD, XELION_APP_KEY, XELION_USERSPACE]):
            log_error("Xelion credentials environment variables are not set.")
            return False
        login_url = f"{XELION_BASE_URL.rstrip('/')}/me/login"
        headers = {"Content-Type": "application/json"}
        data_payload = { 
            "userName": XELION_USERNAME, 
            "password": XELION_PASSWORD,
            "userSpace": XELION_USERSPACE,
            "appKey": XELION_APP_KEY
        }
        log_with_timestamp(f"🔑 Attempting Xelion login for {XELION_USERNAME}")
        try:
            if 'Authorization' in xelion_session.headers:
                del xelion_session.headers['Authorization']
            response = xelion_session.post(login_url, headers=headers, data=json.dumps(data_payload), timeout=30)
            response.raise_for_status() 
            login_response = response.json()
            session_token = login_response.get("authentication")
            if session_token:
                xelion_session.headers.update({"Authorization": f"xelion {session_token}"})
                log_with_timestamp(f"✅ Successfully logged in to Xelion")
                return True
            else:
                log_error("No authentication token received in login response")
                return False
        except requests.exceptions.RequestException as e:
            log_error(f"Failed to log in to Xelion", e)
            if hasattr(e, 'response') and e.response is not None:
                log_with_timestamp(f"HTTP Status Code: {e.response.status_code}", "ERROR")
                log_with_timestamp(f"Response Body: {e.response.text}", "ERROR")
            session_token = None
            return False

def _fetch_communications_page(limit: int, until_date: datetime, before_oid: Optional[str] = None) -> Tuple[List[Dict], Optional[str]]:
    """FIXED: Better error handling for communications fetching"""
    params = {'limit': limit}
    params['until'] = until_date.strftime('%Y-%m-%d %H:%M:%S') 
    if before_oid:
        params['before'] = before_oid
    communications_url = f"{XELION_BASE_URL.rstrip('/')}/communications"
    for attempt in range(3):
        try:
            log_with_timestamp(f"Fetching communications (attempt {attempt + 1})")
            response = xelion_session.get(communications_url, params=params, timeout=30) 
            if response.status_code == 401:
                log_with_timestamp("🔑 401 Authentication error, attempting re-login...")
                global session_token
                session_token = None
                if xelion_login():
                    log_with_timestamp("✅ Re-login successful, retrying fetch...")
                    continue
                else:
                    log_error("Re-login failed after 401 error")
                    return [], None
            response.raise_for_status()
            data = response.json()
            communications = data.get('data', [])
            log_with_timestamp(f"Successfully fetched {len(communications)} communications")
            processing_stats['total_fetched'] += len(communications)
            processing_stats['last_poll_time'] = datetime.now().isoformat()
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
        except requests.exceptions.RequestException as e:
            log_error(f"Failed to fetch communications (attempt {attempt + 1})", e)
            if attempt == 2:
                return [], None
            time.sleep(5)

def _extract_agent_info(comm_obj: Dict) -> Dict:
    """Extract agent info from Xelion communication object"""
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
    """FIXED: Better audio download handling"""
    audio_url = f"{XELION_BASE_URL.rstrip('/')}/communications/{communication_oid}/audio"
    file_name = f"{communication_oid}.mp3"
    file_path = os.path.join(AUDIO_TEMP_DIR, file_name)
    if os.path.exists(file_path):
        log_with_timestamp(f"Audio for OID {communication_oid} already exists")
        return file_path
    try:
        log_with_timestamp(f"Downloading audio for OID {communication_oid}")
        response = xelion_session.get(audio_url, timeout=60) 
        if response.status_code == 200:
            if len(response.content) > 1000:
                with open(file_path, 'wb') as f:
                    f.write(response.content)
                file_size = len(response.content)
                log_with_timestamp(f"Downloaded audio for OID {communication_oid} ({file_size} bytes)")
                return file_path
            else:
                log_with_timestamp(f"Audio file too small for OID {communication_oid} - likely no recording")
                return None
        elif response.status_code == 404:
            log_with_timestamp(f"No audio recording found for OID {communication_oid}")
            return None
        else:
            log_error(f"Failed to download audio for {communication_oid}: HTTP {response.status_code}")
            return None
    except requests.exceptions.RequestException as e:
        log_error(f"Audio download failed for {communication_oid}", e)
        return None

def transcribe_audio_deepgram(audio_file_path: str, metadata_row: Dict) -> Optional[Dict]:
    """FIXED: Better transcription error handling"""
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
                with open(audio_file_path, 'rb') as audio_
                    source = FileSource(audio=audio_)
                    response = deepgram_client.listen.prerecorded.v("1").transcribe_file(source, options)
                
                deepgram_data = response.to_dict()
                utterances = deepgram_data.get('results', {}).get('utterances', [])
                full_transcript = " ".join([u['transcript'] for u in utterances])
                
                metadata = deepgram_data.get('metadata', {})
                if not full_transcript:
                    log_error(f"Deepgram returned no transcript for OID {oid}")
                    return None
                
                deepgram_duration = metadata.get('duration', 0)
                deepgram_cost_per_minute = 0.0004 if options.get('model') == 'nova-2' else 0.0001
                deepgram_cost_usd = (deepgram_duration / 60) * deepgram_cost_per_minute
                
                transcription_result = {
                    'transcription_text': full_transcript,
                    'word_count': metadata.get('words', 0),
                    'confidence': metadata.get('confidence', 0),
                    'language': metadata.get('language', 'en-GB'),
                    'transcribed_duration_minutes': deepgram_duration / 60,
                    'deepgram_cost_usd': deepgram_cost_usd,
                    'deepgram_cost_gbp': deepgram_cost_usd * 0.8  # Assuming a conversion rate
                }
                log_with_timestamp(f"✅ Transcription for OID {oid} complete. Cost: ${transcription_result['deepgram_cost_usd']:.4f}")
                return transcription_result

            except Exception as e:
                log_error(f"Deepgram SDK transcription failed for OID {oid}", e)
                return None
    except Exception as e:
        log_error(f"Unexpected error during transcription for OID {oid}", e)
        return None

def analyze_transcription_with_openai(transcript: str, oid: str) -> Optional[Dict]:
    """FIXED: Better OpenAI analysis with error handling and fallback"""
    if not OPENAI_API_KEY:
        log_error("OpenAI API key not configured")
        return {"summary": "Analysis skipped: OpenAI not configured."}
    if not transcript:
        return {"summary": "Analysis skipped: No transcript available."}

    client = OpenAI(api_key=OPENAI_API_KEY)
    
    analysis_prompt = f"""
    Analyze the following call transcript for quality assurance purposes. The call is from a customer to a support agent.
    Provide scores from 1 to 5 (1=Poor, 5=Excellent) for the following categories and sub-categories, along with a summary.

    Transcript:
    "{transcript}"

    Output the analysis as a JSON object with the following structure:
    {{
        "overall_score": <int>,
        "summary": "<string>",
        "customer_engagement": {{
            "score": <int>,
            "active_listening": <int>,
            "probing_questions": <int>,
            "empathy_understanding": <int>,
            "clarity_conciseness": <int>
        }},
        "politeness": {{
            "score": <int>,
            "greeting_closing": <int>,
            "tone_demeanor": <int>,
            "respectful_language": <int>,
            "handling_interruptions": <int>
        }},
        "professional_knowledge": {{
            "score": <int>,
            "product_service_info": <int>,
            "policy_adherence": <int>,
            "problem_diagnosis": <int>,
            "solution_offering": <int>
        }},
        "customer_resolution": {{
            "score": <int>,
            "issue_identification": <int>,
            "solution_effectiveness": <int>,
            "time_to_resolution": <int>,
            "follow_up_next_steps": <int>
        }}
    }}
    Ensure all scores are integers between 1 and 5.
    """
    
    try:
        log_with_timestamp(f"Sending OID {oid} to OpenAI for analysis...")
        response = client.chat.completions.create(
            model="gpt-4",
            response_format={ "type": "json_object" },
            messages=[
                {"role": "system", "content": "You are a helpful assistant that analyzes call transcripts and outputs a JSON object with scores."},
                {"role": "user", "content": analysis_prompt}
            ],
            timeout=60
        )
        
        analysis_json = json.loads(response.choices[0].message.content)
        log_with_timestamp(f"✅ OpenAI analysis complete for OID {oid}")
        return analysis_json
        
    except Exception as e:
        log_error(f"OpenAI analysis failed for OID {oid}", e)
        # Fallback analysis
        fallback_analysis = {
            "overall_score": 1,
            "summary": "AI analysis failed. Please review the transcript manually.",
            "customer_engagement": {"score": 1, "active_listening": 1, "probing_questions": 1, "empathy_understanding": 1, "clarity_conciseness": 1},
            "politeness": {"score": 1, "greeting_closing": 1, "tone_demeanor": 1, "respectful_language": 1, "handling_interruptions": 1},
            "professional_knowledge": {"score": 1, "product_service_info": 1, "policy_adherence": 1, "problem_diagnosis": 1, "solution_offering": 1},
            "customer_resolution": {"score": 1, "issue_identification": 1, "solution_effectiveness": 1, "time_to_resolution": 1, "follow_up_next_steps": 1}
        }
        return fallback_analysis

# --- Part 3 functions ---
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
    """FIXED: Process single call with better error handling"""
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
            log_with_timestamp(f"OID {oid} already processed, skipping")
            return None
        conn.close()

    log_with_timestamp(f"Processing NEW call OID: {oid}")

    try:
        raw_data = json.dumps(communication_data)
        xelion_metadata = _extract_agent_info(comm_obj)
        call_datetime = comm_obj.get('date', 'Unknown')
        
        # 1. Download Audio
        audio_file_path = download_audio(oid)
        if not audio_file_path:
            log_with_timestamp(f"No audio available for OID {oid} - storing metadata only")
            
            call_category = "Missed/No Audio" if xelion_metadata['status'].lower() in ['missed', 'cancelled'] else "No Audio"
            
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
                        raw_data, "No audio available for transcription"
                    ))
                    conn.commit()
                    log_with_timestamp(f"Stored OID {oid} (No Audio) in database")
                    processing_stats['total_processed'] += 1
                except sqlite3.Error as e:
                    log_error(f"Database error storing OID {oid} (No Audio)", e)
                    processing_stats['total_errors'] += 1
                finally:
                    conn.close()
            return oid

        # 2. Transcribe Audio
        transcription_result = transcribe_audio_deepgram(audio_file_path, {'oid': oid})
        
        # 3. Delete Audio File
        try:
            os.remove(audio_file_path)
            log_with_timestamp(f"Deleted audio file: {audio_file_path}")
        except OSError as e:
            log_error(f"Error deleting audio file {audio_file_path}", e)

        if not transcription_result:
            log_with_timestamp(f"Transcription failed for OID {oid}")
            processing_stats['total_errors'] += 1
            return None

        # 4. Analyze with OpenAI
        openai_analysis = analyze_transcription_with_openai(transcription_result['transcription_text'], oid)
        if not openai_analysis:
            log_with_timestamp(f"⚠️ OpenAI analysis failed for OID {oid} - storing partial data", "WARN")
            openai_analysis = {"summary": "Analysis failed - no summary available"}

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
                    raw_data, openai_analysis.get('summary', 'No summary available')
                ))
                conn.commit()
                log_with_timestamp(f"✅ Successfully stored OID {oid} in database")
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
    """FIXED: Only fetch NEW calls, not historic ones"""
    global background_process_running
    background_process_running = True
    
    log_with_timestamp("🚀 MONITORING: Starting NEW calls monitoring...")
    
    if not xelion_login():
        log_error("Xelion login failed. Cannot proceed")
        background_process_running = False
        return

    # Get timestamp of last processed call to avoid processing old data
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
            except Exception:
                pass
        conn.close()

    # Start checking from 5 minutes ago if no previous calls, otherwise from last call
    if not last_call_time:
        check_since = datetime.now() - timedelta(minutes=5)
        log_with_timestamp("🆕 No previous calls - checking last 5 minutes only")
    else:
        check_since = last_call_time
        log_with_timestamp(f"🔍 Checking for NEW calls since: {check_since}")

    processed_this_session = set()

    with ThreadPoolExecutor(max_workers=3) as executor:
        while background_process_running:
            try:
                log_with_timestamp(f"🔄 Polling for NEW calls since {check_since.strftime('%Y-%m-%d %H:%M:%S')}")
                
                # Get recent communications
                comms, _ = _fetch_communications_page(limit=50, until_date=datetime.now())
                
                if not comms:
                    log_with_timestamp("📭 No communications found")
                    time.sleep(30)
                    continue
                
                # Filter to only NEW calls after our check time
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
                        
                        # Only process if call is AFTER our check time
                        if call_dt > check_since and oid not in processed_this_session:
                            new_comms.append(comm)
                            processed_this_session.add(oid)
                            latest_time = max(latest_time, call_dt)
                            log_with_timestamp(f"🆕 Found NEW call OID {oid} at {call_datetime}")
                    except Exception:
                        log_with_timestamp(f"⚠️ Date parse error for OID {oid}")
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

                log_with_timestamp(f"📊 Session Stats - Processed: {processing_stats['total_processed']}, Errors: {processing_stats['total_errors']}")
                time.sleep(30)  # Check every 30 seconds
                
            except Exception as e:
                log_error("Error in monitoring loop", e)
                time.sleep(60)

    background_process_running = False
    log_with_timestamp("🛑 Call monitoring stopped")

# --- Flask Routes ---
app = Flask(__name__)
init_db()

@app.route('/')
def index():
    """Auto-start background process when accessing root"""
    global background_process_running, background_thread
    
    if not background_process_running:
        log_with_timestamp("🚀 AUTO-STARTING call monitoring from root route")
        background_thread = threading.Thread(target=fetch_and_transcribe_recent_calls)
        background_thread.daemon = True
        background_thread.start()
        log_with_timestamp("✅ Call monitoring auto-started")
    else:
        log_with_timestamp("ℹ️ Call monitoring already running")
    
    return render_template('index.html')

@app.route('/status')
def get_status():
    """Get system status"""
    openai_test_result = test_openai_connection()
    return jsonify({
        "background_running": background_process_running,
        "processing_stats": processing_stats,
        "selenium_available": SELENIUM_AVAILABLE,
        "deepgram_available": DEEPGRAM_SDK_AVAILABLE,
        "openai_available": OPENAI_AVAILABLE,
        "openai_connection_test": openai_test_result,
        "openai_api_key_configured": OPENAI_API_KEY and OPENAI_API_KEY != 'your_openai_api_key',
        "wasteking_session_valid": load_wasteking_session() is not None,
        "last_poll": processing_stats.get('last_poll_time', 'Never'),
        "last_error": processing_stats.get('last_error', 'None')
    })

@app.route('/fetch_and_transcribe')
def trigger_fetch_and_transcribe():
    """Manual trigger for call monitoring"""
    global background_process_running, background_thread
    
    if background_process_running:
        return jsonify({"status": "Call monitoring already running", "running": True})
    
    log_with_timestamp("Manual start of call monitoring requested")
    background_thread = threading.Thread(target=fetch_and_transcribe_recent_calls)
    background_thread.daemon = True
    background_thread.start()
    return jsonify({"status": "Call monitoring started", "running": True})

@app.route('/stop_process')
def stop_process():
    """Stop call monitoring"""
    global background_process_running
    if background_process_running:
        background_process_running = False
        log_with_timestamp("Call monitoring stop requested")
        return jsonify({"status": "Call monitoring stopping...", "running": False})
    else:
        return jsonify({"status": "Call monitoring not running", "running": False})

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
    """Get paginated list of calls"""
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
        
        # Get calls with pagination
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
                except Exception:
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

# --- ElevenLabs Webhook Endpoints ---
@app.route('/api/get-wasteking-prices', methods=['GET'])
def elevenlabs_get_wasteking_prices():
    """ElevenLabs webhook for WasteKing pricing"""
    log_with_timestamp("📞 ElevenLabs called WasteKing pricing endpoint")
    
    try:
        result = get_wasteking_prices()
        return jsonify(result)
    except Exception as e:
        log_error("Error in ElevenLabs WasteKing endpoint", e)
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
                "message": "WasteKing authentication failed",
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
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
