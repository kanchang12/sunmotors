import requests
import json
import time

def fetch_and_save_data():
    """Fetch vehicle data from the API and save it as a JSON file."""
    url = "https://api.sunmotors.co.uk/marketcheck/vehicles"
    response = requests.get(url)
    
    if response.status_code == 200:
        data = response.json()
        
        # Save the data to a JSON file in the current directory
        with open("vehicles_data.json", "w") as json_file:
            json.dump(data, json_file, indent=4)
        print("Data saved to vehicles_data.json")
    else:
        print(f"Failed to fetch data. Status code: {response.status_code}")

def refresh_data_every_60_minutes():
    """Refresh the vehicle data every 60 minutes."""
    while True:
        try:
            fetch_and_save_data()
            print("Waiting for 60 minutes before the next refresh...")
            time.sleep(60 * 60)  # Wait for 60 minutes (3600 seconds)
        except Exception as e:
            print(f"An error occurred: {str(e)}")
            time.sleep(60)  # Retry after 1 minute if there's an error

if __name__ == "__main__":
    refresh_data_every_60_minutes()
