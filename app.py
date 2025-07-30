import os
import requests
import json
from flask import Flask, request, jsonify
from twilio.twiml.voice_response import VoiceResponse, Dial
from datetime import datetime, timedelta

# --- Flask App Initialization ---
app = Flask(__name__)

# --- Configuration (Load from Environment Variables) ---
# It is CRUCIAL to set these environment variables in your deployment environment (e.g., Koyeb).
# For local development, you can use a .env file and `python-dotenv`.
# Example environment variables (set these where you deploy):
# XELION_BASE_URL=https://lvsl01.xelion.com/api/v1/wasteking
# XELION_USER_ID=abi.housego@wasteking.co.uk
# XELION_PASSWORD=Passw0rd#
# XELION_API_KEY=NtYFnwKdrqbuXAd4N88txxnim2Nd6LnE
# TWILIO_ACCOUNT_SID=AC...
# TWILIO_AUTH_TOKEN=your_twilio_auth_token
# TWILIO_PHONE_NUMBER=+447... # Your Twilio phone number
# FINAL_DESTINATION_NUMBER=+447... # The number you want the call to ultimately reach

XELION_BASE_URL = os.environ.get("XELION_BASE_URL")
XELION_USER_ID = os.environ.get("XELION_USER_ID")
XELION_PASSWORD = os.environ.get("XELION_PASSWORD")
XELION_API_KEY = os.environ.get("XELION_API_KEY")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
FINAL_DESTINATION_NUMBER = os.environ.get("FINAL_DESTINATION_NUMBER")

# --- Global variables for Xelion token and OIDs (Simple State Management) ---
# These will hold the values for subsequent API calls within the same Flask process.
# In a multi-worker production setup (like Gunicorn), these would need to be
# stored in a shared, persistent store (e.g., Redis, database) if a single
# worker doesn't handle all requests from a single user. For a single-user
# scenario or for demonstration, global variables are acceptable.
xelion_auth_token = None
xelion_token_expiry = None # Stores when the token expires
current_device_oid = None
current_active_call_oid = None

# --- API Endpoints to Perform Actions ---

@app.route('/api/xelion/login', methods=['POST'])
def xelion_login():
    """
    Logs into Xelion and retrieves an authentication token.
    Stores the token and its expiry globally.
    """
    global xelion_auth_token, xelion_token_expiry

    if not all([XELION_BASE_URL, XELION_USER_ID, XELION_PASSWORD, XELION_API_KEY]):
        return jsonify({"error": "Server configuration error: Xelion credentials missing"}), 500

    login_url = f"{XELION_BASE_URL}/me/login"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"xelion {XELION_API_KEY}"
    }
    payload = {
        "userName": XELION_USER_ID,
        "password": XELION_PASSWORD,
    }

    try:
        response = requests.post(login_url, headers=headers, json=payload)
        response.raise_for_status() # Raises HTTPError for bad responses (4xx or 5xx)
        data = response.json()
        
        token = data.get("authentication")
        valid_until_str = data.get("validUntil")

        if token and valid_until_str:
            # Parse validUntil string to datetime object
            # Example format: "2025-07-30T21:54:44.000Z"
            xelion_token_expiry = datetime.fromisoformat(valid_until_str.replace('Z', '+00:00'))
            xelion_auth_token = f"xelion {token}" # Store with "xelion " prefix
            app.logger.info(f"Xelion login successful. Token expires: {xelion_token_expiry}")
            return jsonify({"message": "Xelion login successful", "token": token, "valid_until": valid_until_str})
        else:
            app.logger.error("Xelion login successful but token/expiry missing in response.")
            return jsonify({"error": "Authentication successful but token or expiry data missing from Xelion"}), 500
    except requests.exceptions.RequestException as e:
        error_details = str(e)
        if e.response is not None:
            error_details = e.response.text # Get detailed error from Xelion if available
        app.logger.error(f"Xelion login failed: {error_details}")
        return jsonify({"error": "Failed to login to Xelion", "details": error_details}), 500

@app.route('/api/xelion/get_call_info', methods=['GET'])
def get_xelion_call_info():
    """
    Retrieves the device OID for the configured user and the OID of the first active call
    on that device. Stores them globally.
    Requires an active Xelion token.
    """
    global current_device_oid, current_active_call_oid

    if not xelion_auth_token or (xelion_token_expiry and datetime.now(xelion_token_expiry.tzinfo) > xelion_token_expiry):
        app.logger.warning("Attempted to get Xelion info with missing/expired token.")
        return jsonify({"error": "Xelion token is missing or expired. Please login first."}), 401
    
    if not XELION_BASE_URL:
        return jsonify({"error": "Server configuration error: XELION_BASE_URL not set"}), 500

    headers = {
        "Content-Type": "application/json",
        "Authorization": xelion_auth_token
    }
    
    device_oid = None
    active_call_oid = None

    try:
        # 1. Get User Devices (using the /me/phones path as per your discovery)
        app.logger.info("Fetching user devices from Xelion...")
        devices_url = f"{XELION_BASE_URL}/me/phones"
        response = requests.get(devices_url, headers=headers)
        response.raise_for_status()
        devices_data = response.json()
        
        devices = devices_data.get("data", [])
        if devices:
            device_oid = devices[0].get("object", {}).get("oid")
            device_name = devices[0].get("object", {}).get("commonName", "Unknown Device")
            app.logger.info(f"Identified Xelion Device: {device_name}, OID: {device_oid}")
        else:
            app.logger.warning("No devices found for the authenticated Xelion user.")
            return jsonify({"error": "No Xelion devices found for the configured user."}), 404

        # 2. Get Active Calls for the selected device
        if device_oid:
            app.logger.info(f"Fetching active calls for device OID: {device_oid}...")
            calls_url = f"{XELION_BASE_URL}/phones/{device_oid}/calls" # Confirmed correct path
            response = requests.get(calls_url, headers=headers)
            response.raise_for_status()
            calls_data = response.json()
            
            active_calls = calls_data.get("data", [])
            if active_calls:
                # Assuming the first active call is the one we want to transfer
                active_call_oid = active_calls[0].get("object", {}).get("oid")
                remote_address = active_calls[0].get("remoteAddress", "N/A")
                app.logger.info(f"Identified Active Call OID: {active_call_oid}, Remote Address: {remote_address}")
            else:
                app.logger.info(f"No active calls found for device {device_oid}. Ensure a call is active.")
        
        current_device_oid = device_oid
        current_active_call_oid = active_call_oid

        return jsonify({
            "message": "Call info retrieved.",
            "device_oid": device_oid,
            "active_call_oid": active_call_oid
        })

    except requests.exceptions.RequestException as e:
        error_details = str(e)
        if e.response is not None:
            error_details = e.response.text
        app.logger.error(f"Failed to retrieve Xelion info: {error_details}")
        return jsonify({"error": "Failed to retrieve Xelion info", "details": error_details}), 500

@app.route('/api/xelion/transfer_call', methods=['POST'])
def transfer_xelion_call():
    """
    Initiates a call transfer on Xelion.
    Requires an active Xelion token, and previously obtained device_oid and active_call_oid.
    """
    if not xelion_auth_token or (xelion_token_expiry and datetime.now(xelion_token_expiry.tzinfo) > xelion_token_expiry):
        app.logger.warning("Attempted call transfer with missing/expired Xelion token.")
        return jsonify({"error": "Xelion token is missing or expired. Please login first."}), 401

    if not all([XELION_BASE_URL, TWILIO_PHONE_NUMBER, FINAL_DESTINATION_NUMBER]):
        return jsonify({"error": "Server configuration error: Twilio or Xelion numbers missing"}), 500
    
    # Use globally stored OIDs, or get them from request if passed.
    # For this minimal setup, we'll rely on global state from previous calls.
    device_oid = current_device_oid
    active_call_oid = current_active_call_oid

    if not device_oid or not active_call_oid:
        app.logger.error("Missing device_oid or active_call_oid for transfer. Run /api/xelion/get_call_info first.")
        return jsonify({"error": "Device OID or Active Call OID not set. Please run /api/xelion/get_call_info first."}), 400

    headers = {
        "Content-Type": "application/json",
        "Authorization": xelion_auth_token
    }

    try:
        # Step 1: Initiate a new outbound call from Xelion to your Twilio number
        app.logger.info(f"Xelion: Initiating new outbound call from device {device_oid} to Twilio number {TWILIO_PHONE_NUMBER}")
        start_call_url = f"{XELION_BASE_URL}/phones/{device_oid}/call"
        start_call_payload = {
            "address": TWILIO_PHONE_NUMBER,
            "callType": "outbound"
        }
        start_call_response = requests.post(start_call_url, headers=headers, json=start_call_payload)
        start_call_response.raise_for_status()
        new_call_data = start_call_response.json()
        new_call_oid = new_call_data.get("object", {}).get("oid")
        
        if not new_call_oid:
            app.logger.error("Xelion did not return a new call OID after initiating call to Twilio.")
            return jsonify({"error": "Xelion did not return a new call OID after initiation"}), 500
        
        app.logger.info(f"Xelion: New call to Twilio initiated successfully. New Call OID: {new_call_oid}")

        # Step 2: Transfer the original call with the newly initiated call
        app.logger.info(f"Xelion: Attempting to transfer original call {active_call_oid} with new call {new_call_oid}")
        transfer_url = f"{XELION_BASE_URL}/phones/{device_oid}/transferSelectedCalls"
        transfer_payload = {
            "callOids": [active_call_oid, new_call_oid]
        }
        transfer_response = requests.post(transfer_url, headers=headers, json=transfer_payload)
        transfer_response.raise_for_status()

        app.logger.info("Xelion: Call transfer process successfully initiated.")
        return jsonify({"message": "Call transfer process initiated successfully."})

    except requests.exceptions.RequestException as e:
        error_details = str(e)
        if e.response is not None:
            error_details = e.response.text
        app.logger.error(f"Xelion call transfer failed: {error_details}")
        return jsonify({"error": "Failed to transfer call via Xelion", "details": error_details}), 500

# --- Twilio Webhook Endpoint ---
@app.route('/voice', methods=['POST'])
def twilio_voice():
    """
    This endpoint is called by Twilio when your Twilio number receives a call.
    It generates TwiML to forward the call to the FINAL_DESTINATION_NUMBER.
    """
    call_sid = request.form.get('CallSid', 'N/A')
    from_number = request.form.get('From', 'N/A')
    to_number = request.form.get('To', 'N/A')

    app.logger.info(f"Twilio Webhook Received: Call SID={call_sid}, From={from_number}, To={to_number}")

    if not FINAL_DESTINATION_NUMBER:
        app.logger.error("Server configuration error: FINAL_DESTINATION_NUMBER is not set for Twilio webhook.")
        response = VoiceResponse()
        response.say("Call transfer failed due to missing configuration.")
        return str(response), 200, {'Content-Type': 'text/xml'}

    response = VoiceResponse()
    response.dial(FINAL_DESTINATION_NUMBER)

    app.logger.info(f"Twilio Webhook: Generated TwiML to dial {FINAL_DESTINATION_NUMBER}")
    return str(response), 200, {'Content-Type': 'text/xml'}

# --- Home Route (basic instructions) ---
@app.route('/')
def home():
    return jsonify({
        "message": "Welcome to the Xelion Call Forwarder!",
        "instructions": [
            "1. Send a POST request to /api/xelion/login to get your Xelion token.",
            "2. Send a GET request to /api/xelion/get_call_info to get your device OID and active call OID (ensure a call is active on your Xelion device).",
            "3. Send a POST request to /api/xelion/transfer_call to initiate the transfer.",
            "4. Configure your Twilio number's voice webhook to POST to your_app_url/voice."
        ]
    })


# --- Run the Flask App ---
if __name__ == '__main__':
    # For local development, load environment variables from a .env file
    try:
        from dotenv import load_dotenv
        load_dotenv()
        app.logger.info("Loaded environment variables from .env file (if it exists).")
    except ImportError:
        app.logger.warning("python-dotenv not installed. Environment variables must be set manually for local development.")

    # Get port from environment, default to 5000
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True) # debug=True for local dev, False for production
