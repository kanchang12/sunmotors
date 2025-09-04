#!/usr/bin/env python3
"""
WasteKing Simple Pricing & Booking API
Simplified version that handles price check and booking creation
"""

import os
import requests
import json
import traceback
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify

# WasteKing API Configuration
WASTEKING_BASE_URL = "https://wk-smp-api-dev.azurewebsites.net/"
WASTEKING_ACCESS_TOKEN = "wk-KZPY-tGF-@d.Aby9fpvMC_VVWkX-GN.i7jCBhF3xceoFfhmawaNc.RH.G_-kwk8*"

app = Flask(__name__)

def log_with_timestamp(message, level="INFO"):
    """Enhanced logging with timestamps"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {message}")

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
            log_with_timestamp(f"❌ Failed to create booking. Status: {response.status_code}")
            return None
            
    except Exception as e:
        log_with_timestamp(f"❌ Failed to create WasteKing booking: {str(e)}")
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
            log_with_timestamp(f"✅ Updated booking {booking_ref}")
            return response.json()
        else:
            log_with_timestamp(f"❌ Failed to update booking {booking_ref}. Status: {response.status_code}")
            return None
            
    except Exception as e:
        log_with_timestamp(f"❌ Failed to update booking {booking_ref}: {str(e)}")
        return None

def get_current_datetime_info():
    """Get current UK date/time information for AI context"""
    now_utc = datetime.now(timezone.utc)
    
    return {
        "current_date": now_utc.strftime("%Y-%m-%d"),
        "current_time": now_utc.strftime("%H:%M"),
        "current_day": now_utc.strftime("%A"),
        "tomorrow_date": (now_utc + timedelta(days=1)).strftime("%Y-%m-%d"),
        "current_datetime_utc": now_utc.isoformat(),
        "ai_context": {
            "today_is": now_utc.strftime("%Y-%m-%d"),
            "current_time": now_utc.strftime("%H:%M"),
            "tomorrow_is": (now_utc + timedelta(days=1)).strftime("%Y-%m-%d"),
            "current_day": now_utc.strftime("%A")
        }
    }

@app.route('/api/get-price', methods=['POST'])
def get_price():
    """Get price quote - Step 1"""
    try:
        log_with_timestamp("=" * 50)
        log_with_timestamp("💰 PRICE CHECK STARTED")
        
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "message": "No data provided"
            }), 400

        # Validate required fields
        required = ['postcode', 'service', 'type']
        missing = [field for field in required if not data.get(field)]
        if missing:
            return jsonify({
                "success": False,
                "message": f"Missing required fields: {', '.join(missing)}"
            }), 400

        log_with_timestamp(f"📦 Price request: {data['postcode']}, {data['service']}, {data['type']}")

        # Create booking reference
        booking_ref = create_wasteking_booking()
        if not booking_ref:
            return jsonify({
                "success": False,
                "message": "Service unavailable"
            }), 503

        # Get pricing
        search_payload = {
            "search": {
                "postCode": data['postcode'],
                "service": data['service'],
                "type": data['type']
            }
        }
        
        price_data = update_wasteking_booking(booking_ref, search_payload)
        if not price_data or not price_data.get('quote'):
            return jsonify({
                "success": False,
                "message": "No pricing available for this location"
            }), 404

        price = price_data['quote'].get('price')
        datetime_info = get_current_datetime_info()

        log_with_timestamp(f"✅ Price found: £{price}")

        return jsonify({
            "success": True,
            "booking_ref": booking_ref,
            "price": price,
            "message": f"The price is £{price} including VAT",
            **datetime_info
        })

    except Exception as e:
        log_with_timestamp(f"❌ Price check error: {str(e)}")
        return jsonify({
            "success": False,
            "message": "Unable to get price",
            "error": str(e)
        }), 500

@app.route('/api/create-booking', methods=['POST'])
def create_booking():
    """Create booking with payment link - Step 2"""
    try:
        log_with_timestamp("=" * 50)
        log_with_timestamp("📝 BOOKING CREATION STARTED")
        
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "message": "No data provided"
            }), 400

        # Validate required fields
        required = ['booking_ref', 'customer_phone']
        missing = [field for field in required if not data.get(field)]
        if missing:
            return jsonify({
                "success": False,
                "message": f"Missing required fields: {', '.join(missing)}"
            }), 400

        booking_ref = data['booking_ref']
        log_with_timestamp(f"🔍 Processing booking: {booking_ref}")

        # Step 1: Add customer details (if provided)
        if data.get('first_name') and data.get('last_name'):
            customer_payload = {
                "customer": {
                    "firstName": data['first_name'],
                    "lastName": data['last_name'],
                    "phone": data['customer_phone'],
                    "emailAddress": data.get('email', ''),
                    "addressPostcode": data.get('postcode', '')
                }
            }
            log_with_timestamp("👤 Adding customer details...")
            customer_response = update_wasteking_booking(booking_ref, customer_payload)
            if not customer_response:
                return jsonify({
                    "success": False,
                    "message": "Failed to add customer details"
                }), 500

        # Step 2: Add service details (if provided)
        if data.get('service_date'):
            service_payload = {
                "service": {
                    "date": data['service_date'],
                    "time": data.get('service_time', 'am'),
                    "placement": data.get('placement', 'drive')
                }
            }
            log_with_timestamp("📅 Adding service details...")
            service_response = update_wasteking_booking(booking_ref, service_payload)
            if not service_response:
                return jsonify({
                    "success": False,
                    "message": "Failed to add service details"
                }), 500

        # Step 3: Generate payment link
        payment_payload = {
            "action": "quote",
            "postPaymentUrl": "https://wasteking.co.uk/thank-you/"
        }
        log_with_timestamp("💳 Generating payment link...")
        payment_response = update_wasteking_booking(booking_ref, payment_payload)
        if not payment_response or not payment_response.get('quote', {}).get('paymentLink'):
            return jsonify({
                "success": False,
                "message": "Failed to generate payment link"
            }), 500

        payment_link = payment_response['quote']['paymentLink']
        price = payment_response['quote'].get('price', '0')
        datetime_info = get_current_datetime_info()

        log_with_timestamp(f"✅ Booking created with payment link")

        return jsonify({
            "success": True,
            "message": "Booking created successfully",
            "booking_ref": booking_ref,
            "payment_link": payment_link,
            "price": price,
            "customer_phone": data['customer_phone'],
            **datetime_info
        })

    except Exception as e:
        log_with_timestamp(f"❌ Booking creation error: {str(e)}")
        return jsonify({
            "success": False,
            "message": "Failed to create booking",
            "error": str(e)
        }), 500

@app.route('/api/full-booking-flow', methods=['POST'])
def full_booking_flow():
    """Complete flow: Price check + Booking creation in one call"""
    try:
        log_with_timestamp("=" * 60)
        log_with_timestamp("🚀 FULL BOOKING FLOW STARTED")
        
        data = request.get_json()
        if not data:
            return jsonify({
                "success": False,
                "message": "No data provided"
            }), 400

        # Validate required fields for pricing
        pricing_required = ['postcode', 'service', 'type']
        missing = [field for field in pricing_required if not data.get(field)]
        if missing:
            return jsonify({
                "success": False,
                "message": f"Missing pricing fields: {', '.join(missing)}"
            }), 400

        # Validate required fields for booking
        booking_required = ['customer_phone']
        missing_booking = [field for field in booking_required if not data.get(field)]
        if missing_booking:
            return jsonify({
                "success": False,
                "message": f"Missing booking fields: {', '.join(missing_booking)}"
            }), 400

        log_with_timestamp(f"📦 Full flow request: {data['postcode']}, {data['service']}, {data['type']}")

        # STEP 1: Create booking reference
        booking_ref = create_wasteking_booking()
        if not booking_ref:
            return jsonify({
                "success": False,
                "message": "Service unavailable"
            }), 503

        # STEP 2: Get pricing
        search_payload = {
            "search": {
                "postCode": data['postcode'],
                "service": data['service'],
                "type": data['type']
            }
        }
        
        price_data = update_wasteking_booking(booking_ref, search_payload)
        if not price_data or not price_data.get('quote'):
            return jsonify({
                "success": False,
                "message": "No pricing available for this location"
            }), 404

        price = price_data['quote'].get('price')
        log_with_timestamp(f"💰 Price obtained: £{price}")

        # STEP 3: Add customer details (if provided)
        if data.get('first_name') and data.get('last_name'):
            customer_payload = {
                "customer": {
                    "firstName": data['first_name'],
                    "lastName": data['last_name'],
                    "phone": data['customer_phone'],
                    "emailAddress": data.get('email', ''),
                    "addressPostcode": data['postcode']
                }
            }
            log_with_timestamp("👤 Adding customer details...")
            update_wasteking_booking(booking_ref, customer_payload)

        # STEP 4: Add service details (if provided)
        if data.get('service_date'):
            service_payload = {
                "service": {
                    "date": data['service_date'],
                    "time": data.get('service_time', 'am'),
                    "placement": data.get('placement', 'drive')
                }
            }
            log_with_timestamp("📅 Adding service details...")
            update_wasteking_booking(booking_ref, service_payload)

        # STEP 5: Generate payment link
        payment_payload = {
            "action": "quote",
            "postPaymentUrl": "https://wasteking.co.uk/thank-you/"
        }
        log_with_timestamp("💳 Generating payment link...")
        payment_response = update_wasteking_booking(booking_ref, payment_payload)
        if not payment_response or not payment_response.get('quote', {}).get('paymentLink'):
            return jsonify({
                "success": False,
                "message": "Failed to generate payment link"
            }), 500

        payment_link = payment_response['quote']['paymentLink']
        final_price = payment_response['quote'].get('price', price)
        datetime_info = get_current_datetime_info()

        log_with_timestamp(f"✅ FULL FLOW COMPLETE - Booking: {booking_ref}, Price: £{final_price}")

        return jsonify({
            "success": True,
            "message": f"Booking complete! Price: £{final_price}",
            "booking_ref": booking_ref,
            "price": final_price,
            "payment_link": payment_link,
            "customer_phone": data['customer_phone'],
            "customer_name": f"{data.get('first_name', 'Customer')} {data.get('last_name', 'Unknown')}",
            **datetime_info
        })

    except Exception as e:
        log_with_timestamp(f"❌ Full booking flow error: {str(e)}")
        log_with_timestamp(traceback.format_exc())
        return jsonify({
            "success": False,
            "message": "System error during booking",
            "error": str(e)
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "endpoints": [
            "/api/get-price",
            "/api/create-booking", 
            "/api/full-booking-flow"
        ]
    })

if __name__ == '__main__':
    print("=" * 60)
    print("🚀 WasteKing Simple API Starting...")
    print("📋 Available endpoints:")
    print("   POST /api/get-price        - Get price only")
    print("   POST /api/create-booking   - Create booking with payment link")
    print("   POST /api/full-booking-flow - Complete price + booking flow")
    print("   GET  /health               - Health check")
    print("=" * 60)
    
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
