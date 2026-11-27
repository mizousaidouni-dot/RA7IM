import os
import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from functools import wraps

from cryptography.fernet import Fernet
from flask import (
    Flask, request, redirect, url_for, session, render_template,
    send_from_directory, flash, abort, g, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# ------------------------------------------------------------------ المسارات
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ------------------------------------------------------------------ المفتاح السري
# إن لم تضع SECRET_KEY في المتغيرات، يُنشأ مفتاح عشوائي ويُحفظ داخل مجلد البيانات.
def load_secret_key():
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    key_file = os.path.join(DATA_DIR, ".secret_key")
    if os.path.exists(key_file):
        with open(key_file) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(key_file, "w") as f:
        f.write(key)
    return key

SECRET_KEY = load_secret_key()
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SQLALCHEMY_DATABASE_URI=f"sqlite:///{os.path.join(DATA_DIR, 'ra7_stor.db')}",
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    UPLOAD_FOLDER=UPLOAD_FOLDER,
    MAX_CONTENT_LENGTH=24 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
db = SQLAlchemy(app)

# ------------------------------------------------------------------ تشفير الرسائل
cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()))

def encrypt_msg(text):
    return cipher.encrypt(text.encode("utf-8")).decode("utf-8")

def decrypt_msg(token):
    try:
        return cipher.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        return "[رسالة مشفرة غير قابلة للقراءة]"

# ------------------------------------------------------------------ الألوان والتصنيفات
THEMES = [
    ("gold", "ذهبي", "#d4af37"),
    ("emerald", "زمردي", "#2fbf8f"),
    ("blue", "أزرق ملكي", "#4aa8ff"),
    ("rose", "وردي", "#f06292"),
    ("violet", "بنفسجي", "#a78bfa"),
    ("crimson", "ياقوتي", "#e5484d"),
    ("ivory", "عاجي فاتح", "#a87a12"),
]
THEME_KEYS = [t[0] for t in THEMES]
CATEGORIES = ["إلكترونيات", "ملابس", "ألعاب وحسابات", "سيارات", "عقارات", "أخرى"]
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_IMAGES = 6

# ------------------------------------------------------------------ النماذج
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_banned = db.Column(db.Boolean, default=False)
    theme = db.Column(db.String(20), default="gold")
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def is_online(self):
        return bool(self.last_seen) and (datetime.utcnow() - self.last_seen) < timedelta(minutes=2)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False)
    price = db.Column(db.String(50), nullable=False)
    category = db.Column(db.String(50), default="أخرى")
    images = db.Column(db.Text, nullable=False)
    is_sold = db.Column(db.Boolean, default=False)
    seller_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    seller = db.relationship("User", backref="products")
    comments = db.relationship("Comment", backref="product", cascade="all, delete-orphan",
                               order_by="Comment.created_at.asc()")

    @property
    def image_list(self):
        return [i for i in self.images.split(",") if i]


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship("User")


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=True)
    encrypted_text = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sender = db.relationship("User", foreign_keys=[sender_id])
    receiver = db.relationship("User", foreign_keys=[receiver_id])


with app.app_context():
    db.create_all()
    admin_user = os.environ.get("ADMIN_USER", "admin")
    admin_pass = os.environ.get("ADMIN_PASS", "admin123")
    if not User.query.filter_by(username=admin_user).first():
        db.session.add(User(username=admin_user, password_hash=generate_password_hash(admin_pass),
                            is_admin=True, theme="gold"))
        db.session.commit()

# ------------------------------------------------------------------ الجلسة والحماية
def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    user = db.session.get(User, uid)
    if not user or user.is_banned:
        session.clear()
        return None
    return user


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def before():
    if request.endpoint == "static":
        return
    g.user = get_current_user()
    if request.method == "POST":
        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        if not secrets.compare_digest(token, session.get("csrf", "")):
            abort(400)
    if g.user and (not g.user.last_seen or (datetime.utcnow() - g.user.last_seen).total_seconds() > 30):
        g.user.last_seen = datetime.utcnow()
        db.session.commit()


@app.context_processor
def inject():
    user = getattr(g, "user", None)
    unread = 0
    if user:
        unread = Message.query.filter_by(receiver_id=user.id, is_read=False).count()
    return dict(user=user, csrf_token=csrf_token, themes=THEMES, categories=CATEGORIES,
                unread_count=unread, current_theme=user.theme if user else "gold")


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            flash("سجّل الدخول أولاً للمتابعة.")
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if not g.user.is_admin:
            abort(403)
        return f(*a, **kw)
    return wrapper


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def error_page(e):
    msgs = {
        400: "انتهت صلاحية الصفحة. أعد تحميلها وحاول مرة أخرى.",
        403: "لا تملك صلاحية الوصول إلى هذه الصفحة.",
        404: "الصفحة التي تبحث عنها غير موجودة.",
        413: "حجم الملفات كبير جداً. الحد الأقصى 24 ميغابايت.",
    }
    return render_template("error.html", code=e.code, message=msgs.get(e.code, "حدث خطأ.")), e.code

# ------------------------------------------------------------------ الصفحات
@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/theme/<name>", methods=["POST"])
@login_required
def set_theme(name):
    if name not in THEME_KEYS:
        abort(404)
    g.user.theme = name
    db.session.commit()
    return jsonify(ok=True, theme=name)


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    cat = request.args.get("cat", "").strip()
    query = Product.query.order_by(Product.is_sold.asc(), Product.created_at.desc())
    if q:
        query = query.filter(Product.title.contains(q) | Product.description.contains(q))
    if cat:
        query = query.filter_by(category=cat)
    return render_template("index.html", products=query.all(), q=q, cat=cat)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if len(username) < 3 or len(password) < 6:
            flash("اسم المستخدم 3 أحرف على الأقل وكلمة المرور 6 أحرف على الأقل.")
        elif User.query.filter_by(username=username).first():
            flash("اسم المستخدم مستخدم بالفعل، اختر اسماً آخر.")
        else:
            u = User(username=username, password_hash=generate_password_hash(password))
            db.session.add(u)
            db.session.commit()
            session.clear()
            session["user_id"] = u.id
            session.permanent = True
            flash("أهلاً بك في RA7 STOR! تم إنشاء حسابك.")
            return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        u = User.query.filter_by(username=username).first()
        if u and check_password_hash(u.password_hash, password):
            if u.is_banned:
                flash("هذا الحساب محظور من قبل إدارة المتجر.")
                return redirect(url_for("login"))
            session.clear()
            session["user_id"] = u.id
            session.permanent = True
            return redirect(url_for("index"))
        flash("اسم المستخدم أو كلمة المرور غير صحيحة.")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        old = request.form.get("old_password", "")
        new = request.form.get("new_password", "")
        if not check_password_hash(g.user.password_hash, old):
            flash("كلمة المرور الحالية غير صحيحة.")
        elif len(new) < 6:
            flash("كلمة المرور الجديدة يجب ألا تقل عن 6 أحرف.")
        else:
            g.user.password_hash = generate_password_hash(new)
            db.session.commit()
            flash("تم تغيير كلمة المرور.")
        return redirect(url_for("settings"))
    return render_template("settings.html")


@app.route("/add", methods=["GET", "POST"])
@login_required
def add_product():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        price = request.form.get("price", "").strip()
        category = request.form.get("category", "أخرى")
        description = request.form.get("description", "").strip()
        saved = []
        for f in request.files.getlist("images")[:MAX_IMAGES]:
            if not f or not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in ALLOWED_EXT:
                continue
            name = f"{uuid.uuid4().hex}{ext}"
            f.save(os.path.join(UPLOAD_FOLDER, name))
            saved.append(name)
        if not title or not price or not description:
            flash("أكمل اسم المنتج والسعر والوصف.")
        elif not saved:
            flash("أضف صورة واحدة على الأقل بصيغة JPG أو PNG أو WEBP.")
        else:
            p = Product(title=title, price=price, category=category if category in CATEGORIES else "أخرى",
                        description=description, images=",".join(saved), seller_id=g.user.id)
            db.session.add(p)
            db.session.commit()
            flash("تم نشر المنتج.")
            return redirect(url_for("product_detail", pid=p.id))
    return render_template("add_product.html")


@app.route("/product/<int:pid>", methods=["GET", "POST"])
def product_detail(pid):
    product = db.session.get(Product, pid) or abort(404)
    if request.method == "POST":
        if not g.user:
            return redirect(url_for("login"))
        text = request.form.get("comment", "").strip()
        if text:
            db.session.add(Comment(content=text[:1000], product_id=pid, user_id=g.user.id))
            db.session.commit()
        return redirect(url_for("product_detail", pid=pid) + "#comments")
    return render_template("product.html", product=product)


@app.route("/toggle_sold/<int:pid>", methods=["POST"])
@login_required
def toggle_sold(pid):
    p = db.session.get(Product, pid) or abort(404)
    if g.user.id == p.seller_id or g.user.is_admin:
        p.is_sold = not p.is_sold
        db.session.commit()
    return redirect(url_for("product_detail", pid=pid))


@app.route("/delete_product/<int:pid>", methods=["POST"])
@login_required
def delete_product(pid):
    p = db.session.get(Product, pid) or abort(404)
    if g.user.id == p.seller_id or g.user.is_admin:
        for img in p.image_list:
            try:
                os.remove(os.path.join(UPLOAD_FOLDER, os.path.basename(img)))
            except OSError:
                pass
        Message.query.filter_by(product_id=p.id).update({"product_id": None})
        db.session.delete(p)
        db.session.commit()
        flash("تم حذف المنتج.")
    return redirect(url_for("index"))

# ------------------------------------------------------------------ المحادثات المشفرة
@app.route("/inbox")
@login_required
def inbox():
    u = g.user
    msgs = (Message.query.filter((Message.sender_id == u.id) | (Message.receiver_id == u.id))
            .order_by(Message.created_at.desc()).all())
    partners = {}
    for m in msgs:
        other = m.receiver if m.sender_id == u.id else m.sender
        if other.id not in partners:
            partners[other.id] = {"user": other, "last": decrypt_msg(m.encrypted_text),
                                  "time": m.created_at.strftime("%Y-%m-%d %H:%M"), "unread": 0}
        if m.receiver_id == u.id and not m.is_read:
            partners[other.id]["unread"] += 1
    return render_template("inbox.html", partners=list(partners.values()))


@app.route("/chat/<int:other_id>", methods=["GET", "POST"])
@login_required
def chat(other_id):
    u = g.user
    if u.id == other_id:
        return redirect(url_for("inbox"))
    other = db.session.get(User, other_id) or abort(404)
    if request.method == "POST":
        text = request.form.get("message", "").strip()
        if text:
            pid = request.args.get("product", type=int)
            db.session.add(Message(sender_id=u.id, receiver_id=other.id, product_id=pid,
                                   encrypted_text=encrypt_msg(text[:2000])))
            db.session.commit()
        return redirect(url_for("chat", other_id=other_id))
    raw = (Message.query.filter(
        ((Message.sender_id == u.id) & (Message.receiver_id == other.id)) |
        ((Message.sender_id == other.id) & (Message.receiver_id == u.id)))
        .order_by(Message.created_at.asc()).all())
    changed = False
    for m in raw:
        if m.receiver_id == u.id and not m.is_read:
            m.is_read = True
            changed = True
    if changed:
        db.session.commit()
    messages = [{"is_me": m.sender_id == u.id, "text": decrypt_msg(m.encrypted_text),
                 "time": m.created_at.strftime("%H:%M")} for m in raw]
    return render_template("chat.html", other=other, messages=messages)

# ------------------------------------------------------------------ لوحة المالك
@app.route("/admin")
@admin_required
def admin_panel():
    return render_template("admin.html", users=User.query.order_by(User.created_at.desc()).all(),
                           product_count=Product.query.count(), message_count=Message.query.count())


@app.route("/admin/ban/<int:uid>", methods=["POST"])
@admin_required
def toggle_ban(uid):
    target = db.session.get(User, uid) or abort(404)
    if not target.is_admin:
        target.is_banned = not target.is_banned
        db.session.commit()
    return redirect(url_for("admin_panel"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
