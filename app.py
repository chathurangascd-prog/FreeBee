import os
import sqlite3
import csv
from datetime import datetime, timedelta, timezone
from functools import wraps
from io import StringIO, TextIOWrapper
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / 'static'
STORAGE_DIR = Path(os.environ.get('FREEBEE_STORAGE_DIR', str(BASE_DIR)))
DATABASE_PATH = Path(os.environ.get('FREEBEE_DB_PATH', str(STORAGE_DIR / 'fashion_store.db')))
UPLOADS_DIR = Path(os.environ.get('FREEBEE_UPLOADS_DIR', str(STORAGE_DIR / 'uploads')))
ALLOWED_IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
ACTIVITY_TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'
LOW_STOCK_THRESHOLD = 10

app.secret_key = os.environ.get('FREEBEE_SECRET_KEY') or os.environ.get('FLASK_SECRET_KEY', 'freebee-admin-dev')


def load_app_timezone():
    try:
        return ZoneInfo('Asia/Colombo')
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=5, minutes=30), name='Asia/Colombo')


APP_TIMEZONE = load_app_timezone()

SEED_PRODUCTS = [
    {
        'name': 'Golden Hour Dress',
        'category': 'Editorial Drop',
        'price': 89.00,
        'description': 'A clean statement silhouette with warm contrast styling for standout campaign looks.',
        'image_filename': 'uploads/front-view-smiley-woman-pointing-herself1.jpg',
        'accent_label': 'New',
        'featured': 1,
        'display_order': 1,
        'stock_quantity': 14,
        'stock_status': 'in_stock',
    },
    {
        'name': 'FreeBee Signature Tee',
        'category': 'Core Essential',
        'price': 42.00,
        'description': 'Minimal everyday styling with a premium dark-store presentation and sharp visual balance.',
        'image_filename': 'uploads/user.png',
        'accent_label': 'Best Seller',
        'featured': 1,
        'display_order': 2,
        'stock_quantity': 5,
        'stock_status': 'low_stock',
    },
    {
        'name': 'After Dark Set',
        'category': 'Night Edit',
        'price': 118.00,
        'description': 'Designed for bold evening looks with elevated structure and a polished runway feel.',
        'image_filename': 'uploads/front-view-smiley-woman-pointing-herself1.jpg',
        'accent_label': 'Limited',
        'featured': 0,
        'display_order': 3,
        'stock_quantity': 0,
        'stock_status': 'out_of_stock',
    },
    {
        'name': 'Street Muse Layer',
        'category': 'Weekend Select',
        'price': 64.00,
        'description': 'A relaxed layering piece curated for premium casual styling and strong product focus.',
        'image_filename': 'uploads/user.png',
        'accent_label': 'Featured',
        'featured': 0,
        'display_order': 4,
        'stock_quantity': 9,
        'stock_status': 'in_stock',
    },
]

STOCK_STATUS_OPTIONS = {'in_stock', 'low_stock', 'out_of_stock'}


def get_db_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def get_current_timestamp():
    return datetime.now(APP_TIMEZONE).isoformat(timespec='seconds')


def parse_activity_timestamp(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed = datetime.strptime(value, ACTIVITY_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(APP_TIMEZONE)


def serialize_activity_row(row):
    activity = dict(row)
    parsed_timestamp = parse_activity_timestamp(activity.get('created_at'))

    if parsed_timestamp is None:
        activity['created_at_display'] = activity.get('created_at', '')
        activity['created_at_iso'] = activity.get('created_at', '')
        return activity

    activity['created_at_display'] = parsed_timestamp.strftime(ACTIVITY_TIMESTAMP_FORMAT)
    activity['created_at_iso'] = parsed_timestamp.isoformat(timespec='seconds')
    activity['created_at'] = activity['created_at_display']
    return activity


def init_db():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    connection = get_db_connection()
    connection.execute(
        '''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        '''
    )
    connection.execute(
        '''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price REAL NOT NULL,
            description TEXT NOT NULL,
            image_filename TEXT NOT NULL,
            accent_label TEXT NOT NULL,
            featured INTEGER NOT NULL DEFAULT 0,
            display_order INTEGER NOT NULL DEFAULT 0,
            stock_quantity INTEGER NOT NULL DEFAULT 0,
            stock_status TEXT NOT NULL DEFAULT 'in_stock'
        )
        '''
    )
    connection.execute(
        '''
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        '''
    )

    existing_columns = {
        row['name']
        for row in connection.execute('PRAGMA table_info(products)').fetchall()
    }

    if 'featured' not in existing_columns:
        connection.execute('ALTER TABLE products ADD COLUMN featured INTEGER NOT NULL DEFAULT 0')

    if 'display_order' not in existing_columns:
        connection.execute('ALTER TABLE products ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0')

    if 'stock_quantity' not in existing_columns:
        connection.execute('ALTER TABLE products ADD COLUMN stock_quantity INTEGER NOT NULL DEFAULT 0')

    if 'stock_status' not in existing_columns:
        connection.execute("ALTER TABLE products ADD COLUMN stock_status TEXT NOT NULL DEFAULT 'in_stock'")

    product_count = connection.execute('SELECT COUNT(*) FROM products').fetchone()[0]

    if product_count == 0:
        connection.executemany(
            '''
            INSERT INTO products (name, category, price, description, image_filename, accent_label, featured, display_order, stock_quantity, stock_status)
            VALUES (:name, :category, :price, :description, :image_filename, :accent_label, :featured, :display_order, :stock_quantity, :stock_status)
            ''',
            SEED_PRODUCTS,
        )

    rows_needing_order = connection.execute(
        'SELECT id FROM products WHERE display_order = 0 OR display_order IS NULL ORDER BY id ASC'
    ).fetchall()

    if rows_needing_order:
        for index, row in enumerate(rows_needing_order, start=1):
            connection.execute(
                'UPDATE products SET display_order = ? WHERE id = ?',
                (index, row['id']),
            )

    connection.execute(
        'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
        ('admin_password_hash', generate_password_hash('freebee123')),
    )

    connection.commit()
    connection.close()


def log_activity(action, details):
    connection = get_db_connection()
    connection.execute(
        'INSERT INTO activity_logs (action, details, created_at) VALUES (?, ?, ?)',
        (action, details, get_current_timestamp()),
    )
    connection.commit()
    connection.close()


def get_recent_activity(limit=8):
    connection = get_db_connection()
    rows = connection.execute(
        '''
        SELECT id, action, details, created_at
        FROM activity_logs
        ORDER BY id DESC
        LIMIT ?
        ''',
        (limit,),
    ).fetchall()
    connection.close()
    return [serialize_activity_row(row) for row in rows]


def get_activity_count():
    connection = get_db_connection()
    count = connection.execute('SELECT COUNT(*) FROM activity_logs').fetchone()[0]
    connection.close()
    return count


def get_setting(key):
    connection = get_db_connection()
    row = connection.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    connection.close()
    return row['value'] if row else None


def set_setting(key, value):
    connection = get_db_connection()
    connection.execute(
        '''
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        ''',
        (key, value),
    )
    connection.commit()
    connection.close()


def get_next_display_order():
    connection = get_db_connection()
    current_max = connection.execute('SELECT MAX(display_order) FROM products').fetchone()[0] or 0
    connection.close()
    return current_max + 1


def normalize_stock_status(stock_quantity):
    quantity = int(stock_quantity)

    if quantity <= 0:
        return 'out_of_stock'

    if quantity < LOW_STOCK_THRESHOLD:
        return 'low_stock'

    return 'in_stock'


def serialize_product_row(row):
    product = dict(row)
    product['stock_quantity'] = int(product.get('stock_quantity', 0) or 0)
    product['stock_status'] = normalize_stock_status(product['stock_quantity'])
    return product


def get_products(limit=None):
    connection = get_db_connection()
    query = '''
        SELECT id, name, category, price, description, image_filename, accent_label, featured, display_order, stock_quantity, stock_status
        FROM products
        ORDER BY display_order ASC, featured DESC, id DESC
    '''

    parameters = ()
    if limit is not None:
        query += ' LIMIT ?'
        parameters = (limit,)

    rows = connection.execute(query, parameters).fetchall()
    connection.close()
    return [serialize_product_row(row) for row in rows]


def get_product(product_id):
    connection = get_db_connection()
    row = connection.execute(
        '''
        SELECT id, name, category, price, description, image_filename, accent_label, featured, display_order, stock_quantity, stock_status
        FROM products
        WHERE id = ?
        ''',
        (product_id,),
    ).fetchone()
    connection.close()
    return serialize_product_row(row) if row else None


def create_product(product_data):
    stock_quantity = int(product_data.get('stock_quantity', 0))
    payload = {
        'featured': int(product_data.get('featured', 0)),
        'display_order': product_data.get('display_order', get_next_display_order()),
        'stock_quantity': stock_quantity,
        'stock_status': normalize_stock_status(stock_quantity),
        **product_data,
    }
    payload['stock_quantity'] = stock_quantity
    payload['stock_status'] = normalize_stock_status(stock_quantity)
    connection = get_db_connection()
    connection.execute(
        '''
        INSERT INTO products (name, category, price, description, image_filename, accent_label, featured, display_order, stock_quantity, stock_status)
        VALUES (:name, :category, :price, :description, :image_filename, :accent_label, :featured, :display_order, :stock_quantity, :stock_status)
        ''',
        payload,
    )
    connection.commit()
    connection.close()


def update_product(product_id, product_data):
    stock_quantity = int(product_data.get('stock_quantity', 0))
    connection = get_db_connection()
    connection.execute(
        '''
        UPDATE products
        SET name = :name,
            category = :category,
            price = :price,
            description = :description,
            image_filename = :image_filename,
            accent_label = :accent_label,
            stock_quantity = :stock_quantity,
            stock_status = :stock_status
        WHERE id = :id
        ''',
        {
            'id': product_id,
            **product_data,
            'stock_quantity': stock_quantity,
            'stock_status': normalize_stock_status(stock_quantity),
        },
    )
    connection.commit()
    connection.close()


def delete_product(product_id):
    connection = get_db_connection()
    connection.execute('DELETE FROM products WHERE id = ?', (product_id,))
    connection.commit()
    connection.close()


def set_products_order(ordered_ids):
    connection = get_db_connection()
    for index, product_id in enumerate(ordered_ids, start=1):
        connection.execute(
            'UPDATE products SET display_order = ? WHERE id = ?',
            (index, product_id),
        )
    connection.commit()
    connection.close()


def set_product_featured(product_id, featured):
    connection = get_db_connection()
    connection.execute(
        'UPDATE products SET featured = ? WHERE id = ?',
        (1 if featured else 0, product_id),
    )
    connection.commit()
    connection.close()


def get_image_options():
    image_paths = []
    seen_paths = set()

    for file_path in sorted(STATIC_DIR.rglob('*')):
        if file_path.is_file() and file_path.suffix.lower() in ALLOWED_IMAGE_SUFFIXES:
            relative_path = file_path.relative_to(STATIC_DIR).as_posix()
            if relative_path not in seen_paths:
                image_paths.append(relative_path)
                seen_paths.add(relative_path)

    for file_path in sorted(UPLOADS_DIR.rglob('*')):
        if file_path.is_file() and file_path.suffix.lower() in ALLOWED_IMAGE_SUFFIXES:
            relative_path = f"media/{file_path.relative_to(UPLOADS_DIR).as_posix()}"
            if relative_path not in seen_paths:
                image_paths.append(relative_path)
                seen_paths.add(relative_path)

    return image_paths


def build_unique_upload_name(filename):
    sanitized = secure_filename(filename)
    stem = Path(sanitized).stem or 'upload'
    suffix = Path(sanitized).suffix.lower()
    candidate = f'{stem}{suffix}'
    counter = 1

    while (UPLOADS_DIR / candidate).exists():
        candidate = f'{stem}-{counter}{suffix}'
        counter += 1

    return candidate


def save_uploaded_image(uploaded_file):
    if uploaded_file is None or uploaded_file.filename == '':
        return None

    suffix = Path(uploaded_file.filename).suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise ValueError('Uploaded image must be PNG, JPG, JPEG, GIF, or WEBP.')

    filename = build_unique_upload_name(uploaded_file.filename)
    uploaded_file.save(UPLOADS_DIR / filename)
    return f'media/{filename}'


def resolve_asset_url(path):
    if not path:
        return url_for('static', filename='uploads/user.png')

    if path.startswith(('http://', 'https://')):
        return path

    normalized_path = path.lstrip('/')

    if normalized_path.startswith('media/'):
        return url_for('uploaded_media', filename=normalized_path.removeprefix('media/'))

    return url_for('static', filename=normalized_path)


@app.context_processor
def inject_asset_helpers():
    return {
        'asset_url': resolve_asset_url,
        'media_base_url': url_for('uploaded_media', filename=''),
    }


def parse_product_form(form_data, uploaded_file=None):
    image_options = get_image_options()
    product = {
        'name': form_data.get('name', '').strip(),
        'category': form_data.get('category', '').strip(),
        'description': form_data.get('description', '').strip(),
        'image_filename': form_data.get('image_filename', '').strip(),
        'accent_label': form_data.get('accent_label', '').strip(),
        'stock_status': form_data.get('stock_status', 'in_stock').strip(),
    }

    price_value = form_data.get('price', '').strip()
    stock_quantity_value = form_data.get('stock_quantity', '').strip()
    if not all(product.values()) or not price_value or not stock_quantity_value:
        raise ValueError('Please fill in all product fields.')

    try:
        product['price'] = float(price_value)
    except ValueError as error:
        raise ValueError('Price must be a valid number.') from error

    if product['price'] < 0:
        raise ValueError('Price must be zero or greater.')

    try:
        product['stock_quantity'] = int(stock_quantity_value)
    except ValueError as error:
        raise ValueError('Stock quantity must be a whole number.') from error

    if product['stock_quantity'] < 0:
        raise ValueError('Stock quantity must be zero or greater.')

    if product['stock_status'] not in STOCK_STATUS_OPTIONS:
        raise ValueError('Please choose a valid stock status.')

    product['stock_status'] = normalize_stock_status(product['stock_quantity'])

    uploaded_image_path = save_uploaded_image(uploaded_file)
    if uploaded_image_path is not None:
        product['image_filename'] = uploaded_image_path
        image_options = get_image_options()

    if product['image_filename'] not in image_options:
        raise ValueError('Please choose a valid image from the available options.')

    return product


def get_product_metrics():
    connection = get_db_connection()
    total_products = connection.execute('SELECT COUNT(*) FROM products').fetchone()[0]
    total_categories = connection.execute('SELECT COUNT(DISTINCT category) FROM products').fetchone()[0]
    average_price = connection.execute('SELECT AVG(price) FROM products').fetchone()[0] or 0
    connection.close()

    return {
        'total_products': total_products,
        'total_categories': total_categories,
        'average_price': round(average_price, 2),
    }


def build_admin_summary(products):
    total_products = len(products)
    featured_count = sum(1 for product in products if product.get('featured'))
    categories = {product['category'] for product in products}
    average_price = round(
        sum(product['price'] for product in products) / total_products,
        2,
    ) if total_products else 0
    low_stock_count = sum(1 for product in products if product.get('stock_status') == 'low_stock')
    out_of_stock_count = sum(1 for product in products if product.get('stock_status') == 'out_of_stock')

    return {
        'total_products': total_products,
        'featured_count': featured_count,
        'category_count': len(categories),
        'average_price': average_price,
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'activity_count': get_activity_count(),
    }


def is_external_admin_password_configured():
    return bool(os.environ.get('FREEBEE_ADMIN_PASSWORD'))


def verify_admin_password(password):
    external_password = os.environ.get('FREEBEE_ADMIN_PASSWORD')

    if external_password:
        return password == external_password

    stored_hash = get_setting('admin_password_hash')
    return bool(stored_hash and check_password_hash(stored_hash, password))


def update_admin_password(password):
    set_setting('admin_password_hash', generate_password_hash(password))


def is_admin_authenticated():
    return bool(session.get('admin_authenticated'))


def require_admin(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not is_admin_authenticated():
            flash('Please log in to access admin tools.', 'error')
            return redirect(url_for('admin_login'))
        return view_func(*args, **kwargs)

    return wrapper


init_db()


@app.route('/')
def home():
    products = get_products(limit=4)
    metrics = get_product_metrics()
    return render_template('index.html', products=products, metrics=metrics)


@app.route('/media/<path:filename>')
def uploaded_media(filename):
    return send_from_directory(UPLOADS_DIR, filename)


@app.route('/admin')
def admin():
    authenticated = is_admin_authenticated()

    if not authenticated:
        return redirect(url_for('admin_login'))

    search_query = request.args.get('q', '').strip()
    featured_filter = request.args.get('featured', 'all').strip() or 'all'
    all_products = get_products()

    if search_query:
        lowered_query = search_query.lower()
        all_products = [
            product for product in all_products
            if lowered_query in product['name'].lower()
            or lowered_query in product['category'].lower()
            or lowered_query in product['accent_label'].lower()
        ]

    if featured_filter in {'featured', 'regular'}:
        want_featured = featured_filter == 'featured'
        all_products = [product for product in all_products if bool(product['featured']) is want_featured]

    image_options = get_image_options()
    last_deleted_product = session.get('last_deleted_product')
    recent_activity = get_recent_activity()
    admin_summary = build_admin_summary(all_products)
    return render_template(
        'admin.html',
        all_products=all_products,
        image_options=image_options,
        last_deleted_product=last_deleted_product,
        admin_authenticated=True,
        admin_summary=admin_summary,
        search_query=search_query,
        featured_filter=featured_filter,
        password_managed_externally=is_external_admin_password_configured(),
        recent_activity=recent_activity,
    )


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'GET':
        if is_admin_authenticated():
            return redirect(url_for('admin', _anchor='admin-panel'))

        return render_template(
            'admin_login.html',
            password_managed_externally=is_external_admin_password_configured(),
        )

    password = request.form.get('password', '')

    if not verify_admin_password(password):
        flash('Incorrect admin password.', 'error')
        return redirect(url_for('admin_login'))

    session['admin_authenticated'] = True
    log_activity('admin_login', 'Admin logged in successfully')
    flash('Admin login successful.', 'success')
    return redirect(url_for('admin', _anchor='admin-panel'))


@app.route('/admin/logout', methods=['POST'])
@require_admin
def admin_logout():
    session.pop('admin_authenticated', None)
    log_activity('admin_logout', 'Admin logged out')
    flash('Logged out from admin.', 'success')
    return redirect(url_for('admin_login'))


@app.route('/admin/settings/password', methods=['POST'])
@require_admin
def change_admin_password():
    if is_external_admin_password_configured():
        flash('Password changes are disabled while FREEBEE_ADMIN_PASSWORD is configured.', 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    if not verify_admin_password(current_password):
        flash('Current password is incorrect.', 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    if len(new_password) < 6:
        flash('New password must be at least 6 characters long.', 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    if new_password != confirm_password:
        flash('New password and confirmation do not match.', 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    update_admin_password(new_password)
    log_activity('password_change', 'Admin password was updated')
    flash('Admin password updated successfully.', 'success')
    return redirect(url_for('admin', _anchor='admin-panel'))


@app.route('/admin/products', methods=['POST'])
@require_admin
def add_product():
    try:
        product = parse_product_form(request.form, request.files.get('upload_image'))
    except ValueError as error:
        flash(str(error), 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    create_product(product)
    log_activity('product_create', f"Created product: {product['name']}")
    session.pop('last_deleted_product', None)
    flash('Product added successfully.', 'success')
    return redirect(url_for('admin', _anchor='admin-panel'))


@app.route('/admin/products/<int:product_id>', methods=['POST'])
@require_admin
def edit_product(product_id):
    existing_product = get_product(product_id)
    if existing_product is None:
        flash('Product not found.', 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    try:
        product = parse_product_form(request.form, request.files.get('upload_image'))
    except ValueError as error:
        flash(str(error), 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    update_product(product_id, product)
    log_activity('product_update', f"Updated product: {existing_product['name']}")
    session.pop('last_deleted_product', None)
    flash('Product updated successfully.', 'success')
    return redirect(url_for('admin', _anchor='admin-panel'))


@app.route('/admin/products/<int:product_id>/delete', methods=['POST'])
@require_admin
def remove_product(product_id):
    product = get_product(product_id)
    if product is None:
        flash('Product not found.', 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    session['last_deleted_product'] = product
    delete_product(product_id)
    log_activity('product_delete', f"Deleted product: {product['name']}")
    flash('Product deleted. You can undo it from the admin page.', 'success')
    return redirect(url_for('admin', _anchor='admin-panel'))


@app.route('/admin/products/undo-delete', methods=['POST'])
@require_admin
def undo_delete_product():
    product = session.pop('last_deleted_product', None)

    if product is None:
        flash('There is no deleted product to restore.', 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    create_product({
        'name': product['name'],
        'category': product['category'],
        'price': product['price'],
        'description': product['description'],
        'image_filename': product['image_filename'],
        'accent_label': product['accent_label'],
        'featured': product.get('featured', 0),
        'display_order': product.get('display_order', get_next_display_order()),
        'stock_quantity': product.get('stock_quantity', 0),
        'stock_status': product.get('stock_status', 'in_stock'),
    })
    log_activity('product_restore', f"Restored deleted product: {product['name']}")
    flash('Deleted product restored successfully.', 'success')
    return redirect(url_for('admin', _anchor='admin-panel'))


@app.route('/admin/products/<int:product_id>/toggle-featured', methods=['POST'])
@require_admin
def toggle_featured_product(product_id):
    product = get_product(product_id)

    if product is None:
        flash('Product not found.', 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    next_featured = 0 if product['featured'] else 1
    set_product_featured(product_id, next_featured)
    log_activity('product_featured', f"{'Marked featured' if next_featured else 'Removed featured'}: {product['name']}")
    flash('Product feature status updated.', 'success')
    return redirect(url_for('admin', _anchor='admin-panel'))


@app.route('/admin/products/reorder', methods=['POST'])
@require_admin
def reorder_products():
    payload = request.get_json(silent=True) or {}
    ordered_ids = payload.get('ordered_ids', [])

    if not ordered_ids or not all(isinstance(product_id, int) for product_id in ordered_ids):
        return jsonify({'ok': False, 'message': 'Invalid order payload.'}), 400

    existing_ids = {product['id'] for product in get_products()}
    if set(ordered_ids) != existing_ids:
        return jsonify({'ok': False, 'message': 'Order payload does not match current products.'}), 400

    set_products_order(ordered_ids)
    log_activity('product_reorder', f'Saved product order for {len(ordered_ids)} items')
    return jsonify({'ok': True})


@app.route('/admin/products/export', methods=['GET'])
@require_admin
def export_products_csv():
    products = get_products()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'name', 'category', 'price', 'description', 'image_filename', 'accent_label',
        'featured', 'display_order', 'stock_quantity', 'stock_status'
    ])

    for product in products:
        writer.writerow([
            product['name'],
            product['category'],
            product['price'],
            product['description'],
            product['image_filename'],
            product['accent_label'],
            product['featured'],
            product['display_order'],
            product['stock_quantity'],
            product['stock_status'],
        ])

    log_activity('csv_export', f'Exported {len(products)} products to CSV')
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=freebee-products.csv'},
    )


@app.route('/admin/products/import', methods=['POST'])
@require_admin
def import_products_csv():
    uploaded_file = request.files.get('csv_file')

    if uploaded_file is None or uploaded_file.filename == '':
        flash('Please choose a CSV file to import.', 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    if Path(uploaded_file.filename).suffix.lower() != '.csv':
        flash('Import file must be a CSV.', 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    stream = TextIOWrapper(uploaded_file.stream, encoding='utf-8', newline='')
    reader = csv.DictReader(stream)
    imported_count = 0

    try:
        for row in reader:
            if not any((value or '').strip() for value in row.values()):
                continue

            product_data = {
                'name': (row.get('name') or '').strip(),
                'category': (row.get('category') or '').strip(),
                'price': (row.get('price') or '').strip(),
                'description': (row.get('description') or '').strip(),
                'image_filename': (row.get('image_filename') or '').strip(),
                'accent_label': (row.get('accent_label') or '').strip(),
                'stock_quantity': (row.get('stock_quantity') or '0').strip(),
                'stock_status': (row.get('stock_status') or 'in_stock').strip(),
            }
            parsed_product = parse_product_form(product_data)
            parsed_product['featured'] = 1 if (row.get('featured') or '0').strip() in {'1', 'true', 'True', 'yes'} else 0

            display_order_value = (row.get('display_order') or '').strip()
            parsed_product['display_order'] = int(display_order_value) if display_order_value else get_next_display_order()

            create_product(parsed_product)
            imported_count += 1
    except (ValueError, TypeError) as error:
        flash(f'CSV import failed: {error}', 'error')
        return redirect(url_for('admin', _anchor='admin-panel'))

    log_activity('csv_import', f'Imported {imported_count} products from CSV')
    flash(f'Imported {imported_count} products from CSV.', 'success')
    return redirect(url_for('admin', _anchor='admin-panel'))

if __name__ == '__main__':
    host = os.environ.get('FREEBEE_HOST', '0.0.0.0' if os.environ.get('PORT') else '127.0.0.1')
    port = int(os.environ.get('PORT', os.environ.get('FREEBEE_PORT', 5000)))
    debug_setting = os.environ.get('FLASK_DEBUG')
    debug = (not os.environ.get('PORT')) if debug_setting is None else debug_setting.lower() in {'1', 'true', 'yes', 'on'}

    print(f'Home URL: http://{host}:{port}/', flush=True)
    print(f'Admin URL: http://{host}:{port}/admin/login', flush=True)

    app.run(host=host, port=port, debug=debug)