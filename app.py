import base64
import os
import logging
import re
from flask import Flask, render_template, request, redirect, url_for,make_response
from openai import OpenAI
from dotenv import load_dotenv
from PIL import Image
import io
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from flask_login import (
    LoginManager,
    login_user,
    login_required,
    logout_user,
    current_user,
    UserMixin,
)
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer
import requests
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import func


# -------------------------
# CONFIG
# -------------------------

load_dotenv()

app = Flask(__name__)
csrf = CSRFProtect(app)
limiter = Limiter(
    key_func=lambda: current_user.id if current_user.is_authenticated else get_remote_address(),
    app=app,
    default_limits=[]
)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL").replace(
    "postgres://", "postgresql://"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

logging.basicConfig(level=logging.INFO)

MAX_IMAGES = 5

# -------------------------
# SYSTEM PROMPT (YOUR ORIGINAL)
# -------------------------

SYSTEM_PROMPT = """
You are an expert Vinted clothing reseller, product copywriter, and fashion SEO specialist.

Your goal is to generate accurate, high-converting Vinted listings that maximise search visibility, buyer trust, and likelihood of sale.

STRICT RULES:

ACCURACY FIRST
- Base ALL information ONLY on what is visible in the images.
- Do NOT guess brand, size, material, or features.
- If brand is unclear, leave blank.
- If size is unclear, leave blank.
- Do not invent details not visible.

CONDITION ASSESSMENT
- Condition must be one of: New, Excellent, Very Good, Good, Fair.
- Judge condition ONLY from visible wear.
- Do not inflate condition to sound appealing.

FLAWS HANDLING
- Carefully inspect ALL images for flaws before writing anything.
- Visible flaws include: stains, fading, cracking, holes, pulls, loose stitching, marks, distressing, discolouration, fabric thinning, pilling, repairs, missing parts, or damage.

IF flaws ARE visible:
- Include ONE Flaws section directly AFTER the Condition line.
- The Flaws section must appear exactly once in the entire output.
- Each flaw must be listed as a bullet point starting with "- ".
- Each bullet must be one short factual sentence.

IF NO flaws are visible:
- Do NOT include a Flaws section.
- Do NOT write "None", "No flaws", "N/A", or similar.
- Do NOT mention flaws anywhere in the output.

STRICT OUTPUT RULES:
- The header "Flaws:" may appear either 0 times or 1 time.
- It must NEVER appear more than once.
- If present, it must appear immediately after the Condition line.

WRITING STYLE
- Professional, natural, human-like reseller tone.
- Clear, concise, and trustworthy.
- No emojis.
- No hype, exaggeration, or filler.
- No markdown formatting (no bold, asterisks, or symbols).
- No extra commentary outside the format.

TITLE OPTIMISATION
- Prioritise search keywords buyers actually use.
- Include brand (if known), item type, colour, style/fit, size (if known).
- Keep readable and natural.
- Avoid repetition or keyword stuffing.

DESCRIPTION OPTIMISATION
- Do NOT repeat or restate flaws in the description.
- Write 2–4 sentences.
- Focus on style, fit, wearability, and typical use cases.
- Use relevant fashion keywords naturally.
- Highlight desirable features visible in the images.

HASHTAGS
- Exactly 5 hashtags.
- Lowercase only.
- Highly relevant search terms.
- No punctuation except #.
- No duplicates.

FORMAT (FOLLOW EXACTLY):

Title:

Brand:
Size:
Condition:

[If flaws exist, insert the Flaws section here]

[2–4 sentence description]

#hashtag1 #hashtag2 #hashtag3 #hashtag4 #hashtag5
"""

# -------------------------
# DATABASE MODELS
# -------------------------

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    credits = db.Column(db.Integer, default=10)
    is_generating = db.Column(db.Boolean, default=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Generation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    tokens_used = db.Column(db.Integer)
    status = db.Column(db.String(20), default="completed")
    result = db.Column(db.Text)
    error = db.Column(db.Text)

class PromoCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    credits = db.Column(db.Integer, nullable=False)

    is_active = db.Column(db.Boolean, default=True)
    max_uses = db.Column(db.Integer, nullable=True)  # None = unlimited
    uses_count = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class PromoRedemption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    promo_id = db.Column(db.Integer, db.ForeignKey("promo_code.id"), nullable=False)
    redeemed_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# -------------------------
# OUTPUT VALIDATION
# -------------------------

def validate_and_fix_listing(raw_output):
    if not raw_output:
        return "Error generating full listing.", True

    fallback_used = False

    raw_output = re.sub(r"(Flaws:.*?)(\nFlaws:)", r"\1", raw_output, flags=re.S)

    sections = {
        "Title:": "",
        "Brand:": "",
        "Size:": "",
        "Condition:": ""
    }

    for key in sections.keys():
        match = re.search(rf"{key}\s*(.*)", raw_output)
        if match:
            sections[key] = match.group(1).strip()

    # Fallback title if missing or blank
    if not sections["Title:"]:
        sections["Title:"] = "Clothing Item"
        fallback_used = True

    # You could optionally enforce required Condition:
    if not sections["Condition:"]:
        fallback_used = True

    rebuilt = (
        f"Title: {sections['Title:']}\n\n"
        f"Brand: {sections['Brand:']}\n"
        f"Size: {sections['Size:']}\n"
        f"Condition: {sections['Condition:']}\n"
    )

    flaws_match = re.search(r"Flaws:\s*(.*)", raw_output)
    if flaws_match:
        rebuilt += f"Flaws: {flaws_match.group(1).strip()}\n"

    description_split = re.split(r"Condition:.*?\n", raw_output, maxsplit=1)
    if len(description_split) > 1:
        rebuilt += "\n" + description_split[1].strip()

    return rebuilt.strip(), fallback_used

# -------------------------
# GENERATION LOGIC
# -------------------------

def generate_listing(images):

    content = [
        {
            "type": "text",
            "text": "Carefully inspect ALL provided images for visible flaws such as holes, stains, fading, cracking, or damage. Then generate ONE Vinted listing for this clothing item using ALL provided images."
        }
    ]

    for image in images:
        img = Image.open(image)
        img.thumbnail((800, 800))

        buffer = io.BytesIO()
        img.convert("RGB").save(buffer, format="JPEG", quality=65)
        buffer.seek(0)

        encoded_image = base64.b64encode(buffer.read()).decode("utf-8")

        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{encoded_image}"
            }
        })

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content}
        ],
        max_tokens=500,
        temperature=0.4
    )

    raw_listing = response.choices[0].message.content
    listing, fallback_used = validate_and_fix_listing(raw_listing)
    tokens_used = response.usage.total_tokens if response.usage else None

    return listing, tokens_used, fallback_used
    
def generate_reset_token(email):
    return serializer.dumps(email, salt="password-reset-salt")


def verify_reset_token(token, expiration=3600):
    try:
        email = serializer.loads(
            token,
            salt="password-reset-salt",
            max_age=expiration
        )
    except Exception:
        return None
    return email

def send_reset_email(to_email, reset_url):
    api_key = os.getenv("RESEND_API_KEY")

    if not api_key:
        print("RESEND_API_KEY not found!")
        return

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": "onboarding@resend.dev",
            "to": to_email,
            "subject": "Reset Your Password",
            "text": f"Click the link below to reset your password:\n\n{reset_url}\n\n If you did not request this please ignore for security.",
        },
    )

    print("Resend response:", response.status_code, response.text)

def verify_turnstile(token):
    secret = os.environ.get("TURNSTILE_SECRET_KEY")

    url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

    response = requests.post(url, data={
        "secret": secret,
        "response": token
    })

    result = response.json()
    return result.get("success", False)




# -------------------------
# AUTH ROUTES
# -------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email").lower().strip()
        password = request.form.get("password")

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for("index"))

        response = make_response(render_template("login.html", error="Invalid email or password."))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    response = make_response(render_template("login.html"))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        turnstile_token = request.form.get("cf-turnstile-response")

        if not verify_turnstile(turnstile_token):
            response = make_response(render_template("register.html", error="CAPTCHA verification failed. Please try again."))
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

        email = request.form.get("email").lower().strip()
        password = request.form.get("password")

        if User.query.filter_by(email=email).first():
            response = make_response(render_template("register.html", error="Email already registered."))
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

        hashed_password = generate_password_hash(password)

        new_user = User(
            email=email,
            password_hash=hashed_password,
            credits=0,
        )

        db.session.add(new_user)
        db.session.commit()

        login_user(new_user)
        return redirect(url_for("index"))

    response = make_response(render_template("register.html"))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email").lower().strip()
        user = User.query.filter_by(email=email).first()

        if user:
            token = generate_reset_token(user.email)
            reset_url = url_for("reset_password", token=token, _external=True)

            send_reset_email(user.email, reset_url)

        return render_template(
            "forgot_password.html",
            message="If that email exists, a reset link has been sent."
        )

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    email = verify_reset_token(token)

    if not email:
        return render_template("reset_password.html", error="Invalid or expired token.")

    user = User.query.filter_by(email=email).first()
    if not user:
        return redirect(url_for("login"))

    if request.method == "POST":
        password = request.form.get("password")

        user.password_hash = generate_password_hash(password)
        db.session.commit()

        return redirect(url_for("login"))

    return render_template("reset_password.html")

@app.route("/redeem", methods=["POST"])
@login_required
def redeem_code():
    code_input = request.form.get("promo_code").strip().upper()

    promo = PromoCode.query.filter_by(code=code_input).first()

    if not promo or not promo.is_active:
        return render_template("index.html", listing="Invalid or inactive code.")

    # Check usage limit
    if promo.max_uses and promo.uses_count >= promo.max_uses:
        return render_template("index.html", listing="This code has reached its usage limit.")

    # Check if THIS user already redeemed
    existing = PromoRedemption.query.filter_by(
        user_id=current_user.id,
        promo_id=promo.id
    ).first()

    if existing:
        return render_template("index.html", listing="You have already used this code.")

    # Apply credits
    user = User.query.get(current_user.id)
    user.credits += promo.credits

    promo.uses_count += 1

    redemption = PromoRedemption(
        user_id=user.id,
        promo_id=promo.id
    )

    db.session.add(redemption)
    db.session.commit()

    return render_template(
        "index.html",
        listing=f"Promo applied! {promo.credits} credits added."
    )

@app.route("/admin/promos")
@login_required
def admin_promos():
    if not current_user.is_admin:
        return redirect(url_for("index"))

    promos = PromoCode.query.order_by(PromoCode.created_at.desc()).all()
    return render_template("admin_promos.html", promos=promos)

@app.route("/admin/promos/create", methods=["POST"])
@login_required
def create_promo():
    if not current_user.is_admin:
        return redirect(url_for("index"))

    code = request.form.get("code").strip().upper()
    credits = int(request.form.get("credits"))
    max_uses = request.form.get("max_uses")

    promo = PromoCode(
        code=code,
        credits=credits,
        max_uses=int(max_uses) if max_uses else None,
        is_active=True
    )

    db.session.add(promo)
    db.session.commit()

    return redirect(url_for("admin_promos"))

@app.route("/admin/promos/toggle/<int:promo_id>")
@login_required
def toggle_promo(promo_id):
    if not current_user.is_admin:
        return redirect(url_for("index"))

    promo = PromoCode.query.get_or_404(promo_id)
    promo.is_active = not promo.is_active
    db.session.commit()

    return redirect(url_for("admin_promos"))

@app.route("/terms")
def terms():
    return render_template("terms.html")

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/")
def home():
    total_generations = db.session.query(func.count(Generation.id)).scalar()

    return render_template(
        "home.html",
        total_generations=total_generations
    )

# -------------------------
# MAIN ROUTE
# -------------------------

@app.route("/generator", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute; 100 per hour; 400 per day")
def index():
    listing = None

    if request.method == "POST":

        user = db.session.query(User).with_for_update().filter_by(id=current_user.id).first()

        if user.is_generating:
            return render_template("index.html", listing="Generation already in progress.")

        if not user.is_admin and user.credits <= 0:
            return render_template("index.html", listing="You have no credits remaining.")

        images = request.files.getlist("images")

        if not images or images[0].filename == "":
            return render_template("index.html", listing="Please upload at least one image.")

        if len(images) > MAX_IMAGES:
            return render_template("index.html", listing=f"Maximum {MAX_IMAGES} images allowed.")

        try:
            user.is_generating = True
            if not user.is_admin:
                user.credits -= 1
            db.session.commit()

            start_time = datetime.utcnow()

            listing, tokens_used, fallback_used = generate_listing(images)

            end_time = datetime.utcnow()
            logging.info(f"Generation took {(end_time - start_time).total_seconds()} seconds")

            if fallback_used and not user.is_admin:
                user.credits += 1

            generation = Generation(
                user_id=user.id,
                tokens_used=tokens_used,
                status="degraded" if fallback_used else "completed",
                result=listing
            )


            db.session.add(generation)
            db.session.commit()

        except Exception as e:
            logging.error(f"Generation error: {e}")
            db.session.rollback()

            user = User.query.get(current_user.id)
            if not user.is_admin:
                user.credits += 1
            db.session.commit()

            listing = "Error generating listing. Please try again."

        finally:
            user = User.query.get(current_user.id)
            user.is_generating = False
            db.session.commit()

    return render_template("index.html", listing=listing)


if __name__ == "__main__":
    app.run()