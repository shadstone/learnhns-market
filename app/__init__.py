from flask import Flask, render_template, request
from flask_migrate import Migrate
from flask_cors import CORS
from app.config import Config
from app.models import db
from app.blueprints.main import main_bp
from app.blueprints.api import api_bp
from app.blueprints.account import account_bp
from app.blueprints.support_wall import support_wall_bp
from app.auth import SESSION_COOKIE, current_account
import os

def display_name(name):
    decoded = decoded_name(name)
    if decoded == name:
        return name
    return f"{decoded} {name}"


def decoded_name(name):
    if not isinstance(name, str) or not name.startswith('xn--'):
        return name
    try:
        return name.encode('ascii').decode('idna')
    except UnicodeError:
        return name


def create_app(config_overrides=None):
    app = Flask(__name__, template_folder='templates', static_folder='static')
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)
    
    db.init_app(app)
    Migrate(app, db)
    CORS(app)
    
    # Blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(account_bp)
    app.register_blueprint(support_wall_bp)
    app.register_blueprint(api_bp, url_prefix='/api')

    @app.context_processor
    def inject_current_account():
        return {
            'current_account': current_account(),
            'display_name': display_name,
            'decoded_name': decoded_name,
            'static_asset_version': app.config['STATIC_ASSET_VERSION'],
        }

    @app.after_request
    def set_cache_policy(response):
        if request.method not in {'GET', 'HEAD'} or response.status_code != 200:
            return response

        if request.endpoint == 'static':
            if request.args.get('v'):
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            else:
                response.headers['Cache-Control'] = 'public, max-age=3600'
            return response

        public_routes = {
            'main.index',
            'main.pending',
            'main.sold',
            'main.stats',
            'api.auctions',
            'api.pending_listings',
            'api.sales',
        }
        if request.endpoint in public_routes and not request.cookies.get(SESSION_COOKIE):
            response.headers['Cache-Control'] = (
                'public, max-age=15, s-maxage=30, stale-while-revalidate=60'
            )
            response.headers.add('Vary', 'Cookie')
        elif request.cookies.get(SESSION_COOKIE):
            response.headers['Cache-Control'] = 'private, no-store'

        return response

    @app.errorhandler(404)
    def not_found(error):
        return render_template('404.html'), 404
    
    # Create upload folder
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    
    return app
