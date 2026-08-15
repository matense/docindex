"""Create a user (or the first admin) from the command line.

Usage: python create_admin.py [username] [email] [password]
Prompts for anything not given as arguments.
"""

import getpass
import sys

from app import create_app
from app.extensions import db
from app.models import User

app = create_app()

with app.app_context():
    username = sys.argv[1] if len(sys.argv) > 1 else input("Username: ").strip()
    email = sys.argv[2] if len(sys.argv) > 2 else input("Email: ").strip()
    password = sys.argv[3] if len(sys.argv) > 3 else getpass.getpass("Password: ")

    if User.query.filter((User.username == username) | (User.email == email)).first():
        print("Error: username or email already exists.")
        sys.exit(1)

    user = User(username=username, email=email, is_admin=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    print(f"Admin user '{username}' created.")
