import base64
import os
import logging
import re
from flask import Flask, render_template, request, redirect, url_for, make_response, session
from openai import OpenAI
from dotenv import load_dotenv
from PIL import Image
import io
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
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
import stripe


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
app.config["SERVER_NAME"] = "www.resellerdescriptions.com"
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL").replace(
    "postgres://", "postgresql://"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["STRIPE_SECRET_KEY"] = os.getenv("STRIPE_SECRET_KEY")
app.config["STRIPE_PUBLISHABLE_KEY"] = os.getenv("STRIPE_PUBLISHABLE_KEY")
app.config["STRIPE_WEBHOOK_SECRET"] = os.getenv("STRIPE_WEBHOOK_SECRET")

app.config["STRIPE_PRICE_50"] = os.getenv("STRIPE_PRICE_50")
app.config["STRIPE_PRICE_200"] = os.getenv("STRIPE_PRICE_200")
app.config["STRIPE_PRICE_600"] = os.getenv("STRIPE_PRICE_600")

stripe.api_key = app.config["STRIPE_SECRET_KEY"]

serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])

db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

logging.basicConfig(level=logging.INFO)

MAX_IMAGES = 5


def normalize_email(email):
    email = (email or "").lower().strip()

    if "@" not in email:
        return email

    local, domain = email.split("@", 1)

    if domain == "googlemail.com":
        domain = "gmail.com"

    if domain == "gmail.com":
        local = local.split("+", 1)[0]
        local = local.replace(".", "")

    return f"{local}@{domain}"


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

ALWAYS include a Flaws section directly after the Condition line.

If flaws ARE visible:
- Write "Flaws:" on its own line.
- Put each flaw on the next lines as bullet points starting with "- ".
- Do not place a flaw on the same line as "Flaws:".
- Do not repeat any flaw.
- Each bullet must be one short factual sentence.

If NO flaws are visible:
- Write exactly: Flaws: None
- Do not add bullet points.

STRICT OUTPUT RULES:
- The Flaws section must appear exactly once in the output.
- It must appear directly after the Condition line.
- Never create a second Flaws section.

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
Flaws:

[2–4 sentence description]

#hashtag1 #hashtag2 #hashtag3 #hashtag4 #hashtag5
"""

# -------------------------
# DATABASE MODELS
# -------------------------

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    normalized_email = db.Column(db.String(120), nullable=True)
    password_hash = db.Column(db.String(256), nullable=False)
    credits = db.Column(db.Integer, default=10)
    is_generating = db.Column(db.Boolean, default=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_verified = db.Column(db.Boolean, default=False)
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
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    promo_id = db.Column(db.Integer, db.ForeignKey("promo_code.id"), nullable=False)
    redeemed_email = db.Column(db.String(120), nullable=False)
    redeemed_email_normalized = db.Column(db.String(120), nullable=True)
    redeemed_at = db.Column(db.DateTime, default=datetime.utcnow)

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    stripe_session_id = db.Column(db.String(255), unique=True, nullable=False)
    stripe_payment_intent_id = db.Column(db.String(255), nullable=True)
    stripe_customer_email = db.Column(db.String(120), nullable=True)
    amount_total = db.Column(db.Integer, nullable=True)
    currency = db.Column(db.String(20), nullable=True)
    credits_added = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(50), default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def get_credit_pack(pack_key):
    packs = {
        "50": {
            "credits": 50,
            "price_id": app.config["STRIPE_PRICE_50"]
        },
        "200": {
            "credits": 200,
            "price_id": app.config["STRIPE_PRICE_200"]
        },
        "600": {
            "credits": 600,
            "price_id": app.config["STRIPE_PRICE_600"]
        }
    }
    return packs.get(pack_key)


# -------------------------
# OUTPUT VALIDATION
# -------------------------
def send_feedback_email(to_email):
    api_key = os.getenv("RESEND_API_KEY")

    if not api_key:
        print("RESEND_API_KEY not found!")
        return False

    survey_url = "https://docs.google.com/forms/d/e/1FAIpQLSfHu3BR0JKFNl3NohnerPP-z7wQwxk-N053Ky2-x0AZTeub_Q/viewform?usp=sharing&ouid=111029793169100977981"

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": "Reseller Descriptions <noreply@resellerdescriptions.com>",
            "to": to_email,
            "subject": "What would make the generator more useful for you?",
            "text": (
                "Hi,\n\n"
                "Thanks for using Reseller Descriptions.\n\n"
                "I'm trying to improve the tool and would really value your feedback.\n\n"
                "This quick survey takes less than 60 seconds:\n\n"
                f"{survey_url}\n\n"
                "Thanks for helping improve the tool!"
            ),
        },
    )

    print("Feedback email response:", response.status_code, response.text)
    return response.status_code in [200, 201]
    
def validate_and_fix_listing(raw_output):
    if not raw_output:
        return "Error generating full listing.", True

    fallback_used = False
    raw_output = raw_output.strip().replace("\r\n", "\n")

    def get_single_line_value(label):
        match = re.search(
            rf"^{re.escape(label)}\s*(.*)$",
            raw_output,
            flags=re.MULTILINE
        )
        return match.group(1).strip() if match else ""

    title = get_single_line_value("Title:")
    brand = get_single_line_value("Brand:")
    size = get_single_line_value("Size:")
    condition = get_single_line_value("Condition:")

    if not title:
        title = "Clothing Item"
        fallback_used = True

    if not condition:
        fallback_used = True

    flaws_block_match = re.search(
        r"^Flaws:\s*(.*?)(?=\n\s*\n|\n#|$)",
        raw_output,
        flags=re.MULTILINE | re.DOTALL
    )

    flaws_value = "None"

    if flaws_block_match:
        flaws_raw = flaws_block_match.group(1).strip()

        if flaws_raw and flaws_raw.lower() != "none":
            flaw_lines = []

            for line in flaws_raw.split("\n"):
                cleaned_line = line.strip()

                if not cleaned_line:
                    continue

                cleaned_line = re.sub(r"^-\s*", "", cleaned_line).strip()

                if cleaned_line:
                    flaw_lines.append(cleaned_line)

            seen = set()
            unique_flaws = []
            for flaw in flaw_lines:
                normalized = flaw.lower()
                if normalized not in seen:
                    seen.add(normalized)
                    unique_flaws.append(flaw)

            if unique_flaws:
                flaws_value = "\n" + "\n".join(f"- {flaw}" for flaw in unique_flaws)
            else:
                flaws_value = "None"
                fallback_used = True
        else:
            flaws_value = "None"
    else:
        flaws_value = "None"
        fallback_used = True

    body = raw_output

    body = re.sub(r"^Title:.*$\n?", "", body, flags=re.MULTILINE)
    body = re.sub(r"^Brand:.*$\n?", "", body, flags=re.MULTILINE)
    body = re.sub(r"^Size:.*$\n?", "", body, flags=re.MULTILINE)
    body = re.sub(r"^Condition:.*$\n?", "", body, flags=re.MULTILINE)
    body = re.sub(
        r"^Flaws:\s*(.*?)(?=\n\s*\n|\n#|$)",
        "",
        body,
        flags=re.MULTILINE | re.DOTALL
    )

    body = body.strip()

    rebuilt = (
        f"Title: {title}\n\n"
        f"Brand: {brand}\n"
        f"Size: {size}\n"
        f"Condition: {condition}\n"
        f"Flaws: {flaws_value}"
    )

    if body:
        rebuilt += f"\n\n{body}"

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


def generate_verification_token(email):
    return serializer.dumps(email, salt="email-verification-salt")


def verify_email_token(token, expiration=86400):
    try:
        email = serializer.loads(
            token,
            salt="email-verification-salt",
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
            "from": "Reseller Descriptions <noreply@resellerdescriptions.com>",
            "to": to_email,
            "subject": "Reset Your Password",
            "text": f"Click the link below to reset your password:\n\n{reset_url}\n\n If you did not request this please ignore for security.",
        },
    )

    print("Resend response:", response.status_code, response.text)


def send_verification_email(to_email):
    api_key = os.getenv("RESEND_API_KEY")

    if not api_key:
        print("RESEND_API_KEY not found!")
        return

    token = generate_verification_token(to_email)
    verify_url = url_for("verify_email", token=token, _external=True)

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": "Reseller Descriptions <noreply@resellerdescriptions.com>",
            "to": to_email,
            "subject": "Verify Your Email",
            "text": f"Click the link below to verify your email:\n\n{verify_url}\n\nIf you did not create this account, you can ignore this email.",
        },
    )

    print("Verification email response:", response.status_code, response.text)


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
        email = request.form.get("email", "").lower().strip()
        normalized_email = normalize_email(email)
        password = request.form.get("password")

        user = User.query.filter_by(normalized_email=normalized_email).first()

        if user and check_password_hash(user.password_hash, password):
            if not user.is_verified:
                response = make_response(render_template(
                    "login.html",
                    error="Please verify your email before logging in.",
                    email=email,
                    show_resend=True
                ))
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
                return response

            login_user(user)
            return redirect(url_for("index"))

        response = make_response(render_template(
            "login.html",
            error="Invalid email or password.",
            email=email,
            show_resend=False
        ))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    response = make_response(render_template(
        "login.html",
         email="",
         show_resend=False
         ))
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
            response = make_response(render_template(
                "register.html",
                error="CAPTCHA verification failed. Please try again.",
                turnstile_site_key=os.getenv("TURNSTILE_SITE_KEY")
            ))
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

        email = request.form.get("email").lower().strip()
        normalized_email = normalize_email(email)
        password = request.form.get("password")

        existing_user = User.query.filter(
            db.or_(
                User.email == email,
                User.normalized_email == normalized_email
            )
        ).first()

        if existing_user:
            response = make_response(render_template(
                "register.html",
                error="Email already registered.",
                turnstile_site_key=os.getenv("TURNSTILE_SITE_KEY")
            ))
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

        hashed_password = generate_password_hash(password)

        new_user = User(
            email=email,
            normalized_email=normalized_email,
            password_hash=hashed_password,
            credits=0,
            is_verified=False,
        )

        db.session.add(new_user)
        db.session.commit()

        send_verification_email(new_user.email)

        response = make_response(render_template(
        "register.html",
        message="Account created. Please check your email to verify your account.",
        turnstile_site_key=os.getenv("TURNSTILE_SITE_KEY")
    ))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    response = make_response(render_template(
        "register.html",
        turnstile_site_key=os.getenv("TURNSTILE_SITE_KEY")
    ))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/verify-email/<token>")
def verify_email(token):
    email = verify_email_token(token)

    if not email:
        response = make_response(render_template("login.html", error="Invalid or expired verification link."))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    normalized_email = normalize_email(email)
    user = User.query.filter_by(normalized_email=normalized_email).first()

    if not user:
        response = make_response(render_template("login.html", error="Account not found."))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    user.is_verified = True
    db.session.commit()

    login_user(user)
    return redirect(url_for("index"))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email").lower().strip()
        normalized_email = normalize_email(email)
        user = User.query.filter_by(normalized_email=normalized_email).first()

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

    normalized_email = normalize_email(email)
    user = User.query.filter_by(normalized_email=normalized_email).first()
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
    code_input = request.form.get("promo_code", "").strip().upper()

    if not code_input:
        session["listing"] = "Please enter a promo code."
        return redirect(url_for("index"))

    promo = PromoCode.query.filter_by(code=code_input).first()

    if not promo or not promo.is_active:
        session["listing"] = "Invalid or inactive code."
        return redirect(url_for("index"))

    if promo.max_uses and promo.uses_count >= promo.max_uses:
        session["listing"] = "This code has reached its usage limit."
        return redirect(url_for("index"))

    user = User.query.get(current_user.id)

    if not user.is_verified:
        session["listing"] = "Please verify your email before redeeming promo codes."
        return redirect(url_for("index"))

    existing = PromoRedemption.query.filter(
        PromoRedemption.promo_id == promo.id,
        PromoRedemption.redeemed_email_normalized == user.normalized_email
    ).first()

    if existing:
        session["listing"] = "You have already used this code."
        return redirect(url_for("index"))

    user.credits += promo.credits
    promo.uses_count += 1

    redemption = PromoRedemption(
        user_id=user.id,
        promo_id=promo.id,
        redeemed_email=user.email,
        redeemed_email_normalized=user.normalized_email
    )

    db.session.add(redemption)
    db.session.commit()

    session["listing"] = f"Promo applied! {promo.credits} credits added."
    return redirect(url_for("index"))


from datetime import datetime, timedelta


@app.route("/admin/promos")
@login_required
def admin_promos():
    if not current_user.is_admin:
        return redirect(url_for("index"))

    promos = PromoCode.query.order_by(PromoCode.created_at.desc()).all()

    thirty_days_ago = datetime.utcnow() - timedelta(days=30)

    active_customers = db.session.query(
        func.count(func.distinct(Generation.user_id))
    ).filter(
        Generation.created_at >= thirty_days_ago
    ).scalar()

    total_promo_uses = db.session.query(
        func.coalesce(func.sum(PromoCode.uses_count), 0)
    ).scalar()

    total_generations = db.session.query(
        func.count(Generation.id)
    ).scalar()

    monthly_revenue_pence = db.session.query(
        func.coalesce(func.sum(Payment.amount_total), 0)
    ).filter(
        Payment.status == "completed",
        Payment.created_at >= thirty_days_ago
    ).scalar()

    monthly_revenue = monthly_revenue_pence / 100

    monthly_generations = Generation.query.filter(
        Generation.created_at >= thirty_days_ago
    ).count()

    last_signup = db.session.query(
        func.max(User.created_at)
    ).scalar()

    monthly_ai_cost = monthly_generations * 0.02
    monthly_render_cost = 25
    estimated_profit_value = monthly_revenue - monthly_ai_cost - monthly_render_cost
    estimated_profit = f"{estimated_profit_value:.2f}"

    return render_template(
        "admin_promos.html",
        promos=promos,
        active_customers=active_customers,
        estimated_profit=estimated_profit,
        total_promo_uses=total_promo_uses,
        total_generations=total_generations,
        total_users = User.query.count(),
        last_signup=last_signup,
        monthly_revenue=f"{monthly_revenue:.2f}",
        monthly_generations=monthly_generations,
        monthly_ai_cost=f"{monthly_ai_cost:.2f}",
        monthly_render_cost=f"{monthly_render_cost:.2f}"
    )

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

@app.route("/admin/send-feedback-batch")
@login_required
def send_feedback_batch():
    if not current_user.is_admin:
        return redirect(url_for("index"))

    users = (
        db.session.query(User)
        .join(Generation, Generation.user_id == User.id)
        .filter(User.is_verified == True)
        .distinct()
        .all()
    )

    success_count = 0
    fail_count = 0

    for user in users:
        sent = send_feedback_email(user.email)
        if sent:
            success_count += 1
        else:
            fail_count += 1

    return f"Feedback batch complete. Sent: {success_count}, Failed: {fail_count}"

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


@app.route("/delete-account", methods=["POST"])
@login_required
def delete_account():
    user = User.query.get(current_user.id)

    if not user:
        return redirect(url_for("login"))

    try:
        user_id = user.id

        PromoRedemption.query.filter_by(user_id=user_id).update({"user_id": None})
        Payment.query.filter_by(user_id=user_id).update({"user_id": None})
        Generation.query.filter_by(user_id=user_id).delete()
        User.query.filter_by(id=user_id).delete()

        db.session.commit()

        logout_user()
        session.clear()

        return redirect(url_for("home"))

    except Exception as e:
        logging.error(f"Delete account error: {e}")
        db.session.rollback()
        session["listing"] = "There was a problem deleting your account. Please try again."
        return redirect(url_for("index"))

@app.route("/resend-verification", methods=["POST"])
@limiter.limit("5 per hour")
def resend_verification():
    email = request.form.get("email", "").lower().strip()
    normalized_email = normalize_email(email)

    user = User.query.filter_by(normalized_email=normalized_email).first()

    if not user:
        response = make_response(render_template(
            "login.html",
            error="Account not found.",
            email=email
        ))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    if user.is_verified:
        response = make_response(redirect(url_for("login")))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    send_verification_email(user.email)

    response = make_response(render_template(
        "login.html",
        message="Verification email resent. Please check your inbox.",
        email=email,
        show_resend=True
    ))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route("/buy-credits", methods=["POST"])
@login_required
def buy_credits():
    if not current_user.is_verified:
        session["listing"] = "Please verify your email before purchasing credits."
        return redirect(url_for("index"))

    pack_key = request.form.get("pack")
    pack = get_credit_pack(pack_key)

    if not pack:
        session["listing"] = "Invalid credit pack selected."
        return redirect(url_for("index"))

    if not pack["price_id"]:
        session["listing"] = "Payment option is not configured correctly."
        return redirect(url_for("index"))

    try:
        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price": pack["price_id"],
                    "quantity": 1,
                }
            ],
            success_url=url_for("index", _external=True) + "?payment=success",
            cancel_url=url_for("index", _external=True) + "?payment=cancelled",
            client_reference_id=str(current_user.id),
            customer_email=current_user.email,
            metadata={
                "user_id": str(current_user.id),
                "credits": str(pack["credits"]),
                "pack": str(pack_key),
            }
        )

        return redirect(checkout_session.url, code=303)

    except Exception as e:
        logging.error(f"Stripe checkout creation error: {e}")
        session["listing"] = "Could not start checkout. Please try again."
        return redirect(url_for("index"))
@app.route("/buy-credits")
@login_required
def buy_credits_page():
    return render_template("buy_credits.html")
    
@app.route("/stripe-webhook", methods=["POST"])
@csrf.exempt
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    webhook_secret = app.config["STRIPE_WEBHOOK_SECRET"]

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=webhook_secret
        )
    except ValueError:
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400

    event_type = event["type"]

    if event_type in ["checkout.session.completed", "checkout.session.async_payment_succeeded"]:
        checkout_session = event["data"]["object"]

        payment_status = checkout_session.get("payment_status")
        if payment_status != "paid":
            return "OK", 200

        stripe_session_id = checkout_session.get("id")

        existing_payment = Payment.query.filter_by(stripe_session_id=stripe_session_id).first()
        if existing_payment:
            return "OK", 200

        user_id = checkout_session.get("metadata", {}).get("user_id")
        credits = checkout_session.get("metadata", {}).get("credits")

        if not user_id or not credits:
            logging.error("Stripe webhook missing metadata.")
            return "Missing metadata", 400

        user = User.query.get(int(user_id))
        if not user:
            logging.error(f"Stripe webhook user not found: {user_id}")
            return "User not found", 400

        credits_to_add = int(credits)

        payment = Payment(
            user_id=user.id,
            stripe_session_id=stripe_session_id,
            stripe_payment_intent_id=checkout_session.get("payment_intent"),
            stripe_customer_email=checkout_session.get("customer_email"),
            amount_total=checkout_session.get("amount_total"),
            currency=checkout_session.get("currency"),
            credits_added=credits_to_add,
            status="completed"
        )

        user.credits += credits_to_add

        db.session.add(payment)
        db.session.commit()

    elif event_type == "checkout.session.async_payment_failed":
        checkout_session = event["data"]["object"]
        stripe_session_id = checkout_session.get("id")

        existing_payment = Payment.query.filter_by(stripe_session_id=stripe_session_id).first()
        if not existing_payment:
            user_id = checkout_session.get("metadata", {}).get("user_id")
            credits = checkout_session.get("metadata", {}).get("credits")

            if user_id and credits:
                payment = Payment(
                    user_id=int(user_id),
                    stripe_session_id=stripe_session_id,
                    stripe_payment_intent_id=checkout_session.get("payment_intent"),
                    stripe_customer_email=checkout_session.get("customer_email"),
                    amount_total=checkout_session.get("amount_total"),
                    currency=checkout_session.get("currency"),
                    credits_added=int(credits),
                    status="failed"
                )
                db.session.add(payment)
                db.session.commit()

    return "OK", 200
# -------------------------
# MAIN ROUTE
# -------------------------

@app.route("/generator", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute; 100 per hour; 400 per day")
def index():
    listing = session.pop("listing", None)

    if request.method == "POST":

        user = db.session.query(User).with_for_update().filter_by(id=current_user.id).first()

        if user.is_generating:
            session["listing"] = "Generation already in progress."
            return redirect(url_for("index"))

        if not user.is_admin and user.credits <= 0:
            session["listing"] = "You have no credits remaining."
            return redirect(url_for("index"))

        images = request.files.getlist("images")

        if not images or images[0].filename == "":
            session["listing"] = "Please upload at least one image."
            return redirect(url_for("index"))

        if len(images) > MAX_IMAGES:
            session["listing"] = f"Maximum {MAX_IMAGES} images allowed."
            return redirect(url_for("index"))

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

            session["listing"] = listing

        except Exception as e:
            logging.error(f"Generation error: {e}")
            db.session.rollback()

            user = User.query.get(current_user.id)
            if not user.is_admin:
                user.credits += 1
            db.session.commit()

            session["listing"] = "Error generating listing. Please try again."

        finally:
            user = User.query.get(current_user.id)
            user.is_generating = False
            db.session.commit()

        return redirect(url_for("index"))

    return render_template(
    "index.html",
    listing=listing,
    stripe_publishable_key=app.config["STRIPE_PUBLISHABLE_KEY"]
)


if __name__ == "__main__":
    app.run()
