"""``/help`` route blueprint."""

from flask import Blueprint

from annuli.interfaces import web as _web

bp = Blueprint("help", __name__)


@bp.route("/help")
def help_page():
    """Render the in-app Help & Glossary page."""
    return _web.render(
        "help",
        active="help",
        title="Help",
    )
