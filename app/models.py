from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Listing(db.Model):
    __tablename__ = 'listings'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    price_hns = db.Column(db.Numeric(12, 6), nullable=False)
    description = db.Column(db.Text)
    
    seller_hns_address = db.Column(db.String(100), nullable=False)
    gfavip_user_id = db.Column(db.String(100), nullable=True)
    gfavip_username = db.Column(db.String(100), nullable=True)
    
    ipfs_cid = db.Column(db.String(100), nullable=False)
    proof_json = db.Column(db.JSON, nullable=False)
    
    status = db.Column(db.String(20), default='active', index=True)  # active, sold, cancelled, archived, flagged
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sold_at = db.Column(db.DateTime, nullable=True, index=True)
    sale_tx_hash = db.Column(db.String(64), nullable=True, index=True)
    transfer_start_tx_hash = db.Column(db.String(64), nullable=True, index=True)
    cancelled_at = db.Column(db.DateTime, nullable=True, index=True)
    cancel_tx_hash = db.Column(db.String(64), nullable=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    flagged_reason = db.Column(db.Text, nullable=True)
    featured_web = db.Column(db.Boolean, default=False, nullable=False, index=True)
    featured_rank = db.Column(db.Integer, default=0, nullable=False)
    featured_label = db.Column(db.String(40), nullable=True)
    featured_starts_at = db.Column(db.DateTime, nullable=True)
    featured_ends_at = db.Column(db.DateTime, nullable=True)
    featured_admin_note = db.Column(db.Text, nullable=True)

    def effective_expires_at(self):
        if self.expires_at:
            return self.expires_at

        proof_data = self.proof_json or {}
        bids = proof_data.get('data') or []
        lock_times = [
            bid.get('lockTime')
            for bid in bids
            if isinstance(bid, dict) and isinstance(bid.get('lockTime'), int)
        ]

        if not lock_times:
            return None

        return datetime.utcfromtimestamp(max(lock_times))

    def is_expired(self):
        expires_at = self.effective_expires_at()
        return bool(expires_at and expires_at < datetime.utcnow())

    def is_featured_on_web(self, now=None):
        now = now or datetime.utcnow()
        return bool(
            self.featured_web
            and (self.featured_starts_at is None or self.featured_starts_at <= now)
            and (self.featured_ends_at is None or self.featured_ends_at > now)
        )


class PendingListing(db.Model):
    __tablename__ = 'pending_listings'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    network = db.Column(db.String(20), default='main', nullable=False, index=True)
    transfer_tx_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    transfer_output_idx = db.Column(db.Integer, nullable=True)
    lock_script_addr = db.Column(db.String(100), nullable=True)
    listing_mode = db.Column(db.String(40), default='fixed-price', nullable=False)
    expected_price = db.Column(db.BigInteger, nullable=True)
    seller_note = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(40), default='pending-submitted', nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('name', 'network', name='uq_pending_listings_name_network'),
    )

    def is_expired(self):
        return self.expires_at and self.expires_at < datetime.utcnow()


class ExpiringNameWatch(db.Model):
    __tablename__ = 'expiring_name_watches'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    network = db.Column(db.String(20), default='main', nullable=False, index=True)
    source = db.Column(db.String(40), default='market-observed', nullable=False, index=True)
    state = db.Column(db.String(40), nullable=True, index=True)
    renewal_height = db.Column(db.Integer, nullable=True)
    expiration_height = db.Column(db.Integer, nullable=True, index=True)
    blocks_until_expire = db.Column(db.Integer, nullable=True, index=True)
    days_until_expire = db.Column(db.Numeric(12, 4), nullable=True)
    hours_until_expire = db.Column(db.Numeric(12, 4), nullable=True)
    expired = db.Column(db.Boolean, default=False, nullable=False, index=True)
    found = db.Column(db.Boolean, default=False, nullable=False, index=True)
    error = db.Column(db.Text, nullable=True)
    source_height = db.Column(db.Integer, nullable=True, index=True)
    last_checked_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('name', 'network', name='uq_expiring_name_watches_name_network'),
    )


class GlobalNameState(db.Model):
    __tablename__ = 'global_name_states'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    network = db.Column(db.String(20), default='main', nullable=False, index=True)
    state = db.Column(db.String(40), nullable=True, index=True)
    renewal_height = db.Column(db.Integer, nullable=True)
    expiration_height = db.Column(db.Integer, nullable=True, index=True)
    blocks_until_expire = db.Column(db.Integer, nullable=True, index=True)
    days_until_expire = db.Column(db.Numeric(12, 4), nullable=True)
    hours_until_expire = db.Column(db.Numeric(12, 4), nullable=True)
    expired = db.Column(db.Boolean, default=False, nullable=False, index=True)
    source_height = db.Column(db.Integer, nullable=True, index=True)
    last_checked_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('name', 'network', name='uq_global_name_states_name_network'),
    )


class NameIndexerProgress(db.Model):
    __tablename__ = 'name_indexer_progress'

    id = db.Column(db.Integer, primary_key=True)
    network = db.Column(db.String(20), default='main', nullable=False, unique=True, index=True)
    status = db.Column(db.String(40), default='not-started', nullable=False, index=True)
    last_indexed_height = db.Column(db.Integer, nullable=True, index=True)
    target_height = db.Column(db.Integer, nullable=True, index=True)
    names_indexed = db.Column(db.Integer, default=0, nullable=False)
    last_error = db.Column(db.Text, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MarketplaceCovenantEvent(db.Model):
    __tablename__ = 'marketplace_covenant_events'

    id = db.Column(db.Integer, primary_key=True)
    network = db.Column(db.String(20), default='main', nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    covenant_action = db.Column(db.String(24), nullable=False, index=True)
    tx_hash = db.Column(db.String(64), nullable=False, index=True)
    output_index = db.Column(db.Integer, nullable=False, default=0)
    block_height = db.Column(db.Integer, nullable=True, index=True)
    block_hash = db.Column(db.String(64), nullable=True, index=True)
    block_time = db.Column(db.DateTime, nullable=True, index=True)
    source = db.Column(db.String(40), default='hsd-block', nullable=False, index=True)
    raw_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('network', 'tx_hash', 'output_index', 'covenant_action', 'name', name='uq_market_covenant_event'),
    )


class MarketplaceIndexerProgress(db.Model):
    __tablename__ = 'marketplace_indexer_progress'

    id = db.Column(db.Integer, primary_key=True)
    network = db.Column(db.String(20), default='main', nullable=False, unique=True, index=True)
    status = db.Column(db.String(40), default='not-started', nullable=False, index=True)
    last_indexed_height = db.Column(db.Integer, nullable=True, index=True)
    target_height = db.Column(db.Integer, nullable=True, index=True)
    events_indexed = db.Column(db.Integer, default=0, nullable=False)
    last_error = db.Column(db.Text, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MarketplaceBlockCheckpoint(db.Model):
    __tablename__ = 'marketplace_block_checkpoints'

    id = db.Column(db.Integer, primary_key=True)
    network = db.Column(db.String(20), default='main', nullable=False, index=True)
    block_height = db.Column(db.Integer, nullable=False, index=True)
    block_hash = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            'network',
            'block_height',
            name='uq_market_block_checkpoint_network_height',
        ),
    )


class Account(db.Model):
    __tablename__ = 'accounts'

    id = db.Column(db.Integer, primary_key=True)
    gfavip_user_id = db.Column(db.String(100), nullable=False, unique=True, index=True)
    email = db.Column(db.String(255), nullable=True, index=True)
    username = db.Column(db.String(100), nullable=True, index=True)
    display_name = db.Column(db.String(255), nullable=True)
    gfavip_tier = db.Column(db.String(40), default='free', nullable=False, index=True)
    local_tier = db.Column(db.String(40), default='free', nullable=False, index=True)
    support_wall_role = db.Column(db.String(40), default='none', nullable=False, index=True)
    email_verified_at = db.Column(db.DateTime, nullable=True)
    last_login_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sessions = db.relationship('AccountSession', backref='account', lazy=True, cascade='all, delete-orphan')
    watchlist_items = db.relationship('AccountWatchlistItem', backref='account', lazy=True, cascade='all, delete-orphan')
    alert_preferences = db.relationship('AccountAlertPreference', backref='account', uselist=False, cascade='all, delete-orphan')
    support_wall_posts = db.relationship(
        'SupportWallPost',
        foreign_keys='SupportWallPost.account_id',
        backref='account',
        lazy=True,
    )


class AccountSession(db.Model):
    __tablename__ = 'account_sessions'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False, index=True)
    session_token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    revoked_at = db.Column(db.DateTime, nullable=True, index=True)


class AccountWatchlistItem(db.Model):
    __tablename__ = 'account_watchlist_items'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    network = db.Column(db.String(20), default='main', nullable=False, index=True)
    source = db.Column(db.String(40), default='manual', nullable=False, index=True)
    alerts_enabled = db.Column(db.Boolean, default=True, nullable=False, index=True)
    note = db.Column(db.Text, nullable=True)
    tags_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('account_id', 'network', 'name', name='uq_account_watchlist_account_network_name'),
    )


class AccountAlertPreference(db.Model):
    __tablename__ = 'account_alert_preferences'

    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), primary_key=True)
    email_enabled = db.Column(db.Boolean, default=True, nullable=False, index=True)
    digest_enabled = db.Column(db.Boolean, default=False, nullable=False)
    reminder_days_json = db.Column(db.JSON, default=lambda: [30, 14, 7, 1], nullable=False)
    timezone = db.Column(db.String(80), default='UTC', nullable=False)
    manage_token_hash = db.Column(db.String(64), nullable=True, unique=True, index=True)
    unsubscribed_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AccountAlertEvent(db.Model):
    __tablename__ = 'account_alert_events'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False, index=True)
    watchlist_item_id = db.Column(db.Integer, db.ForeignKey('account_watchlist_items.id'), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    network = db.Column(db.String(20), default='main', nullable=False, index=True)
    alert_type = db.Column(db.String(40), default='renewal-reminder', nullable=False, index=True)
    cadence_days = db.Column(db.Integer, nullable=False, index=True)
    expiration_height = db.Column(db.Integer, nullable=True, index=True)
    blocks_until_expire = db.Column(db.Integer, nullable=True)
    days_until_expire = db.Column(db.Numeric(12, 4), nullable=True)
    mailgun_message_id = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(40), nullable=False, index=True)
    error = db.Column(db.Text, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            'account_id',
            'watchlist_item_id',
            'alert_type',
            'cadence_days',
            'expiration_height',
            name='uq_account_alert_event_cadence',
        ),
    )


class SupportWallPost(db.Model):
    __tablename__ = 'support_wall_posts'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=True, index=True)
    submitted_by_account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=True, index=True)
    approved_by_account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=True, index=True)

    submitted_on_behalf_of = db.Column(db.Boolean, default=False, nullable=False, index=True)
    public_name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(80), nullable=False)
    location = db.Column(db.String(120), nullable=True)
    message = db.Column(db.Text, nullable=False)
    link = db.Column(db.String(500), nullable=True)
    hns_name = db.Column(db.String(255), nullable=True, index=True)

    status = db.Column(db.String(40), default='pending', nullable=False, index=True)
    verification_status = db.Column(db.String(40), default='unverified', nullable=False, index=True)
    verification_method = db.Column(db.String(40), nullable=True, index=True)
    verification_payload_private = db.Column(db.Text, nullable=True)
    verification_nonce = db.Column(db.String(120), nullable=True, unique=True, index=True)

    source_channel = db.Column(db.String(40), nullable=True, index=True)
    source_note_private = db.Column(db.Text, nullable=True)
    consent_status = db.Column(db.String(40), default='unknown', nullable=False, index=True)
    admin_note_private = db.Column(db.Text, nullable=True)
    badges_json = db.Column(db.JSON, default=list, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    approved_at = db.Column(db.DateTime, nullable=True, index=True)

    submitted_by_account = db.relationship(
        'Account',
        foreign_keys=[submitted_by_account_id],
        lazy=True,
    )
    approved_by_account = db.relationship(
        'Account',
        foreign_keys=[approved_by_account_id],
        lazy=True,
    )
