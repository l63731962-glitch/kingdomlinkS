import os
from flask import Flask
from app.database import db


def create_app():
    app = Flask(__name__, static_folder="static", static_url_path="")

    app.config["SECRET_KEY"] = os.environ.get("NEOMAP_SECRET_KEY", "dev-secret-change-in-production")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "sqlite:///neomap.db"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    from app.routes import bp as api_bp
    app.register_blueprint(api_bp)

    @app.route("/")
    def index():
        return app.send_static_file("index.html")

    with app.app_context():
        db.create_all()

    return app