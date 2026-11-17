"""Idempotent additive route registration for both legacy and factory entrypoints."""

def register(app):
    from .routes import bp
    if bp.name not in app.blueprints:
        app.register_blueprint(bp)
