from flask import Blueprint, jsonify

from app.web import snapshot
from .service import ACTIONS, build_release

bp = Blueprint('release', __name__)


@bp.get('/api/release')
def release():
    run = snapshot()
    revision = build_release(run)
    return jsonify(id=run['manifest']['id'], actions=ACTIONS, data=revision)
