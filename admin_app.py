from flask import redirect, request, url_for

from app import app


@app.before_request
def redirect_root_to_admin_login():
    if request.path == '/':
        return redirect(url_for('admin_login'))

    return None


if __name__ == '__main__':
    app.run(debug=True, port=5001)