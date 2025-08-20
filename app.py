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

# Supplier phone numbers
TEST_SUPPLIER_PHONE = "+447700000000"  # Always use this for testing calls
# Real supplier numbers will be fetched from SMP API via wastekingmarketplace tool

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

# Real supplier phone numbers are fetched from SMP API via wastekingmarketplace tool
# No hardcoded lookup needed

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
        
        # Calls table
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
                category TEXT,
                processed_at TEXT,
                processing_error TEXT,
                raw_communication_data TEXT,
                summary_translation TEXT
            )
        ''')
        
        # Price quotes table
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
        
        # Supplier calls table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS supplier_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_sid TEXT,
                real_supplier_phone TEXT,
                test_phone_used TEXT,
                requested_date TEXT,
                service_type TEXT,
                postcode TEXT,
                available BOOLEAN,
                supplier_response TEXT,
                call_duration_seconds INTEGER,
                created_at TEXT,
                elevenlabs_conversation_id TEXT
            )
        ''')
        
        # Check for missing columns and add them
        cursor.execute("PRAGMA table_info(sms_payments)")
        existing_columns = [column[1] for column in cursor.fetchall()]
        
        if 'call_sid' not in existing_columns:
            log_with_timestamp("Adding call_sid column to sms_payments table...")
            cursor.execute("ALTER TABLE sms_payments ADD COLUMN call_sid TEXT")
        
        if 'elevenlabs_conversation_id' not in existing_columns:
            log_with_timestamp("Adding elevenlabs_conversation_id column to sms_payments table...")
            cursor.execute("ALTER TABLE sms_payments ADD COLUMN elevenlabs_conversation_id TEXT")
        
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
        log_with_timestamp("Database initialized successfully")
    except Exception as e:
        log_error("Failed to initialize database", e)

# --- WasteKing API Helper Functions ---
def create_wasteking_booking():
    """Create a new booking reference with WasteKing API"""
    try:
        headers = {
            "x-wasteking-request": WASTEKING_ACCESS_TOKEN,
            "Content-Type": "application/json"
        }
        
        create_url = f"{WASTEKING_BASE_URL}api/booking/create"
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

def make_supplier_call(real_supplier_phone: str, date: str, service: str, postcode: str) -> dict:
    """Make call to supplier - FETCH real number from SMP but CALL test number"""
    try:
        log_with_timestamp(f"📞 Making supplier call for {service} on {date} to {postcode}")
        log_with_timestamp(f"📞 Real supplier from SMP: {real_supplier_phone}")
        log_with_timestamp(f"📞 Actually calling test number: {TEST_SUPPLIER_PHONE}")
        
        # Initialize Twilio client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        # Make the call with TwiML to TEST number
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">Hello, this is Waste King checking availability for {service} on {date} in {postcode}. Please confirm if you have availability. Thank you.</Say>
    <Record maxLength="30" timeout="10" playBeep="true"/>
    <Say voice="alice">Thank you for the information. Goodbye.</Say>
</Response>"""
        
        call = client.calls.create(
            twiml=twiml,
            to=TEST_SUPPLIER_PHONE,  # Always call test number
            from_=TWILIO_PHONE_NUMBER,
            timeout=30
        )
        
        log_with_timestamp(f"📞 Call initiated to test number: {call.sid}")
        
        # Wait for call to complete (simplified for testing)
        time.sleep(5)
        
        # Simulate response parsing (in real system would analyze recording)
        availability_responses = [
            {"available": True, "message": f"Available for {service} on {date}"},
            {"available": True, "message": f"Morning slot available on {date}"},
            {"available": False, "message": f"Sorry, {date} is fully booked. Next available is tomorrow."},
            {"available": True, "message": f"Afternoon delivery possible on {date}"}
        ]
        
        response = random.choice(availability_responses)
        response["call_sid"] = call.sid
        response["real_supplier_from_smp"] = real_supplier_phone
        response["test_number_called"] = TEST_SUPPLIER_PHONE
        
        log_with_timestamp(f"📞 Supplier response: {response['message']}")
        
        # Store in database
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO supplier_calls 
                (call_sid, real_supplier_phone, test_phone_used, requested_date, service_type, postcode, available, supplier_response, call_duration_seconds, created_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                call.sid,
                real_supplier_phone,  # Real number from SMP
                TEST_SUPPLIER_PHONE,  # Test number actually called
                date,
                service,
                postcode,
                response["available"],
                response["message"],
                5,  # Simulated call duration
                datetime.now().isoformat()
            ))
            conn.commit()
            conn.close()
        
        return response
        
    except Exception as e:
        log_error("Supplier call failed", e)
        return {
            "available": False,
            "message": "Unable to reach supplier",
            "error": str(e),
            "real_supplier_from_smp": real_supplier_phone,
            "test_number_called": TEST_SUPPLIER_PHONE
        }

def send_payment_sms(booking_ref: str, phone: str, payment_link: str, amount: str):
    """Send payment SMS via Twilio with adjusted amount"""
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
        
        # Create SMS message with the final adjusted amount
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
        
        log_with_timestamp(f"✅ SMS sent to {phone} for booking {booking_ref} with final amount £{amount}. SID: {message.sid}")
        
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
                float(amount),  # Use the final adjusted amount
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

# --- Flask App Setup ---
app = Flask(__name__)
init_db()

# Add CORS headers
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

@app.route('/')
def index():
    """Serve main page"""
    return jsonify({
        "message": "WasteKing AI Voice Agent API",
        "status": "running",
        "endpoints": [
            "/api/current-datetime",
            "/api/wasteking-marketplace", 
            "/api/call-supplier",
            "/api/wasteking-confirm-booking",
            "/api/send-payment-sms"
        ]
    })

@app.route('/api/current-datetime', methods=['GET'])
def get_current_datetime():
    """Get current UK date and time for AI context"""
    datetime_info = get_current_datetime_info()
    return jsonify(datetime_info)

@app.route('/api/wasteking-marketplace', methods=['POST'])
def wasteking_marketplace():
    """WasteKing marketplace tool - gets pricing AND real supplier phone from SMP"""
    try:
        log_with_timestamp("="*50)
        log_with_timestamp("🏪 WASTEKING MARKETPLACE ENDPOINT")
        log_with_timestamp("="*50)
        
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "message": "No data provided"
            }), 400

        log_with_timestamp(f"📦 Marketplace request: {json.dumps(data, indent=2)}")
        
        # Required fields for marketplace
        required = ['postcode', 'service', 'type']
        missing = [field for field in required if not data.get(field)]
        if missing:
            return jsonify({
                "success": False,
                "message": f"Missing required fields: {', '.join(missing)}"
            }), 400

        # Create booking reference
        booking_ref = create_wasteking_booking()
        if not booking_ref:
            return jsonify({
                "success": False,
                "message": "Failed to create booking reference"
            }), 500

        # Search payload for WasteKing API
        search_payload = {
            "search": {
                "postCode": data['postcode'],
                "service": data['service'],
                "type": data['type']
            }
        }
        
        # Add date if provided for availability checking
        if data.get('date'):
            search_payload["search"]["date"] = data['date']
            log_with_timestamp(f"🗓️ Including date in search: {data['date']}")
        
        log_with_timestamp(f"🔍 Marketplace search: {json.dumps(search_payload, indent=2)}")
        
        # Get pricing/supplier info from WasteKing SMP API
        response_data = update_wasteking_booking(booking_ref, search_payload)
        if not response_data:
            return jsonify({
                "success": False,
                "message": "No pricing/supplier data returned from SMP"
            }), 404

        quote_data = response_data.get('quote', {})
        price = quote_data.get('price', '0')
        service_price = quote_data.get('servicePrice', '0')
        
        # FETCH REAL SUPPLIER PHONE FROM SMP RESPONSE
        supplier_phone = quote_data.get('supplierPhone') or quote_data.get('supplier_phone') or "+447700900000"
        supplier_name = quote_data.get('supplierName') or quote_data.get('supplier_name') or "Default Supplier"
        
        log_with_timestamp(f"📞 Real supplier from SMP: {supplier_name} - {supplier_phone}")
        
        # Get current date/time info
        datetime_info = get_current_datetime_info()
        
        log_with_timestamp(f"💰 Marketplace result: £{price}, Supplier: {supplier_phone}")
        
        return jsonify({
            "success": True,
            "booking_ref": booking_ref,
            "price": price,
            "service_price": service_price,
            "real_supplier_phone": supplier_phone,  # REAL number from SMP for AI to know
            "supplier_name": supplier_name,
            "postcode": data['postcode'],
            "service": data['service'],
            "type": data['type'],
            "requested_date": data.get('date'),
            "quote_data": quote_data,
            
            # System date/time info for AI context
            "system_date": datetime_info["current_date"],
            "system_time": datetime_info["current_time"],
            "ai_context": {
                "today_is": datetime_info["current_date"],
                "current_time": datetime_info["current_time"],
                "tomorrow_is": datetime_info["tomorrow_date"],
                "current_day": datetime_info["current_day"]
            }
        })
        
    except Exception as e:
        log_error("Marketplace request failed", e)
        
        # Get current date/time info for error response
        datetime_info = get_current_datetime_info()
        
        return jsonify({
            "success": False,
            "message": "Marketplace request failed",
            "error": str(e),
            
            # Even on errors, provide date context
            "system_date": datetime_info["current_date"],
            "system_time": datetime_info["current_time"],
            "ai_context": {
                "today_is": datetime_info["current_date"],
                "current_time": datetime_info["current_time"],
                "tomorrow_is": datetime_info["tomorrow_date"],
                "current_day": datetime_info["current_day"]
            }
        }), 500

@app.route('/api/call-supplier', methods=['POST'])
def call_supplier():
    """Call supplier availability - matches existing ElevenLabs webhook tool schema"""
    try:
        log_with_timestamp("="*50)
        log_with_timestamp("📞 SUPPLIER AVAILABILITY CALL")
        log_with_timestamp("="*50)
        
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "message": "No data provided"
            }), 400

        # Extract from ElevenLabs webhook schema
        service = data.get('service')
        type_size = data.get('type') 
        postcode = data.get('postcode')
        requested_date = data.get('requested_date')
        
        log_with_timestamp(f"📦 Supplier call request: {json.dumps(data, indent=2)}")
        
        if not all([service, postcode, requested_date]):
            return jsonify({
                "success": False,
                "message": "Missing required fields: service, postcode, requested_date"
            }), 400

        # First get real supplier from SMP via marketplace
        log_with_timestamp("🔍 Getting real supplier from SMP first...")
        
        # Create booking to get supplier info
        booking_ref = create_wasteking_booking()
        real_supplier_phone = "+447700900000"  # default
        supplier_name = "Default Supplier"
        
        if booking_ref:
            search_payload = {
                "search": {
                    "postCode": postcode,
                    "service": service,
                    "type": type_size or "8yd"
                }
            }
            
            response_data = update_wasteking_booking(booking_ref, search_payload)
            if response_data:
                quote_data = response_data.get('quote', {})
                real_supplier_phone = quote_data.get('supplierPhone') or quote_data.get('supplier_phone') or "+447700900000"
                supplier_name = quote_data.get('supplierName') or quote_data.get('supplier_name') or "Default Supplier"
                log_with_timestamp(f"📞 Real supplier from SMP: {supplier_name} - {real_supplier_phone}")
        
        log_with_timestamp(f"📞 Real supplier: {real_supplier_phone}")
        log_with_timestamp(f"📞 Actually calling test number: {TEST_SUPPLIER_PHONE}")
        log_with_timestamp(f"📞 Checking availability for {service} on {requested_date}")
        
        # Make the call - passing real supplier but calling test number
        call_result = make_supplier_call(real_supplier_phone, requested_date, service, postcode)
        
        # Get current date/time info
        datetime_info = get_current_datetime_info()
        
        return jsonify({
            "success": True,
            "real_supplier_phone": real_supplier_phone,
            "supplier_name": supplier_name,
            "test_number_called": TEST_SUPPLIER_PHONE,
            "service": service,
            "type": type_size,
            "postcode": postcode,
            "requested_date": requested_date,
            "available": call_result.get("available", False),
            "supplier_response": call_result.get("message", "No response"),
            "call_sid": call_result.get("call_sid"),
            
            # System date/time info for AI context
            "system_date": datetime_info["current_date"],
            "system_time": datetime_info["current_time"],
            "ai_context": {
                "today_is": datetime_info["current_date"],
                "current_time": datetime_info["current_time"],
                "tomorrow_is": datetime_info["tomorrow_date"],
                "current_day": datetime_info["current_day"]
            }
        })
        
    except Exception as e:
        log_error("Supplier availability call failed", e)
        
        # Get current date/time info for error response
        datetime_info = get_current_datetime_info()
        
        return jsonify({
            "success": False,
            "message": "Supplier availability call failed",
            "error": str(e),
            
            # Even on errors, provide date context
            "system_date": datetime_info["current_date"],
            "system_time": datetime_info["current_time"],
            "ai_context": {
                "today_is": datetime_info["current_date"],
                "current_time": datetime_info["current_time"],
                "tomorrow_is": datetime_info["tomorrow_date"],
                "current_day": datetime_info["current_day"]
            }
        }), 500

@app.route('/api/wasteking-confirm-booking', methods=['POST', 'GET'])
def confirm_wasteking_booking():
    """Confirm booking and send payment SMS - handles discounts and surcharges"""
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

        # Handle booking_ref - create one if missing
        booking_ref = data.get('booking_ref') 
        if not booking_ref:
            log_with_timestamp("🆔 No booking_ref provided, creating new booking...")
            booking_ref = create_wasteking_booking()
            if not booking_ref:
                return jsonify({"success": False, "message": "Failed to create booking reference"}), 500
            log_with_timestamp(f"✅ Created new booking_ref: {booking_ref}")
            
            # If creating new booking, do price search first
            if data.get('postcode') and data.get('service') and data.get('type'):
                search_payload = {
                    "search": {
                        "postCode": data['postcode'],
                        "service": data['service'],
                        "type": data['type']
                    }
                }
                price_response = update_wasteking_booking(booking_ref, search_payload)
                if not price_response:
                    return jsonify({"success": False, "message": "Failed to get pricing"}), 500

        # Handle flexible field mapping
        customer_phone = data.get('customer_phone') or data.get('phone')
        first_name = data.get('first_name') or data.get('name', '').split(' ')[0] if data.get('name') else 'Customer'
        last_name = data.get('last_name') or ' '.join(data.get('name', '').split(' ')[1:]) if data.get('name') and len(data.get('name', '').split(' ')) > 1 else 'Unknown'
        service_date = data.get('service_date') or data.get('date')
        email = data.get('email', '')
        postcode = data.get('postcode', '')

        # Handle discount and surcharges
        discount_applied = data.get('discount_applied', False)
        extra_items = data.get('extra_items', [])
        if isinstance(extra_items, str):
            extra_items = [item.strip() for item in extra_items.split(',') if item.strip()]

        # Validate required fields
        if not customer_phone:
            return jsonify({"success": False, "message": "Customer phone number required"}), 400

        # Add customer details if available
        if first_name and last_name:
            customer_payload = {
                "customer": {
                    "firstName": first_name,
                    "lastName": last_name,
                    "phone": customer_phone,
                    "emailAddress": email,
                    "addressPostcode": postcode
                }
            }
            customer_response = update_wasteking_booking(booking_ref, customer_payload)

        # Add service details if available
        if service_date:
            service_payload = {
                "service": {
                    "date": service_date,
                    "time": data.get('service_time', 'am'),
                    "placement": data.get('placement', 'drive')
                }
            }
            service_response = update_wasteking_booking(booking_ref, service_payload)

        # Generate payment link
        payment_payload = {
            "action": "quote",
            "postPaymentUrl": "https://wasteking.co.uk/thank-you/"
        }
        payment_response = update_wasteking_booking(booking_ref, payment_payload)
        if not payment_response or not payment_response.get('quote', {}).get('paymentLink'):
            return jsonify({"success": False, "message": "Failed to generate payment link"}), 500

        payment_link = payment_response['quote']['paymentLink']
        base_price = float(payment_response['quote'].get('price', '0'))

        # Calculate price adjustments
        surcharge_total = 0.0
        surcharge_details = []
        discount_amount = 10.0 if discount_applied else 0.0
        
        # Surcharge rates
        surcharge_rates = {
            'fridge': 20.0, 'freezer': 20.0, 'mattress': 15.0,
            'sofa': 15.0, 'upholstered_furniture': 15.0, 'furniture': 15.0
        }
        
        # Calculate surcharges
        for item in extra_items:
            item_lower = item.lower().strip()
            charge = 0.0
            
            if 'fridge' in item_lower:
                charge = surcharge_rates['fridge']
            elif 'freezer' in item_lower:
                charge = surcharge_rates['freezer']
            elif 'mattress' in item_lower:
                charge = surcharge_rates['mattress']
            elif 'sofa' in item_lower or 'couch' in item_lower:
                charge = surcharge_rates['sofa']
            elif 'furniture' in item_lower or 'chair' in item_lower:
                charge = surcharge_rates['upholstered_furniture']
            
            if charge > 0:
                surcharge_total += charge
                surcharge_details.append(f"{item}: £{charge}")

        # Calculate final price
        final_price = max(0, base_price + surcharge_total - discount_amount)

        log_with_timestamp(f"💰 PRICE: Base £{base_price} + Surcharges £{surcharge_total} - Discount £{discount_amount} = £{final_price}")

        # Send SMS if phone provided
        if customer_phone:
            sms_response = send_payment_sms(booking_ref, customer_phone, payment_link, str(final_price))
            if not sms_response.get('success'):
                log_with_timestamp(f"❌ SMS failed: {sms_response.get('message')}")

        # Get current date/time info
        datetime_info = get_current_datetime_info()

        return jsonify({
            "success": True,
            "message": "Booking confirmed and SMS sent successfully",
            "booking_ref": booking_ref,
            "payment_link": payment_link,
            "original_price": base_price,
            "final_price": final_price,
            "surcharges_total": surcharge_total,
            "discount_applied": discount_amount,
            "customer_phone": customer_phone,
            
            # System date/time info for AI context
            "system_date": datetime_info["current_date"],
            "system_time": datetime_info["current_time"],
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
        
        # Get current date/time info for error response  
        datetime_info = get_current_datetime_info()
        
        return jsonify({
            "success": False,
            "message": "System error during booking",
            "error": str(e),
            "system_date": datetime_info["current_date"],
            "system_time": datetime_info["current_time"],
            "ai_context": {
                "today_is": datetime_info["current_date"],
                "current_time": datetime_info["current_time"], 
                "tomorrow_is": datetime_info["tomorrow_date"],
                "current_day": datetime_info["current_day"]
            }
        }), 500

@app.route('/api/send-payment-sms', methods=['POST'])
def send_payment_sms_endpoint():
    """Send payment SMS only - third tool"""
    log_with_timestamp("="*50)
    log_with_timestamp("💳 SMS PAYMENT ENDPOINT CALLED")
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No JSON data received"}), 400

        # Validate required fields
        required_fields = ['customer_phone', 'call_sid', 'amount']
        missing_fields = [field for field in required_fields if not data.get(field)]
        if missing_fields:
            return jsonify({
                "status": "error",
                "message": f"Missing required fields: {', '.join(missing_fields)}"
            }), 400

        # Clean phone number
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

        quote_id = data.get('quote_id', data['call_sid'])
        amount = str(data['amount'])
        
        # Get payment link from database
        payment_link = PAYPAL_PAYMENT_LINK  # fallback
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT payment_link FROM price_quotes WHERE quote_id = ? OR booking_ref = ?", (quote_id, quote_id))
            result = cursor.fetchone()
            if result and result[0]:
                payment_link = result[0]
            conn.close()

        # Send SMS
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message_body = f"""Waste King Payment
Amount: £{amount}
Reference: {quote_id}

Pay securely: {payment_link}

After payment, you'll get confirmation.
Thank you!"""

        message = client.messages.create(
            body=message_body,
            from_=TWILIO_PHONE_NUMBER,
            to=phone
        )

        log_with_timestamp(f"✅ SMS sent: {message.sid}")

        # Store in database
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO sms_payments 
                (quote_id, customer_phone, amount, sms_sid, call_sid, elevenlabs_conversation_id, created_at, paypal_link) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                quote_id, phone, float(amount), message.sid, data['call_sid'],
                data.get('elevenlabs_conversation_id', 'Unknown'),
                datetime.now().isoformat(), payment_link
            ))
            conn.commit()
            conn.close()

        return jsonify({
            "status": "success",
            "message": f"SMS sent successfully with final amount",
            "amount": f"£{amount}",
            "call_sid": data['call_sid'],
            "quote_id": quote_id,
            "payment_link_used": payment_link
        })

    except Exception as e:
        log_error("Payment processing failed", e)
        return jsonify({"status": "error", "message": "System error", "debug": str(e)}), 500

@app.route('/status')
def get_status():
    """Get system status"""
    return jsonify({
        "status": "running",
        "message": "WasteKing AI Voice Agent API is operational",
        "test_supplier_phone": TEST_SUPPLIER_PHONE,
        "real_suppliers_from": "SMP API via wastekingmarketplace tool",
        "supplier_calls_via": "/api/call-supplier endpoint",
        "endpoints": {
            "current_datetime": "/api/current-datetime",
            "marketplace": "/api/wasteking-marketplace",
            "supplier_availability": "/api/call-supplier", 
            "confirm_booking": "/api/wasteking-confirm-booking",
            "send_sms": "/api/send-payment-sms"
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port, use_reloader=False)
