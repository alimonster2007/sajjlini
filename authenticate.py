import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/drive'
]

def main():
    if not os.path.exists('credentials.json'):
        print("❌ Error: credentials.json is missing in the project folder!")
        return

    print("🔑 Starting authentication flow...")
    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
    creds = flow.run_local_server(port=0)

    with open('token.json', 'w') as token:
        token.write(creds.to_json())

    print("✅ Success! 'token.json' has been created successfully.")

if __name__ == '__main__':
    main()