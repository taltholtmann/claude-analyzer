"""Flask server: JSON API + web UI for the Claude/Codex Memory & Compliance Analyzer."""
import os

from flask import Flask, jsonify, render_template, abort

import sources

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/projects")
def api_projects():
    return jsonify(sources.list_projects())


@app.route("/api/projects/<project>/sessions")
def api_sessions(project):
    return jsonify(sources.list_sessions(_safe(project)))


@app.route("/api/projects/<project>/sessions/<session>")
def api_session(project, session):
    result = sources.analyze(_safe(project), _safe(session))
    if result is None:
        abort(404)
    return jsonify(result)


def _safe(name: str) -> str:
    """Prevent path traversal."""
    if "/" in name or "\\" in name or ".." in name:
        abort(400)
    return name


if __name__ == "__main__":
    # Direct-run defaults to loopback (transcripts contain full history); the
    # Docker image serves via gunicorn on 0.0.0.0 inside the container instead.
    app.run(host=os.environ.get("BIND", "127.0.0.1"),
            port=int(os.environ.get("PORT", 8420)), debug=False)
