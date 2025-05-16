import os
import pathlib

import sqlalchemy
from flask import Blueprint
from flask import flash
from flask import redirect
from flask import render_template
from flask import request
from flask import url_for

from ...core.activities import ActivityRepository
from ...core.config import Config
from ...core.datamodel import DB
from ...core.datamodel import Kind
from ...core.enrichment import populate_database_from_extracted
from ...core.tasks import work_tracker_path
from ...explorer.tile_visits import compute_tile_evolution
from ...explorer.tile_visits import compute_tile_visits_new
from ...explorer.tile_visits import TileVisitAccessor
from ...importers.directory import get_file_hash
from ...importers.directory import import_from_directory
from ...importers.strava_api import import_from_strava_api
from ...importers.strava_checkout import import_from_strava_checkout
from ..authenticator import Authenticator
from ..authenticator import needs_authentication


def make_upload_blueprint(
    repository: ActivityRepository,
    tile_visit_accessor: TileVisitAccessor,
    config: Config,
    authenticator: Authenticator,
) -> Blueprint:
    blueprint = Blueprint("upload", __name__, template_folder="templates")

    @blueprint.route("/")
    @needs_authentication(authenticator)
    def index():
        pathlib.Path("Activities").mkdir(exist_ok=True, parents=True)
        directories = []
        for root, dirs, files in os.walk("Activities"):
            directories.append(root)
        directories.sort()
        return render_template("upload/index.html.j2", directories=directories)

    @blueprint.route("/receive", methods=["POST"])
    @needs_authentication(authenticator)
    def receive():
        # check if the post request has the file part
        if "file" not in request.files:
            flash("No file could be found. Did you select a file?", "warning")
            return redirect("/upload")

        file = request.files["file"]
        # If the user does not select a file, the browser submits an
        # empty file without a filename.
        if file.filename == "":
            flash("No selected file", "warning")
            return redirect("/upload")
        if file:
            filename = file.filename
            target_path = pathlib.Path(request.form["directory"]) / filename
            assert target_path.suffix in [
                ".csv",
                ".fit",
                ".gpx",
                ".gz",
                ".kml",
                ".kmz",
                ".tcx",
            ]
            assert target_path.is_relative_to("Activities")
            file.save(target_path)
            scan_for_activities(
                repository,
                tile_visit_accessor,
                config,
                skip_strava=True,
            )
            activity_id = get_file_hash(target_path)
            flash(f"Activity was saved with ID {activity_id}.", "success")
            return redirect(f"/activity/{activity_id}")

    @blueprint.route("/refresh")
    @needs_authentication(authenticator)
    def reload():
        return render_template("upload/reload.html.j2")

    @blueprint.route("/execute-reload")
    @needs_authentication(authenticator)
    def execute_reload():
        scan_for_activities(
            repository,
            tile_visit_accessor,
            config,
            skip_strava=True,
        )
        flash("Scanned for new activities.", category="success")
        return redirect(url_for("index"))

    return blueprint


def scan_for_activities(
    repository: ActivityRepository,
    tile_visit_accessor: TileVisitAccessor,
    config: Config,
    skip_strava: bool = False,
) -> None:
    if pathlib.Path("Activities").exists():
        import_from_directory(config.metadata_extraction_regexes, config)
    if pathlib.Path("Strava Export").exists():
        import_from_strava_checkout()
    if config.strava_client_code and not skip_strava:
        import_from_strava_api(config)

    populate_database_from_extracted(config)

    if len(repository) > 0:
        kinds = DB.session.scalars(sqlalchemy.select(Kind)).all()
        if all(kind.consider_for_achievements == False for kind in kinds):
            for kind in kinds:
                kind.consider_for_achievements = True
            DB.session.commit()
            tile_visit_accessor.reset()
            work_tracker_path("tile-state").unlink(missing_ok=True)
        compute_tile_visits_new(repository, tile_visit_accessor)
        compute_tile_evolution(tile_visit_accessor.tile_state, config)
        tile_visit_accessor.save()
