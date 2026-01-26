# This file contains the WSGI configuration required to serve up your
# web application at http://KG6G.pythonanywhere.com/
# It works by setting the variable 'application' to a WSGI handler of some
# description.
#

import sys
import os

"""
PythonAnywhere WSGI configuration file.

This file tells PythonAnywhere how to load the Flask app.

You do NOT run this file directly.
You do NOT need to rename your app.py to flask_app.py.

We add the project directory to sys.path and point WSGI to the Flask app
inside app.py (imported as 'application').
"""

# Full path to your project folder
project_home = '/home/KG6G/ham-exam-results'

# Add the project directory to the sys.path if it's not already there
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Optionally set environment variable
os.environ['FLASK_ENV'] = 'production'

# Import the Flask app object as 'application' for WSGI to use
from app import app as application
