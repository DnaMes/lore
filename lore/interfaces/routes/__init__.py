"""Flask blueprints that make up the Lore web UI (#119).

``lore.interfaces.web`` owns the ``app`` object, the request hooks and the
shared seams (index access, rendering, session loading); it registers these
blueprints at import time. Route modules reach those seams through
``lore.interfaces.web`` so the module-global patch targets keep working — see
"Web module layout" in ``docs/WEB-UI.md``.
"""

from flask import Flask


def register_blueprints(app: Flask) -> None:
    """Register every route blueprint on ``app``, skipping already-registered ones.

    The imports stay inside the function: a blueprint module imports
    ``lore.interfaces.web`` for its seams, and ``web`` imports this package to
    register them, so the submodules must load only once ``web`` asks for them.
    """
    from .api_v1 import bp as api_v1_bp
    from .jobs import bp as jobs_bp
    from .memory import bp as memory_bp
    from .search import bp as search_bp
    from .sessions import bp as sessions_bp
    from .tags import bp as tags_bp

    for blueprint in (
        api_v1_bp,
        jobs_bp,
        memory_bp,
        search_bp,
        sessions_bp,
        tags_bp,
    ):
        if blueprint.name not in app.blueprints:
            app.register_blueprint(blueprint)
