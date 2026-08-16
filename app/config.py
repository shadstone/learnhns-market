import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-prod')
    APP_ENV = os.getenv('APP_ENV', 'production')
    ALLOWED_PROOF_NETWORKS = [
        network.strip()
        for network in os.getenv('ALLOWED_PROOF_NETWORKS', 'main').split(',')
        if network.strip()
    ]
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', 'sqlite:///dev.db')
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith('postgres://'):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace('postgres://', 'postgresql://', 1)
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
    
    # Pinata
    PINATA_API_KEY = os.getenv('PINATA_API_KEY')
    PINATA_SECRET_KEY = os.getenv('PINATA_SECRET_KEY')
    
    # GFAVIP Webhook
    GFAVIP_WEBHOOK_URL = os.getenv('GFAVIP_WEBHOOK_URL')

    # Marketplace fee policy.
    # Rate is expressed in basis points: 250 = 2.5%, 100 = 1%.
    MARKETPLACE_FEE_RATE = int(os.getenv('MARKETPLACE_FEE_RATE', '0'))
    MARKETPLACE_FEE_ADDRESS = os.getenv('MARKETPLACE_FEE_ADDRESS')

    # Optional full/indexed hsd node used to serve chain data to SPV clients.
    # Example raw hsd: HSD_HTTP_URL=http://127.0.0.1:12037 HSD_API_KEY=hunter2
    # Example Fire HSD: HSD_HTTP_URL=https://hsd.hns.au HSD_API_STYLE=firehsd
    HSD_HTTP_URL = os.getenv('HSD_HTTP_URL')
    HSD_API_KEY = os.getenv('HSD_API_KEY')
    HSD_API_STYLE = os.getenv('HSD_API_STYLE', 'raw')
    HSD_HTTP_TIMEOUT = float(os.getenv('HSD_HTTP_TIMEOUT', '5'))
    TX_EXPLORER_BASE_URL = os.getenv('TX_EXPLORER_BASE_URL', 'https://shakeshift.com/transaction').rstrip('/')
    MARKET_INDEXER_STALE_SECONDS = int(os.getenv('MARKET_INDEXER_STALE_SECONDS', '300'))
    MARKET_INDEXER_MAX_LAG_BLOCKS = int(os.getenv('MARKET_INDEXER_MAX_LAG_BLOCKS', '6'))
    STATIC_ASSET_VERSION = os.getenv('STATIC_ASSET_VERSION', '20260816')
    EXPIRING_WATCH_REFRESH_MINUTES = int(os.getenv('EXPIRING_WATCH_REFRESH_MINUTES', '60'))
    MARKET_ADMIN_TOKEN = os.getenv('MARKET_ADMIN_TOKEN')

    # Optional GFAVIP SSO account layer.
    GFAVIP_SSO_BASE_URL = os.getenv('GFAVIP_SSO_BASE_URL', 'https://wallet.gfavip.com').rstrip('/')
    GFAVIP_SSO_SERVICE = os.getenv('GFAVIP_SSO_SERVICE', 'learnhns-market')
    GFAVIP_SSO_CALLBACK_URL = os.getenv('GFAVIP_SSO_CALLBACK_URL')
    GFAVIP_SSO_VALIDATE_URL = os.getenv(
        'GFAVIP_SSO_VALIDATE_URL',
        f'{GFAVIP_SSO_BASE_URL}/api/auth/validate',
    )
    GFAVIP_SSO_TIMEOUT = float(os.getenv('GFAVIP_SSO_TIMEOUT', '8'))
    ACCOUNT_SESSION_DAYS = int(os.getenv('ACCOUNT_SESSION_DAYS', '30'))
    ACCOUNT_COOKIE_SECURE = os.getenv('ACCOUNT_COOKIE_SECURE', '').lower() in {'1', 'true', 'yes'}
    FREE_WATCHLIST_LIMIT = int(os.getenv('FREE_WATCHLIST_LIMIT', '10'))
    PRO_WATCHLIST_LIMIT = int(os.getenv('PRO_WATCHLIST_LIMIT', '100'))
    SUPPORT_WALL_ADMIN_GFAVIP_IDS = {
        item.strip()
        for item in os.getenv('SUPPORT_WALL_ADMIN_GFAVIP_IDS', '').split(',')
        if item.strip()
    }
    SUPPORT_WALL_ADMIN_EMAILS = {
        item.strip().lower()
        for item in os.getenv('SUPPORT_WALL_ADMIN_EMAILS', '').split(',')
        if item.strip()
    }
    SUPPORT_WALL_ADMIN_USERNAMES = {
        item.strip().lower()
        for item in os.getenv('SUPPORT_WALL_ADMIN_USERNAMES', '').split(',')
        if item.strip()
    }
    SUPPORT_WALL_API_CACHE_SECONDS = int(os.getenv('SUPPORT_WALL_API_CACHE_SECONDS', '60'))

    # Mailgun renewal alerts.
    MAILGUN_API_KEY = os.getenv('MAILGUN_API_KEY')
    MAILGUN_DOMAIN = os.getenv('MAILGUN_DOMAIN')
    MAILGUN_FROM = os.getenv('MAILGUN_FROM', 'LearnHNS Alerts <alerts@learnhns.com>')
    MAILGUN_REGION = os.getenv('MAILGUN_REGION', 'us')
    ALERTS_BASE_URL = os.getenv('ALERTS_BASE_URL', 'https://market.learnhns.com').rstrip('/')
    
    # Domain configuration
    # SERVER_NAME = os.getenv('SERVER_NAME', 'market.learnhns.com')
