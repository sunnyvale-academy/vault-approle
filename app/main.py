import os
import sys
import time
import datetime
import hvac
import psycopg2
import threading
import logging
from flask import Flask, jsonify
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables. ROLE_ID and SECRET_ID should be set in the environment.
load_dotenv() 

app = Flask(__name__)

VAULT_ADDR = os.getenv("VAULT_ADDR", "http://localhost:8200")
VAULT_TOKEN = None # Will be read from stdin

class VaultManager:
    def __init__(self):
        self.vault_client = None
        self.db_creds = None
        self.db_creds_expiry = None

    def login(self):
        """Authenticates with Vault using the provided token (supports wrapping)."""
        global VAULT_TOKEN
        if not VAULT_TOKEN:
            logger.error("VAULT_TOKEN not found.")
            return None
        
        logger.info("Initializing Vault client...")
        client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)
        
        # Check if the token needs unwrapping
        try:
            logger.info("Attempting to unwrap Vault token...")
            # If token is a wrapping token, this will return the unwrapped content
            unwrap_response = client.sys.unwrap()
            VAULT_TOKEN = unwrap_response['auth']['client_token']
            client.token = VAULT_TOKEN
            logger.info("Token unwrapped successfully.")
        except Exception as e:
            # If unwrapping fails, we assume the token is already a client token
            logger.info(f"Token unwrap not performed or failed: {e}")
        
        client.token = VAULT_TOKEN
        if not client.is_authenticated():
            logger.error("Provided VAULT_TOKEN is invalid or expired.")
            return None
            
        self.vault_client = client
        self.start_token_renewal()
        return client

    def start_token_renewal(self):
        """Starts a background thread to renew the Vault token periodically."""
        def renew_worker():
            while True:
                # Renew every 30 minutes (periodic token is 1h)
                time.sleep(1800)
                try:
                    logger.info("Renewing Vault token...")
                    self.vault_client.auth.token.renew_self()
                except Exception as e:
                    logger.error(f"Failed to renew Vault token: {e}")

        renewal_thread = threading.Thread(target=renew_worker, daemon=True)
        renewal_thread.start()
        logger.info("Background token renewal thread started.")

    def get_db_credentials(self, force_refresh=False):
        """Fetches dynamic DB credentials, with caching and expiry check."""
        now = datetime.datetime.now()

        # Return cached credentials if they are still valid
        if not force_refresh and self.db_creds and self.db_creds_expiry > now:
            logger.info("Using cached database credentials.")
            return self.db_creds

        # Otherwise, fetch fresh credentials
        if not self.vault_client:
            self.login()

        try:
            logger.info("Fetching fresh database credentials from Vault...")
            read_creds_response = self.vault_client.secrets.database.generate_credentials(
                name='my-db-role',
                mount_point='database'
            )
            
            self.db_creds = read_creds_response['data']
            lease_duration = read_creds_response['lease_duration']
            # Set expiry to now + duration (minus a small buffer of 30 seconds)
            self.db_creds_expiry = now + datetime.timedelta(seconds=lease_duration - 30)
            
            logger.info(f"New credentials fetched. Expiry at: {self.db_creds_expiry}")
            return self.db_creds
        except Exception as e:
            # If Vault request fails, it might be due to an expired token (though renewal should prevent this)
            logger.error(f"Vault error generating credentials: {e}")
            return None

vault_mgr = VaultManager()

@app.route('/')
def index():
    return jsonify({
        "message": "Welcome to the Secure Web App with Credential Caching!",
        "auth_method": "Vault AppRole"
    })

@app.route('/data')
def get_data():
    try:
        # 1. Get dynamic DB credentials (handles login/cache internally)
        db_creds = vault_mgr.get_db_credentials()
        
        if not db_creds:
            logger.error("Could not obtain database credentials from Vault.")
            return jsonify({
                "status": "error",
                "message": "Service Unavailable: Failed to fetch database credentials from Vault."
            }), 503

        # 2. Connect to PostgreSQL
        conn = psycopg2.connect(
            dbname="myappdb",
            user=db_creds['username'],
            password=db_creds['password'],
            host="localhost", # When running locally
            port="5432"
        )
        
        cur = conn.cursor()
        query = "SELECT current_user, current_database(), now();"
        logger.info(f"Executing DB query: {query}")
        cur.execute(query)
        db_info = cur.fetchone()
        
        cur.close()
        conn.close()
        
        return jsonify({
            "status": "success",
            "cached_creds_used": vault_mgr.db_creds_expiry > datetime.datetime.now(),
            "db_user": db_creds['username'],
            "expiry": str(vault_mgr.db_creds_expiry),
            "db_info": {
                "user": db_info[0],
                "database": db_info[1],
                "server_time": str(db_info[2])
            }
        })
    except psycopg2.OperationalError as e:
        # If DB connection fails (e.g. creds expired unexpectedly), try once more with fresh creds
        logger.warning(f"DB Connection error: {e}. Retrying with fresh credentials...")
        db_creds = vault_mgr.get_db_credentials(force_refresh=True)
        # (Recursive call or repeat logic - for simplicity in demo we just return error if second attempt fails)
        # In a real app, you'd implement a more robust retry decorator.
        return jsonify({"status": "retry_suggested", "message": "Credentials might have expired. Refreshing..."}), 503
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

if __name__ == '__main__':
    # Read VAULT_TOKEN from stdin
    logger.info("Waiting for VAULT_TOKEN on stdin...")
    try:
        # Read one line from stdin
        VAULT_TOKEN = sys.stdin.readline().strip()
    except Exception as e:
        logger.error(f"Error reading from stdin: {e}")
        exit(1)

    if not VAULT_TOKEN:
        logger.error("Error: VAULT_TOKEN not provided on stdin.")
        exit(1)
    
    # Update the global vault_mgr with the token (triggering login/renewal)
    vault_mgr.login()
    
    app.run(debug=False, port=5001)
