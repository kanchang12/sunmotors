import os
import json
import sqlite3
import threading
import time
import random
import re
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
import requests

# --- Configuration ---
WASTEKING_BASE_URL = "https://wk-smp-api-dev.azurewebsites.net/"
WASTEKING_ACCESS_TOKEN = "wk-KZPY-tGF-@d.Aby9fpvMC_VVWkX-GN.i7jCBhF3xceoFfhmawaNc.RH.G_-kwk8*"

# Twilio Configuration
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', 'your_twilio_sid')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', 'your_twilio_token')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER', 'your_twilio_phone_number')

# Test supplier phone
TEST_SUPPLIER_PHONE = "+447823656907"

# PayPal fallback
PAYPAL_PAYMENT_LINK = "https://www.paypal.com/ncp/payment/BQ82GUU9VSKYN"

# Database
DATABASE_FILE = 'calls.db'
db_lock = threading.Lock()

# --- Import Twilio only if credentials are set ---
twilio_available = False
try:
    if TWILIO_ACCOUNT_SID != 'your_twilio_sid' and TWILIO_AUTH_TOKEN != 'your_twilio_token':
        from twilio.rest import Client
        twilio_available = True
except ImportError:
    print("Twilio not available - SMS features disabled")

def log_with_timestamp(message, level="INFO"):
    """Enhanced logging with timestamps"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {message}")

def log_error(message, error=None):
    """Log errors"""
    if error:
        log_with_timestamp(f"ERROR: {message}: {error}", "ERROR")
    else:
        log_with_timestamp(f"ERROR: {message}", "ERROR")

def get_current_datetime_info():
    """Get current UK date/time information"""
    now_utc = datetime.now()
    return {
        "current_date": now_utc.strftime("%Y-%m-%d"),
        "current_time": now_utc.strftime("%H:%M"),
        "current_day": now_utc.strftime("%A"),
        "tomorrow_date": (now_utc + timedelta(days=1)).strftime("%Y-%m-%d"),
        "system_timezone": "UTC"
    }

def get_db_connection():
    """Get database connection"""
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize database"""
    log_with_timestamp("Initializing database...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Simple quotes table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_quotes (
                quote_id TEXT PRIMARY KEY,
                booking_ref TEXT,
                postcode TEXT,
                service TEXT,
                price_data TEXT,
                created_at TEXT,
                status TEXT DEFAULT 'pending'
            )
        ''')
        
        conn.commit()
        conn.close()
        log_with_timestamp("Database initialized successfully")
    except Exception as e:
        log_error("Failed to initialize database", e)

def create_wasteking_booking():
    """Create booking reference"""
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
            response_json = response.json()
            booking_ref = response_json.get('bookingRef')
            if booking_ref:
                log_with_timestamp(f"✅ Created booking: {booking_ref}")
                time.sleep(1)
                return booking_ref
        
        log_with_timestamp(f"❌ Failed to create booking: {response.status_code}")
        return None
            
    except Exception as e:
        log_error("Failed to create booking", e)
        return None

def update_wasteking_booking(booking_ref, update_data):
    """Update booking"""
    try:
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
            log_with_timestamp(f"✅ Updated booking {booking_ref}")
            return response.json()
        else:
            log_with_timestamp(f"❌ Failed to update booking: {response.status_code}")
            return None
            
    except Exception as e:
        log_error(f"Failed to update booking {booking_ref}", e)
        return None

def send_payment_sms(booking_ref, phone, payment_link, amount):
    """Send SMS if Twilio available"""
    try:
        if not twilio_available:
            log_with_timestamp("⚠️ Twilio not available - SMS disabled")
            return {"success": False, "message": "SMS service not available"}
        
        # Clean phone number
        if phone.startswith('0'):
            phone = f"+44{phone[1:]}"
        elif not phone.startswith('+'):
            phone = f"+44{phone}"
            
        if not re.match(r'^\+44\d{9,10}$', phone):
            return {"success": False, "message": "Invalid UK phone number"}
        
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        message_body = f"""Waste King Payment
Amount: {amount}
Reference: {booking_ref}

Pay securely: {payment_link}

Thank you!"""
        
        message = client.messages.create(
            body=message_body,
            from_=TWILIO_PHONE_NUMBER,
            to=phone
        )
        
        log_with_timestamp(f"✅ SMS sent: {message.sid}")
        return {"success": True, "message": "SMS sent", "sms_sid": message.sid}
        
    except Exception as e:
        log_error("Failed to send SMS", e)
        return {"success": False, "message": str(e)}

# --- Initialize Flask App ---
app = Flask(__name__)

# Initialize database on startup
init_db()

@app.after_request
def after_request(response):
    """Add CORS headers"""
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

@app.route('/')
def index():
    """Main endpoint"""
    return jsonify({
        "message": "WasteKing AI Voice Agent API",
        "status": "running",
        "twilio_available": twilio_available,
        "endpoints": [
            "/api/current-datetime",
            "/api/wasteking-get-price", 
            "/api/call-supplier",
            "/api/wasteking-confirm-booking",
            "/api/send-payment-sms"
        ]
    })

@app.route('/api/current-datetime', methods=['GET'])
def get_current_datetime():
    """Get current date/time"""
    return jsonify(get_current_datetime_info())

@app.route('/api/wasteking-get-price', methods=['POST'])
def wasteking_marketplace():
    """Get pricing and supplier info"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "No data provided"}), 400

        # Required fields
        required = ['postcode', 'service', 'type']
        missing = [field for field in required if not data.get(field)]
        if missing:
            return jsonify({
                "success": False,
                "message": f"Missing required fields: {', '.join(missing)}"
            }), 400

        # Create booking
        booking_ref = create_wasteking_booking()
        if not booking_ref:
            return jsonify({"success": False, "message": "Failed to create booking"}), 500

        # Search payload
        search_payload = {
            "search": {
                "postCode": data['postcode'],
                "service": data['service'],
                "type": data['type']
            }
        }
        
        # Get pricing
        response_data = update_wasteking_booking(booking_ref, search_payload)
        if not response_data:
            return jsonify({"success": False, "message": "No pricing data"}), 404

        quote_data = response_data.get('quote', {})
        price = quote_data.get('price', '0')
        supplier_phone = quote_data.get('supplierPhone', "+447700900000")
        supplier_name = quote_data.get('supplierName', "Default Supplier")
        
        datetime_info = get_current_datetime_info()
        
        return jsonify({
            "success": True,
            "booking_ref": booking_ref,
            "price": price,
            "real_supplier_phone": supplier_phone,
            "supplier_name": supplier_name,
            "postcode": data['postcode'],
            "service": data['service'],
            "type": data['type'],
            "ai_context": datetime_info
        })
        
    except Exception as e:
        log_error("Marketplace request failed", e)
        return jsonify({
            "success": False,
            "message": "Marketplace request failed",
            "error": str(e)
        }), 500

@app.route('/api/call-supplier', methods=['POST'])
def call_supplier():
    """Mock supplier call"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "No data provided"}), 400

        service = data.get('service')
        postcode = data.get('postcode')
        requested_date = data.get('requested_date')
        
        if not all([service, postcode, requested_date]):
            return jsonify({
                "success": False,
                "message": "Missing required fields"
            }), 400

        # Mock response
        availability_responses = [
            {"available": True, "message": f"Available for {service} on {requested_date}"},
            {"available": False, "message": f"Sorry, {requested_date} is fully booked"}
        ]
        
        response = random.choice(availability_responses)
        datetime_info = get_current_datetime_info()
        
        return jsonify({
            "success": True,
            "test_number_called": TEST_SUPPLIER_PHONE,
            "service": service,
            "postcode": postcode,
            "requested_date": requested_date,
            "available": response["available"],
            "supplier_response": response["message"],
            "ai_context": datetime_info
        })
        
    except Exception as e:
        log_error("Supplier call failed", e)
        return jsonify({
            "success": False,
            "message": "Supplier call failed",
            "error": str(e)
        }), 500

@app.route('/api/wasteking-confirm-booking', methods=['POST'])
def confirm_wasteking_booking():
    """Confirm booking and send SMS"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "No data provided"}), 400

        # Get or create booking ref
        booking_ref = data.get('booking_ref')
        if not booking_ref:
            booking_ref = create_wasteking_booking()
            if not booking_ref:
                return jsonify({"success": False, "message": "Failed to create booking"}), 500

        customer_phone = data.get('customer_phone') or data.get('phone')
        if not customer_phone:
            return jsonify({"success": False, "message": "Phone number required"}), 400

        # Generate payment link
        payment_payload = {"action": "quote"}
        payment_response = update_wasteking_booking(booking_ref, payment_payload)
        
        if payment_response and payment_response.get('quote', {}).get('paymentLink'):
            payment_link = payment_response['quote']['paymentLink']
            base_price = float(payment_response['quote'].get('price', '0'))
        else:
            payment_link = PAYPAL_PAYMENT_LINK
            base_price = 50.0  # Default price

        # Calculate final price (simplified)
        final_price = base_price

        # Send SMS
        sms_response = send_payment_sms(booking_ref, customer_phone, payment_link, str(final_price))
        
        datetime_info = get_current_datetime_info()

        return jsonify({
            "success": True,
            "message": "Booking confirmed",
            "booking_ref": booking_ref,
            "payment_link": payment_link,
            "final_price": final_price,
            "customer_phone": customer_phone,
            "sms_sent": sms_response.get('success', False),
            "ai_context": datetime_info
        })

    except Exception as e:
        log_error("Booking confirmation failed", e)
        return jsonify({
            "success": False,
            "message": "Booking failed",
            "error": str(e)
        }), 500

@app.route('/api/send-payment-sms', methods=['POST'])
def send_payment_sms_endpoint():
    """Send payment SMS"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No data"}), 400

        required_fields = ['customer_phone', 'amount']
        missing = [f for f in required_fields if not data.get(f)]
        if missing:
            return jsonify({
                "status": "error",
                "message": f"Missing: {', '.join(missing)}"
            }), 400

        phone = data['customer_phone']
        amount = str(data['amount'])
        quote_id = data.get('quote_id', f"WK{random.randint(100000, 999999)}")
        
        sms_response = send_payment_sms(quote_id, phone, PAYPAL_PAYMENT_LINK, amount)

        return jsonify({
            "status": "success" if sms_response.get('success') else "error",
            "message": sms_response.get('message'),
            "amount": f"£{amount}",
            "quote_id": quote_id
        })

    except Exception as e:
        log_error("SMS failed", e)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/status')
def get_status():
    """Status endpoint"""
    return jsonify({
        "status": "running",
        "message": "WasteKing API operational",
        "twilio_available": twilio_available,
        "test_supplier_phone": TEST_SUPPLIER_PHONE
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
