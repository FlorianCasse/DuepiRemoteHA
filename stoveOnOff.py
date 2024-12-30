#!/usr/bin/env python3

import sys
import requests

def control_stove(command):
    url = 'https://dpremoteiot.com/devices/setSettings'

    # Common headers (simplified)
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Origin': 'https://dpremoteiot.com',
        'Referer': 'https://dpremoteiot.com/dashboard',
        'User-Agent': 'Mozilla/5.0',
    }

    # Replace with your actual session cookie securely
    cookies = {
        'session': 'ToBeReplace'
    }

    # Data common to both commands
    data = {
        'deviceId': '66c5b3de5762492bc642e320',
        'emailNotifications': '0',
        'settedPower': '1',
        'settedTemperature': '23',
        # 'active': '1' or '0' will be set based on the command
    }

    # Set the 'active' parameter based on the command
    if command.lower() == 'on':
        data['active'] = '1'  # Starts the stove
    elif command.lower() == 'off':
        data['active'] = '0'  # Stops the stove
    else:
        print("Invalid command. Use 'On' or 'Off'.")
        sys.exit(1)

    try:
        response = requests.post(url, headers=headers, cookies=cookies, data=data)
        if response.status_code == 200:
            print(f"Stove turned {command}.")
        else:
            print(f"Failed to turn stove {command}. HTTP status code: {response.status_code}")
            print(f"Response content: {response.text}")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 control_stove.py <On/Off>")
        sys.exit(1)
    command = sys.argv[1]
    control_stove(command)