"""Create a user (or the first admin) from the command line.

Usage: python create_admin.py [username] [email] [password]
Prompts for anything not given as arguments.
"""

import getpass
import sys

from app import create_app
from app.extensions import db
from app.models import User


def main():
    app = create_app()

    with app.app_context():
        username = sys.argv[1] if len(sys.argv) > 1 else input("Username: ").strip()
        email = sys.argv[2] if len(sys.argv) > 2 else input("Email: ").strip()
        # Never take the password from argv (shell history/ps leak) — prompt it.
        if len(sys.argv) > 3:
            print("Warning: ignoring password from command line (use the prompt).")
        password = getpass.getpass("Password: ")

        if not username or len(username) < 3 or len(username) > 64:
            print("Error: username must be 3-64 characters.")
            sys.exit(1)
        if "@" not in email or len(email) > 120:
            print("Error: invalid email.")
            sys.exit(1)
        if len(password) < 8:
            print("Error: password must be at least 8 characters.")
            sys.exit(1)

        if User.query.filter((User.username == username) | (User.email == email)).first():
            print("Error: username or email already exists.")
            sys.exit(1)

        from sqlalchemy.exc import IntegrityError
        user = User(username=username, email=email, is_admin=True)
        user.set_password(password)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            print("Error: username or email already exists (race).")
            sys.exit(1)
        print(f"Admin user '{username}' created.")


if __name__ == "__main__":
    main()
