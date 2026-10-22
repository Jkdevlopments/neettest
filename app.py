
import os
import json
import logging
import warnings

from pathlib import Path
from functools import wraps
from threading import RLock
from uuid import uuid4
from io import BytesIO

from PIL import (
    Image,
    ImageOps,
    UnidentifiedImageError,
    ImageFile,
)

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    abort,
    send_file,
)

from werkzeug.utils import secure_filename

from firebase_auth import (
    initialize_firebase,
    firebase_email_password_login,
    is_authorized_admin,
)

from drive_storage import (
    upload_question_image,
    download_question_image,
    delete_question_image,
)


# ==================================================
# APP CONFIG
# ==================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configure SECRET_KEY only once.
app.secret_key = os.environ.get("SECRET_KEY")

if not app.secret_key:
    raise RuntimeError(
        "Set SECRET_KEY in your environment before starting Flask."
    )

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(
        os.environ.get("COOKIE_SECURE", "false").lower() == "true"
    ),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 2,
)

# Initialize Firebase after validating Flask configuration.
initialize_firebase()

BASE_DIR = Path(__file__).resolve().parent
DATABASE_FILE = BASE_DIR / "questions.json"

UPLOAD_FOLDER = BASE_DIR / "static" / "uploads"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

MAX_IMAGE_SIZE = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000

JPEG_QUALITY = 98
WEBP_QUALITY = 98

ImageFile.LOAD_TRUNCATED_IMAGES = False
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

db_lock = RLock()


# ==================================================
# CONSTANTS
# ==================================================

ALLOWED_IMAGES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

VALID_SUBJECTS = {"Physics", "Chemistry", "Biology"}
VALID_PAPERS = VALID_SUBJECTS | {"Full Exam"}

PAPER_ALIASES = {
    "physics": "Physics",
    "physics only": "Physics",
    "chemistry": "Chemistry",
    "chemistry only": "Chemistry",
    "biology": "Biology",
    "biology only": "Biology",
    "full exam": "Full Exam",
    "full exam (physics, chemistry & biology)": "Full Exam",
}


# ==================================================
# EXAM SELECTION HELPERS
# ==================================================

def normalize_paper(value):
    if not value:
        return None

    return PAPER_ALIASES.get(value.strip().lower())


def normalize_subject(value):
    if not isinstance(value, str):
        return ""

    return value.strip().lower()


def filter_questions_by_paper(questions, paper):
    if paper == "Full Exam":
        return questions

    return [
        question
        for question in questions
        if normalize_subject(question.get("subject")) == paper.lower()
    ]


def get_exam_selection():
    exam_name = session.get("selected_exam", "NEET")
    paper = session.get("selected_paper", "Full Exam")

    if paper not in VALID_PAPERS:
        paper = "Full Exam"

    return exam_name, paper


# ==================================================
# JSON DATABASE
# ==================================================

def load_questions():
    with db_lock:
        if not DATABASE_FILE.exists():
            return []

        try:
            with DATABASE_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)

            if not isinstance(data, list):
                raise ValueError(
                    "questions.json must contain a JSON list."
                )

            return data

        except (json.JSONDecodeError, OSError, ValueError):
            logger.exception("Could not load questions.json")
            raise


def save_questions(questions):
    """Atomically save the question database."""
    with db_lock:
        temp_file = DATABASE_FILE.with_suffix(".tmp")

        try:
            with temp_file.open("w", encoding="utf-8") as file:
                json.dump(
                    questions,
                    file,
                    indent=2,
                    ensure_ascii=False,
                )

            os.replace(temp_file, DATABASE_FILE)

        finally:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    logger.warning(
                        "Could not remove temporary JSON file."
                    )


def get_question(question_id):
    wanted_id = str(question_id)

    for question in load_questions():
        if str(question.get("id")) == wanted_id:
            return question

    return None


def next_question_id(questions):
    numeric_ids = []

    for question in questions:
        try:
            numeric_ids.append(int(question.get("id")))
        except (TypeError, ValueError):
            continue

    return max(numeric_ids, default=0) + 1


# ==================================================
# HD IMAGE PROCESSING
# ==================================================

def process_image_hd(image_bytes, extension):
    """
    Validate image data, correct EXIF orientation and preserve dimensions.
    PNG is saved losslessly; JPEG and WEBP use high-quality settings.
    """

    try:
        # First pass: validate the image structure and dimensions.
        with warnings.catch_warnings():
            warnings.simplefilter(
                "error",
                Image.DecompressionBombWarning,
            )

            with Image.open(BytesIO(image_bytes)) as check_image:
                if check_image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError(
                        "Only JPEG, PNG and WEBP images are supported."
                    )

                width, height = check_image.size

                if width <= 0 or height <= 0:
                    raise ValueError("Invalid image dimensions.")

                if width * height > MAX_IMAGE_PIXELS:
                    raise ValueError(
                        "Image resolution exceeds 40 megapixels."
                    )

                check_image.verify()

        # Second pass: decode the image.
        with warnings.catch_warnings():
            warnings.simplefilter(
                "error",
                Image.DecompressionBombWarning,
            )

            with Image.open(BytesIO(image_bytes)) as source:
                image = ImageOps.exif_transpose(source)
                image.load()
                image = image.copy()

        if extension in {".jpg", ".jpeg"}:
            if image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            ):
                rgba_image = image.convert("RGBA")

                background = Image.new(
                    "RGB",
                    rgba_image.size,
                    (255, 255, 255),
                )

                background.paste(
                    rgba_image,
                    mask=rgba_image.getchannel("A"),
                )

                image = background
            else:
                image = image.convert("RGB")

            output_format = "JPEG"

        elif extension == ".png":
            if image.mode not in {"RGB", "RGBA", "L", "LA", "P", "1"}:
                image = image.convert("RGBA")

            output_format = "PNG"

        elif extension == ".webp":
            if image.mode not in {"RGB", "RGBA"}:
                if "A" in image.getbands() or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    image = image.convert("RGBA")
                else:
                    image = image.convert("RGB")

            output_format = "WEBP"

        else:
            raise ValueError("Unsupported image extension.")

        output = BytesIO()

        if output_format == "JPEG":
            image.save(
                output,
                format="JPEG",
                quality=JPEG_QUALITY,
                subsampling=0,
                optimize=True,
            )

        elif output_format == "PNG":
            image.save(
                output,
                format="PNG",
                optimize=True,
            )

        elif output_format == "WEBP":
            image.save(
                output,
                format="WEBP",
                quality=WEBP_QUALITY,
                method=6,
            )

        processed_bytes = output.getvalue()

        if not processed_bytes:
            raise ValueError("Image processing produced an empty file.")

        return processed_bytes, output_format

    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
    ) as exc:
        raise ValueError(
            "Invalid, corrupted or excessively large image."
        ) from exc


# ==================================================
# LEGACY LOCAL IMAGE SUPPORT
# ==================================================

def get_local_image_path(image_value):
    """Resolve legacy image paths safely inside static/uploads."""
    if not image_value:
        return None

    relative_path = Path(str(image_value))

    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None

    # Support both "uploads/file.png" and "file.png".
    if relative_path.parts and relative_path.parts[0] == "uploads":
        relative_path = Path(*relative_path.parts[1:])

    if not relative_path.parts:
        return None

    upload_root = UPLOAD_FOLDER.resolve()
    image_path = (upload_root / relative_path).resolve()

    try:
        image_path.relative_to(upload_root)
    except ValueError:
        return None

    if not image_path.is_file():
        return None

    return image_path


def delete_local_question_image(image_value):
    image_path = get_local_image_path(image_value)

    if image_path is not None:
        image_path.unlink()


# ==================================================
# ADMIN AUTHENTICATION
# ==================================================

def admin_required(view_function):
    @wraps(view_function)
    def wrapper(*args, **kwargs):
        uid = session.get("firebase_admin_uid")

        if not uid or not is_authorized_admin(uid):
            session.clear()
            return redirect(url_for("admin_login"))

        session.permanent = True
        return view_function(*args, **kwargs)

    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():

    # Already authenticated admins can go directly to the dashboard.
    if request.method == "GET":
        uid = session.get("firebase_admin_uid")

        if uid and is_authorized_admin(uid):
            return redirect(url_for("admin_dashboard"))

        return render_template("admin_login.html")

    # Process the login form.
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    if not email or not password:
        flash("Enter your email and password.", "error")
        return render_template("admin_login.html"), 400

    try:
        # Authenticate using Firebase Email/Password.
        claims = firebase_email_password_login(email, password)
        uid = claims.get("uid", "")

        # Firebase authentication alone does not grant admin access.
        if not uid or not is_authorized_admin(uid):
            app.logger.warning(
                "Unauthorized Firebase user attempted admin login: %s",
                uid,
            )

            flash(
                "This account is not authorized for admin access.",
                "error",
            )
            return render_template("admin_login.html"), 403

        # Start a fresh session after successful authentication.
        session.clear()
        session["firebase_admin_uid"] = uid
        session["firebase_admin_email"] = claims.get("email", email)
        session.permanent = True

        # IMPORTANT: the endpoint is admin_dashboard, not admin.
        return redirect(url_for("admin_dashboard"))

    except ValueError:
        # The helper should raise ValueError for invalid credentials.
        flash("Invalid email or password.", "error")
        return render_template("admin_login.html"), 401

    except Exception:
        app.logger.exception("Firebase admin login failed")
        flash("Login service is temporarily unavailable.", "error")
        return render_template("admin_login.html"), 503


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# ==================================================
# ADMIN DASHBOARD
# ==================================================

@app.route("/admin")
@admin_required
def admin_dashboard():
    questions = load_questions()

    return render_template(
        "admin.html",
        questions=questions,
        total_questions=len(questions),
    )


# ==================================================
# ADD IMAGE QUESTION - GOOGLE DRIVE
# ==================================================

@app.route("/admin/add-question", methods=["POST"])
@admin_required
def add_question():
    subject = request.form.get("subject", "").strip()
    answer = request.form.get("answer", "").strip()
    image = request.files.get("question_image")

    if subject not in VALID_SUBJECTS:
        flash("Please select a valid subject.", "error")
        return redirect(url_for("admin_dashboard"))

    if answer not in {"1", "2", "3", "4"}:
        flash("Please select the correct option.", "error")
        return redirect(url_for("admin_dashboard"))

    if not image or not image.filename:
        flash("Please select a question image.", "error")
        return redirect(url_for("admin_dashboard"))

    drive_file_id = None

    try:
        safe_name = secure_filename(image.filename)

        if not safe_name:
            raise ValueError("Invalid image filename.")

        extension = Path(safe_name).suffix.lower()

        if extension not in ALLOWED_IMAGES:
            raise ValueError(
                "Only PNG, JPG, JPEG and WEBP images are allowed."
            )

        expected_mime = ALLOWED_IMAGES[extension]
        uploaded_mime = (image.mimetype or "").lower()

        if uploaded_mime and uploaded_mime != expected_mime:
            raise ValueError(
                "Image type does not match the file extension."
            )

        image_bytes = image.stream.read(MAX_IMAGE_SIZE + 1)

        if not image_bytes:
            raise ValueError("The uploaded image is empty.")

        if len(image_bytes) > MAX_IMAGE_SIZE:
            raise ValueError("Image size must not exceed 8 MB.")

        processed_bytes, output_format = process_image_hd(
            image_bytes,
            extension,
        )

        output_details = {
            "JPEG": (".jpg", "image/jpeg"),
            "PNG": (".png", "image/png"),
            "WEBP": (".webp", "image/webp"),
        }

        output_extension, mime_type = output_details[output_format]
        filename = f"question_{uuid4().hex}{output_extension}"

        # Upload validated image bytes to Google Drive.
        drive_file_id = upload_question_image(
            processed_bytes,
            filename,
            mime_type,
        )

        questions = load_questions()
        question_id = next_question_id(questions)

        question = {
            "id": question_id,
            "type": "image",
            "subject": subject,
            "question": "",
            "image": "",
            "image_drive_id": drive_file_id,
            "image_mime_type": mime_type,
            "options": ["1", "2", "3", "4"],
            "answer": answer,
        }

        questions.append(question)

        try:
            save_questions(questions)

        except Exception:
            # If saving JSON fails, attempt to clean up the Drive upload.
            try:
                delete_question_image(drive_file_id)
            except Exception:
                logger.exception(
                    "Could not clean up the uploaded Drive image."
                )

            raise

        flash(
            "Question image uploaded to Google Drive successfully.",
            "success",
        )

    except ValueError as error:
        flash(str(error), "error")

    except Exception:
        logger.exception("Google Drive question upload failed")

        flash(
            "Upload failed. Check Google Drive credentials, "
            "authorization and server logs.",
            "error",
        )

    return redirect(url_for("admin_dashboard"))


# ==================================================
# DELETE QUESTION
# ==================================================

@app.route(
    "/admin/delete-question/<question_id>",
    methods=["POST"],
)
@admin_required
def delete_question(question_id):
    questions = load_questions()

    question = next(
        (
            item for item in questions
            if str(item.get("id")) == str(question_id)
        ),
        None,
    )

    if question is None:
        flash("Question not found.", "error")
        return redirect(url_for("admin_dashboard"))

    remaining_questions = [
        item for item in questions
        if str(item.get("id")) != str(question_id)
    ]

    try:
        save_questions(remaining_questions)

    except (OSError, json.JSONDecodeError):
        logger.exception("Could not save question deletion.")
        flash("Could not delete the question.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        drive_id = question.get("image_drive_id")

        if drive_id:
            delete_question_image(drive_id)
        else:
            delete_local_question_image(question.get("image", ""))

    except Exception:
        logger.exception(
            "Question removed from JSON, but image cleanup failed."
        )

        flash(
            "Question deleted, but its stored image could not be removed.",
            "error",
        )

        return redirect(url_for("admin_dashboard"))

    flash("Question deleted successfully.", "success")
    return redirect(url_for("admin_dashboard"))


# ==================================================
# QUESTION IMAGE ROUTE - DRIVE + LOCAL
# ==================================================

@app.route("/question-image/<question_id>")
def question_image(question_id):
    question = get_question(question_id)

    if question is None:
        abort(404)

    allowed_ids = {
        str(qid)
        for qid in session.get("exam_question_ids", [])
    }

    # Permit an authorized admin or a student taking an active exam.
    uid = session.get("firebase_admin_uid")
    is_admin = bool(uid and is_authorized_admin(uid))

    is_exam_question_allowed = (
        session.get("exam_started")
        and str(question_id) in allowed_ids
    )

    if not is_admin and not is_exam_question_allowed:
        abort(403)

    drive_id = question.get("image_drive_id")

    if drive_id:
        mime_type = question.get(
            "image_mime_type",
            "image/jpeg",
        )

        if mime_type not in {
            "image/jpeg",
            "image/png",
            "image/webp",
        }:
            abort(404)

        try:
            image_buffer = download_question_image(drive_id)

            response = send_file(
                image_buffer,
                mimetype=mime_type,
                as_attachment=False,
                conditional=False,
                download_name=f"question-{question_id}",
            )

            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"

            return response

        except Exception:
            logger.exception(
                "Could not retrieve question image from Google Drive."
            )
            abort(502)

    # Backward compatibility for existing local images.
    image_path = get_local_image_path(
        question.get("image", "")
    )

    if image_path is None:
        abort(404, description="Question image not found.")

    mime_type = ALLOWED_IMAGES.get(image_path.suffix.lower())

    if not mime_type:
        abort(404)

    response = send_file(
        image_path,
        mimetype=mime_type,
        as_attachment=False,
        conditional=True,
        max_age=300,
    )

    response.headers["Cache-Control"] = "private, max-age=300"
    response.headers["X-Content-Type-Options"] = "nosniff"

    return response


# ==================================================
# HOME
# ==================================================

@app.route("/")
def home():
    return render_template("home.html")


# ==================================================
# INSTRUCTIONS
# ==================================================

@app.route("/instructions", methods=["GET", "POST"])
def instructions():
    if request.method != "POST":
        return redirect(url_for("home"))

    exam_type = request.form.get("exam", "").strip()
    submitted_paper = request.form.get("paper", "").strip()

    if not exam_type:
        flash("Please select an exam.", "error")
        return redirect(url_for("home"))

    paper = normalize_paper(submitted_paper)

    if paper not in VALID_PAPERS:
        flash("Please select a valid paper.", "error")
        return redirect(url_for("home"))

    session["selected_exam"] = exam_type
    session["selected_paper"] = paper

    session.pop("exam_question_ids", None)
    session.pop("exam_started", None)

    all_questions = load_questions()

    selected_questions = filter_questions_by_paper(
        all_questions,
        paper,
    )

    total_questions = len(selected_questions)

    marks_correct = 4
    marks_incorrect = -1
    marks_unanswered = 0

    max_marks = total_questions * marks_correct

    duration = 180 if paper == "Full Exam" else 50
    time_seconds = duration * 60

    return render_template(
        "instructions.html",
        exam_type=exam_type,
        paper=paper,
        total_questions=total_questions,
        max_marks=max_marks,
        duration=duration,
        time_seconds=time_seconds,
        marks_correct=marks_correct,
        marks_incorrect=marks_incorrect,
        marks_unanswered=marks_unanswered,
    )


# ==================================================
# EXAM
# ==================================================

@app.route("/exam", methods=["GET", "POST"])
def exam():
    if request.method != "POST":
        return redirect(url_for("home"))

    exam_name = session.get("selected_exam")
    saved_paper = session.get("selected_paper")

    if not exam_name:
        exam_name = (
            request.form.get("exam", "").strip() or "NEET"
        )

    if not saved_paper:
        saved_paper = normalize_paper(
            request.form.get("paper", "")
        )

    paper = (
        saved_paper
        if saved_paper in VALID_PAPERS
        else None
    )

    if not paper:
        flash(
            "Please select your exam and paper again.",
            "error",
        )
        return redirect(url_for("home"))

    all_questions = load_questions()

    exam_questions = filter_questions_by_paper(
        all_questions,
        paper,
    )

    if not exam_questions:
        flash(
            f"No questions are available for {paper} yet.",
            "error",
        )
        return redirect(url_for("home"))

    session["selected_exam"] = exam_name
    session["selected_paper"] = paper

    session["exam_question_ids"] = [
        str(question["id"])
        for question in exam_questions
    ]

    session["exam_started"] = True

    duration = 180 if paper == "Full Exam" else 50
    time_seconds = duration * 60

    return render_template(
        "exam.html",
        questions=exam_questions,
        duration=duration,
        time_seconds=time_seconds,
        exam=exam_name,
        exam_type=exam_name,
        paper=paper,
    )


# ==================================================
# SUBMIT AND SCORE
# ==================================================

@app.route("/submit", methods=["POST"])
def submit():
    if not session.get("exam_started"):
        abort(403)

    allowed_ids = {
        str(qid)
        for qid in session.get("exam_question_ids", [])
    }

    questions = [
        question
        for question in load_questions()
        if str(question.get("id")) in allowed_ids
    ]

    correct = 0
    incorrect = 0
    unanswered = 0

    for question in questions:
        question_id = str(question["id"])

        selected = (
            request.form.get(f"answer_{question_id}")
            or request.form.get(f"answers[{question_id}]")
            or request.form.get(question_id)
            or request.form.get(f"question_{question_id}")
            or ""
        ).strip()

        correct_answer = str(
            question.get("answer", "")
        ).strip()

        if not selected:
            unanswered += 1
        elif selected == correct_answer:
            correct += 1
        else:
            incorrect += 1

    total = len(questions)
    score = correct * 4 - incorrect
    maximum_score = total * 4

    exam_name, paper = get_exam_selection()

    session["exam_started"] = False
    session.pop("exam_question_ids", None)

    return render_template(
        "result.html",
        score=score,
        total=total,
        maximum_score=maximum_score,
        correct=correct,
        incorrect=incorrect,
        unanswered=unanswered,
        exam=exam_name,
        exam_type=exam_name,
        paper=paper,
    )


# ==================================================
# ERROR HANDLERS
# ==================================================

@app.errorhandler(413)
def upload_too_large(error):
    flash(
        "The uploaded file is too large. Maximum request size is 10 MB.",
        "error",
    )
    return redirect(url_for("admin_dashboard"))


# ==================================================
# RUN APP
# ==================================================

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
    )
