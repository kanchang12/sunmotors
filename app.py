import os
import sqlite3

# Delete database file
DATABASE_FILE = 'calls.db'

try:
    if os.path.exists(DATABASE_FILE):
        os.remove(DATABASE_FILE)
        print("✅ Database file deleted")
    else:
        print("⚠️ Database file not found")
        
    # Also clear any session files
    if os.path.exists("wasteking_session.pkl"):
        os.remove("wasteking_session.pkl")
        print("✅ Session file deleted")
        
    print("🗑️ ALL DATA DELETED SUCCESSFULLY")
    
except Exception as e:
    print(f"❌ Error: {e}")
