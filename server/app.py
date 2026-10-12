"""QSYSRadio Flask app (served by gunicorn via start.sh)."""
from flask import Flask, jsonify

from . import config, db
from .migrate import discover

VERSION = config.version()

app = Flask(__name__)
app.config['SECRET_KEY'] = config.get('SECRET_KEY') or None


@app.errorhandler(db.DatabaseUnavailable)
def _db_unavailable(exc):
    app.logger.error('database unavailable: %s', exc)
    return jsonify(ok=False, error='database unavailable'), 503


@app.get('/api/v1/health')
def health():
    """Version + schema state; 503 when PostgreSQL is down."""
    app_schema, _ = db.schemas()
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT COALESCE(MAX(version), 0) FROM schema_migrations')
            schema_version = cur.fetchone()[0]
        conn.rollback()
    finally:
        conn.close()
    migrations = discover()
    expected = migrations[-1][0] if migrations else 0
    return jsonify(ok=schema_version == expected, app='qsysradio', version=VERSION,
                   schema=app_schema, schema_version=schema_version,
                   schema_expected=expected)
