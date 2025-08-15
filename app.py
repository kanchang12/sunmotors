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
import hashlib
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re
from bs4 import BeautifulSoup

# OpenAI import
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# --- Configuration (FROM WORKING SYSTEM) ---
XELION_BASE_URL = os.getenv('XELION_BASE_URL', 'https://lvsl01.xelion.com/api/v1/wasteking')
XELION_USERNAME = os.getenv('XELION_USERNAME', 'your_xelion_username')
XELION_PASSWORD = os.getenv('XELION_PASSWORD', 'your_xelion_password')
XELION_APP_KEY = os.getenv('XELION_APP_KEY', 'your_xelion_app_key')
XELION_USERSPACE = os.getenv('XELION_USERSPACE', 'transcriber-abi-housego')

# WasteKing API Configuration
WASTEKING_API_BASE = os.getenv('WASTEKING_API_BASE', 'https://api.wasteking.co.uk')
WASTEKING_ACCESS_TOKEN = os.getenv('WASTEKING_ACCESS_TOKEN', 'your_wasteking_access_token')

# Deepgram API
DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY', 'your_deepgram_api_key')

# OpenAI API
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', 'your_openai_api_key')

# Database configuration
DATABASE_FILE = 'calls.db'

# Directory for temporary audio files
AUDIO_TEMP_DIR = 'temp_audio'
os.makedirs(AUDIO_TEMP_DIR, exist_ok=True)

# Deepgram pricing and currency conversion
DEEPGRAM_PRICE_PER_MINUTE = 0.0043
USD_TO_GBP_RATE = 0.79

# Global session and locks for thread safety (FROM WORKING SYSTEM)
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

# Add after the import statements
def fix_uk_postcode(postcode: str) -> str:
    """Fix UK postcode formatting by adding space if missing"""
    if not postcode:
        return postcode
    
    # Remove any existing spaces and convert to uppercase
    clean_postcode = postcode.replace(' ', '').upper()
    
    # UK postcodes are 5-7 characters, last 3 are always digits + letter + letter
    if len(clean_postcode) >= 5:
        # Add space before last 3 characters
        formatted = clean_postcode[:-3] + ' ' + clean_postcode[-3:]
        log_with_timestamp(f"🔧 Fixed postcode: '{postcode}' -> '{formatted}'")
        return formatted
    
    return postcode

# Update the SALES_TEAM_AGENTS list (around line 85)
SALES_TEAM_AGENTS = [
    'Kanchan Ghosh',
    'Anxhela Kopaci', 'Caroline Jones', 'Chris Hood', 'Clara Roake',
    'Courtney Wildman', 'Farron Bishop', 'Jack Herring', 'Jackie Duke', 
    'Jody Conybeare-Jones', 'Keith Middleton', 'Kristy Stuart',
    'Lauren Czyzewicz', 'Marc Reeve', 'Micky Brearley', 'Ryan Willis',
    'Sara Smith', 'Tracey Bower'
]

def is_sales_team_or_kanchan(agent_name: str) -> bool:
    """Check if agent is Sales Team or Kanchan Ghosh"""
    if not agent_name:
        return False
    return agent_name in SALES_TEAM_AGENTS or 'kanchan' in agent_name.lower()

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

# --- WasteKing API Functions ---
def generate_booking_ref() -> str:
    """Generate a unique booking reference"""
    return str(uuid.uuid4())

def call_wasteking_api(endpoint: str, data: Dict, method: str = 'POST') -> Optional[Dict]:
    """Make API call to WasteKing API"""
    try:
        url = f"{WASTEKING_API_BASE.rstrip('/')}/api/booking/{endpoint}"
        headers = {
            'Content-Type': 'application/json',
            'X-Wasteking-Request': f'{{{WASTEKING_ACCESS_TOKEN}}}'
        }
        
        log_with_timestamp(f"🌐 WasteKing API call: {method} {url}")
        log_with_timestamp(f"📤 Request data: {json.dumps(data, indent=2)}")
        
        if method.upper() == 'POST':
            response = requests.post(url, headers=headers, json=data, timeout=30)
        else:
            response = requests.get(url, headers=headers, params=data, timeout=30)
        
        log_with_timestamp(f"📥 Response status: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            log_with_timestamp(f"✅ WasteKing API success: {json.dumps(result, indent=2)}")
            return result
        else:
            log_error(f"WasteKing API error: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        log_error(f"WasteKing API call failed for {endpoint}", e)
        return None

def create_wasteking_booking(booking_data: Dict) -> Optional[Dict]:
    """Create complete WasteKing booking following EXACT 4-step API flow from images"""
    try:
        booking_ref = generate_booking_ref()
        log_with_timestamp(f"🎯 Creating WasteKing booking: {booking_ref}")
        
        # FIX POSTCODE FORMAT - add space if missing
        postcode = fix_uk_postcode(booking_data.get('postcode', ''))
        booking_data['postcode'] = postcode
        
        # FIX SERVICE/TYPE - service stays 'mav', type becomes skip size
        if not booking_data.get('type') or booking_data.get('type') == booking_data.get('service'):
            booking_data['type'] = '6yd'  # Default skip size
            log_with_timestamp(f"🔧 Fixed type to 6yd (service: {booking_data.get('service', 'mav')})")
        
        # FIX DATE/TIME - add today's date and time if missing
        if not booking_data.get('date') or booking_data.get('date') in ['today', '', None]:
            booking_data['date'] = datetime.now().strftime('%Y-%m-%d')
            log_with_timestamp(f"📅 Set date to today: {booking_data['date']}")
            
        if not booking_data.get('time'):
            booking_data['time'] = 'am'
            log_with_timestamp(f"🕐 Set time to am")
        
        # ENSURE ALL REQUIRED FIELDS ARE PRESENT
        required_defaults = {
            'firstName': 'Customer',
            'lastName': 'Unknown', 
            'phone': '01234567890',
            'emailAddress': 'customer@example.com',
            'address1': 'Customer Address',
            'addressCity': 'Leeds',
            'addressPostcode': postcode,
            'placement': 'drive'
        }
        
        for field, default_value in required_defaults.items():
            if not booking_data.get(field):
                booking_data[field] = default_value
                log_with_timestamp(f"🔧 Set default {field}: {default_value}")
        
        # STEP 1: Initial booking with search criteria (Image 1)
        step1_data = {
            "bookingRef": booking_ref,
            "search": {
                "postCode": booking_data['postcode'],
                "service": booking_data.get('service', 'mav'),  # Keep as 'mav'
                "type": booking_data['type']                    # Now '6yd' etc
            }
        }
        
        log_with_timestamp(f"📋 STEP 1 - Search: {json.dumps(step1_data, indent=2)}")
        result1 = call_wasteking_api('update', step1_data)
        if not result1:
            log_error("Step 1 failed - search")
            return None
        
        # STEP 2: Add complete customer details (Image 2)
        step2_data = {
            "bookingRef": booking_ref,
            "customer": {
                "firstName": booking_data['firstName'],
                "lastName": booking_data['lastName'],
                "phone": booking_data['phone'],
                "emailAddress": booking_data['emailAddress'],
                "address1": booking_data['address1'],
                "address2": booking_data.get('address2', ''),
                "addressCity": booking_data['addressCity'],
                "addressCounty": booking_data.get('addressCounty', ''),
                "addressPostcode": booking_data['addressPostcode']
            }
        }
        
        log_with_timestamp(f"📋 STEP 2 - Customer: {json.dumps(step2_data, indent=2)}")
        result2 = call_wasteking_api('update', step2_data)
        if not result2:
            log_error("Step 2 failed - customer details")
            return None
        
        # STEP 3: Add service details with supplements and images (Image 3)
        step3_data = {
            "bookingRef": booking_ref,
            "service": {
                "date": booking_data['date'],
                "time": booking_data['time'],
                "placement": booking_data['placement'],
                "notes": booking_data.get('notes', '')
            }
        }
        
        # Add collection date if provided
        if booking_data.get('collection'):
            step3_data["service"]["collection"] = booking_data['collection']
        
        # Add supplements if provided
        supplements = []
        if booking_data.get('supplement_code') and booking_data.get('supplement_qty'):
            supplements.append({
                "code": booking_data['supplement_code'],
                "qty": int(booking_data['supplement_qty'])
            })
        
        if supplements:
            step3_data["service"]["supplements"] = supplements
        
        # Add images if provided
        images = []
        if booking_data.get('imageUrl'):
            images.append({
                "imageUrl": booking_data['imageUrl']
            })
        
        if images:
            step3_data["images"] = images
        
        log_with_timestamp(f"📋 STEP 3 - Service: {json.dumps(step3_data, indent=2)}")
        result3 = call_wasteking_api('update', step3_data)
        if not result3:
            log_error("Step 3 failed - service details")
            return None
        
        # STEP 4: Generate quote (Image 4)
        step4_data = {
            "bookingRef": booking_ref,
            "action": "quote",
            "postPaymentUrl": "https://wasteking.co.uk/thank-you/"
        }
        
        log_with_timestamp(f"📋 STEP 4 - Quote: {json.dumps(step4_data, indent=2)}")
        result4 = call_wasteking_api('update', step4_data)
        if not result4:
            log_error("Step 4 failed - quote generation")
            return None
        
        # Extract quote response (from Image 4)
        quote = result4.get('quote', {})
        payment_link = result4.get('paymentLink', '')
        
        log_with_timestamp(f"✅ WasteKing booking created successfully: {booking_ref}")
        log_with_timestamp(f"💰 Quote: {json.dumps(quote, indent=2)}")
        
        return {
            "booking_ref": booking_ref,
            "quote": quote,
            "payment_link": payment_link,
            "post_payment_url": result4.get('postPaymentUrl', ''),
            "success": True
        }
        
    except Exception as e:
        log_error("Error creating WasteKing booking", e)
        return None

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

# --- Database Functions (ENHANCED WITH NEW FEATURES) ---
def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def create_all_wasteking_users():
    """Create all Waste King users from the system screenshots with email as username and password!12 as default password"""
    
    # Complete list of all users from both screenshots
    wasteking_users = [
        # From Image 1
        ('anxhela.kopaci@wasteking.co.uk', 'Anxhela Kopaci'),
        ('caroline.jones@wasteking.co.uk', 'Caroline Jones'),
        ('chris.hood@wasteking.co.uk', 'Chris Hood'),
        ('clara.roake@wasteking.co.uk', 'Clara Roake'),
        ('courtney.wildman@wasteking.co.uk', 'Courtney Wildman'),
        ('farron.bishop@wasteking.co.uk', 'Farron Bishop'),
        ('jack.herring@wasteking.co.uk', 'Jack Herring'),
        ('jackie.duke@wasteking.co.uk', 'Jackie Duke'),
        ('jody.conybeare-jones@wasteking.co.uk', 'Jody Conybeare-Jones'),
        ('keith.middleton@wasteking.co.uk', 'Keith Middleton'),
        ('kristy.stuart@wasteking.co.uk', 'Kristy Stuart'),
        ('lauren.czyzewicz@wasteking.co.uk', 'Lauren Czyzewicz'),
        ('marc@wasteking.co.uk', 'Marc Reeve'),
        ('michael.brearley@wasteking.co.uk', 'Micky Brearley'),
        ('ryan.willis@wasteking.co.uk', 'Ryan Willis'),
        ('sara.smith@wasteking.co.uk', 'Sara Smith'),
        ('tracey.bower@wasteking.co.uk', 'Tracey Bower'),
        ('transfersite@wasteking.co.uk', 'Weighbridge Office'),
        
        # From Image 2
        ('belinda.moyo@wasteking.co.uk', 'Belinda Moyo'),
        ('georgiana.glavac@wasteking.co.uk', 'Georgiana Glavac'),
        ('helen.curtis@wasteking.co.uk', 'Helen Curtis'),
        ('kanchan.ghosh@wasteking.co.uk', 'Kanchan Ghosh'),
        ('russell.nurse@wasteking.co.uk', 'Russell Nurse'),
        ('wallboard2@wasteking.co.uk', 'Wallboard 2'),
        ('wallboard@wasteking.co.uk', 'Wallboard'),
        ('abi.housego@wasteking.co.uk', 'Abi Housego'),
        ('alec.henson@wasteking.co.uk', 'Alec Henson'),
        ('alison.janes@wasteking.co.uk', 'Alison Janes'),
        ('anita.hoskins@wasteking.co.uk', 'Anita Hoskins'),
        ('behaerder@wasteking.co.uk', 'Behaerder'),
        ('apiuser@wasteking.co.uk', 'API User')
    ]
    
    default_password = 'password!12'
    password_hash = hashlib.sha256(default_password.encode()).hexdigest()
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        users_created = 0
        users_skipped = 0
        
        for email, full_name in wasteking_users:
            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO users (username, password_hash, role, full_name, created_by, created_at, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (email, password_hash, 'user', full_name, 'system', datetime.now().isoformat(), 1))
                
                if cursor.rowcount > 0:
                    users_created += 1
                    log_with_timestamp(f"✅ Created user: {email} ({full_name})")
                else:
                    users_skipped += 1
                    
            except Exception as e:
                log_error(f"Failed to create user {email}", e)
        
        conn.commit()
        conn.close()
        
        log_with_timestamp(f"🎉 User creation complete: {users_created} created, {users_skipped} already existed")
        
    except Exception as e:
        log_error("Failed to create Waste King users", e)

def init_db():
    log_with_timestamp("Initializing database...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Users table (NEW FEATURE)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                full_name TEXT NOT NULL,
                created_by TEXT,
                created_at TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')
        
        # Bookings table for WasteKing API calls
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_sid TEXT,
                booking_ref TEXT UNIQUE,
                customer_name TEXT,
                customer_phone TEXT,
                customer_email TEXT,
                postcode TEXT,
                service_type TEXT,
                service_size TEXT,
                delivery_date TEXT,
                placement TEXT,
                quote_price TEXT,
                payment_link TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                booking_data TEXT
            )
        ''')
        
        # Updated calls table with YOUR NEW Waste King Criteria (from first document)
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
                assigned_username TEXT,
                transcription_text TEXT,
                transcribed_duration_minutes REAL,
                deepgram_cost_usd REAL,
                deepgram_cost_gbp REAL,
                word_count INTEGER,
                confidence REAL,
                language TEXT,
                
                -- YOUR NEW Waste King Evaluation Criteria (from your first document)
                greeting_closing_score REAL,
                customer_needs_identification REAL,
                product_knowledge_score REAL,
                pricing_accuracy REAL,
                sales_approach_score REAL,
                objection_handling REAL,
                call_closing_score REAL,
                overall_professionalism REAL,
                waste_king_compliance REAL,
                
                -- YOUR NEW Sub-scores based on training manual (from your first document)
                empathy_score REAL,
                active_listening_score REAL,
                solution_offering_score REAL,
                permit_awareness REAL,
                prohibited_items_check REAL,
                access_assessment REAL,
                due_diligence_completion REAL,
                
                category TEXT,
                processed_at TEXT,
                processing_error TEXT,
                raw_communication_data TEXT,
                summary_translation TEXT,
                manager_notes TEXT,
                coaching_required INTEGER DEFAULT 0
            )
        ''')
        
        # Insert default manager user
        cursor.execute('''
            INSERT OR IGNORE INTO users (username, password_hash, role, full_name, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', ('manager', hashlib.sha256('manager!1'.encode()).hexdigest(), 'manager', 'System Manager', datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        # Create all Waste King users
        create_all_wasteking_users()
        
        log_with_timestamp("Database initialized successfully")
    except Exception as e:
        log_error("Failed to initialize database", e)

# --- User Management Functions (NEW FEATURES) ---
def authenticate_user(username: str, password: str) -> Optional[Dict]:
    """Authenticate user and return user info"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        cursor.execute('''
            SELECT id, username, role, full_name, is_active 
            FROM users 
            WHERE username = ? AND password_hash = ? AND is_active = 1
        ''', (username, password_hash))
        
        user = cursor.fetchone()
        conn.close()
        
        if user:
            return {
                'id': user[0],
                'username': user[1],
                'role': user[2],
                'full_name': user[3],
                'is_active': user[4]
            }
        return None
    except Exception as e:
        log_error("Authentication error", e)
        return None

def create_user(username: str, password: str, full_name: str, created_by: str) -> bool:
    """Create new user"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        cursor.execute('''
            INSERT INTO users (username, password_hash, role, full_name, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (username, password_hash, 'user', full_name, created_by, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log_error("Error creating user", e)
        return False

def get_all_users() -> List[Dict]:
    """Get all users for manager"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, username, full_name, role, created_at, is_active
            FROM users
            ORDER BY created_at DESC
        ''')
        
        users = []
        for row in cursor.fetchall():
            users.append({
                'id': row[0],
                'username': row[1],
                'full_name': row[2],
                'role': row[3],
                'created_at': row[4],
                'is_active': row[5]
            })
        
        conn.close()
        return users
    except Exception as e:
        log_error("Error fetching users", e)
        return []

# Agent name to email mapping (NEW FEATURE)
def assign_call_to_user(agent_name: str) -> Optional[str]:
    """Match agent name to email username for call assignment"""
    
    # FILTER: Only process Sales Team and Kanchan Ghosh calls
    if not is_sales_team_or_kanchan(agent_name):
        log_with_timestamp(f"🚫 Skipping call from {agent_name} - not Sales Team or Kanchan")
        return None
    
    log_with_timestamp(f"✅ Processing call from {agent_name} - Sales Team or Kanchan")
    
    # Mapping of common agent names to email addresses
    agent_email_mapping = {
        'Anxhela Kopaci': 'anxhela.kopaci@wasteking.co.uk',
        'Caroline Jones': 'caroline.jones@wasteking.co.uk', 
        'Chris Hood': 'chris.hood@wasteking.co.uk',
        'Clara Roake': 'clara.roake@wasteking.co.uk',
        'Courtney Wildman': 'courtney.wildman@wasteking.co.uk',
        'Farron Bishop': 'farron.bishop@wasteking.co.uk',
        'Jack Herring': 'jack.herring@wasteking.co.uk',
        'Jackie Duke': 'jackie.duke@wasteking.co.uk',
        'Jody Conybeare-Jones': 'jody.conybeare-jones@wasteking.co.uk',
        'Keith Middleton': 'keith.middleton@wasteking.co.uk',
        'Lee S (Driver) S': 'keith.middleton@wasteking.co.uk',  # Same person
        'Kristy Stuart': 'kristy.stuart@wasteking.co.uk',
        'Lauren Czyzewicz': 'lauren.czyzewicz@wasteking.co.uk',
        'Marc Reeve': 'marc@wasteking.co.uk',
        'Micky Brearley': 'michael.brearley@wasteking.co.uk',
        'Ryan Willis': 'ryan.willis@wasteking.co.uk',
        'Sara Smith': 'sara.smith@wasteking.co.uk',
        'Tracey Bower': 'tracey.bower@wasteking.co.uk',
        'Weighbridge Office': 'transfersite@wasteking.co.uk',
        'Belinda Moyo': 'belinda.moyo@wasteking.co.uk',
        'Georgiana Glavac': 'georgiana.glavac@wasteking.co.uk',
        'Helen Curtis': 'helen.curtis@wasteking.co.uk',
        'Kanchan Ghosh': 'kanchan.ghosh@wasteking.co.uk',
        'Russell Nurse': 'russell.nurse@wasteking.co.uk',
        'Wallboard 2': 'wallboard2@wasteking.co.uk',
        'Wallboard': 'wallboard@wasteking.co.uk',
        'Abi Housego': 'abi.housego@wasteking.co.uk',
        'Alec Henson': 'alec.henson@wasteking.co.uk',
        'Alison Janes': 'alison.janes@wasteking.co.uk',
        'Anita Hoskins': 'anita.hoskins@wasteking.co.uk',
        'Behaerder': 'behaerder@wasteking.co.uk',
        'API User': 'apiuser@wasteking.co.uk',
        
        # Generic mappings that don't assign to specific users
        'Main Number': None,
        'Unknown': None,
    }
    
    # Direct mapping first
    if agent_name in agent_email_mapping:
        return agent_email_mapping[agent_name]
    
    # Partial name matching for cases where agent name might be slightly different
    if agent_name and agent_name != 'Unknown':
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Try exact match on full name
            cursor.execute("SELECT username FROM users WHERE full_name = ?", (agent_name,))
            user = cursor.fetchone()
            
            if not user:
                # Try partial match on full name
                cursor.execute("SELECT username FROM users WHERE full_name LIKE ?", (f"%{agent_name}%",))
                user = cursor.fetchone()
            
            conn.close()
            
            if user:
                return user[0]
                
        except Exception as e:
            log_error(f"Error matching agent name {agent_name} to user", e)
    
    return None

# --- Flask App Setup (ENHANCED WITH NEW FEATURES) ---
app = Flask(__name__)
app.secret_key = 'waste_king_secret_key_2025'  # Change this in production

# Configure session settings for persistence
app.config.update(
    SESSION_COOKIE_SECURE=False,  # Set to True in production with HTTPS
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_PATH='/',
    SESSION_COOKIE_DOMAIN=None,  # Use default
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),  # 8 hour sessions
    SESSION_COOKIE_NAME='wasteking_session',
    SESSION_REFRESH_EACH_REQUEST=True  # Refresh session on each request
)

# Add CORS headers for development
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    response.headers.add('Access-Control-Allow-Credentials', 'true')  # Important for sessions
    return response

# Handle OPTIONS requests for CORS
@app.route('/api/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    return '', 200

# Initialize database
init_db()

# --- NEW: WasteKing API Webhook Endpoints ---
@app.route('/api/wasteking-quote', methods=['POST'])
def wasteking_quote():
    """Handle quote requests from ElevenLabs AI"""
    try:
        data = request.get_json()
        log_with_timestamp(f"🎯 WasteKing quote request: {json.dumps(data, indent=2)}")
        
        # FIX POSTCODE FIRST - before any validation
        if data.get('postcode'):
            data['postcode'] = fix_uk_postcode(data['postcode'])
            log_with_timestamp(f"📍 Using postcode: {data['postcode']}")
        
        # Set ALL required defaults immediately
        required_defaults = {
            'service': 'mav',
            'type': '6yd',
            'firstName': 'Customer',
            'lastName': 'Unknown',
            'phone': '01234567890',
            'emailAddress': 'customer@example.com',
            'address1': 'Customer Address',
            'addressCity': 'Leeds',
            'addressPostcode': data.get('postcode', 'LS1 4ED'),
            'placement': 'drive',
            'date': datetime.now().strftime('%Y-%m-%d'),
            'time': 'am'
        }
        
        # Apply defaults for missing fields
        for field, default_value in required_defaults.items():
            if not data.get(field):
                data[field] = default_value
                log_with_timestamp(f"🔧 Set default {field}: {default_value}")
        
        # Ensure addressPostcode matches postcode
        data['addressPostcode'] = data['postcode']
        
        # Create WasteKing booking
        booking_result = create_wasteking_booking(data)
        
        if not booking_result:
            return jsonify({
                'success': False,
                'error': 'Failed to create booking',
                'speak': 'I apologize, there was an issue getting your quote. Let me transfer you to our team who can help you directly.'
            }), 500
        
        # Create WasteKing booking
        booking_result = create_wasteking_booking(data)
        
        if not booking_result:
            return jsonify({
                'success': False,
                'error': 'Failed to create booking',
                'speak': 'I apologize, there was an issue getting your quote. Let me transfer you to our team who can help you directly.'
            }), 500
        
        
        # Store booking in database
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR REPLACE INTO bookings (
                    call_sid, booking_ref, customer_name, customer_phone, customer_email,
                    postcode, service_type, service_size, delivery_date, placement,
                    quote_price, payment_link, status, created_at, booking_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                data.get('call_sid', 'unknown'),
                booking_result['booking_ref'],
                f"{data['firstName']} {data['lastName']}",
                data['phone'],
                data['emailAddress'],
                data['postcode'],
                data['service'],
                data['type'],
                data['date'],
                data['placement'],
                booking_result['quote'].get('price', '0'),
                booking_result['payment_link'],
                'quoted',
                datetime.now().isoformat(),
                json.dumps(data)
            ))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            log_error("Error storing booking", e)
        
        # Format response for AI
        quote = booking_result['quote']
        service_price = quote.get('servicePrice', '0')
        supplement_price = quote.get('supplementsPrice', '0')
        total_price = quote.get('price', '0')
        
        response_text = f"Perfect! I've got your quote ready. For a {data['type']} skip delivered to {data['postcode']} on {data['date']}, the total cost is £{total_price}."
        
        if supplement_price and supplement_price != '0':
            response_text += f" This includes £{service_price} for the skip hire and £{supplement_price} for additional items."
        
        response_text += " I can send you a secure payment link now to complete your booking. Shall I send that to you?"
        
        return jsonify({
            'success': True,
            'booking_ref': booking_result['booking_ref'],
            'quote': quote,
            'payment_link': booking_result['payment_link'],
            'speak': response_text
        })
        
    except Exception as e:
        log_error("Error in wasteking_quote endpoint", e)
        return jsonify({
            'success': False,
            'error': 'Internal server error',
            'speak': 'I apologize, there was a technical issue. Let me transfer you to our team who can help you directly.'
        }), 500

@app.route('/api/send-payment-sms', methods=['POST'])
def send_payment_sms():
    """Handle payment SMS requests from ElevenLabs AI"""
    try:
        data = request.get_json()
        log_with_timestamp(f"💳 Payment SMS request: {json.dumps(data, indent=2)}")
        
        call_sid = data.get('call_sid')
        customer_phone = data.get('phone')
        amount = data.get('amount', 'GBP 1')
        
        if not all([call_sid, customer_phone]):
            return jsonify({
                'success': False,
                'error': 'Missing call_sid or phone',
                'speak': 'I need your phone number to send the payment link.'
            }), 400
        
        # Get booking details from database
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute('SELECT booking_ref, payment_link FROM bookings WHERE call_sid = ? ORDER BY created_at DESC LIMIT 1', (call_sid,))
            booking = cursor.fetchone()
            conn.close()
            
            if not booking:
                return jsonify({
                    'success': False,
                    'error': 'No booking found for this call',
                    'speak': 'I couldn\'t find your booking details. Let me transfer you to our team.'
                }), 404
            
            booking_ref, payment_link = booking
            
        except Exception as e:
            log_error("Error retrieving booking", e)
            return jsonify({
                'success': False,
                'error': 'Database error',
                'speak': 'There was an issue retrieving your booking. Let me transfer you to our team.'
            }), 500
        
        # Send actual SMS with Twilio (your existing SMS code)
        try:
            from twilio.rest import Client
            client = Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN'))
            
            # Format UK phone number
            if not customer_phone.startswith('+'):
                if customer_phone.startswith('0'):
                    customer_phone = '+44' + customer_phone[1:]
                else:
                    customer_phone = '+44' + customer_phone
            
            sms_message = f"WasteKing: Your secure payment link: {payment_link} - Complete your booking now. Ref: {booking_ref}"
            
            message = client.messages.create(
                body=sms_message,
                from_=os.getenv('TWILIO_PHONE_NUMBER', '+441234567890'),
                to=customer_phone
            )
            
            log_with_timestamp(f"📱 SMS sent successfully to {customer_phone}. SID: {message.sid}")
            
        except Exception as sms_error:
            log_error(f"SMS sending failed to {customer_phone}", sms_error)
            # Continue anyway - don't fail the API call
        
        return jsonify({
            'success': True,
            'message': 'Payment link sent',
            'booking_ref': booking_ref,
            'speak': f'Perfect! I\'ve sent a secure payment link to {customer_phone}. You can pay now while I\'m here, or complete it later. Once you\'ve paid, you\'ll receive an automatic confirmation by text with all your booking details.'
        })
        
    except Exception as e:
        log_error("Error in send_payment_sms endpoint", e)
        return jsonify({
            'success': False,
            'error': 'Internal server error',
            'speak': 'There was an issue sending the payment link. Let me transfer you to our team.'
        }), 500

@app.route('/api/check-postcode', methods=['POST'])
def check_postcode():
    """Check if WasteKing services the given postcode"""
    try:
        data = request.get_json()
        raw_postcode = data.get('postcode', '').strip()
        
        if not raw_postcode:
            return jsonify({
                'success': False,
                'error': 'Postcode required',
                'speak': 'I need your postcode to check if we can service your area.'
            }), 400
        
        # Fix postcode format immediately
        postcode = fix_uk_postcode(raw_postcode)
        log_with_timestamp(f"📍 Checking postcode: '{raw_postcode}' -> '{postcode}'")
        
        # Basic UK postcode validation (after formatting)
        uk_postcode_pattern = r'^[A-Z]{1,2}[0-9R][0-9A-Z]? [0-9][A-Z]{2}

@app.route('/api/transfer-call', methods=['POST'])
def transfer_call():
    """Handle call transfer requests from ElevenLabs AI"""
    try:
        data = request.get_json()
        log_with_timestamp(f"🔄 Call transfer request: {json.dumps(data, indent=2)}")
        
        call_sid = data.get('call_sid')
        customer_name = data.get('customer_name')
        reason = data.get('reason')
        
        if not all([call_sid, customer_name, reason]):
            return jsonify({
                'success': False,
                'error': 'Missing required transfer details'
            }), 400
        
        # Log transfer request (you would integrate with your call center system here)
        transfer_details = {
            'call_sid': call_sid,
            'customer_name': customer_name,
            'phone_number': data.get('phone_number'),
            'postcode': data.get('postcode'),
            'service_type': data.get('service_type'),
            'reason': reason,
            'urgency': data.get('urgency', 'medium'),
            'timestamp': datetime.now().isoformat()
        }
        
        log_with_timestamp(f"📋 Transfer logged: {json.dumps(transfer_details, indent=2)}")
        
        return jsonify({
            'success': True,
            'transfer_id': str(uuid.uuid4()),
            'speak': f"I have all your details, {customer_name}. Please hold while I transfer you to the right person who can help with {reason}. They\'ll be with you shortly."
        })
        
    except Exception as e:
        log_error("Error in transfer_call endpoint", e)
        return jsonify({
            'success': False,
            'error': 'Internal server error',
            'speak': 'Let me transfer you to our team right away.'
        }), 500

# --- Authentication Routes (EXISTING) ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return redirect('/')
        
    try:
        if request.is_json:
            data = request.get_json()
            username = data.get('username') if data else None
            password = data.get('password') if data else None
        else:
            username = request.form.get('username')
            password = request.form.get('password')
        
        log_with_timestamp(f"Login attempt for username: {username}")
        
        if not username or not password:
            return jsonify({'success': False, 'message': 'Username and password required'})
        
        user = authenticate_user(username, password)
        if user:
            session.permanent = True
            session['user'] = user
            session['login_time'] = datetime.now().isoformat()
            
            log_with_timestamp(f"User {username} logged in successfully")
            return jsonify({'success': True, 'user': user})
        else:
            log_with_timestamp(f"Failed login attempt for username: {username}")
            return jsonify({'success': False, 'message': 'Invalid credentials'})
            
    except Exception as e:
        log_error("Login error", e)
        return jsonify({'success': False, 'message': 'Login failed - server error'})

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    try:
        if 'user' in session:
            log_with_timestamp(f"User {session['user']['username']} logged out")
        session.clear()
        
        if request.method == 'GET':
            return redirect('/')
        return jsonify({'success': True})
        
    except Exception as e:
        log_error("Logout error", e)
        if request.method == 'GET':
            return redirect('/')
        return jsonify({'success': False, 'message': 'Logout failed'})

@app.route('/check-session')
def check_session():
    """Check if user session is still active"""
    try:
        log_with_timestamp(f"Session check - Session keys: {list(session.keys())}")
        
        if 'user' in session:
            user = session['user']
            login_time = session.get('login_time', 'Unknown')
            log_with_timestamp(f"✅ Active session found for user: {user['username']}")
            return jsonify({
                'logged_in': True, 
                'user': user,
                'login_time': login_time,
                'session_keys': list(session.keys())
            })
        else:
            log_with_timestamp("❌ No active session found")
            return jsonify({'logged_in': False, 'session_keys': list(session.keys())})
    except Exception as e:
        log_error("Session check error", e)
        return jsonify({'logged_in': False, 'error': str(e)})

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')

@app.route('/status')
def get_status():
    """Get system status"""
    openai_test_result = test_openai_connection()
    return jsonify({
        "background_running": background_process_running,
        "processing_stats": processing_stats,
        "openai_available": OPENAI_AVAILABLE,
        "openai_connection_test": openai_test_result,
        "openai_api_key_configured": OPENAI_API_KEY and OPENAI_API_KEY != 'your_openai_api_key',
        "wasteking_api_configured": WASTEKING_ACCESS_TOKEN and WASTEKING_ACCESS_TOKEN != 'your_wasteking_access_token',
        "last_poll": processing_stats.get('last_poll_time', 'Never'),
        "last_error": processing_stats.get('last_error', 'None'),
        "session_active": 'user' in session,
        "current_user": session.get('user', {}).get('username', 'None') if 'user' in session else 'None'
    })

if __name__ == '__main__':
    # Test configurations on startup
    log_with_timestamp("🚀 WasteKing AI Integration System starting up...")
    log_with_timestamp(f"OpenAI available: {OPENAI_AVAILABLE}")
    log_with_timestamp(f"WasteKing API configured: {WASTEKING_ACCESS_TOKEN != 'your_wasteking_access_token'}")
    
    if OPENAI_AVAILABLE and OPENAI_API_KEY and OPENAI_API_KEY != 'your_openai_api_key':
        openai_test = test_openai_connection()
        log_with_timestamp(f"OpenAI test: {'✅ PASSED' if openai_test else '❌ FAILED'}")
    else:
        log_with_timestamp("⚠️ OpenAI not configured - using fallback scores")
    
    log_with_timestamp("✅ All users created with email logins and password: password!12")
    log_with_timestamp("✅ Manager login: manager / manager!1")
    log_with_timestamp("✅ WasteKing API Integration ready at http://localhost:5000")
    log_with_timestamp(f"✅ Sales Team filter: Only processing calls from {len(SALES_TEAM_AGENTS)} agents: {', '.join(SALES_TEAM_AGENTS)}")
    
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
        
        if re.match(uk_postcode_pattern, postcode):
            service_available = True
            speak_text = f"Great! We do service {postcode}. What type of service are you looking for today?"
        else:
            service_available = False
            speak_text = f"I'm sorry, {postcode} appears to be outside our service area. Let me transfer you to our team to see if we can arrange something special for you."
        
        return jsonify({
            'success': True,
            'postcode': postcode,  # Return the fixed postcode
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

@app.route('/api/transfer-call', methods=['POST'])
def transfer_call():
    """Handle call transfer requests from ElevenLabs AI"""
    try:
        data = request.get_json()
        log_with_timestamp(f"🔄 Call transfer request: {json.dumps(data, indent=2)}")
        
        call_sid = data.get('call_sid')
        customer_name = data.get('customer_name')
        reason = data.get('reason')
        
        if not all([call_sid, customer_name, reason]):
            return jsonify({
                'success': False,
                'error': 'Missing required transfer details'
            }), 400
        
        # Log transfer request (you would integrate with your call center system here)
        transfer_details = {
            'call_sid': call_sid,
            'customer_name': customer_name,
            'phone_number': data.get('phone_number'),
            'postcode': data.get('postcode'),
            'service_type': data.get('service_type'),
            'reason': reason,
            'urgency': data.get('urgency', 'medium'),
            'timestamp': datetime.now().isoformat()
        }
        
        log_with_timestamp(f"📋 Transfer logged: {json.dumps(transfer_details, indent=2)}")
        
        return jsonify({
            'success': True,
            'transfer_id': str(uuid.uuid4()),
            'speak': f"I have all your details, {customer_name}. Please hold while I transfer you to the right person who can help with {reason}. They\'ll be with you shortly."
        })
        
    except Exception as e:
        log_error("Error in transfer_call endpoint", e)
        return jsonify({
            'success': False,
            'error': 'Internal server error',
            'speak': 'Let me transfer you to our team right away.'
        }), 500

# --- Authentication Routes (EXISTING) ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return redirect('/')
        
    try:
        if request.is_json:
            data = request.get_json()
            username = data.get('username') if data else None
            password = data.get('password') if data else None
        else:
            username = request.form.get('username')
            password = request.form.get('password')
        
        log_with_timestamp(f"Login attempt for username: {username}")
        
        if not username or not password:
            return jsonify({'success': False, 'message': 'Username and password required'})
        
        user = authenticate_user(username, password)
        if user:
            session.permanent = True
            session['user'] = user
            session['login_time'] = datetime.now().isoformat()
            
            log_with_timestamp(f"User {username} logged in successfully")
            return jsonify({'success': True, 'user': user})
        else:
            log_with_timestamp(f"Failed login attempt for username: {username}")
            return jsonify({'success': False, 'message': 'Invalid credentials'})
            
    except Exception as e:
        log_error("Login error", e)
        return jsonify({'success': False, 'message': 'Login failed - server error'})

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    try:
        if 'user' in session:
            log_with_timestamp(f"User {session['user']['username']} logged out")
        session.clear()
        
        if request.method == 'GET':
            return redirect('/')
        return jsonify({'success': True})
        
    except Exception as e:
        log_error("Logout error", e)
        if request.method == 'GET':
            return redirect('/')
        return jsonify({'success': False, 'message': 'Logout failed'})

@app.route('/check-session')
def check_session():
    """Check if user session is still active"""
    try:
        log_with_timestamp(f"Session check - Session keys: {list(session.keys())}")
        
        if 'user' in session:
            user = session['user']
            login_time = session.get('login_time', 'Unknown')
            log_with_timestamp(f"✅ Active session found for user: {user['username']}")
            return jsonify({
                'logged_in': True, 
                'user': user,
                'login_time': login_time,
                'session_keys': list(session.keys())
            })
        else:
            log_with_timestamp("❌ No active session found")
            return jsonify({'logged_in': False, 'session_keys': list(session.keys())})
    except Exception as e:
        log_error("Session check error", e)
        return jsonify({'logged_in': False, 'error': str(e)})

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')

@app.route('/status')
def get_status():
    """Get system status"""
    openai_test_result = test_openai_connection()
    return jsonify({
        "background_running": background_process_running,
        "processing_stats": processing_stats,
        "openai_available": OPENAI_AVAILABLE,
        "openai_connection_test": openai_test_result,
        "openai_api_key_configured": OPENAI_API_KEY and OPENAI_API_KEY != 'your_openai_api_key',
        "wasteking_api_configured": WASTEKING_ACCESS_TOKEN and WASTEKING_ACCESS_TOKEN != 'your_wasteking_access_token',
        "last_poll": processing_stats.get('last_poll_time', 'Never'),
        "last_error": processing_stats.get('last_error', 'None'),
        "session_active": 'user' in session,
        "current_user": session.get('user', {}).get('username', 'None') if 'user' in session else 'None'
    })

if __name__ == '__main__':
    # Test configurations on startup
   
    
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
